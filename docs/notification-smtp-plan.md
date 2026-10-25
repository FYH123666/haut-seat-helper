# 邮件通知（SMTP）实现计划

> **状态：计划，未实现。** 本文档只是实施方案，本轮**没有**导入 `smtplib`、
> 没有修改 `_deliver`、没有发出任何真实邮件。
>
> 本计划**不包含**自动登录、验证码处理或会话提取 —— 那几项风险性质不同
> （涉及向学校站点提交凭据、绕过人机校验），必须单独一轮、单独评审。见第 7 节。

## 1. 现状（代码事实，含出处）

「通知」这条链路**只有装配和数据落库**已经是完整的，唯独最后一步发送是空实现。

| 事实 | 出处 |
|---|---|
| 事件枚举与中文标签已定：`booking_success` / `auth_failure` / `auth_recovery` / `no_seat` / `task_exception` / `submit_unknown` / `service_exception` | `haut_seat/services/notification_service.py:24-42` |
| 默认开启的事件（`no_seat` 默认关） | `notification_service.py:48-56` |
| 去重窗口 30 分钟、最大尝试 3 次 | `notification_service.py:44-45` |
| 去重只看 `status='sent'` 的记录，因此**失败的通知不会阻止后续重试** | `notification_service.py:138-142` |
| `_deliver()` 直接 `raise NotImplementedError`，被 `_send_with_retry` 捕获后记成 `status='skipped'` 并如实写明「未实现」 | `notification_service.py:150-153`、`161-168` |
| `notify()` 的契约是**绝不抛异常**，任何异常都被吞掉并记录 | `notification_service.py:115-130` |
| 正文与错误落库前都过 `repo.redact_message()` | `notification_service.py:82`、`181-182` |
| 设置表字段：`enabled` / `channel` / `smtp_host` / `smtp_port` / `smtp_user` / `smtp_password_env` / `mail_from` / `mail_to` / `use_tls` / `event_flags` | `haut_seat/web/db.py:132-147` |
| 设置读写：`get_notify_setting` / `set_notify_setting`；**不接受也不存储口令** | `haut_seat/web/repository.py:528-581` |
| 前端表单字段与设置页（含「本轮仅预留」提示、历史表） | `haut_seat/web/templates/notifications.html`、`haut_seat/web/routes.py:479-514` |
| 配置模型里已预留 `smtp_password`（`SecretStr`，对应 `HAUT_SMTP_PASSWORD`） | `haut_seat/config.py:71-72` |
| 调度器在**运行结束后的同一条线程**里同步调用 `notify_*` | `haut_seat/services/scheduler.py:463-484`、`501-531` |
| 通知服务实例由应用装配产生 | `haut_seat/web/app.py:68-86` |

## 2. 实现前必须先定下的四件事

### 2.1 传输模式：`use_tls` 一个布尔量不足以表达三种模式

现在是「端口 + 一个开关」。SMTP 实际上有三种模式，必须明确映射规则，否则
465 和 587 会配错（最常见的结果是连不上或静默降级）：

| `use_tls` | 端口 | 模式 | `smtplib` 调用 |
|---|---|---|---|
| 1 | 465 | 隐式 TLS（连上就是 TLS） | `SMTP_SSL(host, port, context=..., timeout=...)` |
| 1 | 其它（587/25） | 显式 STARTTLS | `SMTP(host, port, timeout=...)` → `starttls(context=...)` |
| 0 | 任意 | 明文 | `SMTP(...)` |

**建议**：按上表在代码里推导，不改表结构。`use_tls=0` 时**只允许** `smtp_host`
是回环地址（`127.0.0.1` / `::1` / `localhost`，即本机自建中转），否则把这次发送
判为配置错误并拒绝，绝不把口令与正文发到公网的明文连接上。

**必须避免的坑**：`SMTP.starttls()` 在服务器不支持时会抛 `SMTPNotSupportedError`；
必须让它成为失败，**不允许** catch 之后继续明文 `login()`（这是常见的凭据泄露写法）。
`login()` 之前还要检查 `sock` 已加密。

### 2.2 口令怎么读：`.env` 里的值**不在** `os.environ` 里（已实测）

设置页保存的是**变量名**（`smtp_password_env`），实现时自然会写成
`os.environ[setting["smtp_password_env"]]`。但本项目用 pydantic-settings 读 `.env`，
它是**自己解析该文件**，不会写回 `os.environ`。实测（仓库外一次性脚本，用假口令）：

