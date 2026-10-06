# 公式ドキュメントで直接確認できていない外部 API の仕様

実装時、この作業環境からは `docs.recall.ai` / `docs.cloud.google.com` / `www.zoho.com` などを直接開けず、
検索結果の抜粋をもとに実装した箇所がある。**実接続の前に、下の項目を公式ドキュメントか実際のレスポンスで確認する。**
確認したら「確認済み」に移し、違っていればコードを直す（該当ファイルを併記）。

## 優先度：高（ここが違うと動かない）

| # | 項目 | 実装での想定 | 該当箇所 |
|---|---|---|---|
| H5d | デスクトップ録音の Webhook（`sdk_upload.*`）の名前と本文 | 完了は `sdk_upload.complete`（公式ドキュメント）か `sdk_upload.completed`（Recall.ai のブログ）。両方受ける。本文の `data.sdk_upload.id` と `data.recording.id` | `app/pipeline/recall_flow.py`, `app/services/recall_events.py` |
| H6 | デスクトップ SDK のアップロード | `POST /api/v1/sdk_upload/`（`metadata`・`recording_config`）→ `id`・`upload_token`。`GET /api/v1/sdk_upload/{id}/` に録音 ID（`recording.id` か `recording_id`）。作成時に `metadata` を受け付けるかは検索結果でも確認できていない | `app/services/recall.py` |
| H7d | デスクトップ録音の削除 | `DELETE /api/v1/recording/{id}/`（検索結果の抜粋ではエンドポイントがある。実物ではまだ） | `app/services/recall.py` |
| H11 | Gemini 3.5 Flash の考える量（2026-10-06 追加） | `GenerateContentConfig(thinking_config=ThinkingConfig(thinking_level="LOW"))`。3.5 Flash の値は MINIMAL・LOW・MEDIUM・HIGH で既定は MEDIUM、Gemini 3 系は数値の `thinking_budget` を使えない（検索結果の抜粋）。Vertex AI は Gemini 3 Flash で MINIMAL を `Thinking level is unsupported` で断り、LOW・MEDIUM・HIGH は通るという報告がある → 既定は LOW。考えた分は `usage_metadata.thoughts_token_count`（出力として課金）。Gemini 2.5 までには指定しない。デプロイ後の最初の処理で、`gemini.generated` の `考える量` が low のまま通り、`考えたトークン` が出ることを確かめる | `app/services/gemini.py`, `app/config.py` |
| H8 | COQL の書き方 | `Email like '%@domain'`、ルックアップ先の名前 `Account_Name.Account_Name`、カスタムモジュールへの `where Recall_ID = '...'` | `app/routers/desktop.py`, `app/pipeline/records.py` |

H5d・H6・H7d はデスクトップ録音（入口C。Phase 2）を初めて動かすときに確かめる。

確認済み（2026-10-01 日本時間、マルサン木型の本番組織と Google Meet で、ボットの予約から録画の削除まで通しで処理。
[recall-bot.md](recall-bot.md) の「記録」）：

- H2 ボット作成（`POST /api/v1/bot/`）で渡した `metadata: {"client_id", "record_id"}` が、Webhook の本文の `data.bot.metadata` に
  そのまま入って届いた（処理ログの `recall.event_parsed` に `record_id`）。応答の `id` がボット ID（商談記録の「Recall ID」）
- H3 `GET /api/v1/bot/{id}/` の `recordings[0].id` で録音を取り、`GET /api/v1/recording/{id}/` の
  `media_shortcuts.transcript.status.code` が `done` になったら `data.download_url` から文字起こしを取れた（依頼から2分後の確認で取得）
- H4 `POST /api/v1/recording/{id}/create_transcript/` に `{"provider": {"recallai_async": {"language_code": "ja"}}}` で、
  日本語の文字起こしができた。`bot.done` を受けてすぐ依頼して通った
