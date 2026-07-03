from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config" / "sagi_operator_update.json"
VERSION_PATH = ROOT / "config" / "sagi_operator_version.json"


def _read_json(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return data


def load_current_version() -> dict[str, Any]:
    if not VERSION_PATH.exists():
        return {"version": "dev", "build": "unknown", "built_at": ""}
    data = _read_json(VERSION_PATH)
    return {
        "version": str(data.get("version") or "dev"),
        "build": str(data.get("build") or "unknown"),
        "built_at": str(data.get("built_at") or ""),
    }


def _version_key(value: str) -> list[tuple[int, int | str]]:
    parts = re.findall(r"\d+|[A-Za-z]+", str(value or ""))
    key: list[tuple[int, int | str]] = []
    for part in parts:
        key.append((1, int(part)) if part.isdigit() else (0, part.lower()))
    return key


def _is_newer(remote_version: str, current_version: str) -> bool:
    if not remote_version:
        return False
    if not current_version or current_version == "dev":
        return True
    return _version_key(remote_version) > _version_key(current_version)


def _fetch_json(source: str, timeout: int) -> dict[str, Any]:
    source = source.strip()
    if not source:
        raise ValueError("latest_url is empty")
    path = Path(source).expanduser()
    if path.exists():
        return _read_json(path)
    req = urllib.request.Request(source, headers={"User-Agent": "UnariSagiOperator/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as res:
        raw = res.read(2 * 1024 * 1024)
    data = json.loads(raw.decode("utf-8"))
    if not isinstance(data, dict):
        raise ValueError("latest.json must contain a JSON object")
    return data


def _asset(manifest: dict[str, Any], key: str) -> dict[str, Any]:
    assets = manifest.get("assets")
    if not isinstance(assets, dict):
        return {}
    item = assets.get(key)
    return item if isinstance(item, dict) else {}


def collect_update_status() -> dict[str, Any]:
    current = load_current_version()
    if not CONFIG_PATH.exists():
        return {
            "ok": True,
            "enabled": False,
            "current": current,
            "update_available": False,
            "message": "更新確認は未設定です。",
        }
    try:
        config = _read_json(CONFIG_PATH)
    except (OSError, ValueError, json.JSONDecodeError) as e:
        return {
            "ok": False,
            "enabled": False,
            "current": current,
            "update_available": False,
            "message": f"更新設定を読めません: {type(e).__name__}",
        }

    enabled = bool(config.get("enabled"))
    latest_url = str(config.get("latest_url") or "").strip()
    timeout = max(2, min(int(config.get("check_timeout_seconds") or 8), 20))
    if not enabled:
        return {
            "ok": True,
            "enabled": False,
            "current": current,
            "update_available": False,
            "message": "更新確認は無効です。",
        }
    if not latest_url:
        return {
            "ok": False,
            "enabled": True,
            "current": current,
            "update_available": False,
            "message": "更新確認URLが未設定です。",
        }

    try:
        manifest = _fetch_json(latest_url, timeout)
    except (OSError, ValueError, json.JSONDecodeError, urllib.error.URLError) as e:
        return {
            "ok": False,
            "enabled": True,
            "current": current,
            "update_available": False,
            "message": f"更新確認に失敗しました: {type(e).__name__}",
            "latest_url": latest_url,
        }

    latest_version = str(manifest.get("version") or "")
    dmg = _asset(manifest, "dmg")
    zip_asset = _asset(manifest, "zip")
    download_url = str(
        manifest.get("download_url")
        or manifest.get("download_page_url")
        or dmg.get("url")
        or zip_asset.get("url")
        or ""
    )
    update_available = _is_newer(latest_version, str(current.get("version") or ""))
    return {
        "ok": True,
        "enabled": True,
        "current": current,
        "latest": {
            "version": latest_version,
            "build": str(manifest.get("build") or ""),
            "published_at": str(manifest.get("published_at") or ""),
            "release_notes": str(manifest.get("release_notes") or ""),
            "download_url": download_url,
            "dmg_sha256": str(dmg.get("sha256") or ""),
            "zip_sha256": str(zip_asset.get("sha256") or ""),
        },
        "update_available": update_available,
        "message": "更新があります。" if update_available else "最新版です。",
        "latest_url": latest_url,
    }
