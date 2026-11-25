#!/usr/bin/env python3
"""Run the Pagefold example over real HTTP and assemble offline evidence.

This builder is intentionally kept outside the handoff package.  It starts the
repository's HTTP server on an ephemeral port, performs the merge workflow,
and writes only portable artifacts into the requested handoff directory.
"""

from __future__ import annotations

import base64
import hashlib
import json
import sqlite3
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from threading import Thread
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request, urlopen

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from pagefold.codec import atom  # noqa: E402
from pagefold.demo import image  # noqa: E402
from pagefold.server import make_server  # noqa: E402
from pagefold.store import Store  # noqa: E402


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def now_utc() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def quote_ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def plain_value(value: Any) -> Any:
    if isinstance(value, bytes):
        return {
            "__type__": "blob",
            "length": len(value),
            "hex": value.hex(),
            "base64": base64.b64encode(value).decode("ascii"),
            "sha256": sha256_bytes(value),
            "python_repr": repr(value),
        }
    return value


def typed_row(values: tuple[Any, ...]) -> list[list[Any]]:
    return [atom(v) for v in values]


def export_database(path: Path) -> dict[str, Any]:
    db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        tables = [
            row[0]
            for row in db.execute(
                "SELECT name FROM main.sqlite_schema WHERE type='table' "
                "AND substr(name,1,7) <> 'sqlite_' ORDER BY name"
            )
        ]
        result: dict[str, Any] = {}
        for table in tables:
            qname = quote_ident(table)
            info = list(db.execute(f"PRAGMA main.table_info({qname})"))
            columns = [col[1] for col in info]
            key_columns = [col[1] for col in sorted(info, key=lambda c: c[5]) if col[5]]
            order = ""
            if key_columns:
                order = " ORDER BY " + ",".join(quote_ident(c) for c in key_columns)
            rows = db.execute(f"SELECT * FROM main.{qname}{order}").fetchall()
            result[table] = {
                "columns": columns,
                "key": key_columns,
                "row_count": len(rows),
                "rows_plain": [dict(zip(columns, (plain_value(v) for v in row))) for row in rows],
                "rows_pagefold_atoms": [
                    {"key": [atom(row[columns.index(k)]) for k in key_columns],
                     "values": typed_row(row)}
                    for row in rows
                ],
            }
        return result
    finally:
        db.close()


