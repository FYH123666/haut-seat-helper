# 认证侦查笔记（自动登录可行性）

> **状态：侦查结论已落地为「浏览器辅助的人工登录」；账号口令自动登录仍然不实现。**
> 本文记录**观测到的事实**与**明确的未知项**。§5 是 2026-10-18 落地实现时**基于本文事实**
> 做出的取舍与它的限制；**事实部分（§1、§2）没有变化，仍是本轮及以后的设计依据**。
> 程序按 README「认证机制与登录有效期」一节工作：凭据来源为「浏览器会话快照」或 `.env`，
> 失效时由**人**重新登录一次（弹窗登录或手工复制），本文存在的价值是：
> **下次有人想「自动刷新令牌」时，不必再重新侦查一遍，也不会把推测当成结论。**
>
> 侦查方式：静态读取前端 JS 与只读 GET 请求，**没有执行过任何一次真实登录**，
> 没有提交任何预约，没有尝试识别或绕过验证码。**这一条至今未变** ——
> 落地实现同样不含任何验证码处理，登录动作始终由人完成。

## 结论摘要

| 问题 | 结论 |
|---|---|
| 登录流程 | 前端 SPA 直接 `POST` 到 `/stage-api/login`（或同形的 `/login4web2HGD`），参数 `{username, password, code, uuid}` |
| 验证码类型 | **图片验证码**（Kaptcha 风格，JPEG，base64 内联），**不是**滑块、**不是**短信 |
| 验证码出现条件 | **未知**（未实测）。已知登录接口**要求** `code` + `uuid` 两个字段 |
| JWT 获取机制 | 登录响应体的 `token` 字段；前端写进 cookie `Admin-Token` |
| `Authorization` 从何而来 | **可由 cookie 推导**：前端请求拦截器写 `"Bearer " + getToken()`，而 `getToken()` 读的就是 cookie `Admin-Token` |
| 是否存在可靠续期 | **未发现任何续期机制**（见下），也没有证据表明存在服务端滑动续期 |
| 浏览器持久化会话是否可行 | **可行，但不足以持久化凭据**：`Admin-Token` 是**会话级 cookie**，不会落进持久化 profile 的 cookie 库 ⇒ 只能"打开可见浏览器由人登录 + **显式取出令牌另存**"（见 §5） |
| 推荐下一步 | 「浏览器辅助的人工登录」**已实现**（人工环节保留）；**账号口令自动登录仍不实现** |

---

## 1. 已确认的事实（有观测证据）

### 站点形态

- `GET https://wslib.haut.edu.cn/` → HTTP 200，页面标题「智慧空间综合平台」，
  是一个 **RuoYi-Vue 风格的 SPA**；未知路径一律回落到同一个 `index.html`。
- API 前缀为 **`/stage-api`**（前端 axios 的 `baseURL`）。
- 前端 JS 中**没有** `oauth` / `redirect_uri` / `client_id` / `单点登录` 等字符串。
  也就是说：**侦查过程中没有观测到「跳转到学校统一身份认证页面」这一跳转**。
  官方说明页要求「使用学校统一身份认证系统的用户名和密码登录」——
  目前只能推断统一身份认证是**服务端侧**集成的（后端代为校验，或门户带着 token 跳入），
  **这是推断，未经证实。**

### 验证码

- 端点：**`GET /stage-api/captchaImage`**，**无需登录即可调用**，可随时按需调用，
  每次返回新的 `uuid` 与图片。
- 响应形如：

  ```json
  {"code": 200, "msg": "成功",
   "img": "/9j/4AAQSkZJRg...（base64 JPEG）",
   "uuid": "<32 位十六进制>"}
  ```

- `img` 解码后是 **JPEG**（magic `\xff\xd8\xff\xe0`），人工看图判定为
  **字符/算式类图片验证码**（蓝字浅绿底），符合 RuoYi 常见的 Kaptcha 数学型。
  **答案约定（填字面字符还是填算式结果）没有确认**。
- 响应里**没有** RuoYi 常见的 `captchaEnabled` 字段。

### 登录接口

- 前端定义的登录相关接口：`/login`、`/login4web2HGD`、`/logout`、`/getInfo`、
  `/getRouters`、`/captchaImage`、`/repassword`。
