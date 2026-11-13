/* 时间线日期控件：点击任意位置弹出系统日历（showPicker），旧浏览器回退聚焦。 */
const test = require("node:test");
const assert = require("node:assert");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const html = fs.readFileSync(path.join(__dirname, "../web/index.html"), "utf8");
const script = html.match(/<script>([\s\S]*?)<\/script>/)[1];

function dateWrapBinding() {
  // 按「花括号平衡」从 forEach 起点截取完整语句，避免正则跨块误截
  const marker = 'document.querySelectorAll(".tl-date-wrap").forEach((wrap) => {';
  let start = -1;
  let snippet = "";
  while ((start = script.indexOf(marker, start + 1)) !== -1) {
    let depth = 1;                     // marker 末尾处 forEach( 的括号仍开着
    let end = start + marker.length;
    for (; end < script.length; end += 1) {
      const ch = script[end];
      if (ch === "(") depth += 1;
      else if (ch === ")") {
        depth -= 1;
        if (depth === 0) break;
      }
    }
    const candidate = script.slice(start, end + 1);
    if (candidate.includes("addEventListener")) snippet = candidate;   // 点击绑定块（跳过刷新函数里的）
  }
  assert.ok(snippet, "缺少日期控件点击绑定代码");
  return snippet;
}

test("clicking the date control opens the native calendar picker", () => {
  const calls = [];
  const input = { type: "date", value: "", showPicker: () => calls.push("picker"), focus: () => calls.push("focus") };
  const wrap = {
    handlers: {},
    addEventListener(type, handler) { this.handlers[type] = handler; },
    querySelector: (selector) => (selector.includes("input") ? input : { textContent: "年 / 月 / 日" }),
  };
  vm.runInContext(dateWrapBinding(), vm.createContext({
    document: { querySelectorAll: (sel) => (sel === ".tl-date-wrap" ? [wrap] : []) },
  }));
  assert.equal(typeof wrap.handlers.click, "function", "应绑定 click 事件");
  wrap.handlers.click();
  assert.deepEqual(calls, ["picker"], "点击应调用 showPicker 弹出日历");
});

test("browsers without showPicker fall back to focusing the input", () => {
  const calls = [];
  const input = { type: "date", focus: () => calls.push("focus-fallback") };   // 无 showPicker 方法
  const wrap = {
    handlers: {},
    addEventListener(type, handler) { this.handlers[type] = handler; },
    querySelector: () => input,
  };
  vm.runInContext(dateWrapBinding(), vm.createContext({
    document: { querySelectorAll: () => [wrap] },
  }));
  wrap.handlers.click();
  assert.deepEqual(calls, ["focus-fallback"], "无 showPicker 时应聚焦输入框");
});
