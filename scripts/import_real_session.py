#!/usr/bin/env python3
"""
captures/{ds_user_id}.json (mitmproxy addon の成果物) を instagrapi 互換の
sessions/{username}.json に変換し、config/accounts.json にエントリを upsert する。

使い方:
    python3 scripts/import_real_session.py --capture captures/36671075508.json
    python3 scripts/import_real_session.py --all
    python3 scripts/import_real_session.py --capture captures/XXX.json --dry-run
    python3 scripts/import_real_session.py --capture captures/XXX.json --username so__r__a_05

成功すれば verify_captured_session.py で同じプローブが通るはず。
"""
import argparse
import json
import re
import shutil
import sys
import time
import uuid as uuid_mod
from datetime import datetime
from pathlib import Path

from instagrapi import Client
from instagrapi.exceptions import ChallengeRequired, LoginRequired

BASE_DIR = Path(__file__).resolve().parent.parent
CONFIG_PATH = BASE_DIR / "config" / "accounts.json"
SESSIONS_DIR = BASE_DIR / "sessions"
CAPTURES_DIR = BASE_DIR / "captures"
FAILED_DIR = CAPTURES_DIR / "failed"

DEFAULT_CONFIG = {
    "batch_per_account": 50,
    "sessions_dir": "sessions",
    "accounts": [],
}

UUID_KEYS = (
    "phone_id", "uuid", "client_session_id", "advertising_id",
    "android_device_id", "request_id", "tray_session_id",
)

DEVICE_KEYS = (
    "android_version", "android_release", "dpi", "resolution",
    "manufacturer", "device", "model", "cpu",
    "app_version", "version_code", "bloks_versioning_id",
)

# Instagram 385.0.0.47.74 Android (33/13.0.0; 480dpi; 1080x2340; Samsung; Galaxy S22; o1s; exynos; ja_JP; 378906843)
UA_PATTERN = re.compile(
    r"Instagram\s+([\d.]+)\s+Android\s+\("
    r"(\d+)/([\d.]+);\s*"
    r"(\d+dpi);\s*"
    r"(\d+x\d+);\s*"
    r"([^;]+);\s*"
    r"([^;]+);\s*"
    r"([^;]+);\s*"
    r"([^;]+);\s*"
    r"([a-zA-Z_]+);\s*"
    r"(\d+)\)"
)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _gen_android_device_id() -> str:
    return "android-" + uuid_mod.uuid4().hex[:16]


def _gen_uuid() -> str:
    return str(uuid_mod.uuid4())


def parse_user_agent(ua: str | None) -> dict:
    """IG の User-Agent から device_settings をできるだけ埋める。"""
    if not ua:
        return {}
    m = UA_PATTERN.search(ua)
    if not m:
        return {}
    (
        app_version, android_version, android_release, dpi, resolution,
        manufacturer, model, device, cpu, locale, version_code,
    ) = m.groups()
    return {
        "app_version": app_version,
        "android_version": int(android_version),
        "android_release": android_release,
        "dpi": dpi,
        "resolution": resolution,
        "manufacturer": manufacturer.strip(),
        "model": model.strip(),
        "device": device.strip(),
        "cpu": cpu.strip(),
        "version_code": version_code,
        "_parsed_locale": locale,  # 使う側で拾う
    }


def build_instagrapi_session(
    capture: dict,
    existing_uuids: dict | None,
    existing_device: dict | None,
) -> dict:
    """capture JSON を instagrapi の session dict に変換する。

    existing_uuids / existing_device は accounts.json に既にエントリがあれば渡す。
    無いフィールドは生成 or UA から推定する。
    """
    ua = capture.get("user_agent") or ""
    ua_parsed = parse_user_agent(ua)

    # --- uuids ---
    existing_uuids = existing_uuids or {}
    uuids = {
        "phone_id": capture.get("x_ig_family_device_id")
            or existing_uuids.get("phone_id")
            or _gen_uuid(),
        "uuid": capture.get("x_ig_device_id")
            or existing_uuids.get("uuid")
            or _gen_uuid(),
        "android_device_id": capture.get("x_ig_android_id")
            or existing_uuids.get("android_device_id")
            or _gen_android_device_id(),
        "client_session_id": existing_uuids.get("client_session_id") or _gen_uuid(),
        "advertising_id": existing_uuids.get("advertising_id") or _gen_uuid(),
        "request_id": existing_uuids.get("request_id") or _gen_uuid(),
        "tray_session_id": existing_uuids.get("tray_session_id") or _gen_uuid(),
    }

    # --- device_settings ---
    device_settings: dict = {}
    if existing_device:
        for k in DEVICE_KEYS:
            if k in existing_device:
                device_settings[k] = existing_device[k]
    # UA から抽出できた値で上書き（実機の情報が正）
    for k in DEVICE_KEYS:
        if k in ua_parsed and ua_parsed[k]:
            device_settings[k] = ua_parsed[k]
    # bloks versioning は UA に無いので header から
    if capture.get("x_bloks_version_id"):
        device_settings["bloks_versioning_id"] = capture["x_bloks_version_id"]

    # --- locale / timezone ---
    locale = ua_parsed.get("_parsed_locale") or "ja_JP"
    country_code_map = {"ja_JP": ("JP", 81), "en_US": ("US", 1)}
    country, phone_cc = country_code_map.get(locale, ("JP", 81))
    # JSTデフォルト（日本アカウントメイン想定、運用で問題なら個別に直す）
    timezone_offset = 32400 if locale == "ja_JP" else -14400

    session = {
        "uuids": uuids,
        "mid": capture.get("mid"),
        "ig_u_rur": capture.get("rur"),
        "ig_www_claim": capture.get("x_ig_www_claim"),
        "authorization_data": {
            "ds_user_id": str(capture["ds_user_id"]),
            "sessionid": capture["sessionid"],
            # csrftoken は sessionid 側に含まれているが、header でも送られることがある
            "should_use_header_over_cookies": True,
        },
        "cookies": {},
        "last_login": time.time(),
        "device_settings": device_settings,
        "user_agent": ua,
        "country": country,
        "country_code": phone_cc,
        "locale": locale,
        "timezone_offset": timezone_offset,
        # 実機mitmproxy由来の「強session」マーカー
        # api_warning_check.py はこのフラグを見て dump_settings をスキップし、
        # login ログインで上書きされるのを防ぐ。
        "source": "mitmproxy",
        "imported_at": datetime.now().isoformat(),
        "captured_at": capture.get("captured_at"),
    }
    return session


