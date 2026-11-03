"""動画の解析：業務システムの画面を操作しながら説明した動画を Gemini で読み、1本ごとに商談記録を作る。

流れ（docs/video-analysis.md）：
1. 人が GCS の videos/<クライアント>/ に動画を置く（Cloud Console からアップロード。ライフサイクルで1日で消える）
2. ops（GitHub Actions）から /internal/video-batch を積む → 置いてある動画ごとに /internal/video-analyze を積む
3. 動画の長さを調べ（ffprobe。署名付き URL で先頭などだけを読む）、VIDEO_WINDOW_SECONDS ずつ Gemini に読ませて、
   画面・項目・操作・説明を場面ごとに書き出す（出力が切れたら範囲を半分にして読み直す）
4. 書き出したものから要約・画面の一覧・課題・要件の候補・確認事項を作る
5. 商談記録を1回で作る（同じ動画の記録があればそれを更新）。全文は文字起こしの項目と添付ファイルに入れる
6. 動画を消す（DRY_RUN のときは消さない）

ログには ID・件数・秒数だけを出す。動画のファイル名（画面名などを含みうる）・中身・署名付き URL は出さない（CLAUDE.md ルール4）。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import PurePosixPath
from typing import Any
from zoneinfo import ZoneInfo

from pydantic import BaseModel

from app.errors import AppError, ExternalServiceError, PermanentError
from app.logs import log_event
from app.pipeline import formatting as fmt
from app.pipeline.records import picklist_value
from app.services.gemini import LlmResult, TextPart, VideoPart
from app.services.tasks import task_name
from app.videos.report import report_text, strings

logger = logging.getLogger(__name__)

BATCH_PATH = "/internal/video-batch"
ANALYZE_PATH = "/internal/video-analyze"
VIDEO_ROOT = "videos"
# Gemini が読める動画の形式（拡張子 → MIME）
VIDEO_MIME = {
    "mp4": "video/mp4",
    "m4v": "video/mp4",
    "mov": "video/quicktime",
    "webm": "video/webm",
    "avi": "video/x-msvideo",
    "mpeg": "video/mpeg",
    "mpg": "video/mpeg",
    "wmv": "video/x-ms-wmv",
    "3gp": "video/3gpp",
}
# 出力が切れたときに、これより短くは分けない（秒）
MIN_WINDOW_SECONDS = 60
# 前の範囲の終わりを、次の範囲の手がかりとして渡す長さ（文字）
CONTEXT_CHARS = 1500
# 動画ごとの処理を積むときの間隔（秒）。Gemini に同時に投げすぎないように少しずつずらす
STAGGER_SECONDS = 45
# 長さを調べる署名付き URL の有効時間・ffprobe の待ち時間（秒）
PROBE_URL_TTL = 600
PROBE_TIMEOUT = 120
# 録音アプリから上げる動画のファイル名（拡張子を除く）の長さの上限
MAX_TITLE_CHARS = 80


class VideoBatchTask(BaseModel):
    client_id: str
    limit: int = 0  # 0 なら置いてある動画を全部
    force: bool = False


class VideoTask(BaseModel):
    client_id: str
    object_name: str
    force: bool = False


@dataclass
class Described:
    text: str
    windows: int = 0
    splits: int = 0
    incomplete: list[str] = field(default_factory=list)


def video_prefix(client_id: str) -> str:
    return f"{VIDEO_ROOT}/{client_id}/"


def video_mime(object_name: str) -> str | None:
    return VIDEO_MIME.get(PurePosixPath(object_name).suffix.lower().lstrip("."))


def video_id(object_name: str) -> str:
    """ログ用の短い ID（ファイル名そのものは出さない）。"""
    return hashlib.sha256(object_name.encode()).hexdigest()[:12]


def video_title(object_name: str) -> str:
    """商談記録の名前・添付ファイルの名前に使う、動画のファイル名（拡張子なし）。COQL を壊す文字は除く。"""
    stem = PurePosixPath(object_name).stem
    stem = re.sub(r"['\"\\\\%\x00-\x1f]", "", stem).strip()
    return stem or "動画"


def clock(seconds: float) -> str:
    s = int(seconds)
    h, rest = divmod(s, 3600)
    m, sec = divmod(rest, 60)
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"


def windows(duration: float | None, size: int) -> list[tuple[float | None, float | None]]:
    """読む範囲（秒）。長さが分からなければ全体を1回で読む。"""
    if not duration or duration <= 0:
        return [(None, None)]
    out: list[tuple[float | None, float | None]] = []
    start = 0.0
    while start < duration:
        end = min(duration, start + size)
        # 最後の範囲が短すぎるときは、1つ前の範囲に含める
        if duration - end < MIN_WINDOW_SECONDS:
            end = duration
        out.append((start, end))
        start = end
    return out


async def probe_duration_url(url: str) -> float | None:
    """動画の長さ（秒）。読めなければ None（署名付き URL を含むので、エラーの中身は返さない・出さない）。"""
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1:nk=1", url,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )  # fmt: skip
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=PROBE_TIMEOUT)
    except (OSError, TimeoutError):
        return None
    if proc.returncode != 0:
        return None
    try:
        value = float(stdout.decode().strip().splitlines()[0])
    except (ValueError, IndexError):
        return None
    return value if value > 0 else None


def upload_name(client_id: str, filename: str) -> str | None:
    """録音アプリから上げる動画の置き場所（videos/<クライアント>/<ファイル名>）。読めない形式なら None。

    ファイル名は商談記録の名前に使うので残す。パスの区切り・制御文字・URL や検索を壊す文字は除く。
    """
    name = PurePosixPath(filename.replace("\\", "/")).name
    name = re.sub(r"[\x00-\x1f\x7f/\\#?\[\]*'\"%]", "", name).strip()
    stem, suffix = PurePosixPath(name).stem, PurePosixPath(name).suffix.lower()
    if not stem or stem.startswith(".") or video_mime(name) is None:
        return None
    return f"{video_prefix(client_id)}{stem[:MAX_TITLE_CHARS]}{suffix}"


async def enqueue_video(
    rt: Any, client_id: str, object_name: str, size: int, *, force: bool = False, delay_seconds: int = 0
) -> bool:
    """動画1本の解析を積む。同じ動画を短い間に二重に積まない（10分ごとに名前が変わるので、やり直しは10分後から積める）。"""
    slot = str(int(time.time() // 600))
    name = task_name("video", client_id, object_name, str(size), slot, "force" if force else "")
    payload = {"client_id": client_id, "object_name": object_name, "force": force}
    return bool(await rt.tasks.enqueue(ANALYZE_PATH, payload, name=name, delay_seconds=delay_seconds))


async def enqueue_videos(rt: Any, task: VideoBatchTask) -> int:
    """videos/<クライアント>/ に置いてある動画ごとに、解析を積む。積んだ数を返す。"""
    cs = rt.client_services(task.client_id)
    if not cs.config.features.video_analysis:
        raise PermanentError(f"クライアント {task.client_id} では動画の解析を使えません")
    objects = [
        o for o in await rt.storage.list(video_prefix(task.client_id)) if video_mime(o.name) and o.size > 0
    ]
    objects.sort(key=lambda o: o.name)
    if task.limit > 0:
        objects = objects[: task.limit]
    count = 0
    for i, obj in enumerate(objects):
        if await enqueue_video(
            rt, task.client_id, obj.name, obj.size, force=task.force, delay_seconds=i * STAGGER_SECONDS
        ):
            count += 1
    log_event(logger, "video.batch_enqueued", client_id=task.client_id, videos=len(objects), enqueued=count)
    return count


class VideoRun:
    def __init__(self, rt: Any, task: VideoTask, *, final_attempt: bool) -> None:
        self.rt = rt
        self.task = task
        self.final_attempt = final_attempt
        self.cs = rt.client_services(task.client_id)
        self.fm = self.cs.field_map
        self.settings = rt.settings
        self.vid = video_id(task.object_name)
        self.title = video_title(task.object_name)
        self.base_name = fmt.clip(f"{self.fm.video.name_prefix}{self.title}", self.fm.limits.name - 10) or ""
        self.record_id: str | None = None

    def _log(self, event: str, level: int = logging.INFO, **fields: Any) -> None:
        log_event(logger, event, level, client_id=self.task.client_id, video=self.vid, **fields)

    # ---- 入口 ----

    async def run(self) -> str:
        """ "done" / "skipped" / "failed" を返す。もう一度試す価値がある失敗（最後の試行でない）は例外のまま投げる。"""
        try:
            return await self._guarded()
        except AppError as exc:
            if exc.retryable and not self.final_attempt:
                raise
            self._log("video.failed", logging.ERROR, error_code=exc.code, error=exc.message)
            await self._record_failure(exc.message)
            return "failed"

    async def _guarded(self) -> str:
        try:
            return await self._main()
        except AppError:
            raise
        except Exception as exc:
            # 想定外のエラーは種類だけを出す（中身に動画の場所・署名付き URL が入りうるので、メッセージ・トレースは出さない）。
            # 原因が分からないので再試行に回し、最後の試行で「失敗」を書く
            self._log("video.unexpected_error", logging.ERROR, error_code=type(exc).__name__)
            raise ExternalServiceError(
                "video", f"想定外のエラー（{type(exc).__name__}）", retryable=True
            ) from None

    async def _main(self) -> str:
        if not self.cs.config.features.video_analysis:
            raise PermanentError(f"クライアント {self.task.client_id} では動画の解析を使えません")
        name = self.task.object_name
        if not name.startswith(video_prefix(self.task.client_id)) or ".." in name:
            raise PermanentError("動画の置き場所が違います")
        mime = video_mime(name)
        if mime is None:
            raise PermanentError("動画の形式に対応していません")
        f, s = self.fm.meeting_record, self.fm.status
        crm = await self.cs.crm()
        existing = await self._find_record()
        if existing:
            self.record_id = str(existing["id"])
            if picklist_value(existing.get(f.status)) == s.done and not self.task.force:
                self._log("video.skipped", record_id=self.record_id)
                return "skipped"
        if not [o for o in await self.rt.storage.list(name) if o.name == name]:
            raise PermanentError(
                "動画が見つかりません（1日を過ぎて自動で消えたか、すでに解析して消した可能性があります）"
            )

        started = time.monotonic()
        url = await self.rt.storage.download_url(name, PROBE_URL_TTL)
        duration = await probe_duration_url(url)
        self._log("video.started", seconds=round(duration or 0), known_length=duration is not None)
        described = await self._describe(self.rt.storage.gs_uri(name), mime, duration)
        report = await self._summarize(described.text, duration)
        fields = self._fields(report, described)
        if self.record_id:
            await crm.update_record(f.module, self.record_id, fields)
        else:
            self.record_id = await crm.create_record(f.module, {f.name: self.base_name, **fields})
        if self.cs.config.features.transcript_attachment:
            await self._attach(report, described, duration)
        await self.rt.storage.delete_prefix(name, dry_run=self.cs.dry_run)
        self._log(
            "video.done",
            record_id=self.record_id,
            windows=described.windows,
            splits=described.splits,
            incomplete=len(described.incomplete),
            chars=len(described.text),
            elapsed=round(time.monotonic() - started, 1),
        )
        return "done"

    # ---- CRM ----

    async def _find_record(self) -> dict[str, Any] | None:
        """同じ動画の商談記録（名前が「…動画解析 <ファイル名>」で終わるもの。【TEST】の有無は問わない）。"""
        f = self.fm.meeting_record
        crm = await self.cs.crm()
        # 名前から引用符・バックスラッシュ・% は除いてある（video_title）
        query = (
            f"select id, {f.name}, {f.status} from {f.module} "  # noqa: S608
            f"where {f.name} like '%{self.base_name}' limit 1"
        )
        rows = await crm.coql(query)
        return rows[0] if rows else None

    def _fields(self, report: dict[str, Any], described: Described) -> dict[str, Any]:
        f, s, lim = self.fm.meeting_record, self.fm.status, self.fm.limits
        summary = report.get("summary") or ""
        screens = fmt.bullets(strings(report.get("screens")))
        functions = fmt.bullets(strings(report.get("functions")))
        parts = [summary]
        if screens:
            parts.append(f"【出てきた画面】\n{screens}")
        if functions:
            parts.append(f"【機能・使い方】\n{functions}")
        split = fmt.split_transcript(
            described.text, lim.transcript, attached=self.cs.config.features.transcript_attachment
        )
        return {
            f.status: s.done,
            f.error_message: None,
            f.category: self.fm.video.category,
            f.summary: fmt.clip("\n\n".join(p for p in parts if p), lim.summary),
            f.issues: fmt.clip(fmt.bullets(strings(report.get("issues"))), lim.issues),
            f.needs: fmt.clip(fmt.bullets(strings(report.get("requirements"))), lim.needs),
            f.next_actions: fmt.clip(fmt.bullets(strings(report.get("questions"))), lim.next_actions),
            f.transcript: split.first,
            f.transcript_2: split.second,
        }

    async def _attach(self, report: dict[str, Any], described: Described, duration: float | None) -> None:
        """書き出したものの全文と要約を .txt で付ける（失敗しても記録は書けているので、処理は失敗にしない）。"""
        f = self.fm.meeting_record
        crm = await self.cs.crm()
        today = datetime.now(ZoneInfo(self.cs.config.timezone)).date().isoformat()
        header = f"長さ：{clock(duration) if duration else '（不明）'}　解析日：{today}"
        text = report_text(self.title, header, report, described.text)
        content = ("\ufeff" + text).encode("utf-8")
        filename = re.sub(r'[\\/:*?"<>|]', "_", f"{self.fm.video.attachment_prefix}{self.title}") + ".txt"
        try:
            await crm.upload_attachment(f.module, str(self.record_id), filename, content)
        except (AppError, ValueError) as exc:
            self._log(
                "video.attachment_failed",
                logging.WARNING,
                error_code=getattr(exc, "code", type(exc).__name__),
            )

    async def _record_failure(self, message: str) -> None:
        f, s = self.fm.meeting_record, self.fm.status
        data = {f.status: s.failed, f.error_message: fmt.clip(message, self.fm.limits.error_message)}
        try:
            crm = await self.cs.crm()
            if self.record_id:
                await crm.update_record(f.module, self.record_id, data)
            else:
                self.record_id = await crm.create_record(
                    f.module, {f.name: self.base_name, f.category: self.fm.video.category, **data}
                )
        except AppError as exc:
            self._log("video.failure_not_recorded", logging.ERROR, error_code=exc.code)

    # ---- Gemini ----

    async def _describe(self, uri: str, mime: str, duration: float | None) -> Described:
        out = Described(text="")
        texts: list[str] = []
        for start, end in windows(duration, self.settings.video_window_seconds):
            texts += await self._describe_window(uri, mime, start, end, texts[-1] if texts else "", out)
        out.text = "\n\n".join(t for t in texts if t)
        return out

    async def _describe_window(
        self, uri: str, mime: str, start: float | None, end: float | None, previous: str, out: Described
    ) -> list[str]:
        result = await self._generate_window(uri, mime, start, end, previous)
        out.windows += 1
        if (
            result.truncated
            and start is not None
            and end is not None
            and end - start >= MIN_WINDOW_SECONDS * 2
        ):
            # 出力が切れた：範囲を半分にして読み直す
            out.splits += 1
            self._log("video.split", start=round(start), end=round(end))
            middle = start + (end - start) / 2
            first = await self._describe_window(uri, mime, start, middle, previous, out)
            second = await self._describe_window(
                uri, mime, middle, end, first[-1] if first else previous, out
            )
            return first + second
        text = result.text.strip()
        if result.truncated:
            span = f"{clock(start)}〜{clock(end)}" if start is not None and end is not None else "動画の後半"
            out.incomplete.append(span)
            text += f"\n（{span} の終わりの方は、詳しく書ききれませんでした）"
        return [text]

    async def _generate_window(
        self, uri: str, mime: str, start: float | None, end: float | None, previous: str
    ) -> LlmResult:
        if start is None or end is None:
            instruction = "動画の全体を書き出してください。"
        else:
            instruction = (
                f"動画の {clock(start)}〜{clock(end)} の部分を書き出してください。"
                "時刻は動画の先頭からの時刻で書いてください。"
            )
        if previous:
            instruction += (
                "\n\n参考：この直前の部分の記録の終わり（画面名・項目名の表記をそろえるため。書き直さない）\n"
                + previous[-CONTEXT_CHARS:]
            )
        part = VideoPart(uri, mime, start, end, self.settings.gemini_video_fps)
        return await self.rt.llm.generate(
            task="video_describe",
            model=self.settings.need("gemini_model_transcribe"),
            system_instruction=self.rt.prompts.video_describe,
            parts=[part, TextPart(instruction)],
        )

    async def _summarize(self, text: str, duration: float | None) -> dict[str, Any]:
        if not text.strip():
            raise PermanentError("動画から何も読み取れませんでした")
        header = f"動画：{self.title}（長さ {clock(duration) if duration else '不明'}）"
        # まず画面ごとの詳細まで含めてまとめる。出力が切れた・形が崩れたときは、詳細を省いた形でまとめ直す
        for schema, detailed in (
            (self.rt.prompts.video_schema_detail, True),
            (self.rt.prompts.video_schema, False),
        ):
            result = await self.rt.llm.generate(
                task="video_summarize",
                model=self.settings.need("gemini_model_text"),
                system_instruction=self.rt.prompts.video_summarize,
                parts=[TextPart(f"{header}\n\n{text}")],
                json_schema=schema,
            )
            try:
                data = json.loads(result.text)
            except json.JSONDecodeError:
                data = None
            if isinstance(data, dict) and not result.truncated:
                return data
            if detailed:
                self._log("video.summary_simplified", truncated=result.truncated)
        raise PermanentError("要約の形式が正しくありませんでした")


async def run_video_analysis(rt: Any, task: VideoTask, *, final_attempt: bool) -> str:
    return await VideoRun(rt, task, final_attempt=final_attempt).run()
