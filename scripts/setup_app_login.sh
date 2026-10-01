#!/usr/bin/env bash
# 録音アプリ（/app/）のログイン（Zoho アカウント）と、GPS の候補（Google Geocoding API）をつなぐ（Cloud Shell で実行する想定）。
#
#   cd ~/voice-app && git pull && bash scripts/setup_app_login.sh
#
# 行うこと（何度実行しても壊れない。CRM には書き込まない。DRY_RUN は変えない）:
#   1. Zoho の API コンソールに「Server-based Applications」のクライアントを作ってもらい、Client ID と Client Secret を聞く
#   2. ログインの Cookie の署名鍵を作る（初回だけ）
#   3. （任意）Google Geocoding API を有効にし、Geocoding API だけに絞った API キーを作る（GPS の候補に使う）
#   4. Secret Manager に保存し、Cloud Run のクライアント設定に app を足す（zoho・recall などほかの設定は変えない）
#   5. 録音アプリの URL を表示する
# 2回目からは、保存済みの値をそのまま Enter で使える。手順は docs/visit-app.md。
set -euo pipefail
cd "$(dirname "$0")/.."
# Ctrl+Z（一時停止）を効かなくする。「元に戻す」のつもりで押すと、スクリプトが止まったままになって分かりにくいため
trap '' TSTP

PROJECT="${PROJECT:-voice-ai-510014}"
REGION="asia-northeast1"
SERVICE="meeting-notes"
RUN_SA="meeting-notes-run@${PROJECT}.iam.gserviceaccount.com"
APP_CLIENT="${APP_CLIENT:-default}" # クライアント設定のキー
MAPS_KEY_ID="app-geocoding"         # Google の API キーの ID（英小文字・数字・ハイフン）
declare -A REUSED=()                # 保存済みの値をそのまま使ったシークレット名（保存し直さない）

step() { printf '\n==== %s\n' "$*"; }
fail() {
  printf 'エラー: %s\n' "$*" >&2
  exit 1
}
# 秘密の値を聞く（画面に出さない）。保存済みのシークレット名を渡すと、空の Enter でその値を使う
ask_secret() {
  local __var=$1 __prompt=$2 __stored=${3:-} __value=""
  [ -z "$__stored" ] || unset "REUSED[$__stored]"
  while [ -z "$__value" ]; do
    read -rsp "$__prompt" __value
    echo
    if [ -z "$__value" ] && [ -n "$__stored" ]; then
      __value="$(gcloud secrets versions access latest --secret="$__stored" "${LOC_FLAG[@]}" 2>/dev/null)" || __value=""
      if [ -n "$__value" ]; then
        echo "保存済みの値を使います: ${__stored}"
        REUSED[$__stored]=1
      else
        echo "保存済みの値を読めませんでした。貼ってください。"
      fi
    fi
  done
  printf -v "$__var" '%s' "$__value"
}
reused() { [ -n "${REUSED[$1]-}" ]; }
secret_exists() { gcloud secrets describe "$1" "${LOC_FLAG[@]}" >/dev/null 2>&1; }
# yes で進む・no で false を返す。それ以外（空の Enter など）は聞き直す
yes_no() {
  local answer=""
  while true; do
    read -rp "$1" answer
    case "$answer" in
      yes) return 0 ;;
      no) return 1 ;;
    esac
  done
}
# 値は標準入力で渡し、画面にもコマンド履歴にも出さない
save_secret() {
  local name=$1 value=$2
  if secret_exists "$name"; then
    printf '%s' "$value" | gcloud secrets versions add "$name" "${LOC_FLAG[@]}" --data-file=- >/dev/null
  else
    printf '%s' "$value" | gcloud secrets create "$name" "${LOC_FLAG[@]}" --data-file=- >/dev/null
  fi
  gcloud secrets add-iam-policy-binding "$name" "${LOC_FLAG[@]}" \
    --member="serviceAccount:${RUN_SA}" --role=roles/secretmanager.secretAccessor >/dev/null
  echo "保存しました: ${name}"
}

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
# 1行目：アプリが使うサービス URL（SERVICE_URL）。2行目：Zoho の DC（API コンソールの URL に使う。無ければ空）
mapfile -t SVC_INFO < <(SVC_JSON="$SVC_JSON" python3 -c '
import json, os, sys
svc = json.loads(os.environ["SVC_JSON"])
env = {e.get("name"): e.get("value") for e in svc["spec"]["template"]["spec"]["containers"][0].get("env", [])}
client = json.loads(env.get("CLIENTS_CONFIG_JSON") or "{}").get("clients", {}).get(sys.argv[1], {})
print(env.get("SERVICE_URL") or svc["status"]["url"])
print((client.get("zoho") or {}).get("dc") or ("us" if client.get("zoho") else ""))
' "$APP_CLIENT")
SERVICE_URL="${SVC_INFO[0]:-}"
ZOHO_DC="${SVC_INFO[1]:-}"
[[ "$SERVICE_URL" == https://* ]] || fail "Cloud Run のサービス URL を読めませんでした"
[ -n "$ZOHO_DC" ] || fail "クライアント設定（${APP_CLIENT}）に zoho がありません（先に scripts/setup_zoho_connection.sh を実行）"
case "$ZOHO_DC" in
  us) CONSOLE="https://api-console.zoho.com" ;;
  jp) CONSOLE="https://api-console.zoho.jp" ;;
  eu) CONSOLE="https://api-console.zoho.eu" ;;
  in) CONSOLE="https://api-console.zoho.in" ;;
  au) CONSOLE="https://api-console.zoho.com.au" ;;
  *) fail "Zoho の DC（${ZOHO_DC}）が分かりません" ;;
