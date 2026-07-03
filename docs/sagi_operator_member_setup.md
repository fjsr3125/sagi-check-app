# Unari Sagi Operator メンバーMac手順書

## 目的

メンバーのMacだけで、詐欺チェック用の強session補充から詐欺チェック、シート書き戻し確認まで進める。
sessionファイルは各Macの中だけで使い、他のMacへ共有しない。

詐欺チェック本番と verify は常に no proxy で実行する。
ただし、強session補充の最中だけ、AVD内のInstagram通信をMac内で受けるために `10.0.2.2:8080` のローカルcapture経路を使う。
これは外部proxyではない。

## 事前に用意するもの

- Apple Silicon Mac
- 安定したWi-Fi
- iPhoneテザリング
- チェック用Instagramアカウントの username / password
- Google Sheetsへアクセスできる状態
- 配布された `Unari Sagi Operator.dmg`

Python / Homebrew / Java は事前に入れなくてよい。
必要なPythonはアプリに入っている。JavaはAndroid SDK導入時にアプリ用フォルダへ自動で入る。
Instagram password は Operator 画面には入力しない。
AVDで開いたInstagramアプリの中だけに手入力する。

## 録画時の注意

全工程を録画してよいが、次は映さない。

- Instagram password
- 2FA / 認証コード
- Google / Slack の認証情報
- `sessions/` や `captures/` の中身
- sessionid / Authorization / cookie が見えるログ

映りそうな場面では、録画を一時停止するか、後からぼかす。

## 手順

### 1. アプリを開く

1. `Unari Sagi Operator.dmg` をダブルクリックして開く
2. 中にある `Unari Sagi Operator.app` を `アプリケーション` にコピーする
3. `アプリケーション` の `Unari Sagi Operator.app` を右クリックして `開く` を押す
4. 初回だけ数分待つ
5. ブラウザで `Unari Sagi Operator` が開く
6. 画面上部の `初回セットアップ` を見る

何も起きない場合は、Finderで `移動 > フォルダへ移動...` を押し、次の場所の最新ログを藤巻へ渡す。

```text
~/Library/Logs/UnariSagiOperator/
```

Chromeで `localhost:5070` の `このサイトにアクセスできません` が出た場合も、同じ場所の最新ログを藤巻へ渡す。
これはChromeの問題ではなく、アプリの裏側サーバーが起動できていない状態。

ログに `No module named 'flask'` が出ている場合は、古いPython環境が途中で壊れている。
最新版のDMGへ入れ替えれば自動で作り直す。
それでも直らない場合だけ、Finderで `移動 > フォルダへ移動...` から次を開き、`unari` フォルダを削除してからアプリを開き直す。

```text
~/Library/Application Support/UnariSagiOperator/
```

### 2. 初回セットアップを緑にする

`初回セットアップ` の赤い項目を上から順に進める。

1. 迷ったら先に `まとめて導入（①〜⑤）` を押す
2. 個別に進める場合は `① Python環境` を押す
3. `② Android SDK` を押す
4. `③ Android画面作成` を押す
5. `初回セットアップ実行ログ` に文字が流れていることを確認し、完了まで待つ
6. `④ 通信用設定` を押す
7. `⑤ Instagram導入` を押す
8. シートを使う前に `⑥ Google Sheets接続設定` を確認する
9. `状態更新` を押し、主要項目が緑になったことを確認する

補足:

- Android SDK は数GBのダウンロードがある
- AVDにPlay Storeが無いのは正常。root/CA設定のためにPlay StoreなしのGoogle APIs imageを使う
- Instagram本体は配布DMGの中に同梱済み。メンバー側で別ファイルを探したり、Downloadsへ置いたりしない
- Javaが無いMacでは、Android SDK導入時にアプリ用Javaも自動で入る
- macOSの許可画面が出たら許可する
- Android SDKライセンス同意が出たら進める
- `④ 通信用設定` はAndroid画面の起動に時間がかかる
- `③ Android画面作成` 中にログ欄が止まって見えても、数分単位のダウンロード中なら待つ

### 2-1. Instagram導入で止まった場合

