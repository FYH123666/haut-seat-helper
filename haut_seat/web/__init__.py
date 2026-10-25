"""Web 管理面板：FastAPI 应用、路由、模板与数据访问。

模块划分：
- ``db``             SQLite 连接与建表
- ``security``       口令哈希、会话、CSRF、登录限流
- ``validation``     表单校验（复用核心时间窗口校验）
- ``repository``     计划 / 运行记录 / 事件 / 设置的数据访问
- ``deps``           模板上下文、会话依赖、CSRF 校验
- ``app``            应用工厂与安全响应头
- ``routes``         全部 HTTP 路由
- ``templates``      Jinja2 模板（移动端优先）
- ``static``         本地 CSS / JS

安全基线：默认只监听 127.0.0.1；管理接口默认要求登录；所有状态变更走 POST + CSRF；
页面与日志均不出现 Cookie / Authorization / JWT / 口令。
"""

from __future__ import annotations

__all__ = ["create_app"]


def create_app(*args, **kwargs):  # pragma: no cover - 便捷再导出
    from haut_seat.web.app import create_app as _create_app

    return _create_app(*args, **kwargs)