#!/usr/bin/env python3
from __future__ import annotations

import os
import hashlib
import fcntl
import json
import shutil
import stat
import subprocess
import tarfile
import tempfile
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APP_DIR = ROOT / "dist" / "Unari Sagi Operator.app"
CONTENTS = APP_DIR / "Contents"
MACOS = CONTENTS / "MacOS"
RESOURCES = CONTENTS / "Resources"
BUNDLED_SRC = RESOURCES / "unari-src"
BUNDLED_PYTHON = RESOURCES / "python"
WHEELHOUSE = RESOURCES / "wheelhouse"
EXECUTABLE = MACOS / "Unari Sagi Operator"

PYTHON_RUNTIME_URL = "https://github.com/astral-sh/python-build-standalone/releases/download/20260623/cpython-3.14.6%2B20260623-aarch64-apple-darwin-install_only_stripped.tar.gz"
PYTHON_RUNTIME_SHA256 = "35d774f61d63c1fd4f1bc9495a7ada92e500dc4382a0df8a9910eb87ea48e8cf"
PYTHON_RUNTIME_ARCHIVE = "cpython-3.14.6+20260623-aarch64-apple-darwin-install_only_stripped.tar.gz"

SCRIPT_FILES = [
    "api_warning_check.py",
    "ensure_capture_infra.sh",
    "ig_mitm_capture.py",
    "import_real_session.py",
    "install_android_cmdline_tools.py",
    "install_instagram_apk.sh",
    "manual_writeback_notify.py",
    "sagi_operator_extract_input.py",
    "sagi_request_processor.py",
    "sagi_sheets_webapp.gs",
    "setup_ig_capture_avd.sh",
    "setup_ig_capture_device.sh",
    "sheets_auth.py",
    "sheets_bridge.py",
    "shin_capture_auto.py",
    "strong_session_pool.py",
    "verify_captured_session.py",
]

TOOL_FILES = [
    "android-proxy-config.js",
    "android-unpinning-fallback.js",
    "android-unpinning-httptoolkit.js",
    "c8750f0d.0",
    "config.js",
    "frida-multiple-unpinning.js",
    "frida-server-17.9.1-android-arm64",
]

CONFIG_EXAMPLES = [
    "accounts.example.json",
    "capture_pool.example.json",
    "members.example.json",
    "sagi_operator_update.example.json",
    "sagi_sheets_bridge.example.json",
    "soax.example.json",
]

INSTAGRAM_PACKAGE_SUFFIXES = (".apk", ".apkm", ".xapk")
INSTAGRAM_PACKAGE_ENV = "SAGI_OPERATOR_INSTAGRAM_PACKAGE"
REQUIRE_INSTAGRAM_PACKAGE_ENV = "SAGI_OPERATOR_REQUIRE_INSTAGRAM_PACKAGE"
CAPTURE_TOOLS_DIR_ENV = "SAGI_OPERATOR_CAPTURE_TOOLS_DIR"
REQUIRE_CAPTURE_TOOLS_ENV = "SAGI_OPERATOR_REQUIRE_CAPTURE_TOOLS"
MEMBERS_CONFIG_ENV = "SAGI_OPERATOR_MEMBERS_CONFIG"
REQUIRE_MEMBERS_CONFIG_ENV = "SAGI_OPERATOR_REQUIRE_MEMBERS_CONFIG"
UPDATE_CONFIG_ENV = "SAGI_OPERATOR_UPDATE_CONFIG"
REQUIRE_SHEETS_BRIDGE_CONFIG_ENV = "SAGI_OPERATOR_REQUIRE_SHEETS_BRIDGE_CONFIG"

SHEETS_BRIDGE_CONFIG_CANDIDATES = [
    Path(os.environ["SAGI_SHEETS_BRIDGE_CONFIG"]).expanduser() if os.environ.get("SAGI_SHEETS_BRIDGE_CONFIG") else None,
    ROOT / "config" / "sagi_sheets_bridge.json",
    Path.home() / ".config" / "unari" / "sagi_sheets_bridge.json",
]


