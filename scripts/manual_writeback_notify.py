#!/usr/bin/env python3
"""手動実行した詐欺チェック結果を、対象シートD列に書き戻してSlack通知する。

sagi_request_processor.py から writeback_results / notify_slack / extract_accounts_from_tab
を流用する単発用スクリプト。
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

from sagi_request_processor import (
    writeback_results,
    notify_slack,
    find_slack_user_id,
    extract_accounts_from_tab,
    load_members,
)

MEMBERS_JSON = PROJECT_ROOT / "config" / "members.json"


def classify_result(row: dict) -> str:
    err = (row.get("error") or "").strip()
    flag = row.get("has_scam_flag")
    harm_type = (row.get("harm_type") or "").strip().upper()
    if flag == "TRUE":
        return "SCAM"
    if harm_type.startswith("SCAM"):
        return "SCAM"
    if err == "user_not_found":
        return "NOT_FOUND"
    if flag == "FALSE":
        return "CLEAN"
    return "ERROR"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--result-csv", required=True, help="api_warning_check.py の出力CSV")
    ap.add_argument("--sheet-id", required=True, help="書き戻し先シートID")
    ap.add_argument("--tab-name", required=True, help="対象タブ名 (例: 4_23)")
    ap.add_argument("--requester", default="", help="Slack通知先のメンバー名 (members.json)。空なら通知だけスキップ")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    requester = args.requester.strip()
    members_cfg = load_members()
    webhook = members_cfg.get("slack_webhook_url", "")
    slack_uid = find_slack_user_id(members_cfg, requester) if requester and not members_cfg.get("_missing") else None
    if members_cfg.get("_missing"):
        print("Slack通知設定がないため、最後のSlack通知だけスキップします。シート書き戻しは続行します。")
    elif not requester:
        print("Slack通知先の登録名が空のため、最後のSlack通知だけスキップします。シート書き戻しは続行します。")
    elif not slack_uid:
        print(f"⚠ Slack user_id が members.json に見つからない: {requester}")

    # 1) 結果CSV読み込み
    results: dict[str, dict] = {}
    total = 0
    scam = 0
    with open(args.result_csv) as f:
        for row in csv.DictReader(f):
            total += 1
            status = classify_result(row)
            if status == "SCAM":
                scam += 1
            results[row["account_id"]] = {"status": status}

    # 2) row_map 取得 (対象シートのタブからアカウントID→行番号を引く)
    print(f"対象: {args.sheet_id} {args.tab_name}")
    _accounts, row_map, skipped = extract_accounts_from_tab(args.sheet_id, args.tab_name)
    print(f"row_map: {len(row_map)} accounts, skipped(既に判定済) {skipped} 件")

    # row_mapに居ないアカウントは書き戻せない
    missing = [a for a in results if a not in row_map]
    if missing:
        print(f"⚠ シート側row未発見 {len(missing)}件 (既に詐欺表示済マークされた等): {missing[:5]}...")

    if args.dry_run:
        by_status = {}
        for aid, info in results.items():
            by_status.setdefault(info["status"], []).append(aid)
        for s, accs in by_status.items():
            writeable = [a for a in accs if a in row_map]
            print(f"[dry-run] {s}: 全{len(accs)}件 書戻対象{len(writeable)}件")
        return

    # 3) 書き戻し実行
    wb = writeback_results(args.sheet_id, args.tab_name, results, row_map)
    summary = (
        f"SCAM {scam}/{total}件 "
        f"(書戻: 詐欺{wb['scam']}/不明{wb['not_found']}/なし{wb['clean']}, "
        f"エラー{wb['error']}件, シート未発見{wb['no_row']}件)"
    )
    print(f"✓ {summary}")

    # 4) Slack通知
    if webhook and slack_uid:
        text = f"✅ 詐欺チェック完了（手動実行）: {args.tab_name} → {summary}"
        notify_slack(webhook, slack_uid, text)
        print(f"✓ Slack通知送信 → {requester} ({slack_uid})")
    else:
        print("Slack通知は未設定のためスキップしました。")


if __name__ == "__main__":
    main()
