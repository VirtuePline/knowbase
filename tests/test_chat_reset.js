// Exercise the shipped chat session and mode handlers with controlled network responses.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const html = fs.readFileSync(path.join(__dirname, "../web/index.html"), "utf8");
const script = html.match(/<script>([\s\S]*?)<\/script>/)[1];

function shippedSection(from, to) {
  const start = script.indexOf(from), end = script.indexOf(to, start);
  assert.ok(start >= 0 && end > start, `Missing shipped section: ${from}`);
  return script.slice(start, end);
}
function shippedFunction(name) {
  const match = script.match(new RegExp(`^(?:async )?function ${name}\\([^]*?^}`, "m"));
  assert.ok(match, `Missing shipped function: ${name}`);
  return match[0];
}
function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}
function reply(data) { return { ok: true, status: 200, json: async () => data }; }
function answer(text = "本次回答", id = "current-conversation", sources = []) {
  return { status: "ok", answer: text, conversation_id: id, sources };
}
function attributes(raw) {
  const result = Object.fromEntries([...raw.matchAll(/([\w-]+)="([^"]*)"/g)]
    .map((match) => [match[1], match[2]]));
  for (const name of ["disabled", "hidden"]) {
    if (new RegExp(`(?:^|\\s)${name}(?:\\s|=|$)`).test(raw)) result[name] = "";
  }
  return result;
}
function buttonLocation(id) {
  const stack = [];
  const empty = new Set(["meta", "link", "br", "hr", "input", "img"]);
  for (const match of html.matchAll(/<\/?([a-z][a-z0-9-]*)\b([^>]*)>/gi)) {
    const tag = match[1].toLowerCase();
    if (match[0].startsWith("</")) { stack.splice(stack.map((item) => item.tag).lastIndexOf(tag)); continue; }
    const attrs = attributes(match[2]);
    if (attrs.id === id) return { tag, attrs, parents: [...stack] };
    if (!empty.has(tag) && !match[0].endsWith("/>")) stack.push({ tag, attrs });
  }
  throw new Error(`Missing shipped button: ${id}`);
}

function element(tag = "div", attrs = {}) {
  const node = {
    tagName: tag.toUpperCase(), attributes: { ...attrs }, dataset: {},
    children: [], parentElement: null, className: attrs.class || "", textContent: "",
    value: "", disabled: "disabled" in attrs, hidden: "hidden" in attrs,
    scrollTop: 0, focused: 0, handlers: new Map(), connected: false,
    style: { display: "", removeProperty(name) { this[name] = ""; } },
    setAttribute(name, value) {
      this.attributes[name] = String(value);
      if (name.startsWith("data-")) this.dataset[name.slice(5).replace(/-([a-z])/g, (_, letter) => letter.toUpperCase())] = String(value);
    },
    getAttribute(name) { return this.attributes[name] ?? null; },
    removeAttribute(name) { delete this.attributes[name]; },
    addEventListener(name, handler) {
      if (!this.handlers.has(name)) this.handlers.set(name, []);
      this.handlers.get(name).push(handler);
    },
    async dispatch(name, event = {}) {
      await Promise.all((this.handlers.get(name) || []).map((handler) => handler({ target: this, ...event })));
    },
    appendChild(child) { child.parentElement = this; this.children.push(child); return child; },
    remove() {
      if (this.parentElement) this.parentElement.children = this.parentElement.children.filter((child) => child !== this);
      this.parentElement = null;
    },
    contains(other) { return this === other || this.children.some((child) => child.contains(other)); },
    matches(selector) {
      if (selector.startsWith(".")) return this.className.split(/\s+/).includes(selector.slice(1));
      const data = selector.match(/^\[data-([\w-]+)\]$/);
      return !!data && this.getAttribute(`data-${data[1]}`) !== null;
    },
    closest(selector) {
      for (let current = this; current; current = current.parentElement) if (current.matches(selector)) return current;
      return null;
    },
    querySelectorAll(selector) {
      return this.children.flatMap((child) => [...(child.matches(selector) ? [child] : []), ...child.querySelectorAll(selector)]);
    },
    querySelector(selector) { return this.querySelectorAll(selector)[0] || null; },
    focus() { this.focused += 1; },
  };
  node.classList = {
    contains: (name) => node.className.split(/\s+/).includes(name),
    add(...names) { node.className = [...new Set([...node.className.split(/\s+/), ...names])].filter(Boolean).join(" "); },
    remove(...names) { node.className = node.className.split(/\s+/).filter((name) => !names.includes(name)).join(" "); },
    toggle(name, force) { const on = force ?? !this.contains(name); if (on) this.add(name); else this.remove(name); return on; },
  };
  Object.defineProperties(node, {
    innerHTML: {
      get() { return this._html || ""; },
      set(value) { this._html = String(value); this.children.forEach((child) => { child.parentElement = null; }); this.children = []; },
    },
    isConnected: { get() { return this.connected || !!this.parentElement?.isConnected; } },
    scrollHeight: { get() { return this.children.length * 100; } },
  });
  return node;
}

