"""组织签名密钥环：信任锚、轮换链与 PEM 公开材料。

- 公开材料（ring.json + *.pub.pem）可以交给收件方离线验证。
- 私钥材料只保存在服务端密钥目录的 secrets.json（权限 0600），
  export 等公开导出路径不会带出任何私钥。
- 每次轮换生成一条由旧私钥签名的轮换记录；旧密钥随即停用，
  历史交接凭证仍可用公开环中的旧公钥验证，但不能再用旧密钥新签。
"""

import base64
import json
import os
import time
from pathlib import Path

from . import ed25519
from .codec import canonical
from .errors import Problem

RING_FILE = "ring.json"
SECRETS_FILE = "secrets.json"
RING_FORMAT = "pagefold-keyring-v1"
ROTATION_FORMAT = "pagefold-key-rotation-v1"
ANCHOR_KIND = "ed25519-self-signed-anchor"
ROTATION_KIND = "ed25519-rotation"


def _spki_der(public):
    # SubjectPublicKeyInfo: Ed25519 算法标识符 + 32 字节 BIT STRING 公钥。
    return bytes.fromhex(
        "302a300506032b6570032100") + public


def _pkcs8_der(secret):
    # OneAsymmetricKey（PKCS#8 v2）封装的 Ed25519 私钥。
    return bytes.fromhex(
        "302e020100300506032b657004220420") + secret


def _pem(kind, der):
    body = base64.encodebytes(der).decode("ascii").strip()
    return f"-----BEGIN {kind}-----\n{body}\n-----END {kind}-----\n"


def public_pem(public):
    return _pem("PUBLIC KEY", _spki_der(public))


def secret_pem(secret):
    return _pem("PRIVATE KEY", _pkcs8_der(secret))


class Keyring:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.ring_path = self.directory / RING_FILE
        self.secret_path = self.directory / SECRETS_FILE

    def exists(self):
        return self.ring_path.exists() and self.secret_path.exists()

    def _read(self):
        ring = json.loads(self.ring_path.read_text(encoding="utf-8"))
        secrets = json.loads(self.secret_path.read_text(encoding="utf-8"))
        if ring.get("format") != RING_FORMAT or not ring.get("entries"):
            raise Problem("keyring", "密钥环材料格式不正确。", 500)
        return ring, secrets

    def public_ring(self):
        ring, _ = self._read()
        return ring

    def active(self):
        ring, secrets = self._read()
        key_id = ring.get("active")
        entry = next((item for item in ring["entries"] if item["key_id"] == key_id), None)
        secret = secrets.get(key_id)
        if entry is None or secret is None or entry.get("status") != "active":
            raise Problem("signing_key", "组织没有可用于新签的活动密钥。", 503)
        raw = bytes.fromhex(secret)
        if ed25519.public_key(raw).hex() != key_id:
            raise Problem("signing_key", "私钥与公开环中的公钥不匹配。", 500)
        return key_id, raw

    def initialize(self, organization, seed=None):
        if self.exists():
            raise Problem("keyring", "密钥环已经存在，不能重复初始化。", 409)
        self.directory.mkdir(parents=True, exist_ok=True)
        secret = seed or os.urandom(32)
        if len(secret) != 32:
            raise ValueError("种子必须是 32 字节")
        key_id = ed25519.public_key(secret).hex()
        timestamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        anchor = {"format": ROTATION_FORMAT, "kind": ANCHOR_KIND, "organization": organization,
                  "key_id": key_id, "issued_at": timestamp, "sequence": 0,
                  "previous_key_id": None, "note": "组织信任锚（演练密钥）"}
        anchor["signature"] = ed25519.sign(secret, canonical(anchor).encode("ascii")).hex()
        ring = {"format": RING_FORMAT, "organization": organization, "active": key_id,
                "entries": [{"key_id": key_id, "status": "active", "sequence": 0,
                             "issued_at": timestamp, "rotation": anchor}]}
        secrets = {key_id: secret.hex()}
        self._write(ring, secrets)
        return ring

    def rotate(self, note="组织签名密钥轮换"):
        ring, secrets = self._read()
        old_id, old_secret = self.active()
        old_entry = next(item for item in ring["entries"] if item["key_id"] == old_id)
        new_secret = os.urandom(32)
        new_id = ed25519.public_key(new_secret).hex()
        if any(item["key_id"] == new_id for item in ring["entries"]):
            raise Problem("keyring", "新密钥与既有密钥碰撞。", 500)
        timestamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        sequence = old_entry["sequence"] + 1
        record = {"format": ROTATION_FORMAT, "kind": ROTATION_KIND,
                  "organization": ring["organization"], "key_id": new_id,
                  "issued_at": timestamp, "sequence": sequence,
                  "previous_key_id": old_id, "note": note}
        record["signature"] = ed25519.sign(old_secret, canonical(record).encode("ascii")).hex()
        old_entry["status"] = "retired"
        old_entry["retired_at"] = timestamp
        ring["entries"].append({"key_id": new_id, "status": "active", "sequence": sequence,
                                "issued_at": timestamp, "rotation": record})
        ring["active"] = new_id
        secrets[new_id] = new_secret.hex()
        self._write(ring, secrets)
        return ring

    def _write(self, ring, secrets):
        self.directory.mkdir(parents=True, exist_ok=True)
        tmp_ring = self.ring_path.with_suffix(".json.tmp")
        tmp_secret = self.secret_path.with_suffix(".json.tmp")
        tmp_ring.write_text(json.dumps(ring, ensure_ascii=True, indent=2), encoding="utf-8")
        tmp_secret.write_text(json.dumps(secrets, ensure_ascii=True, indent=2), encoding="utf-8")
        os.chmod(tmp_secret, 0o600)
        os.replace(tmp_ring, self.ring_path)
        os.replace(tmp_secret, self.secret_path)
        for entry in ring["entries"]:
            pem_path = self.directory / f"{entry['key_id']}.pub.pem"
            if not pem_path.exists():
                pem_path.write_text(public_pem(bytes.fromhex(entry["key_id"])), encoding="ascii")
        if os.path.exists(self.secret_path):
            os.chmod(self.secret_path, 0o600)


