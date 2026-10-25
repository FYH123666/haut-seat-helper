"""每用户学校凭据的静态加密（``services/credential_crypto.py``）。

这是一个"自己拼一套 AEAD"的模块（本机没有 ``cryptography``），因此它必须被**逐条**
钉住：哪些情况下能解、哪些情况下必须拒绝、拒绝时错误消息里有没有值。
用例全部离线、全部只用标准库。

覆盖：
- 往返正确；
- **AAD 绑定了用户与字段**：把一个用户的密文塞进另一个用户/另一个字段 ⇒ 拒绝；
- **先验标签再解密**：密文被翻转一位 ⇒ 拒绝，且不会"解出乱码当成有效凭据"；
- 换主密钥 ⇒ 拒绝；格式不认识 ⇒ 拒绝（不"猜着解"）；空值拒绝；
- ``HAUT_AUTH_CRED_KEY``（别名 ``HAUT_CRED_KEY``）的三种合法写法与被拒绝的写法；
- 主密钥文件的生成、复用与并发（同一把密钥，不会互相覆盖）；
- ``user_credentials`` 把底层错误统一成"请重新绑定"，且消息里没有任何值。
"""

from __future__ import annotations

import base64
import logging

import pytest

from haut_seat.services import credential_crypto as cc
from haut_seat.services import user_credentials as uc

SENTINEL = "SENTINEL-credential-value-9f3a"
COOKIE_VALUE = "Admin-Token=" + "eyJhbGciOiJIUzUxMiJ9." + SENTINEL + ".SIG"
AUTH_VALUE = "Bearer " + "eyJhbGciOiJIUzUxMiJ9." + SENTINEL + ".SIG"

MASTER_A = bytes(range(32))
MASTER_B = bytes(range(32, 64))


@pytest.fixture
def key_file(tmp_path, make_settings):
    """把主密钥文件指到 tmp；返回 ``(settings, path)``。"""
    path = tmp_path / "auth" / "master.key"
    settings = make_settings(auth_master_key_file=str(path))
    return settings, path


# ================================================================== 1. 往返


def test_round_trip_with_aad():
    token = cc.encrypt(MASTER_A, SENTINEL, aad=cc.user_aad(7, "cookie"))
    assert cc.decrypt(MASTER_A, token, aad=cc.user_aad(7, "cookie")) == SENTINEL


def test_ciphertext_is_self_contained_and_versioned():
    token = cc.encrypt(MASTER_A, SENTINEL)
    parts = token.split("$")
    assert len(parts) == 5
    assert parts[0] == cc.CIPHER_VERSION == "hsc1"
    assert SENTINEL not in token, "密文串里不该出现明文"


def test_two_encryptions_of_the_same_value_differ():
    """盐与 nonce 每条随机 ⇒ 同一条明文两次加密的密文不同（语义安全）。"""
    one = cc.encrypt(MASTER_A, SENTINEL)
    two = cc.encrypt(MASTER_A, SENTINEL)
    assert one != two
    assert cc.decrypt(MASTER_A, one) == cc.decrypt(MASTER_A, two) == SENTINEL


def test_unicode_round_trip():
    text = "用户名：张三 / 备注 emoji ✅"
    assert cc.decrypt(MASTER_A, cc.encrypt(MASTER_A, text)) == text


# ================================================================== 2. AAD 绑定


def test_ciphertext_cannot_be_moved_to_another_user():
    """把一个用户的 Cookie 密文塞进另一个用户的行里 ⇒ 解不开。

    这是"多账号隔离"最后一道结构性保证：即使有人拿到了库文件的写权限，
    也不能把 A 的凭据搬到 B 名下而不被发现。
    """
    token = cc.encrypt(MASTER_A, COOKIE_VALUE, aad=cc.user_aad(1, "cookie"))
    with pytest.raises(cc.CredentialCryptoError) as excinfo:
        cc.decrypt(MASTER_A, token, aad=cc.user_aad(2, "cookie"))
    assert SENTINEL not in str(excinfo.value)
    assert COOKIE_VALUE not in str(excinfo.value)


