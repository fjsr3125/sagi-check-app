#!/usr/bin/env python3
from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
import zipfile
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
APP_DIR = ROOT / "dist" / "Unari Sagi Operator.app"
APP_RESOURCES = APP_DIR / "Contents" / "Resources"
APP_ROOT = APP_DIR / "Contents" / "Resources" / "unari-src"
APP_EXECUTABLE = APP_DIR / "Contents" / "MacOS" / "Unari Sagi Operator"
BUNDLED_PYTHON = APP_DIR / "Contents" / "Resources" / "python" / "bin" / "python3"
WHEELHOUSE = APP_DIR / "Contents" / "Resources" / "wheelhouse"
INSTAGRAM_PACKAGE_SUFFIXES = {".apk", ".apkm", ".xapk"}
TOOL_FILES = [
    "android-proxy-config.js",
    "android-unpinning-fallback.js",
    "android-unpinning-httptoolkit.js",
    "c8750f0d.0",
    "config.js",
    "frida-multiple-unpinning.js",
    "frida-server-17.9.1-android-arm64",
]
CAPTURE_TOOLS_DIR_ENV = "SAGI_OPERATOR_CAPTURE_TOOLS_DIR"
MEMBERS_CONFIG_ENV = "SAGI_OPERATOR_MEMBERS_CONFIG"
UPDATE_CONFIG_ENV = "SAGI_OPERATOR_UPDATE_CONFIG"
SHEETS_BRIDGE_CONFIG_ENV = "SAGI_SHEETS_BRIDGE_CONFIG"
ALLOW_MISSING_PRIVATE_ASSETS_ENV = "SAGI_OPERATOR_ALLOW_MISSING_PRIVATE_ASSETS"
SECRET_PATTERNS = [
    "accounts.json",
    "hubspot_members.json",
    "capture_pool.json",
    "soax.json",
    ".env",
]
LOCAL_PATH_PATTERNS = [
    "/Users/fujimakisora",
    "fujimakisora",
]


def acquire_release_check_lock():
    (ROOT / "dist").mkdir(parents=True, exist_ok=True)
    lock_path = ROOT / "dist" / ".sagi_operator_release_check.lock"
    lock_file = lock_path.open("w", encoding="utf-8")
    fcntl.flock(lock_file, fcntl.LOCK_EX)
    return lock_file


