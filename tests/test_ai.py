from __future__ import annotations

from datetime import date

import pytest

from app.errors import ExternalServiceError, PermanentError
from app.pipeline.ai import MeetingAI, PromptStore, chunk_utterances
from app.pipeline.models import MeetingContext, Transcript, Utterance
from app.services.audio import AudioSegment
from app.services.gemini import LlmResult
from tests.conftest import FakeLlm, make_settings, summary_json

CTX = MeetingContext(
    client_id="default", record_id="1", meeting_date=date(2026, 9, 28), account_name="カスタマー"
)


def ai(llm: FakeLlm, **settings: object) -> MeetingAI:
    return MeetingAI(llm, PromptStore(), make_settings(**settings))


def test_prompts_are_loaded_from_files() -> None:
    prompts = PromptStore()
    assert "文字起こし" in prompts.transcribe
    assert "校正" in prompts.correct
    assert "JSON" in prompts.summarize
    schema = prompts.schema(("新規", "既存"))
    assert schema["properties"]["category"]["enum"] == ["新規", "既存", None]
    assert "enum" not in prompts.schema()["properties"]["category"], "選択肢が無ければ自由記述"


def test_chunk_utterances_keeps_lines_whole() -> None:
    utterances = [Utterance("A", "あ" * 40) for _ in range(10)]
    chunks = chunk_utterances(utterances, 100)
    assert all(len(c) == 2 for c in chunks)
    assert sum(len(c) for c in chunks) == 10


async def test_correction_applies_glossary_fix() -> None:
    llm = FakeLlm()
    llm.replace = {"丸三木型": "マルサン木型"}
    t = Transcript([Utterance("話者A", "丸三木型さんの件です")])
    out = await ai(llm).correct(t, "# 用語辞書", CTX)
    assert out.utterances == [Utterance("話者A", "マルサン木型さんの件です")]


async def test_correction_is_rejected_when_content_is_lost() -> None:
    class DroppingLlm(FakeLlm):
        async def generate(self, **kwargs: object) -> LlmResult:
            await super().generate(**kwargs)  # type: ignore[arg-type]
            return LlmResult("話者A: 短い")

    t = Transcript([Utterance("話者A", "とても長い発言がここにあります。" * 5)])
    out = await ai(DroppingLlm()).correct(t, "", CTX)
    assert out.utterances == t.utterances, "発言が消えた補正は使わない"


async def test_summarize_retries_invalid_json_once() -> None:
    class FlakyLlm(FakeLlm):
        async def generate(self, **kwargs: object) -> LlmResult:
            await super().generate(**kwargs)  # type: ignore[arg-type]
            return LlmResult("{broken" if len(self.calls) == 1 else summary_json())

    llm = FlakyLlm()
    summary = await ai(llm).summarize(Transcript([Utterance("A", "x")]), CTX, ())
    assert summary.budget == "300万円程度"
    assert len(llm.calls) == 2
    assert llm.calls[0]["json_schema"]["required"]


async def test_summarize_gives_up_after_two_invalid_answers() -> None:
    llm = FakeLlm()
    llm.summary_output = "not json"
    with pytest.raises(ExternalServiceError):
        await ai(llm).summarize(Transcript([Utterance("A", "x")]), CTX, ())


async def test_truncated_transcription_is_an_error() -> None:
    class TruncLlm(FakeLlm):
        async def generate(self, **kwargs: object) -> LlmResult:
            return LlmResult("話者A: 途中", truncated=True)

    with pytest.raises(PermanentError):
        await ai(TruncLlm()).transcribe([AudioSegment(0, 0, 10, b"x")], CTX, "")


async def test_parallel_transcription_option() -> None:
    llm = FakeLlm()
    llm.transcribe_outputs = ["話者A: 1", "話者A: 2", "話者A: 3"]
    segs = [AudioSegment(i, i * 10.0, (i + 1) * 10.0, b"x") for i in range(3)]
    out = await ai(llm, transcribe_concurrency=3).transcribe(segs, CTX, "")
    assert len(out.utterances) == 3
    assert all("前の区間の末尾" not in c["parts"][0].text for c in llm.calls)