esac
echo "プロジェクト: ${PROJECT} / サービス: ${SERVICE_URL} / Zoho の DC: ${ZOHO_DC}"

step "1. Zoho の API コンソールにログイン用のクライアントを作る"
if secret_exists app-login-client-id && secret_exists app-login-client-secret; then
  echo "保存済みのクライアントがあります。作り直していなければ、2つともそのまま Enter で使います。"
else
  echo "${CONSOLE} を開き、「ADD CLIENT」→「Server-based Applications」で、次のとおり作ってください:"
  echo "  Client Name: 商談音声日報（ログイン）"
  echo "  Homepage URL: ${SERVICE_URL}/app/"
  echo "  Authorized Redirect URIs: ${SERVICE_URL}/auth/callback"
  echo "作ったら「Client Secret」タブにある Client ID と Client Secret を、1つずつ貼ってください。"
fi
STORED_ID=""
STORED_SECRET=""
secret_exists app-login-client-id && STORED_ID=app-login-client-id
secret_exists app-login-client-secret && STORED_SECRET=app-login-client-secret
for _ in 1 2 3; do
  ask_secret LOGIN_CLIENT_ID "Client ID（表示されません）: " "$STORED_ID"
  [[ "$LOGIN_CLIENT_ID" == 1000.* ]] && break
  echo "Client ID の形ではありません（1000. で始まる値を貼ってください）。"
  LOGIN_CLIENT_ID=""
done
[ -n "$LOGIN_CLIENT_ID" ] || fail "Client ID を確かめられませんでした。何も保存していません"
ask_secret LOGIN_CLIENT_SECRET "Client Secret（表示されません）: " "$STORED_SECRET"

step "2. ログインの Cookie の署名鍵"
if secret_exists app-session-secret; then
  echo "保存済みの鍵をそのまま使います（作り直すとログイン中の人は全員ログアウトになります）。"
  SESSION_SECRET=""
else
  SESSION_SECRET="$(openssl rand -base64 48 | tr -d '\n')"
  echo "新しい鍵を作りました（保存は手順4）。"
fi

