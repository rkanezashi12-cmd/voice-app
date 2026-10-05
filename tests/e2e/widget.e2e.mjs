// 商談日報ウィジェット（widgets/meeting-report）の E2E テスト。
// Zoho の SDK の代わりに偽物（このファイル内）を置き、Chromium で次を確かめる：
//   日報の表示（空の項目は出さない・取引先を開く）/ 文字起こしの話者ごとの表示と検索 / 失敗・処理中の説明 /
//   商談記録以外の画面に置かれたとき / SDK のエラーの表示 / 関連リストの高さ合わせ
//
//   node tests/e2e/widget.e2e.mjs
//   SCREENSHOT_DIR=/tmp/shots node tests/e2e/widget.e2e.mjs   # 画面のスクリーンショットも残す

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
const APP = path.join(ROOT, "widgets", "meeting-report", "app");
const SHOTS = process.env.SCREENSHOT_DIR || "";

// scripts/build_widget.py が作る field-map.js と同じ形（既定の API 名）
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

const RECORDS = {
  "4928352000069211999": {
    id: "4928352000069211999",
    Name: "2026-10-02 株式会社アイシン 訪問",
    Status: "完了",
    Meeting_Type: "対面",
    Capture_Method: "対面録音",
    Account: { name: "株式会社アイシン", id: "4928352000000412001" },
    Owner: { name: "マルサン木型 OFFICE", id: "1" },
    Contact_Name: "小林 克彰、山田 竜成",
    Start_At: "2026-10-02T23:55:00+09:00",
    Summary: "株式会社アイシンより、鋳造に関するご相談をいただいた。",
    Needs: "・木型製作から機械加工まで対応できること",
    Budget: "8000万",
    Competitors: null,
    Next_Actions: "・見積もりを送る（期限 2026-10-10）",
    Due_Date: "2026-10-10",
    Transcript: "話者A: 中子の抜き勾配について相談です\n話者B: 中子は木口側で\n話者A: 承知しました",
    Transcript_2: "話者B: では中子の図面を送ります",
  },
  "4928352000069212000": {
    id: "4928352000069212000",
    Name: "2026-10-02 株式会社アイシン 訪問",
    Status: "失敗",
    Error_Message: "文字起こし結果が空でした（音声が無音の可能性があります）",
  },
  "4928352000069212001": { id: "4928352000069212001", Name: "オンライン商談", Status: "文字起こし中" },
};

// 偽の Zoho SDK：PageLoad に渡す値と getRecord の結果を window.__fake で決める。呼び出しを記録する
const FAKE_SDK = `
(function () {
  const fake = window.__fake || {};
  window.__calls = { getRecord: [], resize: [], open: [] };
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
      },
      UI: {
        // 本物は iframe の高さを変える。ここでは document の高さを広げて、同じ測り方の違いが出るようにする
        Resize(args) {
          window.__calls.resize.push(args);
          if (fake.resizeError) return Promise.reject(fake.resizeError);
          document.documentElement.style.minHeight = args.height + "px";
          return Promise.resolve(true);
        },
        Record: { open(args) { window.__calls.open.push(args); return Promise.resolve(true); } },
      },
    },
  };
})();
`;

