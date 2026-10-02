# 商談の文字起こし・要約アプリ（バックエンド＋対面録音ページ）

オンライン商談（Zoom / Teams / Google Meet）と対面商談の会話を文字起こしし、
Gemini で補正・要約して Zoho CRM に蓄積する。中小製造業の営業向け。
将来は複数のクライアント企業に展開する前提で、設定はクライアント単位に分ける。

---

## 1. 確定済みの構成（変更しない）

- 画面・データの正は **Zoho CRM**。Zoho Creator は使わない。DC はクライアント設定で切り替える
  （既定 US：`accounts.zoho.com` / `www.zohoapis.com`。デモ環境のお客様 CRM は US DC）
  - スマホは Zoho CRM 公式アプリ、PC は CRM ウィジェット（別フェーズ）
- オンライン会議は **Recall.ai 東京リージョン** `https://ap-northeast-1.recall.ai`。会議ボットは自作しない
  - **オンライン商談は CRM から呼ぶボット（Meeting Bot API、入口A）で行う**（2026-10-01 のユーザーの判断）
  - Desktop Recording SDK（入口C。ボットを会議に入れない）は保留。バックエンドの受け口は実装済み
- 対面商談は Cloud Run が配信する録音 Web ページ（スマホのブラウザ）。ネイティブアプリは作らない
  - CRM の録音用URLから開く方法と、録音アプリ（`/app/`。Zoho アカウントでログインし、訪問先・担当者を選ぶ）から開く方法がある
- バックエンドは **Cloud Run（asia-northeast1）/ Python 3.12 / FastAPI**
- AI は **Vertex AI の Gemini、リージョナルエンドポイント asia-northeast1**。global エンドポイントは使わない
- 文字起こし全文は **CRM の「商談記録」** の `transcript` / `transcript_2`（各 32,000 文字）に保存する。
  64,000 文字を超えた分は末尾を切り「（以降省略：全体○○文字）」と明記、ログに警告を出す。WorkDrive は使わない
- **GCP に DB を持たない。** 処理状態は CRM のレコードで管理する
- **録画・音声は残さない。** 文字起こし後に削除（Recall.ai は API で削除、GCS はライフサイクル1日も併用）
- 秘密情報は Secret Manager。リポジトリに含めない
- デプロイは GitHub Actions → Artifact Registry → Cloud Run（Workload Identity 連携。鍵ファイルは使わない）

## 2. 絶対ルール

1. **外部 API の仕様は記憶に頼らず公式ドキュメントで確認する。** 未確認のまま実装した箇所は
   `docs/unverified-apis.md` に必ず記録する。
2. **CRM の API 名をコードに直書きしない。** すべて `app/field_map.py` を経由する。
3. **1件の処理で CRM への更新は原則1回。** 途中経過の書き込みは `field_map.BOT_EVENT_STATUS` などで決めたものだけ。
4. **ログに文字起こし本文・要約・API キー・トークン・署名付き URL を出さない。**
   ID・件数・文字数・所要時間だけを出す（`tests/test_logging_redaction.py` で検査している）。
5. **`DRY_RUN=true`（既定）では外部への書き込み（CRM の作成・更新、音声の削除）を行わずログだけ出す。**
   開発・デモは**お客様（株式会社マルサン木型製作所）の本番 Zoho CRM** で行う。実書き込みはユーザーの指示があるときだけ。
6. **CRM への書き込みはカスタムモジュール「商談記録」「用語辞書」だけ。** 取引先・連絡先・商談などの標準モジュールは
   読み取りのみ。`app/services/crm.py` のガードで、それ以外のモジュールへの書き込みはエラーにする。削除処理は作らない。
   **新しく作ったもの以外のデータ・設定は、書き換えも変更もしない**（ユーザーの指示）。標準モジュールや既存のカスタムモジュールの
   レコード、既存の項目・レイアウト・ワークフローなどは、API でも手順書（画面の操作）でも変えない。CRM に足すのは、新しく作る
   モジュール・項目・関数・ワークフロー・変数だけ。既存のものに手を入れないと実現できないときは、作業を止めてユーザーに相談する。
   例外（2026-10-02 ユーザー了承）：商談記録の「先方担当者（連絡先）」（連絡先の複数選択ルックアップ）。Zoho が中間モジュールを作り、
   連絡先に逆向きの項目と関連リストを足す（連絡先のデータは書き換えない）。書き込むのは商談記録の作成だけ（docs/crm-setup.md の手順7）。
7. **バックエンドが作るテスト用レコードは名前の先頭に【TEST】を付ける**（`CRM_TEST_RECORDS=true` が既定）。
8. Zoho の接続先は `app/clients.py` の DC 設定（`"dc": "us" | "jp" | …`）だけで決める。URL を直書きしない。
9. プロンプトは `prompts/` のファイルで管理し、コードに埋め込まない。モデル名は環境変数。
10. 4xx はリトライしない。429 / 5xx / 通信エラーは指数バックオフで最大3回。
11. テストでは外部 API をすべてモックする（実 API を叩くテストは書かない。tests/conftest.py で通信を遮断している）。

