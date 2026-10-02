// 録音アプリ（/app/）：入力（訪問先・担当者）→ 録音（録音ページへ）→ 日報（処理の結果）。
// ログインは Zoho アカウント（/auth/login）。API は同じサイトの /api/app/*（Cookie）。
// 位置・検索語・顧客名は画面とサーバーの間だけで使い、この端末には訪問先の選択の途中だけを残す（sessionStorage）。

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
} from "./lib/view.js";

const $ = (id) => document.getElementById(id);
const APP_HEADER = { "X-Requested-With": "meeting-notes-app" };
const DRAFT_KEY = "app.draft";
const LAST_RECORD_KEY = "app.lastRecord";
const POLL_MS = 15000;
const POLL_MAX = 40;

const state = {
  client: "default",
  me: null,
  candidates: [],
  selected: null, // { id, name, newCustomer }
  contacts: [], // 選んだ担当者の名前
  contactIds: [], // 選んだ担当者のうち CRM の連絡先の ID（先方担当者（連絡先）に紐づける）
  contactOptions: [], // 担当者の候補 { id, name, detail }
  pollTimer: null,
  pollCount: 0,
};

class LoginRequired extends Error {}

// ---- API ----

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
  return res.status === 204 ? null : res.json();
}

// ---- 表示の切り替え ----

function show(id, visible) {
  $(id).hidden = !visible;
}

function toast(message) {
  const el = $("toast");
  el.textContent = message;
  el.hidden = false;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => {
    el.hidden = true;
  }, 4000);
}

function setChrome(visible) {
  document.querySelector(".tabbar").hidden = !visible;
  document.querySelector(".steps").hidden = !visible;
}

function showLogin(errorCode) {
  for (const id of ["view-loading", "view-error", "tab-input", "tab-record", "tab-report"]) show(id, false);
  setChrome(false);
  $("btn-login").href = `/auth/login?c=${encodeURIComponent(state.client)}`;
  const message = loginErrorText(errorCode);
  $("login-error").textContent = message;
  show("login-error", Boolean(message));
  show("view-login", true);
}

function showError(message) {
  for (const id of ["view-loading", "view-login", "tab-input", "tab-record", "tab-report"]) show(id, false);
  setChrome(false);
  $("error-message").textContent = message;
  show("view-error", true);
}

function handleError(err) {
  if (err instanceof LoginRequired) {
    showLogin("");
    return;
  }
  toast(err && err.message ? err.message : String(err));
}

function route() {
  if (!state.me) return;
  const { tab, reportId } = parseHash(window.location.hash);
  for (const name of ["input", "record", "report"]) show(`tab-${name}`, name === tab);
  for (const el of document.querySelectorAll(".step")) el.classList.toggle("active", el.dataset.step === tab);
  for (const el of document.querySelectorAll(".tabbar a")) el.classList.toggle("active", el.dataset.tab === tab);
  stopPolling();
  if (tab === "record") renderRecord();
  if (tab === "report") loadReport(reportId || readLastRecord()).catch(handleError);
  window.scrollTo(0, 0);
}

// ---- 入力：選択の途中を残す ----

function saveDraft() {
  try {
    window.sessionStorage.setItem(
      DRAFT_KEY,
      JSON.stringify({ selected: state.selected, contacts: state.contacts, contactIds: state.contactIds }),
    );
  } catch {
    // 保存できなくても動作には影響しない
  }
}

function restoreDraft() {
  try {
    const draft = JSON.parse(window.sessionStorage.getItem(DRAFT_KEY) || "null");
    if (draft && draft.selected && typeof draft.selected.name === "string") {
      state.selected = draft.selected;
      state.contacts = Array.isArray(draft.contacts) ? draft.contacts.filter((n) => typeof n === "string") : [];
      state.contactIds = Array.isArray(draft.contactIds)
        ? draft.contactIds.filter((id) => typeof id === "string" && /^[0-9]{1,30}$/.test(id))
        : [];
    }
  } catch {
    // 読めなければ最初から
  }
}

function clearDraft() {
  state.selected = null;
  state.contacts = [];
  state.contactIds = [];
  try {
    window.sessionStorage.removeItem(DRAFT_KEY);
  } catch {
    // 無視
  }
}