function page() {
  const nodes = new Map(), requests = [], saved = [], notices = [], revoked = [];
  const historyKey = "kb_chat_history:employee:permissions";
  const existingHistory = JSON.stringify([{ q: "之前的问题", answer: "历史回答" }]);
  const storage = new Map([[historyKey, existingHistory]]);
  const find = (selector) => {
    if (!nodes.has(selector)) {
      let attrs = {};
      if (selector.startsWith("#")) {
        const match = html.match(new RegExp(`<([a-z]+)\\b([^>]*\\bid="${selector.slice(1)}"[^>]*)>`));
        assert.ok(match, `Missing shipped element: ${selector}`);
        attrs = attributes(match[2]);
      }
      const node = element("div", attrs);
      node.connected = true;
      nodes.set(selector, node);
    }
    return nodes.get(selector);
  };
  const hero = find(".chat-hero");
  hero.innerHTML = "<h1>欢迎介绍</h1><div>知识库概览</div>";
  find("#overview-documents").textContent = "24";
  find("#overview-chunks").textContent = "180";
  const hints = find("#hints");
  hints.innerHTML = "从一个问题开始";
  for (const text of ["快捷问题甲", "快捷问题乙"]) {
    const node = element("button", { class: "hint" }); node.textContent = text; hints.appendChild(node);
  }
  const documentEvents = new Map();
  const context = vm.createContext({
    $: find, AbortController, Date, Set, Map,
    document: {
      querySelector: find,
      querySelectorAll: (selector) => selector === ".hint" ? hints.querySelectorAll(selector) : [],
      addEventListener(name, handler) { documentEvents.set(name, handler); },
      createElement: (tag) => element(tag), body: element("body"),
    },
    window: { scrollTo() {} },
    performance: { now: () => 1000 },
    URL: class extends URL {
      static createObjectURL() { return "blob:test-image"; }
      static revokeObjectURL(value) { revoked.push(value); }
    },
    FormData: class { constructor() { this.values = []; } append(name, value) { this.values.push([name, value]); } },
    localStorage: {
      getItem: (key) => storage.get(key), setItem: (key, value) => storage.set(key, value),
      removeItem: (key) => storage.delete(key),
    },
    enterpriseHistoryKey: () => historyKey,
    HISTORY_KEY: "kb_chat_history",
    renderMarkdown: (value) => value,
    toast: (value) => notices.push(value),
    saveHistoryItem(item) {
      saved.push(item);
      storage.set(historyKey, JSON.stringify([item, ...JSON.parse(storage.get(historyKey) || "[]")]));
    },
    fetch(url, options = {}) {
      if (url === "/api/questions/top") return Promise.resolve(reply({ questions: [] }));
      if (url === "/api/health") return Promise.resolve(reply({ stats: {} }));
      const pending = deferred();
      requests.push({ url, options, ...pending });
      return pending.promise;
    },
  });
  vm.runInContext([
    shippedFunction("escapeHtml"), shippedFunction("escapeAttr"),
    shippedSection("/* ---------- 问答 ---------- */", "/* ---------- 文档管理 ---------- */"),
    shippedSection("/* ---------- 提问开关：", "/* ---------- 历史记录："),
  ].join("\n"), context);
  // A fully initialized page has enabled both controls after reading the server mode.
  find("#new-chat").disabled = find("#send").disabled = false;
  return {
    find, requests, saved, notices, revoked, storage, historyKey, existingHistory, hero, hints,
    run: (code) => vm.runInContext(code, context),
    click: (selector) => find(selector).dispatch("click"),
    state: () => JSON.parse(vm.runInContext("JSON.stringify({history, historyConversationId, lastAnswered, pendingImage: !!pendingImage})", context)),
    answerRequests: () => requests.filter((request) => request.url === "/api/chat"),
  };
}

