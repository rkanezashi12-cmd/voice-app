#!/usr/bin/env bash
# 対面録音ページの iPhone テスト環境を用意する（Cloud Shell で実行する想定）。
#
#   git clone -b claude/meeting-transcription-app-setup-nkaz1m https://github.com/rkanezashi12-cmd/voice-app.git
#   cd voice-app && bash scripts/setup_recorder_test.sh
#
# 行うこと（何度実行しても壊れない）:
#   1. Cloud Build API の有効化（gcloud run deploy --source に必要）
#   2. 録音の一時保存バケット（1日で自動削除）
#   3. 実行用サービスアカウントと権限（バケット・署名・シークレットの読み取り）
#   4. Cloud Run へデプロイ（DRY_RUN=true。CRM には何も書かない）
#   5. サービス URL の設定、バケットの CORS、動作確認
#   6. テスト用の録音 URL を発行（CRM に書かない・文字起こしもしないテスト用）
set -euo pipefail

PROJECT="${PROJECT:-voice-ai-510014}"
REGION="asia-northeast1"
BUCKET="${PROJECT}-meeting-audio"
SERVICE="meeting-notes"
RUN_SA="meeting-notes-run@${PROJECT}.iam.gserviceaccount.com"
SECRETS=(backend-api-key recording-token-secret)

step() { printf '\n==== %s\n' "$*"; }

gcloud config set project "$PROJECT" >/dev/null

# シークレットが通常かリージョン シークレットかを判定する
if gcloud secrets describe recording-token-secret >/dev/null 2>&1; then
  LOC_FLAG=()
  REF_PREFIX="projects/${PROJECT}/secrets"
elif gcloud secrets describe recording-token-secret --location="$REGION" >/dev/null 2>&1; then
  LOC_FLAG=(--location="$REGION")
  REF_PREFIX="projects/${PROJECT}/locations/${REGION}/secrets"
else
  echo "シークレット recording-token-secret が見つかりません" >&2
  exit 1
fi
echo "シークレットの参照先: ${REF_PREFIX}"

step "1. Cloud Build API"
gcloud services enable cloudbuild.googleapis.com
PROJECT_NUMBER="$(gcloud projects describe "$PROJECT" --format='value(projectNumber)')"
BUILD_SA="${PROJECT_NUMBER}-compute@developer.gserviceaccount.com"
# --source デプロイのビルドは既定のコンピュート SA で動く。新しいプロジェクトでは権限が足りないことがある
# ソースの取得（run-sources バケットの読み取り）とビルドに必要な権限を付ける。権限の反映には数分かかることがある
for role in roles/run.builder roles/cloudbuild.builds.builder; do
  gcloud projects add-iam-policy-binding "$PROJECT" --member="serviceAccount:${BUILD_SA}" \
    --role="$role" --condition=None >/dev/null
done

step "2. バケット gs://${BUCKET}"
if ! gcloud storage buckets describe "gs://${BUCKET}" >/dev/null 2>&1; then
  gcloud storage buckets create "gs://${BUCKET}" --location="$REGION" \
    --uniform-bucket-level-access --public-access-prevention
fi
LIFECYCLE="$(mktemp)"
echo '{"rule": [{"action": {"type": "Delete"}, "condition": {"age": 1}}]}' >"$LIFECYCLE"
gcloud storage buckets update "gs://${BUCKET}" --lifecycle-file="$LIFECYCLE"

step "3. サービスアカウント ${RUN_SA}"
if ! gcloud iam service-accounts describe "$RUN_SA" >/dev/null 2>&1; then
  gcloud iam service-accounts create meeting-notes-run --display-name="meeting-notes (Cloud Run)"
fi
gcloud storage buckets add-iam-policy-binding "gs://${BUCKET}" \
  --member="serviceAccount:${RUN_SA}" --role=roles/storage.objectAdmin >/dev/null
gcloud iam service-accounts add-iam-policy-binding "$RUN_SA" \
  --member="serviceAccount:${RUN_SA}" --role=roles/iam.serviceAccountTokenCreator >/dev/null
for s in "${SECRETS[@]}"; do
  gcloud secrets add-iam-policy-binding "$s" "${LOC_FLAG[@]}" \
    --member="serviceAccount:${RUN_SA}" --role=roles/secretmanager.secretAccessor >/dev/null
done

step "4. Cloud Run へデプロイ（数分かかります）"
EXISTING_URL="$(gcloud run services describe "$SERVICE" --region="$REGION" --format='value(status.url)' 2>/dev/null || true)"
ENV_FILE="$(mktemp --suffix=.yaml)"
PROJECT="$PROJECT" BUCKET="$BUCKET" RUN_SA="$RUN_SA" REF_PREFIX="$REF_PREFIX" \
SERVICE_URL="${EXISTING_URL:-https://pending.invalid}" python3 - "$ENV_FILE" <<'PY'
import json, os, sys
e = os.environ
clients = {"clients": {"default": {"api_key": f"sm:{e['REF_PREFIX']}/backend-api-key/versions/latest"}}}
env = {
    "DRY_RUN": "true",
    "CRM_TEST_RECORDS": "true",
    "GCP_PROJECT_ID": e["PROJECT"],
    "GCS_BUCKET": e["BUCKET"],
    "SIGNING_SERVICE_ACCOUNT": e["RUN_SA"],
    "RECORDING_TOKEN_SECRET_REF": f"sm:{e['REF_PREFIX']}/recording-token-secret/versions/latest",
    "SERVICE_URL": e["SERVICE_URL"],
    "CLIENTS_CONFIG_JSON": json.dumps(clients),
}
with open(sys.argv[1], "w", encoding="utf-8") as f:
    for k, v in env.items():
        f.write(f"{k}: {json.dumps(v)}\n")
PY
gcloud run deploy "$SERVICE" --region="$REGION" --source=. \
  --service-account="$RUN_SA" --allow-unauthenticated \
  --max-instances=3 --timeout=1800 --memory=1Gi \
  --env-vars-file="$ENV_FILE" --quiet

step "5. サービス URL・CORS・動作確認"
SERVICE_URL="$(gcloud run services describe "$SERVICE" --region="$REGION" --format='value(status.url)')"
gcloud run services update "$SERVICE" --region="$REGION" --update-env-vars="SERVICE_URL=${SERVICE_URL}" --quiet
CORS="$(mktemp)"
echo "[{\"origin\": [\"${SERVICE_URL}\"], \"method\": [\"PUT\"], \"responseHeader\": [\"Content-Type\"], \"maxAgeSeconds\": 3600}]" >"$CORS"
gcloud storage buckets update "gs://${BUCKET}" --cors-file="$CORS"
curl -fsS "${SERVICE_URL}/health"
echo

step "6. テスト用の録音 URL（24時間有効。iPhone の Safari で開いてください）"
RECORDING_TOKEN_SECRET="$(gcloud secrets versions access latest --secret=recording-token-secret "${LOC_FLAG[@]}")" \
  python3 scripts/issue_test_url.py --service-url "$SERVICE_URL"
echo
echo "もう1本発行するとき: bash scripts/setup_recorder_test.sh をもう一度実行するか、上の手順6だけを実行してください。"
