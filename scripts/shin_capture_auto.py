#!/usr/bin/env python3
"""
AVDに対してIGアプリの自動ログイン→強session capture→importを一気に回す。

前提:
  - ig_capture AVD 起動済み (-writable-system)
  - mitmdump (scripts/ig_mitm_capture.py addon) が port 8080 で listen
  - frida-server が AVD 上で起動済み
  - tools/config.js, android-unpinning-httptoolkit.js, android-unpinning-fallback.js 配置済み

使い方:
  python3 scripts/shin_capture_auto.py --username s__o__ra__0903
  python3 scripts/shin_capture_auto.py --username s__o__ra__0903 --password OVERRIDE
  python3 scripts/shin_capture_auto.py --username s__o__ra__0903 --manual-login
  python3 scripts/shin_capture_auto.py --usernames u1,u2,u3   # 複数連続

挙動:
  各アカウントについて
    1) IGアプリ data wipe
    2) Frida spawnでIGを起動（SSL unpinning込み）
    3) uiautomator2でログイン画面を検出・入力・ログイン実行
       --manual-login 時は AVD 画面で人間がログインする
    4) ポップアップ類を閉じる
    5) Feed到達後 pull-to-refresh
    6) captures/ 配下に新ファイル出現を待機
    7) import_real_session.py で session 取り込み
    8) 検証 (verify_captured_session.py 風に user_info probe)
  challenge/2FA/メール確認は手動対応待ちに切り替える。
  入力ミスやIG拒否など、続行するとアカウントを消耗する失敗はその場で停止する。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ADB = os.environ.get("ADB", f"{os.environ.get('HOME')}/Library/Android/sdk/platform-tools/adb")
FRIDA = str(PROJECT_ROOT / "venv" / "bin" / "frida")
PYTHON = str(PROJECT_ROOT / "venv" / "bin" / "python")
IMPORT_SCRIPT = str(PROJECT_ROOT / "scripts" / "import_real_session.py")
VERIFY_SCRIPT = str(PROJECT_ROOT / "scripts" / "verify_captured_session.py")
CAPTURES_DIR = PROJECT_ROOT / "captures"
CONFIG_PATH = PROJECT_ROOT / "config" / "accounts.json"
TOOLS_DIR = PROJECT_ROOT / "tools"
MITM_CA_SRC = Path.home() / ".mitmproxy" / "mitmproxy-ca-cert.pem"

IG_PKG = "com.instagram.android"

# IG アプリのリソースIDは version で揺れるので、複数の候補を試す
USERNAME_SELECTORS = [
    {"resourceId": f"{IG_PKG}:id/login_username"},
    {"className": "android.widget.EditText", "descriptionContains": "Username"},
    {"className": "android.widget.EditText", "textContains": "Phone number, username"},
    {"className": "android.widget.EditText", "instance": 0},
]
PASSWORD_SELECTORS = [
    {"resourceId": f"{IG_PKG}:id/password"},
    {"className": "android.widget.EditText", "descriptionContains": "Password"},
    {"className": "android.widget.EditText", "instance": 1},
]
LOGIN_BUTTON_SELECTORS = [
    {"description": "Log in"},
    {"text": "Log in"},
    {"text": "ログイン"},
    {"resourceId": f"{IG_PKG}:id/button_text"},
]

# ログイン後に出がちなモーダル/画面と「閉じる」ボタン相当
DISMISS_TEXTS = [
    "Not now", "Skip", "後で", "Skip for now", "Cancel",
    "Maybe later", "スキップ", "今はしない", "No, thanks",
    "Save", "Save info",  # "save login info" はSaveでOK
    "Got it", "OK", "Continue", "わかりました", "次へ",  # post-login modal
]

# IG側のAVD/IP flag っぽいダイアログ検出用（UI dump の文字列マッチ）
IG_FLAG_TEXTS = [
    "Unable to log in",
    "unable to log in",
    "unexpected error occurred",
    "Unexpected error occurred",
    "Try again later",
    "try again later",
    "ログインできません",
    "予期しないエラー",
    "しばらくしてからもう一度",
]
CHALLENGE_TEXTS = [
    "security code",
    "confirmation code",
    "check your email",
    "verify your account",
    "two-factor",
    "認証コード",
    "確認コード",
]
LOGIN_REJECT_TEXTS = [
    "incorrect password",
    "wrong password",
    "password you entered",
    "couldn't find your account",
    "try again",
    "パスワードが正しくありません",
    "アカウントが見つかりません",
]
FRIDA_READY_MARKERS = [
    "== Certificate unpinning completed ==",
    "== Unpinning fallback auto-patcher installed ==",
]
NETWORK_FAILURE_MARKERS = [
    "nodename nor servname provided",
    "Name or service not known",
    "Temporary failure in name resolution",
    "502 Bad Gateway",
    "error establishing server connection",
]
TLS_FAILURE_MARKERS = [
    "Client TLS handshake failed",
    "certificate pinning",
    "pinning error",
    "Unexpected TLS failure",
    "Unrecognized TLS error",
    "Unpinning fallback auto-patcher installation failed",
]


class IGFlaggedError(RuntimeError):
    """IG側で AVD/IP flag が立っている疑い（refresh側で即中断・クールダウンするため）"""


class CaptureStop(RuntimeError):
    """自動継続せず、ユーザーに渡して止めるべき状態。"""

    def __init__(self, message: str, exit_code: int = 3) -> None:
        super().__init__(message)
        self.exit_code = exit_code


class ManualLoginRequired(CaptureStop):
    """メール確認/2FAなど、AVD画面で人間の操作が必要。"""


class LoginInputError(CaptureStop):
    """username/password不一致など、再実行前に入力確認が必要。"""

    def __init__(self, message: str) -> None:
        super().__init__(message, exit_code=4)


def detect_ig_flag_dialog(d) -> str | None:
    """UI dump からIG側フラグっぽい文言を探し、見つかった文字列を返す（なければNone）。"""
    try:
        xml = d.dump_hierarchy()
    except Exception:
        return None
    low = xml.lower()
    for t in IG_FLAG_TEXTS:
        if t.lower() in low:
            return t
    return None


def _selector_exists(d, selectors: list[dict]) -> bool:
    for sel in selectors:
        try:
            if d(**sel).exists:
                return True
        except Exception:
            pass
    return False


def diagnose_non_feed_screen(d) -> str:
    """フィード未到達時に、次に人が見るべき画面状態を短く返す。"""
    try:
        xml = d.dump_hierarchy()
    except Exception as e:
        return f"ui_dump_failed:{type(e).__name__}"
    low = xml.lower()
    for text in IG_FLAG_TEXTS:
        if text.lower() in low:
            return f"ig_login_rejected:{text}"
    for text in CHALLENGE_TEXTS:
        if text.lower() in low:
            return f"challenge_or_2fa:{text}"
    for text in LOGIN_REJECT_TEXTS:
        if text.lower() in low:
            return f"username_or_password_rejected:{text}"
    if _selector_exists(d, USERNAME_SELECTORS) or _selector_exists(d, PASSWORD_SELECTORS) or _selector_exists(d, LOGIN_BUTTON_SELECTORS):
        return "login_screen_still_visible"
    return "unknown_screen"


def _is_challenge_diagnosis(diagnosis: str) -> bool:
    return diagnosis.startswith("challenge_or_2fa:")


def _is_login_reject_diagnosis(diagnosis: str) -> bool:
    return diagnosis.startswith("username_or_password_rejected:")


def _is_ig_rejected_diagnosis(diagnosis: str) -> bool:
    return diagnosis.startswith("ig_login_rejected:")


@dataclass
class Prereqs:
    adb_device: str
    mitmdump_up: bool
    frida_server_up: bool
    ok: bool
    reason: str = ""


def _mitmdump_listening(port: str) -> bool:
    try:
        lsof = subprocess.run(
            ["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN"],
            capture_output=True,
            text=True,
            timeout=5,
        ).stdout
    except Exception:
        return False
    for line in lsof.splitlines()[1:]:
        parts = line.split()
        if len(parts) < 2:
            continue
        pid = parts[1]
        try:
            args = subprocess.run(["ps", "-p", pid, "-o", "args="], capture_output=True, text=True, timeout=5).stdout
        except Exception:
            args = ""
        if "ig_mitm_capture.py" in args:
            return True
    return False


def _current_proxy_port(device: str) -> str:
    port = os.environ.get("IG_CAP_PORT", "8080")
    try:
        proxy = adb_shell(device, "settings get global http_proxy", timeout=10).stdout.strip()
    except Exception:
        return port
    if proxy and proxy not in {"null", ":0"} and ":" in proxy:
        maybe_port = proxy.rsplit(":", 1)[1]
        if maybe_port.isdigit():
            return maybe_port
    return port


def _current_proxy(device: str) -> str:
    try:
        return adb_shell(device, "settings get global http_proxy", timeout=10).stdout.strip()
    except Exception:
        return ""


def _expected_proxy_for_device(device: str) -> str | None:
    port = os.environ.get("IG_CAP_PORT", "8080")
    if device.startswith("emulator-"):
        return f"10.0.2.2:{port}"
    return None


def check_prereqs() -> Prereqs:
    try:
        out = subprocess.run([ADB, "devices"], capture_output=True, text=True, timeout=10).stdout
        devs = [l.split()[0] for l in out.splitlines() if "\tdevice" in l]
        if not devs:
            return Prereqs("", False, False, False, "no adb device")
        device = devs[0]
    except Exception as e:
        return Prereqs("", False, False, False, f"adb fail: {e}")

    expected_proxy = _expected_proxy_for_device(device)
    current_proxy = _current_proxy(device)
    if expected_proxy and current_proxy != expected_proxy:
        return Prereqs(
            device,
            False,
            False,
            False,
            f"capture proxy mismatch: current={current_proxy or 'empty'} expected={expected_proxy}. run scripts/ensure_capture_infra.sh",
        )

    proxy_port = _current_proxy_port(device)
    mitm_up = _mitmdump_listening(proxy_port)
    if not mitm_up:
        return Prereqs(device, False, False, False, f"mitmdump not listening on :{proxy_port}")

    try:
        ps = subprocess.run([ADB, "-s", device, "shell", "ps", "-A"],
                            capture_output=True, text=True, timeout=10).stdout
        frida_up = "frida-server" in ps
    except Exception:
        frida_up = False
    if not frida_up:
        return Prereqs(device, True, False, False, "frida-server not running on AVD")

    return Prereqs(device, True, True, True)


def adb_shell(device: str, cmd: str, timeout: int = 20) -> subprocess.CompletedProcess:
    return subprocess.run([ADB, "-s", device, "shell", cmd],
                          capture_output=True, text=True, timeout=timeout)


def sync_frida_config(device: str) -> None:
    """Frida native hook 用の CA と proxy 先を、現在の mitmproxy/AVD 設定に合わせる。"""
    config = TOOLS_DIR / "config.js"
    if not config.exists():
        raise RuntimeError(f"missing {config}")
    if not MITM_CA_SRC.exists():
        raise RuntimeError(f"missing {MITM_CA_SRC}; start mitmdump once to generate CA")

    proxy = adb_shell(device, "settings get global http_proxy").stdout.strip()
    if not proxy or proxy in {"null", ":0"} or ":" not in proxy:
        return
    host, port = proxy.rsplit(":", 1)
    if not host or not port.isdigit():
        return

    pem = MITM_CA_SRC.read_text(encoding="utf-8").strip()
    if not pem.startswith("-----BEGIN CERTIFICATE-----") or not pem.endswith("-----END CERTIFICATE-----"):
        raise RuntimeError(f"invalid mitmproxy CA PEM: {MITM_CA_SRC}")

    text = config.read_text(encoding="utf-8")
    updated, cert_count = re.subn(
        r"const CERT_PEM = `-----BEGIN CERTIFICATE-----.*?-----END CERTIFICATE-----`;",
        lambda _: f"const CERT_PEM = `{pem}`;",
        text,
        count=1,
        flags=re.S,
    )
    if cert_count != 1:
        raise RuntimeError("CERT_PEM block not found in tools/config.js")
    updated, host_count = re.subn(r"const PROXY_HOST = '[^']+';", f"const PROXY_HOST = '{host}';", updated, count=1)
    updated, port_count = re.subn(r"const PROXY_PORT = \d+;", f"const PROXY_PORT = {port};", updated, count=1)
    if host_count != 1 or port_count != 1:
        raise RuntimeError("PROXY_HOST/PROXY_PORT not found in tools/config.js")
    if updated != text:
        config.write_text(updated, encoding="utf-8")
        print(f"  [frida] synced tools/config.js CA and proxy to {host}:{port}")


def spawn_ig_via_frida(device: str, log_path: Path) -> subprocess.Popen:
    """Frida で IG を spawn。SSL unpinning 適用。"""
    sync_frida_config(device)
    config = TOOLS_DIR / "config.js"
    unpin = TOOLS_DIR / "android-unpinning-httptoolkit.js"
    fallback = TOOLS_DIR / "android-unpinning-fallback.js"
    for f in (config, unpin, fallback):
        if not f.exists():
            raise RuntimeError(f"missing {f}")
    f = open(log_path, "a")
    proc = subprocess.Popen(
        [FRIDA, "-U", "-f", IG_PKG,
         "-l", str(config), "-l", str(unpin), "-l", str(fallback)],
        stdin=subprocess.PIPE,
        stdout=f, stderr=subprocess.STDOUT,
        preexec_fn=os.setsid,  # プロセスグループで一括killしやすく
    )
    # stdin が閉じると Frida REPL が即終了し、SSL unpinning がログイン中に外れる。
    proc._frida_log_file = f  # type: ignore[attr-defined]
    return proc


def _read_log_tail(path: Path, max_bytes: int = 120_000) -> str:
    try:
        with path.open("rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - max_bytes))
            return f.read().decode("utf-8", errors="replace")
    except OSError:
        return ""


def _latest_mitmdump_log() -> Path | None:
    candidates = [p for p in (PROJECT_ROOT / "logs").glob("mitmdump_keepalive_*.log") if p.is_file()]
    if not candidates:
        return None
    return max(candidates, key=lambda path: path.stat().st_mtime)


def _has_any_marker(text: str, markers: list[str]) -> bool:
    low = text.lower()
    return any(marker.lower() in low for marker in markers)


def wait_for_frida_hooks(log_path: Path, timeout: float = 45.0) -> bool:
    """Frida側のunpin hookが入る前にログイン通信を投げないための待機。"""
    deadline = time.time() + timeout
    seen: set[str] = set()
    while time.time() < deadline:
        text = _read_log_tail(log_path)
        for marker in FRIDA_READY_MARKERS:
            if marker in text:
                seen.add(marker)
        if len(seen) == len(FRIDA_READY_MARKERS):
            print("  ✓ Frida unpinning hooks ready")
            return True
        if "Failed to spawn" in text or "unable to connect to remote frida-server" in text.lower():
            print("  ✗ frida spawn failed")
            print("\n".join(text.splitlines()[-20:]))
            return False
        time.sleep(1)
    text = _read_log_tail(log_path)
    print("  ✗ Frida unpinning hooks not ready")
    print("  ✗ frida readiness missing:", sorted(set(FRIDA_READY_MARKERS) - seen))
    if text:
        print("  --- frida log tail ---")
        print("\n".join(text.splitlines()[-30:]))
    return False


def diagnose_transport_logs(frida_log: Path) -> str:
    """feed未到達時に通信・pinning側の原因を優先して短く出す。"""
    frida_text = _read_log_tail(frida_log)
    mitm_log = _latest_mitmdump_log()
    mitm_text = _read_log_tail(mitm_log) if mitm_log else ""
    combined = "\n".join([frida_text, mitm_text])

    network_failed = _has_any_marker(combined, NETWORK_FAILURE_MARKERS)
    tls_failed = _has_any_marker(combined, TLS_FAILURE_MARKERS)
    frida_ready = all(marker in frida_text for marker in FRIDA_READY_MARKERS)

    parts: list[str] = []
    if network_failed:
        parts.append("network_dns_or_502")
    if tls_failed:
        parts.append("tls_or_pinning")
    if not frida_ready:
        parts.append("frida_unpinning_not_ready")
    if not parts:
        parts.append("no_transport_error_seen")
    if mitm_log:
        parts.append(f"mitm_log={mitm_log.name}")
    parts.append(f"frida_log={frida_log.name}")
    return ",".join(parts)


def kill_process_group(proc: subprocess.Popen) -> None:
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    except Exception:
        pass
    try:
        proc.stdin.close()  # type: ignore[union-attr]
    except Exception:
        pass
    try:
        proc._frida_log_file.close()  # type: ignore[attr-defined]
    except Exception:
        pass


def human_type(el, text: str, device: str | None = None) -> None:
    """フィールドをタップしてフォーカスを確保し、adb shell input text で入力。
    clear_text はフォーカスを外すことがあるので使わず、field を長押しで全選択 → 上書きする。
    """
    import random
    adb_device = device or "emulator-5554"

    # 1. フィールドをタップ (フォーカス確保)
    try:
        el.click()
    except Exception:
        pass
    time.sleep(random.uniform(0.3, 0.6))

    # 2. 既存テキストがあれば全選択して消す (KEYCODE_A with Ctrl + KEYCODE_DEL)
    #    Android では長押しで select all が出るのが標準。でもエミュレータでは keyevent CTRL_A が安定。
    try:
        subprocess.run([ADB, "-s", adb_device, "shell",
                        "input", "keyevent", "--longpress", "KEYCODE_DEL"],
                       check=False, timeout=5)
        for _ in range(30):  # 保険: 逐一 backspace
            subprocess.run([ADB, "-s", adb_device, "shell",
                            "input", "keyevent", "KEYCODE_DEL"],
                           check=False, timeout=3)
    except Exception:
        pass
    time.sleep(random.uniform(0.1, 0.3))

    # 3. タップし直してフォーカス再確保 (keyevent 後に外れてるケース対策)
    try:
        el.click()
    except Exception:
        pass
    time.sleep(random.uniform(0.2, 0.4))

    # 4. adb shell input text で入力 (記号エスケープ)
    escaped = text.replace("\\", "\\\\").replace(" ", "%s").replace("&", "\\&")
    escaped = escaped.replace("(", "\\(").replace(")", "\\)").replace("'", "\\'")
    try:
        subprocess.run(
            [ADB, "-s", adb_device, "shell", "input", "text", escaped],
            check=False, timeout=10,
        )
    except Exception:
        try:
            el.set_text(text)
        except Exception:
            pass
    time.sleep(random.uniform(0.4, 0.9))


def wait_for_element(d, selectors: list[dict], timeout: float = 30.0, poll: float = 1.0):
    """複数候補の selector を試して最初に見つかった要素を返す。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        for sel in selectors:
            try:
                el = d(**sel)
                if el.exists:
                    return el
            except Exception:
                pass
        time.sleep(poll)
    return None


