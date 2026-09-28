# 商談の文字起こし・要約アプリ（バックエンド＋対面録音ページ）

オンライン商談（Zoom / Teams / Google Meet）と対面商談の会話を文字起こしし、Gemini で補正・要約して
Zoho CRM の「商談記録」に保存する（Zoho の DC はクライアント設定で切り替える。デモ環境は US）。前提とルールは [CLAUDE.md](CLAUDE.md) を参照。

| 入口 | 仕組み | 状態 |
|---|---|---|
| A ボット参加（社内デモ・予備） | CRM ワークフロー → `POST /api/bots` → Recall.ai Meeting Bot | Step 2 |
| B 対面録音 | CRM ワークフロー → `POST /api/recordings` → スマホの録音ページ | **Step 1（録音テスト可）** |
| C デスクトップアプリ（本番の標準） | Electron ＋ Recall.ai Desktop Recording SDK | 受け口は Step 2、アプリは Phase 2 |

- 開発・デモの接続先は **お客様の本番 Zoho CRM**。`DRY_RUN=true`（既定）では CRM への書き込みと音声の削除を行わない。
- 実書き込みはユーザーの指示があるときだけ `DRY_RUN=false` にする。

---

## 1. 録音テスト用の GCP 設定（Step 1）

iPhone で録音ページを試すための最小構成。プロジェクトは `voice-ai-510014`（API 有効化・シークレット登録済み）。
リソース名は計画の推奨案（B2）のまま。変える場合はコマンドと `config/env.*.yaml` を合わせて直す。

```bash
PROJECT=voice-ai-510014
REGION=asia-northeast1
BUCKET=${PROJECT}-meeting-audio
RUN_SA=meeting-notes-run@${PROJECT}.iam.gserviceaccount.com
```

### 1-1. 録音の一時保存バケット（1日で自動削除）

```bash
gcloud storage buckets create gs://$BUCKET --project=$PROJECT --location=$REGION \
  --uniform-bucket-level-access --public-access-prevention

cat > /tmp/lifecycle.json <<'EOF'
{"rule": [{"action": {"type": "Delete"}, "condition": {"age": 1}}]}
EOF
gcloud storage buckets update gs://$BUCKET --lifecycle-file=/tmp/lifecycle.json
```

### 1-2. 実行用サービスアカウント

```bash
gcloud iam service-accounts create meeting-notes-run --project=$PROJECT \
  --display-name="meeting-notes (Cloud Run)"

# 録音の保存・一覧・削除
gcloud storage buckets add-iam-policy-binding gs://$BUCKET \
  --member=serviceAccount:$RUN_SA --role=roles/storage.objectAdmin

# 鍵ファイルなしで署名付き URL を作るため、自分自身のトークン作成（signBlob）を許可
gcloud iam service-accounts add-iam-policy-binding $RUN_SA --project=$PROJECT \
  --member=serviceAccount:$RUN_SA --role=roles/iam.serviceAccountTokenCreator

# 使うシークレットだけ読めるようにする
for s in backend-api-key recording-token-secret recall-api-key recall-webhook-secret \
         zoho-client-id zoho-client-secret zoho-refresh-token; do
  gcloud secrets add-iam-policy-binding $s --project=$PROJECT \
    --member=serviceAccount:$RUN_SA --role=roles/secretmanager.secretAccessor
done
```

リージョン シークレット（`projects/…/locations/asia-northeast1/secrets/…`）の場合は、`gcloud secrets` の各コマンドに
`--location=$REGION` を付け、参照名も `sm:projects/$PROJECT/locations/$REGION/secrets/<名前>/versions/latest` にする。
アプリはどちらの形でも読める。

### 1-3. デプロイ（初回は手動。GitHub Actions からのデプロイは Step 5 で用意）

`gcloud run deploy --source` は Cloud Build でイメージを作るため、Cloud Build API も有効にする。

```bash
gcloud services enable cloudbuild.googleapis.com --project=$PROJECT

cp config/env.example.yaml config/env.dev.local.yaml   # 値を確認する（SERVICE_URL は次の手順で埋める）

gcloud run deploy meeting-notes --project=$PROJECT --region=$REGION --source=. \
  --service-account=$RUN_SA --allow-unauthenticated \
  --max-instances=3 --timeout=1800 --memory=1Gi \
  --env-vars-file=config/env.dev.local.yaml
```

- `--allow-unauthenticated`：録音ページ・CRM ワークフロー・Recall.ai の Webhook から呼ばれるため公開する。
  各 API はアプリ側で認証する（X-API-Key / 録音トークン / Webhook 署名 / Cloud Tasks の OIDC）。
- `--max-instances=3`：Zoho のアクセストークン発行回数の上限（10 分で 10 回）に掛からないように絞る。

### 1-4. サービス URL の設定と CORS

```bash
SERVICE_URL=$(gcloud run services describe meeting-notes --project=$PROJECT --region=$REGION \
  --format='value(status.url)')
gcloud run services update meeting-notes --project=$PROJECT --region=$REGION \
  --update-env-vars=SERVICE_URL=$SERVICE_URL

# 録音ページ（サービスの URL）から GCS への直接アップロードを許可する
cat > /tmp/cors.json <<EOF
[{"origin": ["$SERVICE_URL"], "method": ["PUT"], "responseHeader": ["Content-Type"], "maxAgeSeconds": 3600}]
EOF
gcloud storage buckets update gs://$BUCKET --cors-file=/tmp/cors.json

curl -s $SERVICE_URL/healthz   # {"status":"ok","dry_run":true}
```

### 1-5. iPhone テスト

[docs/recorder-test.md](docs/recorder-test.md) の手順で行う。テスト用 URL は CRM に何も書かない。

---

## 2. 開発

```bash
python3.12 -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
ruff check . && ruff format --check .
pytest

node --test tests/js/*.test.mjs                        # 録音ページの部品
npm install --no-save playwright@1.56.1 && npx playwright install chromium
node tests/e2e/recorder.e2e.mjs                        # 擬似マイクで録音ページを通しで確認
```

- テストは外部 API をすべて偽物に置き換え、実際の通信は遮断している（本番 CRM に触れない）。
- GitHub Actions（`.github/workflows/test.yml`）で push のたびに同じテストが動く。
- ローカルで API を動かすときは `TASKS_BACKEND=local` にすると Cloud Tasks を使わずに同じプロセスで処理する。

## 3. ドキュメント

| ファイル | 内容 |
|---|---|
| [CLAUDE.md](CLAUDE.md) | 前提・絶対ルール・処理の流れ |
| [docs/recorder-test.md](docs/recorder-test.md) | 対面録音ページの iPhone テスト手順と記録表 |
| [config/clients.example.json](config/clients.example.json) | クライアント（テナント）設定の例 |
| [config/env.example.yaml](config/env.example.yaml) | Cloud Run の環境変数の例 |