function readLastRecord() {
  try {
    const id = window.sessionStorage.getItem(LAST_RECORD_KEY);
    return id && /^[0-9]{1,30}$/.test(id) ? id : null;
  } catch {
    return null;
  }
}

function renderSelection() {
  const el = $("selection");
  el.textContent = describeSelection(state.selected, state.contacts);
  el.classList.toggle("chosen", Boolean(state.selected));
  $("btn-contacts").disabled = !state.selected;
  $("btn-to-record").disabled = !state.selected;
}

function select(account) {
  state.selected = account;
  state.contacts = [];
  state.contactIds = [];
  state.contactOptions = [];
  show("contact-picker", false);
  saveDraft();
  renderSelection();
}

// ---- 入力：GPS・検索 ----

function setGpsState(text) {
  $("gps-state").textContent = text;
}

function setCandidates(accounts, emptyText) {
  state.candidates = accounts;
  const sel = $("sel-account");
  sel.replaceChildren();
  const head = accounts.length ? `候補 ${accounts.length} 件から選んでください` : emptyText || "候補が見つかりませんでした";
  sel.add(new Option(head, ""));
  for (const a of accounts) sel.add(new Option(candidateLabel(a), a.id));
  if (state.selected && !state.selected.newCustomer && accounts.some((a) => a.id === state.selected.id)) {
    sel.value = state.selected.id;
  }
}

function currentPosition() {
  return new Promise((resolve, reject) => {
    navigator.geolocation.getCurrentPosition(resolve, reject, {
      enableHighAccuracy: true,
      timeout: 15000,
      maximumAge: 60000,
    });
  });
}

async function onGps() {
  if (!navigator.geolocation) {
    setGpsState("このブラウザでは位置情報を使えません");
    return;
  }
  const btn = $("btn-gps");
  btn.disabled = true;
  setGpsState("取得中…");
  try {
    let pos;
    try {
      pos = await currentPosition();
    } catch (err) {
      setGpsState(geoErrorText(err && err.code));
      return;
    }
    setGpsState("取得済み（近くの顧客を探しています…）");
    const data = await api("/api/app/accounts/nearby", {
      method: "POST",
      body: { lat: pos.coords.latitude, lng: pos.coords.longitude },
    });
    setGpsState(gpsLabel(data.place, pos.coords.accuracy));
    show("gps-attribution", true);
    setCandidates(data.accounts, "近くに CRM の顧客が見つかりませんでした（顧客検索で探してください）");
    if (data.accounts.length) $("sel-account").focus();
  } catch (err) {
    handleError(err);
  } finally {
    btn.disabled = !(state.me && state.me.gps_available);
  }
}

async function onSearch() {
  const q = $("q").value.trim();
  if (!q) {
    toast("会社名・住所・担当者名を入れてください");
    return;
  }
  const btn = $("btn-search");
  btn.disabled = true;
  try {
    const data = await api("/api/app/accounts/search", { method: "POST", body: { q } });
    setCandidates(data.accounts, "見つかりませんでした（別の言葉で探すか、新規顧客を登録してください）");
    if (data.accounts.length) $("sel-account").focus();
  } catch (err) {
    handleError(err);
  } finally {
    btn.disabled = false;
  }
}

function onCandidateChange() {
  const id = $("sel-account").value;
  const account = state.candidates.find((a) => a.id === id);
  select(account ? { id: account.id, name: account.name, newCustomer: false } : null);
}

// ---- 入力：新規顧客・担当者 ----

function onNewCustomer() {
  show("contact-picker", false);
  show("new-form", true);
  $("new-name").value = state.selected && state.selected.newCustomer ? state.selected.name : "";
  $("new-name").focus();
}

function onNewCustomerOk() {
  const name = $("new-name").value.trim();
  if (!name) {
    toast("会社名を入れてください");
    return;
  }
  $("sel-account").value = "";
  show("new-form", false);
  select({ id: null, name, newCustomer: true });
}

