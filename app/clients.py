"""クライアント（テナント）単位の設定。

CLIENTS_CONFIG（JSON ファイルのパス）か CLIENTS_CONFIG_JSON（JSON 文字列）から読む。
例は config/clients.example.json。秘密情報は JSON に直接書かず、参照で指定する。
  - "sm:projects/<p>/secrets/<name>/versions/latest"                     … Secret Manager
  - "sm:projects/<p>/locations/<region>/secrets/<name>/versions/latest"  … リージョン シークレット
  - "env:VAR_NAME"                                                       … 環境変数（ローカル・テスト用）
"""

from __future__ import annotations

import asyncio
import hmac
import json
import os
import re
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.errors import ConfigError
from app.field_map import FieldMap, build_field_map
from app.recording_token import CLIENT_ID_RE


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def _check_ref(value: str | None) -> str | None:
    if value is None:
        return None
    if not (value.startswith("sm:") or value.startswith("env:")):
        raise ValueError(
            "秘密情報は 'sm:<Secret Manager のリソース名>' か 'env:<環境変数名>' で指定してください"
        )
    return value


# Zoho のデータセンター（DC）ごとの接続先
ZOHO_DATA_CENTERS: dict[str, tuple[str, str]] = {
    "us": ("https://accounts.zoho.com", "https://www.zohoapis.com"),
    "jp": ("https://accounts.zoho.jp", "https://www.zohoapis.jp"),
    "eu": ("https://accounts.zoho.eu", "https://www.zohoapis.eu"),
    "in": ("https://accounts.zoho.in", "https://www.zohoapis.in"),
    "au": ("https://accounts.zoho.com.au", "https://www.zohoapis.com.au"),
}


class ZohoConfig(_Model):
    # 既定は US（デモ環境のお客様 CRM が US DC）。クライアントごとに "dc" で切り替える
    dc: Literal["us", "jp", "eu", "in", "au"] = "us"
    # DC の既定の接続先を上書きするとき（通常は指定しない）
    accounts_url: str | None = None
    api_domain: str | None = None
    client_id: str
    client_secret: str
    refresh_token: str

    @property
    def accounts_base(self) -> str:
        return (self.accounts_url or ZOHO_DATA_CENTERS[self.dc][0]).rstrip("/")

    @property
    def api_base(self) -> str:
        return (self.api_domain or ZOHO_DATA_CENTERS[self.dc][1]).rstrip("/")

    @field_validator("client_id", "client_secret", "refresh_token")
    @classmethod
    def check_secret_refs(cls, value: str) -> str | None:
        return _check_ref(value)


class RecallConfig(_Model):
    base_url: str = "https://ap-northeast-1.recall.ai"
    api_key: str
    webhook_secret: str
    # ボットの表示名（参加者に録音中と分かる名前にする）
    bot_name: str | None = None
    # async: 会議後に create_transcript で文字起こしを依頼する
    # realtime: 作成時の recording_config で会議中に文字起こしする
    transcription_mode: Literal["async", "realtime"] = "async"
    # create_transcript に渡す本文（async のとき）
    transcript_request: dict[str, Any] = Field(default_factory=dict)
    # ボット作成時の recording_config（空なら Recall の既定）
    bot_recording_config: dict[str, Any] = Field(default_factory=dict)
    # デスクトップ SDK のアップロード作成時の recording_config
    sdk_upload_recording_config: dict[str, Any] = Field(default_factory=dict)
    # 開始時刻の何秒前にボットを入室させるか
    join_lead_seconds: int = 60

    @field_validator("api_key", "webhook_secret")
    @classmethod
    def check_secret_refs(cls, value: str) -> str | None:
        return _check_ref(value)


class ClientConfig(_Model):
    client_id: str
    display_name: str = ""
    # CRM ワークフロー・デスクトップアプリから呼ぶときの X-API-Key
    api_key: str | None = None
    timezone: str = "Asia/Tokyo"
    # 自社のメールドメイン（取引先候補の検索から除外する）
    own_email_domains: tuple[str, ...] = ()
    zoho: ZohoConfig | None = None
    recall: RecallConfig | None = None
    field_map_overrides: dict[str, Any] = Field(default_factory=dict, alias="field_map")

    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

    @field_validator("api_key")
    @classmethod
    def check_secret_refs(cls, value: str | None) -> str | None:
        return _check_ref(value)

    @field_validator("client_id")
    @classmethod
    def check_client_id(cls, value: str) -> str:
        if not CLIENT_ID_RE.match(value):
            raise ValueError("client_id は英小文字・数字・ハイフン（32文字まで）にしてください")
        return value

    @property
    def field_map(self) -> FieldMap:
        return build_field_map(self.field_map_overrides)

    def need_zoho(self) -> ZohoConfig:
        if self.zoho is None:
            raise ConfigError(f"クライアント {self.client_id} に zoho の設定がありません")
        return self.zoho

    def need_recall(self) -> RecallConfig:
        if self.recall is None:
            raise ConfigError(f"クライアント {self.client_id} に recall の設定がありません")
        return self.recall