def _env_enabled(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def _allow_missing_private_assets() -> bool:
    return _env_enabled(ALLOW_MISSING_PRIVATE_ASSETS_ENV)


def _existing_path_from_env(name: str) -> Path | None:
    value = os.environ.get(name, "").strip()
    if not value:
        return None
    path = Path(value).expanduser()
    return path if path.exists() else None


def run(
    cmd: list[str],
    *,
    cwd: Path = ROOT,
    timeout: int = 120,
    env: dict[str, str] | None = None,
) -> dict:
    proc = subprocess.run(
        cmd,
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=timeout,
        env=env,
    )
    return {
        "cmd": " ".join(cmd),
        "ok": proc.returncode == 0,
        "returncode": proc.returncode,
        "stdout": (proc.stdout or "").strip(),
        "stderr": (proc.stderr or "").strip(),
    }


def step(name: str, result: dict, results: list[dict], *, quiet: bool = False) -> None:
    result["name"] = name
    results.append(result)
    if quiet:
        return
    mark = "OK" if result["ok"] else "NG"
    print(f"[{mark}] {name}")
    if not result["ok"]:
        tail = "\n".join([result.get("stdout", ""), result.get("stderr", "")]).strip()
        if tail:
            print(tail[-1200:])


def check_bundle_secrets() -> dict:
    if not APP_ROOT.exists():
        return {"ok": False, "error": f"bundle source not found: {APP_ROOT}"}
    found = []
    for path in APP_ROOT.rglob("*"):
        if not path.is_file():
            continue
        if path.name in SECRET_PATTERNS or path.name.endswith(".env"):
            found.append(str(path.relative_to(APP_ROOT)))
        if ".bak" in path.name:
            found.append(str(path.relative_to(APP_ROOT)))
        if path.parts and "sessions" in path.parts:
            found.append(str(path.relative_to(APP_ROOT)))
        if path.parts and "captures" in path.parts:
            found.append(str(path.relative_to(APP_ROOT)))
    return {"ok": not found, "found": found}


def check_bundle_local_paths() -> dict:
    if not APP_ROOT.exists():
        return {"ok": False, "error": f"bundle source not found: {APP_ROOT}"}
    found = []
    for path in APP_ROOT.rglob("*"):
        if not path.is_file():
            continue
        if path.suffix.lower() in INSTAGRAM_PACKAGE_SUFFIXES:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        matches = [pattern for pattern in LOCAL_PATH_PATTERNS if pattern in text]
        if matches:
            found.append({"path": str(path.relative_to(APP_ROOT)), "matches": matches})
    return {"ok": not found, "found": found}


def check_bundle_python_caches() -> dict:
    if not APP_RESOURCES.exists():
        return {"ok": False, "error": f"app resources not found: {APP_RESOURCES}"}
    found = []
    for path in APP_RESOURCES.rglob("*"):
        if path.name == "__pycache__" or path.suffix == ".pyc":
            found.append(str(path.relative_to(APP_RESOURCES)))
            if len(found) >= 50:
                break
    return {"ok": not found, "found": found}


def check_app_signature() -> dict:
    if not APP_DIR.exists():
        return {"ok": False, "error": f"app not found: {APP_DIR}"}
    codesign = shutil.which("codesign")
    if codesign is None:
        return {"ok": False, "error": "codesign command not found"}
    return run(
        [codesign, "--verify", "--deep", "--strict", "--verbose=4", str(APP_DIR)],
        timeout=60,
    )


def check_bundled_python() -> dict:
    if not BUNDLED_PYTHON.exists():
        return {"ok": False, "error": f"bundled python not found: {BUNDLED_PYTHON}"}
    with tempfile.TemporaryDirectory(prefix="unari_operator_pycache_") as td:
        env = os.environ.copy()
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        env["PYTHONPYCACHEPREFIX"] = td
        result = run([str(BUNDLED_PYTHON), "-V"], timeout=30, env=env)
        output = " ".join([result.get("stdout", ""), result.get("stderr", "")]).strip()
        return {"ok": result["ok"] and "Python 3.14" in output, "output": output, "result": result}


def check_bundled_wheelhouse() -> dict:
    if not WHEELHOUSE.exists():
        return {"ok": False, "error": f"wheelhouse not found: {WHEELHOUSE}"}
    wheels = sorted(path.name for path in WHEELHOUSE.glob("*.whl"))
    required_names = ["Flask", "requests", "instagrapi", "frida", "mitmproxy"]
    missing = [
        name for name in required_names
        if not any(wheel.lower().startswith(name.lower().replace("-", "_")) for wheel in wheels)
    ]
    return {
        "ok": len(wheels) >= 40 and not missing,
        "wheel_count": len(wheels),
        "missing": missing,
        "sample": wheels[:10],
    }


def check_sheets_bridge_bundle() -> dict:
    source_candidates = [
        _existing_path_from_env(SHEETS_BRIDGE_CONFIG_ENV),
        ROOT / "config" / "sagi_sheets_bridge.json",
        Path.home() / ".config" / "unari" / "sagi_sheets_bridge.json",
    ]
    source_available = any(path and path.exists() for path in source_candidates)
    required = [
        APP_ROOT / "scripts" / "sheets_bridge.py",
        APP_ROOT / "scripts" / "sheets_auth.py",
        APP_ROOT / "scripts" / "sagi_sheets_webapp.gs",
        APP_ROOT / "config" / "sagi_sheets_bridge.json",
        APP_ROOT / "config" / "sagi_sheets_bridge.example.json",
    ]
    missing = [str(path.relative_to(APP_ROOT)) for path in required if not path.exists()]
    stale = []
    if (APP_ROOT / "tools" / "gog").exists():
        stale.append("tools/gog")
    config_ok = False
    config_summary: dict[str, Any] = {}
    config_path = APP_ROOT / "config" / "sagi_sheets_bridge.json"
    if config_path.exists():
        try:
            data = json.loads(config_path.read_text(encoding="utf-8"))
            config_ok = (
                data.get("backend") == "apps-script"
                and bool(data.get("web_app_url"))
                and bool(data.get("token"))
            )
            config_summary = {
                "backend": data.get("backend"),
                "web_app_url_set": bool(data.get("web_app_url")),
                "token_set": bool(data.get("token")),
            }
        except (OSError, json.JSONDecodeError) as e:
            config_summary = {"error": str(e)}
    private_missing = missing == ["config/sagi_sheets_bridge.json"] and not source_available
    if private_missing and _allow_missing_private_assets():
        return {
            "ok": True,
            "skipped": "private Sheets bridge config is not available in this local checkout",
            "missing": missing,
            "stale": stale,
            "config": config_summary,
        }
    return {
        "ok": not missing and not stale and config_ok,
        "missing": missing,
        "stale": stale,
        "config": config_summary,
    }


def check_instagram_package_bundle() -> dict:
    apk_dir = APP_ROOT / "apks"
    if not apk_dir.exists():
        if _allow_missing_private_assets():
            return {
                "ok": True,
                "skipped": "Instagram APK/APKM/XAPK is not available in this local checkout",
                "missing": "unari-src/apks directory is missing",
            }
        return {"ok": False, "missing": "unari-src/apks directory is missing"}
    packages = [
        path
        for path in apk_dir.iterdir()
        if path.is_file()
        and path.suffix.lower() in INSTAGRAM_PACKAGE_SUFFIXES
        and "instagram" in path.name.lower()
    ]
    if not packages:
        if _allow_missing_private_assets():
            return {
                "ok": True,
                "skipped": "Instagram APK/APKM/XAPK is not available in this local checkout",
                "missing": "Instagram APK/APKM/XAPK is not bundled",
            }
        return {"ok": False, "missing": "Instagram APK/APKM/XAPK is not bundled"}
    return {
        "ok": True,
        "packages": [
            {"name": path.name, "size_mb": round(path.stat().st_size / 1024 / 1024, 1)}
            for path in sorted(packages)
        ],
    }


def check_capture_tools_bundle() -> dict:
    source_dir = Path(os.environ.get(CAPTURE_TOOLS_DIR_ENV, str(ROOT / "tools"))).expanduser()
    bundled_dir = APP_ROOT / "tools"
    source_missing = [name for name in TOOL_FILES if not (source_dir / name).exists()]
    if source_missing:
        return {"ok": True, "skipped": "capture tools are not present in repo", "missing_in_repo": source_missing}
    bundled_missing = [name for name in TOOL_FILES if not (bundled_dir / name).exists()]
    return {"ok": not bundled_missing, "missing": bundled_missing}


def check_members_config_bundle() -> dict:
    source = Path(os.environ.get(MEMBERS_CONFIG_ENV, str(ROOT / "config" / "members.json"))).expanduser()
    bundled = APP_ROOT / "config" / "members.json"
    if not source.exists():
        if _allow_missing_private_assets():
            return {
                "ok": True,
                "skipped": "private members config is not available in this local checkout",
                "missing": f"members config is missing: {source}",
            }
        return {"ok": False, "missing": f"members config is missing: {source}"}
    if not bundled.exists():
        return {"ok": False, "missing": "unari-src/config/members.json is not bundled"}
    source_size = source.stat().st_size
    bundled_size = bundled.stat().st_size
    return {
        "ok": source_size > 0 and source_size == bundled_size,
        "source_size": source_size,
        "bundled_size": bundled_size,
    }


def check_update_bundle() -> dict:
    source = Path(os.environ.get(UPDATE_CONFIG_ENV, str(ROOT / "config" / "sagi_operator_update.json"))).expanduser()
    bundled = APP_ROOT / "config" / "sagi_operator_update.json"
    version = APP_ROOT / "config" / "sagi_operator_version.json"
    missing = []
    if source.exists() and not bundled.exists():
        missing.append("unari-src/config/sagi_operator_update.json")
    if not version.exists():
        missing.append("unari-src/config/sagi_operator_version.json")
    details: dict[str, Any] = {}
    if bundled.exists():
        try:
            data = json.loads(bundled.read_text(encoding="utf-8"))
            details = {
                "enabled": bool(data.get("enabled")),
                "latest_url_set": bool(data.get("latest_url")),
            }
        except (OSError, json.JSONDecodeError) as e:
            details = {"error": str(e)}
            missing.append("update config is not valid json")
    return {"ok": not missing, "missing": missing, "update_config": details}


def check_launcher_script() -> dict:
    if not APP_EXECUTABLE.exists():
        return {"ok": False, "error": f"app executable not found: {APP_EXECUTABLE}"}
    syntax = run(["zsh", "-n", str(APP_EXECUTABLE)], timeout=30)
    text = APP_EXECUTABLE.read_text(encoding="utf-8")
    required = [
        'osascript - "$msg" "$BOOT_LOG"',
        "on run argv",
        "display dialog messageText & linefeed",
        "display dialog \"アプリ画面の起動に失敗しました。\" & linefeed",
        "required=[\"flask\",\"requests\",\"instagrapi\",\"googleapiclient\",\"frida\",\"mitmproxy\"]",
        "--retries 5 --timeout 60 --prefer-binary",
        "WHEELHOUSE=",
        "installing dependencies from bundled wheelhouse",
        "--no-index --find-links",
        "PIP_PROGRESS_BAR=off",
    ]
    missing = [item for item in required if item not in text]
    if '--exclude "members.json"' in text:
        missing.append("members.json must be copied into member APP_ROOT")
    return {"ok": syntax["ok"] and not missing, "syntax": syntax, "missing": missing}


def check_archive_payloads() -> dict:
    zip_path = ROOT / "dist" / "Unari Sagi Operator.zip"
    dmg_path = ROOT / "dist" / "Unari Sagi Operator.dmg"
    checks = []
    if zip_path.exists():
        checks.append(run(["unzip", "-t", str(zip_path)], timeout=180))
    if dmg_path.exists():
        checks.append(run(["hdiutil", "verify", str(dmg_path)], timeout=180))
    if not checks:
        return {"ok": True, "checked": []}
    return {
        "ok": all(item.get("ok") for item in checks),
        "checked": [
            {
                "cmd": item.get("cmd"),
                "ok": item.get("ok"),
                "stdout_tail": item.get("stdout", "")[-500:],
                "stderr_tail": item.get("stderr", "")[-500:],
            }
            for item in checks
        ],
    }


ARCHIVE_TEXT_CHECKS = {
    "scripts/sagi_request_processor.py": [
        "from sheets_bridge import get_metadata, get_values, update_values",
        "def sheets_get",
        "def sheets_update",
    ],
    "scripts/sheets_bridge.py": [
        "def backend_kind",
        "apps-script",
        "google-api",
        "SAGI_SHEETS_WEBAPP_URL",
    ],
    "scripts/sagi_sheets_webapp.gs": [
        "function doPost",
        "SpreadsheetApp.openById",
        "SAGI_OPERATOR_TOKEN",
    ],
    "ops_dashboard/setup_jobs.py": [
        "Google Sheets接続設定",
        "google-auth",
        "sheets_bridge",
        "sheets_auth",
        "Google API認証ファイルがありません",
    ],
    "scripts/ensure_capture_infra.sh": [
        "probe_mac_instagram_dns",
        "Mac DNS: Instagram接続先を解決できます",
    ],
    "scripts/shin_capture_auto.py": [
        "wait_for_frida_hooks",
        "wait_for_manual_login_completion",
        "manual_login_mode",
        "--manual-login",
        "manual_login_required",
        "--continue-on-error",
        "transport diagnosis",
        '"--no-proxy"',
        '"--no-verify"',
    ],
    "scripts/import_real_session.py": [
        "accounts.json が無いため",
        "DEFAULT_CONFIG",
        "inserted (proxy/passwordなし)",
    ],
    "scripts/verify_captured_session.py": [
        "DEFAULT_CONFIG",
        "_accounts_from_sessions",
    ],
    "scripts/api_warning_check.py": [
        "DEFAULT_CONFIG",
        "_accounts_from_strong_sessions",
    ],
    "ops_dashboard/capture_jobs.py": [
        "--manual-login",
        "manual_login_timeout",
        "login_input_error",
        "メール確認/2FA",
        "network_dns_or_502",
        "tls_or_pinning",
        "DNS/502",
        "Google Sheetsの認証",
        "SheetsBridgeError",
        "NEEDS_SUPPLEMENT",
        "続きから再開",
        "チェック用ログインを1つ作る",
        "capacity_shortage",
        "login_required",
        "port_conflict",
    ],
    "ops_dashboard/check_jobs.py": [
        "start_sheet_check_job",
        "詐欺チェック: ①取込と件数確認",
        "強session必要本数チェック",
        "NEEDS_SUPPLEMENT",
        "CSVファイルを指定してください",
        "このアプリ内のファイルだけ指定できます",
        "latest_results",
        "チェック済み",
        "--dry-run",
        "--no-proxy",
        "--resume",
    ],
    "ops_dashboard/templates/index.html": [
        "/api/update/status",
        "/api/update/download",
        "新しい版があります",
        "アプリは最新版です",
        "sagiUpdateHint",
        "normalizeSagiTabName",
        "step-pills",
        "① まず件数を確認（本番はまだ走りません）",
        "② 本番チェックを実行",
        "途中から再開（ログイン追加後）",
        "直近CSV",
        "CSVファイル（アプリ内、account_id列）",
        "チェック用ログイン追加",
        "詳細設定 / CSVで実行する場合",
        "Google Sheets接続設定",
        "書き戻さずに件数だけ確認",
        "表示ログをコピー",
        "詐欺チェックの進行状況",
        "sagiSheetsBridgeHint",
        "setSagiTabByOffset",
        "検証用ログ（必要時だけ）",
    ],
    "ops_dashboard/update_check.py": [
        "collect_update_status",
        "download_latest_update",
        "sagi_operator_update.json",
        "sagi_operator_version.json",
        "latest.json",
    ],
    "ops_dashboard/app.py": [
        "api_update_status",
        "api_update_download",
        "/api/update/status",
        "/api/update/download",
    ],
    "Contents/MacOS/Unari Sagi Operator": [
        'osascript - "$msg" "$BOOT_LOG"',
        "on run argv",
    ],
}


def _check_archive_text(root: Path) -> list[dict]:
    missing: list[dict] = []
    for suffix, expected in ARCHIVE_TEXT_CHECKS.items():
        matches = [path for path in root.rglob("*") if path.is_file() and str(path).endswith(suffix)]
        if not matches:
            missing.append({"path": suffix, "missing": ["file not found"]})
            continue
        text = matches[0].read_text(encoding="utf-8", errors="ignore")
        absent = [item for item in expected if item not in text]
        if absent:
            missing.append({"path": suffix, "missing": absent})
    return missing


def check_archive_contents() -> dict:
    zip_path = ROOT / "dist" / "Unari Sagi Operator.zip"
    dmg_path = ROOT / "dist" / "Unari Sagi Operator.dmg"
    issues: list[dict] = []
    if zip_path.exists():
        try:
            with tempfile.TemporaryDirectory(prefix="unari_operator_zip_check_") as td:
                with zipfile.ZipFile(zip_path) as zf:
                    zf.extractall(td)
                issues.extend({"archive": "zip", **item} for item in _check_archive_text(Path(td)))
        except Exception as e:
            issues.append({"archive": "zip", "error": f"{type(e).__name__}: {e}"})
    if dmg_path.exists():
        attach = run(["hdiutil", "attach", "-nobrowse", "-readonly", str(dmg_path)], timeout=120)
        if not attach["ok"]:
            issues.append({"archive": "dmg", "error": "attach failed", "detail": attach})
        else:
            mount = None
            try:
                for line in attach.get("stdout", "").splitlines():
                    if "/Volumes/" in line:
                        mount = line[line.index("/Volumes/") :].strip()
                        break
                if not mount:
                    issues.append({"archive": "dmg", "error": "mounted volume path not found"})
                else:
                    issues.extend({"archive": "dmg", **item} for item in _check_archive_text(Path(mount)))
            finally:
                if mount:
                    subprocess.run(["hdiutil", "detach", mount], capture_output=True, text=True, timeout=60)
    return {"ok": not issues, "issues": issues}


def check_dmg_app_signature() -> dict:
    dmg_path = ROOT / "dist" / "Unari Sagi Operator.dmg"
    if not dmg_path.exists():
        return {"ok": True, "skipped": "dmg not found"}
    attach = run(["hdiutil", "attach", "-nobrowse", "-readonly", str(dmg_path)], timeout=120)
    if not attach["ok"]:
        return attach
    mount = None
    try:
        for line in attach.get("stdout", "").splitlines():
            if "/Volumes/" in line:
                mount = line[line.index("/Volumes/") :].strip()
                break
        if not mount:
            return {"ok": False, "error": "mounted volume path not found", "attach": attach}
        mounted_app = Path(mount) / "Unari Sagi Operator.app"
        codesign = shutil.which("codesign")
        if codesign is None:
            return {"ok": False, "error": "codesign command not found"}
        return run(
            [codesign, "--verify", "--deep", "--strict", "--verbose=4", str(mounted_app)],
            timeout=60,
        )
    finally:
        if mount:
            subprocess.run(["hdiutil", "detach", mount], capture_output=True, text=True, timeout=60)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _kill_port(port: int) -> None:
    proc = subprocess.run(
        ["lsof", f"-tiTCP:{port}", "-sTCP:LISTEN"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    for line in proc.stdout.splitlines():
        try:
            os.kill(int(line.strip()), signal.SIGTERM)
        except Exception:
            pass


def _read_json(url: str, timeout: int = 10) -> dict:
    with urllib.request.urlopen(url, timeout=timeout) as res:
        return json.loads(res.read().decode("utf-8"))


def check_member_first_launch() -> dict:
    if not APP_EXECUTABLE.exists():
        return {"ok": False, "error": f"app executable not found: {APP_EXECUTABLE}"}
    port = _free_port()
    with tempfile.TemporaryDirectory(prefix="unari_operator_member_home_") as td:
        home = Path(td)
        env = os.environ.copy()
        env.update(
            {
                "HOME": str(home),
                "OPS_PORT": str(port),
                "UNARI_OPERATOR_NO_UI": "1",
                "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
            }
        )
        try:
            proc = subprocess.run(
                [str(APP_EXECUTABLE)],
                cwd=ROOT,
                env=env,
                capture_output=True,
                text=True,
                timeout=900,
            )
            if proc.returncode != 0:
                return {
                    "ok": False,
                    "returncode": proc.returncode,
                    "stdout": proc.stdout[-2000:],
                    "stderr": proc.stderr[-2000:],
                }
            deadline = time.time() + 20
            data = None
            while time.time() < deadline:
                try:
                    data = _read_json(f"http://localhost:{port}/api/setup/status", timeout=5)
                    break
                except Exception:
                    time.sleep(1)
            app_root = home / "Library" / "Application Support" / "UnariSagiOperator" / "unari"
            logs_dir = home / "Library" / "Logs" / "UnariSagiOperator"
            missing = []
            for required in [
                app_root / "venv" / "bin" / "python",
                logs_dir,
            ]:
                if not required.exists():
                    missing.append(str(required))
            launcher_logs = sorted(logs_dir.glob("launcher_*.log")) if logs_dir.exists() else []
            app_logs = sorted(logs_dir.glob("app_*.log")) if logs_dir.exists() else []
            log_text = "\n".join(path.read_text(encoding="utf-8", errors="ignore") for path in launcher_logs)
            if not launcher_logs:
                missing.append("launcher log")
            if not app_logs:
                missing.append("app log")
            if "installing dependencies from bundled wheelhouse" not in log_text:
                missing.append("launcher did not install dependencies from bundled wheelhouse")
            if "Downloading " in log_text:
                missing.append("launcher downloaded Python dependencies during first launch")
            checks = (data or {}).get("checks", {})
            if "python3" in checks:
                missing.append("setup status still exposes python3 requirement")
            if not checks.get("python_runtime", {}).get("ok"):
                missing.append("python_runtime status is not ok")
            return {
                "ok": data is not None and not missing,
                "port": port,
                "setup_status_ok": bool(data),
                "root": data.get("root") if data else None,
                "missing": missing,
                "stdout_tail": proc.stdout[-500:],
                "launcher_log_tail": log_text[-1200:],
            }
        finally:
            _kill_port(port)


def check_member_broken_venv_repair() -> dict:
    if not APP_EXECUTABLE.exists():
        return {"ok": False, "error": f"app executable not found: {APP_EXECUTABLE}"}
    if not BUNDLED_PYTHON.exists():
        return {"ok": False, "error": f"bundled python not found: {BUNDLED_PYTHON}"}
    port = _free_port()
    with tempfile.TemporaryDirectory(prefix="unari_operator_broken_venv_home_") as td:
        home = Path(td)
        app_root = home / "Library" / "Application Support" / "UnariSagiOperator" / "unari"
        app_root.mkdir(parents=True, exist_ok=True)
        env = os.environ.copy()
        env.update(
            {
                "HOME": str(home),
                "OPS_PORT": str(port),
                "UNARI_OPERATOR_NO_UI": "1",
                "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
                "PYTHONDONTWRITEBYTECODE": "1",
                "PYTHONPYCACHEPREFIX": str(home / "pycache"),
            }
        )
        create_venv = run([str(BUNDLED_PYTHON), "-m", "venv", str(app_root / "venv")], env=env, timeout=120)
        if not create_venv["ok"]:
            return {"ok": False, "error": "failed to create intentionally broken venv", "detail": create_venv}
        venv_python = app_root / "venv" / "bin" / "python"
        precheck = run([str(venv_python), "-c", "import flask"], env=env, timeout=30)
        if precheck["ok"]:
            return {"ok": False, "error": "test setup did not create a broken venv"}
        try:
            proc = subprocess.run(
                [str(APP_EXECUTABLE)],
                cwd=ROOT,
                env=env,
                capture_output=True,
                text=True,
                timeout=900,
            )
            postcheck = run([str(venv_python), "-c", "import flask, instagrapi"], env=env, timeout=60)
            data = None
            deadline = time.time() + 20
            while time.time() < deadline:
                try:
                    data = _read_json(f"http://localhost:{port}/api/setup/status", timeout=5)
                    break
                except Exception:
                    time.sleep(1)
            logs_dir = home / "Library" / "Logs" / "UnariSagiOperator"
            launcher_logs = sorted(logs_dir.glob("launcher_*.log")) if logs_dir.exists() else []
            log_text = "\n".join(path.read_text(encoding="utf-8", errors="ignore") for path in launcher_logs)
            missing = []
            if "installing dependencies from bundled wheelhouse" not in log_text:
                missing.append("launcher did not install dependencies from bundled wheelhouse")
            if "Downloading " in log_text:
                missing.append("launcher downloaded Python dependencies during broken venv repair")
            return {
                "ok": proc.returncode == 0 and postcheck["ok"] and data is not None and bool(launcher_logs) and not missing,
                "returncode": proc.returncode,
                "postcheck_ok": postcheck["ok"],
                "setup_status_ok": data is not None,
                "launcher_log_count": len(launcher_logs),
                "missing": missing,
                "stdout_tail": proc.stdout[-800:],
                "stderr_tail": proc.stderr[-800:],
                "postcheck_stderr_tail": postcheck.get("stderr", "")[-800:],
                "launcher_log_tail": log_text[-1200:],
            }
        finally:
            _kill_port(port)


def check_member_stale_server_guard() -> dict:
    if not APP_EXECUTABLE.exists():
        return {"ok": False, "error": f"app executable not found: {APP_EXECUTABLE}"}
    port = _free_port()
    server_code = r"""
import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/api/runtime/status":
            body = json.dumps(
                {
                    "ok": True,
                    "root": "/tmp/old-unari-operator",
                    "version": "0.0.0-old",
                    "build": "oldbuild",
                }
            ).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        body = b"old operator"
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        return

server = ThreadingHTTPServer(("127.0.0.1", int(sys.argv[1])), Handler)
server.serve_forever()
"""
    with tempfile.TemporaryDirectory(prefix="unari_operator_stale_server_home_") as td:
        home = Path(td)
        app_root = home / "Library" / "Application Support" / "UnariSagiOperator" / "unari"
        repo_venv = ROOT / "venv"
        repo_python = repo_venv / "bin" / "python"
        if not repo_python.exists():
            return {
                "ok": True,
                "skipped": "repo venv is not available for fast stale-server executable check",
            }
        app_root.mkdir(parents=True, exist_ok=True)
        (app_root / "venv").symlink_to(repo_venv, target_is_directory=True)
        env = os.environ.copy()
        env.update(
            {
                "HOME": str(home),
                "OPS_PORT": str(port),
                "UNARI_OPERATOR_NO_UI": "1",
                "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
                "PYTHONDONTWRITEBYTECODE": "1",
                "PYTHONPYCACHEPREFIX": str(home / "pycache"),
            }
        )
        fake_server = subprocess.Popen(
            [sys.executable, "-c", server_code, str(port)],
            cwd=ROOT,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        try:
            deadline = time.time() + 10
            while time.time() < deadline:
                try:
                    _read_json(f"http://127.0.0.1:{port}/api/runtime/status", timeout=2)
                    break
                except Exception:
                    time.sleep(0.2)
            else:
                return {"ok": False, "error": "fake stale server did not start"}

            proc = subprocess.run(
                [str(APP_EXECUTABLE)],
                cwd=ROOT,
                env=env,
                capture_output=True,
                text=True,
                timeout=120,
            )
            logs_dir = home / "Library" / "Logs" / "UnariSagiOperator"
            launcher_logs = sorted(logs_dir.glob("launcher_*.log")) if logs_dir.exists() else []
            log_text = "\n".join(path.read_text(encoding="utf-8", errors="ignore") for path in launcher_logs)
            expected = [
                "runtime check failed: root mismatch",
                "古いUnari Sagi Operatorが起動中です",
                "Macを再起動",
            ]
            missing = [item for item in expected if item not in log_text and item not in proc.stdout and item not in proc.stderr]
            return {
                "ok": proc.returncode != 0 and not missing,
                "returncode": proc.returncode,
                "missing": missing,
                "launcher_log_count": len(launcher_logs),
                "stdout_tail": proc.stdout[-800:],
                "stderr_tail": proc.stderr[-800:],
                "log_tail": log_text[-1200:],
            }
        finally:
            fake_server.terminate()
            try:
                fake_server.wait(timeout=5)
            except subprocess.TimeoutExpired:
                fake_server.kill()
            _kill_port(port)


def check_flask_api() -> dict:
    code = """
from ops_dashboard.app import app
client = app.test_client()
paths = ['/api/runtime/status', '/api/setup/status', '/api/sagi/status', '/api/capture/status', '/api/update/status']
for path in paths:
    res = client.get(path)
    assert res.status_code == 200, (path, res.status_code)
    assert res.is_json, path
runtime = client.get('/api/runtime/status').get_json()
assert runtime['ok'] is True
assert 'root' in runtime
assert 'version_info' in runtime
html = client.get('/').get_data(as_text=True)
assert 'setupJobLog' in html
assert '初回セットアップ実行ログ' in html
assert 'Instagram導入' in html
assert 'Google Sheets接続設定' in html
assert 'Android画面のInstagram' in html
assert '① まず件数を確認（本番はまだ走りません）' in html
assert '② 本番チェックを実行' in html
assert '途中から再開（ログイン追加後）' in html
assert '直近CSV' in html
assert 'チェック用ログイン追加' in html
assert '詳細設定 / CSVで実行する場合' in html
assert '書き戻さずに件数だけ確認' in html
assert '表示ログをコピー' in html
assert 'showLoadingIfFirst' in html
assert 'simple-status-row' in html
assert 'updateStatus' in html
assert '新しい版があります' in html
assert 'capturePassword' not in html
print('api ok')
"""
    python = str(ROOT / "venv" / "bin" / "python") if (ROOT / "venv" / "bin" / "python").exists() else sys.executable
    return run([python, "-c", code], timeout=120)


class _InlineScriptCollector(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self._in_inline_script = False
        self._current: list[str] = []
        self.scripts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "script":
            return
        attr_names = {name.lower() for name, _value in attrs}
        if "src" in attr_names:
            return
        self._in_inline_script = True
        self._current = []

    def handle_data(self, data: str) -> None:
        if self._in_inline_script:
            self._current.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "script" and self._in_inline_script:
            self.scripts.append("".join(self._current))
            self._current = []
            self._in_inline_script = False


def check_dashboard_js_syntax() -> dict:
    html_path = ROOT / "ops_dashboard" / "templates" / "index.html"
    html = html_path.read_text(encoding="utf-8")
    parser = _InlineScriptCollector()
    parser.feed(html)
    if not parser.scripts:
        return {"ok": False, "error": "no inline dashboard script found"}
    node = shutil.which("node")
    if node is None:
        return {"ok": True, "skipped": "node command not found; dashboard JS syntax check skipped locally"}
    checks: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix="unari_dashboard_js_check_") as td:
        for index, script in enumerate(parser.scripts, start=1):
            js_path = Path(td) / f"dashboard_inline_{index}.js"
            js_path.write_text(script, encoding="utf-8")
            checks.append(run([node, "--check", str(js_path)], timeout=60))
    return {
        "ok": all(item.get("ok") for item in checks),
        "script_count": len(parser.scripts),
        "checked": [
            {
                "cmd": item.get("cmd"),
                "ok": item.get("ok"),
                "stdout_tail": item.get("stdout", "")[-500:],
                "stderr_tail": item.get("stderr", "")[-500:],
            }
            for item in checks
        ],
    }


def check_dashboard_browser_smoke() -> dict:
    code = """
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.request import urlopen

from playwright.sync_api import sync_playwright

root = Path.cwd()
with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
    sock.bind(("127.0.0.1", 0))
    port = int(sock.getsockname()[1])

with TemporaryDirectory(prefix="unari_dashboard_browser_smoke_") as td:
    env = os.environ.copy()
    env.update(
        {
            "OPS_HOST": "127.0.0.1",
            "OPS_PORT": str(port),
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONPYCACHEPREFIX": str(Path(td) / "pycache"),
        }
    )
    proc = subprocess.Popen(
        [str(root / "venv" / "bin" / "python") if (root / "venv" / "bin" / "python").exists() else sys.executable, "-m", "ops_dashboard.app"],
        cwd=root,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        deadline = time.time() + 25
        while time.time() < deadline:
            try:
                with urlopen(f"http://127.0.0.1:{port}/api/runtime/status", timeout=2) as res:
                    if int(res.status) == 200:
                        break
            except Exception:
                time.sleep(0.5)
        else:
            raise AssertionError("dashboard server did not start")

        viewport_results = []
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            try:
                for name, viewport in [
                    ("desktop", {"width": 1366, "height": 900}),
                    ("mobile", {"width": 390, "height": 844}),
                ]:
                    page = browser.new_page(viewport=viewport)
                    errors = []
                    page.on("pageerror", lambda exc, errors=errors: errors.append(str(exc)))
                    page.on(
                        "console",
                        lambda msg, errors=errors: errors.append(msg.text)
                        if msg.type == "error" and "status of 409" not in msg.text and "status of 500" not in msg.text
                        else None,
                    )
                    capture_calls = {"count": 0}
                    capture_status_mode = {"fail": False}
                    sagi_status_mode = {"fail": False}

                    def fulfill_json(route, payload):
                        route.fulfill(
                            status=200,
                            content_type="application/json",
                            body=json.dumps(payload, ensure_ascii=False),
                        )

                    def capture_status(route):
                        if capture_status_mode["fail"]:
                            route.fulfill(
                                status=500,
                                content_type="text/html",
                                body="<html><body>capture status failed</body></html>",
                            )
                            return
                        capture_calls["count"] += 1
                        if capture_calls["count"] > 1:
                            time.sleep(0.8)
                        fulfill_json(
                            route,
                            {
                                "updated_at": "2026-07-05T12:00:00+09:00",
                                "wifi": {"ok": True, "output": "Current Wi-Fi Network: iPhone"},
                                "adb": {"ok": True, "output": "List of devices attached\\nemulator-5554 device product:sdk_gphone64_arm64 model:sdk_gphone64_arm64"},
                                "infra": {"ok": True, "output": "[OK] capture proxy設定 = 10.0.2.2:8080"},
                                "sessions": {"ok": True, "output": "合計: 0件"},
                                "captures": [],
                                "session_files": [],
                                "latest_job": None,
                                "jobs": [],
                                "setup_running": {
                                    "id": "setup1",
                                    "kind": "setup",
                                    "label": "初回セットアップ: まとめて実行",
                                    "status": "running",
                                    "outcome": "running",
                                    "current_step": "Android cmdline tools",
                                    "current_step_index": 2,
                                    "total_steps": 7,
                                    "log": ["setup internal should not leak into capture panel"],
                                },
                            },
                        )

                    def sagi_status(route):
                        if sagi_status_mode["fail"]:
                            route.fulfill(
                                status=500,
                                content_type="text/html",
                                body="<html><body>sagi status failed</body></html>",
                            )
                            return
                        fulfill_json(
                            route,
                            {
                                "ok": True,
                                "input_count": 100,
                                "needed_sessions": 2,
                                "healthy_sessions": 1,
                                "probe_sessions": None,
                                "raw_count": {"ok": True, "output": "1"},
                                "probe_count": {"ok": None, "output": "在庫確認ボタンでprobeします"},
                                "sessions": {"ok": True, "output": "合計: 1件"},
                                "sheets_bridge": {"ok": True, "output": "{\\"backend\\":\\"apps-script\\",\\"ok\\":true}"},
                                "latest_inputs": [],
                                "latest_results": [],
                                "no_proxy": True,
                            },
                        )

                    page.route(
                        "**/api/status",
                        lambda route: fulfill_json(
                            route,
                            {
                                "generated_at": "2026-07-05T12:00:00+09:00",
                                "overall": {
                                    "level": "green",
                                    "level_counts": {"red": 0, "yellow": 0, "green": 1},
                                    "top_actions": ["検査中"],
                                },
                                "links": [],
                            },
                        ),
                    )
                    page.route(
                        "**/api/update/status",
                        lambda route: fulfill_json(
                            route,
                            {
                                "ok": True,
                                "enabled": True,
                                "update_available": False,
                                "current": {"version": "2026.07.05.9"},
                                "latest": {"version": "2026.07.05.9", "download_url": ""},
                            },
                        ),
                    )
                    page.route(
                        "**/api/setup/status",
                        lambda route: fulfill_json(
                            route,
                            {
                                "root": str(root),
                                "android_home": str(root / "fake-sdk"),
                                "checks": {
                                    "python_runtime": {"ok": True, "summary": "Python環境は使えます"},
                                    "adb": {"ok": False, "summary": "adb がありません", "next_action": "Android SDK platform-tools を入れてください。"},
                                },
                                "latest_job": None,
                            },
                        ),
                    )
                    page.route("**/api/capture/status", capture_status)
                    page.route(
                        "**/api/capture/run-all",
                        lambda route: route.fulfill(
                            status=409,
                            content_type="application/json",
                            body=json.dumps(
                                {"ok": False, "error": "実行中のジョブがあります: 初回セットアップ: まとめて実行"},
                                ensure_ascii=False,
                            ),
                        ),
                    )
                    page.route(
                        "**/api/capture/verify",
                        lambda route: route.fulfill(
                            status=200,
                            content_type="application/json",
                            body=json.dumps(
                                {
                                    "ok": True,
                                    "job": {
                                        "id": "poll-fail",
                                        "kind": "capture",
                                        "label": "チェック用ログイン verify: sample",
                                        "status": "running",
                                        "outcome": "running",
                                        "current_step": "チェック用ログイン verify",
                                        "current_step_index": 1,
                                        "total_steps": 1,
                                        "commands": [{"name": "チェック用ログイン verify"}],
                                        "log": ["polling starts"],
                                    },
                                },
                                ensure_ascii=False,
                            ),
                        ),
                    )
                    page.route(
                        "**/api/jobs/poll-fail",
                        lambda route: route.fulfill(
                            status=500,
                            content_type="text/plain",
                            body="job polling failed",
                        ),
                    )
                    page.route("**/api/sagi/status**", sagi_status)
                    page.route(
                        "**/api/sagi/check",
                        lambda route: route.fulfill(
                            status=409,
                            content_type="application/json",
                            body=json.dumps(
                                {"ok": False, "error": "実行中のジョブがあります: 詐欺チェック: 本番実行"},
                                ensure_ascii=False,
                            ),
                        ),
                    )
                    page.route(
                        "**/api/sagi/notify-test",
                        lambda route: route.fulfill(
                            status=500,
                            content_type="text/plain",
                            body="temporary server failure",
                        ),
                    )
                    page.goto(f"http://127.0.0.1:{port}/?operator=1", wait_until="domcontentloaded", timeout=30000)
                    page.wait_for_selector("text=Unari Sagi Operator", timeout=10000)
                    page.wait_for_selector("text=チェック用ログイン追加", timeout=10000)
                    page.wait_for_selector("text=① まず件数を確認（本番はまだ走りません）", timeout=10000)
                    page.wait_for_timeout(5000)
                    metrics = page.evaluate(
                        \"\"\"() => ({
                            bodyLen: document.body.innerText.trim().length,
                            scrollWidth: document.documentElement.scrollWidth,
                            clientWidth: document.documentElement.clientWidth,
                            hasSetup: document.body.innerText.includes('初回セットアップ'),
                            hasCapture: document.body.innerText.includes('チェック用ログイン追加'),
                            hasSagi: document.body.innerText.includes('詐欺チェック実行'),
                            hasMainButton: document.body.innerText.includes('① まず件数を確認（本番はまだ走りません）'),
                            loadingTextCount: (document.body.innerText.match(/状態取得中/g) || []).length,
                            capturePanelText: document.querySelector('#capture-panel')?.innerText || '',
                            captureStatusText: document.querySelector('#captureStatus')?.innerText || '',
                            captureJobText: document.querySelector('#jobLog')?.innerText || '',
                            captureProgressText: document.querySelector('#jobProgress')?.innerText || ''
                        })\"\"\"
                    )
                    before_refresh_capture = page.locator("#captureStatus").inner_text()
                    page.evaluate("() => { loadCaptureStatus(); }")
                    page.wait_for_timeout(100)
                    during_refresh_capture = page.locator("#captureStatus").inner_text()
                    page.wait_for_timeout(1000)
                    after_refresh_capture = page.locator("#captureStatus").inner_text()
                    capture_status_mode["fail"] = True
                    page.evaluate("() => { loadCaptureStatus(); }")
                    page.wait_for_timeout(1000)
                    capture_status_error = page.locator("#captureStatus").inner_text()
                    capture_status_mode["fail"] = False
                    sagi_status_mode["fail"] = True
                    page.evaluate("() => { loadSagiStatus(); }")
                    page.wait_for_timeout(1000)
                    sagi_status_error = page.locator("#sagiStatus").inner_text()
                    sagi_status_mode["fail"] = False
                    collapsed_log_text = page.evaluate(
                        \"\"\"() => {
                            renderJob({
                                label: 'チェック用ログイン作成: sample',
                                status: 'running',
                                outcome: 'running',
                                next_action: '実行中です。',
                                current_step: 'チェック用ログイン verify',
                                current_step_index: 1,
                                total_steps: 1,
                                commands: [{name: 'チェック用ログイン verify'}],
                                log: ['line 1', 'line 2', 'line 3']
                            }, 'capture');
                            return document.querySelector('#jobLog')?.textContent || '';
                        }\"\"\"
                    )
                    busy_surface = page.evaluate(
                        \"\"\"async () => {
                            await postJob('/api/sagi/check', {input_csv: 'logs/sagi_operator_input_20260705_120000.csv'}, 'sagi');
                            const sagiBusyState = document.querySelector('#sagiJobState')?.innerText || '';
                            const sagiBusyNext = document.querySelector('#sagiJobNextAction')?.innerText || '';
                            const sagiBusyProgress = document.querySelector('#sagiJobProgress')?.innerText || '';

                            await postJob('/api/capture/run-all', {username: 'sample', confirm_tethering: true}, 'capture');
                            const captureBusyState = document.querySelector('#jobState')?.innerText || '';
                            const captureBusyNext = document.querySelector('#jobNextAction')?.innerText || '';
                            const captureBusyProgress = document.querySelector('#jobProgress')?.innerText || '';

                            return {
                                sagiBusyState,
                                sagiBusyNext,
                                sagiBusyProgress,
                                captureBusyState,
                                captureBusyNext,
                                captureBusyProgress
                            };
                        }\"\"\"
                    )
                    transport_surface = page.evaluate(
                        \"\"\"async () => {
                            await postJob('/api/sagi/notify-test', {requester: '藤巻'}, 'sagi');
                            const state = document.querySelector('#sagiJobState')?.innerText || '';
                            const next = document.querySelector('#sagiJobNextAction')?.innerText || '';
                            const progress = document.querySelector('#sagiJobProgress')?.innerText || '';
                            return {state, next, progress};
                        }\"\"\"
                    )
                    poll_surface = page.evaluate(
                        \"\"\"async () => {
                            await postJob('/api/capture/verify', {username: 'sample'}, 'capture');
                            await new Promise(resolve => setTimeout(resolve, 3200));
                            const state = document.querySelector('#jobState')?.innerText || '';
                            const next = document.querySelector('#jobNextAction')?.innerText || '';
                            const progress = document.querySelector('#jobProgress')?.innerText || '';
                            return {state, next, progress};
                        }\"\"\"
                    )
                    error_surface = page.evaluate(
                        \"\"\"() => {
                            const capacity = 'チェック対象は100件です。1つのチェック用ログインで1日50件まで確認できます。今回は2個必要ですが、今使えるのは1個です。あと1個、新しいInstagramアカウントでチェック用ログインを作ってください。';
                            renderJob({
                                label: '詐欺チェック: 本番実行',
                                status: 'failed',
                                outcome: 'capacity_shortage',
                                next_action: capacity,
                                current_step: '強session必要本数チェック',
                                current_step_index: 1,
                                total_steps: 3,
                                commands: [{name: '強session必要本数チェック'}, {name: 'dry-run'}, {name: 'api_warning_check 本番'}],
                                log: ['NEEDS_SUPPLEMENT target_count=100 needed_sessions=2 healthy_sessions=1 missing_sessions=1']
                            }, 'sagi');
                            const capacityState = document.querySelector('#sagiJobState')?.innerText || '';
                            const capacityNext = document.querySelector('#sagiJobNextAction')?.innerText || '';

                            const login = 'Instagram側で再ログインが必要になりました。結果CSVは消さずに残っています。別のInstagramアカウントで「チェック用ログインを1つ作る」を実行し、完了後に「途中から再開」を押してください。';
                            renderJob({
                                label: '詐欺チェック: 続きから再開',
                                status: 'failed',
                                outcome: 'login_required',
                                next_action: login,
                                current_step: 'api_warning_check 本番',
                                current_step_index: 3,
                                total_steps: 3,
                                commands: [{name: '強session必要本数チェック'}, {name: 'dry-run'}, {name: 'api_warning_check 本番'}],
                                log: ['LoginRequired: relogin needed', 'logs/sagi_operator_result_20260705_120000.csv']
                            }, 'sagi');
                            const loginState = document.querySelector('#sagiJobState')?.innerText || '';
                            const loginNext = document.querySelector('#sagiJobNextAction')?.innerText || '';

                            renderJob({
                                label: 'チェック用ログイン作成: sample',
                                status: 'failed',
                                outcome: 'port_conflict',
                                next_action: '通信準備で止まりました。Macを再起動してから、Unari Sagi Operatorを開き直してください。',
                                current_step: 'AVD/mitmdump/Fridaを起動確認',
                                current_step_index: 1,
                                total_steps: 2,
                                commands: [{name: 'AVD/mitmdump/Fridaを起動確認'}, {name: 'AVDで手動ログイン'}],
                                log: ['[Errno 48] address already in use']
                            }, 'capture');
                            const portState = document.querySelector('#jobState')?.innerText || '';
                            const portNext = document.querySelector('#jobNextAction')?.innerText || '';

                            return {capacityState, capacityNext, loginState, loginNext, portState, portNext};
                        }\"\"\"
                    )
                    metrics["errors"] = errors
                    metrics["duringRefreshCaptureText"] = during_refresh_capture
                    metrics["afterRefreshCaptureText"] = after_refresh_capture
                    metrics["collapsedLogText"] = collapsed_log_text
                    metrics["busySurface"] = busy_surface
                    metrics["transportSurface"] = transport_surface
                    metrics["pollSurface"] = poll_surface
                    metrics["errorSurface"] = error_surface
                    metrics["statusFailureSurface"] = {
                        "capture": capture_status_error,
                        "sagi": sagi_status_error,
                    }
                    metrics["noHorizontalOverflow"] = metrics["scrollWidth"] <= metrics["clientWidth"] + 2
                    assert metrics["bodyLen"] > 500, (name, metrics)
                    assert metrics["hasSetup"] and metrics["hasCapture"] and metrics["hasSagi"], (name, metrics)
                    assert metrics["hasMainButton"], (name, metrics)
                    assert metrics["noHorizontalOverflow"], (name, metrics)
                    assert metrics["loadingTextCount"] == 0, (name, metrics)
                    assert "初回セットアップ完了後に使えます" in metrics["capturePanelText"], (name, metrics)
                    assert "Android cmdline tools" not in metrics["capturePanelText"], (name, metrics)
                    assert "setup internal should not leak" not in metrics["capturePanelText"], (name, metrics)
                    assert "初回セットアップ: まとめて実行 / 実行中" not in metrics["capturePanelText"], (name, metrics)
                    assert "状態取得中" not in during_refresh_capture, (name, during_refresh_capture, before_refresh_capture)
                    assert len(during_refresh_capture.strip()) > 20, (name, during_refresh_capture)
                    assert after_refresh_capture.strip(), (name, after_refresh_capture)
                    assert "ログは閉じています" in collapsed_log_text, (name, collapsed_log_text)
                    assert "別の処理が実行中" in busy_surface["sagiBusyState"], (name, busy_surface)
                    assert "入力確認" not in busy_surface["sagiBusyState"], (name, busy_surface)
                    assert "詐欺チェック: 本番実行" in busy_surface["sagiBusyNext"], (name, busy_surface)
                    assert "終わるまで待って" in busy_surface["sagiBusyNext"], (name, busy_surface)
                    assert "待機中" in busy_surface["sagiBusyProgress"], (name, busy_surface)
                    assert "別の処理が実行中" in busy_surface["captureBusyState"], (name, busy_surface)
                    assert "初回セットアップ: まとめて実行" in busy_surface["captureBusyNext"], (name, busy_surface)
                    assert "終わるまで待って" in busy_surface["captureBusyNext"], (name, busy_surface)
                    assert "通信エラー" in transport_surface["state"], (name, transport_surface)
                    assert "正しい応答" in transport_surface["next"], (name, transport_surface)
                    assert "HTTP 500" in transport_surface["next"], (name, transport_surface)
                    assert "通信確認" in transport_surface["progress"], (name, transport_surface)
                    assert "通信エラー" in poll_surface["state"], (name, poll_surface)
                    assert "実行状況を取得" in poll_surface["next"], (name, poll_surface)
                    assert "HTTP 500" in poll_surface["next"], (name, poll_surface)
                    assert "通信確認" in poll_surface["progress"], (name, poll_surface)
                    assert "状態取得に失敗" in capture_status_error, (name, capture_status_error)
                    assert "状態取得に失敗" in sagi_status_error, (name, sagi_status_error)
                    assert "SyntaxError" not in capture_status_error, (name, capture_status_error)
                    assert "SyntaxError" not in sagi_status_error, (name, sagi_status_error)
                    assert "capture status failed" not in capture_status_error, (name, capture_status_error)
                    assert "sagi status failed" not in sagi_status_error, (name, sagi_status_error)
                    assert "チェック用ログイン不足" in error_surface["capacityState"], (name, error_surface)
                    assert "チェック対象は100件" in error_surface["capacityNext"], (name, error_surface)
                    assert "あと1個" in error_surface["capacityNext"], (name, error_surface)
                    assert "再ログインが必要" in error_surface["loginState"], (name, error_surface)
                    assert "結果CSVは消さず" in error_surface["loginNext"], (name, error_surface)
                    assert "途中から再開" in error_surface["loginNext"], (name, error_surface)
                    assert "通信準備エラー" in error_surface["portState"], (name, error_surface)
                    assert "Macを再起動" in error_surface["portNext"], (name, error_surface)
                    assert not errors, (name, errors)
                    viewport_results.append({"name": name, **metrics})
                    page.close()
            finally:
                browser.close()
        print(json.dumps({"ok": True, "port": port, "viewports": viewport_results}, ensure_ascii=False))
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
"""
    python = str(ROOT / "venv" / "bin" / "python") if (ROOT / "venv" / "bin" / "python").exists() else sys.executable
    return run([python, "-c", code], timeout=120)


def check_update_download_flow() -> dict:
    code = """
import hashlib
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory

from ops_dashboard import update_check


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


old_config = update_check.CONFIG_PATH
old_version = update_check.VERSION_PATH
old_home = os.environ.get("HOME")
try:
    with TemporaryDirectory(prefix="unari_update_flow_") as td:
        root = Path(td)
        fake_home = root / "home"
        fake_home.mkdir()
        os.environ["HOME"] = str(fake_home)

        version_path = root / "sagi_operator_version.json"
        config_path = root / "sagi_operator_update.json"
        manifest_path = root / "latest.json"
        dmg_path = root / "UnariSagiOperator-2099.01.01.1.dmg"
        dmg_path.write_bytes(b"fake-dmg-v1")
        expected_sha = sha256(dmg_path)

        version_path.write_text(
            json.dumps({"version": "2026.07.05.1", "build": "old"}, ensure_ascii=False),
            encoding="utf-8",
        )
        manifest = {
            "app": "Unari Sagi Operator",
            "version": "2099.01.01.1",
            "build": "testbuild",
            "download_url": dmg_path.as_uri(),
            "assets": {
                "dmg": {
                    "name": dmg_path.name,
                    "url": dmg_path.as_uri(),
                    "sha256": expected_sha,
                    "size_bytes": dmg_path.stat().st_size,
                }
            },
        }
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
        config_path.write_text(
            json.dumps({"enabled": True, "latest_url": str(manifest_path), "check_timeout_seconds": 2}, ensure_ascii=False),
            encoding="utf-8",
        )

        update_check.CONFIG_PATH = config_path
        update_check.VERSION_PATH = version_path

        status = update_check.collect_update_status()
        assert status["ok"] is True
        assert status["enabled"] is True
        assert status["update_available"] is True
        assert status["latest"]["version"] == "2099.01.01.1"

        dest = fake_home / "Downloads" / dmg_path.name
        dest.parent.mkdir(parents=True)
        dest.write_bytes(b"corrupt-old-download")
        result = update_check.download_latest_update(open_after=False)
        assert result["ok"] is True, result
        assert result["opened"] is False
        assert Path(result["path"]) == dest
        assert dest.read_bytes() == b"fake-dmg-v1"
        assert result["sha256"] == expected_sha

        manifest["assets"]["dmg"]["sha256"] = "0" * 64
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
        dest.write_bytes(b"corrupt-again")
        bad = update_check.download_latest_update(open_after=False)
        assert bad["ok"] is False
        assert "検証に失敗" in bad["message"]
        assert not dest.exists(), "bad SHA download must be removed"
finally:
    update_check.CONFIG_PATH = old_config
    update_check.VERSION_PATH = old_version
    if old_home is None:
        os.environ.pop("HOME", None)
    else:
        os.environ["HOME"] = old_home

print("update download flow ok")
"""
    python = str(ROOT / "venv" / "bin" / "python") if (ROOT / "venv" / "bin" / "python").exists() else sys.executable
    return run([python, "-c", code], timeout=120)


def check_sagi_sheet_job_wiring() -> dict:
    code = """
from ops_dashboard import check_jobs

captured = []

def fake_new_job(label, commands, **kwargs):
    captured.append((label, commands, kwargs))
    return {"id": "fake", "label": label, "status": "running"}

old_new_job = check_jobs._new_job
old_active_job = check_jobs._active_job
try:
    check_jobs._new_job = fake_new_job
    check_jobs._active_job = lambda: None
    job, error = check_jobs.start_sheet_check_job(
        sheet_url="https://docs.google.com/spreadsheets/d/abc123/edit",
        tab_name="7_3",
    )
    assert error is None, error
    assert job["id"] == "fake"
    label, commands, kwargs = captured[0]
    assert label == "詐欺チェック: ①取込と件数確認"
    assert "② 本番チェックを実行" in kwargs["success_next_action"]
    assert commands[0]["cmd"][:4] == [check_jobs.PYTHON, "-u", "scripts/sagi_operator_extract_input.py", "--output"]
    assert "--sheet-url" in commands[0]["cmd"]
    assert "--tab-name" in commands[0]["cmd"]
    assert commands[1]["name"] == "強session必要本数チェック"
    assert commands[1]["cmd"][:2] == [check_jobs.PYTHON, "-c"]
    assert "NEEDS_SUPPLEMENT" in commands[1]["cmd"][2]
    assert len(commands) == 2

    resume_job, resume_error = check_jobs.start_check_job(
        "ops_dashboard/check_jobs.py",
        result_csv="ops_dashboard/check_jobs.py",
        resume=True,
    )
    assert resume_error is None, resume_error
    resume_commands = captured[1][1]
    assert resume_commands[0]["name"] == "強session必要本数チェック"
    assert "チェック済み" in resume_commands[0]["cmd"][2]
    assert "--dry-run" in resume_commands[1]["cmd"]
    resume_cmd = resume_commands[2]["cmd"]
    assert "--resume" in resume_cmd
    assert "--output" in resume_cmd

    _job, missing = check_jobs.start_sheet_check_job(sheet_url="", sheet_id="", tab_name="7_3")
    assert "Google Sheets URL" in missing
finally:
    check_jobs._new_job = old_new_job
    check_jobs._active_job = old_active_job
print("sagi sheet job wiring ok")
"""
    python = str(ROOT / "venv" / "bin" / "python") if (ROOT / "venv" / "bin" / "python").exists() else sys.executable
    return run([python, "-c", code], timeout=120)


def check_sagi_api_validation() -> dict:
    code = """
from ops_dashboard import app as dashboard_app
from ops_dashboard import capture_jobs

with capture_jobs._LOCK:
    capture_jobs._JOBS.clear()

client = dashboard_app.app.test_client()


def post_error(path, payload):
    res = client.post(path, json=payload)
    assert res.status_code == 400, (path, payload, res.status_code, res.get_data(as_text=True))
    data = res.get_json()
    assert data["ok"] is False, data
    assert isinstance(data["error"], str) and data["error"], data
    return data["error"]


assert "Google Sheets URL" in post_error("/api/sagi/sheet-check", {"tab_name": "7_3"})
assert "タブ名を入力" in post_error("/api/sagi/sheet-check", {"sheet_url": "https://docs.google.com/spreadsheets/d/abc/edit"})
assert "タブ名を入力" in post_error("/api/sagi/extract", {"sheet_url": "https://docs.google.com/spreadsheets/d/abc/edit"})
assert "シートURL/IDまたはCSVファイル" in post_error("/api/sagi/extract", {})
assert "このアプリ内のファイル" in post_error("/api/sagi/extract", {"csv_path": "../outside.csv"})
assert "CSVファイル" in post_error("/api/sagi/check", {})
assert "続きから再開するには結果CSV" in post_error(
    "/api/sagi/check",
    {"input_csv": "ops_dashboard/check_jobs.py", "resume": True},
)
assert "Google Sheets URLとタブ名" in post_error(
    "/api/sagi/writeback",
    {"result_csv": "ops_dashboard/check_jobs.py"},
)
assert "Slack通知先" in post_error("/api/sagi/notify-test", {})
print("sagi api validation ok")
"""
    python = str(ROOT / "venv" / "bin" / "python") if (ROOT / "venv" / "bin" / "python").exists() else sys.executable
    return run([python, "-c", code], timeout=120)


def check_job_busy_api_conflicts() -> dict:
    code = """
from ops_dashboard import app as dashboard_app
from ops_dashboard import capture_jobs

busy_label = "詐欺チェック: 本番実行"
with capture_jobs._LOCK:
    capture_jobs._JOBS.clear()
    capture_jobs._JOBS["busy-job"] = {
        "id": "busy-job",
        "kind": "sagi",
        "label": busy_label,
        "status": "running",
        "log": [],
    }

client = dashboard_app.app.test_client()


def assert_busy(path, payload):
    res = client.post(path, json=payload)
    assert res.status_code == 409, (path, payload, res.status_code, res.get_data(as_text=True))
    data = res.get_json()
    assert data["ok"] is False, data
    assert "実行中のジョブがあります" in data["error"], data
    assert busy_label in data["error"], data


try:
    for path, payload in [
        ("/api/setup/run", {"action": "venv"}),
        ("/api/capture/start-infra", {}),
        ("/api/capture/run-all", {"username": "sample", "confirm_tethering": True}),
        ("/api/capture/import-latest", {"username": "sample"}),
        ("/api/capture/verify", {"username": "sample"}),
        ("/api/sagi/extract", {"sheet_url": "https://docs.google.com/spreadsheets/d/abc/edit", "tab_name": "7_3"}),
        ("/api/sagi/sheet-check", {"sheet_url": "https://docs.google.com/spreadsheets/d/abc/edit", "tab_name": "7_3"}),
        ("/api/sagi/inventory", {}),
        ("/api/sagi/dryrun", {"input_csv": "ops_dashboard/check_jobs.py"}),
        ("/api/sagi/check", {"input_csv": "ops_dashboard/check_jobs.py"}),
        (
            "/api/sagi/writeback",
            {"result_csv": "ops_dashboard/check_jobs.py", "sheet_id": "abc", "tab_name": "7_3"},
        ),
        ("/api/sagi/notify-test", {"requester": "藤巻"}),
    ]:
        assert_busy(path, payload)
finally:
    with capture_jobs._LOCK:
        capture_jobs._JOBS.clear()

print("job busy api conflicts ok")
"""
    python = str(ROOT / "venv" / "bin" / "python") if (ROOT / "venv" / "bin" / "python").exists() else sys.executable
    return run([python, "-c", code], timeout=120)


def check_setup_job_wiring() -> dict:
    code = """
from pathlib import Path
from ops_dashboard import setup_jobs

captured = []

def fake_new_job(label, commands, **kwargs):
    captured.append((label, commands))
    return {"id": "fake", "label": label, "status": "running"}

setup_jobs._new_job = fake_new_job
setup_jobs._sheets_bridge_info = lambda: {"backend": "google-api", "ok": False, "token_exists": False}

missing_job, missing_error = setup_jobs.start_setup_job("google-auth")
assert missing_job is None
assert "Google API認証ファイルがありません" in missing_error

setup_jobs._sheets_bridge_info = lambda: {"backend": "google-api", "ok": True, "token_exists": False}

for action in ("venv", "device", "instagram", "google-auth", "all"):
    job, error = setup_jobs.start_setup_job(action)
    assert error is None, (action, error)
    assert job["id"] == "fake", action

venv_commands = captured[0][1]
device_commands = captured[1][1]
instagram_commands = captured[2][1]
google_auth_commands = captured[3][1]
all_commands = captured[4][1]

assert not any("install --upgrade pip" in " ".join(step["cmd"]) for step in venv_commands)
assert device_commands[0]["cmd"] == ["bash", "scripts/ensure_capture_infra.sh", "--prepare-device"]
assert device_commands[1]["cmd"] == ["bash", "scripts/setup_ig_capture_device.sh", "all"]
assert device_commands[2]["cmd"] == ["bash", "scripts/ensure_capture_infra.sh"]
assert any(step["cmd"] == ["bash", "scripts/ensure_capture_infra.sh", "--prepare-device"] for step in all_commands)
assert any(step["cmd"] == ["bash", "scripts/setup_ig_capture_device.sh", "all"] for step in all_commands)
assert instagram_commands[0]["cmd"] == ["bash", "scripts/install_instagram_apk.sh"]
assert google_auth_commands[0]["cmd"][:3] == [setup_jobs.PYTHON, "-u", "scripts/sheets_auth.py"]
assert google_auth_commands[0]["env"]["SHEETS_AUTH_CONSOLE"] == "0"
assert all_commands[-2]["cmd"] == ["bash", "scripts/ensure_capture_infra.sh"]
assert all_commands[-1]["cmd"] == ["bash", "scripts/install_instagram_apk.sh"]

old_adb = setup_jobs.ADB
old_avd_exists = setup_jobs._avd_exists
old_connected_devices = setup_jobs._connected_devices
old_running_avd_name = setup_jobs._running_avd_name
old_quick_run = setup_jobs._quick_run
try:
    setup_jobs.ADB = Path("/bin/echo")
    setup_jobs._avd_exists = lambda: False
    no_avd = setup_jobs._instagram_status()
    assert no_avd["ok"] is False
    assert "Android画面 ig_capture" in no_avd["summary"]

    setup_jobs._avd_exists = lambda: True
    setup_jobs._connected_devices = lambda: ["emulator-5554"]
    setup_jobs._running_avd_name = lambda serial: "wrong_avd"
    wrong_avd = setup_jobs._instagram_status()
    assert wrong_avd["ok"] is False
    assert "ig_capture ではありません" in wrong_avd["summary"]

    setup_jobs._running_avd_name = lambda serial: setup_jobs.AVD_NAME
    setup_jobs._quick_run = lambda cmd, timeout=20: {
        "ok": True,
        "code": 0,
        "output": "package:/data/app/com.instagram.android/base.apk",
    }
    instagram_ok = setup_jobs._instagram_status()
    assert instagram_ok["ok"] is True
finally:
    setup_jobs.ADB = old_adb
    setup_jobs._avd_exists = old_avd_exists
    setup_jobs._connected_devices = old_connected_devices
    setup_jobs._running_avd_name = old_running_avd_name
    setup_jobs._quick_run = old_quick_run
print("setup wiring ok")
"""
    python = str(ROOT / "venv" / "bin" / "python") if (ROOT / "venv" / "bin" / "python").exists() else sys.executable
    return run([python, "-c", code], timeout=120)


def check_related_log_stitching() -> dict:
    code = """
from pathlib import Path
from tempfile import TemporaryDirectory
from ops_dashboard import capture_jobs

old_root = capture_jobs.ROOT
job_id = "fake-log-tail"
try:
    with TemporaryDirectory(prefix="unari_operator_log_tail_") as td:
        root = Path(td)
        capture_jobs.ROOT = root
        log_dir = root / "logs"
        log_dir.mkdir(parents=True)
        log_path = log_dir / "mitmdump_keepalive_test.log"
        log_path.write_text(
            "first line\\n"
            "Cookie: sessionid=secret_cookie_value\\n"
            "password=secret_password_value\\n"
            "last diagnostic line\\n",
            encoding="utf-8",
        )
        with capture_jobs._LOCK:
            capture_jobs._JOBS[job_id] = {
                "id": job_id,
                "label": "fake",
                "status": "running",
                "log": [f"PID=1 LOG={log_path}"],
            }
        capture_jobs._append_related_log_tails(job_id, [f"PID=1 LOG={log_path}"])
        job = capture_jobs.get_job(job_id)
        text = "\\n".join(job["log"])
        assert "関連する内部ログの末尾" in text
        assert "last diagnostic line" in text
        assert "secret_cookie_value" not in text
        assert "secret_password_value" not in text
        assert "[REDACTED]" in text
finally:
    capture_jobs.ROOT = old_root
    with capture_jobs._LOCK:
        capture_jobs._JOBS.pop(job_id, None)
print("related log stitching ok")
"""
    python = str(ROOT / "venv" / "bin" / "python") if (ROOT / "venv" / "bin" / "python").exists() else sys.executable
    return run([python, "-c", code], timeout=120)


def check_launcher_stale_server_restart() -> dict:
    text = (ROOT / "scripts" / "install_sagi_operator_app.py").read_text(encoding="utf-8")
    required = [
        "stop_existing_operator_server",
        "stopping existing operator server",
        "UnariSagiOperator/unari/ops_dashboard/app.py",
        "runtime_matches_current_bundle",
        "/api/runtime/status",
        "root mismatch",
        "古いUnari Sagi Operatorが起動中",
        "Macを再起動",
        "operator=1&t=$(date +%s)",
    ]
    missing = [item for item in required if item not in text]
    return {"ok": not missing, "missing": missing}


def check_app_build_lock() -> dict:
    text = (ROOT / "scripts" / "install_sagi_operator_app.py").read_text(encoding="utf-8")
    required = [
        "import fcntl",
        ".sagi_operator_build.lock",
        "fcntl.flock(lock_file, fcntl.LOCK_EX)",
        "lock_file.close()",
    ]
    missing = [item for item in required if item not in text]
    return {"ok": not missing, "missing": missing}


def check_published_release_verifier_wiring() -> dict:
    files = {
        ".github/workflows/release.yml": [
            "requirements-ci.txt",
            "Verify published GitHub Release",
            "scripts/verify_published_release.py",
            "scripts/sagi_operator_release_check.py --member-first-launch",
            "releases/latest/download/latest.json",
            "--version \"$VERSION\"",
            "--build \"$build\"",
            "--check-assets",
            "--download-dmg",
            "--first-launch",
            "--retries 30",
            "--retry-delay 10",
            "Delete failed GitHub Release",
            "gh release delete",
            "--cleanup-tag",
        ],
        "Makefile": [
            "sagi-operator-published-smoke",
            "scripts/verify_published_release.py",
            "scripts/sagi_operator_release_check.py --member-first-launch",
            "PUBLISHED_LATEST_URL",
            "PUBLISHED_DOWNLOAD_DMG",
            "PUBLISHED_FIRST_LAUNCH",
        ],
        "README.md": [
            "requirements-ci.txt",
            "Python wheelhouseを同梱",
            "PUBLISHED_FIRST_LAUNCH=1",
            "sagi-operator-published-smoke",
            "公開URLの `latest.json` を読み直し",
            "公開DMGを実際にダウンロード",
        ],
        "scripts/verify_published_release.py": [
            "def verify_manifest",
            "download_url must match assets.dmg.url",
            "--check-assets",
            "--download-dmg",
            "--first-launch",
            '[hdiutil, "verify"',
            "sagi_operator_version.json",
            "--retries",
        ],
    }
    missing: dict[str, list[str]] = {}
    for rel_path, required in files.items():
        path = ROOT / rel_path
        if not path.exists():
            missing[rel_path] = ["file not found"]
            continue
        text = path.read_text(encoding="utf-8")
        absent = [item for item in required if item not in text]
        if absent:
            missing[rel_path] = absent
    return {"ok": not missing, "missing": missing}


def check_release_docs_guardrails() -> dict:
    docs = {
        "README.md": (ROOT / "README.md").read_text(encoding="utf-8"),
        ".github/workflows/release.yml": (ROOT / ".github" / "workflows" / "release.yml").read_text(
            encoding="utf-8"
        ),
    }
    project_status = ROOT / "docs" / "project_status_2026-07-04.html"
    if project_status.exists():
        docs["docs/project_status_2026-07-04.html"] = project_status.read_text(encoding="utf-8")

    forbidden_patterns = {
        "fixed version DMG URL": r"releases/latest/download/UnariSagiOperator-\d{4}\.\d{2}\.\d{2}\.\d+\.dmg",
        "direct versioned DMG filename": r"https?://[^\s)]+UnariSagiOperator-\d{4}\.\d{2}\.\d{2}\.\d+\.dmg",
    }
    forbidden = {
        f"{rel_path}: {name}": re.findall(pattern, text)
        for rel_path, text in docs.items()
        for name, pattern in forbidden_patterns.items()
        if re.findall(pattern, text)
    }

    required = {
        "README.md": [
            "READMEには固定のDMG URLを書かない",
            "公開配布の正はGitHub Releaseと `latest.json`",
            "ローカルの `dist/` は作業用の生成物",
            "通常pushとメンバー配布は別",
        ],
        ".github/workflows/release.yml": [
            "公開配布の正はGitHub Releaseとlatest.jsonです",
            "ローカルのdist/は作業用生成物",
            "### latest.json",
        ],
    }
    if "docs/project_status_2026-07-04.html" in docs:
        required["docs/project_status_2026-07-04.html"] = [
            "GitHub ActionsでReleaseを作っても、手元の <code>dist/</code> は自動更新されません",
            "GitHub Releaseと公開 <code>latest.json</code> を正とします",
            "READMEには固定バージョンのDMG URLを書かず",
        ]
    missing: dict[str, list[str]] = {}
    for rel_path, needles in required.items():
        text = docs[rel_path]
        absent = [needle for needle in needles if needle not in text]
        if absent:
            missing[rel_path] = absent

    return {"ok": not forbidden and not missing, "forbidden": forbidden, "missing": missing}


def check_capture_diagnostics() -> dict:
    code = """
from pathlib import Path
from ops_dashboard import capture_jobs
from scripts import shin_capture_auto

_classify_result = capture_jobs._classify_result

old_quick_run = capture_jobs._quick_run
old_latest_files = capture_jobs._latest_files
with capture_jobs._LOCK:
    capture_jobs._JOBS.clear()
    capture_jobs._JOBS["setup1"] = {
        "id": "setup1",
        "kind": "setup",
        "label": "初回セットアップ: まとめて実行",
        "status": "running",
        "started_at": "2026-07-04T20:00:00+09:00",
    }
    capture_jobs._JOBS["capture1"] = {
        "id": "capture1",
        "kind": "capture",
        "label": "チェック用ログイン作成: sample",
        "status": "failed",
        "started_at": "2026-07-04T19:00:00+09:00",
    }
try:
    capture_jobs._quick_run = lambda *args, **kwargs: {"ok": True, "code": 0, "output": ""}
    capture_jobs._latest_files = lambda *args, **kwargs: []
    status = capture_jobs.collect_capture_status()
    assert status["latest_job"]["id"] == "capture1"
    assert status["setup_running"]["id"] == "setup1"
finally:
    capture_jobs._quick_run = old_quick_run
    capture_jobs._latest_files = old_latest_files
    with capture_jobs._LOCK:
        capture_jobs._JOBS.clear()

frida = _classify_result(1, ["[FAIL] frida-server 入れ直し失敗 (AVD capture設定をやり直してください)"])
assert "通信用設定" in frida["next_action"]

port = _classify_result(1, ["[Errno 48] address already in use"])
assert port["outcome"] == "port_conflict"
assert "Macを再起動" in port["next_action"]

other_user_port = _classify_result(1, ["[NG] port 8080 は別ユーザー(tesuto)のプロセスが使用中です。"])
assert other_user_port["outcome"] == "port_conflict"
assert "Macを再起動" in other_user_port["next_action"]

capacity = _classify_result(5, ["NEEDS_SUPPLEMENT target_count=100 needed_sessions=2 healthy_sessions=1 missing_sessions=1"])
assert capacity["outcome"] == "capacity_shortage"
assert "チェック対象は100件" in capacity["next_action"]
assert "あと1個" in capacity["next_action"]

login_required = _classify_result(5, ["LoginRequired: relogin needed"])
assert login_required["outcome"] == "login_required"
assert "Instagram側で再ログイン" in login_required["next_action"]
assert "チェック用ログインを1つ作る" in login_required["next_action"]

missing_ca = _classify_result(1, ["✗ /Users/tesuto/.mitmproxy/mitmproxy-ca-cert.pem が無い。一度 mitmdump を起動してCAを生成してください"])
assert "通信の証明書" in missing_ca["next_action"]
assert "通信用設定" in missing_ca["next_action"]

apk = _classify_result(1, ["InstagramのAPK/APKM/XAPKが見つかりません。"])
assert apk["outcome"] == "manual_needed"
assert "Instagram導入" in apk["next_action"]
assert "同梱" in apk["next_action"]
assert "Play Storeなしは正常" in apk["next_action"]

ig_flag = _classify_result(3, ["IGFlaggedError: Unable to log in: An unexpected error occurred."])
assert ig_flag["outcome"] == "manual_needed"
assert "ログイン連打" in ig_flag["next_action"]
assert "通信用設定" in ig_flag["next_action"]

feed = _classify_result(2, ["✗ feed not reached (possibly still in post-login modals)", "✗ feed diagnosis: login_screen_still_visible"])
assert feed["outcome"] == "manual_needed"
assert "Instagramログインが完了していません" in feed["next_action"]

challenge = _classify_result(3, [
    "✗ feed diagnosis: challenge_or_2fa:check your email",
    "two_step_verification",
    "CertificateException: pinning error",
])
assert challenge["outcome"] == "manual_needed"
assert "メール確認/2FA" in challenge["next_action"]
assert "録画" in challenge["next_action"]

manual_timeout = _classify_result(3, ["manual_login_timeout:challenge_or_2fa:check your email"])
assert manual_timeout["outcome"] == "manual_needed"
assert "認証完了後に「チェック用ログインを1つ作る」を再実行" in manual_timeout["next_action"]

manual_mode = _classify_result(3, ["manual_login_mode: AVD画面でInstagramへ手動ログインしてください"])
assert manual_mode["outcome"] == "manual_needed"

missing_accounts = _classify_result(1, ["FileNotFoundError: /Users/member/Library/Application Support/UnariSagiOperator/unari/config/accounts.json"])
assert missing_accounts["outcome"] == "failed"
assert "最新版" in missing_accounts["next_action"]
assert "取り込みだけやり直す" in missing_accounts["next_action"]

sheet_permission = _classify_result(1, ["SheetsBridgeError: Google Sheetsの認証または権限で止まりました。Error 403: Forbidden"])
assert sheet_permission["outcome"] == "manual_needed"
assert "Google Sheetsの認証" in sheet_permission["next_action"]
assert "結果CSV" in sheet_permission["next_action"]

tab_missing = _classify_result(2, ["タブ '7_3' が見つかりません"])
assert tab_missing["outcome"] == "manual_needed"
assert "タブ名" in tab_missing["next_action"]

input_error = _classify_result(4, ["login_input_error:username_or_password_rejected:incorrect password"])
assert input_error["outcome"] == "manual_needed"
assert "自動再試行" in input_error["next_action"]

feed_with_tls = _classify_result(2, [
    "✗ feed not reached (possibly still in post-login modals)",
    "✗ feed diagnosis: login_screen_still_visible",
    "Client TLS handshake failed",
])
assert feed_with_tls["outcome"] == "failed"
assert "通信補助設定" in feed_with_tls["next_action"]

dns = _classify_result(1, ["error establishing server connection: [Errno 8] nodename nor servname provided", "502 Bad Gateway"])
assert dns["outcome"] == "failed"
assert "ネットワーク/DNS" in dns["next_action"]

dns_tls = _classify_result(1, [
    "✗ transport diagnosis: network_dns_or_502,tls_or_pinning,mitm_log=mitmdump_keepalive.log",
    "Client TLS handshake failed",
])
assert dns_tls["outcome"] == "failed"
assert "DNS/502" in dns_tls["next_action"]

stale_proxy = _classify_result(1, ["[FAIL] AVDからcapture proxyへ接続できません (10.0.2.2:8080)"])
assert stale_proxy["outcome"] == "failed"
assert "通信用設定" in stale_proxy["next_action"]

avd_boot_timeout = _classify_result(1, ["[FAIL] AVD boot タイムアウト", "-- AVD起動ログ末尾: logs/avd_keepalive_x.log"])
assert avd_boot_timeout["outcome"] == "failed"
assert "Android画面の起動" in avd_boot_timeout["next_action"]
assert "Mac再起動" in avd_boot_timeout["next_action"]

tls = _classify_result(1, ["Client TLS handshake failed", "pinning error"])
assert tls["outcome"] == "failed"
assert "通信補助設定" in tls["next_action"]
assert "通信用設定" in tls["next_action"]

assert shin_capture_auto._mitmdump_listening("65535") is False
assert shin_capture_auto._expected_proxy_for_device("emulator-5554") == "10.0.2.2:8080"
assert shin_capture_auto._expected_proxy_for_device("physical-device") is None

assert "def diagnose_non_feed_screen" in Path("scripts/shin_capture_auto.py").read_text(encoding="utf-8")
shin = Path("scripts/shin_capture_auto.py").read_text(encoding="utf-8")
assert "MITM_CA_SRC" in shin
assert "def sync_frida_config" in shin
assert "wait_for_frida_hooks" in shin
assert "wait_for_manual_login_completion" in shin
assert "manual_login_mode" in shin
assert "--manual-login" in shin
assert "manual_login_required" in shin
assert "--manual-login-timeout" in shin
assert "--continue-on-error" in shin
assert "transport diagnosis" in shin
assert '"--no-proxy"' in shin
assert '"--no-verify"' in shin
assert "CERT_PEM block not found" in shin
assert "synced tools/config.js CA and proxy" in shin

capture_jobs = Path("ops_dashboard/capture_jobs.py").read_text(encoding="utf-8")
assert "--manual-login" in capture_jobs
assert "--manual-login-timeout" in capture_jobs
assert '"900"' in capture_jobs
assert '"timeout": 1800' in capture_jobs
assert "login_input_error" in capture_jobs
assert "メール確認/2FA" in capture_jobs
assert "config/accounts.json" in capture_jobs

app_html = Path("ops_dashboard/templates/index.html").read_text(encoding="utf-8")
assert "Android画面のInstagram" in app_html
assert "① まず件数を確認（本番はまだ走りません）" in app_html
assert "② 本番チェックを実行" in app_html
assert "途中から再開（ログイン追加後）" in app_html
assert "直近CSV" in app_html
assert "詳細設定 / CSVで実行する場合" in app_html
assert "詐欺チェックの進行状況" in app_html
assert "sagiSheetsBridgeHint" in app_html
assert "setSagiTabByOffset" in app_html
assert "検証用ログ（必要時だけ）" in app_html
assert "表示ログをコピー" in app_html
assert "hasConnectedAndroid" in app_html
assert "未接続" in app_html
assert "チェック用ログイン追加" in app_html
assert "showLoadingIfFirst" in app_html
assert "simple-status-row" in app_html
assert "capturePassword" not in app_html

ensure = Path("scripts/ensure_capture_infra.sh").read_text(encoding="utf-8")
assert "cleanup_stale_avd_locks" in ensure
assert "古いAVDロックを掃除しました" in ensure
assert "-avd-name" in ensure
assert "-no-snapshot-save -gpu swiftshader_indirect" in ensure
assert "tail_avd_launch_log" in ensure
assert 'bash "$SETUP_DEVICE" frida' in ensure
assert 'frida-server 入れ直し完了' in ensure
assert 'capture_proxy_host' in ensure
assert '10.0.2.2' in ensure
assert 'probe_proxy_from_avd' in ensure
assert 'probe_mac_instagram_dns' in ensure
assert 'Mac DNS: Instagram接続先を解決できます' in ensure
assert 'MITM_CA_SRC="$HOME/.mitmproxy/mitmproxy-ca-cert.pem"' in ensure
assert 'wait_for_mitm_ca' in ensure
assert '別ユーザー($listener_user)' in ensure
assert 'proxy_reachable="unknown"' in ensure
assert 'IG_CAP_USE_UPSTREAM' in ensure
assert 'settings put global http_proxy "$expected_proxy"' in ensure
assert '&& $proxy_reachable' not in ensure
assert 'proxy_reachable -eq' not in ensure

avd_setup = Path("scripts/setup_ig_capture_avd.sh").read_text(encoding="utf-8")
assert "cleanup_stale_avd_locks" in avd_setup
assert "-avd-name" in avd_setup
assert "-no-snapshot-save -gpu swiftshader_indirect" in avd_setup

instagram_install = Path("scripts/install_instagram_apk.sh").read_text(encoding="utf-8")
assert 'emu avd name' in instagram_install
assert "tr -d '\\\\r'" in instagram_install
assert "s/[^[:print:]]//g" in instagram_install
assert '起動中のAVDが $AVD_NAME ではありません' in instagram_install
assert '"$ADB" -s "$DEVICE" install-multiple' in instagram_install

device_setup = Path("scripts/setup_ig_capture_device.sh").read_text(encoding="utf-8")
assert 'proxy_host' in device_setup
assert '10.0.2.2' in device_setup
assert 'sync_frida_config' in device_setup
assert 'CERT_PEM block not found' in device_setup
assert 'Frida config synced to mitmproxy CA' in device_setup
print("capture diagnostics ok")
"""
    python = str(ROOT / "venv" / "bin" / "python") if (ROOT / "venv" / "bin" / "python").exists() else sys.executable
    return run([python, "-c", code], timeout=120)


def check_accounts_config_bootstrap() -> dict:
    code = """
import json
from pathlib import Path
from tempfile import TemporaryDirectory

from scripts import api_warning_check, import_real_session, verify_captured_session

with TemporaryDirectory(prefix="unari_accounts_bootstrap_") as td:
    root = Path(td)
    config_path = root / "config" / "accounts.json"
    sessions_dir = root / "sessions"
    sessions_dir.mkdir(parents=True)

    old_import_config = import_real_session.CONFIG_PATH
    old_verify_config = verify_captured_session.CONFIG_PATH
    old_verify_sessions = verify_captured_session.SESSIONS_DIR
    old_api_config = api_warning_check.CONFIG_PATH
    try:
        import_real_session.CONFIG_PATH = config_path
        cfg = import_real_session.load_config()
        assert cfg["accounts"] == []
        assert "password" not in cfg
        changed, status = import_real_session.upsert_account_entry(
            cfg,
            username="sora27_1",
            uuids={"phone_id": "phone-1"},
            device={"model": "sdk_gphone64_arm64"},
        )
        assert changed is True
        assert "proxy/passwordなし" in status
        import_real_session.save_config(cfg)
        written = json.loads(config_path.read_text(encoding="utf-8"))
        assert written["accounts"][0]["username"] == "sora27_1"
        assert written["accounts"][0]["proxy"] == ""
        assert "password" not in written

        session_path = sessions_dir / "sora27_1.json"
        session_path.write_text(json.dumps({"source": "mitmproxy", "device_settings": {"model": "x"}, "uuids": {"phone_id": "phone-1"}}), encoding="utf-8")

        verify_captured_session.CONFIG_PATH = root / "config" / "missing_accounts.json"
        verify_captured_session.SESSIONS_DIR = sessions_dir
        verify_cfg = verify_captured_session.load_config()
        assert verify_cfg["accounts"] == []
        session_accounts = verify_captured_session._accounts_from_sessions()
        assert session_accounts == [{"username": "sora27_1", "proxy": ""}]

        api_warning_check.CONFIG_PATH = root / "config" / "missing_accounts.json"
        api_cfg = api_warning_check.load_config()
        assert api_cfg["password"] == ""
        strong_accounts = api_warning_check._accounts_from_strong_sessions(sessions_dir)
        assert strong_accounts[0]["username"] == "sora27_1"
        assert strong_accounts[0]["proxy"] == ""
        assert strong_accounts[0]["device"] == {"model": "x"}
        assert strong_accounts[0]["uuids"] == {"phone_id": "phone-1"}
    finally:
        import_real_session.CONFIG_PATH = old_import_config
        verify_captured_session.CONFIG_PATH = old_verify_config
        verify_captured_session.SESSIONS_DIR = old_verify_sessions
        api_warning_check.CONFIG_PATH = old_api_config
print("accounts config bootstrap ok")
"""
    python = str(ROOT / "venv" / "bin" / "python") if (ROOT / "venv" / "bin" / "python").exists() else sys.executable
    return run([python, "-c", code], timeout=120)


def check_shell_syntax() -> dict:
    scripts = [
        "scripts/ensure_capture_infra.sh",
        "scripts/setup_ig_capture_avd.sh",
        "scripts/setup_ig_capture_device.sh",
        "scripts/install_instagram_apk.sh",
    ]
    return run(["bash", "-n", *scripts], timeout=60)


def check_docker_compile() -> dict:
    if shutil.which("docker") is None:
        return {"ok": False, "error": "docker command not found"}
    info = run(["docker", "info"], timeout=30)
    if not info["ok"]:
        return {"ok": False, "error": "docker daemon unavailable", "detail": info}
    with tempfile.TemporaryDirectory(prefix="unari_sagi_docker_") as td:
        tmp = Path(td) / "repo"
        ignore = shutil.ignore_patterns(
            ".git",
            "venv",
            "venv_ios",
            "dist",
            "logs",
            "data",
            "sessions",
            "captures",
            "cooldowns",
            "__pycache__",
            "*.pyc",
            ".env",
            "*.env",
            "accounts.json",
            "members.json",
            "capture_pool.json",
            "soax.json",
        )
        shutil.copytree(ROOT, tmp, ignore=ignore)
        cmd = [
            "docker",
            "run",
            "--rm",
            "-e",
            "PYTHONPYCACHEPREFIX=/tmp/pycache",
            "-v",
            f"{tmp}:/work:ro",
            "-w",
            "/work",
            "python:3.14-slim",
            "python",
            "-m",
            "compileall",
            "-q",
            "scripts",
            "ops_dashboard",
        ]
        return run(cmd, cwd=ROOT, timeout=300)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--docker", action="store_true", help="Docker隔離compile確認も実行する")
    parser.add_argument("--member-first-launch", action="store_true", help="空のHOMEと最小PATHで配布appの初回起動を確認する")
    parser.add_argument(
        "--release-contracts-only",
        action="store_true",
        help="README/Release workflowが公開配布の責務分担からズレていないかだけ確認する",
    )
    parser.add_argument(
        "--allow-missing-private-assets",
        action="store_true",
        help="ローカル開発用。members/Sheets bridge等の秘密設定が無い場合だけ該当チェックをskip扱いにする",
    )
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    if args.allow_missing_private_assets:
        os.environ[ALLOW_MISSING_PRIVATE_ASSETS_ENV] = "1"

    lock_file = acquire_release_check_lock()
    try:
        results: list[dict] = []
        quiet = args.json
        if args.release_contracts_only:
            step("published release verifier wiring", check_published_release_verifier_wiring(), results, quiet=quiet)
            step("release docs guardrails", check_release_docs_guardrails(), results, quiet=quiet)
        else:
            step("make check", run(["make", "check"], timeout=300), results, quiet=quiet)
            step("shell syntax", check_shell_syntax(), results, quiet=quiet)
            step("Flask API smoke", check_flask_api(), results, quiet=quiet)
            step("dashboard JS syntax", check_dashboard_js_syntax(), results, quiet=quiet)
            step("dashboard browser smoke", check_dashboard_browser_smoke(), results, quiet=quiet)
            step("update download flow", check_update_download_flow(), results, quiet=quiet)
            step("sagi sheet job wiring", check_sagi_sheet_job_wiring(), results, quiet=quiet)
            step("sagi API validation", check_sagi_api_validation(), results, quiet=quiet)
            step("job busy API conflicts", check_job_busy_api_conflicts(), results, quiet=quiet)
            step("setup job wiring", check_setup_job_wiring(), results, quiet=quiet)
            step("related log stitching", check_related_log_stitching(), results, quiet=quiet)
            step("launcher stale server restart", check_launcher_stale_server_restart(), results, quiet=quiet)
            step("app build lock", check_app_build_lock(), results, quiet=quiet)
            step("published release verifier wiring", check_published_release_verifier_wiring(), results, quiet=quiet)
            step("release docs guardrails", check_release_docs_guardrails(), results, quiet=quiet)
            step("capture diagnostics", check_capture_diagnostics(), results, quiet=quiet)
            step("accounts config bootstrap", check_accounts_config_bootstrap(), results, quiet=quiet)
            step("build Unari Sagi Operator.app", run(["make", "sagi-operator-install-app"], timeout=300), results, quiet=quiet)
            step("launcher script", check_launcher_script(), results, quiet=quiet)
            step("member stale server guard", check_member_stale_server_guard(), results, quiet=quiet)
            step("Sheets bridge bundle", check_sheets_bridge_bundle(), results, quiet=quiet)
            step("Instagram package bundle", check_instagram_package_bundle(), results, quiet=quiet)
            step("capture tools bundle", check_capture_tools_bundle(), results, quiet=quiet)
            step("members config bundle", check_members_config_bundle(), results, quiet=quiet)
            step("update config bundle", check_update_bundle(), results, quiet=quiet)
            step("bundle Python cache exclusion", check_bundle_python_caches(), results, quiet=quiet)
            step("app code signature", check_app_signature(), results, quiet=quiet)
            step("bundled Python runtime", check_bundled_python(), results, quiet=quiet)
            step("bundled Python wheelhouse", check_bundled_wheelhouse(), results, quiet=quiet)
            step("bundle Python cache exclusion after Python check", check_bundle_python_caches(), results, quiet=quiet)
            step("app code signature after Python check", check_app_signature(), results, quiet=quiet)
            step("bundle secret exclusion", check_bundle_secrets(), results, quiet=quiet)
            step("bundle local path exclusion", check_bundle_local_paths(), results, quiet=quiet)
            if args.member_first_launch:
                step("member Mac first-launch simulation", check_member_first_launch(), results, quiet=quiet)
                step("bundle Python cache exclusion after first launch", check_bundle_python_caches(), results, quiet=quiet)
                step("app code signature after first launch", check_app_signature(), results, quiet=quiet)
                step("member Mac broken venv repair simulation", check_member_broken_venv_repair(), results, quiet=quiet)
                step("bundle Python cache exclusion after broken venv repair", check_bundle_python_caches(), results, quiet=quiet)
                step("app code signature after broken venv repair", check_app_signature(), results, quiet=quiet)
            if args.docker:
                step("Docker isolated compile", check_docker_compile(), results, quiet=quiet)
            step("archive payload verification", check_archive_payloads(), results, quiet=quiet)
            step("archive content freshness", check_archive_contents(), results, quiet=quiet)
            step("DMG app code signature", check_dmg_app_signature(), results, quiet=quiet)
    finally:
        lock_file.close()

    ok = all(item.get("ok") for item in results)
    payload = {"ok": ok, "results": results}
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
