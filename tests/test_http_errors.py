"""外部 API のエラーから作る例外：何をしていて失敗したかに、外部サービスが返した理由を添える（URL は伏せる）。"""

from __future__ import annotations

import httpx

from app.services.http import error_from_response


def _resp(status: int, body: object) -> httpx.Response:
    return httpx.Response(status, json=body, request=httpx.Request("POST", "https://example.invalid/x"))


def test_upstream_reason_is_added_to_the_message() -> None:
    body = {"code": "INVALID_MODULE", "message": "the module name given seems to be invalid"}
    exc = error_from_response("zoho_crm", _resp(400, body), "レコードの取得に失敗しました")
    assert exc.code == "INVALID_MODULE"
    assert exc.message == (
        "zoho_crm (HTTP 400): レコードの取得に失敗しました（the module name given seems to be invalid）"
    )
    assert exc.retryable is False


def test_field_errors_are_summarized_and_urls_are_hidden() -> None:
    """Recall.ai の項目ごとのエラーは項目名と最初の文だけを拾い、会議 URL（パスコードを含む）は伏せる。"""
    body = {
        "meeting_url": ["Could not parse https://zoom.us/j/123?pwd=secret"],
        "bot_name": ["Too long."],
        "join_at": ["Invalid."],
    }
    exc = error_from_response("recall", _resp(400, body), "ボットの予約に失敗しました")
    assert exc.message == (
        "recall (HTTP 400): ボットの予約に失敗しました（meeting_url: Could not parse <URL>、bot_name: Too long.）"
    )
    assert "pwd=secret" not in exc.message


def test_message_is_unchanged_without_an_upstream_reason() -> None:
    exc = error_from_response("recall", _resp(503, {}), "録音情報の取得に失敗しました")
    assert exc.message == "recall (HTTP 503): 録音情報の取得に失敗しました"
    assert exc.retryable is True
