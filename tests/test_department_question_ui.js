// Exercise the shipped department/public FAQ controls and chat scope snapshots.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");
const html = fs.readFileSync(path.join(__dirname, "../web/index.html"), "utf8");
const script = html.match(/<script>([\s\S]*?)<\/script>/)[1];

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
function reply(data, ok = true) { return { ok, status: ok ? 200 : 403, json: async () => data }; }
function element() {
  const node = { children: [], parentElement: null, value: "", hidden: false, disabled: false,
    className: "", textContent: "", dataset: {}, style: { display: "" }, attrs: {}, handlers: {},
    scrollTop: 0, scrollHeight: 100, focused: 0,
    addEventListener(name, handler) { this.handlers[name] = handler; },
    setAttribute(name, value) { this.attrs[name] = String(value); },
    appendChild(child) { child.parentElement = this; this.children.push(child); },
    remove() { this.parentElement.children = this.parentElement.children.filter((child) => child !== this); },
    querySelectorAll(selector) {
      return this.children.flatMap((child) => [...(selector === ".hint" && child.className === "hint" ? [child] : []),
        ...child.querySelectorAll(selector)]);
    },
    focus() { this.focused += 1; },
  };
  Object.defineProperty(node, "innerHTML", { get() { return this._html || ""; },
    set(value) { this._html = String(value); this.children = []; } });
  return node;
}
function page(portal = "admin") {
  const nodes = new Map(), requests = [], renders = [], saved = [];
  const node = (selector) => {
    if (!nodes.has(selector)) nodes.set(selector, element());
    return nodes.get(selector);
  };
  const context = vm.createContext({
    portal, $: node, AbortController, Set, Map, Date,
    document: { querySelector: node, createElement: () => element() },
    window: { scrollTo() {} }, performance: { now: () => 0 },
    FormData: class { constructor() { this.items = []; } append(key, value) { this.items.push([key, value]); } },
    fetch(url, options = {}) { const pending = deferred(); requests.push({ url, options, ...pending }); return pending.promise; },
    renderChatAnswer(bubble, data) { renders.push(data); bubble.textContent = data.answer; },
    saveHistoryItem(item) { saved.push(item); }, toast() {},
    addMessage(role, html) {
      const wrapper = element(), bubble = element(); bubble.textContent = html;
      wrapper.appendChild(bubble); node("#chat-log").appendChild(wrapper); return bubble;
    },
  });
  vm.runInContext(`
    let questionDepartmentId = "", questionScope = "none", questionDepartments = [], questionOptionsReady = false;
    let topQuestionsRequestId = 0, chatSessionVersion = 0, historyConversationId = "", lastAnswered = false;
    let pendingImage = null, resetCount = 0;
    const history = [], chatRequests = new Set();
    const fmState = {enabled:false}, llmState = {enabled:false}, ctxState = {enabled:true}, wsState = {enabled:false};
    const log = $("#chat-log"), questionBox = $("#question"), sendBtn = $("#send"), newChatBtn = $("#new-chat");
    const hintsBox = $("#hints"), questionDepartmentSelect = $("#question-department");
    const questionDepartmentControl = $("#question-department-control"), questionHintsTitle = $("#question-hints-title");
    const questionHintsScope = $("#question-hints-scope"), questionHintsState = $("#question-hints-state");
    function clearAttachImage() { pendingImage = null; }
    ${["escapeHtml", "currentQuestionDepartment", "clearQuestionHints", "renderQuestionDepartment",
      "loadTopQuestions", "changeQuestionDepartment", "startNewChat", "ask", "reaskAnswer"].map(shippedFunction).join("\n")}
    const originalReset = startNewChat;
    startNewChat = (...args) => { resetCount += 1; originalReset(...args); };
  `, context);
  return { nodes, node, requests, renders, saved,
    run: (code) => vm.runInContext(code, context),
    state: () => JSON.parse(vm.runInContext('JSON.stringify({questionDepartmentId,questionScope,questionOptionsReady,resetCount,history,historyConversationId})', context)),
    hintTexts: () => node("#hints").querySelectorAll(".hint").map((hint) => hint.textContent),
    async load(payload) { const pending = vm.runInContext("loadTopQuestions()", context); requests.at(-1).resolve(reply(payload)); await pending; },
  };
}
const departments = [{ id: "hr", name: "人事部门" }, { id: "ops", name: "信息部门" }];
function department(id = "hr", questions = [{ question: "如何请假", count: 3 }], options = departments) {
  return { scope: "department", department_id: id, departments: options, questions };
}
function answer(text = "本人可见回答") { return {status: "ok", answer: text, conversation_id: "current", sources: []}; }

test("FAQ markup has no fixed technical defaults and labels the department selector", () => {
  const markup = html.slice(html.indexOf('id="hints"'), html.indexOf('id="chat-log"'));
  assert.doesNotMatch(markup, /网页打不开|音响没声音|华智剔废乱剔|箱签出不来/);
  assert.match(markup, /aria-label="常见问题所属部门"/);
  assert.match(markup, /id="question-department-control" hidden/);
  assert.match(markup, /id="question-hints-state" role="status"/);
});

test("department suggestions and directory names are rendered as text and use only endpoint options", async () => {
  const ui = page();
  await ui.load(department("hr", [{question:"<img src=x>请假",count:4}], [{id:"hr",name:"人事<部门>"}]));
  assert.equal(ui.requests[0].url, "/api/questions/top");
  assert.deepEqual(ui.hintTexts(), ["<img src=x>请假"]);
  assert.equal(ui.node("#question-hints-title").textContent, "人事<部门>常见问题");
  assert.equal(ui.node("#question-department").children[0].textContent, "人事<部门>");
  assert.equal(ui.node("#question-department-control").hidden, true);
  assert.equal(ui.node("#question-hints-state").hidden, true);
});

