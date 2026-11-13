// Execute the shipped loaders and change handlers with deferred network responses.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const html = fs.readFileSync(path.join(__dirname, "../web/index.html"), "utf8");
const script = html.match(/<script>([\s\S]*?)<\/script>/)[1];
const enterprise = fs.readFileSync(path.join(__dirname, "../web/enterprise.js"), "utf8");

function shippedFunction(source, name) {
  const match = source.match(new RegExp(`^(?:async )?function ${name}\\([^]*?^}`, "m"));
  assert.ok(match, `Missing application function: ${name}`);
  return match[0];
}

function shippedHandler(select) {
  const match = script.match(new RegExp(`^${select}\\.addEventListener\\("change", \\(\\) => \\{[^]*?^\\}\\);`, "m"));
  assert.ok(match, `Missing change handler: ${select}`);
  return match[0];
}

function element() {
  return {
    innerHTML: "", value: "", textContent: "", title: "", hidden: false,
    classList: { contains: () => true }, setAttribute() {},
    addEventListener(name, handler) { this[name] = handler; },
  };
}

function response(data) { return { ok: true, json: async () => data }; }
function deferred() {
  let resolve;
  const promise = new Promise((done) => { resolve = done; });
  return { promise, resolve };
}

function createPage() {
  const nodes = new Map();
  const node = (id) => {
    if (!nodes.has(id)) nodes.set(id, element());
    return nodes.get(id);
  };
  const requests = [];
  const pending = [];
  const renders = [];
  const context = vm.createContext({
    URLSearchParams, AbortController,
    $: node,
    fetch(url, options = {}) {
      const request = deferred();
      requests.push({ url, options });
      pending.push(request);
      return request.promise;
    },
    docDepartmentSelect: node("doc-department"), recDepartmentSelect: node("rec-department"),
    tlDepartmentSelect: node("tl-department"), docRows: node("doc-rows"),
    tlBody: node("tl-body"), tlLoadState: node("tl-load-state"),
    tlLoadMoreButton: node("tl-load-more"), tlCatFilterEl: node("tl-cat-filter"),
    renderCategoryFilter() {}, renderDocs() { renders.push("docs"); },
    renderRecList() { renders.push("records"); },
    renderTimeline() { renders.push("timeline"); },
    resetEditor() { throw new Error("Filtering must not discard an active editor"); },
  });
  vm.runInContext(`
    let isAdmin = true, editingId = "editing";
    let docDocs = [], docCategories = [], recDocs = [];
    let docDepartmentFilter = "", recDepartmentFilter = "", docCatFilter = "", recCatFilter = "";
    let docRequestId = 0, recRequestId = 0, statsRequestId = 0;
    let tlData = null, tlDepartmentFilter = "", tlCatFilter = "", tlVendorFilter = "", tlStatusFilter = "";
    let tlStartDate = "", tlEndDate = "", tlLoading = false, tlHasMore = false, tlNextOffset = 0;
    let tlError = "", tlRequestId = 0, tlRequestController = null, tlAutoLoadReady = false;
    const TL_PAGE_SIZE = 30;
    ${["escapeHtml", "escapeAttr", "renderDepartmentFilter", "loadKnowledgeStats", "loadDocuments", "loadRecordList",
      "loadTimeline", "updateTimelinePagination", "renderTimelineFilter"].map((name) => shippedFunction(script, name)).join("\n")}
    ${["docDepartmentSelect", "recDepartmentSelect", "tlDepartmentSelect"].map(shippedHandler).join("\n")}
  `, context);
  return {
    requests, pending, renders, node,
    run: (code) => vm.runInContext(code, context),
    async change(select, value) {
      context[select].value = value;
      context[select].change();
      await Promise.resolve();
    },
  };
}

function documentPage(id, department = "sales") {
  return { documents: [{ id }], categories: [{ name: "部门资料", count: 1 }],
    departments: [{ id: department, name: "销售部", count: 1 }],
    stats: { documents: 1, chunks: 2, llm: "test" } };
}

