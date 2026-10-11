const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const { webcrypto } = require("node:crypto");
const { test } = require("node:test");

const source = fs.readFileSync("mythtv_web/static/library.js", "utf8");

function deferred() {
  let resolve;
  const promise = new Promise((done) => { resolve = done; });
  return { promise, resolve };
}

function harness() {
  class Element extends EventTarget {
    constructor() {
      super();
      this.dataset = {};
      this.content = "";
      this.value = "";
      this.textContent = "";
      this.currentTime = 0;
      this.duration = 1000;
      this.playCalls = 0;
      this.open = false;
      this.classList = { add() {}, remove() {}, toggle() {} };
    }
    querySelector() { return new Element(); }
    querySelectorAll() { return []; }
    setAttribute() {}
    removeAttribute(name) { delete this[name]; }
    append() {}
    replaceChildren() {}
    add() {}
    focus() {}
    showModal() { this.open = true; }
    close() { this.open = false; }
    pause() {}
    load() { this.currentTime = 0; }
    play() { this.playCalls += 1; return Promise.resolve(); }
  }
  const elements = new Map();
  const element = (selector) => {
    if (!elements.has(selector)) elements.set(selector, new Element());
    return elements.get(selector);
  };
  const requests = [];
  const document = Object.assign(new EventTarget(), {
    body: new Element(),
    querySelector: element,
    querySelectorAll: () => [],
    createElement: () => new Element(),
    fullscreenElement: null,
  });
  const window = Object.assign(new EventTarget(), {
    setTimeout: (fn, delay) => { const timer = setTimeout(fn, delay); timer.unref(); return timer; },
    clearTimeout,
    clearInterval,
    setInterval: (fn, delay) => { const timer = setInterval(fn, delay); timer.unref(); return timer; },
  });
  const context = vm.createContext({
    document, window, AbortController, DOMException, crypto: webcrypto,
    Option: function Option() {},
    fetch: (url, options = {}) => {
      const pending = deferred();
      requests.push({ url, options, ...pending });
      return pending.promise;
    },
  });
  vm.runInContext(source, context);
  return { context, requests, element, video: element("#recording-video"), dialog: element("#player-dialog") };
}

function program(id) {
  return { Title: `Recording ${id}`, Recording: { RecordedId: id } };
}

function info(id) {
  return {
    program: program(id), comm_breaks: [], bookmark: 0, last_position: 0,
    media: { subtitles: [], video_codec: "h264", audio_codec: "aac" },
  };
}

function respond(request, data) {
  request.resolve({ ok: true, json: async () => data });
}

async function settle() {
  await new Promise((resolve) => setImmediate(resolve));
}

test("closing during metadata fetch ignores a late response without saving zero", async () => {
  const h = harness();
  const opening = h.context.openPlayer(program(42));
  const request = h.requests.find((r) => r.url === "/api/playback/42");
  h.context.closePlayer();
  assert.equal(request.options.signal.aborted, true);
  respond(request, info(42));
  await opening;
  assert.equal(h.dialog.open, false);
  assert.equal(h.video.src, undefined);
  assert.equal(h.video.playCalls, 0);
  assert.equal(h.requests.some((r) => r.url.endsWith("/position")), false);
});

test("an older recording response cannot replace the current recording", async () => {
  const h = harness();
  const first = h.context.openPlayer(program(42));
  const second = h.context.openPlayer(program(43));
  respond(h.requests.find((r) => r.url === "/api/playback/43"), info(43));
  await settle();
  h.video.dispatchEvent(new Event("loadedmetadata"));
  await second;
  respond(h.requests.find((r) => r.url === "/api/playback/42"), info(42));
  await first;
  assert.equal(h.video.src, "/api/playback/43/stream");
  assert.equal(h.video.playCalls, 1);
  h.context.closePlayer();
});

test("closing cancels media event waits and delayed saves cannot close a new player", async () => {
  const h = harness();
  const first = h.context.openPlayer(program(42));
  respond(h.requests.find((r) => r.url === "/api/playback/42"), info(42));
  await settle();
  h.context.closePlayer();
  await first;
  const second = h.context.openPlayer(program(43));
  respond(h.requests.find((r) => r.url === "/api/playback/43"), info(43));
  await settle();
  h.video.dispatchEvent(new Event("loadedmetadata"));
  await second;
  h.video.currentTime = 120;
  h.context.closePlayer();
  const saving = h.requests.find((r) => r.url.endsWith("/position"));
  const third = h.context.openPlayer(program(44));
  respond(saving, { saved: true });
  await settle();
  assert.equal(h.dialog.open, true);
  assert.equal(h.element("#player-title").textContent, "Recording 44");
  h.context.closePlayer();
  respond(h.requests.find((r) => r.url === "/api/playback/44"), info(44));
  await third;
});