def write_text(path: Path, text: str, executable: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    if executable:
        path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def copy_file(src: Path, dst: Path) -> None:
    if not src.exists():
        raise FileNotFoundError(src)
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)


def copy_tree(src: Path, dst: Path) -> None:
    if not src.exists():
        raise FileNotFoundError(src)
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(
        src,
        dst,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".DS_Store"),
    )


def copy_source() -> None:
    if BUNDLED_SRC.exists():
        shutil.rmtree(BUNDLED_SRC)
    BUNDLED_SRC.mkdir(parents=True, exist_ok=True)

    copy_file(ROOT / "requirements.txt", BUNDLED_SRC / "requirements.txt")
    copy_tree(ROOT / "ops_dashboard", BUNDLED_SRC / "ops_dashboard")

    for name in SCRIPT_FILES:
        copy_file(ROOT / "scripts" / name, BUNDLED_SRC / "scripts" / name)
    copy_capture_tools()
    for name in CONFIG_EXAMPLES:
        copy_file(ROOT / "config" / name, BUNDLED_SRC / "config" / name)
    copy_members_config()
    copy_sheets_bridge_config()
    copy_update_config()
    write_version_metadata()

    copy_file(
        ROOT / "docs" / "sagi_operator_member_setup.md",
        BUNDLED_SRC / "docs" / "sagi_operator_member_setup.md",
    )


