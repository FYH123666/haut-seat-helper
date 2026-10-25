"""座位查询与预约接口封装。

查询与提交分离：layout/query 为只读；addbooking 为真实预约（有副作用），单次不重试。
凭据验证（check_identity）用站点自己的业务身份接口 ``GET /stage-api/getInfo`` —— 实测它
**明确区分**「凭据被接受」（code=200 + user/roles）与「未被接受」（code=401），
因此可作为「凭据是否可用于业务接口」的判据。原有的 ``check_session`` 仍保留，
但只用于顺带看一眼座位数据：**实测表明它无法验证凭据**（匿名同样放行）。
两者都绝不触碰 addbooking。
"""

from __future__ import annotations

from typing import Any

import httpx

from haut_seat.client.http import SeatHttpClient
from haut_seat.core.auth import (
    AuthCheck,
    AuthStatus,
    canonical_probe_window,
    classify_body,
    classify_exception,
    classify_identity,
    is_auth_business_code,
)
from haut_seat.exceptions import (
    AuthenticationError,
    HttpError,
    NonJsonResponseError,
    RedirectError,
)
from haut_seat.models.booking import BookingOutcome, BookingResult
from haut_seat.models.seat import LayoutQueryResult, Seat

LAYOUT_QUERY_PATH = "/stage-api/api/seatbook/layout/query"
ADD_BOOKING_PATH = "/stage-api/api/seatbook/user/addbooking"
#: 业务身份接口。实测证据：站点自己的路由守卫在登录成功后调用它
#: （``url:"/getInfo"``，请求头 ``Authorization: "Bearer " + Admin-Token``），
#: 并读取响应里的 ``user`` 与 ``roles``；无凭据 / 凭据未被接受时返回业务 code=401。
IDENTITY_PATH = "/stage-api/getInfo"


def _classify_submit_result(data: Any) -> BookingResult:
    """分类提交响应。

    成功判据（来自真实网页请求证据）：
    - code == 200 且 bookingid 有效（非空非零）→ SUCCESS。
    - 认证失效 code（401/403，实测服务器在 HTTP 200 中以业务 code 表达）→ AUTH_FAILED，
      必须立即停止，绝不能当作「可重试的失败」再次提交。
    - 其他非零 business code → FAILED（所有已知记录中均为错误）。
    - code==200 但 bookingid 缺失/无效 → UNKNOWN。
    - 其余情况 → UNKNOWN。
    不输出 readerno / realname / Cookie / Token 等个人信息与凭据。
    """
    if not isinstance(data, dict) or "code" not in data:
        return BookingResult(BookingOutcome.UNKNOWN, "响应结构与预期不符，无法确认提交结果")

    code = data.get("code")
    msg = str(data.get("msg", "") or "")
    booking_id = data.get("bookingid")
    seat_info = str(data.get("seatName") or data.get("seatsno") or "")

    if is_auth_business_code(code):
        return BookingResult(
            BookingOutcome.AUTH_FAILED,
            f"服务器返回认证失败 code={code}，登录已失效，不再提交",
        )

    if code == 200 and booking_id:
        return BookingResult(
            BookingOutcome.SUCCESS,
            f"预约成功 bookingid={booking_id} seat={seat_info}".strip(),
            booking_id=booking_id,
        )
    if code == 200 and not booking_id:
        return BookingResult(
            BookingOutcome.UNKNOWN,
            f"code=200 但缺少 bookingid, seat={seat_info}".strip(),
        )
    if code != 0 and code != "0" and code != "":
        return BookingResult(BookingOutcome.FAILED, msg or f"错误 code={code}")
    return BookingResult(BookingOutcome.UNKNOWN, msg or f"code={code} 无法确认其结果")


