#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

from sagi_request_processor import extract_accounts_from_tab, extract_sheet_id


def write_accounts(accounts: list[str], output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["account_id"])
        writer.writeheader()
        for account in accounts:
            writer.writerow({"account_id": account})


def load_csv_accounts(path: Path) -> list[str]:
    with path.open() as f:
        reader = csv.DictReader(f)
        if not reader.fieldnames or "account_id" not in reader.fieldnames:
            raise ValueError("CSVには account_id 列が必要です")
        accounts = []
        seen = set()
        for row in reader:
            account = (row.get("account_id") or "").strip()
            if account and account not in seen:
                accounts.append(account)
                seen.add(account)
    return accounts


def main() -> int:
    parser = argparse.ArgumentParser()
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--sheet-url")
    source.add_argument("--sheet-id")
    source.add_argument("--csv-path")
    parser.add_argument("--tab-name", help="Google Sheetsの対象タブ名")
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    output = Path(args.output)
    if args.csv_path:
        accounts = load_csv_accounts(Path(args.csv_path))
        write_accounts(accounts, output)
        print(f"CSV取込: {len(accounts)}件 -> {output}")
        return 0

    sheet_id = args.sheet_id or extract_sheet_id(args.sheet_url or "")
    if not sheet_id:
        print("Google Sheets URL/IDを特定できません", file=sys.stderr)
        return 2
    if not args.tab_name:
        print("tab-name を指定してください", file=sys.stderr)
        return 2

    accounts, _row_map, skipped = extract_accounts_from_tab(sheet_id, args.tab_name)
    if not accounts:
        print("対象アカウントが0件です。タブ名とA列を確認してください。", file=sys.stderr)
        return 2
    write_accounts(accounts, output)
    print(f"シート取込: {sheet_id} / {args.tab_name} -> {len(accounts)}件 (重複skip {skipped}) -> {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
