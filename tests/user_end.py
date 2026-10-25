"""用户端账号的测试助手（不在 conftest 里，避免用例去 import conftest）。

用户端现在**有自己的账号体系**：页面与写入口都要求先登录，且每个用户只看得到
自己的计划。这里把"注册 / 登录"这条真实链路封装成几个纯函数，
用例里**不直接往库里塞会话** —— 那样会绕过表单解析、口令校验、CSRF 与
Cookie 作用域，让"登录到底通不通"这条最该被测到的链路变成假绿。
"""

from __future__ import annotations

import re

#: 用户端口令（只在测试内存里存在）。
USER_PASSWORD = "Correct-Horse-9"
#: 默认用户名（小写；``user_auth`` 会规范化成小写后再比对）。
USERNAME = "alice"
#: 用户端 POST 必须带的同源 Origin（TestClient 的 Host 是 ``testserver``）。
ORIGIN = "http://testserver"

_CSRF_FIELD_RE = re.compile(r'name="_csrf" value="([^"]+)"')


def page_csrf(client, path: str) -> str:
    """GET 一个页面并取出它的 ``_csrf`` 值（用户端每个 POST 都必须带上）。"""
    resp = client.get(path)
    assert resp.status_code == 200, (path, resp.status_code)
    match = _CSRF_FIELD_RE.search(resp.text)
    assert match, f"{path} 页面里没有 CSRF 字段"
    return match.group(1)


def create_user(db, username: str = USERNAME, password: str = USER_PASSWORD, **kwargs) -> int:
    """直接在库里建一个用户端账号（口令仍走真实的 scrypt 哈希），返回 user_id。"""
    from haut_seat.web import user_auth

    return int(user_auth.create_user(db, username, password, **kwargs))


def find_user_id(db, username: str = USERNAME) -> int:
    from haut_seat.web import user_auth

    row = user_auth.find_user(db, username)
    assert row is not None, f"库里没有用户 {username}"
    return int(row["id"])


def login_user(client, db, username: str = USERNAME, password: str = USER_PASSWORD):
    """走**真实登录表单**建立用户端会话，返回 ``(client, user_id)``。"""
    from haut_seat.web import user_auth

    token = page_csrf(client, "/booking/login")
    resp = client.post(
        "/booking/login",
        data={"_csrf": token, "username": username, "password": password},
        headers={"Origin": ORIGIN},
    )
    assert resp.status_code == 303, resp.text
    assert resp.headers["location"].startswith("/booking"), resp.headers["location"]
    assert client.cookies.get(user_auth.USER_SESSION_COOKIE), "登录没有种下用户端会话 Cookie"
    return client, find_user_id(db, username)


def logout_user(client):
    """点真实的退出按钮（同样要走 CSRF 与同源校验）。"""
    token = page_csrf(client, "/booking")
    return client.post(
        "/booking/logout",
        data={"_csrf": token},
        headers={"Origin": ORIGIN},
    )


#: 测试用的**假**学校凭据（形状与真实令牌一致，但绝不是任何真账号的令牌）。
FAKE_SCHOOL_USERNAME = "2023000000"
FAKE_SCHOOL_COOKIE = "Admin-Token=" + "eyJhbGciOiJIUzUxMiJ9." + "FAKETESTSENTINEL" + ".SIG"
FAKE_SCHOOL_AUTHORIZATION = "Bearer " + "eyJhbGciOiJIUzUxMiJ9." + "FAKETESTSENTINEL" + ".SIG"


def bind_credentials(
    db, settings, user_id: int, *,
    school_username: str = FAKE_SCHOOL_USERNAME,
    cookie: str = FAKE_SCHOOL_COOKIE,
    authorization: str = FAKE_SCHOOL_AUTHORIZATION,
    status: str = "valid",
) -> None:
    """把一份**假的**学校凭据按用户存好（等价于"该用户已完成绑定"）。

    走的是生产同一条 ``user_credentials.save_credentials``（真加密、真落库），
    只是不经过学校登录接口 —— 绑定流程本身由 ``test_web_bind_flow.py`` 用
    ``httpx.MockTransport`` 覆盖。测试里**不发起任何真实登录**。
    """
    from haut_seat.services import user_credentials as uc

    uc.save_credentials(
        db, settings, int(user_id),
        school_username=school_username, cookie=cookie, authorization=authorization,
        status=status, message="测试预置", source="test",
    )