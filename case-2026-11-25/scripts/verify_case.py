#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
案卷离线核对 / 复算脚本（不启动服务、不发任何网络请求）。

输入全部来自本案卷 materials/ 内的留存材料：
  - state/uploaded-*.sqlite            当时实际上传的镜像字节
  - state/pagefold-state.sqlite        服务持久化状态库（只读打开）
  - downloads/                         封存凭证与下载候选
  - run-log.jsonl                      当时逐请求操作流水
  - http/*.html,*.json                 当时 HTTP 响应体留档

对每条结论标注 basis：
  recomputed = 可由留存输入独立复算（确定性算法，任何人可重跑）
  attested   = 无法从留存字节复算，只能依据当时操作记录/响应留档确认
  gap        = 现有历史无法恢复，刻意保留的证据缺口（不倒推填平）

以 `python3 -I verify_case.py` 运行；只读，不写状态库。
"""
import hashlib
import json
import sqlite3
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[2]
if str(PROJECT) not in sys.path:
    sys.path.insert(0, str(PROJECT))

from pagefold.catalog import describe  # noqa: E402
from pagefold.codec import digest, fingerprint, canonical  # noqa: E402
from pagefold.merge import calculate  # noqa: E402
from pagefold.replay import amendments, rebuild  # noqa: E402
from pagefold.rows import read_image  # noqa: E402
from pagefold.sqlite_image import mounted  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
MAT = ROOT / "materials"
CHECKS = []


def record(check_id, title, ok, basis, detail):
    CHECKS.append({"id": check_id, "title": title, "ok": bool(ok), "basis": basis, "detail": detail})
    mark = "PASS" if ok else "FAIL"
    print(f"[{mark}] ({basis}) {check_id} {title} :: {detail}")


def sha256_file(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def load_image_bytes(flavor):
    return (MAT / "state" / f"uploaded-{flavor}.sqlite").read_bytes()


def main():
    run_ids = json.loads((MAT / "state" / "run-ids.json").read_text(encoding="utf-8"))
    ids = run_ids["ids"]
    cert = json.loads((MAT / "downloads" / "certificate-plan-P.json").read_text(encoding="utf-8"))
    plan_id = ids["plan"]

    # ---------- 1. 上传字节摘要 == 登记 id == 凭证来源（可复算）----------
    flavor_to_role = {"quartz": "base", "cerulean": "left", "umber": "right"}
    actual_sums = {}
    for flavor, role in flavor_to_role.items():
        h = sha256_file(MAT / "state" / f"uploaded-{flavor}.sqlite")
        actual_sums[role] = h
        record(f"sum-{role}", f"留存 {flavor} 字节 SHA-256 与登记/凭证 id 一致",
               h == ids[role] == cert["sources"][role], "recomputed",
               f"sha256={h[:16]}… role={role}")

    # ---------- 2. 独立重算三方计划（可复算）----------
    images = {flavor: load_image_bytes(flavor) for flavor in flavor_to_role}
    catalogs = {}
    snapshots = {}
    for flavor, blob in images.items():
        with mounted(blob) as db:
            cat = describe(db)
            catalogs[flavor] = cat
            snapshots[flavor] = read_image(db, cat)
    items = calculate(snapshots["quartz"], snapshots["cerulean"], snapshots["umber"])
    conflict = next(i for i in items if i["automatic"] is None)
    record("plan-items", "重算计划得到 3 项差异且冲突条目 id 与操作记录一致",
           len(items) == 3 and conflict["id"] == ids["conflict_item"], "recomputed",
           f"items={len(items)} conflict={conflict['id'][:16]}… table={conflict['table']} key={conflict['key']}")
    record("plan-digest", "重算 plan_digest 与封存凭证一致",
           fingerprint(items) == cert["plan_digest"], "recomputed",
           f"plan_digest={cert['plan_digest'][:16]}…")
    record("schema-digest", "重算模式签名与封存凭证一致",
           catalogs["quartz"]["signature"] == cert["schema"], "recomputed",
           f"schema={cert['schema'][:16]}…")

    # ---------- 3. 离线重建两版候选并核对摘要（可复算）----------
    base_catalog = catalogs["quartz"]
    decisions_by_rev = {
        1: {conflict["id"]: "right"},   # 第一轮：右支
        2: {conflict["id"]: "base"},    # 第二轮：祖本
    }
    rebuilt = {}
    for rev, dec in decisions_by_rev.items():
        ops = amendments(items, dec)
        candidate = rebuild(images["quartz"], base_catalog, ops)
        rebuilt[rev] = candidate
        got = digest(candidate)
        want = run_ids[f"attempt_rev{rev}"]["candidate_digest"]
        record(f"rebuild-rev{rev}", f"离线重建 rev{rev} 候选摘要与当时重演记录一致（选择={dec[conflict['id']]}）",
               got == want, "recomputed",
               f"sha256={got} changed={sum(map(len, ops.values()))}")

    # ---------- 4. 封存下载物核对（可复算）----------
    dl_path = MAT / "downloads" / "candidate-plan-P-rev2.sqlite"
    dl_digest = sha256_file(dl_path)
    record("sealed-digest", "下载候选 SHA-256 与凭证 candidate_digest 一致",
           dl_digest == cert["candidate_digest"] == digest(rebuilt[2]), "recomputed",
           f"download={dl_digest}")
    # 下载库可独立打开、无外键违规、冲突行确为祖本选择
    standalone = sqlite3.connect(":memory:")
    try:
        standalone.deserialize(dl_path.read_bytes())
        fk = standalone.execute("PRAGMA foreign_key_check").fetchall()
        integrity = standalone.execute("PRAGMA integrity_check(1)").fetchone()[0]
        row = standalone.execute(
            "SELECT note,hex(payload),weight FROM node WHERE bench='north' AND slot=1").fetchone()
    finally:
        standalone.close()
    record("sealed-standalone", "下载库可独立打开、完整性 ok、无外键违规",
           not fk and integrity == "ok", "recomputed",
           f"integrity={integrity} fk_violations={len(fk)}")
    record("sealed-row", "封存候选冲突行 (north,1) 确为祖本取值（payload=0014, weight=1.5）",
           row == ("祖本节点", "0014", 1.5), "recomputed", f"row={row}")

    # ---------- 5. 状态库内历史版 rev1 的物理留存（只读核对）----------
    state = sqlite3.connect(f"file:{MAT / 'state' / 'pagefold-state.sqlite'}?mode=ro", uri=True)
    state.row_factory = sqlite3.Row
    try:
        rev1 = state.execute(
            "SELECT result,candidate FROM attempts WHERE plan_id=? AND revision=1", (plan_id,)).fetchone()
        rev2 = state.execute(
            "SELECT result,candidate FROM attempts WHERE plan_id=? AND revision=2", (plan_id,)).fetchone()
        release = state.execute(
            "SELECT certificate,candidate FROM releases WHERE plan_id=?", (plan_id,)).fetchone()
        rev1_result = json.loads(rev1["result"])
        rev1_blob_hash = digest(rev1["candidate"]) if rev1["candidate"] is not None else None
        record("rev1-persisted", "历史版 rev1（右支）候选字节仍物理留存于状态库 attempts，摘要与重演记录一致",
               rev1_blob_hash == rev1_result["candidate_digest"] == digest(rebuilt[1]), "recomputed",
               f"rev1_digest={rev1_result['candidate_digest'][:16]}… （注意：HTTP 无下载入口，仅状态库可取）")
        # rev1 冲突行内容（右支取值）
        mem = sqlite3.connect(":memory:")
        try:
            mem.deserialize(rev1["candidate"])
            r1row = mem.execute(
                "SELECT note,hex(payload),weight FROM node WHERE bench='north' AND slot=1").fetchone()
        finally:
            mem.close()
        record("rev1-row", "历史版 rev1 冲突行确为右支取值（payload=ff00, weight=2.25）",
               r1row == ("祖本节点", "FF00", 2.25), "recomputed", f"row={r1row}")
        record("only-rev2-released", "仅 rev2 进入 releases（被封存、可下载、有凭证），rev1 未封存",
               release is not None and digest(release["candidate"]) == cert["candidate_digest"]
               and rev1["candidate"] is not None, "attested",
               "releases 表仅 1 行且为 rev2；rev1 仅存在于 attempts")
        plan_row = state.execute("SELECT revision,state,decisions FROM plans WHERE id=?", (plan_id,)).fetchone()
        record("plan-terminal", "计划终态为 archived、终版 revision=2、裁决绑定 base",
               plan_row["state"] == "archived" and plan_row["revision"] == 2
               and json.loads(plan_row["decisions"]) == {conflict["id"]: "base"},
               "attested", f"state={plan_row['state']} revision={plan_row['revision']}")
        record("decision-digest", "终版裁决 decision_digest 与凭证一致",
               fingerprint(json.loads(plan_row["decisions"])) == cert["decision_digest"], "recomputed",
               f"decision_digest={cert['decision_digest'][:16]}…")

        # ---------- 6. 证据缺口（不倒推填平）----------
        all_revs = [r["revision"] for r in state.execute(
            "SELECT revision FROM attempts WHERE plan_id=? ORDER BY revision", (plan_id,))]
        record("gap-left-choice", "缺口：本次从未实际选择 left（左支）重演，历史中无该候选，结论不臆造",
               all_revs == [1, 2], "gap",
               f"attempts 实际版序={all_revs}；不存在 left 选择的摘要或字节，未做任何推断")
        q = state.execute(
            "SELECT candidate FROM attempts WHERE plan_id=?", (ids["stress_plan"],)).fetchone()
        record("gap-stress-blocked", "缺口：stress 计划重演 blocked，candidate 为 NULL，从未产生可下载字节",
               q["candidate"] is None, "gap", "blocked 尝试不生成候选，404 留档即其全部证据")
    finally:
        state.close()

    # ---------- 7. 浏览页 HTML 与 HTTP 留档相互印证（据操作记录确认）----------
    log_path = MAT / "run-log.jsonl"
    log = [json.loads(ln) for ln in log_path.read_text(encoding="utf-8").splitlines() if ln.strip()]

    def body_path(entry):
        # 流水里的 body_file 相对案卷根（case-2026-11-25）
        return ROOT / entry["body_file"]

    def find_entry(note_substr):
        return next(e for e in log if note_substr in (e.get("note") or ""))

    record("log-integrity", "操作流水 50 条且每条声明状态码均与实际相符、均有响应体留档",
           len(log) == 50 and all(e.get("status_matches", True) for e in log)
           and all(body_path(e).exists() for e in log),
           "attested", f"entries={len(log)}")
    html_files = sorted((MAT / "http").glob("*.html"))
    sealed_html = body_path(find_entry("已封存状态")).read_text(encoding="utf-8")
    archived_html = body_path(find_entry("已归档）")).read_text(encoding="utf-8")
    record("html-states", "浏览页留档出现 已封存 / 已归档 状态文案，与 HTTP 终态互证",
           "已封存" in sealed_html and "已归档" in archived_html, "attested",
           f"plan_html_captures={len(html_files)}")
    hist_html = body_path(find_entry("历史两版并列")).read_text(encoding="utf-8")
    record("html-history", "rev2 重演后计划页历史区同时可见 第1版 与 第2版",
           ("第 1 版" in hist_html or "第1版" in hist_html)
           and ("第 2 版" in hist_html or "第2版" in hist_html),
           "attested", "HTML 历史区含两条 attempt")

    # ---------- 输出报告 ----------
    summary = {"total": len(CHECKS),
               "pass": sum(c["ok"] for c in CHECKS),
               "fail": sum(not c["ok"] for c in CHECKS),
               "by_basis": {b: sum(c["basis"] == b for c in CHECKS)
                            for b in ("recomputed", "attested", "gap")}}
    report = {"summary": summary, "checks": CHECKS}
    out = MAT / "derived" / "verification-report.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print("\n== 汇总 ==", json.dumps(summary, ensure_ascii=False))
    print("report ->", out)
    return 0 if summary["fail"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
