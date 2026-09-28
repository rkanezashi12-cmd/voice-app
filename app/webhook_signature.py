"""Recall.ai の Webhook 署名検証（Svix 形式）。

ヘッダー: webhook-id / webhook-timestamp / webhook-signature（旧名 svix-id / svix-timestamp / svix-signature も受ける）
署名対象: "<webhook-id>.<webhook-timestamp>.<本文>" を、シークレット（whsec_ の後ろを base64 デコードしたもの）で
HMAC-SHA256 し base64 にしたもの。webhook-signature は "v1,<署名>" を空白区切りで複数持てる。
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import time
from collections.abc import Mapping

DEFAULT_TOLERANCE_SECONDS = 300


class SignatureError(Exception):
    pass


def _header(headers: Mapping[str, str], name: str) -> str | None:
    return headers.get(f"webhook-{name}") or headers.get(f"svix-{name}")


def _key(secret: str) -> bytes:
    raw = secret.strip()
    if raw.startswith("whsec_"):
        raw = raw[len("whsec_") :]
    try:
        return base64.b64decode(raw, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise SignatureError("Webhook シークレットの形式が不正です") from exc


def sign(secret: str, msg_id: str, timestamp: int, body: bytes) -> str:
    signed = f"{msg_id}.{timestamp}.".encode() + body
    return "v1," + base64.b64encode(hmac.new(_key(secret), signed, hashlib.sha256).digest()).decode()


def verify(
    secret: str,
    headers: Mapping[str, str],
    body: bytes,
    *,
    tolerance: int = DEFAULT_TOLERANCE_SECONDS,
    now: float | None = None,
) -> str:
    """署名を検証し、webhook-id を返す（重複排除のキーに使う）。"""
    msg_id = _header(headers, "id")
    timestamp = _header(headers, "timestamp")
    signatures = _header(headers, "signature")
    if not msg_id or not timestamp or not signatures:
        raise SignatureError("署名ヘッダーがありません")
    try:
        ts = int(timestamp)
    except ValueError as exc:
        raise SignatureError("タイムスタンプが不正です") from exc
    current = time.time() if now is None else now
    if abs(current - ts) > tolerance:
        raise SignatureError("タイムスタンプが古すぎるか新しすぎます")
    expected = sign(secret, msg_id, ts, body).split(",", 1)[1]
    for part in signatures.split(" "):
        version, _, value = part.partition(",")
        if version == "v1" and hmac.compare_digest(value, expected):
            return msg_id
    raise SignatureError("署名が一致しません")
