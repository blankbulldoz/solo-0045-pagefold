#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
为案卷材料生成逐文件 SHA-256 清单（标准 sha256sum 兼容格式）与分类 JSON。
只读遍历 materials/，不修改任何证据文件；输出写入 materials/MANIFEST.*。
以 `python3 -I build_manifest.py` 运行。
"""
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MAT = ROOT / "materials"

# 文件角色分类，供同事快速判断每件材料的证据用途
ROLE_RULES = [
    ("state/uploaded-", "输入留档：当时实际上传的 SQLite 镜像字节（复算输入）"),
    ("state/pagefold-state.sqlite", "状态留档：服务持久化库（plans/attempts/releases/images 全量历史）"),
    ("state/run-ids.json", "派生标识：本次运行的计划/冲突/各版候选摘要与凭证"),
    ("state/server.log", "运行日志：服务启动记录"),
    ("downloads/candidate", "封存产物：被封存、可下载的 rev2 SQLite 候选"),
    ("downloads/certificate", "封存凭证：绑定三来源/模式/计划/裁决/候选摘要的 JSON 凭证"),
    ("http/01-", "浏览页留档：首页 HTML"),
    ("derived/verification-report.json", "派生报告：21 项离线核对结果（含证明力分级）"),
]


def role_for(rel):
    for prefix, desc in ROLE_RULES:
        if rel.startswith(prefix):
            return desc
    if rel.endswith(".html"):
        return "浏览页留档：服务器渲染 HTML（无截图时的页面证据）"
    if rel.endswith(".json"):
        return "HTTP 留档：API JSON 响应体"
    if rel.endswith(".bin"):
        return "HTTP 留档：字节型响应（下载候选留档副本）"
    return "其他"


def main():
    entries = []
    files = sorted(p for p in MAT.rglob("*") if p.is_file()
                   and not p.name.startswith("MANIFEST"))
    for p in files:
        rel = p.relative_to(MAT).as_posix()
        data = p.read_bytes()
        entries.append({"path": rel, "sha256": hashlib.sha256(data).hexdigest(),
                        "size": len(data), "role": role_for(rel)})

    sha_lines = "\n".join(f"{e['sha256']}  {e['path']}" for e in entries) + "\n"
    (MAT / "MANIFEST.sha256").write_text(sha_lines, encoding="utf-8")

    manifest = {
        "case": "Pagefold 同计划两轮不同选择重演案卷",
        "file_count": len(entries),
        "total_bytes": sum(e["size"] for e in entries),
        "note": "哈希针对 materials/ 内相对路径文件；MANIFEST 自身不计入。",
        "files": entries,
    }
    (MAT / "MANIFEST.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"manifest: {len(entries)} files, {manifest['total_bytes']} bytes")
    print("-> materials/MANIFEST.sha256 , materials/MANIFEST.json")


if __name__ == "__main__":
    main()
