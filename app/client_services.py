"""クライアント（テナント）ごとの外部サービス（CRM・Recall.ai）。

秘密情報は使う時点で Secret Manager から読み、プロセス内で使い回す。
設定の無いサービスを使おうとしたときは ConfigError になる。
"""

from __future__ import annotations

import asyncio

import httpx

from app.clients import ClientConfig, SecretResolver
from app.config import Settings
from app.errors import ConfigError
from app.field_map import FieldMap
from app.services.crm import CrmService
from app.services.geocoding import Geocoder
from app.services.recall import RecallService
from app.services.zoho_auth import ZohoAuth
from app.services.zoho_login import ZohoLogin


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
        login: ZohoLogin | None = None,
        geocoder: Geocoder | None = None,
    ) -> None:
        self.config = config
        self.field_map: FieldMap = config.field_map
        self._settings = settings
        self._secrets = secrets
        self._http = http
        self._crm = crm
        self._recall = recall
        self._login = login
        self._geocoder = geocoder
        self._org_id: str | None = None
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

    # ---- 録音アプリ（/app/） ----

    async def session_secret(self) -> bytes:
        return (await self._secrets.get(self.config.need_app().session_secret)).encode()

    async def zoho_login(self) -> ZohoLogin:
        if self._login is None:
            async with self._lock:
                if self._login is None:
                    app = self.config.need_app()
                    zoho = self.config.need_zoho()
                    self._login = ZohoLogin(
                        self._http,
                        accounts_base=zoho.accounts_base,
                        api_base=zoho.api_base,
                        client_id=await self._secrets.get(app.login_client_id),
                        client_secret=await self._secrets.get(app.login_client_secret),
                        redirect_uri=f"{self._settings.need('service_url')}/auth/callback",
                    )
        return self._login

    async def geocoder(self) -> Geocoder | None:
        """GPS の候補に使う。maps_api_key が無ければ None（GPS の候補は使えない）。"""
        if self._geocoder is None:
            key_ref = self.config.need_app().maps_api_key
            if not key_ref:
                return None
            async with self._lock:
                if self._geocoder is None:
                    self._geocoder = Geocoder(self._http, api_key=await self._secrets.get(key_ref))
        return self._geocoder

    async def org_id(self) -> str:
        """バックエンドが接続している CRM の組織 ID（ログインした人の組織と突き合わせる）。"""
        if self._org_id is None:
            org = await (await self.crm()).get_org()
            org_id = str(org.get("id") or "")
            if not org_id:
                raise ConfigError("接続先の CRM の組織 ID を読めませんでした")
            self._org_id = org_id
        return self._org_id
