(() => {
  // 登录失败时弹出一个原生对话框；关闭后聚焦空密码框，便于立即重试。
  // 无脚本环境由模板中的 <noscript> 提示兜底，不会重复播报。
  const dialog = document.querySelector('#login-error-dialog');
  if (dialog && typeof dialog.showModal === 'function') {
    dialog.addEventListener('close', () => {
      document.querySelector('#password')?.focus();
    });
    dialog.showModal();
  }
})();
