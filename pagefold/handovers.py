"""交接凭证：组织密钥对封存凭证的确认与角色绑定。

交接凭证不重复封存凭证的内容，而是引用其规范字节的摘要，并把
base/left/right 三个来源角色连同候选摘要一起固定下来。即便两个计划
产生字节完全相同的候选数据库，角色字段不同也会得到不同的待签内容，
无法拿另一份计划的交接凭证（或封存凭证）互相代用。
"""

import json

from . import ed25519
from .codec import canonical, digest
from .errors import Problem
from .keyring import RING_FORMAT, verify_ring
from .plans import get_plan
from .releases import get_release

HANDOVER_FORMAT = "pagefold-handover-v1"
HANDOVER_KIND = "ed25519-handover"


def issue(store, keyring, plan_id):
    ring = keyring.public_ring()
    verify_ring(ring)  # 先自证密钥环再用于新签
    key_id, secret = keyring.active()
    with store.connect(write=True) as db:
        plan = get_plan(db, plan_id)
        if plan["state"] != "sealed" and plan["state"] != "archived":
            raise Problem("not_sealed", "只有已封存的计划才能颁发交接凭证。", 409)
        existing = db.execute("SELECT document FROM handovers WHERE plan_id=?", (plan_id,)).fetchone()
        if existing is not None:
            return json.loads(existing["document"])
        release = get_release(db, plan_id, body=True)
        certificate_text = release["certificate"]
        certificate_bytes = certificate_text.encode("ascii") if isinstance(certificate_text, str) \
            else certificate_text
        certificate = json.loads(certificate_text)
        # releases.seal 以 canonical(certificate) 落库，字节即规范编码。
        if canonical(certificate).encode("ascii") != certificate_bytes:
            raise Problem("certificate", "封存凭证的规范编码不一致。", 500)
        if digest(release["candidate"]) != certificate["candidate_digest"]:
            raise Problem("candidate_digest", "封存候选与凭证摘要不一致。", 409)
        anchor_id = sorted(ring["entries"], key=lambda item: item["sequence"])[0]["key_id"]
        document = {
            "format": HANDOVER_FORMAT,
            "kind": HANDOVER_KIND,
            "organization": ring["organization"],
            "plan": plan_id,
            "certificate_format": certificate["format"],
            "certificate_digest": digest(certificate_bytes),
            "ring_format": RING_FORMAT,
            "trust_anchor_key_id": anchor_id,
            "sources": {
                "base": certificate["sources"]["base"],
                "left": certificate["sources"]["left"],
                "right": certificate["sources"]["right"],
            },
            "schema_digest": certificate["schema"],
            "plan_digest": certificate["plan_digest"],
            "decision_digest": certificate["decision_digest"],
            "candidate_digest": certificate["candidate_digest"],
            "revision": certificate["revision"],
            "signing_key_id": key_id,
        }
        document["signature"] = ed25519.sign(secret, canonical(document).encode("ascii")).hex()
        db.execute("INSERT INTO handovers VALUES(?,?)", (plan_id, canonical(document)))
        return document


def get_handover(db, plan_id):
    row = db.execute("SELECT document FROM handovers WHERE plan_id=?", (plan_id,)).fetchone()
    if row is None:
        raise Problem("no_handover", "该计划还没有组织交接凭证。", 404)
    return json.loads(row["document"])


def _check_fields(document):
    required = {"format", "kind", "organization", "plan", "certificate_format",
                "certificate_digest", "ring_format", "trust_anchor_key_id", "sources",
                "schema_digest", "plan_digest", "decision_digest", "candidate_digest",
                "revision", "signing_key_id", "signature"}
    if not isinstance(document, dict) or set(document) != required:
        raise ValueError("交接凭证字段集合不正确")
    if document["format"] != HANDOVER_FORMAT or document["kind"] != HANDOVER_KIND:
        raise ValueError("交接凭证格式不被支持")
    if set(document["sources"]) != {"base", "left", "right"}:
        raise ValueError("来源角色字段不完整")
    hex64 = set("0123456789abcdef")
    for role, value in document["sources"].items():
        if not isinstance(value, str) or len(value) != 64 or set(value) - hex64:
            raise ValueError(f"来源 {role} 不是有效的镜像摘要")
    for name in ("trust_anchor_key_id", "signing_key_id"):
        value = document[name]
        if not isinstance(value, str) or len(value) != 64 or set(value) - hex64:
            raise ValueError(f"{name} 不是有效的 Ed25519 公钥摘要")
    signature = document["signature"]
    if not isinstance(signature, str) or len(signature) != 128 or set(signature) - hex64:
        raise ValueError("signature 不是有效的 Ed25519 十六进制签名")
    if not isinstance(document["revision"], int) or isinstance(document["revision"], bool):
        raise ValueError("revision 必须是整数")


