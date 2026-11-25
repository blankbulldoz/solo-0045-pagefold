"""生成四组评审样本镜像与文件清单。

用法（在仓库根目录执行）：
    python3 -m review.make_samples           生成 review/images/、MANIFEST.md、manifest.json
    python3 -m review.make_samples --check   校验已生成镜像与确定性重生成逐字节一致

差异表由本模块用独立实现（直接 SQL 对比，不依赖 pagefold）计算，
供评审时与 pagefold 计划条目交叉核对（见 run_review）。
"""

import argparse
import hashlib
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

from review.groups import GROUPS, KEY_COLUMNS, SCHEMA

ROOT = Path(__file__).resolve().parent


def fresh_db():
    db = sqlite3.connect(":memory:", isolation_level=None)
    db.execute("PRAGMA foreign_keys=ON")
    db.executescript(SCHEMA)
    return db


def mutate(base, statements):
    """在祖本副本上按顺序应用变更，得到分支镜像（来源 = 祖本 + 这些语句）。"""
    db = sqlite3.connect(":memory:", isolation_level=None)
    try:
        db.deserialize(base)
        db.execute("PRAGMA foreign_keys=ON")
        for sql, params in statements:
            if isinstance(params, list):
                db.executemany(sql, params)
            else:
                db.execute(sql, params)
        return db.serialize()
    finally:
        db.close()


def build_images(spec):
    db = fresh_db()
    try:
        spec["build_base"](db)
        base = db.serialize()
    finally:
        db.close()
    return {"base": base,
            "left": mutate(base, spec["left"]["sql"]),
            "right": mutate(base, spec["right"]["sql"])}


def snapshot(data):
    """以主键为索引读取全部行（独立于 pagefold 的简单实现，用于交叉核对）。"""
    db = sqlite3.connect(":memory:")
    try:
        db.deserialize(data)
        result = {}
        for table, keys in KEY_COLUMNS.items():
            columns = [row[1] for row in db.execute(f"PRAGMA table_xinfo({table})") if not row[6]]
            positions = [columns.index(key) for key in keys]
            rows = {}
            for raw in db.execute(f"SELECT * FROM {table}"):
                rows[tuple(raw[p] for p in positions)] = raw
            result[table] = rows
        return result
    finally:
        db.close()


def side_delta(ancestor, row):
    if ancestor is None:
        return "add"
    if row is None:
        return "del"
    return "change" if ancestor != row else "same"


def diff_snapshots(snaps):
    """按键分类：三份相同 / 仅左变 / 仅右变 / 双侧同值 / 双侧冲突。"""
    tables = {}
    totals = {"identical": 0, "items": 0, "automatic": 0, "conflict": 0}
    for table in KEY_COLUMNS:
        base, left, right = (snaps[role][table] for role in ("base", "left", "right"))
        stats = {"base_rows": len(base), "identical": 0,
                 "left": {"add": 0, "del": 0, "change": 0},
                 "right": {"add": 0, "del": 0, "change": 0},
                 "both_same": {"add": 0, "del": 0, "change": 0},
                 "conflict": 0}
        for key in set(base) | set(left) | set(right):
            ancestor, lrow, rrow = base.get(key), left.get(key), right.get(key)
            if lrow == rrow == ancestor:
                stats["identical"] += 1
            elif lrow == rrow:
                stats["both_same"][side_delta(ancestor, lrow)] += 1
            elif rrow == ancestor:
                stats["left"][side_delta(ancestor, lrow)] += 1
            elif lrow == ancestor:
                stats["right"][side_delta(ancestor, rrow)] += 1
            else:
                stats["conflict"] += 1
        items = sum(stats["left"].values()) + sum(stats["right"].values()) \
            + sum(stats["both_same"].values()) + stats["conflict"]
        stats["items"] = items
        tables[table] = stats
        totals["identical"] += stats["identical"]
        totals["items"] += items
        totals["automatic"] += items - stats["conflict"]
        totals["conflict"] += stats["conflict"]
    return {"tables": tables, "totals": totals}


def verify_expectations(spec, diff):
    expect, totals = spec["expect"], diff["totals"]
    problems = []
    if totals["items"] != expect["items"]:
        problems.append(f"条目 {totals['items']} != 预期 {expect['items']}")
    if totals["automatic"] != expect["automatic"]:
        problems.append(f"自动 {totals['automatic']} != 预期 {expect['automatic']}")
    if totals["conflict"] != expect["conflicts"]:
        problems.append(f"冲突 {totals['conflict']} != 预期 {expect['conflicts']}")
    if problems:
        raise SystemExit(f"{spec['id']} 独立差异与预期不符：" + "；".join(problems))


def build_group(spec):
    images = build_images(spec)
    snaps = {role: snapshot(data) for role, data in images.items()}
    diff = diff_snapshots(snaps)
    verify_expectations(spec, diff)
    files = {}
    for role, data in images.items():
        files[role] = {"path": f"review/images/{spec['id']}/{role}.sqlite",
                       "bytes": len(data),
                       "sha256": hashlib.sha256(data).hexdigest(),
                       "rows": {table: len(snaps[role][table]) for table in KEY_COLUMNS}}
    return {"id": spec["id"], "title": spec["title"], "purpose": spec["purpose"],
            "provenance": {"base": spec["base_note"], "left": spec["left"]["note"],
                           "right": spec["right"]["note"], "decisions": spec["decision_note"]},
            "images": images, "files": files, "diff": diff, "expect": spec["expect"]}


def fmt_delta(delta):
    return f"{delta['add']}/{delta['change']}/{delta['del']}"


