const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const html = fs.readFileSync(path.join(__dirname, "../web/index.html"), "utf8");
const script = html.match(/<script>([\s\S]*?)<\/script>/)[1];
const toolLimit = 2 * 1024 ** 3;

function section(from, to) {
  const start = script.indexOf(from);
  const end = script.indexOf(to, start + 1);
  assert.ok(start >= 0 && end > start, from);
  return script.slice(start, end);
}

function shippedFunction(name) {
  const match = script.match(new RegExp(`^(?:async )?function ${name}\\([^]*?^}`, "m"));
  assert.ok(match, `Missing function: ${name}`);
  return match[0];
}

function controls() {
  const elements = new Map();
  return (id) => {
    if (!elements.has(id)) elements.set(id, {
      value: "", focus() {}, click() {},
      classList: { add() {}, remove() {} },
      events: new Map(),
      addEventListener(name, handler) { this.events.set(name, handler); },
    });
    return elements.get(id);
  };
}

test("the editor lists tools separately and keeps their download links through visual editing", async () => {
  const $ = controls();
  const inserted = [];
  const requests = [];
  const downloads = [];
  const tools = [
    { id: "tool-1", name: "工具 & 驱动.exe", status: "ready", indexed: false, has_source: true },
    { id: "missing", name: "缺少原件.exe", status: "ready", indexed: false, has_source: false },
  ];
  const documents = [{ id: "doc-1", name: "手册.md", status: "ready", indexed: true }];
  const context = vm.createContext({
    $, document: { addEventListener() {} },
    mdPreview: $("#md-preview"), triggerDownload: (url) => downloads.push(url),
    fetch: async (url) => {
      requests.push(url);
      const data = url === "/api/tools" ? { tools } : { documents };
      return { ok: true, json: async () => data };
    },
    enterpriseDepartmentName: () => "信息部",
    editingId: "", editorMode: "source",
    currentEditorRange: () => ({ start: 3, end: 5 }),
    setEditorSpan: (...args) => inserted.push(args),
  });
  vm.runInContext(
    section("function escapeHtml", "function renderMarkdown") +
    section("function documentDownloadPath", "function triggerDownload") +
    section("let docRefDocuments", "/* ---------- 修改记录页：全库编辑溯源 ---------- */") +
    section("function appendInline", "function tableToMarkdown") +
    section('mdPreview.addEventListener("click"', 'mdSrc.addEventListener("paste"'),
    context,
  );
  await context.openDocRef();
  assert.deepEqual(requests.sort(), ["/api/documents", "/api/tools"]);
  assert.match($("#doc-ref-list").innerHTML, /data-ref-name="工具 &amp; 驱动\.exe" data-ref-tool/);
  assert.match($("#doc-ref-list").innerHTML, /工具下载/);
  assert.doesNotMatch($("#doc-ref-list").innerHTML, /缺少原件/);

  const choose = $("#doc-ref-pop").events.get("click");
  for (const doc of [tools[0], documents[0]]) choose({
    target: { closest: () => ({
      dataset: { refId: doc.id, refName: doc.name },
      hasAttribute: (name) => name === "data-ref-tool" && doc.indexed === false,
    }) },
  });
  const markdown = "[下载 工具 & 驱动.exe](/api/tools/tool-1/download)";
  assert.deepEqual(inserted, [[3, 5, markdown], [3, 5, "[《手册.md》](doc-ref://doc-1)"]]);
  assert.match(context.inline(inserted[1][2]), /data-doc-pv="doc-1"/);
  assert.match(context.inline("[外部链接](https://example.org)"), /target="_blank"/);

  for (const link of [markdown, "[旧链接](/api/documents/old-tool/download)"]) {
    const [, label] = link.match(/^\[([^\]]+)\]/);
    const [, href] = context.inline(link).match(/<a href="([^"]+)" download\b/);
    const anchor = {
      nodeType: 1, tagName: "A", textContent: label,
      href, dataset: { toolDownload: href.split("/")[3] },
      getAttribute: (attribute) => attribute === "href" ? href : null,
    };
    const state = { md: "", done: false, stop: null };
    context.appendInline({ childNodes: [anchor] }, state);
    assert.equal(state.md, link);
    $("#md-preview").events.get("click")({ target: { closest: () => anchor }, preventDefault() {} });
  }
  assert.deepEqual(downloads, ["/api/tools/tool-1/download", "/api/documents/old-tool/download"]);
});

test("document and tool upload controls are in their respective tabs", () => {
  const docs = html.match(/<section\b[^>]*\bid="tab-docs"[^>]*>[\s\S]*?<\/section>/)?.[0];
  const tools = html.match(/<section\b[^>]*\bid="tab-tools"[^>]*>[\s\S]*?<\/section>/)?.[0];
  assert.ok(docs);
  assert.ok(tools);
  assert.match(html, /<button\b[^>]*\bdata-tab="tools"/);
  assert.match(docs, /id="file"/);
  assert.doesNotMatch(docs, /2 GB|id="tool-file"|id="tool-rows"/);
  assert.match(tools, /id="tool-file"/);
  assert.match(tools, /id="tool-rows"/);
  assert.match(tools, /2 GB/);
});

