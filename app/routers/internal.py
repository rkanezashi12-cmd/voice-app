"""Cloud Tasks からだけ呼ばれる内部エンドポイント（OIDC トークンを検証する）。

- POST /internal/recall-event … Webhook の中身を処理（状態の書き込み、共通処理を積む）
- POST /internal/process      … 共通処理（入口非依存）

503 を返すと Cloud Tasks が再試行する。再試行しても直らない失敗は 200 を返して CRM に「失敗」を記録済み。
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app.deps import RuntimeDep, TasksOidcDep
from app.errors import AppError
from app.logs import log_event
from app.pipeline.process import ProcessRequest, run_process
from app.pipeline.recall_flow import handle_recall_event

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/internal", tags=["internal"], dependencies=[TasksOidcDep])


class RecallEventTask(BaseModel):
    client_id: str
    webhook_id: str | None = None
    payload: dict[str, Any]


def _retry_count(request: Request) -> int:
    try:
        return int(request.headers.get("X-CloudTasks-TaskRetryCount", "0"))
    except ValueError:
        return 0


@router.post("/recall-event")
async def recall_event(body: RecallEventTask, request: Request, rt: RuntimeDep) -> JSONResponse:
    try:
        result = await handle_recall_event(rt, body.client_id, body.payload)
    except AppError as exc:
        final = _retry_count(request) + 1 >= rt.settings.tasks_max_attempts
        log_event(
            logger,
            "recall_event.failed",
            logging.ERROR if final or not exc.retryable else logging.WARNING,
            client_id=body.client_id,
            error_code=exc.code,
            error=exc.message,
        )
        if exc.retryable and not final:
            return JSONResponse({"status": "retry"}, status_code=503)
        return JSONResponse({"status": "failed", "message": exc.message})
    return JSONResponse({"status": result})


@router.post("/process")
async def process(body: ProcessRequest, request: Request, rt: RuntimeDep) -> JSONResponse:
    final = _retry_count(request) + 1 >= rt.settings.tasks_max_attempts
    outcome = await run_process(rt, body, final_attempt=final)
    code = 503 if outcome.status == "retry" else 200
    return JSONResponse({"status": outcome.status, "record_id": outcome.record_id}, status_code=code)
