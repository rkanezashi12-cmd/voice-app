#!/usr/bin/env bash
# CRM のワークフローなどがバックエンドを呼ぶときの API キーを新しくし、
# CRM の変数に貼るために1度だけ表示する（Cloud Shell で実行する想定）。
#
#   cd ~/voice-app && bash scripts/rotate_api_key.sh                       # マルサン木型（default。backend-api-key）
#   cd ~/voice-app && APP_CLIENT=monodemo bash scripts/rotate_api_key.sh   # ほかのクライアント（monodemo-backend-api-key）
#
# クライアントごとに別のシークレットなので、ほかのクライアントのキーは変わらない。
# 行うこと:
#   1. 新しい API キーを作り、Secret Manager の backend-api-key に新しい版として保存する
#   2. 古い版を無効にする（古いキーは使えなくなる）
#   3. Cloud Run の新しい版を出す（動いているインスタンスは古いキーを覚えているため）
#   4. 画面を消してから新しいキーを表示し、Enter で画面から消す
set -euo pipefail
cd "$(dirname "$0")/.."

PROJECT="${PROJECT:-voice-ai-510014}"
REGION="asia-northeast1"
SERVICE="meeting-notes"
APP_CLIENT="${APP_CLIENT:-default}"
[[ "$APP_CLIENT" =~ ^[a-z0-9][a-z0-9-]{0,31}$ ]] || {
  echo "エラー: APP_CLIENT は英小文字・数字・ハイフン（32文字まで）にしてください" >&2
  exit 1
}
# default（マルサン木型）は今までの名前のまま、ほかのクライアントは名前を頭に付けて分ける
if [ "$APP_CLIENT" = default ]; then SECRET="backend-api-key"; else SECRET="${APP_CLIENT}-backend-api-key"; fi

gcloud config set project "$PROJECT" >/dev/null
if gcloud secrets describe "$SECRET" >/dev/null 2>&1; then
  LOC_FLAG=()
elif gcloud secrets describe "$SECRET" --location="$REGION" >/dev/null 2>&1; then
  LOC_FLAG=(--location="$REGION")
else
  echo "エラー: シークレット ${SECRET} が見つかりません（default 以外は先に APP_CLIENT=${APP_CLIENT} bash scripts/setup_client.sh）" >&2
  exit 1
fi

echo "クライアント ${APP_CLIENT} の API キー（${SECRET}）を新しくしています（1〜2分かかります）..."
OLD_VERSIONS="$(gcloud secrets versions list "$SECRET" "${LOC_FLAG[@]}" --filter='state=ENABLED' --format='value(name)')"
NEW_KEY="$(python3 -c 'import secrets; print(secrets.token_hex(32))')"
# 値は標準入力で渡し、コマンド履歴に残さない
printf '%s' "$NEW_KEY" | gcloud secrets versions add "$SECRET" "${LOC_FLAG[@]}" --data-file=- >/dev/null
for v in $OLD_VERSIONS; do
  gcloud secrets versions disable "${v##*/}" --secret="$SECRET" "${LOC_FLAG[@]}" --quiet >/dev/null
done
gcloud run services update "$SERVICE" --region="$REGION" --quiet \
  --update-env-vars="API_KEY_ROTATED_AT=$(date -u +%Y%m%dT%H%M%SZ)" >/dev/null

clear
echo "新しい API キー（クライアント ${APP_CLIENT} の CRM の変数 meeting_notes_api_key の値に貼ってください）:"
echo
echo "$NEW_KEY"
echo
echo "ダブルクリックで選んで、右クリック →「コピー」。"
echo "この画面はスクリーンショットしないでください。貼り終わったら Enter を押すと、画面から消します。"
read -r _
clear
echo "クライアント ${APP_CLIENT} の API キーを新しくしました（古いキーは使えません。ほかのクライアントのキーは変わりません）。"
