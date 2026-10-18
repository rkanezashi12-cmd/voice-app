# 2つ目のクライアント：ものづくりSFAデモ2（monodemo）

デモ用の Zoho CRM「ものづくりSFAデモ2」を、クライアント `monodemo` としてバックエンドにつなぐ手順。
マルサン木型（クライアント `default`）と同じ Cloud Run・Recall.ai を使い、**マルサン木型の設定・シークレット・動作は変えない**。

| 項目 | 値 |
|---|---|
| クライアント ID | `monodemo`（URL・スクリプトの `APP_CLIENT` に使う） |
| CRM | ものづくりSFAデモ2（domain `monodemo2`、組織 ID `6719392000000020005`、Asia/Tokyo、JPY、ユーザー1名） |
| DC | **us**（ログインの URL が `crm.zoho.com`。2026-10-14 ユーザー確認） |
| 主の管理者 | `zoho_master114@funaisoken.co.jp`（CRM の会社情報の主な連絡先） |
| GCP・Recall.ai | マルサン木型と共通（`voice-ai-510014` の Cloud Run `meeting-notes`、Recall.ai 東京のアカウント） |

## 約束（CLAUDE.md のルール5〜7）

- **書き込むのは新しく作るものだけ**：商談記録・用語辞書（と、その中の項目）・関数・ワークフロー・変数・ウィジェット。
  例外は商談記録の「先方担当者（連絡先）」（連絡先に逆向きの項目と関連リストが足される。連絡先のデータは変えない）。
  既存のモジュール（Nippou・NippouLink・Inquiry・Repair・TEST など）・項目・レイアウト・ワークフロー・データは変えない（2026-10-14 ユーザー確認）
- `monodemo` は **DRY_RUN（CRM に書き込まない）・【TEST】あり** で始める。書き込みに切り替えるのはユーザーの指示があるとき（手順7）
- シークレットは `monodemo-` を頭に付けた別の名前（`monodemo-zoho-client-id` など）。マルサン木型の `zoho-client-id` などには触らない
- Recall.ai はマルサン木型と同じアカウント。**Recall.ai のダッシュボードに Webhook の送り先を足さない**
  （通知はボットの `metadata.client_id` で `monodemo` に振り分ける）

## マルサン木型に影響しないための仕組み（必須）

| 心配なこと | 仕組み |
|---|---|
| マルサン木型のシークレットを上書きする | `APP_CLIENT=monodemo` のスクリプトは `monodemo-…` のシークレットにだけ保存する。`default` には `APP_CLIENT` を付けない限り触らない |
| 違う組織に接続してしまう | 接続スクリプトは `ORG_ID=6719392000000020005` でなければ何も保存せずに止まる |
| マルサン木型の DRY_RUN・【TEST】が変わる | `monodemo` は自分の `dry_run`・`crm_test_records` を持つ。`client_mode.sh` は `default` を断る |
| ボットの通知がマルサン木型側で処理される | 通知はボットの `metadata.client_id` で振り分ける。マルサン木型のボットは `default` のまま |
| 設定を壊して Cloud Run が止まる | 設定はスクリプトが作るので手で書かない。もし新しい版が起動できなくても、Cloud Run は前の版（マルサン木型が動いている版）のまま動き続ける |
| 録音アプリで違う会社の CRM に記録を作る | URL の会社（`?c=`）とログインの会社が違えば、ログインし直しになる |
| 気づかないうちに変わる | `scripts/marusan_snapshot.sh` で、作業の前にマルサン木型の設定・シークレットの版・DRY_RUN を控え、各手順のあとで比べる（読み取りだけ） |

**各手順のあとで必ず次を実行し、`OK` が出ることを確かめる。** 違いが出たらそこで止めて、表示を送る。

マルサン木型の改修は、これまでどおり行ってよい（`APP_CLIENT` を付けずにスクリプトを実行する）。わざとマルサン木型の設定・シークレットを
変えたあとは、`bash scripts/marusan_snapshot.sh save` で控えを取り直す。

```bash
cd ~/voice-app && bash scripts/marusan_snapshot.sh check
```

## 進め方

