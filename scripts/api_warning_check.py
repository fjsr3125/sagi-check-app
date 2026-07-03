#!/usr/bin/env python3
"""
Instagram Private APIで account_warning フィールドを一括取得する

v3: AUTH ERROR時に同じターゲットを別アカウントで再試行
    + 2FA/Challenge自動スキップ（input()ハング防止）

使い方:
  python3 scripts/api_warning_check.py --input logs/merged_appium_results.csv
  python3 scripts/api_warning_check.py --input logs/merged_appium_results.csv --resume
  python3 scripts/api_warning_check.py --input logs/merged_appium_results.csv --dry-run
"""
import argparse
import csv
import json
import random
import signal
import sys
import time
import threading
from datetime import datetime
from pathlib import Path

from instagrapi import Client
from instagrapi.exceptions import (
    ChallengeRequired,
    ChallengeUnknownStep,
    ClientNotFoundError,
    LoginRequired,
    PleaseWaitFewMinutes,
    UserNotFound,
)

# ---------------------------------------------------------------------------
# パス
# ---------------------------------------------------------------------------
BASE_DIR = Path(__file__).parent.parent
SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))
LOGS_DIR = BASE_DIR / "logs"
CONFIG_PATH = BASE_DIR / "config" / "accounts.json"

DEFAULT_CONFIG = {
    "password": "",
    "batch_per_account": 50,
    "sessions_dir": "sessions",
    "accounts": [],
}


def load_config():
    if not CONFIG_PATH.exists():
        return dict(DEFAULT_CONFIG)
    with open(CONFIG_PATH) as f:
        cfg = json.load(f)
    for key, value in DEFAULT_CONFIG.items():
        cfg.setdefault(key, value)
    return cfg


def _accounts_from_strong_sessions(sessions_dir: Path) -> list[dict]:
    if not sessions_dir.exists():
        return []
    accounts: list[dict] = []
    for session_path in sorted(sessions_dir.glob("*.json")):
        if not is_strong_session(session_path):
            continue
        try:
            session = json.load(open(session_path))
        except Exception:
            session = {}
        accounts.append({
            "username": session_path.stem,
            "proxy": "",
            "device": session.get("device_settings") or {},
            "uuids": session.get("uuids") or {},
        })
    return accounts


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--input", required=True, help="入力CSV (account_idカラム必須)")
    p.add_argument("--resume", action="store_true", help="途中再開")
    p.add_argument("--output", help="出力CSV (省略時: 自動生成)")
    p.add_argument("--dry-run", action="store_true", help="セッション復元テストのみ")
    p.add_argument("--no-proxy", action="store_true", help="プロキシを使わない")
    p.add_argument("--start-account", help="このアカウント名からチェック開始（先頭スキップ）")
    p.add_argument(
        "--only-account",
        help="指定した強sessionだけを使う。ローテーションで他アカウントに逃げない",
    )
    p.add_argument(
        "--skip-health-probe",
        action="store_true",
        help="実行前の全強session probeを省略する（強session限定は維持）",
    )
    p.add_argument(
        "--ignore-cooldown",
        action="store_true",
        help="手動復旧用: cooldown中の強sessionも候補にする（通常sessionには落とさない）",
    )
    p.add_argument(
        "--batch-per-account",
        type=int,
        help="1セッション内の処理件数上限。0以下で無制限（未指定時はconfig/accounts.jsonの値）",
    )
    p.add_argument(
        "--daily-limit-per-account",
        type=int,
        default=DAILY_LIMIT_PER_ACCOUNT,
        help=f"1アカウントあたりの日次チェック上限。0以下で無制限（既定: {DAILY_LIMIT_PER_ACCOUNT}）",
    )
    return p.parse_args()


def load_targets(path: str) -> list[str]:
    accounts = []
    with open(path) as f:
        reader = csv.DictReader(f)
        for row in reader:
            aid = row.get("account_id", "").strip()
            if aid:
                accounts.append(aid)
    return accounts


def load_already_checked(path: str) -> set[str]:
    """成功済み（error空）のアカウントIDを返す"""
    checked = set()
    if not Path(path).exists():
        return checked
    with open(path) as f:
        reader = csv.DictReader(f)
        for row in reader:
            if row.get("error", "") == "":
                checked.add(row["account_id"])
    return checked


