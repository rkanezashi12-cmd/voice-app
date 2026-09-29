#!/usr/bin/env bash
# 録音のあとの処理（Cloud Tasks → 文字起こし・補正・要約 → CRM の更新）を動かすための GCP の設定
# （Cloud Shell で実行する想定）。
#
#   cd ~/voice-app && bash scripts/setup_processing.sh
#
# 行うこと（何度実行しても壊れない。CRM には何も書き込まない。DRY_RUN は変えない）:
#   1. Cloud Tasks と Vertex AI の API を有効にする
#   2. 処理の順番待ち（Cloud Tasks のキュー）を作る
#   3. キューが Cloud Run を呼ぶためのサービスアカウントを作り、Cloud Run のサービスアカウントに
#      「タスクを積む」「Gemini を呼ぶ」権限を付ける
#   4. 東京リージョン（asia-northeast1）の Gemini を実際に呼んで、使えるモデルを確かめる
#   5. 使うモデルを選び、Cloud Run の環境変数（TASKS_* / GEMINI_MODEL_*）を設定する
#   6. （任意）テスト用の商談記録で、録音後の処理まで動く録音URLを発行する
set -euo pipefail
cd "$(dirname "$0")/.."

PROJECT="${PROJECT:-voice-ai-510014}"
REGION="asia-northeast1"
SERVICE="meeting-notes"
RUN_SA="meeting-notes-run@${PROJECT}.iam.gserviceaccount.com"
QUEUE="meeting-notes-process"
TASKS_SA_NAME="meeting-notes-tasks"
TASKS_SA="${TASKS_SA_NAME}@${PROJECT}.iam.gserviceaccount.com"
MAX_ATTEMPTS=3 # アプリの TASKS_MAX_ATTEMPTS と揃える
# Model Garden の一覧が取れないときに試すモデル（実際に呼べたものだけを使う）
FALLBACK_MODELS="gemini-3.8-flash gemini-3.5-flash gemini-3-flash gemini-2.5-flash gemini-3.8-pro gemini-3.5-pro gemini-3-pro gemini-2.5-pro"

step() { printf '\n==== %s\n' "$*"; }
fail() {
  printf 'エラー: %s\n' "$*" >&2
  exit 1
}
# 作ったばかりのサービスアカウントは、権限を付けられるまで少しかかることがある
retry() {
  local i
  for i in 1 2 3 4 5 6; do
    "$@" >/dev/null 2>&1 && return 0
    sleep 5
  done
  "$@" >/dev/null
}

step "0. 事前確認"
gcloud config set project "$PROJECT" >/dev/null
SERVICE_URL="$(gcloud run services describe "$SERVICE" --region="$REGION" --format='value(status.url)' 2>/dev/null)" ||
  fail "Cloud Run のサービス ${SERVICE} が見つかりません（先に scripts/setup_recorder_test.sh を実行）"
echo "プロジェクト: ${PROJECT} / サービス: ${SERVICE_URL}"

step "1. API の有効化（Cloud Tasks・Vertex AI）"
gcloud services enable cloudtasks.googleapis.com aiplatform.googleapis.com

step "2. Cloud Tasks のキュー ${QUEUE}"
QUEUE_FLAGS=(--location="$REGION" --max-attempts="$MAX_ATTEMPTS" --min-backoff=30s --max-backoff=300s
  --max-concurrent-dispatches=5)
if gcloud tasks queues describe "$QUEUE" --location="$REGION" >/dev/null 2>&1; then
  gcloud tasks queues update "$QUEUE" "${QUEUE_FLAGS[@]}" >/dev/null
else
  gcloud tasks queues create "$QUEUE" "${QUEUE_FLAGS[@]}" >/dev/null
fi
echo "キュー: ${QUEUE}（最大 ${MAX_ATTEMPTS} 回試行）"

step "3. サービスアカウントと権限"
if ! gcloud iam service-accounts describe "$TASKS_SA" >/dev/null 2>&1; then
  gcloud iam service-accounts create "$TASKS_SA_NAME" --display-name="meeting-notes (Cloud Tasks -> Cloud Run)"
fi
# キュー（TASKS_SA の OIDC トークン付き）→ Cloud Run
retry gcloud run services add-iam-policy-binding "$SERVICE" --region="$REGION" \
  --member="serviceAccount:${TASKS_SA}" --role=roles/run.invoker
# Cloud Run（アプリ）がタスクを積み、そのタスクに TASKS_SA のトークンを付ける
retry gcloud projects add-iam-policy-binding "$PROJECT" --condition=None \
  --member="serviceAccount:${RUN_SA}" --role=roles/cloudtasks.enqueuer
