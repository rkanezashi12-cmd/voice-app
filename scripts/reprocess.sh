#!/usr/bin/env bash
# 録音済みの音声で、録音後の処理（文字起こし・補正・要約・CRM の更新）をもう一度動かす（Cloud Shell で実行する想定）。
# 設定を直したあと、録音し直さずに確かめるときに使う。CRM への書き込みはバックエンドの DRY_RUN に従う。
# 音声は GCS に1日だけ残る（DRY_RUN=false で処理が終わると消える）。
#
#   cd ~/voice-app && bash scripts/reprocess.sh <商談記録の ID>
set -euo pipefail

PROJECT="${PROJECT:-voice-ai-510014}"
REGION="asia-northeast1"
SERVICE="meeting-notes"
RECORD_ID="${1:-}"
CLIENT_ID="${CLIENT_ID:-default}"
fail() {
  printf 'エラー: %s\n' "$*" >&2
  exit 1
}
[[ "$RECORD_ID" =~ ^[0-9]+$ ]] || fail "使い方: bash scripts/reprocess.sh <商談記録の ID（数字）>"

# アプリと同じ設定（キュー・トークンの発行元・宛先）を Cloud Run から読む
SVC_JSON="$(gcloud run services describe "$SERVICE" --project="$PROJECT" --region="$REGION" --format=json)"
read -r SERVICE_URL QUEUE INVOKER < <(SVC_JSON="$SVC_JSON" python3 -c '
import json, os
svc = json.loads(os.environ["SVC_JSON"])
env = {e.get("name"): e.get("value") for e in svc["spec"]["template"]["spec"]["containers"][0].get("env", [])}
print(env.get("SERVICE_URL") or "-", env.get("TASKS_QUEUE") or "-", env.get("TASKS_INVOKER_SA") or "-")
')
[ "$QUEUE" != - ] && [ "$INVOKER" != - ] && [ "$SERVICE_URL" != - ] ||
  fail "Cloud Run に TASKS_QUEUE / TASKS_INVOKER_SA / SERVICE_URL がありません（先に scripts/setup_processing.sh を実行）"

gcloud tasks create-http-task --project="$PROJECT" --queue="$QUEUE" --location="$REGION" \
  --url="${SERVICE_URL}/internal/process" --method=POST \
  --header="Content-Type: application/json" \
  --body-content="{\"client_id\":\"${CLIENT_ID}\",\"source\":\"web_recording\",\"record_id\":\"${RECORD_ID}\"}" \
  --oidc-service-account-email="$INVOKER" --oidc-token-audience="$SERVICE_URL" >/dev/null
echo "処理を積みました（商談記録 ${RECORD_ID}）。数分後に bash scripts/process_logs.sh で結果を確かめてください。"
