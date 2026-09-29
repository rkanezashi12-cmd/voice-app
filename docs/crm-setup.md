# Zoho CRM の設定手順（商談記録・用語辞書）

作業先：**株式会社マルサン木型製作所の本番 Zoho CRM（US DC）**。
作るのは新しいモジュール2つと、その中の項目だけ。**既存のモジュール・項目・レイアウト・関数・ワークフローには触らない。**

`scripts/crm_setup.py`（Cloud Shell で実行）で自動作成する。スクリプトの約束：

- 書き込む前に接続先の組織を照合し（組織 ID・ドメイン名・会社名）、1つでも違えば何もせずに止まる
- 既定は `plan`（作るものを表示するだけ）。`apply` でも最後に「yes」と入力するまで書き込まない
- 使う書き込みは「作成（POST）」だけ。既存のものの変更・削除はしない。作成済みのものは飛ばす（何度流しても同じ結果）
- 同じ名前の別モジュール・別項目が既にあれば、何もせずに止まる
- 通信の記録は `logs/crm_setup-日時.jsonl` に残る

所要時間：15分ほど。

## 1. Self Client を作り、認可コードを発行する（ブラウザ）

1. **マルサン木型の CRM の管理者アカウント**で https://api-console.zoho.com を開く
2. 「Add Client」→「Self Client」→「Create」（既にあればそれを使う）
3. 「Client Secret」タブの **Client ID** と **Client Secret** を控える
4. 「Generate Code」タブで次を入れて「Create」
   - Scope：`ZohoCRM.org.READ,ZohoCRM.settings.modules.ALL,ZohoCRM.settings.fields.ALL,ZohoCRM.settings.profiles.READ`
   - Time Duration：10 minutes
   - Scope Description：CRM setup
   - 組織（ポータル）を選ぶ画面が出たら、**マルサン木型の本番組織**を選ぶ
5. 表示された **認可コード** をコピーする（**10分で失効**。すぐに手順2へ）

このトークンはモジュールと項目を作るためだけのもので、バックエンドの Zoho 接続とは別物。作業が終わったら手順5で無効にする。

## 2. Cloud Shell で接続する

```bash
cd ~/voice-app && git pull
export ZOHO_DC=com
read -rp "Client ID: " ZOHO_CLIENT_ID && export ZOHO_CLIENT_ID
read -rsp "Client Secret: " ZOHO_CLIENT_SECRET && export ZOHO_CLIENT_SECRET && echo
python3 scripts/crm_setup.py exchange-code <認可コード>
```

表示された `export ZOHO_REFRESH_TOKEN='...'` の行をそのまま貼って実行する。

## 3. 接続先を確かめる（書き込みなし）

```bash
python3 scripts/crm_setup.py show-org
```

会社名・ドメイン名がマルサン木型の本番であることを**目で確かめてから**、表示された `export EXPECTED_...` の3行を実行する。
違う組織が出たら、ここで止めて連絡する（手順1の組織の選択を間違えている）。

## 4. 作るものを確かめて、作成する

```bash
python3 scripts/crm_setup.py plan     # 表示だけ。何も変更しない
python3 scripts/crm_setup.py apply    # 内容をもう一度表示し、yes と入力すると作成する
```

最後に「完了しました。すべての項目が API 名どおりに作成されています。」と出れば終わり。
問題が出たら、表示された内容と `logs/crm_setup-*.jsonl` の最後の数行を送る（トークンは記録していない）。

## 5. 後片付け

- api-console.zoho.com の Self Client で、このトークンを無効にする（「Revoke」）か、Self Client ごと削除する
- Cloud Shell を閉じれば環境変数は消える

## 6. 画面で行う仕上げ（任意）

- レイアウトの並べ替え（項目は既定のセクションにまとめて入る）。おすすめは次の4セクション
  - 基本情報：商談形態〜Recall ID
  - 商談の内容：要約〜決裁者
  - 文字起こし：文字起こし全文・文字起こし全文2
  - 処理状況：状態・エラー内容