- H5 ボットの Webhook の本文は `{"event": "bot.xxx", "data": {"data": {"code", "sub_code", "updated_at"}, "bot": {"id", "metadata"}}}`。
  届いた順：`bot.joining_call`・`bot.in_waiting_room`（予約から約10秒）→ `bot.in_call_not_recording`・`bot.in_call_recording`
  （入室を許可したとき）→ `bot.call_ended`（サブコード `timeout_exceeded_everyone_left`）・`bot.done`（全員が抜けたとき）
- H7 ボットの録画の削除 `POST /api/v1/bot/{id}/delete_media/` が通り、Recall.ai のダッシュボードでボットの Status が
  「Media Expired」、録画・文字起こしは表示されなくなった
- H9 ダッシュボードの Webhooks（Svix の画面）で送り先を追加し、送り先の Signing Secret（`whsec_…`）で実際の通知の署名が合った。
  送るイベントは「bot」と「sdk_upload」を選んだ（1つ以上選ばないと作れない）。
  検索結果の抜粋には「2025-12-15 以降に作ったワークスペースは Workspace Verification Secret で検証する」とあるが、
  このワークスペース（2026-09-30 作成）では送り先の Signing Secret で合った
- H10 正しいキーで `GET /api/v1/bot/` が 200（`setup_recall.sh`）。違うキーのときの応答は見ていない
- M2 文字起こしの JSON は `[{"participant": {"name"}, "words": [{"text"}]}]` の形で読めた（話者は Google Meet の表示名）

検索結果の抜粋で確かめたもの（`docs.recall.ai` の全文は開けていない。2026-10-01）：

- すぐ参加させるボット（`join_at` なし・10分未満）は、空きが無いと 507 が返る。Recall.ai は「30秒おきに最大10回やり直す」ことを勧め、
  10分以上先の予約なら 507 は起きない。いまは数秒の再試行だけで、失敗したら「エラー内容」に開始日時を10分以上先にするよう書く
  （`app/routers/bots.py` の `NO_READY_BOT_HINT`）
- 自動で抜けるまでの既定：ほかの参加者が全員抜けてから 2秒（`everyone_left_timeout`）、待機室 1200秒、誰も来ない 1200秒、
  録音していない 3600秒、録音を拒否された 30秒
- 会議後の文字起こしの依頼は、ボットあたり1分に5回まで
- `sub_code` は閉じた一覧として扱わないよう書かれている。`bot_kicked_from_call`（会議の途中で削除された）と
  `timeout_exceeded_recording_permission_denied` は参加失敗にしない（M1）

確認済み（2026-09-29、本番の Cloud Run で対面録音を DRY_RUN のまま通しで処理）：

- H1 `gemini-3.5-flash` は asia-northeast1 のリージョナル エンドポイントで従量課金のまま使える
  （`scripts/setup_processing.sh` の問い合わせで HTTP 200。1〜2分の録音の文字起こし〔音声〕・補正・要約が通った）。
  補正（correct）は 478 文字で1回目は約100秒、2回目（DRY_RUN=false で同じ音声）は約9秒で、処理全体は約35秒。
  長い録音での所要時間は次の確認で見る

## 録音アプリ（docs/visit-app.md）

すべて検索結果の抜粋で確かめた形（公式ページの全文は開けていない。2026-10-01）。実機で最初に使うときに確かめる。

**2026-10-02 の実機の確認（お客様の本番 CRM・スマホ）**：A1・A2（ログインできた）、A3（日本語の会社名で顧客検索できた）、
A5・A7（GPS で現在地の住所が出た）は動いた。A4（請求先住所での候補）は、近くに顧客企業が無く未確認。
A8（先方担当者（連絡先）の紐づけ）も、項目を作ったあとの録音で動いた。
作った商談記録の担当者（ログインした人）・取引先・先方担当者は正しかった。

