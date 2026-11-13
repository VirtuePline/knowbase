// Run with: node --test tests/test_role_navigation.js
// Execute the shipped role/navigation functions; stub only unrelated document services.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const html = fs.readFileSync(path.join(__dirname, "../web/index.html"), "utf8");
const css = fs.readFileSync(path.join(__dirname, "../web/styles.css"), "utf8");
const enterpriseSource = fs.readFileSync(path.join(__dirname, "../web/enterprise.js"), "utf8");
const script = html.match(/<script>([\s\S]*?)<\/script>/)[1];

function shippedFunction(source, name) {
  const match = source.match(new RegExp(`^(?:async )?function ${name}\\([^]*?^}`, "m"));
  assert.ok(match, `Missing application function: ${name}`);
  return match[0];
}

function shippedDeclaration(source, name) {
  const match = source.match(new RegExp(`^(?:let|const) ${name} = [^;]+;`, "m"));
  assert.ok(match, `Missing application state: ${name}`);
  return match[0];
}

function element(attributes = {}) {
  const classes = new Set((attributes.class || "").split(/\s+/).filter(Boolean));
  return {
    id: attributes.id,
    dataset: { tab: attributes["data-tab"] },
    textContent: "",
    innerHTML: "",
    value: "",
    hidden: false,
    classList: {
      contains: (name) => classes.has(name),
      toggle(name, force) {
        const enabled = force === undefined ? !classes.has(name) : force;
        if (enabled) classes.add(name); else classes.delete(name);
        return enabled;
      },
      add: (name) => classes.add(name),
      remove: (...names) => names.forEach((name) => classes.delete(name)),
    },
    setAttribute: (name, value) => { attributes[name] = value; },
    removeAttribute: (name) => { delete attributes[name]; },
    getAttribute: (name) => attributes[name],
  };
}

function createPage(mode, savedTab = "") {
  // Preserve IDs and role classes from the real HTML rather than inventing a fixture menu.
  const elements = Array.from(html.matchAll(/<(body|button|section|[a-z]+)\b([^>]+)>/g), (match) => {
    const attributes = Object.fromEntries(Array.from(match[2].matchAll(/([\w-]+)="([^"]*)"/g),
      (attribute) => [attribute[1], attribute[2]]));
    return { tag: match[1], ...element(attributes) };
  });
  const find = (selector) => {
    if (selector.startsWith("#")) return elements.find((el) => el.id === selector.slice(1));
    const tab = selector.match(/^\.tab-btn\[data-tab="([^"]+)"\]$/);
    if (tab) return elements.find((el) => el.dataset.tab === tab[1]);
    throw new Error(`Unexpected selector: ${selector}`);
  };
  const requests = [];
  const redirects = [];
  const storage = new Map([["kb_last_tab", savedTab]]);
  let currentMode = mode;
  const document = {
    body: elements.find((el) => el.tag === "body"),
    querySelector: find,
    querySelectorAll: (selector) => elements.filter((el) => el.classList.contains(selector.slice(1))),
  };
  const context = vm.createContext({
    document,
    $: find,
    localStorage: { getItem: (key) => storage.get(key), setItem: (key, value) => storage.set(key, value) },
    window: { location: { replace: (url) => redirects.push(url) } },
    openLogin: () => redirects.push("/login"),
    fetch: async (url) => {
      requests.push(url);
      if (url === "/api/mode") {
        if (currentMode instanceof Error) throw currentMode;
        return { ok: true, json: async () => currentMode };
      }
      if (url === "/api/admin/users") return { ok: true, json: async () => ({ users: [
        { username: "admin", display_name: "管理员", role: "admin" },
        { username: "operator", display_name: "运维员", role: "user" },
      ] }) };
      if (url.startsWith("/api/audit-logs?")) return { ok: true, json: async () => ({ total: 0, logs: [] }) };
      throw new Error(`Unexpected request: ${url}`);
    },
  });
  for (const name of ["loadTopQuestions", "loadCategories", "loadDocuments", "loadKnowledgeStats", "loadVendors", "loadTemplates",
    "syncTplEditor", "renderVendorList", "resetEditor", "loadRecordList", "loadTimeline",
    "refreshSettings", "refreshWeeklyReport", "loadEditsLog"]) {
    context[name] = () => {};
  }
  // 管理端声明的状态与函数（index.html）
  const declarations = ["isAdmin", "isSuperAdmin", "isAccountAdmin", "currentUser", "currentDisplayName",
    "portal", "authEnabled", "LAST_TAB_KEY", "PUBLIC_TABS", "SUPER_ADMIN_TABS", "ADMIN_TABS",
    "AUDIT_PAGE_SIZE", "auditPage", "auditTotal", "usersBody", "usersStatus", "sendBtn"];
  const functions = ["escapeHtml", "escapeAttr", "applyRoleVisibility", "canAccessTab", "lastTab",
    "updateNavigation", "switchTo", "loadUsers", "loadAuditLogs", "initPage"];
  // 企业权限模块（enterprise.js）：用户表格用真实渲染代码，部门初始化用轻量 stub
  const enterpriseParts = [
    shippedDeclaration(enterpriseSource, "enterpriseIdentity"),
    shippedDeclaration(enterpriseSource, "enterpriseUsers"),
    shippedDeclaration(enterpriseSource, "enterpriseDepartments"),
    shippedFunction(enterpriseSource, "enterpriseUsersRows"),
    `async function enterpriseInitialize(data) { enterpriseIdentity = data; }`,
    `function enterpriseEscape(value) { return String(value ?? "").replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;"); }`,
    `function enterpriseDepartmentName() { return ""; }`,
  ].join("\n");
  vm.runInContext([...declarations.map((name) => shippedDeclaration(script, name)),
    ...functions.map((name) => shippedFunction(script, name)), enterpriseParts].join("\n"), context);
  return {
    document, find, requests, redirects,
    setMode: (value) => { currentMode = value; },
    run: (source) => vm.runInContext(source, context),
    async init() {
      await vm.runInContext("initPage()", context);
      await new Promise(setImmediate); // Finish async tab loaders invoked by switchTo.
    },
    activeTab: () => elements.find((el) => el.tag === "section" && el.classList.contains("active"))?.id,
  };
}

