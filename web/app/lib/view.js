// 録音アプリ（/app/）の表示用の純粋な関数（DOM を触らない。tests/js/app-view.test.mjs で検査する）。

export const TABS = ["input", "record", "report"];

// "#report=123" → { tab: "report", reportId: "123" }。知らない値は入力に戻す
export function parseHash(hash) {
  const raw = String(hash || "").replace(/^#/, "");
  const [name, value] = raw.split("=", 2);
  const tab = TABS.includes(name) ? name : "input";
  const reportId = tab === "report" && value && /^[0-9]{1,30}$/.test(value) ? value : null;
  return { tab, reportId };
}

const LOGIN_ERRORS = {
  denied: "ログインが取り消されました。もう一度ログインしてください。",
  expired: "ログインの途中で時間が過ぎました。もう一度ログインしてください。",
  not_crm_user: "このアカウントは、この CRM のユーザーではありません。会社の Zoho CRM のアカウントでログインしてください。",
  no_api_access:
    "CRM のプロファイルで API の利用（Zoho CRM API Access）が許可されていません。CRM の管理者に許可してもらってください。",
  inactive: "CRM のユーザーが有効ではありません。CRM の管理者に確認してください。",
  failed: "ログインを確かめられませんでした。少し待ってから、もう一度ログインしてください。",
};

export function loginErrorText(code) {
  if (!code) return "";
  return LOGIN_ERRORS[code] || LOGIN_ERRORS.failed;
}

export function geoErrorText(code) {
  if (code === 1) return "位置情報の利用が許可されていません（ブラウザの設定で許可してください）";
  if (code === 3) return "時間内に位置を取得できませんでした（もう一度押してください）";
  return "位置を取得できませんでした";
}

export function joinNames(names) {
  return (names || []).filter((n) => n && n.trim()).join("、");
}

// 入力画面の下に出す、選んだ訪問先と担当者の説明
export function describeSelection(selected, contacts) {
  if (!selected) return "訪問先を選択してください。";
  const name = selected.newCustomer ? `${selected.name}（新規）` : selected.name;
  const people = joinNames(contacts);
  return `訪問先：${name}　担当者：${people || "未選択"}`;
}

// 一覧のオプションの文字（会社名と、候補になった理由）
export function candidateLabel(account) {
  return account.reason ? `${account.name}（${account.reason}）` : account.name;
}

// 日報の状態の表示（API の state：waiting / processing / done / failed）
export function stateLabel(visit) {
  if (!visit) return "";
  if (visit.state === "done") return visit.status || "完了";
  if (visit.state === "failed") return visit.status || "失敗";
  if (visit.state === "processing") return "文字起こし・要約中";
  return "録音待ち・送信待ち";
}

export function isPending(visit) {
  return Boolean(visit) && (visit.state === "waiting" || visit.state === "processing");
}

// 日報に出す項目（API のキーと見出し）。空の項目は出さない
export const REPORT_SECTIONS = [
  ["summary", "要約"],
  ["issues", "課題"],
  ["needs", "ニーズ"],
  ["budget", "予算"],
  ["decision_maker", "決裁者"],
  ["competitors", "競合"],
  ["next_actions", "次のアクション"],
  ["due_date", "次回期限"],
];

export function reportSections(visit) {
  if (!visit) return [];
  return REPORT_SECTIONS.filter(([key]) => typeof visit[key] === "string" && visit[key].trim()).map(([key, label]) => ({
    label,
    text: visit[key].trim(),
  }));
}

// "2026-10-01T10:05:00+09:00" → "10:05"。CRM が返した時刻帯のまま出す（端末の時刻帯に左右されない）
export function timeLabel(iso) {
  const m = /T(\d{2}):(\d{2})/.exec(String(iso || ""));
  return m ? `${m[1]}:${m[2]}` : "";
}

// GPS の状態の表示（誤差が分からない・0 のときは出さない）
export function gpsLabel(place, accuracy) {
  const where = place ? `${place}付近` : "住所不明";
  const meters = Math.round(Number(accuracy) || 0);
  return meters > 0 ? `取得済み（${where}・誤差 約${meters}m）` : `取得済み（${where}）`;
}

// API のエラー本文（FastAPI の detail・アプリの message）から画面に出す文
export function errorMessage(body, status) {
  if (body && typeof body.detail === "string") return body.detail;
  if (body && typeof body.message === "string") return body.message;
  if (body && Array.isArray(body.detail) && body.detail.length) return "入力の形が正しくありません";
  return `エラーが発生しました（${status}）`;
}
