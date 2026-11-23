from .errors import Problem


def check_database(db):
    integrity = db.execute("PRAGMA main.integrity_check(1)").fetchone()[0]
    if integrity != "ok":
        raise Problem("integrity", f"SQLite 完整性检查失败：{integrity}")
    missing = db.execute("PRAGMA main.foreign_key_check").fetchmany(12)
    if missing:
        locations = "、".join(f"{row[0]} → {row[2]}（约束 {row[3]}）" for row in missing)
        raise Problem("foreign_key", f"外键检查失败：{locations}")
