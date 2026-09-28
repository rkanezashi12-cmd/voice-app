"""アプリ共通の例外。

メッセージは CRM の error_message やログに出るため、文字起こし本文・要約・秘密情報を含めないこと。
"""

from __future__ import annotations


class AppError(Exception):
    """アプリの例外の基底。retryable=True なら Cloud Tasks の再試行に任せる。"""

    retryable: bool = False
    code: str = "app_error"

    def __init__(self, message: str, *, code: str | None = None, retryable: bool | None = None) -> None:
        super().__init__(message)
        self.message = message
        if code is not None:
            self.code = code
        if retryable is not None:
            self.retryable = retryable


class ConfigError(AppError):
    """設定値の不足・不正。再試行しても直らない。"""

    code = "config_error"


class PermanentError(AppError):
    """入力やデータの問題で、再試行しても直らないもの（無音で文字起こしが空、など）。"""

    code = "permanent_error"


class ExternalServiceError(AppError):
    """外部 API のエラー。429 / 5xx / 通信エラーは retryable。"""

    code = "external_error"

    def __init__(
        self,
        service: str,
        message: str,
        *,
        status: int | None = None,
        code: str | None = None,
        retryable: bool = False,
    ) -> None:
        detail = f"{service}: {message}"
        if status is not None:
            detail = f"{service} (HTTP {status}): {message}"
        super().__init__(detail[:500], code=code or "external_error", retryable=retryable)
        self.service = service
        self.status = status


class TranscriptNotReady(AppError):
    """Recall.ai の文字起こしがまだ終わっていない。少し待って処理をやり直す。"""

    code = "transcript_not_ready"
    retryable = True