- 前端 action：
  - `Login` → `{username, password, code, uuid}` → `POST /stage-api/login`
  - `login4web2HGD` → 同名参数 → `POST /stage-api/login4web2HGD`
  - `ssoLogin` → 额外带一个 `ssotoken` 字段（来源未查明）
- 前端路由白名单里存在多个登录入口：`/login4web2HGD`、`/login2`、`/hzdlogin`、
  `/login`、`/auth-redirect` 等。**站点默认走哪一个，未确定。**
- **没有观测到任何 prelogin / 公钥 / 密码加密步骤**：检索 `prelogin`、`publicKey`、
  `getKey`、`encrypt`、`rsa`、`aes` 均无命中。所以「密码是否需要加密后提交」这一点，
  **目前没有证据表明需要**，但也没有实测证明不需要。

### 令牌

- 令牌来自**登录响应体的 `token` 字段**，前端把它写进 cookie，键名 **`Admin-Token`**。
  登出时清空。
- 对既有真实令牌的解码结果：载荷只有 `{"login_user_key": "<uuid>"}`
  —— **没有 `exp`、没有 `iat`、没有任何刷新标记**。
- `Authorization` 头与 `Admin-Token` cookie 里是**同一个 JWT**。
- **`Authorization` 的生成方式（已确认，非推断）**：站点自己的 JS 请求拦截器里有
  `e.headers["Authorization"] = "Bearer " + getToken()`，而 `getToken()` 读的正是
  cookie `Admin-Token`。旁证：用户 `.env` 里两行只差 `Bearer ` 与 `Admin-Token=` 前缀，
  去掉前缀后两串**逐字节一致**（用 sha256 比对确认过，摘要值不在此处记录）。
  ⇒ **`Authorization == "Bearer " + <Admin-Token 的值>`，这就是它唯一的来源。**
- **`Admin-Token` 是会话级 cookie**：站点 JS 里对该 cookie 的唯一写入是 js-cookie 的
  **两参**调用 `set(a, e)`（第三参 options 缺省），全文检索 `expires` / `max-age` /
  `SameSite` / `secure` **零命中** ⇒ 它是 session cookie，**不会被写进持久化
  browser profile 的 cookie 库**（那正是 §5 必须"显式取值另存"的原因）。

### 续期

- 前端定义的全部接口里**没有任何刷新端点**。
- 实测响应头**没有 `Set-Cookie`**，响应体里也没有 refresh token 字段。
- 令牌自身没有 `exp`。

### 旁证：网关行为

- 对 `/static/js/*` 的**突发 / keep-alive** 请求会被网关软封：仍然返回 **HTTP 200**，
  但 `content-type` 变成 `text/html`，内容是 SPA 回落页而不是 JS。
  改成单发请求、加停顿即可正常取到。
- 这条本身不是登录问题，但**说明「HTTP 200」在这个站点上不能当作成功的判据**，
  与 README 里「认证失效时 HTTP 仍然是 200」的结论一致。

---

## 2. 仍然未知（不得当作事实）

| 事项 | 状态 |
|---|---|
| 是否真的发生「跳转学校统一身份认证页」 | **未观测到**；服务端侧集成只是推断 |
| 生产默认登录路径是 `/login` 还是 `/login4web2HGD` | **未确定** |
| 验证码的出现条件（始终必需 / 失败 N 次后 / 仅新设备新 IP） | **未实测**；「始终必需」只是由「接口要求 code+uuid」推断而来 |
| 验证码答案约定（字面 vs 算式结果） | **未确认** |
| 是否存在短信 / 扫码 / 人脸等登录环节的二次验证 | **未确认**（前端无相关字符串，登录页 chunk 未能取到） |
| `ssotoken` 从何而来、`/ssologin` 的真实 URL | **未知** |
| 是否存在服务端滑动续期、会话有效期具体多久 | **未证实** |
| 前端是否会自动附加 `Authorization: Bearer` 头 | **已确认会**，且值就是 `"Bearer " + cookie.Admin-Token`（§1「令牌」末两条）⇒ 本程序按这条**已被证明的推导关系**构造该头，**不靠观测实时请求头** |
| 令牌是否使用 localStorage | **未知**（已确证的是存在 cookie `Admin-Token`；但这条未知**不影响**实现——见 §1「令牌」末条） |
| Playwright 持久化会话是否可行 | **有结论**：可行，但**不足以持久化凭据**（`Admin-Token` 是 session cookie）⇒ 只能"人工登录 + 显式取出令牌另存"，见 §5 |

