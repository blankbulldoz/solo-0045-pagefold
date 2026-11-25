"""生成三张（外加一张对照）值班演练卡的镜像和清单。

用法：python3 -m drills.build
输出：drills/cards/<slug>/{quartz,cerulean,umber}.sqlite、card.json、SHA256SUMS、RUNBOOK.md

构建时会独立重算三方差异与通过卡的合流结果，作为 verify.py 的黄金对照。
"""

import base64
import hashlib
import json
import math
import sqlite3
from pathlib import Path

from pagefold.catalog import describe
from pagefold.constraints import check_database
from pagefold.rows import read_image
from pagefold.sqlite_image import mounted

from .spec import BASE_ROWS, CARDS, ROLES, SCHEMA

HERE = Path(__file__).resolve().parent
CARDS_DIR = HERE / "cards"
CARD_FORMAT = "pagefold-drill-card-v1"


# ---------- 独立编码 / 读取（不依赖 pagefold.merge） ----------

def atom(value):
    if value is None:
        return ["n"]
    if isinstance(value, int):
        return ["i", str(value)]
    if isinstance(value, float) and math.isfinite(value):
        return ["r", value.hex()]
    if isinstance(value, str):
        return ["t", value]
    if isinstance(value, bytes):
        return ["b", base64.b64encode(value).decode("ascii")]
    raise TypeError(f"unsupported value: {type(value)!r}")


def canonical(value):
    return json.dumps(value, ensure_ascii=True, separators=(",", ":"), sort_keys=True)


def digest(data):
    return hashlib.sha256(data).hexdigest()


def key_atom(value):
    if isinstance(value, float) and value.is_integer() and -(2**63) <= value < 2**63:
        return atom(int(value))
    return atom(value)


def connect(data):
    db = sqlite3.connect(":memory:", isolation_level=None)
    db.deserialize(data)
    return db


