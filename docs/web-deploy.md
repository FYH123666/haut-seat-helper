# Web 管理面板：部署与运维

本文件说明如何安装、启动、加固、备份第一阶段的 Web 管理面板。
CLI 的用法见 [README](../README.md)。

> **一句话边界**：面板负责「建计划、管开关、看运行记录」；它**不会**自动登录学校系统，
> 也**不会**在你没点确认的情况下开始预约。所有真实预约仍然受
> `HAUT_ENABLE_REAL_BOOKING`、暂停旗标、全局暂停开关和计划状态四道闸门约束。

---

## 1. 它是什么 / 不是什么

**已实现（第一阶段）**

- 管理员登录 / 登出 / 会话（scrypt 哈希口令、CSRF、登录限流、Cookie 加固）
- 预约计划管理：新建、编辑、暂停、恢复、停止、删除；重复规则与执行时间
- 后台调度器：与浏览器生命周期解耦、同计划不并发、重启后的状态恢复、持久状态机
- 运行记录与事件日志（分阶段记录，日志脱敏）
- 认证状态框架（三态展示 + 人工恢复流程引导）
- 邮件通知的**接口与设置模型**（事件去重 / 重试 / 记录）
- 系统状态页：全局暂停开关、调度器开关、维护清理

**明确未实现（不要当成可用）**

| 项目 | 现状 |
|---|---|
| 邮件发送（SMTP） | **未实现**。`NotificationService._deliver()` 一律抛 `NotImplementedError`，只能记成 `skipped`，界面与记录里都如实写「未实现」。填了 SMTP 服务器也不会收到邮件。 |
| 自动登录 / 认证自动续期 | **未实现**。认证页的「进入人工恢复」「重新检测登录状态」只做状态流转与只读预检，不发任何登录请求、不猜参数、不碰验证码。 |
| 浏览器自动化（Playwright 等） | **未引入**。仅作为未来候选记录在案。 |
| 预约结果查询接口 | 未采集，未实现。 |
| 多管理员权限分级 | 只有「管理员」一种角色，没有只读账号。 |

认证页与通知页上的相关按钮**都是可用的**（会真实写入状态、真实执行只读预检），
但它们的功能是「引导人工处理」和「保存设置」，不是「自动完成」。

---

## 2. 安装

要求 Python 3.10+。**建虚拟环境前先确认版本**：

```bash
python3 -V        # 必须 ≥ 3.10
```

RHEL 系发行版（含 OpenCloudOS、CentOS / Rocky / Alma）自带的 `python3` 版本随发行版
版本号而异，不一定满足要求。若低于 3.10，请先用发行版软件源装一个 3.10+ 解释器，
再用**那个**解释器建虚拟环境（例如 `python3.11 -m venv .venv`）——
不要用系统默认的 `python3` 建 venv，否则 `pip install` 会因
`requires-python = ">=3.10"` 直接失败。

```bash
cd haut-seat-helper
python -m venv .venv

# Windows
.venv\Scripts\activate
# Linux / macOS
source .venv/bin/activate

# 至少装 CLI（核心依赖）
pip install -e ".[dev]"      # 开发/测试：含 pytest 与全部 Web 依赖
# 或：pip install -e ".[web]"  # 只跑面板，不装 pytest
```

配置：

```bash
cp .env.example .env      # Windows: copy .env.example .env
cp config/seats.json.example config/seats.json   # 可选：座位优先级
```

`.env` 的 Web 相关项见 [`.env.example`](../.env.example) 中的
「Web 管理面板」一节，关键几项：

| 变量 | 默认 | 说明 |
|---|---|---|
| `HAUT_WEB_HOST` | `127.0.0.1` | **安全默认只监听本机**。改成 `0.0.0.0` 等于用明文 HTTP 把登录页暴露出去，除非在完全可信的内网，否则不要改。 |
| `HAUT_WEB_PORT` | `8080` | 监听端口 |
| `HAUT_WEB_DB` | `data/web.db` | SQLite 路径（含管理员哈希、计划、运行记录） |
| `HAUT_WEB_SESSION_SECURE` | `false` | **走 HTTPS 时必须置 `true`**，否则会话 Cookie 没有 `Secure` 属性 |
| `HAUT_WEB_TRUST_PROXY` | `false` | 是否由**应用自己**解析 `X-Forwarded-For`（取最右一项 = 反向代理追加的真实来源）。默认 `false` 时限流同样有效：只监听 `127.0.0.1` 时服务只可能被本机 nginx 访问，uvicorn 默认只信任本机来源，会按「从右往左第一个不受信任的地址」解析该头。开启的前提是服务不能被绕过代理直连。 |
| `HAUT_WEB_ALLOW_FIRST_RUN_SETUP` | `true` | 是否允许在网页上创建**第一个**管理员 |
| `HAUT_WEB_ENABLE_SCHEDULER` | `true` | 后台调度器总开关 |
| `HAUT_SCHEDULER_TICK_S` | `5.0` | 调度器轮询间隔 |
| `HAUT_SCHEDULER_MAX_CONCURRENT` | `3` | 同时运行的计划数上限 |
| `HAUT_SCHEDULER_RUN_LOG_KEEP_DAYS` | `30` | 运行日志保留天数（「系统」页「维护」按钮据此清理） |

---

## 3. 数据库初始化

```bash
python -m haut_seat init-db
```

- **幂等**：重复执行只建缺失的表，不会删任何数据，也不会把已有的管理员、
  计划、运行记录或你手动改过的全局暂停开关重置回默认值。
- 输出会告诉你当前管理员数量；为 0 时会提示下一步用 `create-admin` 建一个。
- 启动 Web 服务时**也会**自动建表（`create_app` 内部会调 `init_schema()`），
  所以 `web` 命令不依赖你先跑 `init-db`。`--init-db` 只是让这一步显式可见：

  ```bash
  python -m haut_seat web --init-db
  ```

建出来的表：`admin_user`、`login_attempt`、`session`、`plan`、`run`、`run_event`、
`auth_state`、`auth_event`、`notify_setting`、`notify_event`、`app_setting`、`meta`，
以及**用户端（多用户）**的六张表：`app_user`、`user_session`、`user_login_attempt`、
`user_credential`、`user_plan`、`user_auth_event`。

