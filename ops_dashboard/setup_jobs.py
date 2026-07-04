from __future__ import annotations

import os
import platform
import json
import shutil
import sys
from pathlib import Path
from typing import Any

try:
    from .capture_jobs import PYTHON, ROOT, _active_job, _new_job, _quick_run, list_jobs
except ImportError:
    from capture_jobs import PYTHON, ROOT, _active_job, _new_job, _quick_run, list_jobs

ANDROID_HOME = Path(os.environ.get("ANDROID_HOME", str(Path.home() / "Library" / "Android" / "sdk")))
SDKMANAGER = ANDROID_HOME / "cmdline-tools" / "latest" / "bin" / "sdkmanager"
AVDMANAGER = ANDROID_HOME / "cmdline-tools" / "latest" / "bin" / "avdmanager"
ADB = ANDROID_HOME / "platform-tools" / "adb"
EMULATOR = ANDROID_HOME / "emulator" / "emulator"
AVD_NAME = os.environ.get("IG_CAP_AVD", "ig_capture")
INSTAGRAM_PKG = "com.instagram.android"


def _bool_status(ok: bool, summary: str, next_action: str = "") -> dict[str, Any]:
    return {"ok": ok, "summary": summary, "next_action": next_action}


def _exists_status(path: Path, label: str, next_action: str) -> dict[str, Any]:
    if path.exists():
        return _bool_status(True, f"{label} はあります: {path}")
    return _bool_status(False, f"{label} がありません: {path}", next_action)


def _avd_exists() -> bool:
    if not AVDMANAGER.exists():
        return False
    result = _quick_run([str(AVDMANAGER), "list", "avd"], timeout=30)
    if result["ok"]:
        for line in result.get("output", "").splitlines():
            if line.strip() == f"Name: {AVD_NAME}":
                return True
    if EMULATOR.exists():
        listed = _quick_run([str(EMULATOR), "-list-avds"], timeout=20)
        if listed["ok"] and AVD_NAME in listed.get("output", "").splitlines():
            return True
    return (Path.home() / ".android" / "avd" / f"{AVD_NAME}.avd").exists()


def _connected_devices() -> list[str]:
    devices = _quick_run([str(ADB), "devices"], timeout=10)
    if not devices["ok"]:
        return []
    serials: list[str] = []
    for line in devices.get("output", "").splitlines()[1:]:
        parts = line.split()
        if len(parts) >= 2 and parts[1] == "device":
            serials.append(parts[0])
    return serials


def _running_avd_name(serial: str) -> str | None:
    result = _quick_run([str(ADB), "-s", serial, "emu", "avd", "name"], timeout=10)
    if not result["ok"]:
        return None
    for line in result.get("output", "").splitlines():
        text = "".join(ch for ch in line.replace("\r", "") if ch.isprintable()).strip()
        if text and text != "OK":
            return text
    return None


def _avd_status() -> dict[str, Any]:
    if not AVDMANAGER.exists():
        return _bool_status(False, "Android作成ツールがありません", "② Android SDKを実行してから、③ Android画面作成を実行してください。")
    if _avd_exists():
        return _bool_status(True, f"Android画面 {AVD_NAME} は作成済みです")
    return _bool_status(False, f"Android画面 {AVD_NAME} がありません", "初回セットアップで③ Android画面作成を実行してください。")


