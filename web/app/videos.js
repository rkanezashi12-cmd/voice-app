// 動画の解析（/app/videos.html）：動画を選ぶ → GCS に直接アップロード（署名付き URL）→ 解析を依頼 → 状況を見る。
// ログインは録音アプリと同じ Cookie。API は /api/app/videos/*。動画そのものはサービスを通らない。

import { dateLabel, formatBytes, hasWorking, isVideoFile, percent, statusKind } from "./lib/videos.js";
import { errorMessage } from "./lib/view.js";

const $ = (id) => document.getElementById(id);
const APP_HEADER = { "X-Requested-With": "meeting-notes-app" };
const POLL_MS = 30000;

let uploading = false;
let labels = { done: "完了", failed: "失敗" };
let pollTimer = null;

class LoginRequired extends Error {}

async function api(path, { method = "GET", body } = {}) {
  const headers = { ...APP_HEADER };
  if (body !== undefined) headers["Content-Type"] = "application/json";
  const res = await fetch(path, {
    method,
    headers,
    body: body === undefined ? undefined : JSON.stringify(body),
    cache: "no-store",
    credentials: "same-origin",
  });
  if (res.status === 401) throw new LoginRequired();
  if (!res.ok) {
    let data = null;
    try {
      data = await res.json();
    } catch {
      // 本文が JSON でない
    }
    throw new Error(errorMessage(data, res.status));
  }
  return res.json();
}

function show(id, visible) {
  $(id).hidden = !visible;
}

function toast(text) {
  const el = $("toast");
  el.textContent = text;
  el.hidden = false;
  setTimeout(() => {
    el.hidden = true;
  }, 4000);
}

function showError(message) {
  show("view-loading", false);
  $("error-message").textContent = message;
  show("view-error", true);
}

// ---- アップロード ----

function putFile(url, file, contentType, onProgress) {
  // 進み具合を出すため XMLHttpRequest を使う（fetch はアップロードの進み具合を取れない）
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("PUT", url);
    xhr.setRequestHeader("Content-Type", contentType);
    xhr.upload.onprogress = (e) => onProgress(e.loaded, e.total || file.size);
    xhr.onload = () =>
      xhr.status >= 200 && xhr.status < 300 ? resolve() : reject(new Error(`アップロードに失敗しました（${xhr.status}）`));
    xhr.onerror = () => reject(new Error("通信が切れました。もう一度選んでやり直してください"));
    xhr.send(file);
  });
}

function addRow(file) {
  const li = document.createElement("li");
  const name = document.createElement("strong");
  name.textContent = file.name;
  const info = document.createElement("span");
  info.className = "hint";
  info.textContent = ` ${formatBytes(file.size)} … 待っています`;
  li.append(name, info);
  $("upload-list").append(li);
  return (text, kind) => {
    info.textContent = ` ${formatBytes(file.size)} … ${text}`;
    li.dataset.state = kind || "";
  };
}

async function uploadOne(file, force) {
  const setStatus = addRow(file);
  if (!isVideoFile(file.name)) {
    setStatus("この形式は読めません（mp4・mov・webm などにしてください）", "failed");
    return false;
  }
  try {
    const target = await api("/api/app/videos/upload-url", {
      method: "POST",
      body: { filename: file.name, size: file.size },
    });
    await putFile(target.upload_url, file, target.content_type, (loaded, total) =>
      setStatus(`アップロード中 ${percent(loaded, total)}%`, "working"),
    );
    setStatus("解析を依頼しています", "working");
    const res = await api("/api/app/videos/analyze", {
      method: "POST",
      body: { object_name: target.object_name, force },
    });
    setStatus(res.status === "queued" ? "解析を依頼しました" : "解析はもう依頼済みです", "done");
    return true;
  } catch (err) {
    if (err instanceof LoginRequired) {
      setStatus("ログインが切れました。録音アプリでログインし直してください", "failed");
    } else {
      setStatus(err && err.message ? err.message : "失敗しました", "failed");
    }
    return false;
  }
}

