"""録音アプリ（/app/videos.html）から動画を上げて解析する API（docs/video-analysis.md）。

Zoho でログインした人だけが使える（Cookie。app/deps.py の AppUserDep）。クライアントの features.video_analysis が true のときだけ。

- POST /api/app/videos/upload-url … 動画を GCS に直接上げる署名付き URL（PUT）を返す。置き場所は videos/<クライアント>/<ファイル名>
- POST /api/app/videos/analyze    … 上げ終わった動画の解析を積む
- GET  /api/app/videos            … 動画解析の商談記録の一覧（新しい順。状態を見る）

動画そのものはこのサービスを通らない（ブラウザから GCS へ直接）。ログには ID・大きさだけを出し、ファイル名は出さない。
"""

from __future__ import annotations

import logging
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field

from app.deps import AppUserDep, RuntimeDep
from app.logs import log_event
from app.pipeline.records import picklist_value
from app.services.crm import TEST_PREFIX
from app.videos.analysis import enqueue_video, upload_name, video_id, video_mime, video_prefix

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/app/videos", tags=["app"])

# 署名付き URL の有効時間（秒）。大きい動画を遅い回線で上げても間に合うように長めにする（PUT の始まりまでに使えればよい）
UPLOAD_URL_TTL = 6 * 3600
# 1本の大きさの上限（バイト）
MAX_VIDEO_BYTES = 5 * 1024**3
# 一覧に出す件数
LIST_LIMIT = 100


def _check_feature(rt: Any, client_id: str) -> Any:
    cs = rt.client_services(client_id)
    if not cs.config.features.video_analysis:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "動画の解析は使えません")
    return cs


class UploadRequest(BaseModel):
    filename: Annotated[str, Field(min_length=1, max_length=255)]
    size: Annotated[int, Field(gt=0)]


@router.post("/upload-url")
async def upload_url(body: UploadRequest, user: AppUserDep, rt: RuntimeDep) -> dict[str, Any]:
    _check_feature(rt, user.client_id)
    if body.size > MAX_VIDEO_BYTES:
        raise HTTPException(422, "動画が大きすぎます（1本 5GB まで）")
    object_name = upload_name(user.client_id, body.filename)
    if object_name is None:
        raise HTTPException(
            422,
            "この形式の動画は読めません（mp4・mov・webm などにしてください）",
        )
    content_type = video_mime(object_name) or "video/mp4"
    url = await rt.storage.upload_url(object_name, content_type, UPLOAD_URL_TTL)
    log_event(
        logger, "video.upload_url", client_id=user.client_id, video=video_id(object_name), bytes=body.size
    )
    return {"upload_url": url, "object_name": object_name, "content_type": content_type}


class AnalyzeRequest(BaseModel):
    object_name: Annotated[str, Field(min_length=1, max_length=400)]
    force: bool = False


@router.post("/analyze")
async def analyze(body: AnalyzeRequest, user: AppUserDep, rt: RuntimeDep) -> dict[str, Any]:
    _check_feature(rt, user.client_id)
    name = body.object_name
    # 自分のクライアントの置き場所で、upload-url が返す形の名前だけを受け付ける
    if not name.startswith(video_prefix(user.client_id)) or upload_name(user.client_id, name) != name:
        raise HTTPException(422, "動画の置き場所が違います")
    found = [o for o in await rt.storage.list(name) if o.name == name and o.size > 0]
    if not found:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, "動画が見つかりません（アップロードが終わっていない可能性があります）"
        )
    queued = await enqueue_video(rt, user.client_id, name, found[0].size, force=body.force)
    log_event(
        logger, "video.analyze_requested", client_id=user.client_id, video=video_id(name), queued=queued
    )
    return {"status": "queued" if queued else "already_queued"}


@router.get("")
async def list_videos(user: AppUserDep, rt: RuntimeDep) -> dict[str, Any]:
    cs = _check_feature(rt, user.client_id)
    fm = cs.field_map
    f = fm.meeting_record
    # 分類は固定の値（field_map）だけで、利用者の入力は入らない
    query = (
        f"select id, {f.name}, {f.status}, {f.error_message}, Created_Time from {f.module} "  # noqa: S608
        f"where {f.category} = '{fm.video.category}' order by Created_Time desc limit {LIST_LIMIT}"
    )
    rows = await (await cs.crm()).coql(query)
    items = []
    for row in rows:
        name = str(row.get(f.name) or "")
        if name.startswith(TEST_PREFIX):
            name = name[len(TEST_PREFIX) :]
        items.append(
            {
                "id": str(row.get("id")),
                "name": name.removeprefix(fm.video.name_prefix),
                "status": picklist_value(row.get(f.status)) or "",
                "error": row.get(f.error_message) or "",
                "created": row.get("Created_Time") or "",
            }
        )
    return {"videos": items, "done": fm.status.done, "failed": fm.status.failed}
