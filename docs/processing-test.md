# 録音後の処理の準備と通し確認

録音した音声を Gemini で文字起こし・補正・要約し、商談記録を更新する処理（Cloud Tasks から呼ばれる）を動かす。
対面録音でもオンライン（Recall.ai）でも共通の仕組み。

1. まず `DRY_RUN=true` のまま、**CRM に書き込まずに最後まで通る**ことを確かめる（手順 1・2）。
   要約の中身は CRM に入らないので見られない（ログで件数・文字数・状態を確かめる）
2. 次に、ユーザーの指示で `DRY_RUN=false` にして、テスト用の商談記録に書き込んで中身を確かめる（手順 3）

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
   | `dry_run.skip`（3行） | `項目` が入っている行は CRM に書く予定だった項目（処理の開始時と終了時）、空の行は音声の削除。どれも `DRY_RUN` なので行っていない |
   | `pipeline.finished` | `状態` が `done` |

設定を直したあと、録音し直さずに同じ音声で処理をやり直すときは（音声は1日だけ残る）:

```bash
cd ~/voice-app && bash scripts/reprocess.sh <商談記録の ID>
```

## 3. CRM に書き込んで確かめる（DRY_RUN=false）

ユーザーの指示があるときだけ行う。書き込むのは商談記録だけ（ほかのモジュールへの書き込みはバックエンドが止める）。

1. 書き込みを有効にする（Cloud Shell）

   ```bash
   gcloud run services update meeting-notes --project=voice-ai-510014 --region=asia-northeast1 --update-env-vars=DRY_RUN=false
   ```

2. CRM で**新しい**商談記録を作る（名前の先頭に【TEST】、取得方法「対面録音」）。
   ワークフローがバックエンドを呼び、商談記録の「録音用URL」に URL が入る
3. スマホで「録音用URL」を開いて録音する（QR コードで開くときは `bash scripts/issue_process_url.sh <商談記録の ID>`）
4. 数分後に `bash scripts/process_logs.sh`。手順 2 の表の `dry_run.skip` の代わりに次が出ていれば成功

   | イベント | 意味 |
   |---|---|
   | `crm.updated`（1行目） | 処理の開始。状態を「文字起こし中」にし、エラー内容を消した |
   | `crm.updated`（2行目） | `項目` が書き込んだ項目（要約・構造化項目・文字起こし全文・状態） |
   | `gcs.deleted` | 音声を消した（`削除数` に消したファイルの数） |
   | `pipeline.finished` | `状態` が `done`（`秒` に全体の所要時間） |

5. CRM で商談記録を開き、状態（取引先が空なら「取引先未設定」、入っていれば「完了」）・要約・構造化項目・
   文字起こし全文を確かめる

注意:

- **録音のテストは毎回新しい商談記録で行う。** 処理が済んだ商談記録（状態が「完了」か「取引先未設定」）の録音用URLを開くと、
  録音ページが「処理が済んでいます」と表示して録音させない（共通処理は二重処理を防ぐため、処理済みの記録の録音を飛ばして
  音声を消す作りなので、録音の前に止める）
- `DRY_RUN=false` では処理の最後に音声を消すので、`reprocess.sh` で同じ音声をやり直せるのは書き込みが済むまで

書き込みを止める（`DRY_RUN=true` に戻す）ときと、今の設定を確かめるとき（`"dry_run":false` なら書き込む設定）:

```bash
gcloud run services update meeting-notes --project=voice-ai-510014 --region=asia-northeast1 --update-env-vars=DRY_RUN=true
curl -s "$(gcloud run services describe meeting-notes --project=voice-ai-510014 --region=asia-northeast1 --format='value(status.url)')/health"
```

## 記録

日時は日本時間。

| 日時 | 内容 | 結果 |
|---|---|---|
| 2026-09-30 02:05 | 1〜2分の対面録音（2人）を `DRY_RUN=true` で処理 | 最後まで通った（文字起こし 478 文字）。補正に約100秒かかった |
| 2026-09-30 02:10 | 同じ音声を `DRY_RUN=false` で処理し直し（商談記録「【TEST】録音URLの確認」） | 約35秒で完了（補正は約9秒。100秒は一時的だった）。CRM で状態「取引先未設定」、要約・文字起こし全文（話者A/B）が入ったことを確認。録音が商談ではない会話（リハーサル）なので、ニーズ・課題などの構造化項目は空。録音用URLは `DRY_RUN=true` のときに作った記録なので空 |

## うまくいかないとき

`bash scripts/process_logs.sh` の結果を送る。よくあるもの:

| ログ | 原因 |
|---|---|
| `recording.completed` の後に何も出ない | キューの権限か `TASKS_*` の設定。`setup_processing.sh` をもう一度実行する |
| `auth.oidc_rejected` | キューのトークンの向き先（`SERVICE_URL`）かサービスアカウントの違い |
| `gemini.retry` が続く・`pipeline.failed` に gemini | モデルの選び直し（`setup_processing.sh` の手順5） |
| `pipeline.retry` | 一時的な失敗。最大3回まで自動で再試行する |
| `pipeline.failed` に `INVALID_MODULE` | Cloud Run のコードが古い（商談記録の API 名が変わる前のまま）。`bash scripts/deploy.sh` でデプロイし直す |
| `pipeline.failed` に `OAUTH_SCOPE_MISMATCH` | バックエンドの Zoho 接続の権限不足。[zoho-connection.md](zoho-connection.md) の手順で発行し直す |
| 録音ページに「処理が済んでいます」 | 処理済みの商談記録の録音用URLを開いた。新しい商談記録を作って録音する |
| `pipeline.skipped_finished` | 処理済みの商談記録の音声が届いたので、処理せずに音声を消した（二重処理の防止） |
| `correct.rejected` | 補正の結果が元の発言と大きく違うため、補正前の文字起こしを使った（処理は続く） |
| `summarize.invalid_json` | 要約の形が崩れたので、要約だけ作り直した。2回続けて崩れると処理ごとやり直す（`pipeline.retry`） |