def dismiss_popups(d, rounds: int = 3) -> None:
    """ログイン後に出がちなモーダルを片っ端から閉じる。"""
    for _ in range(rounds):
        closed = False
        for t in DISMISS_TEXTS:
            try:
                btn = d(text=t)
                if btn.exists:
                    btn.click()
                    time.sleep(1.5)
                    closed = True
            except Exception:
                pass
        if not closed:
            break


def wait_for_manual_login_completion(d, timeout: float = 600.0, *, initial_login: bool = False) -> bool:
    """Instagramログインやメール確認/2FAを人間がAVD画面で完了するまで待つ。"""
    if initial_login:
        print("  ⚠ manual_login_mode: AVD画面でInstagramへ手動ログインしてください")
        print("  ⚠ username/password/メール確認/2FAはAVD内のInstagramだけで入力してください")
    else:
        print("  ⚠ manual_login_required: Instagramがメール確認/2FAを要求しています")
        print("  ⚠ AVD画面でメール確認/認証コード入力を手動で完了してください")
    print("  ⚠ パスワードや認証コードが映る場合は録画を止めてください")
    print(f"  ... waiting manual login completion up to {int(timeout)}s")
    deadline = time.time() + timeout
    last_notice = 0.0
    last_diagnosis = ""
    while time.time() < deadline:
        if is_feed_visible(d):
            print("  ✓ manual login completed: feed visible")
            dismiss_popups(d, rounds=3)
            return True

        diagnosis = diagnose_non_feed_screen(d)
        if diagnosis != last_diagnosis:
            print(f"  ... manual login screen: {diagnosis}")
            last_diagnosis = diagnosis

        if _is_login_reject_diagnosis(diagnosis):
            raise LoginInputError(f"login_input_error:{diagnosis}")
        if _is_ig_rejected_diagnosis(diagnosis):
            raise IGFlaggedError(diagnosis)

        # challenge画面以外に進んだあと、保存確認などの軽いモーダルなら閉じる。
        if not _is_challenge_diagnosis(diagnosis):
            dismiss_popups(d, rounds=1)

        now = time.time()
        if now - last_notice >= 30:
            remaining = max(0, int(deadline - now))
            print(f"  ... still waiting manual login ({remaining}s left)")
            last_notice = now
        time.sleep(2)

    print("  ✗ manual_login_timeout: メール確認/2FAが時間内に完了しませんでした")
    return False


