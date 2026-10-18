// ニーズ分析ウィジェット（widgets/needs-analysis）の E2E テスト。
// Zoho の SDK の代わりに偽物（このファイル内）を置き、Chromium で次を確かめる：
//   件数・グラフ・カードの表示 / 棒を押すと一覧を絞り込む / 元の記録・ニーズを開く / COQL のページ送り /
//   分析前・失敗の説明 / ニーズ分析以外の画面に置かれたとき / SDK のエラーの表示
//
//   node tests/e2e/widget-needs.e2e.mjs
//   SCREENSHOT_DIR=/tmp/shots node tests/e2e/widget-needs.e2e.mjs   # 画面のスクリーンショットも残す

import assert from "node:assert/strict";
import { mkdir, readFile } from "node:fs/promises";
import http from "node:http";
import { createRequire } from "node:module";
import path from "node:path";
import { fileURLToPath } from "node:url";

const require = createRequire(import.meta.url);
function loadPlaywright() {
  for (const c of ["playwright", "/opt/node22/lib/node_modules/playwright"]) {
    try {
      return require(c);
    } catch {
      // 次の候補
    }
  }
  throw new Error("playwright が見つかりません（npm install --no-save playwright）");
}
const { chromium } = loadPlaywright();

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../..");
const APP = path.join(ROOT, "widgets", "needs-analysis", "app");
const SHOTS = process.env.SCREENSHOT_DIR || "";