| 手順 | やること | 場所 | 状態 |
|---|---|---|---|
| 1 | コードを入れる（デプロイ） | Cloud Shell | |
| 2 | クライアント `monodemo` を足す | Cloud Shell | |
| 3 | Self Client を作る | ブラウザ（api-console） | |
| 4 | 商談記録・用語辞書を作る（`crm_setup.py`） | Cloud Shell | |
| 5 | バックエンドの接続 | Cloud Shell | |
| 6 | 変数・関数・ワークフロー（録音用URL・ボット） | CRM の画面 | |
| 7 | 書き込みに切り替える（ユーザーの指示があるとき） | Cloud Shell | |
| 8 | 録音アプリのログイン（`/app/?c=monodemo`） | Cloud Shell・スマホ | |
| 9 | ボット（Recall.ai） | Cloud Shell・会議 | |
| 10 | 商談日報ウィジェット | CRM の画面 | |

ユーザーの依頼の順（Self Client → 接続 → crm_setup.py）から、**4と5を入れ替えている**。
バックエンドの接続（手順5）は、商談記録・用語辞書の項目が読めるかを確かめてから保存するので、先に項目を作る。

コマンドは **1行ずつ貼って実行する**（入力を待っている間に次の行まで貼ると、次の行が値として読み込まれる）。
Cloud Shell では **Ctrl+C を押さない**（コピーではなく中止になる）。

---

## 1. コードを入れる（Cloud Shell、5分）

クライアントごとのシークレット名・Webhook の振り分け・クライアントごとの DRY_RUN が入ったコードにする。
マルサン木型の動きは変わらない（マルサン木型のボットの通知は `metadata.client_id` が `default` なので、今までどおり処理される）。
念のため、マルサン木型の商談（ボット・録音）の処理中は避ける。

1. コードを取ってくる（PR を既定のブランチ `claude/meeting-transcription-app-setup-nkaz1m` にマージしたあと）

   ```bash
   cd ~/voice-app && git checkout claude/meeting-transcription-app-setup-nkaz1m && git pull
   ```

2. **作業の前のマルサン木型の状態を控える**（読み取りだけ。`~/marusan-snapshot.json` ができる）

   ```bash
   bash scripts/marusan_snapshot.sh save
   ```

3. デプロイする（環境変数は変えない）

   ```bash
   bash scripts/deploy.sh
   ```

   最後に `{"status":"ok",...}` と `デプロイしました` が出れば終わり
4. `bash scripts/marusan_snapshot.sh check` → `OK` を確かめる

## 2. クライアント `monodemo` を足す（Cloud Shell、3分）

```bash
cd ~/voice-app && APP_CLIENT=monodemo DISPLAY_NAME="ものづくりSFAデモ2" bash scripts/setup_client.sh
```

- CRM のワークフローがバックエンドを呼ぶときの API キーを作り、`monodemo-backend-api-key` に保存する（値は表示しない。手順6で作り直して表示する）
- Cloud Run のクライアント設定に `monodemo` を `dry_run: true`・`crm_test_records: true` で足す。マルサン木型の `default` は変えない
- `保存して設定を更新するなら yes` に `yes`

## 3. Self Client を作る（ブラウザ、5分）

1. **ものづくりSFAデモ2 の管理者の Zoho アカウント**で https://api-console.zoho.com を開く
   （US DC。`api-console.zoho.jp` ではない）
2. 「GET STARTED」か「ADD CLIENT」→「Self Client」→「CREATE」→ 確認に「OK」
3. 「Client Secret」タブの **Client ID**（`1000.` で始まる）と **Client Secret** を、このあと貼る（メモ帳などには残さない）

> **マルサン木型と同じ Zoho アカウントで開いたときの注意**：Self Client は Zoho のユーザー1人に1つで、既に作ってあれば
> 「Self Client」がそのまま出る。それを使ってよい（認可コードを発行するときに組織を選ぶので、マルサン木型とは別の接続になる）。
> **その Self Client の削除や Client Secret の作り直しは絶対にしない**（マルサン木型のバックエンドが CRM に接続できなくなる）。

認可コードはまだ発行しない（10分で失効するので、手順4・5の途中で発行する）。

## 4. 商談記録・用語辞書を作る（Cloud Shell、15分）

[crm-setup.md](crm-setup.md) の手順2〜4と同じ。違うところだけ書く。

1. 手順2の2（Client ID と Secret を入れる）：コマンドの `ZOHO_DC=com` はそのまま（US DC）。手順3の Self Client の値を貼る
2. 手順2の3（認可コード）：スコープは crm-setup.md のもの
   （`ZohoCRM.org.READ,ZohoCRM.settings.modules.ALL,ZohoCRM.settings.fields.ALL,ZohoCRM.settings.profiles.READ`）。
   **組織を選ぶ画面が出たら「ものづくりSFAデモ2」を選ぶ**