def _instagram_status() -> dict[str, Any]:
    if not ADB.exists():
        return _bool_status(False, "ADBがありません", "Android SDKを入れてからInstagram導入を実行してください。")
    if not _avd_exists():
        return _bool_status(False, f"Android画面 {AVD_NAME} がないためInstagram確認を止めています", "先に初回セットアップで③ Android画面作成を実行してください。")
    serials = _connected_devices()
    if not serials:
        return _bool_status(False, "Android画面が起動していないためInstagram確認ができません", "③ Android画面作成または④ 通信用設定を実行してからInstagram導入を実行してください。")
    if len(serials) > 1:
        return _bool_status(False, "複数のAndroid画面が起動しています", "不要なAndroid画面を閉じて、ig_captureだけを起動してください。")
    running_name = _running_avd_name(serials[0])
    if running_name != AVD_NAME:
        shown = running_name or "不明"
        return _bool_status(False, f"起動中のAndroid画面が {AVD_NAME} ではありません: {shown}", f"{AVD_NAME} を作成・起動してからInstagram導入を実行してください。")
    result = _quick_run([str(ADB), "-s", serials[0], "shell", "pm", "path", INSTAGRAM_PKG], timeout=20)
    if result["ok"] and INSTAGRAM_PKG in result.get("output", ""):
        return _bool_status(True, "Instagramアプリはインストール済みです")
    return _bool_status(
        False,
        "Instagramアプリがありません",
        "Instagram同梱済みの最新版DMGを管理者から受け取り、Instagram導入を実行してください。",
    )


def _python_status() -> dict[str, Any]:
    venv_python = ROOT / "venv" / "bin" / "python"
    if not venv_python.exists():
        return _bool_status(False, "Python環境がありません", "アプリを開き直してください。初回起動時に自動作成します。")
    result = _quick_run([str(venv_python), "-c", "import sys; print(sys.version.split()[0])"], timeout=20)
    if result["ok"]:
        return _bool_status(True, f"Python環境は使えます: {result.get('output', '').strip()}")
    return _bool_status(False, "Python環境が壊れています", "アプリを終了してから開き直してください。")


def _package_status(executable: Path, label: str, import_name: str) -> dict[str, Any]:
    if not executable.exists():
        return _bool_status(False, f"{label} が確認できません", "Python環境を作成してください。")
    result = _quick_run([str(executable), "-c", f"import {import_name}"], timeout=20)
    if result["ok"]:
        return _bool_status(True, f"{label} は使えます")
    return _bool_status(False, f"{label} が不足しています", "Python環境を作成し直してください。")


def _java_status() -> dict[str, Any]:
    local_java = Path.home() / "Library" / "Application Support" / "UnariSagiOperator" / "jdk" / "temurin-17" / "bin" / "java"
    candidates = [local_java]
    system_java = shutil.which("java")
    if system_java:
        candidates.append(Path(system_java))
    for java in candidates:
        if not java.exists():
            continue
        result = _quick_run([str(java), "-version"], timeout=10)
        if result["ok"]:
            return _bool_status(True, f"Java は使えます: {java}")
    return _bool_status(False, "Java は未導入です", "Android SDKボタンを押すと、必要なJavaも自動導入します。")


def _sheets_bridge_info() -> dict[str, Any] | None:
    result = _quick_run([PYTHON, "scripts/sheets_bridge.py", "--status"], timeout=20)
    if not result["ok"]:
        return None
    try:
        return json.loads(result.get("output") or "{}")
    except json.JSONDecodeError:
        return None


def _sheets_bridge_status(info: dict[str, Any] | None) -> dict[str, Any]:
    if not info:
        return _bool_status(
            False,
            "Google Sheets連携状態を確認できません",
            "Python環境を作成してから、Google Sheets接続設定を確認してください。",
        )
    if info.get("backend") == "apps-script":
        if info.get("ok"):
            return _bool_status(True, "Google Sheets連携はApps Script経由で設定済みです")
        return _bool_status(False, "Apps Script連携URLまたは内部トークンが未設定です", "管理者がGoogle Sheets連携設定済みの最新版アプリを配布してください。")
    if info.get("ok"):
        return _bool_status(True, "Google Sheets API直接接続の準備があります")
    return _bool_status(
        False,
        "Google Sheets連携設定がありません",
        "管理者がApps Script連携設定済みの最新版アプリを配布するか、Google API認証ファイルを設定してください。",
    )


