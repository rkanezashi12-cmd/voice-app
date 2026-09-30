"""共通処理（入口3種）を偽物の外部サービスで通しで確かめる。"""

from __future__ import annotations

import logging
from typing import Any

import pytest

from app.errors import ExternalServiceError
from app.pipeline.process import ProcessRequest, run_process
from app.runtime import Runtime
from app.services.audio import AudioSegment
from tests.conftest import F, FakeCrm, FakeLlm, FakeRecall, FakeStorage, FakeTasks, S

RECALL_TRANSCRIPT = [
    {"participant": {"id": 1, "name": "山田（当社）"}, "words": [{"text": "本日は"}, {"text": "ありがとうございます。"}]},
    {"participant": {"id": 2, "name": "佐藤様"}, "words": [{"text": "治具の"}, {"text": "見積を"}, {"text": "お願いします。"}]},
]  # fmt: skip


def meeting_record(**extra: Any) -> dict[str, Any]:
    return {
        F.account: {"id": "a1", "name": "カスタマー株式会社"},
        F.start_at: "2026-09-28T10:00:00+09:00",
        F.status: S.reserved,
        F.meeting_type: "オンライン",
        **extra,
    }


def bot_request(**extra: Any) -> ProcessRequest:
    return ProcessRequest(client_id="default", source="recall_bot", bot_id="bot-1", **extra)


# ---- 入口A：ボット ----


async def test_bot_flow_updates_crm_once_and_deletes_media(
    runtime: Runtime, crm: FakeCrm, recall: FakeRecall, llm: FakeLlm
) -> None:
    crm.add(F.module, "5001", meeting_record())
    crm.add(
        "Glossary",
        "g1",
        {"Name": "マルサン木型", "Misrecognitions": "丸三木型、まるさん", "Term_Type": "社名"},
    )
    recall.add_bot("bot-1", "5001", transcript=RECALL_TRANSCRIPT)

    outcome = await run_process(runtime, bot_request(), final_attempt=False)

    assert outcome.status == "done"
    writes = crm.writes_to("5001")
    assert writes[0] == {F.status: S.transcribing, F.error_message: None}
    assert len(writes) == 2, "途中経過1回＋結果1回"
    final = writes[1]
    assert final[F.status] == S.done
    assert (
        final[F.transcript]
        == "山田（当社）: 本日はありがとうございます。\n佐藤様: 治具の見積をお願いします。"
    )
    assert final[F.transcript_2] is None
    assert final[F.summary] == "新型治具の見積を依頼された。"
    assert final[F.issues] == "・段取り替えに時間がかかる"
    assert final[F.next_actions].startswith("・見積書を送る（担当: 当社、期限: 2026-10-03）")
    assert final[F.due_date] == "2026-10-01", "次のアクションのうち最も早い期日"
    assert final[F.budget] == "300万円程度"
    assert final[F.error_message] is None
    assert recall.deleted_bot_media == ["bot-1"]
    # 用語辞書が補正のプロンプトに入る
    correct_call = next(c for c in llm.calls if c["task"] == "correct")
    assert "マルサン木型" in correct_call["parts"][0].text
    assert "丸三木型" in correct_call["parts"][0].text
    assert "取引先: カスタマー株式会社" in correct_call["parts"][0].text


async def test_bot_flow_is_not_processed_twice(runtime: Runtime, crm: FakeCrm, recall: FakeRecall) -> None:
    crm.add(F.module, "5001", meeting_record())
    recall.add_bot("bot-1", "5001", transcript=RECALL_TRANSCRIPT)
    await run_process(runtime, bot_request(), final_attempt=False)
    writes_before = len(crm.writes)

    outcome = await run_process(runtime, bot_request(), final_attempt=False)

    assert outcome.status == "skipped"
    assert len(crm.writes) == writes_before
    assert recall.deleted_bot_media == ["bot-1", "bot-1"], "削除だけはやり直す"


async def test_waits_for_recall_transcript(
    runtime: Runtime, crm: FakeCrm, recall: FakeRecall, tasks: FakeTasks
) -> None:
    crm.add(F.module, "5001", meeting_record())
    recall.add_bot("bot-1", "5001", transcript=None)

    outcome = await run_process(runtime, bot_request(), final_attempt=False)

    assert outcome.status == "waiting"
    assert recall.transcript_requests == [
        ("rec-1", {"provider": {"recallai_async": {"language_code": "ja"}}})
    ]
    queued = tasks.enqueued[0]
    assert queued["payload"]["wait_count"] == 1
    assert queued["delay_seconds"] == 120
    assert crm.writes_to("5001") == [{F.status: S.transcribing, F.error_message: None}]

    # 2回目の確認では依頼し直さず、途中経過も書かない
    outcome = await run_process(runtime, bot_request(wait_count=1), final_attempt=False)
    assert outcome.status == "waiting"
    assert len(recall.transcript_requests) == 1
    assert len(crm.writes_to("5001")) == 1
    assert tasks.enqueued[1]["payload"]["wait_count"] == 2


