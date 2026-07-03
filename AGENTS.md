# Codex Notes

- このrepoは `Unari Sagi Operator.app` / 詐欺チェックアプリ配布専用。
- `config/*.json`, `sessions/`, `captures/`, `logs/`, `apks/`, `tools/` の実体は秘密情報または大容量ローカル資産として扱い、gitに入れない。
- 変更時は `make ci` を通す。
- メンバー配布用DMG/ZIPを作る前は `make sagi-operator-release-package` を使い、生成された `latest.json` とSHA256を確認する。
- `unari` 本体repoの営業資料、HubSpot分析、PDCA資料はこのrepoへ混ぜない。
