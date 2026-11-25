"""裁决练习：对同一批三份镜像，走真实 Pagefold HTTP 入口跑三种结局。

  策略 A「尊重删除」：唯一人工冲突 L-102 选 left（左支行缺席 => 删除），可封存；
  策略 B「保留改写」：同一冲突选 right（右支改写后的 L-102），可封存；
  被拒组合「回退祖本」：同一冲突选 base，重演被 UNIQUE 约束拦下，无法封存/下载。

脚本在临时状态库和本机临时端口上启动真实服务，全部通过 /api 入口操作，
把计划、裁决、重演结果、凭证、候选镜像和可查询差异写入 evidence 目录。
镜像路径由参数传入，脚本不依赖运行目录，且以 -I 隔离模式运行。
"""
import argparse
import hashlib
import json
import sqlite3
import sys
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from pagefold.server import make_server  # noqa: E402
from pagefold.store import Store  # noqa: E402

IMAGE_FILES = {
    "base": "base-ledger.sqlite",
    "left": "left-branch.sqlite",
    "right": "right-branch.sqlite",
}


class Client:
    def __init__(self, port):
        self.origin = f"http://127.0.0.1:{port}"

    def call(self, route, data=None, status=200):
        headers = {}
        if isinstance(data, dict):
            data = json.dumps(data).encode()
            headers["Content-Type"] = "application/json"
        request = Request(self.origin + route, data=data, headers=headers)
        try:
            with urlopen(request, timeout=10) as result:
                body, code, kind = result.read(), result.status, result.headers.get_content_type()
        except HTTPError as error:
            with error:
                body, code, kind = error.read(), error.code, error.headers.get_content_type()
        assert code == status, f"{route}: 期望 {status} 实际 {code}: {body[:300]!r}"
        return json.loads(body) if kind == "application/json" else body

    def upload(self, role, data):
        request = Request(self.origin + "/api/images", data=data, headers={
            "Content-Type": "application/octet-stream",
            "X-Image-Name": f"{role}-{IMAGE_FILES[role]}",
        })
        with urlopen(request, timeout=10) as result:
            assert result.status == 201
            return json.loads(result.read())


def query_candidate(candidate_bytes):
    """对封存下载的候选镜像做只读核验，返回可查询差异。"""
    db = sqlite3.connect(":memory:")
    try:
        db.deserialize(candidate_bytes)
        fk_violations = db.execute("PRAGMA foreign_key_check").fetchall()
        integrity = db.execute("PRAGMA integrity_check").fetchone()[0]
        ledger = db.execute(
            "SELECT ledger_id,title,site_note,auto_flag FROM ledger ORDER BY ledger_id").fetchall()
        citations = db.execute(
            "SELECT ref_id,ledger_id FROM citation ORDER BY ref_id").fetchall()
        l102 = db.execute("SELECT site_note,auto_flag FROM ledger WHERE ledger_id='L-102'").fetchall()
        return {"integrity": integrity, "fk_violations": fk_violations,
                "ledger_rows": ledger, "citation_rows": citations,
                "l102_present": bool(l102), "l102_note_and_flag": l102}
    finally:
        db.close()


