// 録音アプリ（/app/）の E2E テスト。
// Chromium で画面を動かし、モックの API サーバー（このファイル内）で次を確かめる：
//   ログインしていないときのログイン画面・ログインの失敗の表示 / GPS から訪問先候補 / 顧客検索 /
//   担当者の複数選択と追加 / 新規顧客 / 録音画面への引き継ぎ（下のタブ・日報へのリンク）/ 日報の表示と自動更新
//
//   node tests/e2e/app.e2e.mjs
//   SCREENSHOT_DIR=/tmp/shots node tests/e2e/app.e2e.mjs   # 画面のスクリーンショットも残す
//
// playwright は CI では npm で入れる。ローカルで全体インストールされている場合はその場所から読む。

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
const WEB = path.join(ROOT, "web");
const SHOTS = process.env.SCREENSHOT_DIR || "";

function b64url(obj) {
  return Buffer.from(JSON.stringify(obj)).toString("base64url");
}

function makeToken(recordId) {
  return `${b64url({ v: 1, c: "default", r: recordId, e: Math.floor(Date.now() / 1000) + 3600, t: 1 })}.sig`;
}

const ACCOUNTS = [
  { id: "3001", name: "株式会社サンプル鋳造", address: "神奈川県横浜市中区山下町1-2", reason: "同じ町（山下町）" },
  { id: "3002", name: "港南精機株式会社", address: "神奈川県横浜市中区本町3-4", reason: "同じ区（中区）" },
];
const CONTACTS = {
  3001: [
    { id: "4001", name: "田中 太郎", detail: "製造部・課長" },
    { id: "4002", name: "山本 次郎", detail: "工場長" },
  ],
};

class MockServer {
  constructor() {
    this.loggedIn = true;
    this.visits = [];
    this.detailCalls = 0;
    this.headers = [];
    this.server = http.createServer((req, res) =>
      this.handle(req, res).catch((e) => {
        res.writeHead(500);
        res.end(String(e));
      }),
    );
  }

  listen() {
    return new Promise((resolve) => {
      this.server.listen(0, "127.0.0.1", () => {
        this.origin = `http://127.0.0.1:${this.server.address().port}`;
        resolve();
      });
    });
  }

  close() {
    return new Promise((resolve) => this.server.close(resolve));
  }

  async body(req) {
    const chunks = [];
    for await (const c of req) chunks.push(c);
    const text = Buffer.concat(chunks).toString();
    return text ? JSON.parse(text) : {};
  }

  json(res, status, data) {
    res.writeHead(status, { "Content-Type": "application/json" });
    res.end(JSON.stringify(data));
  }

  async static(res, rel) {
    const file = path.join(WEB, rel);
    if (!file.startsWith(WEB)) return this.json(res, 403, {});
    const type = file.endsWith(".js") ? "text/javascript" : file.endsWith(".css") ? "text/css" : "text/html";
    try {
      const data = await readFile(file);
      res.writeHead(200, { "Content-Type": `${type}; charset=utf-8`, "Cache-Control": "no-cache" });
      return res.end(data);
    } catch {
      return this.json(res, 404, {});
    }
  }

  detail(id) {
    this.detailCalls += 1;
    const base = { id, name: "2026-10-01 株式会社サンプル鋳造 訪問", account: "株式会社サンプル鋳造", contacts: "田中 太郎、山本 次郎" };
    if (this.detailCalls < 2) return { ...base, status: "文字起こし中", state: "processing" };
    return {
      ...base,
      status: "完了",
      state: "done",
      summary: "納期短縮と木型の摩耗について相談を受けた。",
      issues: "・木型の納期が6週間かかる",
      needs: "・納期を3週間に短縮したい",
      budget: "今期300万円",
      decision_maker: "工場長の山本さん",
      competitors: null,
      next_actions: "・見積もりを10月9日までに送る",
      due_date: "2026-10-09",
      error_message: null,
    };
  }

