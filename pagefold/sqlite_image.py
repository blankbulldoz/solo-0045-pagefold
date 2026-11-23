import sqlite3
from contextlib import contextmanager

from .errors import Problem

MAX_BYTES = 4 * 1024 * 1024


def validate_header(data):
    if len(data) > MAX_BYTES:
        raise Problem("image_size", "镜像超过 4 MiB 上限。", 413)
    if len(data) < 100 or data[:16] != b"SQLite format 3\x00":
        raise Problem("image_header", "文件不是完整的 SQLite 镜像。")
    size = int.from_bytes(data[16:18], "big") or 0
    if size == 1:
        size = 65536
    if size < 512 or size > 65536 or size & (size - 1) or len(data) % size:
        raise Problem("image_pages", "镜像的页大小或文件长度无效。")
    if data[18:20] != b"\x01\x01":
        raise Problem("image_journal", "请先完成检查点并保存为独立的 SQLite 文件。")


@contextmanager
def mounted(data, writable=False):
    validate_header(data)
    db = sqlite3.connect(":memory:", isolation_level=None)
    try:
        db.deserialize(data)
        db.execute("PRAGMA trusted_schema=OFF")
        if not writable:
            db.execute("PRAGMA query_only=ON")
        yield db
    finally:
        db.close()


def ident(name):
    return '"' + name.replace('"', '""') + '"'