def render_manifest(metas, generated_at):
    lines = [
        "# Pagefold 评审样本 · 文件清单与差异表",
        "",
        "- 生成：`python3 -m review.make_samples`（确定性生成，无随机因素；"
        "`python3 -m review.make_samples --check` 校验逐字节复现）",
        f"- 生成时间：{generated_at:%Y-%m-%d %H:%M:%S} UTC",
        "- 模式：`entry(id INTEGER PRIMARY KEY, name TEXT, qty INTEGER, weight REAL, note TEXT) STRICT`"
        " ＋ `tag(entry_id, label) STRICT, WITHOUT ROWID`（外键指向 entry）＋ 索引 `tag_label`；",
        "  每组三份镜像的建表与索引记录逐字节一致，满足工作区的模式匹配要求。",
        "- 镜像文件不入库（`.gitignore` 忽略 `*.sqlite`），运行生成脚本即可重建相同文件。",
        "",
        "## 文件清单",
        "",
        "| 文件 | 字节 | entry 行 | tag 行 | SHA-256 |",
        "|---|--:|--:|--:|---|",
    ]
    for meta in metas:
        for role in ("base", "left", "right"):
            f = meta["files"][role]
            lines.append(f"| `{f['path']}` | {f['bytes']:,} | {f['rows']['entry']:,} "
                         f"| {f['rows']['tag']:,} | `{f['sha256']}` |")
    lines += ["", "## 汇总：输入体积 vs 预期计划条目", "",
              "| 组 | 输入行合计 | 输入字节合计 | 三份相同行 | 预期计划条目 | 其中冲突 | 条目/千行输入 |",
              "|---|--:|--:|--:|--:|--:|--:|"]
    for meta in metas:
        rows_in = sum(f["rows"]["entry"] + f["rows"]["tag"] for f in meta["files"].values())
        bytes_in = sum(f["bytes"] for f in meta["files"].values())
        totals, expect = meta["diff"]["totals"], meta["expect"]
        per_k = expect["items"] / (rows_in / 1000)
        lines.append(f"| {meta['id']}（{meta['title']}） | {rows_in:,} | {bytes_in:,} "
                     f"| {totals['identical']:,} | {expect['items']:,} | {expect['conflicts']} "
                     f"| {per_k:.2f} |")
    lines.append("")
    for meta in metas:
        prov = meta["provenance"]
        lines += [f"## {meta['id']} — {meta['title']}", "",
                  f"用途：{meta['purpose']}", "",
                  "来源（祖本 → 分支，均为确定性生成）：",
                  f"- 祖本：{prov['base']}",
                  f"- 左支 = 祖本 + ：{prov['left']}",
                  f"- 右支 = 祖本 + ：{prov['right']}",
                  f"- 评审裁决：{prov['decisions']}", "",
                  "差异表（独立于 pagefold 的 SQL 逐键对比；“增/改/删”相对祖本）：", "",
                  "| 表 | 祖本行 | 三份相同 | 仅左支变 | 仅右支变 | 双侧同值 | 双侧冲突 |",
                  "|---|--:|--:|--:|--:|--:|--:|"]
        for table, stats in meta["diff"]["tables"].items():
            lines.append(f"| {table} | {stats['base_rows']:,} | {stats['identical']:,} "
                         f"| {fmt_delta(stats['left'])} | {fmt_delta(stats['right'])} "
                         f"| {fmt_delta(stats['both_same'])} | {stats['conflict']} |")
        expect = meta["expect"]
        lines += ["",
                  f"预期计划条目 **{expect['items']}**（自动 {expect['automatic']} ＋ 冲突 "
                  f"{expect['conflicts']}）；预期重演变更 **{expect['changed']}** 行。", ""]
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="生成 Pagefold 评审样本镜像与文件清单")
    parser.add_argument("--check", action="store_true", help="校验已生成镜像可逐字节复现")
    args = parser.parse_args()
    if args.check:
        ok = True
        for spec in GROUPS:
            images = build_images(spec)
            for role, data in images.items():
                path = ROOT / "images" / spec["id"] / f"{role}.sqlite"
                if not path.exists():
                    print(f"缺失 {path.relative_to(ROOT.parent)}")
                    ok = False
                    continue
                actual = hashlib.sha256(path.read_bytes()).hexdigest()
                want = hashlib.sha256(data).hexdigest()
                ok &= actual == want
                print(f"{spec['id']}/{role}.sqlite {'一致' if actual == want else '不一致'}"
                      f"  {actual[:16]}…")
        sys.exit(0 if ok else 1)
    generated_at = datetime.now(timezone.utc)
    metas = [build_group(spec) for spec in GROUPS]
    for meta in metas:
        folder = ROOT / "images" / meta["id"]
        folder.mkdir(parents=True, exist_ok=True)
        for role, data in meta["images"].items():
            (folder / f"{role}.sqlite").write_bytes(data)
    manifest = {"generator": "review/make_samples.py",
                "generated_at": f"{generated_at:%Y-%m-%dT%H:%M:%SZ}",
                "groups": {meta["id"]: {key: meta[key] for key in
                                        ("title", "purpose", "provenance", "files", "diff", "expect")}
                           for meta in metas}}
    (ROOT / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (ROOT / "MANIFEST.md").write_text(render_manifest(metas, generated_at), encoding="utf-8")
    for meta in metas:
        totals = meta["diff"]["totals"]
        rows_in = sum(f["rows"]["entry"] + f["rows"]["tag"] for f in meta["files"].values())
        print(f"{meta['id']}: 输入 {rows_in:,} 行 → 计划条目 {meta['expect']['items']}"
              f"（三份相同 {totals['identical']:,} 行已跳过）")
    print(f"已写出 {len(metas) * 3} 份镜像、MANIFEST.md 与 manifest.json")


if __name__ == "__main__":
    main()