---

## 3. 为什么仍然不实现「账号口令自动登录」（本条至今成立）

1. **验证码确实存在，且出现条件不明。** 登录接口要求 `code` + `uuid`，
   而识别验证码属于「绕过验证码」，明确在本项目范围之外。
   人工看图输入意味着自动登录**不可能无人值守**，也就无法解决「半夜令牌失效」这件事。
2. **自动登录即使成功，也解决不了会话本身失效的问题**：令牌无 `exp`、无刷新接口，
   续期机制是否存在也无证据。拿密码去换一个新令牌，等于把长期凭据放到服务器上，
   换来的只是「重新开始一段长度未知的会话」。
3. **账号密码落盘是长期风险**：一旦落盘，泄露面从「一段会失效的令牌」变成「学号+密码」。
4. **多个登录入口 + 未确认的加密/SSO 分支**意味着任何自动化都建立在一堆未验证的假设上，
   与项目「不猜测接口、参数或返回字段」的原则冲突。

## 4. 如果以后要推进，建议的顺序

每步都必须**先取得证据再写代码**，且任何一步都不允许尝试自动识别验证码：

1. **先确认默认登录路径与验证码是否可空**：用浏览器正常走到登录页，观察
   实际发出的请求（F12 → Network），确认走的是 `/login` 还是 `/login4web2HGD`，
   以及「不填验证码能否提交」。
2. **确认 `code` 的答案约定**：人工看图，对比「填图上字符」与「填算式结果」哪个被接受。
3. **浏览器持久化会话（Playwright 等）—— 已实现（含限制），见 §5**：只做「打开可见浏览器 →
   人工登录 → 从会话里取出令牌存成受限快照」这一件事。它把「手工 F12 复制」变成「点一下
   + 图里输码」，属于体验改进，**不触碰验证码**，是风险最低的一步。
   实现中暴露的两个限制（§1「令牌」两处证据的必然结果）：① 只能用**可见**窗口
   （headless 无人登录毫无意义）；② 持久化 profile **不能**充当凭据来源，
   令牌必须**显式取出另存**。
4. **只有在①②③都确认之后**，才考虑是否值得继续；并且必须明确：
   进程不得保存账号密码，人工环节不可省略，失败时一律按 README 的三态判定
   （`valid` / `invalid` / `indeterminate`）处理，绝不把网络故障误判成掉登录。

> 上述任一步骤都不得写入密码、JWT 或完整 Cookie 到源码、日志、测试或页面。

## 5. 落地实现（2026-10-18）：浏览器辅助的人工登录

**只做了 §4 的第 3 步**，且严格按 §1 已证明的事实来设计；§4 的第 1、2 步
（默认登录路径、验证码答案约定）**仍未确认，也没有去做** —— 实现不依赖它们：
程序只打开站点根 `https://wslib.haut.edu.cn/`，由 SPA 自己跳转到登录页（§1「站点形态」），
**不猜任何登录路径，也不碰 `code`/`uuid`/`ssotoken` 任何一个参数**。

| 环节 | 落到哪里 | 依据 |
|---|---|---|
| 打开**可见** Edge（复用系统安装的浏览器，不下载） | `haut_seat/services/auth_browser.py` | 真需求是"人工必须**看到**登录页"；在**有桌面**的机器上最省事的做法是 `headless=False` + `channel="msedge"`。**没有桌面的服务器上这条路走不通**，改用 §6 的画面转发（同样由人本人看，只是画面走网络而不走显示器） |
| 轮询取出 `Admin-Token` 的值 | 同上，`ctx.cookies(base_url)` 过滤 `name == "Admin-Token"` | §1「令牌」末条：session cookie **不在**持久化 profile 的 cookie 库里 ⇒ 必须显式取值 |
| 由该值构造 `Authorization` | 同上：`f"Bearer {token}"` | §1「令牌」末两条：拦截器 `"Bearer " + getToken()`，且 `.env` 两值去前缀后逐字节一致 |
| 存成受限快照 | `haut_seat/services/auth_store.py` → `data/auth/credentials.json` | 字段刻意最小（无账号/无口令/无验证码/无 `exp`——本来就没有）；原子替换 + `chmod 0600` |
| 按来源取用 | `resolve_credentials()`，由 `HAUT_AUTH_SOURCE=auto\|browser\|env` 决定 | `auto` 的"较新"用真实信号（快照 `captured_at` vs `.env` mtime），**不发明"凭据寿命上限"**（令牌无 `exp`，任何年龄判断都是猜测） |
| 人工触发 | CLI `python -m haut_seat auth-login` / 面板按钮 | 面板走 **detached 子进程**：`playwright.sync_api` 在已有的 asyncio 事件循环里会直接抛错，且浏览器绝不能在 HTTP 请求线程里跑 |