- 用語辞書に1件登録してみる（例：用語「マルサン木型」、誤認識例「丸三木型、まるさん」、種類「社名」）

---

## 作成される内容

### 商談記録（API 名 MeetingRecords）

| 設定 | 値 |
|---|---|
| モジュール名（単数・複数） | 商談記録 |
| API 名 | **MeetingRecords**（アンダースコア不可） |
| 権限（プロファイル） | すべてのプロファイル（スクリプトが自動で付ける） |

名前の項目 **「商談記録名」**（API 名 `Name`）と **「商談記録の担当者」**（`Owner`）はモジュールと一緒に作られる。

#### 項目

| # | 表示名 | 種類 | API 名 | 設定 |
|---|---|---|---|---|
| 1 | 商談形態 | 選択リスト | `Meeting_Type` | 選択肢：対面 / オンライン |
| 2 | 取得方法 | 選択リスト | `Capture_Method` | 選択肢：デスクトップ / ボット / 対面録音 |
| 3 | 状態 | 選択リスト | `Status` | 選択肢（この順）：予約済 / 参加待ち / 参加中 / 参加失敗 / 録音中 / 文字起こし中 / 完了 / 失敗 / 取引先未設定 |
| 4 | 取引先 | ルックアップ | `Account` | 関連付けるモジュール：取引先 |
| 5 | 商談 | ルックアップ | `Deal` | 関連付けるモジュール：商談 |
| 6 | 先方担当者 | 1行 | `Contact_Name` | 文字数 255 |
| 7 | 開始日時 | 日付/時刻 | `Start_At` | |
| 8 | 会議URL | URL | `Meeting_URL` | |
| 9 | 録音用URL | URL | `Recording_URL` | バックエンドが書き込む |
| 10 | Recall ID | 1行 | `Recall_ID` | 文字数 255。**「重複する値を許可しない」に✓** |
| 11 | エラー内容 | 複数行（小） | `Error_Message` | |
| 12 | 要約 | 複数行（大） | `Summary` | |
| 13 | 課題 | 複数行（大） | `Issues` | |
| 14 | ニーズ | 複数行（大） | `Needs` | |
| 15 | 次のアクション | 複数行（大） | `Next_Actions` | |
| 16 | 次回期限 | 日付 | `Due_Date` | |
| 17 | 分類 | 1行 | `Category` | 文字数 255（選択肢が決まったら選択リストに変える） |
| 18 | 競合 | 複数行（大） | `Competitors` | |
| 19 | 予算 | 1行 | `Budget` | 文字数 255 |
| 20 | 決裁者 | 1行 | `Decision_Maker` | 文字数 255 |
| 21 | 文字起こし全文 | 複数行（大） | `Transcript` | |
| 22 | 文字起こし全文2 | 複数行（大） | `Transcript_2` | 1つ目に入りきらない分（32,000文字超）が入る |

- 「複数行（大）」は最大 32,000 文字。
- どれも必須項目にはしない（バックエンドが後から書き込むため）。

### 用語辞書（API 名 Glossary）

文字起こしの補正に使う（社名・製品名など、聞き間違えやすい言葉を登録する）。

| 設定 | 値 |
|---|---|
| モジュール名（単数・複数） | 用語辞書 |
| API 名 | **Glossary** |
| 権限（プロファイル） | 商談記録と同じ |

名前の項目は表示名 **「用語」**（API 名 `Name`）でモジュールと一緒に作られる。

| # | 表示名 | 種類 | API 名 | 設定 |
|---|---|---|---|---|
| 1 | 誤認識例 | 複数行（小） | `Misrecognitions` | 聞き間違いの例を「、」区切りで入れる（例：丸三木型、まるさん） |
| 2 | 種類 | 選択リスト | `Term_Type` | 選択肢：社名 / 製品名 / 人名 / 専門用語 / その他 |
