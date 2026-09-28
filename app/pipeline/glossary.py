"""用語辞書（CRM のカスタムモジュール「用語辞書」）。読み取りだけ行う。

補正・文字起こしのプロンプトに「正しい表記」と「誤認識の例」を渡す。
CRM の API クレジット節約のため、クライアントごとに GLOSSARY_CACHE_SECONDS の間キャッシュする。
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from typing import Any

from app.field_map import FieldMap
from app.logs import log_event

logger = logging.getLogger(__name__)

_SPLIT_RE = re.compile(r"[,、，/／;\n]+")
MAX_TERMS = 1000


@dataclass(frozen=True)
class Term:
    term: str
    misrecognitions: tuple[str, ...] = ()
    term_type: str | None = None


def parse_terms(records: list[dict[str, Any]], fm: FieldMap) -> list[Term]:
    g = fm.glossary
    terms: list[Term] = []
    for r in records:
        term = r.get(g.term)
        if not isinstance(term, str) or not term.strip():
            continue
        raw = r.get(g.misrecognitions)
        if isinstance(raw, list):
            raw = "、".join(str(x) for x in raw)
        mis = (
            tuple(x.strip() for x in _SPLIT_RE.split(raw or "") if x.strip()) if isinstance(raw, str) else ()
        )
        term_type = r.get(g.term_type)
        terms.append(Term(term.strip(), mis, term_type if isinstance(term_type, str) else None))
    return terms[:MAX_TERMS]


def format_glossary(terms: list[Term]) -> str:
    if not terms:
        return ""
    lines = ["# 用語辞書（正しい表記）"]
    for t in terms:
        line = f"- {t.term}"
        if t.term_type:
            line += f"（{t.term_type}）"
        if t.misrecognitions:
            line += f" ／ 誤認識の例: {'、'.join(t.misrecognitions)}"
        lines.append(line)
    return "\n".join(lines)


class GlossaryCache:
    def __init__(self, ttl_seconds: int, clock: Any = time.monotonic) -> None:
        self._ttl = ttl_seconds
        self._clock = clock
        self._items: dict[str, tuple[float, str]] = {}

    async def get(self, client_id: str, crm: Any, fm: FieldMap) -> str:
        cached = self._items.get(client_id)
        if cached and self._clock() - cached[0] < self._ttl:
            return cached[1]
        g = fm.glossary
        records = await crm.list_records(g.module, [g.term, g.misrecognitions, g.term_type])
        terms = parse_terms(records, fm)
        text = format_glossary(terms)
        self._items[client_id] = (self._clock(), text)
        log_event(logger, "glossary.loaded", client_id=client_id, terms=len(terms))
        return text
