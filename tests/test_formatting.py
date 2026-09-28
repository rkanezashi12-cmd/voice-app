from __future__ import annotations

from datetime import date

from app.field_map import FieldMap
from app.pipeline.formatting import (
    bullets,
    clip,
    earliest_due,
    normalize_summary,
    record_name,
    split_transcript,
    u16len,
)
from app.pipeline.models import NextAction, SummaryResult, Transcript

LINE = "話者A: " + "あ" * 95  # 100 文字


def text_of(lines: int) -> str:
    return "\n".join([LINE] * lines)


def test_short_transcript_fits_first_field() -> None:
    s = split_transcript(text_of(300), 32000)
    assert s.second is None and not s.truncated
    assert s.first == text_of(300)


def test_overflow_goes_to_second_field_at_line_break() -> None:
    s = split_transcript(text_of(500), 32000)
    assert not s.truncated
    assert len(s.first) <= 32000 and len(s.second) <= 32000
    assert s.first.endswith("あ") and s.second.startswith("話者A: ")
    assert f"{s.first}\n{s.second}" == text_of(500), "区切っても何も失わない"


def test_over_64000_is_truncated_with_marker() -> None:
    text = text_of(700)
    s = split_transcript(text, 32000)
    assert s.truncated
    assert s.total_chars == len(text)
    assert len(s.first) <= 32000 and len(s.second) <= 32000
    assert s.second.endswith(f"（以降省略：全体{len(text):,}文字）")


def test_limit_is_counted_in_utf16_units() -> None:
    s = split_transcript("😀" * 20000, 32000)  # 40,000 単位
    assert u16len(s.first) <= 32000
    assert u16len(s.first) + u16len(s.second) == 40000


def test_clip_and_bullets() -> None:
    assert clip("abcdef", 4) == "abc…"
    assert clip("  ", 10) is None
    assert clip(None, 10) is None
    assert bullets(["a", "b"]) == "・a\n・b"
    assert bullets([]) is None


def test_earliest_due_ignores_invalid_dates() -> None:
    actions = [
        NextAction(action="a", due="2026-13-01"),
        NextAction(action="b", due="2026-10-05"),
        NextAction(action="c"),
    ]
    assert earliest_due(actions) == "2026-10-05"


def test_normalize_summary_limits_category_and_dates() -> None:
    summary = SummaryResult(category="その他", next_actions=[NextAction(action="a", due="来週")])
    out = normalize_summary(summary, ("新規", "既存深耕"))
    assert out.category is None
    assert out.next_actions[0].due is None
    assert normalize_summary(SummaryResult(category="新規"), ("新規",)).category == "新規"
    assert normalize_summary(SummaryResult(category="自由記述"), ()).category == "自由記述"


def test_summary_accepts_nulls_for_lists() -> None:
    s = SummaryResult.model_validate(
        {"summary": None, "issues": None, "needs": [" ", "a"], "next_actions": None}
    )
    assert s.issues == [] and s.needs == ["a"] and s.next_actions == []


def test_record_name() -> None:
    assert record_name(date(2026, 9, 28), "カスタマー株式会社", FieldMap()) == "2026-09-28 カスタマー株式会社"
    assert record_name(date(2026, 9, 28), None, FieldMap()) == "2026-09-28 オンライン商談"


def test_transcript_parsing_round_trip() -> None:
    t = Transcript.from_text("話者A: こんにちは\n\n話者B：15:00 からです\n続きです\n```")
    assert t.to_text() == "話者A: こんにちは\n話者B: 15:00 からです続きです"
    assert t.speakers == ["話者A", "話者B"]
