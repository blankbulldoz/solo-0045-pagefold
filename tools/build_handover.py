"""用同一组样例镜像构造交班三个现场，并通过真实 HTTP 入口固化证据。

现场：
  site-a-open/      开放计划（仍有工作）：rev1 曾通过，改判后失去当前资格；
                    当前 rev2 待裁决，重演被拦，无任何可领取成果。
  site-b-archived/  已归档未封存：受阻计划直接归档，无封存对象，只剩历史。
  site-c-sealed/    封存后归档：归档不销毁成果，SQLite 与凭证继续可领取。

用法：
  python3 -m tools.build_handover
所有状态写入 handover/，状态库每次重建，证据可由 tools.verify_handover 复核。
"""
import hashlib
import json
import shutil
import sqlite3
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from threading import Thread
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from pagefold.demo import image
from pagefold.server import make_server
from pagefold.store import Store

ROOT = Path(__file__).resolve().parent.parent
HANDOVER = ROOT / "handover"
SITES = {
    "site-a-open": HANDOVER / "site-a-open",
    "site-b-archived": HANDOVER / "site-b-archived",
    "site-c-sealed": HANDOVER / "site-c-sealed",
}
SAMPLE_NAMES = ("quartz", "cerulean", "umber", "stress-left", "stress-right")
ROLES = ("base", "left", "right")
ROLE_SAMPLES = {"base": "quartz", "left": "cerulean", "right": "umber"}


def role_sources(images):
    return {role: images[sample]["id"] for role, sample in ROLE_SAMPLES.items()}


class Client:
    def __init__(self, port):
        self.origin = f"http://127.0.0.1:{port}"
        self.trace = []

    def call(self, route, data=None, status=200, binary=False, headers=None):
        request_headers = dict(headers or {})
        if data is not None and not isinstance(data, (bytes, bytearray)):
            data = json.dumps(data).encode()
            request_headers["Content-Type"] = "application/json"
        request = Request(self.origin + route, data=data, headers=request_headers)
        try:
            with urlopen(request, timeout=15) as result:
                body, actual, kind = result.read(), result.status, result.headers.get_content_type()
        except HTTPError as error:
            with error:
                body, actual, kind = error.read(), error.code, error.headers.get_content_type()
        entry = {"route": route, "method": "POST" if data is not None else "GET",
                 "expected": status, "actual": actual, "content_type": kind}
        if binary:
            entry["sha256"] = hashlib.sha256(body).hexdigest()
            entry["bytes"] = len(body)
        else:
            try:
                entry["body"] = json.loads(body)
            except json.JSONDecodeError:
                entry["body_text"] = body.decode("utf-8", "replace")
        self.trace.append(entry)
        assert actual == status, f"{route}: {actual} != {status}: {body[:300]!r}"
        if binary:
            return body
        return json.loads(body) if kind == "application/json" else body

    def upload(self, name):
        return self.call("/api/images", image(name), 201,
                         headers={"X-Image-Name": name, "Content-Type": "application/octet-stream"})

    def post(self, route, payload, status=200):
        return self.call(route, payload, status)

    def get(self, route, status=200, binary=False):
        return self.call(route, None, status, binary=binary)


def save_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def conflict_item(plan):
    return next(item for item in plan["items"] if item["automatic"] is None)


def prepare_directories():
    # 只清理可再生成的制品，保留 handover/*.md 交班文档。
    generated = [HANDOVER / name for name in (
        "handover-state.sqlite", "handover-state.sqlite-wal", "handover-state.sqlite-shm",
        "images-manifest.json", "cases-index.json", "http-trace.json",
        "verify-report-offline.json", "verify-report-live.json", *SITES)]
    for target in generated:
        if target.is_dir():
            shutil.rmtree(target)
        elif target.exists():
            target.unlink()
    HANDOVER.mkdir(parents=True, exist_ok=True)
    for directory in SITES.values():
        directory.mkdir(parents=True)