function timelinePage(ids, extra = {}) {
  return { events: ids.map((doc_id) => ({ doc_id })), total: ids.length, event_count: ids.length,
    issue_count: 0, departments: [{ id: "sales", name: "销售部", count: ids.length }],
    categories: [], status_counts: {}, has_more: false, next_offset: ids.length, ...extra };
}

test("documents, records and timeline expose labeled department selectors", () => {
  for (const id of ["doc-department-filter", "rec-department-filter", "tl-department-filter"]) {
    assert.match(html, new RegExp(`<select id="${id}"[^>]*aria-label="[^"]+"[^>]*><option value="">全部部门</option>`));
  }
});

test("department options use only the endpoint list and escape department names", () => {
  const page = createPage();
  page.run('renderDepartmentFilter(docDepartmentSelect, [{ id: "sales", name: "销售<&部门", count: 8 }], "sales")');
  assert.match(page.node("doc-department").innerHTML, /销售&lt;&amp;部门（8）/);
  assert.doesNotMatch(page.node("doc-department").innerHTML, /operations/);
  assert.equal(page.node("doc-department").value, "sales");
  page.run('renderDepartmentFilter(docDepartmentSelect, [], "sales")');
  assert.match(page.node("doc-department").innerHTML, /已选部门（当前无记录）/);
  assert.equal(page.node("doc-department").value, "sales");
  assert.equal(page.requests.length, 0);
});

for (const [kind, select, filter, docs, loader, rendered] of [
  ["doc", "docDepartmentSelect", "docDepartmentFilter", "docDocs", "loadDocuments", "docs"],
  ["record", "recDepartmentSelect", "recDepartmentFilter", "recDocs", "loadRecordList", "records"],
]) {
  test(`${kind} selection reloads scoped data and ignores older responses`, async () => {
    const page = createPage();
    page.node("#overview-documents").textContent = "100";
    page.node("#overview-chunks").textContent = "500";
    page.node("#stats").textContent = "100 份文档 · 500 个检索单元";
    page.run('docCatFilter = recCatFilter = "旧分类"');
    await page.change(select, "sales & service");
    const first = new URL(page.requests[0].url, "http://localhost");
    assert.equal(first.pathname, "/api/documents");
    assert.equal(first.searchParams.get("doc_type"), kind);
    assert.equal(first.searchParams.get("department_id"), "sales & service");
    assert.equal(page.run(kind === "doc" ? "docCatFilter" : "recCatFilter"), "");
    page.run(`${filter} = "hr"`);
    const latest = page.run(`${loader}()`);
    page.pending[1].resolve(response(documentPage("latest", "hr")));
    await latest;
    page.pending[0].resolve(response(documentPage("old")));
    await new Promise(setImmediate);
    assert.equal(page.run(`${docs}[0].id`), "latest");
    assert.deepEqual(page.renders, [rendered]);
    assert.equal(page.node("#overview-documents").textContent, "100");
    assert.equal(page.node("#overview-chunks").textContent, "500");
    assert.equal(page.node("#stats").textContent, "100 份文档 · 500 个检索单元");
    assert.ok(page.requests.every(({ url }) => !url.startsWith("/api/departments")));
  });
}

test("overview statistics use all visible knowledge and ignore older refreshes", async () => {
  const page = createPage();
  page.run('docDepartmentFilter = "sales"; recDepartmentFilter = "hr"');
  const old = page.run("loadKnowledgeStats()");
  const current = page.run("loadKnowledgeStats()");
  assert.deepEqual(page.requests.map(({ url }) => url), ["/api/health", "/api/health"]);
  page.pending[1].resolve(response({ stats: { documents: 125, chunks: 750, llm: "test" } }));
  await current;
  page.pending[0].resolve(response({ stats: { documents: 100, chunks: 500, llm: "test" } }));
  await old;
  assert.equal(page.node("#overview-documents").textContent, "125");
  assert.equal(page.node("#overview-chunks").textContent, "750");
  assert.equal(page.node("#stats").textContent, "125 份文档 · 750 个检索单元");
  assert.match(page.node("#stats").title, /全部文档与记录/);
});

