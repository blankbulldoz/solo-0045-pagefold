import sqlite3
from contextlib import contextmanager


SCHEMA = """
CREATE TABLE IF NOT EXISTS images (
    id TEXT PRIMARY KEY, label TEXT NOT NULL, catalog TEXT NOT NULL, body BLOB NOT NULL
);
CREATE TABLE IF NOT EXISTS plans (
    id TEXT PRIMARY KEY, base TEXT NOT NULL REFERENCES images(id),
    left_id TEXT NOT NULL REFERENCES images(id), right_id TEXT NOT NULL REFERENCES images(id),
    items TEXT NOT NULL, decisions TEXT NOT NULL DEFAULT '{}', revision INTEGER NOT NULL DEFAULT 0,
    state TEXT NOT NULL DEFAULT 'open' CHECK(state IN ('open', 'sealed', 'archived'))
);
CREATE TABLE IF NOT EXISTS attempts (
    plan_id TEXT NOT NULL REFERENCES plans(id), revision INTEGER NOT NULL,
    result TEXT NOT NULL, candidate BLOB, PRIMARY KEY(plan_id, revision)
);
CREATE TABLE IF NOT EXISTS releases (
    plan_id TEXT PRIMARY KEY REFERENCES plans(id), certificate TEXT NOT NULL,
    candidate BLOB NOT NULL
);
"""


class Store:
    def __init__(self, path):
        self.path = path
        with self.connect(snapshot=False) as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript(SCHEMA)

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