def build_site_a(client, images):
    """开放现场：保留一条已通过但失去当前资格的历史重演。"""
    site = SITES["site-a-open"]
    plan = client.post("/api/plans", role_sources(images), 201)
    route = f"/api/plans/{plan['id']}"
    item = conflict_item(plan)
    # rev0 -> rev1：冲突选右支，重演通过（这是后来“失去当前资格”的那条通过记录）。
    first = client.post(route + "/decisions",
                        {"revision": 0, "item": item["id"], "choice": "right"}, 200)
    rev_passed = first["revision"]
    passed = client.post(route + "/rehearse", {"revision": rev_passed}, 200)
    assert passed["status"] == "passed"
    # rev1 -> rev2：改判祖本，真实变更推进版本，rev1 的通过随即变成历史、不能再封存。
    second = client.post(route + "/decisions",
                         {"revision": rev_passed, "item": item["id"], "choice": "base"}, 200)
    rev_rebased = second["revision"]
    # 看见“通过”就封存必须被拒绝：当前 rev2 没有自己的通过候选。
    stale_seal = client.post(route + "/seal", {"revision": rev_rebased}, 409)
    assert stale_seal["code"] == "rehearsal_required"
    # 当前版改回未决（rev2 -> rev3），重演当前版被拦，证明现场仍有工作。
    cleared = client.post(route + "/decisions",
                          {"revision": rev_rebased, "item": item["id"], "choice": "clear"}, 200)
    rev_open = cleared["revision"]
    blocked_review = client.post(route + "/rehearse", {"revision": rev_open}, 409)
    assert blocked_review["code"] == "unresolved"
    no_download = client.get(route + "/download", 404)
    no_certificate = client.get(route + "/certificate", 404)
    assert no_download["code"] == no_certificate["code"] == "unpublished"
    # 受阻旁证：同样合法的 stress 分支合成时撞唯一约束，不产生可下载候选。
    stress = client.post("/api/plans", {
        "base": images["quartz"]["id"], "left": images["stress-left"]["id"],
        "right": images["stress-right"]["id"]}, 201)
    stress_route = f"/api/plans/{stress['id']}"
    stress_issue = client.post(stress_route + "/rehearse", {"revision": 0}, 200)
    assert stress_issue["status"] == "blocked" and stress_issue["problem"]["code"] == "constraint"
    stress_download = client.get(stress_route + "/download", 404)
    assert stress_download["code"] == "unpublished"
    final = client.get(route)
    summary = {
        "case": "site-a-open", "title": "开放计划：仍有裁决工作，无可领取成果",
        "plan_id": plan["id"], "stress_plan_id": stress["id"],
        "state": final["state"], "revision": final["revision"],
        "unresolved": final["unresolved"],
        "historical_passed_attempt_revision": passed["revision"],
        "historical_passed_candidate_digest": passed["candidate_digest"],
        "stale_seal_rejection": stale_seal,
        "unresolved_rehearsal_rejection": blocked_review,
        "stress_blocked_attempt": stress_issue,
        "entries": {
            "plan_page": f"/plans/{plan['id']}",
            "plan_api": route, "attempts": route + "/attempts",
            "stress_plan_page": f"/plans/{stress['id']}",
        },
    }
    save_json(site / "case-summary.json", summary)
    return summary


def build_site_b(client, images):
    """归档未封存现场：受阻计划归档，无成果可领，记录仅为历史。"""
    site = SITES["site-b-archived"]
    plan = client.post("/api/plans", {
        "base": images["quartz"]["id"], "left": images["stress-left"]["id"],
        "right": images["stress-right"]["id"]}, 201)
    route = f"/api/plans/{plan['id']}"
    blocked = client.post(route + "/rehearse", {"revision": 0}, 200)
    assert blocked["status"] == "blocked"
    no_seal = client.post(route + "/seal", {"revision": 0}, 409)
    assert no_seal["code"] == "rehearsal_required"
    no_download = client.get(route + "/download", 404)
    assert no_download["code"] == "unpublished"
    # 未封存也允许归档（open -> archived），归档后一切写入口关闭。
    client.post(route + "/archive", {"revision": 0}, 200)
    after = client.get(route)
    assert after["state"] == "archived"
    write_rehearse = client.post(route + "/rehearse", {"revision": 0}, 409)
    write_seal = client.post(route + "/seal", {"revision": 0}, 409)
    write_decide = client.post(route + "/decisions",
                               {"revision": 0, "item": "0" * 64, "choice": "base"}, 409)
    assert all(body["code"] == "closed_plan"
               for body in (write_rehearse, write_seal, write_decide))
    # 重复归档是幂等操作：状态不变，不产生新写入。
    archive_again = client.post(route + "/archive", {"revision": 0}, 200)
    assert archive_again["state"] == "archived"
    still_nothing = client.get(route + "/download", 404)
    assert still_nothing["code"] == "unpublished"
    attempts = client.get(route + "/attempts")
    summary = {
        "case": "site-b-archived", "title": "已归档未封存：只剩历史记录，无交付物",
        "plan_id": plan["id"], "state": after["state"], "revision": after["revision"],
        "unresolved": after["unresolved"], "blocked_attempt": blocked,
        "seal_before_archive_rejection": no_seal,
        "closed_write_rejections": {
            "rehearse": write_rehearse, "seal": write_seal, "decisions": write_decide},
        "archive_idempotent_reply": archive_again,
        "download_after_archive": still_nothing, "attempts_history": attempts,
        "entries": {"plan_page": f"/plans/{plan['id']}", "plan_api": route,
                    "attempts": route + "/attempts"},
    }
    save_json(site / "case-summary.json", summary)
    return summary


