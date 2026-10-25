/* 远程浏览器：画面轮询 + 键鼠转发。
 *
 * 这个文件**只做转发**，不做任何渲染之外的判断：
 *
 * - 画面：把 <img> 的 src 指向 /admin/auth/browser/frame?t=<序号>。
 *   之所以不是 iframe / blob / WebSocket，见 routes.py 里那一段说明
 *   （本应用自己的 X-Frame-Options: DENY 与没有 blob: 的 CSP）。
 * - 输入：把鼠标/键盘事件打包成 JSON，POST 到 /admin/auth/browser/input，
 *   带 X-CSRF-Token 头。**服务端才是真正的边界**：事件类型、按键白名单、
 *   坐标范围、体积上限全在服务端校验，这里做的一切都只是为了手感。
 * - 按键 → 远端按键的映射**不在这个文件里**（在 services/remote_browser.py），
 *   因为只有白名单里的键能到达远端浏览器，而白名单必须可单测。
 * - 状态：定时读 /admin/auth/browser/state，**原地更新页面上的那几个元素**。
 *   服务端给出终态（`terminal:true`）时立刻停掉画面与状态轮询，并留下结论。
 *
 * 三条硬约束（都是踩过的坑）：
 *
 * 1. **绝不调用 window.location.reload()**。之前状态一变就整页重载，页面上会一闪一闪
 *    （而且 reloadedFor 在重载后归零，状态来回变就是重载风暴）。现在只改 DOM 文本，
 *    并且只改服务端返回的那几个值。
 * 2. **画面按 frame_seq 换源**：服务端每 interval 才产出一帧，没有新帧时换 src 只会
 *    把画面闪成空白（204 会让 <img> 清掉已显示的位图）。
 * 3. **任何终态都必须停止刷新**：终态时清掉两个定时器并把结论写在提示行里，
 *    绝不允许"一直转圈"。
 *
 * 本文件不读 localStorage / sessionStorage / document.cookie，
 * 也不把任何凭据写进 DOM。
 */
