#!/usr/bin/env python3
"""离线交接凭证验证器（只使用公开材料）。

收件方拿到的材料：
  --ring      组织公开密钥环 ring.json（含信任锚与轮换链）
  --handover  交接凭证 pagefold-*.handover.json
  --certificate 对应封存凭证 pagefold-*.json
  --sqlite    可选，下载得到的候选 SQLite 字节

验证过程不需要服务在线，也不读取任何私钥材料：
  1. 校验密钥环的信任锚与轮换链签名；
  2. 用环中公钥验证交接凭证的 Ed25519 签名（支持已轮换的旧密钥）；
  3. 核对交接凭证与封存凭证的角色、模式、计划、裁决、候选摘要；
  4. 给定 SQLite 文件时复算字节摘要。
另提供 --cross 比较两份交接凭证，演示同候选不同角色不可代用。
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pagefold.handovers import compare_lineage, verify_handover  # noqa: E402


def load(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def main():
    parser = argparse.ArgumentParser(description="离线验证 Pagefold 交接凭证")
    parser.add_argument("--ring", required=True)
    parser.add_argument("--handover", required=True)
    parser.add_argument("--certificate", required=True)
    parser.add_argument("--sqlite")
    parser.add_argument("--cross", help="另一份交接凭证，用于角色代用比较")
    args = parser.parse_args()

    ring = load(args.ring)
    handover = load(args.handover)
    certificate_path = Path(args.certificate)
    certificate = load(certificate_path)
    certificate_bytes = certificate_path.read_bytes()
    candidate_bytes = Path(args.sqlite).read_bytes() if args.sqlite else None

    try:
        result = verify_handover(handover, certificate, certificate_bytes, ring, candidate_bytes)
    except (ValueError, KeyError) as exc:
        print(f"验证失败：{exc}", file=sys.stderr)
        sys.exit(1)
    print("交接凭证离线验证通过")
    print(f"  计划：{result['plan']}")
    print(f"  组织：{result['organization']}")
    print(f"  候选摘要：{result['candidate_digest']}")
    for label, detail in result["checks"].items():
        print(f"  - {label}: {detail}")

    if args.cross:
        other = load(args.cross)
        comparison = compare_lineage(handover, other)
        print("跨计划角色比较")
        for key, value in comparison.items():
            print(f"  - {key}: {value}")
        if not comparison["interchangeable"]:
            sys.exit(2)
    print("结论：凭证可由公开材料独立验证，来源角色绑定成立。")


if __name__ == "__main__":
    main()
