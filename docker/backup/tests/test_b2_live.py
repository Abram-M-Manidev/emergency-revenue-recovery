"""Against the real Backblaze B2 bucket. Skipped unless ERRS_B2_LIVE=1 and
the B2_* variables are set (scripts/backup-tests.sh --live passes them
through from the root .env without printing them).

Objects go under `restore-drill/pytest/<run>/` with a one-day lock, so
tests never mix with production backups and expire on their own under the
lifecycle rule in docs/RUNBOOK.md."""

from __future__ import annotations

import os
import subprocess
import uuid

import pytest
from conftest import fingerprint, fresh_database

import errs_offsite as eo

LIVE = os.environ.get("ERRS_B2_LIVE") == "1" and all(
    os.environ.get(n) for n in ("B2_APPLICATION_KEY_ID", "B2_APPLICATION_KEY", "B2_BUCKET")
)
pytestmark = pytest.mark.skipif(not LIVE, reason="set ERRS_B2_LIVE=1 and B2_* to run against real B2")


@pytest.fixture
def live_cfg(tmp_path, monkeypatch):
    monkeypatch.setenv("BACKUP_DIR", str(tmp_path / "backups"))
    monkeypatch.setenv("BACKUP_REMOTE_PREFIX", f"restore-drill/pytest/{uuid.uuid4().hex[:12]}")
    monkeypatch.setenv("BACKUP_OBJECT_LOCK_DAYS", "1")
    monkeypatch.setenv("BACKUP_SSE", "AES256")
    if not os.environ.get("B2_S3_ENDPOINT"):
        monkeypatch.delenv("B2_S3_ENDPOINT", raising=False)
    return eo.Config.from_env()


def test_live_b2_key_scope_privacy_and_lock(live_cfg, capsys):
    assert eo.run_once(live_cfg) == eo.EXIT_OK
    capsys.readouterr()
    code = eo.cmd_check(live_cfg)
    out = capsys.readouterr().out
    print(out)  # the PASS/FAIL table, already redacted, for the -rA report
    for secret in (os.environ["B2_APPLICATION_KEY"], os.environ["B2_APPLICATION_KEY_ID"]):
        assert secret not in out
    assert "[PASS] anonymous download refused" in out
    assert "[PASS] key restricted to bucket" in out
    assert code == eo.EXIT_OK, out


def test_live_b2_backup_download_and_restore(live_cfg, seeded_source_db, tmp_path):
    assert eo.run_once(live_cfg) == eo.EXIT_OK
    [dump] = eo.local_dumps(live_cfg)
    marker = eo.read_json(eo.marker_path(dump))
    assert marker["server_side_encryption"] == "AES256"
    assert marker["object_lock_mode"] == "GOVERNANCE"
    assert marker["read_back_verified"] is True

    dest = tmp_path / "from-b2.dump"
    record = eo.download(live_cfg, eo.make_s3(live_cfg), "latest", dest)
    assert record["key"] == marker["key"]
    target = fresh_database()
    subprocess.run(
        ["pg_restore", "--no-owner", "--no-privileges", "--exit-on-error", "--single-transaction",
         "--dbname", target, str(dest)],
        check=True,
    )
    assert fingerprint(target) == fingerprint(seeded_source_db)