> **升级既有部署不需要任何迁移动作。** 本仓库没有迁移机制（见 `web/db.py` 的说明），
> 所以多用户改造**只加新表、绝不给既有表加列** —— 加列会让老库的 `SELECT` 直接抛
> `no such column`。你只要按平常的方式重启一次服务，这六张表就会被自动补建，
> 既有的计划、运行记录、管理员、全局开关**一个字节都不会变**
> （`tests/test_web_db.py::test_a_database_from_before_multiuser_only_gains_new_tables`
> 把这条逐项钉住：列清单、行内容、`global_pause` 都做前后比对）。
> 备份与恢复仍然只需管 `data/web.db` 与 `data/auth/`（见第 8 节）。

安全默认值：`app_setting.global_pause = 1`（全局暂停**开**）、
`notify_setting.enabled = 0`（通知**关**）、`auth_state.status = 'indeterminate'`、
自助注册**默认关闭**（`meta` 里的开关，需管理员在 `/admin/users` 打开并生成邀请码）。

### 多用户凭据的主密钥（`data/auth/master.key`）

每个用户**自己那一份**学校凭据是加密保存的，密钥来自 `data/auth/master.key`
（首次有人绑定时自动生成 32 字节；也可以用 `HAUT_AUTH_CRED_KEY` 提供，
简写 `HAUT_CRED_KEY` 同样接受 —— 两个都设时以 `HAUT_AUTH_CRED_KEY` 为准）。

> ⚠️ **这个文件必须纳入备份。** 删掉或换掉它，所有用户已保存的凭据都会解不开，
> 每个人都要重新绑定一次。系统在解密失败时**不会**回退到别人的账号或旧的全局凭据，
> 而是把该用户的计划转入「等待认证」并提示重新绑定 —— 这是刻意的设计，不是故障。
> 它同样**不应提交进版本库**（`data/` 已在 `.gitignore` 中）。

第 8 节的备份/恢复流程请把它与 `data/web.db` 一起处理。

---

## 4. 管理员初始化

有两条路，**二选一**。

### 方式 A：CLI 创建（推荐，尤其服务已能被外部访问时）

```bash
python -m haut_seat create-admin --username 你的用户名
```

- 口令**交互式输入**，不回显；省略 `--password` 就不会留在 shell 历史里。
- 口令至少 10 位，首尾不能有空白；只以 `scrypt$n$r$p$salt$hash` 形式入库，
  **明文不入库、不入日志、不出现在任何页面或 API 响应里**。
- 用户已存在时默认拒绝，需显式重置：

  ```bash
  python -m haut_seat create-admin --username 你的用户名 --reset
  ```

  重置会**使该管理员的全部既有网页会话立即失效**，并要求重新登录。
- 非交互环境（脚本 / systemd 前置步骤）可以用 `--password`，但那样口令会进入
  shell 历史与进程列表，**不推荐**。

建完后建议把网页创建入口关掉：

```bash
# .env
HAUT_WEB_ALLOW_FIRST_RUN_SETUP=false
```

### 方式 B：首次运行网页引导

库中**没有任何管理员**时，访问 `/admin` 会被引到 `/admin/setup`，
在页面上创建第一个管理员。

- 该入口带一次性引导令牌与 CSRF 校验，创建成功后立即关闭，不能重用。
- `HAUT_WEB_ALLOW_FIRST_RUN_SETUP=false` 时该页面直接 403，并提示改用 `create-admin`。
- **注意**：只要服务此刻能被外部访问，任何人抢先访问 `/admin/setup` 就能成为管理员。
  首次部署请先本地（或经代理限制访问）完成这一步，再对外开放。

### 登录安全策略（内置，无需配置）

| 项目 | 值 |
|---|---|
| 口令哈希 | `scrypt`（n=2^14, r=8, p=1, dklen=32），每用户独立盐 |
| 会话 Cookie | `hs_session`，`HttpOnly` + `SameSite=Lax` + `Path=/`（HTTPS 下加 `Secure`） |
| 会话有效期 | 绝对 12 小时；空闲 120 分钟 |
| 会话存储 | 只存 token 的 sha256 摘要，泄露数据库也拿不到可用 Cookie |
| 登录限流 | 同一用户名+IP 15 分钟内失败 8 次 → 锁定 15 分钟 |
| 状态变更 | 全部要求 CSRF 令牌 + 有效会话 |
| 页面缓存 | `/admin` 全部响应带 `Cache-Control: no-store` |
| 响应头 | CSP、`X-Frame-Options: DENY`、`X-Content-Type-Options: nosniff` |
| API 文档 | `/docs`、`/redoc`、`/openapi.json` 一律 404（不暴露） |

---

## 5. 本地启动

```bash
# 前台启动（Ctrl+C 停止）
python -m haut_seat web --init-db

# 换端口 / 换地址
python -m haut_seat web --host 127.0.0.1 --port 9000
```

启动后访问 <http://127.0.0.1:8080/admin>，用第 4 步建的管理员登录。

- 只读健康检查：<http://127.0.0.1:8080/healthz>
- 启动日志会打印面板地址、数据库路径、真实预约开关状态、**会话 Cookie 是否带 `Secure`**；
  监听非回环地址时会额外打印一条警告。
- 会话 Cookie 那一行是关键：开着 `Secure` 却用 `http://` 访问，表现就是
  「登录成功却一直回到登录页」（排查见第 12 节）。
- **启动服务本身不会执行任何预约**：新计划默认「已暂停 + 未启用」，
  全局暂停默认开启，且仍受 `HAUT_ENABLE_REAL_BOOKING` 与暂停旗标限制。

---

## 6. systemd 部署

示例单元文件：[`deploy/haut-seat-web.service`](../deploy/haut-seat-web.service)。
示例按「代码放在 `/opt/haut-seat-helper`、用独立系统账号 `hautseat` 运行」编写。

### 先准备运行账号与目录

**这一步不能跳过**：单元里的 `ExecStartPre` 会以该账号运行，账号不存在、或 `data/`、`logs/`
不可写，`systemctl enable --now` 会直接失败。