# ---------------------------------------------------------------------------
# セッション管理（セッション再利用優先、失敗時のみlogin）
# ---------------------------------------------------------------------------
# BadPassword / UnknownError(アカウント不在) と判定される例外メッセージパターン
DEAD_ACCOUNT_PATTERNS = (
    "BadPassword", "bad_password", "incorrect password",
    "We can't find an account", "find an account with",
)

# IP ブラックリスト起因のエラーパターン
# Instagram が "BadPassword" を返してもメッセージに change your IP / blacklist が含まれる場合は
# パスワード問題ではなくIP側の問題。誤ってアカウント削除しないよう分岐する。
IP_BLACKLIST_PATTERNS = (
    "change your IP address",
    "blacklist of the Instagram",
)

# IP blacklist 検出フラグ（検出したらメインループを即停止する）
_IP_BLACKLIST_DETECTED = False


class ProbeTimeoutError(TimeoutError):
    pass


class UsernameInfoUnavailableError(RuntimeError):
    pass


def _is_usernameinfo_endpoint_unavailable(exc: Exception) -> bool:
    """usernameinfo endpoint自体がそのsessionで使えない404かを判定する。"""
    msg = str(exc)
    return "usernameinfo/" in msg and (
        "Client Error: Not Found" in msg or "does not exist" in msg
    )


def _with_alarm(timeout: int, func, label: str = "operation"):
    """instagrapi が内部で固まっても詐欺チェック全体を止めないための外側timeout。"""
    if threading.current_thread() is not threading.main_thread():
        return func()

    def _handler(_signum, _frame):
        raise ProbeTimeoutError(f"{label} timeout after {timeout}s")

    previous_handler = signal.getsignal(signal.SIGALRM)
    previous_timer = signal.setitimer(signal.ITIMER_REAL, timeout)
    signal.signal(signal.SIGALRM, _handler)
    try:
        return func()
    finally:
        signal.setitimer(signal.ITIMER_REAL, previous_timer[0], previous_timer[1])
        signal.signal(signal.SIGALRM, previous_handler)


def probe_instagram(cl: Client, timeout: int = 20):
    try:
        return _with_alarm(
            timeout,
            lambda: cl.private_request("users/instagram/usernameinfo/"),
            "probe",
        )
    except (ClientNotFoundError, UserNotFound) as e:
        if _is_usernameinfo_endpoint_unavailable(e):
            raise UsernameInfoUnavailableError("usernameinfo endpoint unavailable") from e
        raise


def _is_ip_blacklisted_error(exc: Exception) -> bool:
    s = f"{type(exc).__name__}: {exc}"
    return any(p in s for p in IP_BLACKLIST_PATTERNS)


def _is_dead_account_error(exc: Exception) -> bool:
    # IP blacklist メッセージが含まれる場合はアカウントではなくIP側の問題
    if _is_ip_blacklisted_error(exc):
        return False
    s = f"{type(exc).__name__}: {exc}"
    return any(p in s for p in DEAD_ACCOUNT_PATTERNS)


def remove_account_from_config(username: str) -> None:
    """accounts.json から該当アカウントを削除。削除前にバックアップを残す。"""
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            cfg = json.load(f)
        before = len(cfg["accounts"])
        cfg["accounts"] = [a for a in cfg["accounts"] if a["username"] != username]
        after = len(cfg["accounts"])
        if before != after:
            backup_path = CONFIG_PATH.with_name(
                f"{CONFIG_PATH.name}.bak_remove_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
            )
            backup_path.write_text(
                CONFIG_PATH.read_text(encoding="utf-8"),
                encoding="utf-8",
            )
            with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                json.dump(cfg, f, indent=2, ensure_ascii=False)
            print(f"  ⓧ {username} を accounts.json から削除 ({before}→{after})")
            print(f"  backup: {backup_path.name}")
    except Exception as e:
        print(f"  削除失敗: {e}")


COOLDOWN_DIR_NAME = "cooldowns"
# challenge等で焼けた時のクールダウン期間（時間単位）
CHALLENGE_COOLDOWN_HOURS = 6
# 強session が死んでいる可能性が高い時は ADR に合わせて長めに寝かせる
STRONG_DEAD_COOLDOWN_HOURS = 48
# 1アカウントあたりの1日あたり最大チェック数（焼け予防）
DAILY_LIMIT_PER_ACCOUNT = 50