def test_ciphertext_cannot_be_moved_to_another_field():
    """Cookie 的密文不能当成 Authorization 用（字段名也绑进标签）。"""
    token = cc.encrypt(MASTER_A, COOKIE_VALUE, aad=cc.user_aad(1, "cookie"))
    with pytest.raises(cc.CredentialCryptoError):
        cc.decrypt(MASTER_A, token, aad=cc.user_aad(1, "authorization"))


def test_aad_is_required_consistently():
    """加密时带了 AAD、解密时没带（或反之）同样拒绝 —— 不允许"空 AAD 兜底"。"""
    token = cc.encrypt(MASTER_A, SENTINEL, aad=cc.user_aad(1, "cookie"))
    with pytest.raises(cc.CredentialCryptoError):
        cc.decrypt(MASTER_A, token)
    plain = cc.encrypt(MASTER_A, SENTINEL)
    with pytest.raises(cc.CredentialCryptoError):
        cc.decrypt(MASTER_A, plain, aad=cc.user_aad(1, "cookie"))


# ================================================================== 3. 篡改与换密钥


def _flip(token: str, index: int = 3) -> str:
    """翻转密文里某一段的一个字符（其余结构保持不变）。"""
    parts = token.split("$")
    payload = parts[index]
    flipped = ("A" if payload[0] != "A" else "B") + payload[1:]
    parts[index] = flipped
    return "$".join(parts)


def test_tampered_ciphertext_is_rejected():
    """密文被改一位 ⇒ 拒绝。没有这一步，被改坏的凭据会被报成"登录失效"。"""
    token = cc.encrypt(MASTER_A, SENTINEL)
    with pytest.raises(cc.CredentialCryptoError) as excinfo:
        cc.decrypt(MASTER_A, _flip(token))
    assert "校验失败" in str(excinfo.value)


def test_tag_is_verified_before_decrypting(monkeypatch):
    """**先验标签再解密**：标签不匹配时，密钥流根本不该被算出来。

    这条顺序是有意的（encrypt-then-MAC）：若先解密，乱码会流出去被当成合法凭据。
    这里用"密钥流函数一旦被调用就失败"来钉住顺序。
    """
    token = cc.encrypt(MASTER_A, SENTINEL)

    def boom(*args, **kwargs):  # pragma: no cover - 只可能在顺序被改坏时触发
        raise AssertionError("标签没通过就解了密")

    monkeypatch.setattr(cc, "_keystream", boom)
    with pytest.raises(cc.CredentialCryptoError):
        cc.decrypt(MASTER_A, _flip(token, 3))


def test_tampered_tag_is_rejected():
    token = cc.encrypt(MASTER_A, SENTINEL)
    with pytest.raises(cc.CredentialCryptoError):
        cc.decrypt(MASTER_A, _flip(token, 4))


def test_wrong_master_key_is_rejected():
    token = cc.encrypt(MASTER_A, SENTINEL)
    with pytest.raises(cc.CredentialCryptoError) as excinfo:
        cc.decrypt(MASTER_B, token)
    assert SENTINEL not in str(excinfo.value)


# ================================================================== 4. 格式与入参


@pytest.mark.parametrize(
    "token",
    [
        "",
        "not-a-cipher",
        "hsc2$aa$bb$cc$dd",          # 版本不认识
        "hsc1$aa$bb$cc",             # 段数不对
        "hsc1$a$b$c$d",              # 结构长度不对（盐/nonce/标签）
        "hsc1$!!!$!!!$!!!$!!!",      # 编码不合法
    ],
)
def test_unsupported_or_malformed_ciphertexts_are_rejected(token):
    """格式不认识就报错，**绝不"猜着解"**。"""
    with pytest.raises(cc.CredentialCryptoError):
        cc.decrypt(MASTER_A, token)


def test_encrypt_refuses_empty_and_short_values():
    with pytest.raises(ValueError):
        cc.encrypt(MASTER_A, "")
    with pytest.raises(cc.CredentialCryptoError):
        cc.encrypt(b"too-short", SENTINEL)


