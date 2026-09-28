"""Backup pipeline behaviour against a real PostgreSQL and a real S3 server
with Object Lock. The letters match the failure matrix in docs/RUNBOOK.md."""

from __future__ import annotations

import datetime as dt
import fcntl
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest
from botocore.exceptions import ClientError
from conftest import SRC, fingerprint, fresh_database, psql, run_cli, s3_admin

import errs_offsite as eo


def events(output: str) -> list[dict[str, Any]]:
    out = []
    for line in output.splitlines():
        line = line.strip()
        if line.startswith("{"):
            out.append(json.loads(line))
    return out


def named(output: str, event: str) -> list[dict[str, Any]]:
    return [e for e in events(output) if e["event"] == event]


def versions(bucket: str, key: str) -> list[dict[str, Any]]:
    listing = s3_admin().list_object_versions(Bucket=bucket, Prefix=key)
    return [v for v in listing.get("Versions", []) if v["Key"] == key]


# --- A/B/C: backup, upload, remote verification -------------------------------


def test_cycle_dumps_uploads_verifies_and_locks(backup_env, bucket, capsys):
    cfg = backup_env()
    assert eo.run_once(cfg) == eo.EXIT_OK

    [dump] = eo.local_dumps(cfg)
    meta = eo.read_json(eo.meta_path(dump))
    marker = eo.read_json(eo.marker_path(dump))
    assert meta["schema_revision"] == "testrev0001"
    assert meta["server_version"].startswith("16.")
    assert marker["sha256"] == meta["sha256"] == eo.digest_file(dump)[0]
    assert marker["read_back_verified"] is True

    key = eo.remote_key(cfg, dump.name)
    assert marker["key"] == key
    assert key.startswith(f"errs/postgres/{cfg.safe_db}/") and key.endswith(dump.name)
    info = s3_admin().head_object(Bucket=bucket, Key=key)
    assert info["Metadata"]["sha256"] == meta["sha256"]
    assert info["ContentLength"] == meta["size"]
    assert info["ObjectLockMode"] == "GOVERNANCE"
    assert info["ObjectLockRetainUntilDate"] > dt.datetime.now(dt.UTC) + dt.timedelta(hours=23)

    status = eo.read_json(eo.status_path(cfg))
    assert status["last_success_key"] == key and status["pending_uploads"] == []
    assert eo.cmd_health(cfg) == eo.EXIT_OK
    out = capsys.readouterr().out
    assert named(out, "offsite_upload_verified") and named(out, "backup_cycle_succeeded")


def test_uploaded_object_cannot_be_deleted_while_locked(backup_env, bucket):
    cfg = backup_env()
    assert eo.run_once(cfg) == eo.EXIT_OK
    [dump] = eo.local_dumps(cfg)
    key = eo.remote_key(cfg, dump.name)
    [version] = versions(bucket, key)
    # Even the server's root credentials cannot remove a locked version:
    # this is the ransomware / stolen-key guarantee the lock exists for.
    with pytest.raises(ClientError) as err:
        s3_admin().delete_object(Bucket=bucket, Key=key, VersionId=version["VersionId"])
    assert err.value.response["Error"]["Code"] == "AccessDenied"


def test_pipeline_never_issues_a_delete(backup_env, monkeypatch):
    calls: list[str] = []
    real = eo.make_s3

    def recording(cfg: eo.Config, endpoint: str | None = None) -> Any:
        client = real(cfg, endpoint)
        client.meta.events.register("before-call.s3.*", lambda model, **_: calls.append(model.name))
        return client

    monkeypatch.setattr(eo, "make_s3", recording)
    cfg = backup_env(BACKUP_RETENTION_DAYS="0")
    assert eo.run_once(cfg) == eo.EXIT_OK
    for dump in eo.local_dumps(cfg):  # age them past retention and go again
        os.utime(dump, (time.time() - 3 * 86400,) * 2)
    assert eo.run_once(cfg) == eo.EXIT_OK
    forbidden = {"PutObjectRetention", "PutObjectLegalHold", "PutBucketLifecycleConfiguration"}
    assert calls and not [c for c in calls if c.startswith("Delete") or c in forbidden]
    assert {"PutObject", "HeadObject", "GetObject"} <= set(calls)


