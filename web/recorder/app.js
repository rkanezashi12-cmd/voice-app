// 対面録音ページ。
// 録音 → 分割 → 端末内に保存 → 署名付き URL で GCS へ送信。停止を検知したら画面と音で警告し、
// 「再開」で同じ記録に追記する。URL のフラグメント（#以降）のトークンで API を呼ぶ。

import { Alarm } from "./lib/alarm.js";
import { buildSummary, Diagnostics } from "./lib/diagnostics.js";
import { ChunkedRecorder, newSessionId, pickMimeType } from "./lib/recorder-core.js";
import { chunkKey, IdbStore, MemoryStore, NonRetryableError, UploadQueue } from "./lib/upload-queue.js";
import { GapWatchdog, SilenceDetector } from "./lib/watchdog.js";

const $ = (id) => document.getElementById(id);

const DEFAULTS = { mode: "a", chunkSeconds: 60, levelMonitor: true };
const HEALTH_INTERVAL_MS = 5000;
const MUTED_ALERT_MS = 3000;

const REASONS = {
  mic_ended: "マイクが使えなくなりました（着信、他のアプリによるマイクの使用、画面ロックなど）。",
  mic_muted: "マイクの音声が止まりました（着信などの割り込み）。",
  recorder_stopped: "録音が中断されました（画面ロック・アプリの切り替えなど）。",
  start_failed: "録音を続けられませんでした。",
  rotate_failed: "録音を続けられませんでした。",
  no_data: "しばらく音声データが届いていません（画面ロック・アプリの切り替えなど）。",
};

const state = {
  token: "",
  recordId: "",
  test: false,
  expiresAt: null,
  phase: "init", // init | ready | recording | interrupted | finishing | done | error
  starting: false,
  settings: { ...DEFAULTS },
  stream: null,
  recorder: null,
  audioCtx: null,
  levelTimer: null,
  healthTimer: null,
  uiTimer: null,
  wakeLock: null,
  wakeLockStatus: "-",
  mimeType: "",
  sessions: [], // [{ session_id, mode, mime, chunks, recorded_ms }]
  sessionStartedAt: null,
  firstStartedAt: null,
  gaps: [],
  rotationGaps: [],
  hiddenAt: null,
  unhealthyCount: 0,
  mutedSince: null,
  chunkWrites: new Set(),
  uploadStats: { pending: 0, uploaded: 0, failedAttempts: 0, lastError: null },
  store: null,
  queue: null,
  diag: null,
  watchdog: null,
  alarm: new Alarm(),
};

// ---- API ----

function apiPath(suffix) {
  return `/api/recordings/${encodeURIComponent(state.recordId)}${suffix}`;
}

async function api(suffix, { method = "GET", body, keepalive = false } = {}) {
  const headers = { Authorization: `Bearer ${state.token}` };
  if (body !== undefined) headers["Content-Type"] = "application/json";
  return fetch(apiPath(suffix), {
    method,
    headers,
    body: body === undefined ? undefined : JSON.stringify(body),
    cache: "no-store",
    keepalive,
  });
}

async function errorText(res) {
  try {
    const data = await res.json();
    if (typeof data.detail === "string") return data.detail;
    if (data.detail && typeof data.detail.message === "string") return data.detail.message;
    if (typeof data.message === "string") return data.message;
  } catch {
    // 本文なし
  }
  return `HTTP ${res.status}`;
}

async function uploadChunk(item) {
  const res = await api("/upload-url", {
    method: "POST",
    body: { session_id: item.sessionId, seq: item.seq, mime_type: item.mimeType },
  });
  if (res.status === 401 || res.status === 403) throw new NonRetryableError(await errorText(res));
  if (!res.ok) throw new Error(`送信先の取得に失敗（${res.status}）`);
  const { url, headers } = await res.json();
  const put = await fetch(url, {
    method: "PUT",
    headers,
    body: new Blob([item.data], { type: headers["Content-Type"] }),
  });
  if (!put.ok) throw new Error(`送信に失敗（${put.status}）`);
}

// ---- 表示 ----

function show(id, visible) {
  $(id).hidden = !visible;
}

function fmtDuration(ms) {
  const s = Math.max(0, Math.floor(ms / 1000));
  const hh = String(Math.floor(s / 3600)).padStart(2, "0");
  const mm = String(Math.floor((s % 3600) / 60)).padStart(2, "0");
  const ss = String(s % 60).padStart(2, "0");
  return `${hh}:${mm}:${ss}`;
}

