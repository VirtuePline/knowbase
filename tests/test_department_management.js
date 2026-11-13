// Exercise the shipped department forms and deletion flow with a local DOM/network substitute.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const source = fs.readFileSync(path.join(__dirname, "../web/enterprise.js"), "utf8");
const accounts = fs.readFileSync(path.join(__dirname, "../web/accounts.js"), "utf8");

function shippedFunction(name) {
  const match = source.match(new RegExp(`^(?:async )?function ${name}\\([^]*?^}`, "m"));
  assert.ok(match, `Missing shipped function: ${name}`);
  return match[0];
}

function page({ admin = true, documents = 2, departments, failure } = {}) {
  const directory = departments || [{ id: "operations", name: "默认部门" }, { id: "hr", name: "人事部" }];
  const requests = [];
  const dialogs = [];
  const notices = [];
  let cleared = 0, reloaded = 0;
  const context = vm.createContext({
    isSuperAdmin: admin,
    enterpriseDepartments: directory,
    enterpriseIdentity: { role: admin ? "admin" : "user" },
    enterpriseUsers: [],
    enterpriseManagedDepartments: () => directory,
    enterpriseDepartmentName: (id) => directory.find((item) => item.id === id)?.name || id,
    enterpriseJson: async (url, options = {}) => {
      requests.push({ url, ...options });
      if (options.method === "DELETE") {
        if (failure) throw new Error(failure);
        return { deleted: true };
      }
      return { department: directory[0], documents, members: 3, shared_documents: 4, departments: directory };
    },
    toast: (message) => notices.push(message),
    enterpriseClearSession: () => { cleared += 1; },
    window: { location: { reload: () => { reloaded += 1; } } },
    document: {
      getElementById: () => null,
      body: { appendChild() {} },
      createElement() {
        const listeners = {};
        const save = { disabled: false, addEventListener: (name, fn) => { listeners[name] = fn; } };
        const error = { textContent: "" };
        const target = { value: "" };
        const dialog = {
          innerHTML: "", closed: false, removed: false,
          querySelector(selector) {
            return { "[data-enterprise-save]": save, ".enterprise-error": error,
              "[data-department-transfer]": target }[selector];
          },
          showModal() {}, close() { this.closed = true; }, remove() { this.removed = true; },
          async confirm(value = "") { target.value = value; await listeners.click({ target: save }); },
          get error() { return error.textContent; },
        };
        dialogs.push(dialog);
        return dialog;
      },
    },
  });
  vm.runInContext(["enterpriseEscape", "enterpriseOptions", "enterpriseDepartmentRolesMarkup",
    "enterpriseMembershipValue", "enterpriseUsersRows", "enterpriseDepartmentDirectoryRows",
    "enterpriseOpenDialog", "enterpriseDeleteDepartment"].map(shippedFunction).join("\n"), context);
  return {
    requests, dialogs, notices,
    run: (script) => vm.runInContext(script, context),
    remove: () => vm.runInContext("enterpriseDeleteDepartment(enterpriseDepartments[0])", context),
    get cleared() { return cleared; }, get reloaded() { return reloaded; },
  };
}

test("department roles default to editor and offer only editor or administrator", () => {
  const ui = page();
  for (const roles of [{}, { hr: "member" }, { hr: "editor" }]) {
    const markup = ui.run(`enterpriseDepartmentRolesMarkup("account", ["hr"], ${JSON.stringify(roles)})`);
    assert.doesNotMatch(markup, /value="member"|只读成员/);
    assert.match(markup, /value="editor" selected/);
    assert.match(markup, /value="admin"/);
  }
  assert.match(ui.run('enterpriseDepartmentRolesMarkup("account", ["hr"], {hr: "admin"})'), /value="admin" selected/);
  assert.doesNotMatch(accounts, /option value="member"|只读成员/);
  assert.match(accounts, /option value="editor">文档编辑者/);
  const membership = ui.run(`enterpriseMembershipValue({ querySelectorAll: (selector) =>
    selector === "[data-membership]:checked" ? [{dataset: {membership: "hr"}}] : [] })`);
  assert.equal(membership.department_roles.hr, "editor");
});

