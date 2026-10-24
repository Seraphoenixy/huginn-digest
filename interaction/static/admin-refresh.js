(() => {
  // 三层刷新，任何一层都不整页重绘：
  // 1) /admin/status 轻量轮询，只更新当前动作、阶段、进度与计数，间隔即配置值；
  // 2) /admin 片段刷新只更新 [data-live-region] 区域，间隔为其 6 倍（至少 15 秒）；
  // 3) 表单提交与视图切换用 fetch，成功后就地替换同一批区域，不重新加载页面。
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
  let actionRequest = null;

  const setStatus = text => {
    if (status.textContent !== text) status.textContent = text;
  };

  const actionStatus = document.querySelector('#dashboard-action-status');
  const say = (text, failed = false) => {
    if (!actionStatus) return;
    if (actionStatus.textContent !== text) actionStatus.textContent = text;
    actionStatus.classList.toggle('text-danger', Boolean(failed));
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

  // 把另一个文档里的同源片段换进来；只动 [data-live-region]，页面其余部分（含滚动位置）保持不变。
  const applyRegions = (page, sortVersion) => {
    const csrf = page.querySelector('input[name="token"]')?.value;
    if (csrf) {
      document.querySelectorAll('input[name="token"]').forEach(input => { input.value = csrf; });
      const sources = document.querySelector('#source-list');
      if (sources) sources.dataset.token = csrf;
    }
    const current = page.querySelector('a.seg.seg--current');
    if (current) {
      const href = current.getAttribute('href');
      document.querySelectorAll('a.seg').forEach(link => {
        const on = link.getAttribute('href') === href;
        link.classList.toggle('seg--current', on);
        if (on) link.setAttribute('aria-current', 'page');
        else link.removeAttribute('aria-current');
      });
    }
    document.querySelectorAll('[data-live-region]').forEach(target => {
      // 外层区域换掉后，被包含的内层区域已脱离文档，无需再处理。
      if (!target.isConnected) return;
      const next = page.querySelector(`[data-live-region="${target.dataset.liveRegion}"]`);
      if (!next) return;
      // 已有未保存改动时不换，避免轮询把正在填的内容清掉。
      const dirty = [...target.querySelectorAll('input, select, textarea')]
        .some(field => field.type !== 'hidden' && field.value !== field.defaultValue);
      if (dirty) return;
      // 正在输入的控件不换，避免打断输入与选择；按钮失焦可以接受。
      const focused = document.activeElement;
      const editing = target.contains(focused) && focused !== target
        && (focused.matches('input, textarea, select') || focused.isContentEditable);
      if (editing) return;
      if (target.id === 'source-list' && target.dataset.sorting === 'true') return;
      if (sortVersion !== undefined && target.id === 'source-list'
        && (target.dataset.sortVersion || '') !== sortVersion) return;
      if (target.innerHTML === next.innerHTML) return;
      target.replaceChildren(...next.childNodes);
      target.dispatchEvent(new Event('dashboard:updated'));
    });
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
      const response = await fetch(`${location.pathname}${location.search}`, {
        credentials: 'same-origin', cache: 'no-store', redirect: 'error', signal: current.signal,
      });
      if (response.status === 401 || response.status === 403) {
        showExpired();
        return;
      }
      if (!response.ok) throw new Error('list');
      const page = new DOMParser().parseFromString(await response.text(), 'text/html');
      if (!page.querySelector('#dashboard-refresh-status')) throw new Error('list');
      applyRegions(page, sortVersion);
    } catch (_) {
      // 列表刷新失败不回退实时状态文案，下一次照常重试。
    } finally {
      clearTimeout(timeout);
      listRequest = null;
      scheduleList();
    }
  };

  const detailOf = async response => {
    const type = response.headers.get('content-type') || '';
    if (!type.includes('application/json')) return '';
    try {
      const data = await response.json();
      return data && data.detail ? String(data.detail) : '';
    } catch (_) {
      return '';
    }
  };

  // 写操作：提交后按重定向结果就地替换区域，不触发整页导航。
  const submitAction = async (form, submitter) => {
    if (actionRequest) return;
    const button = submitter && submitter.tagName === 'BUTTON' ? submitter : form.querySelector('button');
    const sortVersion = document.querySelector('#source-list')?.dataset.sortVersion || '';
    const current = new AbortController();
    actionRequest = current;
    if (button) button.disabled = true;
    say('正在提交…');
    try {
      const response = await fetch(form.action, {
        method: 'POST', body: new FormData(form), credentials: 'same-origin',
        redirect: 'follow', signal: current.signal,
        headers: {Accept: 'text/html, application/json'},
      });
      if (response.status === 401) {
        showExpired();
        return;
      }
      if (!response.ok) {
        const detail = await detailOf(response);
        say(detail || `操作失败（${response.status}），页面内容未改动。`, true);
        return;
      }
      const page = new DOMParser().parseFromString(await response.text(), 'text/html');
      if (!page.querySelector('#dashboard-refresh-status')) throw new Error('page');
      // 提交后让输入框失焦，区域才会被替换成服务端的新值。
      if (form.contains(document.activeElement)) document.activeElement.blur();
      applyRegions(page, sortVersion);
      // 提交成功后才清空输入，失败时保留用户已填内容。
      if (form.hasAttribute('data-reset')) form.reset();
      if (response.url && response.url !== location.href) history.replaceState(null, '', response.url);
      say('已更新');
      listDue = Date.now() + listInterval;
      pollLive();
    } catch (_) {
      say('操作提交失败，请重试；页面内容未改动。', true);
    } finally {
      actionRequest = null;
      if (button) button.disabled = false;
    }
  };

  // 视图切换：只换任务面板与计数，地址栏同步，前进后退仍可用。
  const navigate = async (url, push) => {
    if (actionRequest) return;
    const current = new AbortController();
    actionRequest = current;
    try {
      const response = await fetch(url, {
        credentials: 'same-origin', cache: 'no-store', redirect: 'error',
        signal: current.signal, headers: {Accept: 'text/html'},
      });
      if (response.status === 401) {
        showExpired();
        return;
      }
      if (!response.ok) {
        location.href = url;
        return;
      }
      const page = new DOMParser().parseFromString(await response.text(), 'text/html');
      if (!page.querySelector('#dashboard-refresh-status')) {
        location.href = url;
        return;
      }
      applyRegions(page);
      if (push) history.pushState(null, '', url);
      say('');
      listDue = Date.now() + listInterval;
      pollLive();
    } catch (_) {
      location.href = url;
    } finally {
      actionRequest = null;
    }
  };

  document.addEventListener('submit', event => {
    const form = event.target;
    if (!(form instanceof HTMLFormElement)) return;
    if (expired) {
      event.preventDefault();
      return;
    }
    if (form.hasAttribute('data-async')) {
      event.preventDefault();
      submitAction(form, event.submitter);
      return;
    }
    if (form.hasAttribute('data-async-nav')) {
      event.preventDefault();
      const target = new URL(form.action, location.href);
      new URLSearchParams(new FormData(form)).forEach((value, key) => target.searchParams.set(key, value));
      navigate(target.toString(), true);
    }
  });

  document.addEventListener('click', event => {
    if (event.defaultPrevented || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
    const link = event.target.closest('a[data-async-nav]');
    if (!link || expired) return;
    event.preventDefault();
    navigate(link.href, true);
  });

  window.addEventListener('popstate', () => {
    if (!expired) navigate(location.href, false);
  });

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
    actionRequest?.abort();
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
