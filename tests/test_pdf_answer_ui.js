const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const html = fs.readFileSync(path.join(__dirname, "../web/index.html"), "utf8");
const script = html.match(/<script>([\s\S]*?)<\/script>/)[1];

function shippedSection(from, to) {
  const start = script.indexOf(from);
  const end = script.indexOf(to, start);
  assert.ok(start >= 0 && end > start, `Missing shipped section: ${from}`);
  return script.slice(start, end);
}

function pdfSource(id, pages, firstPage) {
  return { doc_id: id, doc_name: `${id}.pdf`, pdf_available: true, pdf_pages: pages, pdf_page: firstPage };
}

function page(sources, answer = "提取的正文") {
  const historyBody = { hidden: true };
  const historyButton = {
    dataset: { hi: "0" },
    parentElement: { querySelector: () => historyBody },
    setAttribute() {},
  };
  const documentEvents = new Map();
  const historyEvents = new Map();
  const opened = [];
  const item = JSON.stringify([{ answer, sources }]);
  const context = vm.createContext({
    $: (selector) => selector === "#history-list" ? {
      addEventListener: (event, handler) => historyEvents.set(event, handler),
    } : null,
    document: { addEventListener: (event, handler) => documentEvents.set(event, handler) },
    enterpriseHistoryKey: () => "kb_chat_history:test-user:test-permissions",
    localStorage: { getItem: () => item },
    openPreview: (...args) => opened.push(args),
    performance: { now: () => 1000 },
  });
  vm.runInContext([
    shippedSection("function escapeHtml", "/* ---------- 标签页 ---------- */"),
    shippedSection("function safeHttpUrl", "// 「答案不对」"),
    shippedSection("/* ---------- 历史记录：", "// 进入「知识分类与模板」"),
  ].join("\n"), context);
  const bubble = { dataset: {} };
  context.renderChatAnswer(bubble, { answer, sources }, "问题", 500);
  return {
    answer: bubble.innerHTML,
    expandHistory() {
      historyEvents.get("click")({ target: { closest: () => historyButton } });
      return historyBody.innerHTML;
    },
    previewReference(id, pdfPage) {
      documentEvents.get("click")({ target: { closest: () => ({ dataset: { srcPv: id, srcPage: pdfPage } }) } });
      return opened[0];
    },
  };
}

test("chat and history show each exact original page once and preserve links for other PDFs", () => {
  const manual = { ...pdfSource("manual", [56, 55, 55, 0, "54"], 56), doc_name: '手册 <原版> "v1".pdf' };
  const ui = page([
    manual,
    pdfSource("manual", [55], 55),
    pdfSource("unknown", []),
    pdfSource("range", undefined, 12),
    { ...pdfSource("unavailable", [1], 1), pdf_available: false },
    { ...pdfSource("web", [1], 1), scoring_method: "web" },
    { ...pdfSource("search-context", [55], 55), scoring_method: "search_context" },
  ]);
  for (const markup of [ui.answer, ui.expandHistory()]) {
    assert.deepEqual([...markup.matchAll(/<img src="([^"]+)"/g)].map((match) => match[1]), [
      "/api/documents/manual/pages/56.png", "/api/documents/manual/pages/55.png",
    ]);
    assert.match(markup, /alt="手册 &lt;原版&gt; &quot;v1&quot;.pdf第 55 页原图"/);
    const beforeText = markup.slice(0, markup.indexOf("提取的正文"));
    assert.match(beforeText, /\/api\/documents\/manual\/pdf#page=55/);
    assert.match(beforeText, /\/api\/documents\/unknown\/pdf"/);
    assert.match(beforeText, /\/api\/documents\/range\/pdf#page=12/);
    assert.doesNotMatch(beforeText, /\/api\/documents\/(unavailable|web|search-context)\//);
  }
});

test("PDF references open the cited original page without inserting unrelated images", () => {
  const ui = page([
    { doc_id: "record", doc_name: "记录", references: [pdfSource("reference", [4, 5], 4)] },
    { ...pdfSource("legacy", [2], 2), scoring_method: "reference" },
  ]);
  for (const markup of [ui.answer, ui.expandHistory()]) {
    assert.doesNotMatch(markup, /<img /);
    assert.match(markup, /data-src-pv="reference" data-src-page="4"/);
    assert.match(markup, /\/api\/documents\/reference\/pdf#page=4/);
    assert.match(markup, /\/api\/documents\/legacy\/pdf#page=2/);
  }
  assert.deepEqual(ui.previewReference("reference", "4"), ["reference", 4]);
});