const admin = { portal: "admin", authenticated: true, role: "admin", can_write: true, can_manage_users: true, can_register: true, account: "admin" };
const operator = { portal: "admin", authenticated: true, role: "user", can_register: false, account: "operator" };
const guest = { portal: "search", public_access: true, authenticated: false, auth: false,
  role: "guest", can_write: false, can_manage_users: false, account: "", permission_stamp: "public-v1" };

test("the full shipped script has valid JavaScript syntax", () => {
  assert.doesNotThrow(() => new vm.Script(script));
});

test("administrator-only navigation and panels stay hidden until role initialization", () => {
  const page = createPage(admin);
  assert.equal(page.document.body.classList.contains("super-admin"), false);
  assert.match(css, /body:not\(\.super-admin\)\s+\.super-admin-only\s*\{[^}]*display:\s*none\s*!important/);
  for (const tab of ["edits", "logs"]) {
    assert.ok(page.find(`.tab-btn[data-tab="${tab}"]`).classList.contains("super-admin-only"));
    assert.ok(page.find(`#tab-${tab}`).classList.contains("super-admin-only"));
  }
  assert.ok(page.find(`.tab-btn[data-tab="accounts"]`).classList.contains("account-admin-only"));
  assert.ok(page.find("#tab-accounts").classList.contains("account-admin-only"));
});

for (const tab of ["accounts", "logs"]) {
  test(`admin initializes role and restores ${tab} with working data loading`, async () => {
    const page = createPage(admin, tab);
    await page.init();
    assert.ok(page.document.body.classList.contains("super-admin"));
    assert.ok(page.document.body.classList.contains("account-admin"));
    assert.equal(page.find("#current-role").textContent, "平台管理员");
    assert.equal(page.activeTab(), `tab-${tab}`);
    if (tab === "accounts") {
      assert.ok(page.requests.includes("/api/admin/users"));
      assert.match(page.find("#users-body").innerHTML, /运维员/);
      assert.match(page.find("#users-body").innerHTML, /data-del-user="operator"/);
      assert.doesNotMatch(page.find("#users-body").innerHTML, /data-del-user="admin"/);
    } else {
      assert.ok(page.requests.some((url) => url.startsWith("/api/audit-logs?")));
      assert.match(page.find("#audit-body").innerHTML, /暂无操作记录/);
    }
  });
}

