#!/usr/bin/env python3
"""強session (source=mitmproxy) の在庫と健全性を管理する。

- list_strong_sessions(): sessions/*.json のうち source=mitmproxy を列挙
- health_check(username): usernameinfo probe で healthy 判定
- count_healthy(probe=False): 在庫数。probe=True なら実際に当てて判定
- CLI: --list / --probe / --count

cron 運用時はネットワーク I/O 有り (probe=True) は必要最小限に。
"""
from __future__ import annotations

import argparse
import json
import random
import signal
import sys
import time
import threading
from dataclasses import dataclass
from pathlib import Path
from datetime import datetime, timedelta

BASE_DIR = Path(__file__).resolve().parent.parent
SESSIONS_DIR = BASE_DIR / "sessions"
COOLDOWNS_DIR = BASE_DIR / "cooldowns"

# probe NG (= IG側で死んでいる可能性が高い) の場合は ADR に合わせて長めに寝かせる
DEAD_PROBE_COOLDOWN_HOURS = 48


class ProbeTimeoutError(TimeoutError):
    pass


def _is_usernameinfo_endpoint_unavailable(exc: Exception) -> bool:
    msg = str(exc)
    return "usernameinfo/" in msg and (
        "Client Error: Not Found" in msg or "does not exist" in msg
    )


def _with_alarm(timeout: int, func):
    """instagrapi が内部で固まっても probe 全体を止めないための外側timeout。"""
    if threading.current_thread() is not threading.main_thread():
        return func()

    def _handler(_signum, _frame):
        raise ProbeTimeoutError(f"probe timeout after {timeout}s")

    previous_handler = signal.getsignal(signal.SIGALRM)
    previous_timer = signal.setitimer(signal.ITIMER_REAL, timeout)
    signal.signal(signal.SIGALRM, _handler)
    try:
        return func()
    finally:
        signal.setitimer(signal.ITIMER_REAL, previous_timer[0], previous_timer[1])
        signal.signal(signal.SIGALRM, previous_handler)

@dataclass
class StrongSession:
    username: str
    path: Path
    source: str
    mtime: float
    age_hours: float
    in_cooldown: bool = False
    healthy: bool | None = None  # None = probe未実行
    probe_reason: str = ""


def is_in_cooldown(username: str) -> bool:
    cd = COOLDOWNS_DIR / f"{username}.cooldown"
    if not cd.exists():
        return False
    try:
        until_ts = float(cd.read_text().strip())
        return time.time() < until_ts
    except Exception:
        return False


def set_cooldown(username: str, hours: float) -> None:
    """cooldowns/{username}.cooldown に until timestamp を書く。"""
    COOLDOWNS_DIR.mkdir(exist_ok=True)
    until_ts = (datetime.now() + timedelta(hours=hours)).timestamp()
    (COOLDOWNS_DIR / f"{username}.cooldown").write_text(str(until_ts))


def list_strong_sessions() -> list[StrongSession]:
    """sessions/*.json を読んで source=mitmproxy のものだけ返す。"""
    out: list[StrongSession] = []
    if not SESSIONS_DIR.exists():
        return out
    now = time.time()
    for f in sorted(SESSIONS_DIR.glob("*.json")):
        try:
            d = json.load(open(f))
        except Exception:
            continue
        if d.get("source") != "mitmproxy":
            continue
        mtime = f.stat().st_mtime
        out.append(StrongSession(
            username=f.stem,
            path=f,
            source="mitmproxy",
            mtime=mtime,
            age_hours=(now - mtime) / 3600.0,
            in_cooldown=is_in_cooldown(f.stem),
        ))
    return out