// scripts/build_widget.py が作る field-map.js と同じ形（既定の API 名）
const CONFIG = {
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

const DONE_ID = "6719392000008507050";
const RECORDS = {
  [DONE_ID]: {
    id: DONE_ID,
    Name: "【TEST】10月",
    Analysis_Status: "完了",
    Period_From: "2026-10-01",
    Period_To: "2026-10-31",
    Targets: null,
    Counts: "商談 32件・WEB問合せ 30件 → ニーズ 95件（今回新しく抜き出した 95件）",
    Analyzed_At: "2026-10-19T00:06:32+09:00",
    Top_Needs: [
      "1. タッチパネルによる条件管理と操作簡易化（10件・6社）\n溶接条件の設定が熟練者の勘に依存している。\n例：株式会社サファイア・システム「誰でも扱える操作画面」、株式会社ゼウス・ホールディングス「条件の一元管理」",
      "2. 自動供給・位置決めによる省人化ライン（6件・4社）\n人手不足で夜勤が組めない。\n例：株式会社あけぼのソリューション「リング溶接の自動供給」",
      "3. 溶接データの自動記録とトレーサビリティ（5件・3社）\n取引先から記録を求められている。",
      "4. 溶接部良否の自動判定と検査効率化（3件・2社）\n目視検査で見落としがある。",
    ].join("\n\n"),
    Matched_Needs:
      "■ 溶接データの自動記録とトレーサビリティ × 溶接データ記録ユニット WeldLog\n理由：既存機に後付けできる\n提案：監査向けの帳票まで含めて提案する\n求める声：5件・3社（株式会社あさひ印刷、株式会社ポセイドン・リゾート、株式会社信州スプリング）",
    Product_Ideas:
      "■ 溶接部良否の自動判定と検査効率化\n方向性：カメラと電流波形で良否を判定するユニット\n理由：目視の見落としでクレームが出ている\n土台にできる自社技術：溶接データ記録ユニット WeldLog\n求める声：3件・2社（株式会社アルタイル・ソフトウェア、株式会社山口チェーン）",
  },
  "6719392000008507051": { id: "6719392000008507051", Name: "11月", Analysis_Status: "未実行" },
  "6719392000008507052": { id: "6719392000008507052", Name: "失敗", Analysis_Status: "失敗", Error_Message: "ニーズ分析に失敗しました: 期間（開始）が期間（終了）より後になっています" },
};

// ニーズ 230件（COQL は 200件ずつなので、2ページに分かれる）
const TYPES = ["機能・性能", "品質", "コスト", "納期", "使いやすさ", "サービス"];
const COMPANIES = ["株式会社あさひ印刷", "株式会社ゼウス・ホールディングス", "北海道スチール株式会社", "株式会社山口チェーン", "合同会社びわこフーズ"];
const NEEDS = Array.from({ length: 230 }, (_, i) => ({
  id: `67193920000085${24000 + i}`, // 19桁は数値にすると桁が落ちるので文字で作る
  Name: `【TEST】ニーズ${i + 1}`,
  Detail: i === 0 ? "粘着性のあるグミを正確に量りたい" : `内容${i + 1}`,
  Need_Type: TYPES[i % 4 === 0 ? 0 : i % TYPES.length],
  Source_Type: i % 3 === 0 ? "商談" : "WEB問合せ",
  Company_Name: COMPANIES[i % COMPANIES.length],
  Need_Date: `2026-10-${String((i % 28) + 1).padStart(2, "0")}`,
  Extract_Key: i % 3 === 0 ? `meeting:67193920000085010${String(i % 90).padStart(2, "0")}:1` : `inquiry:67193920000085190${String(i % 90).padStart(2, "0")}:1`,
}));

// 偽の Zoho SDK：PageLoad に渡す値と getRecord・coql の結果を window.__fake で決める。呼び出しを記録する
const FAKE_SDK = `
(function () {
  const fake = window.__fake || {};
  window.__calls = { getRecord: [], coql: [], resize: [], open: [] };
  let onLoad = null;
  window.ZOHO = {
    embeddedApp: {
      on(name, fn) { if (name === "PageLoad") onLoad = fn; },
      init() { setTimeout(() => onLoad && onLoad(fake.pageLoad), 0); return Promise.resolve(); },
    },
    CRM: {
      API: {
        getRecord(args) {
          window.__calls.getRecord.push(args);
          if (fake.error) return Promise.reject(fake.error);
          const rec = fake.records[args.RecordID];
          return Promise.resolve(rec ? { data: [rec] } : { data: [] });
        },
        coql(args) {
          window.__calls.coql.push(args.select_query);
          const m = args.select_query.match(/offset (\\d+)$/);
          const offset = Number(m[1]);
          const rows = fake.needs.slice(offset, offset + 200);
          if (!rows.length) return Promise.resolve(undefined);
          return Promise.resolve({ data: rows, info: { more_records: offset + 200 < fake.needs.length } });
        },
      },
      UI: {
        Resize(args) { window.__calls.resize.push(args); return Promise.resolve(true); },
        Record: { open(args) { window.__calls.open.push(args); return Promise.resolve(true); } },
      },
    },
  };
})();
`;

function serve() {
  const server = http.createServer(async (req, res) => {
    const url = new URL(req.url, "http://x");
    const name = url.pathname.replace(/^\//, "");
    const send = (type, body) => {
      res.writeHead(200, { "Content-Type": `${type}; charset=utf-8`, "Cache-Control": "no-cache" });
      res.end(body);
    };
    if (name === "ZohoEmbededAppSDK.min.js") return send("text/javascript", FAKE_SDK);
    if (name === "field-map.js") return send("text/javascript", `window.NEEDS_ANALYSIS_CONFIG = ${JSON.stringify(CONFIG)};`);
    const file = path.join(APP, name);
    if (!file.startsWith(APP)) {
      res.writeHead(403);
      return res.end();
    }
    try {
      const data = await readFile(file);
      const type = file.endsWith(".js") ? "text/javascript" : file.endsWith(".css") ? "text/css" : "text/html";
      return send(type, data);
    } catch {
      res.writeHead(404);
      return res.end();
    }
  });
  return new Promise((resolve) =>
    server.listen(0, "127.0.0.1", () => resolve({ server, origin: `http://127.0.0.1:${server.address().port}` })),
  );
}

async function shot(page, name) {
  if (!SHOTS) return;
  await mkdir(SHOTS, { recursive: true });
  await page.screenshot({ path: path.join(SHOTS, `needs-${name}.png`), fullPage: true });
}

const { server, origin } = await serve();
const browser = await chromium.launch();
const results = [];

async function run(name, fake, fn, viewport = { width: 1100, height: 900 }) {
  const context = await browser.newContext({ viewport, locale: "ja-JP" });
  await context.addInitScript((f) => {
    window.__fake = f;
  }, fake);
  const page = await context.newPage();
  const errors = [];
  page.on("pageerror", (e) => errors.push(String(e)));
  try {
    await page.goto(`${origin}/index.html`);
    await fn(page);
    assert.deepEqual(errors, [], "ページのエラーが無い");
    results.push(`ok - ${name}`);
  } catch (err) {
    results.push(`NG - ${name}: ${err.message}`);
    process.exitCode = 1;
  } finally {
    await context.close();
  }
}

const DONE = { pageLoad: { Entity: "NeedsAnalyses", EntityId: DONE_ID }, records: RECORDS, needs: NEEDS };

await run("件数・グラフ・カードを出す", DONE, async (page) => {
  await page.locator("#report").waitFor();
  assert.equal(await page.locator("#r-name").textContent(), "10月", "【TEST】は付けずに出す");
  assert.equal(await page.locator("#r-state").textContent(), "完了");
  assert.equal(await page.locator("#r-period").textContent(), "2026/10/01 〜 2026/10/31");
  assert.equal(await page.locator("#r-targets").textContent(), "商談とWEB問合せ");
  assert.equal(await page.locator("#k-meetings").textContent(), "32件");
  assert.equal(await page.locator("#k-needs").textContent(), "230件", "2ページ目まで読む");
  assert.equal(await page.locator("#k-companies").textContent(), "5社");
  const coql = await page.evaluate(() => window.__calls.coql);
  assert.equal(coql.length, 2);
  assert.match(coql[0], /from CustomerNeeds where Need_Date >= '2026-10-01' and Need_Date <= '2026-10-31'/);
  assert.equal(await page.locator("#c-themes li").count(), 4);
  assert.match(await page.locator("#c-themes li").first().textContent(), /タッチパネル.*10件・6社/);
  assert.deepEqual(
    await page.locator("#c-types .bar-label").allTextContents(),
    ["機能・性能", "品質", "コスト", "納期", "使いやすさ", "サービス"],
    "種類は決まった順",
  );
  assert.equal(await page.locator("#c-sources .stack button").count(), 2);
  assert.match(await page.locator("#c-sources .legend").textContent(), /商談 77件（33%）.*WEB問合せ 153件（67%）/);
  assert.equal(await page.locator("#c-companies .bar-row").count(), 5);
  assert.match(await page.locator("#r-matched .card h3").textContent(), /WeldLog/);
  assert.match(await page.locator("#r-ideas .card").textContent(), /土台にできる自社技術溶接データ記録ユニット WeldLog/);
  assert.equal(await page.locator("#l-body tr").count(), 230);
  assert.equal(await page.locator("#l-count").textContent(), "230件");
  assert.equal(await page.locator("#r-notice").isHidden(), true);
  await shot(page, "01-overview");
  await page.locator("#c-themes summary").first().click();
  assert.equal(await page.locator("#c-themes .theme-body").first().isVisible(), true, "押すと説明が開く");
  await page.locator("#c-types .bar-row").first().hover();
  assert.match(await page.locator("#tip").textContent(), /機能・性能\n\d+件（全体の\d+%）/);
});

await run("棒を押すと一覧を絞り込み、もう一度押すと外す", DONE, async (page) => {
  await page.locator("#report").waitFor();
  await page.locator("#c-types .bar-row", { hasText: "品質" }).click();
  const n = await page.locator("#l-body tr").count();
  assert.ok(n > 0 && n < 230);
  assert.equal(await page.locator("#f-type").inputValue(), "品質", "選択欄も合わせる");
  assert.match(await page.locator("#l-count").textContent(), /件（全230件）/);
  await page.locator("#c-sources .stack button.s1").click();
  assert.equal(await page.locator("#f-source").inputValue(), "商談");
  assert.ok((await page.locator("#l-body tr").count()) <= n);
  await shot(page, "02-filtered");
  await page.locator("#c-types .bar-row", { hasText: "品質" }).click();
  assert.equal(await page.locator("#f-type").inputValue(), "");
  await page.fill("#f-q", "グミ");
  assert.equal(await page.locator("#l-body tr").count(), 1);
  await page.click("#f-clear");
  assert.equal(await page.locator("#l-body tr").count(), 230);
});

await run("ニーズと元の記録を開く", DONE, async (page) => {
  await page.locator("#report").waitFor();
  const first = page.locator("#l-body tr").first();
  await first.locator("button.link").first().click();
  await first.locator("td").last().locator("button").click();
  const opened = await page.evaluate(() => window.__calls.open);
  assert.deepEqual(opened[0], { Entity: "CustomerNeeds", RecordID: NEEDS[0].id });
  assert.deepEqual(opened[1], { Entity: "MeetingRecords", RecordID: "6719392000008501000" });
});

await run("分析前は押すボタンを案内する", { ...DONE, pageLoad: { Entity: "NeedsAnalyses", EntityId: "6719392000008507051" }, needs: [] }, async (page) => {
  await page.locator("#report").waitFor();
  assert.match(await page.locator("#r-notice").textContent(), /ニーズ分析実行/);
  assert.equal(await page.locator("#k-needs").textContent(), "0件");
  assert.match(await page.locator("#r-matched").textContent(), /まだありません/);
  assert.equal(await page.locator("#l-empty").isVisible(), true);
  await shot(page, "03-idle");
});

await run("失敗はエラー内容を出す", { ...DONE, pageLoad: { Entity: "NeedsAnalyses", EntityId: ["6719392000008507052"] }, needs: [] }, async (page) => {
  await page.locator("#report").waitFor();
  assert.match(await page.locator("#r-state").getAttribute("class"), /failed/);
  assert.match(await page.locator("#r-notice").textContent(), /期間（開始）が期間（終了）より後/);
});

await run("ニーズ分析以外の画面に置かれたときは読まない", { ...DONE, pageLoad: { Entity: "MeetingRecords", EntityId: DONE_ID } }, async (page) => {
  await page.locator("#message").waitFor();
  assert.match(await page.locator("#message").textContent(), /「ニーズ分析」の画面で使います/);
  assert.deepEqual(await page.evaluate(() => window.__calls.getRecord), []);
});

await run("SDK のエラーは中身（code・message）まで出す", { ...DONE, error: { data: [{ code: "NO_PERMISSION", message: "permission denied" }] } }, async (page) => {
  await page.locator("#message.error").waitFor();
  assert.match(await page.locator("#message").textContent(), /NO_PERMISSION: permission denied/);
});

await run("狭い幅でも横にはみ出さない", DONE, async (page) => {
  await page.locator("#report").waitFor();
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
  assert.ok(overflow <= 0, `横にはみ出していない（${overflow}px）`);
  await shot(page, "04-narrow");
}, { width: 600, height: 900 });

await browser.close();
server.close();
console.log(results.join("\n"));