test("new conversation is an independent action and stays available in every answering mode", () => {
  const location = buttonLocation("new-chat");
  assert.equal(location.tag, "button");
  assert.equal(location.attrs.type, "button");
  assert.equal("hidden" in location.attrs, false);
  assert.ok(!location.parents.some((parent) => (parent.attrs.class || "").split(/\s+/).includes("chat-toggles")));
  const ui = page();
  for (const fast of [false, true]) for (const polish of [false, true]) {
    ui.run(`fmState.enabled=${fast}; llmState.enabled=${polish}; renderToggles()`);
    assert.equal(ui.find("#new-chat").hidden, false, `fast=${fast}, polish=${polish}`);
    assert.equal(ui.find("#new-chat").disabled, false);
  }
});

test("starting a conversation restores welcome content, preserves saved history, and clears only the current draft", async () => {
  const ui = page();
  const welcome = ui.hero.innerHTML;
  const shortcuts = ui.hints.children.map((node) => node.textContent);
  ui.hero.style.display = ui.hints.style.display = "none";
  ui.run('history.push({role:"user",content:"旧上下文"}); historyConversationId="old"; lastAnswered=true; questionBox.value="旧草稿"; setAttachImage({name:"截图.png",type:"image/png",size:5})');
  ui.find("#chat-log").innerHTML = "旧对话";
  ui.find("#chat-log").setAttribute("aria-busy", "true");
  await ui.click("#new-chat");
  assert.deepEqual(ui.state(), { history: [], historyConversationId: "", lastAnswered: false, pendingImage: false });
  assert.equal(ui.find("#chat-log").innerHTML, "");
  assert.equal(ui.find("#chat-log").getAttribute("aria-busy"), "false");
  assert.equal(ui.find("#send").disabled, false);
  assert.equal(ui.find("#question").value, "");
  assert.ok(ui.find("#question").focused > 0);
  assert.equal(ui.find("#attach-chips").hidden, true);
  assert.deepEqual(ui.revoked, ["blob:test-image"]);
  assert.notEqual(ui.hero.style.display, "none");
  assert.notEqual(ui.hints.style.display, "none");
  assert.notEqual(ui.hints.style.display, "block", "Shortcut cards retain their grid layout");
  assert.equal(ui.hero.innerHTML, welcome);
  assert.deepEqual(ui.hints.children.map((node) => node.textContent), shortcuts);
  assert.equal(ui.find("#overview-documents").textContent, "24");
  assert.equal(ui.find("#overview-chunks").textContent, "180");
  assert.equal(ui.storage.get(ui.historyKey), ui.existingHistory);
});

for (const outcome of ["success", "failure"]) {
  test(`an old request ${outcome} cannot change a fresh conversation or release its pending send`, async () => {
    const ui = page();
    ui.run("llmState.enabled=true");
    const older = ui.run('ask("旧问题")');
    const first = ui.answerRequests()[0];
    await ui.click("#new-chat");
    assert.equal(first.options.signal.aborted, true);
    const current = ui.run('ask("新问题")');
    const currentRequest = ui.answerRequests()[1];
    const pending = ui.find("#chat-log").children.at(-1).children[0];
    const initialPending = pending.innerHTML;
    if (outcome === "success") first.resolve(reply(answer("过期回答", "old-conversation")));
    else first.reject(new Error("old-network-failure"));
    await older;
    assert.deepEqual(ui.state().history, []);
    assert.equal(ui.state().historyConversationId, "");
    assert.equal(ui.state().lastAnswered, false);
    assert.equal(ui.saved.length, 0);
    assert.equal(ui.find("#send").disabled, true);
    assert.equal(ui.find("#chat-log").getAttribute("aria-busy"), "true");
    assert.equal(ui.find("#chat-log").children.length, 2);
    assert.equal(pending.innerHTML, initialPending);
    currentRequest.resolve(reply(answer("当前回答", "new-conversation")));
    await current;
    assert.equal(ui.state().historyConversationId, "new-conversation");
    assert.equal(ui.state().lastAnswered, true);
    assert.deepEqual(ui.state().history.map((item) => item.content), ["新问题", "当前回答"]);
    assert.deepEqual(ui.saved.map((item) => item.q), ["新问题"]);
    assert.equal(ui.find("#send").disabled, false);
  });
}

