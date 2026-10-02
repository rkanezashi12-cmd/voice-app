"""録音アプリ（/app/）のログインの Cookie と、ログイン途中の state の署名。

形式は録音 URL のトークン（app/recording_token.py）と同じ: base64url(JSON) + "." + base64url(HMAC-SHA256)。
サーバーには何も保存しない（GCP に DB を持たない）。Cookie は HttpOnly・Secure・SameSite=Lax で渡す。
用途の取り違え（state を Cookie に使うなど）を防ぐため、本文に種類（"k"）を入れて照合する。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from dataclasses import dataclass
from typing import Any

from app.recording_token import CLIENT_ID_RE

SESSION_COOKIE = "mn_session"
STATE_COOKIE = "mn_login_state"
MAX_TOKEN_LENGTH = 2048
_KIND_SESSION = "s"
_KIND_STATE = "l"


class SessionError(Exception):
    """Cookie・state が不正か期限切れ。"""


@dataclass(frozen=True)
class AppSession:
    client_id: str
    user_id: str
    name: str
    email: str
    expires_at: int
    # 最後に「CRM の有効なユーザー」と確かめた時刻（ログインした時刻。その後は app/deps.py が12時間ごとに確かめ直す）
    checked_at: int = 0


def _b64e(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64d(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def _sign(secret: bytes, body: str) -> str:
    return _b64e(hmac.new(secret, body.encode("ascii"), hashlib.sha256).digest())


def _seal(secret: bytes, payload: dict[str, Any]) -> str:
    body = _b64e(json.dumps(payload, separators=(",", ":")).encode("ascii"))
    return f"{body}.{_sign(secret, body)}"


def peek_client_id(token: str) -> str:
    """署名を確かめる前に、どのクライアントの鍵で確かめるかを読む（値は検証後にしか信用しない）。"""
    if not token or len(token) > MAX_TOKEN_LENGTH:
        raise SessionError("形式が不正です")
    try:
        payload = json.loads(_b64d(token.partition(".")[0]))
    except (ValueError, UnicodeDecodeError) as exc:
        raise SessionError("形式が不正です") from exc
    client_id = payload.get("c") if isinstance(payload, dict) else None
    if not isinstance(client_id, str) or not CLIENT_ID_RE.match(client_id):
        raise SessionError("形式が不正です")
    return client_id


def _open(secret: bytes, token: str, kind: str, now: float) -> dict[str, Any]:
    if not token or len(token) > MAX_TOKEN_LENGTH:
        raise SessionError("形式が不正です")
    body, sep, signature = token.partition(".")
    if not sep or not body or not signature:
        raise SessionError("形式が不正です")
    if not hmac.compare_digest(_sign(secret, body), signature):
        raise SessionError("署名が一致しません")
    try:
        payload = json.loads(_b64d(body))
    except (ValueError, UnicodeDecodeError) as exc:
        raise SessionError("形式が不正です") from exc
    if not isinstance(payload, dict) or payload.get("v") != 1 or payload.get("k") != kind:
        raise SessionError("種類が違います")
    expires_at = payload.get("e")
    if not isinstance(expires_at, int) or now >= expires_at:
        raise SessionError("期限が切れています")
    return payload


def issue_session(secret: bytes, session: AppSession) -> str:
    if not CLIENT_ID_RE.match(session.client_id):
        raise ValueError("client_id の形式が不正です")
    return _seal(
        secret,
        {
            "v": 1,
            "k": _KIND_SESSION,
            "c": session.client_id,
            "u": session.user_id,
            "n": session.name[:100],
            "m": session.email[:200],
            "e": int(session.expires_at),
            "t": int(session.checked_at),
        },
    )


def verify_session(secret: bytes, token: str, *, now: float) -> AppSession:
    payload = _open(secret, token, _KIND_SESSION, now)
    client_id, user_id = payload.get("c"), payload.get("u")
    if not isinstance(client_id, str) or not CLIENT_ID_RE.match(client_id):
        raise SessionError("内容が不正です")
    if not isinstance(user_id, str) or not user_id.isdigit():
        raise SessionError("内容が不正です")
    return AppSession(
        client_id=client_id,
        user_id=user_id,
        name=str(payload.get("n") or ""),
        email=str(payload.get("m") or ""),
        expires_at=payload["e"],
        # 確かめた時刻の無い Cookie（前の版で発行したもの）は、次の操作で確かめ直す
        checked_at=payload["t"] if isinstance(payload.get("t"), int) else 0,
    )


def issue_state(secret: bytes, *, client_id: str, nonce: str, expires_at: int) -> str:
    """Zoho のログイン画面に渡す state。同じ nonce を Cookie にも置き、戻ってきたときに突き合わせる。"""
    if not CLIENT_ID_RE.match(client_id):
        raise ValueError("client_id の形式が不正です")
    return _seal(secret, {"v": 1, "k": _KIND_STATE, "c": client_id, "o": nonce, "e": int(expires_at)})


def verify_state(secret: bytes, token: str, *, nonce: str, now: float) -> str:
    """state を確かめ、クライアント ID を返す。Cookie の nonce と一致しなければ拒否する。"""
    payload = _open(secret, token, _KIND_STATE, now)
    expected = payload.get("o")
    if not isinstance(expected, str) or not nonce or not hmac.compare_digest(expected, nonce):
        raise SessionError("ログインの途中の情報が一致しません")
    client_id = payload.get("c")
    if not isinstance(client_id, str) or not CLIENT_ID_RE.match(client_id):
        raise SessionError("内容が不正です")
    return client_id
