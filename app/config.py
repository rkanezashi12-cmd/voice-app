"""プロセス全体の設定（環境変数）。

クライアント（テナント）ごとの設定は app/clients.py が CLIENTS_CONFIG の JSON から読む。
値が無いときに「どこかへ繋がる」既定値は置かない。機能を使う時点で need() が ConfigError を出す。
"""

from __future__ import annotations

from functools import lru_cache
from typing import Any, Literal

from pydantic import SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.errors import ConfigError


class Settings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore", case_sensitive=False)

    # 既定は dry-run。外部への書き込み（CRM の作成・更新、音声の削除）を行わずログだけ出す。
    dry_run: bool = True
    # バックエンドが作る CRM レコードの名前の先頭に【TEST】を付ける（本番 CRM でのテスト・デモ中は true）
    crm_test_records: bool = True
    log_level: str = "INFO"

    # クライアント設定の JSON（ファイルのパスか、JSON 文字列のどちらか）。秘密情報そのものは含めない
    clients_config: str | None = None
    clients_config_json: str | None = None
    # このサービスの公開 URL（例: https://meeting-notes-xxxx.a.run.app）。録音 URL と Cloud Tasks の宛先に使う
    service_url: str | None = None

    gcp_project_id: str | None = None
    gcp_region: str = "asia-northeast1"

    # 対面録音の一時保存先（ライフサイクルで1日後に自動削除する）
    gcs_bucket: str | None = None
    # 署名付き URL の署名に使うサービスアカウント（未指定なら実行中の認証情報のもの）
    signing_service_account: str | None = None
    upload_url_ttl_seconds: int = 900

    # 録音 URL の署名鍵。値そのもの（Cloud Run の --set-secrets で渡す）か、参照（sm:... / env:...）のどちらか
    recording_token_secret: SecretStr | None = None
    recording_token_secret_ref: str | None = None
    recording_url_ttl_hours: int = 24

    tasks_backend: Literal["cloud_tasks", "local"] = "cloud_tasks"
    tasks_queue: str | None = None
    tasks_location: str = "asia-northeast1"
    # Cloud Tasks が OIDC トークンを発行するときのサービスアカウント（/internal/* はこのメールだけ許可）
    tasks_invoker_sa: str | None = None
    # キューの max-attempts と揃える。最後の試行で失敗したときだけ CRM に「失敗」を書く
    tasks_max_attempts: int = 3
    tasks_dispatch_deadline_seconds: int = 1800
    # Recall の文字起こし完了待ちの再確認間隔と回数
    transcript_poll_seconds: int = 120
    transcript_poll_max: int = 30

    gemini_location: str = "asia-northeast1"
    gemini_model_transcribe: str | None = None
    gemini_model_text: str | None = None
    gemini_max_output_tokens: int = 32768
    gemini_temperature: float = 0.0

    # 対面録音を Gemini に渡す単位（秒）と、区切りを無音位置に寄せる幅（秒）
    audio_segment_seconds: int = 1200
    audio_split_window_seconds: int = 90
    audio_bitrate: str = "32k"
    # 1 のときは区間を順に処理し、前区間の末尾を文脈に渡して話者ラベルを揃える
    transcribe_concurrency: int = 1
    correct_chunk_chars: int = 6000
    correct_concurrency: int = 3
    glossary_cache_seconds: int = 600
    # これより古いボット状態通知（参加待ち・録音中など一時的な状態）は CRM に書かない
    status_event_max_age_minutes: int = 30
    # 失敗時も音声を削除するか（既定は残して再処理できるようにする。GCS はライフサイクルで消える）
    delete_media_on_failure: bool = False

    @field_validator("gemini_location")
    @classmethod
    def check_regional_only(cls, value: str) -> str:
        if value.strip().lower() == "global":
            raise ValueError("GEMINI_LOCATION に global は使えません（リージョナルエンドポイントを使う）")
        return value.strip()

    @field_validator("service_url")
    @classmethod
    def strip_trailing_slash(cls, value: str | None) -> str | None:
        return value.rstrip("/") if value else value

    def need(self, name: str) -> Any:
        """機能に必須の設定を取り出す。未設定なら ConfigError。"""
        value = getattr(self, name)
        if value is None or value == "":
            raise ConfigError(f"環境変数 {name.upper()} が設定されていません")
        if isinstance(value, SecretStr):
            secret = value.get_secret_value()
            if not secret:
                raise ConfigError(f"環境変数 {name.upper()} が設定されていません")
            return secret
        return value


@lru_cache
def get_settings() -> Settings:
    return Settings()