def run_case(client, sources, conflict_id, case_name, choice, out_dir):
    plan = client.call("/api/plans", sources, 201)
    plan_id = plan["id"]
    route = f"/api/plans/{plan_id}"
    (out_dir / f"{case_name}-plan.json").write_text(
        json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")

    # 未裁决直接重演应被拦。
    blocked_open = client.call(route + "/rehearse", {"revision": plan["revision"]}, 409)

    # 对唯一人工冲突下裁决，版本应从 0 推进到 1。
    decision = client.call(route + "/decisions",
                           {"revision": 0, "item": conflict_id, "choice": choice}, 200)
    assert decision["revision"] == 1

    # 过期版本裁决应被拒。
    stale = client.call(route + "/decisions",
                        {"revision": 0, "item": conflict_id, "choice": choice}, 409)

    rehearsal = client.call(route + "/rehearse", {"revision": 1}, 200)
    attempts = client.call(route + "/attempts")
    evidence = {"case": case_name, "conflict_choice": choice, "plan_id": plan_id,
                "blocked_before_decision": blocked_open, "decision": decision,
                "stale_revision": stale, "rehearsal": rehearsal, "attempts": attempts}

    if rehearsal["status"] == "passed":
        certificate = client.call(route + "/seal", {"revision": 1}, 200)
        # 重复封存应幂等。
        certificate_again = client.call(route + "/seal", {"revision": 1}, 200)
        assert certificate_again == certificate
        candidate = client.call(route + "/download")
        candidate_digest = hashlib.sha256(candidate).hexdigest()
        assert candidate_digest == certificate["candidate_digest"]
        (out_dir / f"{case_name}-certificate.json").write_text(
            json.dumps(certificate, ensure_ascii=False, indent=2), encoding="utf-8")
        (out_dir / f"{case_name}-candidate.sqlite").write_bytes(candidate)
        checks = query_candidate(candidate)
        evidence["sealed"] = True
        evidence["candidate_digest_verified"] = True
        evidence["candidate_checks"] = checks
        # 封存后再下裁决应被拒。
        evidence["edit_after_seal"] = client.call(
            route + "/decisions", {"revision": 1, "item": conflict_id, "choice": choice}, 409)
    else:
        # 被拒组合：不能封存，也不能下载。
        evidence["sealed"] = False
        evidence["seal_rejected"] = client.call(route + "/seal", {"revision": 1}, 409)
        evidence["download_rejected"] = client.call(route + "/download", status=404)
    (out_dir / f"{case_name}-result.json").write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
    return evidence


def main():
    parser = argparse.ArgumentParser(description="跑裁决练习的两种可交付结果与一个被拒组合")
    parser.add_argument("--images", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="pagefold-exercise-") as state_dir:
        store = Store(Path(state_dir) / "state.sqlite")
        with make_server(store, 0) as server:
            thread = threading.Thread(target=server.serve_forever)
            thread.start()
            try:
                client = Client(server.server_port)
                sources = {}
                for role, filename in IMAGE_FILES.items():
                    sources[role] = client.upload(role, (args.images / filename).read_bytes())["id"]

                # 三个计划基于同一批镜像；先建一个计划定位唯一冲突条目。
                probe = client.call("/api/plans", sources, 201)
                conflict = next(item for item in probe["items"] if item["automatic"] is None)
                conflict_id = conflict["id"]
                assert conflict["table"] == "ledger" and conflict["key"] == [["t", "L-102"]]
                automatic = [{"table": i["table"], "key": i["key"], "automatic": i["automatic"]}
                             for i in probe["items"] if i["automatic"] is not None]

                cases = [
                    ("strategy-a-respect-delete", "left"),
                    ("strategy-b-keep-rewrite", "right"),
                    ("rejected-base-rollback", "base"),
                ]
                results = []
                for case_name, choice in cases:
                    results.append(run_case(client, sources, conflict_id, case_name, choice, args.out))

                manifest = {
                    "generated_at": datetime.now(timezone.utc).isoformat(),
                    "image_sources": {role: {"image_id": sources[role], "file": IMAGE_FILES[role],
                                             "sha256": hashlib.sha256(
                                                 (args.images / IMAGE_FILES[role]).read_bytes()).hexdigest()}
                                      for role in IMAGE_FILES},
                    "conflict_item": {"id": conflict_id, "table": conflict["table"],
                                      "key": conflict["key"]},
                    "automatic_items": automatic,
                    "cases": [{"case": r["case"], "choice": r["conflict_choice"],
                               "status": r["rehearsal"]["status"], "sealed": r["sealed"],
                               "plan_id": r["plan_id"]} for r in results],
                }
                (args.out / "run-manifest.json").write_text(
                    json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
            finally:
                server.shutdown()
                thread.join()

    for result in results:
        status = result["rehearsal"]["status"]
        print(f"{result['case']}: choice={result['conflict_choice']} "
              f"rehearsal={status} sealed={result['sealed']}")
        if status == "blocked":
            print("  problem:", result["rehearsal"]["problem"])


if __name__ == "__main__":
    main()