def test_multipart_upload_is_verified_like_a_single_put(backup_env, bucket):
    cfg = backup_env(
        BACKUP_MULTIPART_THRESHOLD_BYTES="1", BACKUP_MULTIPART_PART_BYTES=str(5 * 1024 * 1024)
    )
    assert eo.run_once(cfg) == eo.EXIT_OK
    [dump] = eo.local_dumps(cfg)
    assert eo.read_json(eo.meta_path(dump))["size"] > 5 * 1024 * 1024  # really multi-part
    info = s3_admin().head_object(Bucket=bucket, Key=eo.remote_key(cfg, dump.name))
    assert info["ObjectLockMode"] == "GOVERNANCE"
    assert eo.is_offsite_confirmed(dump)


# --- restore from the off-site copy --------------------------------------------


def test_downloaded_backup_restores_into_a_fresh_database(backup_env, seeded_source_db, tmp_path):
    cfg = backup_env()
    assert eo.run_once(cfg) == eo.EXIT_OK
    dest = tmp_path / "restore" / "latest.dump"
    record = eo.download(cfg, eo.make_s3(cfg), "latest", dest)
    assert record["sha256"] == eo.read_json(eo.meta_path(eo.local_dumps(cfg)[-1]))["sha256"]

    target = fresh_database()
    subprocess.run(
        ["pg_restore", "--no-owner", "--no-privileges", "--exit-on-error", "--single-transaction",
         "--dbname", target, str(dest)],
        check=True,
    )
    assert fingerprint(target) == fingerprint(seeded_source_db)
    assert psql("SELECT version_num FROM alembic_version", target) == "testrev0001"


