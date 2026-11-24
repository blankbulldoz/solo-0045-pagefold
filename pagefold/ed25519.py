"""纯标准库实现的 Ed25519（RFC 8032）。

项目环境没有第三方密码学库，这里按 RFC 8032 实现签名与验证，
并在 selftest() 中对照 RFC 第 7.1 节的公开测试向量自检。
点运算使用扩展扭曲爱德华坐标，只依赖 hashlib.sha512 与整数模运算。
"""

import hashlib

p = 2**255 - 19
d = (-121665 * pow(121666, p - 2, p)) % p
q = 2**252 + 27742317777372353535851937790883648493
IDENT = (0, 1, 1, 0)  # 扩展坐标 (X, Y, Z, T) 中的恒等元


def _inv(x):
    return pow(x, p - 2, p)


def _xrecover(y):
    xx = (y * y - 1) * _inv(d * y * y + 1)
    x = pow(xx, (p + 3) // 8, p)
    if (x * x - xx) % p != 0:
        x = (x * pow(2, (p - 1) // 4, p)) % p
    if x & 1:
        x = p - x
    return x


def _b():
    y = 4 * _inv(5) % p
    return (_xrecover(y), y, 1, (_xrecover(y) * y) % p)


B = _b()


def _edwards_add(one, two):
    x1, y1, z1, t1 = one
    x2, y2, z2, t2 = two
    a = (y1 - x1) * (y2 - x2) % p
    b = (y1 + x1) * (y2 + x2) % p
    c = t1 * 2 * d * t2 % p
    dd = z1 * 2 * z2 % p
    e = b - a
    f = dd - c
    g = dd + c
    h = b + a
    return (e * f, g * h, f * g, e * h)


def _scalarmult(point, scalar):
    result = IDENT
    addend = point
    while scalar:
        if scalar & 1:
            result = _edwards_add(result, addend)
        addend = _edwards_add(addend, addend)
        scalar >>= 1
    return result


def _encodeint(y):
    return y.to_bytes(32, "little")


def _decodeint(data):
    return int.from_bytes(data, "little")


def _encodepoint(point):
    x, y, z, _ = point
    zi = _inv(z)
    x = x * zi % p
    y = y * zi % p
    return _encodeint(y | ((x & 1) << 255))


def _point_equal(one, two):
    # 扩展坐标 (X:Y:Z:T) 有不同的 Z 表示，按仿射坐标比较。
    x1, y1, z1, _ = one
    x2, y2, z2, _ = two
    return x1 * z2 % p == x2 * z1 % p and y1 * z2 % p == y2 * z1 % p


def _decodepoint(data):
    y = _decodeint(data) & ((1 << 255) - 1)
    x = _xrecover(y)
    if (x & 1) != (data[31] >> 7):
        x = p - x
    point = (x, y, 1, x * y % p)
    if _encodepoint(point) != data:
        raise ValueError("点不在曲线上")
    return point


def _hkey(secret):
    return hashlib.sha512(secret).digest()


def _clamp(prefix):
    h = bytearray(prefix)
    h[0] &= 248
    h[31] &= 127
    h[31] |= 64
    return bytes(h)


def public_key(secret):
    """由 32 字节种子推导 32 字节公钥。"""
    h = _hkey(secret)
    a = _decodeint(_clamp(h[:32]))
    return _encodepoint(_scalarmult(B, a))


def sign(secret, message):
    """用 32 字节种子对 bytes 消息签名，返回 64 字节签名。"""
    h = _hkey(secret)
    a = _decodeint(_clamp(h[:32]))
    prefix = h[32:]
    r = _decodeint(hashlib.sha512(prefix + message).digest()) % q
    r_point = _scalarmult(B, r)
    r_bytes = _encodepoint(r_point)
    a_bytes = _encodepoint(_scalarmult(B, a))
    k = _decodeint(hashlib.sha512(r_bytes + a_bytes + message).digest()) % q
    s = (r + k * a) % q
    return r_bytes + _encodeint(s)


def verify(public, message, signature):
    """验证失败时抛出 ValueError，成功返回 None。"""
    if len(public) != 32 or len(signature) != 64:
        raise ValueError("公钥或签名长度不正确")
    r_bytes = signature[:32]
    r_point = _decodepoint(r_bytes)
    a_point = _decodepoint(public)
    s = _decodeint(signature[32:])
    if s >= q:
        raise ValueError("签名标量越界")
    k = _decodeint(hashlib.sha512(r_bytes + public + message).digest()) % q
    if not _point_equal(_scalarmult(B, s), _edwards_add(r_point, _scalarmult(a_point, k))):
        raise ValueError("签名不匹配")


# RFC 8032 第 7.1 节 TEST 1 与 TEST 2，公钥/签名为确定值。
_VECTORS = (
    ("9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60",
     "", "d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a",
     "e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e06522490155"
     "5fb8821590a33bacc61e39701cf9b46bd25bf5f0595bbe24655141438e7a100b"),
    ("4ccd089b28ff96da9db6c346ec114e0f5b8a319f35aba624da8cf6ed4fb8a6fb",
     "72", "3d4017c3e843895a92b70aa74d1b7ebc9c982ccf2ec4968cc0cd55f12af4660c",
     "92a009a9f0d4cab8720e820b5f642540a2b27b5416503f8fb3762223ebdb69da"
     "085ac1e43e15996e458f3613d0f11d8c387b2eaeb4302aeeb00d291612bb0c00"),
)


def selftest():
    for secret_hex, message_hex, expected_public, expected_signature in _VECTORS:
        secret = bytes.fromhex(secret_hex)
        message = bytes.fromhex(message_hex)
        public = public_key(secret)
        signature = sign(secret, message)
        if public.hex() != expected_public or signature.hex() != expected_signature:
            raise AssertionError("Ed25519 测试向量不匹配")
        verify(public, message, signature)


if __name__ == "__main__":
    selftest()
    print("Ed25519 RFC 8032 测试向量全部通过")