def verify_handover(document, certificate, certificate_bytes=None, ring=None, candidate_bytes=None):
    """只使用公开材料离线验证交接凭证。

    document：交接凭证（dict）
    certificate：对应的封存凭证（dict）
    ring：组织公开密钥环（dict）；为 None 时只做密码学签名之外的交叉核对
    certificate_bytes：封存凭证的原始下载字节，用于核对 certificate_digest
    candidate_bytes：候选 SQLite 的原始下载字节，用于核对 candidate_digest
    返回核对结论 dict；任何一项不通过都会抛出 ValueError。
    """
    _check_fields(document)
    checks = {}
    signature = bytes.fromhex(document["signature"])
    unsigned = {key: value for key, value in document.items() if key != "signature"}
    if ring is not None:
        if document["ring_format"] != ring.get("format"):
            raise ValueError("密钥环格式与凭证引用不一致")
        verify_ring(ring)
        checks["key_ring"] = "信任锚与轮换链验证通过"
        anchor_id = sorted(ring["entries"], key=lambda item: item["sequence"])[0]["key_id"]
        if document["trust_anchor_key_id"] != anchor_id:
            raise ValueError("信任锚密钥与凭证引用的组织不一致")
        entry = next((item for item in ring["entries"]
                      if item["key_id"] == document["signing_key_id"]), None)
        if entry is None:
            raise ValueError("签名密钥不在组织公开环中")
        checks["signing_key"] = (
            f"密钥 {document['signing_key_id'][:16]}… 位于公开环，状态 {entry['status']}")
        ed25519.verify(bytes.fromhex(document["signing_key_id"]),
                       canonical(unsigned).encode("ascii"), signature)
        checks["signature"] = "Ed25519 签名验证通过（含已轮换的历史密钥）"
    else:
        raise ValueError("缺少公开密钥环，无法验证组织签名")

    if not isinstance(certificate, dict) or certificate.get("format") != document["certificate_format"]:
        raise ValueError("封存凭证格式与交接凭证引用不一致")
    checks["certificate_format"] = "封存凭证格式一致"

    if certificate_bytes is not None:
        actual = digest(certificate_bytes)
        if actual != document["certificate_digest"]:
            raise ValueError("封存凭证字节摘要与交接凭证绑定值不一致")
        if canonical(certificate).encode("ascii") != certificate_bytes:
            raise ValueError("封存凭证不是规范编码，摘要绑定不可复现")
        checks["certificate_digest"] = "封存凭证规范字节摘要一致"

    # 交接凭证的每个派生字段都必须能在封存凭证上复现，
    # 任何一项对不上都说明它引自另一份计划（即便候选摘要相同）。
    expected = {
        "sources": {role: certificate["sources"].get(role) for role in ("base", "left", "right")},
        "schema_digest": certificate.get("schema"),
        "plan_digest": certificate.get("plan_digest"),
        "decision_digest": certificate.get("decision_digest"),
        "candidate_digest": certificate.get("candidate_digest"),
        "revision": certificate.get("revision"),
    }
    for field, cert_value in expected.items():
        if cert_value != document[field]:
            raise ValueError(f"字段 {field} 与封存凭证不一致，凭证可能来自另一份计划")
    checks["role_binding"] = "base/left/right 角色位置与封存凭证精确绑定"
    checks["lineage"] = "模式、计划、裁决、候选摘要与封存凭证逐项一致"

    if candidate_bytes is not None:
        if digest(candidate_bytes) != document["candidate_digest"]:
            raise ValueError("下载的 SQLite 字节与候选摘要不一致")
        checks["candidate_bytes"] = "下载 SQLite 的 SHA-256 与凭证一致"

    return {"plan": document["plan"], "organization": document["organization"],
            "candidate_digest": document["candidate_digest"], "checks": checks}


def compare_lineage(first, second):
    """比较两份交接凭证：候选摘要相同但来源角色不同则判定不可代用。"""
    same_candidate = first["candidate_digest"] == second["candidate_digest"]
    same_sources = first["sources"] == second["sources"]
    return {
        "same_candidate_digest": same_candidate,
        "same_sources": same_sources,
        "interchangeable": same_candidate and same_sources,
        "conclusion": ("角色字段不同，即使候选字节相同也不能互相代用"
                       if same_candidate and not same_sources else
                       "来源角色一致" if same_sources else "来源与候选均不同"),
    }
