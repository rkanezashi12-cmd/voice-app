"""Gemini による文字起こし（対面録音）・補正・要約。

プロンプトは prompts/ のファイルから読み、コードに埋め込まない。
商談の情報と用語辞書はシステム指示ではなく入力の一部として渡し、文字起こし本文とは別の部品にする。
"""

from __future__ import annotations

import asyncio
import copy
import json
import logging
from functools import cached_property
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from app.config import Settings
from app.errors import ExternalServiceError, PermanentError
from app.logs import log_event
from app.pipeline.formatting import normalize_summary
from app.pipeline.models import MeetingContext, SummaryResult, Transcript, Utterance
from app.services.audio import AudioSegment
from app.services.gemini import AudioPart, LlmClient, TextPart

logger = logging.getLogger(__name__)

PROMPTS_DIR = Path(__file__).resolve().parent.parent.parent / "prompts"
# 前の区間の末尾として次の区間に渡す行数（話者ラベルをそろえるため）
PREVIOUS_TAIL_LINES = 12
# 補正結果を採用する条件（元の文字数に対する比率）。外れたら補正せず元の文を使う
CORRECTION_MIN_RATIO = 0.7
CORRECTION_MAX_RATIO = 1.3


class PromptStore:
    def __init__(self, directory: Path = PROMPTS_DIR) -> None:
        self.directory = directory

    def _read(self, name: str) -> str:
        return (self.directory / name).read_text(encoding="utf-8").strip()

    @cached_property
    def transcribe(self) -> str:
        return self._read("transcribe.md")

    @cached_property
    def correct(self) -> str:
        return self._read("correct.md")

    @cached_property
    def summarize(self) -> str:
        return self._read("summarize.md")

    @cached_property
    def _schema(self) -> dict[str, Any]:
        return json.loads(self._read("schema.json"))

    def schema(self, category_choices: tuple[str, ...] = ()) -> dict[str, Any]:
        schema = copy.deepcopy(self._schema)
        if category_choices:
            schema["properties"]["category"] = {"type": ["string", "null"], "enum": [*category_choices, None]}
        return schema


def chunk_utterances(utterances: list[Utterance], max_chars: int) -> list[list[Utterance]]:
    """発言の切れ目で、おおむね max_chars ごとに分ける。"""
    chunks: list[list[Utterance]] = []
    current: list[Utterance] = []
    size = 0
    for u in utterances:
        length = len(u.speaker) + len(u.text) + 3
        if current and size + length > max_chars:
            chunks.append(current)
            current, size = [], 0
        current.append(u)
        size += length
    if current:
        chunks.append(current)
    return chunks


class MeetingAI:
    def __init__(self, llm: LlmClient, prompts: PromptStore, settings: Settings) -> None:
        self.llm = llm
        self.prompts = prompts
        self.settings = settings

    # ---- 対面録音の文字起こし ----

    async def transcribe(
        self, segments: list[AudioSegment], ctx: MeetingContext, glossary: str
    ) -> Transcript:
        model = self.settings.need("gemini_model_transcribe")
        results: list[Transcript]
        if self.settings.transcribe_concurrency <= 1:
            results = []
            tail: list[Utterance] = []
            for seg in segments:
                t = await self._transcribe_one(model, seg, ctx, glossary, len(segments), tail)
                results.append(t)
                tail = (tail + t.utterances)[-PREVIOUS_TAIL_LINES:]
        else:
            sem = asyncio.Semaphore(self.settings.transcribe_concurrency)

            async def run(seg: AudioSegment) -> Transcript:
                async with sem:
                    return await self._transcribe_one(model, seg, ctx, glossary, len(segments), [])

            results = list(await asyncio.gather(*(run(s) for s in segments)))
        return Transcript([u for t in results for u in t.utterances])

    async def _transcribe_one(
        self,
        model: str,
        seg: AudioSegment,
        ctx: MeetingContext,
        glossary: str,
        total: int,
        tail: list[Utterance],
    ) -> Transcript:
        info = [ctx.describe(), f"区間: {seg.index + 1} / {total}（{int(seg.start)}秒〜{int(seg.end)}秒）"]
        if glossary:
            info.append(glossary)
        if tail:
            info.append("# 前の区間の末尾（参考。出力しない）\n" + Transcript(tail).to_text())
        result = await self.llm.generate(
            task="transcribe",
            model=model,
            system_instruction=self.prompts.transcribe,
            parts=[TextPart("\n\n".join(info)), AudioPart(seg.data, seg.mime_type)],
        )
        if result.truncated:
            raise PermanentError(
                "文字起こしが出力の上限で途中で切れました（AUDIO_SEGMENT_SECONDS を短くしてください）"
            )
        return Transcript.from_text(result.text)

    # ---- 補正 ----

    async def correct(self, transcript: Transcript, glossary: str, ctx: MeetingContext) -> Transcript:
        model = self.settings.need("gemini_model_text")
        chunks = chunk_utterances(transcript.utterances, self.settings.correct_chunk_chars)
        sem = asyncio.Semaphore(max(1, self.settings.correct_concurrency))
        info = ctx.describe() + (f"\n\n{glossary}" if glossary else "")

        async def run(index: int, chunk: list[Utterance]) -> list[Utterance]:
            async with sem:
                original = Transcript(chunk)
                result = await self.llm.generate(
                    task="correct",
                    model=model,
                    system_instruction=self.prompts.correct,
                    parts=[TextPart(info), TextPart(original.to_text())],
                )
            corrected = Transcript.from_text(result.text)
            ratio = corrected.char_count / max(1, original.char_count)
            if (
                result.truncated
                or not corrected.utterances
                or not (CORRECTION_MIN_RATIO <= ratio <= CORRECTION_MAX_RATIO)
            ):
                # 発言が消えた・増えたなどの疑いがあるときは補正を使わない
                log_event(
                    logger,
                    "correct.rejected",
                    logging.WARNING,
                    chunk=index,
                    ratio=round(ratio, 2),
                    truncated=result.truncated,
                )
                return chunk
            return corrected.utterances

        parts = await asyncio.gather(*(run(i, c) for i, c in enumerate(chunks)))
        return Transcript([u for part in parts for u in part])

    # ---- 要約・構造化 ----

    async def summarize(
        self, transcript: Transcript, ctx: MeetingContext, category_choices: tuple[str, ...]
    ) -> SummaryResult:
        model = self.settings.need("gemini_model_text")
        info = ctx.describe()
        if category_choices:
            info += f"\ncategory の選択肢: {'、'.join(category_choices)}"
        parts = [TextPart(info), TextPart(transcript.to_text())]
        schema = self.prompts.schema(category_choices)
        # JSON が壊れていたときは要約だけをもう一度試す（文字起こしからやり直さない）
        for attempt in range(2):
            result = await self.llm.generate(
                task="summarize",
                model=model,
                system_instruction=self.prompts.summarize,
                parts=parts,
                json_schema=schema,
            )
            if result.truncated:
                raise ExternalServiceError("gemini", "要約が出力の上限で途中で切れました", retryable=False)
            try:
                summary = SummaryResult.model_validate(json.loads(result.text))
            except (ValueError, ValidationError) as exc:
                log_event(logger, "summarize.invalid_json", logging.WARNING, attempt=attempt + 1)
                if attempt == 1:
                    raise ExternalServiceError("gemini", "要約の形式が不正です", retryable=True) from exc
                continue
            return normalize_summary(summary, category_choices)
        raise ExternalServiceError("gemini", "要約の形式が不正です", retryable=True)
