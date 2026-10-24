/* 录用记录页：行展开详情、删除/恢复、筛选提交反馈。
 *
 * 全部行为走事件委托 + data-* 属性，模板里不再有 onclick 字符串拼接，
 * 之后加 CSP 时这页不用改。
 */
(function () {
  'use strict';

  var root = document.getElementById('records-root');
  if (!root) return;

  var isAdmin = root.dataset.admin === 'true';
  var csrf = (document.querySelector('meta[name="csrf-token"]') || {}).content || '';
  var SCROLL_KEY = 'records-scroll';

  /* ---------- 小工具 ---------- */
  function fmtTime(iso) {
    if (!iso) return '-';
    var d = new Date(iso);
    if (isNaN(d.getTime())) return iso;
    var p = function (n) { return String(n).padStart(2, '0'); };
    return d.getFullYear() + '-' + p(d.getMonth() + 1) + '-' + p(d.getDate()) +
      ' ' + p(d.getHours()) + ':' + p(d.getMinutes()) + ':' + p(d.getSeconds());
  }

  function field(label, value, opts) {
    opts = opts || {};
    var box = document.createElement('div');
    box.className = 'detail-field';
    var k = document.createElement('div');
    k.className = 'k';
    k.textContent = label;
    var v = document.createElement('div');
    v.className = 'v' + (opts.mono ? ' mono' : '') + (opts.danger ? ' danger' : '') + (opts.warn ? ' warn' : '');
    v.textContent = value === null || value === undefined || value === '' ? '-' : String(value);
    box.appendChild(k);
    box.appendChild(v);
    return box;
  }

  function reloadKeepingScroll() {
    try { sessionStorage.setItem(SCROLL_KEY, String(window.scrollY)); } catch (e) { /* 忽略 */ }
    location.reload();
  }

  window.addEventListener('load', function () {
    try {
      var y = sessionStorage.getItem(SCROLL_KEY);
      if (y) {
        window.scrollTo(0, parseInt(y, 10) || 0);
        sessionStorage.removeItem(SCROLL_KEY);
      }
    } catch (e) { /* 忽略 */ }
  });

  /* ---------- 详情面板 ---------- */
  function buildDetail(d) {
    var wrap = document.createElement('div');
    wrap.className = 'detail-body';

    var grid = document.createElement('div');
    grid.className = 'detail-grid';
    grid.appendChild(field('工作日期', d.work_date));
    grid.appendChild(field('提交时间', fmtTime(d.created_at)));
    grid.appendChild(field('员工', d.employee_name));
    grid.appendChild(field('模板', d.template_name));
    grid.appendChild(field('模板 ID', d.template_id, { mono: true }));
    grid.appendChild(field('合作伙伴', d.partner_name));
    grid.appendChild(field('作者', d.author));
    grid.appendChild(field('标题', d.title));
    grid.appendChild(field('提交机器', d.client_machine, { mono: true }));
    grid.appendChild(field('PS 版本', d.ps_version, { mono: true }));
    grid.appendChild(field('耗时', d.processing_ms ? (d.processing_ms / 1000).toFixed(1) + ' s' : '-'));
    /* 图片只展示原始文件名：to_dict() 刻意不含服务器绝对路径（与 CSV 删「图片路径」
       列同一约束，审计 B3/D1），要看图走下方「查看原图」的鉴权接口 */
    grid.appendChild(field('图片文件名', d.image_filename, { mono: true }));
    if (d.error_message) grid.appendChild(field('错误信息', d.error_message, { danger: true }));
    if (d.warnings && d.warnings.length) {
      grid.appendChild(field('生成告警（' + d.warnings.length + '）', d.warnings.join('；'), { warn: true }));
    }
    wrap.appendChild(grid);

    var actions = document.createElement('div');
    actions.className = 'detail-actions';

    if (d.has_image) {
      var view = document.createElement('button');
      view.type = 'button';
      view.className = 'btn ghost sm';
      view.textContent = '查看原图';
      view.addEventListener('click', function () {
        openLightbox('/api/v1/records/' + d.id + '/image', d.author + '《' + d.title + '》');
      });
      actions.appendChild(view);
    }
    if (isAdmin) {
      var del = document.createElement('button');
      del.type = 'button';
      del.className = 'btn ghost sm danger-text';
      del.textContent = '删除此记录';
      del.addEventListener('click', function () { delRecord(d.id, del); });
      actions.appendChild(del);
    }
    wrap.appendChild(actions);
    return wrap;
  }

  function closeAllDetails() {
    document.querySelectorAll('tr.detail-row').forEach(function (el) { el.remove(); });
    document.querySelectorAll('tr.row-click.open').forEach(function (el) {
      el.classList.remove('open');
      el.setAttribute('aria-expanded', 'false');
    });
  }

  async function toggleDetail(tr) {
    var next = tr.nextElementSibling;
    if (next && next.classList.contains('detail-row')) {
      next.remove();
      tr.classList.remove('open');
      tr.setAttribute('aria-expanded', 'false');
      return;
    }

    closeAllDetails();

    var detail = document.createElement('tr');
    detail.className = 'detail-row';
    var td = document.createElement('td');
    var colCount = tr.closest('table').querySelectorAll('thead th').length;
    td.colSpan = colCount;

    var loading = document.createElement('div');
    loading.className = 'detail-loading';
    loading.textContent = '加载中…';
    td.appendChild(loading);
    detail.appendChild(td);
    tr.after(detail);
    tr.classList.add('open');
    tr.setAttribute('aria-expanded', 'true');

    try {
      var res = await fetchJSON('/api/v1/records/' + tr.dataset.id, {}, 12000);
      if (!res.data.ok) throw new Error(res.data.error || '加载失败');
      td.replaceChildren(buildDetail(res.data.data));
    } catch (err) {
      td.replaceChildren();
      var box = document.createElement('div');
      box.className = 'detail-loading';
      box.textContent = '详情加载失败：' + (err.message || err);
      td.appendChild(box);
      toast('详情加载失败', 'error');
    }
  }

  /* ---------- 删除 / 恢复 ---------- */
  async function delRecord(id, btn) {
    var ok = await confirmDialog({
      title: '删除这条记录？',
      message: '删除后不进列表与统计，可在下方"回收站"一键恢复，图片文件会保留。',
      okText: '删除',
      danger: true
    });
    if (!ok) return;

    await withBusy(btn, async function () {
      try {
        var res = await fetchJSON('/api/v1/records/' + id, {
          method: 'DELETE',
          headers: { 'X-CSRF-Token': csrf }
        }, 15000);
        if (res.data.ok) {
          toast('已删除，可在回收站恢复', 'success');
          reloadKeepingScroll();
        } else {
          toast(res.data.error || '删除失败', 'error');
        }
      } catch (err) {
        toast(err.message || '删除失败', 'error');
      }
    });
  }

  async function restoreRecord(id, btn) {
    var ok = await confirmDialog({
      title: '恢复这条记录？',
      message: '恢复后将重新出现在列表与统计中。',
      okText: '恢复',
      danger: false
    });
    if (!ok) return;

    await withBusy(btn, async function () {
      try {
        var res = await fetchJSON('/api/v1/records/' + id + '/restore', {
          method: 'POST',
          headers: { 'X-CSRF-Token': csrf }
        }, 15000);
        if (res.data.ok) {
          toast('已恢复', 'success');
          reloadKeepingScroll();
        } else {
          toast(res.data.error || '恢复失败', 'error');
        }
      } catch (err) {
        toast(err.message || '恢复失败', 'error');
      }
    });
  }

  /* ---------- 事件委托 ---------- */
  document.addEventListener('click', function (e) {
    var delBtn = e.target.closest('[data-delete]');
    if (delBtn) {
      e.stopPropagation();
      delRecord(delBtn.getAttribute('data-delete'), delBtn);
      return;
    }

    var restoreBtn = e.target.closest('[data-restore]');
    if (restoreBtn) {
      e.stopPropagation();
      restoreRecord(restoreBtn.getAttribute('data-restore'), restoreBtn);
      return;
    }

    var row = e.target.closest('tr.row-click');
    if (row && !e.target.closest('button, a, [data-full]')) {
      toggleDetail(row);
    }
  });

  // 键盘也能展开详情：tr 上有 tabindex=0，回车/空格触发
  document.addEventListener('keydown', function (e) {
    if (e.key !== 'Enter' && e.key !== ' ') return;
    var row = e.target.closest && e.target.closest('tr.row-click');
    if (!row || e.target.closest('button, a')) return;
    e.preventDefault();
    toggleDetail(row);
  });

  /* ---------- 筛选提交反馈 ---------- */
  var form = document.getElementById('filter-form');
  if (form) {
    form.addEventListener('submit', function () {
      var btn = document.getElementById('filter-submit');
      if (!btn) return;
      btn.disabled = true;
      btn.setAttribute('aria-busy', 'true');
      btn.innerHTML = '';
      var spinner = document.createElement('span');
      spinner.className = 'spinner';
      btn.appendChild(spinner);
      btn.appendChild(document.createTextNode('筛选中…'));
    });
  }
})();
