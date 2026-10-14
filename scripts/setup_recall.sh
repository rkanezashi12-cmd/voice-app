#!/usr/bin/env bash
# Recall.ai（入口A：ボット参加）をバックエンド（Cloud Run）につなぐ設定を行う（Cloud Shell で実行する想定）。
#
#   cd ~/voice-app && git pull && bash scripts/setup_recall.sh                      # マルサン木型（default）
#   cd ~/voice-app && git pull && APP_CLIENT=monodemo bash scripts/setup_recall.sh  # 2つ目以降のクライアント
#
# 行うこと（何度実行しても壊れない。ボットは作らない。CRM には書き込まない。DRY_RUN は変えない）:
#   1. Recall.ai の API キー（東京リージョン）を聞き、読み取りの問い合わせで使えるか確かめる（キーは画面に出さない）
#   2. Webhook の送り先 URL を表示し、Recall.ai のダッシュボードで登録してもらって、署名用のシークレットを聞く
#   3. ボットの表示名（会議の参加者に見える名前）を決める
#   4. Secret Manager に保存する（default は recall-api-key / recall-webhook-secret、
#      ほかのクライアントは <APP_CLIENT>-recall-api-key などクライアントの名前を頭に付けた別のシークレット）
#   5. Cloud Run のクライアント設定（CLIENTS_CONFIG_JSON）のそのクライアントにだけ recall を足す（zoho などほかの設定は変えない）
#   6. Webhook のテスト送信が届き、署名が合うかをログで確かめる（任意）
# 2回目からは、保存済みの API キー・シークレットをそのまま Enter で使える（表示名だけ変えるときなど）。
#
# default 以外のクライアントでは、最初に「default と同じ Recall.ai のアカウントを使うか」を聞く。yes なら
# default のシークレットを参照するだけで、キーもシークレットも保存し直さず、Recall.ai に送り先も足さない
# （同じアカウントに同じ URL の送り先を2つ作ると、通知が2回届く）。通知は、ボットの metadata の client_id で
# クライアントに振り分ける（app/routers/webhooks.py）。
set -euo pipefail
cd "$(dirname "$0")/.."
# Ctrl+Z（一時停止）を効かなくする。「元に戻す」のつもりで押すと、スクリプトが止まったままになって分かりにくいため
trap '' TSTP

PROJECT="${PROJECT:-voice-ai-510014}"
REGION="asia-northeast1"
SERVICE="meeting-notes"
RUN_SA="meeting-notes-run@${PROJECT}.iam.gserviceaccount.com"
APP_CLIENT="${APP_CLIENT:-default}" # クライアント設定のキー
RECALL_BASE_URL="https://ap-northeast-1.recall.ai" # 東京リージョン（CLAUDE.md で固定）
DEFAULT_BOT_NAME="議事録ボット（録音中）"
SHARED=""            # default と同じ Recall.ai のアカウントを使うなら 1
declare -A REUSED=() # 保存済みの値をそのまま使ったシークレット名（保存し直さない）

step() { printf '\n==== %s\n' "$*"; }
fail() {
  printf 'エラー: %s\n' "$*" >&2
  exit 1
}
# 秘密の値を聞く（画面に出さない）。保存済みのシークレット名を渡すと、空の Enter でその値を使う。
# 保存済みが無いときの空の入力（貼り付けの後の余分な Enter など）は聞き直す
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
# yes で進む・no で中止。それ以外（空の Enter など）は聞き直す
confirm() {
  local answer=""
  while true; do
    read -rp "$1" answer
    case "$answer" in
      yes) return 0 ;;
      no) fail "中止しました。$2" ;;
    esac
  done
}
# 値は標準入力で渡し、画面にもコマンド履歴にも出さない
save_secret() {
  local name=$1 value=$2
  if gcloud secrets describe "$name" "${LOC_FLAG[@]}" >/dev/null 2>&1; then
    printf '%s' "$value" | gcloud secrets versions add "$name" "${LOC_FLAG[@]}" --data-file=- >/dev/null
  else
    printf '%s' "$value" | gcloud secrets create "$name" "${LOC_FLAG[@]}" --data-file=- >/dev/null
  fi
  gcloud secrets add-iam-policy-binding "$name" "${LOC_FLAG[@]}" \
    --member="serviceAccount:${RUN_SA}" --role=roles/secretmanager.secretAccessor >/dev/null
  echo "保存しました: ${name}"
}
# Webhook のシークレット（Svix 形式。whsec_ の後ろが base64）として読めるか。値は標準入力で渡す
valid_webhook_secret() {
  printf '%s' "$1" | python3 -c '
import base64, sys
raw = sys.stdin.read().strip()
raw = raw[len("whsec_"):] if raw.startswith("whsec_") else raw
try:
    key = base64.b64decode(raw, validate=True)
except ValueError:
    sys.exit(1)
sys.exit(0 if len(key) >= 16 else 1)
'
}

