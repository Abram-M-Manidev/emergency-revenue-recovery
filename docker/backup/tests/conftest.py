"""Fixtures for the backup suite: a real PostgreSQL, a real S3 server with
Object Lock, and a TCP proxy that can cut or throttle the connection between
them. No S3 mocks — every storage call here crosses a socket to a server
that enforces the same rules B2 does."""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import boto3
import pytest
from botocore.config import Config as BotoConfig

import errs_offsite as eo

SRC = Path(__file__).resolve().parent.parent

SCHEMA = """
DROP TABLE IF EXISTS attachments, appointments, emergency_tickets, conversations,
    customers, users, organizations, alembic_version CASCADE;
CREATE TABLE alembic_version (version_num varchar(32) PRIMARY KEY);
INSERT INTO alembic_version VALUES ('testrev0001');
CREATE TABLE organizations (id serial PRIMARY KEY, name text NOT NULL UNIQUE);
CREATE TABLE users (id serial PRIMARY KEY,
    organization_id int NOT NULL REFERENCES organizations(id), email text NOT NULL);
CREATE TABLE customers (id serial PRIMARY KEY,
    organization_id int NOT NULL REFERENCES organizations(id), name text, phone varchar(32));
CREATE TABLE conversations (id serial PRIMARY KEY,
    customer_id int REFERENCES customers(id), transcript text);
CREATE TABLE emergency_tickets (id serial PRIMARY KEY,
    conversation_id int NOT NULL REFERENCES conversations(id), severity text);
CREATE TABLE appointments (id serial PRIMARY KEY,
    customer_id int NOT NULL REFERENCES customers(id), starts_at timestamptz NOT NULL);
CREATE TABLE attachments (id serial PRIMARY KEY, blob bytea NOT NULL);
INSERT INTO organizations (name) SELECT 'Org ' || g FROM generate_series(1, 5) g;
INSERT INTO users (organization_id, email) SELECT 1 + g % 5, 'u' || g || '@example.test'
    FROM generate_series(1, 40) g;
INSERT INTO customers (organization_id, name, phone) SELECT 1 + g % 5, 'Customer ' || g, '555-01' || g
    FROM generate_series(1, 300) g;
INSERT INTO conversations (customer_id, transcript) SELECT 1 + g % 300, repeat('furnace out ', 20)
    FROM generate_series(1, 500) g;
INSERT INTO emergency_tickets (conversation_id, severity) SELECT 1 + g % 500, 'high'
    FROM generate_series(1, 60) g;
INSERT INTO appointments (customer_id, starts_at) SELECT 1 + g % 300, now() + g * interval '1 hour'
    FROM generate_series(1, 200) g;
-- ~6 MiB of incompressible bytes: large enough to interrupt an upload in
-- flight and to exercise the multipart path with S3's 5 MiB minimum part.
INSERT INTO attachments (blob)
    SELECT decode(string_agg(md5(random()::text || g), ''), 'hex')
    FROM generate_series(1, 400000) g GROUP BY g % 6;
"""

# The comparison a restore must pass: every table's row count, plus a digest
# of representative rows, identical between source and restored database.
FINGERPRINT_SQL = """
SELECT 'organizations', count(*), md5(string_agg(id || ':' || name, ',' ORDER BY id)) FROM organizations
UNION ALL SELECT 'users', count(*), md5(string_agg(id || ':' || email, ',' ORDER BY id)) FROM users
UNION ALL SELECT 'customers', count(*), md5(string_agg(id || ':' || phone, ',' ORDER BY id)) FROM customers
UNION ALL SELECT 'conversations', count(*), md5(string_agg(id::text, ',' ORDER BY id)) FROM conversations
UNION ALL SELECT 'emergency_tickets', count(*), md5(string_agg(id::text, ',' ORDER BY id)) FROM emergency_tickets
UNION ALL SELECT 'appointments', count(*), md5(string_agg(id || ':' || starts_at, ',' ORDER BY id)) FROM appointments
UNION ALL SELECT 'attachments', count(*), md5(string_agg(md5(blob), ',' ORDER BY id)) FROM attachments
UNION ALL SELECT 'foreign_keys', count(*), string_agg(conname, ',' ORDER BY conname)
    FROM pg_constraint WHERE contype = 'f' AND convalidated
"""


def psql(sql: str, db: str | None = None) -> str:
    cmd = ["psql", "-X", "-q", "-A", "-t", "-v", "ON_ERROR_STOP=1", "-c", sql]
    if db:
        cmd += ["-d", db]
    return subprocess.run(cmd, check=True, capture_output=True, text=True).stdout.strip()


def fingerprint(db: str) -> str:
    return psql(FINGERPRINT_SQL, db)


def fresh_database() -> str:
    name = f"restore_{uuid.uuid4().hex[:10]}"
    psql(f'CREATE DATABASE "{name}"', "postgres")
    return name


@pytest.fixture(scope="session", autouse=True)
def seeded_source_db() -> str:
    psql(SCHEMA)
    return os.environ.get("PGDATABASE", "errs")