メンバー本人がAPK配布サイトを探さない。
通常は配布DMGの中にInstagramが入っているので、`Instagram導入` を押すだけでよい。

`InstagramのAPK/APKM/XAPKが見つかりません` と出た場合は、配布DMGが古いか、管理者側の同梱漏れ。
メンバー本人では直さず、画面スクショとログを藤巻または管理者へ渡す。

管理者側は、Instagram同梱済みの最新版DMGを作り直して再配布する。

#### 再配布版を受け取ったら

1. `Unari Sagi Operator` の画面に戻る
2. `初回セットアップ` を開く
3. `Instagram導入` を押す
4. 完了後に `状態更新` を押す
5. `Instagramアプリ` が緑になればOK

### 3. 強sessionを補充する

1. `強session補充` を開く
2. Instagram username を入れる
3. Password欄はない。passwordはOperatorに入れない
4. `MacがiPhoneテザリングに接続済み` にチェックする
5. `sessionを1本作る（推奨）` を押す
6. Android画面上でInstagramログイン画面が出たら、人間がusername/passwordを手入力する
7. メール確認 / 2FA が出たら、録画を止めてAndroid画面で手動完了する
8. フィードが出たら、アプリが自動で更新してcaptureを発生させるのでそのまま待つ
9. ログに `login_input_error` が出たら、username/passwordを確認する。連打せず、その実行は止める
10. 画面のログが完了になったら、必要な場合だけ `作ったsessionを確認` を押す

成功の目安:

- `sessions/{username}.json` がこのMac内に作られる
- `作ったsessionを確認` が成功する
- 画面の `次の一手` が完了系になる

手動認証が必要な時:

- `manual_login_mode`: Android画面でInstagramに手動ログインする
- `manual_login_required` / `challenge_or_2fa:check your email`: Android画面でメール確認や2FAを完了する
- `manual_login_timeout`: 待ち時間内に認証が終わっていない。認証を終えてから `sessionを1本作る（推奨）` を再実行する
- `login_input_error` / `username_or_password_rejected`: username/password違い、または入力欄の残骸の可能性がある。自動再試行はしない
- 認証コード、password、メール本文が映る場合は録画を一時停止する

### 4. 詐欺チェック

1. `詐欺チェック実行` を開く
2. Google Sheets URL を貼る
3. タブ名を入れる
4. `① まず件数を確認（本番はまだ走りません）` を押す
5. 画面に対象件数と必要session本数が出る
6. 件数が問題なければ `② 本番チェックを実行` を押す
7. 確認画面が出るので、件数を見てからOKを押す
8. 完了後、結果CSV欄に `logs/sagi_operator_result_...csv` が自動で入る
9. 書き戻し前に `③ 書き戻さずに件数だけ確認（安全）` を押す
10. 件数が問題なければ `④ シートのD列に反映（確定）` を押す

初回検証で大量チェックしたくない場合は、`② 本番チェックを実行` や `④ シートのD列に反映（確定）` は押さず、①の件数確認だけで止める。
CSVで実行する場合だけ `詳細設定 / CSVで実行する場合` を開く。

途中で止まった時:

- `NEEDS_SUPPLEMENT` が出て結果CSVがまだ無い: 先に強session補充で追加sessionを作り、同じシートで `① まず件数を確認（本番はまだ走りません）` を押し直す
- `50件上限` / `ローテーション先なし` / `LoginRequired` が出て結果CSVがある: 強session補充で追加sessionを作り、入力CSVと結果CSVを消さずに `途中から再開（追加session後）` を押す
- `途中から再開（追加session後）` は、結果CSVに入っているチェック済みアカウントをスキップして残りだけ進める
- 画面を閉じた場合は、右側の `直近CSV` から入力CSVと結果CSVを選んでから再開する

## Google Sheets接続について

Google Sheets取込、D列書き戻し、Slack通知には認証設定が必要。
認証情報は配布zipに入れない。

