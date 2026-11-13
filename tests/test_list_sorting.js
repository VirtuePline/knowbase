/* 全站列表排序：默认时间倒序，可切换时间正序 / 名称 A-Z。 */
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const html = fs.readFileSync(path.join(__dirname, "../web/index.html"), "utf8");
const script = html.match(/<script>([\s\S]*?)<\/script>/)[1];

function shippedFunction(source, name) {
  const match = source.match(new RegExp(`^(?:async )?function ${name}\\([^]*?^}`, "m"));
  assert.ok(match, `Missing shipped function: ${name}`);
  return match[0];
}

test("every list UI exposes a sort selector defaulting to time-desc", () => {
  for (const id of ["doc-sort", "rec-sort", "tl-sort", "wr-sort", "users-sort", "edits-sort", "audit-sort"]) {
    const match = html.match(new RegExp(`id="${id}"[^>]*>([\\s\\S]*?)</select>`));
    assert.ok(match, `缺少排序下拉：${id}`);
    assert.match(match[1], /value="time-desc" selected/, `${id} 默认应为时间倒序`);
  }
});

test("applyListSort orders by time desc, time asc, or name asc", () => {
  const context = vm.createContext({});
  vm.runInContext(shippedFunction(script, "applyListSort"), context);
  const run = (list, mode, timeKey, nameKey) => JSON.parse(vm.runInContext(
    `JSON.stringify(applyListSort(${JSON.stringify(list)}, ${JSON.stringify(mode)}, ${JSON.stringify(timeKey || "uploaded_at")}, ${JSON.stringify(nameKey || "name")}))`,
    context)).map((d) => d.name);
  const docs = [
    { name: "丙", uploaded_at: "2026-01-02" },
    { name: "甲", uploaded_at: "2026-03-01" },
    { name: "乙", uploaded_at: "" },
  ];
  assert.deepEqual(run(docs, "time-desc"), ["甲", "丙", "乙"]);
  assert.deepEqual(run(docs, "time-asc"), ["乙", "丙", "甲"]);
  assert.deepEqual(run(docs, "name-asc", "", "name"), ["丙", "甲", "乙"]);   // localeCompare zh 拼音序：丙(bǐng) 甲(jiǎ) 乙(yǐ)
  assert.deepEqual(docs.map((d) => d.name), ["丙", "甲", "乙"], "原数组不被修改");
});

test("timeline groups and audit logs honor the selected order", () => {
  const render = shippedFunction(script, "renderTimeline");
  assert.match(render, /groupEntries\.reverse\(\)/, "时间线正序应反转分组顺序");
  const loader = script.match(/async function loadAuditLogs\(\) \{[\s\S]*?\n\}/m);
  assert.match(loader[0], /order=\$\{auditSortMode/, "审计日志应把排序传给后端");
});
