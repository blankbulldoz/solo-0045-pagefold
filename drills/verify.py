"""离线核验全部演练卡：每张都在独立临时状态库 + 本机 HTTP 服务上重放。

用法：
    python3 -m drills.verify              # 全部卡
    python3 -m drills.verify 03-partial-null

除逐卡流程外，还核对：卡文件摘要、镜像可导入、通过卡封存幂等、受组卡重演幂等。
"""

import argparse
import hashlib
import json
import sys
import tempfile
from pathlib import Path
from threading import Thread

from pagefold.server import make_server
from pagefold.store import Store

from .build import CARDS_DIR, CARD_FORMAT, inspect
from .wire import Client, DrillFailure, load_manifest, walk


def select(values):
    index = json.loads((CARDS_DIR / "index.json").read_text(encoding="utf-8"))
    slugs = [card["slug"] for card in index["cards"]]
    if not values:
        return slugs
    unknown = [value for value in values if value not in slugs]
    if unknown:
        raise SystemExit(f"未知卡：{unknown}；可用：{slugs}")
    return values


def verify_card(slug, echo):
    directory = CARDS_DIR / slug
    manifest, _ = load_manifest(directory)
    if manifest["format"] != CARD_FORMAT:
        raise DrillFailure(f"卡格式不符：{manifest['format']}")
    for line in (directory / "SHA256SUMS").read_text(encoding="utf-8").splitlines():
        expected, name = line.split()
        actual = hashlib.sha256((directory / name).read_bytes()).hexdigest()
        if actual != expected:
            raise DrillFailure(f"{name} 摘要不符：{actual} != {expected}")
        inspect((directory / name).read_bytes(), f"{slug} {name}")
    with tempfile.TemporaryDirectory(prefix=f"pagefold-{slug}-") as root:
        with make_server(Store(Path(root) / "state.sqlite"), 0) as server:
            thread = Thread(target=server.serve_forever)
            thread.start()
            try:
                client = Client(f"http://127.0.0.1:{server.server_port}")
                observations = walk(client, manifest, directory, echo=echo)
                route = f"/api/plans/{observations['plan']}"
                if manifest["expectation"] == "passed":
                    # 封存后计划关闭：同版重演被拒，历史保留，重复封存返回同一凭证。
                    closed = client.call(route + "/rehearse", {"revision": 0}, 409)
                    if closed["code"] != "closed_plan":
                        raise DrillFailure(f"封存后重演未被拒绝：{closed}")
                    history = client.call(route + "/attempts")
                    if history != [observations["attempt"]]:
                        raise DrillFailure("封存后重演历史改变")
                    first = client.call(route + "/seal", {"revision": 0})
                    second = client.call(route + "/seal", {"revision": 0})
                    if first != second:
                        raise DrillFailure("重复封存结果改变")
                else:
                    repeat = client.call(route + "/rehearse", {"revision": 0})
                    if repeat != observations["attempt"]:
                        raise DrillFailure("同版重演没有复用同一结果")
                    if client.call(route + "/seal", {"revision": 0}, 409)["code"] != "rehearsal_required":
                        raise DrillFailure("受组卡仍可封存")
            finally:
                server.shutdown()
                thread.join()
    return manifest, observations


def main():
    parser = argparse.ArgumentParser(description="离线核验 Pagefold 值班演练卡")
    parser.add_argument("cards", nargs="*")
    args = parser.parse_args()
    failures = 0
    for slug in select(args.cards):
        print(f"\n=== {slug} ===")
        try:
            manifest, _ = verify_card(slug, echo=print)
        except (DrillFailure, AssertionError) as exc:
            failures += 1
            print(f"✗ {slug}：{exc}")
            continue
        ending = "通过" if manifest["expectation"] == "passed" else "受组"
        problem = manifest["expect"]["problem"]
        detail = f" · {problem['code']}" if problem else ""
        print(f"✓ {slug}：按设计{ending}{detail}")
    if failures:
        print(f"\n{failures} 张卡未通过核验。")
        return 1
    print("\n全部演练卡核验通过。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
