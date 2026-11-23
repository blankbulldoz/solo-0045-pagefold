import base64
import hashlib
import json
import math

from .errors import Problem


def canonical(value):
    return json.dumps(value, ensure_ascii=True, separators=(",", ":"), sort_keys=True)


def digest(data):
    return hashlib.sha256(data).hexdigest()


def fingerprint(value):
    return digest(canonical(value).encode("ascii"))


def atom(value):
    if value is None:
        return ["n"]
    if isinstance(value, int):
        return ["i", str(value)]
    if isinstance(value, float) and math.isfinite(value):
        return ["r", value.hex()]
    if isinstance(value, str):
        return ["t", value]
    if isinstance(value, bytes):
        return ["b", base64.b64encode(value).decode("ascii")]
    raise Problem("unsupported_value", "镜像包含无法保真编码的值。")


def unpack(value):
    if value[0] == "n":
        return None
    decoders = {"i": int, "r": float.fromhex, "t": str, "b": base64.b64decode}
    return decoders[value[0]](value[1])