function recordedMs() {
  const closed = state.sessions.reduce((a, s) => a + (s.recorded_ms ?? 0), 0);
  const current = state.phase === "recording" && state.sessionStartedAt ? Date.now() - state.sessionStartedAt : 0;
  return closed + current;
}

const PHASE_LABELS = {
  init: "準備中",
  ready: "待機中",
  recording: "録音中",
  interrupted: "停止（要再開）",
  finishing: "送信中",
  done: "完了",
  error: "エラー",
};

function render() {
  const recording = state.phase === "recording";
  show("rec-bar", recording);
  document.title = recording ? "● 録音中 - 商談の録音" : "商談の録音";
  show("view-main", ["ready", "recording", "interrupted"].includes(state.phase));
  show("view-finishing", state.phase === "finishing");
  show("view-done", state.phase === "done");
  show("btn-start", state.phase === "ready");
  show("btn-finish", recording || (state.phase === "ready" && hasRecording()));
  $("btn-start").textContent = hasRecording() ? "録音を再開（続きから）" : "録音開始";
  show("consent", state.phase === "ready" && !hasRecording());
  $("st-state").textContent = PHASE_LABELS[state.phase] ?? state.phase;
  $("st-uploaded").textContent = String(state.uploadStats.uploaded);
  $("st-pending").textContent = String(state.uploadStats.pending);
  $("fin-pending").textContent = String(state.uploadStats.pending);
  $("st-wakelock").textContent = state.wakeLockStatus;
  show("st-error-box", Boolean(state.uploadStats.lastError));
  $("st-error").textContent = state.uploadStats.lastError || "-";
  $("rec-elapsed").textContent = fmtDuration(recordedMs());
  if (state.test) $("summary").textContent = JSON.stringify(currentSummary(), null, 2);
}

function hasRecording() {
  return state.sessions.some((s) => s.chunks > 0);
}

function showError(title, message, { copyUrl = false } = {}) {
  state.phase = "error";
  $("error-title").textContent = title;
  $("error-message").textContent = message;
  show("copy-url", copyUrl);
  show("view-error", true);
  show("view-main", false);
  show("view-finishing", false);
  show("view-done", false);
  show("rec-bar", false);
}

function softWarning(message) {
  const el = $("soft-warning");
  el.textContent = message ?? "";
  el.hidden = !message;
}

function logToScreen(entry) {
  if (!state.test) return;
  const li = document.createElement("li");
  const t = new Date(entry.at).toLocaleTimeString("ja-JP", { hour12: false });
  li.textContent = `${t} ${entry.type} ${JSON.stringify(entry.detail)}`;
  const list = $("event-log");
  list.prepend(li);
  while (list.children.length > 200) list.lastChild.remove();
}

function currentSummary() {
  return buildSummary({
    counters: state.diag ? state.diag.counters : {},
    startedAt: state.firstStartedAt,
    now: Date.now(),
    uploaded: state.uploadStats.uploaded,
    pending: state.uploadStats.pending,
    failedAttempts: state.uploadStats.failedAttempts,
    gaps: state.gaps,
    mimeType: state.mimeType,
    mode: state.settings.mode,
    chunkSeconds: state.settings.chunkSeconds,
    wakeLock: state.wakeLockStatus,
    userAgent: navigator.userAgent,
    rotationGaps: state.rotationGaps,
  });
}

// ---- 端末内の状態 ----

async function persistMeta(extra = {}) {
  try {
    await state.store.putMeta({ sessions: state.sessions, ...extra });
  } catch (err) {
    log("storage_error", { op: "meta", name: err && err.name });
  }
}

function log(type, detail = {}) {
  const sessionId = state.recorder ? state.recorder.sessionId : null;
  if (state.diag) state.diag.log(type, detail, sessionId);
}

// ---- 録音 ----

function currentSession() {
  return state.sessions[state.sessions.length - 1];
}

