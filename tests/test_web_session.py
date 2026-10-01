"""録音アプリのログインの Cookie と、ログイン途中の state の署名。"""

from __future__ import annotations

import pytest

from app.web_session import (
    AppSession,
    SessionError,
    issue_session,
    issue_state,
    peek_client_id,
    verify_session,
    verify_state,
)

SECRET = b"session-secret"
NOW = 1_800_000_000


def session(**overrides: object) -> AppSession:
    values: dict[str, object] = {
        "client_id": "default",
        "user_id": "9001",
        "name": "金指 営業",
        "email": "sales@example.com",
        "expires_at": NOW + 3600,
    }
    values.update(overrides)
    return AppSession(**values)  # type: ignore[arg-type]


def test_session_round_trip_keeps_japanese_name() -> None:
    token = issue_session(SECRET, session())
    assert token.isascii()
    assert verify_session(SECRET, token, now=NOW) == session()
    assert peek_client_id(token) == "default"


def test_session_rejects_expired_tampered_and_other_keys() -> None:
    token = issue_session(SECRET, session())
    with pytest.raises(SessionError):
        verify_session(SECRET, token, now=NOW + 3600)
    with pytest.raises(SessionError):
        verify_session(b"other-secret", token, now=NOW)
    body, _, sig = token.partition(".")
    with pytest.raises(SessionError):
        verify_session(SECRET, f"{body}x.{sig}", now=NOW)
    with pytest.raises(SessionError):
        verify_session(SECRET, "", now=NOW)


def test_state_cannot_be_used_as_session_and_needs_matching_nonce() -> None:
    state = issue_state(SECRET, client_id="default", nonce="nonce-1", expires_at=NOW + 600)
    assert verify_state(SECRET, state, nonce="nonce-1", now=NOW) == "default"
    with pytest.raises(SessionError):
        verify_state(SECRET, state, nonce="nonce-2", now=NOW)
    with pytest.raises(SessionError):
        verify_state(SECRET, state, nonce="", now=NOW)
    with pytest.raises(SessionError):
        verify_session(SECRET, state, now=NOW)
    with pytest.raises(SessionError):
        verify_state(SECRET, issue_session(SECRET, session()), nonce="nonce-1", now=NOW)


def test_bad_client_ids_are_rejected() -> None:
    with pytest.raises(ValueError):
        issue_session(SECRET, session(client_id="Bad_ID"))
    with pytest.raises(SessionError):
        peek_client_id("not-a-token")