| # | 項目 | 実装での想定 | 該当箇所 |
|---|---|---|---|
| A1 | Zoho の「ログイン」（認可コード方式） | `GET {accounts}/oauth/v2/auth?scope=ZohoCRM.users.READ,ZohoCRM.org.READ&client_id&response_type=code&access_type=online&redirect_uri&state`。戻りは `?code&state&location&accounts-server`（拒否は `error=access_denied`）。`POST {accounts}/oauth/v2/token`（grant_type=authorization_code）は失敗しても HTTP 200 で `{"error": ...}` を返すので、`access_token` の有無で判定 | `app/services/zoho_login.py`, `app/routers/app_auth.py` |
| A2 | ログインした人の確認 | 本人のトークンで `GET /crm/v8/users?type=CurrentUser` → `users[0]`（`id`・`full_name`・`email`・`status`）、`GET /crm/v8/org` → `org[0].id` をバックエンドの組織と突き合わせる。プロファイルで API の利用が許可されていないと 403（`NO_PERMISSION`） | `app/services/zoho_login.py` |
| A3 | COQL の書き方 | `like '%語%'`（文字列）。3つ以上の条件は2つずつかっこでくくる（`((A or B) or (C or D))`）。`Owner = '<ID>'`、連絡先の `Account_Name = '<ID>'`、`is not null`、日時の `between '2026-10-01T00:00:00+09:00' and '…'`。日本語の `like` は未確認 | `app/visits.py` |
| A4 | お客様の CRM の顧客企業の住所の項目 | 請求先住所（`Billing_State` / `Billing_City` / `Billing_Street`）に入っている。違えば `field_map.standard` で変える | `app/field_map.py` |
| A5 | Google Geocoding API（逆ジオコーディング） | `GET https://maps.googleapis.com/maps/api/geocode/json?latlng&language=ja&key` → `status`、`results[].address_components[]`（都道府県 `administrative_area_level_1`、市区町村・東京23区 `locality`、政令指定都市の区 `sublocality_level_1`（`ward` の種類は付かないことがある）、町 `sublocality_level_2`、丁目 `sublocality_level_3`） | `app/services/geocoding.py` |
| A6 | Geocoding API のキーの作り方 | `gcloud services api-keys create --key-id --api-target=service=geocoding-backend.googleapis.com` と `gcloud services api-keys get-key-string <ID> --format='value(keyString)'`。**確認済み（2026-10-01、Cloud Shell）**：作れる。ただし `create` は終わったときの結果（`keyString` を含む）を**標準エラーに**出す（`>/dev/null` では消えず、キーの値が画面に出た）→ 出力ごと捨てる（`run_quietly`）。作り直し：gcloud にキーの値だけを作り直すコマンドは無く、新しいキーを作って古いキーを削除する（コンソールの「Rotate key」も同じ考え方）。削除は30日以内なら `gcloud services api-keys undelete <ID>` で戻せ、削除したキーは `list --show-deleted` でしか出ない（検索結果の抜粋）。`list --format='value(createTime,name)'` の形と、削除したキーの ID を使い直せるかは未確認（ID には作った日時を付けて重ならないようにした） | `scripts/setup_app_login.sh` |
| A7 | スマホのブラウザの位置情報 | HTTPS で `navigator.geolocation.getCurrentPosition`（初回に許可を聞く。拒否は code 1）。iPhone の Chrome はアプリ自体の位置情報の許可も要る | `web/app/app.js` |
| A8 | 先方担当者（連絡先）＝連絡先の複数選択ルックアップ（2026-10-02 追加） | **確認済み（2026-10-02、お客様の CRM）**：`POST /settings/fields` の `multiselectlookup.connected_details`（参照先と逆向きの項目名）で作れた。`linking_details.module.plural_label` は使われず、中間モジュールは Zoho が「商談記録 X 顧客担当者」（API 名 `X`）と付けた。項目の設定の `multiselectlookup.linking_details.connected_lookup_field.api_name`（`field3_1`）が書き込みに使う中間モジュールの連絡先のルックアップ。商談記録の作成で `{"Customer_Contacts": [{"field3_1": {"id": …}}]}` を送り、担当者3人が紐づいた（バックエンドのスコープのままで足りた）。中間モジュールは `GET /settings/modules` に `generated_type: linking` で出る。エディションによる違い（1モジュールに作れる数など）は未確認。断られたら紐づけを外して作り直す（録音は止めない） | `scripts/crm_setup.py`, `app/visits.py`, `app/routers/app_api.py` |
| A9 | CRM のユーザーが今も有効か | バックエンドの接続（`ZohoCRM.users.READ`）で `GET /crm/v8/users/{id}` → `users[0].status`（`active` 以外は無効）。いないユーザーは 204 か 400 `INVALID_DATA` を想定 | `app/services/crm.py`, `app/deps.py` |
| A10 | 90日のログインの Cookie | サーバーが付ける HttpOnly・Secure・SameSite=Lax の Cookie（同じサイト）。iPhone の Safari の追跡防止（スクリプトで付けた Cookie を7日で消す）には当たらない想定 | `app/routers/app_auth.py` |