async function handleChunk(chunk) {
  const data = await chunk.blob.arrayBuffer();
  const item = {
    key: chunkKey(state.recordId, chunk.sessionId, chunk.seq),
    recordId: state.recordId,
    sessionId: chunk.sessionId,
    seq: chunk.seq,
    mimeType: chunk.mimeType,
    size: data.byteLength,
    createdAt: Date.now(),
    data,
  };
  const session = state.sessions.find((s) => s.session_id === chunk.sessionId);
  if (session) session.chunks = Math.max(session.chunks, chunk.seq + 1);
  await persistMeta();
  try {
    await state.queue.add(item);
  } catch (err) {
    log("storage_error", { op: "chunk", name: err && err.name });
    softWarning("端末に音声を保存できませんでした。空き容量を確認してください。");
  }
  log("chunk", { seq: chunk.seq, bytes: data.byteLength, ms: chunk.endedAt - chunk.startedAt });
  render();
}

function trackChunkWrite(promise) {
  state.chunkWrites.add(promise);
  promise.finally(() => state.chunkWrites.delete(promise));
}

async function startRecording(isResume) {
  // 二度押しで録音が二重に始まらないようにする
  if (state.starting || state.phase === "recording") return;
  state.starting = true;
  try {
    await startRecordingInner(isResume);
  } finally {
    state.starting = false;
  }
}

async function startRecordingInner(isResume) {
  state.alarm.unlock();
  state.alarm.stop();
  show("overlay", false);
  softWarning(null);

  let stream;
  try {
    stream = await navigator.mediaDevices.getUserMedia({
      audio: { echoCancellation: false, noiseSuppression: false, autoGainControl: true, channelCount: 1 },
    });
  } catch (err) {
    log("mic_error", { name: err && err.name });
    if (state.phase === "interrupted") show("overlay", true);
    softWarning(
      err && err.name === "NotAllowedError"
        ? "マイクの使用が許可されていません。ブラウザの設定でマイクを許可してから、もう一度お試しください。"
        : "マイクを使えませんでした。他のアプリがマイクを使っていないか確認してください。",
    );
    return;
  }
  state.stream = stream;
  state.mutedSince = null;
  const track = stream.getAudioTracks()[0];
  if (track) {
    const s = track.getSettings ? track.getSettings() : {};
    log("mic_ready", { sample_rate: s.sampleRate ?? null, channels: s.channelCount ?? null, resume: isResume });
    track.addEventListener("ended", () => {
      log("track_ended");
      interrupted("mic_ended");
    });
    track.addEventListener("mute", () => {
      state.mutedSince = Date.now();
      log("track_mute");
    });
    track.addEventListener("unmute", () => {
      log("track_unmute", { muted_ms: state.mutedSince ? Date.now() - state.mutedSince : null });
      state.mutedSince = null;
    });
    // 通話中に再開すると、最初からミュートされた状態でマイクが渡される（mute イベントが来ないこともある）
    if (track.muted && !state.mutedSince) {
      state.mutedSince = Date.now();
      log("track_muted_at_start");
    }
  }

  const { mode, chunkSeconds } = state.settings;
  state.mimeType = pickMimeType(window.MediaRecorder);
  const sessionId = newSessionId(mode);
  state.sessions.push({ session_id: sessionId, mode, mime: state.mimeType, chunks: 0, recorded_ms: 0 });
  await persistMeta();

  const recorder = new ChunkedRecorder({
    stream,
    mode,
    chunkMs: chunkSeconds * 1000,
    MediaRecorder: window.MediaRecorder,
    mimeType: state.mimeType,
    sessionId,
    onChunk: (chunk) => trackChunkWrite(handleChunk(chunk)),
    onUnexpectedStop: ({ reason }) => interrupted(reason),
    onEvent: (type, detail) => {
      if (type === "rotation_gap" && detail) state.rotationGaps.push(detail.gap_ms);
      log(type, detail);
    },
  });
  state.recorder = recorder;
  try {
    recorder.start();
  } catch (err) {
    log("recorder_start_error", { name: err && err.name });
    stopStream();
    softWarning("録音を開始できませんでした。ページを再読み込みしてお試しください。");
    return;
  }

  state.phase = "recording";
  state.sessionStartedAt = Date.now();
  state.firstStartedAt = state.firstStartedAt ?? state.sessionStartedAt;
  state.unhealthyCount = 0;
  log(isResume ? "resume" : "start", { mode, chunk_seconds: chunkSeconds, mime: state.mimeType || "default" });

  startLevelMonitor(stream);
  state.watchdog.start();
  state.healthTimer = setInterval(healthCheck, HEALTH_INTERVAL_MS);
  await requestWakeLock();
  render();
  state.diag.flush();
}

