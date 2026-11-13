// Run with: node --test tests/test_record_attachments.js
// Exercise the shipped queue and editor save/paste handlers without browser dependencies.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");
const { File } = require("node:buffer");
const { Queue, mount, clipboardFiles, MAX_BYTES, MAX_COUNT } = require("../web/record-attachments.js");

if (!globalThis.File) globalThis.File = File;
const html = fs.readFileSync(path.join(__dirname, "../web/index.html"), "utf8");
const script = html.match(/<script>([\s\S]*?)<\/script>/)[1];
function shippedFunction(name) {
  const match = script.match(new RegExp(`^(?:async )?function ${name}\\([^]*?^}`, "m"));
  assert.ok(match, `Missing application function: ${name}`);
  return match[0];
}
function file(name = "现场照片.png", size = 12, type = "image/png") { return { name, size, type }; }
function response(ok = true, attachments = []) { return { ok, json: async () => ok ? { attachments } : { detail: "暂时失败" } }; }
function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}
function makeQueue(options = {}) {
  const requests = [], notices = [];
  const queue = new Queue({
    makeForm: () => ({ values: [], append(key, value) { this.values.push([key, value]); } }),
    fetch: async (url, request) => { requests.push({ url, ...request }); return response(true, [{ name: "saved" }]); },
    notify: (message) => notices.push(message), confirm: () => true, ...options,
  });
  return { queue, requests, notices };
}
function element() {
  return { value: "", textContent: "", innerHTML: "", disabled: false, hidden: false,
    handlers: {}, classList: { add() {}, remove() {}, toggle() {} },
    addEventListener(name, handler) { this.handlers[name] = handler; },
    setAttribute() {}, focus() {}, click() {},
  };
}

test("multiple selections accumulate, named same-size versions remain separate, remove and clear work", () => {
  const { queue } = makeQueue();
  queue.add([file("日志.txt", 10), file("照片.png", 20)]);
  queue.add([file("日志.txt", 10)]);
  assert.equal(queue.entries.length, 3);
  queue.remove(queue.entries[1].id);
  assert.deepEqual(queue.entries.map((entry) => entry.file.name), ["日志.txt", "日志.txt"]);
  queue.clear();
  assert.equal(queue.entries.length, 0);
});

test("client limits count existing attachments and reject empty, executable and oversized files", () => {
  const { queue, notices } = makeQueue();
  queue.bind({ id: "existing", attachments: Array(18).fill({}) });
  assert.equal(queue.add([file("bad.EXE"), file("empty.txt", 0), file("huge.zip", MAX_BYTES + 1)]), 0);
  assert.equal(queue.add([file("a.pdf", MAX_BYTES), file("b.txt"), file("c.txt")]), 2);
  assert.equal(queue.entries.length + queue.existingCount, MAX_COUNT);
  assert.ok(notices.some((message) => message.includes("20 个")));
});

test("viewer cannot add files and an unbound queue does not make a request", async () => {
  const viewer = makeQueue({ canAdd: () => false });
  assert.equal(viewer.queue.add([file()]), 0);
  assert.match(viewer.notices[0], /权限/);
  const { queue, requests } = makeQueue();
  queue.add([file()]);
  await assert.rejects(queue.upload(), /先保存/);
  assert.equal(requests.length, 0);
});

test("only the captured snapshot is removed after upload and concurrent submit is ignored", async () => {
  const pending = deferred();
  let count = 0;
  const { queue } = makeQueue({ fetch: () => { count += 1; return pending.promise; } });
  queue.add([file("first.png")]); queue.bind({ id: "record-1" });
  const upload = queue.upload();
  queue.add([file("next.pdf")]);
  assert.deepEqual(await queue.upload(), { busy: true, count: 0 });
  assert.equal(queue.remove(queue.entries[0].id), false);
  assert.equal(queue.prepareContextChange(), false);
  pending.resolve(response(true, [{ name: "first.png" }]));
  await upload;
  assert.equal(count, 1);
  assert.deepEqual(queue.entries.map((entry) => entry.file.name), ["next.pdf"]);
  assert.equal(queue.existingCount, 1);
  assert.equal(queue.target.id, "record-1");
});