  async handle(req, res) {
    const url = new URL(req.url, this.origin);
    const p = url.pathname;
    if (p === "/app/" || p.startsWith("/app/")) return this.static(res, `app/${p.replace(/^\/app\//, "") || "index.html"}`);
    if (p.startsWith("/recorder/")) return this.static(res, `recorder/${p.replace(/^\/recorder\//, "") || "index.html"}`);
    if (p.startsWith("/api/app/")) {
      if (req.method !== "GET") this.headers.push(req.headers["x-requested-with"]);
      if (!this.loggedIn) return this.json(res, 401, { detail: "ログインしてください" });
      if (p === "/api/app/me") {
        return this.json(res, 200, {
          client_id: "default",
          client_name: "テスト",
          user: { id: "9001", name: "金指 営業", email: "sales@example.com" },
          gps_available: true,
          dry_run: false,
        });
      }
      if (p === "/api/app/accounts/nearby") {
        const body = await this.body(req);
        assert.ok(Math.abs(body.lat - 35.4437) < 0.01 && Math.abs(body.lng - 139.638) < 0.01, "位置を本文で送る");
        return this.json(res, 200, { place: "神奈川県横浜市中区山下町", accounts: ACCOUNTS });
      }
      if (p === "/api/app/accounts/search") {
        const body = await this.body(req);
        return this.json(res, 200, { accounts: ACCOUNTS.filter((a) => a.name.includes(body.q)) });
      }
      let m = p.match(/^\/api\/app\/accounts\/(\d+)\/contacts$/);
      if (m) return this.json(res, 200, { contacts: CONTACTS[m[1]] || [] });
      if (p === "/api/app/visits" && req.method === "POST") {
        const body = await this.body(req);
        const id = String(5000 + this.visits.length + 1);
        this.visits.push({ id, body });
        return this.json(res, 200, { record_id: id, recording_url: `/recorder/#t=${makeToken(id)}&app=1`, test: false });
      }
      if (p === "/api/app/visits") {
        return this.json(res, 200, {
          date: "2026-10-01",
          visits: this.visits.map((v) => ({
            id: v.id,
            name: "訪問",
            status: "完了",
            state: "done",
            account: v.body.account_name,
            contacts: v.body.contacts.join("、"),
            start_at: "2026-10-01T10:05:00+09:00",
          })),
        });
      }
      m = p.match(/^\/api\/app\/visits\/(\d+)$/);
      if (m) return this.json(res, 200, this.detail(m[1]));
      return this.json(res, 404, { detail: "not found" });
    }
    if (p === "/auth/logout") {
      this.loggedIn = false;
      res.writeHead(204);
      return res.end();
    }
    const m = p.match(/^\/api\/recordings\/([^/]+)\/(session|events)$/);
    if (m) {
      if (m[2] === "events") {
        res.writeHead(204);
        return res.end();
      }
      return this.json(res, 200, {
        record_id: m[1],
        expires_at: new Date(Date.now() + 3600e3).toISOString(),
        test: true,
        chunk_seconds: 60,
      });
    }
    return this.json(res, 404, { detail: "not found" });
  }
}

async function shot(page, name) {
  if (!SHOTS) return;
  await mkdir(SHOTS, { recursive: true });
  await page.screenshot({ path: path.join(SHOTS, `${name}.png`), fullPage: true });
}

const server = new MockServer();
await server.listen();
const browser = await chromium.launch({
  args: ["--use-fake-device-for-media-stream", "--use-fake-ui-for-media-stream"],
});
const results = [];

