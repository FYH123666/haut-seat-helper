"""登录状态判定与预约前认证预检（纯逻辑，无 IO）。

判定依据全部来自本项目对线上站点的**受控只读实测**（非猜测）。实测记录
（2026-10-23，只发 GET，不提交任何预约）：

**A. 座位查询接口 ``/stage-api/api/seatbook/layout/query`` —— 不能用来验证凭据**

| 场景 | HTTP | 业务 code | 响应体特征 |
|---|---|---|---|
| 有效 ``Authorization`` | 200 | 200 | 含 ``seatList``（70 个座位） |
| **完全没有** ``Authorization`` 头 | 200 | 200 | 含 ``seatList``（同样 70 个座位） |
| 已失效、但格式合法的 ``Authorization``（本项目 ``.env`` 里那份） | 200 | **1111** | 与匿名**逐字相同** |
| 伪造签名（JWT 形状合法、签名不对） | 200 | **401** | ``msg=请求访问：/spacelink/error，认证失败，无法访问系统资源`` |
| 当天窗口（闭馆后） ``08:00→22:00`` | 200 | 1111 | ``msg=预约开始时间必须大于结束时间`` |
| 明天及以后的窗口 | 200 | 1111 | ``msg=只能预约1天之内的时间(含当天)`` |
| 无 ``Authorization`` 的 ``GET /stage-api/api/seatbook/layout/getDefaultTime`` | 200 | 200 | ``startDate=今天 23:05`` / ``endDate=今天 22:00``（闭馆后窗口为空）|

由 A 得出的硬结论：

1. **这个接口的响应无法证明登录状态**：携带已失效凭据、不携带凭据、携带伪造签名
   三种情况里，「已失效」与「不携带」得到的业务响应**完全一致**（同一段业务时间规则
   文案），而「伪造签名」得到的 401 只说明服务器**解析不了这个头**。
   因此「code=1111 ≠ 认证失败」和「code≠401 ⇒ 认证有效」**两条都不能成立**。
2. ``code=1111`` 是纯业务时间规则（且只要闭馆，任何当天窗口都会命中），
   ``code=500`` 是服务端异常 —— 都与认证无关，不得据此判定掉登录。

**B. 业务身份接口 ``GET /stage-api/getInfo`` —— 可用来验证凭据**

| 场景 | HTTP | 业务 code | 响应体特征 |
|---|---|---|---|
| 站点自身 SPA：登录成功后由路由守卫调用（``headers:{Authorization:"Bearer "+Admin-Token}``），读取 ``user`` 与 ``roles`` | 200 | 200 | 含 ``user``/``roles`` |
| 无 ``Authorization`` | 200 | **401** | ``msg=请求访问：/spacelink/getInfo，认证失败，无法访问系统资源`` |
| 携带已失效（但格式合法）的 ``Authorization`` | 200 | **401** | 与匿名**逐字相同** |

B 是**唯一有实测依据**的凭据验证入口：它是站点自己登录后必经的一步，
且**明确区分**「被接受」与「未被接受」。本项目据此判定凭据是否可用于业务接口。

**C. 状态码语义（取自站点自己的前端错误表，不是本项目的推测）**：
``401:"认证失败，无法访问系统资源"``、``403:"当前操作没有权限"``。
因此 **HTTP/业务 403 不说「令牌过期」** —— 它按站点原文只表示「没有权限」。
本项目据此停止流程（不放任盲提交），但如实保留 HTTP 状态与业务 code，
**绝不断言令牌过期**。

本模块只做判定，不发请求；探测编排在
``haut_seat.client.seatbook.SeatBookClient.check_identity``（验证凭据）与
``check_session``（顺带看座位数据）。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any

from haut_seat.exceptions import (
    AuthenticationError,
    HttpError,
    RedirectError,
    SeatBookError,
)

# 实测：认证失败时业务 code=401（RuoYi 风格在 HTTP 200 中承载）。
# 403 一并纳入 —— 站点自己的前端错误表把 403 记为「当前操作没有权限」，
# 方向保守（停止而非继续），**但措辞上绝不说「令牌过期」**（见 AUTH_403_NOTE）。
AUTH_BUSINESS_CODES = frozenset({401, 403})

#: 实测到的认证失败文案特征（仅作 code 之外的兜底佐证，且要求响应无 seatList）。
AUTH_MESSAGE_KEYWORDS = ("认证失败", "无法访问系统资源")

#: 站点自身前端错误表里的 403 语义（原样引用，不是本项目对过期与否的推断）。
AUTH_403_NOTE = "403「当前操作没有权限」"

#: 业务身份接口的响应字段（实测：站点登录后由路由守卫读取这两个字段）。
IDENTITY_FIELDS = ("user", "roles")

# 服务端错误 code 下界：仅用于「非认证问题」的提示措辞，不影响判定方向。
_SERVER_ERROR_CODE_MIN = 500

_MAX_MSG_LEN = 200

RELOGIN_HINT = """\
服务器未接受当前凭据（认证失败 / 没有权限），本次预约已停止（未轮询座位、未提交任何预约）。
以上是服务器的原样返回，本程序**没有**据此推断令牌是否过期：
  - HTTP/业务 401 = 站点原文「认证失败，无法访问系统资源」；
  - HTTP/业务 403 = 站点原文「当前操作没有权限」（也可能是网关/权限问题）。