3. 手順3（`python3 scripts/crm_setup.py show-org`）で、次の3つを**目で確かめてから** `export EXPECTED_...` の3行を実行する。
   違えば止めて連絡する（ここで止めれば何も書き込んでいない）

   ```
   company_name: ものづくりSFAデモ2
   domain_name: monodemo2
   id: 6719392000000020005
   ```

4. 手順4：`python3 scripts/crm_setup.py plan` の内容を送る（商談記録・用語辞書とその項目、先方担当者（連絡先）だけが出るはず）。
   確かめてから `python3 scripts/crm_setup.py apply` → `yes`
5. 最後に `python3 scripts/crm_setup.py show-fields` の結果を送る（実物の形を記録する）

連絡先に「商談記録」という名前の項目が既にあるときは、何もせずに止まる（既存の項目を変えないため）。そのときは連絡する。

## 5. バックエンドの接続（Cloud Shell、10分）

[zoho-connection.md](zoho-connection.md) と同じスクリプトを、クライアントと組織 ID を付けて実行する。

```bash
cd ~/voice-app && APP_CLIENT=monodemo ORG_ID=6719392000000020005 bash scripts/setup_zoho_connection.sh
```

- Client ID・Client Secret は手順3の Self Client の値（手順4と同じ）
- 認可コードは**手順4とは別のスコープ**（スクリプトが表示する9つ）で発行し直す。組織は「ものづくりSFAデモ2」
- 接続先の組織 ID が `6719392000000020005` でなければ、何も保存せずに止まる（マルサン木型の組織を選んでしまったときなど）
- 保存先は `monodemo-zoho-client-id` / `monodemo-zoho-client-secret` / `monodemo-zoho-refresh-token`。マルサン木型の
  `zoho-client-id` などは変えない
- Cloud Run の `monodemo` に `zoho` を足す（`dry_run: true` のまま）

スクリプトが、項目が読めるか・権限（スコープ）がそろっているかを読み取りで確かめてから保存する。
あとで商談記録の読み取りを切り分けるときは `APP_CLIENT=monodemo bash scripts/check_backend_zoho.sh <商談記録の ID>`。

**この Self Client は削除しない**（バックエンドが使い続ける）。

## 6. 変数・関数・ワークフロー（CRM の画面、20分）

ものづくりSFAデモ2 の CRM で、[crm-workflow.md](crm-workflow.md) の手順2〜4と、[recall-bot.md](recall-bot.md) の手順2を行う。違うところだけ書く。

1. API キー（変数 `meeting_notes_api_key` の値）は、`monodemo` のキーを作り直して表示する（マルサン木型のキーは変わらない）

   ```bash
   cd ~/voice-app && APP_CLIENT=monodemo bash scripts/rotate_api_key.sh
   ```

   URL（変数 `meeting_notes_url` の値）はマルサン木型と同じ（`gcloud run services describe meeting-notes --project=voice-ai-510014 --region=asia-northeast1 --format='value(status.url)'`）
2. 変数・関数（`issue_recording_url` と `create_bot`）・ワークフロー（「録音用URLの発行」と「ボットの予約」）を、手順書のとおりに新しく作る。
   既存の関数・ワークフローは変えない。**引数の設定で `rec_id` に「商談記録 Id」を割り当てる**（API では設定できない。忘れると何も起きない）
3. 確かめる：商談記録を1件作る（商談記録名「【TEST】録音URLの確認」、取得方法「対面録音」）。
   関数「録音用URLを発行」の実行ログの最後が `===== SUCCESS crm_updated=false` なら通っている（DRY_RUN なので false が正しい）。
   バックエンドのログでも確かめるとき：

   ```bash
   gcloud logging read 'resource.type="cloud_run_revision" AND jsonPayload.client_id="monodemo"' \
     --project=voice-ai-510014 --freshness=1h --limit=20 \
     --format='table(timestamp.date(tz=Asia/Tokyo),jsonPayload.message,jsonPayload.record_id)'
   ```

   `recording.url_issued` と `dry_run.skip` が出ていればよい。`auth.api_key_rejected` なら変数の API キーの貼り間違い

商談記録・用語辞書は、デモ2の CRM の左のメニュー（「CRMのチームスペース」の一覧）に出る。上のタブの並びには出ないことがあるので、
そのときは次の URL で開く：商談記録 `https://crm.zoho.com/crm/monodemo2/tab/CustomModule13`、用語辞書 `https://crm.zoho.com/crm/monodemo2/tab/CustomModule14`
（`/crm/` のあとは組織 ID ではなく domain の `monodemo2`）。関数の実行ログは、反映に数分かかる。

