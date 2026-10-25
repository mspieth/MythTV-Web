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
    constructor(tagName = "div") {
      super();
      this.tagName = tagName;
      this.dataset = {};
      this.content = "";
      this.value = "";
      this.textContent = "";
      this.currentTime = 0;
      this.duration = 1000;
      this.track = { mode: "disabled" };
      this.playCalls = 0;
      this.open = false;
      this.attributes = new Map();
      this.children = [];
      const classes = new Set();
      this.classList = {
        add: (...names) => names.forEach((name) => classes.add(name)),
        remove: (...names) => names.forEach((name) => classes.delete(name)),
        contains: (name) => classes.has(name),
        toggle: (name, force) => {
          const enabled = force ?? !classes.has(name);
          if (enabled) classes.add(name);
          else classes.delete(name);
          return enabled;
        },
      };
    }
    querySelector() { return new Element(); }
    querySelectorAll() { return []; }
    setAttribute(name, value) { this.attributes.set(name, value); }
    getAttribute(name) { return this.attributes.get(name) ?? null; }
    removeAttribute(name) { delete this[name]; }
    remove() { this.removed = true; }
    append(...children) { this.children.push(...children); }
    contains(element) { return element === this; }
    replaceChildren(...children) { this.children = children; }
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
    createElement: (tagName) => new Element(tagName),
    createElementNS: (_namespace, tagName) => new Element(tagName),
    pictureInPictureEnabled: true,
    fullscreenElement: null,
  });
  const video = element("#recording-video");
  video.requestPictureInPicture = async () => {
    document.pictureInPictureElement = video;
    video.dispatchEvent(new Event("enterpictureinpicture"));
  };
  document.exitPictureInPicture = async () => {
    document.pictureInPictureElement = null;
    video.dispatchEvent(new Event("leavepictureinpicture"));
  };
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
  return { context, requests, element, document, video, dialog: element("#player-dialog") };
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

test("DVB subtitles restart server playback and Off removes the overlay", async () => {
  const h = harness();
  const opening = h.context.openPlayer(program(42));
  const metadata = info(42);
  metadata.media.subtitles = [{ index: 3, codec: "dvb_subtitle", language: "eng" }];
  respond(h.requests.find((r) => r.url === "/api/playback/42"), metadata);
  await settle();
  h.video.dispatchEvent(new Event("loadedmetadata"));
  await opening;
  h.video.currentTime = 80;
  h.element("#subtitle-track").value = "3";
  await h.context.selectSubtitle("3");
  assert.match(h.video.src, /start=80&token=[a-f0-9]{32}&subtitle=3$/);
  h.video.currentTime = 10;
  h.element("#subtitle-track").value = "";
  await h.context.selectSubtitle("");
  assert.match(h.video.src, /start=90&token=[a-f0-9]{32}$/);
  h.context.closePlayer();
});

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

test("theater mode expands the shared player and can be exited", () => {
  const h = harness();
  h.element("#player-theater").dispatchEvent(new Event("click"));
  assert.equal(h.dialog.classList.contains("theater-mode"), true);
  assert.equal(h.element(".player-shell").classList.contains("theater-mode"), true);
  assert.equal(h.element("#player-exit-theater").hidden, false);
  h.element("#player-exit-theater").dispatchEvent(new Event("click"));
  assert.equal(h.dialog.classList.contains("theater-mode"), false);
  assert.equal(h.element("#player-exit-theater").hidden, true);
});

test("detach toggles Picture-in-Picture and exits theater mode", async () => {
  const h = harness();
  h.element("#player-theater").dispatchEvent(new Event("click"));
  h.element("#player-detach").dispatchEvent(new Event("click"));
  await settle();
  assert.equal(h.document.pictureInPictureElement, h.video);
  assert.equal(h.dialog.classList.contains("theater-mode"), false);
  assert.equal(h.element("#player-detach").getAttribute("aria-pressed"), "true");
  h.element("#player-detach").dispatchEvent(new Event("click"));
  await settle();
  assert.equal(h.document.pictureInPictureElement, null);
  assert.equal(h.element("#player-detach").getAttribute("aria-pressed"), "false");
});

test("player icons use SVG DOM nodes instead of HTML parsing", () => {
  const h = harness();
  h.video.dispatchEvent(new Event("play"));
  const icon = h.element("#player-toggle").children[0];
  assert.equal(icon.tagName, "svg");
  assert.equal(icon.children[0].tagName, "path");
  assert.equal(icon.getAttribute("aria-hidden"), "true");
  assert.doesNotMatch(source, /\.innerHTML\b/);
});

test("subtitle URLs use the matched numeric track metadata", async () => {
  const h = harness();
  vm.runInContext(
    'activeVideo = { Id: 17 }; videoPlaybackInfo = { subtitles: [{ index: 2, language: "en", title: "English" }] };',
    h.context,
  );

  await h.context.selectSubtitle("2");

  assert.equal(
    h.video.children[0].src,
    "/api/videos/17/subtitles/2?start=0",
  );
});
