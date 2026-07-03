#!/usr/bin/env python3
"""
sessions/{username}.json が生きているか、api_warning_check.py と同じプローブで検証する。

使い方:
    python3 scripts/verify_captured_session.py --username so__r__a_05
    python3 scripts/verify_captured_session.py --all
    python3 scripts/verify_captured_session.py --all --no-proxy

exit 0 = 全 OK / 1 = 1 件でも NG
"""
import argparse
import json
import sys
from pathlib import Path

from instagrapi import Client

BASE_DIR = Path(__file__).resolve().parent.parent
CONFIG_PATH = BASE_DIR / "config" / "accounts.json"
SESSIONS_DIR = BASE_DIR / "sessions"

DEFAULT_CONFIG = {
    "batch_per_account": 50,
    "sessions_dir": "sessions",
    "accounts": [],
}


def load_config() -> dict:
    if not CONFIG_PATH.exists():
        return dict(DEFAULT_CONFIG)
    with open(CONFIG_PATH) as f:
        cfg = json.load(f)
    cfg.setdefault("accounts", [])
    return cfg


def find_account(cfg: dict, username: str) -> dict | None:
    for acc in cfg.get("accounts", []):
        if acc["username"] == username:
            return acc
    return None


def _session_account(username: str) -> dict:
    return {"username": username, "proxy": ""}


def _accounts_from_sessions() -> list[dict]:
    if not SESSIONS_DIR.exists():
        return []
    accounts: list[dict] = []
    for path in sorted(SESSIONS_DIR.glob("*.json")):
        try:
            data = json.load(open(path))
        except Exception:
            continue
        if data.get("source") != "mitmproxy":
            continue
        accounts.append(_session_account(path.stem))
    return accounts


def probe(username: str, proxy: str | None) -> tuple[bool, str]:
    session_path = SESSIONS_DIR / f"{username}.json"
    if not session_path.exists():
        return False, "session file missing"

    cl = Client()
    cl.delay_range = [4, 6]
    try:
        cl.load_settings(str(session_path))
        if proxy:
            cl.set_proxy(proxy)
        # api_warning_check.py と同じ「他人参照」プローブ
        cl.user_info_by_username("instagram")
        return True, "probe OK"
    except Exception as e:
        return False, f"{type(e).__name__}: {str(e)[:200]}"


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--username", help="単一アカウントのみ検証")
    p.add_argument("--all", action="store_true", help="accounts.json 全件")
    p.add_argument("--no-proxy", action="store_true", help="プロキシを使わない")
    args = p.parse_args()

    if not args.username and not args.all:
        print("--username か --all を指定してください", file=sys.stderr)
        return 2

    cfg = load_config()

    if args.username:
        acc = find_account(cfg, args.username)
        if not acc:
            acc = _session_account(args.username)
        targets = [acc]
    else:
        # sessions/ にファイルが存在するアカウントのみ
        targets = [
            a for a in cfg.get("accounts", [])
            if (SESSIONS_DIR / f"{a['username']}.json").exists()
        ]
        if not targets:
            targets = _accounts_from_sessions()

    print("username,ok,reason")
    all_ok = True
    for acc in targets:
        username = acc["username"]
        proxy = "" if args.no_proxy else acc.get("proxy", "")
        ok, reason = probe(username, proxy)
        if not ok:
            all_ok = False
        print(f"{username},{'OK' if ok else 'NG'},{reason}")

    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
