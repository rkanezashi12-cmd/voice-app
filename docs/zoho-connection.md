# バックエンドの Zoho CRM 接続

> 2つ目以降のクライアント（ものづくりSFAデモ2 など）は [monodemo.md](monodemo.md) の手順で、`APP_CLIENT=<クライアント ID>` を付けて行う。
> シークレットはクライアントごとに別の名前になり、マルサン木型（`default`）の設定は変わらない。

バックエンド（Cloud Run）がお客様の Zoho CRM を読み書きするための接続を作る。
先に CRM の「商談記録」「用語辞書」を作っておく（[crm-setup.md](crm-setup.md)）。

`DRY_RUN=true`（既定）のままなので、接続を作っても CRM には書き込まない。

## 事前に決めること

- **バックエンドを動かす Zoho ユーザー。** API の操作はこのユーザーとして行われ、バックエンドが作るレコードの
  所有者も（指定しない限り）このユーザーになる。当面はマルサン木型の管理者アカウントでよい

## 手順（Cloud Shell で 10 分ほど）

1. api-console.zoho.com を上のユーザーで開き、Self Client を用意する（無ければ「Add Client」→「Self Client」→「Create」）
2. Cloud Shell で次を実行し、画面の案内どおりに1つずつ貼る
   （Client ID → Client Secret → 認可コード → 組織の確認で `yes` → 保存の確認で `yes`）

   ```bash
   cd ~/voice-app && git pull && bash scripts/setup_zoho_connection.sh
   ```

スクリプトが行うこと（何度実行しても壊れない）:

- 認可コードをリフレッシュトークンに交換する（トークンは画面に出さない）。
  スコープは CLAUDE.md の10個（接続先の組織の確認に使う `ZohoCRM.org.READ`、全文の添付ファイルに使う
  `ZohoCRM.modules.attachments.ALL` を含む）
- 接続先の組織を表示して確認を求める。`no` なら何も保存せずに止まる
- 商談記録・用語辞書の項目が読めるか確かめる
- バックエンドに要る権限（スコープ）を1つずつ読み取りで試す（`crm_setup.py check-access`）。NG があれば何も保存せずに止まる
- Secret Manager の `zoho-client-id` / `zoho-client-secret` / `zoho-refresh-token` に新しい版を足し、
  Cloud Run のサービスアカウントに読み取り権限を付ける
- Cloud Run の `CLIENTS_CONFIG_JSON` の `default` に `zoho` を足す（`DRY_RUN` などほかの設定は変えない）

**この Self Client は削除しない。** 削除するとバックエンドが CRM に接続できなくなる。

## 全文の添付ファイル（クライアントごと。2026-10-19 追加）

文字起こしの全文を `.txt`（ファイル名 `文字起こし全文_<商談の日付>.txt`）で商談記録に添付する。項目（合計 64,000 文字）に
入りきらない長い商談でも、全文を CRM で読める。項目に入りきらなかったときは、項目の末尾が「（以降省略：全体○○文字。全文は添付ファイル）」になる。

- データは CRM のまま（GCP には置かない）。CRM の画面の商談記録の「添付ファイル」から開ける・保存できる
- 日報を書いたあとに付ける。添付に失敗しても日報は「完了」のまま（ログに `transcript.attachment_failed`）
- `DRY_RUN`（クライアントの `dry_run`）のときは付けない
- 処理をやり直すと（`scripts/reprocess.sh`）、添付がもう1つ増える（バックエンドは消さない。要らない方は CRM の画面で消す）
- CRM の接続に `ZohoCRM.modules.attachments.ALL` が要る。**2026-10-19 より前に発行した接続には無い**ので、使うクライアントだけ発行し直す

使い始める手順（例：monodemo。**マルサン木型（default）で使うときは、マルサン木型の接続を発行し直すことになるので、ユーザーの指示で行う**）

1. コードを入れる：`cd ~/voice-app && git checkout claude/meeting-transcription-app-setup-nkaz1m && git pull && bash scripts/marusan_snapshot.sh save && bash scripts/deploy.sh`
   （**デプロイより先に手順3を実行しない。** 古いコードは `transcript_attachment` を知らず、新しい版が起動に失敗する）
2. 接続を発行し直す：`APP_CLIENT=monodemo ORG_ID=6719392000000020005 bash scripts/setup_zoho_connection.sh`
   （Self Client は今のまま。認可コードは、スクリプトが表示する10個のスコープで発行する。組織は「ものづくりSFAデモ2」を選ぶ。
   途中の権限の確認に「商談記録の添付ファイルの一覧」が出て OK なら、添付の権限も通っている）
3. 添付を使う：`APP_CLIENT=monodemo bash scripts/client_mode.sh --attach-on`（yes）
4. `bash scripts/marusan_snapshot.sh check` で、マルサン木型が変わっていないことを確かめる
5. 短い録音を1件処理し、商談記録の「添付ファイル」に `文字起こし全文_….txt` ができていることを確かめる

## うまくいかないとき

| 表示 | 原因と対処 |
|---|---|
| `invalid_client` | Client ID / Secret の誤り。api-console で値を確かめて、最初からやり直す |
| `invalid_code` | 認可コードの期限切れ（10分）か使用済み。発行し直して貼る（3回まで聞き直す） |
| 権限の確認で NG・処理のログに `OAUTH_SCOPE_MISMATCH` | 認可コードを発行したときのスコープが違う（CRM 作成用のスコープのままなど）。スクリプトが表示するスコープを最後まで貼って発行し直す |
| 組織が違う | 認可コードの発行で組織の選択を間違えている。`no` で止めてやり直す |
| 足りない項目がある | CRM の項目が未作成。[crm-setup.md](crm-setup.md) の手順で作成してから実行する |
