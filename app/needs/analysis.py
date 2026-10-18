"""ニーズ分析：商談記録と WEB の問合せからニーズを抜き出して「ニーズ」にため、自社技術と照らし合わせる。

流れ（docs/needs-analysis.md）：
1. 「ニーズ分析」の記録を読み、状態を「実行中」にする
2. 期間内の元を集める（商談記録の要約・課題・ニーズ、問合せ内容の本文。読むだけ）
3. まだ抜き出していない元だけ Gemini でニーズを抜き出し、「ニーズ」にまとめて作る（抽出キーで二重に作らない）
4. 期間内のニーズと自社技術を Gemini に渡し、テーマ・応えられるニーズ・新商品開発の候補を出す
5. 結果を「ニーズ分析」に1回で書く。失敗したら状態「失敗」とエラー内容

ログには件数・ID だけを出し、ニーズや問合せの本文は出さない（CLAUDE.md ルール4）。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from pydantic import BaseModel

from app.errors import AppError, ExternalServiceError, PermanentError
from app.field_map import FieldMap
from app.logs import log_event
from app.pipeline import formatting as fmt
from app.pipeline.records import lookup_name, picklist_value
from app.services.crm import TEST_PREFIX
from app.services.gemini import TextPart

logger = logging.getLogger(__name__)

ANALYZE_PATH = "/internal/needs-analyze"
# 1回の抜き出しで Gemini に渡す元の数と、同時に投げる数
EXTRACT_BATCH = 20
EXTRACT_CONCURRENCY = 3
# 分析に渡すニーズの上限（新しい順）。超えた分は件数の説明に書く
MAX_NEEDS_FOR_ANALYSIS = 800
# 期間の既定（開始が空なら終了の1年前から）
DEFAULT_PERIOD_DAYS = 365
# 本文がこれより短い問合せはニーズの元にしない（「テストです」など）
MIN_INQUIRY_CHARS = 10
TEXT_LIMIT = 32000
SMALL_LIMIT = 2000
LINE_LIMIT = 255
NAME_LIMIT = 120


class NeedsAnalysisTask(BaseModel):
    client_id: str
    record_id: str


@dataclass(frozen=True)
class Source:
    """ニーズの元（商談記録1件か、問合せ1件）。"""

    kind: str  # "meeting" / "inquiry"
    record_id: str
    company: str
    day: date
    text: str

    @property
    def key(self) -> str:
        return f"{self.kind}:{self.record_id}"


@dataclass
class Need:
    id: str
    title: str
    detail: str
    need_type: str
    source_type: str
    company: str
    day: date | None


@dataclass
class Collected:
    meetings: int = 0
    inquiries: int = 0
    sources: list[Source] = field(default_factory=list)
    truncated: list[str] = field(default_factory=list)


def _clip(value: str | None, limit: int) -> str:
    return fmt.clip(value or "", limit) or ""


def _strip_test(name: str) -> str:
    return name[len(TEST_PREFIX) :] if name.startswith(TEST_PREFIX) else name


def _parse_day(value: Any) -> date | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def _created_day(row: dict[str, Any], tz: ZoneInfo) -> date | None:
    value = row.get("Created_Time")
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(tz).date()
    except ValueError:
        return None


def _text_of(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


class NeedsAnalysisRun:
    def __init__(self, rt: Any, client_id: str, record_id: str, *, final_attempt: bool) -> None:
        self.rt = rt
        self.cs = rt.client_services(client_id)
        self.client_id = client_id
        self.record_id = record_id
        self.final_attempt = final_attempt
        self.fm: FieldMap = self.cs.field_map
        self.tz = ZoneInfo(self.cs.config.timezone)

    # ---- 入口 ----

    async def run(self) -> str:
        """ "done" / "failed" を返す。もう一度試す価値がある失敗（最後の試行でない）は例外のまま投げる。"""
        if not self.cs.config.features.needs_analysis:
            raise PermanentError(f"クライアント {self.client_id} ではニーズ分析を使えません")
        crm = await self.cs.crm()
        a = self.fm.needs.analysis
        v = self.fm.needs.values
        try:
            record = await crm.get_record(a.module, self.record_id)
            if record is None:
                raise PermanentError("ニーズ分析の記録が見つかりません")
            period_from, period_to = self._period(record)
            targets = picklist_value(record.get(a.targets)) or v.target_both
            log_event(
                logger,
                "needs.started",
                client_id=self.client_id,
                record_id=self.record_id,
                period_from=period_from.isoformat(),
                period_to=period_to.isoformat(),
            )
            await crm.update_record(
                a.module, self.record_id, {a.status: v.status_running, a.error_message: None}
            )
            fields = await self._analyze(crm, period_from, period_to, targets)
        except AppError as exc:
            if exc.retryable and not self.final_attempt:
                raise
            await self._record_failure(crm, exc.message)
            return "failed"
        await crm.update_record(a.module, self.record_id, fields)
        log_event(logger, "needs.done", client_id=self.client_id, record_id=self.record_id)
        return "done"

    async def _record_failure(self, crm: Any, message: str) -> None:
        a = self.fm.needs.analysis
        log_event(
            logger,
            "needs.failed",
            logging.ERROR,
            client_id=self.client_id,
            record_id=self.record_id,
            error=message,
        )
        try:
            await crm.update_record(
                a.module,
                self.record_id,
                {
                    a.status: self.fm.needs.values.status_failed,
                    a.error_message: _clip(f"ニーズ分析に失敗しました: {message}", SMALL_LIMIT),
                    a.analyzed_at: self._now_text(),
                },
            )
        except AppError as exc:
            log_event(logger, "needs.failure_not_recorded", logging.ERROR, error_code=exc.code)

    # ---- 期間 ----

    def _period(self, record: dict[str, Any]) -> tuple[date, date]:
        a = self.fm.needs.analysis
        today = datetime.now(self.tz).date()
        period_to = _parse_day(record.get(a.period_to)) or today
        period_from = _parse_day(record.get(a.period_from)) or period_to - timedelta(days=DEFAULT_PERIOD_DAYS)
        if period_from > period_to:
            raise PermanentError("期間（開始）が期間（終了）より後になっています")
        return period_from, period_to

    def _now_text(self) -> str:
        return datetime.now(self.tz).replace(microsecond=0).isoformat()

    # ---- 本体 ----

    async def _analyze(self, crm: Any, period_from: date, period_to: date, targets: str) -> dict[str, Any]:
        v = self.fm.needs.values
        use_meetings = targets != v.target_inquiries
        use_inquiries = targets != v.target_meetings
        since = datetime.combine(period_from, time.min, tzinfo=self.tz).astimezone(UTC)
        collected = await self._collect(crm, since, period_from, period_to, use_meetings, use_inquiries)
        existing, truncated_needs = await self._existing_needs(crm)
        done_sources = {key.rsplit(":", 1)[0] for key in existing}
        todo = [s for s in collected.sources if s.key not in done_sources]
        new_needs = await self._extract_and_save(crm, todo) if todo else []
        in_period = [
            n
            for n in [*existing.values(), *new_needs]
            if n.day is not None
            and period_from <= n.day <= period_to
            and (
                (use_meetings and n.source_type == v.source_meeting)
                or (use_inquiries and n.source_type == v.source_inquiry)
            )
        ]
        if truncated_needs:
            collected.truncated.append("ニーズが多いため、新しい 2,000 件までで判定しました")
        log_event(
            logger,
            "needs.collected",
            client_id=self.client_id,
            record_id=self.record_id,
            meetings=collected.meetings,
            inquiries=collected.inquiries,
            extracted_sources=len(todo),
            new_needs=len(new_needs),
            needs_in_period=len(in_period),
        )
        a = self.fm.needs.analysis
        counts = (
            f"商談 {collected.meetings}件・WEB問合せ {collected.inquiries}件 → ニーズ {len(in_period)}件"
            f"（今回新しく抜き出した {len(new_needs)}件）"
        )
        if collected.truncated:
            counts += "。" + "。".join(collected.truncated)
        fields: dict[str, Any] = {
            a.status: v.status_done,
            a.counts: _clip(counts, LINE_LIMIT),
            a.error_message: None,
            a.analyzed_at: self._now_text(),
        }
        if not in_period:
            fields[a.top_needs] = "期間内にニーズが見つかりませんでした。"
            fields[a.matched_needs] = None
            fields[a.product_ideas] = None
            return fields
        technologies = await self._technologies(crm)
        report = await self._summarize(in_period, technologies)
        fields.update(report)
        return fields

    # ---- 2. 元を集める ----

    async def _collect(
        self,
        crm: Any,
        since: datetime,
        period_from: date,
        period_to: date,
        use_meetings: bool,
        use_inquiries: bool,
    ) -> Collected:
        out = Collected()
        if use_meetings:
            f = self.fm.meeting_record
            rows, truncated = await crm.list_records_since(
                f.module, [f.name, f.summary, f.issues, f.needs, f.account, f.status], since
            )
            if truncated:
                out.truncated.append("商談記録が多いため、新しい 2,000 件までを読みました")
            finished = {self.fm.status.done, self.fm.status.no_account}
            for row in rows:
                day = _created_day(row, self.tz)
                if day is None or not period_from <= day <= period_to:
                    continue
                if picklist_value(row.get(f.status)) not in finished:
                    continue
                parts = [
                    f"要約: {_text_of(row.get(f.summary))}" if _text_of(row.get(f.summary)) else "",
                    f"課題: {_text_of(row.get(f.issues))}" if _text_of(row.get(f.issues)) else "",
                    f"ニーズ: {_text_of(row.get(f.needs))}" if _text_of(row.get(f.needs)) else "",
                ]
                text = "\n".join(p for p in parts if p)
                if not text:
                    continue
                out.meetings += 1
                out.sources.append(
                    Source("meeting", str(row["id"]), lookup_name(row.get(f.account)) or "", day, text)
                )
        if use_inquiries:
            q = self.fm.needs.inquiry
            rows, truncated = await crm.list_records_since(q.module, [q.request, q.item, q.company], since)
            if truncated:
                out.truncated.append("問合せが多いため、新しい 2,000 件までを読みました")
            for row in rows:
                day = _created_day(row, self.tz)
                body = _text_of(row.get(q.request))
                if day is None or not period_from <= day <= period_to or len(body) < MIN_INQUIRY_CHARS:
                    continue
                item = picklist_value(row.get(q.item))
                text = f"問合せ項目: {item}\n本文: {body}" if item else f"本文: {body}"
                out.inquiries += 1
                out.sources.append(Source("inquiry", str(row["id"]), _text_of(row.get(q.company)), day, text))
        return out

    async def _existing_needs(self, crm: Any) -> tuple[dict[str, Need], bool]:
        """これまでに作ったニーズ（抽出キー → ニーズ）。抽出キーで、抜き出し済みの元を判定する。"""
        n = self.fm.needs.need
        rows, truncated = await crm.list_records_since(
            n.module,
            [n.name, n.detail, n.need_type, n.source_type, n.company, n.need_date, n.extract_key],
            datetime.min.replace(tzinfo=UTC),
        )
        found: dict[str, Need] = {}
        for row in rows:
            key = _text_of(row.get(n.extract_key))
            if not key:
                continue
            found[key] = Need(
                id=str(row.get("id")),
                title=_strip_test(_text_of(row.get(n.name))),
                detail=_text_of(row.get(n.detail)),
                need_type=picklist_value(row.get(n.need_type)) or "",
                source_type=picklist_value(row.get(n.source_type)) or "",
                company=_text_of(row.get(n.company)),
                day=_parse_day(row.get(n.need_date)),
            )
        return found, truncated

    # ---- 3. 抜き出して、ためる ----

    async def _extract_and_save(self, crm: Any, sources: list[Source]) -> list[Need]:
        model = self.rt.settings.need("gemini_model_text")
        batches = [sources[i : i + EXTRACT_BATCH] for i in range(0, len(sources), EXTRACT_BATCH)]
        sem = asyncio.Semaphore(EXTRACT_CONCURRENCY)

        async def extract(batch: list[Source]) -> list[tuple[Source, list[dict[str, str]]]]:
            async with sem:
                return await self._extract_batch(model, batch)

        results = await asyncio.gather(*(extract(b) for b in batches))
        links = await self._record_links(crm)
        n = self.fm.needs.need
        v = self.fm.needs.values
        rows: list[dict[str, Any]] = []
        needs: list[Need] = []
        for source, items in (pair for batch in results for pair in batch):
            source_type = v.source_meeting if source.kind == "meeting" else v.source_inquiry
            for index, item in enumerate(items, start=1):
                need_type = item["type"] if item["type"] in v.need_types else v.need_type_other
                title = _clip(item["title"], NAME_LIMIT - len(TEST_PREFIX))
                rows.append(
                    {
                        n.name: title,
                        n.detail: _clip(item["detail"], TEXT_LIMIT),
                        n.need_type: need_type,
                        n.source_type: source_type,
                        n.source_url: links(source),
                        n.company: _clip(source.company, LINE_LIMIT) or None,
                        n.evidence: _clip(item["evidence"], SMALL_LIMIT),
                        n.need_date: source.day.isoformat(),
                        n.extract_key: f"{source.key}:{index}",
                        n.analysis: {"id": self.record_id},
                    }
                )
                needs.append(
                    Need(
                        "", item["title"], item["detail"], need_type, source_type, source.company, source.day
                    )
                )
        if not rows:
            return []
        results_ = await crm.create_records(n.module, rows)
        kept: list[Need] = []
        failed = 0
        for need, (record_id, result) in zip(needs, results_, strict=True):
            if result in ("created", "dry-run"):
                need.id = record_id or need.title
                kept.append(need)
            elif result != "duplicate":
                failed += 1
        if failed:
            log_event(logger, "needs.create_failed", logging.WARNING, client_id=self.client_id, rows=failed)
        return kept

    async def _extract_batch(
        self, model: str, batch: list[Source]
    ) -> list[tuple[Source, list[dict[str, str]]]]:
        prompts = self.rt.prompts
        by_key = {s.key: s for s in batch}
        text = "\n\n".join(
            f"# 元: {s.key}\n種類: {'商談' if s.kind == 'meeting' else 'WEB問合せ'} / 会社: {s.company or '不明'} / "
            f"日付: {s.day.isoformat()}\n{s.text}"
            for s in batch
        )
        for attempt in range(2):
            result = await self.rt.llm.generate(
                task="needs_extract",
                model=model,
                system_instruction=prompts.needs_extract,
                parts=[TextPart(text)],
                json_schema=prompts.needs_extract_schema,
            )
            try:
                data = json.loads(result.text)
                out: list[tuple[Source, list[dict[str, str]]]] = []
                for entry in data.get("results", []):
                    source = by_key.get(str(entry.get("source", "")))
                    if source is None:
                        continue
                    items = [
                        {k: str(item.get(k) or "").strip() for k in ("title", "detail", "type", "evidence")}
                        for item in entry.get("needs", [])
                        if isinstance(item, dict)
                    ]
                    out.append((source, [i for i in items if i["title"] and i["detail"]]))
                return out
            except (ValueError, AttributeError, TypeError):
                log_event(logger, "needs.extract_invalid_json", logging.WARNING, attempt=attempt + 1)
        raise ExternalServiceError("gemini", "ニーズの抜き出しの形式が不正です", retryable=True)

    async def _record_links(self, crm: Any) -> Any:
        """元の記録を開く CRM の URL を作る関数（組織のドメインとモジュールの内部名から）。"""
        org = await crm.get_org()
        domain = str(org.get("domain_name") or "")
        modules = {m.get("api_name"): m.get("module_name") for m in await crm.list_modules()}
        zoho = self.cs.config.need_zoho()
        web = re.sub(r"^https://www\.zohoapis\.", "https://crm.zoho.", zoho.api_base)
        names = {
            "meeting": modules.get(self.fm.meeting_record.module),
            "inquiry": modules.get(self.fm.needs.inquiry.module),
        }

        def link(source: Source) -> str | None:
            name = names.get(source.kind)
            if not domain or not name:
                return None
            return f"{web}/crm/{domain}/tab/{name}/{source.record_id}"

        return link

    # ---- 4. まとめる ----

    async def _technologies(self, crm: Any) -> list[dict[str, str]]:
        t = self.fm.needs.own_technology
        fields = [t.name, t.summary, t.strengths, t.capabilities, t.applications, t.keywords, t.status]
        rows = await crm.list_records(t.module, fields)
        keys = ("name", "summary", "strengths", "capabilities", "applications", "keywords", "status")
        return [
            {
                key: _text_of(picklist_value(row.get(api)) or row.get(api))
                for key, api in zip(keys, fields, strict=True)
            }
            for row in rows
            if _text_of(row.get(t.name))
        ]

    async def _summarize(self, needs: list[Need], technologies: list[dict[str, str]]) -> dict[str, Any]:
        model = self.rt.settings.need("gemini_model_text")
        prompts = self.rt.prompts
        needs = sorted(needs, key=lambda n: n.day or date.min, reverse=True)[:MAX_NEEDS_FOR_ANALYSIS]
        by_id = {n.id: n for n in needs}
        need_lines = "\n".join(
            f"ID: {n.id} | {n.source_type} | {n.need_type} | {n.company or '会社不明'} | "
            f"{n.day.isoformat() if n.day else ''} | {n.title}: {n.detail}"
            for n in needs
        )
        tech_text = (
            "\n\n".join(
                f"## {t['name']}（{t['status'] or '状態不明'}）\n概要: {t['summary']}\n強み・特長: {t['strengths']}\n"
                f"対応できる範囲: {t['capabilities']}\n用途・実績: {t['applications']}\nキーワード: {t['keywords']}"
                for t in technologies
            )
            or "（自社技術はまだ登録されていません）"
        )
        parts = [
            TextPart(f"# ニーズの一覧（{len(needs)}件）\n{need_lines}"),
            TextPart(f"# 自社技術\n{tech_text}"),
        ]
        for attempt in range(2):
            result = await self.rt.llm.generate(
                task="needs_analyze",
                model=model,
                system_instruction=prompts.needs_analyze,
                parts=parts,
                json_schema=prompts.needs_analyze_schema,
            )
            if result.truncated:
                raise ExternalServiceError(
                    "gemini", "ニーズの分析が出力の上限で途中で切れました", retryable=False
                )
            try:
                data = json.loads(result.text)
                return self._format(data, by_id)
            except (ValueError, AttributeError, TypeError, KeyError):
                log_event(logger, "needs.analyze_invalid_json", logging.WARNING, attempt=attempt + 1)
        raise ExternalServiceError("gemini", "ニーズの分析の形式が不正です", retryable=True)

    def _format(self, data: dict[str, Any], by_id: dict[str, Need]) -> dict[str, Any]:
        """Gemini の結果を、件数・社数を数えてから読みやすい文にする（件数は Gemini に数えさせない）。"""
        a = self.fm.needs.analysis
        themes: dict[str, list[Need]] = {}
        lines: list[str] = []
        for i, theme in enumerate(data["themes"], start=1):
            members = [by_id[x] for x in dict.fromkeys(map(str, theme.get("need_ids") or [])) if x in by_id]
            themes[str(theme["theme"])] = members
            companies = sorted({m.company for m in members if m.company})
            examples = "、".join(f"{m.company or '会社不明'}「{m.title}」" for m in members[:3])
            lines.append(
                f"{i}. {theme['theme']}（{len(members)}件・{len(companies)}社）\n{theme['description']}"
                + (f"\n例：{examples}" if examples else "")
            )

        def voice(theme: str) -> str:
            members = themes.get(theme, [])
            companies = sorted({m.company for m in members if m.company})
            shown = "、".join(companies[:5]) + ("ほか" if len(companies) > 5 else "")
            return f"{len(members)}件・{len(companies)}社" + (f"（{shown}）" if shown else "")

        matched = [
            f"■ {m['theme']} × {m['technology']}\n理由：{m['reason']}\n提案：{m['proposal']}\n求める声：{voice(str(m['theme']))}"
            for m in data["matched"]
        ]
        ideas = [
            f"■ {p['theme']}\n方向性：{p['idea']}\n理由：{p['reason']}\n"
            f"土台にできる自社技術：{p.get('base_technology') or 'なし'}\n求める声：{voice(str(p['theme']))}"
            for p in data["product_ideas"]
        ]
        return {
            a.top_needs: _clip("\n\n".join(lines), TEXT_LIMIT) or "まとめられるニーズがありませんでした。",
            a.matched_needs: _clip("\n\n".join(matched), TEXT_LIMIT)
            or "自社技術で応えられるニーズは見つかりませんでした。",
            a.product_ideas: _clip("\n\n".join(ideas), TEXT_LIMIT)
            or "新商品開発の候補は見つかりませんでした。",
        }


async def run_needs_analysis(rt: Any, task: NeedsAnalysisTask, *, final_attempt: bool) -> str:
    run = NeedsAnalysisRun(rt, task.client_id, task.record_id, final_attempt=final_attempt)
    return await run.run()
