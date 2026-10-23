/* 后台通用交互：主题切换、灯箱、确认弹窗、带超时的 fetch、按钮忙碌态。
 *
 * 为什么单独抽出来：
 *   1. 模板里的内联 <script> 和 onclick 是将来上 CSP 的唯一障碍。把这些
 *      行为集中到这里，模板只剩 data-* 属性，之后就能加 script-src 'self'。
 *   2. 之前删除确认用的是原生 confirm()，移动端会挡地址栏、样式不可控，
 *      与产品自带的 toast 风格割裂。
 */
(function () {
  'use strict';

  var THEME_KEY = 'psluyong-theme';
  var THEME_LABEL = { auto: '主题：跟随系统', light: '主题：浅色', dark: '主题：深色' };
  var THEME_NEXT = { auto: 'light', light: 'dark', dark: 'auto' };

  /* ---------- 主题 ---------- */
  function readTheme() {
    try {
      var saved = localStorage.getItem(THEME_KEY);
      if (saved === 'dark' || saved === 'light') return saved;
    } catch (e) { /* 隐私模式下 localStorage 可能抛错，退回系统偏好 */ }
    return 'auto';
  }

  function applyTheme(theme) {
    if (theme === 'auto') document.documentElement.removeAttribute('data-theme');
    else document.documentElement.setAttribute('data-theme', theme);
  }

  function syncThemeButton() {
    var btn = document.getElementById('theme-toggle');
    if (!btn) return;
    var theme = readTheme();
    var label = btn.querySelector('.theme-label');
    if (label) label.textContent = THEME_LABEL[theme];
    btn.setAttribute('aria-label', THEME_LABEL[theme] + '，点击切换');
  }

  window.cycleTheme = function () {
    var next = THEME_NEXT[readTheme()];
    try { localStorage.setItem(THEME_KEY, next); } catch (e) { /* 忽略 */ }
    applyTheme(next);
    syncThemeButton();
  };

  /* ---------- 遮罩层（灯箱 / 弹窗共用）---------- */
  var lastFocused = null;

  function openOverlay(el) {
    if (!el) return;
    lastFocused = document.activeElement;
    el.classList.add('open');
    el.setAttribute('aria-hidden', 'false');
    document.body.style.overflow = 'hidden';
    var target = el.querySelector('[data-autofocus]') || el.querySelector('button, [href], input');
    if (target) target.focus();
  }

  function closeOverlay(el) {
    if (!el) return;
    el.classList.remove('open');
    el.setAttribute('aria-hidden', 'true');
    document.body.style.overflow = '';
    if (lastFocused && lastFocused.focus) lastFocused.focus();
    lastFocused = null;
  }

  window.openLightbox = function (src, alt) {
    var box = document.getElementById('lightbox');
    if (!box || !src) return;
    var img = document.getElementById('lightbox-img');
    img.src = src;
    img.alt = alt || '成品图';
    openOverlay(box);
  };

  window.closeLightbox = function () {
    var box = document.getElementById('lightbox');
    if (!box) return;
    closeOverlay(box);
    var img = document.getElementById('lightbox-img');
    if (img) img.removeAttribute('src');
  };

  /* ---------- 确认弹窗（Promise 版 confirm）---------- */
  var confirmResolve = null;

  window.confirmDialog = function (opts) {
    opts = opts || {};
    var modal = document.getElementById('confirm-modal');
    if (!modal) return Promise.resolve(window.confirm(opts.message || '确认执行？'));

    modal.querySelector('[data-modal-title]').textContent = opts.title || '请确认';
    modal.querySelector('[data-modal-message]').textContent = opts.message || '';

    var okBtn = modal.querySelector('[data-modal-ok]');
    okBtn.textContent = opts.okText || '确认';
    okBtn.className = 'btn ' + (opts.danger === false ? '' : 'danger');

    openOverlay(modal);
    return new Promise(function (resolve) {
      confirmResolve = resolve;
    });
  };

  function settleConfirm(value) {
    var modal = document.getElementById('confirm-modal');
    closeOverlay(modal);
    if (confirmResolve) {
      var resolve = confirmResolve;
      confirmResolve = null;
      resolve(value);
    }
  }

  /* ---------- 带超时的 fetch ---------- */
  window.fetchJSON = async function (url, options, timeoutMs) {
    options = options || {};
    var controller = new AbortController();
    var timer = setTimeout(function () { controller.abort(); }, timeoutMs || 15000);
    try {
      var resp = await fetch(url, Object.assign({}, options, { signal: controller.signal }));
      var text = await resp.text();
      var data;
      try {
        data = text ? JSON.parse(text) : {};
      } catch (e) {
        throw new Error('服务器返回了非预期的内容（HTTP ' + resp.status + '）');
      }
      return { ok: resp.ok, status: resp.status, data: data };
    } catch (err) {
      if (err.name === 'AbortError') throw new Error('请求超时，请检查网络后重试');
      throw err;
    } finally {
      clearTimeout(timer);
    }
  };

  /* ---------- 按钮忙碌态 ---------- */
  window.withBusy = async function (btn, task, busyText) {
    if (!btn) return task();
    var original = btn.innerHTML;
    btn.disabled = true;
    btn.setAttribute('aria-busy', 'true');
    if (busyText) {
      btn.innerHTML = '';
      var spinner = document.createElement('span');
      spinner.className = 'spinner';
      btn.appendChild(spinner);
      btn.appendChild(document.createTextNode(busyText));
    }
    try {
      return await task();
    } finally {
      btn.disabled = false;
      btn.removeAttribute('aria-busy');
      btn.innerHTML = original;
    }
  };

  /* ---------- 事件委托 ---------- */
  document.addEventListener('click', function (e) {
    // 缩略图 / 带 data-full 的元素 → 开灯箱
    var zoom = e.target.closest('[data-full]');
    if (zoom) {
      e.preventDefault();
      openLightbox(zoom.getAttribute('data-full'), zoom.getAttribute('data-alt') || zoom.getAttribute('alt'));
      return;
    }

    // 遮罩本身可点，内部卡片不关
    var overlay = e.target.classList && (e.target.classList.contains('lightbox') || e.target.classList.contains('modal'));
    if (overlay && !e.target.closest('.modal-card')) {
      if (e.target.id === 'confirm-modal') settleConfirm(false);
      else closeOverlay(e.target);
      return;
    }

    if (e.target.closest('[data-close-overlay]')) {
      e.preventDefault();
      closeOverlay(e.target.closest('.lightbox, .modal'));
      return;
    }

    if (e.target.closest('[data-modal-ok]')) {
      e.preventDefault();
      settleConfirm(true);
      return;
    }

    if (e.target.closest('[data-modal-cancel]')) {
      e.preventDefault();
      settleConfirm(false);
      return;
    }

    if (e.target.closest('#theme-toggle')) {
      e.preventDefault();
      window.cycleTheme();
    }
  });

  // Esc：先关确认弹窗与灯箱，再交给页面自己的处理（详情行折叠）
  document.addEventListener('keydown', function (e) {
    if (e.key !== 'Escape') return;
    var confirmBox = document.getElementById('confirm-modal');
    if (confirmBox && confirmBox.classList.contains('open')) {
      e.preventDefault();
      settleConfirm(false);
      return;
    }
    var lightbox = document.getElementById('lightbox');
    if (lightbox && lightbox.classList.contains('open')) {
      e.preventDefault();
      window.closeLightbox();
    }
  });

  // 焦点别跑出弹窗：简单的 Tab 首尾循环，够满足键盘可用
  document.addEventListener('keydown', function (e) {
    if (e.key !== 'Tab') return;
    var modal = document.querySelector('.modal.open .modal-card');
    if (!modal) return;
    var nodes = modal.querySelectorAll('button, [href], input, select, textarea');
    if (!nodes.length) return;
    var first = nodes[0];
    var last = nodes[nodes.length - 1];
    if (e.shiftKey && document.activeElement === first) {
      e.preventDefault();
      last.focus();
    } else if (!e.shiftKey && document.activeElement === last) {
      e.preventDefault();
      first.focus();
    }
  });

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', syncThemeButton);
  } else {
    syncThemeButton();
  }
})();
