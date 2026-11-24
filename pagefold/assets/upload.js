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

const planForm = document.querySelector('.new-plan');
const identityHint = document.querySelector('#source-identity-hint');
if (planForm && identityHint) {
  const pairLabels = [['base', 'left', '祖本 ↔ 左支'], ['base', 'right', '祖本 ↔ 右支'], ['left', 'right', '左支 ↔ 右支']];
  const verdictText = {
    same_file: '同一文件摘要',
    defragmented_equivalent: '碎片整理后等价',
    type_shifted_lookalike: '行位置相同但类型不同',
    different: '内容不同'
  };
  async function refreshIdentityHint() {
    const picked = Object.fromEntries(['base', 'left', 'right'].map((name) => [name, planForm.elements[name].value]));
    const selected = pairLabels.filter(([a, b]) => picked[a] && picked[b]);
    if (!selected.length) { identityHint.innerHTML = ''; return; }
    identityHint.textContent = '正在核对两种身份…';
    try {
      const reports = await Promise.all(selected.map(async ([a, b, label]) => {
        const response = await fetch(`/api/images/compare?a=${picked[a]}&b=${picked[b]}`);
        if (!response.ok) throw new Error('identity');
        return [label, await response.json()];
      }));
      identityHint.innerHTML = reports.map(([label, report]) => {
        const cls = report.verdict === 'defragmented_equivalent' ? 'eq'
          : report.verdict === 'type_shifted_lookalike' ? 'shift' : '';
        const extra = report.verdict === 'type_shifted_lookalike' ? ` · ${report.type_change_count} 处类型差异` : '';
        return `<a class="identity-pair ${cls}" href="/images/compare?a=${report.a.id}&b=${report.b.id}"><b>${label}</b><span>${verdictText[report.verdict] || report.verdict_label}${extra}</span></a>`;
      }).join('');
    } catch (error) {
      identityHint.textContent = '身份对照暂不可用，仍可直接创建计划。';
    }
  }
  ['base', 'left', 'right'].forEach((name) => planForm.elements[name].addEventListener('change', refreshIdentityHint));
}