```bash
# 1) 独立系统账号（系统账号、无登录 shell）
sudo useradd --system --home /opt/haut-seat-helper --shell /usr/sbin/nologin hautseat

# 2) 代码与虚拟环境放到 /opt/haut-seat-helper：属主 root，运行账号只读
sudo mv haut-seat-helper /opt/haut-seat-helper
sudo chown -R root:root /opt/haut-seat-helper
sudo chmod -R a+rX /opt/haut-seat-helper

# 3) 运行态目录归运行账号所有（单元里它们是 ReadWritePaths：数据库、跨进程锁、暂停旗标、日志）
sudo install -d -o hautseat -g hautseat -m 750 \
     /opt/haut-seat-helper/data /opt/haut-seat-helper/logs

# 4) .env 只允许运行账号读取：里面有图书馆 Cookie / Authorization
sudo chown hautseat:hautseat /opt/haut-seat-helper/.env
sudo chmod 600 /opt/haut-seat-helper/.env
```

### 安装与启动

```bash
sudo cp deploy/haut-seat-web.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now haut-seat-web
sudo systemctl status haut-seat-web
```

按实际情况要改的是 `User` / `Group` / `WorkingDirectory` 三项（三处保持一致）。还有三点容易踩：

- 单元以 `ProtectSystem=strict` + `ReadWritePaths=data logs` 运行，代码目录整体只读；
  `data/` 与 `logs/` **必须存在**——`ReadWritePaths=` 里没有 `-` 前缀的路径若不存在，
  单元会因挂载命名空间配置失败而拒绝启动（`systemd.exec(5)`）。
- **不要把代码装在 `/home` 或 `/root` 下**：单元里的 `ProtectHome=true` 会让
  `/home`、`/root`、`/run/user` 对服务**不可见且为空**，服务会读不到代码和虚拟环境。
  确实要装在 `/home` 时，把 `ProtectHome=true` 改成 `read-only` 或删掉该行。
- `StartLimitIntervalSec` / `StartLimitBurst` 属于 `[Unit]` 段（见 `systemd.unit(5)`
  的 `[UNIT] SECTION OPTIONS`）。写在 `[Service]` 段会被 systemd 当作未知选项忽略并告警，
  重启频率限制静默退回默认值；示例文件里已经放在正确位置。

### 常用运维命令

```bash
sudo systemctl restart haut-seat-web     # 改了 .env 之后必须重启（见第 7 节）
sudo systemctl stop haut-seat-web        # 停机
sudo journalctl -u haut-seat-web -f      # 若改用 journal 收集输出时查看
tail -f logs/web.log                     # 按示例单元：systemd 收集的 stdout/stderr
tail -f logs/haut_seat.log               # 应用自身的日志文件（内容与上面基本重复）
```

**`stop` 的行为（别当成「干净收尾」）**：`SIGTERM` 交给 uvicorn 做优雅关闭，收尾时只停掉
调度线程（最多等 5 秒）；**正在执行的那一次运行不会被等待**，运行线程是守护线程，随进程退出
而结束。所以停机时若有运行在飞，会留下一条「运行中」记录，并在下次启动时被判为「结果中断」
（已发生过提交则判为「提交结果未知」，必须人工核实、绝不自动重放，见第 8 节）。
要避免这种情况，先暂停计划、等这次运行结束再停服务。

**日志轮转**：两个日志文件都不会自动轮转，示例配置见
[`deploy/logrotate-haut-seat.example`](../deploy/logrotate-haut-seat.example)。

**只暂停预约、不停服务**：

```bash
touch data/pause.flag     # 存在即暂停一切真实预约（只读查询不受影响）
rm data/pause.flag        # 恢复
```

或者登录面板 →「系统」页 → 关闭/打开「全局暂停」。两者独立生效，任一为「暂停」即不提交。

**和 cron 里的 CLI 共存**：`book` 命令与面板调度器共用同一把跨进程锁
（`HAUT_BOOK_LOCK_FILE`，默认 `data/book.lock`），不会同时提交。

### 远程浏览器登录（服务器没有桌面时用）

背景与设计见 [`docs/auth-recon.md`](auth-recon.md) 第 6 节。一句话：服务器端跑**无头** Chromium，
画面转发到面板页面，你用自己电脑的鼠标键盘完成登录（**图形验证码仍由你本人识别**，
程序不代填口令、不识别验证码）。

它**不需要**服务器有桌面，但需要三样东西：`playwright` 依赖、项目里的 Chromium、
以及单元里两个环境变量。**只在你确实要用这个功能时才做**；不做也不影响其它功能。

```bash
cd /opt/haut-seat-helper

# 1) Python 依赖（[auth] extra 早就存在，不新增任何第三方包）
sudo -u hautseat .venv/bin/python -m pip install -e ".[auth]"

# 2) 浏览器装进**项目目录**。这一步必须显式给出 PLAYWRIGHT_BROWSERS_PATH：
#    不给的话 playwright 会装到 $HOME/.cache/ms-playwright，而单元里读的是项目里的
#    .cache/ms-playwright —— 结果就是"装了也找不到浏览器"。装机路径与运行时变量必须逐字一致。
sudo -u hautseat env PLAYWRIGHT_BROWSERS_PATH=/opt/haut-seat-helper/.cache/ms-playwright \
  .venv/bin/python -m playwright install chromium

# 3) HOME 重定向的目标目录，必须**先建出来并归运行账号**：
#    单元里 ProtectHome=true 已让 /home、/root 变空，Chromium 的 node driver 又要在 HOME 下写临时文件；
#    而 ReadWritePaths 里不存在的路径会让单元拒绝启动。
sudo install -d -o hautseat -g hautseat -m 750 /opt/haut-seat-helper/data/home

# 4) 单元（deploy/haut-seat-web.service 里已经写好，逐行核对这三处）：
#    Environment=PLAYWRIGHT_BROWSERS_PATH=/opt/haut-seat-helper/.cache/ms-playwright
#    Environment=HOME=/opt/haut-seat-helper/data/home
#    RestrictNamespaces=~user pid     # 原来是 true，它会拒绝**全部** namespace
sudo cp deploy/haut-seat-web.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl restart haut-seat-web
```

