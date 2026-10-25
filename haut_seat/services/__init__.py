"""服务层（Web 与 CLI 共用）。

本包只封装既有能力，不重写预约实现：
- ``state``：计划/运行状态的纯函数状态机（无 IO，可离线测试）。
- ``booking_runner``：把 ``__main__.cmd_book`` 的主循环抽成可注入依赖的运行器，
  CLI 与 Web 后台调度器共用同一份实现。
- ``auth_service`` / ``notification_service``：本轮只做接口预留与状态记录，
  不实现学校登录接口、不实现 SMTP。
- ``scheduler``：与浏览器生命周期解耦的后台调度器。
"""