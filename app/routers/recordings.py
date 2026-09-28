"""入口B：対面録音。

- POST /api/recordings                         … CRM ワークフロー（X-API-Key）。録音ページ URL を発行
- GET  /api/recordings/{record_id}/session      … 録音ページ（Bearer）。トークンの内容確認
- POST /api/recordings/{record_id}/upload-url   … 録音ページ。1チャンク分の署名付き PUT URL を発行
- POST /api/recordings/{record_id}/events       … 録音ページ。停止検知などの診断イベントをログに残す
- POST /api/recordings/{record_id}/complete     … 録音ページ。録音終了 → 共通処理を Cloud Tasks に積む
"""

from __future__ import annotations

import logging
import re
import time
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Response, status
from pydantic import BaseModel, Field, field_validator

from app import recording_objects as ro
from app.deps import ApiClientDep, RecordingTokenDep, RuntimeDep
from app.logs import log_event
from app.recording_token import RECORD_ID_RE, compute_expiry, issue
from app.services.tasks import task_name

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/recordings", tags=["recordings"])

PROCESS_PATH = "/internal/process"
_EVENT_TYPE_RE = re.compile(r"^[a-z0-9_.-]{1,40}$")
_DETAIL_KEY_RE = re.compile(r"^[A-Za-z0-9_]{1,40}$")


class IssueRequest(BaseModel):
    record_id: str
    start_at: datetime | None = None
    # テスト用 URL（CRM に書き込まず、録音完了後も共通処理を動かさない）
    test: bool = False

    @field_validator("record_id")
    @classmethod
    def check_record_id(cls, value: str) -> str:
        if not RECORD_ID_RE.match(value):
            raise ValueError("record_id の形式が不正です")
        return value


class IssueResponse(BaseModel):
    recording_url: str
    expires_at: datetime
    crm_updated: bool


@router.post("", response_model=IssueResponse)
async def issue_recording_url(body: IssueRequest, client: ApiClientDep, rt: RuntimeDep) -> IssueResponse:
    settings = rt.settings
    start_ts = body.start_at.timestamp() if body.start_at else None
    expires_at = compute_expiry(
        now=time.time(), start_at=start_ts, ttl_hours=settings.recording_url_ttl_hours
    )
    token = issue(
        await rt.recording_secret(),
        client_id=client.client_id,
        record_id=body.record_id,
        expires_at=expires_at,
        test=body.test,
    )
    url = f"{settings.need('service_url')}/recorder/#{token}"
    crm_updated = False
    if not body.test:
        raise HTTPException(status.HTTP_501_NOT_IMPLEMENTED, "CRM への書き込みは Step 2 で実装します")
    log_event(
        logger,
        "recording.url_issued",
        client_id=client.client_id,
        record_id=body.record_id,
        test=body.test,
        expires_at=expires_at,
    )
    return IssueResponse(
        recording_url=url, expires_at=datetime.fromtimestamp(expires_at, tz=UTC), crm_updated=crm_updated
    )


class SessionInfo(BaseModel):
    record_id: str
    expires_at: datetime
    test: bool
    chunk_seconds: int = 60


@router.get("/{record_id}/session", response_model=SessionInfo)
async def session_info(claims: RecordingTokenDep) -> SessionInfo:
    return SessionInfo(
        record_id=claims.record_id,
        expires_at=datetime.fromtimestamp(claims.expires_at, tz=UTC),
        test=claims.test,
    )


class UploadUrlRequest(BaseModel):
    session_id: str
    seq: Annotated[int, Field(ge=0, le=ro.MAX_SEQ)]
    mime_type: Annotated[str, Field(max_length=100)]

    @field_validator("session_id")
    @classmethod
    def check_session_id(cls, value: str) -> str:
        if not ro.SESSION_ID_RE.match(value):
            raise ValueError("session_id の形式が不正です")
        return value


class UploadUrlResponse(BaseModel):
    url: str
    headers: dict[str, str]
    expires_in: int