def health_check(username: str, timeout: int = 15) -> tuple[bool, str]:
    """users/instagram/usernameinfo/ probe。戻り値 (ok, reason)。"""
    from instagrapi import Client
    from instagrapi.exceptions import ClientNotFoundError, UserNotFound

    session_path = SESSIONS_DIR / f"{username}.json"
    if not session_path.exists():
        return False, "session file missing"
    try:
        cl = Client()
        cl.delay_range = [1, 2]
        cl.load_settings(str(session_path))
        try:
            _with_alarm(timeout, lambda: cl.private_request("users/instagram/usernameinfo/"))
            return True, "probe OK usernameinfo"
        except (ClientNotFoundError, UserNotFound) as first_error:
            if _is_usernameinfo_endpoint_unavailable(first_error):
                return False, "usernameinfo endpoint unavailable"
            user_id = _with_alarm(timeout, lambda: cl.user_id_from_username("instagram"))
            _with_alarm(timeout, lambda: cl.private_request(f"users/{user_id}/info/"))
            return True, f"probe OK fallback after {type(first_error).__name__}"
    except Exception as e:
        return False, f"{type(e).__name__}: {str(e)[:100]}"


def health_check_all(timeout: int = 15) -> list[str]:
    """強session全体をprobeし、成功したusername一覧を返す。

    - cooldown中はprobeしない
    - probe失敗 (= 死んでいる可能性が高い) は cooldown を 48h 延長してしばらく触らない
    """
    ok_users: list[str] = []
    candidates = [s for s in list_strong_sessions() if not s.in_cooldown]
    for idx, s in enumerate(candidates):
        if s.in_cooldown:
            continue
        ok, _reason = health_check(s.username, timeout=timeout)
        if ok:
            ok_users.append(s.username)
        else:
            set_cooldown(s.username, DEAD_PROBE_COOLDOWN_HOURS)
        if idx < len(candidates) - 1:
            time.sleep(random.uniform(1.0, 2.0))
    return ok_users


def count_healthy(probe: bool = False) -> int:
    """healthy な強sessionの数。probe=False ならファイル存在 + not in cooldown 基準。"""
    sessions = list_strong_sessions()
    if not probe:
        return sum(1 for s in sessions if not s.in_cooldown)
    return len(health_check_all())


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--list", action="store_true", help="強session一覧表示")
    ap.add_argument("--probe", action="store_true", help="全sessionにprobe実行")
    ap.add_argument("--count", action="store_true", help="healthy件数のみ出力")
    ap.add_argument("--count-probe", action="store_true", help="probe付きでhealthy件数を出力")
    args = ap.parse_args()

    sessions = list_strong_sessions()

    if args.count:
        print(sum(1 for s in sessions if not s.in_cooldown))
        return 0

    if args.count_probe:
        print(count_healthy(probe=True))
        return 0

    if args.probe:
        probe_targets = [s for s in sessions if not s.in_cooldown]
        for idx, s in enumerate(sessions):
            if s.in_cooldown:
                s.healthy = False
                s.probe_reason = "in cooldown"
                continue
            ok, reason = health_check(s.username)
            s.healthy = ok
            s.probe_reason = reason
            if not ok:
                set_cooldown(s.username, DEAD_PROBE_COOLDOWN_HOURS)
                s.in_cooldown = True
                s.probe_reason = f"{reason} (cooldown {DEAD_PROBE_COOLDOWN_HOURS}h)"
            if s in probe_targets and probe_targets.index(s) < len(probe_targets) - 1:
                time.sleep(random.uniform(1.0, 2.0))

    if args.list or args.probe:
        print(f"{'USERNAME':<24} {'AGE(h)':>8}  {'COOLDOWN':<10} {'HEALTHY':<10} REASON")
        for s in sessions:
            h = "?" if s.healthy is None else ("OK" if s.healthy else "NG")
            cd = "YES" if s.in_cooldown else "-"
            print(f"{s.username:<24} {s.age_hours:>8.1f}  {cd:<10} {h:<10} {s.probe_reason}")
        print(f"\n合計: {len(sessions)}件 / healthy推定: {sum(1 for s in sessions if not s.in_cooldown and (s.healthy in (None, True)))}件")
        return 0

    ap.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
