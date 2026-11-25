"""演练卡共用的 HTTP 客户端与走卡流程。

所有写操作都走 Pagefold 的公开 HTTP 入口；不直接读写服务状态库。
"""

import hashlib
import json
import sqlite3
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen


class DrillFailure(AssertionError):
    pass


def check(condition, message):
    if not condition:
        raise DrillFailure(message)


class Client:
    def __init__(self, origin):
        self.origin = origin.rstrip("/")

    def call(self, route, data=None, status=200, headers=None):
        request_headers = dict(headers or {})
        body = data
        if isinstance(data, dict):
            body = json.dumps(data).encode()
            request_headers["Content-Type"] = "application/json"
        request = Request(self.origin + route, data=body, headers=request_headers)
        try:
            with urlopen(request, timeout=20) as result:
                raw, actual, kind = result.read(), result.status, result.headers.get_content_type()
        except HTTPError as error:
            with error:
                raw, actual, kind = error.read(), error.code, error.headers.get_content_type()
        payload = None
        if kind == "application/json" and raw:
            payload = json.loads(raw)
        if actual != status:
            raise DrillFailure(f"{route}: 期望 HTTP {status}，实际 {actual}：{raw[:400]!r}")
        return payload if kind == "application/json" else raw

    def upload(self, path, label):
        return self.call("/api/images", Path(path).read_bytes(), 201, {
            "Content-Type": "application/octet-stream", "X-Image-Name": quote(label, safe="")})


def load_manifest(target):
    path = Path(target)
    if path.is_dir():
        path = path / "card.json"
    elif not path.exists():
        raise SystemExit(f"找不到演练卡：{target}")
    return json.loads(path.read_text(encoding="utf-8")), path.parent


