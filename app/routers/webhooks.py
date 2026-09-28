"""Recall.ai の Webhook 受信。

署名（Svix 形式）を検証し、処理は Cloud Tasks に積んですぐ 200 を返す。
同じ webhook-id の通知はタスク名が同じになるので、Cloud Tasks が二重登録を拒否する。
どのクライアントの通知かは、署名が一致したシークレットで判定する。
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
from app.services.tasks import task_name
from app.webhook_signature import SignatureError, verify

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/webhooks", tags=["webhooks"])

MAX_BODY_BYTES = 1_000_000
RECALL_EVENT_PATH = "/internal/recall-event"
HANDLED_EVENTS = ("bot.", "sdk_upload.complete", "sdk_upload.failed")


async def _identify(rt: Runtime, headers: dict[str, str], body: bytes) -> tuple[ClientConfig, str]:
    for client in rt.registry.all():
        if client.recall is None:
            continue
        try:
            secret = await rt.secrets.get(client.recall.webhook_secret)
        except ConfigError:
            continue
        try:
            return client, verify(secret, headers, body)
        except SignatureError:
            continue
    raise HTTPException(status.HTTP_401_UNAUTHORIZED, "署名が不正です")


@router.post("/recall")
async def recall_webhook(request: Request, rt: RuntimeDep) -> dict[str, object]:
    body = await request.body()
    if len(body) > MAX_BODY_BYTES:
        raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "本文が大きすぎます")
    headers = {k.lower(): v for k, v in request.headers.items()}
    client, msg_id = await _identify(rt, headers, body)
    try:
        payload = json.loads(body)
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "JSON ではありません") from exc
    event = payload.get("event") if isinstance(payload, dict) else None
    if not isinstance(event, str) or not event.startswith(HANDLED_EVENTS):
        log_event(logger, "webhook.ignored", client_id=client.client_id, event=str(event))
        return {"ok": True, "queued": False}
    queued = await rt.tasks.enqueue(
        RECALL_EVENT_PATH,
        {"client_id": client.client_id, "webhook_id": msg_id, "payload": payload},
        name=task_name("wh", client.client_id, msg_id),
    )
    log_event(logger, "webhook.received", client_id=client.client_id, event=event, duplicate=not queued)
    return {"ok": True, "queued": queued}