test("user rows show editor roles and an explicit unassigned state", () => {
  const ui = page();
  const rows = ui.run(`enterpriseUsersRows([
    { account: "legacy", department_ids: ["hr"], department_roles: {hr: "member"} },
    { account: "unassigned", department_ids: [], department_roles: {} },
    { account: "invalid", department_ids: ["hr"], department_roles: {} }
  ])`);
  assert.match(rows, /人事部 · 文档编辑者/);
  assert.match(rows, /未分配部门/);
  assert.match(rows, /人事部 · 角色异常/);
  assert.doesNotMatch(rows, /只读成员/);
});

test("department deletion buttons are platform-only and the last department is protected", async () => {
  const manager = page({ admin: false });
  assert.doesNotMatch(manager.run("enterpriseDepartmentDirectoryRows()"), /data-delete-department/);
  await manager.remove();
  assert.deepEqual(manager.requests, []);
  const admin = page();
  assert.match(admin.run("enterpriseDepartmentDirectoryRows()"), /data-delete-department="operations"/);
  const last = page({ departments: [{ id: "operations", name: "默认部门" }] });
  assert.match(last.run("enterpriseDepartmentDirectoryRows()"), /data-delete-department="operations" disabled/);
  await last.remove();
  assert.equal(last.dialogs.length, 0);
  assert.equal(last.requests.length, 1);
  assert.match(last.notices[0], /至少保留一个部门/);
});

test("opening or cancelling deletion never sends DELETE and displays the concrete effects", async () => {
  const ui = page();
  await ui.remove();
  assert.equal(ui.requests.length, 1);
  assert.equal(ui.requests[0].url, "/api/departments/operations/deletion");
  const dialog = ui.dialogs[0];
  for (const label of ["所属文档", "部门成员", "共享文档", "账号保留", "目标部门成员可访问"]) {
    assert.ok(dialog.innerHTML.includes(label));
  }
  assert.match(dialog.innerHTML, /<form method="dialog">/);
  assert.match(dialog.innerHTML, /<button value="cancel" class="ghost">取消/);
  assert.match(dialog.innerHTML, /class="primary danger" data-enterprise-save>删除部门/);
  dialog.close("cancel");
  assert.equal(ui.requests.length, 1);
  assert.equal(ui.reloaded, 0);
});

test("document migration requires an explicit valid target before deleting", async () => {
  const ui = page();
  await ui.remove();
  const dialog = ui.dialogs[0];
  await dialog.confirm();
  assert.match(dialog.error, /请选择接收文档的部门/);
  assert.equal(ui.requests.length, 1);
  assert.equal(dialog.closed, false);
  await dialog.confirm("operations");
  assert.equal(ui.requests.length, 1);
  await dialog.confirm("hr");
  assert.equal(ui.requests[1].url, "/api/departments/operations");
  assert.equal(ui.requests[1].method, "DELETE");
  assert.deepEqual(JSON.parse(ui.requests[1].body), { target_department_id: "hr" });
  assert.equal(ui.cleared, 1);
  assert.equal(ui.reloaded, 1);
  assert.equal(dialog.closed, true);
});

test("departments without owned documents can be deleted without migrating", async () => {
  const ui = page({ documents: 0 });
  await ui.remove();
  await ui.dialogs[0].confirm();
  assert.deepEqual(JSON.parse(ui.requests[1].body), { target_department_id: "" });
  assert.equal(ui.reloaded, 1);
});

test("a rejected deletion retains the dialog and explains the server response", async () => {
  const ui = page({ failure: "目标部门已被删除，请重新选择" });
  await ui.remove();
  const dialog = ui.dialogs[0];
  await dialog.confirm("hr");
  assert.equal(dialog.error, "目标部门已被删除，请重新选择");
  assert.equal(dialog.closed, false);
  assert.equal(ui.cleared, 0);
  assert.equal(ui.reloaded, 0);
  assert.equal(dialog.querySelector("[data-enterprise-save]").disabled, false);
});
