"""Zoho OAuth（リフレッシュトークン方式）。

- トークン発行先はクライアント設定の DC による（既定 US: https://accounts.zoho.com/oauth/v2/token）。
- アクセストークンはプロセス内にキャッシュする。Zoho は1つのリフレッシュトークンにつき
  10分で10回まで・同時に有効なアクセストークン15個までという上限があるため、期限の少し前まで使い回す。
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable

import httpx

from app.errors import ExternalServiceError
from app.logs import log_event
from app.services.http import send

logger = logging.getLogger(__name__)

# 期限のこの秒数前に更新する
REFRESH_MARGIN_SECONDS = 300


class ZohoAuth:
    def __init__(
        self,
        http: httpx.AsyncClient,
        *,
        accounts_url: str,
        client_id: str,
        client_secret: str,
        refresh_token: str,
        clock: Callable[[], float] = time.monotonic,
        retry_base_delay: float = 1.0,
    ) -> None:
        self._http = http
        self._url = f"{accounts_url.rstrip('/')}/oauth/v2/token"
        self._client_id = client_id
        self._client_secret = client_secret
        self._refresh_token = refresh_token
        self._clock = clock
        self._retry_base_delay = retry_base_delay
        self._token: str | None = None
        self._expires_at = 0.0
        self._lock = asyncio.Lock()

    def invalidate(self) -> None:
        self._token = None
        self._expires_at = 0.0

    async def token(self) -> str:
        if self._token and self._clock() < self._expires_at:
            return self._token
        async with self._lock:
            if self._token and self._clock() < self._expires_at:
                return self._token
            return await self._refresh()

    async def _refresh(self) -> str:
        resp = await send(
            self._http,
            "POST",
            self._url,
            service="zoho_auth",
            base_delay=self._retry_base_delay,
            data={
                "grant_type": "refresh_token",
                "refresh_token": self._refresh_token,
                "client_id": self._client_id,
                "client_secret": self._client_secret,
            },
        )
        try:
            body = resp.json()
        except ValueError:
            body = {}
        token = body.get("access_token") if isinstance(body, dict) else None
        if resp.status_code != 200 or not token:
            error = body.get("error") if isinstance(body, dict) else None
            # 短時間の発行回数超過（Access Denied など）は時間をおけば直るので再試行扱いにする
            raise ExternalServiceError(
                "zoho_auth",
                f"アクセストークンを取得できません（{error or resp.status_code}）",
                status=resp.status_code,
                code=str(error) if error else None,
                retryable=resp.status_code >= 500 or error in (None, "Access Denied"),
            )
        expires_in = int(body.get("expires_in", 3600))
        self._token = token
        self._expires_at = self._clock() + max(60, expires_in - REFRESH_MARGIN_SECONDS)
        log_event(logger, "zoho_auth.refreshed", expires_in=expires_in)
        return token
