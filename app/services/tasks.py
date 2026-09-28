"""Cloud Tasks への投入。

- タスク名を指定すると、同じ名前の二重投入を Cloud Tasks が拒否する（重複通知の排除に使う）。
  拒否された場合は False を返す。
- Cloud Run の /internal/* は OIDC トークン（TASKS_INVOKER_SA 発行、audience=SERVICE_URL）で呼ばれる。
- ローカル開発用に、同じプロセス内で直接実行する LocalTaskQueue も用意する。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from app.config import Settings
from app.errors import ExternalServiceError
from app.logs import log_event

logger = logging.getLogger(__name__)

_NAME_RE = re.compile(r"[^A-Za-z0-9_-]")


def task_name(prefix: str, *parts: str) -> str:
    """外部由来の ID からタスク名を作る（使える文字に限定し、長さを抑えるためハッシュ化する）。"""
    digest = hashlib.sha256("\x1f".join(parts).encode()).hexdigest()[:40]
    return f"{_NAME_RE.sub('-', prefix)[:40]}-{digest}"


class TaskQueue(Protocol):
    async def enqueue(
        self, path: str, payload: dict[str, Any], *, name: str | None = None, delay_seconds: int = 0
    ) -> bool: ...


class CloudTasksQueue:
    def __init__(self, settings: Settings) -> None:
        self._project = settings.need("gcp_project_id")
        self._location = settings.tasks_location
        self._queue = settings.need("tasks_queue")
        self._service_url = settings.need("service_url")
        self._invoker = settings.need("tasks_invoker_sa")
        self._deadline = settings.tasks_dispatch_deadline_seconds
        self._client: Any = None

    def _get_client(self) -> Any:
        from google.cloud import tasks_v2

        if self._client is None:
            self._client = tasks_v2.CloudTasksAsyncClient()
        return self._client

    async def enqueue(
        self, path: str, payload: dict[str, Any], *, name: str | None = None, delay_seconds: int = 0
    ) -> bool:
        from google.api_core import exceptions as gexc

        client = self._get_client()
        parent = client.queue_path(self._project, self._location, self._queue)
        task: dict[str, Any] = {
            "http_request": {
                "http_method": "POST",
                "url": f"{self._service_url}{path}",
                "headers": {"Content-Type": "application/json"},
                "body": json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                "oidc_token": {"service_account_email": self._invoker, "audience": self._service_url},
            },
            "dispatch_deadline": timedelta(seconds=self._deadline),
        }
        if name:
            task["name"] = client.task_path(self._project, self._location, self._queue, name)
        if delay_seconds > 0:
            task["schedule_time"] = datetime.now(UTC) + timedelta(seconds=delay_seconds)
        try:
            await client.create_task(parent=parent, task=task)
        except gexc.AlreadyExists:
            log_event(logger, "tasks.duplicate", path=path, task=name)
            return False
        except gexc.GoogleAPICallError as exc:
            raise ExternalServiceError("cloud_tasks", type(exc).__name__, retryable=True) from exc
        log_event(logger, "tasks.enqueued", path=path, task=name, delay_seconds=delay_seconds)
        return True


Handler = Callable[[dict[str, Any]], Awaitable[Any]]


class LocalTaskQueue:
    """開発用：同じプロセス内でバックグラウンド実行する（Cloud Tasks も OIDC も使わない）。"""

    def __init__(self) -> None:
        self._handlers: dict[str, Handler] = {}
        self._seen: set[str] = set()
        self._running: set[asyncio.Task[Any]] = set()

    def register(self, path: str, handler: Handler) -> None:
        self._handlers[path] = handler

    async def enqueue(
        self, path: str, payload: dict[str, Any], *, name: str | None = None, delay_seconds: int = 0
    ) -> bool:
        if name:
            if name in self._seen:
                return False
            self._seen.add(name)
        handler = self._handlers.get(path)
        if handler is None:
            raise RuntimeError(f"ローカルのタスク処理先が登録されていません: {path}")

        async def run() -> None:
            if delay_seconds:
                await asyncio.sleep(delay_seconds)
            try:
                await handler(payload)
            except Exception:
                logger.exception("local_task.failed", extra={"fields": {"path": path}})

        task = asyncio.create_task(run())
        self._running.add(task)
        task.add_done_callback(self._running.discard)
        return True