test("switching departments clears old suggestions, resets the chat, and ignores older responses", async () => {
  const ui = page(); await ui.load(department());
  ui.run('history.push({role:"assistant",content:"旧部门内容"}); historyConversationId="old";');
  ui.node("#question-department").value = "ops";
  const older = ui.run("changeQuestionDepartment()");
  assert.deepEqual(ui.hintTexts(), []);
  assert.match(ui.requests[1].url, /department_id=ops/);
  assert.equal(ui.state().historyConversationId, "");
  assert.equal(ui.node("#question-department-control").hidden, false);
  ui.node("#question-department").value = "hr";
  const newer = ui.run("changeQuestionDepartment()");
  ui.requests[2].resolve(reply(department("hr", [{question:"新的人事问题"}]))); await newer;
  ui.requests[1].resolve(reply(department("ops", [{question:"过期信息问题"}]))); await older;
  assert.deepEqual(ui.hintTexts(), ["新的人事问题"]);
  assert.equal(ui.state().questionDepartmentId, "hr");
  assert.equal(ui.state().resetCount, 2);
  assert.equal(ui.node("#question-department").value, "hr");
});

test("empty department and failed requests never reuse another department's questions", async () => {
  const ui = page(); await ui.load(department());
  ui.node("#question-department").value = "ops";
  const pending = ui.run("changeQuestionDepartment()");
  ui.requests.at(-1).resolve(reply({detail:"无权限"}, false)); await pending;
  assert.deepEqual(ui.hintTexts(), []);
  assert.match(ui.node("#question-hints-state").textContent, /无法加载/);
  assert.equal(ui.state().questionDepartmentId, "ops");
  assert.equal(ui.node("#hints").attrs["aria-busy"], "false");
  await ui.load(department("ops", []));
  assert.match(ui.node("#question-hints-state").textContent, /本部门暂无/);
  assert.deepEqual(ui.hintTexts(), []);
});

test("public pages share a public pool and cannot render department data or a stale department selection", async () => {
  const ui = page("search");
  ui.run('questionDepartmentId="hr"; questionDepartments=[{id:"hr",name:"秘密部门"}];');
  await ui.load({scope:"public",department_id:"",departments:[],questions:[{question:"公共常见问题"}]});
  assert.equal(ui.requests.at(-1).url, "/api/questions/top");
  assert.deepEqual(ui.hintTexts(), ["公共常见问题"]);
  assert.equal(ui.state().questionDepartmentId, "");
  assert.equal(ui.node("#question-department-control").hidden, true);
  await ui.load(department());
  assert.deepEqual(ui.hintTexts(), []);
  assert.match(ui.node("#question-hints-title").textContent, /公共/);
  assert.equal(ui.requests.filter((request) => request.url.includes("departments")).length, 0);
});

test("unassigned and mismatched scopes have an explicit empty or failure state", async () => {
  const ui = page();
  await ui.load({scope:"none",department_id:"",departments:[],questions:[{question:"不可借用的问题"}]});
  assert.deepEqual(ui.hintTexts(), []);
  assert.match(ui.node("#question-hints-state").textContent, /尚未分配部门/);
  await ui.load(department("foreign", [{question:"未授权部门问题"}], departments));
  assert.deepEqual(ui.hintTexts(), []);
  assert.equal(ui.state().questionDepartmentId, "");
  assert.match(ui.node("#question-hints-state").textContent, /无法加载/);
});

test("image questions capture their department before image recognition while public chat omits it", async () => {
  const ui = page();
  ui.run('questionDepartmentId="hr"; pendingImage={file:{name:"test.png"}};');
  const asking = ui.run('ask("图片描述")');
  assert.equal(ui.requests[0].url, "/api/chat/image");
  ui.run('questionDepartmentId="ops";');
  ui.requests[0].resolve(reply({question:"图片中的问题"}));
  await new Promise(setImmediate);
  const body = JSON.parse(ui.requests[1].options.body);
  assert.equal(body.department_id, "hr");
  ui.requests[1].resolve(reply(answer())); await asking;
  const publicUi = page("search"); publicUi.run('questionDepartmentId="hr";');
  const publicQuestion = publicUi.run('ask("公开问题")');
  assert.equal(JSON.parse(publicUi.requests[0].options.body).department_id, "");
  publicUi.requests[0].resolve(reply(answer())); await publicQuestion;
});

test("re-asking uses the selected department and a new conversation retains the FAQ selection", async () => {
  const ui = page(); await ui.load(department("ops", []));
  const bubble = element(); bubble.dataset = {question:"原问题",reject:"[]",kbdocs:'["doc"]'};
  const button = element(); button.closest = () => bubble;
  ui.run('questionDepartmentId="ops";');
  const request = ui.run("reaskAnswer");
  const pending = request({target:{closest:()=>button}});
  assert.equal(JSON.parse(ui.requests.at(-1).options.body).department_id, "ops");
  ui.requests.at(-1).resolve(reply(answer())); await pending;
  const emptyMessage = ui.node("#question-hints-state").textContent;
  ui.run("startNewChat()");
  assert.equal(ui.state().questionDepartmentId, "ops");
  assert.equal(ui.node("#question-hints-state").textContent, emptyMessage);
  assert.notEqual(ui.node("#hints").style.display, "none");
});
