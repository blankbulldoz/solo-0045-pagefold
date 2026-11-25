"""按交班目录复核三个现场的证据。

默认离线模式（--offline，不启动服务），直接读取状态库与封存制品核对：
  python3 -I tools/verify_handover.py --offline

在线模式（对运行中的工作区复核只读入口，不写入任何数据）：
  python3 -I tools/verify_handover.py --live --base-url http://127.0.0.1:8745

任一检查失败即以非零码退出，并把完整核对结果写入 handover/verify-report.json。
"""
import argparse
import hashlib
import json
import sqlite3
import sys
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parent.parent
HANDOVER = ROOT / "handover"
CASE_IDS = ("site-a-open", "site-b-archived", "site-c-sealed")


class Report:
    def __init__(self):
        self.checks = []
        self.failed = 0

    def check(self, name, condition, detail):
        status = "PASS" if condition else "FAIL"
        if not condition:
            self.failed += 1
        self.checks.append({"status": status, "check": name, "detail": detail})
        return condition

    def section(self, title):
        self.checks.append({"status": "SECTION", "check": title, "detail": ""})


def load_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def sha256_file(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def open_state():
    db = sqlite3.connect(HANDOVER / "handover-state.sqlite")
    db.row_factory = sqlite3.Row
    return db


def offline(index):
    report = Report()
    with open_state() as db:
        for case_id in CASE_IDS:
            summary = load_json(HANDOVER / case_id / "case-summary.json")
            plan_id = summary["plan_id"]
            report.section(f"{case_id} {summary['title']}")
            plan = db.execute("SELECT revision,state,decisions FROM plans WHERE id=?",
                              (plan_id,)).fetchone()
            report.check("计划存在且版本/状态与摘要一致",
                         plan is not None and plan["revision"] == summary["revision"]
                         and plan["state"] == summary["state"],
                         {"db": dict(plan) if plan else None,
                          "summary": {"revision": summary["revision"], "state": summary["state"]}})
            attempts = [{"revision": r["revision"], "result": json.loads(r["result"]),
                         "has_candidate": r["has_candidate"]} for r in db.execute(
                "SELECT revision,result,candidate IS NOT NULL AS has_candidate FROM attempts "
                "WHERE plan_id=? ORDER BY revision", (plan_id,))]
            release = db.execute(
                "SELECT certificate,candidate FROM releases WHERE plan_id=?",
                (plan_id,)).fetchone()

            if case_id == "site-a-open":
                report.check("现场仍开放（还有工作）", plan["state"] == "open", plan["state"])
                report.check("当前版有 1 项未裁决", summary["unresolved"] == 1
                             and json.loads(plan["decisions"]) == {},
                             {"unresolved": summary["unresolved"],
                              "decisions": plan["decisions"]})
                passed = [a for a in attempts if a["result"]["status"] == "passed"]
                report.check("保留一条已通过但非当前版的历史尝试",
                             len(passed) == 1 and passed[0]["revision"] == 1
                             and passed[0]["revision"] != plan["revision"],
                             [{"revision": a["revision"], "status": a["result"]["status"]}
                              for a in attempts])
                report.check("历史通过尝试带候选字节，但 releases 表无封存对象",
                             passed and passed[0]["has_candidate"] == 1 and release is None,
                             {"attempts": [{"revision": a["revision"],
                                            "has_candidate": a["has_candidate"]} for a in attempts],
                              "release_exists": release is not None})
                report.check("封存被拒原因记录为 rehearsal_required",
                             summary["stale_seal_rejection"]["code"] == "rehearsal_required",
                             summary["stale_seal_rejection"])
                stress = [dict(r) for r in db.execute(
                    "SELECT revision,result,candidate IS NOT NULL AS has_candidate FROM attempts "
                    "WHERE plan_id=? ORDER BY revision", (summary["stress_plan_id"],))]
                stress_blocked = all(
                    json.loads(r["result"])["status"] == "blocked" and r["has_candidate"] == 0
                    for r in stress) and len(stress) == 1
                report.check("受阻旁证计划只有 blocked 尝试且无候选", stress_blocked,
                             [{"revision": r["revision"],
                              "result": json.loads(r["result"])["status"],
                              "has_candidate": r["has_candidate"]} for r in stress])
                stress_release = db.execute(
                    "SELECT 1 FROM releases WHERE plan_id=?",
                    (summary["stress_plan_id"],)).fetchone()
                report.check("受阻旁证计划无封存成果", stress_release is None,
                             {"release_exists": stress_release is not None})

            if case_id == "site-b-archived":
                report.check("计划已归档", plan["state"] == "archived", plan["state"])
                only_blocked = len(attempts) == 1 and all(
                    a["result"]["status"] == "blocked" and a["has_candidate"] == 0
                    for a in attempts)
                report.check("重演历史仅一条 blocked 记录、无候选", only_blocked,
                             [{"revision": a["revision"], "status": a["result"]["status"],
                               "has_candidate": a["has_candidate"]} for a in attempts])
                report.check("归档后仍无封存对象（历史不可当交付）", release is None,
                             {"release_exists": release is not None})
                report.check("写入口拒绝码为 closed_plan",
                             all(v["code"] == "closed_plan"
                                 for v in summary["closed_write_rejections"].values()),
                             summary["closed_write_rejections"])

            if case_id == "site-c-sealed":
                report.check("封存后已归档但 release 保留",
                             plan["state"] == "archived" and release is not None,
                             {"state": plan["state"], "release_exists": release is not None})
                certificate = json.loads(release["certificate"])
                candidate = release["candidate"]
                report.check("凭证候选摘要与状态库候选字节一致",
                             hashlib.sha256(candidate).hexdigest()
                             == certificate["candidate_digest"],
                             certificate["candidate_digest"])
                artifact = HANDOVER / case_id / "release" / "candidate.sqlite"
                on_disk = load_json(HANDOVER / case_id / "release" / "certificate.json")
                report.check("下载制品落盘且摘要与凭证一致",
                             artifact.exists() and sha256_file(artifact)
                             == certificate["candidate_digest"],
                             {"file": str(artifact.relative_to(ROOT)),
                              "digest": sha256_file(artifact) if artifact.exists() else None,
                              "certificate_digest": certificate["candidate_digest"]})
                report.check("落盘凭证与状态库凭证逐字节一致", on_disk == certificate,
                             {"plan": certificate["plan"], "revision": certificate["revision"]})
                probe = sqlite3.connect(":memory:")
                try:
                    probe.deserialize(candidate)
                    fk = probe.execute("PRAGMA foreign_key_check").fetchall()
                    integrity = probe.execute("PRAGMA integrity_check").fetchone()[0]
                    note = probe.execute(
                        "SELECT note FROM node WHERE bench='north' AND slot=1").fetchone()[0]
                finally:
                    probe.close()
                report.check("候选可独立打开：完整性/外键通过，裁决结果落库",
                             fk == [] and integrity == "ok" and note == "祖本节点",
                             {"foreign_key_check": fk, "integrity_check": integrity,
                              "north_node_note": note})
                report.check("写入口拒绝码为 closed_plan",
                             summary["archived_rehearse_rejection"]["code"] == "closed_plan"
                             and summary["sealed_write_rejection"]["code"] == "closed_plan",
                             {"rehearse": summary["archived_rehearse_rejection"],
                              "decide": summary["sealed_write_rejection"]})

        report.section("跨现场一致性")
        plan_rows = db.execute("SELECT id,state FROM plans").fetchall()
        states = {r["id"]: r["state"] for r in plan_rows}
        expected = {index["cases"][i]["plan_id"]:
                    {"site-a-open": "open", "site-b-archived": "archived",
                     "site-c-sealed": "archived"}[cid]
                    for i, cid in enumerate(CASE_IDS)}
        report.check("三个现场状态分别为 open / archived / archived",
                     all(states.get(pid) == state for pid, state in expected.items()),
                     {"expected": expected, "actual": states})
        release_ids = {r[0] for r in db.execute("SELECT plan_id FROM releases")}
        sealed_only = release_ids == {index["cases"][2]["plan_id"]}
        report.check("只有封存现场存在可领取 release", sealed_only,
                     {"release_plan_ids": sorted(release_ids)})
    return report


def http_json(url, binary=False):
    try:
        with urlopen(url, timeout=10) as response:
            body = response.read()
            kind = response.headers.get_content_type()
            if binary or kind != "application/json":
                return response.status, body
            return response.status, json.loads(body)
    except HTTPError as error:
        with error:
            body = error.read()
            kind = error.headers.get_content_type()
            if kind == "application/json":
                return error.code, json.loads(body)
            return error.code, body


def live(index, base_url):
    report = Report()
    for case_id in CASE_IDS:
        summary = load_json(HANDOVER / case_id / "case-summary.json")
        plan_id = summary["plan_id"]
        report.section(f"{case_id} 在线只读复核")
        status, plan = http_json(f"{base_url}/api/plans/{plan_id}")
        report.check("GET 计划入口可达且状态一致",
                     status == 200 and plan["state"] == summary["state"]
                     and plan["revision"] == summary["revision"],
                     {"http": status, "plan": plan if status == 200 else None})
        status, attempts = http_json(f"{base_url}/api/plans/{plan_id}/attempts")
        report.check("重演历史入口可查", status == 200 and isinstance(attempts, list),
                     {"http": status, "count": len(attempts) if status == 200 else None})
        status, page = http_json(f"{base_url}/plans/{plan_id}")
        marker = {"site-a-open": "裁决中", "site-b-archived": "已归档",
                  "site-c-sealed": "已归档"}[case_id]
        report.check("网页入口渲染且状态标签正确",
                     status == 200 and marker.encode() in page and "合流计划".encode() in page,
                     {"http": status, "marker": marker})
        if case_id == "site-c-sealed":
            status, candidate = http_json(f"{base_url}/api/plans/{plan_id}/download", binary=True)
            digest = hashlib.sha256(candidate).hexdigest() if status == 200 else None
            report.check("归档后下载仍可用且摘要不变",
                         status == 200 and digest == summary["candidate_digest"],
                         {"http": status, "digest": digest})
            status, proof = http_json(f"{base_url}/api/plans/{plan_id}/certificate")
            report.check("归档后凭证仍可领取",
                         status == 200 and proof["candidate_digest"]
                         == summary["candidate_digest"], {"http": status})
        else:
            status, body = http_json(f"{base_url}/api/plans/{plan_id}/download")
            report.check("无封存现场下载返回 unpublished",
                         status == 404 and body["code"] == "unpublished",
                         {"http": status, "body": body})
    return report


def main():
    parser = argparse.ArgumentParser(description="复核 Pagefold 交班目录证据")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--offline", action="store_true", help="离线核对状态库与制品")
    mode.add_argument("--live", action="store_true", help="对运行中的服务做只读复核")
    parser.add_argument("--base-url", default="http://127.0.0.1:8745")
    args = parser.parse_args()
    index = load_json(HANDOVER / "cases-index.json")
    report = offline(index) if args.offline else live(index, args.base_url.rstrip("/"))
    result = {"mode": "offline" if args.offline else "live",
              "total": sum(1 for c in report.checks if c["status"] in ("PASS", "FAIL")),
              "failed": report.failed, "checks": report.checks}
    mode = result["mode"]
    out = HANDOVER / f"verify-report-{mode}.json"
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    for check in report.checks:
        if check["status"] == "SECTION":
            print(f"\n## {check['check']}")
        else:
            print(f"[{check['status']}] {check['check']}")
    print(f"\n{result['total']} 项检查，{report.failed} 项失败；报告：{out.relative_to(ROOT)}")
    sys.exit(1 if report.failed else 0)


if __name__ == "__main__":
    main()
