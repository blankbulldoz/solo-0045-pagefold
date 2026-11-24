const uploadForm = document.querySelector('#image-upload');
if (uploadForm) {
  const status = document.querySelector('#upload-status');
  uploadForm.addEventListener('submit', async (event) => {
    event.preventDefault();
    const input = uploadForm.elements.image;
    const button = uploadForm.querySelector('button');
    button.disabled = true;
    status.textContent = '正在读取 SQLite 镜像…';
    try {
      const response = await fetch('/api/images', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/octet-stream',
          'X-Image-Name': encodeURIComponent(input.files[0].name)
        },
        body: input.files[0]
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail);
      window.location.assign(`/images/${data.id}`);
    } catch (error) {
      status.textContent = error.message || '登记未完成。';
      button.disabled = false;
    }
  });
}

// 人工合成行：选择来源时填充类型化单元格，并用 localStorage 暂存未提交编辑，
// 以应对“离开后回来”。草稿绑定计划版本；版本被另一窗口推进后自动失效。
document.querySelectorAll('form.decision[data-item]').forEach((form) => {
  const item = form.dataset.item;
  const revision = form.dataset.revision;
  const draftKey = `pagefold:draft:${form.action.split('/plans/')[1]?.split('/')[0] || 'plan'}:${item}`;
  const choice = form.querySelector('.choice-select');
  const note = document.createElement('p');
  note.className = 'draft-note';
  form.appendChild(note);

  let rows = {};
  try { rows = JSON.parse(form.dataset.rows || '{}'); } catch (_) { rows = {}; }

  const cells = () => Array.from(form.querySelectorAll('.cell'));

  function fillRow(row) {
    if (!Array.isArray(row)) return;
    cells().forEach((cell, index) => {
      const atom = row[index];
      const type = cell.querySelector('.cell-type');
      const value = cell.querySelector('.cell-value');
      if (!atom || !type || !value) return;
      type.value = atom[0];
      value.value = atom[0] === 'n' ? '' : (atom[1] ?? '');
    });
  }

  function syncManual() {
    form.classList.toggle('manual-on', choice.value === 'manual');
  }

  function saveDraft() {
    const draft = { revision, fields: {} };
    new FormData(form).forEach((value, key) => { draft.fields[key] = value; });
    try {
      localStorage.setItem(draftKey, JSON.stringify(draft));
      note.textContent = '未提交的人工行编辑已暂存在本浏览器，离开后可回来继续。';
      note.classList.add('show');
    } catch (_) { /* 隐私模式等场景下静默降级 */ }
  }

  function restoreDraft() {
    let draft = null;
    try { draft = JSON.parse(localStorage.getItem(draftKey)); } catch (_) { draft = null; }
    if (!draft) return false;
    if (draft.revision !== revision) {
      localStorage.removeItem(draftKey);
      return false;
    }
    Object.entries(draft.fields || {}).forEach(([key, value]) => {
      const element = form.elements.namedItem(key);
      if (!element) return;
      if (typeof RadioNodeList !== 'undefined' && element instanceof RadioNodeList) {
        element.value = value;
      } else {
        element.value = value;
      }
    });
    syncManual();
    note.textContent = '已恢复本浏览器暂存的未提交编辑；提交时仍会校验当前版本。';
    note.classList.add('show');
    return true;
  }

  form.classList.add('collapsible');
  const restored = restoreDraft();
  if (!restored) syncManual();

  choice.addEventListener('change', () => {
    syncManual();
    if (choice.value === 'manual') fillRow(rows.manual || rows.base);
    else if (['base', 'left', 'right'].includes(choice.value)) fillRow(rows[choice.value]);
    saveDraft();
  });
  form.addEventListener('input', saveDraft);
  form.addEventListener('submit', saveDraft);
});
