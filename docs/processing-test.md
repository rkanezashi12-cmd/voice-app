# 録音後の処理の準備と、DRY_RUN のままの通し確認

録音した音声を Gemini で文字起こし・補正・要約し、商談記録を更新する処理（Cloud Tasks から呼ばれる）を動かす。
対面録音でもオンライン（Recall.ai）でも共通の仕組み。

まず `DRY_RUN=true` のまま、**CRM に書き込まずに最後まで通る**ことを確かめる。
要約の中身は CRM に入らないので見られない（ログで件数・文字数・状態を確かめる）。
中身を確かめるには、ユーザーの指示で `DRY_RUN=false` にしてテスト用の商談記録に書き込む（次の段階）。

## 1. 準備（Cloud Shell、5〜10分）

```bash
cd ~/voice-app && git pull && bash scripts/setup_processing.sh
```

スクリプトが行うこと（何度実行しても壊れない。CRM には書き込まない。`DRY_RUN` は変えない）:

- Cloud Tasks と Vertex AI の API を有効にし、処理の順番待ち（キュー `meeting-notes-process`、最大3回試行）を作る
- キューが Cloud Run を呼ぶためのサービスアカウント（`meeting-notes-tasks`）を作り、
  Cloud Run のサービスアカウントに「タスクを積む」「Gemini を呼ぶ」権限を付ける
- 東京リージョン（asia-northeast1）の Gemini に小さな問い合わせを送り、使えるモデルを一覧する
- 使うモデルを選ぶ（そのまま Enter で、使える Flash のうち一番新しいもの）。Cloud Run の環境変数に設定する
- 最後に商談記録の ID を入れると、**録音後の処理まで動く録音URL**を出す（空のまま Enter で飛ばせる）。
  ID は、CRM で商談記録を開いたときの URL の最後の数字（関数のログの `rec_id` と同じ）

録音URLだけを出し直すときは:

```bash
cd ~/voice-app && bash scripts/issue_process_url.sh <商談記録の ID>
```

バックエンドと同じ署名鍵で発行し、バックエンドが受け付けることを確かめてから、URL と QR コードを表示する。

## 2. 録音して確かめる

1. スマホのカメラで QR コードを読んで開く（QR が出ないときは URL を最後の文字まで送る。`#` 以降が切れると「署名が一致しません」になる）
2. 2人で1〜2分話して録音し、「録音終了」を押す
3. 数分待ってから Cloud Shell で次を実行する

   ```bash
   bash scripts/process_logs.sh
   ```

4. 上から次の順に出ていれば成功

   | イベント | 意味 |
   |---|---|
   | `recording.completed` | 録音ページから音声が届いた |
   | `tasks.enqueued` | 処理を順番待ちに積んだ |
   | `pipeline.transcribed` | 文字起こしができた（`文字数` に文字数） |
   | `gemini.generated`（3回） | `処理` が transcribe・correct・summarize |
   | `dry_run.skip` | CRM に書く予定だった項目（`DRY_RUN` なので書いていない） |
   | `pipeline.finished` | `状態` が `done` |

設定を直したあと、録音し直さずに同じ音声で処理をやり直すときは（音声は1日だけ残る）:

```bash
cd ~/voice-app && bash scripts/reprocess.sh <商談記録の ID>
```

## うまくいかないとき

`bash scripts/process_logs.sh` の結果を送る。よくあるもの:

| ログ | 原因 |
|---|---|
| `recording.completed` の後に何も出ない | キューの権限か `TASKS_*` の設定。`setup_processing.sh` をもう一度実行する |
| `auth.oidc_rejected` | キューのトークンの向き先（`SERVICE_URL`）かサービスアカウントの違い |
| `gemini.retry` が続く・`pipeline.failed` に gemini | モデルの選び直し（`setup_processing.sh` の手順5） |
| `pipeline.retry` | 一時的な失敗。最大3回まで自動で再試行する |
| `pipeline.failed` に `OAUTH_SCOPE_MISMATCH` | バックエンドの Zoho 接続の権限不足。[zoho-connection.md](zoho-connection.md) の手順で発行し直す |
