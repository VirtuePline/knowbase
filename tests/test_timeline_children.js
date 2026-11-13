// Exercise shipped follow-up rendering and requests independently of main timeline paging.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const html = fs.readFileSync(path.join(__dirname, "../web/index.html"), "utf8");
const script = html.match(/<script>([\s\S]*?)<\/script>/)[1];

function shippedFunction(name) {
  const match = script.match(new RegExp(`^(?:async )?function ${name}\\([^]*?^}`, "m"));
  assert.ok(match, `Missing shipped function: ${name}`);
  return match[0];
}
function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}
function response(data, ok = true) { return { ok, json: async () => data }; }
function event(docId, extra = {}) {
  return { doc_id: docId, chunk_id: docId, doc_name: `记录${docId}`, brief: `进展${docId}`,
    summary: `详细进展${docId}`, date: "2026-11-12", child_count: 0, status: "improving",
    uploaded_by_name: "张工", doc_type: "record", writable: true, ...extra };
}
function childPage(events, extra = {}) {
  return { events, total: events.length, has_more: false, next_offset: events.length, ...extra };
}
function page(events) {
  const requests = [], previews = [], tracks = [];
  const body = { innerHTML: "", addEventListener(type, handler) { this[type] = handler; } };
  const context = vm.createContext({
    URLSearchParams, AbortController, tlBody: body,
    fetch(url, options = {}) {
      const pending = deferred();
      requests.push({ url, options, ...pending });
      return pending.promise;
    },
    enterpriseCanWrite: (item) => item.writable === true,
    catBadges: () => '<span class="cat-badge">网络问题</span>',
    openPreview: (id) => previews.push(id),
    startTracking: (item) => tracks.push(item.doc_id),
  });
  vm.runInContext(`
    let tlData = { events: ${JSON.stringify(events)} }, tlRequestId = 0, tlChildrenRequestId = -1, isAdmin = true;
    let tlSortMode = "time-desc";
    const tlChildren = new Map(), tlCollapsed = new Set(), TL_CHILD_PAGE_SIZE = 20;
    ${["escapeHtml", "escapeAttr", "recordRelationsHtml", "syncTimelineChildren", "tlChildState",
      "tlChildrenPanelId", "tlChildrenHtml", "loadTimelineChildren", "toggleTimelineChildren",
      "findTimelineEvent", "tlDateLabel", "tlBadge", "tlEffective", "tlStatusControl", "tlItemHtml", "renderTimeline"]
      .map(shippedFunction).join("\n")}
    ${script.match(/^tlBody\.addEventListener\("click", \(e\) => \{[^]*?^\}\);/m)[0]}
    renderTimeline();
  `, context);
  return {
    requests, previews, tracks, body,
    run: (code) => vm.runInContext(code, context),
    click(attribute, value) {
      const name = attribute.replace(/-([a-z])/g, (_, letter) => letter.toUpperCase());
      body.click({ target: { closest(selector) {
        return selector === `[data-${attribute}]` ? { dataset: { [name]: value } } : null;
      } } });
    },
    state(id) { return JSON.parse(vm.runInContext(`JSON.stringify(tlChildState(${JSON.stringify(id)}))`, context)); },
  };
}
const tick = () => new Promise(setImmediate);

test("follow-up count expands on demand outside the main date page and supports preview and tracking", async () => {
  const ui = page([event("parent", { child_count: 1 })]);
  assert.equal(ui.requests.length, 0);
  assert.match(ui.body.innerHTML, /data-tl-children="parent" aria-expanded="false" aria-controls="[^"]+"/);
  assert.match(ui.body.innerHTML, /class="tl-children"[^>]+ hidden/);
  ui.click("tl-children", "parent");
  assert.match(ui.body.innerHTML, /正在加载后续记录/);
  assert.match(ui.body.innerHTML, /aria-expanded="true"/);
  assert.equal(ui.requests.length, 1);
  const url = new URL(ui.requests[0].url, "http://localhost");
  assert.equal(url.pathname, "/api/timeline/parent/children");
  assert.deepEqual([...url.searchParams], [["offset", "0"], ["limit", "20"]]);
  ui.requests[0].resolve(response(childPage([event("old-child", { date: "2025-03-04", uploaded_by_name: "李工" })])));
  await tick();
  assert.match(ui.body.innerHTML, /2025 年 3 月 4 日/);
  assert.match(ui.body.innerHTML, /记录人：李工/);
  assert.match(ui.body.innerHTML, /data-tl-pv="old-child"/);
  assert.match(ui.body.innerHTML, /data-track="old-child"/);
  ui.click("tl-pv", "old-child");
  ui.click("track", "old-child");
  assert.deepEqual(ui.previews, ["old-child"]);
  assert.deepEqual(ui.tracks, ["old-child"]);
  ui.click("tl-children", "parent");
  assert.match(ui.body.innerHTML, /aria-expanded="false"/);
  assert.doesNotMatch(ui.body.innerHTML, /data-tl-pv="old-child"/);
  ui.click("tl-children", "parent");
  assert.match(ui.body.innerHTML, /data-tl-pv="old-child"/);
  assert.equal(ui.requests.length, 1, "reopening retains the loaded child page");
});

