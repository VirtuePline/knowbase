/* 管理员工作周报：列表按「部门 → 个人」分组；统一周报设置移入用户管理页。 */
const test = require("node:test");
const assert = require("node:assert");
const fs = require("node:fs");
const path = require("node:path");

const html = fs.readFileSync(path.join(__dirname, "../web/index.html"), "utf8");
const script = html.match(/<script>([\s\S]*?)<\/script>/)[1];

test("weekly report list groups by department then person", () => {
  const loader = script.match(/async function loadWeeklyReports\(\) \{[\s\S]*?\n\}/m);
  assert.ok(loader, "缺少周报列表渲染函数");
  const source = loader[0];
  assert.match(source, /owner_departments/, "应按后端返回的所属部门分组");
  assert.match(source, /未分配部门/, "无部门资料的周报应归入「未分配部门」");
  assert.match(source, /wr-dept/, "应有部门分组容器");
  assert.match(source, /wr-person/, "应有个人分组容器");
  assert.match(source, /localeCompare/, "部门组应按名称排序");
});

test("global weekly schedule lives in the accounts tab", () => {
  const accounts = html.match(/<section class="tab account-admin-only" id="tab-accounts">[\s\S]*?<\/section>/);
  assert.ok(accounts, "缺少用户管理页");
  assert.match(accounts[0], /id="wr-global-settings"/, "统一周报设置应位于用户管理页");
  assert.match(accounts[0], /定时工作周报/, "卡片标题应为「定时工作周报」");
  const weekly = html.match(/<section class="tab session-only" id="tab-weekly">[\s\S]*?<\/section>/);
  assert.ok(weekly, "缺少工作周报页");
  assert.doesNotMatch(weekly[0], /wr-global-settings/, "工作周报页不应再包含统一设置");
});

test("entering the accounts tab refreshes weekly settings", () => {
  assert.match(script, /tab === "accounts"\) \{ loadUsers\(\); refreshWeeklyReport\(\); \}/,
    "进入用户管理页应加载统一周报设置");
});
