"""用户本人的「学校统一身份认证」登录：**只做官方接口的转发，不做任何绕过**。

## 这个模块做什么、不做什么

做：

1. ``GET /stage-api/captchaImage`` 取一张验证码图片 + 它的 ``uuid``；
2. 把图片**原样**交给用户（前端渲染），由用户**本人**在手机或电脑上读出字符；
3. 用户提交「学校账号 + 学校口令 + 验证码」后，用官方接口
   ``POST /stage-api/login4web2HGD`` **一次性**提交 ``{username, password, code, uuid}``；
4. 成功（``code == 200`` 且返回了 ``token``）时，按站点自己的约定
   （登录成功后前端把 ``token`` 写成 Cookie ``Admin-Token``，请求头
   ``Authorization = "Bearer " + 该 Cookie``）换算出该用户自己的
   ``Cookie`` / ``Authorization``，交给 :mod:`haut_seat.services.user_credentials`
   加密保存。

**不做**（这几条是红线，不是"暂未实现"）：

- 不识别、不解码、不猜测验证码 —— 字符只可能来自人的眼睛；
- **不重试**验证码：一次提交一次结果。若失败，必须重新取图重新输入。
  任何"失败了再试一次"的循环，本质都是对验证码的暴力尝试，不做；
- 不保存学校口令（一个字节都不落盘、不进日志、不进事件消息），
  不保存验证码答案，不保存 ``uuid`` 以外的会话状态；
- 不伪造成功：只有站点真的返回了 ``code == 200`` 与 ``token`` 才算成功。

## 依据（不是猜的）

接口路径、请求体字段名、响应信封 ``{code, msg, ...}``、判定条件
``code === 200``、以及 ``Authorization`` 与 ``Admin-Token`` 的关系，
全部来自站点自身前端代码与一次真实响应（见 ``docs/auth-recon.md``）。
站点**没有** OAuth / CAS / 授权回调这类正规第三方登录机制，所以"用户本人
在这台服务器上完成一次官方登录"是唯一可行的路径。
"""

from __future__ import annotations

import base64
import logging
import re
from dataclasses import dataclass

from haut_seat.logging_conf import redact_text

logger = logging.getLogger("haut_seat.school_login")

#: 智慧空间接口前缀（站点前端自己的 baseURL）
STAGE_PREFIX = "/stage-api"
CAPTCHA_PATH = f"{STAGE_PREFIX}/captchaImage"
LOGIN_PATH = f"{STAGE_PREFIX}/login4web2HGD"

#: 登录成功后站点前端写入的 Cookie 名。Authorization 由它推导而来：
#: 站点自己的请求拦截器就是 ``"Bearer " + 读该 Cookie``，用户的 .env 也逐字节一致。
TOKEN_COOKIE_NAME = "Admin-Token"

#: 站点成功码
SUCCESS_CODE = 200

MAX_CAPTCHA_BYTES = 512 * 1024
MAX_MESSAGE_LEN = 200
_B64_RE = re.compile(r"^[A-Za-z0-9+/]+={0,2}$")


class SchoolLoginError(RuntimeError):
    """登录过程中出现可预期的失败（网络、站点拒绝、响应形态异常）。

    消息面向用户展示，**绝不含口令、验证码答案或任何凭据值**。
    """


@dataclass(frozen=True)
class CaptchaChallenge:
    """一张验证码：``uuid`` 用于配对本次输入，图片以 data URI 形式交给前端。"""

    uuid: str
    image_data_uri: str

    def __repr__(self) -> str:  # pragma: no cover - 简洁性由测试钉住
        return (
            f"CaptchaChallenge(uuid={self.uuid!r}, "
            f"image_data_uri=<{len(self.image_data_uri)} 字节>)"
        )


@dataclass(frozen=True)
class LoginOutcome:
    """一次登录尝试的结果。

    ``ok`` 为假时 ``cookie`` / ``authorization`` 一定是空串 —— 调用方不必
    自己判断"这次到底有没有值"，因为那次失败根本不会产出凭据。
    """

    ok: bool
    message: str
    cookie: str = ""
    authorization: str = ""
    token_fingerprint: str = ""

    def __repr__(self) -> str:  # pragma: no cover - 简洁性由测试钉住
        # 默认 repr 会把整条 JWT 打进日志（与 Credentials 同样的坑），必须覆盖。
        return (
            f"LoginOutcome(ok={self.ok!r}, message={self.message!r}, "
            f"token_fingerprint={self.token_fingerprint!r}, "
            f"cookie=<redacted>, authorization=<redacted>)"
        )


def anonymous_client(settings):
    """构造一个**不带任何凭据**的客户端。

    取验证码与提交登录都必须是匿名的：带上管理端那份全局 Cookie 去请求登录接口
    既无意义，又会让"这次登录用的是谁的凭据"变得含糊。这里显式传一个空凭据对象，
    而不是走 ``resolve_credentials`` —— 不走解析层就不可能意外附带任何账号。
    """
    from haut_seat.services.auth_store import Credentials
    from haut_seat.services.io import build_http_client

    return build_http_client(
        settings, credentials=Credentials(cookie=None, authorization=None, source="none")
    )


