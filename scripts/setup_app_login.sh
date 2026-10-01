#!/usr/bin/env bash
# 録音アプリ（/app/）のログイン（Zoho アカウント）と、GPS の候補（Google Geocoding API）をつなぐ（Cloud Shell で実行する想定）。
#
#   cd ~/voice-app && git pull && bash scripts/setup_app_login.sh
#
# 行うこと（何度実行しても壊れない。CRM には書き込まない。DRY_RUN は変えない）:
#   1. Zoho の API コンソールに「Server-based Applications」のクライアントを作ってもらい、Client ID と Client Secret を聞く
#   2. ログインの Cookie の署名鍵を作る（初回だけ）
#   3. （任意）Google Geocoding API を有効にし、Geocoding API だけに絞った API キーを作る（GPS の候補に使う）。
#      キーの値は画面に出さない。作り直すときは、新しいキーに切り替えたあとで古いキーを削除する
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
MAPS_KEY_ID="app-geocoding"         # Google の API キーの ID の頭（作るたびに日時を付ける。英小文字・数字・ハイフン）
declare -A REUSED=()                # 保存済みの値をそのまま使ったシークレット名（保存し直さない）
SAVING=""                           # 手順4（保存）に入ったら 1
PASTE_START=$'\e[200~'              # 貼り付けの目印（ブラケットペースト）。値に混ざったら外す
PASTE_END=$'\e[201~'

