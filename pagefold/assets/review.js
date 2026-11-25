function roleCard(entry, role) {
  return entry.querySelector(`.outcome[data-role="${CSS.escape(role)}"]`);
}

function renderPreview(select) {
  const entry = select.closest("[data-entry]");
  if (!entry) return;
  const preview = entry.querySelector("[data-selection-preview]");
  if (!preview) return;

  entry.querySelectorAll(".outcome").forEach((card) => {
    card.classList.toggle("is-preview", card.dataset.role === select.value);
  });
  entry.querySelectorAll(".diff-grid [data-role]").forEach((cell) => {
    cell.classList.toggle("preview-role", cell.dataset.role === select.value);
  });
  preview.replaceChildren();

  if (!select.value || select.value === "clear") {
    const waiting = document.createElement("span");
    waiting.className = "preview-waiting";
    waiting.textContent = "当前选择：保留未决；未保存前不会改变计划，未决时不能重演。";
    preview.append(waiting);
    return;
  }

  const card = roleCard(entry, select.value);
  if (!card) return;

  const line = document.createElement("span");
  line.className = "preview-line";
  const role = document.createElement("strong");
  role.textContent = card.querySelector("h4")?.textContent || select.value;
  const action = document.createElement("span");
  action.className = "preview-action";
  action.textContent = card.dataset.actionLabel;
  line.append(role, action);

  const code = document.createElement("code");
  code.className = "preview-json";
  code.textContent = card.dataset.rowJson;

  const hint = document.createElement("small");
  hint.textContent = "这是下拉框当前高亮、保存后将绑定到计划版本的对象；JSON 为 Pagefold 的类型化行语义。";
  preview.append(line, code, hint);
}

document.querySelectorAll("[data-choice-select]").forEach((select) => {
  select.addEventListener("change", () => renderPreview(select));
  renderPreview(select);

  const entry = select.closest("[data-entry]");
  entry?.querySelectorAll("[data-pick-role]").forEach((button) => {
    button.addEventListener("click", () => {
      select.value = button.dataset.pickRole;
      select.dispatchEvent(new Event("change", { bubbles: true }));
      select.focus();
    });
  });
});
