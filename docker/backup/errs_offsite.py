"""ERRS PostgreSQL backups: dump -> validate -> upload off-site -> verify -> retain.

Run on a schedule by backup.sh, and by operators by hand:

    errs-offsite run-once [--if-due]   take a dump, upload everything pending, verify
    errs-offsite list                  off-site backups, newest first
    errs-offsite download KEY|latest DEST
                                       fetch a backup and prove its sha256
    errs-offsite status                what the last cycles recorded
    errs-offsite health                exit 0 iff the newest off-site copy is fresh
    errs-offsite check                 credentials, key scope, bucket privacy, lock

Invariants — each one has a test in tests/:

  * A dump is uploaded only if pg_restore can read the whole archive, and only
    while its sha256 still equals the value recorded the moment pg_dump
    finished. A file that changed on disk since then is refused, not shipped.
  * An upload counts only once the remote object has been read back and its
    size, sha256, encryption and Object Lock retention all check out. Only
    then is the local `<dump>.offsite.json` marker written.
  * A local dump without that marker is never pruned, however old it is.
  * Nothing here deletes, overwrites or shortens the retention of a remote
    object. Remote expiry belongs to Object Lock plus a B2 lifecycle rule,
    so the application key does not need, and should not have, deleteFiles.
  * One cycle at a time: a second run while one holds the lock exits 75
    without touching anything.
  * Secrets never reach output. Every line printed passes through redact().

Talks to Backblaze B2 through its S3-compatible API (any S3 server that
implements Object Lock works, which is how the test suite runs without B2),
plus B2's native b2_authorize_account for endpoint discovery and `check`.
"""

from __future__ import annotations

import argparse
import base64
import contextlib
import datetime as dt
import fcntl
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TypeVar

import boto3
from botocore.config import Config as BotoConfig
from botocore.exceptions import BotoCoreError, ClientError

T = TypeVar("T")

EXIT_OK = 0
EXIT_FAILED = 1
# EX_TEMPFAIL: "try again later". Distinct from failure so a manual run that
# collided with the scheduled one is not mistaken for a broken backup.
EXIT_BUSY = 75

DUMP_RE = re.compile(r"^errs-(\d{8}T\d{6}Z)\.dump$")
CHUNK = 1024 * 1024
# v4, not v3: B2 refuses to authorize keys created through the v4 key API
# (e.g. `b2 key create` from CLI 5.x, which records a bucket *list*) on any
# older version — "not currently supported on API version number 3".
B2_AUTHORIZE_URL = "https://api.backblazeb2.com/b2api/v4/b2_authorize_account"

# Environment variables whose values must never be printed. redact() also
# covers any value registered at runtime via register_secret().
SECRET_ENV_VARS = (
    "B2_APPLICATION_KEY",
    "B2_APPLICATION_KEY_ID",
    "PGPASSWORD",
    "BACKUP_HEARTBEAT_URL",
)
_runtime_secrets: set[str] = set()

# Rejections that no retry can fix: re-sending the same request with the same
# key fails the same way, and retrying only delays the log line an operator
# needs. Everything else (5xx, throttling, resets, timeouts) is retried.
FATAL_S3_CODES = frozenset(
    {
        "AccessDenied",
        "AllAccessDisabled",
        "InvalidAccessKeyId",
        "InvalidArgument",
        "InvalidBucketName",
        "InvalidRequest",
        "NoSuchBucket",
        "RequestTimeTooSkewed",
        "SignatureDoesNotMatch",
        "Unauthorized",
    }
)
RETRYABLE_4XX_CODES = frozenset({"BadDigest", "RequestTimeout", "SlowDown", "TooManyRequests"})

# Application-key capabilities, for `check`. REQUIRED is what the uploader
# actually calls; DANGEROUS is anything that would let a stolen backup key
# destroy or expose the backups it exists to protect.
REQUIRED_CAPABILITIES = frozenset({"listBuckets", "listFiles", "readFiles", "writeFiles"})
LOCK_CAPABILITIES = frozenset({"readFileRetentions", "writeFileRetentions"})
DANGEROUS_CAPABILITIES = frozenset(
    {
        "bypassGovernance",
        "deleteBuckets",
        "deleteFiles",
        "deleteKeys",
        "shareFiles",
        "writeBucketRetentions",
        "writeBuckets",
        "writeKeys",
        # Bucket reconfiguration: replication or event notifications can copy
        # backups (or their names) elsewhere; lifecycle rules can expire them
        # the moment their lock ends; encryption settings can be switched off.
        "writeBucketEncryption",
        "writeBucketLifecycleRules",
        "writeBucketNotifications",
        "writeBucketReplications",
    }
)


class BackupError(Exception):
    """A failure the operator must see. Retried where retrying can help."""


class FatalRemoteError(BackupError):
    """Storage rejected the request in a way retrying cannot fix."""


class IntegrityError(BackupError):
    """Bytes are not what they are supposed to be. Never retried, never trusted."""


# --- output ------------------------------------------------------------------


def register_secret(value: str) -> None:
    if len(value) >= 4:
        _runtime_secrets.add(value)


