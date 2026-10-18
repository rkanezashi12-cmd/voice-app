#!/usr/bin/env bash
# 2つ目以降のクライアント（お客様の CRM）を Cloud Run のクライアント設定に足す（Cloud Shell で実行する想定）。
#
#   cd ~/voice-app && git pull && APP_CLIENT=monodemo DISPLAY_NAME="ものづくりSFAデモ2" bash scripts/setup_client.sh
#
# 行うこと（何度実行しても壊れない。CRM には何も書き込まない。ほかのクライアントの設定・シークレットは変えない）:
#   1. CRM のワークフローがバックエンドを呼ぶときの API キーを作り、Secret Manager の <APP_CLIENT>-backend-api-key に保存する
#      （作り済みならそのまま使う。作り直すときは APP_CLIENT=… bash scripts/rotate_api_key.sh）
#   2. Cloud Run のクライアント設定（CLIENTS_CONFIG_JSON）に、このクライアントを足す。
#      新しいクライアントは dry_run=true・crm_test_records=true で始める（CRM に書き込まない。【TEST】を付ける）。
#      書き込みに切り替えるのはユーザーの指示があるときだけ（APP_CLIENT=… bash scripts/client_mode.sh --write）
# API キーの値は表示しない。CRM の変数に貼るときに APP_CLIENT=… bash scripts/rotate_api_key.sh で作り直して表示する。
# default（マルサン木型）には使えない（今の設定のまま変えない）。手順は docs/monodemo.md。
set -euo pipefail
cd "$(dirname "$0")/.."
# Ctrl+Z（一時停止）を効かなくする。「元に戻す」のつもりで押すと、スクリプトが止まったままになって分かりにくいため
trap '' TSTP

PROJECT="${PROJECT:-voice-ai-510014}"
REGION="asia-northeast1"
SERVICE="meeting-notes"
RUN_SA="meeting-notes-run@${PROJECT}.iam.gserviceaccount.com"
APP_CLIENT="${APP_CLIENT:-}"
DISPLAY_NAME="${DISPLAY_NAME:-}"
CLIENT_TZ="${CLIENT_TZ:-Asia/Tokyo}"

step() { printf '\n==== %s\n' "$*"; }
fail() {
  printf 'エラー: %s\n' "$*" >&2
  exit 1
}
# yes で進む・no で中止。それ以外（空の Enter など）は聞き直す
confirm() {
  local answer=""
  while true; do
    read -rp "$1" answer || fail "入力が終わりました（中止します）"
    case "$answer" in
      yes) return 0 ;;
      no) fail "中止しました。$2" ;;
    esac
  done
}

[ -n "$APP_CLIENT" ] || fail "APP_CLIENT（クライアント ID）を付けて実行してください（例: APP_CLIENT=monodemo bash scripts/setup_client.sh）"
[ "$APP_CLIENT" != default ] || fail "default（マルサン木型）の設定は、このスクリプトでは変えません"
[[ "$APP_CLIENT" =~ ^[a-z0-9][a-z0-9-]{0,31}$ ]] || fail "APP_CLIENT は英小文字・数字・ハイフン（32文字まで）にしてください"
[[ "$DISPLAY_NAME" != *@@* ]] || fail "DISPLAY_NAME に @@ は使えません"
SECRET="${APP_CLIENT}-backend-api-key"

step "0. 事前確認"
gcloud config set project "$PROJECT" >/dev/null
# シークレットが通常かリージョン シークレットかを判定する（scripts/setup_zoho_connection.sh と同じ）
if gcloud secrets describe recording-token-secret >/dev/null 2>&1; then
  LOC_FLAG=()
  REF_PREFIX="projects/${PROJECT}/secrets"
elif gcloud secrets describe recording-token-secret --location="$REGION" >/dev/null 2>&1; then
  LOC_FLAG=(--location="$REGION")
  REF_PREFIX="projects/${PROJECT}/locations/${REGION}/secrets"
else
  fail "シークレット recording-token-secret が見つかりません（先に scripts/setup_recorder_test.sh を実行）"
fi
SVC_JSON="$(gcloud run services describe "$SERVICE" --region="$REGION" --format=json 2>/dev/null)" ||
  fail "Cloud Run のサービス ${SERVICE} が見つかりません"
