#!/usr/bin/env python3
"""交接凭证与密钥轮换演练（真实 HTTP 入口 + 离线验证）。

覆盖：
  1. Ed25519 RFC 8032 测试向量自检；
  2. 初始化组织密钥环（信任锚），轮换前后分别颁发交接凭证；
  3. 旧密钥停用后不能再新签，但旧交接凭证仍可离线验证（跨一次轮换）；
  4. 构造两份候选字节完全相同、但 left/right 角色互换的计划，
     证明内容摘要相同也不能拿另一份计划的公证代用；
  5. 离线验证器只凭公开材料（ring.json + 凭证）完成验证，
     并对篡改后的凭证给出拒绝结论；
  6. SQLite 下载流程保持可用，下载字节与凭证摘要一致。
"""

import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from threading import Thread

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pagefold import ed25519
from pagefold.codec import canonical
from pagefold.errors import Problem
from pagefold.handovers import compare_lineage
from pagefold.keyring import Keyring, verify_ring
from pagefold.server import make_server
from pagefold.store import Store
from examples.walkthrough import Client, expect

ORGANIZATION = "pagefold-drill-organization"


def section(title):
    print(f"\n=== {title} ===")


def seal_plan(client, sources, conflict_choice="right"):
    plan = client.call("/api/plans", dict(sources), 201)
    route = f"/api/plans/{plan['id']}"
    for item in plan["items"]:
        if item["automatic"] is None:
            client.call(route + "/decisions",
                        {"item": item["id"], "choice": conflict_choice,
                         "revision": plan["revision"]})
            plan = client.call(route)
    result = client.call(route + "/rehearse", {"revision": plan["revision"]})
    expect(result["status"] == "passed", f"重演未通过：{result}")
    certificate = client.call(route + "/seal", {"revision": plan["revision"]})
    return plan, certificate, route


def save(directory, name, payload):
    path = Path(directory) / name
    if isinstance(payload, (dict, list)):
        path.write_text(json.dumps(payload, ensure_ascii=True, indent=2), encoding="utf-8")
    else:
        path.write_bytes(payload)
    return path


def save_canonical(directory, name, document):
    # 凭证摘要绑定规范字节，落盘时保持与服务端 canonical() 完全一致。
    path = Path(directory) / name
    path.write_bytes(canonical(document).encode("ascii"))
    return path


