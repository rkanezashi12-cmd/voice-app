import assert from "node:assert/strict";
import { test } from "node:test";

import {
  dateLabel,
  extensionOf,
  formatBytes,
  hasWorking,
  isVideoFile,
  percent,
  statusKind,
} from "../../web/app/lib/videos.js";

const LABELS = { done: "完了", failed: "失敗" };

test("動画の拡張子だけを受け付ける", () => {
  assert.equal(extensionOf("a.MP4"), "mp4");
  assert.equal(isVideoFile("01_受注入力.mov"), true);
  assert.equal(isVideoFile("memo.txt"), false);
  assert.equal(isVideoFile("noext"), false);
});

test("大きさと進み具合の表示", () => {
  assert.equal(formatBytes(604478 * 1024), "590MB");
  assert.equal(formatBytes(2.5 * 1024 ** 3), "2.5GB");
  assert.equal(percent(50, 200), 25);
  assert.equal(percent(5, 0), 0);
  assert.equal(percent(300, 200), 100);
});

test("状態の見せ方と、解析中があるか", () => {
  assert.equal(statusKind("完了", LABELS), "done");
  assert.equal(statusKind("失敗", LABELS), "failed");
  assert.equal(statusKind("", LABELS), "working");
  assert.equal(hasWorking([{ status: "完了" }, { status: "失敗" }], LABELS), false);
  assert.equal(hasWorking([{ status: "完了" }, { status: "" }], LABELS), true);
});

test("日時の表示", () => {
  assert.equal(dateLabel("2026-11-03T20:12:56+09:00"), "11/3 20:12");
  assert.equal(dateLabel(""), "");
});
