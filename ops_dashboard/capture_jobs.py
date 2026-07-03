from __future__ import annotations

import os
import re
import shlex
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
JST = timezone(timedelta(hours=9))
PYTHON = str(ROOT / "venv" / "bin" / "python") if (ROOT / "venv" / "bin" / "python").exists() else sys.executable
ADB = os.environ.get("ADB", str(Path.home() / "Library" / "Android" / "sdk" / "platform-tools" / "adb"))
MAX_LOG_LINES = 600
RELATED_LOG_TAIL_LINES = 80
RELATED_LOG_TAIL_BYTES = 80 * 1024
RELATED_LOG_LIMIT = 4

LOG_PATH_PATTERNS = [
    re.compile(r"(?:LOG=|log:\s*|ログ:\s*)(.+?\.log)(?=$|\))"),
    re.compile(r"((?:/|~)[^\n\r]+?\.log)(?=$|\))"),
    re.compile(r"(logs/[^\s\n\r)]+?\.log)(?=$|\s|\))"),
]
SENSITIVE_PATTERNS = [
    re.compile(r"(?i)(password|passwd|pwd|SHIN_CAPTURE_PASSWORD)([=:\s]+)([^\s'\"&]+)"),
    re.compile(r"(?i)(sessionid|csrftoken|ds_user_id|mid|ig_did)(=)([^;\s'\"&]+)"),
    re.compile(r"(?i)(authorization|cookie)(:\s*)(.+)$"),
    re.compile(r'(?i)("(?:password|sessionid|csrftoken|authorization|cookie)"\s*:\s*")([^"]+)(")'),
]

_JOBS: dict[str, dict[str, Any]] = {}
_LOCK = threading.Lock()


def _now() -> str:
    return datetime.now(JST).isoformat(timespec="seconds")


def _rel(path: Path) -> str:
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)


def _quick_run(cmd: list[str], timeout: int = 20) -> dict[str, Any]:
    try:
        result = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, timeout=timeout)
        output = "\n".join((result.stdout or result.stderr or "").strip().splitlines()[-80:])
        return {"ok": result.returncode == 0, "code": result.returncode, "output": output}
    except Exception as e:
        return {"ok": False, "code": None, "output": f"{type(e).__name__}: {e}"}


def _latest_files(directory: Path, pattern: str, limit: int = 8) -> list[dict[str, str]]:
    if not directory.exists():
        return []
    files = sorted(directory.glob(pattern), key=lambda p: p.stat().st_mtime, reverse=True)[:limit]
    return [
        {
            "name": path.name,
            "path": _rel(path),
            "mtime": datetime.fromtimestamp(path.stat().st_mtime, JST).isoformat(timespec="seconds"),
        }
        for path in files
        if path.is_file()
    ]


def _redact_log_line(line: str) -> str:
    redacted = line.rstrip()
    for pattern in SENSITIVE_PATTERNS:
        if pattern.pattern.startswith('(?i)("'):
            redacted = pattern.sub(r"\1[REDACTED]\3", redacted)
        else:
            redacted = pattern.sub(r"\1\2[REDACTED]", redacted)
    return redacted


def _allowed_log_roots() -> list[Path]:
    return [
        ROOT / "logs",
        Path.home() / "Library" / "Logs" / "UnariSagiOperator",
    ]


def _safe_log_path(raw_path: str) -> Path | None:
    text = raw_path.strip().strip("'\"")
    if not text:
        return None
    path = Path(text).expanduser()
    if not path.is_absolute():
        path = ROOT / path
    try:
        resolved = path.resolve(strict=False)
    except OSError:
        return None
    if resolved.suffix != ".log":
        return None
    for root in _allowed_log_roots():
        try:
            resolved.relative_to(root.resolve(strict=False))
            return resolved
        except ValueError:
            continue
    return None


def _extract_log_paths(log_lines: list[str]) -> list[Path]:
    paths: list[Path] = []
    seen: set[Path] = set()
    for line in log_lines:
        for pattern in LOG_PATH_PATTERNS:
            for match in pattern.finditer(line):
                path = _safe_log_path(match.group(1))
                if path and path not in seen:
                    seen.add(path)
                    paths.append(path)
    return paths


