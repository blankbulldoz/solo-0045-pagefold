"""自动演练交接核对：生成样例，按文档中的命令逐条验证通过/拒绝行为。

用法：python3 -m examples.handover_check [--keep 目录]

不指定 --keep 时使用临时目录并在结束时删除。每项检查都调用
examples/verify_release.py 的真实命令行，与接收方手工执行的方式一致。
"""

import argparse
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path
from threading import Thread

from examples.handover_sample import Client, run
from pagefold.server import make_server
from pagefold.store import Store

ROOT = Path(__file__).resolve().parent.parent
VERIFY = ROOT / "examples" / "verify_release.py"


def verify(*arguments):
    result = subprocess.run([sys.executable, "-I", str(VERIFY), *map(str, arguments)],
                            capture_output=True, text=True, timeout=120)
    return result.returncode, result.stdout + result.stderr


def check(results, name, condition, detail):
    results.append((name, condition, detail))
    print(f"{'通过' if condition else '失败'}  {name}" + (f"：{detail}" if not condition else ""))


def copy_state(source, target):
    with sqlite3.connect(source) as src, sqlite3.connect(target) as dst:
        src.backup(dst)


def main():
    parser = argparse.ArgumentParser(description="演练 Pagefold 交接核对")
    parser.add_argument("--keep", type=Path, help="保留样例的目录")
    args = parser.parse_args()
    holder = None
    if args.keep is None:
        holder = tempfile.TemporaryDirectory(prefix="pagefold-handover-")
        root = Path(holder.name)
    else:
        root = args.keep
        root.mkdir(parents=True, exist_ok=True)
        state = root / "state.sqlite"
        if state.exists():
            state.unlink()
    try:
        with make_server(Store(root / "state.sqlite"), 0) as server:
            thread = Thread(target=server.serve_forever)
            thread.start()
            try:
                manifest = run(Client(server.server_port), root)
            finally:
                server.shutdown()
                thread.join()
        sealed = manifest["sealed"]
        archived = manifest["archived"]
        mixed = root / "mixed"
        common = ["--certificate", root / sealed["certificate"], "--candidate", root / sealed["candidate"]]
        sources = ["--base", root / "quartz.sqlite", "--left", root / "cerulean.sqlite",
                   "--right", root / "umber.sqlite"]
        results = []

        code, out = verify(*common, *sources, "--state", root / "state.sqlite")
        check(results, "真实封存：完整核对通过", code == 0 and "合流内容" in out, out.strip().splitlines()[-3:])

        code, out = verify("--certificate", root / archived["certificate"],
                           "--candidate", root / archived["candidate"], *sources,
                           "--state", root / "state.sqlite")
        check(results, "归档成果：仍可完整核对", code == 0 and "第 1 版，archived" in out,
              out.strip().splitlines()[-3:])

        code, out = verify("--certificate", root / "rehearsal.json", "--candidate", root / sealed["candidate"])
        check(results, "只重演记录不能充当凭证", code == 1 and "凭证格式" in out, out.strip().splitlines()[-3:])

        code, out = verify(*common, "--base", root / "quartz.sqlite",
                           "--left", mixed / "left-is-really-right.sqlite",
                           "--right", mixed / "right-is-really-left.sqlite")
        check(results, "左右支互换：识别角色错误",
              code == 1 and "角色错误" in out and "属于凭证中的 左支" in out and "属于凭证中的 右支" in out,
              out.strip().splitlines()[-3:])

        code, out = verify(*common, "--base", mixed / "left-is-really-right.sqlite",
                           "--left", root / "cerulean.sqlite", "--right", root / "umber.sqlite")
        check(results, "祖本文件混入分支：识别角色错误",
              code == 1 and "来源 祖本 角色错误" in out, out.strip().splitlines()[-3:])

        code, out = verify(*common, "--base", mixed / "role-swapped-quartz.sqlite",
                           "--left", mixed / "role-swapped-cerulean.sqlite",
                           "--right", mixed / "role-swapped-umber.sqlite")
        check(results, "文件名正确但内容互换：仍被识别",
              code == 1 and "角色错误" in out and "计划摘要重算" in out, out.strip().splitlines()[-3:])

        code, out = verify(*common, "--base", mixed / "base-is-candidate.sqlite",
                           "--left", root / "cerulean.sqlite", "--right", root / "umber.sqlite")
        check(results, "候选冒充祖本：识别文件错误",
              code == 1 and "该文件就是封存候选" in out, out.strip().splitlines()[-3:])

        code, out = verify(*common, *sources, "--candidate", mixed / "tampered.sqlite",
                           "--state", root / "state.sqlite")
        check(results, "候选被篡改：摘要、打开、字节均报错",
              code == 1 and "候选摘要" in out and "候选字节" in out, out.strip().splitlines()[-3:])

        code, out = verify("--certificate", root / sealed["certificate"],
                           "--candidate", root / archived["candidate"], *sources,
                           "--state", root / "state.sqlite")
        check(results, "拿别计划候选：裁决摘要重算不符",
              code == 1 and "裁决摘要重算" in out, out.strip().splitlines()[-3:])

        copy_state(root / "state.sqlite", root / "state-copy.sqlite")
        code, out = verify(*common, "--state", root / "state-copy.sqlite")
        check(results, "只提供状态库副本也能核对封存记录",
              code == 0 and "封存凭证" in out, out.strip().splitlines()[-3:])

        code, out = verify(*common)
        check(results, "无来源无状态库：只核对文件与自洽，并明确声明范围",
              code == 0 and "未提供来源文件" in out and "范围说明" in out, out.strip().splitlines()[-3:])

        failed = [name for name, condition, _ in results if not condition]
        print(f"\n演练结束：{len(results) - len(failed)} 项通过，{len(failed)} 项失败。")
        if failed:
            print("失败项：" + "、".join(failed))
            return 1
        if args.keep:
            print(f"样例保留在 {root}")
        return 0
    finally:
        if holder is not None:
            holder.cleanup()


if __name__ == "__main__":
    sys.exit(main())