[[ "$APP_CLIENT" =~ ^[a-z0-9][a-z0-9-]{0,31}$ ]] || fail "APP_CLIENT は英小文字・数字・ハイフン（32文字まで）にしてください"
# シークレットの名前。default（マルサン木型）は今までの名前のまま、ほかのクライアントは名前を頭に付けて分ける
secret_name() { if [ "$APP_CLIENT" = default ]; then echo "$1"; else echo "${APP_CLIENT}-$1"; fi; }
SECRET_KEY="$(secret_name recall-api-key)"
SECRET_WEBHOOK="$(secret_name recall-webhook-secret)"

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
# 1行目：Recall.ai からの通知の宛先。アプリが使うサービス URL（SERVICE_URL）にそろえる。
# 2行目：いまのボットの表示名（2回目以降の既定値にする。まだ無ければ空）
# 3行目：このクライアントがクライアント設定にあるか（yes / no）
# 4・5行目：default の recall の API キー・Webhook のシークレットの参照（default 以外で、同じアカウントを使うときに使う）
mapfile -t SVC_INFO < <(SVC_JSON="$SVC_JSON" python3 -c '
import json, os, sys
svc = json.loads(os.environ["SVC_JSON"])
env = {e.get("name"): e.get("value") for e in svc["spec"]["template"]["spec"]["containers"][0].get("env", [])}
clients = json.loads(env.get("CLIENTS_CONFIG_JSON") or "{}").get("clients", {})
recall = clients.get(sys.argv[1], {}).get("recall") or {}
default_recall = clients.get("default", {}).get("recall") or {}
print(env.get("SERVICE_URL") or svc["status"]["url"])
print(recall.get("bot_name") or "")
print("yes" if sys.argv[1] in clients else "no")
print(default_recall.get("api_key") or "")
print(default_recall.get("webhook_secret") or "")
' "$APP_CLIENT")
SERVICE_URL="${SVC_INFO[0]:-}"
CURRENT_BOT_NAME="${SVC_INFO[1]:-}"
CLIENT_EXISTS="${SVC_INFO[2]:-}"
DEFAULT_KEY_REF="${SVC_INFO[3]:-}"
DEFAULT_WEBHOOK_REF="${SVC_INFO[4]:-}"
[[ "$SERVICE_URL" == https://* ]] || fail "Cloud Run のサービス URL を読めませんでした"
WEBHOOK_URL="${SERVICE_URL%/}/webhooks/recall"
echo "プロジェクト: ${PROJECT} / サービス: ${SERVICE_URL} / シークレット: ${REF_PREFIX} / クライアント: ${APP_CLIENT}"
if [ "$APP_CLIENT" != default ]; then
  [ "$CLIENT_EXISTS" = yes ] ||
    fail "クライアント ${APP_CLIENT} がまだありません。先に APP_CLIENT=${APP_CLIENT} bash scripts/setup_client.sh を実行してください"
  if [ -n "$DEFAULT_KEY_REF" ] && [ -n "$DEFAULT_WEBHOOK_REF" ]; then
    echo "default（マルサン木型）と同じ Recall.ai のアカウント（東京リージョン）を使いますか？"
    echo "  yes: default の API キーと Webhook の送り先をそのまま使う（保存し直さない。Recall.ai に送り先を足さない）"
    echo "  no : 別のアカウントのキーを貼る（シークレットは ${SECRET_KEY} / ${SECRET_WEBHOOK} に分けて保存）"
    while true; do
      read -rp "yes / no: " _answer || fail "入力が終わりました（中止します）"
      case "$_answer" in
        yes) SHARED=1 && break ;;
        no) break ;;
      esac
    done
  fi
fi

if [ -n "$SHARED" ]; then
  step "1・2. Recall.ai の API キーと Webhook（default と同じものを使う）"
  echo "API キー: ${DEFAULT_KEY_REF}"
  echo "Webhook のシークレット: ${DEFAULT_WEBHOOK_REF}"
  echo "Recall.ai のダッシュボードに送り先を足さないでください（同じ URL の送り先が2つあると、通知が2回届きます）。"
  echo "通知は、ボットの metadata の client_id（${APP_CLIENT}）で振り分けます。"
else
  step "1. Recall.ai の API キー（東京リージョン）"
  STORED_KEY=""
  if gcloud secrets describe "$SECRET_KEY" "${LOC_FLAG[@]}" >/dev/null 2>&1; then
    STORED_KEY="$SECRET_KEY"
    echo "保存済みの API キーがあります。そのまま Enter で保存済みのキーを使います（新しいキーに替えるときだけ貼る）。"
  else
    echo "Recall.ai にログインし、リージョンが東京（ap-northeast-1）のダッシュボードで API キーを作って、貼って Enter を押してください。"
    echo "（API キーはリージョンごとに別です。ほかのリージョンのキーは使えません）"
  fi
  export RECALL_BASE_URL
  KEY_OK=""
  for _ in 1 2 3; do
    ask_secret RECALL_API_KEY "API キー（表示されません）: " "$STORED_KEY"
    export RECALL_API_KEY
    if python3 scripts/recall_check.py check-key; then
      KEY_OK=1
      break
    fi
    echo "もう一度貼ってください。"
  done
  [ -n "$KEY_OK" ] || fail "API キーを確かめられませんでした。何も保存していません"

  step "2. Webhook（Recall.ai からバックエンドへの通知）"
  STORED_SECRET=""
  if gcloud secrets describe "$SECRET_WEBHOOK" "${LOC_FLAG[@]}" >/dev/null 2>&1; then
    STORED_SECRET="$SECRET_WEBHOOK"
    echo "保存済みのシークレットがあります。Recall.ai の送り先を作り直していなければ、そのまま Enter で保存済みのものを使います。"
  fi
  echo "送り先がまだ無ければ、Recall.ai のダッシュボードの左の「Webhooks」→「Add Endpoint」で追加してください:"
  echo "  Endpoint URL: ${WEBHOOK_URL}"
  echo "  Description: 空でよい"
  echo "  Subscribe to events: 「bot」と「sdk_upload」にチェック（1つ以上選ばないと作れません。ほかは不要）"
  echo "作った送り先の画面にある Signing Secret（whsec_ で始まるもの）をコピーして貼ってください。"
  echo "（2026-10-01 に、この送り先の Signing Secret で実際の通知の署名が合うことを確認済み）"
  SECRET_OK=""
  for _ in 1 2 3; do
    ask_secret RECALL_WEBHOOK_SECRET "Webhook のシークレット（表示されません）: " "$STORED_SECRET"
    if valid_webhook_secret "$RECALL_WEBHOOK_SECRET"; then
      SECRET_OK=1
      break
    fi
    echo "シークレットの形ではありません（whsec_ で始まる値を、最後の文字まで貼ってください）。"
  done
  [ -n "$SECRET_OK" ] || fail "Webhook のシークレットを確かめられませんでした。何も保存していません"
fi

step "3. ボットの表示名（会議の参加者に見える名前。録音していると分かる名前にする）"
BOT_NAME_DEFAULT="${CURRENT_BOT_NAME:-$DEFAULT_BOT_NAME}"
echo "打ち間違えたら、矢印キーと Backspace で直せます。変えないときは、何も入れずに Enter。"
# -e: 矢印キーで文字を直せるようにする（-e が無いと矢印キーが ^[[D のような文字として入ってしまう）
read -erp "表示名（そのまま Enter で「${BOT_NAME_DEFAULT}」）: " BOT_NAME || true
BOT_NAME="${BOT_NAME:-$BOT_NAME_DEFAULT}"
[ "${#BOT_NAME}" -le 100 ] || fail "表示名は100文字までにしてください"
[[ "$BOT_NAME" != *@@* ]] || fail "表示名に @@ は使えません"
[[ "$BOT_NAME" != *$'\e'* ]] || fail "表示名に矢印キーなどの制御文字が入りました。もう一度実行してください"

step "4. Secret Manager に保存"
confirm "Secret Manager に保存し（保存済みの値をそのまま使ったものは保存し直しません）、Cloud Run の設定を更新します。よろしければ yes、やめるなら no: " \
  "何も保存していません。"
if [ -n "$SHARED" ]; then
  echo "API キーと Webhook のシークレットは default のものを参照します（保存し直しません）"
  KEY_REF="$DEFAULT_KEY_REF"
  WEBHOOK_REF="$DEFAULT_WEBHOOK_REF"
else
  if reused "$SECRET_KEY"; then
    echo "API キーは保存済みのものをそのまま使います"
  else
    save_secret "$SECRET_KEY" "$RECALL_API_KEY"
  fi
  if reused "$SECRET_WEBHOOK"; then
    echo "Webhook のシークレットは保存済みのものをそのまま使います"
  else
    save_secret "$SECRET_WEBHOOK" "$RECALL_WEBHOOK_SECRET"
  fi
  KEY_REF="sm:${REF_PREFIX}/${SECRET_KEY}/versions/latest"
  WEBHOOK_REF="sm:${REF_PREFIX}/${SECRET_WEBHOOK}/versions/latest"
fi
unset RECALL_API_KEY RECALL_WEBHOOK_SECRET

step "5. Cloud Run のクライアント設定に recall を足す（DRY_RUN は変えません。数分かかります）"
CONFIG_OUT="$(SVC_JSON="$SVC_JSON" BOT_NAME="$BOT_NAME" python3 - "$APP_CLIENT" "$KEY_REF" "$WEBHOOK_REF" "$RECALL_BASE_URL" <<'PY'
import json
import os
import sys

app_client, key_ref, webhook_ref, base_url = sys.argv[1:5]
svc = json.loads(os.environ["SVC_JSON"])
env = {e.get("name"): e.get("value") for e in svc["spec"]["template"]["spec"]["containers"][0].get("env", [])}
clients = json.loads(env.get("CLIENTS_CONFIG_JSON") or "{}")
client = clients.setdefault("clients", {}).setdefault(app_client, {})
recall = client.get("recall") or {}
recall.update(
    {
        "base_url": base_url,
        "api_key": key_ref,
        "webhook_secret": webhook_ref,
        "bot_name": os.environ["BOT_NAME"],
    }
)
# 文字起こしは会議のあとに日本語で依頼する（未設定のときだけ既定を入れる。docs/unverified-apis.md の H4）
recall.setdefault("transcription_mode", "async")
recall.setdefault("transcript_request", {"provider": {"recallai_async": {"language_code": "ja"}}})
recall.setdefault("join_lead_seconds", 60)
client["recall"] = recall
# 1行目は環境変数に渡す形（ASCII だけ。日本語は \u 表記でアプリはそのまま読める）、2行目は画面に出す形
print(json.dumps(clients, separators=(",", ":")))
print(json.dumps(clients, ensure_ascii=False, separators=(",", ":")))
PY
)"
NEW_CLIENTS="$(sed -n 1p <<<"$CONFIG_OUT")"
SHOWN="$(sed -n 2p <<<"$CONFIG_OUT")"
echo "クライアント設定（秘密情報は参照だけ）: ${SHOWN}"
# JSON にカンマが含まれるので、区切り文字を @@ に変えて渡す。
# 動いているインスタンスが新しいシークレットを読むように、設定が同じでも新しい版を出す（RECALL_CONFIG_UPDATED_AT）
gcloud run services update "$SERVICE" --region="$REGION" --quiet \
  --update-env-vars="^@@^CLIENTS_CONFIG_JSON=${NEW_CLIENTS}@@RECALL_CONFIG_UPDATED_AT=$(date -u +%Y%m%dT%H%M%SZ)"