function stopStream() {
  if (state.stream) state.stream.getTracks().forEach((t) => t.stop());
  state.stream = null;
}

function stopMonitors() {
  if (state.healthTimer) clearInterval(state.healthTimer);
  state.healthTimer = null;
  if (state.levelTimer) clearInterval(state.levelTimer);
  state.levelTimer = null;
  if (state.audioCtx) state.audioCtx.close().catch(() => {});
  state.audioCtx = null;
  state.watchdog.stop();
}

function closeSession() {
  const session = currentSession();
  if (session && state.sessionStartedAt) session.recorded_ms = Date.now() - state.sessionStartedAt;
  state.sessionStartedAt = null;
}

async function interrupted(reason, detail = {}) {
  if (state.phase !== "recording") return;
  state.phase = "interrupted";
  log("interrupted", { reason, ...detail });
  log("summary", currentSummary());
  stopMonitors();
  try {
    if (state.recorder) await state.recorder.stop();
  } catch {
    // 既に止まっている
  }
  closeSession();
  stopStream();
  await releaseWakeLock();
  await persistMeta();
  $("overlay-reason").textContent = REASONS[reason] ?? "録音が中断されました。";
  show("overlay", true);
  render();
  const played = await state.alarm.start();
  log("alarm", { played });
  state.diag.flush();
}

function healthCheck() {
  if (state.phase !== "recording") return;
  const recorder = state.recorder;
  const track = state.stream ? state.stream.getAudioTracks()[0] : null;
  let problem = null;
  if (!track || track.readyState !== "live") problem = "mic_ended";
  else if (recorder.state === "failed") problem = "recorder_stopped";
  else if (state.mutedSince && Date.now() - state.mutedSince >= MUTED_ALERT_MS) problem = "mic_muted";
  else if (Date.now() - recorder.lastChunkAt > recorder.chunkMs + 20000) problem = "no_data";
  if (!problem) {
    state.unhealthyCount = 0;
    return;
  }
  state.unhealthyCount += 1;
  log("health_problem", { problem, count: state.unhealthyCount });
  // タイマー再開直後の一時的なずれで誤警報を出さないよう、データ停止だけは2回続いたら判断する
  if (problem !== "no_data" || state.unhealthyCount >= 2) interrupted(problem);
}

function startLevelMonitor(stream) {
  if (!state.settings.levelMonitor) return;
  const Ctx = window.AudioContext || window.webkitAudioContext;
  if (!Ctx) return;
  try {
    const ctx = new Ctx();
    const source = ctx.createMediaStreamSource(stream);
    const analyser = ctx.createAnalyser();
    analyser.fftSize = 2048;
    source.connect(analyser);
    const buf = new Float32Array(analyser.fftSize);
    const silence = new SilenceDetector({
      onSilence: (ms) => {
        log("silence", { ms });
        softWarning("10秒以上、音声がまったく入っていません。マイクが止まっていないか確認してください。");
      },
      onSound: () => softWarning(null),
    });
    ctx.addEventListener("statechange", () => {
      log("audio_ctx_state", { state: ctx.state });
      if (ctx.state !== "running") healthCheck();
    });
    state.audioCtx = ctx;
    state.levelTimer = setInterval(() => {
      analyser.getFloatTimeDomainData(buf);
      let peak = 0;
      for (let i = 0; i < buf.length; i++) peak = Math.max(peak, Math.abs(buf[i]));
      $("st-level").value = Math.min(1, peak * 4);
      if (ctx.state === "running") silence.push(peak);
    }, 500);
  } catch (err) {
    log("level_monitor_error", { name: err && err.name });
  }
}

// ---- 画面の点灯維持 ----

async function requestWakeLock() {
  if (!("wakeLock" in navigator)) {
    state.wakeLockStatus = "非対応";
    show("wakelock-warning", true);
    log("wake_lock", { status: "unsupported" });
    return;
  }
  try {
    const lock = await navigator.wakeLock.request("screen");
    state.wakeLock = lock;
    state.wakeLockStatus = "有効";
    show("wakelock-warning", false);
    log("wake_lock", { status: "acquired" });
    lock.addEventListener("release", () => {
      state.wakeLockStatus = "解除";
      log("wake_lock", { status: "released" });
      render();
    });
  } catch (err) {
    state.wakeLockStatus = "失敗";
    show("wakelock-warning", true);
    log("wake_lock", { status: "error", name: err && err.name });
  }
  render();
}

