(() => {
  // 两层刷新：
  // 1) /admin/status 轻量轮询，只更新当前动作、阶段、进度与计数，间隔即配置值；
  // 2) /admin 整页片段刷新只更新任务/来源/重建列表，间隔为其 6 倍（至少 15 秒），
  //    避免每秒重渲染表格，同时保证列表最终一致。
  const status = document.querySelector('#dashboard-refresh-status');
  if (!status) return;
  const seconds = Number(status.dataset.refreshSeconds);
  if (!Number.isFinite(seconds) || seconds < 1) return;
  const endpoint = status.dataset.liveEndpoint || '/admin/status';
  const liveInterval = seconds * 1000;
  const listInterval = Math.max(15000, seconds * 6000);
  let liveTimer;
  let listTimer;
  let liveRequest;
  let listRequest;
  let expired = false;
  let listDue = Date.now() + listInterval;
  let lastActivity = null;

  const setStatus = text => {
    if (status.textContent !== text) status.textContent = text;
  };

  const showExpired = () => {
    expired = true;
    clearTimeout(liveTimer);
    clearTimeout(listTimer);
    setStatus('登录已过期，请重新登录。');
    if (!status.querySelector('a')) {
      const link = document.createElement('a');
      link.href = '/admin/login';
      link.textContent = '重新登录';
      status.append(' ', link);
    }
  };

  const text = (selector, value) => {
    const node = document.querySelector(selector);
    if (node && value !== undefined && value !== null && node.textContent !== String(value)) {
      node.textContent = String(value);
    }
  };

  const duration = value => {
    const total = Math.max(0, Math.floor(Number(value) || 0));
    if (total < 1) return '不足 1 秒';
    if (total < 60) return `${total} 秒`;
    const minutes = Math.floor(total / 60);
    if (minutes < 60) return `${minutes} 分 ${String(total % 60).padStart(2, '0')} 秒`;
    const hours = Math.floor(minutes / 60);
    return `${hours} 小时 ${String(minutes % 60).padStart(2, '0')} 分`;
  };

  const ago = stamp => {
    const value = Number(stamp);
    if (!Number.isFinite(value) || value <= 0) return '尚未启动';
    return `${duration(Date.now() / 1000 - value)}前`;
  };

  const renderLive = data => {
    const active = data.kind === 'task' || data.kind === 'rebuild' || data.kind === 'poll' || data.kind === 'starting';
    const worker = document.querySelector('#activity-worker');
    if (worker) {
      worker.textContent = '';
      const dot = document.createElement('span');
      dot.className = 'state__dot';
      dot.setAttribute('aria-hidden', 'true');
      worker.append(dot, data.worker_alive ? '后台运行中' : '后台未运行');
      worker.className = `state state--${data.worker_alive ? 'ok' : 'danger'}`;
    }
    text('#activity-heartbeat', ago(data.worker_heartbeat));
    text('#activity-elapsed', active ? duration(data.elapsed) : '—');
    text('#activity-label', data.label || '—');
    const detail = document.querySelector('#activity-detail');
    if (detail) {
      const extra = data.task_id ? ` #${data.task_id} ${data.title || ''}` : (data.day ? ` ${data.day}` : '');
      const value = `${data.detail || ''}${extra}`.trim();
      if (detail.textContent !== value) detail.textContent = value;
    }
    const bar = document.querySelector('#activity-bar');
    if (bar) {
      // null/undefined 表示没有可量化进度；Number(null) 会误算成 0，必须先排除。
      const raw = data.progress;
      const percent = raw === null || raw === undefined || !data.total
        ? null
        : Math.min(100, Math.max(0, Number(raw) || 0));
      bar.style.width = `${percent === null ? 8 : percent}%`;
      bar.classList.toggle('progress__bar--idle', percent === null);
      const wrapper = bar.parentElement;
      if (wrapper) {
        if (percent === null) wrapper.removeAttribute('aria-valuenow');
        else wrapper.setAttribute('aria-valuenow', String(percent));
      }
    }
    const stageText = data.stage
      ? `${data.stage_label || data.stage}${data.current != null && data.total ? ` · ${data.current}/${data.total}` : ''}`
      : '—';
    text('#activity-stage', stageText);
    const counts = data.task_counts || {};
    const chips = document.querySelector('#activity-counts');
    if (chips) {
      const wanted = [
        ['chip--run', `处理中 ${counts.running || 0}`],
        ['chip--warn', `等待 ${counts.queued || 0}`],
        ['chip--danger', `失败 ${counts.failed || 0}`],
        ['chip--muted', `已完成 ${counts.done || 0}`],
      ];
      for (const job of data.active_rebuilds || []) {
        wanted.push(['chip--accent', `重建 ${job.day} · ${job.state_label}${Number.isFinite(job.progress) && job.progress !== null ? ` ${job.progress}%` : ''}`]);
      }
      const signature = wanted.map(([, label]) => label).join('|');
      if (chips.dataset.signature !== signature) {
        chips.dataset.signature = signature;
        chips.replaceChildren(...wanted.map(([cls, label]) => {
          const chip = document.createElement('span');
          chip.className = `chip ${cls}`;
          chip.textContent = label;
          return chip;
        }));
      }
    }
    lastActivity = data;
  };

  const scheduleLive = () => {
    clearTimeout(liveTimer);
    if (!expired && !document.hidden) liveTimer = setTimeout(pollLive, liveInterval);
  };

  const pollLive = async () => {
    if (expired || liveRequest || document.hidden) return;
    const current = new AbortController();
    liveRequest = current;
    const timeout = setTimeout(() => current.abort(), 8000);
    try {
      const response = await fetch(endpoint, {
        credentials: 'same-origin', cache: 'no-store', redirect: 'error',
        headers: {Accept: 'application/json'}, signal: current.signal,
      });
      if (response.status === 401 || response.status === 403) {
        showExpired();
        return;
      }
      if (!response.ok) throw new Error('live');
      renderLive(await response.json());
      setStatus('状态自动更新中');
    } catch (_) {
      if (!document.hidden) setStatus('状态更新暂时失败，正在自动重试。');
    } finally {
      clearTimeout(timeout);
      liveRequest = null;
      scheduleLive();
    }
  };

  const scheduleList = () => {
    clearTimeout(listTimer);
    if (!expired && !document.hidden) listTimer = setTimeout(pollList, Math.max(1000, listDue - Date.now()));
  };

  const pollList = async () => {
    if (expired || listRequest || document.hidden) return;
    listDue = Date.now() + listInterval;
    const sortVersion = document.querySelector('#source-list')?.dataset.sortVersion || '';
    const current = new AbortController();
    listRequest = current;
    const timeout = setTimeout(() => current.abort(), 12000);
    try {
      const response = await fetch('/admin', {
        credentials: 'same-origin', cache: 'no-store', redirect: 'error', signal: current.signal,
      });
      if (response.status === 401 || response.status === 403) {
        showExpired();
        return;
      }
      if (!response.ok) throw new Error('list');
      const page = new DOMParser().parseFromString(await response.text(), 'text/html');
      if (!page.querySelector('#dashboard-refresh-status')) throw new Error('list');
      const csrf = page.querySelector('input[name="token"]')?.value;
      if (csrf) {
        document.querySelectorAll('input[name="token"]').forEach(input => { input.value = csrf; });
        const sources = document.querySelector('#source-list');
        if (sources) sources.dataset.token = csrf;
      }
      const regions = ['sources', 'task-panel', 'task-count', 'source-count'].map(name => ({
        target: document.querySelector(`[data-live-region="${name}"]`),
        next: page.querySelector(`[data-live-region="${name}"]`),
      })).filter(region => region.target && region.next);
      for (const {target, next} of regions) {
        // 保留正在操作的表格与排序过程，不触碰订阅/视频/日期输入表单。
        if (target.contains(document.activeElement)) continue;
        if (target.id === 'source-list' && target.dataset.sorting === 'true') continue;
        if (target.id === 'source-list' && (target.dataset.sortVersion || '') !== sortVersion) continue;
        if (target.innerHTML === next.innerHTML) continue;
        target.replaceChildren(...next.childNodes);
        target.dispatchEvent(new Event('dashboard:updated'));
      }
    } catch (_) {
      // 列表刷新失败不回退实时状态文案，下一次照常重试。
    } finally {
      clearTimeout(timeout);
      listRequest = null;
      scheduleList();
    }
  };

  document.addEventListener('visibilitychange', () => {
    clearTimeout(liveTimer);
    clearTimeout(listTimer);
    if (document.hidden) {
      liveRequest?.abort();
      listRequest?.abort();
    } else {
      pollLive();
      if (Date.now() >= listDue) pollList();
      else scheduleList();
    }
  });
  window.addEventListener('pagehide', () => {
    clearTimeout(liveTimer);
    clearTimeout(listTimer);
    liveRequest?.abort();
    listRequest?.abort();
  });
  window.addEventListener('pageshow', event => {
    if (event.persisted) {
      pollLive();
      pollList();
    }
  });
  scheduleLive();
  listDue = Date.now() + listInterval;
  scheduleList();
})();