CAPTURES_DIR = BASE_DIR / "captures"


def safe_dump_settings(cl: "Client", session_path: Path) -> None:
    """(Plan B) 強session は上書きしない dump_settings ラッパー"""
    if is_strong_session(session_path):
        return
    cl.dump_settings(str(session_path))


def is_strong_session(session_path: Path) -> bool:
    """session file が mitmproxy 由来の強 session かを判定"""
    if not session_path.exists():
        return False
    try:
        d = json.load(open(session_path))
        return d.get("source") == "mitmproxy"
    except Exception:
        return False


def is_scam_harm_type(harm_type: str | None) -> bool:
    """Instagram側のSCAM系harm_typeを詐欺表示として扱う。"""
    return str(harm_type or "").upper().startswith("SCAM")


def fetch_user_by_username(cl: Client, username: str, timeout: int = 30) -> dict:
    """usernameinfoを直接使い、公開Web lookup経由のuser_id解決を避ける。"""
    try:
        raw = _with_alarm(
            timeout,
            lambda: cl.private_request(f"users/{username}/usernameinfo/"),
            "usernameinfo",
        )
    except (ClientNotFoundError, UserNotFound) as first_error:
        if _is_usernameinfo_endpoint_unavailable(first_error):
            raise UsernameInfoUnavailableError("usernameinfo endpoint unavailable") from first_error
        # 一部sessionでは usernameinfo が実在ユーザーにも404を返すため、
        # 404だけ旧経路に戻して「全件NOT FOUND」誤判定を避ける。
        try:
            user_id = _with_alarm(timeout, lambda: cl.user_id_from_username(username), "username lookup fallback")
            raw = _with_alarm(timeout, lambda: cl.private_request(f"users/{user_id}/info/"), "user info fallback")
        except (ClientNotFoundError, UserNotFound) as fallback_error:
            if _is_usernameinfo_endpoint_unavailable(fallback_error):
                raise UsernameInfoUnavailableError("usernameinfo endpoint unavailable") from fallback_error
            raise
        user = raw.get("user", {})
        user.setdefault("pk", user_id)
        return user
    return raw.get("user", {})


def auto_import_capture_if_newer(username: str, session_path: Path) -> bool:
    """(Plan A) captures/ に新しいcaptureがあれば import_real_session.py で取り込む。
    戻り値: import を実行したら True, それ以外 False
    """
    if not CAPTURES_DIR.exists():
        return False
    # session_path から ds_user_id を取得してcaptureファイルを探す
    ds_user_id = None
    if session_path.exists():
        try:
            d = json.load(open(session_path))
            ds_user_id = d.get("authorization_data", {}).get("ds_user_id")
        except Exception:
            pass
    if not ds_user_id:
        return False
    capture_path = CAPTURES_DIR / f"{ds_user_id}.json"
    if not capture_path.exists():
        return False
    # session より capture が新しくなければ何もしない
    if session_path.exists() and capture_path.stat().st_mtime <= session_path.stat().st_mtime:
        return False
    # import 実行
    import subprocess
    print(f"  📥 captures/{ds_user_id}.json → sessions/{username}.json に取り込み")
    importer = Path(__file__).parent / "import_real_session.py"
    r = subprocess.run(
        [sys.executable, str(importer), "--capture", str(capture_path), "--username", username],
        capture_output=True, text=True, timeout=60,
    )
    if r.returncode != 0:
        print(f"  import失敗: {r.stderr[:200]}")
        return False
    return True


def _cooldown_path(username: str) -> Path:
    d = BASE_DIR / COOLDOWN_DIR_NAME
    d.mkdir(exist_ok=True)
    return d / f"{username}.cooldown"


def is_in_cooldown(username: str) -> bool:
    p = _cooldown_path(username)
    if not p.exists():
        return False
    try:
        until_ts = float(p.read_text().strip())
        return datetime.now().timestamp() < until_ts
    except Exception:
        return False


