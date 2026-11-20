import json
import sqlite3
import threading
import time
from contextlib import closing, contextmanager

from app.config import db_path
from app.errors import DomainError
from app.manifest import canonical

# 进程内写串行化：并发同编号提交时只允许一个线程进入插入事务
_write_lock = threading.Lock()


@contextmanager
def connection():
    path = db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path, timeout=10)) as conn, conn:
        conn.execute("PRAGMA busy_timeout=10000")
        conn.execute("""CREATE TABLE IF NOT EXISTS runs (
            id TEXT PRIMARY KEY, source TEXT NOT NULL, record TEXT NOT NULL)""")
        conn.execute("""CREATE TABLE IF NOT EXISTS revisions (
            id TEXT PRIMARY KEY, baseline_id TEXT NOT NULL, source TEXT NOT NULL,
            record TEXT NOT NULL, status TEXT NOT NULL)""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_revisions_baseline ON revisions(baseline_id)")
        yield conn


def _with_retry(write):
    """在锁竞争（database is locked）时退避重试，保证并发提交不丢单。"""
    for attempt in range(5):
        try:
            return write()
        except sqlite3.OperationalError as exc:
            if "locked" not in str(exc) or attempt == 4:
                raise
            time.sleep(0.05 * (attempt + 1))


def fetch(run_id):
    with connection() as conn:
        row = conn.execute("SELECT source, record FROM runs WHERE id = ?", (run_id,)).fetchone()
    if row is None:
        raise DomainError("RUN_NOT_FOUND", "找不到该批次", 404)
    return json.loads(row[0]), json.loads(row[1])


def save(source, record):
    def write():
        with _write_lock, connection() as conn:
            inserted = conn.execute(
                "INSERT INTO runs VALUES (?, ?, ?) ON CONFLICT(id) DO NOTHING",
                (record["id"], canonical(source), canonical(record)),
            ).rowcount
            kept = conn.execute("SELECT record FROM runs WHERE id = ?", (record["id"],)).fetchone()[0]
        return inserted, kept

    inserted, kept = _with_retry(write)
    return {**json.loads(kept), "reused": not bool(inserted)}


def fetch_revision(rev_id, allow_missing=False):
    with connection() as conn:
        row = conn.execute("SELECT record FROM revisions WHERE id = ?", (rev_id,)).fetchone()
    if row is None:
        if allow_missing:
            return None
        raise DomainError("REVISION_NOT_FOUND", "找不到该修订联单", 404)
    return json.loads(row[0])


def list_revisions(baseline_id):
    with connection() as conn:
        rows = conn.execute(
            "SELECT record FROM revisions WHERE baseline_id = ? ORDER BY rowid", (baseline_id,)
        ).fetchall()
    return [json.loads(row[0]) for row in rows]


def save_revision(record):
    """幂等写入：同编号并发插入只有一条成功，其余原样取回。"""
    def write():
        with _write_lock, connection() as conn:
            inserted = conn.execute(
                "INSERT INTO revisions VALUES (?, ?, ?, ?, ?) ON CONFLICT(id) DO NOTHING",
                (record["id"], record["baseline_id"], canonical(record["source"]),
                 canonical(record), record["status"]),
            ).rowcount
            kept = conn.execute(
                "SELECT record FROM revisions WHERE id = ?", (record["id"],)).fetchone()[0]
        return inserted, json.loads(kept)

    inserted, kept = _with_retry(write)
    return kept, bool(inserted)


def update_revision_status(rev_id, status, verify_status=None):
    def write():
        with _write_lock, connection() as conn:
            record = json.loads(
                conn.execute("SELECT record FROM revisions WHERE id = ?", (rev_id,)).fetchone()[0])
            record["status"] = status
            if verify_status is not None:
                record["revision"]["verify"]["status"] = verify_status
            conn.execute("UPDATE revisions SET status = ?, record = ? WHERE id = ?",
                         (status, canonical(record), rev_id))
            return record

    return _with_retry(write)