def is_feed_visible(d) -> bool:
    """ホームタブ or feed らしき要素があるか簡易判定。"""
    indicators = [
        {"resourceId": f"{IG_PKG}:id/feed_tab"},
        {"resourceId": f"{IG_PKG}:id/main_tab_bar"},
        {"resourceId": f"{IG_PKG}:id/action_bar_inbox_button"},
        {"description": "Home"},
        {"description": "ホーム"},
    ]
    for sel in indicators:
        try:
            if d(**sel).exists:
                return True
        except Exception:
            pass
    return False


def pull_to_refresh(d) -> None:
    w = d.info["displayWidth"]
    h = d.info["displayHeight"]
    # 画面中央上寄りから下へスワイプ
    d.swipe(w // 2, int(h * 0.3), w // 2, int(h * 0.85), 0.6)
    time.sleep(2)


def get_latest_capture(before: set[str]) -> Path | None:
    """before に無い最新のcaptureファイルを返す。"""
    current = {f.name for f in CAPTURES_DIR.glob("*.json")}
    new = current - before
    if not new:
        return None
    files = sorted((CAPTURES_DIR / n for n in new), key=lambda p: p.stat().st_mtime, reverse=True)
    return files[0]


def wait_for_new_capture(before: set[str], timeout: float = 60.0) -> Path | None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        new = get_latest_capture(before)
        if new:
            return new
        time.sleep(2)
    return None


def load_password_for(username: str, override: str | None) -> str:
    if override:
        return override
    env_pw = os.environ.get("SHIN_CAPTURE_PASSWORD")
    if env_pw:
        return env_pw
    cfg = json.load(open(CONFIG_PATH))
    return cfg.get("password", "")


def import_session(capture: Path, username: str) -> bool:
    """import_real_session.py に委譲。

    Operatorではusernameを画面で指定済みなので、import時のAPI resolveは不要。
    検証は直後の verify_captured_session.py --no-proxy に一本化し、
    import段階でcaptureをfailedへ移動しない。
    """
    cmd = [PYTHON, IMPORT_SCRIPT, "--capture", str(capture), "--username", username, "--no-verify"]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    print(r.stdout[-600:])
    if r.returncode == 0 and "1 ok" in r.stdout:
        return True
    return False


def verify_session(username: str) -> bool:
    cmd = [PYTHON, VERIFY_SCRIPT, "--username", username, "--no-proxy"]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    print(r.stdout.strip())
    return f"{username},OK" in r.stdout


def process_account(
    d,
    device: str,
    username: str,
    password: str | None,
    log_dir: Path,
    manual_login_timeout: float = 600.0,
    manual_login: bool = False,
) -> bool:
    print(f"\n=== {username} ===")
    ts = time.strftime("%Y%m%d_%H%M%S")
    frida_log = log_dir / f"frida_{username}_{ts}.log"

    # 1) IG wipe
    print("  [1/8] pm clear com.instagram.android")
    adb_shell(device, f"pm clear {IG_PKG}")
    time.sleep(2)
    before_captures = {f.name for f in CAPTURES_DIR.glob("*.json")}

    # 2) frida spawn
    print(f"  [2/8] frida spawn IG → {frida_log}")
    proc = spawn_ig_via_frida(device, frida_log)

    try:
        if not wait_for_frida_hooks(frida_log):
            return False

        import uiautomator2 as u2
        d = u2.connect(device)

        if manual_login:
            # 3) 人間がAVD画面でログインする。2FA/メール認証も同じ待機で扱う。
            print("  [3/8] manual login in AVD")
            if not wait_for_manual_login_completion(d, timeout=manual_login_timeout, initial_login=True):
                diagnosis = diagnose_non_feed_screen(d)
                raise ManualLoginRequired(f"manual_login_timeout:{diagnosis}", exit_code=3)
        else:
            # 3) login screen 待ち
            print("  [3/8] waiting for username field...")
            user_el = wait_for_element(d, USERNAME_SELECTORS, timeout=45)
            if not user_el:
                hit = detect_ig_flag_dialog(d)
                if hit:
                    print(f"  ⚠ IG flag dialog detected in UI dump: '{hit}'")
                    raise IGFlaggedError(hit)
                print("  ✗ username field not found")
                return False
            human_type(user_el, username, device)
            time.sleep(1)

            pw_el = wait_for_element(d, PASSWORD_SELECTORS, timeout=10)
            if not pw_el:
                hit = detect_ig_flag_dialog(d)
                if hit:
                    print(f"  ⚠ IG flag dialog detected in UI dump: '{hit}'")
                    raise IGFlaggedError(hit)
                print("  ✗ password field not found")
                return False
            human_type(pw_el, password or "", device)
            time.sleep(1)

            # 4) login tap
            print("  [4/8] tap login")
            login_btn = wait_for_element(d, LOGIN_BUTTON_SELECTORS, timeout=10)
            if not login_btn:
                hit = detect_ig_flag_dialog(d)
                if hit:
                    print(f"  ⚠ IG flag dialog detected in UI dump: '{hit}'")
                    raise IGFlaggedError(hit)
                print("  ✗ login button not found")
                return False
            login_btn.click()
            time.sleep(8)  # ログイン処理 + 遷移待ち

            # 5) popup / manual login 処理
            print("  [5/8] dismiss popups / manual login check")
            post_login_diagnosis = diagnose_non_feed_screen(d)
            if _is_challenge_diagnosis(post_login_diagnosis):
                print(f"  ⚠ feed diagnosis: {post_login_diagnosis}")
                if not wait_for_manual_login_completion(d, timeout=manual_login_timeout):
                    raise ManualLoginRequired(f"manual_login_timeout:{post_login_diagnosis}", exit_code=3)
            elif _is_login_reject_diagnosis(post_login_diagnosis):
                print(f"  ✗ feed diagnosis: {post_login_diagnosis}")
                raise LoginInputError(f"login_input_error:{post_login_diagnosis}")
            elif _is_ig_rejected_diagnosis(post_login_diagnosis):
                print(f"  ⚠ feed diagnosis: {post_login_diagnosis}")
                raise IGFlaggedError(post_login_diagnosis)
            else:
                dismiss_popups(d)

        # 6) feed 到達確認
        print("  [6/8] waiting for feed")
        feed_deadline = time.time() + 60
        while time.time() < feed_deadline:
            if is_feed_visible(d):
                break
            dismiss_popups(d, rounds=1)
            time.sleep(2)
        else:
            print("  ✗ feed not reached (possibly still in post-login modals)")
            diagnosis = diagnose_non_feed_screen(d)
            print(f"  ✗ feed diagnosis: {diagnosis}")
            if _is_challenge_diagnosis(diagnosis):
                if not wait_for_manual_login_completion(d, timeout=manual_login_timeout):
                    raise ManualLoginRequired(f"manual_login_timeout:{diagnosis}", exit_code=3)
            elif _is_login_reject_diagnosis(diagnosis):
                raise LoginInputError(f"login_input_error:{diagnosis}")
            elif _is_ig_rejected_diagnosis(diagnosis):
                raise IGFlaggedError(diagnosis)
            else:
                transport_diagnosis = diagnose_transport_logs(frida_log)
                print(f"  ✗ transport diagnosis: {transport_diagnosis}")
                hit = detect_ig_flag_dialog(d)
                if hit:
                    print(f"  ⚠ IG flag dialog detected in UI dump: '{hit}'")
                    raise IGFlaggedError(hit)
                return False
            transport_diagnosis = diagnose_transport_logs(frida_log)
            print(f"  ✓ manual login resumed; transport diagnosis: {transport_diagnosis}")

        # 7) pull-to-refresh + capture 待ち
        print("  [7/8] pull-to-refresh and wait for capture")
        for _ in range(3):
            pull_to_refresh(d)
            new = wait_for_new_capture(before_captures, timeout=20)
            if new:
                print(f"  ✓ capture: {new.name}")
                break
        else:
            print("  ✗ no capture file appeared after 3 pull-to-refresh")
            return False

        # 8) import + verify
        print("  [8/8] import + verify")
        if not import_session(new, username):
            print("  ✗ import failed")
            return False
        if not verify_session(username):
            print("  ⚠ verify NG (session saved but probe failed — might still work in api_warning_check)")
        else:
            print("  ✓ verify OK")

        return True

    finally:
        # frida client を止める（AVD側のIGは残るが次で pm clear する）
        kill_process_group(proc)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--username", help="1アカウント処理")
    ap.add_argument("--usernames", help="カンマ区切りで複数処理")
    ap.add_argument("--password", help="パスワード上書き (通常は accounts.json の共通値)")
    ap.add_argument("--interval", type=int, default=120,
                    help="次アカウントまでの待機秒 (rate limit 回避, default 120)")
    ap.add_argument("--dry-run", action="store_true",
                    help="prereqs確認と対象検証のみ、IG操作はしない")
    ap.add_argument("--skip-accounts-check", action="store_true",
                    help="accounts.json に存在しない username でも強行")
    ap.add_argument("--continue-on-error", action="store_true",
                    help="複数アカウント時、失敗しても次へ進む（通常は使わない）")
    ap.add_argument("--manual-login", action="store_true",
                    help="username/password入力とログインボタン押下を自動化せず、AVD画面で手動ログインする")
    ap.add_argument("--manual-login-timeout", type=int, default=600,
                    help="ログイン/メール確認/2FAをAVD画面で手動対応する待機秒数 (default 600)")
    args = ap.parse_args()

    targets: list[str] = []
    if args.username:
        targets.append(args.username)
    if args.usernames:
        targets.extend([x.strip() for x in args.usernames.split(",") if x.strip()])
    if not targets:
        ap.error("--username or --usernames required")

    # accounts.json に存在するか事前検証
    if not args.skip_accounts_check:
        cfg = json.load(open(CONFIG_PATH))
        known = {a["username"] for a in cfg["accounts"]}
        missing = [u for u in targets if u not in known]
        if missing:
            print(f"✗ accounts.jsonに無いusername: {missing}")
            print("  実行したい場合は --skip-accounts-check")
            return 1

    pre = check_prereqs()
    if not pre.ok:
        print(f"✗ prereq failed: {pre.reason}")
        print("ヒント:")
        print("  AVD起動: bash scripts/setup_ig_capture_avd.sh run")
        print("  device setup: bash scripts/setup_ig_capture_device.sh all")
        print("  mitmdump: ./venv/bin/mitmdump -s scripts/ig_mitm_capture.py --listen-port 8080")
        return 1
    print(f"✓ prereqs OK: device={pre.adb_device}")

    if args.dry_run:
        print("[dry-run] 対象:", targets)
        print("[dry-run] interval:", args.interval, "秒")
        return 0

    log_dir = PROJECT_ROOT / "logs"
    log_dir.mkdir(exist_ok=True)

    import uiautomator2 as u2
    d = u2.connect(pre.adb_device)

    results: dict[str, bool] = {}
    for i, username in enumerate(targets):
        password = None if args.manual_login else load_password_for(username, args.password)
        if not args.manual_login and not password:
            print(f"✗ {username}: password not resolved")
            results[username] = False
            if not args.continue_on_error:
                print("✗ stop_on_error: passwordが無いため停止します")
                break
            continue
        ok = False
        try:
            ok = process_account(
                d,
                pre.adb_device,
                username,
                password,
                log_dir,
                manual_login_timeout=max(60, int(args.manual_login_timeout or 600)),
                manual_login=args.manual_login,
            )
        except CaptureStop as e:
            print(f"  ✗ stop_capture: {e}")
            return e.exit_code
        except IGFlaggedError as e:
            print(f"  ⚠ IG側フラグ検知 → 以後のcaptureを中断します: {e}")
            return 3
        except Exception as e:
            print(f"  ✗ exception: {type(e).__name__}: {e}")
        results[username] = ok
        if not ok and not args.continue_on_error:
            print("  ✗ stop_on_error: 失敗したため次のアカウントへ進まず停止します")
            break

        if i < len(targets) - 1:
            print(f"  ... waiting {args.interval}s before next account")
            time.sleep(args.interval)

    print("\n=== summary ===")
    for u, ok in results.items():
        print(f"  {u}: {'OK' if ok else 'NG'}")
    return 0 if all(results.values()) else 2


if __name__ == "__main__":
    sys.exit(main())
