import assert from "node:assert/strict";
import { test } from "node:test";

import { ChunkedRecorder, newSessionId, pickMimeType } from "../../web/recorder/lib/recorder-core.js";

// MediaRecorder の最小限の偽物。stop() で dataavailable → stop の順にイベントを出す。
class FakeMediaRecorder {
  static instances = [];
  static isTypeSupported(type) {
    return type === "audio/mp4";
  }
  constructor(stream, options) {
    this.stream = stream;
    this.options = options;
    this.mimeType = options.mimeType ?? "";
    this.state = "inactive";
    this.timeslice = null;
    FakeMediaRecorder.instances.push(this);
  }
  start(timeslice) {
    this.state = "recording";
    this.timeslice = timeslice ?? null;
  }
  emit(size = 10) {
    this.ondataavailable?.({ data: { size, type: this.mimeType, id: Math.random() } });
  }
  stop() {
    this.state = "inactive";
    queueMicrotask(() => {
      this.emit();
      this.onstop?.();
    });
  }
  crash() {
    this.state = "inactive";
    this.emit();
    this.onstop?.();
  }
}

function fakeTimers() {
  const timers = [];
  return {
    timers,
    setTimeout: (fn, ms) => {
      const t = { fn, ms, cleared: false };
      timers.push(t);
      return t;
    },
    clearTimeout: (t) => {
      if (t) t.cleared = true;
    },
    fireNext() {
      const t = timers.find((x) => !x.cleared && !x.fired);
      t.fired = true;
      t.fn();
    },
  };
}

const tick = () => new Promise((r) => setTimeout(r, 0));

test("pickMimeType は対応している最初の形式を選ぶ", () => {
  assert.equal(pickMimeType(FakeMediaRecorder), "audio/mp4");
  assert.equal(pickMimeType(undefined), "");
});

test("newSessionId はサーバーの形式に合う", () => {
  const id = newSessionId("a", 1727488500123, () => 0.5);
  assert.match(id, /^\d{13}-[ab]-[a-z0-9]{4,12}$/);
  assert.ok(id.startsWith("1727488500123-a-"));
});

test("方式a: 一定時間ごとに録り直し、チャンクに連番を振る", async () => {
  FakeMediaRecorder.instances = [];
  const timers = fakeTimers();
  const chunks = [];
  const events = [];
  const rec = new ChunkedRecorder({
    stream: {},
    mode: "a",
    chunkMs: 1000,
    MediaRecorder: FakeMediaRecorder,
    onChunk: (c) => chunks.push(c),
    onEvent: (t) => events.push(t),
    setTimeout: timers.setTimeout,
    clearTimeout: timers.clearTimeout,
  });
  rec.start();
  assert.equal(FakeMediaRecorder.instances.length, 1);
  timers.fireNext(); // 1回目の区切り
  await tick();
  assert.equal(FakeMediaRecorder.instances.length, 2);
  timers.fireNext(); // 2回目の区切り
  await tick();
  await rec.stop();
  assert.deepEqual(
    chunks.map((c) => c.seq),
    [0, 1, 2],
  );
  assert.ok(chunks.every((c) => c.sessionId === rec.sessionId));
  assert.equal(rec.state, "stopped");
  assert.ok(events.includes("rotation_gap"));
});

test("方式b: timeslice で開始し、区切り直さない", async () => {
  FakeMediaRecorder.instances = [];
  const chunks = [];
  const rec = new ChunkedRecorder({
    stream: {},
    mode: "b",
    chunkMs: 60000,
    MediaRecorder: FakeMediaRecorder,
    onChunk: (c) => chunks.push(c),
  });
  rec.start();
  const mr = FakeMediaRecorder.instances[0];
  assert.equal(mr.timeslice, 60000);
  mr.emit();
  mr.emit();
  await rec.stop();
  assert.equal(FakeMediaRecorder.instances.length, 1);
  assert.deepEqual(
    chunks.map((c) => c.seq),
    [0, 1, 2],
  );
});

test("録音中に止まったら onUnexpectedStop を呼ぶ（止まる直前のデータは残す）", () => {
  FakeMediaRecorder.instances = [];
  const chunks = [];
  let stopped = null;
  const rec = new ChunkedRecorder({
    stream: {},
    mode: "a",
    chunkMs: 60000,
    MediaRecorder: FakeMediaRecorder,
    onChunk: (c) => chunks.push(c),
    onUnexpectedStop: (info) => (stopped = info),
    setTimeout: fakeTimers().setTimeout,
    clearTimeout: () => {},
  });
  rec.start();
  FakeMediaRecorder.instances[0].crash();
  assert.equal(stopped.reason, "recorder_stopped");
  assert.equal(rec.state, "failed");
  assert.equal(chunks.length, 1);
});

test("区切り直しの途中で stop しても最後のチャンクを待ってから終わる", async () => {
  FakeMediaRecorder.instances = [];
  const timers = fakeTimers();
  const chunks = [];
  const rec = new ChunkedRecorder({
    stream: {},
    mode: "a",
    chunkMs: 1000,
    MediaRecorder: FakeMediaRecorder,
    onChunk: (c) => chunks.push(c),
    setTimeout: timers.setTimeout,
    clearTimeout: timers.clearTimeout,
  });
  rec.start();
  timers.fireNext(); // 区切り直し開始（stop イベントはまだ）
  const done = rec.stop();
  await done;
  assert.equal(chunks.length, 1);
  assert.equal(FakeMediaRecorder.instances.length, 1, "停止後に新しい録音を始めない");
});
