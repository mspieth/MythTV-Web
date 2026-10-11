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

test("embedded captions use their endpoint and align cues to server playback", async () => {
  const h = harness();
  const opening = h.context.openPlayer(program(42));
  const metadata = info(42);
  metadata.media.subtitles = [{ index: "cea708", codec: "cea708", title: "CEA-708" }];
  respond(h.requests.find((r) => r.url === "/api/playback/42"), metadata);
  await settle();
  h.video.dispatchEvent(new Event("loadedmetadata"));
  await opening;
  let track;
  h.context.document.createElement = () => {
    track = new EventTarget();
    track.remove = () => {};
    track.track = { cues: [{ startTime: 100, endTime: 105 }], mode: "disabled" };
    return track;
  };
  h.element("#subtitle-track").value = "cea708";
  h.context.startTranscode(90);
  assert.equal(track.src, "/api/playback/42/captions/cea708");
  track.dispatchEvent(new Event("load"));
  assert.equal(track.track.cues[0].startTime, 10);
  assert.equal(track.track.cues[0].endTime, 15);
  h.context.closePlayer();
});