```
settings.smtp_password = 'fake-value-not-a-real-secret'
os.environ.get('HAUT_SMTP_PASSWORD') = None          # ← .env 里的值取不到
with real env var -> settings: 'from-real-environ' | environ: 'from-real-environ'
```

**结论**：口令只放在 `.env` 里、又用 `os.environ[...]` 去读，会**静默取到空值**。

**建议方案**：口令放进一个**独立的、由 systemd 以 root 读取的环境文件**，例如
`/etc/haut-seat/smtp.env`（`root:root`、`chmod 600`），在单元里加一行
`EnvironmentFile=-/etc/haut-seat/smtp.env`（`-` 前缀=文件不存在时不报错）。
systemd 先以 root 读出变量注入进程环境，再降权到 `User=hautseat` 运行，于是：

- `os.environ[name]` 能取到值，且支持设置页里自定义变量名；
- 服务账号**没有权限直接打开那个文件**（权限比放进 `.env` 更严，`.env` 服务账号可读）；
- 口令与图书馆 Cookie 的存放位置解耦。

读取顺序建议写成：先 `os.environ.get(name)`，取不到时再回退到
`Settings.smtp_password`（仅当 `name == "HAUT_SMTP_PASSWORD"`）。两条路径都取不到
→ 记 `status='failed'`，`last_error` 写「未找到环境变量 X（口令未配置）」，
**不写**任何可能与口令有关的内容。

### 2.3 发送不能在预约任务的线程里做

`_after_run()` 由运行线程在预约结束后直接调用，`notify_*` 是同步调用
（`scheduler.py:484` → `501-531`）。SMTP 连接 + 登录 + DATA 在本机网络下也可能
要几百毫秒到数秒，DNS 解析失败或对端不响应时更久（`smtplib` 默认取全局 socket 超时）。
放在这条线程上，等于让一个与预约无关的外部服务决定「运行收尾」要多久。

**建议方案**：`NotificationService` 内部加**一个有界队列 + 一个发送线程**
（1 个 worker 就够，通知频率极低）：

- `notify()` 只做「判开关 / 判事件 / 判去重 / 写一条 `pending` 记录 / 入队」，
  立刻返回，签名与契约（绝不抛异常）不变；
- 队列满（建议容量 100）时丢弃并记 `skipped`，理由写明「队列已满」——
  宁可丢通知，也不能让通知反压预约；
- worker 用 `smtplib` 的显式 `timeout=`（建议 10 秒）发送，结果回写同一条记录为
  `sent` / `failed`；
- 停机时 worker 是守护线程，跟现有 `Scheduler` 的处理方式保持一致（最多等 5 秒）。

**代价（如实写明）**：`notify()` 返回时状态多半还是 `pending`，界面上要能显示这个
中间态；`_send_with_retry` 现在的「3 次重试」语义要挪到 worker 里，否则同一线程内
重试仍然阻塞。

### 2.4 收件人解析

`mail_to` 是单个 TEXT（≤500 字符），模板上写着「多个用逗号分隔」，但**目前没有任何
解析代码**。实现时要：按 `,` / `;` / 换行切分 → `strip` → 丢弃空项 → 去重 →
限制数量（建议 ≤ 5）→ 校验里不含空白与控制字符。空收件人或全部非法时判为配置错误，
不发信。

## 3. 分阶段实施（每阶段可独立验证、可独立回滚）

| 阶段 | 内容 | 验证方式（全部离线） | 是否发真实邮件 |
|---|---|---|---|
| A | 纯函数层：`SmtpConfig` 解析与校验（模式推导、收件人切分、端口/主机/开关一致性） | 单元测试，无网络 | 否 |
| B | 口令读取层：`os.environ` → `Settings.smtp_password` 回退 | 用假值设置环境变量做断言；断言错误信息里不含口令 | 否 |
| C | 发送层：`_deliver()` 实现 + 可注入的 sender（默认才是 `smtplib`） | 注入替身 sender，断言调用参数（模式、超时、收件人），全程不联网 | 否 |
| D | 队列 + worker 改造（2.3 节），`pending` 中间态与界面展示 | 替身 sender + 阻塞式假 sender，断言预约路径不被阻塞 | 否 |
| E | 设置页加「发送测试邮件」按钮（管理员 + CSRF + 独立限流），**必须二次确认** | 用替身 sender 走完整 HTTP 流程 | 否（测试里） |
| F | 真实连通性验证 | **人工、带外**执行一次，收件人用你自己的邮箱 | 是（仅此一步） |
| G | 收尾：清理 `smtp_implemented=False` 这个传了却没人用的模板参数（`routes.py:489`），更新模板与文档措辞 | 全量测试 | 否 |

