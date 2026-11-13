const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const html = fs.readFileSync(path.join(__dirname, "../web/index.html"), "utf8");
const script = html.match(/<script>([\s\S]*?)<\/script>/)[1];
function shippedFunction(name) {
  const match = script.match(new RegExp(`^(?:async )?function ${name}\\([^]*?^}`, "m"));
  assert.ok(match, `Missing function: ${name}`);
  return match[0];
}

function element() {
  return {
    hidden: false, textContent: "", innerHTML: "", handlers: {}, focusCount: 0,
    addEventListener(type, handler) { this.handlers[type] = handler; },
    focus() { this.focusCount += 1; },
  };
}
function setup({ tags = true, shippedEditor = false } = {}) {
  const btn = element(), sub = element(), add = element(), opened = [];
  let changes = 0;
  sub.querySelector = (selector) => selector === "[data-add-category]" && sub.innerHTML.includes("data-add-category") ? add : null;
  const context = vm.createContext({
    btn, sub, tags,
    $: (selector) => ({ "#md-cat-btn": btn, "#md-subcats": sub })[selector],
    selectableCategories: () => ["网络问题", "硬件问题", "厂商系统", '<自定义"分类>'],
    CATEGORY_SUBS: { "厂商系统": { label: "厂商", required: true, getOptions: () => ["金蝶", '设备<厂商"A>'] } },
    catDialog: { open(options) { opened.push(options); } },
    maybeApplyTemplate: () => { changes += 1; },
    changed: () => { changes += 1; },
  });
  vm.runInContext("let pickerSeq = 0;", context);
  for (const name of ["escapeHtml", "escapeAttr", "createCategoryPicker"]) vm.runInContext(shippedFunction(name), context);
  if (shippedEditor) {
    const start = script.indexOf("const mdPicker = createCategoryPicker(");
    const end = script.indexOf("const mdTime =", start);
    assert.ok(start >= 0 && end > start, "Missing editor picker initialization");
    vm.runInContext(script.slice(start, end), context);
  }
  const picker = vm.runInContext(shippedEditor ? "mdPicker" : 'createCategoryPicker({ btn, sub, tags, emptyLabel: tags ? "选择分类" : "自动判定", onChange: changed })', context);
  const click = (kind, category) => {
    const target = { closest: (selector) => selector === (kind === "remove" ? "[data-remove-category]" : "[data-add-category]") ? { dataset: { removeCategory: category } } : null };
    sub.handlers.click({ target, stopPropagation() {} });
  };
  return { btn, sub, add, picker, opened, click, changes: () => changes };
}

test("the editor starts with choose-category and keeps an empty selection for automatic classification", () => {
  const { btn, sub, picker, opened } = setup({ shippedEditor: true });
  assert.equal(btn.textContent, "选择分类");
  assert.equal(btn.hidden, false);
  assert.equal(sub.innerHTML, "");
  assert.equal(picker.isEmpty(), true);
  assert.deepEqual(Array.from(picker.selected()), []);
  btn.handlers.click({ stopPropagation() {} });
  assert.equal(opened[0].allowAuto, true);
  assert.deepEqual(Array.from(opened[0].selected), []);
});

test("confirmed categories show leaf labels while keeping parent and vendor values for saving", () => {
  const { btn, sub, picker, opened } = setup({ shippedEditor: true });
  btn.handlers.click({ stopPropagation() {} });
  opened[0].onConfirm(["厂商系统", "网络问题"], { "厂商系统": "金蝶" });
  assert.equal(btn.hidden, true);
  assert.deepEqual(Array.from(picker.selected()), ["厂商系统", "网络问题"]);
  assert.equal(picker.subValue("厂商系统"), "金蝶");
  assert.match(sub.innerHTML, /<span>金蝶<\/span>/);
  assert.doesNotMatch(sub.innerHTML, /<span>厂商系统<\/span>|type="radio"/);
  assert.match(sub.innerHTML, /title="厂商系统 \/ 金蝶"/);
  assert.match(sub.innerHTML, /<span>网络问题<\/span>/);
  assert.ok(sub.innerHTML.lastIndexOf("data-add-category") > sub.innerHTML.lastIndexOf("data-remove-category"));
});