**保留的限制（如实记录，不假装成功）：**

- **人工环节不可省**：窗口里必须由**人本人**完成登录（含图形验证码）。程序不代填口令、
  不识别验证码、不绕过任何访问控制。
- **持久化 profile 只减少登录次数，不是凭据来源**（§1「令牌」末条）。
- **Windows 上 `chmod 0600` 不产生"仅本人可读"**（无 POSIX 权限位，只映射只读位，真实访问
  控制继承父目录 ACL）。实际防护是三条：文件在用户目录内、`data/` 已被 `.gitignore` 排除、
  程序从不打印任何值。需要更强隔离时自行用 `icacls`。
- **无桌面会话**（Windows 服务 / 会话 0 / Linux 无 X）里弹不出窗口 ⇒ 这条路径**如实显示失败
  原因**，**绝不静默回退到过期凭据**。此时有两条替代路径：命令行 `auth-login`（需要图形桌面），
  或面板里的**远程浏览器登录**（§6，服务器不需要任何桌面）。
- **预约路径（`book` / 调度器 / 运行器）从头到尾只读快照，绝不拉起浏览器** ——
  理由见 README「为什么不做自动续期 / 为什么预约路径绝不自动拉起浏览器」。
- **`auto_renew_supported` 永远为 `False`**：§1「续期」三条证据未变，没有 `exp`、
  没有 refresh token、没有刷新端点 ⇒ 任何"自动续期"都是猜测。

**仍然未知、且没有因为这次实现而变得可知的**：默认登录路径是哪一个、验证码的出现条件与
答案约定、`ssotoken` 的来源、是否存在服务端滑动续期与实际会话时长、是否还有短信/扫码等二次
验证。**这些都留在 §2 里**，不要因为"现在能一键登录了"就把它们当成已解决。

> 实现同样遵守 §4 末那条：不把密码、JWT 或完整 Cookie 写入源码、日志、测试或页面 ——
> 网页只读数据库里的**元数据**（来源 / 采集时间 / sha256 前 12 位指纹），
> 物理上不会打开快照文件。

## 6. 落地实现（2026-10-19）：面板里的远程浏览器（服务器无桌面）

### 6.1 修正 §5 的一条推断

§5 那行写的"必须人工看验证码 ⇒ **只能** `headless=False`"是**从"要看到画面"直接跳到
"浏览器必须有窗口"**，这一步不成立：把画面从服务器端转发到管理员自己的浏览器里，
同样满足"由人本人看着登录"，而且不要求服务器有桌面。§5 的结论（人工环节不可省、
不代填、不识别验证码）一个字都没变，变的只是"画面怎么到人眼前"。

### 6.2 实施方式

服务器端跑**无头** Chromium（playwright 自带的 bundled chromium，不用 `msedge`），
画面按固定间隔截成 JPEG 送进面板页面；页面上的鼠标/键盘事件反向转发回远端页面。

