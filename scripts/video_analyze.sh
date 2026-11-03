#!/usr/bin/env bash
# GCS の videos/<クライアント>/ に置いた動画を解析する（GitHub Actions の ops から実行する想定。Cloud Shell でも動く）。
# 動画ごとに商談記録を1件作る（docs/video-analysis.md）。CRM への書き込みはクライアントの DRY_RUN に従う。
#
#   CLIENT_ID=monodemo bash scripts/video_analyze.sh [本数（0 または省略で全部）]
#   FORCE=1 CLIENT_ID=monodemo bash scripts/video_analyze.sh   # 解析済み（完了）の動画もやり直す
#
# 出すのは件数と状態だけ（動画のファイル名は出さない。GitHub Actions のログは公開されるため）。
set -euo pipefail

PROJECT="${PROJECT:-voice-ai-510014}"
REGION="asia-northeast1"
SERVICE="meeting-notes"
CLIENT_ID="${CLIENT_ID:-}"
LIMIT="${1:-0}"
FORCE_JSON=false
[ "${FORCE:-}" = 1 ] && FORCE_JSON=true
fail() {
  printf 'エラー: %s\n' "$*" >&2
  exit 1
}
[[ "$CLIENT_ID" =~ ^[a-z0-9][a-z0-9-]{0,31}$ ]] || fail "使い方: CLIENT_ID=<クライアント> bash scripts/video_analyze.sh [本数]"
[[ "$LIMIT" =~ ^[0-9]{1,3}$ ]] || fail "本数は数字で入れてください（0 は全部）"

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
  --url="${SERVICE_URL}/internal/video-batch" --method=POST \
  --header="Content-Type: application/json" \
  --body-content="{\"client_id\":\"${CLIENT_ID}\",\"limit\":${LIMIT},\"force\":${FORCE_JSON}}" \
  --oidc-service-account-email="$INVOKER" --oidc-token-audience="$SERVICE_URL" >/dev/null
echo "動画の解析を積みました（クライアント ${CLIENT_ID}・本数 $([ "$LIMIT" = 0 ] && echo 全部 || echo "$LIMIT")）。"
echo "動画ごとに45秒ずつずらして始めます。1本（30分）あたり数分〜10分ほどで、商談記録の「動画解析 <ファイル名>」に結果が入ります。"
