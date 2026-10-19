const list = document.querySelector('#source-list');
if (list) {
  let dragged = null;
  let saving = false;
  const message = document.querySelector('#source-order-status');
  let saved = Array.from(list.querySelectorAll('[data-source-id]'));
  const persist = async () => {
    saving = true;
    message.textContent = '正在保存顺序…';
    try {
      const body = new URLSearchParams({token: list.dataset.token,
        order: Array.from(list.querySelectorAll('[data-source-id]')).map(row => row.dataset.sourceId).join(',')});
      const response = await fetch('/admin/sources/order', {method: 'POST', body});
      if (!response.ok) throw new Error('save');
      saved = Array.from(list.querySelectorAll('[data-source-id]'));
      message.textContent = '顺序已保存';
    } catch (_) {
      saved.forEach(row => list.appendChild(row));
      message.textContent = '保存失败，已恢复顺序；请刷新后重试。';
    } finally { saving = false; }
  };
  list.querySelectorAll('.source-grip').forEach(handle => {
    handle.addEventListener('dragstart', event => {
      if (saving) { event.preventDefault(); return; }
      dragged = handle.closest('tr');
      event.dataTransfer.setData('text/plain', dragged.dataset.sourceId);
      event.dataTransfer.effectAllowed = 'move';
    });
    handle.addEventListener('dragend', () => {
      if (dragged) saved.forEach(row => list.appendChild(row));
      dragged = null;
    });
    handle.addEventListener('keydown', event => {
      if (saving || !['ArrowUp', 'ArrowDown'].includes(event.key)) return;
      event.preventDefault();
      const row = handle.closest('tr');
      const other = event.key === 'ArrowUp' ? row.previousElementSibling : row.nextElementSibling;
      if (!other) return;
      if (event.key === 'ArrowUp') list.insertBefore(row, other);
      else list.insertBefore(other, row);
      handle.focus(); persist();
    });
  });
  list.addEventListener('dragover', event => {
    if (!dragged || saving) return;
    event.preventDefault();
    const row = event.target.closest('tr[data-source-id]');
    if (!row || row === dragged) return;
    const rect = row.getBoundingClientRect();
    list.insertBefore(dragged, event.clientY < rect.top + rect.height / 2 ? row : row.nextSibling);
  });
  list.addEventListener('drop', event => {
    if (!dragged || saving) return;
    event.preventDefault(); dragged = null; persist();
  });
}
