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

const groupForm = document.querySelector('#group-upload');
if (groupForm) {
  const status = document.querySelector('#group-status');
  groupForm.addEventListener('submit', async (event) => {
    event.preventDefault();
    const button = groupForm.querySelector('button');
    const roles = ['base', 'left', 'right'];
    if (roles.some((role) => !groupForm.elements[role].files[0])) {
      status.textContent = '请为祖本、左支、右支各选择一份文件。';
      return;
    }
    const body = new FormData();
    for (const role of roles) {
      body.append(role, groupForm.elements[role].files[0]);
    }
    button.disabled = true;
    status.textContent = '正在同一次事务内核对三份镜像…';
    try {
      // 提交成功即使本页未收到响应，重发同一批文件也只认同一组。
      const response = await fetch('/api/images', { method: 'POST', body });
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail);
      window.location.assign(`/groups/${data.id}`);
    } catch (error) {
      status.textContent = error.message || '组登记未完成，可直接重试同一批文件。';
      button.disabled = false;
    }
  });
}

const planGroup = document.querySelector('#plan-group');
if (planGroup && planGroup.value) {
  const selects = ['base', 'left', 'right'].map((name) => document.querySelector(`select[name="${name}"]`));
  const hint = document.querySelector('#group-hint');
  // 组已固定角色：禁用并取消必填，避免浏览器对禁用控件做原生校验拦截。
  selects.forEach((select) => { select.disabled = true; select.required = false; });
  if (hint) hint.textContent = '已选定镜像组，角色由该组固定；清除后可手工选择已登记镜像。';
  const clear = document.createElement('button');
  clear.type = 'button';
  clear.textContent = '清除镜像组';
  clear.addEventListener('click', () => {
    planGroup.value = '';
    selects.forEach((select) => { select.disabled = false; select.required = true; });
    clear.remove();
  });
  planGroup.closest('form').appendChild(clear);
}
