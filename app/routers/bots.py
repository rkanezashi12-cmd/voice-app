"""入口A：ボット参加（社内デモ・予備）。

POST /api/bots（CRM ワークフローの Deluge 関数から X-API-Key で呼ぶ）
→ Recall.ai でボットを予約 → 商談記録に recall_id と状態「予約済」を書く（1回）。
"""

from __future__ import annotations

import logging
import re
import unicodedata
from datetime import UTC, datetime, timedelta
from typing import Annotated
from urllib.parse import quote, urlparse

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field, field_validator

from app.deps import ApiClientDep, RuntimeDep
from app.errors import AppError, ConfigError, ExternalServiceError
from app.logs import log_event
from app.pipeline import formatting as fmt
from app.recording_token import RECORD_ID_RE

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/bots", tags=["bots"])

# Recall.ai は join_at を10分以上先にすれば定刻の参加を保証する。それより直前なら今すぐ参加させる
SCHEDULE_MIN_LEAD = timedelta(minutes=10)
MEETING_HOSTS = ("zoom.us", "zoom.com", "teams.microsoft.com", "teams.live.com", "meet.google.com")
# Recall.ai の 507：すぐ参加できるボットの空きが無い（10分以上先の予約では起きない。docs/unverified-apis.md）
NO_READY_BOT_HINT = (
    "Recall.ai にすぐ参加できるボットの空きがありませんでした。開始日時を10分以上先にして保存し直してください"
    "（10分以上先の予約なら空きの問題は起きません）"
)


# 会議パスコードの置き場所（Recall.ai は会議 URL の中のパスコードで参加する。docs/unverified-apis.md の H12）。
# Zoom は pwd、Teams の短い URL（/meet/<会議 ID>）は p。Google Meet にはパスコードが無い
ZOOM_HOSTS = ("zoom.us", "zoom.com")
TEAMS_HOSTS = ("teams.microsoft.com", "teams.live.com")
PASSCODE_RE = re.compile(r"^[\x21-\x7e]{1,64}$")
BAD_PASSCODE = (
    "会議パスコードは半角の英数字・記号（64文字まで）で入れてください。直して保存すると、もう一度予約します"
)


def _host_in(host: str, hosts: tuple[str, ...]) -> bool:
    return any(host == h or host.endswith(f".{h}") for h in hosts)


def passcode_param(meeting_url: str) -> str | None:
    """会議パスコードを入れるクエリの名前（入れられない URL なら None）。"""
    url = urlparse(meeting_url)
    host = (url.hostname or "").lower()
    if _host_in(host, ZOOM_HOSTS):
        return "pwd"
    if _host_in(host, TEAMS_HOSTS) and url.path.startswith("/meet/"):
        return "p"
    return None


def with_passcode(meeting_url: str, passcode: str | None) -> tuple[str, str]:
    """会議 URL にパスコードを足す。返り値は（URL、ログに出す結果）。

    URL にパスコードが入っていればそちらを使う（招待の URL のほうが確か）。ほかのクエリはそのまま残す。
    """
    param = passcode_param(meeting_url)
    url = urlparse(meeting_url)
    parts = [p for p in url.query.split("&") if p]
    in_url = param is not None and any(p.startswith(f"{param}=") and len(p) > len(param) + 1 for p in parts)
    if not passcode:
        return meeting_url, "in_url" if in_url else "none"
    if param is None:
        return meeting_url, "unused"
    if in_url:
        return meeting_url, "in_url"
    parts = [p for p in parts if p != param and not p.startswith(f"{param}=")]
    parts.append(f"{param}={quote(passcode, safe='')}")
    return url._replace(query="&".join(parts)).geturl(), "added"


def normalize_passcode(value: str | None) -> str | None:
    """全角を半角にし、空白を除く（招待からのコピーで入ることがある）。空なら None。"""
    value = "".join(unicodedata.normalize("NFKC", value or "").split())
    return value or None


class CreateBotRequest(BaseModel):
    record_id: str
    meeting_url: Annotated[str, Field(max_length=2048)]
    # 商談記録の「会議パスコード」（招待で URL と別に書かれているとき）。ログには出さない。
    # 長さは 422 の応答に値が出ないよう、ここでは制限せず create_bot で確かめる
    meeting_passcode: str | None = None
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
        if url.scheme != "https" or not _host_in(host, MEETING_HOSTS):
            raise ValueError("Zoom / Teams / Google Meet の会議 URL（https）を指定してください")
        return value.strip()

    @field_validator("meeting_passcode")
    @classmethod
    def clean_meeting_passcode(cls, value: str | None) -> str | None:
        # 形が違うときは 422 にせず、商談記録の「エラー内容」に理由を書く（create_bot）
        return normalize_passcode(value)

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
    if body.meeting_passcode and not PASSCODE_RE.match(body.meeting_passcode):
        message = fmt.clip(f"ボットを予約できませんでした: {BAD_PASSCODE}", fm.limits.error_message)
        await crm.update_record(
            f.module, body.record_id, {f.status: fm.status.failed, f.error_message: message}
        )
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, message)
    meeting_url, passcode = with_passcode(body.meeting_url, body.meeting_passcode)
    try:
        bot = await recall.create_bot(
            meeting_url=meeting_url,
            bot_name=bot_name,
            join_at=join_at,
            metadata={"client_id": client.client_id, "record_id": body.record_id},
            recording_config=cfg.bot_recording_config or None,
        )
    except AppError as exc:
        reason = exc.message
        if isinstance(exc, ExternalServiceError) and exc.status == 507:
            reason = f"{NO_READY_BOT_HINT}（{exc.message}）"
        message = fmt.clip(f"ボットを予約できませんでした: {reason}", fm.limits.error_message)
        await crm.update_record(
            f.module, body.record_id, {f.status: fm.status.failed, f.error_message: message}
        )
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, message) from exc
    bot_id = str(bot.get("id", ""))
    await crm.update_record(
        f.module, body.record_id, {f.recall_id: bot_id, f.status: fm.status.reserved, f.error_message: None}
    )
    log_event(
        logger,
        "bot.reserved",
        client_id=client.client_id,
        record_id=body.record_id,
        bot_id=bot_id,
        passcode_state=passcode,
    )
    return CreateBotResponse(bot_id=bot_id, join_at=join_at, crm_updated=not crm.dry_run)