def _sanitize_captcha_image(raw) -> str:
    """把站点返回的 ``img`` 校验成可安全渲染的 data URI。

    只接受 base64 图片数据：站点返回的是 base64（已核实），但我们不假定它永远如此
    —— 若它哪天变成一个 http 地址或一段 HTML，**直接拒绝**（返回空串），
    绝不把远端内容原样塞进页面。
    """
    text = str(raw or "").strip()
    if not text or len(text) > MAX_CAPTCHA_BYTES * 2:
        return ""
    if text.startswith("data:image/"):
        head, _, payload = text.partition(",")
        if "base64" not in head:
            return ""
        text = payload.strip()
    if not _B64_RE.match(text):
        return ""
    try:
        decoded = base64.b64decode(text, validate=True)
    except (ValueError, TypeError):
        return ""
    if len(decoded) < 64 or len(decoded) > MAX_CAPTCHA_BYTES:
        return ""
    return "data:image/jpeg;base64," + text


def _clean_message(raw, fallback: str) -> str:
    """站点给的消息经脱敏与截断后再展示 —— 它可能被拼进事件消息与页面。"""
    text = redact_text(str(raw or "")).strip()
    text = " ".join(text.split())
    if not text:
        return fallback
    return text[:MAX_MESSAGE_LEN]


def fetch_captcha(settings, *, client=None) -> CaptchaChallenge:
    """取一张验证码。**只读**请求，网络抖动允许重试（由客户端的 GET 语义决定）。

    站点返回 ``{code, msg, img, uuid}``：``img`` 是 base64 图片、``uuid`` 是配对本张
    图片的编号。两者缺一不可，缺任一个都当作失败抛出 —— 绝不能凭空造一个 uuid
    去提交登录（那必然失败，而且是在对站点说假话）。
    """
    owned = client is None
    if owned:
        client = anonymous_client(settings)
    try:
        body = client.get(CAPTCHA_PATH)
    except Exception as exc:
        raise SchoolLoginError(
            f"获取验证码失败：{_client_error_text(exc)}。请稍后重试。"
        ) from exc
    finally:
        if owned:
            try:
                client.close()
            except Exception:  # pragma: no cover - 关闭失败不该影响用户
                pass

    if not isinstance(body, dict):
        raise SchoolLoginError("获取验证码失败：站点返回了非预期内容。")
    uuid = str(body.get("uuid") or "").strip()
    image = _sanitize_captcha_image(body.get("img"))
    if not uuid or not image:
        raise SchoolLoginError(
            "获取验证码失败：站点未返回可用的验证码图片。"
            f"（站点消息：{_clean_message(body.get('msg'), '无')}）"
        )
    return CaptchaChallenge(uuid=uuid[:64], image_data_uri=image)


def submit_login(
    settings, *, username: str, password: str, code: str, uuid: str, client=None
) -> LoginOutcome:
    """用官方接口**一次性**提交登录，成功则换算出该用户自己的凭据。

    参数里出现口令是不可避免的（它就是登录的输入），但它的生命周期只有这一次调用：
    不进日志、不进返回值、不进异常消息、不入库。调用方在请求处理结束后即可丢弃。

    失败返回 ``LoginOutcome(ok=False, message=...)``（可预期：验证码错、口令错、
    账号被锁），网络/协议异常抛 :class:`SchoolLoginError`。两者调用方都要如实展示，
    **绝不把失败说成成功**。
    """
    user = str(username or "").strip()
    secret = str(password or "")
    answer = str(code or "").strip()
    ticket = str(uuid or "").strip()
    # 本地就能判定的缺失，不必浪费一次出站请求（也避免对着站点发空请求）
    if not user or not secret or not answer or not ticket:
        return LoginOutcome(False, "请填写学校账号、口令与验证码（验证码请先点击获取）")

    owned = client is None
    if owned:
        client = anonymous_client(settings)
    try:
        try:
            body = client.post(
                LOGIN_PATH,
                json={"username": user, "password": secret, "code": answer, "uuid": ticket},
            )
        except Exception as exc:
            raise SchoolLoginError(
                f"登录请求失败：{_client_error_text(exc)}。请稍后重试。"
            ) from exc
    finally:
        if owned:
            try:
                client.close()
            except Exception:  # pragma: no cover
                pass

    if not isinstance(body, dict):
        return LoginOutcome(False, "登录失败：站点返回了非预期内容")

    code_value = body.get("code")
    token = str(body.get("token") or "").strip()
    try:
        success = int(code_value) == SUCCESS_CODE
    except (TypeError, ValueError):
        success = False

    if not success or not token:
        # 站点的消息（"验证码错误"/"密码错误"…）如实转达；失败**绝不**产出凭据。
        return LoginOutcome(
            False, _clean_message(body.get("msg"), "登录未通过（站点未说明原因）")
        )

    from haut_seat.services.auth_store import fingerprint

    return LoginOutcome(
        ok=True,
        message="登录成功",
        cookie=f"{TOKEN_COOKIE_NAME}={token}",
        authorization=f"Bearer {token}",
        token_fingerprint=fingerprint(f"Bearer {token}"),
    )


def _client_error_text(exc: Exception) -> str:
    """把出站异常翻译成一句人话（不含 URL 查询串与任何请求头）。"""
    name = type(exc).__name__
    mapping = {
        "ConnectError": "无法连接学校站点",
        "ConnectTimeout": "连接学校站点超时",
        "ReadTimeout": "学校站点响应超时",
        "TimeoutException": "请求学校站点超时",
        "RedirectError": "站点要求跳转（可能页面已变更）",
        "NonJsonResponseError": "站点返回的不是接口数据（可能被网关拦截）",
        "HttpError": "站点返回了错误状态码",
        "AuthenticationError": "站点拒绝了本次请求",
    }
    # 异常原文可能带 URL，这里只取类名映射；认不出就给一个中性的说法。
    for key, text in mapping.items():
        if key in name:
            return text
    return "请求未成功（网络或站点异常）"