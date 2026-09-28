"""対面録音ページの署名トークン。

標準ライブラリだけで書いている（scripts/issue_test_url.py から依存なしで使うため）。
形式: base64url(JSON) + "." + base64url(HMAC-SHA256)
トークンは録音ページ URL のフラグメント（#以降）に入れる。フラグメントはサーバーに送られないので
Cloud Run のリクエストログに残らない。ページの JS が Authorization ヘッダーで API に渡す。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import time
from dataclasses import dataclass

MAX_TOKEN_LENGTH = 512
CLIENT_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")
RECORD_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


class TokenError(Exception):
    """トークンが不正・期限切れ。"""


@dataclass(frozen=True)
class RecordingToken:
    client_id: str
    record_id: str
    expires_at: int
    test: bool = False


def _b64e(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64d(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def _sign(secret: bytes, body: str) -> str:
    return _b64e(hmac.new(secret, body.encode("ascii"), hashlib.sha256).digest())


def issue(secret: bytes, *, client_id: str, record_id: str, expires_at: int, test: bool = False) -> str:
    if not CLIENT_ID_RE.match(client_id):
        raise ValueError("client_id の形式が不正です")
    if not RECORD_ID_RE.match(record_id):
        raise ValueError("record_id の形式が不正です")
    payload: dict[str, object] = {"v": 1, "c": client_id, "r": record_id, "e": int(expires_at)}
    if test:
        payload["t"] = 1
    body = _b64e(json.dumps(payload, separators=(",", ":")).encode("ascii"))
    return f"{body}.{_sign(secret, body)}"


def verify(secret: bytes, token: str, *, now: float | None = None) -> RecordingToken:
    if not token or len(token) > MAX_TOKEN_LENGTH:
        raise TokenError("トークンの形式が不正です")
    body, sep, signature = token.partition(".")
    if not sep or not body or not signature:
        raise TokenError("トークンの形式が不正です")
    if not hmac.compare_digest(_sign(secret, body), signature):
        raise TokenError("トークンの署名が一致しません")
    try:
        payload = json.loads(_b64d(body))
    except (ValueError, UnicodeDecodeError) as exc:
        raise TokenError("トークンの形式が不正です") from exc
    if not isinstance(payload, dict) or payload.get("v") != 1:
        raise TokenError("トークンの版が不正です")
    client_id, record_id, expires_at = payload.get("c"), payload.get("r"), payload.get("e")
    if not isinstance(client_id, str) or not CLIENT_ID_RE.match(client_id):
        raise TokenError("トークンの内容が不正です")
    if not isinstance(record_id, str) or not RECORD_ID_RE.match(record_id):
        raise TokenError("トークンの内容が不正です")
    if not isinstance(expires_at, int):
        raise TokenError("トークンの内容が不正です")
    if (time.time() if now is None else now) >= expires_at:
        raise TokenError("録音 URL の有効期限が切れています")
    return RecordingToken(client_id, record_id, expires_at, bool(payload.get("t")))


def compute_expiry(*, now: float, start_at: float | None, ttl_hours: int) -> int:
    """有効期限 = max(発行時刻, 商談開始日時) + ttl。前日に作った記録でも当日使えるようにする。"""
    base = max(now, start_at) if start_at is not None else now
    return int(base + ttl_hours * 3600)