def _env_bool(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def copy_capture_tools() -> None:
    tools_dir = Path(os.environ.get(CAPTURE_TOOLS_DIR_ENV, str(ROOT / "tools"))).expanduser()
    required = _env_bool(REQUIRE_CAPTURE_TOOLS_ENV)
    copied: list[str] = []
    missing: list[str] = []
    for name in TOOL_FILES:
        src = tools_dir / name
        if src.exists():
            copy_file(src, BUNDLED_SRC / "tools" / name)
            copied.append(name)
        else:
            missing.append(name)
    if missing and required:
        raise FileNotFoundError(
            "capture用toolsが不足しています。"
            f"{CAPTURE_TOOLS_DIR_ENV}=/path/to/tools を指定してください。missing={', '.join(missing)}"
        )
    if missing:
        print(
            "warning: capture tools were not fully bundled. "
            f"Set {CAPTURE_TOOLS_DIR_ENV}=/path/to/tools for member-ready DMG. "
            f"missing={', '.join(missing)}"
        )
    elif copied:
        print(f"bundled capture tools: {len(copied)} files")


def copy_sheets_bridge_config() -> None:
    for candidate in SHEETS_BRIDGE_CONFIG_CANDIDATES:
        if candidate and candidate.exists():
            copy_file(candidate, BUNDLED_SRC / "config" / "sagi_sheets_bridge.json")
            print(f"bundled Sheets bridge config: {candidate}")
            return
    if os.environ.get(REQUIRE_SHEETS_BRIDGE_CONFIG_ENV, "").strip() == "1":
        raise FileNotFoundError(
            "sagi_sheets_bridge.json が見つかりません。"
            "SAGI_SHEETS_BRIDGE_CONFIG=/path/to/sagi_sheets_bridge.json を指定してください。"
        )


def find_update_config() -> Path | None:
    explicit = os.environ.get(UPDATE_CONFIG_ENV, "").strip()
    if explicit:
        path = Path(explicit).expanduser()
        if not path.exists():
            raise FileNotFoundError(f"{UPDATE_CONFIG_ENV} が見つかりません: {path}")
        return path
    candidates = [
        ROOT / "config" / "sagi_operator_update.json",
        Path.home() / ".config" / "unari" / "sagi_operator_update.json",
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return None


def copy_update_config() -> None:
    config = find_update_config()
    if not config:
        print(
            "warning: update config was not bundled. "
            f"Set {UPDATE_CONFIG_ENV}=/path/to/sagi_operator_update.json to enable in-app update checks."
        )
        return
    copy_file(config, BUNDLED_SRC / "config" / "sagi_operator_update.json")
    print(f"bundled update config: {config}")


def git_short_sha() -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=ROOT,
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
    except Exception:
        return "unknown"


def app_version() -> str:
    return os.environ.get("SAGI_OPERATOR_VERSION", "").strip() or "0.0.0-dev"


def write_version_metadata() -> None:
    metadata = {
        "app": "Unari Sagi Operator",
        "version": app_version(),
        "build": git_short_sha(),
        "built_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    write_text(
        BUNDLED_SRC / "config" / "sagi_operator_version.json",
        json.dumps(metadata, ensure_ascii=False, indent=2) + "\n",
    )


def find_members_config() -> Path | None:
    explicit = os.environ.get(MEMBERS_CONFIG_ENV, "").strip()
    if explicit:
        path = Path(explicit).expanduser()
        if not path.exists():
            raise FileNotFoundError(f"{MEMBERS_CONFIG_ENV} が見つかりません: {path}")
        return path

    candidates = [
        ROOT / "config" / "members.json",
        Path.home() / ".config" / "unari" / "members.json",
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return None


def copy_members_config() -> None:
    members_config = find_members_config()
    required = os.environ.get(REQUIRE_MEMBERS_CONFIG_ENV, "").strip() == "1"
    if not members_config:
        if required:
            raise FileNotFoundError(
                "members.json が見つかりません。"
                f"{MEMBERS_CONFIG_ENV}=/path/to/members.json を指定してください。"
            )
        print(
            "warning: members.json was not bundled. "
            f"Set {MEMBERS_CONFIG_ENV}=/path/to/members.json for Slack-ready DMG."
        )
        return
    copy_file(members_config, BUNDLED_SRC / "config" / "members.json")
    print("bundled members.json for Slack notifications")


def _is_instagram_package(path: Path) -> bool:
    return path.is_file() and path.suffix.lower() in INSTAGRAM_PACKAGE_SUFFIXES and "instagram" in path.name.lower()


def _package_mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0


def find_instagram_package() -> Path | None:
    explicit = os.environ.get(INSTAGRAM_PACKAGE_ENV, "").strip()
    if explicit:
        path = Path(explicit).expanduser()
        if not path.exists():
            raise FileNotFoundError(f"{INSTAGRAM_PACKAGE_ENV} が見つかりません: {path}")
        if not _is_instagram_package(path):
            raise ValueError(
                f"{INSTAGRAM_PACKAGE_ENV} は Instagram の APK/APKM/XAPK を指定してください: {path}"
            )
        return path

    search_dirs = [
        ROOT / "apks",
        ROOT / "private" / "apks",
        ROOT / "dist" / "apks",
        Path.home() / "Downloads",
        Path.home() / "Desktop",
    ]
    candidates: list[Path] = []
    for directory in search_dirs:
        if not directory.exists():
            continue
        candidates.extend(path for path in directory.iterdir() if _is_instagram_package(path))
    if not candidates:
        return None
    return sorted(set(candidates), key=_package_mtime, reverse=True)[0]


def copy_instagram_package() -> None:
    package = find_instagram_package()
    required = os.environ.get(REQUIRE_INSTAGRAM_PACKAGE_ENV, "").strip() == "1"
    if not package:
        if required:
            raise FileNotFoundError(
                "Instagram APK/APKM/XAPK が見つかりません。"
                f"{INSTAGRAM_PACKAGE_ENV}=/path/to/Instagram.xapk を指定してください。"
            )
        print(
            "warning: Instagram APK/APKM/XAPK was not bundled. "
            f"Set {INSTAGRAM_PACKAGE_ENV}=/path/to/Instagram.xapk for member-ready DMG."
        )
        return

    dst = BUNDLED_SRC / "apks" / package.name
    copy_file(package, dst)
    size_mb = package.stat().st_size / 1024 / 1024
    print(f"bundled Instagram package: {package} ({size_mb:.1f} MB)")


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def download_python_runtime() -> Path:
    cache = ROOT / "dist" / "vendor-cache"
    cache.mkdir(parents=True, exist_ok=True)
    archive = cache / PYTHON_RUNTIME_ARCHIVE
    if archive.exists() and sha256(archive) == PYTHON_RUNTIME_SHA256:
        return archive
    tmp = archive.with_suffix(archive.suffix + ".tmp")
    if tmp.exists():
        tmp.unlink()
    curl = shutil.which("curl")
    if curl:
        subprocess.run(
            [curl, "-fL", "--retry", "3", "--connect-timeout", "20", "-o", str(tmp), PYTHON_RUNTIME_URL],
            cwd=ROOT,
            check=True,
            timeout=600,
        )
    else:
        urllib.request.urlretrieve(PYTHON_RUNTIME_URL, tmp)
    actual = sha256(tmp)
    if actual != PYTHON_RUNTIME_SHA256:
        tmp.unlink(missing_ok=True)
        raise RuntimeError(f"Python runtime checksum mismatch: {actual}")
    tmp.replace(archive)
    return archive


def copy_python_runtime() -> None:
    archive = download_python_runtime()
    if BUNDLED_PYTHON.exists():
        shutil.rmtree(BUNDLED_PYTHON)
    with tempfile.TemporaryDirectory(prefix="unari_python_runtime_") as td:
        with tarfile.open(archive, "r:gz") as tf:
            tf.extractall(td)
        extracted = Path(td) / "python"
        if not (extracted / "bin" / "python3").exists():
            raise RuntimeError("Python runtime archive did not contain python/bin/python3")
        shutil.copytree(extracted, BUNDLED_PYTHON)


def build_wheelhouse() -> None:
    if WHEELHOUSE.exists():
        shutil.rmtree(WHEELHOUSE)
    WHEELHOUSE.mkdir(parents=True, exist_ok=True)
    python = BUNDLED_PYTHON / "bin" / "python3"
    if not python.exists():
        raise RuntimeError(f"bundled python not found: {python}")
    cache = ROOT / "dist" / "pip-cache"
    cache.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env.update(
        {
            "PIP_CACHE_DIR": str(cache),
            "PIP_DISABLE_PIP_VERSION_CHECK": "1",
            "PIP_NO_INPUT": "1",
            "PIP_PROGRESS_BAR": "off",
            "PYTHONDONTWRITEBYTECODE": "1",
        }
    )
    subprocess.run([str(python), "-m", "ensurepip", "--upgrade"], cwd=ROOT, env=env, check=True, timeout=180)
    subprocess.run(
        [
            str(python),
            "-m",
            "pip",
            "wheel",
            "--wheel-dir",
            str(WHEELHOUSE),
            "--prefer-binary",
            "-r",
            str(ROOT / "requirements.txt"),
        ],
        cwd=ROOT,
        env=env,
        check=True,
        timeout=1200,
    )
    wheels = sorted(WHEELHOUSE.glob("*.whl"))
    if not wheels:
        raise RuntimeError("wheelhouse build produced no wheels")
    print(f"bundled Python wheels: {len(wheels)} files")


def remove_python_caches(root: Path) -> None:
    if not root.exists():
        return
    for path in sorted(root.rglob("__pycache__"), reverse=True):
        if path.is_dir():
            shutil.rmtree(path, ignore_errors=True)
    for path in root.rglob("*.pyc"):
        try:
            path.unlink()
        except FileNotFoundError:
            pass


def ad_hoc_sign_app() -> None:
    codesign = shutil.which("codesign")
    if not codesign:
        raise RuntimeError("codesign not found; cannot create a shareable app")
    proc = subprocess.run(
        [codesign, "--force", "--deep", "--sign", "-", str(APP_DIR)],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        raise RuntimeError(f"ad-hoc codesign failed: {detail}")
    verify = subprocess.run(
        [codesign, "--verify", "--deep", "--strict", "--verbose=4", str(APP_DIR)],
        capture_output=True,
        text=True,
    )
    if verify.returncode != 0:
        detail = (verify.stderr or verify.stdout or "").strip()
        raise RuntimeError(f"app signature verification failed after signing: {detail}")


def acquire_build_lock():
    (ROOT / "dist").mkdir(parents=True, exist_ok=True)
    lock_path = ROOT / "dist" / ".sagi_operator_build.lock"
    lock_file = lock_path.open("w", encoding="utf-8")
    print(f"waiting for build lock: {lock_path}")
    fcntl.flock(lock_file, fcntl.LOCK_EX)
    print(f"acquired build lock: {lock_path}")
    return lock_file


def main() -> int:
    lock_file = acquire_build_lock()
    try:
        if APP_DIR.exists():
            shutil.rmtree(APP_DIR)
        MACOS.mkdir(parents=True, exist_ok=True)
        RESOURCES.mkdir(parents=True, exist_ok=True)
        copy_source()
        copy_instagram_package()
        copy_python_runtime()
        build_wheelhouse()
        remove_python_caches(BUNDLED_SRC)
        remove_python_caches(BUNDLED_PYTHON)

        version = app_version()
        plist = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleDevelopmentRegion</key>
  <string>ja</string>
  <key>CFBundleExecutable</key>
  <string>Unari Sagi Operator</string>
  <key>CFBundleIdentifier</key>
  <string>co.unari.sagi-operator</string>
  <key>CFBundleName</key>
  <string>Unari Sagi Operator</string>
  <key>CFBundleDisplayName</key>
  <string>Unari Sagi Operator</string>
  <key>CFBundlePackageType</key>
  <string>APPL</string>
  <key>CFBundleShortVersionString</key>
  <string>{version}</string>
  <key>CFBundleVersion</key>
  <string>{version}</string>
  <key>LSMinimumSystemVersion</key>
  <string>13.0</string>
</dict>
</plist>
"""
        write_text(CONTENTS / "Info.plist", plist)

        launcher = """#!/bin/zsh
set -u

APP_ROOT="$HOME/Library/Application Support/UnariSagiOperator/unari"
LOG_DIR="$HOME/Library/Logs/UnariSagiOperator"
PORT="${OPS_PORT:-5070}"
SELF_DIR="${0:A:h}"
BUNDLE_SRC="${SELF_DIR}/../Resources/unari-src"
BUNDLED_PY="${SELF_DIR}/../Resources/python/bin/python3"
WHEELHOUSE="${SELF_DIR}/../Resources/wheelhouse"

export PYTHONDONTWRITEBYTECODE=1

mkdir -p "$APP_ROOT" "$LOG_DIR"
export PYTHONPYCACHEPREFIX="$APP_ROOT/.pycache"
BOOT_LOG="$LOG_DIR/launcher_$(date +%Y%m%d_%H%M%S).log"
exec >> "$BOOT_LOG" 2>&1
echo "== Unari Sagi Operator launcher $(date '+%Y-%m-%d %H:%M:%S') =="
echo "APP_ROOT=$APP_ROOT"
echo "BUNDLE_SRC=$BUNDLE_SRC"
echo "BUNDLED_PY=$BUNDLED_PY"
echo "WHEELHOUSE=$WHEELHOUSE"

fail() {
  local msg="$1"
  echo "[FAIL] $msg"
  if [[ "${UNARI_OPERATOR_NO_UI:-0}" != "1" ]]; then
    open "$LOG_DIR" >/dev/null 2>&1 || true
    osascript - "$msg" "$BOOT_LOG" <<'APPLESCRIPT' >/dev/null 2>&1 || true
on run argv
  set messageText to item 1 of argv
  set logPath to item 2 of argv
  display dialog messageText & linefeed & linefeed & "ログ: " & logPath buttons {"OK"} default button "OK"
end run
APPLESCRIPT
  fi
  exit 1
}

install_python_deps() {
  "$PY" -m ensurepip --upgrade || return 1
  if [[ -d "$WHEELHOUSE" ]] && find "$WHEELHOUSE" -name '*.whl' -print -quit | grep -q .; then
    echo "installing dependencies from bundled wheelhouse"
    PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_NO_INPUT=1 PIP_PROGRESS_BAR=off \
      "$PY" -m pip install --no-index --find-links "$WHEELHOUSE" -r "$APP_ROOT/requirements.txt"
  else
    echo "bundled wheelhouse is missing; installing dependencies from internet"
    PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_NO_INPUT=1 PIP_DEFAULT_TIMEOUT=60 PIP_PROGRESS_BAR=off \
      "$PY" -m pip install --retries 5 --timeout 60 --prefer-binary -r "$APP_ROOT/requirements.txt"
  fi
}

rsync -a --delete \
  --exclude ".git" \
  --exclude "venv" \
  --exclude "logs" \
  --exclude "data" \
  --exclude "sessions" \
  --exclude "captures" \
  --exclude "cooldowns" \
  --exclude ".env" \
  --exclude "*.env" \
  --exclude "accounts.json" \
  --exclude "hubspot_members.json" \
  --exclude "capture_pool.json" \
  --exclude "soax.json" \
  --exclude "profiles" \
  --exclude "__pycache__" \
  "$BUNDLE_SRC/" "$APP_ROOT/" || fail "アプリの準備ファイルコピーに失敗しました。zipを展開し直してください。"

PY="$APP_ROOT/venv/bin/python"
NEED_VENV=0
if [[ ! -x "$PY" ]]; then
  NEED_VENV=1
elif ! "$PY" -c 'import importlib.util, sys; required=["flask","requests","instagrapi","googleapiclient","frida","mitmproxy"]; missing=[name for name in required if importlib.util.find_spec(name) is None]; print("missing modules: " + ", ".join(missing)) if missing else None; sys.exit(1 if missing else 0)' >/dev/null 2>&1; then
  NEED_VENV=1
fi

if [[ "$NEED_VENV" == "1" ]]; then
  if [[ ! -x "$BUNDLED_PY" ]]; then
    fail "アプリ内のPythonが見つかりません。配布zipをもう一度展開してください。"
  fi
  if [[ "${UNARI_OPERATOR_NO_UI:-0}" != "1" ]]; then
    osascript -e 'display notification "初回だけ数分かかります。閉じずに待ってください。" with title "Unari Sagi Operator"' >/dev/null 2>&1 || true
  fi
  rm -rf "$APP_ROOT/venv"
  "$BUNDLED_PY" -m venv "$APP_ROOT/venv" || fail "Python環境の作成に失敗しました。"
  if ! install_python_deps; then
    echo "dependency install failed; rebuilding venv once and retrying"
    rm -rf "$APP_ROOT/venv"
    "$BUNDLED_PY" -m venv "$APP_ROOT/venv" || fail "Python環境の作り直しに失敗しました。"
    install_python_deps || fail "必要ライブラリのインストールに失敗しました。ネット接続を確認してください。"
  fi
fi

cd "$APP_ROOT" || exit 1
mkdir -p logs sessions captures cooldowns data

stop_existing_operator_server() {
  local pids pid args
  pids=$(lsof -tiTCP:$PORT -sTCP:LISTEN 2>/dev/null || true)
  [[ -n "$pids" ]] || return 0
  for pid in ${(f)pids}; do
    [[ -n "$pid" ]] || continue
    args=$(ps -p "$pid" -o args= 2>/dev/null || true)
    if [[ "$args" == *"UnariSagiOperator/unari/ops_dashboard/app.py"* || "$args" == *"$APP_ROOT/ops_dashboard/app.py"* ]]; then
      echo "stopping existing operator server pid=$pid"
      kill "$pid" >/dev/null 2>&1 || true
      for _ in {1..20}; do
        kill -0 "$pid" >/dev/null 2>&1 || break
        sleep 0.2
      done
      if kill -0 "$pid" >/dev/null 2>&1; then
        echo "force stopping existing operator server pid=$pid"
        kill -9 "$pid" >/dev/null 2>&1 || true
      fi
    else
      echo "port $PORT is used by another process; args=$args"
    fi
  done
}

runtime_matches_current_bundle() {
  "$PY" - "$PORT" "$APP_ROOT" <<'PY'
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

port = sys.argv[1]
app_root = Path(sys.argv[2]).expanduser().resolve()
expected_path = app_root / "config" / "sagi_operator_version.json"
try:
    expected = json.loads(expected_path.read_text(encoding="utf-8"))
except Exception as exc:
    print(f"runtime check failed: cannot read expected metadata: {expected_path}: {exc}")
    sys.exit(2)

try:
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/runtime/status", timeout=3) as res:
        current = json.loads(res.read().decode("utf-8"))
except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
    print(f"runtime check failed: running server does not expose current runtime metadata: {exc}")
    sys.exit(3)

current_root_raw = str(current.get("root") or "")
if not current_root_raw:
    print("runtime check failed: running server did not return root")
    sys.exit(4)
current_root = str(Path(current_root_raw).expanduser().resolve())
if current_root != str(app_root):
    print(f"runtime check failed: root mismatch current={current_root} expected={app_root}")
    sys.exit(4)

for key in ("version", "build"):
    expected_value = str(expected.get(key, ""))
    current_value = str(current.get(key, ""))
    if expected_value and current_value != expected_value:
        print(f"runtime check failed: {key} mismatch current={current_value} expected={expected_value}")
        sys.exit(5)

print(f"runtime check ok: version={current.get('version')} build={current.get('build')} pid={current.get('pid')} user={current.get('user')}")
PY
}

fail_stale_operator_server() {
  echo "== port owner =="
  lsof -nP -iTCP:"$PORT" -sTCP:LISTEN || true
  fail "古いUnari Sagi Operatorが起動中です。Macを再起動してから、ApplicationsのUnari Sagi Operatorを開き直してください。"
}

SERVER_PID=""
SERVER_STARTED=0
LOG="$LOG_DIR/app_$(date +%Y%m%d_%H%M%S).log"
stop_existing_operator_server
if ! lsof -iTCP:$PORT -sTCP:LISTEN >/dev/null 2>&1; then
  UNARI_ROOT="$APP_ROOT" OPS_PORT="$PORT" nohup "$PY" "$APP_ROOT/ops_dashboard/app.py" > "$LOG" 2>&1 &
  SERVER_PID=$!
  SERVER_STARTED=1
  disown "$SERVER_PID" >/dev/null 2>&1 || true
  echo "started server pid=$SERVER_PID log=$LOG"
else
  echo "port $PORT is already in use; waiting for existing server"
fi

for i in {1..90}; do
  if curl -fsS "http://localhost:$PORT/" >/dev/null 2>&1; then
    if runtime_matches_current_bundle; then
      if [[ "${UNARI_OPERATOR_NO_UI:-0}" == "1" ]]; then
        echo "ready: http://localhost:$PORT/?operator=1"
      else
        open "http://localhost:$PORT/?operator=1&t=$(date +%s)"
      fi
      exit 0
    fi
    fail_stale_operator_server
  fi
  if [[ "$SERVER_STARTED" == "1" ]] && ! kill -0 "$SERVER_PID" >/dev/null 2>&1; then
    echo "server process stopped before becoming ready"
    if [[ -f "$LOG" ]]; then
      echo "== app log tail =="
      tail -80 "$LOG" || true
    fi
    fail "アプリの裏側サーバーが途中で停止しました。開いたログフォルダの最新ファイルを藤巻へ渡してください。"
  fi
  sleep 1
done

if [[ "${UNARI_OPERATOR_NO_UI:-0}" != "1" ]]; then
  if [[ -f "$LOG" ]]; then
    echo "== app log tail =="
    tail -80 "$LOG" || true
  fi
  open "$LOG_DIR" >/dev/null 2>&1 || true
  osascript <<'APPLESCRIPT' >/dev/null 2>&1 || true
display dialog "アプリ画面の起動に失敗しました。" & linefeed & linefeed & "Chromeのlocalhost画面ではなく、開いたログフォルダの最新ファイルを藤巻へ渡してください。" buttons {"OK"} default button "OK"
APPLESCRIPT
fi
exit 1
"""
        write_text(EXECUTABLE, launcher, executable=True)
        ad_hoc_sign_app()

        print(f"created: {APP_DIR}")
        print(f"bundled source: {BUNDLED_SRC}")
        print(f"runtime root: {Path.home() / 'Library' / 'Application Support' / 'UnariSagiOperator' / 'unari'}")
        return 0
    finally:
        lock_file.close()


if __name__ == "__main__":
    raise SystemExit(main())
