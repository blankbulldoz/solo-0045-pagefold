"""生成 Pagefold 离线验收包。

走真实 HTTP 工作流(上传镜像 → 创建计划 → 裁决冲突 → 重演 → 封存 → 下载),
把封存成果导出为 acceptance/pack/ 下的交接材料,并构造验收演示用的变体:

  certificate.json                 封存凭证(绑定来源、模式、计划、裁决与候选摘要)
  deliverable.sqlite               封存候选(成果数据库)
  adjudication.json                裁决记录(冲突条目 → 选择)
  inputs/{base,left,right}.sqlite  三份输入镜像
  deliverable-reorganized.sqlite   同一逻辑成果经 VACUUM 改页整理后的字节不同副本
  tampered/swap-inputs/            左右支输入调包(凭证不变)
  tampered/swap-sources/           左右支调包并同步改凭证 sources(计划摘要仍绑定原角色)
  tampered/edited-candidate/       改成果库后仅更新凭证候选摘要

用法:python3 -m acceptance.build_pack
"""
import hashlib
import json
import shutil
import sqlite3
import tempfile
from pathlib import Path
from threading import Thread
from urllib.request import Request, urlopen

from pagefold.demo import image
from pagefold.server import make_server
from pagefold.store import Store

ROOT = Path(__file__).resolve().parent.parent
PACK = ROOT / "acceptance" / "pack"


class Client:
    def __init__(self, port):
        self.origin = f"http://127.0.0.1:{port}"

    def call(self, route, data=None, status=200):
        headers = {}
        if isinstance(data, dict):
            data = json.dumps(data).encode()
            headers["Content-Type"] = "application/json"
        request = Request(self.origin + route, data=data, headers=headers)
        with urlopen(request, timeout=10) as result:
            body = result.read()
        if result.status != status:
            raise RuntimeError(f"{route}: {result.status} != {status}: {body[:300]}")
        return json.loads(body) if result.headers.get_content_type() == "application/json" else body

    def upload(self, name):
        request = Request(self.origin + "/api/images", data=image(name),
                          headers={"X-Image-Name": name, "Content-Type": "application/octet-stream"})
        with urlopen(request, timeout=10) as result:
            return json.loads(result.read())


def produce():
    """走真实 HTTP 工作流,返回计划、冲突项、凭证、候选、裁决记录与三份输入。"""
    with tempfile.TemporaryDirectory(prefix="pagefold-pack-") as root:
        server = make_server(Store(Path(root) / "state.sqlite"), 0)
        thread = Thread(target=server.serve_forever)
        thread.start()
        try:
            client = Client(server.server_port)
            base = client.upload("quartz")
            left = client.upload("cerulean")
            right = client.upload("umber")
            plan = client.call("/api/plans",
                               {"base": base["id"], "left": left["id"], "right": right["id"]}, 201)
            route = f"/api/plans/{plan['id']}"
            conflict = next(item for item in plan["items"] if not item["automatic"])
            client.call(route + "/decisions",
                        {"revision": 0, "item": conflict["id"], "choice": "right"})
            rehearsed = client.call(route + "/rehearse", {"revision": 1})
            if rehearsed["status"] != "passed":
                raise RuntimeError(f"重演未通过:{rehearsed}")
            client.call(route + "/seal", {"revision": 1})
            certificate = client.call(route + "/certificate")
            candidate = client.call(route + "/download")
        finally:
            server.shutdown()
            thread.join()
    adjudication = {"format": "pagefold-adjudication-v1", "plan": plan["id"], "revision": 1,
                    "decisions": {conflict["id"]: "right"}}
    inputs = {"base": image("quartz"), "left": image("cerulean"), "right": image("umber")}
    return plan, conflict, certificate, candidate, adjudication, inputs


def reorganized(candidate):
    """同一逻辑成果经 SQLite 整理(改页大小并 VACUUM)后的字节不同副本。"""
    db = sqlite3.connect(":memory:")
    try:
        db.deserialize(candidate)
        db.execute("PRAGMA page_size=1024")
        db.execute("VACUUM")
        return db.serialize()
    finally:
        db.close()


def edited(candidate):
    """篡改成果:改掉一行的内容,其余保持不变。"""
    db = sqlite3.connect(":memory:")
    try:
        db.deserialize(candidate)
        db.execute("UPDATE node SET note='篡改数据' WHERE bench='north' AND slot=1")
        db.commit()
        return db.serialize()
    finally:
        db.close()


def write_pack(directory, certificate, deliverable, adjudication, inputs):
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "inputs").mkdir(exist_ok=True)
    (directory / "certificate.json").write_text(
        json.dumps(certificate, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (directory / "deliverable.sqlite").write_bytes(deliverable)
    (directory / "adjudication.json").write_text(
        json.dumps(adjudication, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    for role, data in inputs.items():
        (directory / "inputs" / f"{role}.sqlite").write_bytes(data)


def main():
    plan, conflict, certificate, candidate, adjudication, inputs = produce()
    if PACK.exists():
        shutil.rmtree(PACK)
    write_pack(PACK, certificate, candidate, adjudication, inputs)

    variant = reorganized(candidate)
    if variant == candidate:
        raise RuntimeError("整理后的文件与原候选字节相同,需要换一种整理方式")
    (PACK / "deliverable-reorganized.sqlite").write_bytes(variant)

    swapped = dict(inputs, left=inputs["right"], right=inputs["left"])
    write_pack(PACK / "tampered" / "swap-inputs", certificate, candidate, adjudication, swapped)

    swapped_cert = dict(certificate, sources={**certificate["sources"],
                                              "left": certificate["sources"]["right"],
                                              "right": certificate["sources"]["left"]})
    write_pack(PACK / "tampered" / "swap-sources", swapped_cert, candidate, adjudication, swapped)

    tampered = edited(candidate)
    if tampered == candidate:
        raise RuntimeError("篡改后的成果与原候选字节相同,篡改未生效")
    edited_cert = dict(certificate, candidate_digest=hashlib.sha256(tampered).hexdigest(),
                       candidate_size=len(tampered))
    write_pack(PACK / "tampered" / "edited-candidate", edited_cert, tampered, adjudication, inputs)

    print(f"验收包:{PACK}")
    print(f"  计划 {plan['id']} 版本 1,冲突项 {conflict['id'][:16]}… 裁决为 right")
    print(f"  成果 candidate_digest={certificate['candidate_digest']}")
    print(f"  整理副本 digest={hashlib.sha256(variant).hexdigest()}(字节不同,逻辑等价)")


if __name__ == "__main__":
    main()
