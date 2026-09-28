"""共通処理のデータ型（入口に依存しない）。"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from pydantic import BaseModel, field_validator

_LINE_RE = re.compile(r"^\s*(?P<speaker>[^:：\n]{1,40}?)\s*[:：]\s*(?P<text>.*)$")


@dataclass(frozen=True)
class Utterance:
    speaker: str
    text: str


@dataclass
class Transcript:
    utterances: list[Utterance] = field(default_factory=list)

    def to_text(self) -> str:
        return "\n".join(f"{u.speaker}: {u.text}" for u in self.utterances)

    @property
    def char_count(self) -> int:
        return sum(len(u.text) for u in self.utterances)

    @property
    def speakers(self) -> list[str]:
        seen: dict[str, None] = {}
        for u in self.utterances:
            seen.setdefault(u.speaker, None)
        return list(seen)

    @classmethod
    def from_text(cls, text: str) -> Transcript:
        """「話者: 発言」の行を読み取る。形式に合わない行は直前の発言の続きとして扱う。"""
        utterances: list[Utterance] = []
        for raw in text.splitlines():
            line = raw.strip()
            if not line or line.startswith("```"):
                continue
            m = _LINE_RE.match(line)
            # 「15:00」のような時刻を話者と取り違えない
            if m and m["speaker"][-1:].isdigit() and m["text"][:1].isdigit():
                m = None
            if m and m["text"].strip():
                utterances.append(Utterance(m["speaker"].strip(), m["text"].strip()))
            elif utterances:
                last = utterances[-1]
                utterances[-1] = Utterance(last.speaker, f"{last.text}{_joiner(last.text, line)}{line}")
            else:
                utterances.append(Utterance("不明", line))
        return cls(utterances)

    def merged(self) -> Transcript:
        """同じ話者の連続した発言を1つにまとめる。"""
        out: list[Utterance] = []
        for u in self.utterances:
            if out and out[-1].speaker == u.speaker:
                out[-1] = Utterance(u.speaker, f"{out[-1].text}{_joiner(out[-1].text, u.text)}{u.text}")
            else:
                out.append(u)
        return Transcript(out)


def _joiner(left: str, right: str) -> str:
    """日本語どうしは詰め、英数字どうしは空白を入れる。"""
    if (
        left
        and right
        and left[-1].isascii()
        and left[-1].isalnum()
        and right[0].isascii()
        and right[0].isalnum()
    ):
        return " "
    return ""


def join_words(words: list[str]) -> str:
    text = ""
    for w in words:
        w = w.strip()
        if not w:
            continue
        text = f"{text}{_joiner(text, w)}{w}" if text else w
    return text


@dataclass(frozen=True)
class MeetingContext:
    """プロンプトに渡す商談の情報（秘密情報・本文は含めない）。"""

    client_id: str
    record_id: str | None
    meeting_date: date
    account_name: str | None = None
    meeting_type: str | None = None
    capture_method: str | None = None
    participants: tuple[str, ...] = ()

    def describe(self) -> str:
        lines = [f"商談日: {self.meeting_date.isoformat()}"]
        if self.account_name:
            lines.append(f"取引先: {self.account_name}")
        if self.meeting_type:
            lines.append(f"形式: {self.meeting_type}")
        if self.participants:
            lines.append(f"参加者: {'、'.join(self.participants)}")
        return "\n".join(lines)


class NextAction(BaseModel):
    action: str
    owner: str | None = None
    due: str | None = None


class SummaryResult(BaseModel):
    summary: str | None = None
    issues: list[str] = []
    needs: list[str] = []
    next_actions: list[NextAction] = []
    category: str | None = None
    competitors: list[str] = []
    budget: str | None = None
    decision_maker: str | None = None

    @field_validator("issues", "needs", "competitors", "next_actions", mode="before")
    @classmethod
    def none_to_empty(cls, value: Any) -> Any:
        return [] if value is None else value

    @field_validator("issues", "needs", "competitors")
    @classmethod
    def drop_blank(cls, value: list[str]) -> list[str]:
        return [v.strip() for v in value if v and v.strip()]
