"""生成一套可重复的交接样例，供交接文档中的核对步骤使用。

产出目录包含：
  sealed.json / sealed.sqlite            真实封存的凭证与候选
  archived.json / archived.sqlite        封存后归档的凭证与候选
  rehearsal.json / rehearsal-attempts.json  只重演通过、从未封存的记录（无凭证、无候选）
  mixed/                                 人为混放与角色互换的副本，用于演练失败恢复
  manifest.json                          各文件摘要与计划标识
  state.sqlite                           三个计划共用的状态库

用法：python3 -m examples.handover_sample /tmp/pagefold-handover
"""

import argparse
import hashlib
import json
import shutil
import sys
from pathlib import Path
from threading import Thread
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from pagefold.demo import image
from pagefold.server import make_server
from pagefold.store import Store


def expect(condition, detail):
    if not condition:
        raise RuntimeError(detail)


class Client:
    def __init__(self, port):
        self.origin = f"http://127.0.0.1:{port}"

    def call(self, route, data=None, status=200, headers=None):
        request_headers = dict(headers or {})
        if isinstance(data, dict):
            data = json.dumps(data).encode()
            request_headers["Content-Type"] = "application/json"
        request = Request(self.origin + route, data=data, headers=request_headers)
        try:
            with urlopen(request, timeout=10) as result:
                body, actual, kind = result.read(), result.status, result.headers.get_content_type()
        except HTTPError as error:
            with error:
                body, actual, kind = error.read(), error.code, error.headers.get_content_type()
        expect(actual == status, f"{route}: {actual} != {status}: {body[:300]}")
        return json.loads(body) if kind == "application/json" else body

    def upload(self, name, flavor=None, data=None):
        return self.call("/api/images", image(flavor or name) if data is None else data, 201, {
            "X-Image-Name": name, "Content-Type": "application/octet-stream"})


def write(path, data):
    path.write_bytes(data)
    return hashlib.sha256(data).hexdigest()


