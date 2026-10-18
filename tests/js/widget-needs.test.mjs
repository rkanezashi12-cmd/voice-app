// ニーズ分析ウィジェット（widgets/needs-analysis/app/analysis.js）の画面に触れない関数。
// 「上位のニーズ」などの文の形は app/needs/analysis.py の _format と同じ（tests/test_needs.py でも形を確かめている）。
import assert from "node:assert/strict";
import { createRequire } from "node:module";
import { test } from "node:test";

const require = createRequire(import.meta.url);
const V = require("../../widgets/needs-analysis/app/analysis.js");

export const CONFIG = {
  analysis_module: "NeedsAnalyses",
  need_module: "CustomerNeeds",
  meeting_module: "MeetingRecords",
  inquiry_module: "Inquiry",
  analysis_fields: {
    name: "Name", period_from: "Period_From", period_to: "Period_To", targets: "Targets", status: "Analysis_Status",
    counts: "Counts", top_needs: "Top_Needs", matched_needs: "Matched_Needs", product_ideas: "Product_Ideas",
    error_message: "Error_Message", analyzed_at: "Analyzed_At",
  },
  need_fields: {
    name: "Name", detail: "Detail", need_type: "Need_Type", source_type: "Source_Type", company: "Company_Name",
    evidence: "Evidence", need_date: "Need_Date", extract_key: "Extract_Key",
  },
  values: {
    need_types: ["機能・性能", "品質", "コスト", "納期", "使いやすさ", "サービス", "その他"],
    source_meeting: "商談", source_inquiry: "WEB問合せ",
    target_both: "商談とWEB問合せ", target_meetings: "商談だけ", target_inquiries: "WEB問合せだけ",
    status_running: "実行中", status_done: "完了", status_failed: "失敗",
  },
};

const TOP = `1. 溶接データの自動記録（5件・3社）
取引先から求められている。
例：株式会社あさひ印刷「溶接データの自動記録」、株式会社ポセイドン・リゾート「後付け」

2. 自動供給（6件・4社）
人手不足。`;

const IDEAS = `■ 溶接データの自動記録
方向性：後付けのロガー
理由：要望が多い
2行目の続き
土台にできる自社技術：なし
求める声：5件・3社（A社、B社）

■ 自動供給
方向性：ロボットセル`;

test("上位のニーズの文をテーマごとに分ける", () => {
  const themes = V.parseThemes(TOP);
  assert.equal(themes.length, 2);
  assert.deepEqual(
    { ...themes[0] },
    {
      rank: 1, theme: "溶接データの自動記録", count: 5, companies: 3, description: "取引先から求められている。",
      examples: "株式会社あさひ印刷「溶接データの自動記録」、株式会社ポセイドン・リゾート「後付け」",
    },
  );
  assert.equal(themes[1].examples, "");
  assert.deepEqual(V.parseThemes("期間内にニーズが見つかりませんでした。"), []);
});

test("■ のまとまりを見出しとラベルに分ける（続きの行は前のラベルにつなぐ）", () => {
  const blocks = V.parseBlocks(IDEAS);
  assert.equal(blocks.length, 2);
  assert.equal(blocks[0].title, "溶接データの自動記録");
  assert.deepEqual(blocks[0].rows[1], { label: "理由", text: "要望が多い\n2行目の続き" });
  assert.deepEqual(V.parseBlocks("自社技術で応えられるニーズは見つかりませんでした。"), []);
});

test("件数の文から数を取り出す", () => {
  const c = V.parseCounts("商談 32件・WEB問合せ 30件 → ニーズ 95件（今回新しく抜き出した 95件）");
  assert.deepEqual([c.meetings, c.inquiries, c.needs, c.newNeeds], [32, 30, 95, 95]);
  assert.equal(V.parseCounts("").meetings, null);
});

