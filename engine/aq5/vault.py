"""本机密钥保险箱：API Key、推送 token、备份密码等只存在本机，永远不进 Git、不进公开发布。

Windows：用系统 DPAPI 加密（绑定当前 Windows 用户，拷走文件也解不开）。
其他系统：文件权限 600 + 本机随机密钥加密（尽力而为）。
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets as _secrets
import sys
import threading

from .config import DATA

VAULT = DATA / "vault.bin"
_lock = threading.Lock()


# ----------------------------------------------------------------------------- 通用对称加密（标准库实现）
def _keystream(key: bytes, nonce: bytes, n: int) -> bytes:
    out, ctr = bytearray(), 0
    while len(out) < n:
        out += hmac.new(key, nonce + ctr.to_bytes(8, "big"), hashlib.sha256).digest()
        ctr += 1
    return bytes(out[:n])


def seal(data: bytes, password: str | bytes) -> bytes:
    """口令加密：scrypt 派生密钥 + HMAC-SHA256 计数器流密码 + HMAC 完整性校验（先加密后认证）。"""
    pw = password.encode() if isinstance(password, str) else password
    salt, nonce = os.urandom(16), os.urandom(16)
    k = hashlib.scrypt(pw, salt=salt, n=2 ** 14, r=8, p=1, dklen=64)
    ek, mk = k[:32], k[32:]
    ct = bytes(a ^ b for a, b in zip(data, _keystream(ek, nonce, len(data))))
    tag = hmac.new(mk, b"AQ5v1" + salt + nonce + ct, hashlib.sha256).digest()
    return b"AQ5v1" + salt + nonce + tag + ct


def unseal(blob: bytes, password: str | bytes) -> bytes:
    if blob[:5] != b"AQ5v1":
        raise ValueError("不是 A量化 加密文件")
    pw = password.encode() if isinstance(password, str) else password
    salt, nonce, tag, ct = blob[5:21], blob[21:37], blob[37:69], blob[69:]
    k = hashlib.scrypt(pw, salt=salt, n=2 ** 14, r=8, p=1, dklen=64)
    ek, mk = k[:32], k[32:]
    if not hmac.compare_digest(tag, hmac.new(mk, b"AQ5v1" + salt + nonce + ct, hashlib.sha256).digest()):
        raise ValueError("密码错误或文件被改动")
    return bytes(a ^ b for a, b in zip(ct, _keystream(ek, nonce, len(ct))))


WEB_ITER = 200_000


def seal_web(data: bytes, password: str) -> bytes:
    """给浏览器解密用：PBKDF2-SHA256 派生密钥（浏览器 WebCrypto 原生支持）+ 同样的流密码与 HMAC 校验。"""
    salt, nonce = os.urandom(16), os.urandom(16)
    k = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, WEB_ITER, dklen=64)
    ek, mk = k[:32], k[32:]
    ct = bytes(a ^ b for a, b in zip(data, _keystream(ek, nonce, len(data))))
    tag = hmac.new(mk, b"AQ5w1" + salt + nonce + ct, hashlib.sha256).digest()
    return b"AQ5w1" + salt + nonce + tag + ct


def unseal_web(blob: bytes, password: str) -> bytes:
    salt, nonce, tag, ct = blob[5:21], blob[21:37], blob[37:69], blob[69:]
    k = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, WEB_ITER, dklen=64)
    if not hmac.compare_digest(tag, hmac.new(k[32:], b"AQ5w1" + salt + nonce + ct, hashlib.sha256).digest()):
        raise ValueError("密码错误")
    return bytes(a ^ b for a, b in zip(ct, _keystream(k[:32], nonce, len(ct))))


# ----------------------------------------------------------------------------- Windows DPAPI
def _dpapi(data: bytes, protect: bool) -> bytes:
    import ctypes
    from ctypes import wintypes

    class BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    buf = ctypes.create_string_buffer(data, len(data))
    inp = BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    out = BLOB()
    crypt = ctypes.windll.crypt32
    fn = crypt.CryptProtectData if protect else crypt.CryptUnprotectData
    ok = fn(ctypes.byref(inp), None if not protect else "aq5", None, None, None, 0x1, ctypes.byref(out))
    if not ok:
        raise OSError("DPAPI 失败")
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(out.pbData)


def _machine_key() -> bytes:
    kf = DATA / ".vault_key"
    if not kf.exists():
        kf.write_bytes(_secrets.token_bytes(32))
        try:
            os.chmod(kf, 0o600)
        except OSError:
            pass
    return kf.read_bytes()


def _protect(b: bytes) -> bytes:
    if sys.platform == "win32":
        return b"W" + _dpapi(b, True)
    return b"F" + seal(b, _machine_key())


def _unprotect(b: bytes) -> bytes:
    if b[:1] == b"W":
        return _dpapi(b[1:], False)
    return unseal(b[1:], _machine_key())


# ----------------------------------------------------------------------------- 读写
def _load() -> dict:
    if not VAULT.exists():
        return {}
    try:
        return json.loads(_unprotect(VAULT.read_bytes()))
    except (OSError, ValueError):
        return {}


def get(name: str, default: str = "") -> str:
    with _lock:
        return _load().get(name, default)


def set_many(values: dict) -> None:
    with _lock:
        d = _load()
        for k, v in values.items():
            if v is None or v == "":
                d.pop(k, None)
            else:
                d[k] = str(v)
        tmp = VAULT.with_suffix(".tmp")
        tmp.write_bytes(_protect(json.dumps(d).encode()))
        os.replace(tmp, VAULT)
        try:
            os.chmod(VAULT, 0o600)
        except OSError:
            pass


def status() -> dict:
    """只返回“是否已设置”和掩码，不返回明文。"""
    d = _load()
    return {k: (v[:3] + "…" + v[-3:] if len(v) > 10 else "已设置") for k, v in d.items()}

