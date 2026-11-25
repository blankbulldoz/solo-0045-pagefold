"""按一张演练卡走完真实 HTTP 入口；服务需已在 --origin 运行。

用法：
    python3 -m drills.run_card 01-unique-collision --origin http://127.0.0.1:8745
    python3 -m drills.run_card drills/cards/03-partial-null
"""

import argparse

from .wire import Client, DrillFailure, load_manifest, walk


def main():
    parser = argparse.ArgumentParser(description="按演练卡执行一次可复跑的合流流程")
    parser.add_argument("card", help="卡目录、card.json 或 slug")
    parser.add_argument("--origin", default="http://127.0.0.1:8745", help="Pagefold 服务地址")
    args = parser.parse_args()

    target = args.card
    if "/" not in target and not target.endswith(".json"):
        target = f"drills/cards/{target}"
    manifest, directory = load_manifest(target)
    print(f"{manifest['card']} {manifest['title']}")
    print(f"来源：{directory}")
    print(f"服务：{args.origin}")
    try:
        observations = walk(Client(args.origin), manifest, directory)
    except DrillFailure as exc:
        print(f"✗ 与清单不符：{exc}")
        raise SystemExit(1)
    print(f"结论：{'通过' if manifest['expectation'] == 'passed' else '受组'} "
          f"（计划 {observations['plan'][:12]}…）")
    if manifest["expectation"] == "blocked":
        print("本卡按设计在合流产物上受阻，属正确答案。")


if __name__ == "__main__":
    main()