retry gcloud iam service-accounts add-iam-policy-binding "$TASKS_SA" \
  --member="serviceAccount:${RUN_SA}" --role=roles/iam.serviceAccountUser
# Cloud Run（アプリ）が Gemini を呼ぶ
retry gcloud projects add-iam-policy-binding "$PROJECT" --condition=None \
  --member="serviceAccount:${RUN_SA}" --role=roles/aiplatform.user
echo "権限を付けました（${TASKS_SA} / ${RUN_SA}）"

step "4. 東京リージョン（${REGION}）で使える Gemini のモデル（小さな問い合わせを実際に送って確かめます）"
TOKEN="$(gcloud auth print-access-token)"
# Model Garden の一覧（v1beta1 publishers.models.list。取れなければ FALLBACK_MODELS を使う）
LISTED="$(curl -sS --max-time 30 -H "Authorization: Bearer ${TOKEN}" -H "x-goog-user-project: ${PROJECT}" \
  "https://${REGION}-aiplatform.googleapis.com/v1beta1/publishers/google/models?pageSize=300" 2>/dev/null |
  python3 -c '
import json, re, sys
try:
    data = json.load(sys.stdin)
except ValueError:
    sys.exit(0)
skip = re.compile(r"embedding|image|tts|live|native-audio|computer-use|robotics|preview|exp")
for m in data.get("publisherModels", []):
    name = m.get("name", "").rsplit("/", 1)[-1]
    if re.fullmatch(r"gemini-[0-9][0-9a-z.-]*", name) and not skip.search(name):
        print(name)
' || true)"
CANDIDATES="$(printf '%s\n' $LISTED $FALLBACK_MODELS | awk 'NF && !seen[$0]++ && ++n <= 20')"
AVAILABLE=()
for m in $CANDIDATES; do
  code="$(curl -sS --max-time 60 -o /dev/null -w '%{http_code}' -X POST \
    -H "Authorization: Bearer ${TOKEN}" -H 'Content-Type: application/json' \
    "https://${REGION}-aiplatform.googleapis.com/v1/projects/${PROJECT}/locations/${REGION}/publishers/google/models/${m}:generateContent" \
    -d '{"contents":[{"role":"user","parts":[{"text":"OK"}]}],"generationConfig":{"maxOutputTokens":64}}' || true)"
  code="${code:-000}"
  if [ "$code" = 200 ]; then
    printf '  %-28s 使える\n' "$m"
    AVAILABLE+=("$m")
  else
    printf '  %-28s 使えない（HTTP %s）\n' "$m" "$code"
  fi
done
[ ${#AVAILABLE[@]} -gt 0 ] || fail "東京リージョンで使える Gemini のモデルが見つかりません。上の結果を送ってください"

step "5. 使うモデルを選ぶ（そのまま Enter で [ ] の中のモデル）"
SUGGEST="$(printf '%s\n' "${AVAILABLE[@]}" | python3 -c '
import re, sys
names = [line.strip() for line in sys.stdin if line.strip()]
def version(name):
    m = re.fullmatch(r"gemini-([0-9]+(?:\.[0-9]+)?)-flash", name)
    return float(m.group(1)) if m else -1.0
best = max(names, key=version)
print(best if version(best) >= 0 else names[0])
')"
pick() {
  local __var=$1 __label=$2 __value=""
  while true; do
    read -rp "${__label} [${SUGGEST}]: " __value
    __value="${__value:-$SUGGEST}"
    for m in "${AVAILABLE[@]}"; do
      if [ "$m" = "$__value" ]; then
        printf -v "$__var" '%s' "$__value"
        return 0
      fi
    done
    echo "  上の「使える」モデルから選んでください"
  done
}
pick MODEL_TRANSCRIBE "文字起こし（音声）に使うモデル"
pick MODEL_TEXT "補正・要約に使うモデル"
gcloud run services update "$SERVICE" --region="$REGION" --quiet \
  --update-env-vars="TASKS_QUEUE=${QUEUE},TASKS_INVOKER_SA=${TASKS_SA},TASKS_MAX_ATTEMPTS=${MAX_ATTEMPTS},GEMINI_MODEL_TRANSCRIBE=${MODEL_TRANSCRIBE},GEMINI_MODEL_TEXT=${MODEL_TEXT}"
curl -fsS "${SERVICE_URL}/health"
echo
echo "設定しました: 文字起こし ${MODEL_TRANSCRIBE} / 補正・要約 ${MODEL_TEXT}（DRY_RUN は変えていません）"

step "6. （任意）処理まで動く録音URLを発行する"
read -rp "テスト用の商談記録の ID（飛ばすときは空のまま Enter）: " RECORD_ID
if [ -n "$RECORD_ID" ]; then
  bash scripts/issue_process_url.sh "$RECORD_ID"
fi