def verify_ring(ring):
    """只依赖公开材料验证信任锚与整条轮换链，返回活动密钥 id。

    纯函数：不修改传入的 ring 结构。
    """
    if ring.get("format") != RING_FORMAT or not isinstance(ring.get("entries"), list):
        raise ValueError("密钥环格式不正确")
    entries = sorted(ring["entries"], key=lambda item: item["sequence"])
    active_ids = {item["key_id"] for item in entries if item.get("status") == "active"}
    if active_ids != {ring.get("active")}:
        raise ValueError("活动密钥与条目标记不一致")
    previous_id = None
    for entry in entries:
        record = entry.get("rotation")
        key_id = entry["key_id"]
        if not record or record.get("key_id") != key_id or "signature" not in record:
            raise ValueError(f"密钥 {key_id[:12]} 缺少轮换记录")
        try:
            signature = bytes.fromhex(record["signature"])
        except ValueError as exc:
            raise ValueError("轮换记录签名编码不正确") from exc
        unsigned = {key: value for key, value in record.items() if key != "signature"}
        message = canonical(unsigned).encode("ascii")
        if record["kind"] == ANCHOR_KIND:
            if record["sequence"] != 0 or record["previous_key_id"] is not None:
                raise ValueError("信任锚记录字段不正确")
            ed25519.verify(bytes.fromhex(key_id), message, signature)
        elif record["kind"] == ROTATION_KIND:
            if record["previous_key_id"] != previous_id:
                raise ValueError("轮换链断链")
            ed25519.verify(bytes.fromhex(previous_id), message, signature)
        else:
            raise ValueError("未知的轮换记录类型")
        previous_id = key_id
    if ring["active"] != entries[-1]["key_id"]:
        raise ValueError("活动密钥不是轮换链末端")
    return ring["active"]