| 环节 | 落到哪里 | 依据 |
|---|---|---|
| 交互式会话与管理器 | `haut_seat/services/remote_browser.py` | 唯一碰 Playwright 的地方；`playwright.sync_api` 不能在活的 asyncio 循环里跑 ⇒ 全部调用收进**一个** `daemon=True` 工作线程 |
| 画面 | `GET /admin/auth/browser/frame` → `image/jpeg` | 用普通 `<img src>`（CSP 的 `img-src 'self' data:` 里**没有** `blob:`）；`/admin*` 自动带 `Cache-Control: no-store` |
| 键鼠 | `POST /admin/auth/browser/input`（JSON） | `X-CSRF-Token` 头 + 同源校验 + 8KB 体积上限 + 64 条/次上限 + **属主**校验；**绝不记录请求体**（那逐字就是口令的键盘记录） |
| 状态 | `GET /admin/auth/browser/state`（JSON） | 给机器读：未登录回 **401 JSON** 而不是 303（303 会让 `fetch` 拿到登录页 HTML，把"会话过期"这个真实原因掩盖掉） |
| 页面 | `haut_seat/web/templates/remote_browser.html` + `static/remote_browser.js` | 页面里**没有任何输入框**：口令只敲进远端页面，不进我们的 DOM、表单、自动填充与历史；无内联处理器、无外部资源（CSP 未改动一个字节） |
| 凭据归属 | 复用 §5 那条链：`LoginCapture` → `save_snapshot` → `record_login_state`/`record_credential_source` → **只读** `auth_precheck` | 写的是**全局快照** `data/auth/credentials.json`。**绝不触碰** `user_credential` 表：有属主（`user_plan`）的计划永远用该用户自己的凭据（`scheduler._run_settings_for` → `user_credentials.settings_for_user`），不存在"某个用户的 Cookie 被写进所有用户共用的凭据文件"这条路径（有测试逐字节钉住） |
| 单飞 | 与 CLI 共用 `active_login_state()`：`login_state ∈ {launching, awaiting_login}` 时拒绝 | 两处各写一遍"谁在采集"迟早会漂移，漂移的后果是**两个采集同时写同一份全局快照** |
| 会话归属 | `(admin_id, sha256(会话令牌))` 二元组 | 同一个人重新登录后旧令牌立刻失效；别的管理员拿 404 且**一个字节的画面都不返回** |

### 6.3 已知限制（如实记录）

- **全局单会话**：谁先开始谁持有，别人既看不到画面也发不了输入。
- **不支持按键长按重复**、**不支持鼠标拖拽选中文本**，**双击会被远端当成两次单击**
  （每次点击都是独立事件，没有"两次点击间隔 < 500ms"的判定）。
- **有延迟**：画面按固定间隔刷新，键鼠指令要经网络往返，慢一点敲更稳。
- **明文 HTTP 的风险被抬高了**：链路上除了面板会话 Cookie 和登录页画面，
  还多了**学校账号口令的按键**。默认仍然允许（先把功能用起来），页面顶部有红字告警并给出两条
  零成本缓解（`ssh -L` 隧道 / 反代 TLS）；想要更严就设
  `HAUT_AUTH_REMOTE_ALLOW_INSECURE=false`，明文下直接拒绝启动。
- **`HAUT_AUTH_SOURCE=env` 时快照不生效**（既有语义，不是本次引入）：env 模式按设计只读
  `.env`。远程页面会显示当前来源，并在为 `env` 时**在动手之前**就写明"在这里采集到的快照
  不会被使用"。
- **一处真实的残留竞争**：手工在服务器上跑 `auth-login` 会绕过所有检查（`_capture_and_record`
  无条件写状态），于是 CLI 与远程可能同时采集、都写全局快照，**后写者胜**。没有假装根除它。
- **沙箱不放松**：不出现 `--no-sandbox`。Linux 上 `RestrictNamespaces=true` 会让 Chromium 起不来，
  正确做法是**只放行它需要的 user/pid namespace**（`RestrictNamespaces=~user pid`）；若内核确实
  禁用了非特权 user namespace，功能会**如实报错**，不会靠关掉沙箱来"让它能用"。

### 6.4 服务器上仍然必须由人确认的事

代码与测试只能证明"我们在本机用假后端跑通了全部逻辑"。以下**必须在服务器上人工验证**，
本文档不会替你宣称它们已经通过：浏览器能否真的 `launch ok`（user namespace 那一步）、
面板里能否看到真实登录页画面、点击与打字是否真的作用到远端页面、图形验证码是否由你本人输入成功、
登录成功后状态是否变 `captured` 且只读预检结论与 `/admin/auth` 的来源卡片一致、
退出后 `ps -ef | grep -c chrome` 是否不残留。步骤见 `docs/web-deploy.md`。

---

## 7. 2026-10-23 复测：「哪里判定凭据可用」这件事被更正过一次