def set_cooldown(username: str, hours: float = CHALLENGE_COOLDOWN_HOURS) -> None:
    """challenge等で焼けたアカウントを一時休眠させる"""
    from datetime import timedelta
    until = (datetime.now() + timedelta(hours=hours)).timestamp()
    _cooldown_path(username).write_text(str(until))


def _daily_usage_path(username: str) -> Path:
    today = datetime.now().strftime("%Y%m%d")
    d = BASE_DIR / COOLDOWN_DIR_NAME / "daily_usage"
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{username}_{today}.txt"


def get_daily_usage(username: str) -> int:
    p = _daily_usage_path(username)
    if not p.exists():
        return 0
    try:
        return int(p.read_text().strip())
    except Exception:
        return 0


def increment_daily_usage(username: str) -> int:
    p = _daily_usage_path(username)
    cur = get_daily_usage(username) + 1
    p.write_text(str(cur))
    return cur


def restore_or_login(username: str, password: str, device: dict, proxy: str,
                     session_path: Path, uuids: dict | None = None) -> Client | None:
    """セッション復元を試み、失敗したらloginにフォールバック
    戻り値: Client or None (None時は呼び出し側で次のアカウントへ)
    BadPassword/UnknownError 検知時は accounts.json から自動削除する。
    uuids: accounts.json の uuids フィールド（端末fingerprint固定用）
    """
    cl = Client()
    cl.delay_range = [4, 6]

    # プロキシ設定
    if proxy:
        cl.set_proxy(proxy)

    # セッション復元を試みる
    # (Plan A) captures/ に新しい強session があれば先に取り込む
    auto_import_capture_if_newer(username, session_path)

    if session_path.exists():
        try:
            cl.load_settings(str(session_path))
            # load_settingsはプロキシを復元しないので再設定
            if proxy:
                cl.set_proxy(proxy)
            # セッション有効性テスト: 他人参照プローブ
            # account_info() は通るのに user_info_by_username() で LoginRequired が出るケースがある
            # (IP不一致による信用低下状態) ので、実運用と同じ「他人参照」で検査する
            probe_instagram(cl)
            src = "強session" if is_strong_session(session_path) else "通常session"
            print(f"✓ {username} セッション復元成功（{src}, 他人参照OK）")
            return cl
        except Exception as e:
            # (Plan B) 強sessionが probe 失敗した場合は login で上書きしない
            # (実機で取った貴重なsessionを守る。cooldownを入れて明日以降リトライ)
            if is_strong_session(session_path):
                print(f"  ⚠ 強session復元NG ({type(e).__name__}) → login はスキップ、cooldown登録")
                set_cooldown(username, STRONG_DEAD_COOLDOWN_HOURS)
                return None
            print(f"  セッション無効 ({type(e).__name__}) → 再ログイン")

    # フォールバック: ログイン
    try:
        # UUIDs を固定セット（毎回新デバイス扱いされるとchallengeを引きやすいため）
        if uuids:
            try:
                cl.set_uuids(uuids)
            except Exception:
                # instagrapiのバージョン差でNGなら属性直設定で代替
                for k, v in uuids.items():
                    if hasattr(cl, k):
                        setattr(cl, k, v)
        cl.set_device(device)
        if proxy:
            cl.set_proxy(proxy)
        # 2FA/Challenge要求時に自動スキップ（input()ハング防止）
        # last_json を保存してから例外を投げることでチャレンジ情報を失わない
        def _skip_challenge(*a, **kw):
            try:
                print(f"  challenge last_json: {cl.last_json}")
            except Exception:
                pass
            raise Exception("challenge_code requested - skipping")
        cl.challenge_code_handler = _skip_challenge
        cl.login(username, password)
        # ログイン直後に他人参照プローブ。通らなければこのセッションは使えない
        try:
            probe_instagram(cl)
        except Exception as probe_err:
            print(f"✗ {username}: ログインは成功したが他人参照NG ({type(probe_err).__name__})")
            return None
        # (Plan B) 強session保護: 既存が強session ならdumpで上書きしない
        if is_strong_session(session_path):
            print(f"⚠ {username}: 強session検出のため dump_settings をスキップ（ログインのみ）")
        else:
            safe_dump_settings(cl, session_path)
        print(f"✓ {username} ログイン成功 → セッション保存")
        return cl
    except Exception as e:
        print(f"✗ {username}: {type(e).__name__}: {e}")
        if _is_ip_blacklisted_error(e):
            # IP側の問題なのでアカウントは無実。フラグ立てて即停止させる
            global _IP_BLACKLIST_DETECTED
            _IP_BLACKLIST_DETECTED = True
            print(f"  ⚠ IP blacklist 検出: {username} は無実なので削除しません")
        elif _is_dead_account_error(e):
            remove_account_from_config(username)
        elif "ChallengeRequired" in type(e).__name__ or "LoginRequired" in type(e).__name__:
            # challenge/login_required はしばらく置くと解除されることがある
            set_cooldown(username, CHALLENGE_COOLDOWN_HOURS)
            print(f"  ⏸ {username} を {CHALLENGE_COOLDOWN_HOURS}h クールダウン")
        return None