def _sheets_auth_status(info: dict[str, Any] | None) -> dict[str, Any]:
    if not info:
        return _bool_status(False, "Google Sheets接続を確認できません", "Python環境を作成してください。")
    if info.get("backend") == "apps-script":
        return _bool_status(True, "Apps Script経由のため、このMacでGoogleログインは不要です")
    if not info.get("ok"):
        return _bool_status(
            False,
            "Google API認証ファイルがありません",
            "メンバー本人では対応できません。管理者がApps Script連携設定済みの最新版アプリを配布してください。",
        )
    if info.get("token_exists"):
        return _bool_status(True, "Google Sheets認証トークンがあります")
    return _bool_status(
        False,
        "Google Sheets認証が未完了です",
        "Google Sheets接続設定を押して、ブラウザでGoogleログインしてください。",
    )


def collect_setup_status() -> dict[str, Any]:
    machine = platform.machine()
    venv_python = ROOT / "venv" / "bin" / "python"
    sheets_info = _sheets_bridge_info()
    members_json = ROOT / "config" / "members.json"
    checks = {
        "apple_silicon": _bool_status(
            machine == "arm64",
            f"Mac種別: {machine}",
            "この配布版はApple Silicon Macだけを対象にしています。",
        ),
        "python_runtime": _python_status(),
        "flask": _package_status(venv_python, "Flask", "flask"),
        "mitmproxy": _exists_status(ROOT / "venv" / "bin" / "mitmdump", "mitmdump", "Python環境を作成してください。"),
        "frida": _exists_status(ROOT / "venv" / "bin" / "frida", "frida", "Python環境を作成してください。"),
        "java": _java_status(),
        "sdkmanager": _exists_status(SDKMANAGER, "sdkmanager", "Android cmdline tools を入れてください。"),
        "adb": _exists_status(ADB, "adb", "Android SDK platform-tools を入れてください。"),
        "emulator": _exists_status(EMULATOR, "emulator", "Android SDK emulator を入れてください。"),
        "avd": _avd_status(),
        "instagram_app": _instagram_status(),
        "mitm_ca": _exists_status(Path.home() / ".mitmproxy" / "mitmproxy-ca-cert.pem", "通信の証明書", "④ 通信用設定を実行してください。"),
        "sheets_bridge": _sheets_bridge_status(sheets_info),
        "sheets_auth": _sheets_auth_status(sheets_info),
        "members_config": _exists_status(members_json, "members.json", "Slack通知を使う場合は管理者が設定してください。dry-runだけなら不要です。"),
    }
    setup_jobs = list_jobs(limit=8, kind="setup")
    latest_job = setup_jobs[0] if setup_jobs else None
    ready = all(item["ok"] for item in checks.values())
    return {
        "ok": ready,
        "root": str(ROOT),
        "android_home": str(ANDROID_HOME),
        "avd_name": AVD_NAME,
        "checks": checks,
        "latest_job": latest_job,
    }


def _busy_error() -> str | None:
    active = _active_job()
    if active:
        return f"実行中のジョブがあります: {active['label']}"
    return None


