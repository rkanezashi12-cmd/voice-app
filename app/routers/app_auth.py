"""録音アプリ（/app/）の「Zoho でログイン」。

- GET  /auth/login?c=<client_id>  … Zoho のログイン画面へ（state を Cookie の nonce と結び付ける）
- GET  /auth/callback              … Zoho から戻る。CRM の有効なユーザーで、組織が同じなら Cookie を発行して /app/ へ
- POST /auth/logout                … Cookie を消す

失敗したときは /app/?login_error=<種類> に戻し、画面で理由を出す
（denied / expired / not_crm_user / no_api_access / inactive / failed）。
"""

from __future__ import annotations

import logging
import secrets
import time
from typing import Annotated
from urllib.parse import urlencode

from fastapi import APIRouter, HTTPException, Query, Request, Response, status
from fastapi.responses import RedirectResponse

from app.deps import RuntimeDep
from app.errors import AppError, ConfigError
from app.logs import log_event
from app.recording_token import CLIENT_ID_RE
from app.services.zoho_login import ZohoLoginError
from app.web_session import (
    SESSION_COOKIE,
    STATE_COOKIE,
    AppSession,
    SessionError,
    issue_session,
    issue_state,
    peek_client_id,
    verify_state,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/auth", tags=["app-auth"])

STATE_TTL_SECONDS = 600


def app_url(client_id: str, **params: str) -> str:
    return f"/app/?{urlencode({'c': client_id, **params})}"


def _cookie(resp: Response, name: str, value: str, *, max_age: int, path: str) -> None:
    resp.set_cookie(name, value, max_age=max_age, path=path, secure=True, httponly=True, samesite="lax")


def _fail(client_id: str, reason: str) -> RedirectResponse:
    resp = RedirectResponse(app_url(client_id, login_error=reason), status_code=status.HTTP_302_FOUND)
    resp.delete_cookie(STATE_COOKIE, path="/auth", secure=True, httponly=True, samesite="lax")
    return resp


@router.get("/login")
async def login(rt: RuntimeDep, c: Annotated[str, Query(max_length=32)] = "default") -> RedirectResponse:
    if not CLIENT_ID_RE.match(c):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "クライアントが見つかりません")
    try:
        cs = rt.client_services(c)
        zoho_login = await cs.zoho_login()
        secret = await cs.session_secret()
    except ConfigError as exc:
        log_event(logger, "app.login_unavailable", logging.WARNING, client_id=c, error=exc.message)
        raise HTTPException(status.HTTP_404_NOT_FOUND, "このクライアントでは録音アプリを使えません") from exc
    nonce = secrets.token_urlsafe(24)
    state = issue_state(secret, client_id=c, nonce=nonce, expires_at=int(time.time()) + STATE_TTL_SECONDS)
    resp = RedirectResponse(zoho_login.authorize_url(state), status_code=status.HTTP_302_FOUND)
    _cookie(resp, STATE_COOKIE, nonce, max_age=STATE_TTL_SECONDS, path="/auth")
    return resp


@router.get("/callback")
async def callback(
    request: Request,
    rt: RuntimeDep,
    code: Annotated[str | None, Query(max_length=2048)] = None,
    state: Annotated[str | None, Query(max_length=2048)] = None,
    error: Annotated[str | None, Query(max_length=200)] = None,
    accounts_server: Annotated[str | None, Query(alias="accounts-server", max_length=200)] = None,
) -> RedirectResponse:
    client_id = "default"
    try:
        client_id = peek_client_id(state or "")
        cs = rt.client_services(client_id)
        secret = await cs.session_secret()
        verify_state(secret, state or "", nonce=request.cookies.get(STATE_COOKIE, ""), now=time.time())
    except (SessionError, ConfigError):
        # 期限切れ（ログイン画面で10分以上たった）・別のブラウザで開いた・書き換えられた、など
        log_event(logger, "app.login_rejected", logging.WARNING, reason="expired")
        return _fail(client_id, "expired")
    if error or not code:
        log_event(logger, "app.login_rejected", logging.WARNING, client_id=client_id, reason="denied")
        return _fail(client_id, "denied")
    zoho = cs.config.need_zoho()
    if accounts_server and accounts_server.rstrip("/") != zoho.accounts_base:
        # 別のデータセンターの Zoho アカウント（この CRM のユーザーではない）
        log_event(logger, "app.login_rejected", logging.WARNING, client_id=client_id, reason="other_dc")
        return _fail(client_id, "not_crm_user")
    try:
        zoho_login = await cs.zoho_login()
        user = await zoho_login.current_user(await zoho_login.exchange(code))
        if user.org_id != await cs.org_id():
            raise ZohoLoginError("この CRM のユーザーではありません", reason="not_crm_user")
        if user.status.lower() != "active":
            raise ZohoLoginError("CRM のユーザーが有効ではありません", reason="inactive")
    except ZohoLoginError as exc:
        log_event(logger, "app.login_rejected", logging.WARNING, client_id=client_id, reason=exc.reason)
        return _fail(client_id, exc.reason)
    except AppError as exc:
        log_event(logger, "app.login_failed", logging.ERROR, client_id=client_id, error_code=exc.code)
        return _fail(client_id, "failed")
    hours = cs.config.need_app().session_hours
    session = AppSession(
        client_id=client_id,
        user_id=user.user_id,
        name=user.name,
        email=user.email,
        expires_at=int(time.time()) + hours * 3600,
    )
    resp = RedirectResponse(app_url(client_id), status_code=status.HTTP_302_FOUND)
    _cookie(resp, SESSION_COOKIE, issue_session(secret, session), max_age=hours * 3600, path="/")
    resp.delete_cookie(STATE_COOKIE, path="/auth", secure=True, httponly=True, samesite="lax")
    log_event(logger, "app.login", client_id=client_id, user_id=user.user_id)
    return resp


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout() -> Response:
    resp = Response(status_code=status.HTTP_204_NO_CONTENT)
    resp.delete_cookie(SESSION_COOKIE, path="/", secure=True, httponly=True, samesite="lax")
    return resp
