"""Cloud Shell 用スクリプトの中の Python（Cloud Run のクライアント設定を組み立てる部分）が、アプリの読める設定を作ること。"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from app.clients import ClientRegistry

ROOT = Path(__file__).resolve().parent.parent


def _embedded_python(script: str, start: str) -> str:
    """スクリプトの start の行から始まるヒアドキュメント（<<'PY' … PY）の中身を取り出す。"""
    text = (ROOT / "scripts" / script).read_text(encoding="utf-8")
    block = text.split(start, 1)[1]
    return block.split("<<'PY'\n", 1)[1].split("\nPY\n", 1)[0]


def _service(clients: dict) -> str:
    env = [{"name": "CLIENTS_CONFIG_JSON", "value": json.dumps(clients)}]
    return json.dumps({"spec": {"template": {"spec": {"containers": [{"env": env}]}}}})


def test_setup_recall_adds_recall_config_that_the_app_accepts() -> None:
    current = json.loads((ROOT / "config" / "clients.example.json").read_text(encoding="utf-8"))
    del current["clients"]["default"]["recall"]
    code = _embedded_python("setup_recall.sh", 'CONFIG_OUT="$(')
    result = subprocess.run(  # noqa: S603 リポジトリ内のスクリプトの一部を実行する
        [sys.executable, "-c", code, "default", "projects/p/secrets", "https://ap-northeast-1.recall.ai"],
        env={**os.environ, "SVC_JSON": _service(current), "BOT_NAME": "議事録 ボット（録音中）"},
        capture_output=True,
        text=True,
        check=True,
    )
    env_value, shown = result.stdout.splitlines()
    assert env_value.isascii(), "環境変数には ASCII だけで渡す"
    assert json.loads(shown) == json.loads(env_value)

    client = ClientRegistry.load(json_text=env_value).get("default")
    recall = client.need_recall()
    assert recall.base_url == "https://ap-northeast-1.recall.ai"
    assert recall.api_key == "sm:projects/p/secrets/recall-api-key/versions/latest"
    assert recall.webhook_secret == "sm:projects/p/secrets/recall-webhook-secret/versions/latest"
    assert recall.bot_name == "議事録 ボット（録音中）"
    assert recall.transcription_mode == "async"
    assert recall.transcript_request == {"provider": {"recallai_async": {"language_code": "ja"}}}
    assert client.zoho is not None, "既存の zoho の設定は残す"