def build_site_c(client, images):
    """封存后归档现场：成果与凭证保留，可离线核对。"""
    site = SITES["site-c-sealed"]
    plan = client.post("/api/plans", role_sources(images), 201)
    route = f"/api/plans/{plan['id']}"
    item = conflict_item(plan)
    client.post(route + "/decisions",
                {"revision": 0, "item": item["id"], "choice": "right"}, 200)
    current = client.post(route + "/rehearse", {"revision": 1}, 200)
    assert current["status"] == "passed"
    certificate = client.post(route + "/seal", {"revision": 1}, 200)
    sealed = client.get(route)
    assert sealed["state"] == "sealed"
    # 封存后写入口已关闭。
    sealed_write = client.post(route + "/decisions",
                               {"revision": 1, "item": item["id"], "choice": "base"}, 409)
    assert sealed_write["code"] == "closed_plan"
    before_candidate = client.get(route + "/download", binary=True)
    before_proof = client.get(route + "/certificate")
    # sealed -> archived。
    client.post(route + "/archive", {"revision": 1}, 200)
    after = client.get(route)
    assert after["state"] == "archived"
    archived_rehearse = client.post(route + "/rehearse", {"revision": 1}, 409)
    assert archived_rehearse["code"] == "closed_plan"
    # 归档不销毁已封存对象：字节与凭证原样可取。
    after_candidate = client.get(route + "/download", binary=True)
    after_proof = client.get(route + "/certificate")
    assert after_candidate == before_candidate and after_proof == before_proof
    assert hashlib.sha256(after_candidate).hexdigest() == certificate["candidate_digest"]
    (site / "release").mkdir()
    (site / "release" / "candidate.sqlite").write_bytes(after_candidate)
    save_json(site / "release" / "certificate.json", after_proof)
    attempts = client.get(route + "/attempts")
    # 独立打开下载件，核对约束与裁决落库结果。
    probe = sqlite3.connect(":memory:")
    try:
        probe.deserialize(after_candidate)
        fk = probe.execute("PRAGMA foreign_key_check").fetchall()
        note = probe.execute(
            "SELECT note FROM node WHERE bench='north' AND slot=1").fetchone()
        integrity = probe.execute("PRAGMA integrity_check").fetchone()[0]
    finally:
        probe.close()
    independent_check = {"foreign_key_check": fk, "integrity_check": integrity,
                         "north_node_note": note[0],
                         "note_expected_from_right_choice": "祖本节点"}
    summary = {
        "case": "site-c-sealed", "title": "封存后归档：成果保留、可领取可离线复核",
        "plan_id": plan["id"], "state": after["state"], "revision": after["revision"],
        "unresolved": after["unresolved"], "sealed_certificate": certificate,
        "sealed_write_rejection": sealed_write,
        "archived_rehearse_rejection": archived_rehearse,
        "candidate_digest": certificate["candidate_digest"],
        "candidate_size": certificate["candidate_size"],
        "download_stable_after_archive": hashlib.sha256(after_candidate).hexdigest(),
        "independent_check": independent_check, "attempts_history": attempts,
        "entries": {"plan_page": f"/plans/{plan['id']}", "plan_api": route,
                    "attempts": route + "/attempts",
                    "download": route + "/download",
                    "certificate": route + "/certificate"},
    }
    save_json(site / "case-summary.json", summary)
    return summary


def capture_pages(client, summaries):
    """把每个现场的网页入口渲染结果存为 HTML 证据（含状态标签与重演历史）。"""
    for summary in summaries:
        site = SITES[summary["case"]]
        html = client.get(summary["entries"]["plan_page"])
        (site / "plan-page.html").write_bytes(html)
    stress_page = summaries[0]["entries"].get("stress_plan_page")
    if stress_page:
        (SITES["site-a-open"] / "stress-plan-page.html").write_bytes(client.get(stress_page))


def main():
    prepare_directories()
    state_path = HANDOVER / "handover-state.sqlite"
    store = Store(state_path)
    port_holder = {}
    server = make_server(store, 0)
    port_holder["port"] = server.server_port
    thread = Thread(target=server.serve_forever)
    thread.start()
    client = Client(server.server_port)
    try:
        images = {name: client.upload(name) for name in SAMPLE_NAMES}
        save_json(HANDOVER / "images-manifest.json", {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "sample_set": list(SAMPLE_NAMES),
            "images": {name: {"id": images[name]["id"], "label": images[name]["label"],
                              "size": images[name]["size"]} for name in SAMPLE_NAMES},
        })
        summaries = [build_site_a(client, images), build_site_b(client, images),
                     build_site_c(client, images)]
        capture_pages(client, summaries)
        save_json(HANDOVER / "cases-index.json", {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "state_database": str(state_path.relative_to(ROOT)),
            "server_port": server.server_port,
            "cases": summaries,
        })
        save_json(HANDOVER / "http-trace.json", client.trace)
    finally:
        server.shutdown()
        thread.join()
    print(f"handover built at {HANDOVER} (port {server.server_port})")


if __name__ == "__main__":
    main()
