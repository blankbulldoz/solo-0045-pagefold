"""跑同一成果的三次离线验收与篡改对照,证据写入 acceptance/evidence/。

三次验收(同一逻辑成果):
  01 minimal      仅凭证+成果                 → 字节匹配
  02 full-replay  凭证+成果+三份输入+裁决      → 实际行重演
  03 reorganized  内容等价但字节不同的成果文件  → 内容等价(逻辑比对)

三组篡改对照(必须被重新核对出来):
  04 swap-inputs   左右支输入调包              → 来源摘要不吻合
  05 swap-sources  调包并同步改凭证 sources    → 重演计划摘要不符
  06 edited-candidate 改库后仅更新候选摘要     → 重演候选与凭证不符

用法:python3 -m acceptance.run_acceptance(验收包不存在时先生成)
"""
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from acceptance import build_pack

ROOT = Path(__file__).resolve().parent.parent
EVIDENCE = ROOT / "acceptance" / "evidence"

SCENARIOS = [
    {"name": "01-minimal",
     "note": "仅凭证与成果:字节匹配",
     "args": ["--certificate", "acceptance/pack/certificate.json",
              "--deliverable", "acceptance/pack/deliverable.sqlite"],
     "expect": {"accepted": True, "level": "byte-match"}},
    {"name": "02-full-replay",
     "note": "凭证+成果+三份输入+裁决:实际行重演",
     "args": ["--pack", "acceptance/pack"],
     "expect": {"accepted": True, "level": "row-replay", "byte_identical": True}},
    {"name": "03-reorganized",
     "note": "内容等价但字节不同的成果:逻辑比对",
     "args": ["--pack", "acceptance/pack",
              "--deliverable", "acceptance/pack/deliverable-reorganized.sqlite"],
     "expect": {"accepted": True, "level": "logical-equal", "byte_identical": False}},
    {"name": "04-tamper-swap-inputs",
     "note": "左右支调包:来源摘要不吻合",
     "args": ["--pack", "acceptance/pack/tampered/swap-inputs"],
     "expect": {"accepted": False, "failed": ["inputs.left.digest", "inputs.right.digest"]}},
    {"name": "05-tamper-swap-sources",
     "note": "调包并改凭证来源:重演计划摘要不符",
     "args": ["--pack", "acceptance/pack/tampered/swap-sources"],
     "expect": {"accepted": False, "failed": ["plan.digest"]}},
    {"name": "06-tamper-edited-candidate",
     "note": "改库后仅更新候选摘要:重演候选与凭证不符",
     "args": ["--pack", "acceptance/pack/tampered/edited-candidate"],
     "expect": {"accepted": False, "failed": ["replay.digest", "deliverable.logical"]}},
]


def run_scenario(scenario):
    name = scenario["name"]
    report_rel = f"acceptance/evidence/{name}.report.json"
    proc = subprocess.run([sys.executable, "-I", "acceptance/verify.py", *scenario["args"],
                           "--json", report_rel], cwd=ROOT, capture_output=True, text=True)
    (EVIDENCE / f"{name}.stdout.txt").write_text(proc.stdout, encoding="utf-8")
    report = json.loads((ROOT / report_rel).read_text(encoding="utf-8"))
    verdict = report["verdict"]
    failed_ids = [check["id"] for layer in report["layers"]
                  for check in layer.get("checks", []) if not check["ok"]]
    expect = scenario["expect"]
    problems = []
    if verdict["accepted"] != expect["accepted"]:
        problems.append(f"结论不符:accepted={verdict['accepted']}")
    if expect["accepted"] and verdict.get("level") != expect.get("level"):
        problems.append(f"层级不符:level={verdict.get('level')}")
    for cid in expect.get("failed", []):
        if cid not in failed_ids:
            problems.append(f"预期失败的检查未失败:{cid}")
    if "byte_identical" in expect:
        comparison = report.get("comparison") or {}
        if comparison.get("byte_identical") != expect["byte_identical"]:
            problems.append("逻辑比对的字节一致性不符")
    if (proc.returncode == 0) != expect["accepted"]:
        problems.append(f"退出码不符:{proc.returncode}")
    return {"name": name, "note": scenario["note"], "args": scenario["args"],
            "exit_code": proc.returncode, "accepted": verdict["accepted"],
            "level": verdict.get("level"), "label": verdict.get("label"),
            "failed_checks": failed_ids, "report": report_rel,
            "expectation_met": not problems, "problems": problems}


def main():
    if not (ROOT / "acceptance" / "pack" / "certificate.json").is_file():
        build_pack.main()
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    results = [run_scenario(scenario) for scenario in SCENARIOS]
    summary = {"format": "pagefold-acceptance-evidence-v1",
               "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
               "scenarios": results,
               "all_expectations_met": all(result["expectation_met"] for result in results)}
    (EVIDENCE / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    for result in results:
        mark = "✓" if result["expectation_met"] else "✗"
        verdict = result["label"] if result["accepted"] else "不予验收"
        print(f"{mark} {result['name']}: {result['note']} → {verdict}")
        for problem in result["problems"]:
            print(f"    {problem}")
    print(f"证据:{EVIDENCE}(summary.json 与各报告)")
    return 0 if summary["all_expectations_met"] else 1


if __name__ == "__main__":
    sys.exit(main())