async def test_gives_up_waiting_after_limit(runtime: Runtime, crm: FakeCrm, recall: FakeRecall) -> None:
    crm.add(F.module, "5001", meeting_record())
    recall.add_bot("bot-1", "5001", transcript=None)
    outcome = await run_process(runtime, bot_request(wait_count=30), final_attempt=False)
    assert outcome.status == "failed"
    assert crm.record("5001")[F.status] == S.failed


async def test_bot_without_recording_keeps_join_failed(
    runtime: Runtime, crm: FakeCrm, recall: FakeRecall
) -> None:
    crm.add(F.module, "5001", meeting_record(**{F.status: S.join_failed}))
    recall.add_bot("bot-1", "5001", recording_id=None)
    outcome = await run_process(runtime, bot_request(), final_attempt=False)
    assert outcome.status == "failed"
    assert crm.writes == [], "「参加失敗」を上書きしない"


async def test_failure_after_join_failed_with_recording_is_written(
    runtime: Runtime, crm: FakeCrm, recall: FakeRecall, llm: FakeLlm
) -> None:
    """「参加失敗」の後でも録音があれば処理する。その後の失敗は「文字起こし中」のまま残さず「失敗」と書く。"""
    crm.add(F.module, "5001", meeting_record(**{F.status: S.join_failed}))
    recall.add_bot("bot-1", "5001", transcript=RECALL_TRANSCRIPT)
    llm.fail = ExternalServiceError("gemini", "quota", status=429, retryable=True)

    outcome = await run_process(runtime, bot_request(), final_attempt=True)

    assert outcome.status == "failed"
    record = crm.record("5001")
    assert record[F.status] == S.failed
    assert "gemini" in record[F.error_message]


# ---- 失敗時 ----


async def test_retryable_error_does_not_write_failure_until_last_attempt(
    runtime: Runtime, crm: FakeCrm, recall: FakeRecall, llm: FakeLlm
) -> None:
    crm.add(F.module, "5001", meeting_record())
    recall.add_bot("bot-1", "5001", transcript=RECALL_TRANSCRIPT)
    llm.fail = ExternalServiceError("gemini", "quota", status=429, retryable=True)

    outcome = await run_process(runtime, bot_request(), final_attempt=False)
    assert outcome.status == "retry"
    assert crm.record("5001")[F.status] == S.transcribing

    outcome = await run_process(runtime, bot_request(), final_attempt=True)
    assert outcome.status == "failed"
    record = crm.record("5001")
    assert record[F.status] == S.failed
    assert "gemini" in record[F.error_message]
    assert recall.deleted_bot_media == [], "失敗時は再処理できるよう音声を残す（既定）"


async def test_permanent_error_is_written_immediately(
    runtime: Runtime, crm: FakeCrm, recall: FakeRecall
) -> None:
    crm.add(F.module, "5001", meeting_record())
    recall.add_bot("bot-1", "5001", transcript=[])
    outcome = await run_process(runtime, bot_request(), final_attempt=False)
    assert outcome.status == "failed"
    assert "空でした" in crm.record("5001")[F.error_message]


async def test_missing_record_is_reported(runtime: Runtime, crm: FakeCrm, recall: FakeRecall) -> None:
    recall.add_bot("bot-1", "9999", transcript=RECALL_TRANSCRIPT)
    outcome = await run_process(runtime, bot_request(), final_attempt=False)
    assert outcome.status == "failed"
    assert crm.writes_to("9999")[-1][F.status] == S.failed


async def test_media_delete_failure_is_retried(runtime: Runtime, crm: FakeCrm, recall: FakeRecall) -> None:
    crm.add(F.module, "5001", meeting_record())
    recall.add_bot("bot-1", "5001", transcript=RECALL_TRANSCRIPT)
    recall.fail_delete = ExternalServiceError("recall", "unavailable", status=503, retryable=True)
    outcome = await run_process(runtime, bot_request(), final_attempt=False)
    assert outcome.status == "retry"
    assert crm.record("5001")[F.status] == S.done, "結果は保存済み"
    recall.fail_delete = None
    outcome = await run_process(runtime, bot_request(), final_attempt=False)
    assert outcome.status == "skipped"
    assert recall.deleted_bot_media == ["bot-1"]


async def test_long_transcript_is_split_and_truncated(
    runtime: Runtime, crm: FakeCrm, recall: FakeRecall, caplog: pytest.LogCaptureFixture
) -> None:
    long_text = [
        {"participant": {"name": "話者"}, "words": [{"text": f"{i:05d}" + "あ" * 90}]} for i in range(800)
    ]
    # 同じ話者が続くとまとめられるので、話者を交互にする
    for i, seg in enumerate(long_text):
        seg["participant"]["name"] = "山田" if i % 2 else "佐藤"
    crm.add(F.module, "5001", meeting_record())
    recall.add_bot("bot-1", "5001", transcript=long_text)
    with caplog.at_level(logging.WARNING):
        outcome = await run_process(runtime, bot_request(), final_attempt=False)
    assert outcome.status == "done"
    final = crm.writes_to("5001")[-1]
    assert len(final[F.transcript]) <= 32000
    assert len(final[F.transcript_2]) <= 32000
    assert final[F.transcript_2].endswith("文字）")
    assert "（以降省略：全体" in final[F.transcript_2]
    assert any(r.getMessage() == "transcript.truncated" for r in caplog.records)


