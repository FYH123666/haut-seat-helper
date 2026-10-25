"""每个用户的学校凭据的**静态加密**（只用标准库，不新增核心依赖）。

## 为什么是「自己拼一套」而不是引入库

本机 venv 里**没有** ``cryptography``（已实测），而为一个「加密两个短字符串」
的需求引入一个需要编译的二进制依赖，会让部署机器多一类失败模式。标准库里
``hashlib.scrypt`` / ``hashlib.shake_256`` / ``hmac`` 三件套足够拼出**经认证的**
加密（AEAD 的等价物），因此这里自己实现，并把实现的每一条选择写清楚：

- **密钥**：``HAUT_AUTH_CRED_KEY``（环境变量，另接受简写 ``HAUT_CRED_KEY``）或
  ``data/auth/master.key``（自动生成的 32 字节随机数）。**密钥不在数据库里** ——
  只有 ``data/web.db`` 落进别人手里时，密文仍然是不可解的。
- **KDF**：``scrypt(master_key, salt=每条记录随机 16 字节)`` 派生出两个子密钥
  （加密密钥 / 认证密钥），因此同一条明文两次加密得到的密文也不同（语义安全）。
- **加密**：SHAKE256 密钥流（``enc_key || nonce || 计数器``）与明文异或。
  这不是"分组密码模式"，而是一次性密钥流 —— 同样的 ``(key, nonce)`` 绝不重复使用，
  因为 nonce 每条记录随机生成。
- **认证**：``HMAC-SHA256(mac_key, aad || nonce || 密文)``，**先验标签再解密**
  （encrypt-then-MAC）。这一段是必须的：没有它，攻击者可以翻转密文位而我们
  察觉不到，会把"被改坏的凭据"当成"登录失效"报出来。
- **AAD**：把 ``user_id`` 与字段名绑进认证标签，因此**密文不能跨用户、跨字段搬移**
  —— 把一个用户的 Cookie 密文塞进另一个用户的行里，校验必然失败。

## 边界（必须如实说明）

这不是经过审计的密码学实现，不提供前向保密，也不能抵抗拥有主密钥的攻击者。
它要防的是**数据库文件本身泄漏**（备份、误发、共享磁盘）时凭据被直接读走，
而不是防住已经拿到服务器上主密钥文件的人。这一点在 README 与交付说明里同样写明。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import os
import secrets
import threading
from pathlib import Path
from typing import Any

#: 密文前缀（版本号）。解密时严格比对：格式不认识就报错，绝不"猜着解"。
CIPHER_VERSION = "hsc1"

#: scrypt 参数，与 web.security 的口令哈希同级（约 16MB 内存），
#: 但用途不同：这里是对**主密钥**做 KDF，不是对用户口令。
SCRYPT_N = 2 ** 14
SCRYPT_R = 8
SCRYPT_P = 1
_DKLEN = 64          # 前 32 字节 = 加密密钥，后 32 字节 = 认证密钥
_SALT_BYTES = 16
_NONCE_BYTES = 16
_TAG_BYTES = 32
_MIN_MASTER_KEY_BYTES = 16


class CredentialCryptoError(RuntimeError):
    """解密失败（格式、主密钥或认证标签不匹配）。

    **消息里绝不包含任何明文、密文或主密钥片段** —— 它会被写进日志与页面提示。
    """


# ---------------------------------------------------------------- 主密钥


_MK_CACHE: dict[tuple[str, str], bytes] = {}

#: 主密钥的取用/生成必须串行。否则两个线程在"文件还不存在"时同时进入生成分支，
#: 会各自生成一份密钥并依次写进同一个文件：先缓存的线程拿 keyA 加密，
#: 后写入的文件里却是 keyB —— 这份凭据从此谁也解不开，且是那种最难查的间歇故障。
_MK_LOCK = threading.RLock()


def _decode_secret(raw: str) -> bytes | None:
    """把 ``HAUT_AUTH_CRED_KEY``（或简写 ``HAUT_CRED_KEY``）的取值解码成字节。

    接受三种写法（按可靠性排序，任何一种都不做"猜"）：
    64 位十六进制、base64、以及原始 UTF-8 文本（至少 16 字节）。
    三种都不成立时返回 ``None`` —— 由调用方**大声报错**，
    绝不退化成"用一个空密钥加密"。
    """
    text = (raw or "").strip()
    if not text:
        return None
    if len(text) == 64:
        try:
            return bytes.fromhex(text)
        except ValueError:
            pass
    try:
        padded = text + "=" * (-len(text) % 4)
        decoded = base64.urlsafe_b64decode(padded)
        if len(decoded) >= _MIN_MASTER_KEY_BYTES:
            return decoded
    except (ValueError, TypeError):
        pass
    raw_bytes = text.encode("utf-8")
    return raw_bytes if len(raw_bytes) >= _MIN_MASTER_KEY_BYTES else None


def master_key_path(settings) -> Path:
    return Path(str(getattr(settings, "auth_master_key_file", "data/auth/master.key")))


def master_key(settings, *, logger: logging.Logger | None = None) -> bytes:
    """取得主密钥（进程内按「环境变量值 + 文件路径」缓存）。

    优先级：``HAUT_AUTH_CRED_KEY``（别名 ``HAUT_CRED_KEY``）> ``auth_master_key_file``
    > 自动生成并写入该文件。

    自动生成的密钥**只写 32 字节随机数**（不是口令），文件权限尽力收紧到 0600
    （Windows 上 POSIX 权限位不生效，如实见 ``auth_store.save_snapshot`` 的说明）。
    环境变量设置了却解不出合法长度时**直接报错**：静默换用文件里的密钥
    会让"我明明配了密钥"变成一句假话，而且用户换密钥后解不开旧密文会极难排查。

    （环境变量名由 ``config.Settings.auth_cred_key`` 的 ``validation_alias`` 决定：
    文档里写的名字与代码真正读到的必须是同一个 —— 否则"配了主密钥"只是错觉。）
    """
    log = logger or logging.getLogger("haut_seat.auth")
    from haut_seat.services.io import secret_str

    env_raw = secret_str(getattr(settings, "auth_cred_key", None)) or ""
    path = master_key_path(settings)
    cache_key = (env_raw, str(path))
    with _MK_LOCK:
        return _master_key_locked(env_raw, path, cache_key, log)


def _master_key_locked(env_raw: str, path: Path, cache_key: tuple, log) -> bytes:
    cached = _MK_CACHE.get(cache_key)
    if cached is not None:
        return cached

    if env_raw:
        decoded = _decode_secret(env_raw)
        if decoded is None:
            raise CredentialCryptoError(
                "HAUT_AUTH_CRED_KEY（简写 HAUT_CRED_KEY）无法解析："
                "请使用 64 位十六进制、base64 或至少 16 字节的随机文本；"
                "本进程不会回退到其它密钥。"
            )
        _MK_CACHE[cache_key] = decoded
        return decoded

    try:
        existing = path.read_bytes()
    except FileNotFoundError:
        existing = b""
    except OSError as exc:
        raise CredentialCryptoError(
            f"主密钥文件不可读（{type(exc).__name__}）：{path}"
        ) from exc

    if len(existing) >= _MIN_MASTER_KEY_BYTES:
        _MK_CACHE[cache_key] = bytes(existing)
        return _MK_CACHE[cache_key]

    generated = secrets.token_bytes(32)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(generated)
        try:
            os.chmod(path, 0o600)
        except OSError:  # Windows 上无实际效果，但不该因此失败
            pass
    except OSError as exc:
        raise CredentialCryptoError(
            f"无法创建主密钥文件（{type(exc).__name__}）：{path}。"
            "请手动生成一个 32 字节随机文件，或设置 HAUT_AUTH_CRED_KEY。"
        ) from exc
    log.info("已生成新的凭据加密主密钥文件：%s（内容不记录）", path)
    _MK_CACHE[cache_key] = generated
    return generated


def reset_master_key_cache() -> None:
    """清空主密钥缓存（测试隔离用；换密钥后也由它生效）。"""
    with _MK_LOCK:
        _MK_CACHE.clear()


# ---------------------------------------------------------------- 加解密


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _subkeys(mk: bytes, salt: bytes) -> tuple[bytes, bytes]:
    dk = hashlib.scrypt(mk, salt=salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P, dklen=_DKLEN)
    return dk[:32], dk[32:]


def _keystream(enc_key: bytes, nonce: bytes, length: int) -> bytes:
    """SHAKE256 密钥流：``enc_key || nonce || 计数器`` 逐块展开。

    ``(enc_key, nonce)`` 组合在全局唯一（nonce 每条记录随机），
    因此这段密钥流不会被重复使用 —— 这是异或加密唯一不可让步的前提。
    """
    out = bytearray()
    counter = 0
    while len(out) < length:
        out += hashlib.shake_256(enc_key + nonce + counter.to_bytes(4, "big")).digest(64)
        counter += 1
    return bytes(out[:length])


def _tag(mac_key: bytes, aad: bytes, nonce: bytes, ciphertext: bytes) -> bytes:
    return hmac.new(mac_key, aad + b"\x00" + nonce + ciphertext, hashlib.sha256).digest()


def encrypt(master: bytes, plaintext: str, *, aad: str = "") -> str:
    """加密一个字符串，返回自包含的密文串（含盐、nonce 与认证标签）。

    密文串形如 ``hsc1$<b64 盐>$<b64 nonce>$<b64 密文>$<b64 标签>``，
    可以安全入库与备份 —— 没有主密钥就解不开。
    """
    if not isinstance(plaintext, str) or not plaintext:
        raise ValueError("拒绝加密空值")
    if not isinstance(master, (bytes, bytearray)) or len(master) < _MIN_MASTER_KEY_BYTES:
        raise CredentialCryptoError("主密钥长度不足")
    salt = secrets.token_bytes(_SALT_BYTES)
    nonce = secrets.token_bytes(_NONCE_BYTES)
    enc_key, mac_key = _subkeys(master, salt)
    body = plaintext.encode("utf-8")
    ciphertext = bytes(a ^ b for a, b in zip(body, _keystream(enc_key, nonce, len(body))))
    tag = _tag(mac_key, aad.encode("utf-8"), nonce, ciphertext)
    return "$".join([CIPHER_VERSION, _b64(salt), _b64(nonce), _b64(ciphertext), _b64(tag)])


def decrypt(master: bytes, token: str, *, aad: str = "") -> str:
    """解密；任何问题都抛 :class:`CredentialCryptoError`（消息不含值）。

    **先验认证标签再解密**。顺序不能颠倒：密文被改坏而标签恰好不匹配时，
    我们必须在"解出乱码"之前就拒绝，否则乱码会被当成一个合法但无效的凭据，
    把"数据被篡改"误报成"登录失效"。
    """
    if not isinstance(token, str) or not token:
        raise CredentialCryptoError("密文为空")
    parts = token.split("$")
    if len(parts) != 5 or parts[0] != CIPHER_VERSION:
        raise CredentialCryptoError("密文格式不受支持")
    try:
        salt, nonce, ciphertext, tag = (_unb64(p) for p in parts[1:])
    except (ValueError, TypeError) as exc:
        raise CredentialCryptoError("密文编码不合法") from exc
    if len(salt) != _SALT_BYTES or len(nonce) != _NONCE_BYTES or len(tag) != _TAG_BYTES:
        raise CredentialCryptoError("密文结构不合法")
    enc_key, mac_key = _subkeys(master, salt)
    expected = _tag(mac_key, aad.encode("utf-8"), nonce, ciphertext)
    if not hmac.compare_digest(expected, tag):
        # 密钥换了、密文被改过、或密文被搬到别的用户/字段上（AAD 不匹配）。
        raise CredentialCryptoError("密文校验失败（主密钥不匹配或内容已被修改）")
    body = bytes(a ^ b for a, b in zip(ciphertext, _keystream(enc_key, nonce, len(ciphertext))))
    try:
        return body.decode("utf-8")
    except UnicodeDecodeError as exc:  # pragma: no cover - 标签已通过，理论上不可达
        raise CredentialCryptoError("密文内容无法解码") from exc


def user_aad(user_id: Any, field: str) -> str:
    """绑定「用户 + 字段」的附加认证数据。密文因此不能跨用户/跨字段搬移。"""
    return f"user_credential:{int(user_id)}:{field}"