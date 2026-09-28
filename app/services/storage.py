"""GCS（対面録音の一時保存）。

- 録音ページからは署名付き URL（V4 / PUT）で直接アップロードさせる。
  Cloud Run には鍵ファイルが無いので、IAM の signBlob で署名する（実行 SA に自分自身の
  roles/iam.serviceAccountTokenCreator が必要。README 参照）。
- 削除は DRY_RUN のとき行わない（ログのみ）。バケットのライフサイクル（1日で削除）も併用する。
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any

from app.logs import log_event

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class StoredObject:
    name: str
    size: int
    content_type: str | None


class StorageService:
    def __init__(self, bucket: str, *, dry_run: bool, signing_service_account: str | None = None) -> None:
        self.bucket_name = bucket
        self.dry_run = dry_run
        self._signing_sa = signing_service_account
        self._client: Any = None
        self._credentials: Any = None

    def _bucket(self) -> Any:
        from google.cloud import storage

        if self._client is None:
            self._client = storage.Client()
        return self._client.bucket(self.bucket_name)

    def _signing_credentials(self) -> tuple[str, str]:
        import google.auth
        from google.auth.transport.requests import Request

        if self._credentials is None:
            self._credentials, _ = google.auth.default(
                scopes=["https://www.googleapis.com/auth/cloud-platform"]
            )
        creds = self._credentials
        if not creds.valid:
            creds.refresh(Request())
        email = self._signing_sa or getattr(creds, "service_account_email", None)
        if not email or email == "default":
            creds.refresh(Request())
            email = self._signing_sa or creds.service_account_email
        return email, creds.token

    def _sign_put(self, object_name: str, content_type: str, ttl_seconds: int) -> str:
        email, token = self._signing_credentials()
        blob = self._bucket().blob(object_name)
        return blob.generate_signed_url(
            version="v4",
            expiration=timedelta(seconds=ttl_seconds),
            method="PUT",
            content_type=content_type,
            service_account_email=email,
            access_token=token,
        )

    async def upload_url(self, object_name: str, content_type: str, ttl_seconds: int) -> str:
        return await asyncio.to_thread(self._sign_put, object_name, content_type, ttl_seconds)

    def _list(self, prefix: str) -> list[StoredObject]:
        blobs = self._bucket().client.list_blobs(self.bucket_name, prefix=prefix)
        return [StoredObject(b.name, int(b.size or 0), b.content_type) for b in blobs]

    async def list(self, prefix: str) -> list[StoredObject]:
        return await asyncio.to_thread(self._list, prefix)

    async def download(self, object_name: str, dest: Path) -> None:
        await asyncio.to_thread(self._bucket().blob(object_name).download_to_filename, str(dest))

    def _delete_prefix(self, prefix: str) -> int:
        count = 0
        for obj in self._list(prefix):
            self._bucket().blob(obj.name).delete()
            count += 1
        return count

    async def delete_prefix(self, prefix: str) -> int:
        if self.dry_run:
            log_event(logger, "dry_run.skip", operation="gcs.delete_prefix", prefix=prefix)
            return 0
        count = await asyncio.to_thread(self._delete_prefix, prefix)
        log_event(logger, "gcs.deleted", prefix=prefix, objects=count)
        return count