(function () {
  'use strict';

  var root = document.getElementById('remote-browser-root');
  if (!root) return;  // 没有会话：整页没有任何需要轮询的东西

  var img = document.getElementById('rb-frame');
  var hint = document.getElementById('rb-hint');
  var label = document.getElementById('rb-state-label');
  var message = document.getElementById('rb-state-message');
  var detail = document.getElementById('rb-state-detail');
  var capturedLine = document.getElementById('rb-captured-line');
  var precheckLine = document.getElementById('rb-precheck-line');
  var reasonLine = document.getElementById('rb-reason-line');
  if (!img) return;

  var frameUrl = root.getAttribute('data-frame-url');
  var stateUrl = root.getAttribute('data-state-url');
  var inputUrl = root.getAttribute('data-input-url');
  var csrf = root.getAttribute('data-csrf') || '';
  var pollMs = parseInt(root.getAttribute('data-poll-ms'), 10);
  if (!(pollMs > 0)) pollMs = 600;
  var active = root.getAttribute('data-active') === '1';

  var seq = 0;
  var stopped = false;
  var lastSeq = -1;
  var stateTimer = null;

  function setHint(text) {
    if (hint) hint.textContent = text;
  }

  function show(el, text) {
    if (!el) return;
    if (text) {
      el.textContent = text;
      el.hidden = false;
    } else {
      el.hidden = true;
    }
  }

  /* ---------------------------------------------------------------- 画面 */

  function loadFrame(frameSeq) {
    if (stopped || !active) return;
    if (typeof frameSeq === 'number') {
      // 服务端还没产出新的一帧：换 src 会让画面闪成空白（204 会清掉已显示的位图），
      // 也会白白多打一次请求。帧序号变了才换。
      if (frameSeq === lastSeq) return;
      lastSeq = frameSeq;
    }
    seq += 1;
    // /admin* 已经带 Cache-Control: no-store，序号仍然保留：保证每一帧都是新请求。
    img.src = frameUrl + '?t=' + seq;
  }

  img.addEventListener('load', function () {
    if (img.naturalWidth > 0) setHint('在这张画面上点击、拖动滚动、直接打字即可；输入会转发到远端页面。');
  }, false);

  img.addEventListener('error', function () {
    if (stopped) return;
    // 204（画面还没出来）也会触发 error：这不是错误，只是还没准备好。
    setHint('暂时没有画面：会话可能已结束、已过期，或浏览器还在启动中。');
  }, false);

  /* ------------------------------------------------------------ 状态轮询 */

  function stopTimers() {
    stopped = true;
    if (stateTimer !== null) { window.clearInterval(stateTimer); stateTimer = null; }
    if (frameTimer !== null) { window.clearInterval(frameTimer); frameTimer = null; }
  }

  /* 终态：停止一切刷新，并把结论写清楚（这是"绝不无限加载"的落点）。 */
  function finish(data) {
    stopTimers();
    var reason = data.reason_label || data.reason || '';
    if (data.state === 'captured') {
      setHint('登录成功：凭据已通过业务接口验证并保存为快照。画面不再刷新，可以关闭这一页。');
    } else if (data.reason === 'timed_out') {
      setHint('这次会话已超时结束' + (reason ? '（' + reason + '）' : '')
              + '。画面不再刷新；要重试请点上面的「刷新页面」后重新开始。');
    } else {
      setHint('这次会话已结束' + (reason ? '（' + reason + '）' : '')
              + '。画面不再刷新；要重试请点上面的「刷新页面」后重新开始。');
    }
  }

  function applyState(data) {
    if (label && data.label) label.textContent = data.label;
    if (message) message.textContent = data.message || '';

    if (detail) {
      var bits = [];
      if (data.url) bits.push('当前页面：' + data.url);
      if (data.active && typeof data.idle_left_s === 'number') {
        bits.push('空闲自动关闭倒计时：' + data.idle_left_s + ' 秒（有任何输入就重置）');
      }
      detail.textContent = bits.join(' · ');
    }

    show(capturedLine, data.captured
      ? '已获取凭据 · 采集时间 ' + (data.captured_at || '未知')
        + ' · 指纹 ' + (data.fingerprint || '')
      : '');
    show(precheckLine, data.precheck_status
      ? '只读认证预检：' + data.precheck_status + ' —— ' + (data.precheck_message || '')
      : '');
    /* 结束原因：中文标签 + 原始原因码。两者都给 —— 标签让人看懂，
       原因码是排查时唯一能对照服务端日志的东西（不是给普通用户看的）。
       措辞与服务端首次渲染的完全一致，避免"第一次改写就换一种说法"。 */
    show(reasonLine, (!data.captured && data.reason_label)
      ? '结束原因：' + data.reason_label + (data.reason ? '（' + data.reason + '）' : '')
      : '');

    if (data.terminal) {
      finish(data);
      return;
    }
    if (data.active) active = true;
    loadFrame(typeof data.frame_seq === 'number' ? data.frame_seq : null);
  }

  function pollState() {
    if (stopped) return;
    fetch(stateUrl, {
      method: 'GET',
      credentials: 'same-origin',
      headers: { 'Accept': 'application/json' },
      cache: 'no-store'
    }).then(function (resp) {
      if (!resp.ok) {
        // 401 = 管理员会话已过期；404 = 这个会话不属于你（已结束或被别人接手）。
        // 两者都不再轮询：继续刷只会一直失败。
        stopTimers();
        setHint(resp.status === 401
          ? '管理员登录会话已失效，请重新登录后再看这次远程浏览器会话。'
          : '这次远程浏览器会话已不再属于你的登录会话（可能已结束）。');
        return null;
      }
      return resp.json();
    }).then(function (data) {
      if (data) applyState(data);
    })['catch'](function () {
      // 网络抖动不该终止轮询：下一轮再试。
    });
  }

  /* -------------------------------------------------------------- 事件发送 */

  function send(events) {
    if (stopped || !events.length) return;
    var body = JSON.stringify({ events: events });
    fetch(inputUrl, {
      method: 'POST',
      credentials: 'same-origin',
      cache: 'no-store',
      headers: {
        'Content-Type': 'application/json',
        'X-CSRF-Token': csrf
      },
      body: body
    })['catch'](function () {
      // 丢一批事件是可以接受的：这是实时交互，不是需要可靠投递的队列。
    });
  }

  /* 坐标换算：截图是视口尺寸（非整页），图片被 CSS 缩放后
     offsetX 是**显示**像素，必须按 naturalWidth / clientWidth 还原成
     远端页面的 CSS 像素，否则点哪儿都不准。 */
  function coords(event) {
    var sx = 1, sy = 1;
    if (img.naturalWidth > 0 && img.clientWidth > 0) sx = img.naturalWidth / img.clientWidth;
    if (img.naturalHeight > 0 && img.clientHeight > 0) sy = img.naturalHeight / img.clientHeight;
    return { x: event.offsetX * sx, y: event.offsetY * sy };
  }

  /* ---------------------------------------------------------------- 鼠标 */

  var lastMove = 0;
  img.addEventListener('mousemove', function (event) {
    var now = Date.now();
    if (now - lastMove < 50) return;   // 节流：60fps 的移动事件没必要逐个转发
    lastMove = now;
    var p = coords(event);
    send([{ t: 'm', x: p.x, y: p.y }]);
  }, false);

  img.addEventListener('click', function (event) {
    var p = coords(event);
    var button = event.button === 2 ? 'right' : (event.button === 1 ? 'middle' : 'left');
    root.focus();   // 让随后的键盘输入有落脚点
    send([{ t: 'm', x: p.x, y: p.y }, { t: 'c', x: p.x, y: p.y, b: button, n: 1 }]);
  }, false);

  img.addEventListener('contextmenu', function (event) {
    // 远端的右键菜单在我们的画面里没有意义，一律交给远端页面处理。
    event.preventDefault();
  }, false);

  var wheelTimer = null, wheelDy = 0, wheelDx = 0;
  img.addEventListener('wheel', function (event) {
    event.preventDefault();
    wheelDx += event.deltaX;
    wheelDy += event.deltaY;
    if (wheelTimer !== null) return;
    wheelTimer = window.setTimeout(function () {
      wheelTimer = null;
      var dx = wheelDx, dy = wheelDy;
      wheelDx = 0; wheelDy = 0;
      send([{ t: 'w', dx: dx, dy: dy }]);
    }, 80);
  }, { passive: false });

  /* ---------------------------------------------------------------- 键盘 */

  /* 这些键留给**本地**浏览器：刷新页面才能重新拉状态、看结果，
     F12 才能排查问题。其余按键一律转发给远端页面。 */
  function isLocalShortcut(event) {
    var key = String(event.key || '');
    if (key === 'F5' || key === 'F11' || key === 'F12') return true;
    if (event.ctrlKey || event.metaKey) {
      return ['r', 't', 'w', 'n', 'l'].indexOf(key.toLowerCase()) >= 0;
    }
    return false;
  }

  root.addEventListener('keydown', function (event) {
    if (isLocalShortcut(event)) return;
    // 输入法组合中：键码是 'Process'，真正的内容在 compositionend 里给出。
    if (event.isComposing || event.keyCode === 229) return;
    if (['Shift', 'Control', 'Alt', 'Meta'].indexOf(event.key) >= 0) return;
    event.preventDefault();
    send([{
      t: 'k', k: event.key,
      ctrl: event.ctrlKey, alt: event.altKey, shift: event.shiftKey, meta: event.metaKey
    }]);
  }, false);

  /* 中文/日文等输入法：组合结束后把最终文本整段插入远端页面
     （逐键码转发在非 ASCII 下会丢字符）。 */
  root.addEventListener('compositionend', function (event) {
    var text = event.data;
    if (typeof text === 'string' && text.length) send([{ t: 'x', s: text }]);
  }, false);

  /* 点画面即聚焦：否则第一次打字会落到浏览器地址栏之类的别处。 */
  img.addEventListener('mousedown', function () { root.focus(); }, false);

  /* ---------------------------------------------------------------- 启动 */

  /* 一次状态轮询同时做两件事：更新页面上的状态文字，以及在新帧到来时换画面源。
     一个定时器就够 —— 分成两个只会让同一份信息被拉两次。 */
  fetch(stateUrl, { credentials: 'same-origin', cache: 'no-store',
                    headers: { 'Accept': 'application/json' } })
    .then(function (resp) { return resp.ok ? resp.json() : null; })
    .then(function (data) {
      if (data) applyState(data);
    })['catch'](function () {})['then'](function () {
      if (stopped) return;
      stateTimer = window.setInterval(pollState, pollMs);
    });
})();