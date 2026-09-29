# 商談の文字起こし・要約アプリ（バックエンド＋対面録音ページ）

オンライン商談（Zoom / Teams / Google Meet）と対面商談の会話を文字起こしし、Gemini で補正・要約して
Zoho CRM の「商談記録」に保存する（Zoho の DC はクライアント設定で切り替える。デモ環境は US）。前提とルールは [CLAUDE.md](CLAUDE.md) を参照。

| 入口 | 仕組み | 状態 |
|---|---|---|
| A ボット参加（社内デモ・予備） | CRM ワークフロー → `POST /api/bots` → Recall.ai Meeting Bot | 準備済み（設定・テストの手順は [docs/recall-bot.md](docs/recall-bot.md)。Recall.ai の仕様の一部は最初のテストで確かめる） |
| B 対面録音 | CRM ワークフロー → `POST /api/recordings` → スマホの録音ページ | **動作確認済み**（録音 → 文字起こし・要約 → 商談記録の更新。1〜2分の録音で確認） |
| C デスクトップアプリ（本番の標準） | Electron ＋ Recall.ai Desktop Recording SDK | 受け口は実装済み、アプリは Phase 2 |

- 開発・デモの接続先は **お客様の本番 Zoho CRM**。`DRY_RUN=true`（既定）では CRM への書き込みと音声の削除を行わない。
- 実書き込みはユーザーの指示があるときだけ `DRY_RUN=false` にする。今の設定は `/health` の `dry_run` で確かめる（[docs/processing-test.md](docs/processing-test.md) の手順 3）。

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

**2回目以降は `bash scripts/deploy.sh`**（環境変数をそのまま引き継いでコードだけを入れ替える）。下の `--env-vars-file` を使うデプロイは環境変数を丸ごと置き換えるので、初回以外は使わない。
環境変数を変えるだけ（`gcloud run services update`）ではコードは入れ替わらない。

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

curl -s $SERVICE_URL/health   # {"status":"ok","dry_run":true}
```

### 1-5. iPhone テスト

[docs/recorder-test.md](docs/recorder-test.md) の手順で行う。テスト用 URL は CRM に何も書かない。

---

## 2. 環境変数

| 変数 | 既定 | 使うところ |
|---|---|---|
| `DRY_RUN` | `true` | `true` の間は CRM への書き込みと音声の削除を送らない（ログのみ） |
| `CRM_TEST_RECORDS` | `true` | バックエンドが作るレコード名の先頭に【TEST】を付ける |
| `CLIENTS_CONFIG` / `CLIENTS_CONFIG_JSON` | なし（必須） | クライアント設定（ファイルのパス / JSON 文字列）。例は `config/clients.example.json` |
| `SERVICE_URL` | なし | 録音 URL・Cloud Tasks の宛先・OIDC の audience |
| `GCP_PROJECT_ID` | なし | Cloud Tasks・Vertex AI |
| `GCS_BUCKET` / `SIGNING_SERVICE_ACCOUNT` | なし | 対面録音の保存と署名付き URL |
| `RECORDING_TOKEN_SECRET` か `RECORDING_TOKEN_SECRET_REF` | なし | 録音 URL の署名鍵（値そのもの / `sm:` 参照） |
| `TASKS_QUEUE` / `TASKS_INVOKER_SA` / `TASKS_MAX_ATTEMPTS` | なし / なし / `3` | Cloud Tasks（`TASKS_MAX_ATTEMPTS` はキューの最大試行回数と揃える） |
| `TASKS_BACKEND` | `cloud_tasks` | `local` にすると同じプロセスで処理（ローカル開発用） |
| `GEMINI_LOCATION` | `asia-northeast1` | `global` は拒否する |
| `GEMINI_MODEL_TRANSCRIBE` / `GEMINI_MODEL_TEXT` | なし | 対面録音の文字起こし用 / 補正・要約用のモデル名 |
| `AUDIO_SEGMENT_SECONDS` | `1200` | 対面録音を Gemini に渡す単位（秒） |
| `TRANSCRIBE_CONCURRENCY` | `1` | 1 なら区間を順に処理して話者ラベルをそろえる。長い録音で時間が足りなければ増やす |
| `TRANSCRIPT_POLL_SECONDS` / `TRANSCRIPT_POLL_MAX` | `120` / `30` | Recall.ai の文字起こし完了を待つ間隔と回数 |
| `DELETE_MEDIA_ON_FAILURE` | `false` | 失敗時も音声を消すか（既定は残して再処理できるようにする。GCS は1日で消える） |

未確認の外部 API 仕様は [docs/unverified-apis.md](docs/unverified-apis.md) にまとめている。実接続の前に確認する。

## 3. 開発

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

## 4. ドキュメント

| ファイル | 内容 |
|---|---|
| [CLAUDE.md](CLAUDE.md) | 前提・絶対ルール・処理の流れ |
| [docs/recorder-test.md](docs/recorder-test.md) | 対面録音ページの iPhone テスト手順と記録表 |
| [docs/crm-setup.md](docs/crm-setup.md) | CRM の「商談記録」「用語辞書」の作成手順（API） |
| [docs/zoho-connection.md](docs/zoho-connection.md) | バックエンドの Zoho CRM 接続の設定手順 |
| [docs/crm-workflow.md](docs/crm-workflow.md) | CRM から録音用URLを発行する関数・ワークフローの設定手順 |
| [docs/processing-test.md](docs/processing-test.md) | 録音後の処理（Cloud Tasks・Gemini）の準備と通し確認（DRY_RUN のまま / CRM に書き込み）、確認の記録 |
| [docs/recall-bot.md](docs/recall-bot.md) | オンライン商談のボット参加（入口A：Recall.ai）の設定・CRM の関数とワークフロー・テスト手順 |
| [docs/test-conversation.md](docs/test-conversation.md) | 録音テスト用の商談の台本と、期待する結果・用語辞書の例 |
| [docs/unverified-apis.md](docs/unverified-apis.md) | 公式ドキュメントで直接確認できていない外部 API の仕様（実接続前に確認） |
| [config/clients.example.json](config/clients.example.json) | クライアント（テナント）設定の例 |
| [config/env.example.yaml](config/env.example.yaml) | Cloud Run の環境変数の例 |