async function releaseWakeLock() {
  try {
    if (state.wakeLock) await state.wakeLock.release();
  } catch {
    // 既に解除済み
  }
  state.wakeLock = null;
}

// ---- 終了と送信 ----

async function finish() {
  if (state.phase === "finishing" || state.phase === "done") return;
  if (state.phase === "recording" && !window.confirm("録音を終了して送信しますか？")) return;
  log("finish_requested", { phase: state.phase });
  const wasRecording = state.phase === "recording";
  // 先に状態を変えておく。マイクを止めたことを健全性チェックが「停止」と誤判定して警告を出さないように
  state.phase = "finishing";
  state.alarm.stop();
  show("overlay", false);
  if (wasRecording) {
    stopMonitors();
    await state.recorder.stop();
    closeSession();
    stopStream();
    await releaseWakeLock();
  }
  await Promise.allSettled([...state.chunkWrites]);
  await persistMeta();
  state.phase = "finishing";
  show("fin-missing", false);
  render();
  await drainAndComplete(false);
}

async function drainAndComplete(allowMissing) {
  if (!allowMissing) {
    const drained = await state.queue.drain(5 * 60 * 1000);
    if (!drained) {
      $("fin-missing-count").textContent = String(state.uploadStats.pending);
      show("fin-missing", true);
      log("finish_pending", { pending: state.uploadStats.pending });
      render();
      return;
    }
  }
  const sessions = state.sessions
    .filter((s) => s.chunks > 0)
    .map((s) => ({ session_id: s.session_id, chunks: s.chunks }));
  if (sessions.length === 0) {
    showError("録音がありません", "録音された音声がありません。もう一度録音してください。");
    return;
  }
  let res;
  try {
    res = await api("/complete", {
      method: "POST",
      body: { sessions, allow_missing: allowMissing, duration_ms: recordedMs() },
    });
  } catch {
    $("fin-missing-count").textContent = String(state.uploadStats.pending);
    show("fin-missing", true);
    return;
  }
  if (res.ok) {
    const data = await res.json();
    log("completed", { chunks: data.chunks, missing: data.missing, status: data.status });
    // 診断結果もサーバーのログに残す（テストした人がコピーして送らなくても結果を確認できる）
    log("summary", currentSummary());
    await state.queue.stop();
    for (const item of await state.store.list()) await state.store.delete(item.key);
    await persistMeta({ completed: true });
    state.phase = "done";
    state.alarm.stop();
    show("overlay", false);
    if (state.test) {
      $("done-message").textContent = "テスト録音の送信が完了しました。下の「診断結果をコピー」で結果を記録表に貼ってください。";
    }
    render();
    state.diag.flush();
    return;
  }
  if (res.status === 409) {
    let missing = 0;
    try {
      const data = await res.json();
      missing = (data.detail && data.detail.missing_count) || (data.detail && data.detail.missing.length) || 0;
    } catch {
      missing = 0;
    }
    $("fin-missing-count").textContent = String(missing || state.uploadStats.pending);
    show("fin-missing", true);
    log("finish_missing", { missing });
    render();
    return;
  }
  if (res.status === 401 || res.status === 403) {
    showError("URL の有効期限が切れています", await errorText(res));
    return;
  }
  $("fin-missing-count").textContent = String(state.uploadStats.pending);
  show("fin-missing", true);
}

// ---- 初期化 ----

