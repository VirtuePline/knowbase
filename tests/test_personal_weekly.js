// Run the shipped weekly page with an in-memory DOM and controlled API responses.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const html = fs.readFileSync(path.join(__dirname, "../web/index.html"), "utf8");
const script = html.match(/<script>([\s\S]*?)<\/script>/)[1];
const weeklyScript = script.slice(script.indexOf("/* ---------- 工作周报："),
  script.indexOf("/* ---------- 账号管理："));

function shippedFunction(name) {
  const match = script.match(new RegExp(`^(?:async )?function ${name}\\([^]*?^}`, "m"));
  assert.ok(match, `Missing shipped function: ${name}`);
  return match[0];
}

function page({ admin = false, manager = false, authenticated = true, portal = "admin", failSettings = false } = {}) {
  const nodes = new Map();
  const requests = [];
  const notices = [];
  let customFetch = null;
  const find = (selector) => {
    if (!nodes.has(selector)) {
      assert.ok(html.includes(`id="${selector.slice(1)}"`), `Missing shipped element ${selector}`);
      nodes.set(selector, {
        value: "", checked: false, disabled: false, hidden: false, innerHTML: "", textContent: "",
        listeners: {}, addEventListener(name, handler) { this.listeners[name] = handler; },
        reportValidity() { return this.value !== ""; },
      });
    }
    return nodes.get(selector);
  };
  const context = vm.createContext({
    portal, isAuthenticated: authenticated, isSuperAdmin: admin, isAccountAdmin: manager || admin,
    $: find,
    fetch: async (url, options = {}) => {
      requests.push({ url, ...options });
      if (customFetch) return customFetch(url, options);
      let data;
      let ok = true;
      if (url === "/api/weekly-report/my-settings") {
        ok = !failSettings;
        data = ok ? { enabled: false, weekday: 5, time: "18:00" } : { detail: "设置暂时无法读取" };
      } else if (url === "/api/weekly-report/settings") {
        data = { enabled: true, weekday: 3, time: "12:00", operators: ["colleague"], owners: ["employee", "colleague"] };
      } else if (url === "/api/weekly-reports") data = { reports: [] };
      else if (url === "/api/weekly-reports/generate") data = { reports: [{ person: "employee" }] };
      else throw new Error(`Unexpected API: ${url}`);
      return { ok, json: async () => data };
    },
    toast: (message) => notices.push(message),
    renderMarkdown: (value) => value,
  });
  vm.runInContext([shippedFunction("escapeHtml"), shippedFunction("escapeAttr"), weeklyScript].join("\n"), context);
  return { find, requests, notices, run: (source) => vm.runInContext(source, context),
    click: (selector) => find(selector).listeners.click(),
    setFetch: (handler) => { customFetch = handler; },
  };
}

function deferred() {
  let resolve;
  const promise = new Promise((done) => { resolve = done; });
  return { promise, resolve: (data) => resolve({ ok: true, json: async () => data }) };
}

test("personal settings are separate from the platform-only owner list", () => {
  assert.match(html, /class="panel-card" id="wr-personal-settings"/);
  assert.match(html, /class="panel-card super-admin-only" id="wr-global-settings" hidden/);
  assert.match(html, /id="wr-generate"[^>]*>生成我的本周周报/);
  assert.match(html, /id="wr-generate-managed" hidden/);
});

test("an ordinary employee reads their settings and reports without fetching administrator settings", async () => {
  const ui = page(); // Deliberately no document-write or administrator variable.
  await ui.run("refreshWeeklyReport()");
  assert.deepEqual(ui.requests.map(({ url }) => url).sort(), ["/api/weekly-report/my-settings", "/api/weekly-reports"]);
  assert.equal(ui.find("#wr-save").disabled, false);
  assert.equal(ui.find("#wr-time").value, "18:00");
  assert.equal(ui.find("#wr-global-settings").hidden, true);
  assert.equal(ui.find("#wr-generate-managed").hidden, true);
  assert.match(ui.find("#wr-reports").innerHTML, /生成我的本周周报/);
});

test("saving personal settings never sends other accounts or a global owner list", async () => {
  const ui = page();
  await ui.run("refreshWeeklyReport()");
  ui.requests.length = 0;
  ui.run('wrSelected = new Set(["colleague", "another-account"])');
  ui.find("#wr-enabled").checked = true;
  ui.find("#wr-weekday").value = "2";
  ui.find("#wr-time").value = "09:30";
  await ui.click("#wr-save");
  assert.equal(ui.requests.length, 1);
  assert.equal(ui.requests[0].url, "/api/weekly-report/my-settings");
  assert.equal(ui.requests[0].method, "PUT");
  assert.deepEqual(JSON.parse(ui.requests[0].body), { enabled: true, weekday: 2, time: "09:30" });
  assert.equal(ui.find("#wr-save").disabled, false);
  assert.equal(ui.notices.at(-1), "我的周报设置已保存");
});

test("a failed initial settings read cannot overwrite a schedule with default values", async () => {
  const ui = page({ failSettings: true });
  await ui.run("refreshWeeklyReport()");
  assert.equal(ui.find("#wr-save").disabled, true);
  assert.equal(ui.find("#wr-status").textContent, "设置暂时无法读取");
  ui.requests.length = 0;
  await ui.click("#wr-save");
  assert.deepEqual(ui.requests, []);
  assert.match(ui.notices.at(-1), /加载周报设置/);
});