step "3. GPS から訪問先の候補を出す（任意。Google Geocoding API）"
echo "スマホの位置から住所を調べ、同じ市区町村の顧客企業を候補に出します。"
echo "料金は Google Maps Platform の Geocoding API（月1万回まで無料、その後 1,000回あたり約5ドル。2026-10 時点の検索結果）。"
MAPS_KEY=""
if yes_no "使うなら yes、使わないなら no: "; then
  gcloud services enable geocoding-backend.googleapis.com apikeys.googleapis.com --project="$PROJECT"
  if gcloud services api-keys describe "$MAPS_KEY_ID" --project="$PROJECT" >/dev/null 2>&1; then
    echo "API キー ${MAPS_KEY_ID} はあります（作り直しません）。"
  else
    gcloud services api-keys create --project="$PROJECT" --key-id="$MAPS_KEY_ID" \
      --display-name="録音アプリの GPS（Geocoding API だけ）" \
      --api-target=service=geocoding-backend.googleapis.com >/dev/null
    echo "API キー ${MAPS_KEY_ID} を作りました（Geocoding API だけに使えるキー）。"
  fi
  # キーの値は画面に出さず、そのまま Secret Manager に渡す
  MAPS_KEY="$(gcloud services api-keys get-key-string "$MAPS_KEY_ID" --project="$PROJECT" --format='value(keyString)')"
  [ -n "$MAPS_KEY" ] || fail "API キーの値を読めませんでした"
else
  echo "GPS の候補は使いません（あとでこのスクリプトをもう一度実行すれば足せます）。"
fi

step "4. Secret Manager に保存して、Cloud Run の設定に app を足す（DRY_RUN は変えません。数分かかります）"
yes_no "保存して設定を更新するなら yes、やめるなら no: " || fail "中止しました。何も保存していません。"
reused app-login-client-id || save_secret app-login-client-id "$LOGIN_CLIENT_ID"
reused app-login-client-secret || save_secret app-login-client-secret "$LOGIN_CLIENT_SECRET"
[ -z "$SESSION_SECRET" ] || save_secret app-session-secret "$SESSION_SECRET"
[ -z "$MAPS_KEY" ] || save_secret app-maps-api-key "$MAPS_KEY"
unset LOGIN_CLIENT_ID LOGIN_CLIENT_SECRET SESSION_SECRET MAPS_KEY
USE_MAPS=""
secret_exists app-maps-api-key && USE_MAPS=1
CONFIG_OUT="$(SVC_JSON="$SVC_JSON" USE_MAPS="$USE_MAPS" python3 - "$APP_CLIENT" "$REF_PREFIX" <<'PY'
import json
import os
import sys

app_client, prefix = sys.argv[1:3]
svc = json.loads(os.environ["SVC_JSON"])
env = {e.get("name"): e.get("value") for e in svc["spec"]["template"]["spec"]["containers"][0].get("env", [])}
clients = json.loads(env.get("CLIENTS_CONFIG_JSON") or "{}")
client = clients.setdefault("clients", {}).setdefault(app_client, {})
app = client.get("app") or {}
app.update(
    {
        "login_client_id": f"sm:{prefix}/app-login-client-id/versions/latest",
        "login_client_secret": f"sm:{prefix}/app-login-client-secret/versions/latest",
        "session_secret": f"sm:{prefix}/app-session-secret/versions/latest",
    }
)
if os.environ.get("USE_MAPS"):
    app["maps_api_key"] = f"sm:{prefix}/app-maps-api-key/versions/latest"
client["app"] = app
# 1行目は環境変数に渡す形（ASCII だけ）、2行目は画面に出す形
print(json.dumps(clients, separators=(",", ":")))
print(json.dumps(clients, ensure_ascii=False, separators=(",", ":")))
PY
)"
NEW_CLIENTS="$(sed -n 1p <<<"$CONFIG_OUT")"
echo "クライアント設定（秘密情報は参照だけ）: $(sed -n 2p <<<"$CONFIG_OUT")"
# JSON にカンマが含まれるので、区切り文字を @@ に変えて渡す。新しいシークレットを読むように、新しい版を出す
gcloud run services update "$SERVICE" --region="$REGION" --quiet \
  --update-env-vars="^@@^CLIENTS_CONFIG_JSON=${NEW_CLIENTS}@@APP_CONFIG_UPDATED_AT=$(date -u +%Y%m%dT%H%M%SZ)"
curl -fsS "${SERVICE_URL%/}/health"
echo

step "完了"
APP_URL="${SERVICE_URL%/}/app/"
[ "$APP_CLIENT" = default ] || APP_URL="${APP_URL}?c=${APP_CLIENT}"
echo "録音アプリの URL: ${APP_URL}"
echo "スマホの Chrome / Safari で開き、「Zoho でログイン」から CRM のアカウントでログインしてください（手順は docs/visit-app.md）。"
