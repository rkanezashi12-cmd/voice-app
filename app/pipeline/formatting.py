"""CRM に書き込む値の組み立て。

文字起こし全文は「商談記録」の transcript / transcript_2（各 32,000 文字）に入れる。
- 32,000 文字を超えた分は transcript_2 へ。区切りはなるべく行（発言）の切れ目に寄せる。
- 合計 64,000 文字を超える場合は末尾を切り「（以降省略：全体○○文字）」と明記する（呼び出し側でログに警告）。
- 文字数は UTF-16 の単位で数える（絵文字などを2文字と数える側に倒して、CRM の上限を確実に守る）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from typing import Any

from app.field_map import FieldMap
from app.pipeline.models import NextAction, SummaryResult

# 行の切れ目を探すのは上限からこの文字数以内まで（それより前にしか改行が無ければ上限で切る）
LINE_BREAK_SEARCH = 2000
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def u16len(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def cut_u16(text: str, limit: int) -> str:
    """UTF-16 の単位で limit 以内に収まる先頭部分。"""
    if u16len(text) <= limit:
        return text
    if len(text) == u16len(text):  # すべて基本多言語面（通常の日本語）
        return text[:limit]
    units = 0
    for i, ch in enumerate(text):
        units += 2 if ord(ch) > 0xFFFF else 1
        if units > limit:
            return text[:i]
    return text


def cut_at_line(text: str, limit: int) -> str:
    """limit 以内で、なるべく行の終わりで切った先頭部分。"""
    head = cut_u16(text, limit)
    if len(head) == len(text):
        return head
    newline = head.rfind("\n")
    if newline >= 0 and len(head) - newline <= LINE_BREAK_SEARCH:
        return head[: newline + 1]
    return head


@dataclass(frozen=True)
class SplitTranscript:
    first: str
    second: str | None
    truncated: bool
    total_chars: int


def split_transcript(text: str, limit: int) -> SplitTranscript:
    total = len(text)
    first = cut_at_line(text, limit)
    rest = text[len(first) :]
    first = first.rstrip("\n")
    if not rest:
        return SplitTranscript(first, None, False, total)
    rest = rest.lstrip("\n")
    if u16len(rest) <= limit:
        return SplitTranscript(first, rest, False, total)
    marker = f"\n（以降省略：全体{total:,}文字）"
    second = cut_at_line(rest, limit - u16len(marker)).rstrip("\n") + marker
    return SplitTranscript(first, second, True, total)


def clip(text: str | None, limit: int) -> str | None:
    if text is None:
        return None
    text = text.strip()
    if not text:
        return None
    if u16len(text) <= limit:
        return text
    return cut_u16(text, limit - 1) + "…"


def bullets(items: list[str]) -> str | None:
    return "\n".join(f"・{item}" for item in items) if items else None


def format_next_actions(actions: list[NextAction]) -> str | None:
    lines = []
    for a in actions:
        extra = [f"担当: {a.owner}" if a.owner else None, f"期限: {a.due}" if a.due else None]
        detail = "、".join(x for x in extra if x)
        lines.append(f"・{a.action}" + (f"（{detail}）" if detail else ""))
    return "\n".join(lines) if lines else None


def valid_date(value: str | None) -> str | None:
    if not value or not _DATE_RE.match(value):
        return None
    try:
        date.fromisoformat(value)
    except ValueError:
        return None
    return value


def earliest_due(actions: list[NextAction]) -> str | None:
    dues = sorted(d for d in (valid_date(a.due) for a in actions) if d)
    return dues[0] if dues else None


def normalize_summary(summary: SummaryResult, category_choices: tuple[str, ...]) -> SummaryResult:
    """選択肢に無い category と、日付として読めない期限を null にする。"""
    category = summary.category
    if category_choices and category not in category_choices:
        category = None
    actions = [
        NextAction(action=a.action, owner=a.owner, due=valid_date(a.due)) for a in summary.next_actions
    ]
    return summary.model_copy(update={"category": category, "next_actions": actions})


def result_fields(
    fm: FieldMap, summary: SummaryResult, split: SplitTranscript, status_value: str
) -> dict[str, Any]:
    """共通処理の最後の1回の更新で書く項目。"""
    f, lim = fm.meeting_record, fm.limits
    return {
        f.summary: clip(summary.summary, lim.summary),
        f.issues: clip(bullets(summary.issues), lim.issues),
        f.needs: clip(bullets(summary.needs), lim.needs),
        f.next_actions: clip(format_next_actions(summary.next_actions), lim.next_actions),
        f.due_date: earliest_due(summary.next_actions),
        f.category: summary.category,
        f.competitors: clip(bullets(summary.competitors), lim.competitors),
        f.budget: clip(summary.budget, lim.budget),
        f.decision_maker: clip(summary.decision_maker, lim.decision_maker),
        f.transcript: split.first,
        f.transcript_2: split.second,
        f.status: status_value,
        f.error_message: None,
    }


def record_name(meeting_date: date, label: str | None, fm: FieldMap) -> str:
    """バックエンドが作るレコードの名前（【TEST】は CRM サービス側で付ける）。"""
    name = f"{meeting_date.isoformat()} {label or 'オンライン商談'}"
    return clip(name, fm.limits.name) or name