curl -fsS "${SERVICE_URL%/}/health"
echo

if [ -n "$SHARED" ]; then
  step "完了"
  echo "Recall.ai の設定を ${APP_CLIENT} に反映しました（default と同じアカウント。ボットの表示名: ${BOT_NAME}）。"
  echo "Webhook の送り先は default と共通なので、テスト送信は不要です。次は docs/monodemo.md の手順へ。"
  exit 0
fi

step "6. Webhook のテスト送信（任意。飛ばすときは Enter だけ）"
echo "ダッシュボードの左の「Webhooks」で送り先の URL をクリック →「Testing」タブで、"
echo "イベントの種類に bot.joining_call を選んで「Send Example」を押してください（受け取っても何も書き込まない種類）。"
echo "（Testing タブが見つからなければ飛ばしてよい。実際のボットのテストで、状態が「参加待ち」「録音中」に変われば署名は合っています）"
read -rp "送ったら Enter（送らずに飛ばすときも Enter）: " _ || true
echo "20秒待ってから、バックエンドのログを確かめます..."
sleep 20
gcloud logging read "resource.type=\"cloud_run_revision\" AND (jsonPayload.message=\"webhook.received\" OR jsonPayload.message=\"webhook.ignored\" OR jsonPayload.message=\"webhook.client_unresolved\" OR (httpRequest.requestUrl:\"/webhooks/recall\" AND httpRequest.status>=400))" \
  --project="$PROJECT" --freshness=10m --limit=10 \
  --format='table(timestamp.date("%H:%M:%S",tz=Asia/Tokyo),jsonPayload.message,jsonPayload.event,httpRequest.status)'
echo "webhook.received・webhook.ignored・webhook.client_unresolved のどれかが出ていれば、署名が合っています（どれでも OK。"
echo "テスト送信の例にはクライアントの印（metadata の client_id）が無いので、同じアカウントを使うクライアントが複数あると client_unresolved になる）。"
echo "状態が 401 の行だけなら、シークレットが違います（送り先の Signing Secret を貼り直して、このスクリプトをもう一度実行）。"
echo "何も出なければ、テスト送信をしていないか、送り先 URL が違います: ${WEBHOOK_URL}"

step "完了"
echo "Recall.ai の設定をバックエンドに反映しました（ボットの表示名: ${BOT_NAME}）。"
echo "CRM の関数とワークフローがまだなら docs/recall-bot.md の手順2へ、作り済みなら手順3（試す）へ進んでください。"