def write_csv_inventory(db_dump: dict[str, Any], directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    for table, spec in db_dump.items():
        lines = [",".join(spec["columns"])]
        for row in spec["rows_plain"]:
            cells = []
            for col in spec["columns"]:
                value = row[col]
                if isinstance(value, dict) and value.get("__type__") == "blob":
                    text = f"BLOB({value['length']}):hex:{value['hex']}"
                elif value is None:
                    text = "NULL"
                else:
                    text = str(value)
                if any(ch in text for ch in [",", '"', "\n"]):
                    text = '"' + text.replace('"', '""') + '"'
                cells.append(text)
            lines.append(",".join(cells))
        (directory / f"{table}.csv").write_text("\n".join(lines) + "\n", encoding="utf-8")


def markdown_inventory(title: str, db_dump: dict[str, Any]) -> str:
    out = [f"# {title}", ""]
    for table, spec in db_dump.items():
        out.append(f"## `{table}` ({spec['row_count']} 行)")
        out.append("")
        out.append("主键列：`" + "`, `".join(spec["key"]) + "`" if spec["key"] else "主键列：无")
        out.append("")
        out.append("| " + " | ".join(spec["columns"]) + " |")
        out.append("|" + "|".join(["---"] * len(spec["columns"])) + "|")
        for row in spec["rows_plain"]:
            cells = []
            for col in spec["columns"]:
                value = row[col]
                if isinstance(value, dict) and value.get("__type__") == "blob":
                    shown = f"BLOB[{value['length']}] hex=`{value['hex']}` b64=`{value['base64']}`"
                elif value is None:
                    shown = "`NULL`"
                else:
                    shown = str(value).replace("|", "\\|")
                cells.append(shown)
            out.append("| " + " | ".join(cells) + " |")
        out.append("")
    return "\n".join(out)


class HttpRecorder:
    def __init__(self, port: int) -> None:
        self.origin = f"http://127.0.0.1:{port}"
        self.transcript: list[dict[str, Any]] = []

    def call(
        self,
        method: str,
        route: str,
        payload: Any = None,
        *,
        data: bytes | None = None,
        headers: dict[str, str] | None = None,
        expected: int | set[int] = 200,
        binary: bool = False,
    ) -> Any:
        request_headers = dict(headers or {})
        body: bytes | None
        request_view: Any
        if data is not None:
            body = data
            request_view = {"binary_sha256": sha256_bytes(data), "binary_size": len(data)}
        elif payload is not None:
            body = json.dumps(payload).encode("utf-8")
            request_headers.setdefault("Content-Type", "application/json")
            request_view = payload
        else:
            body = None
            request_view = None
        request = Request(self.origin + route, data=body, headers=request_headers, method=method)
        try:
            with urlopen(request, timeout=15) as response:
                status = response.status
                raw = response.read()
                content_type = response.headers.get_content_type()
        except HTTPError as exc:
            status = exc.code
            raw = exc.read()
            content_type = exc.headers.get_content_type()
        expected_set = {expected} if isinstance(expected, int) else set(expected)
        if status not in expected_set:
            raise RuntimeError(f"{method} {route} returned {status}, expected {expected}: {raw[:500]!r}")
        entry: dict[str, Any] = {
            "method": method,
            "route": route,
            "request": request_view,
            "status": status,
            "content_type": content_type,
        }
        if binary:
            entry["response_sha256"] = sha256_bytes(raw)
            entry["response_size"] = len(raw)
            result: Any = raw
        else:
            result = json.loads(raw.decode("utf-8"))
            entry["response"] = result
        self.transcript.append(entry)
        return result


def git_text(*args: str) -> str:
    return subprocess.check_output(["git", *args], cwd=REPO, text=True).strip()


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("usage: build_handoff.py HANDOFF_DIR")
    out = Path(sys.argv[1]).resolve()
    if out.exists() and any(out.iterdir()):
        raise SystemExit(f"refusing to use non-empty output directory: {out}")
    for sub in ["sources", "result", "certificate", "audit/api", "evidence/csv", "tools", "negative-tests"]:
        (out / sub).mkdir(parents=True, exist_ok=True)

    flavors = {
        "base": ("quartz", "共同祖本"),
        "left": ("cerulean", "左支：蓝釉分支，含新增行"),
        "right": ("umber", "右支：赭石分支，含 BLOB/重量与更名"),
    }
    source_bytes: dict[str, bytes] = {}
    source_meta: dict[str, Any] = {}
    for role, (flavor, role_cn) in flavors.items():
        data = image(flavor)
        source_bytes[role] = data
        filename = f"{role}-{flavor}.sqlite"
        (out / "sources" / filename).write_bytes(data)
        source_meta[role] = {
            "flavor": flavor,
            "role_cn": role_cn,
            "file": f"sources/{filename}",
            "sha256": sha256_bytes(data),
            "size": len(data),
        }

    impostors = {
        "stress-left": image("stress-left"),
        "stress-right": image("stress-right"),
    }
    for flavor, data in impostors.items():
        (out / "negative-tests" / f"{flavor}-same-schema.sqlite").write_bytes(data)

    with tempfile.TemporaryDirectory(prefix="pagefold-handoff-state-") as temp:
        state_path = Path(temp) / "pagefold-state.sqlite"
        store = Store(state_path)
        server = make_server(store, 0)
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        http = HttpRecorder(server.server_port)
        try:
            image_ids: dict[str, str] = {}
            for role, (flavor, _role_cn) in flavors.items():
                uploaded = http.call(
                    "POST",
                    "/api/images",
                    data=source_bytes[role],
                    headers={
                        "Content-Type": "application/octet-stream",
                        "X-Image-Name": f"{flavor}-handoff",
                    },
                    expected=201,
                )
                image_ids[role] = uploaded["id"]
                if uploaded["id"] != source_meta[role]["sha256"]:
                    raise RuntimeError("image id is not byte SHA-256")
                source_meta[role]["image_id"] = uploaded["id"]
                catalog = http.call("GET", f"/api/images/{image_ids[role]}")
                write_json(out / "audit/api" / f"{role}-catalog.json", catalog)
                for table in [t["name"] for t in catalog["catalog"]["tables"]]:
                    rows = http.call("GET", f"/api/images/{image_ids[role]}/rows?table={table}&offset=0")
                    write_json(out / "audit/api" / f"{role}-rows-{table}.json", rows)

            sources = {role: image_ids[role] for role in ("base", "left", "right")}
            initial_plan = http.call("POST", "/api/plans", sources, expected=201)
            write_json(out / "audit/initial-plan-response.json", initial_plan)
            conflict = next(item for item in initial_plan["items"] if item["automatic"] is None)
            conflict_id = conflict["id"]

            decision = http.call(
                "POST", f"/api/plans/{initial_plan['id']}/decisions",
                {"revision": 0, "item": conflict_id, "choice": "right"},
            )
            stale = http.call(
                "POST", f"/api/plans/{initial_plan['id']}/decisions",
                {"revision": 0, "item": conflict_id, "choice": "right"},
                expected=409,
            )
            idempotent = http.call(
                "POST", f"/api/plans/{initial_plan['id']}/decisions",
                {"revision": 1, "item": conflict_id, "choice": "right"},
            )
            rehearsal = http.call(
                "POST", f"/api/plans/{initial_plan['id']}/rehearse", {"revision": 1}
            )
            rehearsal_repeat = http.call(
                "POST", f"/api/plans/{initial_plan['id']}/rehearse", {"revision": 1}
            )
            if rehearsal != rehearsal_repeat:
                raise RuntimeError("same-revision rehearsal was not stable")

            certificate = http.call(
                "POST", f"/api/plans/{initial_plan['id']}/seal", {"revision": 1}
            )
            certificate_get = http.call("GET", f"/api/plans/{initial_plan['id']}/certificate")
            if certificate != certificate_get:
                raise RuntimeError("certificate response differs from GET certificate")

            candidate = http.call(
                "GET", f"/api/plans/{initial_plan['id']}/download", binary=True
            )
            if sha256_bytes(candidate) != certificate["candidate_digest"]:
                raise RuntimeError("downloaded candidate does not match certificate")
            if len(candidate) != certificate["candidate_size"]:
                raise RuntimeError("candidate size does not match certificate")

            archive_result = http.call(
                "POST", f"/api/plans/{initial_plan['id']}/archive", {"revision": 1}
            )
            final_plan = http.call("GET", f"/api/plans/{initial_plan['id']}")
            attempts = http.call("GET", f"/api/plans/{initial_plan['id']}/attempts")
            plan_html = http.call("GET", f"/plans/{initial_plan['id']}", binary=True)
            (out / "audit" / "plan-page-after-archive.html").write_bytes(plan_html)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

        backup = sqlite3.connect(out / "audit" / "pagefold-state-backup.sqlite")
        try:
            source_state = sqlite3.connect(state_path)
            try:
                source_state.backup(backup)
            finally:
                source_state.close()
        finally:
            backup.close()

    result_name = "merged-r059-right-choice.sqlite"
    result_path = out / "result" / result_name
    result_path.write_bytes(candidate)

    (out / "certificate" / "pagefold-certificate.json").write_text(
        json.dumps(certificate, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    write_json(out / "audit/decision-response.json", decision)
    write_json(out / "audit/stale-revision-response.json", stale)
    write_json(out / "audit/idempotent-decision-response.json", idempotent)
    write_json(out / "audit/attempt-revision-1.json", rehearsal)
    write_json(out / "audit/attempts.json", attempts)
    write_json(out / "audit/seal-response.json", certificate)
    write_json(out / "audit/archive-response.json", archive_result)
    write_json(out / "audit/final-plan.json", final_plan)
    write_json(out / "audit/http-transcript.json", {
        "generated_at_utc": now_utc(),
        "note": "请求/响应实录；上传与下载正文以 SHA-256 和大小代替，二进制全文已在 sources/ 与 result/。",
        "exchanges": http.transcript,
    })
    write_json(out / "sources" / "SOURCE-ROLES.json", {
        "format": "pagefold-source-roles-v1",
        "plan_id": initial_plan["id"],
        "roles": source_meta,
    })

    source_dumps = {role: export_database(out / meta["file"]) for role, meta in source_meta.items()}
    sealed_dump = export_database(result_path)
    write_json(out / "evidence" / "source-databases.json", source_dumps)
    write_json(out / "evidence" / "sealed-database.json", sealed_dump)
    write_csv_inventory(sealed_dump, out / "evidence" / "csv")
    (out / "evidence" / "sealed-database.md").write_text(
        markdown_inventory("封存结果全表证据", sealed_dump), encoding="utf-8"
    )
    source_md = ["# 三份来源全表证据", ""]
    for role in ("base", "left", "right"):
        source_md.append(markdown_inventory(f"{role} · {source_meta[role]['role_cn']}", source_dumps[role]))
    (out / "evidence" / "source-databases.md").write_text("\n".join(source_md), encoding="utf-8")

    north_key = ["north", 1]
    north_row = next(
        row for row in sealed_dump["node"]["rows_plain"]
        if row["bench"] == "north" and row["slot"] == 1
    )
    south_added = next(
        row for row in sealed_dump["node"]["rows_plain"]
        if row["bench"] == "south" and row["slot"] == 2
    )
    right_north = next(
        row for row in source_dumps["right"]["node"]["rows_plain"]
        if row["bench"] == "north" and row["slot"] == 1
    )
    left_added = next(
        row for row in source_dumps["left"]["node"]["rows_plain"]
        if row["bench"] == "south" and row["slot"] == 2
    )
    key_lookup = {
        "query": {
            "table": "node",
            "composite_key_columns": ["bench", "slot"],
            "composite_key": north_key,
            "sql": "SELECT bench, slot, note, payload, weight FROM node WHERE bench=? AND slot=?",
            "parameters": north_key,
        },
        "row": north_row,
        "blob_columns": {"payload": north_row["payload"]},
    }
    write_json(out / "evidence" / "composite-key-blob-lookup.json", key_lookup)
    payload = north_row["payload"]
    lookup_text = f"""封存结果复合键/BLOB 查询回执
================================

数据库：result/{result_name}
表：node
复合主键：(bench TEXT, slot INTEGER)
查询键：('north', 1)

SQL（无需启动 Pagefold 服务）：
SELECT bench, slot, note, hex(payload) AS payload_hex, weight
FROM node
WHERE bench='north' AND slot=1;

查询值：
  bench   = {north_row['bench']!r}
  slot    = {north_row['slot']!r}
  note    = {north_row['note']!r}
  weight  = {north_row['weight']!r}
  payload = {payload['python_repr']}
  payload length = {payload['length']}
  payload hex    = {payload['hex']}
  payload base64 = {payload['base64']}
  payload sha256 = {payload['sha256']}

结论：该 BLOB 是右支值 x'ff00'，不是祖本/左支共有的 x'0014'。
"""
    (out / "evidence" / "composite-key-blob-lookup.txt").write_text(lookup_text, encoding="utf-8")

    reconciliation = {
        "plan_id": initial_plan["id"],
        "revision": 1,
        "conflict_item_id": conflict_id,
        "decision": "right",
        "candidate_digest": rehearsal["candidate_digest"],
        "certificate_digest": certificate["candidate_digest"],
        "digests_match": rehearsal["candidate_digest"] == certificate["candidate_digest"] == sha256_bytes(candidate),
        "changed_item_count": rehearsal["changed"],
        "merged_node_count": sealed_dump["node"]["row_count"],
        "right_node_count": source_dumps["right"]["node"]["row_count"],
        "left_node_count": source_dumps["left"]["node"]["row_count"],
        "left_added_key_present_in_sealed_result": south_added == left_added,
        "sealed_conflict_row_equals_right_full_row": north_row == right_north,
        "sealed_conflict_note_is_base_value_not_left_overwrite": north_row["note"] == "祖本节点",
        "sealed_conflict_blob_is_right_ff00": payload["hex"] == "ff00",
        "not_whole_right_database": sealed_dump["node"]["row_count"] > source_dumps["right"]["node"]["row_count"],
        "keys_present": {
            "node": [[row["bench"], row["slot"]] for row in sealed_dump["node"]["rows_plain"]]
        },
        "left_added_row": left_added,
        "sealed_left_added_row": south_added,
    }
    write_json(out / "evidence" / "reconciliation.json", reconciliation)

    build_info = {
        "format": "pagefold-offline-handoff-build-v1",
        "generated_at_utc": now_utc(),
        "repository_commit": git_text("rev-parse", "HEAD"),
        "repository_branch": git_text("branch", "--show-current"),
        "entry": "Real HTTP via pagefold.server.make_server on 127.0.0.1 ephemeral port",
        "example_command": "PYTHONPATH=/workspace python3 /workspace/handoff-builder/build_handoff.py /workspace/handoff-pagefold-r059",
        "plan_id": initial_plan["id"],
        "conflict_item_id": conflict_id,
        "decision": {"item": conflict_id, "choice": "right", "revision_before": 0, "revision_after": 1},
        "source_roles": source_meta,
        "impostor_fixtures": {
            flavor: {
                "file": f"negative-tests/{flavor}-same-schema.sqlite",
                "sha256": sha256_bytes(data),
                "size": len(data),
            }
            for flavor, data in impostors.items()
        },
    }
    write_json(out / "audit" / "build-info.json", build_info)
    print(json.dumps({
        "handoff": str(out),
        "plan_id": initial_plan["id"],
        "conflict_item_id": conflict_id,
        "revision": 1,
        "candidate_digest": certificate["candidate_digest"],
        "sources": image_ids,
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