ボットの予約は、手順9（Recall.ai の設定）が済むまで動かない（`recall` の設定が無いので、関数「ボットを予約」の実行ログにエラーが出る。
CRM には何も書かない）。手順9まではボットの商談記録を作らない。

## 7. 書き込みに切り替える（ユーザーの指示があるとき、3分）

```bash
cd ~/voice-app && APP_CLIENT=monodemo bash scripts/client_mode.sh            # 今の状態を見るだけ
cd ~/voice-app && APP_CLIENT=monodemo bash scripts/client_mode.sh --write    # CRM に書き込む → yes
```

`monodemo` だけが変わる（マルサン木型の `DRY_RUN` は変えない）。戻すときは `--dry-run`。
切り替えたら、手順6の3をもう一度行い、商談記録の「録音用URL」に URL が入ることを確かめる（商談記録名の先頭に【TEST】）。

## 8. 録音アプリのログイン（Cloud Shell 15分・スマホ 10分）

[visit-app.md](visit-app.md) の手順1を、クライアントを付けて行う。

```bash
cd ~/voice-app && APP_CLIENT=monodemo bash scripts/setup_app_login.sh
```

- API コンソール（https://api-console.zoho.com）に、**ものづくりSFAデモ2 の管理者のアカウントで**「Server-based Applications」の
  クライアントを新しく作る（スクリプトが出す Homepage URL・Redirect URI。マルサン木型のログイン用クライアントとは別にする）
- 保存先は `monodemo-app-login-client-id` などの別のシークレット。地図の API キーも `monodemo-app-geocoding-…` の別のキーで、
  作り直すときに消すのは `monodemo` のキーだけ
- 最後に出る URL は `https://<サービス>/app/?c=monodemo`。**この URL で開く**（`?c=monodemo` が無いとマルサン木型のログインになる）

スマホで URL を開き、「Zoho でログイン」から ものづくりSFAデモ2 のアカウントでログインする（手順は visit-app.md の手順2）。

> ログインはブラウザに1つだけ。同じスマホでマルサン木型の録音アプリ（`?c=default`）を開くと「別の会社の CRM にログインしています」と出て、
> ログインし直しになる（違う会社の CRM に商談記録を作らないため）。

## 9. ボット（Recall.ai、Cloud Shell 5分・会議 10分）

```bash
cd ~/voice-app && APP_CLIENT=monodemo bash scripts/setup_recall.sh
```

1. 「default（マルサン木型）と同じ Recall.ai のアカウントを使いますか？」に `yes`
   （マルサン木型の API キーと Webhook のシークレットを参照するだけで、保存し直さない）
2. ボットの表示名（会議の参加者に見える名前。録音していると分かる名前）
3. 保存に `yes`

**Recall.ai のダッシュボードに Webhook の送り先を足さない**（送り先はマルサン木型と共通。足すと通知が二重になる）。
試し方は [recall-bot.md](recall-bot.md) の手順3（テスト用の会議で、取得方法「ボット」の商談記録を作る）。
DRY_RUN の間はバックエンドが「Recall ID」を書かないので、保存するたびにボットを予約し直す。ボットは手順7のあとで試す。

## 10. 商談日報ウィジェット（CRM の画面、10分）

ZIP はマルサン木型と同じもの（項目の API 名が同じため）。[widget.md](widget.md) の手順1・2を、ものづくりSFAデモ2 の CRM で行う。

---

## うまくいかないとき

| 表示 | 原因と対処 |
|---|---|
| `クライアント monodemo がまだありません` | 手順2を先に行う |
| `ORG_ID（…）と違います` | 認可コードの発行で、ものづくりSFAデモ2 以外の組織を選んだ。何も保存していない。発行し直す |
| `invalid_client` | Client ID / Secret の誤りか、DC が違う（US の `api-console.zoho.com` で作った Self Client か） |
| 録音アプリで「別の会社の CRM にログインしています」 | 同じブラウザで別のクライアントにログインしていた。表示どおりログインし直す |
| ボットの状態が変わらない（`webhook.client_unresolved` がログに出る） | ボットの `metadata.client_id` が無いか、署名が合うクライアントと違う。ボットを作ったバックエンドのコードが古くないか（手順1）を確かめる |

## 記録

