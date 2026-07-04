#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
import zipfile
from pathlib import Path

ANDROID_HOME = Path(os.environ.get("ANDROID_HOME", str(Path.home() / "Library" / "Android" / "sdk")))
OPERATOR_SUPPORT = Path.home() / "Library" / "Application Support" / "UnariSagiOperator"
LOCAL_JDK_HOME = OPERATOR_SUPPORT / "jdk" / "temurin-17"
VERSION = "14742923"
URL = f"https://dl.google.com/android/repository/commandlinetools-mac-{VERSION}_latest.zip"
# Same archive checksum as Homebrew's android-commandlinetools cask for version 14742923.
SHA256 = "ed304c5ede3718541e4f978e4ae870a4d853db74af6c16d920588d48523b9dee"
JDK_API_URL = "https://api.adoptium.net/v3/assets/latest/17/hotspot?architecture=aarch64&image_type=jdk&os=mac&vendor=eclipse"
PACKAGES = [
    "platform-tools",
    "emulator",
    "platforms;android-33",
    "system-images;android-33;google_apis;arm64-v8a",
]


def run(cmd: list[str], *, input_text: str | None = None, timeout: int = 1800, env: dict[str, str] | None = None) -> None:
    print("$", " ".join(cmd))
    proc = subprocess.run(cmd, input=input_text, text=True, timeout=timeout, env=env)
    if proc.returncode != 0:
        raise SystemExit(proc.returncode)


def try_run(cmd: list[str], *, input_text: str | None = None, timeout: int = 1800, env: dict[str, str] | None = None) -> int:
    print("$", " ".join(cmd))
    try:
        proc = subprocess.run(cmd, input=input_text, text=True, timeout=timeout, env=env)
    except subprocess.TimeoutExpired:
        print(f"command timed out after {timeout}s", file=sys.stderr)
        return 1
    return proc.returncode


def _package_dir(package: str) -> Path:
    return ANDROID_HOME.joinpath(*package.split(";"))


def missing_packages() -> list[str]:
    return [p for p in PACKAGES if not _package_dir(p).is_dir()]


def clear_download_cache() -> None:
    # sdkmanager keeps partially downloaded archives here; a corrupted one causes
    # "Error reading Zip content from a SeekableByteChannel" on every retry.
    temp_dir = ANDROID_HOME / ".temp"
    if temp_dir.exists():
        print(f"clearing sdkmanager download cache: {temp_dir}")
        shutil.rmtree(temp_dir, ignore_errors=True)


def install_packages(sdkmanager: Path, license_input: str, env: dict[str, str], *, attempts: int = 3) -> None:
    for attempt in range(1, attempts + 1):
        missing = missing_packages()
        if not missing:
            print("all SDK packages already installed")
            return
        if attempt > 1:
            clear_download_cache()
            time.sleep(10)
        print(f"installing SDK packages (attempt {attempt}/{attempts}): {missing}")
        code = try_run(
            [str(sdkmanager), "--sdk_root=" + str(ANDROID_HOME), *missing],
            input_text=license_input,
            timeout=1500,
            env=env,
        )
        missing = missing_packages()
        if code == 0 and not missing:
            return
        print(f"sdkmanager attempt {attempt} failed (exit={code}, missing={missing})", file=sys.stderr)
    raise SystemExit(f"SDK packages could not be installed after {attempts} attempts: {missing_packages()}")


def download_zip(dest: Path) -> None:
    print(f"download: {URL}")
    req = urllib.request.Request(URL, headers={"User-Agent": "UnariSagiOperator/1.0"})
    with urllib.request.urlopen(req, timeout=60) as res, dest.open("wb") as f:
        shutil.copyfileobj(res, f)
    actual = hashlib.sha256(dest.read_bytes()).hexdigest()
    if actual != SHA256:
        raise RuntimeError(f"checksum mismatch: expected={SHA256} actual={actual}")


def install_cmdline_tools() -> Path:
    latest = ANDROID_HOME / "cmdline-tools" / "latest"
    sdkmanager = latest / "bin" / "sdkmanager"
    if sdkmanager.exists():
        print(f"cmdline tools already installed: {sdkmanager}")
        return sdkmanager

    ANDROID_HOME.mkdir(parents=True, exist_ok=True)
    cmdline_root = ANDROID_HOME / "cmdline-tools"
    cmdline_root.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="unari_android_tools_") as td:
        tmp = Path(td)
        archive = tmp / "commandlinetools.zip"
        download_zip(archive)
        with zipfile.ZipFile(archive) as zf:
            zf.extractall(tmp)
        extracted = tmp / "cmdline-tools"
        if not extracted.exists():
            raise RuntimeError("zip does not contain cmdline-tools/")
        latest_tmp = tmp / "latest"
        extracted.rename(latest_tmp)
        if latest.exists():
            shutil.rmtree(latest)
        shutil.move(str(latest_tmp), str(latest))
    for tool in (latest / "bin").glob("*"):
        if tool.is_file():
            tool.chmod(tool.stat().st_mode | 0o755)

    print(f"installed: {sdkmanager}")
    return sdkmanager