test("lost response retains bound record and reuses the same batch on retry", async () => {
  const requests = [];
  const { queue } = makeQueue({ fetch: async (url, request) => {
    requests.push({ url, ...request });
    if (requests.length === 1) throw new Error("connection lost");
    return response(true, [{ name: "a.png" }]);
  } });
  queue.add([file("a.png")]); queue.bind({ id: "saved/doc" });
  await assert.rejects(queue.upload(), /connection lost/);
  assert.equal(queue.entries.length, 1);
  assert.match(queue.error, /记录已保存/);
  const batch = queue.lastBatch.id;
  queue.add([file("later.pdf")]);
  queue.remove(queue.entries[1].id); // Does not change the failed snapshot.
  await queue.upload();
  assert.equal(requests[0].url, "/api/documents/saved%2Fdoc/attachments");
  assert.ok(requests.every((request) => request.url.endsWith("/attachments")));
  assert.equal(requests[1].body.values.find(([key]) => key === "batch_id")[1], batch);
  assert.equal(queue.entries.length, 0);
});

test("switching record requires consent and clears target/count only after acceptance", () => {
  let allow = false;
  const { queue } = makeQueue({ confirm: () => allow });
  queue.bind({ id: "old", attachments: [{}] }); queue.add([file()]);
  assert.equal(queue.prepareContextChange(), false);
  assert.equal(queue.target.id, "old");
  assert.equal(queue.existingCount, 1);
  assert.throws(() => queue.bind({ id: "new" }), /另一条记录/);
  allow = true;
  assert.equal(queue.prepareContextChange(), true);
  assert.equal(queue.entries.length, 0);
  assert.equal(queue.target, null);
  assert.equal(queue.existingCount, 0);
  queue.bind({ id: "another", attachments: [{}] });
  queue.bind({ id: "new-empty-record" });
  assert.equal(queue.existingCount, 0);
});

test("clipboard file lists and file items are accepted while text items are ignored", () => {
  const a = file("a.pdf"), b = file("b.png");
  assert.deepEqual(clipboardFiles({ files: [a, b], items: [{ kind: "file", getAsFile: () => a }] }), [a, b]);
  assert.deepEqual(clipboardFiles({ items: [{ kind: "string", type: "text/plain" },
    { kind: "file", getAsFile: () => b }] }), [b]);
  assert.deepEqual(clipboardFiles({ items: [{ kind: "string", type: "text/plain" }] }), []);
});

test("Clipboard.read screenshot is a queued file, unsupported or denied APIs give a keyboard fallback", async () => {
  const { queue, notices } = makeQueue();
  await queue.readClipboard({ read: async () => [{ types: ["text/html", "image/png"], getType: async () => new Blob(["image"], { type: "image/png" }) }] });
  assert.equal(queue.entries.length, 1);
  assert.equal(queue.entries[0].file.type, "image/png");
  assert.match(queue.entries[0].file.name, /\.png$/);
  await queue.readClipboard({});
  await queue.readClipboard({ read: async () => { throw new Error("NotAllowedError"); } });
  assert.equal(queue.entries.length, 1);
  assert.ok(notices.every((message) => /Ctrl\+V/.test(message)));
});

test("late clipboard permission response cannot attach files after switching to a different record", async () => {
  const pending = deferred();
  const { queue, notices } = makeQueue();
  const read = queue.readClipboard({ read: () => pending.promise });
  queue.prepareContextChange();
  pending.resolve([{ types: ["image/png"], getType: async () => new Blob(["image"]) }]);
  assert.equal(await read, 0);
  assert.equal(queue.entries.length, 0);
  assert.ok(notices.some((message) => message.includes("记录已切换")));
});