function renderContactList() {
  const list = $("contact-list");
  list.replaceChildren();
  const names = [...state.contactOptions.map((c) => c.name)];
  for (const n of state.contacts) if (!names.includes(n)) names.push(n);
  for (const name of names) {
    const option = state.contactOptions.find((c) => c.name === name);
    const li = document.createElement("li");
    const label = document.createElement("label");
    const box = document.createElement("input");
    box.type = "checkbox";
    box.value = name;
    if (option) box.dataset.id = option.id;
    box.checked = state.contacts.includes(name);
    label.append(box, document.createTextNode(name));
    if (option && option.detail) {
      const small = document.createElement("small");
      small.textContent = option.detail;
      label.append(small);
    } else if (!option) {
      const small = document.createElement("small");
      small.textContent = "（追加）";
      label.append(small);
    }
    li.append(label);
    list.append(li);
  }
}

async function onContacts() {
  if (!state.selected) return;
  show("new-form", false);
  show("contact-picker", true);
  state.contactOptions = [];
  if (state.selected.newCustomer) {
    $("contact-hint").textContent = "新規顧客なので、担当者の名前を追加してください（複数可）";
    renderContactList();
    return;
  }
  $("contact-hint").textContent = "読み込み中…";
  renderContactList();
  try {
    const data = await api(`/api/app/accounts/${encodeURIComponent(state.selected.id)}/contacts`);
    state.contactOptions = data.contacts;
    $("contact-hint").textContent = data.contacts.length
      ? "担当者を選んでください（複数可）"
      : "CRM に担当者が登録されていません。名前を追加してください";
    renderContactList();
  } catch (err) {
    $("contact-hint").textContent = "担当者を読み込めませんでした。名前を追加できます";
    handleError(err);
  }
}

function checkedBoxes() {
  return [...$("contact-list").querySelectorAll("input[type=checkbox]")].filter((b) => b.checked);
}

function checkedNames() {
  return checkedBoxes().map((b) => b.value);
}

// CRM の連絡先から選んだ人の ID（手で足した名前には ID が無い）
function checkedIds() {
  return [...new Set(checkedBoxes().map((b) => b.dataset.id).filter(Boolean))];
}

function onExtraAdd() {
  const input = $("extra-contact");
  const name = input.value.trim();
  if (!name) return;
  state.contacts = [...new Set([...checkedNames(), name])];
  input.value = "";
  renderContactList();
}

function onContactsDone() {
  state.contacts = checkedNames();
  state.contactIds = checkedIds();
  show("contact-picker", false);
  saveDraft();
  renderSelection();
}

// ---- 録音 ----

function renderRecord() {
  const sel = state.selected;
  $("rec-account").textContent = sel ? (sel.newCustomer ? `${sel.name}（新規）` : sel.name) : "-";
  $("rec-contacts").textContent = joinNames(state.contacts) || "-";
  show("rec-missing", !sel);
  $("btn-open-recorder").disabled = !sel;
}

async function onOpenRecorder() {
  const sel = state.selected;
  if (!sel) return;
  const btn = $("btn-open-recorder");
  btn.disabled = true;
  try {
    const data = await api("/api/app/visits", {
      method: "POST",
      body: {
        account_id: sel.newCustomer ? null : sel.id,
        account_name: sel.name,
        new_customer: Boolean(sel.newCustomer),
        contacts: state.contacts,
        contact_ids: sel.newCustomer ? [] : state.contactIds,
      },
    });
    try {
      window.sessionStorage.setItem(LAST_RECORD_KEY, data.record_id);
    } catch {
      // 無視
    }
    clearDraft();
    window.location.href = data.recording_url;
  } catch (err) {
    handleError(err);
    btn.disabled = false;
  }
}

// ---- 日報 ----

function stopPolling() {
  clearTimeout(state.pollTimer);
  state.pollTimer = null;
}

function chip(visit) {
  const span = document.createElement("span");
  span.className = `chip ${visit.state === "done" ? "done" : visit.state === "failed" ? "failed" : ""}`;
  span.textContent = stateLabel(visit);
  return span;
}

function renderDetail(visit) {
  show("report-detail", true);
  $("rd-title").textContent = visit.account ? `日報：${visit.account}` : `日報：${visit.name}`;
  $("rd-account").textContent = visit.account || visit.name || "-";
  $("rd-contacts").textContent = visit.contacts || "-";
  $("rd-status").replaceChildren(chip(visit));
  show("rd-progress", isPending(visit));
  const err = visit.state === "failed" ? visit.error_message || "処理に失敗しました" : "";
  $("rd-error").textContent = err;
  show("rd-error", Boolean(err));
  const body = $("rd-body");
  body.replaceChildren();
  for (const section of reportSections(visit)) {
    const div = document.createElement("div");
    div.className = "report-section";
    const h3 = document.createElement("h3");
    h3.textContent = section.label;
    const p = document.createElement("p");
    p.textContent = section.text;
    div.append(h3, p);
    body.append(div);
  }
}

