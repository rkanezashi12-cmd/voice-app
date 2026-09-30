"""Recall.ai の Webhook（Cloud Tasks 経由）の処理：状態の書き込みと共通処理の投入。"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.pipeline.recall_flow import bot_status_for, handle_recall_event
from app.runtime import Runtime
from app.services.recall_events import parse_event, recording_id_from_sdk_upload, transcript_shortcut
from tests.conftest import F, FakeCrm, FakeRecall, FakeTasks, S


def bot_event(
    code: str, *, sub_code: str | None = None, record_id: str | None = "5001", age_minutes: int = 0
) -> dict:
    updated = (datetime.now(UTC) - timedelta(minutes=age_minutes)).isoformat()
    metadata = {"record_id": record_id} if record_id else {}
    return {
        "event": f"bot.{code}",
        "data": {
            "data": {"code": code, "sub_code": sub_code, "updated_at": updated},
            "bot": {"id": "bot-1", "metadata": metadata},
        },
    }


def test_parse_event_shapes() -> None:
    ev = parse_event(bot_event("in_waiting_room"))
    assert (ev.kind, ev.status, ev.bot_id, ev.metadata_value("record_id")) == (
        "bot",
        "in_waiting_room",
        "bot-1",
        "5001",
    )
    ev = parse_event(
        {"event": "sdk_upload.complete", "data": {"sdk_upload": {"id": "u1"}, "recording": {"id": "r1"}}}
    )
    assert (ev.sdk_upload_id, ev.recording_id) == ("u1", "r1")
    assert parse_event({"event": 1, "data": "x"}).event == ""


def test_transcript_shortcut_and_upload_recording() -> None:
    rec = {
        "media_shortcuts": {"transcript": {"status": {"code": "done"}, "data": {"download_url": "https://x"}}}
    }
    assert transcript_shortcut(rec) == ("done", "https://x")
    assert transcript_shortcut({"media_shortcuts": {}}) == (None, None)
    assert recording_id_from_sdk_upload({"recording": {"id": "r1"}}) == "r1"
    assert recording_id_from_sdk_upload({"recording_id": "r2"}) == "r2"


def test_join_failure_is_detected_from_sub_code() -> None:
    assert (
        bot_status_for(parse_event(bot_event("fatal", sub_code="bot_kicked_from_waiting_room")))
        == "join_failed"
    )
    assert (
        bot_status_for(parse_event(bot_event("call_ended", sub_code="timeout_exceeded_waiting_room")))
        == "join_failed"
    )
    assert bot_status_for(parse_event(bot_event("call_ended", sub_code="call_ended_by_host"))) is None
    assert bot_status_for(parse_event(bot_event("fatal", sub_code="unknown_error"))) == "failed"
    assert bot_status_for(parse_event(bot_event("joining_call"))) is None
    assert (
        bot_status_for(parse_event(bot_event("call_ended", sub_code="timeout_exceeded_noone_joined")))
        == "join_failed"
    )
    assert (
        bot_status_for(parse_event(bot_event("fatal", sub_code="google_meet_bot_blocked"))) == "join_failed"
    )


@pytest.mark.parametrize(
    "sub_code",
    [
        "timeout_exceeded_everyone_left",  # 2026-10-01 の Google Meet のテストで実際に届いたもの
        "bot_kicked_from_call",
        "timeout_exceeded_recording_permission_denied",
    ],
)
def test_call_ended_after_joining_is_not_join_failure(sub_code: str) -> None:
    """会議に入ったあとで終わったもの。録音があれば共通処理が進むので、参加失敗と書かない。"""
    assert bot_status_for(parse_event(bot_event("call_ended", sub_code=sub_code))) is None


async def test_bot_removed_from_call_writes_nothing(runtime: Runtime, crm: FakeCrm) -> None:
    assert (
        await handle_recall_event(
            runtime, "default", bot_event("call_ended", sub_code="bot_kicked_from_call")
        )
        == "ignored"
    )
    assert crm.writes == []


async def test_waiting_room_and_recording_are_written(runtime: Runtime, crm: FakeCrm) -> None:
    assert await handle_recall_event(runtime, "default", bot_event("in_waiting_room")) == "status_waiting"
    assert await handle_recall_event(runtime, "default", bot_event("in_call_recording")) == "status_recording"
    assert [w[F.status] for w in crm.writes_to("5001")] == [S.waiting, S.recording]


async def test_event_shape_is_logged_without_content(
    runtime: Runtime, caplog: pytest.LogCaptureFixture
) -> None:
    """本文の形（docs/unverified-apis.md の H5）を実物で確かめるため、ID の有無と項目名だけを残す。"""
    with caplog.at_level(logging.INFO):
        await handle_recall_event(runtime, "default", bot_event("in_waiting_room"))
    parsed = [r for r in caplog.records if r.getMessage() == "recall.event_parsed"]
    assert len(parsed) == 1
    fields = parsed[0].fields
    assert (fields["event"], fields["code"], fields["record_id"]) == (
        "bot.in_waiting_room",
        "in_waiting_room",
        "5001",
    )
    assert (fields["has_bot_id"], fields["has_recording_id"], fields["has_sdk_upload_id"]) == (
        True,
        False,
        False,
    )
    assert fields["data_keys"] == ["bot", "data"]


async def test_transient_status_is_ignored(runtime: Runtime, crm: FakeCrm) -> None:
    assert await handle_recall_event(runtime, "default", bot_event("joining_call")) == "ignored"
    assert crm.writes == []


async def test_stale_transient_status_is_not_written(runtime: Runtime, crm: FakeCrm) -> None:
    assert (
        await handle_recall_event(runtime, "default", bot_event("in_call_recording", age_minutes=90))
        == "stale"
    )
    assert crm.writes == []


async def test_join_failure_writes_message(runtime: Runtime, crm: FakeCrm) -> None:
    await handle_recall_event(
        runtime, "default", bot_event("fatal", sub_code="bot_kicked_from_waiting_room", age_minutes=90)
    )
    write = crm.writes_to("5001")[0]
    assert write[F.status] == S.join_failed
    assert "参加できませんでした" in write[F.error_message]
    assert "bot_kicked_from_waiting_room" in write[F.error_message]


async def test_record_id_is_looked_up_when_metadata_missing(
    runtime: Runtime, crm: FakeCrm, recall: FakeRecall
) -> None:
    recall.add_bot("bot-1", "5009")
    await handle_recall_event(runtime, "default", bot_event("in_waiting_room", record_id=None))
    assert crm.writes_to("5009")[0][F.status] == S.waiting


async def test_bot_done_enqueues_processing_once(runtime: Runtime, tasks: FakeTasks) -> None:
    await handle_recall_event(runtime, "default", bot_event("done"))
    await handle_recall_event(runtime, "default", bot_event("done"))
    assert len(tasks.enqueued) == 1
    assert tasks.enqueued[0]["payload"] == {
        "client_id": "default",
        "source": "recall_bot",
        "record_id": None,
        "bot_id": "bot-1",
        "sdk_upload_id": None,
        "recording_id": None,
        "wait_count": 0,
        "force": False,
    }


async def test_sdk_upload_complete_enqueues_desktop_processing(runtime: Runtime, tasks: FakeTasks) -> None:
    payload = {
        "event": "sdk_upload.complete",
        "data": {"sdk_upload": {"id": "u1"}, "recording": {"id": "r1"}},
    }
    assert await handle_recall_event(runtime, "default", payload) == "process_enqueued"
    queued: dict[str, Any] = tasks.enqueued[0]["payload"]
    assert (queued["source"], queued["sdk_upload_id"], queued["recording_id"]) == (
        "recall_desktop",
        "u1",
        "r1",
    )


async def test_sdk_upload_completed_is_also_accepted(runtime: Runtime, tasks: FakeTasks) -> None:
    """公式ドキュメントは sdk_upload.complete、Recall.ai のブログは sdk_upload.completed。どちらでも処理を積む。"""
    payload = {"event": "sdk_upload.completed", "data": {"sdk_upload": {"id": "u1"}}}
    assert await handle_recall_event(runtime, "default", payload) == "process_enqueued"
    assert tasks.enqueued[0]["payload"]["sdk_upload_id"] == "u1"


async def test_sdk_upload_failed_creates_failed_record(
    runtime: Runtime, crm: FakeCrm, recall: FakeRecall
) -> None:
    recall.uploads["u1"] = {"id": "u1", "metadata": {}}
    payload = {"event": "sdk_upload.failed", "data": {"sdk_upload": {"id": "u1"}}}
    assert await handle_recall_event(runtime, "default", payload) == "failure_recorded"
    kind, _, _, data = crm.writes[0]
    assert kind == "upsert"
    assert data[F.status] == S.failed
    assert data[F.recall_id] == "u1"


async def test_sdk_upload_failed_for_unknown_upload_writes_nothing(runtime: Runtime, crm: FakeCrm) -> None:
    """ダッシュボードのテスト送信（例の ID）では、CRM に失敗の記録を作らない。"""
    payload = {"event": "sdk_upload.failed", "data": {"sdk_upload": {"id": "example-upload"}}}
    assert await handle_recall_event(runtime, "default", payload) == "failure_recorded"
    assert crm.writes == []


def test_internal_endpoint_runs_the_handler(client: TestClient, crm: FakeCrm) -> None:
    res = client.post(
        "/internal/recall-event", json={"client_id": "default", "payload": bot_event("in_waiting_room")}
    )
    assert res.json() == {"status": "status_waiting"}
    assert crm.writes_to("5001")[0][F.status] == S.waiting
