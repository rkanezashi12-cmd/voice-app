#!/usr/bin/env bash
# 手元（Cloud Shell）のコードを Cloud Run にデプロイする。環境変数（DRY_RUN・クライアント設定・キュー・モデルなど）は
# そのまま引き継ぎ、どのコミットを動かしているかを APP_COMMIT に残す（Cloud Shell で実行する想定）。
#
#   cd ~/voice-app && git pull && bash scripts/deploy.sh
#
# 環境変数の設定を変えるだけ（gcloud run services update）ではコードは入れ替わらない。コードを直したらこれを実行する。
# README の --env-vars-file を使うデプロイは環境変数を丸ごと置き換えるので、初回以外は使わない。
set -euo pipefail
cd "$(dirname "$0")/.."

PROJECT="${PROJECT:-voice-ai-510014}"
REGION="asia-northeast1"
SERVICE="meeting-notes"
RUN_SA="meeting-notes-run@${PROJECT}.iam.gserviceaccount.com"
fail() {
  printf 'エラー: %s\n' "$*" >&2
  exit 1
}

[ -z "$(git status --porcelain --untracked-files=no)" ] ||
  fail "コミットしていない変更があります（git status で確認し、元に戻してから実行してください）"
COMMIT="$(git rev-parse --short HEAD)"
echo "デプロイするコード: $(git rev-parse --abbrev-ref HEAD) ${COMMIT}（$(git log -1 --format=%s)）"
echo "Cloud Build でイメージを作るので、数分かかります。"

# 既存のサービスの設定（環境変数・公開設定・インスタンス数など）は、指定しないものはそのまま残る
gcloud run deploy "$SERVICE" --project="$PROJECT" --region="$REGION" --source=. \
  --service-account="$RUN_SA" --quiet \
  --update-env-vars="APP_COMMIT=${COMMIT}"

SERVICE_URL="$(gcloud run services describe "$SERVICE" --project="$PROJECT" --region="$REGION" --format='value(status.url)')"
curl -fsS "${SERVICE_URL}/health"
echo
echo "デプロイしました（${COMMIT}）。DRY_RUN などの設定は変えていません。"
