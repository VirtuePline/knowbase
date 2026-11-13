// Run with: node --test tests/test_theme.js
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");
const source = fs.readFileSync(path.join(__dirname, "../web/theme.js"), "utf8");
const css = fs.readFileSync(path.join(__dirname, "../web/theme.css"), "utf8");

function preview(stored, denied = false) {
  const root = {};
  const meta = {};
  const controls = [];
  const statuses = [{ textContent: "" }];
  const storage = new Map(stored === undefined ? [] : [["kb_ui_theme", stored]]);
  const documentEvents = new Map();
  const windowEvents = new Map();
  const document = {
    documentElement: { setAttribute(name, value) { root[name] = value; } },
    querySelectorAll(selector) { return selector === "[data-theme-select]" ? controls : statuses; },
    querySelector() { return { setAttribute(name, value) { meta[name] = value; } }; },
    addEventListener(name, handler) { documentEvents.set(name, handler); },
  };
  const window = {
    document,
    localStorage: {
      getItem(key) { if (denied) throw new Error("storage denied"); return storage.get(key); },
      setItem(key, value) { if (denied) throw new Error("storage denied"); storage.set(key, value); },
    },
    addEventListener(name, handler) { windowEvents.set(name, handler); },
  };
  vm.runInNewContext(source, { window });
  return { root, meta, controls, statuses, storage, theme: window.KnowledgeTheme,
    ready() { documentEvents.get("DOMContentLoaded")(); },
    choose(value) { documentEvents.get("change")({ target: { value, matches: () => true } }); },
    storageChange(value) { windowEvents.get("storage")({ key: "kb_ui_theme", newValue: value }); },
  };
}

test("all shipped pages apply the theme before loading styles and expose an accessible control", () => {
  for (const page of ["index.html", "login.html", "register.html", "change-password.html"]) {
    const html = fs.readFileSync(path.join(__dirname, "../web", page), "utf8");
    assert.ok(html.indexOf('/static/theme.js') < html.indexOf('rel="stylesheet"'), page);
    assert.match(html, /href="\/static\/theme\.css"/);
    assert.match(html, /data-theme-select aria-label="主题颜色"/);
    for (const value of ["teal", "blue", "violet", "amber"]) assert.match(html, new RegExp(`option value="${value}"`));
  }
});

test("saved color is applied immediately and controls synchronize once parsed", () => {
  const page = preview("blue");
  assert.equal(page.root["data-theme"], "blue");
  assert.equal(page.meta.content, "#152c49");
  page.controls.push({ value: "teal" }, { value: "teal" });
  page.ready();
  assert.deepEqual(page.controls.map((control) => control.value), ["blue", "blue"]);
});

test("changing colors saves the preference, syncs controls and announces the choice", () => {
  const page = preview();
  page.controls.push({ value: "teal" });
  page.choose("violet");
  assert.equal(page.theme.get(), "violet");
  assert.equal(page.controls[0].value, "violet");
  assert.equal(page.storage.get("kb_ui_theme"), "violet");
  assert.equal(page.statuses[0].textContent, "已切换为紫色主题");
  page.choose("amber");
  assert.equal(page.meta.content, "#382b20");
  assert.equal(page.storage.get("kb_ui_theme"), "amber");
});

test("unknown and prototype names fall back to teal without copying untrusted values into the page", () => {
  for (const value of ["unknown", "constructor", "__proto__", '<img src=x onerror=alert(1)>']) {
    const page = preview(value);
    assert.equal(page.root["data-theme"], "teal");
    assert.equal(page.theme.set(value), "teal");
    assert.equal(page.storage.get("kb_ui_theme"), "teal");
  }
});

test("privacy mode permits switching even when preference storage is unavailable", () => {
  const page = preview("blue", true);
  assert.equal(page.theme.get(), "teal");
  assert.doesNotThrow(() => page.choose("blue"));
  assert.equal(page.root["data-theme"], "blue");
});

test("other tabs synchronize changes and clearing storage restores the default", () => {
  const page = preview("teal");
  page.storageChange("violet");
  assert.equal(page.root["data-theme"], "violet");
  assert.equal(page.storage.get("kb_ui_theme"), "teal"); // 不反向写入并触发循环事件。
  page.storageChange(null);
  assert.equal(page.root["data-theme"], "teal");
});

test("palette overrides brand colors without redefining semantic status badges", () => {
  for (const value of ["blue", "violet", "amber"]) assert.ok(css.includes(`html[data-theme="${value}"]`));
  assert.match(css, /header\.app-sidebar\s*\{[^}]*var\(--theme-sidebar\)/);
  assert.match(css, /\.record-attachment-drop\.over/);
  assert.doesNotMatch(css, /\.tl-badge|\.account-error|\.account-success|\.record-attachment-status\.error/);
});