`.cache/ms-playwright` 必须对运行账号**可读可执行**（按第 6 节开头的 `chmod -R a+rX` 就是可读的；
如果你事后单独装的，补一条 `sudo chmod -R a+rX /opt/haut-seat-helper/.cache`）。

**启动自检（不登录、不预约、不发任何提交请求）**：

```bash
sudo -u hautseat env HOME=/opt/haut-seat-helper/data/home \
  PLAYWRIGHT_BROWSERS_PATH=/opt/haut-seat-helper/.cache/ms-playwright \
  /opt/haut-seat-helper/.venv/bin/python -c \
  "from playwright.sync_api import sync_playwright as s; p=s().start(); \
   b=p.chromium.launch(headless=True); print('launch ok', b.version); b.close(); p.stop()"
```

看到 `launch ok ...` 才说明沙箱这关过了。**起不来时按这个顺序排查，不要先动沙箱**：

```bash
sudo journalctl -u haut-seat-web -n 80 --no-pager      # 服务侧的错误
sysctl user.max_user_namespaces                        # 0 = 内核关掉了非特权 user namespace
ls -l /proc/sys/kernel/unprivileged_userns_clone       # 老内核上的同名开关
df -h /dev/shm                                         # 太小也会崩（代码已用 --disable-dev-shm-usage 兜底）
systemd-analyze security haut-seat-web | head -20
```

**若沙箱确实起不来，正确结果是功能如实报错**（面板里显示原因、日志里有记录），
**不是**加 `--no-sandbox`（本项目里根本没有这个参数），也不是把 `RestrictNamespaces` 越改越松。

**关于 systemd 停机**：远程浏览器的工作线程是守护线程，`stop`/重启只做**有界等待**
（单元里 `TimeoutStopSec=30`）。某个会话正在跑时重启，Chromium 会被 systemd 按
`KillMode=control-group` 一并收走，不会留下孤儿进程；面板上那个会话随之变为"已结束"。

**面板上的做法**：`/admin/auth` 页有入口 → `/admin/auth/browser`。点「开始远程登录」，
等画面出现后在其中完成登录（含验证码）；成功后浏览器自动关闭，令牌存进
`data/auth/credentials.json`（全局快照），页面同时显示一次**只读**认证预检的结论。
若服务器 `.env` 里是 `HAUT_AUTH_SOURCE=env`，页面会提前提示"采集到的快照不会被使用"。

---

## 7. 反向代理与 HTTPS

示例：[`deploy/nginx-haut-seat.conf.example`](../deploy/nginx-haut-seat.conf.example)。

```bash
# .env
HAUT_WEB_SESSION_SECURE=true    # 必须改：HTTPS 下给会话 Cookie 加 Secure
HAUT_WEB_TRUST_PROXY=true       # 可选：由应用自己解析 X-Forwarded-For（见下）
```

```bash
sudo systemctl restart haut-seat-web
```

示例代理配置的要点：

- HTTP 301 → HTTPS，HSTS 一年。
- 先看 nginx 版本：示例用的是**全版本可用**的 `listen 443 ssl http2;`。
  `http2 on;` 这个指令 **1.25.1 才引入**，在 Debian 12（1.22.1）、Ubuntu 22.04（1.18）、
  Ubuntu 24.04（1.24）上会直接报 `unknown directive "http2"`，`nginx -t` 失败、配置加载不了；
  反过来在 1.25.1+ 上 `listen ... http2` 只是被标记弃用，仍可使用。用 `nginx -v` 确认。
- `proxy_set_header X-Forwarded-For` 必须传（示例用 `$proxy_add_x_forwarded_for`，
  即「客户端自带的值 + 真实来源」）。应用与 uvicorn 都取**最右一项**（代理追加的真实来源），
  所以客户端伪造的前缀不会生效。**任何情况下都不要相信这个头的最左项**。
- `X-Forwarded-Proto` 只是让 uvicorn 知道 scheme，**与 Secure Cookie 无关**：
  Cookie 是否带 `Secure` 完全由 `HAUT_WEB_SESSION_SECURE` 决定，应用不读这个头。
  也就是说这一项漏传不会让 Cookie 静默降级，但漏设 `HAUT_WEB_SESSION_SECURE=true` 会。
- `HAUT_WEB_TRUST_PROXY=true` 是**可选**的：`HAUT_WEB_HOST` 保持 `127.0.0.1` 时，
  uvicorn 默认只信任本机来源（`forwarded_allow_ips=127.0.0.1,::1`）并已正确解析该头，
  默认值 `false` 下记录的就已经是真实来源 IP，限流按真实 IP 生效（本机实测，见下）。
  开启 `true` 只是不再依赖 uvicorn 的解析，效果等价；**前提是服务不能被绕过代理直接访问**
  （`HAUT_WEB_HOST` 不要改成 `0.0.0.0`，也不要设置 `FORWARDED_ALLOW_IPS=*`，
  否则「最右一项」就成了客户端自己填的值，一样可伪造）。
- **不要**在 nginx 日志格式里加 `$http_authorization` 或 `$http_cookie`：
  那是完整凭据，会以明文长期留在磁盘上。默认格式不含它们，保持默认即可。
- 可选地再叠一层 HTTP Basic 认证（示例里已注释给出），前提是只在 HTTPS 下启用。
- 若服务器没有 IPv6，删掉示例里带 `[::]` 的两行 `listen`。
- **用宝塔面板时不要照抄示例里的路径**：示例按 Debian/Ubuntu 的
  `sites-available` + `sites-enabled` 写法给出；宝塔（含 OpenCloudOS 上的宝塔）
  把每个站点的 nginx 配置放在 `/www/server/panel/vhost/nginx/<域名>.conf`。
  正确做法是在面板「网站」里建站并申请证书，再通过面板的「配置文件」编辑界面
  把示例中 `location / { … }` 里的 `proxy_pass` 与 `proxy_set_header` 几行贴进去，
  最后用面板的「重载配置」或 `nginx -t && systemctl reload nginx` 生效。
  不要手工去改宝塔生成的 `nginx.conf` 主文件 —— 面板会覆盖它。

---

## 8. 备份与恢复