def _latest_support_logs(limit: int = 2) -> list[Path]:
    candidates: list[Path] = []
    for directory, patterns in [
        (ROOT / "logs", ["avd_keepalive_*.log", "mitmdump_keepalive_*.log"]),
        (Path.home() / "Library" / "Logs" / "UnariSagiOperator", ["launcher_*.log", "app_*.log"]),
    ]:
        if not directory.exists():
            continue
        for pattern in patterns:
            candidates.extend(path for path in directory.glob(pattern) if path.is_file())
    return sorted(candidates, key=lambda path: path.stat().st_mtime, reverse=True)[:limit]


def _tail_log_file(path: Path, *, lines: int = RELATED_LOG_TAIL_LINES) -> list[str]:
    try:
        with path.open("rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - RELATED_LOG_TAIL_BYTES))
            text = f.read().decode("utf-8", errors="replace")
    except OSError as e:
        return [f"(ログを読めません: {type(e).__name__}: {e})"]
    return [_redact_log_line(line) for line in text.splitlines()[-lines:]]


def _append_related_log_tails(job_id: str, log_snapshot: list[str]) -> None:
    paths: list[Path] = []
    seen: set[Path] = set()
    for path in [*_extract_log_paths(log_snapshot), *_latest_support_logs(limit=2)]:
        safe_path = _safe_log_path(str(path))
        if not safe_path or safe_path in seen or not safe_path.exists() or not safe_path.is_file():
            continue
        seen.add(safe_path)
        paths.append(safe_path)
        if len(paths) >= RELATED_LOG_LIMIT:
            break
    if not paths:
        return
    _append(job_id, "")
    _append(job_id, "== 関連する内部ログの末尾")
    for path in paths:
        _append(job_id, f"-- {_rel(path)}")
        tail = _tail_log_file(path)
        if tail:
            for line in tail:
                _append(job_id, line)
        else:
            _append(job_id, "(ログは空です)")


def collect_capture_status() -> dict[str, Any]:
    jobs = list_jobs(limit=5)
    latest_job = jobs[0] if jobs else None
    return {
        "updated_at": _now(),
        "wifi": _quick_run(["networksetup", "-getairportnetwork", "en0"], timeout=6),
        "network": _quick_run(["scutil", "--nwi"], timeout=6),
        "adb": _quick_run([ADB, "devices", "-l"], timeout=10),
        "infra": _quick_run(["bash", "scripts/ensure_capture_infra.sh", "--status"], timeout=45),
        "sessions": _quick_run([PYTHON, "scripts/strong_session_pool.py", "--list"], timeout=60),
        "captures": _latest_files(ROOT / "captures", "*.json"),
        "session_files": _latest_files(ROOT / "sessions", "*.json"),
        "latest_job": latest_job,
        "jobs": jobs,
    }


def _new_job(label: str, commands: list[dict[str, Any]], *, success_next_action: str = "") -> dict[str, Any]:
    job_id = uuid.uuid4().hex[:12]
    job = {
        "id": job_id,
        "label": label,
        "status": "running",
        "started_at": _now(),
        "finished_at": None,
        "returncode": None,
        "outcome": "running",
        "next_action": "実行中です。このまま完了まで待ってください。",
        "success_next_action": success_next_action,
        "current_step": None,
        "current_step_index": 0,
        "total_steps": len(commands),
        "log": [],
        "commands": [
            {"name": step["name"], "cmd": shlex.join(step["cmd"])}
            for step in commands
        ],
    }
    with _LOCK:
        _JOBS[job_id] = job
    thread = threading.Thread(target=_run_job, args=(job_id, commands), daemon=True)
    thread.start()
    return get_job(job_id) or job


def _classify_result(returncode: int, log: list[str], *, success_next_action: str = "") -> dict[str, str]:
    joined = "\n".join(log[-120:])
    joined_lower = joined.lower()
    if returncode == 0:
        return {
            "outcome": "succeeded",
            "next_action": success_next_action or "完了しました。必要なら結果とシート書き戻しを確認してください。",
        }
    if "NEEDS_SUPPLEMENT" in joined:
        return {
            "outcome": "needs_supplement",
            "next_action": "今の強session本数では対象件数を処理できません。強session補充で追加sessionを作成してください。結果CSVはまだ作られていないので、追加後は同じシートで「① まず件数を確認」を押し直してください。",
        }
    if returncode == 5 and (
        "LoginRequired" in joined
        or "ChallengeRequired" in joined
        or "ChallengeUnknownStep" in joined
        or "AUTH ERROR" in joined
        or "usernameinfo endpoint unavailable" in joined
    ):
        return {
            "outcome": "needs_supplement",
            "next_action": "使用中の強sessionがLoginRequired/Challenge系で使えなくなりました。自動再ログインはしません。強session補充で別のチェック用アカウントを追加し、入力CSVと結果CSVを残したまま「続きから再開」を押してください。",
        }
    if returncode == 5 and (
        "ローテーション先なし" in joined
        or "全アカウント使い切り" in joined
        or "1日上限" in joined
        or "バッチサイズ" in joined
        or "50件" in joined
    ):
        return {
            "outcome": "needs_supplement",
            "next_action": "50件上限または使える強session不足で止まりました。強session補充で追加sessionを作成し、入力CSVと結果CSVを残したまま「続きから再開」を押してください。",
        }
    if (
        returncode == 5
        or "強sessionが全滅" in joined
        or "強sessionが見つかりません" in joined
        or "strong session" in joined.lower() and "not found" in joined.lower()
    ):
        return {
            "outcome": "needs_supplement",
            "next_action": "強sessionが足りません。強session補充で追加sessionを作成してください。結果CSVがある場合は「途中から再開」、まだ無い場合は「① まず件数を確認」を押し直してください。",
        }
    if (
        returncode == 4
        or "login_input_error" in joined
        or "username_or_password_rejected" in joined
    ):
        return {
            "outcome": "manual_needed",
            "next_action": "username/passwordの入力ミス、またはInstagram側の認証拒否です。自動再試行は止めています。入力内容と対象アカウントを確認してから、必要な場合だけ「sessionを1本作る」をやり直してください。",
        }
    if (
        "manual_login_required" in joined
        or "manual_login_mode" in joined
        or "manual_login_timeout" in joined
        or "challenge_or_2fa" in joined
        or "two_step_verification" in joined
        or "check your email" in joined_lower
    ):
        return {
            "outcome": "manual_needed",
            "next_action": "Android画面でInstagramへ手動ログイン、またはメール確認/2FAを完了してください。時間切れになった場合は、認証完了後に「sessionを1本作る」を再実行してください。認証コードやパスワードは録画に映さないでください。",
        }
    if "FileNotFoundError" in joined and "config/accounts.json" in joined:
        return {
            "outcome": "failed",
            "next_action": "ローカル設定ファイルが無い状態で止まりました。最新版では自動作成されます。最新版DMGへ入れ替えて、強session補充の「取り込みだけやり直す」または「sessionを1本作る」を再実行してください。",
        }
    if (
        "Google Sheets連携設定がありません" in joined
        or "Google Sheets認証に必要なOAuth設定ファイルがありません" in joined
        or "Apps Script連携URLが未設定" in joined
    ):
        return {
            "outcome": "manual_needed",
            "next_action": "Google Sheets接続設定が未完了の古いアプリで動いています。最新版のUnari Sagi Operatorを開き直してください。最新版でも出る場合は、管理者にSheets連携設定済みDMGの再配布を依頼してください。",
        }
    if (
        "Google Sheetsの認証または権限" in joined
        or "Google Sheetsが見つからない、または権限がありません" in joined
        or "SheetsBridgeError" in joined
        or "permission" in joined_lower
        or "forbidden" in joined_lower
        or "403" in joined
        or "not authenticated" in joined_lower
        or "no credentials" in joined_lower
        or "auth" in joined_lower and "google" in joined_lower
    ):
        return {
            "outcome": "manual_needed",
            "next_action": "Google Sheetsの認証または権限で止まっています。対象シートをSheets連携用アカウントに共有してください。Google直接認証で使う場合は「Google Sheets接続設定」を押してログインしてください。結果CSVができている場合は消さずに、権限付与後に書き戻し確認から再開できます。",
        }
    if (
        "タブ" in joined and "見つかりません" in joined
        or "Unable to parse range" in joined
        or "対象アカウントが0件" in joined
    ):
        return {
            "outcome": "manual_needed",
            "next_action": "シートURL、タブ名、A列のアカウントIDを確認してください。タブ名は画面下のシート名と完全一致させます。例: 7_3",
        }
    network_or_dns_failed = (
        "network_dns_or_502" in joined
        or "nodename nor servname provided" in joined
        or "Name or service not known" in joined
        or "Temporary failure in name resolution" in joined
        or "502 Bad Gateway" in joined
        or "error establishing server connection" in joined
        or "MacでInstagram向けDNS解決に失敗" in joined
    )
    tls_or_pinning_failed = (
        "tls_or_pinning" in joined
        or "Client TLS handshake failed" in joined
        or "certificate pinning" in joined_lower
        or "pinning error" in joined_lower
        or "Unexpected TLS failure" in joined
        or "Unrecognized TLS error" in joined
        or "frida_unpinning_not_ready" in joined
        or "Frida unpinning hooks not ready" in joined
        or "Unpinning fallback auto-patcher installation failed" in joined
    )
    if network_or_dns_failed and tls_or_pinning_failed:
        return {
            "outcome": "failed",
            "next_action": "Instagramへの通信がDNS/502で失敗し、その後TLS/pinningでも失敗しています。iPhoneテザリング/Wi-Fiをつなぎ直し、初回セットアップの「通信用設定」を押してから、強session補充の「sessionを1本作る」を再実行してください。",
        }
    if network_or_dns_failed:
        return {
            "outcome": "failed",
            "next_action": "Mac側のネットワーク/DNSがInstagram接続先を解決できていません。iPhoneテザリング/Wi-Fiをつなぎ直してから、初回セットアップの「通信用設定」→強session補充の「sessionを1本作る」をやり直してください。",
        }
    if tls_or_pinning_failed:
        return {
            "outcome": "failed",
            "next_action": "通信補助設定または証明書設定が効いていません。初回セットアップの「通信用設定」を押し直してから、強session補充の「sessionを1本作る」を再実行してください。繰り返す場合はログ末尾を藤巻へ渡してください。",
        }
    if (
        returncode == 3
        or "IGFlaggedError" in joined
        or "Unable to log in" in joined
        or "unexpected error occurred" in joined_lower
        or "Try again later" in joined
        or "ig_login_rejected" in joined
        or "challenge detected" in joined_lower
        or "ChallengeRequired" in joined
        or "two-factor" in joined_lower
        or "2fa" in joined_lower
    ):
        if (
            "IGFlaggedError" in joined
            or "Unable to log in" in joined
            or "unexpected error occurred" in joined_lower
            or "Try again later" in joined
            or "ig_login_rejected" in joined
        ):
            return {
                "outcome": "manual_needed",
                "next_action": "Instagramのログイン通信が拒否されています。ログイン連打は止めて、OKを押し、初回セットアップの「通信用設定」→強session補充の「sessionを1本作る」をやり直してください。繰り返す場合は別のチェック用アカウントかテザリング回線に切り替えてください。",
            }
        return {
            "outcome": "manual_needed",
            "next_action": "Instagram側で手動対応が必要です。AVD画面を確認し、challengeや2FAを分かる人に渡してください。",
        }
    if (
        "feed not reached" in joined
        or "feed diagnosis" in joined
        or "login_screen_still_visible" in joined
        or "username_or_password_rejected" in joined
    ):
        return {
            "outcome": "manual_needed",
            "next_action": "Instagramログインが完了していません。AVD画面を確認してください。ログイン画面のままならusername/passwordを確認し、認証コード画面なら録画を止めて手動対応、Unable to log inなら連打せず別アカウントかテザリング回線に切り替えてください。",
        }
    if (
        "InstagramのAPK/APKM/XAPKが見つかりません" in joined
        or ("InstagramのAPK" in joined and "見つかりません" in joined)
    ):
        return {
            "outcome": "manual_needed",
            "next_action": "Instagramが配布DMGに同梱されていません。管理者からInstagram同梱済みの最新版DMGを受け取り直し、初回セットアップの「Instagram導入」を押してください。Play Storeなしは正常です。",
        }
    if (
        "frida-server 起動失敗" in joined
        or "frida-server 入れ直し失敗" in joined
        or "frida-server not running" in joined
        or "setup_ig_capture_device.sh frida" in joined
    ):
        return {
            "outcome": "failed",
            "next_action": "通信補助設定が未完了です。初回セットアップタブで「通信用設定」を押してから、強session補充をやり直してください。",
        }
    if (
        "address already in use" in joined.lower()
        or "port 8080" in joined and "使用中" in joined
        or "port 8080" in joined and "別プロセス" in joined
        or "port 8080" in joined and "別ユーザー" in joined
    ):
        return {
            "outcome": "failed",
            "next_action": "Macの8080番を別アプリまたは別ユーザーのUnari Sagi Operatorが使っています。別ユーザー側を終了するか、Macを再起動してから、通信用設定をやり直してください。",
        }
    if (
        "mitmproxy CA が生成されていません" in joined
        or "mitmproxy-ca-cert.pem が無い" in joined
        or "mitmproxy CAがありません" in joined
    ):
        return {
            "outcome": "failed",
            "next_action": "通信の証明書がまだ作られていません。初回セットアップの「通信用設定」をもう一度押してください。繰り返す場合はMacを再起動してから同じ操作をしてください。",
        }
    if (
        "AVDからcapture proxyへ接続できません" in joined
        or "capture proxy設定が違います" in joined
        or "capture proxy mismatch" in joined
        or "Network is unreachable" in joined
    ):
        return {
            "outcome": "failed",
            "next_action": "Android画面の通信先が古いか、Mac内の受け口に届いていません。初回セットアップの「通信用設定」を押し直してから、強session補充の「sessionを1本作る」を再実行してください。",
        }
    if returncode == 124 or "timeout after" in joined:
        return {
            "outcome": "failed",
            "next_action": "処理が時間切れです。画面ログ末尾を藤巻さんか担当者に共有してください。",
        }
    return {
        "outcome": "failed",
        "next_action": "失敗しました。画面ログ末尾を藤巻さんか担当者に共有してください。",
    }


def _append(job_id: str, line: str) -> None:
    with _LOCK:
        job = _JOBS.get(job_id)
        if not job:
            return
        job["log"].append(line.rstrip())
        if len(job["log"]) > MAX_LOG_LINES:
            job["log"] = job["log"][-MAX_LOG_LINES:]


def _set_job(job_id: str, **updates: Any) -> None:
    with _LOCK:
        job = _JOBS.get(job_id)
        if job:
            job.update(updates)


def _run_job(job_id: str, commands: list[dict[str, Any]]) -> None:
    final_code = 0
    for index, step in enumerate(commands, start=1):
        name = step["name"]
        cmd = step["cmd"]
        timeout = int(step.get("timeout", 900))
        env = os.environ.copy()
        env.update(step.get("env", {}))
        _set_job(job_id, current_step=name, current_step_index=index, total_steps=len(commands))
        _append(job_id, f"== [{index}/{len(commands)}] {name}")
        _append(job_id, f"$ {shlex.join(cmd)}")
        start = time.time()
        try:
            proc = subprocess.Popen(
                cmd,
                cwd=ROOT,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
            assert proc.stdout is not None
            while True:
                line = proc.stdout.readline()
                if line:
                    _append(job_id, line)
                if proc.poll() is not None:
                    rest = proc.stdout.read()
                    if rest:
                        for rest_line in rest.splitlines():
                            _append(job_id, rest_line)
                    break
                if time.time() - start > timeout:
                    proc.kill()
                    final_code = 124
                    _append(job_id, f"✗ timeout after {timeout}s")
                    break
            if final_code == 124:
                break
            code = proc.returncode or 0
            if code != 0:
                final_code = code
                _append(job_id, f"✗ step failed: {name} (exit={code})")
                break
            _append(job_id, f"✓ step ok: {name}")
        except Exception as e:
            final_code = 1
            _append(job_id, f"✗ {type(e).__name__}: {e}")
            break

    with _LOCK:
        log_tail = list((_JOBS.get(job_id) or {}).get("log", []))
    if final_code != 0:
        _append_related_log_tails(job_id, log_tail)
        with _LOCK:
            log_tail = list((_JOBS.get(job_id) or {}).get("log", []))
    success_next_action = ""
    with _LOCK:
        success_next_action = str((_JOBS.get(job_id) or {}).get("success_next_action") or "")
    classification = _classify_result(final_code, log_tail, success_next_action=success_next_action)
    _set_job(
        job_id,
        status="succeeded" if final_code == 0 else "failed",
        finished_at=_now(),
        returncode=final_code,
        **classification,
        current_step=None,
        current_step_index=len(commands) if final_code == 0 else max(0, min(len(commands), index if "index" in locals() else 0)),
    )


def _active_job() -> dict[str, Any] | None:
    with _LOCK:
        for job in _JOBS.values():
            if job.get("status") == "running":
                return dict(job)
    return None


def start_infra_job() -> tuple[dict[str, Any] | None, str | None]:
    active = _active_job()
    if active:
        return None, f"実行中のジョブがあります: {active['label']}"
    return _new_job(
        "AVD/mitmdump/Frida 準備",
        [{"name": "AVD/mitmdump/Fridaを起動確認", "cmd": ["bash", "scripts/ensure_capture_infra.sh"], "timeout": 420}],
    ), None


def start_capture_all_job(
    *,
    username: str,
    confirm_tethering: bool,
    password: str = "",
    skip_accounts_check: bool = True,
    interval: int = 120,
    manual_login: bool = True,
) -> tuple[dict[str, Any] | None, str | None]:
    active = _active_job()
    if active:
        return None, f"実行中のジョブがあります: {active['label']}"
    username = username.strip()
    if not username:
        return None, "username を入力してください"
    if not manual_login and not password:
        return None, "password を入力してください。保存はせず、この実行だけに使います"
    if not confirm_tethering:
        return None, "iPhoneテザリング接続済みの確認にチェックしてください"
    interval = max(30, min(int(interval or 120), 600))
    capture_cmd = [
        PYTHON,
        "-u",
        "scripts/shin_capture_auto.py",
        "--username",
        username,
        "--interval",
        str(interval),
        "--manual-login-timeout",
        "900",
    ]
    if manual_login:
        capture_cmd.append("--manual-login")
    if skip_accounts_check:
        capture_cmd.append("--skip-accounts-check")
    env = {} if manual_login else {"SHIN_CAPTURE_PASSWORD": password}
    step_name = "AVDで手動ログイン→capture→import→verify" if manual_login else "AVDでIGログイン→capture→import→verify"
    return _new_job(
        f"強session作成: {username}",
        [
            {"name": "AVD/mitmdump/Fridaを起動確認", "cmd": ["bash", "scripts/ensure_capture_infra.sh"], "timeout": 420},
            {
                "name": step_name,
                "cmd": capture_cmd,
                "env": env,
                "timeout": 1800,
            },
        ],
    ), None


def start_import_latest_job(username: str) -> tuple[dict[str, Any] | None, str | None]:
    active = _active_job()
    if active:
        return None, f"実行中のジョブがあります: {active['label']}"
    username = username.strip()
    if not username:
        return None, "username を入力してください"
    latest = _latest_files(ROOT / "captures", "*.json", limit=1)
    if not latest:
        return None, "captures/*.json がありません"
    capture_path = latest[0]["path"]
    return _new_job(
        f"最新capture import: {username}",
        [
            {
                "name": "最新captureをsessionへ取り込み",
                "cmd": [PYTHON, "-u", "scripts/import_real_session.py", "--capture", capture_path, "--username", username, "--no-verify"],
                "timeout": 180,
            },
            {
                "name": "強session verify",
                "cmd": [PYTHON, "-u", "scripts/verify_captured_session.py", "--username", username, "--no-proxy"],
                "timeout": 120,
            },
        ],
    ), None


def start_verify_job(username: str) -> tuple[dict[str, Any] | None, str | None]:
    active = _active_job()
    if active:
        return None, f"実行中のジョブがあります: {active['label']}"
    username = username.strip()
    if not username:
        return None, "username を入力してください"
    return _new_job(
        f"強session verify: {username}",
        [{"name": "強session verify", "cmd": [PYTHON, "-u", "scripts/verify_captured_session.py", "--username", username, "--no-proxy"], "timeout": 120}],
    ), None


def get_job(job_id: str) -> dict[str, Any] | None:
    with _LOCK:
        job = _JOBS.get(job_id)
        if not job:
            return None
        return dict(job)


def list_jobs(limit: int = 10) -> list[dict[str, Any]]:
    with _LOCK:
        jobs = sorted(_JOBS.values(), key=lambda j: j.get("started_at") or "", reverse=True)
        return [dict(job) for job in jobs[:limit]]
