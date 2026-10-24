/* 数据概览页（渲染端）：html 片段替换、伙伴行展开 / 收起、TOP5 定位。
 *
 * 与 dashboard.js（状态与轮询宿主）经 window.__dashboardPage 共享：
 * 本文件挂 apply / toggleRow / jumpToPartner，反向只用它暴露的
 * overviewUrl / exportUrl / showError / syncChips / load。
 * 无打包器的原生 JS，按 dashboard.js → dashboard-render.js 顺序引入；
 * 双方互调都发生在事件或轮询触发时，不在脚本执行期。
 *
 * html 片段全部由服务端生成并转义（panels），这里只 innerHTML，
 * 不拿用户数据拼字符串——伙伴名 / 模板名不会成为 XSS 入口。
 */
(function () {
  'use strict';

  var D = window.__dashboardPage;
  if (!D) return;  // 核心脚本没挂载（页面没有 dashboard-root）时不初始化

  var root = D.root;
  var boxes = {
    kpi: document.getElementById('kpi-row'),
    trend: document.getElementById('trend-card'),
    top: document.getElementById('top-card'),
    table: document.getElementById('partner-table-box'),
    note: document.getElementById('table-note-box'),
    status: document.getElementById('system-status-box')
  };
  var recordsLink = document.getElementById('all-records-link');
  var exportLink = document.getElementById('export-overview');
  var updatedAt = document.getElementById('last-updated');

  var expanded = {};  // 展开中的伙伴名 -> true（轮询重渲染后要恢复）

  /* ---------- 渲染 ---------- */
  function apply(data) {
    D.current = data;
    root.dataset.range = data.range.preset;
    root.dataset.start = data.range.start;
    root.dataset.end = data.range.end;

    if (boxes.kpi) boxes.kpi.innerHTML = data.html.kpi;
    if (boxes.trend) boxes.trend.innerHTML = data.html.trend;
    if (boxes.top) boxes.top.innerHTML = data.html.top;
    if (boxes.table) boxes.table.innerHTML = data.html.table;
    if (boxes.note) boxes.note.innerHTML = data.html.note;
    if (boxes.status) boxes.status.innerHTML = data.html.status;

    if (updatedAt) {
      updatedAt.textContent = '最后更新于 ' + data.updated_at;
      updatedAt.classList.add('just');
      setTimeout(function () { updatedAt.classList.remove('just'); }, 1200);
    }
    if (recordsLink) {
      recordsLink.href = '/records?start=' + data.range.start + '&end=' + data.range.end;
    }
    if (exportLink) {
      exportLink.href = D.exportUrl();
    }
    D.syncChips();
    reapplyExpanded();
    D.showError('');
  }

  function detailFor(name) {
    if (!D.current) return null;
    for (var i = 0; i < D.current.partners.length; i++) {
      if (D.current.partners[i].name === name && D.current.html.details[i]) {
        return D.current.html.details[i];
      }
    }
    return null;
  }

  function expandRow(row) {
    var name = row.dataset.name;
    if (row.nextElementSibling && row.nextElementSibling.classList.contains('detail-row')) return;
    var html = detailFor(name);
    if (!html) return;
    var tpl = document.createElement('template');
    tpl.innerHTML = html;
    row.after(tpl.content.firstChild);
    row.classList.add('open');
    row.setAttribute('aria-expanded', 'true');
    expanded[name] = true;
  }

  function collapseRow(row) {
    var next = row.nextElementSibling;
    if (next && next.classList.contains('detail-row')) next.remove();
    row.classList.remove('open');
    row.setAttribute('aria-expanded', 'false');
    delete expanded[row.dataset.name];
  }

  function toggleRow(row) {
    if (row.classList.contains('open')) collapseRow(row);
    else expandRow(row);
  }

  function reapplyExpanded() {
    var tbody = boxes.table && boxes.table.querySelector('tbody');
    if (!tbody) return;
    Array.prototype.forEach.call(tbody.querySelectorAll('tr.partner-row'), function (row) {
      if (expanded[row.dataset.name]) expandRow(row);
    });
  }

  /* ---------- TOP5 定位：滚到明细表对应行，展开并高亮两秒 ---------- */
  function jumpToPartner(name, attempt) {
    attempt = attempt || 0;
    var tbody = boxes.table && boxes.table.querySelector('tbody');
    if (!tbody) return;
    var target = null;
    Array.prototype.forEach.call(tbody.querySelectorAll('tr.partner-row'), function (row) {
      if (row.dataset.name === name) target = row;
    });
    if (!target) {
      // 伙伴不在当前截断的表格里（超过前 limit 家）：先显示全部再定位。
      // 最多补拉两次——若 join 上的那次请求没带 limit=0（撞上正在进行的轮询），
      // 第二次才是真请求；两次之后仍找不到就是数据里真没有，不能再循环。
      if (attempt >= 2) return;
      D.showAll = true;
      D.load(false).then(function () { jumpToPartner(name, attempt + 1); });
      return;
    }
    expandRow(target);
    var reduce = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
    target.scrollIntoView({ behavior: reduce ? 'auto' : 'smooth', block: 'center' });
    target.classList.remove('flash-row');
    void target.offsetWidth;  // 重置动画，连点也能重新闪
    target.classList.add('flash-row');
    setTimeout(function () { target.classList.remove('flash-row'); }, 2000);
  }

  // 挂到共享命名空间，核心脚本的事件委托从这里取
  D.apply = apply;
  D.toggleRow = toggleRow;
  D.jumpToPartner = jumpToPartner;
})();
