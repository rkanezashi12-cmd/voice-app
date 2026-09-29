#!/usr/bin/env bash
# バックエンド（Cloud Run）から Zoho CRM に接続する設定を行う（Cloud Shell で実行する想定）。
#
#   cd ~/voice-app && bash scripts/setup_zoho_connection.sh
#
# 行うこと（何度実行しても壊れない。CRM には何も書き込まない）:
#   1. Self Client の Client ID / Secret と認可コードを聞き、リフレッシュトークンに交換する（画面に出さない）
#   2. 接続先の組織を表示し、合っているか確かめる（違えば何も保存せずに止まる）
#   3. 商談記録・用語辞書の項目が読めるか確かめる
#   4. Secret Manager に保存する（zoho-client-id / zoho-client-secret / zoho-refresh-token）
#   5. Cloud Run のクライアント設定（CLIENTS_CONFIG_JSON）に zoho を足す（DRY_RUN は変えない）
set -euo pipefail
cd "$(dirname "$0")/.."

PROJECT="${PROJECT:-voice-ai-510014}"
REGION="asia-northeast1"
SERVICE="meeting-notes"
RUN_SA="meeting-notes-run@${PROJECT}.iam.gserviceaccount.com"
APP_CLIENT="${APP_CLIENT:-default}" # クライアント設定のキー
DC="${DC:-us}"                      # Zoho の DC（マルサン木型は us）
# CLAUDE.md の8つ＋接続先の組織の確認に使う ZohoCRM.org.READ（読み取りのみ）
SCOPES="ZohoCRM.modules.custom.ALL,ZohoCRM.modules.accounts.READ,ZohoCRM.modules.contacts.READ,ZohoCRM.modules.deals.READ,ZohoCRM.coql.READ,ZohoCRM.users.READ,ZohoCRM.settings.modules.READ,ZohoCRM.settings.fields.READ,ZohoCRM.org.READ"

step() { printf '\n==== %s\n' "$*"; }
fail() {
  printf 'エラー: %s\n' "$*" >&2
  exit 1
}

# 空の入力（貼り付けの後の余分な Enter など）は聞き直す
ask() {
  local __var=$1 __prompt=$2 __hidden=${3:-} __value=""
  while [ -z "$__value" ]; do
    if [ -n "$__hidden" ]; then
      read -rsp "$__prompt" __value
      echo
    else
      read -rp "$__prompt" __value
    fi
  done
  printf -v "$__var" '%s' "$__value"
}

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

case "$DC" in
  us) export ZOHO_DC=com ;;
  au) export ZOHO_DC=com.au ;;
  jp | eu | in) export ZOHO_DC="$DC" ;;
  *) fail "DC は us / jp / eu / in / au のどれかにしてください" ;;
esac

step "0. 事前確認"
gcloud config set project "$PROJECT" >/dev/null
# シークレットが通常かリージョン シークレットかを判定する（scripts/setup_recorder_test.sh と同じ）
if gcloud secrets describe recording-token-secret >/dev/null 2>&1; then
  LOC_FLAG=()
  REF_PREFIX="projects/${PROJECT}/secrets"
elif gcloud secrets describe recording-token-secret --location="$REGION" >/dev/null 2>&1; then
  LOC_FLAG=(--location="$REGION")
  REF_PREFIX="projects/${PROJECT}/locations/${REGION}/secrets"
else
  fail "シークレット recording-token-secret が見つかりません（先に scripts/setup_recorder_test.sh を実行）"
fi
SERVICE_URL="$(gcloud run services describe "$SERVICE" --region="$REGION" --format='value(status.url)' 2>/dev/null)" ||
  fail "Cloud Run のサービス ${SERVICE} が見つかりません（先に scripts/setup_recorder_test.sh を実行）"
echo "プロジェクト: ${PROJECT} / サービス: ${SERVICE_URL} / シークレット: ${REF_PREFIX}"

step "1. Self Client の Client ID と Secret"
echo "api-console.zoho.com を、バックエンドを動かす Zoho ユーザー（マルサン木型の管理者）で開き、"
echo "Self Client の「Client Secret」タブの値を1つずつ貼って Enter を押してください。"
ask ZOHO_CLIENT_ID "Client ID: "
ask ZOHO_CLIENT_SECRET "Client Secret（表示されません）: " hidden
[[ "$ZOHO_CLIENT_ID" == 1000.* ]] || fail "Client ID は 1000. で始まります。貼った値を確かめてください"
export ZOHO_CLIENT_ID ZOHO_CLIENT_SECRET

