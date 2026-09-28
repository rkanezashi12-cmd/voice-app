"""入口A：ボット参加（社内デモ・予備）。

POST /api/bots（CRM ワークフローの Deluge 関数から X-API-Key で呼ぶ）
→ Recall.ai でボットを予約 → 商談記録に recall_id と状態「予約済」を書く（1回）。
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Annotated
from urllib.parse import urlparse

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field, field_validator

from app.deps import ApiClientDep, RuntimeDep
from app.errors import AppError, ConfigError
from app.logs import log_event
from app.pipeline import formatting as fmt
from app.recording_token import RECORD_ID_RE

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/bots", tags=["bots"])

# Recall.ai は join_at を10分以上先にすれば定刻の参加を保証する。それより直前なら今すぐ参加させる
SCHEDULE_MIN_LEAD = timedelta(minutes=10)
MEETING_HOSTS = ("zoom.us", "zoom.com", "teams.microsoft.com", "teams.live.com", "meet.google.com")


class CreateBotRequest(BaseModel):
    record_id: str
    meeting_url: Annotated[str, Field(max_length=2048)]
    start_at: datetime | None = None
    bot_name: Annotated[str | None, Field(max_length=100)] = None

    @field_validator("record_id")
    @classmethod
    def check_record_id(cls, value: str) -> str:
        if not RECORD_ID_RE.match(value):
            raise ValueError("record_id の形式が不正です")
        return value

    @field_validator("meeting_url")
    @classmethod
    def check_meeting_url(cls, value: str) -> str:
        url = urlparse(value.strip())
        host = (url.hostname or "").lower()
        if url.scheme != "https" or not any(host == h or host.endswith(f".{h}") for h in MEETING_HOSTS):
            raise ValueError("Zoom / Teams / Google Meet の会議 URL（https）を指定してください")
        return value.strip()

    @field_validator("start_at")
    @classmethod
    def require_timezone(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is None:
            raise ValueError("start_at にはタイムゾーンを含めてください（例: 2026-10-01T10:00:00+09:00）")
        return value


class CreateBotResponse(BaseModel):
    bot_id: str
    join_at: datetime | None
    crm_updated: bool


def compute_join_at(start_at: datetime | None, now: datetime, lead_seconds: int) -> datetime | None:
    if start_at is None:
        return None
    join_at = start_at - timedelta(seconds=lead_seconds)
    return join_at if join_at - now >= SCHEDULE_MIN_LEAD else None


@router.post("", response_model=CreateBotResponse)
async def create_bot(body: CreateBotRequest, client: ApiClientDep, rt: RuntimeDep) -> CreateBotResponse:
    cs = rt.client_services(client.client_id)
    cfg = client.need_recall()
    bot_name = body.bot_name or cfg.bot_name
    if not bot_name:
        raise ConfigError("ボットの表示名（recall.bot_name）が設定されていません")
    fm = cs.field_map
    f = fm.meeting_record
    crm = await cs.crm()
    recall = await cs.recall()
    join_at = compute_join_at(body.start_at, datetime.now(UTC), cfg.join_lead_seconds)
    try:
        bot = await recall.create_bot(
            meeting_url=body.meeting_url,
            bot_name=bot_name,
            join_at=join_at,
            metadata={"client_id": client.client_id, "record_id": body.record_id},
            recording_config=cfg.bot_recording_config or None,
        )
    except AppError as exc:
        message = fmt.clip(f"ボットを予約できませんでした: {exc.message}", fm.limits.error_message)
        await crm.update_record(
            f.module, body.record_id, {f.status: fm.status.failed, f.error_message: message}
        )
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, message) from exc
    bot_id = str(bot.get("id", ""))
    await crm.update_record(
        f.module, body.record_id, {f.recall_id: bot_id, f.status: fm.status.reserved, f.error_message: None}
    )
    log_event(logger, "bot.reserved", client_id=client.client_id, record_id=body.record_id, bot_id=bot_id)
    return CreateBotResponse(bot_id=bot_id, join_at=join_at, crm_updated=not crm.dry_run)