# ---- 入口B：対面録音 ----


async def test_web_recording_flow(
    runtime: Runtime,
    crm: FakeCrm,
    storage: FakeStorage,
    llm: FakeLlm,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    crm.add(F.module, "6001", meeting_record(**{F.meeting_type: "対面"}))
    session = "1727488500123-a-abc123"
    for seq in range(3):
        storage.objects[f"recordings/default/6001/{session}/{seq:06d}.m4a"] = b"audio"
    seen: dict[str, Any] = {}

    async def fake_prepare(
        sessions: list[Any], download: Any, workdir: Any, **kwargs: Any
    ) -> list[AudioSegment]:
        seen["sessions"] = sessions
        seen["kwargs"] = kwargs
        return [AudioSegment(0, 0.0, 1200.0, b"mp3-1"), AudioSegment(1, 1200.0, 1500.0, b"mp3-2")]

    monkeypatch.setattr("app.services.audio.prepare_segments", fake_prepare)
    llm.transcribe_outputs = ["話者A: 前半です。\n話者B: はい。", "話者A: 後半です。"]

    outcome = await run_process(
        runtime,
        ProcessRequest(client_id="default", source="web_recording", record_id="6001"),
        final_attempt=False,
    )

    assert outcome.status == "done"
    assert [c.seq for c in seen["sessions"][0].chunks] == [0, 1, 2]
    assert seen["kwargs"]["segment_seconds"] == 1200
    final = crm.writes_to("6001")[-1]
    assert final[F.transcript] == "話者A: 前半です。\n話者B: はい。\n話者A: 後半です。"
    assert storage.deleted_prefixes == ["recordings/default/6001/"]
    transcribe_calls = [c for c in llm.calls if c["task"] == "transcribe"]
    assert len(transcribe_calls) == 2
    assert transcribe_calls[0]["model"] == "gemini-test-audio"
    assert transcribe_calls[1]["parts"][1].data == b"mp3-2"
    assert "前の区間の末尾" in transcribe_calls[1]["parts"][0].text, (
        "話者ラベルをそろえるため前区間の末尾を渡す"
    )


async def test_web_recording_without_audio_fails(runtime: Runtime, crm: FakeCrm) -> None:
    crm.add(F.module, "6001", meeting_record())
    outcome = await run_process(
        runtime,
        ProcessRequest(client_id="default", source="web_recording", record_id="6001"),
        final_attempt=False,
    )
    assert outcome.status == "failed"
    assert "録音データが見つかりません" in crm.record("6001")[F.error_message]


# ---- 入口C：デスクトップ ----


async def test_desktop_flow_creates_record_without_account(
    runtime: Runtime, crm: FakeCrm, recall: FakeRecall
) -> None:
    recall.uploads["upload-1"] = {"id": "upload-1", "metadata": {"owner_id": "9001"}, "recording_id": "rec-9"}
    recall.add_recording("rec-9", RECALL_TRANSCRIPT)
    outcome = await run_process(
        runtime,
        ProcessRequest(client_id="default", source="recall_desktop", sdk_upload_id="upload-1"),
        final_attempt=False,
    )
    assert outcome.status == "done"
    kind, _module, _record_id, data = crm.writes[-1]
    assert kind == "upsert"
    assert data[F.status] == S.no_account
    assert data[F.recall_id] == "upload-1"
    assert data[F.owner] == {"id": "9001"}
    assert data[F.capture_method] == "デスクトップ"
    assert data[F.name].endswith("オンライン商談")
    assert recall.deleted_recordings == ["rec-9"]


async def test_desktop_flow_updates_linked_record(runtime: Runtime, crm: FakeCrm, recall: FakeRecall) -> None:
    crm.add(F.module, "7001", {F.recall_id: "upload-1", F.status: S.transcribing, F.account: {"id": "a1"}})
    recall.uploads["upload-1"] = {"id": "upload-1", "metadata": {}, "recording": {"id": "rec-9"}}
    recall.add_recording("rec-9", RECALL_TRANSCRIPT)
    outcome = await run_process(
        runtime,
        ProcessRequest(client_id="default", source="recall_desktop", sdk_upload_id="upload-1"),
        final_attempt=False,
    )
    assert outcome.status == "done"
    assert outcome.record_id == "7001"
    assert crm.record("7001")[F.status] == S.done
    assert [w[0] for w in crm.writes] == ["update", "update"]