test("knowledge changes refresh the overview even while timeline is hidden", async () => {
  const page = createPage();
  page.node("#tab-timeline").classList.contains = () => false;
  page.run(shippedFunction(script, "refreshTimelineIfVisible"));
  page.run("refreshTimelineIfVisible()");
  assert.deepEqual(page.requests.map(({ url }) => url), ["/api/health"]);
  page.pending[0].resolve(response({ stats: { documents: 126, chunks: 755, llm: "test" } }));
  await new Promise(setImmediate);
  assert.equal(page.node("#overview-documents").textContent, "126");
});

test("timeline department change aborts paging and restarts the 30-item range", async () => {
  const page = createPage();
  page.run('tlDepartmentFilter = "old"; tlStartDate = "2026-11-01"; tlEndDate = "2026-11-13"');
  const first = page.run("loadTimeline()");
  page.pending[0].resolve(response(timelinePage(["old"], { has_more: true, next_offset: 30, total: 60 })));
  await first;
  page.run('tlCatFilter = "厂商系统"; tlVendorFilter = "旧厂商"; tlStatusFilter = "no"');
  const olderPage = page.run("loadTimeline({ append: true })");
  await page.change("tlDepartmentSelect", "sales");
  const params = new URL(page.requests[2].url, "http://localhost").searchParams;
  assert.equal(params.get("department_id"), "sales");
  assert.equal(params.get("offset"), "0");
  assert.equal(params.get("limit"), "30");
  assert.equal(params.get("start_date"), "2026-11-01");
  assert.equal(params.get("end_date"), "2026-11-13");
  for (const name of ["category", "vendor", "status"]) assert.equal(params.has(name), false);
  assert.equal(page.requests[1].options.signal.aborted, true);
  page.pending[2].resolve(response(timelinePage(["sales-1"])));
  await new Promise(setImmediate);
  // Simulate a network adapter that resolves an aborted request anyway.
  page.pending[1].resolve(response(timelinePage(["old-2"])));
  await olderPage;
  assert.equal(page.run('tlData.events.map((event) => event.doc_id).join(",")'), "sales-1");
  assert.equal(page.node("tl-department").value, "sales");
});

test("timeline All count includes knowledge documents and preserves zero", () => {
  const page = createPage();
  for (const [eventCount, expected] of [[42, 42], [0, 0], [undefined, 7]]) {
    page.run(`tlData = { event_count: ${eventCount}, issue_count: 7, categories: [], status_counts: {} }; renderTimelineFilter()`);
    assert.match(page.node("tl-cat-filter").innerHTML, new RegExp(`data-tl-filter="">全部<span class="chip-count">${expected}</span>`));
  }
});

test("public document labels use the supplied department name without a directory lookup", () => {
  const context = vm.createContext({
    enterpriseIdentity: { public_access: true },
    enterpriseEscape: (value) => String(value).replace(/</g, "&lt;"),
    enterpriseDepartmentName: () => { throw new Error("Department directory should be unnecessary"); },
  });
  vm.runInContext(shippedFunction(enterprise, "enterpriseDocumentLabel"), context);
  const label = vm.runInContext('enterpriseDocumentLabel({ department_id: "sales", department_name: "销售部", visibility: "public" })', context);
  assert.match(label, /销售部/);
  assert.match(label, /企业公共/);
});

test("page terminology applies to general enterprise work", () => {
  for (const filename of fs.readdirSync(path.join(__dirname, "../web"))) {
    if (!/\.(html|js|css)$/.test(filename)) continue;
    const source = fs.readFileSync(path.join(__dirname, "../web", filename), "utf8");
    assert.doesNotMatch(source, /运维/, filename);
  }
  assert.match(html, /data-tab="timeline">时间线</);
  for (const label of ["已完成", "处理中", "阶段完成", "未完成"]) {
    assert.ok(shippedFunction(script, "tlBadge").includes(label));
  }
});
