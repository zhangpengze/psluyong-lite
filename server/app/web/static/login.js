/* 登录页：密码显隐 + 提交反馈 */
(function () {
  'use strict';

  var toggle = document.getElementById('pw-toggle');
  var pw = document.getElementById('password');

  if (toggle && pw) {
    toggle.addEventListener('click', function () {
      var showing = pw.getAttribute('type') === 'text';
      pw.setAttribute('type', showing ? 'password' : 'text');
      toggle.textContent = showing ? '显示' : '隐藏';
      toggle.setAttribute('aria-pressed', showing ? 'false' : 'true');
      toggle.setAttribute('aria-label', showing ? '显示密码' : '隐藏密码');
      pw.focus();
    });
  }

  var form = document.getElementById('login-form');
  var submit = document.getElementById('login-submit');
  if (form && submit) {
    form.addEventListener('submit', function () {
      // 表单校验没过时不改按钮状态，避免"转圈但没提交"
      if (!form.checkValidity()) return;
      submit.disabled = true;
      submit.setAttribute('aria-busy', 'true');
      submit.innerHTML = '';
      var spinner = document.createElement('span');
      spinner.className = 'spinner';
      submit.appendChild(spinner);
      submit.appendChild(document.createTextNode('登录中…'));
    });
  }
})();
