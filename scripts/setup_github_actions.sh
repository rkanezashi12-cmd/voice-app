#!/usr/bin/env bash
# GitHub Actions から GCP を操作できるようにする（Cloud Shell で1回だけ。何度実行してもよい）。
# これ以降のデプロイ・再処理・クライアントの切り替え・費用の確認は、GitHub Actions（.github/workflows/ops.yml）で行う。
#
#   cd ~/voice-app && git pull && bash scripts/setup_github_actions.sh
#
# 作るもの（鍵ファイルは作らない。Workload Identity 連携）:
#   - Workload Identity プール github・プロバイダ github（このリポジトリの既定のブランチから動いたワークフローだけを通す）
#   - サービスアカウント meeting-notes-deployer と、その最小の権限
#       run.developer（デプロイ・環境変数の変更）、logging.viewer（ログを読む）、cloudtasks.enqueuer（再処理を積む）、
#       secretmanager.viewer（シークレットの「版の番号」だけ。値は読めない）、serviceusage.apiKeysViewer（地図のキーの ID）、
#       Artifact Registry への書き込み（cloud-run-source-deploy だけ）、
#       実行用の SA（meeting-notes-run）と Cloud Tasks の SA（meeting-notes-tasks）を使う権限
# 渡さないもの：シークレットの値の読み取り、IAM の変更、CRM の接続の作り直し（認可コードが要るので Cloud Shell で行う）
set -euo pipefail
cd "$(dirname "$0")/.."

PROJECT="${PROJECT:-voice-ai-510014}"
REGION="asia-northeast1"
REPO="${GITHUB_REPO:-rkanezashi12-cmd/voice-app}"
BRANCH="${GITHUB_BRANCH:-claude/meeting-transcription-app-setup-nkaz1m}"
POOL="github"
PROVIDER="github"
DEPLOYER="meeting-notes-deployer"
DEPLOYER_SA="${DEPLOYER}@${PROJECT}.iam.gserviceaccount.com"
RUN_SA="meeting-notes-run@${PROJECT}.iam.gserviceaccount.com"
TASKS_SA="meeting-notes-tasks@${PROJECT}.iam.gserviceaccount.com"
AR_REPO="cloud-run-source-deploy"

step() { printf '\n==== %s\n' "$*"; }
fail() {
  printf 'エラー: %s\n' "$*" >&2
  exit 1
}
# IAM の反映待ちで失敗することがあるので、数回やり直す
retry() {
  local i
  for i in 1 2 3 4 5; do
    if "$@" >/dev/null 2>&1; then return 0; fi
    sleep $((i * 5))
  done
  "$@" >/dev/null
}

NUMBER="$(gcloud projects describe "$PROJECT" --format='value(projectNumber)')" || fail "プロジェクト ${PROJECT} が見つかりません"
echo "プロジェクト: ${PROJECT}（番号 ${NUMBER}）"
echo "リポジトリ: ${REPO}（ブランチ ${BRANCH} から動いたワークフローだけ）"
answer=""
until [ "$answer" = yes ] || [ "$answer" = y ]; do
  read -rp "上のプロジェクトに、GitHub Actions 用の設定を作ります。よろしければ yes: " answer
  [ "$answer" = no ] && fail "中止しました。何も変えていません。"
done

step "1/4 API"
retry gcloud services enable iam.googleapis.com iamcredentials.googleapis.com sts.googleapis.com \
  artifactregistry.googleapis.com --project="$PROJECT"

step "2/4 サービスアカウント ${DEPLOYER}"
gcloud iam service-accounts describe "$DEPLOYER_SA" --project="$PROJECT" >/dev/null 2>&1 ||
  gcloud iam service-accounts create "$DEPLOYER" --project="$PROJECT" --display-name="GitHub Actions（デプロイ・運用）" >/dev/null
for role in roles/run.developer roles/logging.viewer roles/cloudtasks.enqueuer roles/secretmanager.viewer \
  roles/serviceusage.apiKeysViewer; do
  retry gcloud projects add-iam-policy-binding "$PROJECT" --member="serviceAccount:${DEPLOYER_SA}" --role="$role" --condition=None
done
retry gcloud artifacts repositories add-iam-policy-binding "$AR_REPO" --project="$PROJECT" --location="$REGION" \
  --member="serviceAccount:${DEPLOYER_SA}" --role=roles/artifactregistry.writer
for sa in "$RUN_SA" "$TASKS_SA"; do
  retry gcloud iam service-accounts add-iam-policy-binding "$sa" --project="$PROJECT" \
    --member="serviceAccount:${DEPLOYER_SA}" --role=roles/iam.serviceAccountUser
done

step "3/4 Workload Identity 連携（${REPO} の ${BRANCH} だけ）"
gcloud iam workload-identity-pools describe "$POOL" --project="$PROJECT" --location=global >/dev/null 2>&1 ||
  gcloud iam workload-identity-pools create "$POOL" --project="$PROJECT" --location=global \
    --display-name="GitHub Actions" >/dev/null
CONDITION="assertion.repository=='${REPO}' && assertion.ref=='refs/heads/${BRANCH}'"
MAPPING="google.subject=assertion.sub,attribute.repository=assertion.repository,attribute.ref=assertion.ref"
if gcloud iam workload-identity-pools providers describe "$PROVIDER" --project="$PROJECT" --location=global \
  --workload-identity-pool="$POOL" >/dev/null 2>&1; then
  gcloud iam workload-identity-pools providers update-oidc "$PROVIDER" --project="$PROJECT" --location=global \
    --workload-identity-pool="$POOL" --attribute-condition="$CONDITION" --attribute-mapping="$MAPPING" >/dev/null
else
  gcloud iam workload-identity-pools providers create-oidc "$PROVIDER" --project="$PROJECT" --location=global \
    --workload-identity-pool="$POOL" --issuer-uri="https://token.actions.githubusercontent.com" \
    --attribute-condition="$CONDITION" --attribute-mapping="$MAPPING" >/dev/null
fi
retry gcloud iam service-accounts add-iam-policy-binding "$DEPLOYER_SA" --project="$PROJECT" \
  --role=roles/iam.workloadIdentityUser \
  --member="principalSet://iam.googleapis.com/projects/${NUMBER}/locations/global/workloadIdentityPools/${POOL}/attribute.repository/${REPO}"

step "4/4 確認"
WIF_PROVIDER="projects/${NUMBER}/locations/global/workloadIdentityPools/${POOL}/providers/${PROVIDER}"
EXPECTED="$(grep '^WIF_PROVIDER=' deploy/ops.env | cut -d= -f2-)"
if [ "$EXPECTED" = "$WIF_PROVIDER" ]; then
  echo "OK：deploy/ops.env の WIF_PROVIDER と一致しています。"
else
  echo "deploy/ops.env の WIF_PROVIDER と違います。次の値を Claude に貼ってください:"
  echo "  WIF_PROVIDER=${WIF_PROVIDER}"
fi
echo
echo "終わりました。これ以降は GitHub Actions（ops）でデプロイ・再処理などを行えます。"
echo "Cloud Shell が要るのは、CRM・Recall.ai などの接続を作り直すとき（認可コードの入力）だけです。"