def main() -> int:
    args = parse_args()
    config = load_config()

    password = config.get("password", "")
    batch_per_account = (
        args.batch_per_account
        if args.batch_per_account is not None
        else config.get("batch_per_account", DEFAULT_CONFIG["batch_per_account"])
    )
    daily_limit_per_account = args.daily_limit_per_account
    sessions_dir = BASE_DIR / config.get("sessions_dir", DEFAULT_CONFIG["sessions_dir"])
    sessions_dir.mkdir(exist_ok=True)
    # NOTE:
    # 通常session(instagrapi login) へのフォールバックは連鎖焼けを誘発するため使わない。
    # 強sessionだけを使い、全滅したら non-zero で停止する。
    all_accounts = config.get("accounts", [])
    if not all_accounts:
        all_accounts = _accounts_from_strong_sessions(sessions_dir)

    # 強sessionのみ抽出（sessions/{username}.json が source=mitmproxy のもの）
    strong_accounts = []
    for acc in all_accounts:
        u = acc["username"]
        sp = sessions_dir / f"{u}.json"
        if sp.exists() and is_strong_session(sp):
            strong_accounts.append(acc)
    if args.only_account:
        strong_accounts = [acc for acc in strong_accounts if acc["username"] == args.only_account]
        args.start_account = args.only_account

    # 強session内で probe 成功を最優先（NGは 48h cooldown を延長）。
    # dry-run はセッション復元テスト用途なので、preflightの全件probeは避ける。
    strong_ok: set[str] | None = None
    if args.dry_run or args.skip_health_probe:
        print("[dry-run] preflight probe はスキップします")
    else:
        try:
            from strong_session_pool import health_check_all as _health_check_all
            strong_ok = set(_health_check_all())
            print(f"probe OK 強session: {len(strong_ok)}件")
        except Exception as e:
            print(f"✗ strong_session_pool.health_check_all 失敗: {e}")
            return 5

    def _strong_priority(acc):
        u = acc["username"]
        return 0 if (strong_ok is not None and u in strong_ok) else 1

    login_accounts = sorted(strong_accounts, key=_strong_priority)
    if not login_accounts:
        print("✗ 強sessionが見つかりません（通常sessionへのフォールバックは無効です）")
        return 5

    # --start-account 未指定なら、先頭の強sessionを自動選択（probe OK を優先）
    if not args.start_account:
        for acc in login_accounts:
            u = acc["username"]
            if is_in_cooldown(u) and not args.ignore_cooldown:
                continue
            if strong_ok is not None and u not in strong_ok:
                continue
            args.start_account = u
            print(f"[auto] start-account に強session {u} を自動選択")
            break

    # 出力パス
    if args.output:
        output_path = Path(args.output)
    else:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        output_path = LOGS_DIR / f"api_warning_{ts}.csv"

    # チェック対象読み込み
    targets = load_targets(args.input)
    print(f"入力: {len(targets)}アカウント")

    # resume: 成功済みをスキップ（エラーは再試行）
    already_checked = set()
    if args.resume and output_path.exists():
        already_checked = load_already_checked(str(output_path))
        print(f"チェック済み: {len(already_checked)}件スキップ")

    remaining = [a for a in targets if a not in already_checked]
    print(f"チェック対象: {len(remaining)}件")
    batch_label = "無制限" if batch_per_account <= 0 else f"{batch_per_account}件/アカウント"
    daily_label = "無制限" if daily_limit_per_account <= 0 else f"{daily_limit_per_account}件/日/アカウント"
    print(f"バッチサイズ: {batch_label}")
    print(f"日次上限: {daily_label}")
    print(f"使用アカウント数: {len(login_accounts)}")

    if not remaining and not args.dry_run:
        print("全件チェック済み")
        return 0

    # ---------------------------------------------------------------------------
    # セッション復元 / ログイン
    # ---------------------------------------------------------------------------
    account_idx = 0
    if args.start_account:
        for idx, acc in enumerate(login_accounts):
            if acc["username"] == args.start_account:
                account_idx = idx
                print(f"開始アカウント: {args.start_account} (index={idx})")
                break
        else:
            print(f"警告: {args.start_account} が見つかりません。先頭から開始します。")
    requests_this_session = 0
    cl = None

    consecutive_login_fails = 0  # 連続login失敗数
    MAX_CONSECUTIVE_LOGIN_FAILS = 10  # 10連続で失敗したら「IP/環境全滅」と判定して停止

    def next_account():
        nonlocal cl, account_idx, requests_this_session, consecutive_login_fails
        while account_idx < len(login_accounts):
            acc = login_accounts[account_idx]
            username = acc["username"]
            session_path = sessions_dir / f"{username}.json"
            if not is_strong_session(session_path):
                print(f"--- {username} は強sessionではない → skip", flush=True)
                account_idx += 1
                continue

            # クールダウン中のアカウントはスキップ (A-2: セッション保全)
            if is_in_cooldown(username) and not args.ignore_cooldown:
                print(f"--- {username} はクールダウン中 → skip", flush=True)
                account_idx += 1
                continue

            # クールダウン: 連続ログインバーストでchallenge誘発されるのを防ぐ
            # 初回(cl is None)はスキップ。セッション復元成功時も省略するが、
            # 現状の実装では切替判定前に判定できないので一律適用。
            if cl is not None:
                cooldown = random.uniform(15, 30)
                print(f"... クールダウン {cooldown:.1f}s", flush=True)
                time.sleep(cooldown)

            print(f"\n--- アカウント切替: {username} ---", flush=True)

            proxy = "" if args.no_proxy else acc.get("proxy", "")
            cl = restore_or_login(
                username=username,
                password=password,
                device=acc["device"],
                proxy=proxy,
                session_path=session_path,
                uuids=acc.get("uuids"),
            )
            if cl:
                requests_this_session = 0
                consecutive_login_fails = 0  # リセット
                return True
            # IP blacklist 検出時は即停止（10件待たない）
            if _IP_BLACKLIST_DETECTED:
                print(
                    f"\n⚠ IP blacklist 検出 → 即停止します。"
                    f"IPを変更するか時間を置いてから再実行してください。"
                )
                return False
            consecutive_login_fails += 1
            if consecutive_login_fails >= MAX_CONSECUTIVE_LOGIN_FAILS:
                print(
                    f"\n⚠ 連続 {consecutive_login_fails} 件login/probe失敗 → "
                    f"IP/環境が全滅している可能性が高いため停止します。"
                )
                return False
            account_idx += 1
        print("全アカウント使い切り")
        return False

    if not next_account():
        print("✗ 強sessionが全滅しました（probe/LoginRequired）。停止します。")
        return 5

    # dry-run: セッション復元テストのみ
    if args.dry_run:
        print(f"\n[dry-run] セッション復元成功。チェックは実行しません。")
        # 残りのアカウントもテスト
        while account_idx + 1 < len(login_accounts):
            account_idx += 1
            next_account()
        return 0

    # ---------------------------------------------------------------------------
    # CSV準備
    # ---------------------------------------------------------------------------
    fieldnames = [
        "account_id", "user_id", "has_scam_flag", "harm_type",
        "account_warning_raw", "links_integrity_info",
        "follower_count", "following_count", "media_count",
        "error", "checked_by", "checked_at",
    ]
    write_header = not output_path.exists() or not args.resume
    fh = open(output_path, "a" if args.resume else "w", newline="")
    writer = csv.DictWriter(fh, fieldnames=fieldnames)
    if write_header:
        writer.writeheader()

    # ---------------------------------------------------------------------------
    # チェックループ（AUTH ERROR時は同じターゲットを別アカウントで再試行）
    # ---------------------------------------------------------------------------
    success = 0
    fail = 0
    scam_count = 0
    consecutive_errors = 0
    MAX_CONSECUTIVE_ERRORS = 3
    target_auth_retry = {}  # {account_id: 回数} 同一ターゲットでAUTH ERRORが続いたら諦める
    MAX_TARGET_AUTH_RETRY = 2
    target_timeout_retry = {}  # {account_id: 回数} timeoutは別sessionで再試行する
    MAX_TARGET_TIMEOUT_RETRY = 2
    current_username = login_accounts[account_idx]["username"]

    i = 0
    while i < len(remaining):
        account_id = remaining[i]

        # アカウントローテーション（バッチ上限 or 1日あたり上限）
        daily_used = get_daily_usage(current_username)
        batch_limit_reached = batch_per_account > 0 and requests_this_session >= batch_per_account
        daily_limit_reached = daily_limit_per_account > 0 and daily_used >= daily_limit_per_account
        if batch_limit_reached or daily_limit_reached:
            session_path = sessions_dir / f"{current_username}.json"
            safe_dump_settings(cl, session_path)

            if daily_limit_reached:
                set_cooldown(current_username, 24)
                print(f"  ⏸ {current_username} 1日上限({daily_limit_per_account}件)到達 → 24h cooldown", flush=True)

            account_idx += 1
            if not next_account():
                print("✗ 強sessionが全滅しました（ローテーション先なし）。停止します。")
                fh.close()
                return 5
            current_username = login_accounts[account_idx]["username"]

        print(f"[{i+1}/{len(remaining)}] {account_id}...", end=" ", flush=True)

        row = {
            "account_id": account_id,
            "checked_by": current_username,
            "checked_at": datetime.now().isoformat(),
        }

        def rotate_account_after_failure(reason: str) -> bool:
            nonlocal account_idx, current_username, consecutive_errors
            print(f"{reason} → チェッカー切替")
            session_path = sessions_dir / f"{current_username}.json"
            safe_dump_settings(cl, session_path)
            account_idx += 1
            if not next_account():
                return False
            current_username = login_accounts[account_idx]["username"]
            consecutive_errors = 0
            return True

        try:
            # 生JSONでuser_info取得
            # usernameinfo は username から直接取得できるため、
            # user_id_from_username() の公開Web lookup timeoutを避けられる。
            user = fetch_user_by_username(cl, account_id)
            row["user_id"] = str(user.get("pk") or user.get("pk_id") or user.get("id") or "")

            # account_warning
            warning = user.get("account_warning")
            if warning:
                harm_type = warning.get("harm_type", "")
                row["has_scam_flag"] = "TRUE" if is_scam_harm_type(harm_type) else "FALSE"
                row["harm_type"] = harm_type
                row["account_warning_raw"] = json.dumps(warning)
                if is_scam_harm_type(harm_type):
                    scam_count += 1
            else:
                row["has_scam_flag"] = "FALSE"
                row["harm_type"] = ""
                row["account_warning_raw"] = ""

            # links_integrity_info
            links_info = user.get("links_integrity_info")
            row["links_integrity_info"] = json.dumps(links_info) if links_info else ""

            # 基本情報
            row["follower_count"] = user.get("follower_count", "")
            row["following_count"] = user.get("following_count", "")
            row["media_count"] = user.get("media_count", "")
            row["error"] = ""

            print(f"SCAM={row['has_scam_flag']}")
            success += 1
            consecutive_errors = 0
            requests_this_session += 1
            increment_daily_usage(current_username)
            writer.writerow(row)
            fh.flush()
            i += 1  # 成功 → 次のターゲットへ

        except UsernameInfoUnavailableError as e:
            print(f"SESSION NG ({e}) → 現在の強sessionを休眠してチェッカー切替")
            set_cooldown(current_username, STRONG_DEAD_COOLDOWN_HOURS)
            session_path = sessions_dir / f"{current_username}.json"
            safe_dump_settings(cl, session_path)
            account_idx += 1
            if not next_account():
                print("✗ 強sessionが全滅しました（usernameinfo endpoint unavailable）。停止します。")
                fh.close()
                return 5
            current_username = login_accounts[account_idx]["username"]
            continue

        except UserNotFound:
            row["error"] = "user_not_found"
            print("NOT FOUND")
            fail += 1
            writer.writerow(row)
            fh.flush()
            i += 1  # NOT FOUND → 次のターゲットへ

        except PleaseWaitFewMinutes:
            print("RATE LIMITED - 5分待機...")
            row["error"] = "rate_limited"
            fail += 1
            writer.writerow(row)
            fh.flush()
            time.sleep(300)
            # レート制限後も同じターゲットを再試行しない（待機済み）
            i += 1

        except ProbeTimeoutError as e:
            target_timeout_retry[account_id] = target_timeout_retry.get(account_id, 0) + 1
            retry_count = target_timeout_retry[account_id]
            print(f"TIMEOUT ({e}) x{retry_count}")

            if retry_count <= MAX_TARGET_TIMEOUT_RETRY:
                if rotate_account_after_failure("TIMEOUT"):
                    continue  # iを進めず、同じターゲットを別sessionで再試行

                print("ローテーション先なし → timeout結果として記録")

            row["error"] = str(e)[:200]
            fail += 1
            consecutive_errors += 1
            writer.writerow(row)
            fh.flush()
            i += 1

        except (ChallengeRequired, ChallengeUnknownStep, LoginRequired) as e:
            target_auth_retry[account_id] = target_auth_retry.get(account_id, 0) + 1
            retry_count = target_auth_retry[account_id]
            print(f"AUTH ERROR ({type(e).__name__}) x{retry_count} → 現在の強sessionを休眠")
            set_cooldown(current_username, STRONG_DEAD_COOLDOWN_HOURS)
            session_path = sessions_dir / f"{current_username}.json"
            safe_dump_settings(cl, session_path)

            # 同一ターゲットで MAX_TARGET_AUTH_RETRY 回 AUTH ERROR が出たらそのターゲットを諦める
            # (対象固有の問題である可能性が高いので、全チェッカーを潰すのを防ぐ)
            if retry_count >= MAX_TARGET_AUTH_RETRY:
                print("AUTH ERROR 上限到達 → ターゲットをスキップしてチェッカー切替")
                row["error"] = f"auth_error_target_skip ({type(e).__name__})"
                fail += 1
                writer.writerow(row)
                fh.flush()
                i += 1  # 次のターゲットへ
                account_idx += 1
                if i < len(remaining) and not next_account():
                    print("✗ 強sessionが全滅しました（AUTH ERROR後の切替先なし）。停止します。")
                    fh.close()
                    return 5
                if i < len(remaining):
                    current_username = login_accounts[account_idx]["username"]
                continue

            print("AUTH ERROR → チェッカー切替")
            # AUTH ERROR → アカウント切替して同じターゲットを再試行（iを進めない）
            account_idx += 1
            if not next_account():
                row["error"] = type(e).__name__
                fail += 1
                writer.writerow(row)
                fh.flush()
                print("✗ 強sessionが全滅しました（LoginRequired/Challenge）。停止します。")
                fh.close()
                return 5
            current_username = login_accounts[account_idx]["username"]
            # i を進めない → 同じターゲットを再試行

        except Exception as e:
            row["error"] = str(e)[:200]
            print(f"ERROR: {e}")
            fail += 1
            consecutive_errors += 1
            writer.writerow(row)
            fh.flush()
            i += 1  # 不明エラー → 次のターゲットへ

            # 連続エラーが閾値を超えたらアカウント切替
            if consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
                print(f"連続{consecutive_errors}回エラー → アカウント切替")
                session_path = sessions_dir / f"{current_username}.json"
                safe_dump_settings(cl, session_path)
                account_idx += 1
                if not next_account():
                    print("✗ 強sessionが全滅しました（連続エラーでローテーション先なし）。停止します。")
                    fh.close()
                    return 5
                current_username = login_accounts[account_idx]["username"]
                consecutive_errors = 0

    # 最終セッション保存
    if cl and account_idx < len(login_accounts):
        session_path = sessions_dir / f"{current_username}.json"
        safe_dump_settings(cl, session_path)

    fh.close()

    # サマリー
    print(f"\n{'='*50}")
    print(f"結果: {success}成功 / {fail}失敗")
    print(f"SCAM検出: {scam_count}件")
    print(f"保存: {output_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
