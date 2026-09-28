"""Recall.ai（東京リージョン https://ap-northeast-1.recall.ai）。

API キーはリージョンごとに別。認証は "Authorization: Token <key>"。パスは末尾に / が付く。
リクエスト・レスポンスの形で公式ドキュメントを直接確認できていない点は docs/unverified-apis.md に記録している。
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Any

import httpx

from app.errors import ExternalServiceError
from app.logs import log_event
from app.services.http import error_from_response, send

logger = logging.getLogger(__name__)

# 直前に作成したボットは 507（空きボット不足）になることがある。少し待って作り直す
CREATE_BOT_507_RETRIES = 2
CREATE_BOT_507_WAIT_SECONDS = 5.0


class RecallService:
    def __init__(
        self,
        http: httpx.AsyncClient,
        *,
        base_url: str,
        api_key: str,
        dry_run: bool,
        retry_base_delay: float = 1.0,
        wait_507_seconds: float = CREATE_BOT_507_WAIT_SECONDS,
    ) -> None:
        self._http = http
        self._base = f"{base_url.rstrip('/')}/api/v1"
        self._headers = {"Authorization": f"Token {api_key}", "Accept": "application/json"}
        self.dry_run = dry_run
        self._retry_base_delay = retry_base_delay
        self._wait_507 = wait_507_seconds

    async def _request(self, method: str, path: str, *, json: Any = None) -> httpx.Response:
        return await send(
            self._http,
            method,
            f"{self._base}{path}",
            service="recall",
            base_delay=self._retry_base_delay,
            json=json,
            headers=self._headers,
        )

    async def _json(self, method: str, path: str, operation: str, *, json: Any = None) -> dict[str, Any]:
        resp = await self._request(method, path, json=json)
        if resp.status_code >= 400:
            raise error_from_response("recall", resp, f"{operation}に失敗しました")
        body = resp.json() if resp.content else {}
        return body if isinstance(body, dict) else {}

    # ---- ボット（入口A） ----

    async def create_bot(
        self,
        *,
        meeting_url: str,
        bot_name: str,
        join_at: datetime | None,
        metadata: dict[str, str],
        recording_config: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"meeting_url": meeting_url, "bot_name": bot_name, "metadata": metadata}
        if join_at is not None:
            body["join_at"] = join_at.isoformat()
        if recording_config:
            body["recording_config"] = recording_config
        for attempt in range(CREATE_BOT_507_RETRIES + 1):
            resp = await self._request("POST", "/bot/", json=body)
            if resp.status_code == 507 and attempt < CREATE_BOT_507_RETRIES:
                log_event(logger, "recall.bot_507_retry", logging.WARNING, attempt=attempt + 1)
                await asyncio.sleep(self._wait_507)
                continue
            break
        if resp.status_code >= 400:
            raise error_from_response("recall", resp, "ボットの予約に失敗しました")
        bot = resp.json()
        log_event(logger, "recall.bot_created", bot_id=bot.get("id"), scheduled=join_at is not None)
        return bot

    async def get_bot(self, bot_id: str) -> dict[str, Any]:
        return await self._json("GET", f"/bot/{bot_id}/", "ボット情報の取得")

    async def delete_bot_media(self, bot_id: str) -> None:
        if self.dry_run:
            log_event(logger, "dry_run.skip", operation="recall.delete_bot_media", bot_id=bot_id)
            return
        resp = await self._request("POST", f"/bot/{bot_id}/delete_media/")
        if resp.status_code >= 400 and resp.status_code != 404:
            raise error_from_response("recall", resp, "ボットの録画・音声の削除に失敗しました")
        log_event(logger, "recall.media_deleted", bot_id=bot_id)

    # ---- デスクトップ SDK（入口C） ----

    async def create_sdk_upload(
        self, *, recording_config: dict[str, Any] | None, metadata: dict[str, str]
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"metadata": metadata}
        if recording_config:
            body["recording_config"] = recording_config
        upload = await self._json("POST", "/sdk_upload/", "デスクトップ録音の準備", json=body)
        if not upload.get("id") or not upload.get("upload_token"):
            raise ExternalServiceError("recall", "upload_token が返りませんでした")
        log_event(logger, "recall.sdk_upload_created", upload_id=upload.get("id"))
        return upload

    async def get_sdk_upload(self, upload_id: str) -> dict[str, Any]:
        return await self._json("GET", f"/sdk_upload/{upload_id}/", "デスクトップ録音の情報取得")

    # ---- 録音・文字起こし（共通） ----

    async def get_recording(self, recording_id: str) -> dict[str, Any]:
        return await self._json("GET", f"/recording/{recording_id}/", "録音情報の取得")

    async def create_transcript(self, recording_id: str, request: dict[str, Any]) -> dict[str, Any]:
        result = await self._json(
            "POST", f"/recording/{recording_id}/create_transcript/", "文字起こしの依頼", json=request
        )
        log_event(logger, "recall.transcript_requested", recording_id=recording_id)
        return result

    async def delete_recording(self, recording_id: str) -> None:
        if self.dry_run:
            log_event(logger, "dry_run.skip", operation="recall.delete_recording", recording_id=recording_id)
            return
        resp = await self._request("DELETE", f"/recording/{recording_id}/")
        if resp.status_code >= 400 and resp.status_code != 404:
            raise error_from_response("recall", resp, "録音の削除に失敗しました")
        log_event(logger, "recall.recording_deleted", recording_id=recording_id)

    async def download_json(self, url: str) -> Any:
        """文字起こしのダウンロード（署名付き URL なので API キーを付けない）。"""
        resp = await send(
            self._http, "GET", url, service="recall_download", base_delay=self._retry_base_delay
        )
        if resp.status_code >= 400:
            raise error_from_response("recall_download", resp, "文字起こしのダウンロードに失敗しました")
        return resp.json()
