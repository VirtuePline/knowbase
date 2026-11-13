/* 所有页面共用的主题偏好；在样式加载前应用，避免刷新时闪回默认颜色。 */
(function (global) {
  "use strict";
  const KEY = "kb_ui_theme";
  const THEMES = {
    teal: { label: "青绿", chrome: "#102f2e" },
    blue: { label: "蓝色", chrome: "#152c49" },
    violet: { label: "紫色", chrome: "#302341" },
    amber: { label: "琥珀", chrome: "#382b20" },
  };
  const document = global.document;
  let active = "teal";
  function normalize(value) {
    return Object.prototype.hasOwnProperty.call(THEMES, value) ? value : "teal";
  }
  function render(announce) {
    document.documentElement.setAttribute("data-theme", active);
    document.querySelectorAll("[data-theme-select]").forEach((select) => { select.value = active; });
    const meta = document.querySelector('meta[name="theme-color"]');
    if (meta) meta.setAttribute("content", THEMES[active].chrome);
    document.querySelectorAll("[data-theme-status]").forEach((status) => {
      status.textContent = announce ? `已切换为${THEMES[active].label}主题` : "";
    });
  }
  function set(value, persist = true) {
    active = normalize(value);
    if (persist) {
      try { global.localStorage.setItem(KEY, active); } catch { /* 隐私模式仍允许本页切换 */ }
    }
    render(persist);
    return active;
  }
  try { active = normalize(global.localStorage.getItem(KEY)); } catch {}
  render(false);
  document.addEventListener("DOMContentLoaded", () => render(false));
  document.addEventListener("change", (event) => {
    if (event.target?.matches?.("[data-theme-select]")) set(event.target.value);
  });
  global.addEventListener("storage", (event) => {
    if (event.key === KEY) set(event.newValue, false);
  });
  global.KnowledgeTheme = { get: () => active, set };
})(window);
