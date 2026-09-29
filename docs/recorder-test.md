# 対面録音ページ スマホ実機テスト手順（Android / iPhone）

Web ページでの録音を運用に使えるかを判断するためのテスト。
テスト用 URL で開いた録音ページは **CRM に何も書かず、文字起こしも動かさない**（音声は GCS に届き、1日で自動削除）。

## 0. 準備

1. Cloud Shell で次を実行する（バケット・権限・デプロイ・テスト用 URL の発行までを一度に行う）。

   ```bash
   git clone -b claude/meeting-transcription-app-setup-nkaz1m https://github.com/rkanezashi12-cmd/voice-app.git
   cd voice-app && bash scripts/setup_recorder_test.sh
   ```

   最後に表示される URL が手順3で iPhone に送るもの。別の URL が必要なときは下の2の方法でも発行できる。
2. テスト用 URL を発行する（どちらか）。

   ```bash
   # A. 手元で署名する（標準ライブラリだけで動く）
   export RECORDING_TOKEN_SECRET="$(gcloud secrets versions access latest \
       --secret=recording-token-secret --project=voice-ai-510014)"
   python3 scripts/issue_test_url.py --service-url "$SERVICE_URL"

   # B. API で発行する（X-API-Key が必要）
   curl -s -X POST "$SERVICE_URL/api/recordings" \
     -H "X-API-Key: $(gcloud secrets versions access latest --secret=backend-api-key --project=voice-ai-510014)" \
     -H "Content-Type: application/json" \
     -d '{"record_id": "test-001", "test": true}'
   ```

3. URL を iPhone に送る（AirDrop・メモなど）。**URL の # 以降がトークン**なので、途中で切れないようにする。
4. 画面下の「テスト設定・診断」で分割方式（A / B）と分割の長さを選べる。本番相当は **A・60秒**。
   短時間で確認したいときは 10 秒などにする。

## 1. テスト項目

各テストの終了時に「診断結果をコピー」を押し、下の記録表に貼る。

| # | 手順 | 見るところ |
|---|---|---|
| 1 | 画面点灯のまま 30 分録音（方式A）→ 録音終了 | 中断 0 回、未送信 0、送信済みが約 30 |
| 2 | 1 と同じ（方式B） | 同上 |
| 3 | 録音中にサイドボタンで画面ロック → 1 分後に解除 | 警告が出るか。ロック中の音声が残っているか（下の「音声の確認」） |
| 4 | 録音中に別の電話から着信（出ない → 切れるまで待つ） | 警告が出るか、再開できるか |
| 5 | 録音中に着信に出て 30 秒話す → 切って戻る | 同上 |
| 6 | 録音中にホームに戻る → 30 秒後にブラウザへ戻る | 同上 |
| 7 | Zoho CRM アプリの商談記録のリンクから開く | アプリ内ブラウザでマイクが使えるか。使えない場合に案内が出るか |
| 8 | 録音中に機内モード 2 分 → 解除 | 未送信が増え、解除後に 0 に戻るか |
| 9 | 録音中にページを再読み込み → 「録音を再開」→ 録音終了 | 未送信が再送され、2 区間で完了するか |
| 10 | 低電力モード ON で 10 分録音 | 画面が消えないか（Wake Lock） |

「画面の点灯維持」が「非対応」「失敗」になる場合は、設定 → 画面表示と明るさ → 自動ロック を「なし」にして再テストする。

### Android での追加項目（お客様の端末は Android の見込み）

ブラウザは Chrome を基本とし、端末に入っていれば Samsung Internet なども試す。

| # | 手順 | 見るところ |
|---|---|---|
| A1 | 録音中に電源ボタンで画面を消す → 1分後に点ける | 画面が消えている間も録音が続くか（音声を聞いて確認） |
| A2 | 録音中にホームに戻る → 1分後にブラウザへ戻る | 同上。警告が出るか |
| A3 | 録音中に着信（出ない／出る） | 警告が出るか、再開できるか |
| A4 | Zoho CRM アプリ（Android）のリンクから開く | アプリ内の画面でマイクが使えるか。使えない場合に「Chrome で開く」案内が出るか |
| A5 | 省電力モード（バッテリーセーバー）ON で 10 分 | 画面が消えないか、録音が止まらないか |
| A6 | 画面点灯のまま 30 分 | 中断 0、未送信 0 |

### お客様に Android で試してもらうとき

お客様向けの手順は [customer-android-test.md](customer-android-test.md)。テストごとに記録 ID を分けて URL を発行し、
結果は Cloud Logging から読む（お客様に診断結果をコピーしてもらう必要はない）。

```bash
export RECORDING_TOKEN_SECRET="$(gcloud secrets versions access latest --secret=recording-token-secret --project=voice-ai-510014)"
URL=https://meeting-notes-943049502425.asia-northeast1.run.app
for t in a1-screen-off a2-home a3-call a4-30min a5-crm-app a6-battery; do
  echo "== $t"; python3 scripts/issue_test_url.py --service-url "$URL" --record-id "android-$t" --hours 168
done

bash scripts/test_results.sh                    # 全テストの診断まとめ（完了・中断のたびに記録される）
bash scripts/test_results.sh android-a1-screen-off   # 1 件の出来事を時系列で
```

## 2. 音声の確認

いちばん簡単なのは GCP コンソール：Cloud Storage → バケット `voice-ai-510014-meeting-audio` →
`recordings/default/<記録ID>/<セッション>/` → ファイル名をクリック →「ダウンロード」。PC の Chrome で再生できる。

Cloud Shell でまとめて取る場合：

```bash
gcloud storage cp -r "gs://voice-ai-510014-meeting-audio/recordings/default/<記録ID>/" ./rec/
```

- 方式A：各ファイル（`000000.m4a` など）をそのまま再生できる。区間の境目で音が欠けていないか聞く。
- 方式B：区間ごとに連結して聞く。

  ```bash
  cd rec/<セッションID> && cat $(ls | sort) > ../joined.m4a
  ```

- ロック・着信のテストでは、止まっていた時間の音声が残っているかを確認する
  （診断結果の `timer_gap_total_seconds` と、実際に聞こえる時間を比べる）。

## 3. 記録表

| # | 日付 | 端末 / iOS | ブラウザ | 方式 | 結果（○△×） | 気づいたこと | 診断結果 |
|---|---|---|---|---|---|---|---|
| 1 | | | | | | | |
| 2 | | | | | | | |

## 4. 診断結果の項目

| 項目 | 意味 |
|---|---|
| `mime_type` | 録音形式（iPhone は `audio/mp4` の見込み） |
| `wake_lock` | 画面の点灯維持（有効 / 非対応 / 失敗 / 解除） |
| `interruptions` | 「録音が止まりました」の警告を出した回数 |
| `timer_gaps` / `timer_gap_total_seconds` | ページ（JavaScript）が止まっていた回数と合計秒数 |
| `hidden_count` | 画面が非表示になった回数（ロック・アプリ切り替え） |
| `track_mute_count` | マイクがミュートされた回数（着信など） |
| `silence_warnings` | 10 秒以上完全な無音が続いた回数 |
| `rotation_gap_ms_median` / `_max` | 方式Aの区切り直しにかかった時間（この間の音声は欠ける可能性） |
| `chunks_uploaded` / `chunks_pending` / `upload_failures` | 送信済み・未送信・送信失敗の回数 |

サーバー側のログ（Cloud Logging）にも `recorder.event` として同じイベントが残る。

```
resource.type="cloud_run_revision" jsonPayload.message="recorder.event" jsonPayload.record_id="test-001"
```
