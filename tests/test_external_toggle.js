// Run with: node --test tests/test_external_toggle.js
// Execute the shipped toggle functions; stub only unrelated document services.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const html = fs.readFileSync(path.join(__dirname, "../web/index.html"), "utf8");
const css = fs.readFileSync(path.join(__dirname, "../web/styles.css"), "utf8");
const script = html.match(/<script>([\s\S]*?)<\/script>/)[1];

function shippedFunction(name) {
  const match = script.match(new RegExp(`^(?:async )?function ${name}\\([^]*?^}`, "m"));
  assert.ok(match, `Missing application function: ${name}`);
  return match[0];
}

function element(attributes = {}) {
  const classes = new Set((attributes.class || "").split(/\s+/).filter(Boolean));
  return {
    id: attributes.id,
    textContent: "",
    innerHTML: "",
    hidden: false,
    classList: { contains: (name) => classes.has(name) },
    setAttribute: (name, value) => { attributes[name] = value; },
    removeAttribute: (name) => { delete attributes[name]; },
    getAttribute: (name) => attributes[name],
  };
}

function createPage({ superAdmin = true, status = { enabled: true } } = {}) {
  const elements = new Map();
  for (const id of ["external-apis-panel", "external-apis-toggle", "external-apis-status"]) {
    const attributes = { id };
    const match = html.match(new RegExp(`<([a-z]+)[^>]*id="${id}"[^>]*>`));
    if (match) attributes.class = (match[0].match(/class="([^"]*)"/) || [])[1] || "";
    elements.set(id, element(attributes));
  }
  const panelAttributes = { id: "external-apis-panel", class: "panel-card super-admin-only" };
  elements.set("external-apis-panel", element(panelAttributes));
  const find = (selector) => {
    if (selector.startsWith("#")) return elements.get(selector.slice(1));
    throw new Error(`Unexpected selector: ${selector}`);
  };
  const requests = [];
  const toasts = [];
  let currentStatus = status;
  const context = vm.createContext({
    document: { querySelector: find, querySelectorAll: () => [] },
    $: find,
    window: { location: { replace: () => {} } },
    toast: (message) => toasts.push(message),
    isSuperAdmin: superAdmin,
    fetch: async (url, options = {}) => {
      requests.push({ url, options });
      if (url === "/api/admin/external-apis" && options.method === "POST") {
        const body = JSON.parse(options.body);
        currentStatus = { enabled: body.enabled === true };
        return { ok: true, json: async () => currentStatus };
      }
      if (url === "/api/admin/external-apis") return { ok: true, json: async () => currentStatus };
      throw new Error(`Unexpected request: ${url}`);
    },
  });
  vm.runInContext(["isSuperAdmin", "renderExternalApis", "refreshExternalApis", "toggleExternalApis"]
    .map((name) => (name === "isSuperAdmin" ? "let isSuperAdmin = false;" : shippedFunction(name)))
    .join("\n"), context);
  context.run = (source) => vm.runInContext(source, context);
  context.run("isSuperAdmin = " + (superAdmin ? "true" : "false"));
  return { elements, find, requests, toasts, run: context.run,
    setStatus: (value) => { currentStatus = value; } };
}

test("the settings page ships the external APIs panel and toggle wiring", () => {
  assert.ok(html.includes('id="external-apis-panel"'), "设置页缺少外围接口面板");
  assert.ok(html.includes('id="external-apis-toggle"'), "缺少开关按钮");
  assert.ok(html.includes('id="external-apis-status"'), "缺少状态文案");
  assert.match(css, /body:not\(\.super-admin\)\s+\.super-admin-only\s*\{[^}]*display:\s*none\s*!important/,
    "面板必须对非平台管理员隐藏");
  assert.ok(script.includes('addEventListener("click", toggleExternalApis)'), "开关按钮未绑定点击事件");
  assert.ok(script.includes("await refreshExternalApis()"), "进入设置页未刷新开关状态");
});

test("super admin sees current state and can switch to fully offline mode", async () => {
  const page = createPage({ superAdmin: true, status: { enabled: true } });
  await page.run("refreshExternalApis()");
  assert.equal(page.find("#external-apis-toggle").textContent, "关闭外围接口");
  assert.equal(page.find("#external-apis-toggle").getAttribute("aria-pressed"), "true");
  assert.match(page.find("#external-apis-status").textContent, /当前联网/);

  page.requests.length = 0;
  await page.run("toggleExternalApis()");
  assert.equal(page.requests.length, 1);
  assert.equal(page.requests[0].url, "/api/admin/external-apis");
  assert.deepEqual(JSON.parse(page.requests[0].options.body), { enabled: false });
  assert.equal(page.find("#external-apis-toggle").textContent, "开启外围接口");
  assert.equal(page.find("#external-apis-toggle").getAttribute("aria-pressed"), "false");
  assert.match(page.find("#external-apis-status").textContent, /全离线/);
  assert.ok(page.toasts.some((t) => t.includes("全离线")));
});

test("toggle can restore online mode", async () => {
  const page = createPage({ superAdmin: true, status: { enabled: false } });
  await page.run("refreshExternalApis()");
  assert.equal(page.find("#external-apis-toggle").textContent, "开启外围接口");
  page.requests.length = 0;
  await page.run("toggleExternalApis()");
  assert.equal(page.requests.length, 1);
  assert.deepEqual(JSON.parse(page.requests[0].options.body), { enabled: true });
  assert.match(page.find("#external-apis-status").textContent, /当前联网/);
});

test("ordinary account never loads or toggles the switch", async () => {
  const page = createPage({ superAdmin: false, status: { enabled: true } });
  await page.run("refreshExternalApis()");
  assert.equal(page.requests.length, 0, "普通账号不应请求开关状态");
  assert.equal(page.find("#external-apis-toggle").textContent, "", "普通账号不应渲染开关文案");
  await page.run("toggleExternalApis()");
  assert.equal(page.requests.length, 0, "普通账号不应提交开关请求");
});