test("the trailing plus reopens the dialog with current categories and does not duplicate them", () => {
  const { picker, click, opened } = setup();
  picker.set(["厂商系统", "网络问题"]);
  picker.setSub("厂商系统", "金蝶");
  click("add");
  assert.deepEqual(Array.from(opened[0].selected), ["厂商系统", "网络问题"]);
  assert.equal(opened[0].subs["厂商系统"], "金蝶");
  opened[0].onConfirm(["厂商系统", "网络问题", "硬件问题", "网络问题"], { "厂商系统": "金蝶" });
  assert.deepEqual(Array.from(picker.selected()), ["厂商系统", "网络问题", "硬件问题"]);
});

test("removing a leaf removes its parent and child value, then removing the final tag restores automatic mode", () => {
  const { btn, sub, add, picker, click, changes } = setup();
  picker.set(["厂商系统", "网络问题"]);
  picker.setSub("厂商系统", "金蝶");
  const before = changes();
  click("remove", "厂商系统");
  assert.deepEqual(Array.from(picker.selected()), ["网络问题"]);
  assert.equal(picker.subValue("厂商系统"), "");
  assert.equal(btn.hidden, true);
  assert.equal(add.focusCount, 1);
  assert.equal(changes(), before + 1);
  click("remove", "网络问题");
  assert.equal(picker.isEmpty(), true);
  assert.equal(btn.textContent, "选择分类");
  assert.equal(btn.hidden, false);
  assert.equal(sub.innerHTML, "");
  assert.equal(btn.focusCount, 1);
  picker.set(["厂商系统"]);
  assert.equal(picker.subValue("厂商系统"), "", "re-adding a removed parent must not restore a stale child");
});

test("edit prefills, refreshes and reset retain safe labels and consistent automatic state", () => {
  const { btn, sub, picker, opened, click } = setup();
  picker.set(["厂商系统", '<自定义"分类>', "unknown"]);
  picker.setSub("厂商系统", '设备<厂商"A>');
  picker.refresh();
  assert.equal(btn.hidden, true);
  assert.equal(picker.subValue("厂商系统"), '设备<厂商"A>');
  assert.match(sub.innerHTML, /<span>设备&lt;厂商"A&gt;<\/span>/);
  assert.match(sub.innerHTML, /<span>&lt;自定义"分类&gt;<\/span>/);
  assert.match(sub.innerHTML, /data-remove-category="<自定义&quot;分类>"/);
  assert.doesNotMatch(sub.innerHTML, /<span><自定义|<span>设备<厂商|unknown/);
  click("add");
  opened[0].onConfirm([], { "厂商系统": "金蝶" });
  assert.equal(picker.isEmpty(), true);
  assert.equal(picker.subValue("厂商系统"), "");
  assert.equal(btn.hidden, false);
  assert.equal(btn.textContent, "选择分类");
  assert.equal(sub.innerHTML, "");
  picker.set(["网络问题"]);
  picker.set([]);
  assert.equal(btn.hidden, false);
  assert.equal(sub.innerHTML, "");
});

test("the upload picker preserves its category button and secondary radio interaction", () => {
  const { btn, sub, picker, opened, changes } = setup({ tags: false });
  picker.set([]);
  assert.equal(btn.textContent, "自动判定");
  picker.set(["厂商系统", "网络问题"]);
  picker.setSub("厂商系统", "金蝶");
  assert.equal(btn.hidden, false);
  assert.equal(btn.textContent, "厂商系统 等 2 项");
  assert.match(sub.innerHTML, /type="radio"/);
  assert.match(sub.innerHTML, /value="金蝶" checked/);
  assert.doesNotMatch(sub.innerHTML, /data-remove-category|data-add-category/);
  const before = changes();
  const radio = { dataset: { subcat: "厂商系统" }, value: '设备<厂商"A>' };
  sub.handlers.change({ target: { closest: () => radio } });
  assert.equal(picker.subValue("厂商系统"), radio.value);
  assert.equal(changes(), before + 1);
  btn.handlers.click({ stopPropagation() {} });
  assert.equal(opened[0].subs["厂商系统"], radio.value);
});