def _java_from_home(java_home: Path) -> Path:
    return java_home / "bin" / "java"


def java_is_usable(java: Path) -> bool:
    try:
        result = subprocess.run([str(java), "-version"], capture_output=True, text=True, timeout=10)
        return result.returncode == 0
    except Exception:
        return False


def find_java() -> tuple[Path | None, Path | None]:
    env_home = os.environ.get("JAVA_HOME")
    if env_home and _java_from_home(Path(env_home)).exists() and java_is_usable(_java_from_home(Path(env_home))):
        return Path(env_home), _java_from_home(Path(env_home))
    if _java_from_home(LOCAL_JDK_HOME).exists() and java_is_usable(_java_from_home(LOCAL_JDK_HOME)):
        return LOCAL_JDK_HOME, _java_from_home(LOCAL_JDK_HOME)
    java = shutil.which("java")
    if java and java_is_usable(Path(java)):
        return None, Path(java)
    return None, None


def install_local_jdk() -> Path:
    local_java = _java_from_home(LOCAL_JDK_HOME)
    if local_java.exists() and java_is_usable(local_java):
        print(f"local JDK already installed: {local_java}")
        return LOCAL_JDK_HOME
    if os.environ.get("UNARI_USE_SYSTEM_JAVA") == "1":
        java_home, java = find_java()
        if java:
            print(f"using system java: {java}")
            return java_home or Path(java).parent.parent

    print("installing local Temurin JDK 17 for Unari Sagi Operator")
    OPERATOR_SUPPORT.mkdir(parents=True, exist_ok=True)
    api_req = urllib.request.Request(JDK_API_URL, headers={"User-Agent": "UnariSagiOperator/1.0"})
    with urllib.request.urlopen(api_req, timeout=60) as res:
        assets = json.loads(res.read().decode("utf-8"))
    if not assets:
        raise RuntimeError("Adoptium JDK asset not found")
    package = assets[0]["binary"]["package"]
    download_url = package["link"]
    expected_sha256 = package["checksum"]

    with tempfile.TemporaryDirectory(prefix="unari_jdk_") as td:
        tmp = Path(td)
        archive = tmp / "temurin-jdk17.tar.gz"
        print(f"download: {download_url}")
        download_req = urllib.request.Request(download_url, headers={"User-Agent": "UnariSagiOperator/1.0"})
        with urllib.request.urlopen(download_req, timeout=60) as res, archive.open("wb") as f:
            shutil.copyfileobj(res, f)
        actual = hashlib.sha256(archive.read_bytes()).hexdigest()
        if actual != expected_sha256:
            raise RuntimeError(f"JDK checksum mismatch: expected={expected_sha256} actual={actual}")
        shutil.unpack_archive(str(archive), str(tmp))
        homes = sorted(tmp.glob("*/Contents/Home"))
        if not homes:
            raise RuntimeError("JDK archive did not contain Contents/Home")
        if LOCAL_JDK_HOME.exists():
            shutil.rmtree(LOCAL_JDK_HOME)
        LOCAL_JDK_HOME.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(homes[0]), str(LOCAL_JDK_HOME))
    print(f"installed local JDK: {LOCAL_JDK_HOME}")
    return LOCAL_JDK_HOME


def main() -> int:
    if sys.platform != "darwin":
        print("This installer is for macOS only.", file=sys.stderr)
        return 2
    if os.uname().machine != "arm64":
        print("This installer targets Apple Silicon Mac only.", file=sys.stderr)
        return 2

    java_home = install_local_jdk()
    env = os.environ.copy()
    env["JAVA_HOME"] = str(java_home)
    env["ANDROID_HOME"] = str(ANDROID_HOME)
    env["ANDROID_SDK_ROOT"] = str(ANDROID_HOME)
    env["PATH"] = str(java_home / "bin") + os.pathsep + env.get("PATH", "")
    sdkmanager = install_cmdline_tools()
    license_input = "y\n" * 200
    run([str(sdkmanager), "--sdk_root=" + str(ANDROID_HOME), "--licenses"], input_text=license_input, timeout=900, env=env)
    install_packages(sdkmanager, license_input, env)
    print("Android cmdline tools setup complete")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