for (const tab of ["accounts", "logs", "edits"]) {
  test(`ordinary account rejects previously saved administrator tab ${tab}`, async () => {
    const page = createPage(operator, tab);
    await page.init();
    assert.equal(page.activeTab(), "tab-chat");
    assert.equal(page.document.body.classList.contains("super-admin"), false);
    assert.equal(page.document.body.classList.contains("account-admin"), false);
    assert.equal(page.find("#current-role").textContent, "未分配部门");
    page.run(`switchTo("${tab}")`);
    assert.equal(page.activeTab(), "tab-chat");
    await page.run("loadUsers(); loadAuditLogs()");
    assert.deepEqual(page.requests, ["/api/mode"]);
  });
}

test("search portal remains public even with an administrator role in a stale payload", async () => {
  const page = createPage({ ...admin, portal: "search" }, "logs");
  await page.init();
  assert.equal(page.activeTab(), "tab-chat");
  assert.equal(page.document.body.classList.contains("super-admin"), false);
  assert.equal(page.document.body.classList.contains("admin-mode"), false);
  page.run('switchTo("accounts"); switchTo("logs")');
  assert.equal(page.activeTab(), "tab-chat");
  assert.deepEqual(page.requests, ["/api/mode"]);
});

test("anonymous public portal opens without login and hides account controls", async () => {
  const page = createPage(guest, "accounts");
  await page.init();
  assert.equal(page.activeTab(), "tab-chat");
  assert.deepEqual(page.redirects, []);
  assert.equal(page.find("#mode-badge").textContent, "公共访问 · 仅公共文档");
  assert.equal(page.find("#mode-badge").disabled, true);
  assert.equal(page.find("#account-summary").hidden, true);
  assert.equal(page.find("#change-password-link").hidden, true);
  assert.equal(page.document.body.classList.contains("public-access"), true);
  assert.equal(page.run('canAccessTab("editor")'), false);
  assert.equal(page.run('canAccessTab("accounts")'), false);
  assert.equal(page.find("#send").disabled, false);
  assert.deepEqual(page.requests, ["/api/mode"]);
});

test("anonymous management portal continues to require login", async () => {
  const page = createPage({ portal: "admin", authenticated: false, admin: false });
  await page.init();
  assert.deepEqual(page.redirects, ["/login"]);
  assert.equal(page.run('canAccessTab("editor")'), false);
});

test("public initialization does not request the department directory", async () => {
  const classes = new Set();
  const label = { textContent: "" };
  let departmentLoads = 0;
  const context = vm.createContext({
    portal: "search",
    document: { body: { classList: { toggle(name, enabled) { if (enabled) classes.add(name); else classes.delete(name); } } },
      querySelector: () => label },
    localStorage: { removeItem() {} },
    enterpriseLoadDepartments: async () => { departmentLoads++; },
  });
  vm.runInContext(["enterpriseIdentity", "enterpriseDepartments", "enterpriseUsers"]
    .map((name) => shippedDeclaration(enterpriseSource, name)).join("\n") + "\n"
    + shippedFunction(enterpriseSource, "enterpriseInitialize"), context);
  context.payload = { ...admin, portal: "search" }; // 即使旧响应残留管理员字段也要降为公共只读。
  await vm.runInContext("enterpriseInitialize(payload)", context);
  assert.equal(departmentLoads, 0);
  assert.equal(label.textContent, "公共访问 · 仅公共文档");
  assert.equal(vm.runInContext("enterpriseIdentity.role", context), "guest");
  assert.equal(vm.runInContext("enterpriseIdentity.account", context), "");
  assert.equal(vm.runInContext("enterpriseDepartments.length", context), 0);
  assert.equal(classes.has("department-manager"), false);
});

test("failed session refresh clears administrator visibility and permissions", async () => {
  const page = createPage(admin, "accounts");
  await page.init();
  assert.ok(page.document.body.classList.contains("super-admin"));
  page.requests.length = 0;
  page.setMode(new Error("session service unavailable"));
  await page.init();
  assert.equal(page.activeTab(), "tab-chat");
  assert.equal(page.document.body.classList.contains("super-admin"), false);
  assert.equal(page.document.body.classList.contains("account-admin"), false);
  assert.equal(page.document.body.classList.contains("admin-mode"), false);
  assert.equal(page.run('canAccessTab("accounts")'), false);
  assert.equal(page.run('canAccessTab("logs")'), false);
  await page.run("loadUsers(); loadAuditLogs()");
  assert.deepEqual(page.requests, ["/api/mode"]);
});
