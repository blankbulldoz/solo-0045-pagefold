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
    result TEXT NOT NULL, candidate BLOB, token TEXT NOT NULL DEFAULT '',
    started REAL NOT NULL DEFAULT 0, PRIMARY KEY(plan_id, revision)
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
            columns = {row[1] for row in db.execute("PRAGMA main.table_info(attempts)")}
            for name, kind in (("token", "TEXT NOT NULL DEFAULT ''"),
                               ("started", "REAL NOT NULL DEFAULT 0")):
                if name not in columns:
                    db.execute(f"ALTER TABLE attempts ADD COLUMN {name} {kind}")
            # No rehearsal can still be running in a freshly started process, so any
            # "running" claim left by a previous process is settled as interrupted.
            db.execute("""
                UPDATE attempts SET result=json_object(
                    'id', plan_id, 'revision', revision, 'status', 'blocked',
                    'changed', coalesce(json_extract(result, '$.changed'), 0),
                    'problem', json_object('code', 'interrupted',
                                           'detail', '上次重演在进程退出时中止。'))
                WHERE json_extract(result, '$.status') = 'running'
            """)

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
