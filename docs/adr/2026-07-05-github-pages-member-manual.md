# 2026-07-05 GitHub Pagesでメンバー向けマニュアルを公開する

- Status: Accepted
- Date: 2026-07-05T13:35:03+09:00

## Context

Sagi Operatorの既存資料には録画用カンペと詳細なメンバーMac手順書がある。
ただし、実際に使うメンバーが開くページとしては説明量が多く、公開URLも用意されていない。

## Decision

`docs/sagi_operator_member_manual.html` をメンバー向けの主マニュアルとして作成し、GitHub Pagesで `docs/` 配下だけを公開する。
`docs/index.html` は主マニュアルへリダイレクトする入口にする。
公開は `.github/workflows/pages.yml` のGitHub Actions workflowで行う。

## Consequences

- メンバーはブラウザでURLを開くだけで手順を確認できる。
- 録画用カンペは参照元として残し、閲覧用マニュアルとは分けて管理できる。
- 公開対象は `docs/` 配下になるため、秘密情報やローカル資産を置かない運用を守る必要がある。
