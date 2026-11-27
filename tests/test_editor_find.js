const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const html = fs.readFileSync(path.join(__dirname, "../web/index.html"), "utf8");
const script = html.match(/<script>([\s\S]*?)<\/script>/)[1];

function makeEditor(text) {
  const controls = {
    "#find-text": { value: "", focus() { this.focused = true; } },
    "#find-status": { textContent: "" },
    "#find-case": { checked: false },
    "#replace-text": { value: "" },
  };
  const mdSrc = {
    get value() { return text; },
    set value(next) {
      text = next;
      this.setSelectionRange(text.length, text.length);
    },
    selectionStart: 0,
    selectionEnd: 0,
    clientHeight: 360,
    scrollHeight: 360,
    focus() { this.focused = true; },
    setSelectionRange(start, end) {
      this.selectionStart = start;
      this.selectionEnd = end;
    },
  };
  let rendered = text;
  const renderEditorPreview = () => { rendered = mdSrc.value; };
  const context = vm.createContext({
    $: (selector) => controls[selector], mdSrc, editorMode: "preview",
    lastEditorRange: { start: 0, end: 0 }, mdUndoStack: [], mdRedoStack: [], mdLastPush: 0,
    performance, renderEditorPreview, applyEditorMode: renderEditorPreview,
  });
  for (const name of ["syncEditorRange", "mdSnapshot", "mdPushUndo", "applyMdHistory", "mdUndo",
    "replaceEditorText", "scrollEditorFindRangeIntoView", "selectEditorFindRange", "runEditorFind"]) {
    const match = script.match(new RegExp(`^function ${name}\\([^]*?^}`, "m"));
    assert.ok(match, `Missing application function: ${name}`);
    vm.runInContext(match[0], context);
  }
  return {
    context, mdSrc,
    setQuery(value) { controls["#find-text"].value = value; },
    setReplacement(value) { controls["#replace-text"].value = value; },
    setMatchCase(value) { controls["#find-case"].checked = value; },
    run: (action) => context.runEditorFind(action),
    selected: () => mdSrc.value.slice(mdSrc.selectionStart, mdSrc.selectionEnd),
    status: () => controls["#find-status"].textContent,
    rendered: () => rendered,
  };
}

test("find selects literal text in source and cycles through case-insensitive matches", () => {
  const editor = makeEditor("[A] Aa [a]");
  editor.setQuery("[a]");
  editor.run("find-next");
  assert.equal(editor.context.editorMode, "source");
  assert.equal(editor.mdSrc.focused, true);
  assert.equal(editor.selected(), "[A]");
  editor.run("find-next");
  assert.equal(editor.selected(), "[a]");
  assert.equal(editor.status(), "第 2 / 2 处");
  editor.run("find-next");
  assert.equal(editor.selected(), "[A]");
});

test("empty and unmatched searches report their status and leave preview active", () => {
  const editor = makeEditor("Pump");
  editor.run("find-next");
  assert.equal(editor.status(), "请输入查找内容");
  editor.setQuery("pump");
  editor.setMatchCase(true);
  editor.run("find-next");
  assert.equal(editor.status(), "未找到匹配内容");
  assert.equal(editor.context.editorMode, "preview");
});

test("replacing one uses the current match and supports undo", () => {
  const editor = makeEditor("pump pump");
  editor.setQuery("pump");
  editor.setReplacement("$&");
  editor.run("find-next");
  editor.run("find-next");
  editor.run("replace-one");
  assert.equal(editor.mdSrc.value, "pump $&");
  assert.equal(editor.rendered(), "pump $&");
  assert.equal(editor.status(), "已替换 1 处");
  editor.context.mdUndo();
  assert.equal(editor.mdSrc.value, "pump pump");
  assert.equal(editor.mdSrc.selectionStart, 5);
  assert.equal(editor.selected(), "pump");
});

test("replacing all respects case and treats replacement tokens as text", () => {
  const editor = makeEditor("Pump pump pump");
  editor.setQuery("pump");
  editor.setMatchCase(true);
  editor.setReplacement("pump");
  editor.run("replace-all");
  assert.equal(editor.context.mdUndoStack.length, 0);
  editor.setReplacement("$&");
  editor.run("replace-all");
  assert.equal(editor.mdSrc.value, "Pump $& $&");
  assert.equal(editor.rendered(), "Pump $& $&");
  assert.equal(editor.status(), "已替换 2 处");
  assert.equal(editor.context.mdUndoStack.length, 1);
  assert.equal(editor.context.editorMode, "preview");
});