要备份的东西只有四样：**数据库**、**`.env`**、**`config/` 下的本地配置**，
以及**每用户凭据的加密主密钥 `data/auth/master.key`**（见第 3 节末尾：
丢了它，所有用户都得重新绑定）。日志可以不备（`logs/` 里没有凭据，但也没有恢复价值）。

### 备份

运行时用 SQLite 的在线备份命令，不要在数据库被写入时直接 `cp`：

```bash
mkdir -p backups
sqlite3 data/web.db ".backup 'backups/web-$(date +%F).db'"
```

没有 `sqlite3` 命令行时，用 Python 标准库（同样是在线备份，安全）：

```bash
.venv/bin/python - <<'PY'
import sqlite3, datetime, pathlib
src = "data/web.db"
dst = pathlib.Path("backups") / f"web-{datetime.date.today()}.db"
pathlib.Path("backups").mkdir(exist_ok=True)
with sqlite3.connect(src) as s, sqlite3.connect(dst) as d:
    s.backup(d)
print("已备份到", dst)
PY
```

`.env` 与本地配置：

```bash
tar czf backups/config-$(date +%F).tar.gz .env config/seats.json config/booking.json
```

主密钥（`data/auth/master.key` 在首次有人绑定图书馆账号之后才存在；
没配 `HAUT_AUTH_CRED_KEY` / `HAUT_CRED_KEY` 时它才是凭据的唯一钥源）：

```bash
mkdir -p backups
[ -f data/auth/master.key ] && cp -p data/auth/master.key backups/master-key-$(date +%F).key
```

> `backups/` 本身**已经**被 `.gitignore` 排除（见该文件「运行产物」一节），
> 但这不足以当保障：数据库里有管理员口令哈希与会话摘要，`.env` 里有图书馆凭据。
> 更稳妥的做法是把备份目录放到**仓库之外**，并限制权限（`chmod 600 backups/*`），
> 同时确认备份路径没有被提交。

### 恢复

```bash
sudo systemctl stop haut-seat-web        # 1. 先停服务，避免恢复期间被写入

cp backups/web-2026-10-16.db data/web.db # 2. 覆盖数据库（WAL 文件一并清掉）
rm -f data/web.db-wal data/web.db-shm

tar xzf backups/config-2026-10-16.tar.gz # 3. 还原 .env 与本地配置

sudo systemctl start haut-seat-web       # 4. 启动
```

恢复后核对：

- 能登录面板，且「系统」页的管理员数量正确；
- 「运行记录」里历史记录都在；
- 「认证状态」不是异常的「失效」（必要时点「重新检测登录状态」）。

### 重启后的状态恢复（不需要手工干预）

服务重启时，库里仍标着「运行中」的运行记录会被标记为 **`interrupted`（结果未知）**，
并**不会**被自动重放 —— 因为服务重启时无法确认上一次提交到底成功了没有。
这类记录需要在计划详情页确认后手工处理（「已确认未知结果」），之后计划才能再次执行。

「提交结果未知」（`unknown`）与「预约失败」（`failed`）是**两种不同状态**，
界面上的文案与颜色也不同，不会把未知当成普通失败。

---

## 9. 运行记录与日志

- 每次执行都会创建一条「运行记录」，分阶段写入事件：
  `start` → `auth_check` → `wait` → `query` → `select` → `submit` → `end`，
  异常再记一条 `exception`。
- 每条运行记录带：开始/结束时间、计划、触发方式（调度器 / 手动）、状态、
  错误分类、简要描述、查询次数、提交次数、换座次数。
- 日志与页面展示都经过脱敏：**不会**出现 Cookie、Authorization、JWT、
  口令或 SMTP 口令。
- 清理：面板「系统」页 →「维护」→ 清理过期会话、旧登录记录和超出
  `HAUT_SCHEDULER_RUN_LOG_KEEP_DAYS` 的运行日志。

### 日志文件在哪里、会不会无限增长

按示例单元部署时会有**两份**内容基本重复的日志：

| 文件 | 写入者 | 内容 |
|---|---|---|
| `logs/web.log` | systemd（`StandardOutput=append:`） | 应用 stdout/stderr，含 uvicorn 的访问日志与启动横幅 |
| `logs/haut_seat.log` | 应用自身（`HAUT_LOG_FILE`） | 同一批日志的副本，格式带时间戳与 logger 名 |

这两个文件**都不自动轮转**：应用用的是 `logging.FileHandler`，systemd 的 `append:` 也只是
持续追加。长期运行必须自己配轮转，示例见
[`deploy/logrotate-haut-seat.example`](../deploy/logrotate-haut-seat.example)
（用 `copytruncate`，因为两个写入者都以 append 模式打开文件且不会重新打开）。
「系统」页的「维护」按钮只管**数据库里**的运行记录，不会动这两个文件。

两份日志都已经过脱敏，不含 Cookie、Authorization、JWT、口令或 SMTP 口令。

---

## 10. 手动配置检查清单（首次部署逐项确认）

- [ ] `.env` 由 `.env.example` 复制而来，且**没有**被提交到版本库（`.gitignore` 已排除）
- [ ] `.env` 权限 `600`、属主是服务运行用户（里面有图书馆 Cookie / Authorization）
- [ ] `HAUT_WEB_HOST` 保持 `127.0.0.1`（或确认前面的反向代理会拦住外部访问）
- [ ] 运行账号已创建（`useradd --system`），`data/` 与 `logs/` 属主是该账号
- [ ] 代码**没有**放在 `/home` 或 `/root` 下（否则要同时改 `ProtectHome`）
- [ ] `systemctl status haut-seat-web` 是 `active (running)`，且 `ExecStartPre` 没有报错
- [ ] 已经建好管理员（`create-admin` 或首次运行引导），且口令 ≥10 位
- [ ] 生产环境下把 `HAUT_WEB_ALLOW_FIRST_RUN_SETUP` 改为 `false` 并重启
- [ ] `HAUT_WEB_SESSION_SECURE`：走 HTTPS 置 `true`；**若只提供 HTTP 就必须置 `false`**，
      否则浏览器不会回传会话 Cookie，表现为「登录成功却一直回到登录页」（见第 12 节）
- [ ] 启动横幅（`grep "会话 Cookie" /opt/haut-seat-helper/logs/web.log | tail -n 3`，
      示例单元写文件不是 journal，见第 9 节）与你的实际访问协议一致