async function onUpload() {
  const files = Array.from($("file-input").files || []);
  if (files.length === 0 || uploading) return;
  uploading = true;
  $("btn-upload").disabled = true;
  $("file-input").disabled = true;
  $("upload-list").textContent = "";
  const force = $("chk-force").checked;
  let ok = 0;
  // 1本ずつ順に上げる（回線を取り合わない）
  for (const file of files) {
    if (await uploadOne(file, force)) ok += 1;
  }
  uploading = false;
  $("file-input").value = "";
  $("file-input").disabled = false;
  $("btn-upload").disabled = true;
  toast(`${files.length}本のうち${ok}本の解析を依頼しました`);
  await refresh();
}

// ---- 解析の状況 ----

function renderVideos(videos) {
  const list = $("video-list");
  list.textContent = "";
  for (const v of videos) {
    const li = document.createElement("li");
    const kind = statusKind(v.status, labels);
    li.dataset.state = kind;
    const name = document.createElement("strong");
    name.textContent = v.name || "（名前なし）";
    const info = document.createElement("span");
    info.className = "hint";
    info.textContent = ` ${v.status || "解析中"}　${dateLabel(v.created)}`;
    li.append(name, info);
    if (kind === "failed" && v.error) {
      const err = document.createElement("div");
      err.className = "warning";
      err.textContent = v.error;
      li.append(err);
    }
    list.append(li);
  }
  show("video-empty", videos.length === 0);
}

async function refresh() {
  clearTimeout(pollTimer);
  try {
    const data = await api("/api/app/videos");
    labels = { done: data.done, failed: data.failed };
    renderVideos(data.videos || []);
    const working = hasWorking(data.videos, labels);
    show("video-working", working);
    if (working) pollTimer = setTimeout(refresh, POLL_MS);
  } catch (err) {
    if (err instanceof LoginRequired) {
      showLogin(clientInUrl(), false);
      return;
    }
    toast(err && err.message ? err.message : "状況を読み込めませんでした");
  }
}

// URL の会社（?c=）。録音アプリへのリンク・ログインに引き継ぐ（無ければログイン中の会社）
function clientInUrl() {
  const c = new URLSearchParams(window.location.search).get("c");
  return c && /^[a-z0-9][a-z0-9-]{0,31}$/.test(c) ? c : "";
}

function showLogin(client, otherClient) {
  show("view-loading", false);
  show("view-main", false);
  if (otherClient) {
    $("login-text").textContent =
      "別の会社の CRM にログインしています。この会社の CRM のアカウントで、ログインし直してください（Zoho に別のアカウントでログインしたままのときは、先に Zoho からログアウトしてください）。";
  }
  $("btn-login").href = client ? `/auth/login?c=${encodeURIComponent(client)}` : "/app/";
  show("view-login", true);
}

async function init() {
  const client = clientInUrl();
  $("file-input").addEventListener("change", () => {
    $("btn-upload").disabled = uploading || !($("file-input").files || []).length;
  });
  $("btn-upload").addEventListener("click", onUpload);
  $("btn-refresh").addEventListener("click", refresh);
  window.addEventListener("beforeunload", (e) => {
    if (uploading) e.preventDefault();
  });
  let me;
  try {
    me = await api("/api/app/me");
  } catch (err) {
    if (err instanceof LoginRequired) {
      showLogin(client, false);
      return;
    }
    show("view-loading", false);
    showError(err && err.message ? err.message : "読み込めませんでした。ページを再読み込みしてください。");
    return;
  }
  // ログインの Cookie は会社をまたいで1つ。URL の会社と違う会社のログインのまま使うと、別の会社の CRM に書くので止める
  if (client && me.client_id !== client) {
    showLogin(client, true);
    return;
  }
  $("link-app").href = `/app/?c=${encodeURIComponent(me.client_id)}`;
  show("view-loading", false);
  if (!me.video_analysis) {
    showError("この会社では動画の解析を使えません。");
    return;
  }
  $("user-name").textContent = `${me.client_name || ""} ${me.user.name || me.user.email || ""}`.trim();
  show("view-main", true);
  await refresh();
}

init().catch((err) => showError(String(err && err.message ? err.message : err)));
