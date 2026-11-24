"""碰撞举证工具：针对一次受阻重演还原相互碰撞的主键行。

供支持人员脱离计划页面使用，直接读取 Pagefold 状态库：

    python3 -m examples.collision_probe --state ./pagefold-state.sqlite \
        --plan <计划id> --revision <版本>

默认输出书面报告，追加 --json 输出可复核的机读行级证据。
"""

import argparse
import json

from pagefold.collisions import probe_attempt, render_report
from pagefold.errors import Problem
from pagefold.store import Store


def main():
    parser = argparse.ArgumentParser(description="针对受阻重演还原相互碰撞的主键行")
    parser.add_argument("--state", required=True, help="Pagefold 状态库路径")
    parser.add_argument("--plan", required=True, help="合流计划 id")
    parser.add_argument("--revision", type=int, required=True, help="受阻重演所属的计划版本")
    parser.add_argument("--json", action="store_true", help="输出机读 JSON 证据")
    args = parser.parse_args()
    try:
        evidence = probe_attempt(Store(args.state), args.plan, args.revision)
    except Problem as exc:
        parser.exit(2, f"{exc.code}：{exc.detail}\n")
    if args.json:
        print(json.dumps(evidence, ensure_ascii=False, indent=2))
    else:
        print(render_report(evidence))


if __name__ == "__main__":
    main()