test("attachment-area paste adds all files once without swallowing ordinary text, filenames are escaped", () => {
  const selectors = ["#rec-attach", "[data-attachment-drop]", "[data-attachment-list]", "[data-attachment-status]",
    "[data-attachment-count]", "[data-attachment-retry]", "[data-attachment-clear]", "[data-attachment-target]",
    "[data-attachment-choose]", "[data-attachment-clipboard]"];
  const elements = Object.fromEntries(selectors.map((selector) => [selector, element()]));
  const root = { querySelector: (selector) => elements[selector], setAttribute() {} };
  const queue = mount({ root, confirm: () => true });
  const paste = elements["[data-attachment-drop]"].handlers.paste;
  let prevented = 0, stopped = 0;
  paste({ clipboardData: { files: [file('<img src=x onerror="x">.png'), file("b.pdf")] },
    preventDefault() { prevented += 1; }, stopPropagation() { stopped += 1; } });
  assert.equal(queue.entries.length, 2);
  assert.equal(prevented, 1); assert.equal(stopped, 1);
  assert.doesNotMatch(elements["[data-attachment-list]"].innerHTML, /<img/);
  assert.match(elements["[data-attachment-list]"].innerHTML, /&lt;img/);
  paste({ clipboardData: { items: [{ kind: "string", type: "text/plain" }] },
    preventDefault() { throw new Error("text paste should not be intercepted"); } });
  assert.equal(queue.entries.length, 2);
});

test("body screenshot is inserted inline once, mixed file paste is queued and text is untouched", async () => {
  const added = [], uploaded = [], inserted = [];
  const context = vm.createContext({ RecordAttachments: { clipboardFiles },
    recordAttachmentQueue: { add: (files) => added.push(...files) }, isAdmin: true,
    mdStatus: { textContent: "" }, uploadImageFile: async (value) => { uploaded.push(value); return "/images/image.png"; },
    insertImageMarkdown: (...args) => inserted.push(args), toast() {},
  });
  vm.runInContext(shippedFunction("handleImagePaste"), context);
  const image = file("screen.png"), pdf = file("manual.pdf", 12, "application/pdf");
  let prevented = 0;
  await context.handleImagePaste({ clipboardData: { files: [image], items: [{ kind: "file", type: "image/png", getAsFile: () => image }] }, preventDefault() { prevented += 1; } });
  assert.equal(uploaded.length, 1); assert.equal(inserted.length, 1); assert.equal(added.length, 0);
  await context.handleImagePaste({ clipboardData: { files: [image], items: [] }, preventDefault() { prevented += 1; } });
  assert.equal(uploaded.length, 2); assert.equal(inserted.length, 2); assert.equal(added.length, 0);
  await context.handleImagePaste({ clipboardData: { files: [image, pdf], items: [] }, preventDefault() { prevented += 1; } });
  assert.equal(uploaded.length, 2); assert.equal(added.length, 2); assert.equal(prevented, 3);
  await context.handleImagePaste({ clipboardData: { items: [{ kind: "string", type: "text/plain" }] }, preventDefault() { throw new Error("text should remain editable"); } });
});