for (const role of [{}, { manager: true }, { admin: true }]) {
  test(`${role.admin ? "platform admin" : role.manager ? "department admin" : "employee"} self generation targets only their own report`, async () => {
    const ui = page(role);
    await ui.click("#wr-generate");
    const generate = ui.requests.find((request) => request.method === "POST");
    assert.equal(generate.url, "/api/weekly-reports/generate");
    assert.deepEqual(JSON.parse(generate.body), { scope: "self" });
    assert.equal(ui.find("#wr-generate-status").textContent, "我的本周周报已生成");
    assert.equal(ui.find("#wr-generate").disabled, false);
  });
}

test("department admins have a separate managed action while employees cannot invoke it", async () => {
  const employee = page();
  await employee.click("#wr-generate-managed");
  assert.deepEqual(employee.requests, []);
  const manager = page({ manager: true });
  await manager.run("refreshWeeklyReport()");
  assert.equal(manager.find("#wr-generate-managed").hidden, false);
  assert.equal(manager.find("#wr-generate-managed").textContent, "生成本部门周报");
  assert.equal(manager.find("#wr-global-settings").hidden, true);
  assert.ok(manager.requests.every(({ url }) => url !== "/api/weekly-report/settings"));
  manager.requests.length = 0;
  await manager.click("#wr-generate-managed");
  assert.deepEqual(JSON.parse(manager.requests[0].body), { scope: "managed" });
});

test("platform admins edit their own schedule independently from the global schedule", async () => {
  const ui = page({ admin: true });
  await ui.run("refreshWeeklyReport()");
  assert.equal(ui.find("#wr-time").value, "18:00");
  assert.equal(ui.find("#wr-global-time").value, "12:00");
  assert.equal(ui.find("#wr-global-settings").hidden, false);
  ui.requests.length = 0;
  await ui.click("#wr-save");
  assert.equal(ui.requests[0].url, "/api/weekly-report/my-settings");
  assert.equal("operators" in JSON.parse(ui.requests[0].body), false);
  await ui.click("#wr-global-save");
  assert.equal(ui.requests[1].url, "/api/weekly-report/settings");
  assert.deepEqual(JSON.parse(ui.requests[1].body), { enabled: true, weekday: 3, time: "12:00", operators: ["colleague"] });
});

test("public and unauthenticated management pages do not fetch or modify weekly data", async () => {
  for (const options of [{ portal: "search", admin: true }, { authenticated: false }]) {
    const ui = page(options);
    await ui.run("refreshWeeklyReport(); loadWeeklySettings(true); saveWeeklySettings(); generateWeeklyReport() ");
    assert.deepEqual(ui.requests, []);
  }
});

test("older settings refreshes cannot replace the latest form while personal and global reads remain independent", async () => {
  const ui = page({ admin: true });
  const pending = [];
  ui.setFetch(() => { const request = deferred(); pending.push(request); return request.promise; });
  const older = ui.run("loadWeeklySettings()");
  const global = ui.run("loadWeeklySettings(true)");
  const latest = ui.run("loadWeeklySettings()");
  pending[1].resolve({ enabled: true, weekday: 3, time: "12:00", operators: ["colleague"] });
  await global;
  assert.equal(ui.find("#wr-global-time").value, "12:00");
  assert.equal(ui.find("#wr-global-save").disabled, false);
  assert.equal(ui.find("#wr-save").disabled, true);
  pending[2].resolve({ enabled: true, weekday: 2, time: "10:00" });
  await latest;
  pending[0].resolve({ enabled: false, weekday: 1, time: "07:00" });
  await older;
  assert.equal(ui.find("#wr-time").value, "10:00");
  assert.equal(ui.find("#wr-weekday").value, "2");
  assert.equal(ui.find("#wr-enabled").checked, true);
  assert.equal(ui.find("#wr-status").textContent, "");
  assert.equal(ui.find("#wr-save").disabled, false);
  assert.equal(ui.find("#wr-global-time").value, "12:00");
});

test("saving invalidates old reads and blocks same-form duplicate saves or refreshes", async () => {
  for (const global of [false, true]) {
    const ui = page({ admin: true });
    await ui.run("refreshWeeklyReport()");
    ui.requests.length = 0;
    const pending = [];
    ui.setFetch(() => { const request = deferred(); pending.push(request); return request.promise; });
    const prefix = global ? "wr-global" : "wr";
    const older = ui.run(`loadWeeklySettings(${global})`);
    const latest = ui.run(`loadWeeklySettings(${global})`);
    pending[1].resolve({ enabled: true, weekday: 2, time: "10:00", operators: [] });
    await latest;
    ui.find(`#${prefix}-time`).value = "11:30";
    const saving = ui.click(`#${prefix}-save`);
    assert.equal(ui.requests.length, 3);
    assert.equal(ui.requests[2].method, "PUT");
    await ui.run(`loadWeeklySettings(${global}); saveWeeklySettings(${global})`);
    assert.equal(ui.requests.length, 3);
    assert.equal(ui.find(`#${prefix}-save`).disabled, true);
    assert.equal(ui.find(`#${prefix}-status`).textContent, "保存中…");
    pending[0].resolve({ enabled: false, weekday: 1, time: "07:00", operators: ["stale-account"] });
    await older;
    assert.equal(ui.find(`#${prefix}-time`).value, "11:30");
    assert.equal(ui.find(`#${prefix}-status`).textContent, "保存中…");
    pending[2].resolve({ enabled: true, weekday: 2, time: "11:30" });
    await saving;
    assert.equal(ui.find(`#${prefix}-time`).value, "11:30");
    assert.equal(ui.find(`#${prefix}-save`).disabled, false);
    assert.equal(ui.find(`#${prefix}-status`).textContent, "已保存，到点自动生成");
    assert.equal(JSON.parse(ui.requests[2].body).time, "11:30");
  }
});
