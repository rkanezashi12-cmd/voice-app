"""プロセス内で共有するサービスの入れ物。

FastAPI の app.state.runtime に1つ置き、ルーターは get_runtime() で受け取る。
テストでは各サービスを偽物に差し替えて組み立てる。
"""

from __future__ import annotations

from typing import Any

import httpx
from fastapi import Request

from app.client_services import ClientServices
from app.clients import ClientRegistry, SecretResolver
from app.config import Settings
from app.errors import ConfigError
from app.pipeline.ai import MeetingAI, PromptStore
from app.pipeline.glossary import GlossaryCache
from app.services.gemini import GeminiClient, LlmClient
from app.services.storage import StorageService
from app.services.tasks import CloudTasksQueue, LocalTaskQueue, TaskQueue


class Runtime:
    def __init__(
        self,
        settings: Settings,
        registry: ClientRegistry,
        *,
        secrets: SecretResolver | None = None,
        http: httpx.AsyncClient | None = None,
        storage: StorageService | Any | None = None,
        tasks: TaskQueue | None = None,
        llm: LlmClient | None = None,
        prompts: PromptStore | None = None,
        client_services: dict[str, Any] | None = None,
    ) -> None:
        self.settings = settings
        self.registry = registry
        self.secrets = secrets or SecretResolver()
        self.http = http or httpx.AsyncClient(timeout=httpx.Timeout(60.0, connect=10.0))
        self._storage = storage
        self._tasks = tasks
        self._llm = llm
        self.prompts = prompts or PromptStore()
        self._client_services: dict[str, Any] = dict(client_services or {})
        self.glossary = GlossaryCache(settings.glossary_cache_seconds)

    @property
    def storage(self) -> StorageService:
        if self._storage is None:
            self._storage = StorageService(
                self.settings.need("gcs_bucket"),
                dry_run=self.settings.dry_run,
                signing_service_account=self.settings.signing_service_account,
            )
        return self._storage

    @property
    def tasks(self) -> TaskQueue:
        if self._tasks is None:
            if self.settings.tasks_backend == "local":
                self._tasks = LocalTaskQueue()
            else:
                self._tasks = CloudTasksQueue(self.settings)
        return self._tasks

    @property
    def llm(self) -> LlmClient:
        if self._llm is None:
            self._llm = GeminiClient(self.settings)
        return self._llm

    @property
    def ai(self) -> MeetingAI:
        return MeetingAI(self.llm, self.prompts, self.settings)

    def client_services(self, client_id: str) -> ClientServices:
        services = self._client_services.get(client_id)
        if services is None:
            config = self.registry.get(client_id)
            services = ClientServices(config, self.settings, self.secrets, self.http)
            self._client_services[client_id] = services
        return services

    async def recording_secret(self) -> bytes:
        """録音 URL の署名鍵（値の直接指定か、Secret Manager の参照）。"""
        if self.settings.recording_token_secret is not None:
            return self.settings.need("recording_token_secret").encode()
        ref = self.settings.recording_token_secret_ref
        if not ref:
            raise ConfigError(
                "環境変数 RECORDING_TOKEN_SECRET か RECORDING_TOKEN_SECRET_REF が設定されていません"
            )
        return (await self.secrets.get(ref)).encode()

    async def aclose(self) -> None:
        await self.http.aclose()


def get_runtime(request: Request) -> Runtime:
    return request.app.state.runtime