test("each follow-up page preserves previous records, retries its own offset and avoids duplicates", async () => {
  const ui = page([event("parent", { child_count: 22 })]);
  const initial = ui.run('toggleTimelineChildren("parent")');
  ui.requests[0].resolve(response(childPage([event("a"), event("b")], { total: 22, next_offset: 20, has_more: true })));
  await initial;
  assert.match(ui.body.innerHTML, /加载更多后续记录（已加载 2 \/ 22 条）/);
  const older = ui.run('loadTimelineChildren("parent")');
  assert.equal(new URL(ui.requests[1].url, "http://localhost").searchParams.get("offset"), "20");
  await ui.run('loadTimelineChildren("parent")');
  assert.equal(ui.requests.length, 2, "a pending request must not be duplicated");
  ui.requests[1].reject(new Error("网络暂不可用"));
  await older;
  assert.match(ui.body.innerHTML, /网络暂不可用/);
  assert.match(ui.body.innerHTML, /data-tl-children-more="parent">重试/);
  assert.match(ui.body.innerHTML, /data-tl-pv="a"/);
  const retry = ui.run('loadTimelineChildren("parent")');
  assert.equal(new URL(ui.requests[2].url, "http://localhost").searchParams.get("offset"), "20");
  ui.requests[2].resolve(response(childPage([event("b"), event("c")], { total: 22, next_offset: 22 })));
  await retry;
  assert.deepEqual(ui.state("parent").events.map((item) => item.doc_id), ["a", "b", "c"]);
  assert.doesNotMatch(ui.body.innerHTML, /重试|加载更多后续记录/);
});

test("follow-ups recursively expand and show non-ready records without editing actions", async () => {
  const ui = page([event("parent", { child_count: 2 })]);
  const initial = ui.run('toggleTimelineChildren("parent")');
  ui.requests[0].resolve(response(childPage([
    event("child", { child_count: 1, writable: false }),
    event("failed", { ingest_status: "failed", error: '<故障 "原因">', doc_name: '<失败 "记录">' }),
    event("processing", { ingest_status: "processing" }),
  ])));
  await initial;
  assert.match(ui.body.innerHTML, /入库失败/);
  assert.match(ui.body.innerHTML, /&lt;失败 "记录"&gt;/);
  assert.match(ui.body.innerHTML, /title="&lt;故障 &quot;原因&quot;&gt;"/);
  assert.doesNotMatch(ui.body.innerHTML, /data-track="(?:failed|processing|child)"/);
  assert.doesNotMatch(ui.body.innerHTML, /data-tl-status="(?:failed|processing|child)"/);
  assert.match(ui.body.innerHTML, /data-tl-pv="failed"/);
  const nested = ui.run('toggleTimelineChildren("child")');
  ui.requests[1].resolve(response(childPage([event("grandchild"), event("parent")])));
  await nested;
  assert.match(ui.body.innerHTML, /data-tl-pv="grandchild"/);
  assert.equal((ui.body.innerHTML.match(/data-tl-pv="parent"/g) || []).length, 1, "malformed cycles cannot recurse into ancestors");
  ui.click("track", "grandchild");
  assert.deepEqual(ui.tracks, ["grandchild"]);
});

test("filter changes discard stale follow-up responses and cached records", async () => {
  const ui = page([event("parent", { child_count: 1 })]);
  const old = ui.run('toggleTimelineChildren("parent")');
  ui.run('tlRequestId += 1; tlData = { events: [] }; renderTimeline()');
  assert.equal(ui.requests[0].options.signal.aborted, true);
  ui.requests[0].resolve(response(childPage([event("stale-private-child")])));
  await old;
  assert.doesNotMatch(ui.body.innerHTML, /stale-private-child/);
  assert.equal(ui.run('findTimelineEvent("stale-private-child")'), null);
  assert.equal(ui.run("tlChildren.size"), 0);
  ui.run(`tlData = { events: [${JSON.stringify(event("parent", { child_count: 1 }))}] }; renderTimeline()`);
  assert.match(ui.body.innerHTML, /aria-expanded="false"/);
  const current = ui.run('toggleTimelineChildren("parent")');
  assert.equal(ui.requests.length, 2);
  ui.requests[1].resolve(response(childPage([])));
  await current;
  assert.match(ui.body.innerHTML, /暂无可查看的后续记录/);
});

test("collapsing during a request keeps the response hidden and reuses it on reopen", async () => {
  const ui = page([event("parent", { child_count: 1 })]);
  const initial = ui.run('toggleTimelineChildren("parent")');
  await ui.run('toggleTimelineChildren("parent")');
  ui.requests[0].resolve(response(childPage([event("child")])));
  await initial;
  assert.doesNotMatch(ui.body.innerHTML, /data-tl-pv="child"/);
  await ui.run('toggleTimelineChildren("parent")');
  assert.match(ui.body.innerHTML, /data-tl-pv="child"/);
  assert.equal(ui.requests.length, 1);
});
