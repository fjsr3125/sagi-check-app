# sagi-check-app

Unari Sagi Operator / 詐欺チェック用Macアプリの配布専用repo。

## 方針

- `unari` 本体repoから、詐欺チェックアプリ配布に必要なコードだけを分離する
- CIはGitHub Actionsで回す
- member-ready releaseは手動workflowで作る
- 実session、capture、ログ、Instagram APK/APKM/XAPK、Frida tools、`members.json`、Sheets連携設定はgitに入れない

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

Private GitHub Releasesだけに置くと、メンバーMacが認証なしで取得できない場合がある。
非エンジニア配布では、Cloudflare R2などの固定URLにDMG/ZIP/latest.jsonを置く方が安全。