step() { printf '\n==== %s\n' "$*"; }
fail() {
  printf 'エラー: %s\n' "$*" >&2
  exit 1
}
# Cloud Shell の Ctrl+C はコピーではなく中止になる。止まったことと、やり直し方を出す
# （止まったのに気づかず、続けて Client Secret などを貼ると、ふつうのコマンドとして画面と履歴に残るため）
on_interrupt() {
  stty echo 2>/dev/null || true
  echo >&2
  echo "中止しました（Cloud Shell の Ctrl+C は、コピーではなく中止です）。" >&2
  if [ -n "$SAVING" ]; then
    echo "保存の途中で止めました。もう一度実行して、最後まで進めてください（↑ キー → Enter）。" >&2
  else
    echo "何も保存していません。やり直すときは ↑ キー → Enter。" >&2
  fi
  echo "このあとに Client ID や Client Secret を貼らないでください（ふつうのコマンドとして実行され、画面と履歴に残ります）。" >&2
  exit 130
}
trap on_interrupt INT
# 画面に出すと困る出力（作った API キーの値など）を捨てて実行する。失敗したときだけ、キーの値を伏せて理由を出す
run_quietly() {
  local out
  if out="$("$@" 2>&1)"; then
    return 0
  fi
  printf '%s\n' "$out" |
    sed -E 's/AIza[0-9A-Za-z_-]+/（キーの値は伏せました）/g; s/("keyString"[[:space:]]*:[[:space:]]*")[^"]*/\1（伏せました）/g' >&2
  return 1
}
# 秘密の値を聞く（画面に出さない）。保存済みのシークレット名を渡すと、空の Enter でその値を使う
ask_secret() {
  local __var=$1 __prompt=$2 __stored=${3:-} __value=""
  [ -z "$__stored" ] || unset "REUSED[$__stored]"
  while [ -z "$__value" ]; do
    read -rsp "$__prompt" __value || fail "入力が終わりました。何も保存していません"
    echo
    __value="${__value#"$PASTE_START"}"
    __value="${__value%"$PASTE_END"}"
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
    read -rp "$1" answer || fail "入力が終わりました（中止します）"
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
# このスクリプトが作った地図の API キーの ID（app-geocoding で始まるもの。削除済みは出ない）を新しい順に出す
maps_keys() {
  local name id
  gcloud services api-keys list --project="$PROJECT" --format='value(createTime,name)' |
    sort -r |
    while read -r _ name; do
      id="${name##*/}"
      if [[ "$id" == "$MAPS_KEY_ID" || "$id" == "$MAPS_KEY_ID"-* ]]; then
        echo "$id"
      fi
    done
}
# 地図のキーの値を、画面に出さずに Secret Manager に渡す（保存済みと同じなら保存し直さない）
save_maps_key() {
  local value stored=""
  value="$(gcloud services api-keys get-key-string "$1" --project="$PROJECT" --format='value(keyString)')"
  [ -n "$value" ] || fail "API キー $1 の値を読めませんでした"
  if secret_exists app-maps-api-key; then
    stored="$(gcloud secrets versions access latest --secret=app-maps-api-key "${LOC_FLAG[@]}" 2>/dev/null)" || stored=""
  fi
  if [ "$value" = "$stored" ]; then
    echo "保存済みの地図のキー（${1}）をそのまま使います。"
  else
    save_secret app-maps-api-key "$value"
  fi
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
echo "貼り付けは Ctrl+V か右クリックの「貼り付け」。貼り付けられるのは最後にコピーした1つだけなので、"
echo "Zoho の画面で Client ID をコピー → ここに貼る → Client Secret をコピー → ここに貼る、の順に進めてください。"
echo "この画面（Cloud Shell）では Ctrl+C を押さないでください（コピーではなく中止になります）。"
STORED_ID=""
STORED_SECRET=""
secret_exists app-login-client-id && STORED_ID=app-login-client-id
secret_exists app-login-client-secret && STORED_SECRET=app-login-client-secret
for _ in 1 2 3; do
  ask_secret LOGIN_CLIENT_ID "Client ID（表示されません）: " "$STORED_ID"
  [[ "$LOGIN_CLIENT_ID" == 1000.* ]] && break
  # 貼った値は出さずに、よくある取り違えを知らせる
  if [[ "$LOGIN_CLIENT_ID" =~ ^[0-9a-f]{30,}$ ]]; then
    echo "Client Secret が貼られたようです（保存していません）。先に Client ID（1000. で始まる値）を貼ってください。"
  elif [[ "$LOGIN_CLIENT_ID" == *[[:space:]]* ]]; then
    echo "Client ID ではない文（コマンドなど）が貼られました。Zoho の画面で Client ID をコピーし直してから貼ってください。"
  else
    echo "Client ID の形ではありません（1000. で始まる値を貼ってください）。"
  fi
  LOGIN_CLIENT_ID=""
done
[ -n "$LOGIN_CLIENT_ID" ] || fail "Client ID を確かめられませんでした。何も保存していません"
for _ in 1 2 3; do
  ask_secret LOGIN_CLIENT_SECRET "Client Secret（表示されません）: " "$STORED_SECRET"
  if [[ "$LOGIN_CLIENT_SECRET" == 1000.* ]]; then
    echo "Client ID がもう一度貼られたようです。Zoho の画面で Client Secret をコピーしてから貼ってください。"
  elif [[ "$LOGIN_CLIENT_SECRET" == *[[:space:]]* ]]; then
    echo "Client Secret ではない文（コマンドなど）が貼られました。Zoho の画面で Client Secret をコピーし直してから貼ってください。"
  else
    break
  fi
  LOGIN_CLIENT_SECRET=""
done
[ -n "$LOGIN_CLIENT_SECRET" ] || fail "Client Secret を確かめられませんでした。何も保存していません"

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
# ここでは決めるだけ。キーを作る・保存する・古いキーを削除するのは、手順4で yes と答えたあと
MAPS_ACTION=""      # create（新しいキーを作る）/ keep（今のキーを使い続ける）/ 空（GPS の候補を使わない）
MAPS_KEYS=()        # このスクリプトが作ったキー（新しい順）
OLD_MAPS_KEYS=()    # 新しいキーに切り替えたあとで削除する古いキー
if yes_no "使うなら yes、使わないなら no: "; then
  gcloud services enable geocoding-backend.googleapis.com apikeys.googleapis.com --project="$PROJECT"
  KEYS_TEXT="$(maps_keys)" || fail "API キーの一覧を読めませんでした"
  [ -z "$KEYS_TEXT" ] || mapfile -t MAPS_KEYS <<<"$KEYS_TEXT"
  MAPS_ACTION=create
  if [ "${#MAPS_KEYS[@]}" -gt 0 ]; then
    echo "API キー ${MAPS_KEYS[0]} があります。"
    echo "キーの値が画面や画面写真に出てしまったときは、作り直してください（新しいキーに切り替えたあとで、今のキーを削除します）。"
    if yes_no "作り直すなら yes、今のキーを使い続けるなら no: "; then
      OLD_MAPS_KEYS=("${MAPS_KEYS[@]}")
      echo "手順4で新しいキーを作って切り替え、そのあとで今のキーを削除します。"
    else
      MAPS_ACTION=keep
    fi
  else
    echo "手順4で、Geocoding API だけに使える API キーを作ります（値は画面に出しません）。"
  fi
else
  echo "GPS の候補は使いません（あとでこのスクリプトをもう一度実行すれば足せます）。"
fi

step "4. Secret Manager に保存して、Cloud Run の設定に app を足す（DRY_RUN は変えません。数分かかります）"
yes_no "保存して設定を更新するなら yes、やめるなら no: " ||
  fail "中止しました。何も保存していません（API キーも作っていません）。"
SAVING=1
reused app-login-client-id || save_secret app-login-client-id "$LOGIN_CLIENT_ID"
reused app-login-client-secret || save_secret app-login-client-secret "$LOGIN_CLIENT_SECRET"
[ -z "$SESSION_SECRET" ] || save_secret app-session-secret "$SESSION_SECRET"
unset LOGIN_CLIENT_ID LOGIN_CLIENT_SECRET SESSION_SECRET
if [ "$MAPS_ACTION" = create ]; then
  KEY_ID="${MAPS_KEY_ID}-$(date -u +%Y%m%d%H%M%S)"
  # gcloud は作ったキーの値を結果として画面に出す（標準エラー）ので、出力ごと捨てる
  run_quietly gcloud services api-keys create --project="$PROJECT" --key-id="$KEY_ID" \
    --display-name="録音アプリの GPS（Geocoding API だけ）" \
    --api-target=service=geocoding-backend.googleapis.com || fail "API キーを作れませんでした"
  echo "API キー ${KEY_ID} を作りました（Geocoding API だけに使えるキー。値は画面に出しません）。"
  save_maps_key "$KEY_ID"
elif [ "$MAPS_ACTION" = keep ]; then
  save_maps_key "${MAPS_KEYS[0]}"
fi
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
# 新しいキーに切り替わったので、古いキーを削除する（30日以内なら gcloud services api-keys undelete <ID> で戻せる）
for old in "${OLD_MAPS_KEYS[@]}"; do
  run_quietly gcloud services api-keys delete "$old" --project="$PROJECT" --quiet ||
    fail "古い API キー ${old} を削除できませんでした（新しいキーには切り替わっています）。Google Cloud コンソールの「API とサービス」→「認証情報」で削除してください"
  echo "古い API キー ${old} を削除しました（30日以内なら元に戻せます）。"
done
SAVING=""

step "完了"
APP_URL="${SERVICE_URL%/}/app/"
[ "$APP_CLIENT" = default ] || APP_URL="${APP_URL}?c=${APP_CLIENT}"
echo "録音アプリの URL: ${APP_URL}"
echo "スマホの Chrome / Safari で開き、「Zoho でログイン」から CRM のアカウントでログインしてください（手順は docs/visit-app.md）。"