def read_snapshot(data):
    """独立读取候选 SQLite：{表: {主键原子规范串: 行}}，用于与黄金摘要比对。"""
    db = sqlite3.connect(":memory:", isolation_level=None)
    try:
        db.deserialize(data)
        names = [row[0] for row in db.execute(
            "SELECT name FROM main.sqlite_schema WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
        result = {}
        for name in names:
            fields = db.execute(f'PRAGMA main.table_xinfo("{name}")').fetchall()
            columns = [field[1] for field in fields]
            key = [col for _, col in sorted((field[5], field[1]) for field in fields if field[5])]
            positions = [columns.index(col) for col in key]
            cols = ",".join('"' + col.replace('"', '""') + '"' for col in columns)
            rows = {}
            for raw in db.execute(f'SELECT {cols} FROM main."{name}"'):
                rows[canonical([atom(raw[pos]) for pos in positions])] = [atom(value) for value in raw]
            result[name] = rows
        return result
    finally:
        db.close()


def atom(value):
    import base64
    import math
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
    raise DrillFailure(f"候选含无法编码的值：{type(value)!r}")


def canonical(value):
    return json.dumps(value, ensure_ascii=True, separators=(",", ":"), sort_keys=True)


def logical_digest(snapshot):
    ordered = {table: {token: rows[token] for token in sorted(rows)} for table, rows in sorted(snapshot.items())}
    return hashlib.sha256(canonical(ordered).encode("ascii")).hexdigest()


def walk(client, manifest, directory, echo=print):
    """按卡执行一次完整流程，返回观察结果；任何与清单不符都抛 DrillFailure。"""
    expect = manifest["expect"]
    files = manifest["files"]
    observations = {"uploads": {}, "checks": []}

    def note(message):
        observations["checks"].append(message)
        echo("  ✓ " + message)

    for role, name in (("base", "base"), ("left", "left"), ("right", "right")):
        info = files[role]
        record = client.upload(directory / info["name"], f"{manifest['card']}-{role}")
        check(len(record["id"]) == 64, f"{role} 镜像摘要不是 64 位十六进制")
        check(record["catalog"]["signature"] == info["schema_signature"],
              f"{role} 模式摘要与清单不符")
        counts = {table["name"]: table["rows"] for table in record["catalog"]["tables"]}
        check(counts == info["tables"], f"{role} 表行数与清单不符：{counts} != {info['tables']}")
        observations["uploads"][role] = record["id"]
        note(f"上传 {info['name']} → {record['id'][:12]}…（{counts}）")

    reused = client.upload(directory / files["base"]["name"], "重复上传检查")
    check(reused["id"] == observations["uploads"]["base"], "重复上传未复用同一镜像摘要")
    note("重复上传按字节摘要复用")
    table_catalog = {table["name"]: table for table in reused["catalog"]["tables"]}

    plan = client.call("/api/plans", {
        "base": observations["uploads"]["base"],
        "left": observations["uploads"]["left"],
        "right": observations["uploads"]["right"],
    }, 201)
    observations["plan"] = plan["id"]
    check(plan["revision"] == 0, f"新计划版本应为 0，实际 {plan['revision']}")
    check(len(plan["items"]) == expect["items"],
          f"差异项应为 {expect['items']}，实际 {len(plan['items'])}")
    check(plan["unresolved"] == expect["unresolved"],
          f"待裁决项应为 {expect['unresolved']}，实际 {plan['unresolved']}")
    observed_items = sorted((item["table"], canonical(item["key"]), item["automatic"]) for item in plan["items"])
    wanted_items = sorted((item["table"], canonical(item["key"]), item["automatic"]) for item in manifest["items"])
    check(observed_items == wanted_items, f"差异项与清单不符：\n{observed_items}\n!=\n{wanted_items}")
    check(all(item["choice"] is not None for item in plan["items"]), "存在未自动裁决的项")
    note(f"计划 {plan['id'][:12]}…：{len(plan['items'])} 项全部自动，待裁决 {plan['unresolved']}")

    for item in plan["items"]:
        table = table_catalog[item["table"]]
        page = client.call(
            f"/api/images/{observations['uploads']['base']}/rows?table={quote(item['table'])}")
        observed = next((row for row in page["rows"] if row["key"] == item["key"]), None)
        if observed is None:  # 新增行不在祖本里，改查来源镜像
            role = "left" if item["automatic"] == "left" else "right"
            page = client.call(
                f"/api/images/{observations['uploads'][role]}/rows?table={quote(item['table'])}")
            observed = next((row for row in page["rows"] if row["key"] == item["key"]), None)
            check(observed is not None, f"差异项 {item['table']} {item['key']} 在来源镜像中缺失")
        check(len(observed["values"]) == len(table["columns"]), "行窗口列数与目录不符")
    note(f"逐项行窗口可回查（{len(plan['items'])} 项，含新增行转向来源分支）")

    route = f"/api/plans/{plan['id']}"
    attempt = client.call(route + "/rehearse", {"revision": 0})
    observations["attempt"] = attempt
    check(attempt["changed"] == expect["changed"],
          f"目标操作应为 {expect['changed']} 行，实际 {attempt['changed']}")
    check(attempt["status"] == expect["rehearsal"],
          f"重演结局应为 {expect['rehearsal']}，实际 {attempt['status']}：{attempt.get('problem')}")
    revision = 0

    if expect["rehearsal"] == "blocked":
        problem = expect["problem"]
        got = attempt.get("problem") or {}
        check(got.get("code") == problem["code"],
              f"诊断码应为 {problem['code']}，实际 {got.get('code')}")
        for token in problem["detail_contains"]:
            check(token in got.get("detail", ""), f"诊断详情缺少 {token!r}：{got.get('detail')!r}")
        for route_suffix in ("/download", "/certificate"):
            failure = client.call(route + route_suffix, status=404)
            check(failure["code"] == "unpublished", f"{route_suffix} 受组时仍可读取")
        note(f"重演受阻：{got.get('code')} · {got.get('detail')}")
        note("受组候选无下载、无凭证（404 unpublished）")
    else:
        check(attempt.get("candidate_digest"), "通过候选缺少摘要")
        certificate = client.call(route + "/seal", {"revision": revision})
        check(certificate["revision"] == revision, f"凭证版本应为 {revision}")
        check(certificate["candidate_digest"] == attempt["candidate_digest"], "凭证与重演摘要不一致")
        check(certificate["sources"] == observations["uploads"], "凭证来源角色不匹配")
        candidate = client.call(route + "/download")
        check(hashlib.sha256(candidate).hexdigest() == certificate["candidate_digest"],
              "下载字节摘要与凭证不符")
        snapshot = read_snapshot(candidate)
        check({table: len(rows) for table, rows in snapshot.items()} == expect["rows"],
              f"候选行数与清单不符：{ {t: len(r) for t, r in snapshot.items()} } != {expect['rows']}")
        check(logical_digest(snapshot) == expect["rows_digest"], "候选逻辑行摘要与清单不符")
        db = sqlite3.connect(":memory:")
        try:
            db.deserialize(candidate)
            check(db.execute("PRAGMA main.integrity_check(1)").fetchone()[0] == "ok", "候选完整性检查失败")
            check(db.execute("PRAGMA main.foreign_key_check").fetchall() == [], "候选存在外键违规")
        finally:
            db.close()
        note(f"重演通过：{attempt['changed']} 行，候选 {attempt['size']} B")
        note(f"封存凭证绑定三源与候选摘要 {certificate['candidate_digest'][:12]}…")
        note(f"下载候选摘要一致，行摘要 {logical_digest(snapshot)[:12]}…，外键检查为空")
        observations["sealed"] = True

    return observations
