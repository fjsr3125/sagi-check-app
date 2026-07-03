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
- 最新DMG: https://github.com/fjsr3125/sagi-check-app/releases/latest/download/UnariSagiOperator-2026.07.03.1.dmg
- 更新確認: https://github.com/fjsr3125/sagi-check-app/releases/latest/download/latest.json

## ローカル確認

```bash
make ci
```

## member-ready release

ローカルで必要ファイルを用意してから実行する。

```bash
make sagi-operator-release-package BASE_URL=https://example.com/unari-sagi-operator VERSION=2026.07.03.1
```

生成物:

- `dist/sagi_operator_release/UnariSagiOperator-<version>.dmg`
- `dist/sagi_operator_release/UnariSagiOperator-<version>.zip`
- `dist/sagi_operator_release/latest.json`

`BASE_URL` を指定すると、`latest.json` 内のDMG/ZIP URLも同じURL配下で生成される。

## GitHub Actions

通常のpushでは `.github/workflows/ci.yml` が走り、Python構文と最低限のimportだけ確認する。

メンバー配布版を作る時はGitHubのActions画面から `Build member release` を手動実行する。

- `version`: `2026.07.03.1` のように指定
- `base_url`: 空でも可。空の場合は `https://github.com/<owner>/<repo>/releases/latest/download` を使う
- `release_notes`: 更新内容を短く書く

workflowはDMG/ZIP/latest.jsonをGitHub Releaseへアップロードする。アプリ内の更新確認は `latest.json` を見て「最新版あり/なし」を表示する。

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