- CSVだけでdry-runする場合: 認証なしでも確認できる
- Google Sheets URLから取込する場合: 初回セットアップの `Google Sheets接続設定` が必要
- Apps Script連携済みの配布版: メンバーMacでGoogleログインは不要。対象シートを連携用Googleアカウントへ共有する
- Google API直接認証版: `Google Sheets接続設定` でログインしたGoogleアカウントに対象シートの編集権限が必要
- Slack通知する場合: 管理者が `config/members.json` に通知設定を入れる必要がある

`/Users/.../.config/google-api/credentials.json` が無いというエラーは、メンバーの操作ミスではない。
Google API直接認証版の管理者設定が入っていないという意味なので、原則としてApps Script連携済みの最新版DMGへ入れ替える。

権限不足で止まっても、チェック結果CSVは残る。
対象シートを共有してもらった後に、同じ画面で `③ 書き戻さずに件数だけ確認（安全）` から再開する。

ここが赤い場合、メンバー本人ではなく藤巻か管理者へ渡す。

## 赤くなった時の対応表

| 赤い項目 / 状態 | やること | 渡す相手 |
|---|---|---|
| `Apple Silicon` | Intel Macでは使わない | 藤巻 |
| `Python環境` | アプリを開き直す。直らなければログを渡す | 藤巻 / 管理者 |
| `Java` | `Android SDK` ボタンを押す。アプリ用Javaが自動導入される | メンバー本人 |
| `SDK Manager` | `Android SDK` ボタンを押す | メンバー本人 |
| `Android画面` | `③ Android画面作成` ボタンを押す | メンバー本人 |
| `Android画面` が赤で `Instagramアプリ` だけ緑 | 別のAndroid画面にInstagramが入っている可能性がある。`③ Android画面作成` → `④ 通信用設定` → `⑤ Instagram導入` の順にやり直す | メンバー本人 |
| `Instagramアプリ` | Instagram同梱済みの最新版DMGを受け取り直して、`Instagram導入` を押す | 管理者 |
| `通信の証明書` | `④ 通信用設定` を実行する | メンバー本人 |
| `シート連携設定` | Apps Script連携URLまたはGoogle API認証設定が入っていない。Google Sheets接続設定済みの最新版DMGへ入れ替える | 藤巻 / 管理者 |
| `Google Sheets接続` | Apps Script版ならログイン不要。直接認証版なら初回セットアップの `Google Sheets接続設定` を押し、ブラウザでGoogleログインする | メンバー本人 |
| 書き戻しで `Google Sheetsの権限` | Apps Script版なら対象シートを連携用Googleアカウントへ共有する。直接認証版ならログインしたGoogleアカウントに編集者権限を付ける。結果CSVは消さず、共有後に `③ 書き戻さずに件数だけ確認（安全）` から再開する | 藤巻 / 管理者 |
| ログに `credentials.json を配置してください` | Google API直接認証に必要な管理者設定が入っていない。メンバー本人では直せない。Apps Script連携済みの最新版DMGを配布する | 藤巻 / 管理者 |
| ログに `OAuth credentials not configured` | 古いgog連携画面を開いている。gogは使わないため、Google Sheets接続設定済みの最新版DMGへ入れ替える | 藤巻 / 管理者 |
| `通知設定` | Slack通知設定が未配置。dry-runのみなら無視してよい | 藤巻 / 管理者 |
| ログに `AVDからcapture proxyへの接続確認はできませんでした` | 確認コマンドだけ失敗している可能性がある。ジョブが次へ進むならそのまま待つ。Instagramログインで止まる場合だけログ末尾を渡す | メンバー本人 |
| ログに `Client TLS handshake failed` / `pinning` | 通信設定がずれている可能性がある。最新版では `④ 通信用設定` で自動同期されるので、`④ 通信用設定` → `sessionを1本作る（推奨）` の順に押し直す | メンバー本人 |
| ログに `nodename nor servname provided` / `502 Bad Gateway` | Mac側のネットワーク/DNSがInstagram接続先を解決できていない。iPhoneテザリング/Wi-Fiをつなぎ直してから、`④ 通信用設定` → `sessionを1本作る（推奨）` の順に押し直す | メンバー本人 |
| ログに `transport diagnosis: network_dns_or_502,tls_or_pinning` | ネットワーク不安定の後にTLS/pinningも失敗している。ログイン連打はせず、テザリングをつなぎ直し、`④ 通信用設定` からやり直す | メンバー本人 |
| ログに `Frida unpinning hooks not ready` | 通信補助設定が入る前に止まっている。`④ 通信用設定` を押し直してから `sessionを1本作る（推奨）` を再実行する | メンバー本人 |
| ログに `manual_login_mode` | 正常。Android画面のInstagramにusername/passwordを手入力してログインする。メール確認/2FAもAndroid画面内で完了する | メンバー本人 |
| ログに `manual_login_required` / `challenge_or_2fa:check your email` / `two_step_verification` | Instagramがメール確認または2FAを要求している。録画を止め、Android画面で認証を完了する。完了後はそのまま待つ | メンバー本人 |
| ログに `manual_login_timeout` | 手動認証が時間内に終わらなかった。Android画面で認証を終えてから `sessionを1本作る（推奨）` を再実行する | メンバー本人 |
| ログに `login_input_error` / `username_or_password_rejected` | username/password違い、または入力欄に前の文字が残っている可能性がある。自動再試行は止まっているので、入力内容を確認してからやり直す | メンバー本人 |
| ログに `FileNotFoundError` と `config/accounts.json` | 古い版では初回Macで出る。最新版ではcapture取込時にローカル設定を自動作成するので、最新版DMGへ入れ替えて `取り込みだけやり直す` または `sessionを1本作る（推奨）` を再実行する | メンバー本人 |
| Instagram画面で `Unable to log in` / `unexpected error occurred` | ログイン連打を止める。`OK` を押し、手でInstagramを開かずに、Operatorで `④ 通信用設定` → `強session補充` の `sessionを1本作る（推奨）` を押す。繰り返す場合は別アカウントかテザリング回線へ切り替える | 藤巻 / 分かる人 |
| ログに `feed not reached` | Instagramログインが完了していない。Android画面を見る。ログイン画面のままならusername/password確認、認証コード画面なら録画を止めて手動対応、`Unable to log in` なら連打せず別アカウントかテザリング回線へ切り替える | 藤巻 / 分かる人 |
| ログに `NEEDS_SUPPLEMENT` | 対象件数に対して強sessionが足りない。強session補充で不足分以上の追加sessionを作る。結果CSVがまだ無い場合は `① まず件数を確認（本番はまだ走りません）` を押し直す | メンバー本人 |
| ログに `50件上限` / `ローテーション先なし` | 途中までチェック済み。強session補充で追加sessionを作り、入力CSVと結果CSVを消さずに `途中から再開（追加session後）` を押す | メンバー本人 |
| ログに `LoginRequired` / `ChallengeRequired` / `AUTH ERROR` | 使用中の強sessionが使えなくなった。自動再ログインはしない。別のチェック用アカウントで追加sessionを作り、結果CSVがある場合は `途中から再開（追加session後）` を押す | メンバー本人 |
| `needs_supplement` | 画面の次の一手に従い、強session補充で追加sessionを作る。結果CSVがある時は `途中から再開（追加session後）` | メンバー本人 |
| `manual_needed` | Android画面のchallenge / 2FAを確認 | 藤巻 / 分かる人 |
| `failed` | 画面ログ末尾を共有する | 藤巻 / 管理者 |

## 録画チェックリスト

- [ ] アプリを開くところから録画開始
- [ ] 初回セットアップ画面を映す
- [ ] 赤い項目が緑になる流れを映す
- [ ] password入力前に録画停止、または後でぼかす
- [ ] 2FA / 認証コードは映さない
- [ ] `作ったsessionを確認` 成功を映す
- [ ] `① まず件数を確認（本番はまだ走りません）` 成功を映す
- [ ] 最後に「本番大量チェックはまだやらない」と説明する

## 検証結果の残し方

検証後、次を検証ログへ残す。

- 実施日
- Mac種別
- 成功したところ
- 止まったところ
- 画面に出た赤い項目
- 録画ファイルの保存場所
- 本番運用前に直すこと
