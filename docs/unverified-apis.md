# 公式ドキュメントで直接確認できていない外部 API の仕様

実装時、この作業環境からは `docs.recall.ai` / `docs.cloud.google.com` / `www.zoho.com` などを直接開けず、
検索結果の抜粋をもとに実装した箇所がある。**実接続の前に、下の項目を公式ドキュメントか実際のレスポンスで確認する。**
確認したら「確認済み」に移し、違っていればコードを直す（該当ファイルを併記）。

## 優先度：高（ここが違うと動かない）

| # | 項目 | 実装での想定 | 該当箇所 |
|---|---|---|---|
| H2 | Recall.ai のボット作成で `metadata`（文字列の辞書）を渡せ、`GET /bot/{id}/` と Webhook に返るか | `metadata: {"client_id", "record_id"}` | `app/services/recall.py`, `app/pipeline/sources.py` |
| H3 | 録音の文字起こしの取り方 | `GET /api/v1/recording/{id}/` の `media_shortcuts.transcript.status.code`（done / processing / failed）と `data.download_url` | `app/services/recall_events.py` |
| H4 | 会議後の文字起こし依頼の本文 | `POST /api/v1/recording/{id}/create_transcript/` に `{"provider": {"recallai_async": {"language_code": "ja"}}}`（クライアント設定の `transcript_request`） | `config/clients.example.json` |
| H5 | Webhook 本文の形 | `{"event": "bot.xxx", "data": {"data": {"code", "sub_code", "updated_at"}, "bot": {"id", "metadata"}, "recording": {...}, "sdk_upload": {...}}}` | `app/services/recall_events.py` |
| H6 | デスクトップ SDK のアップロード | `POST /api/v1/sdk_upload/`（`metadata`・`recording_config`）→ `id`・`upload_token`。`GET /api/v1/sdk_upload/{id}/` に録音 ID（`recording.id` か `recording_id`） | `app/services/recall.py` |
| H7 | デスクトップ録音の削除 | `DELETE /api/v1/recording/{id}/`（ボットは `POST /api/v1/bot/{id}/delete_media/`） | `app/services/recall.py` |
| H8 | COQL の書き方 | `Email like '%@domain'`、ルックアップ先の名前 `Account_Name.Account_Name`、カスタムモジュールへの `where Recall_ID = '...'` | `app/routers/desktop.py`, `app/pipeline/records.py` |
| H9 | Recall.ai の Webhook の登録と署名 | ダッシュボードの Webhooks で送り先を追加し、署名用のシークレット（Svix 形式の `whsec_…`）を使う | `app/webhook_signature.py`, `scripts/setup_recall.sh` |
| H10 | API キーの確認 | `GET /api/v1/bot/`（ボットの一覧）が、正しいキーなら 200、違うキー・別リージョンのキーなら 401 か 403 | `scripts/recall_check.py` |

H2〜H7・H9・H10（Recall.ai）の確かめ方：[recall-bot.md](recall-bot.md) の手順3〜4。テストのボットを1回動かし、
`bash scripts/recall_inspect.sh bot <Recall ID>`（ボット・録音の応答の形）と、処理ログの `recall.event_parsed`
（Webhook の本文から読み取れた ID の有無と項目名）を見る。

確認済み（2026-09-29、本番の Cloud Run で対面録音を DRY_RUN のまま通しで処理）：

- H1 `gemini-3.5-flash` は asia-northeast1 のリージョナル エンドポイントで従量課金のまま使える
  （`scripts/setup_processing.sh` の問い合わせで HTTP 200。1〜2分の録音の文字起こし〔音声〕・補正・要約が通った）。
  補正（correct）は 478 文字で1回目は約100秒、2回目（DRY_RUN=false で同じ音声）は約9秒で、処理全体は約35秒。
  長い録音での所要時間は次の確認で見る

## CRM の自動作成（scripts/crm_setup.py）

実測リファレンス（zoho-crm-build）に載っていなかった形。すべて確認済み（2026-09-29、マルサン木型の本番組織で apply を実行。
`apply` の最後と `show-fields` で実物の設定を表示する）。

- Z1 モジュール作成時の `display_field` の項目の API 名は `Name`（表示名は `display_field.field_label`、文字数 120）
- Z2 複数行（大）は `{"data_type": "textarea", "length": 32000, "textarea": {"type": "large"}}` で、そのとおりに作られる
  （複数行（小）は `length: 2000` / `type: small`）
- Z3 重複を許さない項目は `{"unique": {"case_sensitive": false}}`。`true` を送ると
  `INVALID_DATA`（`supported_values: [false]`）
- Z4 URL 項目の型名は `data_type: "website"`。文字数は指定しなくても 450
- 項目作成で一部だけ失敗すると **HTTP 207** が返り、行ごとの `status` が `error` になる（同じ回の他の項目は作成される）。
  HTTP ステータスだけでは失敗に気づけないので、行ごとの `status` を見る（`scripts/crm_setup.py` の `failures`）

