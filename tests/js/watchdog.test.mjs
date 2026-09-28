import assert from "node:assert/strict";
import { test } from "node:test";

import { buildSummary, Diagnostics } from "../../web/recorder/lib/diagnostics.js";
import { GapWatchdog, SilenceDetector } from "../../web/recorder/lib/watchdog.js";

test("タイマーが止まっていた時間を検知する", () => {
  let now = 0;
  const gaps = [];
  const w = new GapWatchdog({
    now: () => now,
    setInterval: () => 1,
    clearInterval: () => {},
    onGap: (g) => gaps.push(g),
  });
  w.start();
  now = 1000;
  w.tick();
  now = 2000;
  w.tick();
  assert.equal(gaps.length, 0);
  now = 32000; // 30秒止まっていた
  w.tick();
  assert.equal(gaps.length, 1);
  assert.equal(gaps[0].durationMs, 29000);
});

test("ほぼ完全な無音が続いたら一度だけ知らせ、音が戻ったら解除する", () => {
  let now = 0;
  let silences = 0;
  let sounds = 0;
  const d = new SilenceDetector({
    now: () => now,
    silenceMs: 10000,
    onSilence: () => (silences += 1),
    onSound: () => (sounds += 1),
  });
  for (let t = 0; t <= 12000; t += 500) {
    now = t;
    d.push(0);
  }
  assert.equal(silences, 1);
  now = 13000;
  d.push(0.2);
  assert.equal(sounds, 1);
});

test("小さな声（無音ではない）では知らせない", () => {
  let now = 0;
  let silences = 0;
  const d = new SilenceDetector({ now: () => now, onSilence: () => (silences += 1) });
  for (let t = 0; t <= 20000; t += 500) {
    now = t;
    d.push(0.002);
  }
  assert.equal(silences, 0);
});

test("診断イベントは送れた分だけ消し、送れなければ残す", async () => {
  let ok = false;
  const batches = [];
  const diag = new Diagnostics({
    send: async (events) => {
      batches.push(events.length);
      return ok;
    },
    batchSize: 2,
  });
  diag.log("a", { n: 1, "bad key": 2, long: "x".repeat(500) });
  diag.log("b");
  diag.log("c");
  await diag.flush();
  assert.equal(diag.pending.length, 3);
  assert.equal(diag.pending[0].detail.long.length, 120);
  assert.equal("bad key" in diag.pending[0].detail, false);
  ok = true;
  await diag.flush();
  assert.equal(diag.pending.length, 0);
  assert.deepEqual(batches, [2, 2, 1]);
});

test("診断結果のまとめ", () => {
  const s = buildSummary({
    counters: { interrupted: 2, hidden: 3 },
    startedAt: 0,
    now: 65000,
    uploaded: 5,
    pending: 1,
    failedAttempts: 2,
    gaps: [{ durationMs: 30000 }],
    mimeType: "audio/mp4",
    mode: "a",
    chunkSeconds: 60,
    wakeLock: "有効",
    userAgent: "test",
    rotationGaps: [30, 10, 20],
  });
  assert.equal(s.elapsed_seconds, 65);
  assert.equal(s.interruptions, 2);
  assert.equal(s.timer_gap_total_seconds, 30);
  assert.equal(s.rotation_gap_ms_median, 20);
  assert.equal(s.rotation_gap_ms_max, 30);
});