def s3_admin(endpoint: str | None = None) -> Any:
    return boto3.client(
        "s3",
        endpoint_url=endpoint or os.environ["TEST_S3_ENDPOINT"],
        region_name="us-east-1",
        aws_access_key_id=os.environ["TEST_S3_ACCESS"],
        aws_secret_access_key=os.environ["TEST_S3_SECRET"],
        config=BotoConfig(
            s3={"addressing_style": "path"},
            request_checksum_calculation="when_required",
            response_checksum_validation="when_required",
        ),
    )


def wait_for_s3() -> None:
    deadline = time.time() + 30
    while True:
        try:
            s3_admin().list_buckets()
            return
        except Exception:
            if time.time() > deadline:
                raise
            time.sleep(0.5)


@pytest.fixture
def bucket() -> str:
    wait_for_s3()
    name = f"errs-test-{uuid.uuid4().hex[:12]}"
    s3_admin().create_bucket(Bucket=name, ObjectLockEnabledForBucket=True)
    return name


@pytest.fixture
def backup_env(
    tmp_path: Path, bucket: str, monkeypatch: pytest.MonkeyPatch
) -> Callable[..., eo.Config]:
    """Point the module at the test bucket through the real environment,
    exactly as the container does, and hand back a Config factory."""
    base = {
        "BACKUP_DIR": str(tmp_path / "backups"),
        "B2_APPLICATION_KEY_ID": os.environ["TEST_S3_ACCESS"],
        "B2_APPLICATION_KEY": os.environ["TEST_S3_SECRET"],
        "B2_BUCKET": bucket,
        "B2_S3_ENDPOINT": os.environ["TEST_S3_ENDPOINT"],
        # versitygw implements Object Lock but not SSE; B2's SSE is verified
        # by the live test instead.
        "BACKUP_SSE": "",
        "BACKUP_OBJECT_LOCK_DAYS": "1",
        "BACKUP_UPLOAD_ATTEMPTS": "3",
        "BACKUP_RETRY_BASE_SECONDS": "0.05",
        "BACKUP_CONNECT_TIMEOUT_SECONDS": "3",
        "BACKUP_READ_TIMEOUT_SECONDS": "10",
    }
    for name, value in base.items():
        monkeypatch.setenv(name, value)

    def make(**overrides: str) -> eo.Config:
        for name, value in overrides.items():
            monkeypatch.setenv(name, value)
        return eo.Config.from_env()

    return make


def run_cli(*args: str, env_overrides: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env.update(env_overrides or {})
    return subprocess.run(
        [sys.executable, str(SRC / "errs_offsite.py"), *args], capture_output=True, text=True, env=env
    )


class FaultProxy:
    """TCP proxy in front of the S3 server.

    drop_uploads: how many connections to cut (with a RST) once the client
      has sent more than drop_after bytes on them — an upload dying mid-body.
    throttle_bps: cap client->server throughput, so a test can kill the
      uploader at a known point.
    """

    def __init__(self, upstream: str, *, drop_uploads: int = 0, drop_after: int = 256 * 1024,
                 throttle_bps: int | None = None) -> None:
        host, port = upstream.removeprefix("http://").split(":")
        self.upstream = (host, int(port))
        self.drop_uploads = drop_uploads
        self.drop_after = drop_after
        self.throttle_bps = throttle_bps
        self.bytes_up = 0
        self.dropped = 0
        self._lock = threading.Lock()
        self.server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.server.bind(("127.0.0.1", 0))
        self.server.listen(16)
        self.endpoint = f"http://127.0.0.1:{self.server.getsockname()[1]}"
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self) -> None:
        while True:
            try:
                client, _ = self.server.accept()
            except OSError:
                return
            upstream = socket.create_connection(self.upstream)
            threading.Thread(target=self._pump_up, args=(client, upstream), daemon=True).start()
            threading.Thread(target=self._pump_down, args=(upstream, client), daemon=True).start()

    @staticmethod
    def _reset(*socks: socket.socket) -> None:
        for s in socks:
            try:
                s.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, b"\x01\x00\x00\x00\x00\x00\x00\x00")
                s.close()
            except OSError:
                pass

    def _pump_up(self, src: socket.socket, dst: socket.socket) -> None:
        sent = 0
        try:
            while data := src.recv(16384):
                sent += len(data)
                with self._lock:
                    self.bytes_up += len(data)
                    cut = self.dropped < self.drop_uploads and sent > self.drop_after
                    if cut:
                        self.dropped += 1
                if cut:
                    self._reset(src, dst)
                    return
                if self.throttle_bps:
                    time.sleep(len(data) / self.throttle_bps)
                dst.sendall(data)
        except OSError:
            pass
        self._reset(src, dst)

    @staticmethod
    def _pump_down(src: socket.socket, dst: socket.socket) -> None:
        try:
            while data := src.recv(16384):
                dst.sendall(data)
        except OSError:
            pass

    def close(self) -> None:
        self.server.close()


@pytest.fixture
def fault_proxy() -> Iterator[Callable[..., FaultProxy]]:
    proxies: list[FaultProxy] = []

    def make(**kwargs: Any) -> FaultProxy:
        proxy = FaultProxy(os.environ["TEST_S3_ENDPOINT"], **kwargs)
        proxies.append(proxy)
        return proxy

    yield make
    for proxy in proxies:
        proxy.close()
