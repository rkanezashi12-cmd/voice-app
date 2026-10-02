// 商談日報ウィジェット（widgets/meeting-report/app/report.js）の画面に触れない関数。
import assert from "node:assert/strict";
import { createRequire } from "node:module";
import { test } from "node:test";

import { REPORT_SECTIONS as APP_SECTIONS } from "../../web/app/lib/view.js";

const require = createRequire(import.meta.url);
const R = require("../../widgets/meeting-report/app/report.js");

const CONFIG = {
  module: "MeetingRecords",
  accounts_module: "Accounts",
  fields: {
    name: "Name",
    status: "Status",
    meeting_type: "Meeting_Type",
    capture_method: "Capture_Method",
    account: "Account",
    owner: "Owner",
    contact_name: "Contact_Name",
    start_at: "Start_At",
    category: "Category",
    error_message: "Error_Message",
    transcript: "Transcript",
    transcript_2: "Transcript_2",
    summary: "Summary",
    issues: "Issues",
    needs: "Needs",
    budget: "Budget",
    decision_maker: "Decision_Maker",
    competitors: "Competitors",
    next_actions: "Next_Actions",
    due_date: "Due_Date",
  },
  status: { done: "完了", no_account: "取引先未設定", failed: "失敗", join_failed: "参加失敗", transcribing: "文字起こし中" },
};

test("日報の項目と順は録音アプリの日報と同じ", () => {
  assert.deepEqual(R.REPORT_SECTIONS, APP_SECTIONS);
});

test("CRM のレコードを画面の形にする（ルックアップは名前、文字起こし全文は2つをつなぐ）", () => {
  const view = R.toView(
    {
      id: "4928352000069211999",
      Name: "【TEST】2026-10-02 株式会社アイシン 訪問",
      Status: "完了",
      Meeting_Type: "対面",
      Capture_Method: "対面録音",
      Account: { name: "株式会社アイシン", id: "4928352000000412001" },
      Owner: { name: "マルサン木型 OFFICE", id: "1", email: "x@example.com" },
      Contact_Name: "小林 克彰、山田 竜成",
      Start_At: "2026-10-02T23:55:00+09:00",
      Summary: "鋳造の相談",
      Budget: null,
      Due_Date: "2026-10-10",
      Transcript: "話者A: こんにちは\n話者B: よろしくお願いします",
      Transcript_2: "話者A: では始めます",
    },
    CONFIG,
  );
  assert.equal(view.account, "株式会社アイシン");
  assert.equal(view.accountId, "4928352000000412001");
  assert.equal(view.owner, "マルサン木型 OFFICE");
  assert.equal(view.state, "done");
  assert.equal(view.budget, "", "null は空");
  assert.equal(view.transcript, "話者A: こんにちは\n話者B: よろしくお願いします\n話者A: では始めます");
  assert.deepEqual(
    R.reportSections(view).map((s) => [s.label, s.text]),
    [
      ["要約", "鋳造の相談"],
      ["次回期限", "2026/10/10"],
    ],
  );
});

test("状態の出し分けと説明", () => {
  const s = CONFIG.status;
  assert.equal(R.stateOf("取引先未設定", s), "done");
  assert.equal(R.stateOf("参加失敗", s), "failed");
  assert.equal(R.stateOf("文字起こし中", s), "processing");
  assert.equal(R.stateOf("予約済", s), "waiting");
  assert.equal(R.stateOf("", s), "none");
  assert.match(R.noticeText({ state: "failed", errorMessage: "文字起こし結果が空でした" }), /文字起こし結果が空でした/);
  assert.match(R.noticeText({ state: "waiting", status: "録音中" }), /状態：録音中/);
  assert.match(R.noticeText({ state: "processing" }), /再読み込み/);
  assert.equal(R.noticeText({ state: "done" }), "");
});

test("文字起こしを話者ごとにまとめる（話者の無い行・時刻・全角のコロン）", () => {
  const blocks = R.parseTranscript(
    ["話者A: こんにちは", "続きの行", "話者A：同じ人", "", "山田 竜成: 10:30 からで", "10:30 に集合です"].join("\n"),
  );
  assert.deepEqual(blocks, [
    { speaker: "話者A", text: "こんにちは\n続きの行\n同じ人" },
    { speaker: "山田 竜成", text: "10:30 からで\n10:30 に集合です" },
  ]);
  assert.deepEqual(R.parseTranscript("見出しの無い文"), [{ speaker: "", text: "見出しの無い文" }]);
  const colors = R.speakerColors([{ speaker: "B" }, { speaker: "A" }, { speaker: "B" }, { speaker: "" }]);
  assert.deepEqual([...colors.entries()], [
    ["B", 0],
    ["A", 1],
  ]);
});

test("検索語で区切る（大文字・小文字を区別しない）", () => {
  assert.deepEqual(R.highlight("中子と中子", "中子"), [
    { text: "中子", hit: true },
    { text: "と", hit: false },
    { text: "中子", hit: true },
  ]);
  assert.deepEqual(R.highlight("Zoho CRM", "crm"), [
    { text: "Zoho ", hit: false },
    { text: "CRM", hit: true },
  ]);
  assert.deepEqual(R.highlight("abc", " "), [{ text: "abc", hit: false }]);
  assert.equal(R.countHits([{ text: "抜き勾配" }, { text: "勾配と勾配" }], "勾配"), 3);
});

test("日時・ID・SDK のエラー", () => {
  assert.equal(R.dateTimeLabel("2026-10-02T23:55:00+09:00"), "2026/10/02 23:55");
  assert.equal(R.dateTimeLabel(null), "");
  assert.equal(R.dateLabel("2026-10-10"), "2026/10/10");
  assert.equal(R.isRecordId("4928352000069211999"), true);
  assert.equal(R.isRecordId("1e5"), false);
  assert.equal(R.isRecordId(undefined), false);
  assert.match(
    R.sdkErrorText({ data: [{ code: "NO_PERMISSION", message: "permission denied", details: {} }] }),
    /NO_PERMISSION: permission denied/,
  );
  assert.match(R.sdkErrorText({ status: 500 }), /"status":500/);
  assert.match(R.sdkErrorText(new Error("network")), /network/);
});
