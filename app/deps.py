"""ルーター共通の依存（認証）。"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Annotated

from fastapi import Depends, Header, HTTPException, Path, Request, status

from app.clients import ClientConfig
from app.errors import ConfigError
from app.logs import log_event
from app.recording_token import RecordingToken, TokenError, verify
from app.runtime import Runtime, get_runtime
from app.web_session import SESSION_COOKIE, AppSession, SessionError, peek_client_id, verify_session

logger = logging.getLogger(__name__)

RuntimeDep = Annotated[Runtime, Depends(get_runtime)]


async def api_client(
    rt: RuntimeDep,
    x_api_key: Annotated[str | None, Header(alias="X-API-Key")] = None,
) -> ClientConfig:
    """CRM ワークフロー・デスクトップアプリからの呼び出し。X-API-Key でクライアントを特定する。"""
    if not x_api_key or len(x_api_key) > 256:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "X-API-Key が必要です")
    client = await rt.registry.find_by_api_key(x_api_key, rt.secrets)
    if client is None:
        log_event(logger, "auth.api_key_rejected", logging.WARNING)
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "X-API-Key が不正です")
    return client


ApiClientDep = Annotated[ClientConfig, Depends(api_client)]


def _bearer(authorization: str | None) -> str:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "認証情報がありません")
    return authorization[7:].strip()


async def recording_token(
    rt: RuntimeDep,
    record_id: Annotated[str, Path()],
    authorization: Annotated[str | None, Header()] = None,
) -> RecordingToken:
    """録音ページからの呼び出し。URL フラグメントのトークンを Bearer で受け取る。"""
    token = _bearer(authorization)
    secret = await rt.recording_secret()
    try:
        claims = verify(secret, token)
    except TokenError as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, str(exc)) from exc
    if claims.record_id != record_id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "この記録の操作は許可されていません")
    try:
        rt.registry.get(claims.client_id)
    except ConfigError as exc:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "クライアントが無効です") from exc
    return claims


RecordingTokenDep = Annotated[RecordingToken, Depends(recording_token)]


async def tasks_oidc(
    rt: RuntimeDep,
    authorization: Annotated[str | None, Header()] = None,
) -> None:
    """/internal/* は Cloud Tasks の OIDC トークンだけを受け付ける。"""
    token = _bearer(authorization)
    audience = rt.settings.need("service_url")
    invoker = rt.settings.need("tasks_invoker_sa")

    def _verify() -> dict:
        from google.auth.transport import requests as google_requests
        from google.oauth2 import id_token

        return id_token.verify_oauth2_token(token, google_requests.Request(), audience=audience)

    try:
        claims = await asyncio.to_thread(_verify)
    except ValueError as exc:
        log_event(logger, "auth.oidc_rejected", logging.WARNING, reason="invalid_token")
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "OIDC トークンが不正です") from exc
    if claims.get("email") != invoker or not claims.get("email_verified"):
        log_event(logger, "auth.oidc_rejected", logging.WARNING, reason="unexpected_email")
        raise HTTPException(status.HTTP_403_FORBIDDEN, "呼び出し元が許可されていません")


TasksOidcDep = Depends(tasks_oidc)


# ---- 録音アプリ（/app/）：Zoho でログインした営業 ----

# 書き込み（POST など）に付けてもらうヘッダー。別のサイトのページからは付けられない（CORS を許可していない）ので、
# Cookie を使ったなりすましの送信（CSRF）を防げる。SameSite=Lax の Cookie と二重の守り
APP_REQUEST_HEADER = "X-Requested-With"
APP_REQUEST_VALUE = "meeting-notes-app"


async def open_app_session(rt: Runtime, token: str) -> AppSession:
    """Cookie の値を確かめる。どのクライアントの鍵で確かめるかは、本文のクライアント ID で決める。"""
    client_id = peek_client_id(token)
    try:
        cs = rt.client_services(client_id)
        secret = await cs.session_secret()
    except ConfigError as exc:
        raise SessionError("クライアントが無効です") from exc
    return verify_session(secret, token, now=time.time())


async def app_user(rt: RuntimeDep, request: Request) -> AppSession:
    if request.method not in ("GET", "HEAD") and request.headers.get(APP_REQUEST_HEADER) != APP_REQUEST_VALUE:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "このページからの操作ではありません")
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "ログインしてください")
    try:
        return await open_app_session(rt, token)
    except SessionError as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "ログインし直してください") from exc


AppUserDep = Annotated[AppSession, Depends(app_user)]