def test_error_messages_never_carry_values():
    """所有错误消息都不含明文、密文或主密钥片段。"""
    token = cc.encrypt(MASTER_A, SENTINEL)
    texts = []
    for call in (
        lambda: cc.decrypt(MASTER_B, token),
        lambda: cc.decrypt(MASTER_A, _flip(token)),
        lambda: cc.decrypt(MASTER_A, "garbage"),
    ):
        with pytest.raises(cc.CredentialCryptoError) as excinfo:
            call()
        texts.append(str(excinfo.value))
    for text in texts:
        assert SENTINEL not in text
        assert MASTER_A.hex()[:16] not in text
        assert token[:16] not in text


# ================================================================== 5. 主密钥


def test_master_key_file_is_created_once_and_reused(key_file):
    """首次取用会生成文件；再次取用**复用**同一把（否则旧密文全部解不开）。"""
    settings, path = key_file
    cc.reset_master_key_cache()
    first = cc.master_key(settings, logger=logging.getLogger("haut_seat.test"))
    assert path.exists()
    assert len(path.read_bytes()) == 32
    assert not path.read_bytes() == b"\x00" * 32

    cc.reset_master_key_cache()
    assert cc.master_key(settings, logger=logging.getLogger("haut_seat.test")) == first
    # 同一进程内不重新读文件也不需要。
    assert cc.master_key(settings, logger=logging.getLogger("haut_seat.test")) == first


def test_master_key_roundtrip_through_a_file(key_file):
    """换进程（清缓存）之后，用同一份文件仍能解开之前加密的内容。"""
    settings, _path = key_file
    cc.reset_master_key_cache()
    token = cc.encrypt(cc.master_key(settings), SENTINEL)
    cc.reset_master_key_cache()
    assert cc.decrypt(cc.master_key(settings), token) == SENTINEL


def test_master_key_generation_is_serialised(key_file):
    """并发取用只能生成**一把**密钥 —— 否则先加密的后写入会互相解不开。"""
    import threading

    settings, path = key_file
    cc.reset_master_key_cache()
    keys: list[bytes] = []
    barrier = threading.Barrier(6)

    def worker():
        barrier.wait()
        keys.append(cc.master_key(settings, logger=logging.getLogger("haut_seat.test")))

    threads = [threading.Thread(target=worker) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(set(keys)) == 1
    assert path.read_bytes() == keys[0]


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("ab" * 32, bytes.fromhex("ab" * 32)),                       # 64 位十六进制
        (base64.urlsafe_b64encode(b"k" * 24).decode(), b"k" * 24),   # base64
        ("my passphrase 123", b"my passphrase 123"),                # 原始文本 ≥16 字节
    ],
)
def test_env_master_key_accepts_three_forms(key_file, raw, expected):
    settings, _path = key_file
    assert cc._decode_secret(raw) == expected


def test_env_master_key_uses_the_documented_priority(key_file):
    """顺序是**先十六进制、再 base64、最后原始文本** —— 歧义时按前两种解释。

    ``"a-plain-text-key-of-24bytes!!"`` 去掉非字母表字符后仍能按 base64 解出 ≥16 字节，
    于是它被当成 base64（这就是文档写明的顺序）。要说清这一点，否则"我明明填了一句话"
    与"实际用的密钥"不一致，将来极难排查。
    """
    key_file_settings, _path = key_file
    assert cc._decode_secret("a-plain-text-key-of-24bytes!!") != b"a-plain-text-key-of-24bytes!!"
    # 想用原始文本，就得让它在 base64 下解不出 ≥16 字节（或者用十六进制）。
    assert cc._decode_secret("my passphrase 123") == b"my passphrase 123"
    assert key_file_settings is not None


@pytest.mark.parametrize("raw", ["", "   ", "short", "aaaa"])
def test_env_master_key_rejects_undecodable_values(raw):
    assert cc._decode_secret(raw) is None


def test_configured_env_key_wins_over_the_file(key_file, make_settings, tmp_path):
    """配了环境变量里的主密钥就用它，**不碰**文件里那把（并据此报错而不是静默换钥）。"""
    path = tmp_path / "auth2" / "master.key"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(MASTER_B)
    settings = make_settings(
        auth_master_key_file=str(path),
        auth_cred_key=base64.urlsafe_b64encode(MASTER_A).decode(),
    )
    cc.reset_master_key_cache()
    assert cc.master_key(settings, logger=logging.getLogger("haut_seat.test")) == MASTER_A
    assert path.read_bytes() == MASTER_B, "配了环境变量就不该改写文件"