def redact(text: str) -> str:
    secrets = set(_runtime_secrets)
    for name in SECRET_ENV_VARS:
        value = os.environ.get(name, "")
        if len(value) >= 4:
            secrets.add(value)
    # Longest first, so a secret that contains another is replaced whole.
    for value in sorted(secrets, key=len, reverse=True):
        text = text.replace(value, "[REDACTED]")
    # Credentials embedded in any URL, e.g. postgresql://user:pass@host.
    return re.sub(r"://[^/\s:@]+:[^/\s@]+@", "://[REDACTED]@", text)


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def iso(ts: dt.datetime) -> str:
    return ts.astimezone(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def log(event: str, **fields: Any) -> None:
    """One JSON line, the same shape as the API's structured logs."""
    record: dict[str, Any] = {"event": event, "service": "backup"}
    for name, value in fields.items():
        record[name] = redact(value) if isinstance(value, str) else value
    record["ts"] = iso(utcnow())
    print(redact(json.dumps(record, default=str)), flush=True)


def one_line(text: str, limit: int = 300) -> str:
    return redact(" ".join(text.split()))[:limit]


# --- configuration -----------------------------------------------------------


def _int(env: Mapping[str, str], name: str, default: int) -> int:
    raw = env.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        raise BackupError(f"{name} must be an integer") from None


def _bool(env: Mapping[str, str], name: str, default: bool) -> bool:
    raw = env.get(name, "").strip().lower()
    if not raw:
        return default
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    raise BackupError(f"{name} must be true or false")


@dataclass(frozen=True)
class Config:
    backup_dir: Path
    database: str
    interval_seconds: int
    local_retention_days: int
    offsite: bool
    bucket: str
    endpoint: str
    prefix: str
    lock_mode: str
    lock_days: int
    sse: str
    verify_download: bool
    attempts: int
    retry_base_seconds: float
    multipart_threshold: int
    part_size: int
    stale_after_seconds: int
    connect_timeout: int
    read_timeout: int
    key_id: str = field(default="", repr=False)
    key: str = field(default="", repr=False)
    heartbeat_url: str = field(default="", repr=False)

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Config:
        env = os.environ if env is None else env
        mode = env.get("BACKUP_OFFSITE", "required").strip().lower() or "required"
        if mode not in {"required", "disabled"}:
            raise BackupError("BACKUP_OFFSITE must be 'required' or 'disabled'")
        offsite = mode == "required"

        key_id = env.get("B2_APPLICATION_KEY_ID", "").strip()
        key = env.get("B2_APPLICATION_KEY", "").strip()
        bucket = env.get("B2_BUCKET", "").strip()
        if offsite:
            missing = [
                name
                for name, value in (
                    ("B2_APPLICATION_KEY_ID", key_id),
                    ("B2_APPLICATION_KEY", key),
                    ("B2_BUCKET", bucket),
                )
                if not value
            ]
            if missing:
                # Names only. The message is the operator's whole clue, so it
                # says exactly what is absent and how to opt out on purpose.
                raise BackupError(
                    "off-site backup is required but not configured; missing: "
                    + ", ".join(missing)
                    + " (set BACKUP_OFFSITE=disabled only for a deliberately local-only stack)"
                )
        for secret in (key_id, key, env.get("BACKUP_HEARTBEAT_URL", "")):
            register_secret(secret)

        lock_mode = env.get("BACKUP_OBJECT_LOCK_MODE", "GOVERNANCE").strip().upper() or "GOVERNANCE"
        if lock_mode not in {"GOVERNANCE", "COMPLIANCE", "NONE"}:
            raise BackupError("BACKUP_OBJECT_LOCK_MODE must be GOVERNANCE, COMPLIANCE or NONE")
        lock_days = _int(env, "BACKUP_OBJECT_LOCK_DAYS", 30)
        if lock_mode != "NONE" and lock_days < 1:
            raise BackupError("BACKUP_OBJECT_LOCK_DAYS must be >= 1 unless the lock mode is NONE")

        interval = _int(env, "BACKUP_INTERVAL_SECONDS", 86400)
        part_size = _int(env, "BACKUP_MULTIPART_PART_BYTES", 100 * CHUNK)
        if part_size < 5 * CHUNK:
            raise BackupError("BACKUP_MULTIPART_PART_BYTES must be at least 5 MiB (S3 minimum)")
        prefix = env.get("BACKUP_REMOTE_PREFIX", "errs/postgres").strip().strip("/")
        return cls(
            backup_dir=Path(env.get("BACKUP_DIR", "/backups")),
            database=env.get("PGDATABASE", "errs") or "errs",
            interval_seconds=interval,
            local_retention_days=_int(env, "BACKUP_RETENTION_DAYS", 7),
            offsite=offsite,
            bucket=bucket,
            endpoint=env.get("B2_S3_ENDPOINT", "").strip().rstrip("/"),
            prefix=prefix or "errs/postgres",
            lock_mode=lock_mode,
            lock_days=lock_days,
            # B2 applies SSE-B2 when the request asks for AES256, and reports
            # it back on HEAD, which is how each upload proves it is encrypted.
            sse=env.get("BACKUP_SSE", "AES256").strip(),
            verify_download=_bool(env, "BACKUP_VERIFY_DOWNLOAD", True),
            attempts=max(1, _int(env, "BACKUP_UPLOAD_ATTEMPTS", 4)),
            retry_base_seconds=float(env.get("BACKUP_RETRY_BASE_SECONDS", "") or 10),
            multipart_threshold=_int(env, "BACKUP_MULTIPART_THRESHOLD_BYTES", 1024 * CHUNK),
            part_size=part_size,
            # A nightly job is late once a full interval plus a slow run has
            # passed; three hours covers a large dump and a retried upload.
            stale_after_seconds=_int(env, "BACKUP_STALE_AFTER_SECONDS", interval + 3 * 3600),
            connect_timeout=_int(env, "BACKUP_CONNECT_TIMEOUT_SECONDS", 15),
            read_timeout=_int(env, "BACKUP_READ_TIMEOUT_SECONDS", 120),
            key_id=key_id,
            key=key,
            heartbeat_url=env.get("BACKUP_HEARTBEAT_URL", "").strip(),
        )

    @property
    def safe_db(self) -> str:
        return re.sub(r"[^A-Za-z0-9_.-]", "_", self.database)


# --- local files -------------------------------------------------------------


def meta_path(dump: Path) -> Path:
    return dump.with_name(dump.name + ".meta.json")


def marker_path(dump: Path) -> Path:
    return dump.with_name(dump.name + ".offsite.json")


def status_path(cfg: Config) -> Path:
    return cfg.backup_dir / "offsite-status.json"


def write_json_atomic(path: Path, data: Mapping[str, Any]) -> None:
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, sort_keys=True)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def read_json(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, dict):
        raise BackupError(f"{path.name} is not a JSON object")
    return data


