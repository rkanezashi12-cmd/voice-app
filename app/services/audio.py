"""対面録音の音声を Gemini に渡せる形にする（ffmpeg）。

1. セッションごとにチャンクを並べる（方式b は順に連結して1本に戻す）
2. 各部分を 16kHz モノラルの MP3 に変換し、1本に連結する
3. 約 AUDIO_SEGMENT_SECONDS ごとに、区切りを近くの無音位置に寄せて分割する

Gemini の受け付ける音声形式で明記されているのは mp3 / wav など。iPhone の audio/mp4 や Android の
audio/webm をそのまま渡せるかは未確認のため、ここで MP3 に変換して吸収する。
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

from app.errors import PermanentError
from app.logs import log_event
from app.recording_objects import Chunk, Session

logger = logging.getLogger(__name__)

# 区切りと区切りの最小間隔（秒）。目安の長さの 1/4 と比べて小さい方を使う
MIN_TAIL_SECONDS = 60.0
_SILENCE_START = re.compile(r"silence_start:\s*(-?\d+(?:\.\d+)?)")
_SILENCE_END = re.compile(r"silence_end:\s*(-?\d+(?:\.\d+)?)")


@dataclass(frozen=True)
class AudioSegment:
    index: int
    start: float
    end: float
    data: bytes
    mime_type: str = "audio/mpeg"


def parse_silences(stderr: str, duration: float | None = None) -> list[tuple[float, float]]:
    """ffmpeg silencedetect の出力から (開始, 終了) の一覧を作る。"""
    silences: list[tuple[float, float]] = []
    start: float | None = None
    for line in stderr.splitlines():
        m = _SILENCE_START.search(line)
        if m:
            start = max(0.0, float(m.group(1)))
            continue
        m = _SILENCE_END.search(line)
        if m and start is not None:
            silences.append((start, float(m.group(1))))
            start = None
    if start is not None and duration is not None:
        silences.append((start, duration))
    return silences


def choose_split_points(
    duration: float, silences: list[tuple[float, float]], target: float, window: float
) -> list[float]:
    """おおむね target 秒ごとの区切り位置を、前後 window 秒以内の無音の中央に寄せて決める。"""
    points: list[float] = []
    last = 0.0
    boundary = target
    min_gap = min(MIN_TAIL_SECONDS, target / 4)
    while boundary < duration - min_gap:
        candidates = [
            (s + e) / 2
            for s, e in silences
            if abs((s + e) / 2 - boundary) <= window and (s + e) / 2 > last + min_gap
        ]
        point = min(candidates, key=lambda m: abs(m - boundary)) if candidates else boundary
        points.append(round(point, 3))
        last = point
        boundary = point + target
    return points


def segments_from_points(duration: float, points: list[float]) -> list[tuple[float, float]]:
    edges = [0.0, *points, duration]
    return [(edges[i], edges[i + 1]) for i in range(len(edges) - 1) if edges[i + 1] > edges[i]]


async def _run(*args: str) -> str:
    proc = await asyncio.create_subprocess_exec(
        *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        # ffmpeg のエラー出力にはファイル名しか含まれないが、念のため末尾だけを短く残す
        raise PermanentError(
            f"音声の変換に失敗しました（{args[0]}: {stderr.decode(errors='replace')[-200:]}）"
        )
    return stdout.decode(errors="replace") + stderr.decode(errors="replace")


async def probe_duration(path: Path) -> float:
    out = await _run(
        "ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1:nk=1", str(path)
    )
    try:
        return float(out.strip().splitlines()[0])
    except (ValueError, IndexError) as exc:
        raise PermanentError("音声の長さを取得できませんでした") from exc


async def _to_mp3(src: Path, dst: Path, bitrate: str) -> None:
    await _run(
        "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y", "-i", str(src),
        "-vn", "-ac", "1", "-ar", "16000", "-c:a", "libmp3lame", "-b:a", bitrate, str(dst),
    )  # fmt: skip


Downloader = Callable[[Chunk, Path], Awaitable[None]]


async def prepare_segments(
    sessions: list[Session],
    download: Downloader,
    workdir: Path,
    *,
    segment_seconds: int,
    window_seconds: int,
    bitrate: str,
) -> list[AudioSegment]:
    pieces: list[Path] = []
    for s_index, session in enumerate(sessions):
        if not session.chunks:
            continue
        if session.mode == "b":
            # timeslice の分割は先頭にだけヘッダーがあるので、順に連結して1本に戻す
            joined = workdir / f"s{s_index:03d}.{session.chunks[0].ext}"
            with joined.open("wb") as out:
                for chunk in session.chunks:
                    part = workdir / f"s{s_index:03d}_{chunk.seq:06d}.{chunk.ext}"
                    await download(chunk, part)
                    out.write(await asyncio.to_thread(part.read_bytes))
                    part.unlink()
            pieces.append(joined)
        else:
            for chunk in session.chunks:
                part = workdir / f"s{s_index:03d}_{chunk.seq:06d}.{chunk.ext}"
                await download(chunk, part)
                pieces.append(part)

    converted: list[Path] = []
    skipped = 0
    for i, piece in enumerate(pieces):
        dst = workdir / f"p{i:05d}.mp3"
        try:
            await _to_mp3(piece, dst, bitrate)
            converted.append(dst)
        except PermanentError:
            # 中断時の最後のチャンクなど、壊れた部分は飛ばして残りを使う
            skipped += 1
        piece.unlink(missing_ok=True)
    if not converted:
        raise PermanentError("変換できる音声がありませんでした")

    full = workdir / "full.mp3"
    listing = workdir / "list.txt"
    listing.write_text("".join(f"file '{p.name}'\n" for p in converted), encoding="utf-8")
    await _run(
        "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
        "-f", "concat", "-safe", "0", "-i", str(listing), "-c", "copy", str(full),
    )  # fmt: skip
    for p in converted:
        p.unlink(missing_ok=True)
    listing.unlink(missing_ok=True)

    duration = await probe_duration(full)
    detect = await _run(
        "ffmpeg", "-nostdin", "-hide_banner", "-i", str(full),
        "-af", "silencedetect=noise=-35dB:d=0.6", "-f", "null", "-",
    )  # fmt: skip
    points = choose_split_points(duration, parse_silences(detect, duration), segment_seconds, window_seconds)

    segments: list[AudioSegment] = []
    for index, (start, end) in enumerate(segments_from_points(duration, points)):
        dst = workdir / f"seg{index:03d}.mp3"
        await _run(
            "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
            "-ss", f"{start:.3f}", "-t", f"{end - start:.3f}", "-i", str(full), "-c", "copy", str(dst),
        )  # fmt: skip
        segments.append(AudioSegment(index, start, end, await asyncio.to_thread(dst.read_bytes)))
        dst.unlink(missing_ok=True)
    full.unlink(missing_ok=True)
    log_event(
        logger,
        "audio.prepared",
        sessions=len(sessions),
        pieces=len(pieces),
        skipped_pieces=skipped,
        duration_seconds=round(duration, 1),
        segments=len(segments),
    )
    return segments
