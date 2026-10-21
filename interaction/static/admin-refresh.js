(() => {
  const status = document.querySelector('#dashboard-refresh-status');
  if (!status) return;
  const seconds = Number(status.dataset.refreshSeconds);
  if (!Number.isFinite(seconds) || seconds < 1) return;
  const interval = seconds * 1000;
  let timer;
  let request;
  let expired = false;

  const schedule = () => {
    clearTimeout(timer);
    if (!expired && !document.hidden) timer = setTimeout(refresh, interval);
  };

  const refresh = async () => {
    if (expired || request || document.hidden) return;
    const sortVersion = document.querySelector('#source-list')?.dataset.sortVersion || '';
    const current = new AbortController();
    request = current;
    const timeout = setTimeout(() => current.abort(), 10000);
    try {
      const response = await fetch('/admin', {
        credentials: 'same-origin', cache: 'no-store', redirect: 'error', signal: current.signal,
      });
      if (response.status === 401 || response.status === 403) {
        expired = true;
        status.textContent = '登录已过期，请重新登录。';
        const link = document.createElement('a');
        link.href = '/admin/login';
        link.textContent = '重新登录';
        status.append(' ', link);
        return;
      }
      if (!response.ok) throw new Error('refresh');
      const page = new DOMParser().parseFromString(await response.text(), 'text/html');
      if (!page.querySelector('#dashboard-refresh-status')) throw new Error('refresh');
      const regions = Array.from(document.querySelectorAll('[data-live-region]')).map(target => {
        const name = target.dataset.liveRegion;
        return {target, name, next: page.querySelector(`[data-live-region="${name}"]`)};
      });
      if (regions.some(region => !region.next)) throw new Error('refresh');
      const csrf = page.querySelector('input[name="token"]')?.value;
      if (csrf) {
        document.querySelectorAll('input[name="token"]').forEach(input => { input.value = csrf; });
        const sources = document.querySelector('#source-list');
        if (sources) sources.dataset.token = csrf;
      }
      for (const {target, name, next} of regions) {
        // 保留正在操作的表格与排序过程，不触碰订阅/视频/日期输入表单。
        if (target.contains(document.activeElement) || target.dataset.sorting === 'true') continue;
        if (name === 'sources' && (target.dataset.sortVersion || '') !== sortVersion) continue;
        if (target.innerHTML === next.innerHTML) continue;
        target.replaceChildren(...next.childNodes);
        target.dispatchEvent(new Event('dashboard:updated'));
      }
      if (status.textContent !== '状态自动更新中') status.textContent = '状态自动更新中';
    } catch (_) {
      if (!document.hidden && status.textContent !== '状态更新暂时失败，正在自动重试。') {
        status.textContent = '状态更新暂时失败，正在自动重试。';
      }
    } finally {
      clearTimeout(timeout);
      request = null;
      schedule();
    }
  };

  document.addEventListener('visibilitychange', () => {
    clearTimeout(timer);
    if (document.hidden) request?.abort();
    else refresh();
  });
  window.addEventListener('pagehide', () => {
    clearTimeout(timer);
    request?.abort();
  });
  window.addEventListener('pageshow', event => {
    if (event.persisted) refresh();
  });
  schedule();
})();