def tables_of(db):
    names = [row[0] for row in db.execute(
        "SELECT name FROM main.sqlite_schema WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
    result = {}
    for name in names:
        fields = db.execute(f'PRAGMA main.table_xinfo("{name}")').fetchall()
        columns = [field[1] for field in fields]
        key = [col for _, col in sorted((field[5], field[1]) for field in fields if field[5])]
        result[name] = {"columns": columns, "key": key}
    return result


def snapshot(data):
    """返回 {表: {主键原子 canonical: 整行原子列表}}。"""
    db = connect(data)
    try:
        catalog = tables_of(db)
        result = {}
        for name, table in catalog.items():
            positions = [table["columns"].index(key) for key in table["key"]]
            cols = ",".join('"' + col.replace('"', '""') + '"' for col in table["columns"])
            rows = {}
            for raw in db.execute(f'SELECT {cols} FROM main."{name}"'):
                token = canonical([key_atom(raw[pos]) for pos in positions])
                if token in rows:
                    raise ValueError(f"{name} 有重复主键")
                rows[token] = [atom(value) for value in raw]
            result[name] = rows
        return result
    finally:
        db.close()


def merge_items(base, left, right):
    """按文档规则独立计算自动项；返回 [(table, key_token, base, left, right, automatic)]。"""
    items = []
    for table in sorted(base):
        keys = sorted(set(base[table]) | set(left[table]) | set(right[table]))
        for token in keys:
            ancestor, lrow, rrow = base[table].get(token), left[table].get(token), right[table].get(token)
            if lrow == rrow == ancestor:
                continue
            if lrow == rrow or rrow == ancestor:
                automatic = "left"
            elif lrow == ancestor:
                automatic = "right"
            else:
                automatic = None
            items.append((table, token, ancestor, lrow, rrow, automatic))
    return items


def apply_items(base, items, decisions):
    result = {table: dict(rows) for table, rows in base.items()}
    changed = 0
    for table, token, ancestor, lrow, rrow, automatic in items:
        role = automatic or decisions.get((table, token))
        if role is None:
            raise ValueError("存在未裁决项")
        row = {"base": ancestor, "left": lrow, "right": rrow}[role]
        if row != ancestor:
            changed += 1
        if row is None:
            result[table].pop(token, None)
        else:
            result[table][token] = row
    return result, changed


def logical_digest(snapshot_):
    ordered = {table: {token: rows[token] for token in sorted(rows)} for table, rows in sorted(snapshot_.items())}
    return digest(canonical(ordered).encode("ascii"))


# ---------- 造镜像 ----------

def build_bytes(marker, operations):
    db = sqlite3.connect(":memory:", isolation_level=None)
    try:
        db.execute("PRAGMA foreign_keys=ON")
        db.executescript(SCHEMA)
        db.executemany("INSERT INTO site VALUES(?,?)", [marker])
        for statement, rows in BASE_ROWS:
            db.executemany(statement, rows)
        for statement, rows in operations:
            db.executemany(statement, rows)
        db.commit()
        return db.serialize()
    finally:
        db.close()


def inspect(data, label):
    """用应用自身入口验证镜像可导入，并记录独立观察。"""
    with mounted(data) as image:
        catalog = describe(image)
        check_database(image)
        rows = read_image(image, catalog)
    db = connect(data)
    try:
        integrity = db.execute("PRAGMA main.integrity_check(1)").fetchone()[0]
        foreign = [list(row) for row in db.execute("PRAGMA main.foreign_key_check")]
    finally:
        db.close()
    counts = {table: len(values) for table, values in sorted(rows.items())}
    if integrity != "ok" or foreign:
        raise ValueError(f"{label} 输入不合法：integrity={integrity} fk={foreign}")
    return {
        "bytes": len(data),
        "sha256": digest(data),
        "schema_signature": catalog["signature"],
        "tables": counts,
        "integrity_check": integrity,
        "foreign_key_check": foreign,
    }


def display_rows(rows):
    """用于 RUNBOOK 的简明行摘录。"""
    if rows is None:
        return "（缺席）"
    parts = []
    for part in rows:
        if part[0] == "n":
            parts.append("NULL")
        elif part[0] == "b":
            parts.append("x'" + base64.b64decode(part[1]).hex() + "'")
        else:
            parts.append(str(part[1]))
    return "(" + ", ".join(parts) + ")"


def expected_ending(card, base, left, right):
    items = merge_items(base, left, right)
    unresolved = sum(1 for item in items if item[5] is None)
    if unresolved:
        raise ValueError(f"{card['slug']} 含未裁决项，演练卡应设计为全自动")
    decisions = {}
    merged, changed = apply_items(base, items, decisions)
    return items, unresolved, merged, changed


def write_card(card):
    directory = CARDS_DIR / card["slug"]
    directory.mkdir(parents=True, exist_ok=True)
    payloads = {
        "base": build_bytes(card["marker"], []),
        "left": build_bytes(card["marker"], card["branches"]["left"]),
        "right": build_bytes(card["marker"], card["branches"]["right"]),
    }
    files = {}
    snapshots = {}
    for role, data in payloads.items():
        name, _label = ROLES[role]
        path = directory / name
        path.write_bytes(data)
        info = inspect(data, f"{card['code']} {role}")
        info["name"] = name
        info["label"] = _label
        files[role] = info
        snapshots[role] = snapshot(data)
    signatures = {info["schema_signature"] for info in files.values()}
    if len(signatures) != 1:
        raise ValueError(f"{card['slug']} 三份模式不一致")

    items, unresolved, merged, changed = expected_ending(card, snapshots["base"], snapshots["left"], snapshots["right"])
    item_records = [{
        "table": table,
        "key": json.loads(token),
        "automatic": automatic,
    } for table, token, _, _, _, automatic in items]

    expect = {
        "rehearsal": card["expectation"],
        "items": len(items),
        "unresolved": unresolved,
        "changed": changed,
        "problem": card["expect_problem"],
        "rows": {table: len(rows) for table, rows in sorted(merged.items())},
        "rows_digest": logical_digest(merged),
    }
    for key, value in card.items():
        if key.startswith("expect_") and key not in {"expectation"}:
            public = key[len("expect_"):]
            if public not in {"items", "unresolved", "changed", "problem"}:
                expect[public] = value

    manifest = {
        "format": CARD_FORMAT,
        "card": card["code"],
        "code": card["code"],
        "slug": card["slug"],
        "title": card["title"],
        "expectation": card["expectation"],
        "lesson": card["lesson"],
        "marker": {"code": card["marker"][0], "name": card["marker"][1]},
        "files": files,
        "items": item_records,
        "expect": expect,
    }
    (directory / "card.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (directory / "SHA256SUMS").write_text(
        "".join(f"{files[role]['sha256']}  {files[role]['name']}\n" for role in ROLES), encoding="utf-8")
    (directory / "RUNBOOK.md").write_text(render_runbook(manifest), encoding="utf-8")
    return manifest


def render_runbook(manifest):
    files = manifest["files"]
    rows = manifest["expect"]["rows"]
    counts = "\n".join(f"| `{table}` | {count} |" for table, count in sorted(rows.items()))
    problem = manifest["expect"]["problem"]
    if problem:
        outcome = (f"**受组**（status=`blocked`），诊断码 `{problem['code']}`，"
                   f"详细信息含 {'、'.join(problem['detail_contains'])}。")
        closing = [
            "这是本卡的正确答案：两份分支各自合法，合流产物在数据库约束上失败。",
            "阻断发生在重演阶段；不要修改输入镜像来绕过，也不要封存受组候选。",
            "`GET .../download` 与 `.../certificate` 此时都应返回 404 `unpublished`。",
        ]
    else:
        outcome = (f"**通过**（status=`passed`），可封存并下载候选，行摘要 "
                   f"`{manifest['expect']['rows_digest'][:16]}…`。")
        closing = [
            "这是本卡本来就应通过的结果；若它被阻断，先检查环境（Python/SQLite 版本、状态库是否复用错误）。",
            "封存后下载的 SQLite 应能独立打开，`PRAGMA integrity_check` 为 `ok`，`foreign_key_check` 为空。",
        ]
    checks = "\n".join(
        f"| `{role}` | `{files[role]['name']}` | {files[role]['bytes']} B | `{files[role]['sha256'][:16]}…` | "
        f"{files[role]['tables'].get('site', 0)} / {files[role]['tables'].get('rack', 0)} / "
        f"{files[role]['tables'].get('lease', 0)} |"
        for role in ROLES)
    lessons = "\n".join(f"- {line}" for line in manifest["lesson"])
    items = "\n".join(
        "| `{}` | `{}` | {} |".format(item["table"], canonical(item["key"]),
                                      "自动·" + {"base": "祖本", "left": "左支", "right": "右支"}[item["automatic"]])
        for item in manifest["items"]) or "| — | — | 无差异 |"
    return f"""# 演练卡 {manifest['card']} · {manifest['title']}

> 自动生成的说明。卡内镜像与 `card.json` 是权威内容；改动请回到 `drills/spec.py` 后重跑
> `python3 -m drills.build`。

## 目的

{lessons}

## 文件

| 角色 | 文件 | 大小 | SHA-256 | 行数 site/rack/lease |
|---|---|---|---|---|
{checks}

三份镜像的模式摘要均为 `{files['base']['schema_signature']}`，各自通过 `integrity_check` 与外键检查。
卡内 `site('{manifest['marker']['code']}', '{manifest['marker']['name']}')` 是这张卡的识别行，
用于在共享状态库中区分卡别；它三边相同，不参与合流。

## 操作者步骤（可复跑）

前置：Python 3.11+、SQLite 3.37+；在仓库根目录执行 `python3 -m compileall -q pagefold drills`。
用一份**全新的**状态库启动服务（轮转时不要复用别人的状态）：

```sh
python3 -m pagefold --state /tmp/pagefold-{manifest['slug']}.sqlite
```

另一终端先核对文件，再走真实 HTTP 入口（把 `DRILLS` 指向本卡目录）：

```sh
cd drills/cards/{manifest['slug']} && sha256sum -c SHA256SUMS && cd -
DRILLS=drills/cards/{manifest['slug']}
python3 -m drills.run_card {manifest['slug']} --origin http://127.0.0.1:8745
```

`run_card` 会依次：上传三份镜像（按字节摘要复用）、创建计划、读取计划、重演第 0 版；
通过则封存并下载候选再核对摘要，受组则核对下载接口 404。也可按 `RUNBOOK` 末尾的手工 curl
步骤逐条替代。

## 预期计划

- 差异项 **{manifest['expect']['items']}** 条，全部为自动项，待裁决 **{manifest['expect']['unresolved']}** 条。
- 重演目标操作 **{manifest['expect']['changed']}** 行。
- 预期结局：{outcome}

| 表 | 主键 | 处置 |
|---|---|---|
{items}

## 预期合流产物行数

| 表 | 行数 |
|---|---|
{counts}

## 核验与结论

{chr(10).join('- ' + line for line in closing)}

也可由导师用独立服务跑完整核验：

```sh
python3 -m drills.verify {manifest['slug']}
```

## 手工 curl（不依赖辅助脚本）

```sh
ORIGIN=http://127.0.0.1:8745
for ROLE in base left right; do
  case $ROLE in base) FILE=quartz.sqlite;; left) FILE=cerulean.sqlite;; right) FILE=umber.sqlite;; esac
  curl -sS -X POST "$ORIGIN/api/images" -H 'Content-Type: application/octet-stream' \\
       -H "X-Image-Name: {manifest['code']}-$ROLE" --data-binary "@$DRILLS/$FILE"
done
```

记下三个返回的 `id`，然后：

```sh
curl -sS -X POST "$ORIGIN/api/plans" -H 'Content-Type: application/json' \\
     -d '{{"base":"<base-id>","left":"<left-id>","right":"<right-id>"}}'
curl -sS -X POST "$ORIGIN/api/plans/<plan-id>/rehearse" -H 'Content-Type: application/json' -d '{{"revision":0}}'
```

通过卡的后续封存/下载：

```sh
curl -sS -X POST "$ORIGIN/api/plans/<plan-id>/seal" -H 'Content-Type: application/json' -d '{{"revision":0}}'
curl -sS -o candidate.sqlite "$ORIGIN/api/plans/<plan-id>/download"
sha256sum candidate.sqlite   # 应等于上一步返回的 candidate_digest
```
"""


def main():
    CARDS_DIR.mkdir(parents=True, exist_ok=True)
    summary = []
    for card in CARDS:
        manifest = write_card(card)
        summary.append(manifest)
        print(f"{manifest['card']} {manifest['slug']} → {manifest['expectation']} "
              f"（{manifest['expect']['items']} 项 / 改 {manifest['expect']['changed']} 行）")
    index = {
        "format": "pagefold-drill-index-v1",
        "cards": [{
            "card": m["card"],
            "slug": m["slug"],
            "title": m["title"],
            "marker": m["marker"],
            "expectation": m["expectation"],
            "files": {role: m["files"][role]["name"] for role in ROLES},
            "rows_digest": m["expect"]["rows_digest"],
        } for m in summary],
    }
    (CARDS_DIR / "index.json").write_text(
        json.dumps(index, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"写入 {CARDS_DIR}")


if __name__ == "__main__":
    main()
