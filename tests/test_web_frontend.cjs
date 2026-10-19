/* Browser regression tests without external DOM dependencies. */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const path = require("node:path");

class FakeElement {
  constructor(tag = "div", id = "") {
    this.tag = tag;
    this.id = id;
    this._value = "";
    this.children = [];
    this.listeners = new Map();
    this.dataset = {};
    this.style = {};
    this.classList = {
      add() {},
      remove() {},
      toggle() {}
    };
    this.textContent = "";
    this.disabled = false;
    this.files = [];
    this.options = this.children;
  }
  get value() { return this._value; }
  set value(v) { this._value = String(v ?? ""); }
  get childNodes() { return this.children; }
  appendChild(child) { this.children.push(child); return child; }
  append(...children) { for (const child of children) this.appendChild(child); }
  replaceChildren(...children) {
    this.children = [...children];
    this.options = this.children;
  }
  addEventListener(event, cb) {
    if (!this.listeners.has(event)) this.listeners.set(event, []);
    this.listeners.get(event).push(cb);
  }
  dispatchEvent(event) {
    for (const callback of this.listeners.get(event.type) || []) callback(event);
  }
  setAttribute() {}
  querySelector() { return new FakeElement("span"); }
  querySelectorAll() { return []; }
  replaceWith() {}
  remove() {}
  focus() {}
  get innerHTML() { return this._html || ""; }
  set innerHTML(text) { this._html = text; this.children = []; }
}
const elements = new Map();
function el(id) {
  if (!elements.has(id)) elements.set(id, new FakeElement("div", id));
  return elements.get(id);
}
const tabs = ["chat", "video"].map(mode => {
  const element = new FakeElement("button");
  element.dataset.mode = mode;
  return element;
});
const windowHandlers = new Map();
const storage = new Map();
const serverThreads = new Map();
let generatedIds = 0;
storage.set("muse-ai-web-chat-v1", JSON.stringify({
  activeId: "a1",
  threads: [
    { id: "a1", accountId: "A", sessionId: "s1", name: "A chat 1", messages: Array.from({length: 130}, (_, i) => ({role: "user", text: "test " + i})) },
    { id: "b1", accountId: "B", sessionId: "s2", name: "B chat 1", messages: [] },
    { id: "a2", accountId: "A", sessionId: "s3", name: "A chat 2", messages: [] },
    { id: "old", sessionId: "primary", name: "Legacy unassigned", messages: [] }
  ]
}));
const windowObject = {
  addEventListener(name, cb) {
    if (!windowHandlers.has(name)) windowHandlers.set(name, []);
    windowHandlers.get(name).push(cb);
  },
  dispatchEvent(event) {
    for (const cb of windowHandlers.get(event.type) || []) cb(event);
  }
};
const context = vm.createContext({
  document: {
    getElementById: el,
    createElement: tag => new FakeElement(tag),
    querySelectorAll: selector => selector === ".mode-tab" ? tabs : []
  },
  window: windowObject,
  localStorage: {
    getItem: key => storage.get(key) || null,
    setItem: (key, value) => storage.set(key, value)
  },
  CustomEvent: class {
    constructor(type, options = {}) { this.type = type; this.detail = options.detail; }
  },
  Event: class { constructor(type) { this.type = type; } },
  crypto: { randomUUID: () => "generated-id-" + (++generatedIds) },
  FormData: class {
    constructor() { this.fields = []; }
    append(key, value, name) { this.fields.push({key, value, name}); }
  },
  fetch: async (url, options = {}) => {
    const success = (body) => ({ok: true, json: async () => body});
    if (url === "/api/chat/threads" && !options.method) {
      return success({threads: [...serverThreads.values()]});
    }
    if (url.startsWith("/api/chat/threads/") && options.method === "PUT") {
      const id = decodeURIComponent(url.split("/").pop());
      const data = JSON.parse(options.body);
      const thread = {id, ...data};
      serverThreads.set(id, thread);
      return success({ok: true, thread});
    }
    if (url.endsWith("/sync") && options.method === "POST") {
      const id = decodeURIComponent(url.split("/").at(-2));
      const old = serverThreads.get(id);
      const thread = {...old, messages: [...old.messages, {
        role: "assistant", text: "Dạ bẩm, đây là câu trả lời đầy đủ từ Muse."
      }]};
      serverThreads.set(id, thread);
      return success({ok: true, thread, remote_count: 1});
    }
    if (url === "/api/chat/send-files" && options.method === "POST") {
      const files = options.body.fields.filter(item => item.key === "files");
      assert.equal(files.length, 2, "Both selected files must be sent together");
      const thread = options.body.fields.find(item => item.key === "thread_id").value;
      return success({
        ok: true, text: "Đã nhận hai tệp.", session_id: "s1",
        reply_finished: true, media_expected: false,
        uploaded_files: files.map((item, i) => ({
          id: (i ? "b" : "a").repeat(32), name: item.name, size: item.value.size,
          kind: i ? "document" : "image",
          url: "/api/chat/uploads/" + thread + "/" + (i ? "b" : "a").repeat(32),
        }))
      });
    }
    if (url === "/api/auth/status") {
      return success({authenticated: false, ready_accounts: 0});
    }
    return success({jobs: []});
  },
  setInterval: () => 1,
  clearInterval() {},
  setTimeout: () => 1,
  console
});
el("taskCount").value = "1";
for (const script of ["app.js", "chat.js"]) {
  vm.runInContext(
    fs.readFileSync(path.join(__dirname, "../muse_ai/web/static", script), "utf8"),
    context,
    { filename: script }
  );
}
const accounts = [
  { id: "A", label: "Account A", enabled: true, status: "ready", busy: false },
  { id: "B", label: "Account B", enabled: true, status: "ready", busy: false },
  { id: "C", label: "Account C", enabled: true, status: "ready", busy: false }
];
(async function() {
  windowObject.dispatchEvent(new context.CustomEvent("muse-accounts-updated", { detail: accounts }));
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(el("generate").disabled, false, "3 free accounts must enable generation");
  assert.equal(el("chatAccount").value, "A");
  assert.deepEqual(
    el("chatThreads").children.map(option => option.textContent),
    ["A chat 1", "A chat 2"],
    "Only conversations for selected A should be listed"
  );
  el("chatAccount").value = "B";
  el("chatAccount").dispatchEvent({ type: "change" });
  assert.deepEqual(
    el("chatThreads").children.map(option => option.textContent),
    ["B chat 1"],
    "Switching to B must hide all A and legacy conversations"
  );
  windowObject.dispatchEvent(new context.CustomEvent("muse-accounts-updated", { detail: accounts }));
  assert.equal(el("chatAccount").value, "B", "pool refresh must preserve explicit account");
  assert.deepEqual(el("chatThreads").children.map(option => option.textContent), ["B chat 1"]);
  el("taskCount").value = "4";
  el("taskCount").dispatchEvent({ type: "input" });
  assert.equal(el("generate").disabled, true, "N above idle accounts must be disabled");
  el("taskCount").value = "2";
  el("taskCount").dispatchEvent({ type: "input" });
  assert.equal(el("generate").disabled, false);
  const busyPool = accounts.map(a => ({ ...a, busy: a.id !== "A" }));
  windowObject.dispatchEvent(new context.CustomEvent("muse-accounts-updated", { detail: busyPool }));
  assert.equal(el("generate").disabled, true, "busy accounts must not be counted");
  const saved = JSON.parse(storage.get("muse-ai-web-chat-v1"));
  assert.equal(saved.threads.find(thread => thread.id === "a1").messages.length, 130,
    "Saving must preserve all messages instead of silently dropping those before 120");
  for (let i = 0; i < 35; i++) {
    el("newChat").dispatchEvent({type: "click"});
  }
  const afterNewChats = JSON.parse(storage.get("muse-ai-web-chat-v1"));
  assert(afterNewChats.threads.length > 30, "Saving must not discard older threads");
  assert(afterNewChats.threads.some(thread => thread.id === "a1"),
    "Oldest original thread should not disappear when new chats are created");
  await new Promise(resolve => setImmediate(resolve));
  assert(serverThreads.has("a1"), "Browser history must be migrated into SQLite via PUT");
  assert.equal(serverThreads.get("a1").messages.length, 130,
    "Migrated browser messages must be preserved");
  el("chatAccount").value = "A";
  el("chatAccount").dispatchEvent({type: "change"});
  el("chatThreads").value = "a1";
  el("chatThreads").dispatchEvent({type: "change"});
  el("syncChat").dispatchEvent({type: "click"});
  await new Promise(resolve => setImmediate(resolve));
  const synced = JSON.parse(storage.get("muse-ai-web-chat-v1"));
  assert(synced.threads.find(t => t.id === "a1").messages.some(m =>
    m.text === "Dạ bẩm, đây là câu trả lời đầy đủ từ Muse."),
    "Sync button must fetch remote Muse history and display it");
  el("chatFileInput").files = [
    {name: "photo.png", size: 100, type: "image/png"},
    {name: "manual.pdf", size: 200, type: "application/pdf"}
  ];
  el("chatFileInput").dispatchEvent({type: "change"});
  assert.equal(el("chatSelectedFiles").children.length, 2);
  el("chatInput").value = "Kiểm tra nhiều tệp";
  el("sendChat").dispatchEvent({type: "click"});
  await new Promise(resolve => setImmediate(resolve));
  await new Promise(resolve => setImmediate(resolve));
  await new Promise(resolve => setImmediate(resolve));
  const uploadSaved = JSON.parse(storage.get("muse-ai-web-chat-v1")).threads.find(t => t.id === "a1");
  const sent = uploadSaved.messages.find(m => m.text === "Kiểm tra nhiều tệp");
  assert.equal(sent?.uploads?.length, 2, "Uploaded file metadata must survive in chat");
  assert.equal(sent.uploads[0].name, "photo.png");
  assert.equal(sent.uploads[1].name, "manual.pdf");
  assert(sent.uploads[0].id, "Backend-generated file reference must be stored");
  assert.equal(el("chatSelectedFiles").children.length, 0, "Sent file selection must clear");
  console.log("Frontend regression: SQLite, Muse sync, multi-file composer and history passed");
})().catch(err => { console.error(err); process.exitCode = 1; });
