// 録音ページの E2E テスト。
// Chromium の擬似マイクで録音ページを動かし、モックの API サーバー（このファイル内）で
// 分割アップロード・停止検知と再開・送信失敗からの再送・再読み込み後の再送・完了通知を確かめる。
//
//   node tests/e2e/recorder.e2e.mjs
//
// playwright は CI では npm で入れる。ローカルで全体インストールされている場合は NODE_PATH を通す。

import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import http from "node:http";
import { createRequire } from "node:module";
import path from "node:path";
import { fileURLToPath } from "node:url";

const require = createRequire(import.meta.url);
function loadPlaywright() {
  const candidates = ["playwright", "/opt/node22/lib/node_modules/playwright"];
  for (const c of candidates) {
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
const RECORDER = path.join(ROOT, "web/recorder");
const EBML = Buffer.from([0x1a, 0x45, 0xdf, 0xa3]);

// ---- モック API ----

function b64url(obj) {
  return Buffer.from(JSON.stringify(obj)).toString("base64url");
}

function makeToken(recordId) {
  return `${b64url({ v: 1, c: "default", r: recordId, e: Math.floor(Date.now() / 1000) + 3600, t: 1 })}.sig`;
}

class MockServer {
  constructor() {
    this.uploads = new Map(); // "<session>/<seq>" -> Buffer
    this.events = [];
    this.completes = [];
    this.failPuts = 0;
    this.rejectPuts = false;
    this.sessionError = null; // { status, detail }：/session を失敗させる
    this.server = http.createServer((req, res) => this.handle(req, res).catch((e) => {
      res.writeHead(500);
      res.end(String(e));
    }));
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

  reset() {
    this.uploads.clear();
    this.events = [];
    this.completes = [];
    this.failPuts = 0;
    this.rejectPuts = false;
    this.sessionError = null;
  }

  async body(req) {
    const chunks = [];
    for await (const c of req) chunks.push(c);
    return Buffer.concat(chunks);
  }

  json(res, status, data) {
    res.writeHead(status, { "Content-Type": "application/json" });
    res.end(JSON.stringify(data));
  }

  async handle(req, res) {
    const url = new URL(req.url, this.origin);
    if (url.pathname.startsWith("/recorder/")) {
      const rel = url.pathname.replace(/^\/recorder\//, "") || "index.html";
      const file = path.join(RECORDER, rel);
      if (!file.startsWith(RECORDER)) return this.json(res, 403, {});
      const type = file.endsWith(".js") ? "text/javascript" : file.endsWith(".css") ? "text/css" : "text/html";
      try {
        const data = await readFile(file);
        res.writeHead(200, { "Content-Type": `${type}; charset=utf-8`, "Cache-Control": "no-cache" });
        return res.end(data);
      } catch {
        return this.json(res, 404, {});
      }
    }
    let m = url.pathname.match(/^\/api\/recordings\/([^/]+)\/(session|upload-url|events|complete)$/);
    if (m) {
      const [, recordId, action] = m;
      if (!(req.headers.authorization || "").startsWith("Bearer ")) return this.json(res, 401, { detail: "no auth" });
      if (action === "session") {
        if (this.sessionError) return this.json(res, this.sessionError.status, { detail: this.sessionError.detail });
        return this.json(res, 200, {
          record_id: recordId,
          expires_at: new Date(Date.now() + 3600e3).toISOString(),
          test: true,
          chunk_seconds: 60,
        });
      }
      const body = JSON.parse((await this.body(req)).toString() || "{}");
      if (action === "upload-url") {
        const mime = body.mime_type.split(";")[0];
        return this.json(res, 200, {
          url: `${this.origin}/upload/${body.session_id}/${body.seq}`,
          headers: { "Content-Type": mime },
          expires_in: 900,
        });
      }
      if (action === "events") {
        this.events.push(...body.events);
        res.writeHead(204);
        return res.end();
      }
      if (action === "complete") {
        this.completes.push(body);
        const missing = [];
        for (const s of body.sessions) {
          for (let i = 0; i < s.chunks; i++) if (!this.uploads.has(`${s.session_id}/${i}`)) missing.push(`${s.session_id}/${i}`);
        }
        if (missing.length && !body.allow_missing) {
          return this.json(res, 409, { detail: { message: "missing", missing, missing_count: missing.length } });
        }
        return this.json(res, 200, { status: "test_completed", chunks: this.uploads.size, missing: missing.length });
      }
    }
    m = url.pathname.match(/^\/upload\/([^/]+)\/(\d+)$/);
    if (m && req.method === "PUT") {
      const data = await this.body(req);
      if (this.rejectPuts) return this.json(res, 503, {});
      if (this.failPuts > 0) {
        this.failPuts -= 1;
        return this.json(res, 500, {});
      }
      this.uploads.set(`${m[1]}/${m[2]}`, data);
      res.writeHead(200);
      return res.end();
    }
    return this.json(res, 404, {});
  }

  sessions() {
    const map = new Map();
    for (const [key, data] of this.uploads) {
      const [session, seq] = key.split("/");
      if (!map.has(session)) map.set(session, []);
      map.get(session).push({ seq: Number(seq), data });
    }
    for (const list of map.values()) list.sort((a, b) => a.seq - b.seq);
    return map;
  }
}

// ---- ヘルパー ----

async function waitFor(fn, { timeout = 20000, interval = 100, message = "条件を満たしませんでした" } = {}) {
  const deadline = Date.now() + timeout;
  let last;
  while (Date.now() < deadline) {
    last = await fn();
    if (last) return last;
    await new Promise((r) => setTimeout(r, interval));
  }
  throw new Error(`${message}（最後の値: ${JSON.stringify(last)}）`);
}

async function openPage(context, origin, recordId, { mode = "a", chunkSeconds = 2 } = {}) {
  const page = await context.newPage();
  page.on("dialog", (d) => d.accept());
  page.on("pageerror", (e) => console.error("pageerror:", e.message));
  await page.goto(`${origin}/recorder/#${makeToken(recordId)}`);
  await page.waitForSelector("#test-panel:not([hidden])");
  await page.evaluate(
    ({ mode, chunkSeconds }) => {
      localStorage.setItem("recorder.settings", JSON.stringify({ mode, chunkSeconds, levelMonitor: true }));
    },
    { mode, chunkSeconds },
  );
  await page.reload();
  await page.waitForSelector("#btn-start:not([hidden])");
  return page;
}

const snapshot = (page) => page.evaluate(() => window.__recorderDebug.snapshot());

// ---- テスト ----

const tests = [];
const it = (name, fn) => tests.push({ name, fn });

it("方式a: 2秒ごとに単体で再生できるチャンクを送り、完了を通知する", async ({ context, server }) => {
  const page = await openPage(context, server.origin, "rec-a");
  await page.click("#btn-start");
  await waitFor(async () => (await snapshot(page)).phase === "recording", { message: "録音が始まらない" });
  assert.equal(await page.isVisible("#rec-bar"), true, "録音中の表示が出る");
  await waitFor(() => server.uploads.size >= 3, { message: "3チャンク届かない" });
  await page.click("#btn-finish");
  await waitFor(() => server.completes.length === 1, { message: "complete が呼ばれない" });
  await page.waitForSelector("#view-done:not([hidden])");
  const complete = server.completes[0];
  assert.equal(complete.sessions.length, 1);
  assert.equal(complete.sessions[0].chunks, server.uploads.size);
  assert.match(complete.sessions[0].session_id, /^\d{13}-a-[a-z0-9]{4,12}$/);
  for (const [key, data] of server.uploads) {
    assert.ok(data.length > 100, `${key} が小さすぎる`);
    assert.ok(data.subarray(0, 4).equals(EBML), `${key} が単体の webm になっていない`);
  }
  const types = new Set(server.events.map((e) => e.type));
  for (const t of ["page_ready", "start", "chunk", "finish_requested"]) assert.ok(types.has(t), `診断イベント ${t} が無い`);
  await page.close();
});

it("方式b: 先頭チャンクだけがヘッダーを持つ（連結して1本になる）", async ({ context, server }) => {
  const page = await openPage(context, server.origin, "rec-b", { mode: "b" });
  await page.click("#btn-start");
  await waitFor(() => server.uploads.size >= 3, { message: "3チャンク届かない" });
  await page.click("#btn-finish");
  await waitFor(() => server.completes.length === 1);
  const [list] = [...server.sessions().values()];
  assert.ok(list[0].data.subarray(0, 4).equals(EBML), "先頭はヘッダーあり");
  assert.ok(!list[1].data.subarray(0, 4).equals(EBML), "2つ目以降はヘッダーなし");
  await page.close();
});

it("マイクが止まったら警告を出し、再開で同じ記録に別区間として追記する", async ({ context, server }) => {
  const page = await openPage(context, server.origin, "rec-resume");
  await page.click("#btn-start");
  await waitFor(() => server.uploads.size >= 1);
  await page.evaluate(() => window.__recorderDebug.stopTrack());
  await page.waitForSelector("#overlay:not([hidden])", { timeout: 15000 });
  const reason = await page.textContent("#overlay-reason");
  assert.ok(reason.length > 0, "止まった理由を表示する");
  assert.equal((await snapshot(page)).phase, "interrupted");
  await page.click("#btn-resume");
  await waitFor(async () => (await snapshot(page)).phase === "recording", { message: "再開しない" });
  await waitFor(async () => {
    const s = await snapshot(page);
    return s.sessions.length === 2 && s.sessions[1].chunks >= 2;
  }, { message: "再開後のチャンクが出ない" });
  await page.click("#btn-finish");
  await waitFor(() => server.completes.length === 1);
  assert.equal(server.completes[0].sessions.length, 2, "2区間として完了を通知する");
  assert.ok(server.events.some((e) => e.type === "interrupted"), "中断を記録する");
  await page.close();
});

it("録音終了の処理中に健全性チェックが走っても「録音が止まりました」を出さない", async ({ context, server }) => {
  const page = await openPage(context, server.origin, "rec-finish-race");
  await page.click("#btn-start");
  await waitFor(() => server.uploads.size >= 1);
  // 実機（iPhone Chrome）では、終了処理でマイクを止めた直後の健全性チェックが中断と誤判定した
  await page.evaluate(() => setInterval(() => window.__recorderDebug.healthCheck(), 1));
  await page.click("#btn-finish");
  await waitFor(() => server.completes.length === 1);
  await page.waitForSelector("#view-done:not([hidden])");
  await new Promise((r) => setTimeout(r, 300));
  assert.equal(await page.isVisible("#overlay"), false, "完了後に警告画面が出ている");
  assert.equal((await snapshot(page)).phase, "done");
  await waitFor(() => server.events.some((e) => e.type === "completed"), { message: "completed が届かない" });
  assert.ok(!server.events.some((e) => e.type === "interrupted"), "終了処理を中断と記録している");
  await page.close();
});

it("送信に失敗しても再送し、最終的に全部届く", async ({ context, server }) => {
  server.failPuts = 3;
  const page = await openPage(context, server.origin, "rec-retry");
  await page.click("#btn-start");
  await waitFor(async () => (await snapshot(page)).sessions[0]?.chunks >= 3, { timeout: 30000 });
  await page.click("#btn-finish");
  await waitFor(() => server.completes.length === 1, { timeout: 60000, message: "再送後に完了しない" });
  const s = await snapshot(page);
  assert.ok(s.uploadStats.failedAttempts >= 3, "失敗を数えている");
  assert.equal(server.completes[0].sessions[0].chunks, server.uploads.size);
  await page.close();
});

it("未送信のまま再読み込みしても、端末に残った音声を送って完了できる", async ({ context, server }) => {
  server.rejectPuts = true;
  const page = await openPage(context, server.origin, "rec-reload");
  await page.click("#btn-start");
  await waitFor(async () => (await snapshot(page)).uploadStats.pending >= 2, { message: "未送信が溜まらない" });
  await page.evaluate(() => window.__recorderDebug.stopTrack());
  await page.waitForSelector("#overlay:not([hidden])", { timeout: 15000 });
  assert.equal(server.uploads.size, 0);
  server.rejectPuts = false;
  await page.reload();
  await page.waitForSelector("#resume-note:not([hidden])");
  await waitFor(async () => (await snapshot(page)).uploadStats.pending === 0, { timeout: 30000, message: "再読み込み後に送られない" });
  assert.ok(server.uploads.size >= 2);
  await page.click("#btn-finish");
  await waitFor(() => server.completes.length === 1);
  await page.waitForSelector("#view-done:not([hidden])");
  await page.close();
});

it("処理が済んだ商談記録を開くと、理由を表示して録音させない", async ({ context, server }) => {
  const detail =
    "この商談記録は処理が済んでいます（要約と文字起こしが入っています）。" +
    "録音するときは、CRM で新しい商談記録を作り、その録音用URLを開いてください。";
  server.sessionError = { status: 409, detail };
  const page = await context.newPage();
  page.on("pageerror", (e) => console.error("pageerror:", e.message));
  await page.goto(`${server.origin}/recorder/#${makeToken("rec-done")}`);
  await page.waitForSelector("#view-error:not([hidden])");
  assert.equal(await page.textContent("#error-message"), detail);
  assert.equal(await page.isVisible("#view-main"), false, "録音の画面を出さない");
  assert.equal(await page.isVisible("#btn-start"), false, "録音開始のボタンを出さない");
  assert.equal(server.uploads.size, 0);
  await page.close();
});

// ---- 実行 ----

const server = new MockServer();
await server.listen();
const browser = await chromium.launch({
  args: ["--use-fake-device-for-media-stream", "--use-fake-ui-for-media-stream", "--autoplay-policy=no-user-gesture-required"],
});
let failed = 0;
for (const t of tests) {
  server.reset();
  const context = await browser.newContext({ permissions: ["microphone"] });
  const started = Date.now();
  try {
    await t.fn({ context, server });
    console.log(`ok - ${t.name} (${Date.now() - started}ms)`);
  } catch (err) {
    failed += 1;
    console.log(`not ok - ${t.name}\n  ${err.stack}`);
    console.log(`  events: ${server.events.map((e) => e.type).join(",")}`);
  } finally {
    await context.close();
  }
}
await browser.close();
await server.close();
console.log(`# pass ${tests.length - failed}\n# fail ${failed}`);
process.exit(failed ? 1 : 0);
