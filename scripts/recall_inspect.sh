#!/usr/bin/env bash
# Recall.ai のボット・録音の応答の「形」を表示する（Cloud Shell で実行する想定。読み取りだけ）。
# バックエンド（Cloud Run）と同じ API キー・接続先を使う。値は ID・状態・日時・数値だけを出し、
# 会議 URL・署名付き URL・参加者名・発言は出さない。
# 未確認の仕様（docs/unverified-apis.md）を実物の応答で確かめるときや、うまくいかないときの調査に使う。
#
#   cd ~/voice-app && bash scripts/recall_inspect.sh bot <ボット ID>        # 商談記録の「Recall ID」の値
#   cd ~/voice-app && bash scripts/recall_inspect.sh recording <録音 ID>    # 上の表示の recordings[0].id
set -euo pipefail
cd "$(dirname "$0")/.."

PROJECT="${PROJECT:-voice-ai-510014}"
REGION="asia-northeast1"
SERVICE="meeting-notes"
APP_CLIENT="${APP_CLIENT:-default}"
KIND="${1:-}"
OBJECT_ID="${2:-}"
fail() {
  printf 'エラー: %s\n' "$*" >&2
  exit 1
}
[[ "$KIND" == bot || "$KIND" == recording ]] && [[ "$OBJECT_ID" =~ ^[A-Za-z0-9_-]+$ ]] ||
  fail "使い方: bash scripts/recall_inspect.sh bot <ボット ID>  または  bash scripts/recall_inspect.sh recording <録音 ID>"

# sm:projects/<p>/secrets/<name>/versions/<v>  か  sm:projects/<p>/locations/<loc>/secrets/<name>/versions/<v>
access_ref() {
  local ref="${1#sm:}" name version location
  name="$(sed -n 's#.*/secrets/\([^/]*\)/versions/.*#\1#p' <<<"$ref")"
  version="${ref##*/versions/}"
  location="$(sed -n 's#^projects/[^/]*/locations/\([^/]*\)/secrets/.*#\1#p' <<<"$ref")"
  if [ -n "$location" ]; then
    gcloud secrets versions access "$version" --secret="$name" --project="$PROJECT" --location="$location"
  else
    gcloud secrets versions access "$version" --secret="$name" --project="$PROJECT"
  fi
}

SVC_JSON="$(gcloud run services describe "$SERVICE" --project="$PROJECT" --region="$REGION" --format=json)"
read -r BASE_URL KEY_REF < <(SVC_JSON="$SVC_JSON" python3 -c '
import json, os, sys
svc = json.loads(os.environ["SVC_JSON"])
env = {e.get("name"): e.get("value") for e in svc["spec"]["template"]["spec"]["containers"][0].get("env", [])}
recall = json.loads(env.get("CLIENTS_CONFIG_JSON") or "{}").get("clients", {}).get(sys.argv[1], {}).get("recall") or {}
print(recall.get("base_url") or "-", recall.get("api_key") or "-")
' "$APP_CLIENT")
[[ "$KEY_REF" == sm:* && "$BASE_URL" == https://* ]] ||
  fail "Cloud Run のクライアント設定（${APP_CLIENT}）に recall がありません（先に scripts/setup_recall.sh を実行）"

RECALL_API_KEY="$(access_ref "$KEY_REF")"
RECALL_BASE_URL="$BASE_URL"
export RECALL_API_KEY RECALL_BASE_URL
python3 scripts/recall_check.py "$KIND" "$OBJECT_ID"
