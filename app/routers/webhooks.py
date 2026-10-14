"""Recall.ai の Webhook 受信。

署名（Svix 形式）を検証し、処理は Cloud Tasks に積んですぐ 200 を返す。
同じ webhook-id の通知はタスク名が同じになるので、Cloud Tasks が二重登録を拒否する。

どのクライアントの通知か：複数のクライアントが同じ Recall.ai のアカウント（同じ署名用シークレット）を使うことがあるので、
署名だけでは決まらない。ボット・アップロードを作るときに metadata に入れた client_id で決め、
そのクライアントのシークレットで署名が合うことも確かめる（合わなければ処理しない）。
metadata に client_id が無い通知は、署名が合うクライアントが1つだけのときに限りそのクライアントとする。
"""

from __future__ import annotations

import json
import logging

from fastapi import APIRouter, HTTPException, Request, status

from app.clients import ClientConfig
from app.deps import RuntimeDep
from app.errors import ConfigError
from app.logs import log_event
from app.runtime import Runtime
from app.services.recall_events import parse_event
from app.services.tasks import task_name
from app.webhook_signature import SignatureError, verify

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/webhooks", tags=["webhooks"])

MAX_BODY_BYTES = 1_000_000
RECALL_EVENT_PATH = "/internal/recall-event"
HANDLED_EVENTS = ("bot.", "sdk_upload.complete", "sdk_upload.failed")


async def _verified_clients(
    rt: Runtime, headers: dict[str, str], body: bytes
) -> tuple[list[ClientConfig], str]:
    """署名が合うクライアントをすべて返す（同じ Recall.ai のアカウントを使うクライアントは、どれも合う）。"""
    verified: list[ClientConfig] = []
    msg_id = ""
    for client in rt.registry.all():
        if client.recall is None:
            continue
        try:
            secret = await rt.secrets.get(client.recall.webhook_secret)
        except ConfigError:
            continue
        try:
            msg_id = verify(secret, headers, body)
        except SignatureError:
            continue
        verified.append(client)
    if not verified:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "署名が不正です")
    return verified, msg_id


def _route(verified: list[ClientConfig], claimed: str | None) -> ClientConfig | None:
    """通知の metadata の client_id から、処理するクライアントを決める（決まらなければ None）。"""
    if claimed:
        return next((c for c in verified if c.client_id == claimed), None)
    return verified[0] if len(verified) == 1 else None


@router.post("/recall")
async def recall_webhook(request: Request, rt: RuntimeDep) -> dict[str, object]:
    body = await request.body()
    if len(body) > MAX_BODY_BYTES:
        raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "本文が大きすぎます")
    headers = {k.lower(): v for k, v in request.headers.items()}
    verified, msg_id = await _verified_clients(rt, headers, body)
    try:
        payload = json.loads(body)
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "JSON ではありません") from exc
    event = payload.get("event") if isinstance(payload, dict) else None
    claimed = parse_event(payload).metadata_value("client_id") if isinstance(payload, dict) else None
    client = _route(verified, claimed)
    if not isinstance(event, str) or not event.startswith(HANDLED_EVENTS):
        log_event(
            logger,
            "webhook.ignored",
            client_id=client.client_id if client else None,
            event=str(event),
        )
        return {"ok": True, "queued": False}
    if client is None:
        # 署名は合っているので 200 を返す（4xx だと Recall.ai が送り直し続ける）。処理はしない
        log_event(
            logger,
            "webhook.client_unresolved",
            logging.WARNING,
            event=event,
            claimed_client_id=claimed,
            verified_clients=sorted(c.client_id for c in verified),
        )
        return {"ok": True, "queued": False}
    queued = await rt.tasks.enqueue(
        RECALL_EVENT_PATH,
        {"client_id": client.client_id, "webhook_id": msg_id, "payload": payload},
        name=task_name("wh", client.client_id, msg_id),
    )
    log_event(logger, "webhook.received", client_id=client.client_id, event=event, duplicate=not queued)
    return {"ok": True, "queued": queued}