async function run(name, fn) {
  const context = await browser.newContext({
    viewport: { width: 400, height: 860 },
    deviceScaleFactor: 2,
    permissions: ["geolocation", "microphone"],
    geolocation: { latitude: 35.4437, longitude: 139.638 },
    locale: "ja-JP",
  });
  const page = await context.newPage();
  const errors = [];
  page.on("pageerror", (e) => errors.push(String(e)));
  try {
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

await run("ログインしていないときはログイン画面（失敗の理由も出す）", async (page) => {
  server.loggedIn = false;
  await page.goto(`${server.origin}/app/?c=default&login_error=not_crm_user`);
  await page.locator("#view-login").waitFor();
  assert.equal(await page.locator("#btn-login").getAttribute("href"), "/auth/login?c=default");
  assert.match(await page.locator("#login-error").textContent(), /この CRM のユーザーではありません/);
  await shot(page, "01-login");
  server.loggedIn = true;
});

await run("GPS の候補 → 担当者を複数選択・追加 → 録音画面へ引き継ぐ", async (page) => {
  await page.goto(`${server.origin}/app/`);
  await page.locator("#tab-input").waitFor();
  assert.equal(await page.locator("#user-name").textContent(), "金指 営業");
  assert.equal(await page.locator("#btn-contacts").isDisabled(), true, "訪問先を選ぶまで担当者は選べない");
  await shot(page, "02-input");
  await page.click("#btn-gps");
  await page.locator("#sel-account option", { hasText: "株式会社サンプル鋳造" }).waitFor({ state: "attached" });
  assert.match(await page.locator("#gps-state").textContent(), /横浜市中区山下町付近/);
  await page.selectOption("#sel-account", "3001");
  await page.click("#btn-contacts");
  await page.locator("#contact-list li", { hasText: "山本 次郎" }).waitFor();
  await page.check("#contact-list li:has-text('田中 太郎') input");
  await page.check("#contact-list li:has-text('山本 次郎') input");
  await page.fill("#extra-contact", "鈴木 一郎");
  await page.click("#btn-extra-add");
  await shot(page, "03-contacts");
  await page.click("#btn-contacts-done");
  assert.match(await page.locator("#selection").textContent(), /株式会社サンプル鋳造.*田中 太郎、山本 次郎、鈴木 一郎/);
  await page.click("#btn-to-record");
  await page.locator("#tab-record").waitFor();
  assert.equal(await page.locator("#rec-contacts").textContent(), "田中 太郎、山本 次郎、鈴木 一郎");
  await shot(page, "04-record");
  await page.click("#btn-open-recorder");
  await page.waitForURL(/\/recorder\/#t=.*&app=1/);
  await page.locator("#app-nav").waitFor();
  const visit = server.visits.at(-1);
  assert.deepEqual(visit.body, {
    account_id: "3001",
    account_name: "株式会社サンプル鋳造",
    new_customer: false,
    contacts: ["田中 太郎", "山本 次郎", "鈴木 一郎"],
    contact_ids: ["4001", "4002"],
  });
  assert.equal(await page.locator("#app-tab-report").getAttribute("href"), `/app/#report=${visit.id}`);
  await page.locator("#view-main").waitFor();
  await shot(page, "05-recorder");
  assert.ok(server.headers.every((h) => h === "meeting-notes-app"), "書き込みには専用のヘッダーを付ける");
});

await run("顧客検索 → 新規顧客を訪問先にする", async (page) => {
  await page.goto(`${server.origin}/app/#input`);
  await page.locator("#tab-input").waitFor();
  await page.fill("#q", "港南");
  await page.click("#btn-search");
  await page.locator("#sel-account option", { hasText: "港南精機株式会社" }).waitFor({ state: "attached" });
  await page.click("#btn-new");
  await page.fill("#new-name", "有限会社みなと鋳物");
  await page.click("#btn-new-ok");
  assert.match(await page.locator("#selection").textContent(), /有限会社みなと鋳物（新規）/);
  await page.click("#btn-contacts");
  await page.fill("#extra-contact", "佐藤 花子");
  await page.click("#btn-extra-add");
  await page.click("#btn-contacts-done");
  await page.click("#btn-to-record");
  await page.click("#btn-open-recorder");
  await page.waitForURL(/\/recorder\//);
  assert.deepEqual(server.visits.at(-1).body, {
    account_id: null,
    account_name: "有限会社みなと鋳物",
    new_customer: true,
    contacts: ["佐藤 花子"],
    contact_ids: [],
  });
});

await run("日報：処理中から完了に自動で切り替わる", async (page) => {
  server.detailCalls = 0;
  await page.clock.install();
  await page.goto(`${server.origin}/app/#report=5001`);
  await page.locator("#report-detail").waitFor();
  await page.locator("#rd-progress").waitFor();
  await page.clock.fastForward(16000);
  await page.locator("#rd-body h3", { hasText: "要約" }).waitFor();
  assert.equal(await page.locator("#rd-progress").isHidden(), true);
  assert.match(await page.locator("#rd-body").textContent(), /工場長の山本さん/);
  assert.equal(await page.locator("#rd-body h3", { hasText: "競合" }).count(), 0, "空の項目は出さない");
  await page.locator("#visit-list li").first().waitFor();
  await shot(page, "06-report");
});

await browser.close();
await server.close();
console.log(results.join("\n"));
