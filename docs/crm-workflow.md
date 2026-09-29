# CRM から録音用URLを発行する（入口B：対面録音）

商談記録を作るときに「取得方法」を「対面録音」にすると、CRM のワークフローが関数を呼び、
バックエンドが録音用URLを発行してその商談記録の「録音用URL」に書き込む。営業はスマホの CRM アプリでその URL を開いて録音する。

- 前提：バックエンドの Zoho 接続が済んでいる（[zoho-connection.md](zoho-connection.md)）
- `DRY_RUN=true` の間、バックエンドは「録音用URL」に書き込まない（ログだけ）。この手順では**発行まで**を確かめる
- 関数は CRM に書き込まない（書き込みはバックエンドだけが行う）
- 画面の名前は多少違うことがある。見つからないときは画面を送る

## 1. バックエンドの URL と API キーを用意する（Cloud Shell）

URL（変数 `meeting_notes_url` に入れる）:

```bash
gcloud run services describe meeting-notes --project=voice-ai-510014 --region=asia-northeast1 --format='value(status.url)'
```

API キー（変数 `meeting_notes_api_key` に入れる）は、新しく作って1度だけ表示する:

```bash
cd ~/voice-app && bash scripts/rotate_api_key.sh
```

画面を消してからキーだけを表示し、Enter で画面から消す。**表示中の画面はスクリーンショットしない。**
実行するたびにキーが新しくなり、古いキーは使えなくなる（そのときは CRM の変数も貼り直す）。

## 2. CRM に変数を2つ作る

設定 →「開発者向け機能（Developer Hub）」→「変数」→「変数を作成」。管理者だけが見られる場所。

| 名前 | API 名 | 種類 | 値 |
|---|---|---|---|
| meeting-notes URL | `meeting_notes_url` | 1行 | 手順1の URL（末尾の `/` なし） |
| meeting-notes API キー | `meeting_notes_api_key` | 1行 | 手順1の API キー |

**API 名は表のとおりにする**（関数がこの名前で読む）。

## 3. 関数を作る（組織で最初の関数は画面でしか作れない）

設定 →「開発者向け機能」→「関数」→「新しい関数」

1. 関数名 `issue_recording_url`、表示名「録音用URLを発行」、カテゴリー「自動化（Automation）」で作成する
2. エディタ上部の「引数を編集」で、引数 `rec_id`（種類：文字列 / String）を1つ追加する。
   エディタの1行目が `void automation.issue_recording_url(String rec_id)` になる
3. `{` と `}` の間を、[crm/issue_recording_url.dg](../crm/issue_recording_url.dg) の `{` と `}` の間（コメントから最後の `info` まで）で置き換えて「保存」する

## 4. ワークフローを作る

設定 →「自動化」→「ワークフロールール」→「ルールを作成」

1. モジュール「商談記録」、ルール名「録音用URLの発行」
2. 実行のタイミング：レコードの操作 →「作成または編集」。「繰り返し実行する」にチェック
3. 条件：「取得方法」が「対面録音」と等しい **かつ**「録音用URL」が空
4. 即時のアクション：「関数」→ 既存の関数から「録音用URLを発行」を選ぶ
5. **引数の設定で、`rec_id` に「商談記録 Id」を割り当てる**（`#` から 商談記録 → 商談記録 Id）。
   これを忘れると、エラーも出ずに何も起きない
6. 保存して関連付け、ルールを保存する

## 5. 確かめる

1. 商談記録を1件作る：商談記録名「【TEST】録音URLの確認」、取得方法「対面録音」（開始日時は任意）
2. 設定 →「開発者向け機能」→「関数」→「録音用URLを発行」のログ（実行ログ）を開く。
   最後に `===== SUCCESS crm_updated=false` と出ていれば成功（`DRY_RUN` なので false が正しい）
3. バックエンド側でも確かめるとき（Cloud Shell）:

   ```bash
   gcloud logging read 'resource.type="cloud_run_revision" AND (jsonPayload.message="recording.url_issued" OR jsonPayload.message="dry_run.skip" OR jsonPayload.message="auth.api_key_rejected")' \
     --project=voice-ai-510014 --freshness=1h --limit=10 \
     --format='table(timestamp.date(tz=Asia/Tokyo),jsonPayload.message,jsonPayload.record_id)'
   ```

   `recording.url_issued` と `dry_run.skip`（書き込みを止めた記録）が出ていれば、発行まで通っている

テスト用の商談記録は、確認が済んだら画面から削除してよい。

## うまくいかないとき

| ログ | 原因と対処 |
|---|---|
| 関数のログに何も出ない | ワークフローが動いていない。実行のタイミングと条件（取得方法・録音用URL）を確かめる |
| `rec_id が空です` | 手順4-5 の引数の割り当てが無い |
| `変数 … が未設定です` | 手順2 の API 名の誤り |
| `応答コード=401` | API キーの誤り。`rotate_api_key.sh` で出した最新のキーを貼り直す |
| `応答コード=422` | 送った値の形が合わない。ログの `応答=` の行を送る |
| 関数の保存でエラー | 貼った範囲がずれている（1行目の `void …` と外側の `{ }` は貼らない）。直らなければエラー文を送る |