- [ ] `HAUT_WEB_TRUST_PROXY`：保持 `false` 即可；若要置 `true`，先确认服务无法被绕过代理直连
- [ ] 改完这些项后**重启过服务**（`.env` 只在进程启动时读取）
- [ ] `nginx -v` 确认版本与 `http2` 写法匹配（<1.25.1 用 `listen 443 ssl http2;`），
      并已通过 `sudo nginx -t`
- [ ] 已用浏览器实测：登录页走 HTTPS，响应头里有 HSTS，登录后 Cookie 带 `Secure`
- [ ] nginx 日志格式**没有**包含 `$http_authorization` / `$http_cookie`
- [ ] 日志轮转已配置（`deploy/logrotate-haut-seat.example`），
      且 `sudo logrotate -d /etc/logrotate.d/haut-seat` 试运行无报错
- [ ] `HAUT_ENABLE_REAL_BOOKING` 仍是 `false`（除非你已确认学校规则允许自动化）
- [ ] 「系统」页的「全局暂停」符合你的预期
- [ ] `data/` 与 `logs/` 目录属于服务运行用户，且权限不含其他用户
- [ ] 数据库备份已配置，且备份目录不在仓库内、权限受限
- [ ] 已确认「认证机制」一节：令牌无 `exp`，**没有**可靠续期，失效时需人工更新
- [ ] 域名/证书到期时间已加入提醒

---

## 11. 需要人工介入的场合

| 情形 | 界面表现 | 你要做什么 |
|---|---|---|
| 图书馆凭据失效 | 认证状态 = 「认证失效」，「运行」按钮被拒绝 | 浏览器正常登录 → F12 复制 `Authorization` 与 `Cookie` → 更新 `.env` → **重启服务** → 回认证页点「重新检测登录状态」 |
| 认证状态无法确认 | 认证状态 = 「无法确认」 | 通常是网络/服务端抖动，**不是**掉登录。稍后重新检测即可 |
| 提交结果未知 | 运行/计划状态 = 「提交结果未知」，明确提示不会自动重放 | 去学校预约记录页确认是否已约上，再在计划详情页「确认未知结果」并决定下一步 |
| 服务重启打断了运行 | 运行状态 = 「结果未知（服务重启）」 | 同上，先人工确认 |
| 需要立即停手 | 「系统」页打开「全局暂停」，或 `touch data/pause.flag` | 恢复时再关掉 |
| 收到「已保存但未发送」的通知设置提示 | 通知页明确写「邮件发送尚未实现」 | 这是如实告知：SMTP 属于后续阶段，当前不会真的发信 |
| 登录成功却一直回到登录页 | 日志里有「管理员 … 登录成功」，浏览器却停在 `/admin/login` | 按**第 12 节**的顺序排查：多半是会话 Cookie 带 `Secure` 而你在用 `http://` 访问 |

---

## 12. 登录后又回到登录页：按顺序排查

**先分清两件事**：服务端日志里出现「管理员 X 登录成功（来源 I.P）」= 口令校验通过、
会话已写入、`Set-Cookie` 已下发。此后每次访问仍被弹回登录页，只可能是：

- **浏览器没有把 Cookie 送回来**（会话 Cookie 带 `Secure` 而你在用 `http://` 访问），或
- **服务连的不是同一个数据库**（相对路径 `data/web.db` 按进程工作目录解析）。

两者症状一模一样，用下面 4 步区分。**不要凭猜**——每一步都有可核对的输出。

### 第 1 步：服务端到底认没认这次登录

```bash
# 日志去向由 systemd 单元决定：示例单元用的是 `StandardOutput=append:`（写文件，
# 见第 9 节），此时 **journal 里什么都没有**，直接 journalctl 会得到空结果。
grep -E "登录成功|登录尝试过于频繁" /opt/haut-seat-helper/logs/haut_seat.log | tail -n 20
grep -E "登录成功|登录尝试过于频繁" /opt/haut-seat-helper/logs/web.log       | tail -n 20

# 仅当你把单元改成了 journal 收集（删掉 StandardOutput= / StandardError= 那两行）：
journalctl -u haut-seat-web -n 200 --no-pager | grep -E "登录成功|登录尝试过于频繁"
```

- 出现「管理员 … 登录成功」→ 服务端是好的，继续第 2 步。
- 只出现「登录尝试过于频繁」→ 被限流了（见第 4 步末尾）。
- **两条命令都是空**、且 `systemctl status haut-seat-web` 正常 → 先确认单元里的
  `StandardOutput=` 指向哪个文件，别把「找错日志文件」当成「服务端没收到登录」。

### 第 2 步：看启动横幅里的会话 Cookie 策略

```bash
# 启动横幅走 stdout → 按示例单元落在 logs/web.log（第 9 节）。
# 该文件跨重启累积，取**最后**一条才是当前进程正在用的策略。
grep "会话 Cookie" /opt/haut-seat-helper/logs/web.log | tail -n 3

# 仅当你改用 journal 收集输出时：
journalctl -u haut-seat-web --no-pager | grep "会话 Cookie" | tail -n 3
```

- `会话 Cookie: Secure（浏览器只在 HTTPS 下才会回传；用 http:// 访问会一直回到登录页）`
  → 如果你当前地址栏是 `http://…`，**这就是根因**，修法见本节末尾。
- `会话 Cookie: 不带 Secure` → 不是这一项，跳到第 3 步。
- **完全没有这一行** → 这个进程是在该横幅加入之前启动的（或 `StandardOutput=` 指向别处）。
  先 `systemctl restart haut-seat-web` 再看一次，不要在旧日志上作判断。

### 第 3 步：直接看响应头里有没有 `Secure`，以及浏览器存没存

在**服务器本机**执行（会带上真实口令，别把输出贴到别处）：

```bash
curl -sS -i -X POST http://127.0.0.1:8080/admin/login \
     -d 'username=你的用户名&password=你的口令' | grep -i '^set-cookie'
```

浏览器侧（Chrome / Edge）：F12 → Application → Cookies → 你的域名 → 找 `hs_session`。

