#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Pagefold 同计划、两轮不同裁决选择的实际重演记录器。

所有交互都走真实 HTTP 入口（http.client，无第三方依赖）。
每次请求的请求行、关键请求头、响应状态、响应头和响应体都落盘，
并写一份顺序流水 run-log.jsonl，供没有全时段页面截图的同事按序追踪。

脚本位于独立的 scripts 目录；案卷材料写入 materials 目录。
仅使用标准库，且以 `python3 -I` 隔离模式运行。
"""
import http.client
import json
import os
import socket
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

# 以 `python3 -I` 隔离运行时，cwd 与 PYTHONPATH 均不在 sys.path 中；
# 显式加入受信项目目录（材料目录绝不加入，避免其中被放置的同名模块被导入）。
PROJECT = Path(__file__).resolve().parents[2]
if str(PROJECT) not in sys.path:
    sys.path.insert(0, str(PROJECT))

from pagefold.demo import image  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
MAT = ROOT / "materials"
HTTP_DIR = MAT / "http"
HTML_DIR = MAT / "html"
DL_DIR = MAT / "downloads"
for d in (HTTP_DIR, HTML_DIR, DL_DIR):
    d.mkdir(parents=True, exist_ok=True)

HOST = "127.0.0.1"
PORT = int(os.environ.get("PAGEFOLD_PORT", "8745"))
ORIGIN = f"http://{HOST}:{PORT}"


def now_iso():
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


class Recorder:
    def __init__(self):
        self.seq = 0
        self.log = open(MAT / "run-log.jsonl", "w", encoding="utf-8")
        self.ids = {}

    def _emit(self, entry):
        self.log.write(json.dumps(entry, ensure_ascii=False) + "\n")
        self.log.flush()

    def request(self, method, route, body=None, content_type=None, extra_headers=None,
                expect_status=None, save_body_as=None, note=""):
        self.seq += 1
        n = self.seq
        headers = dict(extra_headers or {})
        payload = None
        if body is not None:
            if isinstance(body, (dict, list)):
                payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
                headers["Content-Type"] = content_type or "application/json"
            else:
                payload = body
                headers["Content-Type"] = content_type or "application/octet-stream"
        conn = http.client.HTTPConnection(HOST, PORT, timeout=15)
        conn.request(method, route, body=payload, headers=headers)
        resp = conn.getresponse()
        data = resp.read()
        ctype = resp.getheader("Content-Type") or ""
        is_json = "application/json" in ctype
        parsed = json.loads(data.decode("utf-8")) if is_json else None
        conn.close()

        body_file = None
        if save_body_as:
            target = Path(save_body_as)
            if not target.is_absolute():
                target = (DL_DIR if "download" in note else HTTP_DIR) / target
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            body_file = str(target.relative_to(ROOT))
        else:
            # 默认也把每份响应体单独落盘，便于逐件核对
            ext = "json" if is_json else ("html" if "text/html" in ctype else "bin")
            target = HTTP_DIR / f"{n:02d}-{method.lower()}-{route.replace('/', '_').strip('_') or 'root'}.{ext}"
            target.write_bytes(data)
            body_file = str(target.relative_to(ROOT))

        entry = {
            "seq": n, "ts": now_iso(), "method": method, "route": route,
            "request_headers": {k: v for k, v in headers.items()},
            "status": resp.status, "content_type": ctype,
            "content_length_header": resp.getheader("Content-Length"),
            "body_bytes": len(data), "body_file": body_file, "note": note,
        }
        if expect_status is not None:
            entry["expect_status"] = expect_status
            entry["status_matches"] = (resp.status == expect_status)
        if parsed is not None:
            entry["json"] = parsed
        self._emit(entry)
        print(f"[{n:02d}] {method} {route} -> {resp.status} ({len(data)}B) {note}")
        return resp.status, ctype, data, parsed

    def close(self):
        self.log.close()


def wait_port(timeout=20.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection((HOST, PORT), timeout=1):
                return True
        except except_socket():
            time.sleep(0.25)
    raise RuntimeError("server did not open port")


def except_socket():
    return (OSError,)


def main():
    wait_port()
    rec = Recorder()
    r = rec.request

    # 0. 首页（HTML 浏览面）
    r("GET", "/", note="首页 HTML（浏览面基线）", save_body_as=None)

    # 1. 登记三份镜像（同一字节重复上传用于验证复用，坏镜像用于验证拒绝）
    flavors = {"quartz": "石英祖本", "cerulean": "青蓝分支", "umber": "赤褐分支"}
    img_ids = {}
    for flavor, label in flavors.items():
        status, _, _, parsed = r(
            "POST", "/api/images", body=image(flavor),
            extra_headers={"X-Image-Name": quote(label), "Content-Type": "application/octet-stream"},
            expect_status=201, note=f"登记镜像 {flavor}")
        img_ids[flavor] = parsed["id"]
        # 同时把上传字节留存，保证离线可复算
        (MAT / "state" / f"uploaded-{flavor}.sqlite").write_bytes(image(flavor))
    # 重复上传：应复用同一 id
    status, _, _, dup = r("POST", "/api/images", body=image("quartz"),
                          extra_headers={"X-Image-Name": quote("石英祖本重复")},
                          expect_status=201, note="重复上传同一字节（应复用 id）")
    # 坏镜像：应 422
    r("POST", "/api/images", body=b"broken-not-sqlite",
      extra_headers={"X-Image-Name": "broken"}, expect_status=422, note="坏镜像应被拒绝")

    base_id, left_id, right_id = img_ids["quartz"], img_ids["cerulean"], img_ids["umber"]
    rec.ids.update(base=base_id, left=left_id, right=right_id, duplicate=dup["id"])

    # 2. 镜像浏览面 + 目录 + 行（HTML 与 JSON 双证据）
    r("GET", f"/images/{base_id}", note="祖本镜像页 HTML")
    cat_status, _, _, catalog = r("GET", f"/api/images/{base_id}", note="祖本镜像目录 JSON")
    r("GET", f"/api/images/{base_id}/rows?table=node", note="祖本 node 行 JSON")
    r("GET", f"/images/{base_id}/rows?table=node", note="祖本 node 行 HTML")
    r("GET", f"/api/images/{left_id}/rows?table=node", note="左支 node 行 JSON")
    r("GET", f"/api/images/{right_id}/rows?table=node", note="右支 node 行 JSON")
    r("GET", "/api/images/missing", expect_status=404, note="未知镜像 404 证据")

    # 3. 创建计划
    sources = {"base": base_id, "left": left_id, "right": right_id}
    status, _, _, plan = r("POST", "/api/plans", body=sources, expect_status=201,
                           note="创建三方合流计划 P")
    plan_id = plan["id"]
    rec.ids["plan"] = plan_id
    conflict = next(i for i in plan["items"] if i["automatic"] is None)
    conflict_id = conflict["id"]
    rec.ids["conflict_item"] = conflict_id
    r("GET", f"/plans/{plan_id}", note="计划页 HTML（裁决前，显示 1 项待裁决）")
    r("GET", f"/api/plans/{plan_id}", note="计划 JSON（裁决前 rev0）")
    r("POST", f"/api/plans/{plan_id}/rehearse", body={"revision": 0},
      expect_status=409, note="未决时重演应被拒绝")

    # ---- 第一轮选择：right（右支） ----
    r("POST", f"/api/plans/{plan_id}/decisions",
      body={"revision": 0, "item": conflict_id, "choice": "right"},
      note="第一轮裁决：选右支，推进到 rev1")
    r("POST", f"/api/plans/{plan_id}/decisions",
      body={"revision": 0, "item": conflict_id, "choice": "right"},
      expect_status=409, note="旧版序重写应被拒绝（stale_revision）")
    r("POST", f"/api/plans/{plan_id}/decisions",
      body={"revision": 1, "item": conflict_id, "choice": "right"},
      note="同选择幂等重试，版本保持 rev1")
    r("GET", f"/plans/{plan_id}", note="计划页 HTML（rev1，右支已选）")
    status, _, _, attempt1 = r("POST", f"/api/plans/{plan_id}/rehearse",
                               body={"revision": 1}, note="第一轮重演 rev1（通过）")
    r("POST", f"/api/plans/{plan_id}/rehearse", body={"revision": 1},
      note="同版重演应复用同一证据")
    r("GET", f"/api/plans/{plan_id}/attempts", note="重演历史（此刻仅 rev1）")
    r("GET", f"/plans/{plan_id}", note="计划页 HTML（rev1 重演后，历史区可见）")

    # ---- 改选：base（祖本），第二轮不同选择 ----
    r("POST", f"/api/plans/{plan_id}/decisions",
      body={"revision": 1, "item": conflict_id, "choice": "base"},
      note="第二轮改选祖本，推进到 rev2")
    r("GET", f"/api/plans/{plan_id}", note="计划 JSON 视角（rev2）")
    r("GET", f"/api/plans/{plan_id}/attempts", note="改选后历史（rev1 记录仍在）")
    # 旧版 rev1 的记录不能用于封存 rev2
    r("POST", f"/api/plans/{plan_id}/seal", body={"revision": 2},
      expect_status=409, note="未经当前版重演直接封存应被拒绝")
    status, _, _, attempt2 = r("POST", f"/api/plans/{plan_id}/rehearse",
                               body={"revision": 2}, note="第二轮重演 rev2（通过，不同选择）")
    r("GET", f"/api/plans/{plan_id}/attempts", note="重演历史（rev1 历史版 + rev2 当前版）")
    r("GET", f"/plans/{plan_id}", note="计划页 HTML（rev2 重演后，历史两版并列）")

    # ---- 封存 rev2（祖本选择被封存）----
    status, _, _, cert = r("POST", f"/api/plans/{plan_id}/seal",
                           body={"revision": 2}, note="封存 rev2 候选与凭证")
    r("POST", f"/api/plans/{plan_id}/seal", body={"revision": 2},
      note="重复封存应返回同一凭证（幂等）")
    r("POST", f"/api/plans/{plan_id}/decisions",
      body={"revision": 2, "item": conflict_id, "choice": "right"},
      expect_status=409, note="封存后改裁决应被拒绝（closed_plan）")
    r("GET", f"/api/plans/{plan_id}/certificate", note="封存凭证 JSON",
      save_body_as="certificate-plan-P.json")
    r("GET", f"/api/plans/{plan_id}/download", note="封存候选 SQLite 下载",
      save_body_as="candidate-plan-P-rev2.sqlite")
    r("GET", f"/plans/{plan_id}", note="计划页 HTML（已封存状态）")

    # ---- 归档 ----
    r("POST", f"/api/plans/{plan_id}/archive", body={"revision": 2}, note="归档计划")
    r("GET", f"/api/plans/{plan_id}", note="归档后计划 JSON（state=archived）")
    r("POST", f"/api/plans/{plan_id}/rehearse", body={"revision": 2},
      expect_status=409, note="归档后重演应被拒绝")
    r("GET", f"/api/plans/{plan_id}/download", note="归档后仍可下载封存候选")
    r("GET", f"/api/plans/{plan_id}/certificate", note="归档后仍可提取凭证")
    r("GET", f"/plans/{plan_id}", note="计划页 HTML（已归档）")

    # 4. 旁证：stress 两分支各自合法、合流触发唯一约束冲突（blocked，无可下载候选）
    sl = r("POST", "/api/images", body=image("stress-left"),
           extra_headers={"X-Image-Name": quote("stress-left")}, expect_status=201,
           note="登记 stress-left")[3]
    sr = r("POST", "/api/images", body=image("stress-right"),
           extra_headers={"X-Name-Name": "stress-right", "X-Image-Name": quote("stress-right")},
           expect_status=201, note="登记 stress-right")[3]
    (MAT / "state" / "uploaded-stress-left.sqlite").write_bytes(image("stress-left"))
    (MAT / "state" / "uploaded-stress-right.sqlite").write_bytes(image("stress-right"))
    q = r("POST", "/api/plans",
          body={"base": base_id, "left": sl["id"], "right": sr["id"]},
          expect_status=201, note="创建 stress 计划 Q（自动项，无冲突待决）")[3]
    r("POST", f"/api/plans/{q['id']}/rehearse", body={"revision": 0},
      note="stress 重演应 blocked（唯一约束冲突）")
    r("GET", f"/api/plans/{q['id']}/download", expect_status=404,
      note="blocked 候选不可下载（unpublished）")
    r("GET", f"/api/plans/{q['id']}/certificate", expect_status=404,
      note="blocked 候选无凭证（unpublished）")
    rec.ids["stress_plan"] = q["id"]

    # 记录关键派生标识，供核对脚本使用
    (MAT / "state" / "run-ids.json").write_text(
        json.dumps({"ids": rec.ids, "attempt_rev1": attempt1, "attempt_rev2": attempt2,
                    "certificate": cert, "sources": sources}, ensure_ascii=False, indent=2),
        encoding="utf-8")
    rec.close()
    print("DONE. log ->", (MAT / "run-log.jsonl"))


if __name__ == "__main__":
    main()
