"""通过真实 HTTP 入口核对 supplier-kit 的每份样件。

按接收方实际操作顺序：先逐份上传镜像（场景 1、2 在这一步被拒绝），
再用三份镜像创建合流计划（场景 3 在这一步被拒绝），最后重演修正后的计划。
结果写入 verify-result.json，并在终端打印核对结论。

运行：python3 -I tools/verify_kit.py <样件目录> [--port 8745]
"""
import argparse
import hashlib
import json
import sys
import tempfile
import threading
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pagefold.server import make_server  # noqa: E402
from pagefold.store import Store  # noqa: E402


def call(origin, route, data=None, headers=None):
    request_headers = dict(headers or {})
    if isinstance(data, dict):
        data = json.dumps(data).encode()
        request_headers["Content-Type"] = "application/json"
    request = Request(origin + route, data=data, headers=request_headers)
    try:
        with urlopen(request, timeout=15) as result:
            return result.status, json.loads(result.read())
    except HTTPError as error:
        with error:
            return error.code, json.loads(error.read())


def upload(origin, path, label=None):
    data = Path(path).read_bytes()
    headers = {"Content-Type": "application/octet-stream",
               "X-Image-Name": label or Path(path).name}
    return call(origin, "/api/images", data, headers)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("kit", type=Path)
    parser.add_argument("--port", type=int, default=0)
    args = parser.parse_args()
    kit = args.kit.resolve()
    manifest = json.loads((kit / "manifest.json").read_text(encoding="utf-8"))

    with tempfile.TemporaryDirectory(prefix="pagefold-verify-") as state_dir:
        store = Store(Path(state_dir) / "state.sqlite")
        with make_server(store, args.port) as server:
            thread = threading.Thread(target=server.serve_forever)
            thread.start()
            origin = f"http://127.0.0.1:{server.server_port}"
            report = {"origin": origin, "cases": []}
            try:
                run_cases(origin, kit, manifest, report)
            finally:
                server.shutdown()
                thread.join()

    out = kit / "verify-result.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n",
                   encoding="utf-8")
    failures = [c for c in report["cases"] if not c["ok"]]
    for case in report["cases"]:
        print(("PASS" if case["ok"] else "FAIL"), case["case"], "—", case["summary"])
    print("结果写入", out)
    if failures:
        sys.exit(1)


def run_cases(origin, kit, manifest, report):
    cases = {entry["case"]: entry for entry in manifest["cases"]}

    # 场景 1：坏样件上传被拒，修正副本上传成功。
    c1 = cases["01-generated-column"]
    steps1 = []
    status, body = upload(origin, kit / c1["bad"]["path"])
    steps1.append({"step": "upload_bad", "http": status, "body": body})
    bad_ok = status == c1["bad"]["expected_rejection"]["http"] and \
        body.get("code") == c1["bad"]["expected_rejection"]["code"]
    status, body = upload(origin, kit / c1["fixed"]["path"])
    steps1.append({"step": "upload_fixed", "http": status,
                   "image_id": body.get("id")})
    fixed_ok = status == 201 and bool(body.get("catalog", {}).get("tables"))
    report["cases"].append({
        "case": "01-generated-column",
        "ok": bad_ok and fixed_ok,
        "summary": f"坏件 {steps1[0]['body'].get('code')} / 修正件 HTTP {status}",
        "steps": steps1,
    })

    # 场景 2：WAL 运行文件上传被拒，独立镜像上传成功。
    c2 = cases["02-wal-runtime-file"]
    steps2 = []
    status, body = upload(origin, kit / c2["bad"]["path"])
    steps2.append({"step": "upload_bad", "http": status, "body": body})
    bad_ok = status == c2["bad"]["expected_rejection"]["http"] and \
        body.get("code") == c2["bad"]["expected_rejection"]["code"]
    status, body = upload(origin, kit / c2["fixed"]["path"])
    steps2.append({"step": "upload_fixed", "http": status,
                   "image_id": body.get("id")})
    fixed_ok = status == 201 and bool(body.get("catalog", {}).get("tables"))
    report["cases"].append({
        "case": "02-wal-runtime-file",
        "ok": bad_ok and fixed_ok,
        "summary": f"坏件 {steps2[0]['body'].get('code')} / 修正件 HTTP {status}",
        "steps": steps2,
    })

    # 场景 3：三份坏样件各自可上传，但建计划因目录不一致被拒；修正后可建计划并重演。
    c3 = cases["03-same-index-name"]
    steps3 = []
    bad_ids = {}
    uploads_ok = True
    for role in ("base", "left", "right"):
        entry = c3["bad"]["entries"][role]
        status, body = upload(origin, kit / entry["path"], f"bad-{role}")
        steps3.append({"step": f"upload_bad_{role}", "http": status,
                       "image_id": body.get("id")})
        bad_ids[role] = body.get("id")
        uploads_ok &= status == 201
    status, body = call(origin, "/api/plans", bad_ids)
    steps3.append({"step": "create_plan_bad", "http": status, "body": body})
    plan_bad_ok = status == c3["bad"]["expected_plan_rejection"]["http"] and \
        body.get("code") == c3["bad"]["expected_plan_rejection"]["code"]

    fixed_ids = {}
    for role in ("base", "left", "right"):
        entry = c3["fixed"]["entries"][role]
        status, body = upload(origin, kit / entry["path"], f"fixed-{role}")
        steps3.append({"step": f"upload_fixed_{role}", "http": status,
                       "image_id": body.get("id")})
        fixed_ids[role] = body.get("id")
    status, plan = call(origin, "/api/plans", fixed_ids)
    steps3.append({"step": "create_plan_fixed", "http": status,
                   "plan_id": plan.get("id"), "items": len(plan.get("items", [])),
                   "unresolved": plan.get("unresolved")})
    plan_fixed_ok = status == 201 and plan.get("unresolved") == 0
    rehearse = None
    if plan_fixed_ok:
        status, rehearse = call(
            origin, f"/api/plans/{plan['id']}/rehearse", {"revision": 0})
        steps3.append({"step": "rehearse_fixed", "http": status, "result": rehearse})
    rehearse_ok = bool(rehearse and rehearse.get("status") == "passed")
    report["cases"].append({
        "case": "03-same-index-name",
        "ok": uploads_ok and plan_bad_ok and plan_fixed_ok and rehearse_ok,
        "summary": (f"三份坏件均上传成功、建计划 {steps3[3]['body'].get('code')}；"
                    f"修正后计划 HTTP {status}，重演 "
                    f"{rehearse.get('status') if rehearse else 'n/a'}"),
        "steps": steps3,
    })

    # 附：修正后的场景 3 计划可封存并下载，证明端到端接收成功。
    if plan_fixed_ok:
        status, seal = call(origin, f"/api/plans/{plan['id']}/seal",
                            {"revision": 0})
        status2, certificate = call(
            origin, f"/api/plans/{plan['id']}/certificate")
        request = Request(origin + f"/api/plans/{plan['id']}/download")
        with urlopen(request, timeout=15) as result:
            blob = result.read()
        digest = hashlib.sha256(blob).hexdigest()
        report["seal_proof"] = {
            "seal_http": status,
            "certificate_http": status2,
            "candidate_digest_matches_download":
                certificate.get("candidate_digest") == digest,
            "candidate_digest": digest,
            "download_size": len(blob),
        }


if __name__ == "__main__":
    main()