| 看到的现象 | 结论 |
|---|---|
| 有 `Secure`，地址栏却是 `http://` | 浏览器不会回传它 → 走本节末尾的修法 |
| 列表里根本没有 `hs_session` | 浏览器丢掉了它（协议不匹配，或被反向代理改写/剥离） |
| 有 `hs_session`，但访问 `/admin` 仍被弹回 | 会话没被服务端认出来 → 第 4 步 |

### 第 4 步：确认「只有一个进程、只有一份数据库」

```bash
systemctl status haut-seat-web --no-pager | head -n 5
ps -ef | grep "[h]aut_seat web"          # 应当只有一行

# 横幅里「数据库:」那一行写的路径就是服务真正在用的库
sqlite3 /opt/haut-seat-helper/data/web.db \
  "SELECT count(*) FROM admin_user; SELECT count(*) FROM session;"

sqlite3 /opt/haut-seat-helper/data/web.db \
  "SELECT substr(token_hash,1,12), admin_id, created_at, expires_at, last_seen_at
     FROM session ORDER BY created_at DESC LIMIT 3;"
```

- `session` 是空的，但你刚刚登录成功过 → 服务写的**不是这个库**：
  相对路径 `data/web.db` 相对于**进程工作目录**解析，
  检查 systemd 的 `WorkingDirectory` 与你查看的路径是否一致。
  另外确认没有第二个实例（例如宝塔的「Python 项目管理器」和 systemd 同时起了一份，
  两份工作目录不同 → 两个库）。
- 有行、且 `expires_at` 在未来 → 会话是好的，说明问题回到第 2/3 步。

> `sqlite3` 会读到 WAL 里的最新数据；面板类工具（宝塔文件管理器的"在线查看"）
> 未必读 WAL，别用它下结论。

### 修法（改完必须重启，`.env` 只在进程启动时读取）

| 情况 | 修法 |
|---|---|
| 走 HTTPS 访问（推荐） | 配好证书、用 `https://域名` 打开，保持 `HAUT_WEB_SESSION_SECURE=true` |
| 确实只能用 HTTP（内网 / 临时排查） | `.env` 改 `HAUT_WEB_SESSION_SECURE=false`，`systemctl restart haut-seat-web`；此时**不得**对公网开放 |
| 反向代理信任配置 | 保持 `HAUT_WEB_TRUST_PROXY=false`（默认即可，见第 7 节），**不要**设 `FORWARDED_ALLOW_IPS=*` |

```bash
systemctl restart haut-seat-web
grep "会话 Cookie" /opt/haut-seat-helper/logs/web.log | tail -n 3   # 复核新策略（取最后一条）
```

**不要**为了让登录"过"就把 `Secure` 关掉却继续对公网开放 HTTP ——
那等于让管理员会话 Cookie 走明文传输，中途截获即等于拿到会话。

> **用户端 `/booking` 同理，但表不同。** 用户端有自己的一套会话 Cookie
> （`hs_user`，`path=/booking`，`SameSite=Lax` + HttpOnly，同样受
> `HAUT_WEB_SESSION_SECURE` 控制），排查时要把 `session` 换成 `user_session`
> 并去掉 `admin_id` 那一列：
>
> ```bash
> sqlite3 data/web.db "SELECT substr(token_hash,1,12), user_id, created_at, expires_at
>    FROM user_session ORDER BY created_at DESC LIMIT 3;"
> ```
>
> 「用户明明登录了却一直回到 `/booking/login`」的成因与修法和上面完全一样。
> 另外 `user_session` 有两个独立的时限（绝对有效期
> `HAUT_USER_SESSION_TTL_HOURS` 默认 72 小时、空闲超时
> `HAUT_USER_SESSION_IDLE_MINUTES` 默认 240 分钟），任一超时都会要求重新登录 ——
> 这正是"用户端不用像管理员那样频繁被踢"与"手机丢在一边几天后仍然安全"之间的
> 折中。

---

## 13. 已知限制

- **`.env` 改动需要重启服务**：配置在进程启动时读入，
  认证页的「重新检测登录状态」用的是**进程内已有的凭据**。
- **前端只有一个外部资源：Bootstrap 的样式表**（`cdn.jsdelivr.net`）。
  脚本一律本地：页面的 CSP 是 `script-src 'self'`，**不加载** Bootstrap 的 JS 包，
  导航折叠与提示关闭由本地 `app.js` 原生实现（这也是为了安全，见下）。
  如果服务器所在网络访问不到 jsdelivr（国内时有发生），页面会退化成无样式 HTML ——
  链接仍然可见可点，功能不受影响。想彻底去掉这个依赖：

  ```bash
  curl -o haut_seat/web/static/bootstrap.min.css \
       https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css
  ```

  然后改 `haut_seat/web/templates/base.html` 里的那行 `<link>` 为
  `{{ url_for('static', path='bootstrap.min.css') }}`，
  并从 `haut_seat/web/app.py` 的 CSP 中删掉 `https://cdn.jsdelivr.net`。
  `tests/test_web_static.py` 会提示你这两处需要一起改。
- **模板与静态资源目前随源码目录分发**：`pyproject.toml` 未声明包数据，
  因此请用 `pip install -e .`（可编辑安装）或直接从源码目录运行。
- **单文件 SQLite**：够个人使用，但不适合多实例横向扩展；
  不要同时跑两个指向同一 `web.db` 的面板实例。
- **调度器线程在进程内**：面板进程停掉，调度也就停了。
  如果希望预约不受面板重启影响，可以继续用 cron + `python -m haut_seat book`，
  两者共用同一把跨进程锁，不会冲突。
- **邮件通知未实现**：通知页可以保存设置，但 `_deliver()` 一律
  `raise NotImplementedError`，所有事件都记成 `skipped（未实现）`，**不会发出任何邮件**。
  实现方案（含传输模式、口令读取、线程模型与安全要求）见
  [`docs/notification-smtp-plan.md`](notification-smtp-plan.md)。
