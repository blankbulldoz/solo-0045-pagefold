document.querySelectorAll('[data-batch-form]').forEach((form) => {
  const PENDING = 'pagefold-batch-pending';
  const RECEIPT = 'pagefold-batch-receipt';
  const plan = form.dataset.plan;
  const group = form.dataset.group;
  const preview = form.querySelector('[data-batch-preview]');
  const apply = form.querySelector('[data-batch-apply]');
  const all = form.querySelector('[data-batch-all]');
  const revision = () => Number(form.dataset.revision);
  let confirmation = null;
  let busy = false;

  const escape = (value) => {
    const node = document.createElement('span');
    node.textContent = String(value);
    return node.innerHTML;
  };

  const label = (choice) => ({ base: '祖本', left: '左支', right: '右支', clear: '保留未决' })[choice] || '未决';

  const describe = (rows) => (rows || []).map((row) => {
    const key = row.key.map((atom) => atom[0] === 'n' ? 'NULL' : atom[1]).join(' / ');
    return `<li><code>${escape(row.table)} ${escape(key)}</code> ` +
      (row.from !== undefined
        ? `<span class="tag">${escape(label(row.from))} → ${escape(label(row.to))}</span>`
        : '<span class="muted">与当前值相同，不改变</span>') + '</li>';
  }).join('');

  // Storage is best effort: private windows still submit, they just cannot
  // offer the post-disconnect replay affordance.
  const store = (slot, value) => {
    try {
      if (value === null) localStorage.removeItem(slot);
      else localStorage.setItem(slot, JSON.stringify(value));
    } catch (error) { /* unavailable */ }
  };

  const read = (slot) => {
    try { return JSON.parse(localStorage.getItem(slot) || 'null'); } catch (error) { return null; }
  };

  const entries = () => Array.from(form.querySelectorAll('[data-batch-row]')).map((row) => ({
    item: row.dataset.item,
    choice: row.querySelector('[data-batch-choice]').value
  }));

  const post = async (route, body) => {
    const response = await fetch(`/api/plans/${plan}/${route}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body)
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || '本次提交未完成。');
    return data;
  };

  const summarize = (result) =>
    `<h3>${result.changed} 行实际改变${result.replayed ? ' · 重演既有确认' : ''}</h3>` +
    (describe(result.changes) ? `<ul>${describe(result.changes)}</ul>` : '<p class="muted">没有实际变化。</p>') +
    (result.unchanged && result.unchanged.length
      ? `<h3>${result.unchanged.length} 行与当前值相同</h3><ul>${describe(result.unchanged)}</ul>` : '') +
    `<p class="muted">计划版本 ${result.base_revision ?? revision()} → ${result.revision}，` +
    `当前第 ${result.current_revision ?? result.revision} 版 · 未决 ${result.unresolved_before} → ${result.unresolved_after}` +
    ` · 确认摘要 <code>${escape((result.confirmation || '').slice(0, 16))}</code></p>`;

  const settle = (result) => {
    store(RECEIPT, { plan, revision: result.revision, current_revision: result.current_revision,
                     confirmation: result.confirmation, changed: result.changed,
                     changes: result.changes, unchanged: result.unchanged,
                     unresolved: result.unresolved_after });
    store(PENDING, null);
    window.location.assign(`/plans/${plan}`);
  };

  if (all) {
    all.addEventListener('change', () => {
      form.querySelectorAll('[data-batch-choice]').forEach((select) => { select.value = all.value; });
      confirmation = null;
      apply.disabled = true;
    });
  }

  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    if (busy) return;
    busy = true;
    apply.disabled = true;
    try {
      if (event.submitter && event.submitter.value === 'apply') {
        if (!confirmation) return;
        const body = { revision: revision(), entries: entries(), confirmation };
        // Record the exact confirmation *before* sending it. If the socket
        // drops, the stored receipt lets the operator resend the same
        // confirmation; the server applies it once and replays thereafter.
        store(PENDING, Object.assign({ plan, group }, body));
        settle(await post('decisions', body));
        return;
      }
      const result = await post('decisions/preview', { revision: revision(), entries: entries() });
      confirmation = result.confirmation;
      apply.disabled = result.changed === 0;
      preview.innerHTML = summarize(Object.assign({}, result, { base_revision: revision() }));
    } catch (error) {
      confirmation = null;
      apply.disabled = true;
      preview.innerHTML = `<p class="error-text">${escape(error.message)}</p>`;
    } finally {
      busy = false;
    }
  });

  const pending = read(PENDING);
  const receipt = read(RECEIPT);
  if (receipt && receipt.plan === plan) {
    preview.innerHTML = `<h3>上次确认的提交已落盘</h3><ul>${describe(receipt.changes)}</ul>` +
      (receipt.unchanged && receipt.unchanged.length
        ? `<ul>${describe(receipt.unchanged)}</ul>` : '') +
      `<p class="muted">第 ${receipt.revision} 版 · 剩余 ${receipt.unresolved} 项未决 · 确认摘要 ` +
      `<code>${escape((receipt.confirmation || '').slice(0, 16))}</code></p>`;
  } else if (pending && pending.plan === plan && pending.group === group) {
    const retry = document.createElement('button');
    retry.type = 'button';
    retry.textContent = '重发上次确认';
    retry.addEventListener('click', async () => {
      retry.disabled = true;
      try {
        settle(await post('decisions', pending));
      } catch (error) {
        preview.innerHTML = `<p class="error-text">${escape(error.message)}</p>`;
        retry.disabled = false;
      }
    });
    preview.innerHTML = `<h3>有一份结果未确认的提交</h3><p class="muted">确认摘要 ` +
      `<code>${escape((pending.confirmation || '').slice(0, 16))}</code> · 计划版本 ${pending.revision}。` +
      '浏览器在这期间断线；重发同一确认只会生效一次。</p>';
    preview.appendChild(retry);
  }
});
