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

document.querySelectorAll('[data-witness-image],[data-witness-table]').forEach((select) => {
  select.addEventListener('change', () => {
    const form = select.closest('form');
    if (!form) return;
    const step = form.querySelector('button[name="step"]');
    if (step && form.requestSubmit) form.requestSubmit(step);
    else form.submit();
  });
});