## CRM の Deluge 関数（crm/）

実測リファレンス（zoho-deluge / zoho-crm-build）に載っていなかった形。

| # | 項目 | 実装での想定 | 該当箇所 |
|---|---|---|---|
| D3 | 変数が無い・空のときの `zoho.crm.getOrgVariable` | null か空文字（どちらでも止まるようにしてある） | `crm/issue_recording_url.dg` |
| D5 | ボット予約の関数（`create_bot.dg`）全体 | 本番で動いた `issue_recording_url.dg` と同じ書き方。本番ではまだ動かしていない | `crm/create_bot.dg` |

確認済み（2026-09-29、マルサン木型の本番組織でワークフローから実行。バックエンドは応答 200 で録音用URLを発行）：

- D1 `invokeurl` に `parameters: <Map>.toString()` と、ヘッダー `Content-Type: application/json`・`X-API-Key` の Map を渡すと、
  JSON 本文として届く（FastAPI がそのまま受け取れる）
- D2 `detailed:true` の応答は Map で、`responseCode` は数値（`!= 200` で比べられる）、`responseText` は本文の文字列
  （`.toString().toMap()` で読める）
- ワークフローの関数の引数 `rec_id`（文字列）に「商談記録 Id」を割り当てると、レコード ID が文字列で届く
- `zoho.crm.getOrgVariable("<API 名>")` は、値がある変数ならその値を返す

確認済み（2026-09-30 日本時間、開始日時ありの商談記録で録音用URLを発行）：

- D4 `zoho.crm.getRecordById` で読んだ日時項目（開始日時）を `toString()` してバックエンドに渡すと、タイムゾーン込みの正しい時刻として読めた。
  開始日時 2026/9/30 18:00 の記録で、録音用URLの有効期限が 2026/10/1 18:00（日本時間。開始日時＋24時間）になった
  （Cloud Run の時刻は UTC なので、オフセットが無ければ9時間ずれる）

## 優先度：中（動くが挙動が変わる）

| # | 項目 | 実装での想定 | 該当箇所 |
|---|---|---|---|
| M1 | 参加失敗を示す `sub_code` の一覧 | `waiting_room` / `noone_joined` / `denied` / `kicked` などを含むものを「参加失敗」 | `app/field_map.py` の `JOIN_FAILURE_SUBCODE_HINTS` |
| M2 | Recall.ai の文字起こし JSON | `[{"participant": {"name"}, "words": [{"text"}]}]` | `app/pipeline/sources.py` |
| M3 | `sdk_upload.failed` イベントの有無 | 届けば「失敗」を記録。届かなくても処理は壊れない | `app/pipeline/recall_flow.py` |
| M4 | Zoho の upsert の応答 | `data[0].action` が `insert` / `update` | `app/services/crm.py` |
| M5 | Zoho の複数行（大）の文字数の数え方 | UTF-16 の単位で 32,000 以内に収める（多めに数える側） | `app/pipeline/formatting.py` |
| M6 | Zoho でレコードが無いときの応答 | `GET /crm/v8/{module}/{id}` が 204（違う応答なら、録音ページの確認は録音を止めずに通す） | `app/services/crm.py`, `app/routers/recordings.py` |
| M8 | Gemini に MP3 をインライン（`Part.from_bytes`）で渡せる大きさ | 20 分 × 32kbps ≒ 5MB（1〜2分は通った） | `app/services/audio.py` |
| M9 | Cloud Tasks のタスク名による重複排除が効く期間 | 同じ名前は一定期間登録できない（期間は要確認）。処理済みのレコードは共通処理側でも止める | `app/services/tasks.py` |

確認済み（2026-09-30 日本時間、本番の Cloud Run で `DRY_RUN=false` にして商談記録に書き込み。docs/processing-test.md の記録）：

- M7 `response_json_schema` に `null` を含む型 `["string", "null"]` のスキーマを渡すと受け付けられ、JSON で返る
  （`prompts/schema.json` のまま。要約・構造化項目を商談記録に書き込めた）
- M10 鍵ファイルなしの署名付き URL（`generate_signed_url(service_account_email=..., access_token=...)` で IAM signBlob）で、
  録音ページから GCS に直接アップロードできる

## 実機テストで確かめるもの（docs/recorder-test.md）

- iPhone の Safari の録音形式（`audio/mp4` の見込み）、Wake Lock、画面ロック・着信時の挙動
- Zoho CRM アプリのリンクから開いたとき（アプリ内ブラウザ）にマイクが使えるか
  - 2026-09-30：iPhone の Zoho CRM アプリから開いた録音ページでは録音できず、URL を Chrome に貼り直して録音できた。
    PC の CRM（Chrome）からは、録音用URLをそのまま開けた