请按以下步骤重新登录并更新凭据：
  1. 浏览器打开 https://wslib.haut.edu.cn/ ，用统一身份认证账号正常登录（不要用无痕模式以外的方式绕过验证码）。
  2. F12 → Network（网络）→ 刷新页面或点一次「座位预约」→ 任选一个 /stage-api/ 请求。
  3. 复制该请求的 Authorization 请求头完整值（形如 "Bearer xxx.yyy.zzz"），
     以及 Cookie 请求头中 Admin-Token= 的那一段。
  4. 编辑本项目根目录的 .env，覆盖这两行（不要加引号，不要提交到版本库）：
         HAUT_AUTHORIZATION=Bearer <新值>
         HAUT_COOKIE=Admin-Token=<新值>
     Cookie 与 Authorization 是同一个令牌，两处都必须换成新的。
  5. 运行验证：python -m haut_seat check-auth
     看到「凭据已被业务接口接受」（/getInfo 返回身份数据）即已恢复；
     看到「业务接口未接受该凭据」说明第 3 步复制有误，请重做。
     看到「无法确认」说明是网络/服务端问题，不是掉登录，稍后复核即可。
  6. 验证通过后再重新运行 python -m haut_seat book。
"""


class AuthStatus(str, Enum):
    """预检结论。三态是为了避免把「查不到」当成「失效」。"""

    VALID = "valid"                    # 已确认登录有效
    INVALID = "invalid"                # 已确认认证失效 → 必须停止流程
    INDETERMINATE = "indeterminate"    # 无法确认（网络/服务端/结构异常）→ 不得当作失效


@dataclass(frozen=True)
class AuthCheck:
    """一次认证预检的结果。``message`` 为脱敏后的中文说明，不含凭据。"""

    status: AuthStatus
    message: str
    http_status: int | None = None
    business_code: Any = None
    seat_count: int | None = None

    @property
    def is_valid(self) -> bool:
        return self.status is AuthStatus.VALID

    @property
    def is_invalid(self) -> bool:
        return self.status is AuthStatus.INVALID

    @property
    def is_indeterminate(self) -> bool:
        return self.status is AuthStatus.INDETERMINATE


def _clip(text: Any, limit: int = _MAX_MSG_LEN) -> str:
    s = str(text if text is not None else "")
    return s if len(s) <= limit else s[:limit] + "…"


def is_auth_business_code(code: Any) -> bool:
    """业务 code 是否表示「凭据未被接受 / 没有权限」（实测：401；403 一并纳入）。"""
    if isinstance(code, bool):  # bool 是 int 的子类，需先排除
        return False
    if isinstance(code, int):
        return code in AUTH_BUSINESS_CODES
    if isinstance(code, str):
        return code.strip() in {str(c) for c in AUTH_BUSINESS_CODES}
    return False


def _looks_like_auth_message(msg: str) -> bool:
    return any(kw in msg for kw in AUTH_MESSAGE_KEYWORDS)


def canonical_probe_window(now: datetime | None = None) -> tuple[str, str]:
    """只读探测用的时间窗口：当天 ``08:00:00 → 22:00:00``。

    **这个窗口并不总是合法的**（实测更正，2026-10-23）：服务器的时间规则是
    「只能预约 1 天之内的时间(含当天)」，且当天窗口的起点会被顶到**当前时刻**。
    闭馆之后（``getDefaultTime`` 实测返回 ``startDate=23:05 / endDate=22:00``）
    当天窗口必然 ``start > end``，于是任何当天窗口都会得到
    ``code=1111 预约开始时间必须大于结束时间``；明天及以后则是
    ``code=1111 只能预约1天之内的时间(含当天)``。

    结论：**不要用这个窗口的响应来判断凭据是否有效**（它只反映业务时间规则，
    且在闭馆时段恒为 1111）。验证凭据请用
    ``SeatBookClient.check_identity``（``GET /stage-api/getInfo``，实测能区分
    「凭据被接受」与「未被接受」）；本窗口只用于「顺便看一眼座位数据」。
    """
    day = (now or datetime.now()).strftime("%Y-%m-%d")
    return f"{day} 08:00:00", f"{day} 22:00:00"


def classify_identity(
    data: Any,
    *,
    http_status: int | None = 200,
    expect_authenticated: bool = True,
) -> AuthCheck:
    """根据**业务身份接口**（``GET /stage-api/getInfo``）的响应判定凭据是否可用。

    这是唯一有实测依据的凭据验证入口：站点自己的路由守卫在登录后调用它，
    读取 ``user`` 与 ``roles``；实测无凭据与携带已失效凭据都返回业务 ``code=401``。

    成功判据（严格）：``code==200`` 且响应里**确实带着身份数据**
    （``user`` 是对象，或 ``roles`` 是数组）—— 也就是需求里的
    「通过实际业务接口验证，确认响应包含正常数据」。消息里只出现**字段名与条数**，
    绝不出现任何字段值（可能含姓名/学号等个人信息）。
    """
    if not isinstance(data, dict):
        return AuthCheck(
            AuthStatus.INDETERMINATE,
            f"身份接口响应不是 JSON 对象（{type(data).__name__}），无法验证凭据",
            http_status=http_status,
        )

    code = data.get("code")
    msg = _clip(data.get("msg"))
    user = data.get("user")
    roles = data.get("roles")
    has_user = isinstance(user, dict)
    has_roles = isinstance(roles, list)

    # 1) 明确未被接受：实测匿名与失效凭据都是业务 code=401。
    if is_auth_business_code(code) or _looks_like_auth_message(msg):
        note = f"（{AUTH_403_NOTE}）" if str(code).strip() in ("403",) else ""
        return AuthCheck(
            AuthStatus.INVALID,
            f"业务接口未接受该凭据：HTTP {http_status} / 业务 code={code!r} {msg or '无消息'}{note}"
            " —— 以上为服务器原样返回，未据此推断令牌是否过期",
            http_status=http_status,
            business_code=code,
        )

    # 2) 正常返回且确实带着身份数据 → 凭据被业务接口接受。
    if code in (200, "200") and (has_user or has_roles):
        seen = []
        if has_user:
            seen.append(f"user({len(user)} 个字段)")
        if has_roles:
            seen.append(f"roles({len(roles)} 项)")
        return AuthCheck(
            AuthStatus.VALID,
            "凭据已被业务接口接受（/getInfo 返回 " + "、".join(seen) + "，字段值不显示）",
            http_status=http_status,
            business_code=code,
        )

    # 3) code=200 但没有身份数据：结构不符 → 不确认（绝不当作成功）。
    if code in (200, "200"):
        return AuthCheck(
            AuthStatus.INDETERMINATE,
            "身份接口返回成功但没有身份数据（缺 user/roles 字段），无法验证凭据",
            http_status=http_status,
            business_code=code,
        )

    if isinstance(code, int) and not isinstance(code, bool) and code >= _SERVER_ERROR_CODE_MIN:
        return AuthCheck(
            AuthStatus.INDETERMINATE,
            f"身份接口服务端异常 code={code}，非认证问题，无法据此验证凭据",
            http_status=http_status,
            business_code=code,
        )

    return AuthCheck(
        AuthStatus.INDETERMINATE,
        f"身份接口业务响应 code={code!r} msg={msg!r}，无法据此验证凭据",
        http_status=http_status,
        business_code=code,
    )


def classify_body(
    data: Any,
    *,
    http_status: int | None = 200,
    expect_authenticated: bool = True,
) -> AuthCheck:
    """根据只读座位查询的响应体判定登录状态（纯函数）。

    **注意（实测更正）**：这个接口无法证明凭据可用，因此本函数**永不返回 VALID**。
    携带已失效凭据与完全不携带凭据时，它的响应逐字相同（都能拿到座位列表），
    ``code=200 + seatList`` 只说明「这次查询被受理了」，不说明持谁的身份。
    唯一有实测依据的凭据验证入口是 :func:`classify_identity`。
    ``expect_authenticated=False`` 表示未配置 ``HAUT_AUTHORIZATION``。
    """
    if not isinstance(data, dict):
        return AuthCheck(
            AuthStatus.INDETERMINATE,
            f"响应不是 JSON 对象（{type(data).__name__}），无法判断登录状态",
            http_status=http_status,
        )

    code = data.get("code")
    msg = _clip(data.get("msg"))
    seat_list = data.get("seatList")
    has_seat_list = isinstance(seat_list, list)

    # 1) 明确认证失效：业务 code=401（实测），或 msg 命中认证失败文案且无座位列表
    if is_auth_business_code(code) or (not has_seat_list and _looks_like_auth_message(msg)):
        return AuthCheck(
            AuthStatus.INVALID,
            f"服务器返回认证失败 code={code!r}（{msg or '无消息'}）",
            http_status=http_status,
            business_code=code,
        )

    # 2) 座位查询返回了座位列表：**但这不是登录凭据的证明**。
    #    实测（2026-10-23）：携带已失效凭据与完全不携带凭据时，本接口返回逐字相同的
    #    响应（含座位列表）。因此这里只能报「顺带看到了座位数据」，绝不判 VALID ——
    #    否则「Cookie/令牌还在但早已失效」会被显示成「登录有效」。
    if code in (200, "200") and has_seat_list:
        hint = "" if expect_authenticated else "（另外：未配置 HAUT_AUTHORIZATION）"
        return AuthCheck(
            AuthStatus.INDETERMINATE,
            f"只读座位查询返回 {len(seat_list)} 个座位{hint}，但该接口对未认证请求同样"
            "放行（实测），无法据此验证凭据是否可用；凭据验证请用业务身份接口 /getInfo",
            http_status=http_status,
            business_code=code,
            seat_count=len(seat_list),
        )

    # 3) 服务端异常：不是认证问题
    if isinstance(code, int) and not isinstance(code, bool) and code >= _SERVER_ERROR_CODE_MIN:
        return AuthCheck(
            AuthStatus.INDETERMINATE,
            f"服务端异常 code={code}，非认证问题，无法据此判断登录状态",
            http_status=http_status,
            business_code=code,
        )

    # 4) 其它业务错误（如 code=1111/404 时间规则）或结构不符：保持「无法确认」
    return AuthCheck(
        AuthStatus.INDETERMINATE,
        f"业务响应 code={code!r} msg={msg!r}，与认证无关，无法据此判断登录状态",
        http_status=http_status,
        business_code=code,
    )


def classify_exception(
    exc: BaseException,
    *,
    expect_authenticated: bool = True,
) -> AuthCheck:
    """把预检过程中抛出的异常归类为认证失效或「无法确认」（纯函数）。

    只有能明确指向「凭据被拒」的异常才判定为 INVALID；
    网络故障、超时、服务端 5xx、响应结构异常一律 INDETERMINATE。
    """
    if isinstance(exc, AuthenticationError):
        if exc.status_code == 403:
            # 站点自己的前端错误表把 403 记为「当前操作没有权限」。方向保守（停止流程），
            # 但**绝不说「令牌过期」** —— 403 也可能是网关/WAF 或权限问题。
            return AuthCheck(
                AuthStatus.INVALID,
                f"服务器返回 HTTP 403（{AUTH_403_NOTE}）：已停止本次流程，"
                "但**不据此断言令牌过期**；请如实核对服务器返回的状态。",
                http_status=exc.status_code,
            )
        return AuthCheck(
            AuthStatus.INVALID,
            f"服务器返回 HTTP {exc.status_code}（认证失败）：凭据未被接受。"
            "此为服务器原样返回的状态，未据此推断令牌是否过期。",
            http_status=exc.status_code,
        )

    if isinstance(exc, RedirectError):
        # 实测该接口不会用 3xx 表达未登录（而是 200+code=401）；真出现 3xx 时
        # 通常是跳统一身份认证，属于「需要登录」。保守停止，不继续轮询/提交。
        return AuthCheck(
            AuthStatus.INVALID,
            f"{exc}（疑似跳转登录页）",
        )

    if isinstance(exc, HttpError):
        return AuthCheck(
            AuthStatus.INDETERMINATE,
            f"服务器返回 HTTP {exc.status_code}，非认证问题，无法据此判断登录状态",
            http_status=exc.status_code,
        )

    # 延迟导入：避免核心层在无 httpx 环境下导入失败（httpx 为运行依赖）
    import httpx

    if isinstance(exc, httpx.TimeoutException):
        return AuthCheck(AuthStatus.INDETERMINATE, "预检请求超时（网络问题，非认证失效）")

    if isinstance(exc, httpx.TransportError):
        return AuthCheck(
            AuthStatus.INDETERMINATE, f"网络异常（非认证失效）: {type(exc).__name__}"
        )

    # HTML/非 JSON 响应：可能是登录页，也可能是网关错误页，无法确认 → 保持不确定
    if isinstance(exc, SeatBookError):
        text = str(exc)
        if "HTML" in text or "登录页" in text:
            return AuthCheck(
                AuthStatus.INDETERMINATE,
                f"响应疑似 HTML 页面（可能是登录页，也可能是错误页），无法确认登录状态: {_clip(text)}",
            )
        return AuthCheck(
            AuthStatus.INDETERMINATE, f"响应异常，无法确认登录状态: {_clip(text)}"
        )

    return AuthCheck(
        AuthStatus.INDETERMINATE,
        f"预检异常，无法确认登录状态: {type(exc).__name__}",
    )