"""Recall.ai の API キーの確認と、ボット・録音の応答の形の表示（Cloud Shell で実行する想定）。

標準ライブラリだけで動く。読み取り（GET）だけを行い、ボットの作成・削除はしない。
表示するのは項目の名前（パス）と、ID・状態・日時・数値の値だけ。会議 URL・署名付き URL・参加者名・発言などの
文字列は「<文字列 N文字>」「<URL・非表示>」に置き換える。
docs/unverified-apis.md の H2〜H8 を、公式ドキュメントの代わりに実物の応答で確かめるために使う。

使い方（詳しくは docs/recall-bot.md。ふだんは scripts/setup_recall.sh・scripts/recall_inspect.sh から呼ぶ）:
    python3 scripts/recall_check.py check-key            # API キーとリージョンが合っているか（HTTP の結果だけ）
    python3 scripts/recall_check.py bot <ボット ID>      # ボットの応答の形
    python3 scripts/recall_check.py recording <録音 ID>  # 録音の応答の形（文字起こしの状態を含む）

必要な環境変数:
    RECALL_API_KEY    Recall.ai の API キー（リージョンごとに別）
    RECALL_BASE_URL   例 https://ap-northeast-1.recall.ai（既定値は持たない）
"""

from __future__ import annotations

import json
import os
import re
import sys
import urllib.error
import urllib.request
from typing import Any

# 値を表示してよい項目（ID・状態・日時・種類だけ。人名・発言・URL は含めない）
SAFE_KEYS = frozenset(
    {
        "id",
        "code",
        "sub_code",
        "status",
        "state",
        "event",
        "kind",
        "type",
        "mode",
        "format",
        "platform",
        "provider",
        "language_code",
        "created_at",
        "updated_at",
        "started_at",
        "completed_at",
        "ended_at",
        "join_at",
        "expires_at",
        "client_id",
        "record_id",
        "owner_id",
        "bot_id",
        "recording_id",
        "sdk_upload_id",
    }
)
MAX_LIST_ITEMS = 20
MAX_DEPTH = 10
_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,100}$")


def _is_url_key(key: str) -> bool:
    k = key.lower()
    return k == "url" or k.endswith("_url")


def _scalar(key: str, value: Any) -> str:
    if value is None or isinstance(value, bool | int | float):
        return json.dumps(value)
    if isinstance(value, str):
        if _is_url_key(key) or value.startswith(("http://", "https://")):
            return "<URL・非表示>"
        if key in SAFE_KEYS:
            return json.dumps(value[:80], ensure_ascii=False)
        return f"<文字列 {len(value)}文字>"
    return f"<{type(value).__name__}>"


def shape(value: Any, path: str = "", depth: int = 0) -> list[str]:
    """応答を「パス = 値」の行にする。値は SAFE_KEYS の文字列と数値・真偽値だけを出す。"""
    if depth > MAX_DEPTH:
        return [f"{path} = <深すぎるため省略>"]
    if isinstance(value, dict):
        if not value:
            return [f"{path or '(本体)'} = {{}}"]
        lines: list[str] = []
        for key in sorted(value, key=str):
            child = f"{path}.{key}" if path else str(key)
            lines.extend(shape(value[key], child, depth + 1))
        return lines
    if isinstance(value, list):
        lines = [f"{path} = [{len(value)}件]"]
        for index, item in enumerate(value[:MAX_LIST_ITEMS]):
            lines.extend(shape(item, f"{path}[{index}]", depth + 1))
        if len(value) > MAX_LIST_ITEMS:
            lines.append(f"{path}[{MAX_LIST_ITEMS}〜] = <省略>")
        return lines
    key = path.rsplit(".", 1)[-1].split("[", 1)[0]
    return [f"{path} = {_scalar(key, value)}"]


def _env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise SystemExit(f"エラー: 環境変数 {name} がありません")
    return value


def get(path: str) -> tuple[int, Any]:
    """GET して (HTTP ステータス, 本文の JSON) を返す。本文は呼び出し側が形だけを表示する。"""
    base = _env("RECALL_BASE_URL").rstrip("/")
    if not base.startswith("https://"):
        raise SystemExit("エラー: RECALL_BASE_URL は https:// で始めてください")
    req = urllib.request.Request(
        f"{base}/api/v1{path}",
        headers={
            "Authorization": f"Token {_env('RECALL_API_KEY')}",
            "Accept": "application/json",
            "User-Agent": "meeting-notes-setup/1.0",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            status, raw = resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        status, raw = exc.code, exc.read()
    except urllib.error.URLError as exc:
        raise SystemExit(f"エラー: Recall.ai に接続できません（{exc.reason}）") from exc
    try:
        return status, json.loads(raw) if raw else None
    except ValueError:
        return status, None


def check_key() -> int:
    status, _ = get("/bot/")
    if status == 200:
        print(f"OK: API キーは使えます（HTTP 200、{os.environ['RECALL_BASE_URL']}）")
        return 0
    if status in (401, 403):
        print(
            f"NG: HTTP {status}。API キーが違うか、別のリージョンのキーです"
            "（東京リージョン ap-northeast-1 のダッシュボードで作ったキーを使う）"
        )
        return 1
    print(f"NG: HTTP {status}（想定外の応答。この表示を送ってください）")
    return 1


def show(kind: str, object_id: str) -> int:
    if not _ID_RE.match(object_id):
        raise SystemExit("エラー: ID の形式が不正です（英数字・ハイフン・アンダースコアだけ）")
    status, body = get(f"/{kind}/{object_id}/")
    print(f"GET /api/v1/{kind}/<ID>/ → HTTP {status}")
    if body is not None:
        for line in shape(body):
            print(f"  {line}")
    return 0 if status == 200 else 1


def main(argv: list[str]) -> int:
    if len(argv) == 1 and argv[0] == "check-key":
        return check_key()
    if len(argv) == 2 and argv[0] in ("bot", "recording"):
        return show(argv[0], argv[1])
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
