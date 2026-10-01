"""録音アプリの「Zoho でログイン」（OAuth 2.0 の認可コード方式）。

ログインした人が、このクライアントの CRM の有効なユーザーかを確かめるためだけに使う。
- スコープは CRM のユーザーと組織の読み取りだけ（ZohoCRM.users.READ, ZohoCRM.org.READ）
- access_type=online（リフレッシュトークンを受け取らない）。アクセストークンは確認に1回使って捨て、保存しない
- CRM の読み書きはこれまでどおりバックエンドの接続（app/services/crm.py）で行う
リクエストと応答の形で公式ドキュメントを直接確認できていない点は docs/unverified-apis.md に記録している。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

import httpx

from app.errors import AppError
from app.services.http import send

logger = logging.getLogger(__name__)

LOGIN_SCOPES = ("ZohoCRM.users.READ", "ZohoCRM.org.READ")


class ZohoLoginError(AppError):
    """ログインできない（認可コードが無効・CRM のユーザーではない など）。reason は画面に出す種類。"""

    code = "zoho_login_failed"

    def __init__(self, message: str, *, reason: str = "failed") -> None:
        super().__init__(message)
        self.reason = reason


@dataclass(frozen=True)
class ZohoUser:
    user_id: str
    name: str
    email: str
    status: str
    org_id: str


class ZohoLogin:
    def __init__(
        self,
        http: httpx.AsyncClient,
        *,
        accounts_base: str,
        api_base: str,
        client_id: str,
        client_secret: str,
        redirect_uri: str,
        retry_base_delay: float = 1.0,
    ) -> None:
        self._http = http
        self._accounts = accounts_base.rstrip("/")
        self._api = f"{api_base.rstrip('/')}/crm/v8"
        self._client_id = client_id
        self._client_secret = client_secret
        self.redirect_uri = redirect_uri
        self._retry_base_delay = retry_base_delay

    def authorize_url(self, state: str) -> str:
        query = urlencode(
            {
                "scope": ",".join(LOGIN_SCOPES),
                "client_id": self._client_id,
                "response_type": "code",
                "access_type": "online",
                "redirect_uri": self.redirect_uri,
                "state": state,
            }
        )
        return f"{self._accounts}/oauth/v2/auth?{query}"

    async def exchange(self, code: str) -> str:
        """認可コードをアクセストークンに換える。Zoho は失敗しても HTTP 200 で {"error": ...} を返すことがある。"""
        resp = await send(
            self._http,
            "POST",
            f"{self._accounts}/oauth/v2/token",
            service="zoho_login",
            base_delay=self._retry_base_delay,
            data={
                "grant_type": "authorization_code",
                "client_id": self._client_id,
                "client_secret": self._client_secret,
                "redirect_uri": self.redirect_uri,
                "code": code,
            },
        )
        body = _json(resp)
        token = body.get("access_token")
        if resp.status_code != 200 or not isinstance(token, str) or not token:
            error = body.get("error")
            raise ZohoLoginError(f"ログインを確かめられませんでした（{error or resp.status_code}）")
        return token

    async def current_user(self, access_token: str) -> ZohoUser:
        """ログインした人の CRM のユーザー情報と、所属する組織の ID。"""
        headers = {"Authorization": f"Zoho-oauthtoken {access_token}"}
        users = await self._get("/users", headers, params={"type": "CurrentUser"})
        user = _first(users.get("users"))
        org = _first((await self._get("/org", headers)).get("org"))
        if user is None or not str(user.get("id") or "").isdigit():
            raise ZohoLoginError("CRM のユーザー情報を読めませんでした", reason="not_crm_user")
        if org is None or not str(org.get("id") or ""):
            raise ZohoLoginError("CRM の組織の情報を読めませんでした", reason="not_crm_user")
        return ZohoUser(
            user_id=str(user["id"]),
            name=str(user.get("full_name") or ""),
            email=str(user.get("email") or ""),
            status=str(user.get("status") or ""),
            org_id=str(org["id"]),
        )

    async def _get(
        self, path: str, headers: dict[str, str], params: dict[str, str] | None = None
    ) -> dict[str, Any]:
        resp = await send(
            self._http,
            "GET",
            f"{self._api}{path}",
            service="zoho_login",
            base_delay=self._retry_base_delay,
            headers=headers,
            params=params,
        )
        if resp.status_code == 403:
            # CRM のユーザーだが、プロファイルで API の利用（Zoho CRM API Access）が許可されていない
            raise ZohoLoginError("CRM の API の利用が許可されていません", reason="no_api_access")
        if resp.status_code == 401:
            # 別の組織・別の DC のアカウントなど、この CRM を読めないアカウント
            raise ZohoLoginError("この CRM のユーザーではありません", reason="not_crm_user")
        if resp.status_code >= 400 or resp.status_code == 204:
            raise ZohoLoginError(f"CRM のユーザー情報を読めませんでした（{resp.status_code}）")
        return _json(resp)


def _json(resp: httpx.Response) -> dict[str, Any]:
    try:
        body = resp.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}


def _first(value: Any) -> dict[str, Any] | None:
    if isinstance(value, list) and value and isinstance(value[0], dict):
        return value[0]
    return None
