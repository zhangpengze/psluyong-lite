/* 数据概览页：时间范围切换、30 秒轮询、伙伴行展开。
 *
 * 首屏由服务端渲染（dashboard.html 里 Jinja 直出 panels.py 的片段），
 * 本文件只做三件事：
 *   1. 切范围 / 刷新时调 /api/v1/stats/overview，用返回的 html 片段原地替换
 *   2. 30 秒轮询：页面不可见时跳过，可见且过期立即补一次；连续失败 3 次才提示
 *   3. 伙伴行展开 / 收起：模板明细随首屏数据一起下发，展开不发请求
 *
 * html 片段全部由服务端生成并转义（panels.py），这里只 innerHTML，
 * 不拿用户数据拼字符串——伙伴名 / 模板名不会成为 XSS 入口。
 */
(function () {
  'use strict';

  var root = document.getElementById('dashboard-root');
  if (!root) return;

  var POLL_MS = 30000;
  var boxes = {
    kpi: document.getElementById('kpi-row'),
    trend: document.getElementById('trend-card'),
    top: document.getElementById('top-card'),
    table: document.getElementById('partner-table-box'),
    note: document.getElementById('table-note-box'),
    status: document.getElementById('system-status-box')
  };
  var chips = Array.prototype.slice.call(document.querySelectorAll('#range-bar .chip[data-range]'));
  var customStart = document.getElementById('custom-start');
  var customEnd = document.getElementById('custom-end');
  var customApply = document.getElementById('custom-apply');
  var rangeError = document.getElementById('range-error');
  var refreshBtn = document.getElementById('refresh-btn');
  var recordsLink = document.getElementById('all-records-link');
  var exportLink = document.getElementById('export-overview');
  var updatedAt = document.getElementById('last-updated');

  var current = null;   // 最近一次成功的数据
  var expanded = {};    // 展开中的伙伴名 -> true（轮询重渲染后要恢复）
  var showAll = false;  // 伙伴表是否已展开全部（带上 limit=0 轮询）
  var inflightP = null;   // 进行中的请求（共享引用）：轮询与手动加载互斥，也不会自旋
  var inflightUrl = '';   // 进行中请求对应的区间 URL（判断切档后是否要补拉）
  var fails = 0;
  var lastOkAt = 0;
  var timer = null;

  function state() {
    return { range: root.dataset.range, start: root.dataset.start, end: root.dataset.end };
  }

  function overviewUrl() {
    var s = state();
    var q = 'range=' + encodeURIComponent(s.range);
    if (s.range === 'custom') q += '&start=' + encodeURIComponent(s.start) + '&end=' + encodeURIComponent(s.end);
    if (showAll) q += '&limit=0';
    return '/api/v1/stats/overview?' + q;
  }

  function exportUrl() {
    var s = state();
    var q = 'range=' + encodeURIComponent(s.range);
    if (s.range === 'custom') q += '&start=' + encodeURIComponent(s.start) + '&end=' + encodeURIComponent(s.end);
    return '/api/v1/stats/overview/export?' + q;
  }

  function showError(msg) {
    if (!rangeError) return;
    rangeError.textContent = msg || '';
    rangeError.hidden = !msg;
  }

  function syncChips() {
    var preset = state().range;
    chips.forEach(function (chip) {
      var on = chip.dataset.range === preset;
      chip.classList.toggle('active', on);
      chip.setAttribute('aria-pressed', on ? 'true' : 'false');
    });
  }

  function localToday() {
    var d = new Date();
    var p = function (n) { return String(n).padStart(2, '0'); };
    return d.getFullYear() + '-' + p(d.getMonth() + 1) + '-' + p(d.getDate());
  }

  /* ---------- 渲染 ---------- */
  function apply(data) {
    current = data;
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
      exportLink.href = exportUrl();
    }
    syncChips();
    reapplyExpanded();
    showError('');
  }

  function detailFor(name) {
    if (!current) return null;
    for (var i = 0; i < current.partners.length; i++) {
      if (current.partners[i].name === name && current.html.details[i]) {
        return current.html.details[i];
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
      showAll = true;
      load(false).then(function () { jumpToPartner(name, attempt + 1); });
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

  /* ---------- 数据 ---------- */
  // 服务端拒了区间（400）时回滚到上一次成功的区间：
  // 否则 chip 指着一个没有数据的档，轮询还会每 30 秒 400 一次。
  function revertState() {
    if (!current) return;
    root.dataset.range = current.range.preset;
    root.dataset.start = current.range.start;
    root.dataset.end = current.range.end;
    syncChips();
    history.replaceState(null, '', rangeUrl());
  }

  function load(manual) {
    // 共享 in-flight：并发调用拿到同一个 Promise，等待真实完成，
    // 而不是立刻 resolve——jumpToPartner 的补拉依赖"表真的刷新过了"。
    // 若正在进行的是旧区间（刚切档撞上轮询），完成后再按新区间补拉一次。
    if (inflightP) {
      // 撞上的可能是旧区间：等它落地后按当前区间再拉一次
      return inflightP.then(function () {
        return overviewUrl() !== inflightUrl ? load(manual) : undefined;
      });
    }
    inflightUrl = overviewUrl();
    var requested = inflightUrl;
    var p = fetchJSON(requested, {}, 15000).then(function (res) {
      if (res.status === 401) {
        // 会话过期：与其每 30 秒报一次"加载失败"，不如此刻直接去登录页
        location.href = '/login';
        return;
      }
      if (!res.data || !res.data.ok) {
        var msg = (res.data && res.data.error) || '加载失败';
        if (res.status >= 400 && res.status < 500) {
          // 区间非法（超 90 天、结束早于开始等）：回滚并把原因显示出来。
          // 回滚后把 inflightUrl 对齐到回滚后的区间，收尾时就不会再自动补拉——
          // 补拉成功会调 apply() 把这条错误提示冲掉，用户根本看不到原因。
          revertState();
          inflightUrl = overviewUrl();
          showError(msg);
        }
        throw new Error(msg);
      }
      apply(res.data);
      fails = 0;
      lastOkAt = Date.now();
    }).catch(function (err) {
      fails++;
      if (fails >= 3) {
        toast('数据更新失败，显示的是上次成功加载的数据', 'error');
        fails = 0;
      }
      if (manual) showError(err.message || '加载失败');
    }).then(function () {
      inflightP = null;
      // 收尾时区间又被改过（切档/后退撞上请求）：立刻按当前区间补拉
      if (overviewUrl() !== inflightUrl) {
        var again = load(manual);
        return again;
      }
    });
    inflightP = p;
    return p;
  }

  function rangeUrl() {
    var s = state();
    if (s.range === 'custom') {
      return '?range=custom&start=' + encodeURIComponent(s.start) + '&end=' + encodeURIComponent(s.end);
    }
    return '?range=' + encodeURIComponent(s.range);
  }

  function setRange(preset, start, end) {
    root.dataset.range = preset;
    root.dataset.start = start || '';
    root.dataset.end = end || '';
    syncChips();
    // 地址栏同步：当前视图可收藏、可分享，浏览器后退能回到上一个区间
    history.pushState(null, '', rangeUrl());
    load(false);
    restartTimer();
  }

  function restartTimer() {
    if (timer) clearInterval(timer);
    timer = setInterval(function () {
      if (document.hidden || inflightP) return;
      if (Date.now() - lastOkAt < POLL_MS - 2000) return;  // 刚加载过就跳过一次
      load(false);
    }, POLL_MS);
  }

  /* ---------- 事件 ---------- */
  chips.forEach(function (chip) {
    chip.addEventListener('click', function () {
      showError('');
      setRange(chip.dataset.range);
    });
  });

  if (customApply) {
    customApply.addEventListener('click', function () {
      var s = customStart.value;
      var e = customEnd.value;
      if (!s || !e) {
        showError('请先选择完整的开始和结束日期');
        return;
      }
      if (s > e) {
        showError('开始日期不能晚于结束日期');
        return;
      }
      showError('');
      setRange('custom', s, e);
    });
  }

  if (refreshBtn) {
    refreshBtn.addEventListener('click', function () {
      withBusy(refreshBtn, function () { return load(true); }, '刷新中');
    });
  }

  document.addEventListener('click', function (e) {
    var more = e.target.closest('#show-more-partners');
    if (more) {
      showAll = true;  // 之后轮询也带 limit=0，不再缩回去
      load(true);
      return;
    }
    var top = e.target.closest('.top-row');
    if (top) {
      jumpToPartner(top.dataset.partner);
      return;
    }
    var row = e.target.closest('tr.partner-row');
    if (row && !e.target.closest('a, button')) toggleRow(row);
  });

  // 键盘也能展开：行上有 tabindex=0，回车 / 空格触发（与记录页一致）
  document.addEventListener('keydown', function (e) {
    if (e.key !== 'Enter' && e.key !== ' ') return;
    var top = e.target.closest && e.target.closest('.top-row');
    if (top) {
      e.preventDefault();
      jumpToPartner(top.dataset.partner);
      return;
    }
    var row = e.target.closest && e.target.closest('tr.partner-row');
    if (!row || e.target.closest('a, button')) return;
    e.preventDefault();
    toggleRow(row);
  });

  // 页面重新可见时，若距上次成功加载已超过一个轮询周期，立即补一次
  document.addEventListener('visibilitychange', function () {
    if (!document.hidden && Date.now() - lastOkAt > POLL_MS) load(false);
  });

  // 后退 / 前进：按地址栏里的区间重新加载（popstate 不重新请求页面）
  window.addEventListener('popstate', function () {
    var params = new URLSearchParams(location.search);
    root.dataset.range = params.get('range') || '7d';
    root.dataset.start = params.get('start') || '';
    root.dataset.end = params.get('end') || '';
    if (root.dataset.range === 'custom' && customStart && customEnd) {
      customStart.value = root.dataset.start;
      customEnd.value = root.dataset.end;
    }
    load(false);
  });

  /* ---------- 初始化 ---------- */
  if (customStart && customEnd) {
    customStart.max = localToday();
    customEnd.max = localToday();
  }
  restartTimer();
})();