// "#<トークン>" か "#t=<トークン>"。録音アプリ（/app/）から開いたときは "#t=<トークン>&app=1"
function readHash() {
  const hash = window.location.hash.replace(/^#/, "");
  if (!hash.startsWith("t=")) return { token: hash, fromApp: false };
  const params = new URLSearchParams(hash);
  return { token: params.get("t") || "", fromApp: params.get("app") === "1" };
}

// 録音アプリから開いたときは、アプリと同じ下のタブ（入力・録音・日報）と「日報を見る」を出す
function setupAppNav() {
  const report = `/app/#report=${encodeURIComponent(state.recordId)}`;
  $("app-tab-report").href = report;
  $("btn-report").href = report;
  show("app-nav", true);
  show("btn-report", true);
  document.body.classList.add("from-app");
}

function recordIdFromToken(token) {
  try {
    const body = token.split(".")[0].replace(/-/g, "+").replace(/_/g, "/");
    const payload = JSON.parse(atob(body + "=".repeat((4 - (body.length % 4)) % 4)));
    return typeof payload.r === "string" ? payload.r : "";
  } catch {
    return "";
  }
}

function loadSettings() {
  if (!state.test) return { ...DEFAULTS };
  try {
    const saved = JSON.parse(window.localStorage.getItem("recorder.settings") || "{}");
    return { ...DEFAULTS, ...saved };
  } catch {
    return { ...DEFAULTS };
  }
}

function saveSettings() {
  try {
    window.localStorage.setItem("recorder.settings", JSON.stringify(state.settings));
  } catch {
    // 保存できなくても動作には影響しない
  }
}

function setupTestPanel() {
  show("test-panel", true);
  for (const radio of document.querySelectorAll('input[name="mode"]')) {
    radio.checked = radio.value === state.settings.mode;
    radio.addEventListener("change", () => {
      state.settings.mode = radio.value;
      saveSettings();
    });
  }
  const seconds = $("chunk-seconds");
  seconds.value = String(state.settings.chunkSeconds);
  seconds.addEventListener("change", () => {
    const v = Math.min(300, Math.max(5, Number(seconds.value) || 60));
    seconds.value = String(v);
    state.settings.chunkSeconds = v;
    saveSettings();
  });
  const monitor = $("level-monitor");
  monitor.checked = state.settings.levelMonitor;
  monitor.addEventListener("change", () => {
    state.settings.levelMonitor = monitor.checked;
    saveSettings();
  });
  $("btn-copy-summary").addEventListener("click", async () => {
    const text = JSON.stringify(currentSummary(), null, 2);
    try {
      await navigator.clipboard.writeText(text);
      $("btn-copy-summary").textContent = "コピーしました";
    } catch {
      const range = document.createRange();
      range.selectNodeContents($("summary"));
      window.getSelection().removeAllRanges();
      window.getSelection().addRange(range);
    }
  });
  // e2e テスト用（テストトークンのときだけ）
  window.__recorderDebug = {
    stopTrack: () => state.stream && state.stream.getAudioTracks().forEach((t) => t.stop()),
    snapshot: () => ({
      phase: state.phase,
      sessions: state.sessions.map((s) => ({ ...s })),
      uploadStats: { ...state.uploadStats },
      wakeLockStatus: state.wakeLockStatus,
      mimeType: state.mimeType,
    }),
    summary: currentSummary,
    healthCheck: () => healthCheck(),
  };
}

function wireEvents() {
  $("btn-start").addEventListener("click", () => startRecording(hasRecording()));
  $("btn-finish").addEventListener("click", () => finish());
  $("btn-resume").addEventListener("click", () => startRecording(true));
  $("btn-overlay-finish").addEventListener("click", () => finish());
  $("btn-retry-finish").addEventListener("click", () => {
    show("fin-missing", false);
    state.queue.start();
    state.queue.kick();
    drainAndComplete(false);
  });
  $("btn-force-finish").addEventListener("click", () => {
    if (window.confirm("届いていない音声は文字起こしされません。完了してよいですか？")) drainAndComplete(true);
  });
  $("copy-url").addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText(window.location.href);
      $("copy-url").textContent = "コピーしました。Safari / Chrome に貼り付けて開いてください";
    } catch {
      window.prompt("この URL をコピーして Safari / Chrome で開いてください", window.location.href);
    }
  });

  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "hidden") {
      state.hiddenAt = Date.now();
      log("hidden", { phase: state.phase });
      state.diag.flush();
      return;
    }
    const hiddenMs = state.hiddenAt ? Date.now() - state.hiddenAt : null;
    state.hiddenAt = null;
    log("visible", { phase: state.phase, hidden_ms: hiddenMs });
    state.queue.kick();
    if (state.phase === "recording") {
      requestWakeLock();
      healthCheck();
      if (hiddenMs !== null && hiddenMs > 3000 && state.phase === "recording") {
        softWarning(
          `画面が ${Math.round(hiddenMs / 1000)} 秒間表示されていませんでした。録音が続いているか、音量の表示で確認してください。`,
        );
      }
    }
    state.diag.flush();
  });
  window.addEventListener("pagehide", (e) => {
    log("pagehide", { persisted: e.persisted, phase: state.phase });
    flushKeepalive();
  });
  document.addEventListener("freeze", () => log("freeze", { phase: state.phase }));
  document.addEventListener("resume", () => log("page_resume", { phase: state.phase }));
  window.addEventListener("online", () => {
    log("online");
    state.queue.kick();
  });
  window.addEventListener("offline", () => log("offline"));
  window.addEventListener("beforeunload", (e) => {
    if (state.phase === "recording" || state.phase === "finishing") {
      e.preventDefault();
      e.returnValue = "";
    }
  });
}

