import assert from "node:assert/strict";
import {setTimeout as wait} from "node:timers/promises";
import test from "node:test";
import {createSession} from "../../web/client/session.js";

const identity = {user_id: "user-a", session_generation: "3", csrf_token: "csrf-example"};

test("recover a disconnect between event receipt and snapshot refresh", async t => {
  const streams = [];
  const steps = [];
  class Stream extends EventTarget {
    static CLOSED = 2;
    constructor(url) {
      super();
      this.url = new URL(url, "https://example.test");
      streams.push(this);
      steps.push("stream");
    }
    close() { this.closed = true; }
    send(sequence) {
      this.dispatchEvent(new MessageEvent("job.cancelled", {lastEventId: sequence}));
    }
  }
  const original = globalThis.EventSource;
  globalThis.EventSource = Stream;
  t.after(() => original ? globalThis.EventSource = original : delete globalThis.EventSource);
  t.mock.method(globalThis, "fetch", async (path, options) => {
    assert.equal(options.headers.get("X-Page-User"), identity.user_id);
    assert.equal(options.headers.get("X-Session-Generation"), identity.session_generation);
    steps.push(path.split("/").pop());
    return Response.json(path.endsWith("/cursor") ? {user_seq: "9007199254740992"} : identity);
  });
  const session = createSession(identity, () => assert.fail("Unexpected revocation"));
  t.after(() => session.dispose());
  session.startEvents(() => { steps.push("snapshot"); }, () => {});
  await wait(20);
  assert.deepEqual(steps, ["me", "cursor", "snapshot", "stream"]);
  const first = streams[0];
  first.send("9007199254740993");
  first.onerror();
  await wait(1150);
  assert.equal(first.closed, true);
  assert.equal(streams.length, 2);
  assert.deepEqual(steps.slice(4), ["me", "snapshot", "stream"]);
  assert.equal(streams[1].url.searchParams.get("after"), "9007199254740993");
  assert.equal(streams[1].url.searchParams.get("page_user"), "user-a");
  streams[1].send("9007199254740993");
  await wait(185);
  assert.equal(steps.at(-1), "stream");
});

test("invalidating the bound page aborts its other private reads", async t => {
  let invalidations = 0;
  const session = createSession(identity, () => invalidations++);
  let outstanding;
  t.mock.method(globalThis, "fetch", async (path, options) => {
    if (path.endsWith("/me")) {
      return Response.json({error: {code: "SESSION_INVALID"}}, {status: 401});
    }
    outstanding = options.signal;
    return new Promise((_, reject) => {
      options.signal.addEventListener("abort", () => reject(options.signal.reason), {once: true});
    });
  });
  const pending = assert.rejects(session.api("/jobs"), {name: "AbortError"});
  await assert.rejects(session.api("/me"), {code: "SESSION_INVALID"});
  await pending;
  assert.equal(outstanding.aborted, true);
  assert.equal(invalidations, 1);
});

test("going offline closes the stream and going online resumes from its cursor", async t => {
  const events = new EventTarget();
  const streams = [];
  const statuses = [];
  let online = true;
  class Stream extends EventTarget {
    constructor(url) {
      super();
      this.url = new URL(url, "https://example.test");
      streams.push(this);
    }
    close() { this.closed = true; }
  }
  const bindings = {
    EventSource: Stream,
    addEventListener: events.addEventListener.bind(events),
    removeEventListener: events.removeEventListener.bind(events),
  };
  for (const [name, value] of Object.entries(bindings)) {
    const descriptor = Object.getOwnPropertyDescriptor(globalThis, name);
    Object.defineProperty(globalThis, name, {configurable: true, writable: true, value});
    t.after(() => {
      if (descriptor) Object.defineProperty(globalThis, name, descriptor);
      else delete globalThis[name];
    });
  }
  Object.defineProperty(navigator, "onLine", {configurable: true, get: () => online});
  t.after(() => { delete navigator.onLine; });
  t.mock.method(globalThis, "fetch", async path => {
    assert.ok(online);
    return Response.json(path.endsWith("/cursor") ? {user_seq: "42"} : identity);
  });
  const session = createSession(identity, () => assert.fail("Unexpected revocation"));
  t.after(() => session.dispose());
  session.startEvents(() => {}, status => statuses.push(status));
  await wait(20);
  online = false;
  events.dispatchEvent(new Event("offline"));
  assert.equal(streams[0].closed, true);
  assert.equal(statuses.at(-1), "离线，等待网络");
  online = true;
  events.dispatchEvent(new Event("online"));
  await wait(20);
  assert.equal(streams.length, 2);
  assert.equal(streams[1].url.searchParams.get("after"), "42");
});