12. **Zoho の API で実機で確かめた新しい挙動は、スキル `zoho-crm-build` に追記する。** 正本は
    `rkanezashi12-cmd/zoho-agri-demo` の `.claude/skills/zoho-crm-build/SKILL.md`（更新手順はその冒頭）。

### OAuth スコープ（Zoho。発行後は追加できない）

```
ZohoCRM.modules.custom.ALL, ZohoCRM.modules.accounts.READ, ZohoCRM.modules.contacts.READ,
ZohoCRM.modules.deals.READ, ZohoCRM.coql.READ, ZohoCRM.users.READ,
ZohoCRM.settings.modules.READ, ZohoCRM.settings.fields.READ, ZohoCRM.org.READ
```

`ZohoCRM.org.READ` は接続先の組織の確認に使う（読み取りのみ）。発行・保存は `scripts/setup_zoho_connection.sh`（手順は docs/zoho-connection.md）。

録音アプリのログインは別のクライアント（API コンソールの Server-based Applications）で、スコープは
`ZohoCRM.users.READ, ZohoCRM.org.READ` だけ（`access_type=online`）。受け取ったトークンは本人の確認に1回使って捨て、
保存しない。CRM の検索・書き込みは上のバックエンドの接続で行う（設定は `scripts/setup_app_login.sh`、手順は docs/visit-app.md）。
ログインは90日続き、12時間ごとにバックエンドの接続で CRM の有効なユーザーかを確かめ直す（無効なら使えなくする。`app/deps.py`）。

## 3. 処理の流れ

入口ごとに「文字起こしテキストを得る」までを担当し、以降は共通処理（`app/pipeline/`）に合流する。

| 入口 | 起点 | 文字起こし | 音声の削除 |
|---|---|---|---|
| A ボット | CRM ワークフロー → `POST /api/bots` | Recall.ai（話者名つき） | Recall `delete_media` |
| B 対面録音 | CRM ワークフロー → `POST /api/recordings` → 録音ページ | Gemini（話者A/B） | GCS のオブジェクト削除 |
| B 録音アプリ | `/app/`（Zoho でログイン）→ `POST /api/app/visits`（商談記録を作る）→ 録音ページ | 同上 | 同上 |
| C デスクトップ | アプリ → `POST /api/desktop/upload-token` | Recall.ai（話者名つき） | Recall の録音削除 |
| D モバイル SDK | 未提供。`app/pipeline/sources.py` にソースを1つ足す | — | — |

Webhook（`POST /webhooks/recall`）は署名検証 → Cloud Tasks に積んで即 200。
共通処理は `POST /internal/process`（Cloud Tasks から OIDC 認証で呼ぶ）。

共通処理：補正（用語辞書を注入）→ 要約・構造化（JSON スキーマ固定）→
CRM を1回で更新（要約・構造化項目・全文・状態）→ 音声削除。失敗時は CRM の状態を「失敗」にしてエラー内容を書く。

## 4. ディレクトリ

```
app/main.py            FastAPI アプリ
app/config.py          環境変数（pydantic-settings）
app/clients.py         クライアント（テナント）単位の設定と秘密情報の解決
app/field_map.py       CRM の API 名・選択肢の値（実物に合わせてここだけ直す）
app/routers/           bots / desktop / recordings / webhooks / internal / app_auth・app_api（録音アプリ）
app/services/          zoho_auth / crm / recall / gemini / tasks / storage / audio / zoho_login / geocoding
app/visits.py          録音アプリの訪問先の検索と商談記録の作成（app/web_session.py はログインの Cookie）
app/pipeline/          共通処理（入口非依存）
web/recorder/          対面録音ページ（素の HTML/JS。ビルド不要）
web/app/               録音アプリ（Zoho でログイン・訪問先と担当者の選択・GPS の候補・日報。素の HTML/JS）
prompts/               transcribe.md / correct.md / summarize.md / schema.json
crm/                   Deluge 関数の保管場所
desktop/ widgets/      Phase 2 / 3
tests/                 pytest（外部 API はモック）、tests/js と tests/e2e は録音ページ
```

## 5. 開発コマンド

```bash
pip install -r requirements-dev.txt
ruff check . && ruff format --check .
pytest
node --test tests/js/*.test.mjs            # 録音ページの純粋ロジック
node tests/e2e/recorder.e2e.mjs            # Chromium の擬似マイクで録音ページを通しで確認
node tests/e2e/app.e2e.mjs                 # 録音アプリ（/app/）を通しで確認（SCREENSHOT_DIR で画面も残せる）
```
