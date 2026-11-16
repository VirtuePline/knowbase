const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const html = fs.readFileSync(path.join(__dirname, "../web/index.html"), "utf8");
const script = html.match(/<script>([\s\S]*?)<\/script>/)[1];

function page() {
  const requests = [], messages = [];
  let reloads = 0;
  const body = { addEventListener(type, handler) { this[type] = handler; } };
  const context = vm.createContext({
    tlBody: body,
    enterpriseCanWrite: (event) => event.can_write === true,
    fetch: async (url, options) => {
      requests.push({ url, ...options, body: JSON.parse(options.body) });
      return { ok: true };
    },
    loadTimeline: async () => { reloads++; },
    toast: (message) => messages.push(message),
  });
  const functions = ["escapeHtml", "escapeAttr", "tlBadge", "tlEffective", "tlStatusControl"]
    .map((name) => {
      const match = script.match(new RegExp(`^function ${name}\\([^]*?^}`, "m"));
      assert.ok(match, `Missing shipped function: ${name}`);
      return match[0];
    });
  const handler = script.match(/^tlBody\.addEventListener\("change", async \(e\) => \{[^]*?^\}\);/m);
  assert.ok(handler, "Missing shipped timeline status handler");
  vm.runInContext([...functions, handler[0]].join("\n"), context);
  return {
    requests, messages,
    get reloads() { return reloads; },
    render(status, canWrite = true) {
      return vm.runInContext(`tlStatusControl(${JSON.stringify({
        status, can_write: canWrite, chunk_id: "record-1", doc_id: "record-1",
      })})`, context);
    },
    async choose(markup, label) {
      const options = [...markup.matchAll(/<option value="([^"]*)"[^>]*>([^<]*)<\/option>/g)];
      const option = options.find(([, , text]) => text.startsWith(label));
      assert.ok(option, `Missing status option: ${label}`);
      const select = { value: option[1], dataset: { tlStatus: "record-1", tlDoc: "record-1" } };
      await body.change({ target: { closest: () => select } });
    },
  };
}

for (const initial of ["improving", "temporary", "resolved"]) {
  test(`changing ${initial} to unfinished submits an explicit override and reloads`, async () => {
    const ui = page();
    await ui.choose(ui.render(initial), "未完成");
    assert.equal(ui.requests.length, 1);
    assert.equal(ui.requests[0].url, "/api/timeline/event-status");
    assert.equal(ui.requests[0].method, "PATCH");
    assert.deepEqual(ui.requests[0].body, {
      chunk_id: "record-1", doc_id: "record-1", status: "unresolved",
    });
    assert.equal(ui.reloads, 1);
    assert.deepEqual(ui.messages, ["完成情况已标记"]);
    assert.match(ui.render("unresolved"), /<option value="unresolved" selected>未完成<\/option>/);
  });
}

test("unmarked and legacy unknown records select unfinished without offering an empty reset value", () => {
  const ui = page();
  for (const status of ["", "unknown", "unresolved"]) {
    const markup = ui.render(status);
    assert.match(markup, /<option value="unresolved" selected>未完成<\/option>/);
    assert.doesNotMatch(markup, /<option value=""/);
  }
});

test("read-only records retain their status badges", () => {
  const ui = page();
  assert.match(ui.render("unresolved", false), /<span class="tl-badge no">未完成<\/span>/);
  assert.doesNotMatch(ui.render("unresolved", false), /<select/);
});
