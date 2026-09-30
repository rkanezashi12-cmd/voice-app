"""外部 API 呼び出しの共通処理（リトライ方針）。

429 / 5xx / 通信エラーは指数バックオフで最大3回リトライ。4xx はリトライせずに返す（呼び出し側で例外にする）。
"""

from __future__ import annotations

import asyncio
import logging
import random
import re
from typing import Any

import httpx

from app.errors import ExternalServiceError
from app.logs import log_event

logger = logging.getLogger(__name__)

RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
_URL_RE = re.compile(r"https?://[^\s、。，）」\"'<>]+")
MAX_RETRY_WAIT_SECONDS = 30.0


def _retry_after(resp: httpx.Response) -> float | None:
    value = resp.headers.get("Retry-After")
    if value is None:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        return None


async def send(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    service: str,
    retries: int = 3,
    base_delay: float = 1.0,
    **kwargs: Any,
) -> httpx.Response:
    attempt = 0
    while True:
        try:
            resp = await client.request(method, url, **kwargs)
        except httpx.TransportError as exc:
            if attempt >= retries:
                raise ExternalServiceError(
                    service, f"通信エラー（{type(exc).__name__}）", retryable=True
                ) from exc
            wait = base_delay * (2**attempt)
            log_event(
                logger,
                "http.retry",
                logging.WARNING,
                service=service,
                attempt=attempt + 1,
                reason=type(exc).__name__,
            )
        else:
            if resp.status_code not in RETRY_STATUSES or attempt >= retries:
                return resp
            wait = _retry_after(resp) or base_delay * (2**attempt)
            log_event(
                logger,
                "http.retry",
                logging.WARNING,
                service=service,
                attempt=attempt + 1,
                status=resp.status_code,
            )
        attempt += 1
        await asyncio.sleep(min(wait, MAX_RETRY_WAIT_SECONDS) + random.uniform(0, base_delay / 4))  # noqa: S311


def _upstream_message(item: dict[str, Any]) -> str | None:
    """外部サービスのエラーの説明を拾う。項目ごとのエラー（{"meeting_url": ["..."]}）は項目名と最初の文だけ。

    会議 URL（パスコードを含むことがある）や署名付き URL をログ・CRM に残さないよう、URL は伏せる。
    """
    msg = item.get("message") or item.get("detail") or item.get("error")
    if isinstance(msg, str) and msg:
        return _URL_RE.sub("<URL>", msg)
    parts = [
        f"{key}: {_URL_RE.sub('<URL>', value[0])}"
        for key, value in item.items()
        if isinstance(value, list) and value and isinstance(value[0], str)
    ]
    return "、".join(parts[:2]) or None


def error_from_response(
    service: str, resp: httpx.Response, message: str | None = None
) -> ExternalServiceError:
    """レスポンスから例外を作る。本文は外部サービスのエラーコード・メッセージだけを拾う。

    message（何をしていて失敗したか）に、外部サービスが返した理由を括弧で添える（原因を CRM とログで追えるように）。
    """
    code = None
    detail = message or ""
    try:
        body = resp.json()
    except ValueError:
        body = None
    if isinstance(body, dict):
        item = body
        data = body.get("data")
        if isinstance(data, list) and data and isinstance(data[0], dict):
            item = data[0]
        code = item.get("code") if isinstance(item.get("code"), str) else None
        upstream = _upstream_message(item)
        if upstream:
            detail = f"{detail}（{upstream}）" if detail else upstream
    return ExternalServiceError(
        service,
        (detail or resp.reason_phrase or "エラー")[:300],
        status=resp.status_code,
        code=code,
        retryable=resp.status_code in RETRY_STATUSES,
    )