def test_the_documented_env_var_really_drives_the_cipher(monkeypatch, tmp_path, make_settings):
    """**走环境变量**（不是传参）时，"我配了主密钥"必须是一句真话。

    这条用例来自一个真实缺陷：文档写 ``HAUT_CRED_KEY``，而 pydantic 按字段名
    ``auth_cred_key`` 推导出的是 ``HAUT_AUTH_CRED_KEY`` ⇒ 文档里那个名字被静默
    忽略，程序悄悄用了文件里那把。上面那条用例传的是**关键字参数**，管道再直也
    绕过了"键名到底叫什么"这一步，所以这里必须从环境变量进。
    """
    path = tmp_path / "auth3" / "master.key"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(MASTER_B)  # 文件里预置**另一把**：误用时会被立刻看出来

    for name in ("HAUT_AUTH_CRED_KEY", "HAUT_CRED_KEY"):
        monkeypatch.setenv(name, base64.urlsafe_b64encode(MASTER_A).decode())
        settings = make_settings(auth_master_key_file=str(path))
        cc.reset_master_key_cache()
        assert cc.master_key(settings, logger=logging.getLogger("haut_seat.test")) == MASTER_A, (
            f"{name} 设置了却没生效 —— 那正是「配了密钥」变成错觉的方式"
        )
        assert path.read_bytes() == MASTER_B, f"{name} 生效时不该改写文件"
        monkeypatch.delenv(name)


def test_undecodable_env_key_raises_instead_of_falling_back(key_file, make_settings):
    """环境变量里的主密钥解不出来 ⇒ **大声报错**，绝不退化到文件里的密钥。"""
    path = key_file[1]
    settings = make_settings(auth_master_key_file=str(path), auth_cred_key="tiny")
    cc.reset_master_key_cache()
    with pytest.raises(cc.CredentialCryptoError) as excinfo:
        cc.master_key(settings, logger=logging.getLogger("haut_seat.test"))
    message = str(excinfo.value)
    # 消息里必须写出**文档里那个名字**（两个都写），否则照着消息改配置也会落空。
    assert "HAUT_AUTH_CRED_KEY" in message and "HAUT_CRED_KEY" in message
    stripped = message.replace("HAUT_AUTH_CRED_KEY", "").replace("HAUT_CRED_KEY", "")
    assert "tiny" not in stripped, "错误消息里不得出现主密钥的值"
    assert not path.exists(), "报错路径不该顺手生成一个文件"


# ================================================================== 6. user_credentials 的收口


def test_field_helpers_bind_user_and_field(key_file):
    settings, _path = key_file
    token = uc.encrypt_field(settings, 5, "cookie", COOKIE_VALUE)
    assert token != COOKIE_VALUE and COOKIE_VALUE not in token
    assert uc.decrypt_field(settings, 5, "cookie", token) == COOKIE_VALUE
    with pytest.raises(uc.CredentialDecryptError):
        uc.decrypt_field(settings, 6, "cookie", token)
    with pytest.raises(uc.CredentialDecryptError):
        uc.decrypt_field(settings, 5, "authorization", token)


def test_decrypt_error_is_uniform_and_valueless(key_file):
    """解不开时对外只有一句"请重新绑定"，细节（异常类型）只留在日志里。"""
    settings, _path = key_file
    token = uc.encrypt_field(settings, 5, "cookie", COOKIE_VALUE)
    with pytest.raises(uc.CredentialDecryptError) as excinfo:
        uc.decrypt_field(settings, 9, "cookie", token)
    message = str(excinfo.value)
    assert "重新绑定" in message
    assert SENTINEL not in message
    assert "HMAC" not in message and "tag" not in message.lower()


def test_decrypt_failure_is_logged_without_values(key_file, caplog):
    settings, _path = key_file
    token = uc.encrypt_field(settings, 5, "cookie", COOKIE_VALUE)
    with caplog.at_level(logging.DEBUG, logger="haut_seat"):
        with pytest.raises(uc.CredentialDecryptError):
            uc.decrypt_field(settings, 9, "cookie", token)
    assert SENTINEL not in caplog.text
    assert "CredentialCryptoError" in caplog.text