def start_setup_job(action: str) -> tuple[dict[str, Any] | None, str | None]:
    if error := _busy_error():
        return None, error
    action = (action or "").strip()
    if action == "venv":
        py = str(ROOT / "venv" / "bin" / "python") if (ROOT / "venv" / "bin" / "python").exists() else sys.executable
        return _new_job(
            "初回セットアップ: Python環境",
            [
                {"name": "必要ライブラリをインストール", "cmd": [py, "-m", "pip", "install", "-r", "requirements.txt"], "timeout": 1800},
            ],
            kind="setup",
        ), None
    if action == "android-tools":
        return _new_job(
            "初回セットアップ: Android cmdline tools",
            [{"name": "Android cmdline toolsを導入", "cmd": [PYTHON, "-u", "scripts/install_android_cmdline_tools.py"], "timeout": 1800}],
            kind="setup",
        ), None
    if action == "avd":
        return _new_job(
            "初回セットアップ: Android画面作成",
            [{"name": "Android画面を作成", "cmd": ["bash", "scripts/setup_ig_capture_avd.sh", "setup"], "timeout": 2400}],
            kind="setup",
        ), None
    if action == "device":
        return _new_job(
            "初回セットアップ: 通信用設定",
            [
                {"name": "Android通信の受け口を準備", "cmd": ["bash", "scripts/ensure_capture_infra.sh", "--prepare-device"], "timeout": 600},
                {"name": "Android側へ通信用設定を入れる", "cmd": ["bash", "scripts/setup_ig_capture_device.sh", "all"], "timeout": 900},
                {"name": "通信準備の最終確認", "cmd": ["bash", "scripts/ensure_capture_infra.sh"], "timeout": 600},
            ],
            kind="setup",
        ), None
    if action == "instagram":
        return _new_job(
            "初回セットアップ: Instagram導入",
            [
                {"name": "Instagram APK/APKM/XAPKをAVDへインストール", "cmd": ["bash", "scripts/install_instagram_apk.sh"], "timeout": 600},
            ],
            kind="setup",
        ), None
    if action == "google-auth":
        info = _sheets_bridge_info()
        if info and info.get("backend") == "apps-script" and info.get("ok"):
            return _new_job(
                "初回セットアップ: Google Sheets接続確認",
                [{"name": "Google Sheets連携方式を確認", "cmd": [PYTHON, "scripts/sheets_bridge.py", "--status"], "timeout": 30}],
                kind="setup",
            ), None
        if not info:
            return None, "Google Sheets接続状態を確認できません。アプリを開き直して、直らなければログを管理者へ渡してください。"
        if info.get("backend") == "apps-script":
            return None, "Apps Script連携URLまたは内部トークンが未設定です。管理者がGoogle Sheets接続設定済みの最新版アプリを配布してください。"
        if not info.get("ok"):
            return None, "Google API認証ファイルがありません。メンバー本人では対応できません。管理者がApps Script連携設定済みの最新版アプリを配布してください。"
        return _new_job(
            "初回セットアップ: Google Sheets接続設定",
            [
                {"name": "ブラウザでGoogle Sheets認証", "cmd": [PYTHON, "-u", "scripts/sheets_auth.py"], "timeout": 660, "env": {"SHEETS_AUTH_CONSOLE": "0"}},
            ],
            kind="setup",
        ), None
    if action == "all":
        py = str(ROOT / "venv" / "bin" / "python") if (ROOT / "venv" / "bin" / "python").exists() else sys.executable
        return _new_job(
            "初回セットアップ: まとめて実行",
            [
                {"name": "必要ライブラリをインストール", "cmd": [py, "-m", "pip", "install", "-r", "requirements.txt"], "timeout": 1800},
                {"name": "Android cmdline toolsを導入", "cmd": [py, "-u", "scripts/install_android_cmdline_tools.py"], "timeout": 1800},
                {"name": "rootable AVDを作成", "cmd": ["bash", "scripts/setup_ig_capture_avd.sh", "setup"], "timeout": 2400},
                {"name": "AVD/mitmdumpを起動しCAを生成", "cmd": ["bash", "scripts/ensure_capture_infra.sh", "--prepare-device"], "timeout": 600},
                {"name": "CA/Frida/proxyをAVDへ設定", "cmd": ["bash", "scripts/setup_ig_capture_device.sh", "all"], "timeout": 900},
                {"name": "AVD/mitmdump/Fridaを最終確認", "cmd": ["bash", "scripts/ensure_capture_infra.sh"], "timeout": 600},
                {"name": "Instagram APK/APKM/XAPKをAVDへインストール", "cmd": ["bash", "scripts/install_instagram_apk.sh"], "timeout": 600},
            ],
            kind="setup",
        ), None
    return None, "action は venv / android-tools / avd / device / instagram / google-auth / all のいずれかを指定してください"
