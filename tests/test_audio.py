from __future__ import annotations

import asyncio
import shutil
import subprocess
from pathlib import Path

import pytest

from app import recording_objects as ro
from app.services.audio import choose_split_points, parse_silences, prepare_segments, segments_from_points

FFMPEG = shutil.which("ffmpeg") is not None


def test_parse_silences() -> None:
    stderr = (
        "[silencedetect @ 0x1] silence_start: 10.5\n"
        "[silencedetect @ 0x1] silence_end: 12.5 | silence_duration: 2\n"
        "[silencedetect @ 0x1] silence_start: 99\n"
    )
    assert parse_silences(stderr, duration=100.0) == [(10.5, 12.5), (99.0, 100.0)]


def test_split_points_snap_to_nearby_silence() -> None:
    silences = [(1150.0, 1160.0), (2500.0, 2502.0)]
    points = choose_split_points(3000.0, silences, target=1200, window=90)
    assert points[0] == 1155.0, "20分付近の無音の中央で区切る"
    assert points[1] == 2355.0, "近くに無音が無ければ目安の位置で区切る"
    assert segments_from_points(3000.0, points) == [(0.0, 1155.0), (1155.0, 2355.0), (2355.0, 3000.0)]


def test_short_audio_is_one_segment() -> None:
    assert choose_split_points(600.0, [], target=1200, window=90) == []
    assert choose_split_points(1250.0, [], target=1200, window=90) == [], "末尾が短すぎる区切りは作らない"


def _tone(path: Path, seconds: float, freq: int, silence: float = 0.0) -> None:
    filters = f"sine=frequency={freq}:duration={seconds}"
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i", filters]
    if silence:
        cmd += ["-af", f"apad=pad_dur={silence}"]
    subprocess.run([*cmd, "-c:a", "libopus", str(path)], check=True)  # noqa: S603


@pytest.mark.skipif(not FFMPEG, reason="ffmpeg が無い環境では実行しない")
async def test_prepare_segments_with_real_ffmpeg(tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.mkdir()
    work = tmp_path / "work"
    work.mkdir()
    session = "1727488500123-a-abc123"
    files = {}
    for seq in range(4):
        name = ro.object_name("default", "r1", session, seq, "webm")
        path = src / f"{seq}.webm"
        await asyncio.to_thread(_tone, path, 4.0, 440 + seq * 100, 2.0)
        files[name] = path
    chunks = [ro.parse_object(n) for n in files]
    sessions = ro.group_sessions([c for c in chunks if c])

    async def download(chunk: ro.Chunk, dest: Path) -> None:
        await asyncio.to_thread(shutil.copy, files[chunk.name], dest)

    segments = await prepare_segments(
        sessions, download, work, segment_seconds=8, window_seconds=3, bitrate="32k"
    )
    assert len(segments) >= 2
    assert segments[0].start == 0.0
    assert all(len(s.data) > 0 and s.mime_type == "audio/mpeg" for s in segments)
    total = segments[-1].end
    assert 22.0 <= total <= 26.0, "4 チャンク × 約6秒がつながる"
    assert list(work.iterdir()) == [], "一時ファイルを残さない"
