import sqlite3
from collections import defaultdict

from .codec import canonical, unpack
from .constraints import check_database
from .errors import Problem
from .merge import selected
from .rows import read_image
from .sqlite_image import ident, mounted, validate_header


def amendments(items, decisions):
    changed = defaultdict(list)
    for item in items:
        role, row = selected(item, decisions)
        if role is None:
            raise Problem("unresolved", "存在未裁决的行，请先完成裁决。", 409)
        if row != item["base"]:
            changed[item["table"]].append((item, row))
    return changed


def apply_rows(db, table, operations):
    name = ident(table["name"])
    predicate = " AND ".join(f"{ident(key)} COLLATE BINARY=?" for key in table["key"])
    columns = ",".join(map(ident, table["columns"]))
    params = ",".join("?" for _ in table["columns"])
    for item, _ in operations:
        if item["base"] is not None:
            db.execute(f"DELETE FROM main.{name} WHERE {predicate}", tuple(map(unpack, item["key"])))
    for item, row in operations:
        if row is not None:
            try:
                db.execute(f"INSERT OR ABORT INTO main.{name} ({columns}) VALUES ({params})", tuple(map(unpack, row)))
            except sqlite3.IntegrityError as exc:
                raise Problem("constraint", f"{table['name']} {canonical(item['key'])}：{exc}") from exc


def rebuild(ancestor, catalog, operations):
    with mounted(ancestor, writable=True) as db:
        expected = read_image(db, catalog)
        db.execute("PRAGMA foreign_keys=OFF")
        db.execute("BEGIN")
        for table in catalog["tables"]:
            name = table["name"]
            changes = operations.get(name, [])
            apply_rows(db, table, changes)
            for item, row in changes:
                key = canonical(item["key"])
                if row is None:
                    expected[name].pop(key, None)
                else:
                    expected[name][key] = row
        check_database(db)
        if read_image(db, catalog) != expected:
            raise Problem("row_conversion", "写入后读取到的行与计划目标不一致。")
        db.commit()
        data = db.serialize()
        validate_header(data)
        return data


def port_operations(items, decisions):
    """移植重演的操作集合，未裁决条目直接拒绝。"""
    changed = defaultdict(list)
    for item in items:
        role, row = selected(item, decisions)
        if role is None:
            raise Problem("unresolved", "存在未裁决的移植行，请先完成保留或重新选择。", 409)
        changed[item["table"]].append((item, row))
    return changed


def apply_port_rows(db, table, operations, current):
    """在新祖本（目标库）副本上按当前实际状态对操作做幂等对账。

    与普通重演不同，目标行可能已提前到位：已相等的写操作记为空操作，
    只对真正需要的键执行删除/插入，避免把"提前到位"误算成碰撞。
    返回 (执行的写入数, 空操作数)。
    """
    name = ident(table["name"])
    predicate = " AND ".join(f"{ident(key)} COLLATE BINARY=?" for key in table["key"])
    columns = ",".join(map(ident, table["columns"]))
    params = ",".join("?" for _ in table["columns"])
    applied = skipped = 0
    for item, row in operations:
        key = canonical(item["key"])
        present = current[table["name"]].get(key)
        if row is None:
            if present is not None:
                db.execute(f"DELETE FROM main.{name} WHERE {predicate}",
                           tuple(map(unpack, item["key"])))
                applied += 1
            else:
                skipped += 1
            continue
        if present == row:
            skipped += 1  # 目标库已是目标值：提前到位，无需写入
            continue
        if present is not None:
            db.execute(f"DELETE FROM main.{name} WHERE {predicate}",
                       tuple(map(unpack, item["key"])))
        try:
            db.execute(f"INSERT OR ABORT INTO main.{name} ({columns}) VALUES ({params})",
                       tuple(map(unpack, row)))
        except sqlite3.IntegrityError as exc:
            raise Problem("constraint", f"{table['name']} {canonical(item['key'])}：{exc}") from exc
        applied += 1
    return applied, skipped


def rebuild_port(target, catalog, operations):
    """以新祖本副本为起点重演移植，并按重读结果对账。"""
    with mounted(target, writable=True) as db:
        current = read_image(db, catalog)
        expected = {name: dict(rows) for name, rows in current.items()}
        db.execute("PRAGMA foreign_keys=OFF")
        db.execute("BEGIN")
        applied = skipped = 0
        for table in catalog["tables"]:
            changes = operations.get(table["name"], [])
            done, miss = apply_port_rows(db, table, changes, current)
            applied += done
            skipped += miss
            for item, row in changes:
                key = canonical(item["key"])
                if row is None:
                    expected[table["name"]].pop(key, None)
                else:
                    expected[table["name"]][key] = row
        check_database(db)
        if read_image(db, catalog) != expected:
            raise Problem("row_conversion", "写入后读取到的行与移植目标不一致。")
        db.commit()
        data = db.serialize()
        validate_header(data)
        return data, applied, skipped
