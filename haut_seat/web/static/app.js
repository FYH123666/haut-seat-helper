/* 仅用于界面行为与确认对话框；不引入任何数据到前端，也不处理凭据。
   不读取 localStorage / sessionStorage / cookie，不发起任何网络请求。 */

/* 页面用到的两处 Bootstrap JS 行为（导航折叠、提示关闭）在这里用原生 JS 实现。
   本应用的 CSP 是 script-src 'self'，Bootstrap 的 JS 包无法从 CDN 加载，
   因此不能依赖它——否则手机上导航菜单根本打不开。 */
document.addEventListener('click', function (event) {
  var node = event.target;
  if (!(node instanceof Element)) return;

  var toggler = node.closest('[data-bs-toggle="collapse"]');
  if (toggler) {
    var selector = toggler.getAttribute('data-bs-target');
    var panel = selector ? document.querySelector(selector) : null;
    if (panel) {
      var open = panel.classList.toggle('show');
      toggler.setAttribute('aria-expanded', open ? 'true' : 'false');
    }
    return;
  }

  var dismiss = node.closest('[data-bs-dismiss="alert"]');
  if (dismiss) {
    var alert = dismiss.closest('.alert');
    if (alert && alert.parentNode) {
      alert.parentNode.removeChild(alert);
    }
  }
}, false);

/* 危险操作（暂停/停止/删除等）提交前确认。 */
document.addEventListener('submit', function (event) {
  var form = event.target;
  if (!(form instanceof HTMLFormElement)) return;
  var message = form.getAttribute('data-confirm');
  if (message && !window.confirm(message)) {
    event.preventDefault();
    event.stopPropagation();
    return;
  }
  guardDoubleSubmit(form);
}, true);

/* 防双击：POST 走 303 PRG，刷新页面不会重复提交，但连点两下会创建两条预约
   （服务端没有请求级幂等）。这里在第一次提交后禁用按钮 —— 只是兜底，
   真正的防重复仍然在服务端。 */
function guardDoubleSubmit(form) {
  if (!form.hasAttribute('data-guard-submit')) return;
  window.setTimeout(function () {
    var buttons = form.querySelectorAll('button[type="submit"]');
    for (var i = 0; i < buttons.length; i++) {
      buttons[i].disabled = true;
    }
  }, 0);  // 延到当前事件之后，避免禁用的按钮不随表单一起提交
}

/* 预约表单：按「多久一次」切换「日期 / 仅工作日 / 说明」的可见性。
   只是显示逻辑，提交内容与校验一律以服务端为准。 */
function syncPlanMode() {
  var radios = document.querySelectorAll('[data-mode-radio]');
  if (!radios.length) return;

  var mode = '';
  for (var i = 0; i < radios.length; i++) {
    if (radios[i].checked) mode = radios[i].value;
  }
  var once = mode === 'once';

  toggleAll('[data-once-only]', once);
  toggleAll('[data-daily-only]', !once);
  toggleAll('[data-weekdays-row]', !once);

  /* 日期只在单次预约里有意义。切到「每天」时禁用它而不是清空：
     禁用后它不会跟着表单提交（不留下看不见的值），切回来时用户填过的
     日期还在。服务端对每天预约本来就一律取今天，不看这个字段。 */
  var dateInput = document.getElementById('run_date');
  if (dateInput) dateInput.disabled = !once;
}

function toggleAll(selector, show) {
  var nodes = document.querySelectorAll(selector);
  for (var i = 0; i < nodes.length; i++) {
    nodes[i].style.display = show ? '' : 'none';
  }
}

document.addEventListener('change', function (event) {
  var node = event.target;
  if (node instanceof Element && node.matches('[data-mode-radio], #weekdays_only')) {
    syncPlanMode();
  }
}, false);

document.addEventListener('DOMContentLoaded', syncPlanMode);