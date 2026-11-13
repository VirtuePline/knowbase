const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const html = fs.readFileSync(path.join(__dirname, "../web/index.html"), "utf8");
const script = html.match(/<script>([\s\S]*?)<\/script>/)[1];
function shippedFunction(name) {
  const match = script.match(new RegExp(`^(?:async )?function ${name}\\([^]*?^}`, "m"));
  assert.ok(match, `Missing function: ${name}`);
  return match[0];
}

test("document library groups each document once by its department and keeps newest first", () => {
  const context = vm.createContext({
    enterpriseDepartmentName: (id) => id,
    docRowHtml: (doc) => `<tr data-doc="${doc.id}"></tr>`,
  });
  for (const name of ["escapeHtml", "sortByUploadedDesc", "renderByDepartment"]) {
    vm.runInContext(shippedFunction(name), context);
  }
  const result = vm.runInContext(`renderByDepartment([
    {id:"old", department_id:"it", department_name:"信息<部", uploaded_at:"2026-11-01", categories:["网络问题", "常识库"]},
    {id:"hr", department_id:"hr", department_name:"人事部", uploaded_at:"2026-11-10"},
    {id:"new", department_id:"it", department_name:"信息<部", uploaded_at:"2026-11-13"}
  ])`, context);
  assert.equal((result.match(/class="doc-group"/g) || []).length, 2);
  assert.match(result, /信息&lt;部/);
  assert.ok(result.indexOf('data-doc="new"') < result.indexOf('data-doc="old"'));
  assert.equal((result.match(/data-doc="old"/g) || []).length, 1);
  assert.doesNotMatch(result, /常识库|网络问题/);
});

test("document management has only department grouping while record management retains categories", () => {
  const docs = html.slice(html.indexOf('id="tab-docs"'), html.indexOf('id="tab-timeline"'));
  assert.match(docs, /id="doc-department-filter"/);
  assert.doesNotMatch(docs, /data-view=|id="cat-filter"|<th>分类<|常识库/);
  assert.match(html, /id="rec-cat-filter"/);
});

test("editing a library document hides record categories and completion controls and restores them for records", () => {
  const controls = new Map();
  const context = vm.createContext({
    editingId: "doc", editingDocType: "doc", mdName: {value:"部门制度"}, trackingParent: null,
    $: (id) => { if (!controls.has(id)) controls.set(id, {}); return controls.get(id); },
  });
  vm.runInContext(shippedFunction("setEditorTitle"), context);
  context.setEditorTitle();
  assert.equal(controls.get("#editor-title").textContent, "编辑文档：部门制度");
  for (const id of ["md-cat-multi", "md-subcats", "md-time", "md-resolved"]) {
    assert.equal(controls.get("#" + id).hidden, true);
  }
  context.editingId = "";
  context.editingDocType = "record";
  context.setEditorTitle();
  assert.equal(controls.get("#editor-title").textContent, "新建工作记录");
  assert.equal(controls.get("#md-cat-multi").hidden, false);
});
