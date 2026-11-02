# 運用の作業を GitHub Actions で行う（Cloud Shell を使わない）

デプロイ・対面録音の再処理・クライアントの設定・費用の確認は、GitHub Actions のワークフロー `ops`（`.github/workflows/ops.yml`）で行う。
Claude が GitHub の連携（`actions_run_trigger`）で起動し、ログを読んで結果を確かめる。ユーザーは GitHub の画面（Actions → ops →
Run workflow）からも起動できる。

| 作業（command） | 中身 | 入力 |
|---|---|---|
| `deploy` | ランナーでイメージを作り、Artifact Registry に載せて Cloud Run にデプロイ（`scripts/deploy.sh`。環境変数は変えない） | — |
| `reprocess` | 対面録音の処理をもう一度積む（`scripts/reprocess.sh`）。`force` で「完了」の記録もやり直す（日報は書き直される） | client・record_id・force |
| `client-mode` | クライアントの設定を見る・変える（`scripts/client_mode.sh`） | client・option |
| `usage` | Gemini の使用量と費用の目安・処理した件数（`scripts/usage_report.sh`） | hours |

- `usage` 以外は、作業の前後で `marusan_snapshot.sh save` / `check` を同じ実行の中で行い、マルサン木型が変わっていないことを確かめる
  （マルサン木型の設定を意図して変える `client-mode`（client=default）だけは比べない）
- **このリポジトリは公開。Actions のログは誰でも見られる。** ログに出すのは件数・状態・ID だけにする（商談の中身・会社名・
  シークレットの値を出す作業は ops に入れない）。録音の保存（`scripts/save_recording.sh`）は音声を扱うので ops に入れない
- 認証は Workload Identity 連携（鍵ファイルなし）。通るのは、このリポジトリの既定のブランチから動いたワークフローだけ
- GitHub Actions のサービスアカウント `meeting-notes-deployer` に渡しているのは、デプロイ・環境変数の変更・ログの読み取り・
  Cloud Tasks への登録・シークレットの版の番号の読み取りだけ。シークレットの値は読めない。IAM も変えられない
- Cloud Shell が要るのは、最初の準備（`scripts/setup_github_actions.sh`）と、CRM・Recall.ai などの接続を作り直すとき
  （認可コードの入力）・録音を手元に残すときだけ

## 最初の準備（Cloud Shell で1回）

```bash
cd ~/voice-app && git pull && bash scripts/setup_github_actions.sh
```

最後に「OK：deploy/ops.env の WIF_PROVIDER と一致しています」と出れば終わり。違う値が出たら Claude に貼る。

## 「完了」の記録の全文を添付し直す（項目に入りきらなかった長い商談）

録音は処理が成功すると消えるので、`save_recording.sh` で保存しておいた MP3 を戻してからやり直す。

1. Cloud Shell：`bash scripts/restore_recording.sh <クライアント> <ID>`（保存した MP3 を GCS に戻す）
2. そのクライアントの全文の添付を使えるようにしておく（docs/zoho-connection.md の「全文の添付ファイル」）
3. ops：`reprocess`（client・record_id・force=true）
