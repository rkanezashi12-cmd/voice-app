"""Recall.ai の Webhook（Cloud Tasks 経由で受け取ったもの）の処理。

- bot.*（状態の変化）: 参加待ち・録音中・参加失敗・失敗を CRM に書く（field_map.BOT_EVENT_STATUS で決めたものだけ）。
  一時的な状態（参加待ち・録音中）は、古い通知（STATUS_EVENT_MAX_AGE_MINUTES より前）なら書かない。
- bot.done: 共通処理を積む（ボット ID で名前を付け、二重に積まない）。
- sdk_upload.complete: 共通処理を積む（デスクトップ方式）。
- sdk_upload.failed: 失敗を記録する（レコードが無ければ作る）。
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from app.client_services import ClientServices
from app.field_map import BOT_EVENT_STATUS, JOIN_FAILURE_SUBCODE_HINTS
from app.logs import log_event
from app.pipeline import formatting as fmt
from app.pipeline.process import PROCESS_PATH, ProcessRequest, ProcessRun
from app.services.recall_events import RecallEvent, parse_event
from app.services.tasks import task_name

logger = logging.getLogger(__name__)

TRANSIENT_STATUSES = frozenset({"waiting", "recording", "joining"})


def bot_status_for(ev: RecallEvent) -> str | None:
    """ボットの状態通知から、CRM に書く状態（StatusValues の属性名）を決める。"""
    code = ev.status
    sub = (ev.sub_code or "").lower()
    if code in ("fatal", "call_ended") and any(hint in sub for hint in JOIN_FAILURE_SUBCODE_HINTS):
        return "join_failed"
    return BOT_EVENT_STATUS.get(code)


def failure_message(ev: RecallEvent, attr: str) -> str:
    detail = f"（{ev.sub_code}）" if ev.sub_code else ""
    if attr == "join_failed":
        return f"ボットが会議に参加できませんでした{detail}。相手主催の Zoom では自社の参加者が先に入室してください。"
    if ev.status == "recording_permission_denied":
        return f"会議の主催者が録音を許可しませんでした{detail}"
    return f"ボットでエラーが発生しました{detail}"


async def handle_recall_event(rt: Any, client_id: str, payload: dict[str, Any]) -> str:
    ev = parse_event(payload)
    # 本文の形（docs/unverified-apis.md の H5）を実物で確かめるため、読み取れた ID の有無と項目名だけを残す
    data = payload.get("data")
    log_event(
        logger,
        "recall.event_parsed",
        client_id=client_id,
        event=ev.event,
        code=ev.code,
        sub_code=ev.sub_code,
        record_id=ev.metadata_value("record_id"),
        has_bot_id=bool(ev.bot_id),
        has_recording_id=bool(ev.recording_id),
        has_sdk_upload_id=bool(ev.sdk_upload_id),
        data_keys=sorted(str(k) for k in data) if isinstance(data, dict) else None,
    )
    cs = rt.client_services(client_id)
    if ev.kind == "bot" and ev.bot_id:
        if ev.status == "done":
            req = ProcessRequest(client_id=client_id, source="recall_bot", bot_id=ev.bot_id)
            await rt.tasks.enqueue(
                PROCESS_PATH, req.model_dump(), name=task_name("proc-bot", client_id, ev.bot_id)
            )
            return "process_enqueued"
        return await _apply_bot_status(rt, cs, ev)
    if ev.event == "sdk_upload.complete" and ev.sdk_upload_id:
        req = ProcessRequest(
            client_id=client_id,
            source="recall_desktop",
            sdk_upload_id=ev.sdk_upload_id,
            recording_id=ev.recording_id,
        )
        await rt.tasks.enqueue(
            PROCESS_PATH, req.model_dump(), name=task_name("proc-desktop", client_id, ev.sdk_upload_id)
        )
        return "process_enqueued"
    if ev.event == "sdk_upload.failed" and ev.sdk_upload_id:
        req = ProcessRequest(client_id=client_id, source="recall_desktop", sdk_upload_id=ev.sdk_upload_id)
        return await _record_desktop_failure(rt, req)
    log_event(logger, "recall.event_ignored", client_id=client_id, event=ev.event)
    return "ignored"


async def _apply_bot_status(rt: Any, cs: ClientServices, ev: RecallEvent) -> str:
    attr = bot_status_for(ev)
    if attr is None:
        return "ignored"
    max_age = timedelta(minutes=rt.settings.status_event_max_age_minutes)
    if attr in TRANSIENT_STATUSES and ev.updated_at and datetime.now(UTC) - ev.updated_at > max_age:
        log_event(logger, "recall.status_stale", bot_id=ev.bot_id, event=ev.event)
        return "stale"
    record_id = ev.metadata_value("record_id")
    if not record_id and ev.bot_id:
        bot = await (await cs.recall()).get_bot(ev.bot_id)
        metadata = bot.get("metadata") if isinstance(bot.get("metadata"), dict) else {}
        record_id = metadata.get("record_id")
    if not record_id:
        log_event(logger, "recall.status_no_record", logging.WARNING, bot_id=ev.bot_id, event=ev.event)
        return "no_record"
    fm = cs.field_map
    f = fm.meeting_record
    fields: dict[str, Any] = {f.status: getattr(fm.status, attr)}
    if attr in ("failed", "join_failed"):
        fields[f.error_message] = fmt.clip(failure_message(ev, attr), fm.limits.error_message)
    crm = await cs.crm()
    await crm.update_record(f.module, record_id, fields)
    log_event(
        logger, "recall.status_written", record_id=record_id, bot_id=ev.bot_id, event=ev.event, status=attr
    )
    return f"status_{attr}"


async def _record_desktop_failure(rt: Any, req: ProcessRequest) -> str:
    """デスクトップ録音のアップロード失敗。共通処理の失敗記録と同じ書き方にする。"""
    run = ProcessRun(rt, req, final_attempt=True)
    await run.record_failure("デスクトップアプリからの録音のアップロードに失敗しました")
    return "failure_recorded"
