# バックエンドの Zoho CRM 接続

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
  スコープは CLAUDE.md の9つ（接続先の組織の確認に使う `ZohoCRM.org.READ` を含む）
- 接続先の組織を表示して確認を求める。`no` なら何も保存せずに止まる
- 商談記録・用語辞書の項目が読めるか確かめる
- バックエンドに要る権限（スコープ）を1つずつ読み取りで試す（`crm_setup.py check-access`）。NG があれば何も保存せずに止まる
- Secret Manager の `zoho-client-id` / `zoho-client-secret` / `zoho-refresh-token` に新しい版を足し、
  Cloud Run のサービスアカウントに読み取り権限を付ける
- Cloud Run の `CLIENTS_CONFIG_JSON` の `default` に `zoho` を足す（`DRY_RUN` などほかの設定は変えない）

**この Self Client は削除しない。** 削除するとバックエンドが CRM に接続できなくなる。

## うまくいかないとき

| 表示 | 原因と対処 |
|---|---|
| `invalid_client` | Client ID / Secret の誤り。api-console で値を確かめて、最初からやり直す |
| `invalid_code` | 認可コードの期限切れ（10分）か使用済み。発行し直して貼る（3回まで聞き直す） |
| 権限の確認で NG・処理のログに `OAUTH_SCOPE_MISMATCH` | 認可コードを発行したときのスコープが違う（CRM 作成用のスコープのままなど）。スクリプトが表示するスコープを最後まで貼って発行し直す |
| 組織が違う | 認可コードの発行で組織の選択を間違えている。`no` で止めてやり直す |
| 足りない項目がある | CRM の項目が未作成。[crm-setup.md](crm-setup.md) の手順で作成してから実行する |