def run(client, output):
    output.mkdir(parents=True, exist_ok=True)
    mixed = output / "mixed"
    mixed.mkdir(exist_ok=True)

    base = client.upload("quartz")
    left = client.upload("cerulean")
    right = client.upload("umber")

    # 三份来源镜像原本的字节，用 HTTP 读取不可得，改用已登记摘要重新生成并校验。
    local = {name: image(name) for name in ("quartz", "cerulean", "umber")}
    for name, record in (("quartz", base), ("cerulean", left), ("umber", right)):
        expect(hashlib.sha256(local[name]).hexdigest() == record["id"], f"{name} 摘要与演示镜像不符")
        write(output / (name + ".sqlite"), local[name])

    sources = {"base": base["id"], "left": left["id"], "right": right["id"]}

    # 计划 A：真实封存。第 1 版通过，改裁决后第 2 版通过并封存。
    sealed_plan = client.call("/api/plans", sources, 201)
    route = f"/api/plans/{sealed_plan['id']}"
    conflict = next(item for item in sealed_plan["items"] if not item["automatic"])
    decision = {"item": conflict["id"], "choice": "right", "revision": 0}
    expect(client.call(route + "/decisions", decision)["revision"] == 1, "裁决未推进版本")
    expect(client.call(route + "/rehearse", {"revision": 1})["status"] == "passed", "第 1 版未通过")
    expect(client.call(route + "/decisions", dict(decision, choice="base", revision=1))["revision"] == 2,
           "第二次裁决未推进版本")
    expect(client.call(route + "/rehearse", {"revision": 2})["status"] == "passed", "第 2 版未通过")
    certificate = client.call(route + "/seal", {"revision": 2})
    sealed_bytes = client.call(route + "/download")
    sealed_certificate = client.call(route + "/certificate")
    write(output / "sealed.json", json.dumps(certificate, ensure_ascii=True, indent=2).encode("ascii"))
    write(output / "sealed.sqlite", sealed_bytes)
    write(output / "sealed-attempts.json",
          json.dumps(client.call(route + "/attempts"), ensure_ascii=True, indent=2).encode("ascii"))
    expect(sealed_certificate == certificate, "重复读取凭证不一致")

    # 计划 B：只重演通过、从未封存。当前版有通过记录，但没有凭证和可下载文件。
    rehearsal_plan = client.call("/api/plans", sources, 201)
    rehearsal_route = f"/api/plans/{rehearsal_plan['id']}"
    rehearsal_conflict = next(item for item in rehearsal_plan["items"] if not item["automatic"])
    expect(client.call(rehearsal_route + "/decisions", {
        "item": rehearsal_conflict["id"], "choice": "right", "revision": 0})["revision"] == 1,
        "只重演计划裁决未推进版本")
    attempt = client.call(rehearsal_route + "/rehearse", {"revision": 1})
    expect(attempt["status"] == "passed", "只重演计划未通过")
    expect(client.call(rehearsal_route + "/certificate", status=404)["code"] == "unpublished",
           "未封存计划居然颁发了凭证")
    expect(client.call(rehearsal_route + "/download", status=404)["code"] == "unpublished",
           "未封存计划居然允许下载")
    write(output / "rehearsal.json",
          json.dumps({"kind": "rehearsal-only", "plan": rehearsal_plan["id"], "revision": 1,
                      "attempt": attempt, "certificate": None, "downloadable": False},
                     ensure_ascii=True, indent=2).encode("ascii"))
    write(output / "rehearsal-attempts.json",
          json.dumps(client.call(rehearsal_route + "/attempts"), ensure_ascii=True, indent=2).encode("ascii"))

    # 计划 C：封存后归档。归档前后下载的字节和凭证必须完全不变，归档只停止写入。
    archived_plan = client.call("/api/plans", sources, 201)
    archived_route = f"/api/plans/{archived_plan['id']}"
    archived_conflict = next(item for item in archived_plan["items"] if not item["automatic"])
    expect(client.call(archived_route + "/decisions", {
        "item": archived_conflict["id"], "choice": "right", "revision": 0})["revision"] == 1,
        "归档演练计划裁决未推进版本")
    expect(client.call(archived_route + "/rehearse", {"revision": 1})["status"] == "passed",
           "归档演练计划未通过")
    archived_certificate = client.call(archived_route + "/seal", {"revision": 1})
    archived_bytes = client.call(archived_route + "/download")
    expect(client.call(archived_route + "/archive", {"revision": 1})["state"] == "archived",
           "归档未生效")
    expect(client.call(archived_route)["state"] == "archived", "计划状态未变为已归档")
    expect(client.call(archived_route + "/download") == archived_bytes, "归档改变了候选字节")
    expect(client.call(archived_route + "/certificate") == archived_certificate, "归档改变了凭证")
    expect(client.call(archived_route + "/rehearse", {"revision": 1}, 409)["code"] == "closed_plan",
           "归档后仍可重演")
    expect(client.call(archived_route + "/decisions", {
        "item": archived_conflict["id"], "choice": "base", "revision": 1}, 409)["code"] == "closed_plan",
        "归档后仍可裁决")
    write(output / "archived.json",
          json.dumps(archived_certificate, ensure_ascii=True, indent=2).encode("ascii"))
    write(output / "archived.sqlite", archived_bytes)

    # 演练素材：左右支互换、把候选冒充来源、篡改候选。
    write(mixed / "left-is-really-right.sqlite", local["umber"])
    write(mixed / "right-is-really-left.sqlite", local["cerulean"])
    write(mixed / "base-is-candidate.sqlite", sealed_bytes)
    tampered = bytearray(sealed_bytes)
    tampered[100] ^= 0xFF
    write(mixed / "tampered.sqlite", bytes(tampered))
    write(mixed / "broken.json", b'{"format":"pagefold-certificate-v1"}\n')
    shutil.copyfile(output / "sealed.json", mixed / "certificate.json")
    # 文件名全部正确、内容角色互换：提醒不要凭文件名判断角色。
    write(mixed / "role-swapped-quartz.sqlite", local["quartz"])
    write(mixed / "role-swapped-cerulean.sqlite", local["umber"])
    write(mixed / "role-swapped-umber.sqlite", local["cerulean"])
    manifest = {
        "sealed": {"plan": sealed_plan["id"], "certificate": "sealed.json", "candidate": "sealed.sqlite",
                   "certificate_sha256": hashlib.sha256((output / "sealed.json").read_bytes()).hexdigest(),
                   "candidate_sha256": hashlib.sha256(sealed_bytes).hexdigest(), "revision": 2},
        "archived": {"plan": archived_plan["id"], "certificate": "archived.json",
                     "candidate": "archived.sqlite", "revision": 1,
                     "candidate_sha256": hashlib.sha256(archived_bytes).hexdigest()},
        "rehearsal_only": {"plan": rehearsal_plan["id"], "revision": 1, "attempt": "rehearsal.json",
                           "candidate_digest": attempt["candidate_digest"],
                           "certificate": None, "candidate": None},
        "sources": {name: record["id"] for name, record in
                    (("quartz", base), ("cerulean", left), ("umber", right))},
        "state": "state.sqlite",
    }
    write(output / "manifest.json", json.dumps(manifest, ensure_ascii=True, indent=2).encode("ascii"))
    return manifest


def main():
    parser = argparse.ArgumentParser(description="生成 Pagefold 交接核对样例")
    parser.add_argument("directory", type=Path)
    args = parser.parse_args()
    args.directory.mkdir(parents=True, exist_ok=True)
    state = args.directory / "state.sqlite"
    if state.exists():
        state.unlink()
    with make_server(Store(state), 0) as server:
        thread = Thread(target=server.serve_forever)
        thread.start()
        try:
            manifest = run(Client(server.server_port), args.directory)
        finally:
            server.shutdown()
            thread.join()
    print(json.dumps(manifest, ensure_ascii=True, indent=2))


if __name__ == "__main__":
    sys.exit(main())