## 商談日報ウィジェット（docs/widget.md）

検索結果の抜粋（他の Zoho 製品のウィジェットの説明・コミュニティの記事）と、zoho-crm-build の実測（農業資材デモ）をもとにした形（2026-10-02）。
`www.zoho.com` と `help.zoho.com` はこの作業環境から開けなかった。画面の動きは偽の SDK で確かめてある（`tests/e2e/widget.e2e.mjs`）。

**2026-10-06 の実機の確認（お客様の本番 CRM・PC）**：`scripts/build_widget.py` の ZIP をそのまま登録でき、商談記録の関連リストに
日報（見出し・要約・課題・ニーズ・予算・決裁者など）が出た（W1・W2・W3・W6）。インデックスページは **`/index.html`**
（Zoho は ZIP の中の `app` フォルダーを一番上の階層として置く。`/app/index.html` だと Zoho の「Page Not Found」）。
ベースURL＋`/main.js` を開くと、ZIP の `app/main.js` の中身が出る。インデックスページを直したあと、
ブラウザのキャッシュを消したら日報が出た（それまでは「Page Not Found」のまま。再読み込みだけで直るかは見ていない）。

| # | 項目 | 実装での想定 | 該当箇所 |
|---|---|---|---|
| W1 | ZIP の `plugin-manifest.json` | **確認済み（2026-10-06）**：`service: "CRM"`、`modules.widgets[]` に `location: "custom"`・`name`・`url` の形で受け付けられた。種類（関連リスト）とインデックスページ（`/index.html`）は CRM の画面で選ぶ。ZIP の形（ルートに `plugin-manifest.json` と `app/`、ディレクトリの項目）は zoho-crm-build の実測どおり | `widgets/meeting-report/plugin-manifest.json`, `scripts/build_widget.py` |
| W2 | 関連リストのウィジェットの `PageLoad` | **確認済み（2026-10-06）**：開いている商談記録を読めた（ID が文字列か配列かは見ていない）。`data.Entity`（モジュールの API 名）と `data.EntityId`（開いている記録の ID。記事の例は配列 `data.EntityId[0]`、zoho-crm-build は ID とだけ記載）。どちらの形でも読む。`Entity` が無いときは商談記録とみなす | `widgets/meeting-report/app/main.js` |
| W3 | `ZOHO.CRM.API.getRecord({Entity, RecordID})` | **確認済み（2026-10-06）**：名前・状態・開始日時・担当（ルックアップの名前）・要約などが出た。応答は `{data: [記録]}`。ルックアップは `{name, id}`、日時は `2026-10-02T23:55:00+09:00`、空の項目は null（REST の API と同じ形）。見る権限が無いときは reject（`data[0].code`）か空の `data` | `widgets/meeting-report/app/main.js`, `widgets/meeting-report/app/report.js` |
| W4 | `ZOHO.CRM.UI.Resize({height})` | 2026-10-06 の画面では、枠の中に縦のスクロールバーが出ていた（効いていないか、高さに上限がある可能性。未確認）。関連リストのウィジェットは高さだけ変えられる（記事の抜粋。Resize の例はボタンのウィジェット）。値は数字の文字列（`"600"`）。効かない・断られても表示は続け、文字起こしの欄は枠の中でスクロールする | `widgets/meeting-report/app/main.js` |
| W5 | `ZOHO.CRM.UI.Record.open({Entity, RecordID})` | 取引先の画面を開く。ID は文字列のまま渡す（zoho-crm-build の実測：空だと「アクセスできないビュー」） | `widgets/meeting-report/app/main.js` |
| W6 | ウィジェットを使えるエディション | **確認済み（2026-10-06）**：お客様の CRM で 設定 → 開発者スペース → ウィジェット が使え、商談記録の関連リストにウィジェットを置けた | `docs/widget.md` |

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

