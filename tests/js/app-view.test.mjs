// 録音アプリ（/app/）の表示用の純粋な関数。
import assert from "node:assert/strict";
import { test } from "node:test";

import {
  candidateLabel,
  describeSelection,
  errorMessage,
  geoErrorText,
  gpsLabel,
  isPending,
  joinNames,
  loginErrorText,
  parseHash,
  reportSections,
  stateLabel,
  timeLabel,
} from "../../web/app/lib/view.js";

test("ハッシュから画面と日報の記録 ID を読む（知らない値は入力に戻す）", () => {
  assert.deepEqual(parseHash("#report=4928352000069063054"), { tab: "report", reportId: "4928352000069063054" });
  assert.deepEqual(parseHash("#record"), { tab: "record", reportId: null });
  assert.deepEqual(parseHash(""), { tab: "input", reportId: null });
  assert.deepEqual(parseHash("#report=abc"), { tab: "report", reportId: null }, "ID は数字だけ");
  assert.deepEqual(parseHash("#other=1"), { tab: "input", reportId: null });
});

test("ログインの失敗の理由（知らない種類は一般的な文）", () => {
  assert.match(loginErrorText("not_crm_user"), /この CRM のユーザーではありません/);
  assert.match(loginErrorText("expired"), /時間が過ぎました/);
  assert.match(loginErrorText("no_api_access"), /API の利用/);
  assert.equal(loginErrorText("unknown"), loginErrorText("failed"));
  assert.equal(loginErrorText(null), "");
});

test("選んだ訪問先と担当者の説明", () => {
  assert.equal(describeSelection(null, []), "訪問先を選択してください。");
  assert.equal(
    describeSelection({ id: "1", name: "株式会社サンプル" }, ["田中 太郎", "山本 次郎"]),
    "訪問先：株式会社サンプル　担当者：田中 太郎、山本 次郎",
  );
  assert.equal(describeSelection({ id: null, name: "みなと鋳物", newCustomer: true }, []), "訪問先：みなと鋳物（新規）　担当者：未選択");
  assert.equal(joinNames(["a", " ", "", "b"]), "a、b");
});

test("候補の表示・GPS の状態・位置のエラー", () => {
  assert.equal(candidateLabel({ name: "A社", reason: "同じ町（山下町）" }), "A社（同じ町（山下町））");
  assert.equal(candidateLabel({ name: "A社" }), "A社");
  assert.equal(gpsLabel("横浜市中区", 23.4), "取得済み（横浜市中区付近・誤差 約23m）");
  assert.equal(gpsLabel("", 0), "取得済み（住所不明）");
  assert.match(geoErrorText(1), /許可されていません/);
  assert.match(geoErrorText(3), /時間内/);
  assert.equal(geoErrorText(2), "位置を取得できませんでした");
});

test("日報の状態と、空の項目を出さない区分け", () => {
  assert.equal(stateLabel({ state: "done", status: "取引先未設定" }), "取引先未設定");
  assert.equal(stateLabel({ state: "processing", status: "文字起こし中" }), "文字起こし・要約中");
  assert.equal(stateLabel({ state: "waiting", status: "" }), "録音待ち・送信待ち");
  assert.equal(isPending({ state: "processing" }), true);
  assert.equal(isPending({ state: "done" }), false);
  const sections = reportSections({ summary: "要約です", issues: "  ", budget: null, next_actions: "・見積もり" });
  assert.deepEqual(sections, [
    { label: "要約", text: "要約です" },
    { label: "次のアクション", text: "・見積もり" },
  ]);
});

test("時刻は CRM が返した時刻帯のまま（端末の時刻帯に左右されない）", () => {
  assert.equal(timeLabel("2026-10-01T10:05:00+09:00"), "10:05");
  assert.equal(timeLabel(null), "");
  assert.equal(timeLabel("2026-10-01"), "");
});

test("API のエラー本文から画面に出す文", () => {
  assert.equal(errorMessage({ detail: "訪問先を選んでください" }, 422), "訪問先を選んでください");
  assert.equal(errorMessage({ error: "x", message: "CRM に接続できません" }, 502), "CRM に接続できません");
  assert.equal(errorMessage({ detail: [{ loc: ["body"] }] }, 422), "入力の形が正しくありません");
  assert.equal(errorMessage(null, 500), "エラーが発生しました（500）");
});