test("the tools tab lists and manages downloads through the tools API", async () => {
  const $ = controls();
  const requests = [];
  const copied = [];
  const access = [];
  const readyTool = {
    id: "driver", name: "驱动 <旧版>.exe", ext: ".exe", size: 200,
    status: "ready", has_source: true, can_write: true, can_manage_access: true,
  };
  const missingTool = { ...readyTool, id: "missing", name: "原件缺失", has_source: false, can_write: false, can_manage_access: false };
  const context = vm.createContext({
    $, URL, URLSearchParams,
    window: { location: { origin: "https://kb.example" } },
    navigator: { clipboard: { writeText: async (url) => copied.push(url) } },
    confirm: () => true, toast() {},
    enterpriseDocumentLabel: () => "", enterpriseCanWrite: (tool) => tool.can_write,
    enterpriseCanManageAccess: (tool) => tool.can_manage_access,
    enterpriseEditAccess: (...args) => access.push(args),
    enterpriseJson: async (url, options) => {
      requests.push([url, options?.method || "GET"]);
      return { tools: [readyTool, missingTool], departments: [{ id: "it", name: "信息部", count: 2 }] };
    },
  });
  vm.runInContext([
    ...["escapeHtml", "escapeAttr", "fmtSize", "renderDepartmentFilter", "toolApiPath", "toolDownloadPath"].map(shippedFunction),
    section('const toolRows = $("#tool-rows")', "/* ---------- 修改记录时间弹窗 ---------- */"),
  ].join("\n"), context);

  $("#tool-department-filter").value = "it";
  await $("#tool-department-filter").events.get("change")();
  assert.deepEqual(requests, [["/api/tools?department_id=it", "GET"]]);
  assert.match($("#tool-department-filter").innerHTML, /信息部（2）/);
  const rows = $("#tool-rows").innerHTML;
  assert.match(rows, /驱动 &lt;旧版&gt;\.exe/);
  assert.match(rows, /href="\/api\/tools\/driver\/download" download\b/);
  assert.doesNotMatch(rows, /\/api\/tools\/missing\/download|data-tool-del="missing"/);

  const click = (selector, dataset) => $("#tool-rows").events.get("click")({
    target: { closest: (attribute) => attribute === selector ? { dataset } : null },
  });
  await click("[data-tool-copy]", { toolCopy: "driver" });
  assert.deepEqual(copied, ["https://kb.example/api/tools/driver/download"]);
  await click("[data-tool-access]", { toolAccess: "driver" });
  assert.deepEqual(access, [[readyTool, "tools"]]);
  await click("[data-tool-del]", { toolDel: "driver" });
  assert.deepEqual(requests.slice(1), [["/api/tools/driver", "DELETE"], ["/api/tools?department_id=it", "GET"]]);
});

test("tool uploads and replacements use the separate API with a 2 GB boundary", async () => {
  const $ = controls();
  const requests = [];
  const messages = [];
  const records = [];
  const scopes = [];
  const valid = { name: "setup.EXE", size: toolLimit };
  const oversized = { name: "setup.zip", size: toolLimit + 1 };
  const document = { name: "manual.md", size: 1 };
  class FormDataStub extends Map {
    append(key, value) { this.set(key, value); }
  }
  const context = vm.createContext({
    $, isAdmin: true, replaceTargetId: "", replaceToolTargetId: "",
    docDocs: [{ id: "doc-1", doc_type: "doc", name: "manual.md" }], recDocs: [],
    toolDocs: [{ id: "tool-1", doc_type: "doc", name: "old.exe", indexed: false, has_source: true }],
    FormData: FormDataStub, fileTimeValue: () => "", todayValue: () => "2026-11-24",
    enterpriseAppendScope: (_form, scope) => scopes.push(scope),
    enterpriseCanWrite: () => true, loadDocuments() {}, loadTools() {}, refreshTimelineIfVisible() {},
    uploadRecordFiles: (files) => records.push(...files),
    toast: (message) => messages.push(message),
    fetch: async (url, { method, body }) => {
      const file = body.get("file");
      requests.push([url, method, file]);
      return { ok: true, json: async () => ({
        doc: { doc_type: "doc", name: file.name, indexed: file === document, chunks: 0 },
      }) };
    },
  });
  vm.runInContext(
    shippedFunction("toolApiPath") +
    section("async function uploadFiles", '$("#rebuild").addEventListener') +
    section('const recDrop = $("#rec-drop")', 'mdSrc.addEventListener("beforeinput"'),
    context,
  );
  const drop = (selector, files) => $(selector).events.get("drop")({ dataTransfer: { files }, preventDefault() {} });
  await drop("#drop", [valid, document]);
  assert.deepEqual(requests, [["/api/documents", "POST", document]]);
  await drop("#tool-drop", [oversized, valid, document]);
  assert.deepEqual(requests, [["/api/documents", "POST", document], ["/api/tools", "POST", valid]]);
  assert.deepEqual(scopes, ["doc", "tool"]);
  assert.ok(messages.some((message) => message.includes("2 GB")));
  $("#rec-drop").events.get("drop")({ dataTransfer: { files: [document] } });
  assert.deepEqual(records, [document]);

  const replace = $("#tool-replace-file").events.get("change");
  context.startReplace("tool-1", true);
  await replace({ target: { files: [oversized], value: oversized.name } });
  assert.equal(requests.length, 2);
  assert.match(messages.at(-1), /2 GB/);
  context.startReplace("tool-1", true);
  await replace({ target: { files: [valid], value: valid.name } });
  assert.deepEqual(requests.at(-1), ["/api/tools/tool-1/replace", "PUT", valid]);

  const replaceDoc = $("#replace-file").events.get("change");
  context.startReplace("doc-1");
  await replaceDoc({ target: { files: [valid], value: valid.name } });
  assert.equal(requests.length, 3);
  context.startReplace("doc-1");
  await replaceDoc({ target: { files: [document], value: document.name } });
  assert.deepEqual(requests.at(-1), ["/api/documents/doc-1/replace", "PUT", document]);
});
