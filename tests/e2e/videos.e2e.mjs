// 動画の解析の画面（/app/videos.html）の E2E テスト。
// Chromium で動画を選び、モックの API と、GCS の代わりのアップロード先（このファイル内）で次を確かめる：
//   ログインしていないときの案内 / 動画を選ぶとアップロードして解析を依頼する（読めない形式は断る）/ 解析の状況の一覧
//
//   node tests/e2e/videos.e2e.mjs
//   SCREENSHOT_DIR=/tmp/shots node tests/e2e/videos.e2e.mjs

import assert from "node:assert/strict";
import { chmod, mkdir, mkdtemp, readFile, writeFile } from "node:fs/promises";
import http from "node:http";
import { createRequire } from "node:module";
import os from "node:os";
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

class MockServer {
  constructor() {
    this.loggedIn = true;
    this.puts = [];
    this.analyzed = [];
    this.headers = [];
    this.videos = [
      { id: "1", name: "00_前回の動画", status: "完了", error: "", created: "2026-11-03T19:00:00+09:00" },
    ];
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

  async raw(req) {
    const chunks = [];
    for await (const c of req) chunks.push(c);
    return Buffer.concat(chunks);
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

  async handle(req, res) {
    const url = new URL(req.url, this.origin);
    const p = url.pathname;
    if (p.startsWith("/app/")) return this.static(res, `app/${p.replace(/^\/app\//, "") || "index.html"}`);
    if (p.startsWith("/gcs/") && req.method === "PUT") {
      const body = await this.raw(req);
      this.puts.push({ name: decodeURIComponent(p.slice(5)), type: req.headers["content-type"], bytes: body.length });
      res.writeHead(200);
      return res.end();
    }
    if (!p.startsWith("/api/app/")) return this.json(res, 404, {});
    if (req.method !== "GET") this.headers.push(req.headers["x-requested-with"]);
    if (!this.loggedIn) return this.json(res, 401, { detail: "ログインしてください" });
    if (p === "/api/app/me") {
      return this.json(res, 200, {
        client_id: "monodemo",
        client_name: "ものづくりSFAデモ2",
        user: { id: "9001", name: "金指 営業", email: "sales@example.com" },
        gps_available: true,
        dry_run: false,
        video_analysis: true,
      });
    }
    if (p === "/api/app/videos" && req.method === "GET") {
      return this.json(res, 200, { videos: this.videos, done: "完了", failed: "失敗" });
    }
    const body = JSON.parse((await this.raw(req)).toString() || "{}");
    if (p === "/api/app/videos/upload-url") {
      const objectName = `videos/monodemo/${body.filename}`;
      return this.json(res, 200, {
        upload_url: `${this.origin}/gcs/${encodeURIComponent(objectName)}`,
        object_name: objectName,
        content_type: "video/mp4",
      });
    }
    if (p === "/api/app/videos/analyze") {
      this.analyzed.push(body);
      const stem = body.object_name.split("/").pop().replace(/\.[^.]+$/, "");
      this.videos.unshift({ id: String(this.videos.length + 1), name: stem, status: "", error: "", created: "" });
      return this.json(res, 200, { status: "queued" });
    }
    return this.json(res, 404, {});
  }
}

async function shot(page, name) {
  if (!SHOTS) return;
  await mkdir(SHOTS, { recursive: true });
  await page.screenshot({ path: path.join(SHOTS, `${name}.png`), fullPage: true });
}

const server = new MockServer();
await server.listen();
const browser = await chromium.launch();
const tmp = await mkdtemp(path.join(os.tmpdir(), "videos-e2e-"));
// mkdtemp のフォルダは本人しか読めない。ブラウザがファイルを読めるように開ける。
// ファイル名は ASCII にする（このコンテナのヘッドレス Chromium は日本語のファイル名を開けない。日本語名は tests/test_app_videos.py で確かめる）
await chmod(tmp, 0o755);
const video1 = path.join(tmp, "01_order-entry.mp4");
const video2 = path.join(tmp, "02_shipping.mp4");
const memo = path.join(tmp, "memo.txt");
await writeFile(video1, Buffer.alloc(300_000, 1));
await writeFile(video2, Buffer.alloc(120_000, 2));
await writeFile(memo, "memo");

try {
  const page = await browser.newPage({ viewport: { width: 390, height: 844 } });
  const errors = [];
  page.on("pageerror", (e) => errors.push(String(e)));

  // ログインしていない：録音アプリへの案内
  server.loggedIn = false;
  await page.goto(`${server.origin}/app/videos.html?c=monodemo`);
  await page.locator("#view-login").waitFor({ state: "visible" });
  assert.equal(await page.locator("#btn-login").getAttribute("href"), "/auth/login?c=monodemo");
  server.loggedIn = true;

  // 別の会社のログインのまま：その会社でログインし直してもらう（別の会社の CRM に書かない）
  await page.goto(`${server.origin}/app/videos.html?c=other`);
  await page.locator("#view-login").waitFor({ state: "visible" });
  assert.match(await page.locator("#login-text").textContent(), /別の会社の CRM/);
  assert.equal(await page.locator("#btn-login").getAttribute("href"), "/auth/login?c=other");
  assert.equal(await page.locator("#view-main").isHidden(), true);

  // ログイン済み：これまでの解析が見え、動画を選ぶまでボタンは押せない
  await page.goto(`${server.origin}/app/videos.html?c=monodemo`);
  await page.locator("#view-main").waitFor({ state: "visible" });
  assert.equal(await page.locator("#link-app").getAttribute("href"), "/app/?c=monodemo");
  assert.match(await page.locator("#user-name").textContent(), /ものづくりSFAデモ2 金指 営業/);
  assert.match(await page.locator("#video-list").textContent(), /00_前回の動画/);
  // 完了した動画は Word（詳細・要約）をダウンロードできる
  assert.deepEqual(
    await page.locator("#video-list li[data-state='done'] .downloads a").evaluateAll((els) => els.map((e) => e.getAttribute("href"))),
    ["/api/app/videos/1/report.docx?kind=detail", "/api/app/videos/1/report.docx?kind=summary"],
  );
  assert.equal(await page.locator("#btn-upload").isDisabled(), true);
  await shot(page, "videos-1-start");

  // 動画2本とメモを選んで上げる：動画は GCS に送って解析を依頼、メモは断る
  // 「ファイル選択」を何度押しても足していける（同じ動画は1本にまとめる）。外すこともできる
  await page.locator("#file-input").setInputFiles([video1]);
  await page.locator("#selected-count").filter({ hasText: "選んだ動画：1本" }).waitFor();
  await page.locator("#file-input").setInputFiles([memo, video2, video1]);
  await page.locator("#selected-count").filter({ hasText: "選んだ動画：3本" }).waitFor();
  assert.equal(await page.locator("#btn-upload").textContent(), "3本をアップロードして解析");
  await page.locator("#file-input").setInputFiles([video2]);
  await page.locator("#selected-count").filter({ hasText: "選んだ動画：3本" }).waitFor();
  await shot(page, "videos-1b-selected");
  await page.locator("#btn-upload").click();
  await page.locator("#toast").filter({ hasText: "3本のうち2本の解析を依頼しました" }).waitFor();
  assert.deepEqual(
    server.puts.map((p) => [p.name, p.type, p.bytes]),
    [
      ["videos/monodemo/01_order-entry.mp4", "video/mp4", 300_000],
      ["videos/monodemo/02_shipping.mp4", "video/mp4", 120_000],
    ],
  );
  assert.deepEqual(
    server.analyzed.map((a) => a.object_name),
    ["videos/monodemo/01_order-entry.mp4", "videos/monodemo/02_shipping.mp4"],
  );
  const rows = await page.locator("#upload-list li").allTextContents();
  assert.match(rows[0], /解析を依頼しました/);
  assert.match(rows[1], /この形式は読めません/);
  assert.match(rows[2], /解析を依頼しました/);
  // 解析中の動画があると、案内を出す
  await page.locator("#video-working").waitFor({ state: "visible" });
  assert.match(await page.locator("#video-list").textContent(), /02_shipping/);
  assert.ok(server.headers.every((h) => h === "meeting-notes-app"), "書き込みには録音アプリのヘッダーを付ける");
  await shot(page, "videos-2-uploaded");

  assert.deepEqual(errors, []);
  console.log("videos.e2e: OK");
} finally {
  await browser.close();
  await server.close();
}