test("記録を画面用に直す（状態・期間の既定・対象の既定）", () => {
  const view = V.toView(
    { id: "9", Name: "【TEST】10月", Analysis_Status: "完了", Period_To: "2026-10-31", Top_Needs: TOP },
    CONFIG,
  );
  assert.equal(view.state, "done");
  assert.deepEqual(view.period, { from: "2025-10-31", to: "2026-10-31" }, "開始が空なら365日前");
  assert.equal(view.targets, "商談とWEB問合せ");
  assert.equal(view.themes.length, 2);
  const empty = V.toView({ id: "9" }, CONFIG, new Date(2026, 9, 19));
  assert.equal(empty.state, "idle");
  assert.equal(empty.period.to, "2026-10-19", "終わりが空なら今日");
  assert.match(V.noticeText(empty), /ニーズ分析実行/);
  assert.equal(V.stateOf("実行中", CONFIG.values), "running");
  assert.equal(V.noticeText({ state: "failed", error: "期間が不正" }), "期間が不正");
});

test("期間と対象で絞る COQL（出典は対象が片方のときだけ）", () => {
  const view = { period: { from: "2026-10-01", to: "2026-10-31" }, targets: "商談とWEB問合せ" };
  const both = V.needsQuery(view, CONFIG, 0);
  assert.match(both, /^select id, Name, Detail, Need_Type, Source_Type, Company_Name, Evidence, Need_Date, Extract_Key from CustomerNeeds where /);
  assert.match(both, /Need_Date >= '2026-10-01' and Need_Date <= '2026-10-31' order by Need_Date desc limit 200 offset 0$/);
  assert.doesNotMatch(both, /Source_Type =/);
  const inq = V.needsQuery({ ...view, targets: "WEB問合せだけ", period: { from: "2026-10-01'--", to: "2026-10-31" } }, CONFIG, 200);
  assert.match(inq, /Source_Type = 'WEB問合せ'/);
  assert.match(inq, /Need_Date >= '2026-10-01--'/, "シングルクォートは入れない");
  assert.match(inq, /offset 200$/);
});

test("ニーズの行：元の記録のモジュールと ID を抽出キーから決める", () => {
  const n = V.toNeed({ id: "1", Name: "x", Need_Type: "品質", Source_Type: "商談", Extract_Key: "meeting:6719392000008501002:1" }, CONFIG);
  assert.equal(n.sourceModule, "MeetingRecords");
  assert.equal(n.sourceId, "6719392000008501002");
  const q = V.toNeed({ id: "2", Extract_Key: "inquiry:123:2" }, CONFIG);
  assert.equal(q.sourceModule, "Inquiry");
  const bad = V.toNeed({ id: "3", Extract_Key: "other:../x:1" }, CONFIG);
  assert.equal(bad.sourceId, "");
});

test("数える・絞り込む", () => {
  const needs = [
    { title: "A", detail: "", type: "品質", source: "商談", company: "X社", evidence: "" },
    { title: "B", detail: "グミ", type: "コスト", source: "WEB問合せ", company: "Y社", evidence: "" },
    { title: "C", detail: "", type: "品質", source: "WEB問合せ", company: "", evidence: "" },
    { title: "D", detail: "", type: "新しい種類", source: "商談", company: "X社", evidence: "" },
  ];
  assert.deepEqual(V.countBy(needs, "type", CONFIG.values.need_types), [
    { label: "品質", count: 2 },
    { label: "コスト", count: 1 },
    { label: "新しい種類", count: 1 },
  ]);
  assert.deepEqual(V.countBy(needs, "company")[0], { label: "X社", count: 2 });
  assert.equal(V.companyCount(needs), 2);
  assert.equal(V.filterNeeds(needs, { type: "品質" }).length, 2);
  assert.equal(V.filterNeeds(needs, { source: "商談", company: "X社" }).length, 2);
  assert.deepEqual(V.filterNeeds(needs, { q: "ぐみ" }).map((n) => n.title), []);
  assert.deepEqual(V.filterNeeds(needs, { q: "グミ" }).map((n) => n.title), ["B"]);
});

test("表示の小物", () => {
  assert.equal(V.stripTest("【TEST】10月"), "10月");
  assert.equal(V.dateLabel("2026-10-01"), "2026/10/01");
  assert.equal(V.dateTimeLabel("2026-10-19T00:06:32+09:00"), "2026/10/19 00:06");
  assert.match(V.sdkErrorText({ data: [{ code: "NO_PERMISSION", message: "denied" }] }), /NO_PERMISSION: denied/);
});
