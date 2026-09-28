"""対面録音のオブジェクト配置（録音 API と共通処理の両方で使う）。

gs://<bucket>/recordings/<client_id>/<record_id>/<session_id>/<seq:06d>.<ext>

- session_id は録音ページが「録音開始」「再開」のたびに作る: <開始時刻 epoch ms>-<方式 a|b>-<乱数>
  - 方式 a: 1分ごとに録音を区切り直す（各ファイルが単体で再生できる）
  - 方式 b: timeslice で分割（先頭ファイルにだけヘッダーがあり、順に連結して1本になる）
- 開始時刻が先頭にあるので、名前順に並べれば録音順になる。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

SESSION_ID_RE = re.compile(r"^(?P<started>\d{13})-(?P<mode>[ab])-(?P<rand>[a-z0-9]{4,12})$")
MAX_SEQ = 9999

# 受け付ける MIME（パラメータを除いた型）→ 拡張子
MIME_EXTENSIONS: dict[str, str] = {
    "audio/webm": "webm",
    "audio/mp4": "m4a",
    "audio/x-m4a": "m4a",
    "audio/aac": "aac",
    "audio/ogg": "ogg",
    "audio/wav": "wav",
}

_OBJECT_RE = re.compile(
    r"^recordings/(?P<client>[^/]+)/(?P<record>[^/]+)/(?P<session>[^/]+)/(?P<seq>\d{6})\.(?P<ext>[a-z0-9]+)$"
)


def base_mime(mime_type: str) -> str:
    return mime_type.split(";", 1)[0].strip().lower()


def prefix(client_id: str, record_id: str) -> str:
    return f"recordings/{client_id}/{record_id}/"


def object_name(client_id: str, record_id: str, session_id: str, seq: int, ext: str) -> str:
    return f"{prefix(client_id, record_id)}{session_id}/{seq:06d}.{ext}"


@dataclass(frozen=True)
class Chunk:
    name: str
    session_id: str
    seq: int
    ext: str
    size: int = 0


@dataclass
class Session:
    session_id: str
    mode: str
    started_ms: int
    chunks: list[Chunk] = field(default_factory=list)


def parse_object(name: str, size: int = 0) -> Chunk | None:
    m = _OBJECT_RE.match(name)
    if not m or not SESSION_ID_RE.match(m["session"]):
        return None
    return Chunk(name=name, session_id=m["session"], seq=int(m["seq"]), ext=m["ext"], size=size)


def group_sessions(chunks: list[Chunk]) -> list[Session]:
    """チャンクをセッションごとにまとめ、録音順（開始時刻→連番）に並べる。"""
    sessions: dict[str, Session] = {}
    for chunk in chunks:
        m = SESSION_ID_RE.match(chunk.session_id)
        if m is None:
            continue
        s = sessions.setdefault(chunk.session_id, Session(chunk.session_id, m["mode"], int(m["started"])))
        s.chunks.append(chunk)
    ordered = sorted(sessions.values(), key=lambda s: (s.started_ms, s.session_id))
    for s in ordered:
        s.chunks.sort(key=lambda c: c.seq)
    return ordered
