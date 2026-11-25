"""可重复入口：python3 -m examples.conformance [checks|fixtures|matrix]。

checks   运行全部保证检查（默认）
matrix   打印场景矩阵与每个场景的存在/缺席、单改/双改语义
fixtures 把三份合成镜像写到指定目录，供手工用 sqlite3 复查
"""
import argparse
import sys
from pathlib import Path

from . import fixtures
from .checks import CheckFailure, run_all
from .oracle import expected_plan


def _write_fixtures(directory):
    root = Path(directory)
    root.mkdir(parents=True, exist_ok=True)
    for role, index in (("base", 0), ("left", 1), ("right", 2)):
        path = root / f"conformance-{role}.sqlite"
        path.write_bytes(fixtures.build_image(index))
        print(path)
    null_path = root / "conformance-null-key.sqlite"
    null_path.write_bytes(fixtures.build_null_key_image())
    print(null_path)


def _print_matrix():
    trio = fixtures.build_trio()
    expected = expected_plan(*trio)
    by_identity = {(item["table"], item["identity"]): item for item in expected}
    rows = []
    for scenario in fixtures.SCENARIOS:
        from .oracle import normalize_key
        identity = normalize_key(scenario["key"])
        item = by_identity.get((scenario["table"], identity))
        if scenario["expect"] == "omit":
            automatic = "omit"
            reason = "三支全同，计划省略"
        elif item is None:
            automatic, reason = "缺失", "oracle 未生成该项"
        else:
            automatic, reason = item["automatic"], item["reason"]
        presence = "".join("1" if row is not None else "0" for row in scenario["branches"])
        rows.append((scenario["name"], scenario["table"], presence,
                     str(scenario["key"]), str(automatic), reason))
    width = [max(len(r[i]) for r in rows) for i in range(6)]
    for row in rows:
        print("  ".join(cell.ljust(width[i]) for i, cell in enumerate(row)))


def main(argv=None):
    parser = argparse.ArgumentParser(description="Pagefold 三方合流保证检查")
    parser.add_argument("command", choices=("checks", "matrix", "fixtures"),
                        nargs="?", default="checks")
    parser.add_argument("directory", nargs="?", default=None,
                        help="fixtures 子命令的输出目录，或 checks 的 --keep 目录")
    parser.add_argument("--keep", metavar="DIR", default=None,
                        help="保留检查所用状态库与镜像的目录")
    args = parser.parse_args(argv)

    if args.command == "fixtures":
        if not args.directory:
            parser.error("fixtures 需要指定输出目录")
        _write_fixtures(args.directory)
        return 0
    if args.command == "matrix":
        _print_matrix()
        return 0
    try:
        run_all(keep_dir=args.keep)
    except CheckFailure as failure:
        print(f"\n保证检查失败：{failure}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
