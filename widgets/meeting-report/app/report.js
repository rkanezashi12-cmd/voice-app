// 商談日報ウィジェット（PC の CRM の商談記録の画面）の表示に使う、画面に触れない関数。
// ブラウザでは window.MeetingReport、Node（tests/js/widget-report.test.mjs）では module.exports で使う。
// 項目の API 名は config.fields（scripts/build_widget.py が app/field_map.py から作る field-map.js）で受け取り、ここには書かない。
(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.MeetingReport = api;
})(typeof self !== "undefined" ? self : this, function () {
  "use strict";

  // 日報に出す項目と見出し（web/app/lib/view.js の REPORT_SECTIONS と同じ順。テストで揃っていることを確かめる）
  const REPORT_SECTIONS = [
    ["summary", "要約"],
    ["issues", "課題"],
    ["needs", "ニーズ"],
    ["budget", "予算"],
    ["decision_maker", "決裁者"],
    ["competitors", "競合"],
    ["next_actions", "次のアクション"],
    ["due_date", "次回期限"],
  ];

  // CRM のレコード ID（18〜19桁。数値にすると桁が落ちるので文字列で扱う）
  const RECORD_ID_RE = /^[0-9]{1,30}$/;
  // 「話者: 発言」の行（app/pipeline/models.py と同じ形。全角のコロンも受ける）
  const LINE_RE = /^\s*([^:：\n]{1,40}?)\s*[:：]\s*(.*)$/;

  function text(value) {
    if (typeof value === "string") return value;
    if (typeof value === "number") return String(value);
    return "";
  }

  function lookupName(value) {
    return value && typeof value === "object" && typeof value.name === "string" ? value.name : "";
  }

  function lookupId(value) {
    return value && typeof value === "object" && value.id != null ? String(value.id) : "";
  }

  function isRecordId(value) {
    return RECORD_ID_RE.test(String(value == null ? "" : value));
  }

  // 状態（選択肢の表示値）を、画面の出し分け（done / failed / processing / waiting / none）にする（app/visits.py と同じ分け方）
  function stateOf(status, values) {
    if (!status) return "none";
    if (status === values.done || status === values.no_account) return "done";
    if (status === values.failed || status === values.join_failed) return "failed";
    if (status === values.transcribing) return "processing";
    return "waiting";
  }

  // 文字起こし全文は2つの項目に分けて入っている（1つ目に入りきらない分が2つ目。行の切れ目で分けてある）
  function joinTranscript(first, second) {
    if (!second) return first;
    if (!first) return second;
    return first.endsWith("\n") ? first + second : `${first}\n${second}`;
  }

  // CRM のレコード（API 名がキー）を、画面で使う形にする
  function toView(record, config) {
    const f = config.fields;
    const view = {
      id: text(record.id),
      name: text(record[f.name]),
      status: text(record[f.status]),
      meetingType: text(record[f.meeting_type]),
      captureMethod: text(record[f.capture_method]),
      account: lookupName(record[f.account]),
      accountId: lookupId(record[f.account]),
      owner: lookupName(record[f.owner]),
      contacts: text(record[f.contact_name]),
      startAt: text(record[f.start_at]),
      category: text(record[f.category]),
      errorMessage: text(record[f.error_message]),
      transcript: joinTranscript(text(record[f.transcript]), text(record[f.transcript_2])),
    };
    for (const [key] of REPORT_SECTIONS) view[key] = text(record[f[key]]);
    view.state = stateOf(view.status, config.status);
    return view;
  }

  // 空の項目は出さない
  function reportSections(view) {
    return REPORT_SECTIONS.filter(([key]) => view[key] && view[key].trim()).map(([key, label]) => ({
      key,
      label,
      text: key === "due_date" ? dateLabel(view[key].trim()) : view[key].trim(),
    }));
  }

  // 日報がまだ無いとき・失敗したときの説明（日報ができていれば空）
  function noticeText(view) {
    if (view.state === "failed") return `処理に失敗しました。${view.errorMessage || "エラー内容は CRM の「エラー内容」を見てください。"}`;
    if (view.state === "processing") return "文字起こし・要約の途中です。数分たったら「再読み込み」を押してください。";
    if (view.state === "waiting") return `まだ日報はできていません（状態：${view.status}）。録音・会議が終わって処理が済むと、ここに出ます。`;
    return "";
  }

  // "2026-10-02T23:55:00+09:00" → "2026/10/02 23:55"（CRM が返した時刻帯のまま。端末の時刻帯に左右されない）
  function dateTimeLabel(iso) {
    const m = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})/.exec(String(iso || ""));
    return m ? `${m[1]}/${m[2]}/${m[3]} ${m[4]}:${m[5]}` : "";
  }

  // "2026-10-10" → "2026/10/10"（形が違えばそのまま）
  function dateLabel(value) {
    const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(String(value || ""));
    return m ? `${m[1]}/${m[2]}/${m[3]}` : String(value || "");
  }

  // 文字起こし全文を、話者ごとのまとまりにする。話者の無い行は直前の発言の続き。同じ話者が続けば1つにまとめる
  function parseTranscript(full) {
    const blocks = [];
    for (const raw of String(full || "").split(/\r?\n/)) {
      const line = raw.trim();
      if (!line) continue;
      const m = LINE_RE.exec(line);
      // 「10:30 から…」のような時刻は話者ではない（話者の末尾も発言の先頭も数字）
      const isSpeaker = m && !(/\d$/.test(m[1]) && /^\d/.test(m[2]));
      if (isSpeaker) {
        const speaker = m[1].trim();
        const last = blocks[blocks.length - 1];
        if (last && last.speaker === speaker) last.lines.push(m[2]);
        else blocks.push({ speaker, lines: [m[2]] });
      } else if (blocks.length) {
        blocks[blocks.length - 1].lines.push(line);
      } else {
        blocks.push({ speaker: "", lines: [line] });
      }
    }
    return blocks.map((b) => ({ speaker: b.speaker, text: b.lines.join("\n") }));
  }

  // 話者ごとの色の番号（出てきた順。6色を繰り返す）
  function speakerColors(blocks) {
    const index = new Map();
    for (const b of blocks) if (b.speaker && !index.has(b.speaker)) index.set(b.speaker, index.size % 6);
    return index;
  }

  // 検索語に当たるところで文を区切る（大文字・小文字は区別しない）。[{ text, hit }]
  function highlight(value, query) {
    const source = String(value || "");
    const q = String(query || "").trim();
    if (!q) return [{ text: source, hit: false }];
    const lower = source.toLowerCase();
    const needle = q.toLowerCase();
    const parts = [];
    let from = 0;
    for (;;) {
      const at = lower.indexOf(needle, from);
      if (at < 0) break;
      if (at > from) parts.push({ text: source.slice(from, at), hit: false });
      parts.push({ text: source.slice(at, at + needle.length), hit: true });
      from = at + needle.length;
    }
    if (from < source.length || !parts.length) parts.push({ text: source.slice(from), hit: false });
    return parts;
  }

  function countHits(blocks, query) {
    let n = 0;
    for (const b of blocks) n += highlight(b.text, query).filter((p) => p.hit).length;
    return n;
  }

  // SDK の失敗は Error ではなく応答そのもので返ってくる（zoho-crm-build の第11章）。中の code・message まで見て文にする
  function sdkErrorText(err) {
    const item = err && Array.isArray(err.data) ? err.data[0] : null;
    if (item && (item.code || item.message)) {
      const detail = item.details && typeof item.details === "object" ? ` ${JSON.stringify(item.details).slice(0, 200)}` : "";
      return `CRM から読めませんでした（${item.code || "エラー"}: ${item.message || ""}${detail}）`;
    }
    if (err && typeof err.message === "string" && err.message) return `CRM から読めませんでした（${err.message}）`;
    if (err && typeof err === "object") return `CRM から読めませんでした（${JSON.stringify(err).slice(0, 200)}）`;
    return `CRM から読めませんでした（${String(err)}）`;
  }

  return {
    REPORT_SECTIONS,
    isRecordId,
    stateOf,
    joinTranscript,
    toView,
    reportSections,
    noticeText,
    dateTimeLabel,
    dateLabel,
    parseTranscript,
    speakerColors,
    highlight,
    countHits,
    sdkErrorText,
  };
});
