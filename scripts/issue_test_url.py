#!/usr/bin/env python3
"""iPhone テスト用の録音 URL を発行する（CRM は使わない）。

テスト用トークンで開いた録音ページは、CRM に何も書かず、録音完了後も文字起こしを動かさない。
画面にテスト設定（分割方式・分割の長さ）と診断結果が出る。

使い方:
  export RECORDING_TOKEN_SECRET="$(gcloud secrets versions access latest \\
      --secret=recording-token-secret --project=voice-ai-510014)"
  python3 scripts/issue_test_url.py --service-url https://<Cloud Run の URL>

標準ライブラリだけで動く（依存パッケージのインストールは不要）。
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.recording_token import issue


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--service-url", required=True, help="Cloud Run の URL（https://...）")
    parser.add_argument("--client-id", default="default", help="クライアント ID（既定: default）")
    parser.add_argument("--record-id", help="記録 ID（既定: test-<日時>）")
    parser.add_argument("--hours", type=int, default=24, help="有効時間（既定: 24）")
    args = parser.parse_args()

    secret = os.environ.get("RECORDING_TOKEN_SECRET")
    if not secret:
        print("環境変数 RECORDING_TOKEN_SECRET を設定してください（使い方は --help）", file=sys.stderr)
        return 2
    record_id = args.record_id or f"test-{datetime.now():%Y%m%d-%H%M%S}"
    token = issue(
        secret.encode(),
        client_id=args.client_id,
        record_id=record_id,
        expires_at=int(time.time()) + args.hours * 3600,
        test=True,
    )
    print(f"{args.service_url.rstrip('/')}/recorder/#{token}")
    print(f"記録 ID: {record_id}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