SERVICE_URL="$(SVC_JSON="$SVC_JSON" python3 -c '
import json, os
svc = json.loads(os.environ["SVC_JSON"])
env = {e.get("name"): e.get("value") for e in svc["spec"]["template"]["spec"]["containers"][0].get("env", [])}
print(env.get("SERVICE_URL") or svc["status"]["url"])
')"
echo "プロジェクト: ${PROJECT} / サービス: ${SERVICE_URL} / クライアント: ${APP_CLIENT} / API キーの保存先: ${SECRET}"

step "1. CRM のワークフローがバックエンドを呼ぶときの API キー"
NEW_KEY=""
if gcloud secrets describe "$SECRET" "${LOC_FLAG[@]}" >/dev/null 2>&1; then
  echo "保存済みの API キーをそのまま使います（作り直すときは APP_CLIENT=${APP_CLIENT} bash scripts/rotate_api_key.sh）。"
else
  NEW_KEY="$(python3 -c 'import secrets; print(secrets.token_hex(32))')"
  echo "新しい API キーを作ります（保存は手順2。値は表示しません）。"
fi

step "2. Cloud Run のクライアント設定に ${APP_CLIENT} を足す（ほかのクライアントは変えません。数分かかります）"
CONFIG_OUT="$(SVC_JSON="$SVC_JSON" DISPLAY_NAME="$DISPLAY_NAME" python3 - "$APP_CLIENT" "$REF_PREFIX" "$SECRET" "$CLIENT_TZ" <<'PY'
import json
import os
import sys

app_client, prefix, secret, tz = sys.argv[1:5]
svc = json.loads(os.environ["SVC_JSON"])
env = {e.get("name"): e.get("value") for e in svc["spec"]["template"]["spec"]["containers"][0].get("env", [])}
clients = json.loads(env.get("CLIENTS_CONFIG_JSON") or "{}")
existing = app_client in clients.setdefault("clients", {})
client = clients["clients"].setdefault(app_client, {})
client["api_key"] = f"sm:{prefix}/{secret}/versions/latest"
client.setdefault("timezone", tz)
if os.environ.get("DISPLAY_NAME"):
    client["display_name"] = os.environ["DISPLAY_NAME"]
# 新しいクライアントは書き込まない・【TEST】を付ける状態で始める（作り済みのクライアントの設定は変えない）
if not existing:
    client["dry_run"] = True
    client["crm_test_records"] = True
# 1行目は環境変数に渡す形（ASCII だけ）、2行目は画面に出す形、3行目は作り済みだったか
print(json.dumps(clients, separators=(",", ":")))
print(json.dumps(clients, ensure_ascii=False, separators=(",", ":")))
print("existing" if existing else "new")
PY
)"
NEW_CLIENTS="$(sed -n 1p <<<"$CONFIG_OUT")"
echo "クライアント設定（秘密情報は参照だけ）: $(sed -n 2p <<<"$CONFIG_OUT")"
if [ "$(sed -n 3p <<<"$CONFIG_OUT")" = existing ]; then
  echo "${APP_CLIENT} は作り済みです。DRY_RUN・【TEST】の設定は変えません。"
else
  echo "${APP_CLIENT} は DRY_RUN（CRM に書き込まない）・【TEST】を付ける状態で始めます。"
fi
confirm "保存して設定を更新するなら yes、やめるなら no: " "何も保存していません。"
if [ -n "$NEW_KEY" ]; then
  # 値は標準入力で渡し、画面にもコマンド履歴にも出さない
  printf '%s' "$NEW_KEY" | gcloud secrets create "$SECRET" "${LOC_FLAG[@]}" --data-file=- >/dev/null
  echo "保存しました: ${SECRET}"
fi
gcloud secrets add-iam-policy-binding "$SECRET" "${LOC_FLAG[@]}" \
  --member="serviceAccount:${RUN_SA}" --role=roles/secretmanager.secretAccessor >/dev/null
# JSON にカンマが含まれるので、区切り文字を @@ に変えて渡す
gcloud run services update "$SERVICE" --region="$REGION" --quiet \
  --update-env-vars="^@@^CLIENTS_CONFIG_JSON=${NEW_CLIENTS}@@CLIENTS_UPDATED_AT=$(date -u +%Y%m%dT%H%M%SZ)"
curl -fsS "${SERVICE_URL%/}/health"
echo

step "完了"
echo "クライアント ${APP_CLIENT} を足しました（CRM にはまだつないでいません）。次は docs/monodemo.md の手順へ。"
echo "API キーの値は表示しません。CRM の変数 meeting_notes_api_key に貼るときに、"
echo "APP_CLIENT=${APP_CLIENT} bash scripts/rotate_api_key.sh で作り直して表示します（docs/monodemo.md の手順6）。"