def test_restore_script_from_offsite_latest(backup_env, seeded_source_db):
    cfg = backup_env()
    assert eo.run_once(cfg) == eo.EXIT_OK
    target = f"restored_{os.getpid()}"
    proc = subprocess.run(
        ["sh", str(SRC / "restore.sh"), "--from-offsite", "latest", "--target", target],
        capture_output=True, text=True, env=dict(os.environ),
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert named(proc.stdout, "offsite_download_verified")
    assert named(proc.stdout, "restore_completed")
    assert fingerprint(target) == fingerprint(seeded_source_db)


def test_restore_script_refuses_the_live_database(backup_env, tmp_path):
    backup_env()
    dump = tmp_path / "errs-20260101T000000Z.dump"
    dump.write_bytes(b"never read")
    proc = subprocess.run(
        ["sh", str(SRC / "restore.sh"), "--file", str(dump), "--target", os.environ["PGDATABASE"]],
        capture_output=True, text=True, env=dict(os.environ),
    )
    assert proc.returncode == 1
    assert "refusing to overwrite the live database" in proc.stdout


# --- D: storage unavailable -----------------------------------------------------


def test_storage_unavailable_keeps_dump_and_catches_up_later(backup_env, capsys):
    cfg = backup_env(B2_S3_ENDPOINT="http://127.0.0.1:9")  # nothing listens here
    assert eo.run_once(cfg) == eo.EXIT_FAILED
    [dump] = eo.local_dumps(cfg)
    assert not eo.is_offsite_confirmed(dump)
    status = eo.read_json(eo.status_path(cfg))
    assert "last_success_at" not in status
    assert status["pending_uploads"] == [dump.name]
    assert eo.cmd_health(cfg) == eo.EXIT_FAILED
    out = capsys.readouterr().out
    assert len(named(out, "offsite_retrying")) == cfg.attempts - 1
    assert named(out, "backup_cycle_failed")

    # Storage is back: the next cycle uploads the stranded dump too.
    cfg = backup_env(B2_S3_ENDPOINT=os.environ["TEST_S3_ENDPOINT"])
    assert eo.run_once(cfg) == eo.EXIT_OK
    dumps = eo.local_dumps(cfg)
    assert len(dumps) == 2 and all(eo.is_offsite_confirmed(d) for d in dumps)


# --- E: invalid credentials ------------------------------------------------------


def test_invalid_credentials_fail_fast_and_are_never_printed(backup_env, bucket):
    bogus = "WrongSecret-" + os.urandom(12).hex()
    backup_env()
    proc = run_cli("run-once", env_overrides={"B2_APPLICATION_KEY": bogus})
    assert proc.returncode == eo.EXIT_FAILED
    output = proc.stdout + proc.stderr
    assert bogus not in output
    assert os.environ["TEST_S3_ACCESS"] not in output
    assert os.environ["PGPASSWORD"] not in output
    failures = named(proc.stdout, "offsite_upload_failed")
    assert failures and failures[0]["fatal"] is True
    assert "HTTP 403" in failures[0]["error"] and "credentials rejected" in failures[0]["error"]
    # Rejected credentials are not retried: the same request fails the same way.
    assert not named(proc.stdout, "offsite_retrying")
    assert s3_admin().list_objects_v2(Bucket=bucket).get("KeyCount", 0) == 0


def test_missing_configuration_names_variables_not_values(tmp_path):
    env = {"BACKUP_DIR": str(tmp_path), "B2_APPLICATION_KEY": "shh-this-is-secret"}
    with pytest.raises(eo.BackupError) as err:
        eo.Config.from_env(env)
    message = str(err.value)
    assert "B2_APPLICATION_KEY_ID" in message and "B2_BUCKET" in message
    assert "shh-this-is-secret" not in message


# --- F: interrupted upload -------------------------------------------------------


def test_upload_cut_mid_body_is_retried_and_verified(backup_env, bucket, fault_proxy, capsys):
    # Two cuts: the first is absorbed by botocore's own retry, the second
    # exhausts it and must be caught by ours.
    proxy = fault_proxy(drop_uploads=2)
    cfg = backup_env(B2_S3_ENDPOINT=proxy.endpoint)
    assert eo.run_once(cfg) == eo.EXIT_OK
    assert proxy.dropped == 2
    out = capsys.readouterr().out
    assert named(out, "offsite_retrying")
    [dump] = eo.local_dumps(cfg)
    assert eo.is_offsite_confirmed(dump)
    assert len(versions(bucket, eo.remote_key(cfg, dump.name))) == 1


def test_upload_that_never_completes_leaves_nothing_trusted(backup_env, bucket, fault_proxy):
    proxy = fault_proxy(drop_uploads=100)
    cfg = backup_env(B2_S3_ENDPOINT=proxy.endpoint)
    assert eo.run_once(cfg) == eo.EXIT_FAILED
    [dump] = eo.local_dumps(cfg)
    assert not eo.is_offsite_confirmed(dump)
    assert not eo.marker_path(dump).exists()
    # S3 PUT is atomic: a cut body stores nothing, so there is no torn object.
    assert s3_admin().list_objects_v2(Bucket=bucket).get("KeyCount", 0) == 0


# --- G: corruption ---------------------------------------------------------------


def test_dump_corrupted_on_disk_before_upload_is_refused(backup_env, bucket, capsys):
    cfg = backup_env(B2_S3_ENDPOINT="http://127.0.0.1:9")
    eo.run_once(cfg)  # dump taken, upload fails
    [dump] = eo.local_dumps(cfg)
    with dump.open("r+b") as fh:
        fh.seek(dump.stat().st_size // 2)
        fh.write(b"\x00BITROT\x00")
    cfg = backup_env(B2_S3_ENDPOINT=os.environ["TEST_S3_ENDPOINT"])
    capsys.readouterr()
    assert eo.run_once(cfg) == eo.EXIT_FAILED  # the new dump uploads; the bad one must not
    out = capsys.readouterr().out
    assert any("refusing to upload" in e["error"] for e in named(out, "offsite_upload_failed"))
    assert not eo.is_offsite_confirmed(dump)
    assert eo.remote_key(cfg, dump.name) not in {
        o["Key"] for o in s3_admin().list_objects_v2(Bucket=bucket).get("Contents", [])
    }


def test_truncated_archive_is_rejected_as_a_backup(backup_env, tmp_path):
    cfg = backup_env()
    assert eo.run_once(cfg) == eo.EXIT_OK
    [dump] = eo.local_dumps(cfg)
    truncated = tmp_path / "truncated.dump"
    truncated.write_bytes(dump.read_bytes()[: dump.stat().st_size // 2])
    with pytest.raises(eo.IntegrityError):
        eo.validate_archive(truncated)


def test_corrupted_remote_object_is_refused_on_download(backup_env, bucket, tmp_path):
    cfg = backup_env()
    assert eo.run_once(cfg) == eo.EXIT_OK
    [dump] = eo.local_dumps(cfg)
    meta = eo.read_json(eo.meta_path(dump))
    # A same-named object whose bytes differ from its recorded checksum:
    # what bit-rot or tampering at rest looks like to a restorer.
    bad_key = eo.remote_key(cfg, "errs-20000101T000000Z.dump")
    s3_admin().put_object(Bucket=bucket, Key=bad_key, Body=b"not a dump" * 1000,
                          Metadata={"sha256": meta["sha256"]})
    dest = tmp_path / "bad.dump"
    with pytest.raises(eo.IntegrityError):
        eo.download(cfg, eo.make_s3(cfg), bad_key, dest)
    assert not dest.exists() and not dest.with_name("bad.dump.partial").exists()


def test_remote_object_with_other_content_is_never_overwritten(backup_env, bucket, capsys):
    cfg = backup_env(B2_S3_ENDPOINT="http://127.0.0.1:9")
    eo.run_once(cfg)
    [dump] = eo.local_dumps(cfg)
    key = eo.remote_key(cfg, dump.name)
    s3_admin().put_object(Bucket=bucket, Key=key, Body=b"squatter", Metadata={"sha256": "0" * 64})
    cfg = backup_env(B2_S3_ENDPOINT=os.environ["TEST_S3_ENDPOINT"])
    eo.run_once(cfg)
    assert not eo.is_offsite_confirmed(dump)
    assert len(versions(bucket, key)) == 1
    assert "refusing to overwrite" in capsys.readouterr().out


# --- H: failed restore -----------------------------------------------------------


def test_restore_of_a_broken_dump_fails_loudly(backup_env, tmp_path):
    cfg = backup_env()
    assert eo.run_once(cfg) == eo.EXIT_OK
    [dump] = eo.local_dumps(cfg)
    broken = Path(cfg.backup_dir) / "errs-20000101T000000Z.dump"
    broken.write_bytes(dump.read_bytes()[: dump.stat().st_size * 2 // 3])
    target = f"broken_{os.getpid()}"
    proc = subprocess.run(
        ["sh", str(SRC / "restore.sh"), "--file", str(broken), "--target", target],
        capture_output=True, text=True, env=dict(os.environ),
    )
    assert proc.returncode != 0
    assert named(proc.stdout, "restore_failed")
    assert not named(proc.stdout, "restore_completed")
    # --single-transaction: nothing half-loaded is left behind to look real.
    assert psql("SELECT count(*) FROM pg_tables WHERE schemaname = 'public'", target) == "0"


def test_offsite_restore_aborts_when_download_fails_verification(backup_env, bucket):
    cfg = backup_env()
    key = eo.remote_key(cfg, "errs-20000101T000000Z.dump")
    s3_admin().put_object(Bucket=bucket, Key=key, Body=b"garbage", Metadata={"sha256": "1" * 64})
    target = f"never_{os.getpid()}"
    proc = subprocess.run(
        ["sh", str(SRC / "restore.sh"), "--from-offsite", key, "--target", target],
        capture_output=True, text=True, env=dict(os.environ),
    )
    assert proc.returncode == 1
    assert named(proc.stdout, "restore_aborted")
    assert psql(f"SELECT count(*) FROM pg_database WHERE datname = '{target}'", "postgres") == "0"


# --- I: duplicate execution ------------------------------------------------------


def test_second_cycle_while_one_runs_exits_busy_and_touches_nothing(backup_env):
    cfg = backup_env()
    cfg.backup_dir.mkdir(parents=True)
    with (cfg.backup_dir / ".errs-backup.lock").open("a") as held:
        fcntl.flock(held.fileno(), fcntl.LOCK_EX)
        proc = run_cli("run-once")
    assert proc.returncode == eo.EXIT_BUSY
    assert named(proc.stdout, "backup_skipped_already_running")
    assert eo.local_dumps(cfg) == []


def test_concurrent_cycles_produce_one_consistent_backup_set(backup_env, bucket):
    cfg = backup_env()
    procs = [subprocess.Popen([sys.executable, str(SRC / "errs_offsite.py"), "run-once"],
                              stdout=subprocess.PIPE, text=True) for _ in range(3)]
    codes = sorted(p.wait() for p in procs)
    assert codes.count(eo.EXIT_OK) >= 1 and set(codes) <= {eo.EXIT_OK, eo.EXIT_BUSY}
    dumps = eo.local_dumps(cfg)
    assert len(dumps) == codes.count(eo.EXIT_OK)
    for dump in dumps:
        assert eo.is_offsite_confirmed(dump)
        assert len(versions(bucket, eo.remote_key(cfg, dump.name))) == 1


def test_crash_between_upload_and_marker_is_idempotent(backup_env, bucket, capsys):
    cfg = backup_env()
    assert eo.run_once(cfg) == eo.EXIT_OK
    [dump] = eo.local_dumps(cfg)
    eo.marker_path(dump).unlink()  # as if killed right after the PUT
    capsys.readouterr()
    eo.upload_and_verify(cfg, eo.make_s3(cfg), dump)
    assert named(capsys.readouterr().out, "offsite_upload_already_present")
    assert len(versions(bucket, eo.remote_key(cfg, dump.name))) == 1  # no second copy


# --- J: retention ---------------------------------------------------------------


def age(path: Path, days: float) -> None:
    stamp = time.time() - days * 86400
    os.utime(path, (stamp, stamp))


def test_local_retention_prunes_only_what_is_safe_off_site(backup_env):
    cfg = backup_env(BACKUP_RETENTION_DAYS="7", B2_S3_ENDPOINT="http://127.0.0.1:9")
    eo.run_once(cfg)  # A: never uploaded
    time.sleep(1.1)
    cfg = backup_env(B2_S3_ENDPOINT=os.environ["TEST_S3_ENDPOINT"], BACKUP_OFFSITE="required")
    offline = eo.local_dumps(cfg)[0]
    # Make the first dump unreachable for upload by corrupting it, so it stays
    # un-uploaded through the next cycle.
    with offline.open("r+b") as fh:
        fh.seek(100)
        fh.write(b"XXXX")
    eo.run_once(cfg)  # B: uploaded
    uploaded = [d for d in eo.local_dumps(cfg) if eo.is_offsite_confirmed(d)][0]

    for dump in (offline, uploaded):
        age(dump, 10)
    stale_partial = cfg.backup_dir / "errs-20000101T000000Z.dump.partial"
    stale_partial.write_bytes(b"half")
    age(stale_partial, 2)
    fresh_partial = cfg.backup_dir / "errs-20990101T000000Z.dump.partial"
    fresh_partial.write_bytes(b"in progress")

    eo.prune_local(cfg)
    assert not uploaded.exists() and not eo.meta_path(uploaded).exists()
    assert offline.exists(), "a dump with no verified off-site copy must never be pruned"
    assert not stale_partial.exists() and fresh_partial.exists()


def test_retention_with_offsite_disabled_prunes_by_age(backup_env):
    cfg = backup_env(BACKUP_OFFSITE="disabled", BACKUP_RETENTION_DAYS="7")
    assert eo.run_once(cfg) == eo.EXIT_OK
    [dump] = eo.local_dumps(cfg)
    age(dump, 8)
    eo.prune_local(cfg)
    assert not dump.exists()


def test_if_due_skips_a_fresh_dump_but_retries_its_upload(backup_env):
    cfg = backup_env(B2_S3_ENDPOINT="http://127.0.0.1:9")
    eo.run_once(cfg)
    cfg = backup_env(B2_S3_ENDPOINT=os.environ["TEST_S3_ENDPOINT"])
    assert eo.run_once(cfg, if_due=True) == eo.EXIT_OK
    [dump] = eo.local_dumps(cfg)  # still one dump: no new one while it is recent
    assert eo.is_offsite_confirmed(dump)


# --- K: restart / kill during a backup --------------------------------------------


def test_uploader_killed_mid_upload_recovers_on_next_cycle(backup_env, bucket, fault_proxy):
    proxy = fault_proxy(throttle_bps=512 * 1024)
    cfg = backup_env(B2_S3_ENDPOINT=proxy.endpoint)
    proc = subprocess.Popen([sys.executable, str(SRC / "errs_offsite.py"), "run-once"],
                            stdout=subprocess.PIPE, text=True, env=dict(os.environ))
    deadline = time.time() + 60
    while proxy.bytes_up < 1024 * 1024:
        assert time.time() < deadline and proc.poll() is None, "upload never started"
        time.sleep(0.05)
    proc.send_signal(signal.SIGKILL)  # what `docker kill` / a host crash does
    proc.wait()

    [dump] = eo.local_dumps(cfg)
    assert not eo.is_offsite_confirmed(dump)
    assert s3_admin().list_objects_v2(Bucket=bucket).get("KeyCount", 0) == 0
    # The kernel released the flock with the process: nothing is wedged.
    cfg = backup_env(B2_S3_ENDPOINT=os.environ["TEST_S3_ENDPOINT"])
    assert eo.run_once(cfg, if_due=True) == eo.EXIT_OK
    assert eo.is_offsite_confirmed(dump)


def test_partial_dump_is_never_treated_as_a_backup(backup_env):
    cfg = backup_env()
    cfg.backup_dir.mkdir(parents=True)
    (cfg.backup_dir / "errs-20260101T000000Z.dump.partial").write_bytes(b"killed mid-dump")
    assert eo.local_dumps(cfg) == []
    proc = subprocess.run(["sh", str(SRC / "restore.sh"), "--list"], capture_output=True, text=True,
                          env={**os.environ, "BACKUP_DIR": str(cfg.backup_dir)})
    assert ".dump.partial" not in proc.stdout and "(none" in proc.stdout


# --- secrets ----------------------------------------------------------------------


def test_redact_scrubs_secret_values_and_url_credentials(monkeypatch, capsys):
    monkeypatch.setenv("B2_APPLICATION_KEY", "K005supersecretvalue")
    monkeypatch.setenv("PGPASSWORD", "pg-pass-123")
    eo.log("x", error="auth K005supersecretvalue failed for postgresql://errs:pg-pass-123@db/errs")
    out = capsys.readouterr().out
    assert "K005supersecretvalue" not in out and "pg-pass-123" not in out
    assert out.count("[REDACTED]") >= 2


def test_backup_log_and_state_files_contain_no_secrets(backup_env, capsys):
    cfg = backup_env()
    assert eo.run_once(cfg) == eo.EXIT_OK
    secrets = [os.environ["TEST_S3_SECRET"], os.environ["TEST_S3_ACCESS"], os.environ["PGPASSWORD"]]
    everything = capsys.readouterr().out
    for path in cfg.backup_dir.iterdir():
        if path.suffix == ".json":
            everything += path.read_text()
        assert not any(s in path.name for s in secrets)
    assert not any(s in everything for s in secrets)


def test_health_reports_stale_backups(backup_env):
    cfg = backup_env(BACKUP_STALE_AFTER_SECONDS="60")
    assert eo.cmd_health(cfg) == eo.EXIT_FAILED  # nothing yet
    assert eo.run_once(cfg) == eo.EXIT_OK
    assert eo.cmd_health(cfg) == eo.EXIT_OK
    status = eo.read_json(eo.status_path(cfg))
    status["last_success_at"] = "2000-01-01T00:00:00Z"
    eo.write_json_atomic(eo.status_path(cfg), status)
    assert eo.cmd_health(cfg) == eo.EXIT_FAILED


def test_check_reports_bucket_private_and_locked(backup_env, capsys):
    cfg = backup_env()
    assert eo.run_once(cfg) == eo.EXIT_OK
    capsys.readouterr()
    assert eo.cmd_check(cfg) == eo.EXIT_OK
    out = capsys.readouterr().out
    assert "[PASS] anonymous download refused" in out
    assert "[PASS] object lock (S3 view)" in out
    assert os.environ["TEST_S3_SECRET"] not in out


def test_latest_skips_a_backup_of_an_empty_replacement_database(backup_env, bucket, tmp_path, capsys):
    """Host lost -> new VM boots -> its first cycle backs up the EMPTY new
    database and uploads it as the newest object. `latest` must not pick it."""
    cfg = backup_env()
    assert eo.run_once(cfg) == eo.EXIT_OK
    [real] = eo.local_dumps(cfg)

    empty_db = fresh_database()
    psql("CREATE TABLE organizations (id serial PRIMARY KEY, name text)", empty_db)
    empty_dump = tmp_path / "empty.dump"
    subprocess.run(["pg_dump", "--format=custom", f"--file={empty_dump}", "-d", empty_db], check=True)
    sha, md5, size = eo.digest_file(empty_dump)
    newer_key = eo.remote_key(cfg, "errs-29990101T000000Z.dump")
    s3_admin().put_object(Bucket=bucket, Key=newer_key, Body=empty_dump.read_bytes(), ContentMD5=md5,
                          Metadata={"sha256": sha, "organizations": "0"})

    capsys.readouterr()
    record = eo.download(cfg, eo.make_s3(cfg), "latest", tmp_path / "restore.dump")
    assert record["key"] == eo.remote_key(cfg, real.name)
    assert named(capsys.readouterr().out, "offsite_latest_skipped_empty")
    # An explicit key is still honoured: the operator can restore anything.
    assert eo.download(cfg, eo.make_s3(cfg), newer_key, tmp_path / "explicit.dump")["key"] == newer_key


def test_lifecycle_verdict_requires_expiry_after_the_lock(backup_env):
    cfg = backup_env(BACKUP_OBJECT_LOCK_DAYS="30")
    good = {"fileNamePrefix": "errs/postgres/", "daysFromUploadingToHiding": 31, "daysFromHidingToDeleting": 1}
    assert eo.lifecycle_verdict(cfg, [good])[0]
    assert eo.lifecycle_verdict(cfg, [{**good, "fileNamePrefix": ""}])[0]  # bucket-wide rule
    assert not eo.lifecycle_verdict(cfg, [])[0]
    assert not eo.lifecycle_verdict(cfg, [{**good, "fileNamePrefix": "other/"}])[0]
    assert not eo.lifecycle_verdict(cfg, [{**good, "daysFromUploadingToHiding": 30}])[0]
    assert not eo.lifecycle_verdict(cfg, [{**good, "daysFromHidingToDeleting": None}])[0]


def test_key_scope_reads_b2api_v3_and_v2_shapes():
    # v3 (what B2 returns today): scope directly on storageApi.
    v3 = {"apiInfo": {"storageApi": {"bucketName": "ERRS-production-backups",
                                     "capabilities": ["writeFiles", "deleteFiles"]}}}
    assert eo.key_scope(v3) == ({"writeFiles", "deleteFiles"}, ["ERRS-production-backups"])
    # v2-style nesting under "allowed".
    v2 = {"apiInfo": {"storageApi": {"allowed": {"buckets": [{"name": "b"}], "capabilities": ["readFiles"]}}}}
    assert eo.key_scope(v2) == ({"readFiles"}, ["b"])
    # An unrestricted key reports no bucket: check must treat that as ALL buckets.
    assert eo.key_scope({"apiInfo": {"storageApi": {"bucketName": None, "capabilities": []}}})[1] == []


def test_destructive_capabilities_are_flagged():
    for cap in ("deleteFiles", "bypassGovernance", "shareFiles", "writeBucketRetentions",
                "writeBucketLifecycleRules", "writeBucketReplications"):
        assert cap in eo.DANGEROUS_CAPABILITIES


def test_native_b2_api_is_v4_and_its_key_scope_shape_is_read():
    # Keys created through B2's v4 key API are refused by b2_authorize_account
    # on v1-v3 ("not currently supported on API version number 3"), so the
    # native calls must be v4 — for `check`, and for endpoint discovery when
    # B2_S3_ENDPOINT is empty.
    assert "/b2api/v4/b2_authorize_account" in eo.B2_AUTHORIZE_URL
    source = (SRC / "errs_offsite.py").read_text(encoding="utf-8")
    assert "/b2api/v3/" not in source and "/b2api/v4/b2_list_buckets" in source
    # The shape v4 returned live on 2026-09-28 (ids elided, no token field).
    v4 = {
        "accountId": "<id>",
        "apiInfo": {"storageApi": {
            "absoluteMinimumPartSize": 5000000,
            "allowed": {
                "buckets": [{"id": "<id>", "name": "ERRS-production-backups"}],
                "capabilities": ["listBuckets", "listFiles", "readBucketEncryption",
                                 "readBucketLifecycleRules", "readBucketRetentions",
                                 "readFileRetentions", "readFiles", "writeFileRetentions", "writeFiles"],
                "namePrefix": None,
            },
            "apiUrl": "https://api005.backblazeb2.com",
            "downloadUrl": "https://f005.backblazeb2.com",
            "recommendedPartSize": 100000000,
            "s3ApiUrl": "https://s3.us-east-005.backblazeb2.com",
        }},
        "applicationKeyExpirationTimestamp": None,
    }
    caps, buckets = eo.key_scope(v4)
    assert buckets == ["ERRS-production-backups"]
    assert caps >= eo.REQUIRED_CAPABILITIES | eo.LOCK_CAPABILITIES
    assert not caps & eo.DANGEROUS_CAPABILITIES
    assert eo.storage_api(v4)["s3ApiUrl"] == "https://s3.us-east-005.backblazeb2.com"
