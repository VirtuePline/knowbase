const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const html = fs.readFileSync(path.join(__dirname, "../web/index.html"), "utf8");
const script = html.match(/<script>([\s\S]*?)<\/script>/)[1];
function section(from, to) {
  const start = script.indexOf(from);
  const end = script.indexOf(to, start);
  assert.ok(start >= 0 && end > start);
  return script.slice(start, end);
}

class ImageElement {
  constructor(src) { this.src = src; this.parentElement = { hidden: false }; }
  matches(selector) { return selector === "[data-pdf-preview-image]"; }
}
class Element {
  constructor() {
    this.events = new Map(); this._html = ""; this.hidden = false; this.textContent = "";
    const classes = new Set();
    this.classList = { add: (name) => classes.add(name), remove: (name) => classes.delete(name), contains: (name) => classes.has(name) };
  }
  addEventListener(type, handler) { this.events.set(type, handler); }
  replaceChildren() { this.innerHTML = ""; }
  set innerHTML(value) {
    this._html = value;
    this.viewer = null;
    const source = value.match(/<img data-pdf-preview-image src="([^"]+)"/);
    this.image = source ? new ImageElement(source[1]) : null;
    this.input = source ? { value: value.match(/value="([0-9]+)" data-pdf-page-input/)[1] } : null;
    this.status = source ? { hidden: false, innerHTML: "正在加载原图" } : null;
  }
  get innerHTML() { return this._html; }
  querySelector(selector) {
    if (selector === "[data-pdf-viewer]") return this.viewer;
    const owner = this.viewer || this;
    return { "[data-pdf-page-input]": owner.input, "[data-pdf-status]": owner.status }[selector] || null;
  }
  contains(image) { return image === (this.viewer || this).image; }
}