確認済み（2026-10-01 日本時間、マルサン木型の本番組織でワークフロー「ボットの予約」から実行）：

- D5 ボット予約の関数（`create_bot.dg`）が動き、バックエンドがボットを予約して「Recall ID」と状態「予約済」を書いた（`bot.reserved`）

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

Gemini の費用（2026-10-06、Cloud Billing の 9/28〜10/4 の SKU 別の実績。テスト数回・音声は合計約6分）：

- 音声の入力は 11,437 トークンで ¥3。テキストの入力と同じ単価（$1.50／100万トークン）として計算が合う
- 出力（Gemini 3.5 Flash Regional Text Output）は 76,260 トークンで ¥120（100万トークンあたり約1,570円）。3.5 Flash には考えた分の別の SKU が無く、出力に含まれる
- 音声約6分に対して出力が 76,260 トークンあり、大半が考えた分とみられる（考える量は既定の medium だった）→ H11 で low にした。対面録音は補正（全文の書き直し）も省いた（`CORRECT_GEMINI_TRANSCRIPTS`）
- Gemini 以外（Cloud Run・Cloud Storage・Cloud Tasks・Secret Manager・Artifact Registry・Cloud Build・Geocoding）はどれも無料枠の中

Artifact Registry の古い版の自動削除（`scripts/setup_artifact_cleanup.sh`、2026-10-06 追加。検索結果の抜粋で確かめた形）：

- `gcloud artifacts repositories set-cleanup-policies <リポジトリ> --location --policy=<JSON ファイル> --no-dry-run`。JSON は `[{"name", "action": {"type": "Keep"}, "mostRecentVersions": {"keepCount": 5}}, {"name", "action": {"type": "Delete"}, "condition": {"olderThan": "604800s"}}]`。Keep と Delete の両方に当てはまる版は残る（Keep が優先）。削除は1日に1回ほどの裏の処理で行われる
- `--source` のデプロイが作るリポジトリは `cloud-run-source-deploy`（請求の SKU に Artifact Registry Storage 0.35 GiB・月が出ている）。`describe` の `sizeBytes` で大きさが取れる想定（取れなければ大きさを出さずに進む）。実行後に `list-cleanup-policies` の表示と、翌日の大きさで確かめる

## 優先度：中（動くが挙動が変わる）

| # | 項目 | 実装での想定 | 該当箇所 |
|---|---|---|---|
| M1 | 参加失敗を示す `sub_code` の一覧 | `waiting_room` / `noone_joined` / `denied` / `kicked` / `bot_blocked` などを含むものを「参加失敗」。ただし `bot_kicked_from_call`・`timeout_exceeded_recording_permission_denied` は除く（検索結果の抜粋の一覧で確認。実物で見たのは `timeout_exceeded_everyone_left` だけ） | `app/field_map.py` の `JOIN_FAILURE_SUBCODE_HINTS` / `NOT_JOIN_FAILURE_SUBCODES` |
| M3 | `sdk_upload.failed` イベントの有無 | 届けば「失敗」を記録（Recall.ai に無いアップロードなら記録を作らない）。届かなくても処理は壊れない | `app/pipeline/recall_flow.py` |
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
  - 2026-09-30：まだ試していない。iPhone は録音用URLを Chrome に貼って開き、録音できた。
    PC の CRM（Chrome）からは、録音用URLをそのまま開けた