def load_capture(path: Path) -> dict:
    with open(path) as f:
        return json.load(f)


def load_config() -> dict:
    if not CONFIG_PATH.exists():
        print("  accounts.json が無いため、このMac用の最小設定を自動作成します")
        return json.loads(json.dumps(DEFAULT_CONFIG))
    with open(CONFIG_PATH) as f:
        cfg = json.load(f)
    cfg.setdefault("batch_per_account", DEFAULT_CONFIG["batch_per_account"])
    cfg.setdefault("sessions_dir", DEFAULT_CONFIG["sessions_dir"])
    cfg.setdefault("accounts", [])
    return cfg


def save_config(cfg: dict) -> None:
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    if CONFIG_PATH.exists():
        backup = CONFIG_PATH.with_suffix(f".json.bak_{ts}")
        shutil.copy2(CONFIG_PATH, backup)
        print(f"  accounts.json backup: {backup.name}")
    else:
        print("  accounts.json create: password/proxyなしのローカル設定を作成")
    with open(CONFIG_PATH, "w") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)


def find_account(cfg: dict, username: str) -> dict | None:
    for acc in cfg.get("accounts", []):
        if acc["username"] == username:
            return acc
    return None


def find_account_by_uuid(cfg: dict, phone_id: str | None) -> dict | None:
    if not phone_id:
        return None
    for acc in cfg.get("accounts", []):
        if (acc.get("uuids") or {}).get("phone_id") == phone_id:
            return acc
    return None


def resolve_username(session_dict: dict, proxy: str) -> str | None:
    """キャプチャしたセッションで API を叩き、自分の username を取得する。
    同時にセッションの健全性チェックを兼ねる。
    """
    cl = Client()
    cl.delay_range = [4, 6]
    cl.set_settings(session_dict)
    if proxy:
        cl.set_proxy(proxy)
    ds_user_id = session_dict["authorization_data"]["ds_user_id"]
    try:
        info = cl.user_info(int(ds_user_id))
        return info.username
    except (ChallengeRequired, LoginRequired) as e:
        print(f"  ✗ session invalid: {type(e).__name__}: {str(e)[:120]}")
        return None
    except Exception as e:
        print(f"  ✗ resolve_username failed: {type(e).__name__}: {str(e)[:120]}")
        return None


def write_session(username: str, session_dict: dict) -> Path:
    SESSIONS_DIR.mkdir(exist_ok=True)
    dest = SESSIONS_DIR / f"{username}.json"
    if dest.exists():
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup = dest.with_suffix(f".json.bak_{ts}")
        shutil.copy2(dest, backup)
        print(f"  session backup: {backup.name}")
    with open(dest, "w") as f:
        json.dump(session_dict, f, indent=4, ensure_ascii=False)
    return dest


def upsert_account_entry(
    cfg: dict,
    username: str,
    uuids: dict,
    device: dict,
) -> tuple[bool, str]:
    """accounts.json にエントリを upsert する。戻り値: (変更あり, status文字列)"""
    acc = find_account(cfg, username)
    if acc:
        before_uuids = acc.get("uuids", {})
        before_device = acc.get("device", {})
        # uuids は実機由来優先で上書き、不足分のみ既存から補完（build側でやってる想定）
        acc["uuids"] = uuids
        # device はキーマージ（proxy は保持）
        merged_device = dict(before_device)
        for k, v in device.items():
            if v is not None:
                merged_device[k] = v
        acc["device"] = merged_device
        changed = (before_uuids != uuids) or (before_device != merged_device)
        return changed, "updated" if changed else "no-change"

    # 新規エントリ
    cfg.setdefault("batch_per_account", DEFAULT_CONFIG["batch_per_account"])
    cfg.setdefault("sessions_dir", DEFAULT_CONFIG["sessions_dir"])
    cfg.setdefault("accounts", [])
    cfg["accounts"].append({
        "username": username,
        "proxy": "",
        "device": dict(device),
        "uuids": dict(uuids),
    })
    return True, "inserted (proxy/passwordなし)"


