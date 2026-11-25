"""组装发往供应方的 Pagefold 合规/拒绝样件。

每个坏样件只暴露一个问题，并在 fixed/ 下提供可修正到接收成功的副本；
原始坏样件生成后不再改写，供复查。本脚本只使用标准库，路径一律以参数传入，
可用 `python3 -I tools/build_kit.py <输出目录>` 运行。
"""
import argparse
import hashlib
import json
import shutil
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pagefold.catalog import describe  # noqa: E402
from pagefold.sqlite_image import mounted  # noqa: E402


def sha256_file(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_bytes(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def catalog_of(data):
    with mounted(data) as db:
        return describe(db)


def index_ddl(data, table, index_name):
    with mounted(data) as db:
        row = db.execute(
            "SELECT sql FROM main.sqlite_schema WHERE type='index' AND name=?",
            (index_name,)).fetchone()
        parts = db.execute(
            "PRAGMA main.index_xinfo(%s)" % '"%s"' % index_name).fetchall()
        columns = [p[2] for p in parts if p[5]]
    return row[0], columns


# ---------------------------------------------------------------- 场景 1
def build_generated_column(out):
    case = out / "01-generated-column"
    bad = case / "bad" / "part-ledger-generated.sqlite"
    fixed = case / "fixed" / "part-ledger-plain.sqlite"

    db = sqlite3.connect(":memory:")
    db.execute(
        "CREATE TABLE part_ledger("
        "sku TEXT PRIMARY KEY, qty INTEGER NOT NULL, "
        "total INTEGER GENERATED ALWAYS AS (qty * 2) VIRTUAL)")
    db.executemany("INSERT INTO part_ledger(sku, qty) VALUES (?, ?)",
                   [("SKU-1", 7), ("SKU-2", 3)])
    db.commit()
    bad_data = db.serialize()
    db.close()
    write_bytes(bad, bad_data)

    # 修正：生成列改为可写入的普通列，值按同一表达式预计算后落库。
    db = sqlite3.connect(":memory:")
    db.execute(
        "CREATE TABLE part_ledger("
        "sku TEXT PRIMARY KEY, qty INTEGER NOT NULL, total INTEGER NOT NULL)")
    db.executemany("INSERT INTO part_ledger(sku, qty, total) VALUES (?, ?, ?)",
                   [("SKU-1", 7, 14), ("SKU-2", 3, 6)])
    db.commit()
    fixed_data = db.serialize()
    db.close()
    write_bytes(fixed, fixed_data)

    with mounted(bad_data) as db:
        xinfo = db.execute("PRAGMA table_xinfo(part_ledger)").fetchall()
    bad_flags = {col[1]: col[6] for col in xinfo}
    with mounted(fixed_data) as db:
        xinfo = db.execute("PRAGMA table_xinfo(part_ledger)").fetchall()
    fixed_flags = {col[1]: col[6] for col in xinfo}

    return {
        "case": "01-generated-column",
        "point": "普通表与生成列的差别（table_xinfo hidden 标志非 0 即不可写）",
        "bad": {
            "path": str(bad.relative_to(out)),
            "sha256": sha256_file(bad),
            "size": bad.stat().st_size,
            "column_hidden_flags": bad_flags,
            "expected_rejection": {"http": 422, "code": "table_shape"},
        },
        "fixed": {
            "path": str(fixed.relative_to(out)),
            "sha256": sha256_file(fixed),
            "size": fixed.stat().st_size,
            "column_hidden_flags": fixed_flags,
            "expected": {"http": 201},
        },
    }


# ---------------------------------------------------------------- 场景 2
def build_wal_file(out):
    case = out / "02-wal-runtime-file"
    bad = case / "bad" / "sensor-reading-wal.sqlite"
    fixed = case / "fixed" / "sensor-reading-standalone.sqlite"
    bad.parent.mkdir(parents=True, exist_ok=True)

    if bad.exists():
        bad.unlink()
    db = sqlite3.connect(bad)
    mode = db.execute("PRAGMA journal_mode=WAL").fetchone()[0]
    db.execute(
        "CREATE TABLE sensor_reading("
        "id INTEGER PRIMARY KEY, taken_at TEXT NOT NULL, value REAL NOT NULL)")
    db.executemany("INSERT INTO sensor_reading VALUES (?, ?, ?)",
                   [(1, "2026-11-25T09:00Z", 36.5), (2, "2026-11-25T09:05Z", 37.1)])
    db.commit()
    db.close()
    bad_data = bad.read_bytes()
    bad_hash_before = sha256_file(bad)

    # 修正只在副本上进行：检查点并切回 rollback journal，原始坏样件保持不动。
    fixed.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(bad, fixed)
    db = sqlite3.connect(fixed)
    checkpoint = db.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchall()
    mode_after = db.execute("PRAGMA journal_mode=DELETE").fetchone()[0]
    db.close()
    fixed_data = fixed.read_bytes()

    sidecars = sorted(p.name for p in bad.parent.iterdir() if p != bad)
    return {
        "case": "02-wal-runtime-file",
        "point": "运行中的 WAL 文件与独立镜像的差别（文件头偏移 18、19 字节）",
        "bad": {
            "path": str(bad.relative_to(out)),
            "sha256": sha256_file(bad),
            "size": bad.stat().st_size,
            "journal_mode": mode,
            "header_bytes_18_19_hex": bad_data[18:20].hex(),
            "sidecars_left_in_bad_dir": sidecars,
            "expected_rejection": {"http": 422, "code": "image_journal"},
        },
        "fix_steps": [
            "复制坏样件（不直接改原件）",
            "PRAGMA wal_checkpoint(TRUNCATE);",
            "PRAGMA journal_mode=DELETE;",
        ],
        "checkpoint_result": checkpoint,
        "fixed": {
            "path": str(fixed.relative_to(out)),
            "sha256": sha256_file(fixed),
            "size": fixed.stat().st_size,
            "journal_mode": mode_after,
            "header_bytes_18_19_hex": fixed_data[18:20].hex(),
            "expected": {"http": 201},
        },
        "original_bad_unchanged": sha256_file(bad) == bad_hash_before,
    }


# ---------------------------------------------------------------- 场景 3
def build_same_index_name(out):
    case = out / "03-same-index-name"
    index_name = "stock_lookup"
    rows = [("SKU-1", "A-01", 7), ("SKU-2", "B-02", 3)]

    def make(index_column):
        db = sqlite3.connect(":memory:")
        db.execute(
            "CREATE TABLE inventory("
            "sku TEXT PRIMARY KEY, bin TEXT NOT NULL, qty INTEGER NOT NULL)")
        db.execute(
            f"CREATE INDEX {index_name} ON inventory({index_column})")
        db.executemany("INSERT INTO inventory VALUES (?, ?, ?)", rows)
        db.commit()
        data = db.serialize()
        db.close()
        ddl, columns = index_ddl(data, "inventory", index_name)
        signature = catalog_of(data)["signature"]
        return data, {"ddl": ddl, "index_columns": columns, "signature": signature}

    bad_specs = {
        "base": "bin",
        "left": "qty",
        "right": "bin",
    }
    bad_entries = {}
    for role, column in bad_specs.items():
        data, meta = make(column)
        path = case / "bad" / f"inventory-{role}.sqlite"
        write_bytes(path, data)
        bad_entries[role] = {
            "path": str(path.relative_to(out)),
            "sha256": sha256_file(path),
            "size": path.stat().st_size,
            **meta,
            "upload_expected": {"http": 201},
        }

    fixed_entries = {}
    for role in ("base", "left", "right"):
        data, meta = make("bin")
        path = case / "fixed" / f"inventory-{role}.sqlite"
        write_bytes(path, data)
        fixed_entries[role] = {
            "path": str(path.relative_to(out)),
            "sha256": sha256_file(path),
            "size": path.stat().st_size,
            **meta,
        }

    bad_sigs = {role: entry["signature"] for role, entry in bad_entries.items()}
    fixed_sigs = {role: entry["signature"] for role, entry in fixed_entries.items()}
    return {
        "case": "03-same-index-name",
        "point": "索引名称相同不代表目录相同：目录指纹覆盖完整建表/建索引 SQL 记录",
        "index_name_shared": index_name,
        "bad": {
            "entries": bad_entries,
            "signatures": bad_sigs,
            "signatures_identical": len(set(bad_sigs.values())) == 1,
            "expected_plan_rejection": {"http": 409, "code": "schema_mismatch"},
        },
        "fixed": {
            "entries": fixed_entries,
            "signatures": fixed_sigs,
            "signatures_identical": len(set(fixed_sigs.values())) == 1,
            "expected_plan": {"http": 201},
            "expected_rehearse": {"status": "passed", "changed": 0},
        },
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("out", type=Path, help="样件输出目录（新建独立目录）")
    args = parser.parse_args()
    out = args.out.resolve()
    out.mkdir(parents=True, exist_ok=True)
    manifest = {
        "kit": "pagefold-compliance-samples",
        "sqlite_version": sqlite3.sqlite_version,
        "cases": [
            build_generated_column(out),
            build_wal_file(out),
            build_same_index_name(out),
        ],
    }
    manifest_path = out / "manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"written": str(out), "manifest": str(manifest_path)},
                     ensure_ascii=False))


if __name__ == "__main__":
    main()
