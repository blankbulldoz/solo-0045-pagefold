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

# 升级后的重演记录会把当次裁决快照随证据一起固定下来。升级前写入的旧记录
# 没有这些列，读取时以 choices IS NULL 识别为旧版记录（legacy）。
ATTEMPT_COLUMNS = {
    "choices": "TEXT",
    "decision_digest": "TEXT",
    "requested_at": "TEXT NOT NULL DEFAULT ''",
    "sequence": "INTEGER NOT NULL DEFAULT 0",
}

# 裁决事件按写入顺序追加，同版同值的幂等重试也保留独立记录。
EVENT_SQL = """
CREATE TABLE IF NOT EXISTS decision_events (
    plan_id TEXT NOT NULL REFERENCES plans(id),
    seq INTEGER NOT NULL,
    revision INTEGER NOT NULL,
    item TEXT NOT NULL,
    choice TEXT,
    advanced INTEGER NOT NULL,
    recorded_at TEXT NOT NULL DEFAULT '',
    PRIMARY KEY(plan_id, seq)
)
"""


def _column_names(db, table):
    return {row[1] for row in db.execute(f"PRAGMA table_info({table})")}


def migrate(db):
    db.executescript(SCHEMA)
    existing = _column_names(db, "attempts")
    for name, declaration in ATTEMPT_COLUMNS.items():
        if name not in existing:
            db.execute(f"ALTER TABLE attempts ADD COLUMN {name} {declaration}")
    db.executescript(EVENT_SQL)


class Store:
    def __init__(self, path):
        self.path = path
        with self.connect(snapshot=False) as db:
            db.execute("PRAGMA journal_mode=WAL")
            migrate(db)

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