def move_failed(capture_path: Path) -> None:
    FAILED_DIR.mkdir(parents=True, exist_ok=True)
    dest = FAILED_DIR / capture_path.name
    shutil.move(str(capture_path), str(dest))
    print(f"  moved to {dest.relative_to(BASE_DIR)}")


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def process_one(
    capture_path: Path,
    cfg: dict,
    *,
    dry_run: bool,
    no_verify: bool,
    username_override: str | None,
) -> bool:
    print(f"\n=== {capture_path.name} ===")
    capture = load_capture(capture_path)
    ds_user_id = str(capture.get("ds_user_id", ""))
    if not ds_user_id or not capture.get("sessionid"):
        print("  ✗ invalid capture (missing ds_user_id or sessionid)")
        return False

    # username の解決順: CLI override > phone_id から逆引き > API で resolve
    username: str | None = username_override
    acc: dict | None = None

    if not username:
        acc = find_account_by_uuid(cfg, capture.get("x_ig_family_device_id"))
        if acc:
            username = acc["username"]
            print(f"  phone_id 一致で username 解決: {username}")
    if username and not acc:
        acc = find_account(cfg, username)

    existing_uuids = (acc or {}).get("uuids")
    existing_device = (acc or {}).get("device")

    session_dict = build_instagrapi_session(capture, existing_uuids, existing_device)

    if not no_verify:
        proxy = (acc or {}).get("proxy", "") if acc else ""
        print(f"  resolve/probe via API (proxy={'yes' if proxy else 'no'})...")
        resolved = resolve_username(session_dict, proxy)
        if not resolved:
            if not dry_run:
                move_failed(capture_path)
            return False
        if username and username != resolved:
            print(f"  ✗ username mismatch: override={username} resolved={resolved} — 拒否")
            return False
        username = resolved

    if not username:
        print("  ✗ username を特定できません (--username か --no-verify 時の --username が必要)")
        return False

    # 既存の別アカウントと ds_user_id が衝突していないか確認
    existing_session = SESSIONS_DIR / f"{username}.json"
    if existing_session.exists():
        try:
            existing = json.load(open(existing_session))
            existing_dsid = existing.get("authorization_data", {}).get("ds_user_id")
            if existing_dsid and str(existing_dsid) != ds_user_id:
                print(
                    f"  ✗ conflict: {username} の既存セッションは "
                    f"ds_user_id={existing_dsid}, capture は {ds_user_id} — 拒否"
                )
                return False
        except Exception:
            pass

    if dry_run:
        print(f"  [dry-run] would write sessions/{username}.json")
        print(f"  [dry-run] device: {session_dict['device_settings']}")
        print(f"  [dry-run] uuids.phone_id: {session_dict['uuids']['phone_id']}")
        return True

    # 書き込み
    write_session(username, session_dict)
    changed, status = upsert_account_entry(
        cfg,
        username=username,
        uuids=session_dict["uuids"],
        device=session_dict["device_settings"],
    )
    print(f"  accounts.json: {status}")
    print(f"  ✓ sessions/{username}.json 書き込み完了")
    # 成功したら capture を保持（再実行での誤作動防止は mitmproxy addon 側の seen で担保）
    return True


def main() -> int:
    p = argparse.ArgumentParser()
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("--capture", help="単一 capture JSON を import")
    src.add_argument("--all", action="store_true", help="captures/*.json を一括 import")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--no-verify", action="store_true",
                   help="API プローブをスキップ（--username 併用必須）")
    p.add_argument("--username", help="username を手動指定（API resolve を上書き）")
    args = p.parse_args()

    if args.no_verify and not args.username and not args.all:
        print("--no-verify は --username と併用してください", file=sys.stderr)
        return 2

    if args.all:
        capture_paths = sorted(CAPTURES_DIR.glob("*.json"))
        if not capture_paths:
            print(f"{CAPTURES_DIR} に capture がありません")
            return 0
    else:
        capture_paths = [Path(args.capture)]

    cfg = load_config()
    ok_count = 0
    fail_count = 0
    for path in capture_paths:
        try:
            ok = process_one(
                path, cfg,
                dry_run=args.dry_run,
                no_verify=args.no_verify,
                username_override=args.username,
            )
        except Exception as e:
            print(f"  ✗ unexpected: {type(e).__name__}: {e}")
            ok = False
        if ok:
            ok_count += 1
        else:
            fail_count += 1

    # accounts.json 保存（1回だけ）
    if not args.dry_run and ok_count > 0:
        save_config(cfg)

    print(f"\n{'='*40}")
    print(f"result: {ok_count} ok / {fail_count} fail")
    return 0 if fail_count == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