§7 起因于服务器上一次**现象不明**的复现：`check-auth` **带**凭据时 HTTP 403、**不带**凭据时
HTTP 200；面板里能打字、点完图书馆的登录按钮后按钮一直转圈，最后没有可用的凭据。
**这两条现象本身既不能证明令牌过期，也不能证明服务器出口 IP 被封** ——
本轮就是按这个前提去代码、日志与实测里找原因的。

### 7.1 更正：`layout/query` **无法**判定凭据是否可用

原实现（以及 §1 的推论）拿**座位查询**的返回值当"凭据是否有效"的判据。实测推翻了这个做法：

| 判据 | 输入 | 实测返回 | 能否判定凭据 |
|---|---|---|---|
| `POST /stage-api/api/seatbook/layout/query` | **匿名**（无任何头） | HTTP 200 + 正常座位列表 | **不能**：匿名也拿到同样的座位 |
| 同上 | 格式正确但**已失效**的令牌 | HTTP 200 + `code=1111`，与匿名响应**逐字节相同** | **不能**：把失效令牌误判成"未登录放行" |
| 同上 | **伪造签名**的令牌 | 业务 `code=401` | **不能**：只能证明这个头解析不了，证明不了真令牌的死活 |
| `GET /stage-api/getInfo` | 被接受的令牌 | HTTP 200 + `code=200` + `user`/`roles` | **能**：判 VALID |
| 同上 | 未被接受的令牌 | `code=401`（"认证失败，无法访问系统资源"） | **能**：判 INVALID |
| `GET /stage-api/getInfo` | 匿名 | `code=401` | 能（但此时按"未配置凭据"处理，不叫失效） |

因此 `getInfo` 是**唯一**的凭据判定接口 —— 依据不是猜的：站点自己的路由守卫在登录后就是拿
`Authorization: "Bearer " + Admin-Token` 调它来确认身份的。这张表在
`haut_seat/core/auth.py` 的模块 docstring 里（A/B/C 三张）与 README「认证机制与登录有效期」里
各有一份，三处逐字一致，并由测试钉住（`tests/test_auth_resolve_io.py`、
`tests/test_auth_check_ttl.py`、`tests/test_auth.py`）。

`auth_precheck`（`haut_seat/services/io.py`，**唯一的**凭据结论出口，CLI / 面板 / 调度器 /
用户凭据 / 远程浏览器五条路径都走它）随之改成**身份接口优先**：
`check_identity()` 给出确定结论就不再补发座位查询；只有身份接口**说不出结论**时，
才补一次只读座位查询当第二意见（`_combine_precheck`）。

### 7.2 403 的含义：**不据此断言令牌过期**

站点自己的错误码表就是 `403 = "当前操作没有权限"`（不是认证失败）。所以
`RELOGIN_HINT` / CLI 输出 / README 现在的措辞是：403 如实报"服务器未接受当前凭据
（可能是权限或网关问题）"，并**显式写出"本程序没有据此推断令牌是否过期"**，
同时保留 HTTP 状态与站点原文。测试里反面钉住："认证已失效"、"登录已失效"、
肯定式的"令牌过期"**不得出现**在 403 的结论里。

### 7.3 九问逐条结论（都带代码位置）

