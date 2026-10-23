/* 日报页：重推（带确认与超时）、复制正文 */
(function () {
  'use strict';

  var root = document.getElementById('reports-root');
  if (!root) return;

  var csrf = (document.querySelector('meta[name="csrf-token"]') || {}).content || '';

  var repush = document.getElementById('repush-btn');
  if (repush) {
    repush.addEventListener('click', async function () {
      var day = repush.getAttribute('data-date');
      if (!day) return;

      var ok = await confirmDialog({
        title: '重推 ' + day + ' 的日报？',
        message: '会重新生成该日日报并推送到配置的 QQ 群。推送失败也不会影响已存档的内容。',
        okText: '推送',
        danger: false
      });
      if (!ok) return;

      await withBusy(repush, async function () {
        try {
          // 后端最多重试 3 次（含退避等待），超时给到 60 秒
          var res = await fetchJSON('/api/v1/reports/run', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrf },
            body: JSON.stringify({ date: day, push: true })
          }, 60000);

          if (res.data.pushed) {
            toast('推送成功', 'success');
            setTimeout(function () { location.reload(); }, 700);
          } else {
            toast('推送失败：' + (res.data.message || res.data.error || '未知错误') + '（日报已存档，可稍后再推）', 'error');
          }
        } catch (err) {
          toast('请求失败：' + err.message, 'error');
        }
      });
    });
  }

  var copyBtn = document.getElementById('copy-report');
  if (copyBtn) {
    copyBtn.addEventListener('click', async function () {
      var body = document.getElementById('report-body');
      if (!body) return;
      try {
        await navigator.clipboard.writeText(body.textContent);
        toast('日报内容已复制', 'success');
      } catch (e) {
        toast('复制失败，请手动选中正文复制', 'error');
      }
    });
  }
})();
