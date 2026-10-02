"""ルーター共通の依存（認証）。"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import time
from typing import Annotated

from fastapi import Depends, Header, HTTPException, Path, Request, Response, status

from app.clients import ClientConfig
from app.errors import AppError, ConfigError
from app.logs import log_event
from app.recording_token import RecordingToken, TokenError, verify
from app.runtime import Runtime, get_runtime
from app.web_session import (
    SESSION_COOKIE,
    AppSession,
    SessionError,
    issue_session,
    peek_client_id,
    verify_session,
)

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
# ログインは長く（既定90日）続くので、この間隔で CRM の有効なユーザーかを確かめ直す（退職・無効化に追いつく）
SESSION_RECHECK_SECONDS = 12 * 3600


async def open_app_session(rt: Runtime, token: str) -> AppSession:
    """Cookie の値を確かめる。どのクライアントの鍵で確かめるかは、本文のクライアント ID で決める。"""
    client_id = peek_client_id(token)
    try:
        cs = rt.client_services(client_id)
        secret = await cs.session_secret()
    except ConfigError as exc:
        raise SessionError("クライアントが無効です") from exc
    return verify_session(secret, token, now=time.time())


def cleared_session_cookie() -> str:
    """ログインの Cookie を消す Set-Cookie ヘッダー（/auth/logout と同じ属性）。"""
    tmp = Response()
    tmp.delete_cookie(SESSION_COOKIE, path="/", secure=True, httponly=True, samesite="lax")
    return tmp.headers["set-cookie"]


async def recheck_app_user(rt: Runtime, session: AppSession, response: Response, now: int) -> AppSession:
    """CRM の有効なユーザーかを確かめ直す。無効なら Cookie を消して 401。確かめた時刻を Cookie に入れ直す。"""
    cs = rt.client_services(session.client_id)
    try:
        active = await cs.crm_user_active(session.user_id)
    except AppError as exc:
        # Zoho に届かないときは止めない（前回までは有効だった人）。次の操作でもう一度確かめる
        log_event(
            logger,
            "app.user_recheck_failed",
            logging.WARNING,
            client_id=session.client_id,
            user_id=session.user_id,
            error_code=exc.code,
        )
        return session
    if not active:
        log_event(
            logger, "app.user_inactive", logging.WARNING, client_id=session.client_id, user_id=session.user_id
        )
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            "CRM のユーザーが有効ではありません",
            headers={"set-cookie": cleared_session_cookie()},
        )
    session = dataclasses.replace(session, checked_at=now)
    response.set_cookie(
        SESSION_COOKIE,
        issue_session(await cs.session_secret(), session),
        max_age=max(session.expires_at - now, 0),
        path="/",
        secure=True,
        httponly=True,
        samesite="lax",
    )
    return session


async def app_user(rt: RuntimeDep, request: Request, response: Response) -> AppSession:
    if request.method not in ("GET", "HEAD") and request.headers.get(APP_REQUEST_HEADER) != APP_REQUEST_VALUE:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "このページからの操作ではありません")
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "ログインしてください")
    try:
        session = await open_app_session(rt, token)
    except SessionError as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "ログインし直してください") from exc
    now = int(time.time())
    if now - session.checked_at >= SESSION_RECHECK_SECONDS:
        session = await recheck_app_user(rt, session, response, now)
    return session


AppUserDep = Annotated[AppSession, Depends(app_user)]
