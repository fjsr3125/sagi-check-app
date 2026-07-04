# sagi-check-app

Unari Sagi Operator / 詐欺チェック用Macアプリの配布専用repo。

このrepoは公開運用。秘密設定、session、capture、Instagram APK/APKM/XAPK、Frida tools本体はgitに入れない。

## 方針

- `unari` 本体repoから、詐欺チェックアプリ配布に必要なコードだけを分離する
- CIはGitHub Actionsで回す
- member-ready releaseは手動workflowで作る
- 実session、capture、ログ、Instagram APK/APKM/XAPK、Frida tools、`members.json`、Sheets連携設定はgitに入れない

## 公開配布URL

- repo: https://github.com/fjsr3125/sagi-check-app
- 最新Release: https://github.com/fjsr3125/sagi-check-app/releases/latest
- 最新情報: https://github.com/fjsr3125/sagi-check-app/releases/latest/download/latest.json

DMGのファイル名にはバージョン番号が入るため、READMEには固定のDMG URLを書かない。
メンバーへ案内する時は「最新Release」からDMGを取得するか、`latest.json` の `download_url` を見る。

公開配布の正はGitHub Releaseと `latest.json`。
ローカルの `dist/` は作業用の生成物で、GitHub Actionsで新しいReleaseを作っても自動では更新されない。

## ローカル確認

```bash
make ci
```

秘密設定や配布用toolsが手元に無い状態で広めに確認する場合:

```bash
make sagi-operator-local-smoke PYTHON=venv/bin/python
```

`members.json` や Sheets bridge 設定も含めた member-ready 配布前チェックは次を使う。
このチェックは秘密設定が無い場合に失敗するのが正常。

```bash
make sagi-operator-smoke PYTHON=venv/bin/python
```

公開済みReleaseが本当に最新版を返しているか確認する場合:

```bash
make sagi-operator-published-smoke PYTHON=venv/bin/python VERSION=YYYY.MM.DD.N BUILD=<commit>
```

## member-ready release

ローカルで必要ファイルを用意してから実行する。

```bash
make sagi-operator-release-package BASE_URL=https://example.com/unari-sagi-operator VERSION=YYYY.MM.DD.N
```

生成物:

- `dist/sagi_operator_release/UnariSagiOperator-<version>.dmg`
- `dist/sagi_operator_release/UnariSagiOperator-<version>.zip`
- `dist/sagi_operator_release/latest.json`

`BASE_URL` を指定すると、`latest.json` 内のDMG/ZIP URLも同じURL配下で生成される。

## GitHub Actions

通常のpushでは `.github/workflows/ci.yml` が走り、Python構文と最低限のimportだけ確認する。

メンバー配布版を作る時はGitHubのActions画面から `Build member release` を手動実行する。

- `version`: `2026.07.04.4` のように指定
- `base_url`: 空でも可。空の場合は `https://github.com/<owner>/<repo>/releases/latest/download` を使う
- `release_notes`: 更新内容を短く書く

workflowはDMG/ZIP/latest.jsonをGitHub Releaseへアップロードする。アプリ内の更新確認は `latest.json` を見て「最新版あり/なし」を表示する。
workflowはアップロード後に公開URLの `latest.json` を読み直し、今回の `version` / `build` / DMG・ZIP URL と一致することも確認する。
workflow完了後はGitHub Actions Summaryに、今回公開したバージョン、DMG/ZIP URL、SHA256、`latest.json` の中身が出る。

通常pushとメンバー配布は別。
push後のCI成功は「コード確認済み」で、`Build member release` 成功後が「メンバー配布済み」。

## GitHub Actions secrets

release workflowには次が必要。

- `SAGI_MEMBERS_JSON_B64`: `config/members.json` のbase64
- `SAGI_SHEETS_BRIDGE_JSON_B64`: `config/sagi_sheets_bridge.json` のbase64
- `SAGI_OPERATOR_INSTAGRAM_PACKAGE_URL`: Instagram APK/APKM/XAPKを取得できるURL
- `SAGI_OPERATOR_CAPTURE_TOOLS_URL`: `tools/` に展開するtar.gzを取得できるURL

`SAGI_MEMBERS_JSON_B64` と `SAGI_SHEETS_BRIDGE_JSON_B64` は設定済み。

未設定:

- `SAGI_OPERATOR_INSTAGRAM_PACKAGE_URL`
- `SAGI_OPERATOR_CAPTURE_TOOLS_URL`

この2つを設定すると、GitHub Actionsの `Build member release` から次回版を作れる。
