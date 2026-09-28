"""クライアント（テナント）ごとの外部サービス（CRM・Recall.ai）。

秘密情報は使う時点で Secret Manager から読み、プロセス内で使い回す。
設定の無いサービスを使おうとしたときは ConfigError になる。
"""

from __future__ import annotations

import asyncio

import httpx

from app.clients import ClientConfig, SecretResolver
from app.config import Settings
from app.field_map import FieldMap
from app.services.crm import CrmService
from app.services.recall import RecallService
from app.services.zoho_auth import ZohoAuth


class ClientServices:
    def __init__(
        self,
        config: ClientConfig,
        settings: Settings,
        secrets: SecretResolver,
        http: httpx.AsyncClient,
        *,
        crm: CrmService | None = None,
        recall: RecallService | None = None,
    ) -> None:
        self.config = config
        self.field_map: FieldMap = config.field_map
        self._settings = settings
        self._secrets = secrets
        self._http = http
        self._crm = crm
        self._recall = recall
        self._lock = asyncio.Lock()

    @property
    def client_id(self) -> str:
        return self.config.client_id

    async def crm(self) -> CrmService:
        if self._crm is None:
            async with self._lock:
                if self._crm is None:
                    zoho = self.config.need_zoho()
                    auth = ZohoAuth(
                        self._http,
                        accounts_url=zoho.accounts_base,
                        client_id=await self._secrets.get(zoho.client_id),
                        client_secret=await self._secrets.get(zoho.client_secret),
                        refresh_token=await self._secrets.get(zoho.refresh_token),
                    )
                    self._crm = CrmService(
                        self._http,
                        auth,
                        api_domain=zoho.api_base,
                        field_map=self.field_map,
                        dry_run=self._settings.dry_run,
                        test_records=self._settings.crm_test_records,
                    )
        return self._crm

    async def recall(self) -> RecallService:
        if self._recall is None:
            async with self._lock:
                if self._recall is None:
                    cfg = self.config.need_recall()
                    self._recall = RecallService(
                        self._http,
                        base_url=cfg.base_url,
                        api_key=await self._secrets.get(cfg.api_key),
                        dry_run=self._settings.dry_run,
                    )
        return self._recall