function serve() {
  const server = http.createServer(async (req, res) => {
    const url = new URL(req.url, "http://x");
    const name = url.pathname.replace(/^\/app\//, "");
    const send = (type, body) => {
      res.writeHead(200, { "Content-Type": `${type}; charset=utf-8`, "Cache-Control": "no-cache" });
      res.end(body);
    };
    if (name === "ZohoEmbededAppSDK.min.js") return send("text/javascript", FAKE_SDK);
    if (name === "field-map.js") return send("text/javascript", `window.MEETING_REPORT_CONFIG = ${JSON.stringify(CONFIG)};`);
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
  await page.screenshot({ path: path.join(SHOTS, `widget-${name}.png`), fullPage: true });
}

const { server, origin } = await serve();
const browser = await chromium.launch();
const results = [];

async function run(name, fake, fn) {
  const context = await browser.newContext({ viewport: { width: 1100, height: 900 }, locale: "ja-JP" });
  await context.addInitScript((f) => {
    window.__fake = f;
  }, fake);
  const page = await context.newPage();
  const errors = [];
  page.on("pageerror", (e) => errors.push(String(e)));
  try {
    await page.goto(`${origin}/app/index.html`);
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

const DONE = { pageLoad: { Entity: "MeetingRecords", EntityId: "4928352000069211999" }, records: RECORDS };

await run("日報を見やすく出す（空の項目は出さない・取引先を開ける）", DONE, async (page) => {
  await page.locator("#report").waitFor();
  assert.equal(await page.locator("#r-name").textContent(), "2026-10-02 株式会社アイシン 訪問");
  assert.equal(await page.locator("#r-state").textContent(), "完了");
  assert.match(await page.locator("#r-state").getAttribute("class"), /done/);
  assert.equal(await page.locator("#r-start").textContent(), "2026/10/02 23:55");
  assert.equal(await page.locator("#r-type").textContent(), "対面・対面録音");
  assert.equal(await page.locator("#r-contacts").textContent(), "小林 克彰、山田 竜成");
  assert.equal(await page.locator("#r-category").textContent(), "-");
  assert.deepEqual(await page.locator("#r-sections h2").allTextContents(), [
    "要約",
    "ニーズ",
    "予算",
    "次のアクション",
    "次回期限",
  ]);
  assert.equal(await page.locator("#r-notice").isHidden(), true);
  const calls = await page.evaluate(() => window.__calls);
  assert.deepEqual(calls.getRecord, [{ Entity: "MeetingRecords", RecordID: "4928352000069211999" }]);
  assert.ok(calls.resize.length > 0, "関連リストの高さを中身に合わせる");
  await page.click("#r-account");
  const opened = await page.evaluate(() => window.__calls.open);
  assert.deepEqual(opened, [{ Entity: "Accounts", RecordID: "4928352000000412001" }]);
  await shot(page, "01-report");
});

await run("文字起こしを話者ごとに出し、言葉で検索できる", DONE, async (page) => {
  await page.locator("#report").waitFor();
  assert.deepEqual(await page.locator("#r-transcript .speaker").allTextContents(), ["話者A", "話者B", "話者A", "話者B"]);
  assert.match(await page.locator("#r-transcript li").first().getAttribute("class"), /spk-0/);
  assert.match(await page.locator("#r-transcript li").nth(1).getAttribute("class"), /spk-1/);
  await page.fill("#q", "中子");
  assert.equal(await page.locator("#r-transcript mark").count(), 3, "2つ目の項目に入った続きも検索する");
  assert.equal(await page.locator("#q-count").textContent(), "3件");
  await page.press("#q", "Enter");
  assert.equal(await page.locator("#q-count").textContent(), "1 / 3件");
  await page.click("#q-next");
  await page.click("#q-next");
  assert.equal(await page.locator("#q-count").textContent(), "3 / 3件");
  assert.equal(await page.locator("mark.current").count(), 1);
  await page.click("#q-next");
  assert.equal(await page.locator("#q-count").textContent(), "1 / 3件", "最後の次は最初に戻る");
  await shot(page, "02-search");
  await page.fill("#q", "");
  assert.equal(await page.locator("#r-transcript mark").count(), 0);
  assert.equal(await page.locator("#q-next").isDisabled(), true);
});

await run("失敗した記録はエラー内容を出す", { ...DONE, pageLoad: { Entity: "MeetingRecords", EntityId: ["4928352000069212000"] } }, async (page) => {
  await page.locator("#report").waitFor();
  assert.match(await page.locator("#r-state").getAttribute("class"), /failed/);
  assert.match(await page.locator("#r-notice").textContent(), /文字起こし結果が空でした/);
  assert.equal(await page.locator("#r-sections .card").count(), 0);
  assert.equal(await page.locator("#r-transcript-empty").isVisible(), true);
  await shot(page, "03-failed");
});

await run("処理中は待つよう案内し、再読み込みで読み直す", { ...DONE, pageLoad: { Entity: "MeetingRecords", EntityId: "4928352000069212001" } }, async (page) => {
  await page.locator("#report").waitFor();
  assert.match(await page.locator("#r-notice").textContent(), /文字起こし・要約の途中/);
  const before = await page.evaluate(() => window.__calls.resize.at(-1).height);
  await page.click("#btn-reload");
  await page.waitForFunction(() => window.__calls.getRecord.length === 2);
  await page.click("#btn-reload");
  await page.waitForFunction(() => window.__calls.getRecord.length === 3);
  const after = await page.evaluate(() => window.__calls.resize.at(-1).height);
  assert.equal(after, before, "読み直しても枠の高さが伸び続けない");
});

await run("高さ合わせを断られても日報は出す", { ...DONE, resizeError: { data: [{ code: "NOT_SUPPORTED" }] } }, async (page) => {
  await page.locator("#report").waitFor();
  assert.equal(await page.locator("#r-name").textContent(), "2026-10-02 株式会社アイシン 訪問");
  await page.waitForTimeout(100);
});

await run("商談記録以外の画面に置かれたときは読まない", { ...DONE, pageLoad: { Entity: "Accounts", EntityId: "4928352000000412001" } }, async (page) => {
  await page.locator("#message").waitFor();
  assert.match(await page.locator("#message").textContent(), /「商談記録」の画面で使います/);
  assert.deepEqual(await page.evaluate(() => window.__calls.getRecord), []);
  assert.equal(await page.locator("#report").isHidden(), true);
});

await run("SDK のエラーは中身（code・message）まで出す", { ...DONE, error: { data: [{ code: "NO_PERMISSION", message: "permission denied" }] } }, async (page) => {
  await page.locator("#message.error").waitFor();
  assert.match(await page.locator("#message").textContent(), /NO_PERMISSION: permission denied/);
});

await browser.close();
server.close();
console.log(results.join("\n"));