- 2026-10-14：Zoho CRM のコネクタ（読み取り）で組織を確認（会社名・domain `monodemo2`・組織 ID・Asia/Tokyo・JPY・有料ライセンス1名）。
  商談記録（`MeetingRecords`）・用語辞書（`Glossary`）はまだ無い。既存のカスタムモジュールは Nippou・NippouLink・Inquiry・Repair・TEST・
  Claimmanagement・Deliveredproduct・CustomModule9・CustomModule12
- 2026-10-14：手順1（デプロイ 68d328f）・手順2（`setup_client.sh`）。`marusan_snapshot.sh check` は各手順のあとで OK
- 2026-10-14：手順3（Self Client。api-console.zoho.com、14 October 2026 作成）・手順4（`crm_setup.py apply`）。
  商談記録・用語辞書と全項目が API 名どおりに作成された。デモ2の連絡先の表示名は「見込み客・担当者」で、
  先方担当者（連絡先）の中間モジュールは「商談記録 X 見込み客・担当者」（API 名 `X`）、連絡先の逆向きの項目は `field20`
  （表示名「商談記録」）、中間モジュールの連絡先のルックアップは `field20_1`
- 2026-10-14：手順5（`setup_zoho_connection.sh`）。組織の照合・項目の読み取り・権限（スコープ）の確認がすべて OK で、
  `monodemo-zoho-client-id` / `monodemo-zoho-client-secret` / `monodemo-zoho-refresh-token` に保存した。`dry_run: true` のまま。
  api-console の Generate Code の画面は「Code expiry duration」（既定 3 minutes。10 minutes に変える）・「Description」で、
  Description に `meeting-notes backend`（ハイフン入り）を入れたらエラーになった（エラーの文は未確認。記号を除いた説明文を案内して進めた）
- 2026-10-15 00:13：手順6（変数・関数 `issue_recording_url`・ワークフロー「録音用URLの発行」）。テストの商談記録
  「【TEST】録音URLの確認」で、バックエンドのログに `recording.url_issued`（client_id=monodemo）が出て、CRM の「録音用URL」は
  空のまま（monodemo の `dry_run: true` が効いている。サービス全体の DRY_RUN は false）。CRM の関数の実行ログには直後は出なかった（画面に「反映には最大で5分」とある）。
  関数の本文は `{` と `}` の間だけを置き換えると「Improper code format」になったので、1行目のシグネチャから全文を貼って保存した
- 2026-10-15：手順7（ユーザーの指示で `client_mode.sh --write`。monodemo だけ dry_run=false、【TEST】あり）。直後の
  `marusan_snapshot.sh check` は OK
- 2026-10-15：手順8（`setup_app_login.sh`）。1回目は Google の API を有効にする操作が 429（1分あたりの回数の上限）で止まった
  （保存の前なので何も保存していない）。1〜2分おいて2回目で完了。`monodemo-app-login-client-id` / `monodemo-app-login-client-secret` /
  `monodemo-app-session-secret` / `monodemo-app-maps-api-key`（地図のキーは `monodemo-app-geocoding-20261014152750`）。
  録音アプリの URL は `https://meeting-notes-tnpxeypzla-an.a.run.app/app/?c=monodemo`。スクリプトは、有効な API を有効にし直さないように直した
- 2026-10-15：手順8のやり直し。1回目に貼ったのは Self Client の Client ID・Secret だったため、ログインが「無効なリダイレクトURI」に
  なった。API コンソールの「voice_day」はマルサン木型（default）のログイン用クライアント（同じ Zoho アカウントにある。触らない）。
  デモ2用の Server-based Applications のクライアントを新しく作り、`setup_app_login.sh` で貼り直して（各シークレットの版2）ログインできた。
  地図のキーも作り直した（`monodemo-app-geocoding-20261014152750` を削除。マルサン木型の `app-geocoding-…` は消していない）。
  途中で、期限切れのログインが default の画面に戻ってログインが入れ替わる不具合が見つかり、直した（`app/routers/app_auth.py`）
- 手順8の注意：API コンソールでは、Self Client・voice_day（マルサン木型）と取り違えないように、デモ2用のクライアントの名前で選ぶ
- 2026-10-18：PR #17 をデプロイ（5efce38）。`marusan_snapshot.sh check` は OK。スマホの録音アプリ（`?c=monodemo`）で、ログイン →
  訪問先・担当者の選択 → 録音 → 日報（要約・文字起こし）まで通った。スマホのブラウザがマルサン木型だけに入っている Zoho アカウントで
  ログイン済みだと、組織の選択にデモ2が出ない（シークレットタブで、デモ2のユーザーのアカウントでログインして解決）