function flushKeepalive() {
  const events = state.diag ? state.diag.pending.splice(0, 100) : [];
  if (events.length === 0) return;
  api("/events", { method: "POST", body: { events }, keepalive: true }).catch(() => {});
}

async function init() {
  if (!window.isSecureContext || !navigator.mediaDevices || !navigator.mediaDevices.getUserMedia || !window.MediaRecorder) {
    showError(
      "このブラウザでは録音できません",
      "Safari（iPhone）または Chrome（Android）でこのページを開いてください。アプリ内のブラウザでは録音できないことがあります。",
      { copyUrl: true },
    );
    return;
  }
  const { token, fromApp } = readHash();
  state.token = token;
  state.recordId = recordIdFromToken(state.token);
  if (fromApp && state.recordId) setupAppNav();
  if (!state.token || !state.recordId) {
    showError("URL が正しくありません", "CRM の商談記録にある録音用リンクから開いてください。");
    return;
  }

  let res;
  try {
    res = await api("/session");
  } catch {
    showError("接続できません", "通信状態を確認して、ページを再読み込みしてください。");
    return;
  }
  if (!res.ok) {
    showError("録音用リンクが使えません", await errorText(res));
    return;
  }
  const info = await res.json();
  state.test = Boolean(info.test);
  state.expiresAt = new Date(info.expires_at);
  state.settings = loadSettings();
  $("record-info").textContent =
    `記録 …${state.recordId.slice(-6)}　有効期限 ${state.expiresAt.toLocaleString("ja-JP", { hour12: false })}` +
    (state.test ? "（テスト）" : "");

  state.diag = new Diagnostics({
    send: async (events) => {
      const r = await api("/events", { method: "POST", body: { events } });
      return r.ok;
    },
    onLog: logToScreen,
  });
  state.watchdog = new GapWatchdog({
    onGap: (gap) => {
      state.gaps.push(gap);
      log("timer_gap", { gap_ms: gap.durationMs, phase: state.phase });
      if (state.phase === "recording") {
        softWarning(
          `約 ${Math.round(gap.durationMs / 1000)} 秒間、ページが止まっていました（画面ロックなど）。録音が続いているか確認してください。`,
        );
        setTimeout(healthCheck, 1500);
      }
    },
  });

  let store;
  if (IdbStore.available()) {
    store = new IdbStore(state.recordId);
    try {
      await store.list();
    } catch {
      store = new MemoryStore();
    }
  } else {
    store = new MemoryStore();
  }
  if (store instanceof MemoryStore) log("storage_fallback", { store: "memory" });
  state.store = store;
  const meta = await store.getMeta();
  state.sessions = (meta && meta.sessions) || [];

  state.queue = new UploadQueue({
    store,
    uploader: uploadChunk,
    onChange: (stats) => {
      state.uploadStats = stats;
      if (stats.lastError) log("upload_retry", { error: stats.lastError, failures: stats.consecutiveFailures });
      render();
    },
    onFatal: (err) => {
      log("upload_fatal", { error: String(err.message) });
      showError("録音用リンクが使えません", `${err.message}　未送信の音声はこの端末に残っています。`);
    },
  });

  if (state.test) setupTestPanel();
  wireEvents();

  if (meta && meta.completed) {
    state.phase = "done";
    render();
    return;
  }
  await state.queue.start();
  if (hasRecording()) {
    const pending = (await store.list()).length;
    $("resume-note").textContent =
      `この記録には前回の録音（${state.sessions.length} 区間）があります。` +
      (pending ? `未送信の ${pending} 件を送信しています。` : "") +
      "「録音を再開」で続きから録音、「録音終了」で送信を完了します。";
    show("resume-note", true);
  }
  state.phase = "ready";
  state.uiTimer = setInterval(render, 1000);
  setInterval(() => state.diag.flush(), 15000);
  render();
  log("page_ready", { resume: hasRecording(), test: state.test });
}

init().catch((err) => {
  showError("エラーが発生しました", String(err && err.message ? err.message : err));
});