class SeatBookClient:
    def __init__(self, http: SeatHttpClient):
        self.http = http

    def check_identity(self, *, expect_authenticated: bool = True) -> AuthCheck:
        """凭据验证：调用业务身份接口 ``GET /stage-api/getInfo``（只读，无副作用）。

        这是需求里「只有通过实际业务接口验证、确认响应包含正常数据才算成功」那一步：
        返回 ``VALID`` 的**充要**依据是 ``code=200`` 且响应里带着 ``user``/``roles``；
        返回 ``INVALID`` 的依据是实测到的业务 ``code=401``（或 HTTP 401/403）；
        其余一律 ``INDETERMINATE``（网络/服务端/结构异常），绝不当成掉登录。

        未配置 ``Authorization`` 时（``expect_authenticated=False``）不发起这个请求：
        Cookie 单独存在**不能**证明凭据可用（实测服务端只认 Authorization 头），
        直接如实返回「无法验证」。
        """
        if not expect_authenticated:
            return AuthCheck(
                AuthStatus.INDETERMINATE,
                "未配置 HAUT_AUTHORIZATION：服务端只校验 Authorization 头（实测），"
                "只有 Cookie 无法证明凭据可用；请配置 Authorization 后重新验证",
            )
        try:
            data = self.http.get(IDENTITY_PATH)
        except Exception as exc:  # 网络/HTTP/结构异常 → 交由分类器判定
            return classify_exception(exc, expect_authenticated=expect_authenticated)
        return classify_identity(data, expect_authenticated=expect_authenticated)

    def check_session(
        self,
        regionid: Any,
        starttime: str | None = None,
        endtime: str | None = None,
        *,
        expect_authenticated: bool = True,
    ) -> AuthCheck:
        """只读座位查询探测（**不再**作为凭据判据，见本模块与 ``core.auth`` 的实测说明）。

        - 未指定时间时使用当天 08:00→22:00 窗口；注意该窗口在闭馆后必然命中业务时间
          规则（code=1111），所以它的响应**从不**用于判定凭据是否有效。
        - 只有实测确认的「HTTP 200 + 业务 code 401」（或 HTTP 401/403、重定向）才算
          凭据未被接受；其余业务错误/网络/服务端异常一律保持不确定。
        """
        st, en = starttime, endtime
        if not st or not en:
            st, en = canonical_probe_window()
        params = {
            "pageNum": 1,
            "pageSize": 10,
            "regionid": regionid,
            "starttime": st,
            "endtime": en,
        }
        try:
            data = self.http.get(LAYOUT_QUERY_PATH, params=params)
        except Exception as exc:  # 网络/HTTP/结构异常 → 交由分类器判定
            return classify_exception(exc, expect_authenticated=expect_authenticated)
        return classify_body(data, expect_authenticated=expect_authenticated)

    def query_layout(
        self,
        regionid: Any,
        starttime: str,
        endtime: str,
        page_num: int = 1,
        page_size: int = 100,
    ) -> LayoutQueryResult:
        """只读查询座位布局与座位列表。"""
        params = {
            "pageNum": page_num,
            "pageSize": page_size,
            "regionid": regionid,
            "starttime": starttime,
            "endtime": endtime,
        }
        data = self.http.get(LAYOUT_QUERY_PATH, params=params)
        return LayoutQueryResult.from_response(data)

    def submit_booking(
        self, seat: Seat, starttime: str, endtime: str, channel: str
    ) -> BookingResult:
        """真实预约提交（GET，有副作用，单次、不自动重试）。

        参数来自已观察的网页请求证据：channel / seatid / starttime / endtime。
        响应成功判据未确认：非零业务 code 判定为 FAILED，其余为 UNKNOWN。
        """
        params = {
            "channel": channel,
            "seatid": seat.id,
            "starttime": starttime,
            "endtime": endtime,
        }
        try:
            data = self.http.get(ADD_BOOKING_PATH, params=params, retry=False)
        except RedirectError:
            return BookingResult(BookingOutcome.AUTH_FAILED, "请求被重定向，请更新登录凭据")
        except AuthenticationError as e:
            return BookingResult(
                BookingOutcome.AUTH_FAILED,
                f"服务器拒绝访问（HTTP {e.status_code}），登录已失效，不再提交",
            )
        except HttpError as e:
            return BookingResult(BookingOutcome.FAILED, f"HTTP {e.status_code}")
        except NonJsonResponseError as e:
            msg = str(e)
            if "HTML" in msg or "登录" in msg:
                return BookingResult(BookingOutcome.AUTH_FAILED, msg)
            return BookingResult(BookingOutcome.UNKNOWN, msg)
        except httpx.ConnectError as e:
            # TCP 连接失败 → 请求未到达服务器 → 无副作用 → 可重试
            return BookingResult(BookingOutcome.FAILED, f"连接失败（可重试）: {e}")
        except httpx.TimeoutException as e:
            # 超时 → 无法确认服务器是否已处理 → 不可冒险自动重试
            return BookingResult(BookingOutcome.UNKNOWN, f"请求超时，无法确认是否已提交: {e}")
        except Exception as e:
            return BookingResult(BookingOutcome.UNKNOWN, f"请求异常: {e}")
        return _classify_submit_result(data)

    def query_booking_result(self) -> BookingResult:
        """预约结果回查（独立接口，预留）。仅在取得「预约记录」真实请求证据后实现。"""
        raise NotImplementedError(
            "预约结果回查未实现：缺少「预约记录」接口的真实请求证据"
        )

    def addbooking(self, *args: Any, **kwargs: Any) -> BookingResult:
        """历史别名：真实预约提交，本阶段未实现。"""
        raise NotImplementedError(
            "真实预约提交未实现，禁止调用 addbooking (" + ADD_BOOKING_PATH + ")"
        )