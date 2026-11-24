#!/usr/bin/env python3
"""组织签名密钥环的命令行管理工具（演练用途）。

  init <目录> [--organization 名称]   生成信任锚密钥环
  rotate <目录> [--note 说明]         轮换签名密钥，旧密钥随即停用
  show <目录>                         查看公开环与各密钥状态
  export <目录> <输出目录>            只导出公开验证材料（ring.json + PEM）
  verify <ring.json>                 离线验证信任锚与整条轮换链

私钥材料（secrets.json、*.sec 内容）永远不会被 export 带出。
"""

import argparse
import json
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pagefold.keyring import Keyring, verify_ring  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description="管理 Pagefold 组织签名密钥环")
    sub = parser.add_subparsers(dest="command", required=True)
    init = sub.add_parser("init")
    init.add_argument("directory")
    init.add_argument("--organization", default="pagefold-drill-organization")
    rotate = sub.add_parser("rotate")
    rotate.add_argument("directory")
    rotate.add_argument("--note", default="组织签名密钥轮换")
    show = sub.add_parser("show")
    show.add_argument("directory")
    export = sub.add_parser("export")
    export.add_argument("directory")
    export.add_argument("output")
    verify = sub.add_parser("verify")
    verify.add_argument("ring")
    args = parser.parse_args()

    if args.command == "init":
        ring = Keyring(args.directory).initialize(args.organization)
        print(f"已初始化密钥环：{args.directory}")
        print(f"活动密钥：{ring['active']}")
    elif args.command == "rotate":
        ring = Keyring(args.directory).rotate(args.note)
        print(f"已轮换，新活动密钥：{ring['active']}")
        for entry in ring["entries"]:
            print(f"  {entry['key_id'][:16]}… sequence={entry['sequence']} status={entry['status']}")
    elif args.command == "show":
        ring = Keyring(args.directory).public_ring()
        print(json.dumps(ring, ensure_ascii=False, indent=2))
    elif args.command == "export":
        keyring = Keyring(args.directory)
        ring = keyring.public_ring()
        verify_ring(json.loads(json.dumps(ring)))
        output = Path(args.output)
        output.mkdir(parents=True, exist_ok=True)
        (output / "ring.json").write_text(
            json.dumps(ring, ensure_ascii=True, indent=2), encoding="utf-8")
        for entry in ring["entries"]:
            shutil.copy(keyring.directory / f"{entry['key_id']}.pub.pem",
                        output / f"{entry['key_id']}.pub.pem")
        print(f"公开材料已导出到：{output}（不含任何私钥）")
    elif args.command == "verify":
        ring = json.loads(Path(args.ring).read_text(encoding="utf-8"))
        active = verify_ring(ring)
        print(f"密钥环验证通过，活动密钥：{active}")


if __name__ == "__main__":
    main()
