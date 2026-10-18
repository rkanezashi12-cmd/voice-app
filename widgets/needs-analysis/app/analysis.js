// ニーズ分析ウィジェット（PC の CRM の「ニーズ分析」の画面）の表示に使う、画面に触れない関数。
// ブラウザでは window.NeedsAnalysisView、Node（tests/js/widget-needs.test.mjs）では module.exports で使う。
// 項目の API 名・選択肢は config（scripts/build_widget.py が app/field_map.py から作る field-map.js）で受け取り、ここには書かない。
// 「上位のニーズ」などの文の形は app/needs/analysis.py の _format と同じ（変えるときは両方を直す）。
(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.NeedsAnalysisView = api;
})(typeof self !== "undefined" ? self : this, function () {
  "use strict";

  const RECORD_ID_RE = /^[0-9]{1,30}$/;
  const DATE_RE = /^(\d{4})-(\d{2})-(\d{2})$/;
  // 期間が空のときの既定（app/needs/analysis.py の DEFAULT_PERIOD_DAYS と同じ）
  const DEFAULT_PERIOD_DAYS = 365;
  // COQL は1回 200件、OFFSET は 2,000 まで（zoho-crm-build の実測）
  const COQL_PAGE = 200;
  const COQL_MAX = 2000;

  function text(value) {
    if (typeof value === "string") return value;
    if (typeof value === "number") return String(value);
    return "";
  }

  function picklist(value) {
    if (value && typeof value === "object" && typeof value.display_value === "string") return value.display_value;
    return text(value);
  }

  function isRecordId(value) {
    return RECORD_ID_RE.test(String(value == null ? "" : value));
  }

  function isDate(value) {
    return DATE_RE.test(text(value));
  }

  function isoDate(d) {
    const pad = (n) => String(n).padStart(2, "0");
    return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
  }

  // 期間（空なら終わりは今日、始まりは終わりの365日前。バックエンドと同じ）
  function period(record, f, today) {
    const to = isDate(record[f.period_to]) ? record[f.period_to] : isoDate(today || new Date());
    let from = record[f.period_from];
    if (!isDate(from)) {
      const [y, m, d] = to.split("-").map(Number);
      from = isoDate(new Date(y, m - 1, d - DEFAULT_PERIOD_DAYS));
    }
    return { from, to };
  }

  // 状態（選択肢の表示値）を画面の出し分け（done / running / failed / idle）にする
  function stateOf(status, v) {
    if (status === v.status_done) return "done";
    if (status === v.status_running) return "running";
    if (status === v.status_failed) return "failed";
    return "idle";
  }

  function toView(record, config, today) {
    const f = config.analysis_fields;
    const v = config.values;
    const status = picklist(record[f.status]);
    return {
      id: text(record.id),
      name: text(record[f.name]),
      status,
      state: stateOf(status, v),
      period: period(record, f, today),
      targets: picklist(record[f.targets]) || v.target_both,
      counts: parseCounts(text(record[f.counts])),
      themes: parseThemes(text(record[f.top_needs])),
      matched: parseBlocks(text(record[f.matched_needs])),
      ideas: parseBlocks(text(record[f.product_ideas])),
      error: text(record[f.error_message]),
      analyzedAt: text(record[f.analyzed_at]),
    };
  }

  // 「商談 32件・WEB問合せ 30件 → ニーズ 95件（…）」から数を取り出す
  function parseCounts(value) {
    const num = (re) => {
      const m = value.match(re);
      return m ? Number(m[1]) : null;
    };
    return {
      meetings: num(/商談\s*(\d+)\s*件/),
      inquiries: num(/WEB問合せ\s*(\d+)\s*件/),
      needs: num(/ニーズ\s*(\d+)\s*件/),
      newNeeds: num(/新しく抜き出した\s*(\d+)\s*件/),
      raw: value,
    };
  }

  // 「1. テーマ（3件・2社）\n説明\n例：…」のまとまり（空行区切り）を、テーマごとに取り出す
  const THEME_HEAD = /^\s*(\d+)\.\s*(.+?)（(\d+)件・(\d+)社）\s*$/;
  function parseThemes(value) {
    const themes = [];
    for (const block of value.split(/\n\s*\n/)) {
      const lines = block.split("\n");
      const head = lines[0] ? lines[0].match(THEME_HEAD) : null;
      if (!head) continue;
      const rest = lines.slice(1);
      const example = rest.find((l) => l.startsWith("例："));
      themes.push({
        rank: Number(head[1]),
        theme: head[2],
        count: Number(head[3]),
        companies: Number(head[4]),
        description: rest.filter((l) => l !== example).join("\n").trim(),
        examples: example ? example.slice(2) : "",
      });
    }
    return themes;
  }

  // 「■ 見出し\nラベル：内容\n…」のまとまりを取り出す（応えられるニーズ・新商品開発の候補）
  function parseBlocks(value) {
    const blocks = [];
    for (const chunk of value.split(/\n\s*\n/)) {
      const lines = chunk.split("\n").filter((l) => l.trim());
      if (!lines.length || !lines[0].startsWith("■")) continue;
      const rows = [];
      for (const line of lines.slice(1)) {
        const m = line.match(/^([^：]{1,20})：(.*)$/);
        if (m) rows.push({ label: m[1], text: m[2] });
        else if (rows.length) rows[rows.length - 1].text += `\n${line}`;
        else rows.push({ label: "", text: line });
      }
      blocks.push({ title: lines[0].replace(/^■\s*/, ""), rows });
    }
    return blocks;
  }

  // COQL の値に入れる文字（日付・選択肢の表示値）。シングルクォートは入れない
  function coqlString(value) {
    return `'${text(value).replace(/'/g, "")}'`;
  }

  // 期間内のニーズを読む COQL（バックエンドと同じく、日付で絞り、対象が片方だけなら出典でも絞る）
  function needsQuery(view, config, offset) {
    const f = config.need_fields;
    const v = config.values;
    const fields = ["id", f.name, f.detail, f.need_type, f.source_type, f.company, f.evidence, f.need_date, f.extract_key];
    const where = [`${f.need_date} >= ${coqlString(view.period.from)}`, `${f.need_date} <= ${coqlString(view.period.to)}`];
    if (view.targets === v.target_meetings) where.push(`${f.source_type} = ${coqlString(v.source_meeting)}`);
    if (view.targets === v.target_inquiries) where.push(`${f.source_type} = ${coqlString(v.source_inquiry)}`);
    return (
      `select ${fields.join(", ")} from ${config.need_module} where ${where.join(" and ")} ` +
      `order by ${f.need_date} desc limit ${COQL_PAGE} offset ${offset}`
    );
  }

  function toNeed(row, config) {
    const f = config.need_fields;
    const key = text(row[f.extract_key]);
    const [kind, sourceId] = key.split(":");
    const module = kind === "meeting" ? config.meeting_module : kind === "inquiry" ? config.inquiry_module : "";
    return {
      id: text(row.id),
      title: text(row[f.name]),
      detail: text(row[f.detail]),
      type: picklist(row[f.need_type]),
      source: picklist(row[f.source_type]),
      company: text(row[f.company]),
      evidence: text(row[f.evidence]),
      date: text(row[f.need_date]),
      sourceModule: module,
      sourceId: module && isRecordId(sourceId) ? sourceId : "",
    };
  }

  // 数える（order にある値をその順で先に、残りは多い順。空は「未設定」）
  function countBy(items, key, order) {
    const counts = new Map();
    for (const item of items) {
      const k = item[key] || "未設定";
      counts.set(k, (counts.get(k) || 0) + 1);
    }
    const head = (order || []).filter((k) => counts.has(k)).map((k) => ({ label: k, count: counts.get(k) }));
    const rest = [...counts.entries()]
      .filter(([k]) => !(order || []).includes(k))
      .map(([label, count]) => ({ label, count }))
      .sort((a, b) => b.count - a.count || a.label.localeCompare(b.label, "ja"));
    return [...head, ...rest];
  }

  function companyCount(needs) {
    return new Set(needs.map((n) => n.company).filter(Boolean)).size;
  }

  // 一覧の絞り込み（種類・出典・言葉）
  function filterNeeds(needs, filter) {
    const q = text(filter.q).trim().toLowerCase();
    return needs.filter(
      (n) =>
        (!filter.type || n.type === filter.type) &&
        (!filter.source || n.source === filter.source) &&
        (!filter.company || n.company === filter.company) &&
        (!q || [n.title, n.detail, n.company, n.evidence].some((s) => s.toLowerCase().includes(q))),
    );
  }

  function stripTest(name) {
    return text(name).replace(/^【TEST】/, "");
  }

  function dateLabel(value) {
    const m = text(value).match(DATE_RE);
    return m ? `${m[1]}/${m[2]}/${m[3]}` : "-";
  }

  function dateTimeLabel(value) {
    const m = text(value).match(/^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})/);
    return m ? `${m[1]}/${m[2]}/${m[3]} ${m[4]}:${m[5]}` : "-";
  }

  // 状態ごとの説明（空なら出さない）
  function noticeText(view) {
    if (view.state === "running") return "分析の途中です。数分後に「再読み込み」を押してください。";
    if (view.state === "failed") return view.error || "分析に失敗しました。";
    if (view.state === "idle") return "まだ分析していません。この記録の「ニーズ分析実行」ボタンを押すと、数分後に結果が入ります。";
    return "";
  }

  // SDK は失敗すると Error ではなく応答そのもので reject する（zoho-crm-build の実測）。code・message まで出す
  function sdkErrorText(err) {
    const item = err && Array.isArray(err.data) ? err.data[0] : null;
    if (item && (item.code || item.message)) {
      const detail = item.details && typeof item.details === "object" ? ` ${JSON.stringify(item.details).slice(0, 200)}` : "";
      return `CRM から読めませんでした（${item.code || ""}: ${item.message || ""}${detail}）`;
    }
    if (err && typeof err.message === "string") return `CRM から読めませんでした（${err.message}）`;
    return "CRM から読めませんでした";
  }

  return {
    COQL_PAGE,
    COQL_MAX,
    isRecordId,
    period,
    stateOf,
    toView,
    parseCounts,
    parseThemes,
    parseBlocks,
    needsQuery,
    toNeed,
    countBy,
    companyCount,
    filterNeeds,
    stripTest,
    dateLabel,
    dateTimeLabel,
    noticeText,
    sdkErrorText,
  };
});
