// 動画の解析の画面（/app/videos.html）の純粋なロジック（tests/js/videos.test.mjs で確かめる）。

// 読める動画の拡張子（app/videos/analysis.py の VIDEO_MIME と合わせる）
export const VIDEO_EXTENSIONS = ["mp4", "m4v", "mov", "webm", "avi", "mpeg", "mpg", "wmv", "3gp"];

export function extensionOf(name) {
  const m = /\.([A-Za-z0-9]+)$/.exec(String(name || ""));
  return m ? m[1].toLowerCase() : "";
}

export function isVideoFile(name) {
  return VIDEO_EXTENSIONS.includes(extensionOf(name));
}

export function formatBytes(bytes) {
  const n = Number(bytes) || 0;
  if (n >= 1024 ** 3) return `${(n / 1024 ** 3).toFixed(1)}GB`;
  if (n >= 1024 ** 2) return `${Math.round(n / 1024 ** 2)}MB`;
  if (n >= 1024) return `${Math.round(n / 1024)}KB`;
  return `${n}B`;
}

export function percent(loaded, total) {
  if (!total) return 0;
  return Math.max(0, Math.min(100, Math.floor((loaded / total) * 100)));
}

// 一覧の状態の見せ方：done（完了）/ failed（失敗）/ working（それ以外。解析中）
export function statusKind(status, labels) {
  if (status && labels && status === labels.done) return "done";
  if (status && labels && status === labels.failed) return "failed";
  return "working";
}

export function hasWorking(videos, labels) {
  return (videos || []).some((v) => statusKind(v.status, labels) === "working");
}

export function dateLabel(iso) {
  const m = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})/.exec(String(iso || ""));
  return m ? `${Number(m[2])}/${Number(m[3])} ${m[4]}:${m[5]}` : "";
}
