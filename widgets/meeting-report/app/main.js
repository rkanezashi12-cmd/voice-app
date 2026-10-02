// 商談日報ウィジェット：CRM の商談記録の画面（関連リスト）で、日報と文字起こし全文を見やすく出す。読むだけで、CRM には書かない。
// SDK（ZohoEmbededAppSDK.min.js）と項目の API 名（field-map.js）は scripts/build_widget.py が ZIP に入れる。
(function () {
  "use strict";
  const R = window.MeetingReport;
  const C = window.MEETING_REPORT_CONFIG;
  const $ = (id) => document.getElementById(id);
  const state = { entity: C.module, id: "", blocks: [], hits: [], current: -1 };

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

  // 関連リストの枠の高さを中身に合わせる（関連リストは高さだけ変えられる）。
  // 枠の高さ（document の scrollHeight）ではなく中身の高さを測る（測るたびに枠が伸び続けないように）。
  // SDK が無い・断られても、高さが合わないだけで表示は続ける
  function resize() {
    const height = Math.min(Math.max(Math.ceil($("root").getBoundingClientRect().height) + 8, 320), 1800);
    try {
      Promise.resolve(ZOHO.CRM.UI.Resize({ height: String(height) })).catch(() => {});
    } catch {
      // SDK に Resize が無い
    }
  }

  function setText(id, value) {
    $(id).textContent = value || "-";
  }

  function renderHead(view) {
    $("r-name").textContent = view.name || "（名前なし）";
    const badge = $("r-state");
    badge.textContent = view.status || "状態なし";
    badge.className = `badge ${view.state}`;
    setText("r-start", R.dateTimeLabel(view.startAt));
    setText("r-type", [view.meetingType, view.captureMethod].filter(Boolean).join("・"));
    setText("r-contacts", view.contacts);
    setText("r-owner", view.owner);
    setText("r-category", view.category);
    const account = $("r-account");
    account.textContent = view.account || "-";
    account.disabled = !R.isRecordId(view.accountId);
    account.dataset.id = view.accountId;
  }

  function renderSections(view) {
    const box = $("r-sections");
    box.replaceChildren();
    for (const s of R.reportSections(view)) {
      const card = el("section", `card card-${s.key}`);
      card.append(el("h2", null, s.label), el("p", "body", s.text));
      box.append(card);
    }
    const notice = R.noticeText(view);
    $("r-notice").textContent = notice;
    $("r-notice").className = `notice ${view.state}`;
    $("r-notice").hidden = !notice;
  }

  function renderTranscript() {
    const list = $("r-transcript");
    list.replaceChildren();
    const colors = R.speakerColors(state.blocks);
    const query = $("q").value;
    state.hits = [];
    for (const block of state.blocks) {
      const li = el("li", `utterance spk-${block.speaker ? colors.get(block.speaker) : "none"}`);
      if (block.speaker) li.append(el("span", "speaker", block.speaker));
      const body = el("p", "said");
      for (const part of R.highlight(block.text, query)) {
        if (part.hit) {
          const mark = el("mark", null, part.text);
          state.hits.push(mark);
          body.append(mark);
        } else {
          body.append(document.createTextNode(part.text));
        }
      }
      li.append(body);
      list.append(li);
    }
    $("r-transcript-empty").hidden = state.blocks.length > 0;
    $("q-count").textContent = query.trim() ? `${state.hits.length}件` : "";
    state.current = -1;
    $("q-prev").disabled = $("q-next").disabled = state.hits.length === 0;
  }

  function moveHit(step) {
    if (!state.hits.length) return;
    if (state.current >= 0) state.hits[state.current].classList.remove("current");
    state.current = (state.current + step + state.hits.length) % state.hits.length;
    const mark = state.hits[state.current];
    mark.classList.add("current");
    // 文字起こしの欄の中だけを動かす（scrollIntoView だと CRM の画面ごと動く）
    const list = $("r-transcript");
    const box = list.getBoundingClientRect();
    const hit = mark.getBoundingClientRect();
    list.scrollTop += hit.top - box.top - (list.clientHeight - hit.height) / 2;
    $("q-count").textContent = `${state.current + 1} / ${state.hits.length}件`;
  }

  function render(view) {
    renderHead(view);
    renderSections(view);
    state.blocks = R.parseTranscript(view.transcript);
    renderTranscript();
    $("report").hidden = false;
    resize();
  }

  async function load() {
    showMessage("読み込み中…");
    try {
      const resp = await ZOHO.CRM.API.getRecord({ Entity: state.entity, RecordID: state.id });
      const record = resp && Array.isArray(resp.data) ? resp.data[0] : null;
      if (!record) {
        showMessage("商談記録を読めませんでした（見る権限が無いか、削除されています）", "error");
        return;
      }
      showMessage("");
      render(R.toView(record, C));
    } catch (err) {
      showMessage(R.sdkErrorText(err), "error");
    }
    resize();
  }

  $("q").addEventListener("input", renderTranscript);
  $("q").addEventListener("keydown", (e) => {
    if (e.key === "Enter") moveHit(e.shiftKey ? -1 : 1);
  });
  $("q-next").addEventListener("click", () => moveHit(1));
  $("q-prev").addEventListener("click", () => moveHit(-1));
  $("btn-reload").addEventListener("click", load);
  $("r-account").addEventListener("click", () => {
    const id = $("r-account").dataset.id;
    if (!R.isRecordId(id)) return;
    Promise.resolve(ZOHO.CRM.UI.Record.open({ Entity: C.accounts_module, RecordID: id })).catch((err) =>
      showMessage(R.sdkErrorText(err), "error"),
    );
  });

  ZOHO.embeddedApp.on("PageLoad", (data) => {
    const entity = data && typeof data.Entity === "string" ? data.Entity : "";
    if (entity && entity !== C.module) {
      showMessage("このウィジェットは「商談記録」の画面で使います", "error");
      return;
    }
    const raw = data && (Array.isArray(data.EntityId) ? data.EntityId[0] : data.EntityId);
    if (!R.isRecordId(raw)) {
      showMessage("商談記録の画面で開いてください（記録の ID を受け取れませんでした）", "error");
      return;
    }
    state.id = String(raw);
    load();
  });
  ZOHO.embeddedApp.init();
})();