- **多用户下几件必须知道的事**（详见 README 的「多用户」一节）：
  - `HAUT_ENABLE_REAL_BOOKING` 是**总开关**，不是"每人一个开关"：它为 `true` 时
    **所有**用户的计划都会真实提交。用户端刻意不提供"立即执行"，也没有自己的真实开关。
  - 用户凭据到期后**必须由本人重新绑定**：没有 `exp`、没有 refresh 接口，
    客户端做不了自动续期（理由见 README「为什么不做自动续期」）。
  - 活跃用户数超过 `HAUT_HTTP_CLIENT_CACHE_MAX`（默认 8）时，出站 HTTP 客户端会按
    LRU 淘汰并重建（连接池随之重建）。人多了就调大这个值。
  - **窗口期依赖用户在线**：用户端会话（默认 72 小时）过期后计划仍会执行
    （凭据是独立保存的），但你无法在 `/booking` 上看到状态，直到重新登录。

---

## 附录：第一阶段新增 / 修改的文件

### 新增

**Web 层** `haut_seat/web/`

| 文件 | 作用 |
|---|---|
| `app.py` | FastAPI 应用装配、安全响应头、会话 Cookie 策略、首次引导令牌 |
| `db.py` | SQLite 访问层：每线程连接、WAL、`tx()` 事务、建表 |
| `security.py` | scrypt 口令哈希、会话、CSRF、登录限流、密码强度校验 |
| `validation.py` | 服务端表单校验（日期 / 时间 / 区域 / ID / 重复规则） |
| `repository.py` | 计划、运行、事件、认证状态、通知、应用设置的数据访问 |
| `deps.py` | 依赖注入、鉴权、CSRF 校验、模板上下文、客户端 IP |
| `routes.py` | 全部路由 |
| `templates/*.html` | 15 个模板：`base` / `_macros` / `login` / `setup` / `dashboard` / `plans` / `plan_form` / `plan_detail` / `runs` / `run_detail` / `auth` / `notifications` / `system` / `error` |
| `static/app.css` `static/app.js` | 本地样式与脚本（含导航折叠等原生实现，**不依赖外部脚本**） |

**服务层** `haut_seat/services/`（把原 `__main__.py` 里的预约流程抽出来，供 CLI 与 Web 共用）

| 文件 | 作用 |
|---|---|
| `state.py` | 计划/运行状态机、阶段、错误分类与中文标签 |
| `io.py` | 真实网络 IO 适配（查询 / 提交 / 预检） |
| `booking_runner.py` | 预约主循环（认证预检 → 等待 → 查询 → 选座 → 提交） |
| `scheduler.py` | 后台调度器：持久状态、任务互斥、重启恢复 |
| `auth_service.py` | 认证三态状态与**人工**恢复流程 |
| `notification_service.py` | 通知接口与设置模型（**SMTP 未实现**） |

**测试** `tests/`

`test_web_app.py`（HTTP 层）、`test_web_db.py`、`test_web_security.py`、
`test_web_state.py`、`test_web_validation.py`、`test_web_repository.py`、
`test_web_static.py`、`test_scheduler.py`、`test_booking_runner.py`、
`test_notification_service.py`、`test_cli_web_commands.py`

**部署与文档**

`deploy/haut-seat-web.service`、`deploy/nginx-haut-seat.conf.example`、
`deploy/logrotate-haut-seat.example`、`docs/web-deploy.md`、`docs/auth-recon.md`

### 修改

| 文件 | 改动 |
|---|---|
| `haut_seat/__main__.py` | 新增 `web` / `init-db` / `create-admin` 子命令；预约流程改为调用 `services/` 层 |
| `haut_seat/config.py` | 新增 Web 与调度器相关配置项 |
| `tests/conftest.py` | 离线夹具：网络 IO 与提交一律用替身，提交替身被调用即抛错 |
| `.env.example` | 新增「Web 管理面板」一节 |
| `.gitignore` | 排除 `backups/`、`.recon-tmp/` |
| `README.md` | Web 面板章节、部署形态、认证侦查结论 |
| `haut_seat/web/templates/base.html`、`static/app.js`、`static/app.css`、`web/app.py` | 自查修复：去掉被自身 CSP 拦掉的 Bootstrap JS 外链，改为本地原生实现 |

### 明确**未**改动（本次约束要求保留）

`haut_seat/core/auth.py`、`core/booking.py`、`core/guard.py`、`core/lock.py`、
`core/priority.py`、`core/schedule.py`、`client/http.py`、`client/seatbook.py`、
`models/*`、`sanitize.py`、`logging_conf.py`。

预约核心逻辑是**被服务层调用**的，没有被复制或重写：
`tests/test_auth.py`、`test_booking.py`、`test_guard.py`、`test_lock.py`、
`test_priority.py`、`test_schedule.py`、`test_seatbook.py` 等既有测试全部保持通过。

### 部署准备轮次的改动（本次）

全部集中在部署配置与被污染的限流键上，**没有**触碰预约核心。

| 文件 | 改动 | 原因 |
|---|---|---|
| `haut_seat/web/deps.py` | `client_ip` 取 `X-Forwarded-For` 的**最右一项**（原为最左项） | 最左项是客户端自填的，可用来绕过按 IP 的登录锁定 |
| `tests/test_web_app.py` | 新增 3 个测试覆盖伪造 XFF 与最右项解析 | 让上面的行为可回归 |
| `deploy/haut-seat-web.service` | `StartLimit*` 移入 `[Unit]`；补服务账号 / `ReadWritePaths` / `ProtectHome` / 停机语义注释 | 放在 `[Service]` 会被 systemd 静默忽略；其余是易踩的坑 |
| `deploy/nginx-haut-seat.conf.example` | `http2 on;` → `listen 443 ssl http2;`；改写 `.env` 指导与 XFF / XFP 注释 | `http2 on;` 需要 nginx ≥ 1.25.1（Debian 12 只有 1.22） |
| `deploy/logrotate-haut-seat.example` | **新增** | 此前两份日志都没有轮转 |
| `.env.example` | 修正 `HAUT_WEB_SESSION_SECURE` / `HAUT_WEB_TRUST_PROXY` 注释 | 前者是静态开关，后者默认可不开 |
| `README.md` | 更正 HTTPS Cookie、TRUST_PROXY 说明与目录树 | 原文称「HTTPS 下 Cookie 自动带 Secure」，与实现不符 |
| `docs/web-deploy.md` | 第 2、6、7、8、9、10 节与本节 | 与代码和单元文件对齐 |