step "2. 認可コード（10分で失効します。発行したらすぐ貼ってください）"
echo "同じ Self Client の「Generate Code」タブで次を入れて「Create」を押してください。"
echo "  Scope: ${SCOPES}"
echo "  Time Duration: 10 minutes"
echo "  Scope Description: meeting-notes backend"
echo "  組織を選ぶ画面が出たら、接続先の本番組織（マルサン木型）を選ぶ"
ZOHO_REFRESH_TOKEN=""
for _ in 1 2 3; do
  ask CODE "認可コード: "
  ZOHO_REFRESH_TOKEN="$(python3 scripts/crm_setup.py exchange-code "$CODE" |
    sed -n "s/^export ZOHO_REFRESH_TOKEN='\(.*\)'\$/\1/p" || true)"
  [ -n "$ZOHO_REFRESH_TOKEN" ] && break
  echo "取得できませんでした（上のエラーを確認）。invalid_code なら、認可コードを発行し直して貼ってください。"
done
unset CODE
[ -n "$ZOHO_REFRESH_TOKEN" ] ||
  fail "リフレッシュトークンを取得できませんでした。invalid_client なら Client ID / Secret を確かめて、最初からやり直してください"
export ZOHO_REFRESH_TOKEN
echo "リフレッシュトークンを取得しました（${#ZOHO_REFRESH_TOKEN}文字。画面には出しません）"

step "3. 接続先の組織（書き込みなし）"
ORG_OUT="$(python3 scripts/crm_setup.py show-org)" || fail "組織の情報を取得できませんでした（上のエラーを確認）"
printf '%s\n' "$ORG_OUT" | grep -E '^(company_name|domain_name|id|time_zone|currency|type): '
EXPECTED_ORG_ID="$(printf '%s\n' "$ORG_OUT" | sed -n 's/^id: //p')"
EXPECTED_ORG_DOMAIN="$(printf '%s\n' "$ORG_OUT" | sed -n 's/^domain_name: //p')"
EXPECTED_COMPANY_NAME="$(printf '%s\n' "$ORG_OUT" | sed -n 's/^company_name: //p')"
export EXPECTED_ORG_ID EXPECTED_ORG_DOMAIN EXPECTED_COMPANY_NAME
confirm "上の組織（会社名と type: production）で合っていれば yes、違えば no: " \
  "何も保存していません。認可コードの発行で組織の選択を確かめてください。"

step "4. 商談記録・用語辞書の項目が読めるか（書き込みなし）"
FIELDS_OUT="$(python3 scripts/crm_setup.py show-fields)" || fail "項目を読めませんでした（上のエラーを確認）"
printf '%s\n' "$FIELDS_OUT"
if printf '%s\n' "$FIELDS_OUT" | grep -q ': ありません$'; then
  fail "足りない項目があります。docs/crm-setup.md の手順で作成してから、もう一度実行してください"
fi

step "5. Secret Manager に保存"
confirm "Client ID・Secret・リフレッシュトークンを保存し、Cloud Run の設定に足します。よろしければ yes、やめるなら no: " \
  "何も保存していません。"
save_secret zoho-client-id "$ZOHO_CLIENT_ID"
save_secret zoho-client-secret "$ZOHO_CLIENT_SECRET"
save_secret zoho-refresh-token "$ZOHO_REFRESH_TOKEN"

step "6. Cloud Run のクライアント設定に zoho を足す（DRY_RUN は変えません。数分かかります）"
CURRENT_JSON="$(gcloud run services describe "$SERVICE" --region="$REGION" --format=json)"
NEW_CLIENTS="$(CURRENT_JSON="$CURRENT_JSON" python3 - "$APP_CLIENT" "$DC" "$REF_PREFIX" <<'PY'
import json
import os
import sys

app_client, dc, prefix = sys.argv[1:4]
svc = json.loads(os.environ["CURRENT_JSON"])
env = {e.get("name"): e.get("value") for e in svc["spec"]["template"]["spec"]["containers"][0].get("env", [])}
clients = json.loads(env.get("CLIENTS_CONFIG_JSON") or "{}")
client = clients.setdefault("clients", {}).setdefault(app_client, {})
names = {"client_id": "zoho-client-id", "client_secret": "zoho-client-secret", "refresh_token": "zoho-refresh-token"}
client["zoho"] = {"dc": dc, **{key: f"sm:{prefix}/{name}/versions/latest" for key, name in names.items()}}
print(json.dumps(clients, ensure_ascii=False, separators=(",", ":")))
PY
)"
echo "クライアント設定（秘密情報は参照だけ）: ${NEW_CLIENTS}"
# JSON にカンマが含まれるので、区切り文字を @@ に変えて渡す
gcloud run services update "$SERVICE" --region="$REGION" --quiet \
  --update-env-vars="^@@^CLIENTS_CONFIG_JSON=${NEW_CLIENTS}"
curl -fsS "${SERVICE_URL}/health"
echo

step "完了"
echo "バックエンドが Zoho CRM（${EXPECTED_COMPANY_NAME}）に接続できるようになりました。"
echo "DRY_RUN は true のままなので、CRM には書き込みません。"
echo "この Self Client はバックエンドが使い続けます。api-console で削除しないでください。"