def main():
    ed25519.selftest()
    print("1. Ed25519 RFC 8032 向量自检通过（纯标准库实现）")

    root = Path(tempfile.mkdtemp(prefix="pagefold-handover-drill-"))
    keys = root / "keys"
    public = root / "public"
    bundles = root / "bundles"
    public.mkdir()
    bundles.mkdir()
    store = Store(root / "state.sqlite")
    keyring = Keyring(keys)
    ring = keyring.initialize(ORGANIZATION)
    first_key = ring["active"]
    print(f"2. 初始化组织密钥环：活动密钥 {first_key[:16]}…（信任锚自签名）")

    with make_server(store, 0, keyring) as server:
        thread = Thread(target=server.serve_forever)
        thread.start()
        try:
            client = Client(server.server_port)

            section("轮换前：走完整 HTTP 工作流并颁发交接凭证")
            base = client.upload("quartz")
            left = client.upload("cerulean")
            right = client.upload("umber")
            sources = {"base": base["id"], "left": left["id"], "right": right["id"]}
            plan_a, cert_a, route_a = seal_plan(client, sources)
            handover_a = client.call(route_a + "/handover", {})
            expect(handover_a["signing_key_id"] == first_key, "交接凭证未使用轮换前密钥")
            expect(client.call(route_a + "/handover", {}) == handover_a, "重复颁发没有保持幂等")
            print(f"   计划 A {plan_a['id'][:12]}… 已封存并交接，签名密钥 {first_key[:16]}…")

            section("执行一次签名密钥轮换")
            ring = keyring.rotate("演练：年度签名密钥轮换")
            second_key = ring["active"]
            expect(second_key != first_key, "轮换后密钥未改变")
            expect(verify_ring(ring) == second_key, "公开环轮换链验证失败")
            retired = next(item for item in ring["entries"] if item["key_id"] == first_key)
            expect(retired["status"] == "retired", "旧密钥未标记停用")
            print(f"   新活动密钥 {second_key[:16]}…，旧密钥 {first_key[:16]}… 已停用")
            print("   轮换记录由旧密钥签名，公开环可独立验证整条链")

            section("轮换后：新计划用新密钥颁发交接凭证")
            plan_b, cert_b, route_b = seal_plan(client, sources)
            handover_b = client.call(route_b + "/handover", {})
            expect(handover_b["signing_key_id"] == second_key, "新交接凭证未使用轮换后密钥")
            print(f"   计划 B {plan_b['id'][:12]}… 的交接凭证由新密钥 {second_key[:16]}… 签署")

            section("候选字节相同但 left/right 角色互换：不可代用")
            # 交换两个分支镜像的左右位置，并让冲突项都回到祖本。
            # 两份计划重演出来的候选字节完全一致，但凭证绑定的角色位置不同。
            img_l = client.upload("cerulean")
            img_r = client.upload("umber")
            plan_lr, cert_lr, route_lr = seal_plan(
                client, {"base": base["id"], "left": img_l["id"], "right": img_r["id"]},
                conflict_choice="base")
            plan_rl, cert_rl, route_rl = seal_plan(
                client, {"base": base["id"], "left": img_r["id"], "right": img_l["id"]},
                conflict_choice="base")
            handover_lr = client.call(route_lr + "/handover", {})
            handover_rl = client.call(route_rl + "/handover", {})
            expect(cert_lr["candidate_digest"] == cert_rl["candidate_digest"],
                   "演练前提不成立：角色互换后的候选摘要不一致")
            comparison = compare_lineage(handover_lr, handover_rl)
            expect(comparison["same_candidate_digest"] and not comparison["same_sources"],
                   "角色互换场景构造不正确")
            print(f"   左-右计划候选摘要：{handover_lr['candidate_digest'][:20]}…")
            print(f"   右-左计划候选摘要：{handover_rl['candidate_digest'][:20]}…")
            print(f"   候选摘要相同：{comparison['same_candidate_digest']}")
            print(f"   来源角色相同：{comparison['same_sources']}")
            print(f"   结论：{comparison['conclusion']}")

            section("导出公开材料与下载包，供离线验证")
            ring_path = save(public, "ring.json", ring)
            for entry in ring["entries"]:
                shutil.copy(keys / f"{entry['key_id']}.pub.pem",
                            public / f"{entry['key_id']}.pub.pem")
            bundle_a = bundles / "plan-a"
            bundle_b = bundles / "plan-b"
            bundle_lr = bundles / "plan-lr"
            bundle_rl = bundles / "plan-rl"
            for directory, route, cert, handover in (
                    (bundle_a, route_a, cert_a, handover_a),
                    (bundle_b, route_b, cert_b, handover_b),
                    (bundle_lr, route_lr, cert_lr, handover_lr),
                    (bundle_rl, route_rl, cert_rl, handover_rl)):
                directory.mkdir()
                save_canonical(directory, "certificate.json", cert)
                save_canonical(directory, "handover.json", handover)
                sqlite_bytes = client.call(route + "/download")
                save(directory, "candidate.sqlite", sqlite_bytes)
                expect(hashlib.sha256(sqlite_bytes).hexdigest() == cert["candidate_digest"],
                       "下载字节与封存凭证摘要不一致")
            print("   封存凭证、交接凭证、候选 SQLite 与公开密钥环已分别落盘")
        finally:
            server.shutdown()
            thread.join()

    verifier_base = [sys.executable, "-I", str(Path(__file__).parent / "verify_handover.py"),
                     "--ring", str(ring_path)]

    section("离线验证：轮换前的旧凭证（跨一次轮换，只用公开材料）")
    run = subprocess.run([*verifier_base,
                          "--handover", str(bundle_a / "handover.json"),
                          "--certificate", str(bundle_a / "certificate.json"),
                          "--sqlite", str(bundle_a / "candidate.sqlite")],
                         capture_output=True, text=True)
    expect(run.returncode == 0, f"旧凭证离线验证失败：{run.stdout}\n{run.stderr}")
    print(run.stdout.strip())

    section("离线验证：轮换后的新凭证")
    run = subprocess.run([*verifier_base,
                          "--handover", str(bundle_b / "handover.json"),
                          "--certificate", str(bundle_b / "certificate.json"),
                          "--sqlite", str(bundle_b / "candidate.sqlite")],
                         capture_output=True, text=True)
    expect(run.returncode == 0, f"新凭证离线验证失败：{run.stdout}\n{run.stderr}")
    print(run.stdout.strip())

    section("角色互换的两份凭证：交叉验证必须拒绝代用")
    run = subprocess.run([*verifier_base,
                          "--handover", str(bundle_lr / "handover.json"),
                          "--certificate", str(bundle_rl / "certificate.json"),
                          "--cross", str(bundle_rl / "handover.json")],
                         capture_output=True, text=True)
    expect(run.returncode != 0, "角色互换的凭证被错误接受")
    print("   验证器拒绝：交接凭证与另一份计划的封存凭证字段不一致")
    print(run.stderr.strip().splitlines()[-1])

    section("篡改交接凭证：离线签名验证必须失败")
    tampered = json.loads((bundle_b / "handover.json").read_text(encoding="utf-8"))
    tampered["sources"]["left"], tampered["sources"]["right"] = (
        tampered["sources"]["right"], tampered["sources"]["left"])
    tampered_path = save(bundle_b, "tampered-handover.json", tampered)
    run = subprocess.run([*verifier_base,
                          "--handover", str(tampered_path),
                          "--certificate", str(bundle_b / "certificate.json")],
                         capture_output=True, text=True)
    expect(run.returncode != 0, "篡改后的交接凭证被接受")
    print(f"   验证器拒绝原因：{run.stderr.strip().splitlines()[-1]}")

    section("OpenSSL 交叉核验纯 Python 实现（PEM 公开材料）")
    openssl = shutil.which("openssl")
    if openssl:
        signature = bytes.fromhex(handover_b["signature"])
        unsigned = {key: value for key, value in handover_b.items() if key != "signature"}
        message = canonical(unsigned).encode("ascii")
        msg_path = root / "message.bin"
        sig_path = root / "signature.bin"
        msg_path.write_bytes(message)
        sig_path.write_bytes(signature)
        pem_path = public / f"{second_key}.pub.pem"
        run = subprocess.run([openssl, "pkeyutl", "-verify", "-pubin",
                              "-inkey", str(pem_path), "-rawin",
                              "-in", str(msg_path), "-sigfile", str(sig_path)],
                             capture_output=True, text=True)
        expect(run.returncode == 0 and "Success" in run.stdout,
               f"OpenSSL 核验失败：{run.stdout} {run.stderr}")
        print("   OpenSSL 3 使用公开 PEM 核验纯 Python 签名：Success")
    else:
        print("   未找到 openssl，跳过外部工具交叉核验")

    section("仍可验证的历史凭证 vs 不应新签的凭证")
    print("   - 历史凭证：轮换前计划 A 的交接凭证继续被公开环验证通过（见上）")
    print("   - 不应新签：旧密钥已 retired，Keyring.active() 只返回轮换末端密钥；")
    print("     服务端 issue() 只能拿到当前活动密钥，旧密钥无法再用于任何新签。")
    # 直接证明：强制把活动指针拨回旧密钥时，active() 因其 retired 状态拒绝。
    tampered_ring = json.loads((keys / "ring.json").read_text(encoding="utf-8"))
    real_active = tampered_ring["active"]
    tampered_ring["active"] = first_key
    (keys / "ring.json").write_text(json.dumps(tampered_ring), encoding="utf-8")
    try:
        Keyring(keys).active()
        raised = False
    except Problem as exc:
        raised = exc.code == "signing_key"
    expect(raised, "retired 旧密钥竟可用于新签")
    tampered_ring["active"] = real_active
    (keys / "ring.json").write_text(json.dumps(tampered_ring), encoding="utf-8")
    print("     演练：活动指针被拨回旧密钥时 active() 直接拒绝（signing_key/503）")
    print(f"\n演练材料目录：{root}")
    print("全部演练结论成立。")


if __name__ == "__main__":
    main()