@router.post("/{record_id}/upload-url", response_model=UploadUrlResponse)
async def upload_url(body: UploadUrlRequest, claims: RecordingTokenDep, rt: RuntimeDep) -> UploadUrlResponse:
    mime = ro.base_mime(body.mime_type)
    ext = ro.MIME_EXTENSIONS.get(mime)
    if ext is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"対応していない音声形式です: {mime}")
    name = ro.object_name(claims.client_id, claims.record_id, body.session_id, body.seq, ext)
    ttl = rt.settings.upload_url_ttl_seconds
    signed = await rt.storage.upload_url(name, mime, ttl)
    log_event(
        logger,
        "recording.upload_url_issued",
        client_id=claims.client_id,
        record_id=claims.record_id,
        session_id=body.session_id,
        seq=body.seq,
        mime=mime,
    )
    return UploadUrlResponse(url=signed, headers={"Content-Type": mime}, expires_in=ttl)


class RecorderEvent(BaseModel):
    type: str
    at: int
    session_id: str | None = None
    detail: dict[str, float | int | bool | str | None] = Field(default_factory=dict)

    @field_validator("type")
    @classmethod
    def check_type(cls, value: str) -> str:
        if not _EVENT_TYPE_RE.match(value):
            raise ValueError("type の形式が不正です")
        return value

    @field_validator("detail")
    @classmethod
    def check_detail(cls, value: dict[str, Any]) -> dict[str, Any]:
        if len(value) > 20:
            raise ValueError("detail の項目が多すぎます")
        cleaned: dict[str, Any] = {}
        for key, item in value.items():
            if not _DETAIL_KEY_RE.match(key):
                raise ValueError("detail のキーの形式が不正です")
            cleaned[key] = item[:120] if isinstance(item, str) else item
        return cleaned


class EventsRequest(BaseModel):
    events: Annotated[list[RecorderEvent], Field(max_length=200)]


@router.post("/{record_id}/events", status_code=status.HTTP_204_NO_CONTENT)
async def recorder_events(body: EventsRequest, claims: RecordingTokenDep) -> Response:
    for ev in body.events:
        detail = {f"d_{k}": v for k, v in ev.detail.items()}
        log_event(
            logger,
            "recorder.event",
            client_id=claims.client_id,
            record_id=claims.record_id,
            test=claims.test,
            event_type=ev.type,
            at=ev.at,
            session_id=ev.session_id,
            **detail,
        )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


class SessionManifest(BaseModel):
    session_id: str
    chunks: Annotated[int, Field(ge=0, le=ro.MAX_SEQ + 1)]


class CompleteRequest(BaseModel):
    sessions: Annotated[list[SessionManifest], Field(min_length=1, max_length=100)]
    # 送れなかったチャンクがあっても完了にする（録音ページで利用者が確認したとき）
    allow_missing: bool = False
    duration_ms: int | None = None


class CompleteResponse(BaseModel):
    status: str
    chunks: int
    missing: int


@router.post("/{record_id}/complete", response_model=CompleteResponse)
async def complete(body: CompleteRequest, claims: RecordingTokenDep, rt: RuntimeDep) -> CompleteResponse:
    objects = await rt.storage.list(ro.prefix(claims.client_id, claims.record_id))
    chunks = [c for c in (ro.parse_object(o.name, o.size) for o in objects) if c is not None]
    present = {(c.session_id, c.seq) for c in chunks}
    missing = [
        f"{s.session_id}/{seq}"
        for s in body.sessions
        for seq in range(s.chunks)
        if (s.session_id, seq) not in present
    ]
    if not chunks:
        raise HTTPException(
            status.HTTP_409_CONFLICT, {"message": "音声が1つも届いていません", "missing": missing[:50]}
        )
    if missing and not body.allow_missing:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            {"message": "届いていない音声があります", "missing": missing[:50], "missing_count": len(missing)},
        )
    log_event(
        logger,
        "recording.completed",
        client_id=claims.client_id,
        record_id=claims.record_id,
        test=claims.test,
        sessions=len(ro.group_sessions(chunks)),
        chunks=len(chunks),
        bytes=sum(c.size for c in chunks),
        missing=len(missing),
        duration_ms=body.duration_ms,
    )
    if claims.test:
        return CompleteResponse(status="test_completed", chunks=len(chunks), missing=len(missing))

    names = sorted(c.name for c in chunks)
    await rt.tasks.enqueue(
        PROCESS_PATH,
        {"client_id": claims.client_id, "source": "web_recording", "record_id": claims.record_id},
        name=task_name("web", claims.client_id, claims.record_id, *names),
    )
    return CompleteResponse(status="queued", chunks=len(chunks), missing=len(missing))