function createPage() {
  const elements = new Map();
  for (const id of ["preview-mask", "pv-body", "pv-download", "pv-close", "pv-title", "pv-sub"]) elements.set(`#${id}`, new Element());
  const body = elements.get("#pv-body");
  // The body delegates page controls and image selectors to the current viewer.
  Object.defineProperty(body, "innerHTML", {
    get() { return this._html; },
    set(value) {
      this._html = value;
      if (value.includes("data-pdf-viewer")) {
        this.viewer = new Element();
        this.viewer.innerHTML = value.slice(value.indexOf(">", value.indexOf("<section")) + 1, value.indexOf("</section>"));
      } else this.viewer = null;
    },
  });
  const responses = new Map();
  const requests = [];
  const context = vm.createContext({
    $: (selector) => elements.get(selector),
    document: { addEventListener() {} },
    HTMLImageElement: ImageElement,
    fetch: async (url) => { requests.push(url); return await responses.get(url); },
    enterpriseDepartmentName: () => "部门", catName: () => "分类", toast() {},
    renderMarkdown: (text) => `<p>${text}</p>`,
    escapeHtml: (text) => String(text).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;"),
    escapeAttr: (text) => String(text).replace(/"/g, "&quot;"),
  });
  vm.runInContext(section("function isPdfPage", "function renderPdfPages") +
    section("/* ---------- 文档预览 ---------- */", "/* ---------- Markdown 在线编辑 ---------- */"), context);
  return {
    body, elements, requests, context,
    respond(id, { ext = ".pdf", pages = 135, sections = [], name = id } = {}) {
      responses.set(`/api/documents/${id}/preview`, { ok: true, json: async () => ({
        doc: { id, name, ext, has_source: true, chunks: 0, doc_type: "doc", attachments: [] },
        pdf_page_count: pages, sections,
      }) });
    },
    defer(id) {
      let release;
      responses.set(`/api/documents/${id}/preview`, new Promise((resolve) => { release = resolve; }));
      return () => release({ ok: true, json: async () => ({
        doc: { id, name: id, ext: ".pdf", has_source: true, chunks: 0, doc_type: "doc", attachments: [] },
        pdf_page_count: 100, sections: [],
      }) });
    },
    open: (id, page) => context.openPreview(id, page),
    change: (page) => context.changePdfPreviewPage(page),
    close: () => context.closePreview(),
    click(selector, dataset = {}) {
      body.events.get("click")({ target: { closest: (candidate) => candidate === selector ? { dataset } : null } });
    },
    imageEvent(type, image = body.viewer.image) { body.events.get(type)({ type, target: image }); },
  };
}

test("PDF preview renders one original page even without extracted text and supports navigation", async () => {
  const ui = createPage();
  ui.respond("manual", { pages: 3 });
  await ui.open("manual");
  assert.equal(ui.body.viewer.image.src, "/api/documents/manual/pages/1.png");
  assert.match(ui.body.innerHTML, /暂无可预览的提取文本/);
  assert.match(ui.body.viewer.innerHTML, /data-pdf-step="-1" disabled/);
  assert.match(ui.body.viewer.innerHTML, /放大原图/);
  ui.click("[data-pdf-step]", { pdfStep: "1" });
  assert.equal(ui.body.viewer.image.src, "/api/documents/manual/pages/2.png");
  ui.body.viewer.input.value = "3";
  ui.click("[data-pdf-jump]");
  assert.equal(ui.body.viewer.image.src, "/api/documents/manual/pages/3.png");
  assert.match(ui.body.viewer.innerHTML, /data-pdf-step="1" disabled/);
  assert.match(ui.body.viewer.innerHTML, /\/pdf#page=3/);
  for (const page of [0, 4, 1.5, NaN]) assert.equal(ui.change(page), false);
  assert.equal(ui.body.viewer.image.src, "/api/documents/manual/pages/3.png");
});

test("PDF citations start at their cited page and failed images can be retried", async () => {
  const ui = createPage();
  ui.respond("manual");
  await ui.open("manual", 55);
  assert.equal(ui.body.viewer.image.src, "/api/documents/manual/pages/55.png");
  const failedImage = ui.body.viewer.image;
  ui.imageEvent("error");
  assert.equal(failedImage.parentElement.hidden, true);
  assert.match(ui.body.viewer.status.innerHTML, /data-pdf-retry/);
  assert.match(ui.body.viewer.innerHTML, /\/pdf#page=55/);
  ui.click("[data-pdf-retry]");
  assert.notEqual(ui.body.viewer.image, failedImage);
  ui.imageEvent("error", failedImage);
  assert.doesNotMatch(ui.body.viewer.status.innerHTML, /data-pdf-retry/);
  ui.imageEvent("load");
  assert.equal(ui.body.viewer.status.hidden, true);
  assert.equal(ui.body.viewer.image.parentElement.hidden, false);
});

test("closing or switching previews ignores late responses and stops page changes", async () => {
  const ui = createPage();
  const finishOld = ui.defer("old");
  const old = ui.open("old", 55);
  ui.respond("new");
  await ui.open("new", 2);
  finishOld(); await old;
  assert.equal(ui.elements.get("#pv-title").textContent, "new");
  assert.equal(ui.body.viewer.image.src, "/api/documents/new/pages/2.png");
  const finishClosed = ui.defer("closed");
  const closed = ui.open("closed");
  ui.close(); finishClosed(); await closed;
  assert.equal(ui.body.innerHTML, "");
  assert.equal(ui.elements.get("#preview-mask").classList.contains("show"), false);
  assert.equal(ui.change(1), false);
});

test("non-PDF preview keeps its extracted content without a PDF viewer", async () => {
  const ui = createPage();
  ui.respond("notes", { ext: ".md", sections: [{ kind: "text", text: "原有正文" }] });
  await ui.open("notes");
  assert.equal(ui.body.viewer, null);
  assert.match(ui.body.innerHTML, /原有正文/);
  assert.doesNotMatch(ui.body.innerHTML, /data-pdf-viewer/);
});