阶段 F 是唯一会发出真实邮件的步骤，必须由你在服务器上手动执行，且在此之前
先用 A–E 的替身测试把逻辑跑透。

## 4. 硬性安全要求（实现时必须满足）

1. **口令绝不入库**：`set_notify_setting` 继续不接受口令字段；数据库里只有变量名。
2. **口令绝不出现在日志、页面、异常文本里**：任何异常先过
   `repo.redact_message()` 再落库；发送层自行拼装的错误信息里只允许出现
   「主机 / 端口 / 模式 / 收件人数量 / 异常类型」。
3. **口令绝不进命令行**：不要新增 `--password` 之类的 CLI 参数（会进 shell history
   与进程列表）。
4. **不回退明文**：STARTTLS 失败即失败（见 2.1）。
5. **失败不影响预约**：`notify()` 的「绝不抛异常」契约必须保持；worker 抛出的异常
   只能在 worker 内部消化。
6. **通知内容只用已脱敏文本**：正文继续只由 `build_request(detail=...)` 生成，
   `detail` 来自 `report.message`，落库前过 `redact_message`。
7. **测试里绝不发真实邮件**：契约测试（`tests/conftest.py` 的离线夹具）已经保证
   提交替身一被调用就抛错；SMTP 要照同样的思路做——默认 sender 在测试里被替换，
   若被真实调用即失败。

## 5. 测试计划

| 用例 | 断言 |
|---|---|
| 模式推导 | 465+TLS→SSL；587+TLS→STARTTLS；TLS 关 + 公网主机→拒绝；TLS 关 + 127.0.0.1→允许 |
| 收件人解析 | 逗号/分号/换行/重复/空白/超量 各一例 |
| 口令来源 | 环境变量有值→用它；只有 `Settings.smtp_password` 且名字为默认→用它；都没有→`failed` 且错误信息不含口令 |
| 发送成功 | 记录为 `sent`，`attempts` 正确 |
| 发送失败 | 记录为 `failed`，`last_error` 已脱敏 |
| STARTTLS 不可用 | 判为失败，且**没有**发生 `login()` |
| 去重 | 30 分钟内同 `dedup_key` 且上次 `sent` → 本次 `skipped`；上次 `failed` → 本次仍发送 |
| 不影响预约 | 让 sender 抛异常/长时间阻塞，断言预约流程的返回值与状态不受影响 |
| 队列满 | 记 `skipped` 且原因写明，不抛异常 |

## 6. 上线检查清单（实施那一轮再逐项确认）

- [ ] `/etc/haut-seat/smtp.env` 属主 `root:root`、权限 `600`，且**不在**仓库目录内
- [ ] 单元里有 `EnvironmentFile=-/etc/haut-seat/smtp.env`，且 `systemctl show` 能看到
- [ ] `.env` 与仓库里没有出现任何真实口令（`git status` 干净、日志里搜不到）
- [ ] 设置页只填了变量名，没有填口令本身
- [ ] 真实测试邮件只发到自己的邮箱，且知其会经过第三方邮件服务
- [ ] 确认失败通知不会重试到把 IP 打进对方黑名单（建议重试上限 + 退避）
- [ ] 通知历史里能看到 `pending` / `sent` / `failed` / `skipped` 四种状态

## 7. 明确**不在**本计划内

这三项与 SMTP 是不同性质的风险，**不要**合并到一轮里做：

- **自动登录**：需要向学校站点以程序方式提交凭据。它改变的是「凭据会不会被程序
  自动送去第三方」，属于认证路径的改动，需要单独设计、单独评审。
- **验证码处理**：`docs/auth-recon.md` 记录的是已确认的登录流程事实；处理人机校验
  涉及绕过站点的反自动化措施，需要单独评估合规性与可行性，且很可能不可行。
- **会话提取 / 凭据刷新**：当前认证失效只能人工更新（见 `docs/web-deploy.md` 第 11 节），
  这是刻意的设计，不因 SMTP 而上线自动化更新。

SMTP 这一轮只解决「有事怎么告诉我」，不碰「我怎么登进去」。