| # | 问题 | 结论（证据） |
|---|---|---|
| 1 | 前端点"登录"调的是哪个端点 | **面板里根本没有"图书馆登录"按钮**。我们自己的页面上没有任何输入框和登录按钮（`templates/remote_browser.html` 顶部两条红线）；"点击图书馆登录"是**学校页面里的按钮**，它以像素形式被转发过来，我们的 JS 只把鼠标/键盘事件 POST 到 `/admin/auth/browser/input`（`static/remote_browser.js` 的 `send()`）。所以"点了之后一直转圈"是**远端页面自己的状态**，我们既不渲染也不解释它 |
| 2 | 后端是否正确启动/复用浏览器、context、page | 起点唯一：`RemoteBrowserManager.start()`（`services/remote_browser.py:615`）→ 一个 `daemon=True` 工作线程 → `PlaywrightInteractive.open()`（`:378`，`launch` + `new_context` + `new_page`，**临时** profile，非持久化 user_data_dir，理由在 `:252-258`）。**一次性会话**：同一时刻只允许一个（`:639` 用 `active_login_state()` 单飞），结束时 `close()` 连 context/browser/driver 一起收（`:346`）。**不存在"复用上一次的 page"这条路径** —— 也就没有"复用了旧页面导致看起来登录了其实没有"的可能 |
| 3 | 怎么判定登录成功 | **两步，缺一不可**：① 从**浏览器 context 的 cookie**里读到 `Admin-Token`（`:910`，用 `cookies(login_url)` 按站点过滤，取不到再退回全量并按 host 过滤 `_cookie_belongs_to`）；② 用这份**新**令牌打一次只读 `auth_precheck`（`:1037` → `_readonly_precheck:1110`），只有 `valid` 才 `save_snapshot`（`:1084`）。**"Cookie 非空"绝不是成功**（`:904-909` 的注释就是写这件事的）。默认最多验证 `_VERIFY_MAX_ATTEMPTS` 次，用完就终态 `unverified`，**不写任何快照** |
| 4 | 重定向 / 异步登录 / 验证码 / 统一身份认证 / 超时 | 跳转：`goto(... wait_until="domcontentloaded")`（`:285-290`），超时上下限夹紧；页面跳走不需要专门处理，因为状态判据是 **cookie + 业务接口**而不是"当前 URL 是什么"。验证码与统一身份认证：**由人**在画面里完成，代码里没有任何识别、绕过或代填（全文无 `captcha`/`code=` 相关逻辑）。超时：总时限 `auth_login_timeout_s` 与空闲时限 `auth_remote_idle_timeout_s` 各一条（`:851`、`:857`），都走向**明确终态** `timed_out` 并附带"最后一次验证说了什么"（`_verify_note:1071`） |
| 5 | 是否真的拿到了业务 API 用的 Cookie 与 Authorization | 两者**都取，且来自同一个令牌**：`LoginCapture(cookie=f"{COOKIE_NAME}={token}", authorization=f"Bearer {token}")`（`:912-917`）。这不是推理，是同一条 cookie 的两种写法；实证是站点自己的 axios 拦截器就是 `"Bearer " + getToken()`，而 `getToken()` 读 cookie `Admin-Token`（§1 表）。**凭据只在服务端内存里**，进快照前不落任何中间文件 |
| 6 | 提取/传递/加密/保存/绑定当前用户 | 只有 `valid` 才落盘（`:1084` `save_snapshot`）；写的是**全局快照** `data/auth/credentials.json`，同时只记元数据（`record_login_state` / `record_credential_source`，只写来源/采集时间/sha256 指纹，`:1100-1106`）。**绝不写 `user_credential` 表**：有属主（`user_plan`）的计划永远用该用户自己的凭据（`scheduler._run_settings_for` → `user_credentials.settings_for_user`）—— 用户端那条路的凭据由 `services/credential_crypto` 按 `user_id` 加密分存。远程浏览器会话的**归属**是 `(admin_id, sha256(会话令牌))` 二元组（`:610`），别人的画面一个字节都拿不到 |
| 7 | **为什么前端一直转圈** | 找到**两个真实缺陷**，都已修：**(a)** `?wait=1` 的自刷新在"上一次登录留下的 `awaiting_login` 残留状态"上会**无限自我刷新**，且而后的 `active_login_state()` 又认为它已过期 —— 现在 `wait` 只在 `_live_login_state()` 真的在跑时才刷新（`web/routes.py`，测试 `tests/test_web_auth_view.py::test_wait_flag_stops_refreshing_on_a_leftover_state`）。**(b)** 模板里那三行活体结论（已获取凭据 / 只读预检 / 结束原因）原来包在 `{% if %}` 里，采集成功那一刻**元素并不存在**，`getElementById` 拿到 `null`，JS 静默跳过 ⇒ 页面上永远不出现结论，只剩"看起来在转圈"。现在三个元素**无条件存在**（`templates/remote_browser.html:104-127`），前后端的元素 id 与状态字段由测试逐条钉住（`tests/test_web_remote_browser.py` 的契约小节：9 个 id、15 个字段）。另外 JS 侧的三条硬约束（绝不 `location.reload()`、按 `frame_seq` 换源、终态必须停掉两个定时器）写在 `static/remote_browser.js:16-27` |
| 8 | **"闪照"对应哪段代码** | **这个词在本轮之前于本仓库里一次都不存在**（本轮之前全文 grep 无匹配；本节把它写下来是为了记录"查过了、没有"）。仓库历史上唯一会"一闪一闪"的代码是**每次状态变化就 `window.location.reload()` 整页重载**（重载后计数归零 ⇒ 状态来回变就是重载风暴）—— 已在 `static/remote_browser.js:18-20` 记录并删除，现在只原地改 DOM 文本。因此画面上那个被叫做"闪照"的东西，只可能是**学校自己页面的内容**被当作像素转发过来（本程序不渲染也不解释它）；我们这一侧既不产出它，也拿不到它的语义 |
| 9 | 保存的凭据是否真的用于后续座位查询 | 用。链路：`resolve_credentials()`（`services/auth_store.py:300-319`）→ `http_session(settings, credentials=…)`（`services/io.py:158`，**唯一**注入点）→ 客户端 `_build_headers()` 同时写 `Cookie` 与 `Authorization`（`client/http.py:141-147`）。**两条路的请求形状一致**：远端页面里那些请求是**站点自己**发的（我们只是转发键鼠），我们构造的请求头就从同一条 cookie 的两种写法而来（问 5），没有凭空造头。**无法自动对齐的部分**由 §6.4 移交人工确认 |

