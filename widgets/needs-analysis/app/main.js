// ニーズ分析ウィジェット：CRM の「ニーズ分析」の画面（関連リスト）で、分析の結果をグラフと一覧で見せる。読むだけで、CRM には書かない。
// 読むのは、開いている「ニーズ分析」の記録1件と、その期間の「ニーズ」（COQL）。開いた人の CRM の権限で読む。
// SDK（ZohoEmbededAppSDK.min.js）と項目の API 名（field-map.js）は scripts/build_widget.py が ZIP に入れる。
(function () {
  "use strict";
  const V = window.NeedsAnalysisView;
  const C = window.NEEDS_ANALYSIS_CONFIG;
  const $ = (id) => document.getElementById(id);
  const state = { id: "", view: null, needs: [], truncated: false, filter: { type: "", source: "", company: "", q: "" } };

  function el(tag, className, content) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (content != null) node.textContent = content;
    return node;
  }

  function showMessage(content, kind) {
    const box = $("message");
    box.textContent = content || "";
    box.className = `message${kind ? ` ${kind}` : ""}`;
    box.hidden = !content;
  }

  // 関連リストの枠の高さを中身に合わせる（商談日報ウィジェットと同じ測り方。断られても表示は続ける）
  function resize() {
    const height = Math.min(Math.max(Math.ceil($("root").getBoundingClientRect().height) + 8, 320), 4000);
    try {
      Promise.resolve(ZOHO.CRM.UI.Resize({ height: String(height) })).catch(() => {});
    } catch {
      // SDK に Resize が無い
    }
  }

  // ---- ツールチップ（棒に乗せると件数と割合を出す） ----
  function bindTip(node, textFn) {
    const tip = $("tip");
    const show = (x, y) => {
      tip.textContent = textFn();
      tip.hidden = false;
      const w = tip.offsetWidth;
      tip.style.left = `${Math.min(x + 12, window.innerWidth - w - 8)}px`;
      tip.style.top = `${y + 14}px`;
    };
    node.addEventListener("mousemove", (e) => show(e.clientX, e.clientY));
    node.addEventListener("mouseleave", () => (tip.hidden = true));
    node.addEventListener("focus", () => {
      const r = node.getBoundingClientRect();
      show(r.left, r.top + r.height / 2);
    });
    node.addEventListener("blur", () => (tip.hidden = true));
  }

  function percent(count, total) {
    return total ? `${Math.round((count / total) * 100)}%` : "0%";
  }

  // ---- 横棒 ----
  function barRow(label, count, max, valueText, key, value) {
    const row = el("button", "bar-row");
    row.type = "button";
    row.dataset.key = key;
    row.dataset.value = value;
    const track = el("span", "bar-track");
    const fill = el("span", "bar-fill");
    fill.style.display = "block";
    fill.style.width = `${max ? (count / max) * 100 : 0}%`;
    track.append(fill);
    row.append(el("span", "bar-label", label), track, el("span", "bar-value", valueText));
    row.setAttribute("aria-label", `${label} ${valueText}`);
    row.addEventListener("click", () => toggleFilter(key, value));
    bindTip(row, () => `${label}\n${valueText}（全体の${percent(count, state.needs.length)}）`);
    return row;
  }

  function renderBars(boxId, items, key) {
    const box = $(boxId);
    box.replaceChildren();
    const max = Math.max(0, ...items.map((i) => i.count));
    for (const item of items) box.append(barRow(item.label, item.count, max, `${item.count}件`, key, item.label));
    if (!items.length) box.append(el("p", "muted", "ニーズはありません。"));
  }

  function renderThemes(themes) {
    const box = $("c-themes");
    box.replaceChildren();
    const max = Math.max(0, ...themes.map((t) => t.count));
    themes.forEach((t, i) => {
      const li = el("li");
      const details = el("details");
      const summary = el("summary", "bar-row");
      const track = el("span", "bar-track");
      const fill = el("span", "bar-fill");
      fill.style.display = "block";
      fill.style.width = `${max ? (t.count / max) * 100 : 0}%`;
      track.append(fill);
      summary.append(
        el("span", "rank", String(i + 1)),
        el("span", "bar-label", t.theme),
        track,
        el("span", "bar-value", `${t.count}件・${t.companies}社`),
      );
      bindTip(summary, () => `${t.theme}\n${t.count}件・${t.companies}社（押すと説明）`);
      const body = el("div", "theme-body", t.description);
      if (t.examples) body.append(el("p", "examples", `例：${t.examples}`));
      details.append(summary, body);
      details.addEventListener("toggle", resize);
      li.append(details);
      box.append(li);
    });
    $("c-themes-empty").hidden = themes.length > 0;
  }

  function renderSources(items) {
    const box = $("c-sources");
    box.replaceChildren();
    const total = items.reduce((s, i) => s + i.count, 0);
    if (!total) {
      box.append(el("p", "muted", "ニーズはありません。"));
      return;
    }
    // 色は出典ごとに決める（商談＝1色目、WEB問合せ＝2色目。絞り込んでも色は変えない）
    const slot = (label) => (label === C.values.source_meeting ? "s1" : "s2");
    const stack = el("div", "stack");
    const legend = el("ul", "legend");
    for (const item of items) {
      const seg = el("button", slot(item.label));
      seg.type = "button";
      seg.style.flex = String(item.count);
      seg.dataset.key = "source";
      seg.dataset.value = item.label;
      seg.setAttribute("aria-label", `${item.label} ${item.count}件`);
      seg.addEventListener("click", () => toggleFilter("source", item.label));
      bindTip(seg, () => `${item.label}\n${item.count}件（${percent(item.count, total)}）`);
      stack.append(seg);
      const li = el("li");
      li.append(el("span", `swatch ${slot(item.label)}`), el("span", null, `${item.label} ${item.count}件（${percent(item.count, total)}）`));
      legend.append(li);
    }
    box.append(stack, legend);
  }

  function renderCards(boxId, blocks, emptyText) {
    const box = $(boxId);
    box.replaceChildren();
    for (const b of blocks) {
      const card = el("article", "card");
      card.append(el("h3", null, b.title));
      const dl = el("dl");
      for (const row of b.rows) dl.append(el("dt", null, row.label), el("dd", null, row.text));
      card.append(dl);
      box.append(card);
    }
    if (!blocks.length) box.append(el("p", "muted", emptyText));
  }

  // ---- 一覧と絞り込み ----
  function fillSelect(id, labels, current) {
    const select = $(id);
    const first = select.options[0];
    select.replaceChildren(first);
    for (const label of labels) {
      const opt = el("option", null, label);
      opt.value = label;
      select.append(opt);
    }
    select.value = current;
  }

  function openRecord(entity, id) {
    if (!entity || !V.isRecordId(id)) return;
    Promise.resolve(ZOHO.CRM.UI.Record.open({ Entity: entity, RecordID: id })).catch((err) =>
      showMessage(V.sdkErrorText(err), "error"),
    );
  }

  function renderList() {
    const rows = V.filterNeeds(state.needs, state.filter);
    const body = $("l-body");
    body.replaceChildren();
    for (const n of rows) {
      const tr = el("tr");
      const title = el("td");
      const open = el("button", "link", V.stripTest(n.title) || "（見出しなし）");
      open.type = "button";
      open.addEventListener("click", () => openRecord(C.need_module, n.id));
      title.append(open);
      if (n.detail) title.append(el("div", "detail", n.detail));
      const src = el("td");
      const srcBtn = el("button", "link", n.sourceModule ? "開く" : "-");
      srcBtn.type = "button";
      srcBtn.disabled = !n.sourceId;
      srcBtn.addEventListener("click", () => openRecord(n.sourceModule, n.sourceId));
      src.append(srcBtn);
      tr.append(el("td", "date", V.dateLabel(n.date)), title, el("td", null, n.type || "-"), el("td", null, n.source || "-"), el("td", null, n.company || "-"), src);
      body.append(tr);
    }
    $("l-empty").hidden = rows.length > 0;
    $("l-truncated").hidden = !state.truncated;
    const filtered = rows.length !== state.needs.length;
    $("l-count").textContent = filtered ? `${rows.length}件（全${state.needs.length}件）` : `${rows.length}件`;
    // 棒・積み上げの選択中の印
    for (const node of document.querySelectorAll("[data-key]")) {
      const active = state.filter[node.dataset.key] === node.dataset.value;
      node.classList.toggle("active", active);
      node.classList.toggle("dim", !!state.filter[node.dataset.key] && !active && node.tagName === "BUTTON" && !node.classList.contains("bar-row"));
      node.setAttribute("aria-pressed", String(active));
    }
    resize();
  }

  function toggleFilter(key, value) {
    state.filter[key] = state.filter[key] === value ? "" : value;
    const select = { type: "f-type", source: "f-source", company: "f-company" }[key];
    if (select) $(select).value = state.filter[key];
    renderList();
  }

  // ---- 読み込み ----
  function renderAll() {
    const view = state.view;
    $("r-name").textContent = V.stripTest(view.name) || "（名前なし）";
    const badge = $("r-state");
    badge.textContent = view.status || "未実行";
    badge.className = `badge ${view.state}`;
    $("r-period").textContent = `${V.dateLabel(view.period.from)} 〜 ${V.dateLabel(view.period.to)}`;
    $("r-targets").textContent = view.targets;
    $("r-analyzed").textContent = V.dateTimeLabel(view.analyzedAt);
    const notice = V.noticeText(view);
    $("r-notice").textContent = notice;
    $("r-notice").className = `notice ${view.state}`;
    $("r-notice").hidden = !notice;

    const needs = state.needs;
    const tile = (id, value, unit) => {
      const node = $(id);
      node.replaceChildren(document.createTextNode(value == null ? "-" : String(value)));
      if (value != null) node.append(el("small", null, unit));
    };
    tile("k-meetings", view.counts.meetings, "件");
    tile("k-inquiries", view.counts.inquiries, "件");
    tile("k-needs", needs.length, "件");
    tile("k-companies", V.companyCount(needs), "社");

    renderThemes(view.themes);
    const types = V.countBy(needs, "type", C.values.need_types);
    renderBars("c-types", types, "type");
    renderSources(V.countBy(needs, "source", [C.values.source_meeting, C.values.source_inquiry]));
    const companies = V.countBy(needs, "company").filter((c) => c.label !== "未設定");
    renderBars("c-companies", companies.slice(0, 10), "company");
    renderCards("r-matched", view.matched, view.state === "done" ? "自社技術で応えられるニーズは見つかりませんでした。" : "まだありません。");
    renderCards("r-ideas", view.ideas, view.state === "done" ? "新商品開発の候補は見つかりませんでした。" : "まだありません。");

    fillSelect("f-type", types.map((t) => t.label), state.filter.type);
    fillSelect("f-source", [C.values.source_meeting, C.values.source_inquiry], state.filter.source);
    fillSelect("f-company", companies.map((c) => c.label).sort((a, b) => a.localeCompare(b, "ja")), state.filter.company);
    $("report").hidden = false;
    renderList();
  }

  async function loadNeeds(view) {
    const needs = [];
    let truncated = false;
    for (let offset = 0; offset < V.COQL_MAX; offset += V.COQL_PAGE) {
      const resp = await ZOHO.CRM.API.coql({ select_query: V.needsQuery(view, C, offset) });
      const rows = resp && Array.isArray(resp.data) ? resp.data : [];
      needs.push(...rows.map((r) => V.toNeed(r, C)));
      const more = !!(resp && resp.info && resp.info.more_records);
      if (!more) return { needs, truncated: false };
      truncated = offset + V.COQL_PAGE >= V.COQL_MAX;
    }
    return { needs, truncated };
  }

  async function load() {
    showMessage("読み込み中…");
    try {
      const resp = await ZOHO.CRM.API.getRecord({ Entity: C.analysis_module, RecordID: state.id });
      const record = resp && Array.isArray(resp.data) ? resp.data[0] : null;
      if (!record) {
        showMessage("ニーズ分析の記録を読めませんでした（見る権限が無いか、削除されています）", "error");
        resize();
        return;
      }
      state.view = V.toView(record, C);
      const loaded = await loadNeeds(state.view);
      state.needs = loaded.needs;
      state.truncated = loaded.truncated;
      showMessage("");
      renderAll();
    } catch (err) {
      showMessage(V.sdkErrorText(err), "error");
    }
    resize();
  }

  $("btn-reload").addEventListener("click", load);
  $("f-type").addEventListener("change", (e) => ((state.filter.type = e.target.value), renderList()));
  $("f-source").addEventListener("change", (e) => ((state.filter.source = e.target.value), renderList()));
  $("f-company").addEventListener("change", (e) => ((state.filter.company = e.target.value), renderList()));
  $("f-q").addEventListener("input", (e) => ((state.filter.q = e.target.value), renderList()));
  $("f-clear").addEventListener("click", () => {
    state.filter = { type: "", source: "", company: "", q: "" };
    $("f-type").value = $("f-source").value = $("f-company").value = $("f-q").value = "";
    renderList();
  });

  ZOHO.embeddedApp.on("PageLoad", (data) => {
    const entity = data && typeof data.Entity === "string" ? data.Entity : "";
    if (entity && entity !== C.analysis_module) {
      showMessage("このウィジェットは「ニーズ分析」の画面で使います", "error");
      return;
    }
    const raw = data && (Array.isArray(data.EntityId) ? data.EntityId[0] : data.EntityId);
    if (!V.isRecordId(raw)) {
      showMessage("ニーズ分析の画面で開いてください（記録の ID を受け取れませんでした）", "error");
      return;
    }
    state.id = String(raw);
    load();
  });
  ZOHO.embeddedApp.init();
})();