test("record save failure keeps queue; attachment failure retains the saved ID and retry does not create a record", async () => {
  const { queue } = makeQueue({ fetch: async () => response(false) });
  queue.add([file("proof.png")]);
  const button = element(); let saveHandler, createCalls = 0, resets = 0, recordSaveFails = true;
  button.addEventListener = (event, callback) => { saveHandler = callback; };
  const context = vm.createContext({ isAdmin: true, recordAttachmentQueue: queue, editingId: "", editingChunks: 0,
    editorMode: "source", mdSrc: { value: "# 处理记录\n已修复" }, mdPicker: { selected: () => [], subValue: () => "" },
    mdResolved: { value: "resolved" }, CATEGORY_SUBS: {}, mdStatus: { textContent: "" }, trackingParent: null,
    $: (selector) => selector === "#md-save" ? button : { value: "维修员" },
    normalizeMdFilename: () => "处理记录.md", enterpriseScope: () => ({}), catName: (value) => value,
    toast() {}, renderTrackingParent() {}, enterpriseRenderScope() {}, setEditorTitle() {},
    loadDocuments() {}, loadRecordList() {}, refreshTimelineIfVisible() {}, resetEditor: () => { resets += 1; },
    fetch: async () => {
      createCalls += 1;
      if (recordSaveFails) return { ok: false, json: async () => ({ detail: "记录保存失败" }) };
      return { ok: true, json: async () => ({ created: true, doc: { id: "new-record", name: "处理记录", chunks: 1, category: "网络问题" } }) };
    },
  });
  vm.runInContext(shippedFunction("uploadRecordAttachments"), context);
  const start = script.indexOf('$("#md-save").addEventListener("click", async () => {');
  const end = script.indexOf("\nasync function uploadFiles", start);
  assert.ok(start >= 0 && end > start);
  vm.runInContext(script.slice(start, end), context);
  await saveHandler();
  assert.equal(createCalls, 1); assert.equal(context.editingId, "");
  assert.equal(queue.target, null); assert.equal(queue.entries.length, 1);
  recordSaveFails = false;
  await saveHandler();
  assert.equal(createCalls, 2); assert.equal(context.editingId, "new-record");
  assert.equal(queue.target.id, "new-record"); assert.equal(queue.entries.length, 1);
  assert.equal(resets, 0); assert.match(context.mdStatus.textContent, /记录已保存/);
  queue.fetch = async (url) => { assert.equal(url, "/api/documents/new-record/attachments"); return response(true, [{}]); };
  await queue.upload();
  assert.equal(createCalls, 2); assert.equal(queue.entries.length, 0);
});

test("editing loads existing attachment metadata before allowing new files", async () => {
  const metadataRequest = deferred();
  let context;
  const { queue } = makeQueue({ canAdd: () => context.recordAttachmentsReady });
  const controls = new Map();
  const doc = { id: "edit-record", name: "已有记录", chunks: 1, categories: ["网络问题"], resolved: "resolved" };
  context = vm.createContext({ isAdmin: true, recordAttachmentQueue: queue, recordAttachmentsReady: true,
    editorRequest: 0, recDocs: [doc], docDocs: [], editingId: "", editingChunks: 0, vendorList: [],
    mdSrc: { value: "" }, mdName: { value: "" }, mdResolved: { value: "" }, mdStatus: { textContent: "" }, mdTime: { value: "" },
    mdPicker: { set() {}, setSub() {} }, docCats: (value) => value.categories,
    enterpriseCanWrite: () => true, enterpriseRenderScope() {}, clearTracking() {}, mdResetHistory() {},
    renderEditorPreview() {}, setEditorTitle() {}, toast() {}, timeToInput: () => "", todayValue: () => "2026-11-13",
    $: (selector) => { if (!controls.has(selector)) controls.set(selector, { style: {} }); return controls.get(selector); },
    fetch: async (url) => url.endsWith("/source")
      ? { ok: true, json: async () => ({ name: doc.name, content: "# 原文", restored: false }) }
      : metadataRequest.promise,
  });
  vm.runInContext(shippedFunction("loadEditable"), context);
  const load = context.loadEditable(doc.id);
  assert.equal(queue.add([file("too-early.png")]), 0);
  metadataRequest.resolve({ ok: true, json: async () => ({ ...doc, attachments: Array(19).fill({}) }) });
  await load;
  assert.equal(context.recordAttachmentsReady, true);
  assert.equal(queue.target.id, doc.id);
  assert.equal(queue.existingCount, 19);
  assert.equal(queue.add([file("allowed.png"), file("over-limit.png")]), 1);
});
