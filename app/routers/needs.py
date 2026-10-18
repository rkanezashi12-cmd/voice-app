"""ニーズ分析の受け付け（docs/needs-analysis.md）。

POST /api/needs/analyze（CRM の「ニーズ分析」のボタンの関数から X-API-Key で呼ぶ）
→ Cloud Tasks に積んですぐ返す。分析は /internal/needs-analyze（app/needs/analysis.py）。
features.needs_analysis が true のクライアントだけが使える（ほかは 404）。
"""

from __future__ import annotations

import logging
import time

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, field_validator

from app.deps import ApiClientDep, RuntimeDep
from app.logs import log_event
from app.needs.analysis import ANALYZE_PATH, NeedsAnalysisTask
from app.recording_token import RECORD_ID_RE
from app.services.tasks import task_name

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/needs", tags=["needs"])

# 続けて押されたボタンを1回にまとめる間隔（秒）
DEDUPE_SECONDS = 60


class AnalyzeRequest(BaseModel):
    record_id: str

    @field_validator("record_id")
    @classmethod
    def check_record_id(cls, value: str) -> str:
        if not RECORD_ID_RE.match(value):
            raise ValueError("record_id の形式が不正です")
        return value


@router.post("/analyze")
async def analyze(body: AnalyzeRequest, client: ApiClientDep, rt: RuntimeDep) -> dict[str, object]:
    if not client.features.needs_analysis:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "このクライアントではニーズ分析を使えません")
    task = NeedsAnalysisTask(client_id=client.client_id, record_id=body.record_id)
    bucket = str(int(time.time()) // DEDUPE_SECONDS)
    queued = await rt.tasks.enqueue(
        ANALYZE_PATH, task.model_dump(), name=task_name("needs", client.client_id, body.record_id, bucket)
    )
    log_event(
        logger, "needs.queued", client_id=client.client_id, record_id=body.record_id, duplicate=not queued
    )
    return {"queued": queued}