### 7.4 本轮另外两条实测（与"谜之失败"直接相关）

1. **`.env` 顶部那两行"示例"里是**真**令牌，不是占位符。** 现在文件里
   `HAUT_COOKIE` / `HAUT_AUTHORIZATION` 各出现两次：第 1–2 行是**被注释掉的**一对（注释文末写着
   "DevTools 里复制的一段 Cookie"/"Bearer xxx 或 token"，看起来像文档），第 6–7 行才是生效的一对。
   两对都是**格式完整的真实 JWT**，且**不是同一个令牌**（sha256 指纹不同，但 `login_user_key` 相同 ⇒
   同一次登录的先后两份）。`.env` 在 `.gitignore` 里，所以这没进版本库，但风险是实在的：
   谁把顶部那对**取消注释并删掉下面那对**，程序会静默改用**更旧**的令牌，而现象恰好就是
   "接口报错、看起来像过期" —— 正是本轮要排查的那类假象。
2. **重复键谁胜出：实测是"最后一个"。** 本机实测（python-dotenv 1.2.4，以及项目实际使用的
   pydantic-settings 同版本环境）：同一个键在 `.env` 里出现多次时，**后者覆盖前者**，被注释的不参与。
   所以**当前**生效的确实是第 6–7 行；但这也意味着"改 .env"的任何一次操作都可能把生效行换掉，
   而这**不会**在界面上产生任何提示。
3. **`HAUT_AUTH_SOURCE=auto` 的"较新者胜出"只看文件 mtime。** `services/auth_store.py:344-355`：
   快照胜出当且仅当 `captured_at >= mtime(.env)`；任一时间不可得时**快照优先**。
   于是"采集到新凭据之后又动过一次 `.env`（哪怕只是加一行注释）"会让 **`.env` 里的旧令牌重新胜出**，
   且界面上只会显示来源是 `env`。远程浏览器页面已经**提前**把这件事写出来了：
   `HAUT_AUTH_SOURCE=env` 时显示"在这里采集到的快照不会被使用"（`templates/remote_browser.html:129-138`）。
   要确认当前实际来源，看 `/admin/auth` 的来源卡片。

### 7.5 仍然未知（本节没有把它们变已知）

- 服务器上那次 **403 的真实来源**（网关？权限？站点策略？）—— 代码只能如实转述状态与原文，
  **没有**证据表明是令牌过期或出口 IP 被封，本轮也没有做任何会触发站点风控的请求。
- 同一天窗口**每次** `layout/query` 都回 `code=1111` 这个观测（本轮实测）：它**不是**凭据判据
  （见 7.1 表），也**没有**解释清楚是"当天已无可预约窗口"还是别的业务限制。
- §2 里那些老问题一个都没变：默认登录路径、验证码出现条件、`ssotoken` 来源、
  是否存在滑动续期与实际会话时长。
- **`.env` 的真实内容只有你知道**：本轮只以"行的形态 + 长度 + sha256 前 8 位"的方式核对过，
  没有把任何值抄进本文档、日志或测试。