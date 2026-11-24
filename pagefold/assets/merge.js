// 列合成表单：只有选择“列合成”时才展示逐列来源选择，且禁用的字段不会提交。
document.addEventListener("DOMContentLoaded", () => {
  for (const form of document.querySelectorAll("form.decision")) {
    const mode = form.querySelector("select.decision-mode");
    const picker = form.querySelector(".column-picker");
    if (!mode || !picker) continue;
    const sync = () => {
      const combining = mode.value === "combine";
      picker.hidden = !combining;
      for (const select of picker.querySelectorAll("select")) {
        select.disabled = !combining;
      }
    };
    mode.addEventListener("change", sync);
    sync();
  }
});