function renderList(visits) {
  const list = $("visit-list");
  list.replaceChildren();
  for (const v of visits) {
    const li = document.createElement("li");
    const a = document.createElement("a");
    a.href = `#report=${v.id}`;
    const time = document.createElement("span");
    time.textContent = timeLabel(v.start_at);
    const name = document.createElement("span");
    name.textContent = v.account || v.name;
    a.append(time, name, chip(v));
    li.append(a);
    list.append(li);
  }
  show("visit-empty", visits.length === 0);
}

async function loadReport(recordId) {
  show("report-detail", false);
  const listPromise = api("/api/app/visits").then((data) => renderList(data.visits));
  if (recordId) {
    state.pollCount = 0;
    await refreshDetail(recordId);
  }
  await listPromise;
}

async function refreshDetail(recordId) {
  let visit;
  try {
    visit = await api(`/api/app/visits/${encodeURIComponent(recordId)}`);
  } catch (err) {
    handleError(err);
    return null;
  }
  renderDetail(visit);
  if (isPending(visit) && state.pollCount < POLL_MAX && parseHash(window.location.hash).tab === "report") {
    state.pollCount += 1;
    state.pollTimer = setTimeout(async () => {
      const next = await refreshDetail(recordId);
      // 処理が終わったら一覧の状態も更新する
      if (next && !isPending(next)) api("/api/app/visits").then((d) => renderList(d.visits), handleError);
    }, POLL_MS);
  }
  return visit;
}

// ---- 初期化 ----

async function onLogout() {
  try {
    await api("/auth/logout", { method: "POST" });
  } catch {
    // 失敗しても画面はログインに戻す
  }
  clearDraft();
  showLogin("");
}

function wireEvents() {
  $("btn-gps").addEventListener("click", onGps);
  $("btn-search").addEventListener("click", onSearch);
  $("q").addEventListener("keydown", (e) => {
    if (e.key === "Enter") {
      e.preventDefault();
      onSearch();
    }
  });
  $("sel-account").addEventListener("change", onCandidateChange);
  $("btn-new").addEventListener("click", onNewCustomer);
  $("btn-new-cancel").addEventListener("click", () => show("new-form", false));
  $("btn-new-ok").addEventListener("click", onNewCustomerOk);
  $("btn-contacts").addEventListener("click", onContacts);
  $("btn-extra-add").addEventListener("click", onExtraAdd);
  $("btn-contacts-done").addEventListener("click", onContactsDone);
  $("btn-to-record").addEventListener("click", () => {
    window.location.hash = "#record";
  });
  $("btn-open-recorder").addEventListener("click", onOpenRecorder);
  $("btn-logout").addEventListener("click", onLogout);
  window.addEventListener("hashchange", route);
}

async function init() {
  const params = new URLSearchParams(window.location.search);
  const client = params.get("c");
  if (client && /^[a-z0-9][a-z0-9-]{0,31}$/.test(client)) state.client = client;
  const loginError = params.get("login_error");
  wireEvents();
  try {
    state.me = await api("/api/app/me");
  } catch (err) {
    if (err instanceof LoginRequired) {
      showLogin(loginError);
      return;
    }
    showError(err && err.message ? err.message : "読み込めませんでした。ページを再読み込みしてください。");
    return;
  }
  if (loginError) window.history.replaceState(null, "", `${window.location.pathname}${window.location.hash}`);
  show("view-loading", false);
  setChrome(true);
  $("user-name").textContent = state.me.user.name || state.me.user.email || "-";
  if (!state.me.gps_available) {
    $("btn-gps").disabled = true;
    setGpsState("使えません（地図のキーが未設定）");
  }
  restoreDraft();
  if (state.selected) {
    setCandidates(state.selected.newCustomer ? [] : [state.selected], "");
    if (!state.selected.newCustomer) $("sel-account").value = state.selected.id;
  }
  renderSelection();
  route();
}

init().catch((err) => showError(String(err && err.message ? err.message : err)));