_REGIONAL_SECRET_RE = re.compile(r"^projects/[^/]+/locations/(?P<location>[a-z0-9-]+)/secrets/")


class SecretResolver:
    """秘密情報の参照を解決してプロセス内にキャッシュする。"""

    def __init__(self) -> None:
        self._cache: dict[str, str] = {}
        self._lock = asyncio.Lock()
        self._sm_clients: dict[str | None, Any] = {}

    async def get(self, ref: str) -> str:
        if ref in self._cache:
            return self._cache[ref]
        async with self._lock:
            if ref not in self._cache:
                self._cache[ref] = await self._resolve(ref)
        return self._cache[ref]

    async def _resolve(self, ref: str) -> str:
        if ref.startswith("env:"):
            name = ref[4:]
            value = os.environ.get(name)
            if not value:
                raise ConfigError(f"環境変数 {name} が設定されていません")
            return value
        if ref.startswith("sm:"):
            return await self._access_secret(ref[3:])
        raise ConfigError("秘密情報の参照形式が不正です")

    def _client_for(self, name: str) -> Any:
        from google.cloud import secretmanager

        m = _REGIONAL_SECRET_RE.match(name)
        location = m["location"] if m else None
        if location not in self._sm_clients:
            # リージョン シークレットはリージョンのエンドポイントからしか読めない
            options = {"api_endpoint": f"secretmanager.{location}.rep.googleapis.com"} if location else None
            self._sm_clients[location] = secretmanager.SecretManagerServiceAsyncClient(client_options=options)
        return self._sm_clients[location]

    async def _access_secret(self, name: str) -> str:
        client = self._client_for(name)
        try:
            response = await client.access_secret_version(name=name)
        except Exception as exc:  # Secret Manager の例外は種類が多いので設定エラーとしてまとめる
            raise ConfigError(f"Secret Manager から取得できません: {name.split('/versions/')[0]}") from exc
        return response.payload.data.decode("utf-8").strip()


class ClientRegistry:
    def __init__(self, clients: dict[str, ClientConfig]) -> None:
        if not clients:
            raise ConfigError("クライアント設定が1件もありません")
        self._clients = clients

    @classmethod
    def load(cls, path: str | None = None, json_text: str | None = None) -> ClientRegistry:
        """CLIENTS_CONFIG（ファイルのパス）か CLIENTS_CONFIG_JSON（JSON 文字列）から読む。"""
        if json_text:
            return cls.from_dict(json.loads(json_text))
        if not path:
            raise ConfigError(
                "環境変数 CLIENTS_CONFIG か CLIENTS_CONFIG_JSON（クライアント設定）が設定されていません"
            )
        file = Path(path)
        if not file.is_file():
            raise ConfigError(f"クライアント設定ファイルがありません: {path}")
        return cls.from_dict(json.loads(file.read_text(encoding="utf-8")))

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ClientRegistry:
        raw = data.get("clients")
        if not isinstance(raw, dict):
            raise ConfigError('クライアント設定は {"clients": {"<client_id>": {...}}} の形にしてください')
        clients = {cid: ClientConfig.model_validate({**conf, "client_id": cid}) for cid, conf in raw.items()}
        return cls(clients)

    def get(self, client_id: str) -> ClientConfig:
        try:
            return self._clients[client_id]
        except KeyError:
            raise ConfigError(f"クライアント {client_id} は設定されていません") from None

    def all(self) -> list[ClientConfig]:
        return list(self._clients.values())

    async def find_by_api_key(self, api_key: str, secrets: SecretResolver) -> ClientConfig | None:
        """X-API-Key から呼び出し元のクライアントを特定する（定数時間比較）。"""
        found: ClientConfig | None = None
        for client in self._clients.values():
            if not client.api_key:
                continue
            expected = await secrets.get(client.api_key)
            if hmac.compare_digest(expected.encode(), api_key.encode()):
                found = client
        return found