test("starting over while an answer body is parsing also discards the stale conversation token", async () => {
  const ui = page();
  const body = deferred();
  const old = ui.run('ask("旧问题")');
  ui.answerRequests()[0].resolve({ ok: true, json: () => body.promise });
  await new Promise(setImmediate);
  await ui.click("#new-chat");
  body.resolve(answer("过期解析结果", "stale-token"));
  await old;
  assert.equal(ui.state().historyConversationId, "");
  assert.equal(ui.find("#chat-log").children.length, 0);
  assert.equal(ui.saved.length, 0);
  assert.notEqual(ui.hero.style.display, "none");
});

test("starting over during image recognition cannot launch a stale text question", async () => {
  const ui = page();
  const old = ui.run('setAttachImage({name:"截图.png",type:"image/png",size:5}); ask("")');
  const imageRequest = ui.requests.find((request) => request.url === "/api/chat/image");
  assert.ok(imageRequest);
  await ui.click("#new-chat");
  assert.equal(imageRequest.options.signal.aborted, true);
  imageRequest.resolve(reply({ question: "过期图片识别结果" }));
  await old;
  assert.equal(ui.answerRequests().length, 0);
  assert.equal(ui.state().pendingImage, false);
  assert.equal(ui.find("#chat-log").children.length, 0);
  assert.equal(ui.find("#send").disabled, false);
  assert.equal(ui.saved.length, 0);
});

test("automatic fast-mode reset retains the new draft and image until recognition completes", async () => {
  const ui = page();
  ui.run("fmState.enabled=true");
  const first = ui.run('ask("第一题")');
  ui.answerRequests()[0].resolve(reply(answer("第一题回答")));
  await first;
  const next = ui.run('questionBox.value="新草稿"; setAttachImage({name:"新截图.png",type:"image/png",size:7}); ask("新草稿")');
  const imageRequest = ui.requests.find((request) => request.url === "/api/chat/image");
  assert.ok(imageRequest, "The automatic reset must preserve the newly attached screenshot");
  assert.equal(imageRequest.options.body.values[0][1].name, "新截图.png");
  imageRequest.resolve(reply({ question: "新图片描述" }));
  await new Promise(setImmediate);
  const request = ui.answerRequests()[1];
  const payload = JSON.parse(request.options.body);
  assert.match(payload.question, /新草稿/);
  assert.match(payload.question, /新图片描述/);
  assert.deepEqual(payload.history, []);
  assert.equal(payload.conversation_id, "");
  request.resolve(reply(answer("第二题回答", "second")));
  await next;
  assert.equal(ui.find("#chat-log").children.length, 2);
  assert.equal(ui.saved.length, 2, "Starting a new session does not erase stored answers");
  assert.equal(ui.state().pendingImage, false);
});

for (const outcome of ["success", "failure"]) {
  test(`a re-answer ${outcome} returning after reset cannot repopulate the new session`, async () => {
    const ui = page();
    const initial = ui.run('ask("原问题")');
    ui.answerRequests()[0].resolve(reply(answer("原回答", "original", [{ doc_id: "source", doc_name: "资料", score: 1 }])));
    await initial;
    const bubble = ui.find("#chat-log").children.at(-1).children[0];
    const button = element("button", { "data-reask": "" });
    button.textContent = "重新回答";
    bubble.appendChild(button);
    const older = ui.find("#chat-log").dispatch("click", { target: button });
    const request = ui.answerRequests()[1];
    assert.ok(request);
    await ui.click("#new-chat");
    assert.equal(request.options.signal.aborted, true);
    if (outcome === "success") request.resolve(reply(answer("旧重答结果", "old-reask-token")));
    else request.reject(new Error("stale-reask-failure"));
    await older;
    assert.equal(ui.state().historyConversationId, "");
    assert.deepEqual(ui.state().history, []);
    assert.equal(ui.state().lastAnswered, false);
    assert.equal(ui.find("#chat-log").children.length, 0);
    assert.equal(ui.find("#send").disabled, false);
    assert.equal(ui.saved.length, 1);
    assert.ok(!ui.notices.some((message) => String(message).includes("stale-reask-failure")));
  });
}
