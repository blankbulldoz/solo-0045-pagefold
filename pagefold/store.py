import sqlite3
from contextlib import contextmanager

from .blobs import BlobStore, blob_path
from .errors import Problem

SCHEMA_VERSION = "2"

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY, value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS images (
    id TEXT PRIMARY KEY, label TEXT NOT NULL, catalog TEXT NOT NULL, size INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS plans (
    id TEXT PRIMARY KEY, base TEXT NOT NULL REFERENCES images(id),
    left_id TEXT NOT NULL REFERENCES images(id), right_id TEXT NOT NULL REFERENCES images(id),
    items TEXT NOT NULL, decisions TEXT NOT NULL DEFAULT '{}', revision INTEGER NOT NULL DEFAULT 0,
    state TEXT NOT NULL DEFAULT 'open' CHECK(state IN ('open', 'sealed', 'archived'))
);
CREATE TABLE IF NOT EXISTS attempts (
    plan_id TEXT NOT NULL REFERENCES plans(id), revision INTEGER NOT NULL,
    result TEXT NOT NULL, candidate_digest TEXT, PRIMARY KEY(plan_id, revision)
);
CREATE TABLE IF NOT EXISTS releases (
    plan_id TEXT PRIMARY KEY REFERENCES plans(id), certificate TEXT NOT NULL,
    candidate_digest TEXT NOT NULL
);
"""


def schema_version(db):
    """返回状态库结构版本：None 为空库，"1" 为内嵌二进制的旧结构。"""
    tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if not tables:
        return None
    if "meta" not in tables:
        return "1"
    row = db.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
    return row[0] if row else "unknown"


class Store:
    def __init__(self, path):
        self.path = path
        self.blobs = BlobStore(blob_path(path))
        with self.connect(snapshot=False) as db:
            db.execute("PRAGMA journal_mode=WAL")
            version = schema_version(db)
            if version is None:
                db.executescript(SCHEMA)
                db.execute("INSERT INTO meta VALUES('schema_version',?)", (SCHEMA_VERSION,))
            elif version != SCHEMA_VERSION:
                raise Problem("state_schema",
                              "状态库仍是内嵌二进制的旧结构，请先运行 "
                              f"python3 -m pagefold.migrate run {path} 迁移后再启动。", 500)

    @contextmanager
    def connect(self, write=False, snapshot=True):
        db = sqlite3.connect(self.path, timeout=8, isolation_level=None)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA foreign_keys=ON")
            if write or snapshot:
                db.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            yield db
            if write:
                db.commit()
        finally:
            db.close()
