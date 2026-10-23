/* 轻量 toast：替代原生 alert，供删除/恢复/重推/复制等异步结果反馈 */
(function () {
  function host() {
    let el = document.getElementById('toast-host');
    if (!el) {
      el = document.createElement('div');
      el.id = 'toast-host';
      el.className = 'toast-host';
      el.setAttribute('aria-live', 'polite');
      document.body.appendChild(el);
    }
    return el;
  }

  window.toast = function (message, type) {
    type = type || 'info';
    const item = document.createElement('div');
    item.className = 'toast ' + type;
    item.setAttribute('role', 'status');

    const text = document.createElement('span');
    text.className = 'toast-msg';
    text.textContent = message;
    item.appendChild(text);

    const close = document.createElement('span');
    close.className = 'toast-close';
    close.textContent = '\u00d7';
    close.setAttribute('aria-label', '\u5173\u95ed');
    item.appendChild(close);

    let timer = null;
    function dismiss() {
      if (timer) clearTimeout(timer);
      item.classList.add('out');
      setTimeout(function () { item.remove(); }, 220);
    }
    close.addEventListener('click', dismiss);
    host().appendChild(item);
    timer = setTimeout(dismiss, type === 'error' ? 6000 : 3200);
    return item;
  };
})();