def digest_file(path: Path) -> tuple[str, str, int]:
    """(sha256 hex, md5 base64, size). MD5 only as S3's Content-MD5 transport check."""
    sha = hashlib.sha256()
    md5 = hashlib.md5(usedforsecurity=False)
    size = 0
    with path.open("rb") as fh:
        while chunk := fh.read(CHUNK):
            sha.update(chunk)
            md5.update(chunk)
            size += len(chunk)
    return sha.hexdigest(), base64.b64encode(md5.digest()).decode(), size


def local_dumps(cfg: Config) -> list[Path]:
    """Complete dumps only, oldest first. `.partial` files never match."""
    if not cfg.backup_dir.is_dir():
        return []
    return sorted(p for p in cfg.backup_dir.iterdir() if p.is_file() and DUMP_RE.match(p.name))


def is_offsite_confirmed(dump: Path) -> bool:
    try:
        marker = read_json(marker_path(dump))
        meta = read_json(meta_path(dump))
    except (OSError, ValueError, BackupError):
        return False
    return bool(marker.get("sha256")) and marker.get("sha256") == meta.get("sha256")


@contextlib.contextmanager
def exclusive_lock(path: Path) -> Iterator[bool]:
    """Non-blocking flock. Released by the kernel if the holder dies, so a
    container killed mid-backup can never leave a lock that blocks forever."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as fh:
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


# --- dump --------------------------------------------------------------------


def psql_scalar(sql: str) -> str | None:
    proc = subprocess.run(
        ["psql", "-X", "-A", "-t", "-v", "ON_ERROR_STOP=1", "-c", sql],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        return None
    return proc.stdout.strip() or None


def validate_archive(path: Path) -> None:
    """Read the entire archive the way a restore would, writing nowhere.

    `pg_restore --list` only reads the table of contents; rendering to
    /dev/null decompresses every data block too, so truncation or bit-rot
    anywhere in the file fails here rather than during an incident.
    """
    proc = subprocess.run(
        ["pg_restore", "--file=/dev/null", str(path)], capture_output=True, text=True
    )
    if proc.returncode != 0:
        raise IntegrityError(
            f"{path.name} is not a complete, readable pg_dump archive: {one_line(proc.stderr)}"
        )


def ensure_meta(cfg: Config, dump: Path) -> dict[str, Any]:
    """The sidecar recorded when the dump was taken, or — for a dump older than
    this tooling — one rebuilt after re-validating the archive in full."""
    path = meta_path(dump)
    if path.exists():
        return read_json(path)
    validate_archive(dump)
    sha, _, size = digest_file(dump)
    match = DUMP_RE.match(dump.name)
    stamp = match.group(1) if match else ""
    created = dt.datetime.strptime(stamp, "%Y%m%dT%H%M%SZ").replace(tzinfo=dt.UTC)
    meta = {
        "format": "pg_dump-custom",
        "database": cfg.database,
        "created_at": iso(created),
        "sha256": sha,
        "size": size,
        "meta_reconstructed": True,
    }
    write_json_atomic(path, meta)
    log("backup_meta_reconstructed", file=dump.name)
    return meta


def take_dump(cfg: Config) -> Path:
    cfg.backup_dir.mkdir(parents=True, exist_ok=True)
    target = cfg.backup_dir / f"errs-{utcnow().strftime('%Y%m%dT%H%M%SZ')}.dump"
    while target.exists():  # two runs inside one second, sequentially
        time.sleep(1)
        target = cfg.backup_dir / f"errs-{utcnow().strftime('%Y%m%dT%H%M%SZ')}.dump"
    partial = target.with_name(target.name + ".partial")
    started = utcnow()

    # pg_dump reads one consistent MVCC snapshot and takes only ACCESS SHARE
    # locks: the API keeps serving reads and writes throughout. Written to
    # `.partial` so a dump killed half way can never be mistaken for a backup.
    proc = subprocess.run(
        ["pg_dump", "--format=custom", "--compress=6", f"--file={partial}"],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        partial.unlink(missing_ok=True)
        raise BackupError(f"pg_dump failed: {one_line(proc.stderr)}")
    try:
        validate_archive(partial)
    except IntegrityError:
        partial.unlink(missing_ok=True)
        raise
    sha, _, size = digest_file(partial)
    meta = {
        "format": "pg_dump-custom",
        "database": cfg.database,
        "created_at": iso(started),
        "sha256": sha,
        "size": size,
        "server_version": psql_scalar("SHOW server_version"),
        "schema_revision": psql_scalar("SELECT version_num FROM alembic_version LIMIT 1"),
        # A count, not data: lets `download latest` tell a real backup from
        # one a freshly built replacement host took of its empty database.
        "organizations": psql_scalar("SELECT count(*) FROM organizations"),
        "pg_dump_version": subprocess.run(
            ["pg_dump", "--version"], capture_output=True, text=True
        ).stdout.strip(),
    }
    # Sidecar first, dump second: a dump file that exists always has its
    # checksum beside it, so "complete dump" and "checksum known" coincide.
    write_json_atomic(meta_path(target), meta)
    os.replace(partial, target)
    log(
        "backup_completed",
        file=target.name,
        bytes=size,
        sha256=sha,
        seconds=round((utcnow() - started).total_seconds(), 1),
        schema_revision=meta["schema_revision"],
    )
    return target


# --- storage -----------------------------------------------------------------


def b2_authorize(cfg: Config) -> dict[str, Any]:
    token = base64.b64encode(f"{cfg.key_id}:{cfg.key}".encode()).decode()
    register_secret(token)
    request = urllib.request.Request(B2_AUTHORIZE_URL, headers={"Authorization": f"Basic {token}"})
    try:
        with urllib.request.urlopen(request, timeout=cfg.connect_timeout + 15) as response:
            data = json.load(response)
    except urllib.error.HTTPError as exc:
        code = ""
        with contextlib.suppress(Exception):
            code = str(json.loads(exc.read().decode()).get("code", ""))
        message = f"B2 rejected the application key (HTTP {exc.code} {code})"
        if exc.code in (400, 401, 403):
            raise FatalRemoteError(message) from None
        raise BackupError(message) from None
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise BackupError(f"B2 authorization endpoint unreachable: {one_line(str(exc))}") from None
    for secret_field in ("authorizationToken",):
        register_secret(str(data.get(secret_field, "")))
    return data if isinstance(data, dict) else {}


def storage_api(auth: Mapping[str, Any]) -> dict[str, Any]:
    info = auth.get("apiInfo", {}).get("storageApi", {})
    return info if isinstance(info, dict) else {}


def resolve_endpoint(cfg: Config) -> str:
    if cfg.endpoint:
        return cfg.endpoint
    url = storage_api(b2_authorize(cfg)).get("s3ApiUrl", "")
    if not url:
        raise BackupError("B2 did not report an S3 endpoint; set B2_S3_ENDPOINT")
    return str(url).rstrip("/")


def region_for(endpoint: str) -> str:
    match = re.match(r"https?://s3\.([a-z0-9-]+)\.backblazeb2\.com", endpoint)
    return match.group(1) if match else "us-east-1"


def make_s3(cfg: Config, endpoint: str | None = None) -> Any:
    endpoint = endpoint or resolve_endpoint(cfg)
    boto_cfg = BotoConfig(
        signature_version="s3v4",
        # Path-style: the bucket name has capitals, which are not valid in a
        # DNS label, so virtual-host addressing cannot be relied on.
        s3={"addressing_style": "path"},
        # One SDK-level retry for a blip; the outer, logged with_retries() owns
        # the real retry policy so every attempt is visible in the logs.
        retries={"mode": "standard", "total_max_attempts": 2},
        connect_timeout=cfg.connect_timeout,
        read_timeout=cfg.read_timeout,
        # Only the checksums S3 requires. The integrity story here is our
        # own Content-MD5 on every request plus a full sha256 read-back, not
        # SDK-default trailing CRCs that S3-compatible stores vary on.
        request_checksum_calculation="when_required",
        response_checksum_validation="when_required",
    )
    return boto3.client(
        "s3",
        endpoint_url=endpoint,
        region_name=region_for(endpoint),
        aws_access_key_id=cfg.key_id,
        aws_secret_access_key=cfg.key,
        config=boto_cfg,
    )


def client_error_code(exc: ClientError) -> tuple[str, int]:
    error = exc.response.get("Error", {})
    status = int(exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode") or 0)
    return str(error.get("Code", "")), status


def with_retries(cfg: Config, what: str, fn: Callable[[], T]) -> T:
    last = ""
    for attempt in range(1, cfg.attempts + 1):
        try:
            return fn()
        except IntegrityError:
            raise
        except ClientError as exc:
            code, status = client_error_code(exc)
            if code in FATAL_S3_CODES or (400 <= status < 500 and code not in RETRYABLE_4XX_CODES):
                hint = " — credentials rejected or key lacks permission" if status in (401, 403) else ""
                raise FatalRemoteError(
                    f"{what} rejected by storage: {code or 'error'} (HTTP {status}){hint}"
                ) from None
            last = f"{code or 'error'} (HTTP {status})"
        except (BotoCoreError, OSError) as exc:
            last = f"{type(exc).__name__}: {one_line(str(exc), 200)}"
        if attempt < cfg.attempts:
            delay = cfg.retry_base_seconds * 2 ** (attempt - 1)
            log("offsite_retrying", what=what, attempt=attempt, of=cfg.attempts, delay_seconds=delay, error=last)
            time.sleep(delay)
    raise BackupError(f"{what} failed after {cfg.attempts} attempt(s): {last}")


def remote_key(cfg: Config, dump_name: str) -> str:
    match = DUMP_RE.match(dump_name)
    if not match:
        raise BackupError(f"not a backup file name: {dump_name}")
    stamp = match.group(1)
    # <prefix>/<db>/<yyyy>/<mm>/errs-<stamp>.dump — lexical order is time
    # order, so "latest" is the greatest key, and nothing in the name comes
    # from a credential or from tenant data.
    return f"{cfg.prefix}/{cfg.safe_db}/{stamp[0:4]}/{stamp[4:6]}/{dump_name}"


def head(cfg: Config, s3: Any, key: str) -> dict[str, Any] | None:
    try:
        result: dict[str, Any] = s3.head_object(Bucket=cfg.bucket, Key=key)
        return result
    except ClientError as exc:
        code, status = client_error_code(exc)
        if status == 404 or code in {"404", "NoSuchKey", "NotFound"}:
            return None
        raise


def object_args(cfg: Config, meta: Mapping[str, Any]) -> dict[str, Any]:
    args: dict[str, Any] = {
        "ContentType": "application/octet-stream",
        # Values here are all ours and non-sensitive: checksum, size, times,
        # versions. Nothing derived from tenant data or from credentials.
        "Metadata": {
            "sha256": str(meta["sha256"]),
            "size": str(meta["size"]),
            "created-at": str(meta.get("created_at", "")),
            "format": "pg_dump-custom",
            "database": cfg.safe_db,
            "schema-revision": str(meta.get("schema_revision") or "unknown"),
            "server-version": str(meta.get("server_version") or "unknown"),
            "organizations": str(meta.get("organizations") or "unknown"),
        },
    }
    if cfg.sse:
        args["ServerSideEncryption"] = cfg.sse
    if cfg.lock_mode != "NONE":
        args["ObjectLockMode"] = cfg.lock_mode
        args["ObjectLockRetainUntilDate"] = utcnow() + dt.timedelta(days=cfg.lock_days)
    return args


def put_single(cfg: Config, s3: Any, key: str, dump: Path, meta: Mapping[str, Any], md5: str) -> None:
    # Content-MD5 makes the server reject the body if a single byte changed
    # in transit; S3 also requires it on any write that sets Object Lock.
    with dump.open("rb") as fh:
        s3.put_object(
            Bucket=cfg.bucket,
            Key=key,
            Body=fh,
            ContentLength=int(meta["size"]),
            ContentMD5=md5,
            **object_args(cfg, meta),
        )


def put_multipart(cfg: Config, s3: Any, key: str, dump: Path, meta: Mapping[str, Any]) -> None:
    upload_id = s3.create_multipart_upload(Bucket=cfg.bucket, Key=key, **object_args(cfg, meta))[
        "UploadId"
    ]
    parts: list[dict[str, Any]] = []
    try:
        with dump.open("rb") as fh:
            number = 1
            while chunk := fh.read(cfg.part_size):
                md5 = base64.b64encode(hashlib.md5(chunk, usedforsecurity=False).digest()).decode()

                def send(chunk: bytes = chunk, number: int = number, md5: str = md5) -> Any:
                    return s3.upload_part(
                        Bucket=cfg.bucket,
                        Key=key,
                        UploadId=upload_id,
                        PartNumber=number,
                        Body=chunk,
                        ContentMD5=md5,
                    )

                response = with_retries(cfg, f"upload part {number}", send)
                parts.append({"ETag": response["ETag"], "PartNumber": number})
                number += 1
        s3.complete_multipart_upload(
            Bucket=cfg.bucket, Key=key, UploadId=upload_id, MultipartUpload={"Parts": parts}
        )
    except BaseException:
        # Unfinished parts are invisible but billed. Aborting is best-effort;
        # the B2 lifecycle rule in the runbook cancels any that slip past.
        with contextlib.suppress(Exception):
            s3.abort_multipart_upload(Bucket=cfg.bucket, Key=key, UploadId=upload_id)
        raise


def verify_remote(
    cfg: Config, s3: Any, key: str, meta: Mapping[str, Any], min_retain: dt.datetime
) -> dict[str, Any]:
    info = head(cfg, s3, key)
    if info is None:
        raise BackupError(f"{key} is not present after upload")
    problems: list[str] = []
    if int(info.get("ContentLength", -1)) != int(meta["size"]):
        problems.append(f"size {info.get('ContentLength')} != {meta['size']}")
    if info.get("Metadata", {}).get("sha256") != meta["sha256"]:
        problems.append("recorded sha256 does not match")
    if cfg.sse and info.get("ServerSideEncryption") != cfg.sse:
        problems.append(f"server-side encryption is {info.get('ServerSideEncryption')!r}, expected {cfg.sse!r}")
    retain_until = info.get("ObjectLockRetainUntilDate")
    if cfg.lock_mode != "NONE":
        # `min_retain` is upload time + lock days for a fresh upload (proving
        # the store honoured the requested period), or merely "now" when
        # re-verifying an object an earlier run uploaded.
        if info.get("ObjectLockMode") != cfg.lock_mode:
            problems.append(f"object lock mode is {info.get('ObjectLockMode')!r}, expected {cfg.lock_mode!r}")
        elif not isinstance(retain_until, dt.datetime) or retain_until < min_retain:
            problems.append("object lock retention is missing or shorter than required")
    if problems:
        raise IntegrityError(f"remote verification failed for {key}: " + "; ".join(problems))

    if cfg.verify_download:
        # The only proof that the stored bytes are the dump's bytes is to read
        # them back. Cheap at pilot size; BACKUP_VERIFY_DOWNLOAD=false trades
        # it for egress once dumps are large.
        body = s3.get_object(Bucket=cfg.bucket, Key=key)["Body"]
        sha = hashlib.sha256()
        size = 0
        try:
            for chunk in body.iter_chunks(CHUNK):
                sha.update(chunk)
                size += len(chunk)
        finally:
            body.close()
        if sha.hexdigest() != meta["sha256"] or size != int(meta["size"]):
            raise IntegrityError(f"remote verification failed for {key}: downloaded bytes differ from the dump")

    return {
        "key": key,
        "bucket": cfg.bucket,
        "version_id": info.get("VersionId"),
        "sha256": meta["sha256"],
        "size": int(meta["size"]),
        "object_lock_mode": info.get("ObjectLockMode"),
        "object_lock_retain_until": iso(retain_until) if isinstance(retain_until, dt.datetime) else None,
        "server_side_encryption": info.get("ServerSideEncryption"),
        "read_back_verified": cfg.verify_download,
        "verified_at": iso(utcnow()),
    }


def upload_and_verify(cfg: Config, s3: Any, dump: Path) -> dict[str, Any]:
    meta = ensure_meta(cfg, dump)
    sha, md5, size = digest_file(dump)
    if sha != meta["sha256"] or size != int(meta["size"]):
        raise IntegrityError(
            f"{dump.name} no longer matches the checksum recorded when it was taken; "
            "refusing to upload a changed or corrupted backup"
        )
    key = remote_key(cfg, dump.name)
    existing = with_retries(cfg, f"check {key}", lambda: head(cfg, s3, key))
    min_retain = utcnow()
    if existing is not None:
        # A previous run uploaded it and died before writing the marker. The
        # same bytes are already there: verify them rather than write again.
        if existing.get("Metadata", {}).get("sha256") != sha:
            raise IntegrityError(
                f"{key} already exists off-site with different content; refusing to overwrite it"
            )
        log("offsite_upload_already_present", key=key)
    else:
        started = time.monotonic()
        # An hour of slack for clock skew and a slow upload.
        min_retain = utcnow() + dt.timedelta(days=cfg.lock_days) - dt.timedelta(hours=1)
        if size >= cfg.multipart_threshold:
            put_multipart(cfg, s3, key, dump, meta)
        else:
            with_retries(cfg, f"upload {key}", lambda: put_single(cfg, s3, key, dump, meta, md5))
        log("offsite_upload_completed", key=key, bytes=size, seconds=round(time.monotonic() - started, 1))

    record = with_retries(cfg, f"verify {key}", lambda: verify_remote(cfg, s3, key, meta, min_retain))
    write_json_atomic(marker_path(dump), record)
    log(
        "offsite_upload_verified",
        key=key,
        sha256=sha,
        version_id=record["version_id"],
        retain_until=record["object_lock_retain_until"],
    )
    return record


def list_remote(cfg: Config, s3: Any) -> list[dict[str, Any]]:
    prefix = f"{cfg.prefix}/{cfg.safe_db}/"
    items: list[dict[str, Any]] = []
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=cfg.bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            if DUMP_RE.match(obj["Key"].rsplit("/", 1)[-1]):
                items.append(obj)
    return sorted(items, key=lambda o: str(o["Key"]), reverse=True)


def latest_nonempty_key(cfg: Config, s3: Any) -> str:
    """The newest backup of a database that actually holds tenants.

    After losing the VM, the replacement host's backup service takes its
    first dump of a brand-new, empty database and uploads it — making it the
    newest object. A literal "latest" would restore that emptiness over
    nothing and look like success. Backups recording zero organizations are
    skipped (and said so); an explicit key restores anything.
    """
    found = with_retries(cfg, "list backups", lambda: list_remote(cfg, s3))
    if not found:
        raise BackupError(f"no backups under {cfg.prefix}/{cfg.safe_db}/ in {cfg.bucket}")
    for obj in found:
        key = str(obj["Key"])

        def probe(key: str = key) -> dict[str, Any] | None:
            return head(cfg, s3, key)

        info = with_retries(cfg, f"check {key}", probe) or {}
        if info.get("Metadata", {}).get("organizations") == "0":
            log("offsite_latest_skipped_empty", key=key, detail="backup of a database with no organizations")
            continue
        return key
    raise BackupError("every off-site backup is of an empty database; pass an explicit key to restore one")


def download(cfg: Config, s3: Any, key: str, dest: Path) -> dict[str, Any]:
    if key == "latest":
        key = latest_nonempty_key(cfg, s3)
    info = with_retries(cfg, f"check {key}", lambda: head(cfg, s3, key))
    if info is None:
        raise BackupError(f"{key} does not exist in {cfg.bucket}")
    expected = info.get("Metadata", {}).get("sha256")
    if not expected:
        raise IntegrityError(f"{key} carries no recorded sha256; it was not written by this tool")

    dest.parent.mkdir(parents=True, exist_ok=True)
    partial = dest.with_name(dest.name + ".partial")

    def fetch() -> tuple[str, int]:
        body = s3.get_object(Bucket=cfg.bucket, Key=key)["Body"]
        sha = hashlib.sha256()
        size = 0
        try:
            with partial.open("wb") as fh:
                for chunk in body.iter_chunks(CHUNK):
                    fh.write(chunk)
                    sha.update(chunk)
                    size += len(chunk)
        finally:
            body.close()
        return sha.hexdigest(), size

    try:
        sha, size = with_retries(cfg, f"download {key}", fetch)
        if sha != expected or size != int(info["ContentLength"]):
            raise IntegrityError(
                f"{key} failed its integrity check after download (sha256 or size mismatch); "
                "do not restore it"
            )
        validate_archive(partial)
    except BaseException:
        partial.unlink(missing_ok=True)
        raise
    os.replace(partial, dest)
    log("offsite_download_verified", key=key, file=str(dest), bytes=size, sha256=sha)
    return {"key": key, "sha256": sha, "size": size, "path": str(dest)}


# --- retention ---------------------------------------------------------------


def prune_local(cfg: Config) -> None:
    cutoff = time.time() - cfg.local_retention_days * 86400
    removed = 0
    for dump in local_dumps(cfg):
        if dump.stat().st_mtime > cutoff:
            continue
        if cfg.offsite and not is_offsite_confirmed(dump):
            # The one rule retention must never break: the only copy of a
            # backup is not deleted to save disk. A full disk is loud and
            # recoverable; a silently lost backup is neither.
            log("local_prune_blocked_not_offsite", file=dump.name)
            continue
        for path in (dump, meta_path(dump), marker_path(dump)):
            path.unlink(missing_ok=True)
        removed += 1
    if removed:
        log("backup_pruned", removed=removed, older_than_days=cfg.local_retention_days)

    for path in cfg.backup_dir.glob("*.partial"):
        # Only a run killed mid-write leaves one; a day old is surely dead.
        if path.stat().st_mtime < time.time() - 86400:
            path.unlink(missing_ok=True)
            log("backup_partial_removed", file=path.name)
    for sidecar in list(cfg.backup_dir.glob("errs-*.dump.meta.json")) + list(
        cfg.backup_dir.glob("errs-*.dump.offsite.json")
    ):
        dump = sidecar.with_name(sidecar.name.split(".dump.")[0] + ".dump")
        if not dump.exists():
            sidecar.unlink(missing_ok=True)


# --- cycle -------------------------------------------------------------------


def load_status(cfg: Config) -> dict[str, Any]:
    try:
        return read_json(status_path(cfg))
    except (OSError, ValueError, BackupError):
        return {}


def heartbeat(cfg: Config) -> None:
    if not cfg.heartbeat_url:
        return
    try:
        with urllib.request.urlopen(cfg.heartbeat_url, timeout=15):
            pass
        log("backup_heartbeat_sent")
    except Exception as exc:  # noqa: BLE001 - a monitoring ping must never fail a backup
        log("backup_heartbeat_failed", error=type(exc).__name__)


def dump_is_due(cfg: Config) -> bool:
    dumps = local_dumps(cfg)
    if not dumps:
        return True
    age = time.time() - dumps[-1].stat().st_mtime
    return age >= cfg.interval_seconds * 0.9


def run_once(cfg: Config, *, if_due: bool = False) -> int:
    cfg.backup_dir.mkdir(parents=True, exist_ok=True)
    with exclusive_lock(cfg.backup_dir / ".errs-backup.lock") as acquired:
        if not acquired:
            log("backup_skipped_already_running", detail="another backup cycle holds the lock")
            return EXIT_BUSY
        return _cycle(cfg, if_due=if_due)


def _cycle(cfg: Config, *, if_due: bool) -> int:
    status = load_status(cfg)
    status["last_attempt_at"] = iso(utcnow())
    errors: list[str] = []

    try:
        prune_local(cfg)
    except Exception as exc:  # noqa: BLE001 - pruning must never stop tonight's backup
        log("backup_prune_failed", error=one_line(str(exc)))

    newest: Path | None = None
    if not if_due or dump_is_due(cfg):
        try:
            newest = take_dump(cfg)
        except BackupError as exc:
            errors.append(str(exc))
            log("backup_failed", error=str(exc))
    else:
        existing = local_dumps(cfg)
        newest = existing[-1] if existing else None
        log("backup_not_due", latest=newest.name if newest else None)

    newest_record: dict[str, Any] | None = None
    if not cfg.offsite:
        log("offsite_disabled", detail="BACKUP_OFFSITE=disabled: this backup does NOT survive host loss")
    else:
        pending = [d for d in local_dumps(cfg) if not is_offsite_confirmed(d)]
        if newest is not None and newest not in pending and is_offsite_confirmed(newest):
            with contextlib.suppress(Exception):
                newest_record = read_json(marker_path(newest))
        if pending:
            try:
                s3 = make_s3(cfg)
            except BackupError as exc:
                errors.append(str(exc))
                log("offsite_upload_failed", error=str(exc), pending=len(pending))
            else:
                for dump in pending:
                    try:
                        record = upload_and_verify(cfg, s3, dump)
                        if dump == newest:
                            newest_record = record
                    except FatalRemoteError as exc:
                        # Same key, same rejection for every file: stop here.
                        errors.append(str(exc))
                        log("offsite_upload_failed", file=dump.name, error=str(exc), fatal=True)
                        break
                    except BackupError as exc:
                        errors.append(str(exc))
                        log("offsite_upload_failed", file=dump.name, error=str(exc))

    pending_after = [d.name for d in local_dumps(cfg) if not is_offsite_confirmed(d)] if cfg.offsite else []
    succeeded = newest is not None and not errors and (not cfg.offsite or newest_record is not None)
    status["pending_uploads"] = pending_after
    if succeeded and newest is not None:
        status["last_success_at"] = iso(utcnow())
        status["last_success_file"] = newest.name
        status["last_success_key"] = newest_record["key"] if newest_record else None
        status["last_error"] = None
    else:
        status["last_failure_at"] = iso(utcnow())
        status["last_error"] = redact("; ".join(errors) or "no dump available")
    write_json_atomic(status_path(cfg), status)

    if succeeded:
        heartbeat(cfg)
        log(
            "backup_cycle_succeeded",
            file=newest.name if newest else None,
            key=status.get("last_success_key"),
            offsite=cfg.offsite,
        )
        return EXIT_OK
    log("backup_cycle_failed", errors=len(errors), pending_uploads=len(pending_after), error=status["last_error"])
    return EXIT_FAILED


# --- operator commands -------------------------------------------------------


def cmd_status(cfg: Config) -> int:
    status = load_status(cfg)
    print(redact(json.dumps(status or {"detail": "no backup cycle has run yet"}, indent=2, sort_keys=True)))
    return EXIT_OK


def cmd_health(cfg: Config) -> int:
    last = load_status(cfg).get("last_success_at")
    if not last:
        print("UNHEALTHY: no successful backup recorded")
        return EXIT_FAILED
    age = (utcnow() - dt.datetime.strptime(last, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.UTC)).total_seconds()
    if age > cfg.stale_after_seconds:
        print(f"UNHEALTHY: last verified backup is {int(age)}s old (limit {cfg.stale_after_seconds}s)")
        return EXIT_FAILED
    print(f"OK: last verified backup {int(age)}s ago")
    return EXIT_OK


def cmd_list(cfg: Config) -> int:
    s3 = make_s3(cfg)
    items = with_retries(cfg, "list backups", lambda: list_remote(cfg, s3))
    print(f"Off-site backups in {cfg.bucket}/{cfg.prefix}/{cfg.safe_db}/ (newest first):")
    if not items:
        print("  (none)")
        return EXIT_FAILED
    for obj in items:
        print(f"  {obj['LastModified']:%Y-%m-%d %H:%M}Z  {int(obj['Size']):>12,} B  {obj['Key']}")
    return EXIT_OK


def key_scope(auth: Mapping[str, Any]) -> tuple[set[str], list[str]]:
    """(capabilities, buckets the key is limited to) from b2_authorize_account.

    b2api v3 puts the scope directly on storageApi; v2 nested it under
    "allowed". Reading only the v2 shape saw no capabilities and no bucket
    restriction for every real key — a check that could never tell the truth.
    An empty bucket list means the key is NOT restricted to any bucket.
    """
    storage = storage_api(auth)
    allowed = storage.get("allowed") or storage
    caps = {str(c) for c in allowed.get("capabilities") or []}
    buckets = [str(b.get("name")) for b in allowed.get("buckets") or [] if isinstance(b, dict)]
    if not buckets and allowed.get("bucketName"):
        buckets = [str(allowed["bucketName"])]
    return caps, buckets


def lifecycle_verdict(cfg: Config, rules: list[Any]) -> tuple[bool, str]:
    """Remote expiry is the bucket's lifecycle rule, never this program.

    A rule must cover the backup prefix, hide files only AFTER their lock has
    expired (earlier and B2 must refuse the deletion it schedules), and
    actually delete hidden versions — otherwise backups accumulate forever.
    """
    wanted = f"{cfg.prefix}/"
    for rule in rules:
        if not isinstance(rule, dict):
            continue
        prefix = str(rule.get("fileNamePrefix", ""))
        if not wanted.startswith(prefix):
            continue
        hide = rule.get("daysFromUploadingToHiding")
        delete = rule.get("daysFromHidingToDeleting")
        if not isinstance(hide, int) or not isinstance(delete, int):
            return False, f"rule for '{prefix}' does not both hide and delete (hide={hide}, delete={delete})"
        if cfg.lock_mode != "NONE" and hide <= cfg.lock_days:
            return False, f"rule for '{prefix}' hides on day {hide}, not after the {cfg.lock_days}-day lock"
        return True, f"prefix '{prefix}': hide day {hide}, delete {delete} day(s) later (lock {cfg.lock_days}d)"
    return False, f"no lifecycle rule covers '{wanted}' — backups would never expire (see docs/RUNBOOK.md)"


def cmd_check(cfg: Config) -> int:
    results: list[tuple[str, bool | None, str]] = []

    def add(name: str, ok: bool | None, detail: str) -> None:
        results.append((name, ok, redact(detail)))

    endpoint = cfg.endpoint
    if not cfg.endpoint or "backblazeb2.com" in cfg.endpoint:
        auth = b2_authorize(cfg)
        caps, buckets = key_scope(auth)
        add("key authorizes", True, "b2_authorize_account succeeded")
        add(
            "key restricted to bucket",
            buckets == [cfg.bucket],
            f"key may access: {', '.join(str(b) for b in buckets) or 'ALL BUCKETS'}",
        )
        missing = sorted(REQUIRED_CAPABILITIES - caps)
        if cfg.lock_mode != "NONE":
            missing += sorted(LOCK_CAPABILITIES - caps)
        add("key has required capabilities", not missing, "missing: " + (", ".join(missing) or "none"))
        excess = sorted(caps & DANGEROUS_CAPABILITIES)
        add(
            "key lacks destructive capabilities",
            not excess,
            "excess: " + (", ".join(excess) or "none"),
        )
        endpoint = endpoint or str(storage_api(auth).get("s3ApiUrl", "")).rstrip("/")
        add("S3 endpoint", bool(endpoint), endpoint or "not reported")
        api_url = storage_api(auth).get("apiUrl")
        if "listBuckets" in caps and api_url:
            body = json.dumps({"accountId": auth.get("accountId"), "bucketName": cfg.bucket}).encode()
            request = urllib.request.Request(
                f"{api_url}/b2api/v4/b2_list_buckets",
                data=body,
                headers={"Authorization": str(auth.get("authorizationToken", ""))},
            )
            try:
                with urllib.request.urlopen(request, timeout=30) as response:
                    listed = json.load(response).get("buckets", [])
            except (urllib.error.URLError, OSError, ValueError) as exc:
                listed = []
                add("bucket settings readable", None, one_line(str(exc)))
            for bucket in listed:
                add("bucket is private", bucket.get("bucketType") == "allPrivate", f"bucketType={bucket.get('bucketType')}")
                enc = bucket.get("defaultServerSideEncryption", {})
                if enc.get("isClientAuthorizedToRead"):
                    mode = (enc.get("value") or {}).get("mode")
                    add("default encryption", mode == "SSE-B2", f"mode={mode}")
                lock = bucket.get("fileLockConfiguration", {})
                if lock.get("isClientAuthorizedToRead"):
                    value = lock.get("value") or {}
                    add("object lock enabled", bool(value.get("isFileLockEnabled")), f"defaultRetention={value.get('defaultRetention')}")
                if "readBucketLifecycleRules" in caps or bucket.get("lifecycleRules"):
                    lc_ok, lc_detail = lifecycle_verdict(cfg, bucket.get("lifecycleRules") or [])
                    add("lifecycle rule expires backups after the lock", lc_ok, lc_detail)
                else:
                    add("lifecycle rule expires backups after the lock", None,
                        "not readable: key lacks readBucketLifecycleRules")

    s3 = make_s3(cfg, endpoint)
    try:
        s3.head_bucket(Bucket=cfg.bucket)
        add("bucket reachable over S3", True, cfg.bucket)
    except (ClientError, BotoCoreError) as exc:
        add("bucket reachable over S3", False, one_line(str(exc)))
    try:
        conf = s3.get_object_lock_configuration(Bucket=cfg.bucket).get("ObjectLockConfiguration", {})
        add("object lock (S3 view)", conf.get("ObjectLockEnabled") == "Enabled", f"{conf.get('ObjectLockEnabled')}")
    except (ClientError, BotoCoreError) as exc:
        add("object lock (S3 view)", None, f"not readable with this key: {one_line(str(exc), 120)}")

    # Privacy from the outside: fetch a real backup (or a probe path) with no
    # credentials at all. A private bucket answers 401/403; a public one
    # would serve the file or say 404.
    try:
        latest = list_remote(cfg, s3)
    except (ClientError, BotoCoreError):
        latest = []
    probe_key = str(latest[0]["Key"]) if latest else f"{cfg.prefix}/anonymous-probe"
    anon_status: int | str
    try:
        with urllib.request.urlopen(f"{endpoint}/{cfg.bucket}/{probe_key}", timeout=20) as response:
            anon_status = response.status
    except urllib.error.HTTPError as exc:
        anon_status = exc.code
    except (urllib.error.URLError, OSError) as exc:
        anon_status = type(exc).__name__
    add("anonymous download refused", anon_status in (401, 403), f"unauthenticated GET -> {anon_status}")

    failed = False
    for name, ok, detail in results:
        mark = "PASS" if ok else ("WARN" if ok is None else "FAIL")
        failed = failed or ok is False
        print(f"[{mark}] {name}: {detail}")
    return EXIT_FAILED if failed else EXIT_OK


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="errs-offsite", description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run-once", help="take a dump, upload all pending dumps, verify")
    run.add_argument("--if-due", action="store_true", help="skip the dump if a recent one exists")
    sub.add_parser("list", help="list off-site backups, newest first")
    dl = sub.add_parser("download", help="download and verify a backup")
    dl.add_argument("key", help="object key from `list`, or 'latest'")
    dl.add_argument("dest", type=Path)
    sub.add_parser("status", help="print the locally recorded backup status")
    sub.add_parser("health", help="exit 0 iff the last verified backup is fresh")
    sub.add_parser("check", help="verify credentials, key scope, privacy and lock settings")
    args = parser.parse_args(argv)

    try:
        cfg = Config.from_env()
        if args.command == "run-once":
            return run_once(cfg, if_due=args.if_due)
        if args.command == "list":
            return cmd_list(cfg)
        if args.command == "download":
            download(cfg, make_s3(cfg), args.key, args.dest)
            return EXIT_OK
        if args.command == "status":
            return cmd_status(cfg)
        if args.command == "health":
            return cmd_health(cfg)
        return cmd_check(cfg)
    except BackupError as exc:
        log(f"{args.command.replace('-', '_')}_failed", error=str(exc))
        return EXIT_FAILED
    except Exception as exc:  # noqa: BLE001 - a traceback could carry a secret; log it redacted
        log(f"{args.command.replace('-', '_')}_crashed", error=f"{type(exc).__name__}: {one_line(str(exc))}")
        return EXIT_FAILED


if __name__ == "__main__":
    sys.exit(main())
