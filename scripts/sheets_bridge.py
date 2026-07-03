#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(os.environ.get("UNARI_ROOT", str(Path(__file__).resolve().parent.parent)))
CONFIG_PATH = PROJECT_ROOT / "config" / "sagi_sheets_bridge.json"

_SERVICE: Any | None = None


class SheetsBridgeError(RuntimeError):
    pass


def _load_file_config() -> dict[str, Any]:
    if not CONFIG_PATH.exists():
        return {}
    try:
        with CONFIG_PATH.open() as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        raise SheetsBridgeError(f"Google Sheets連携設定を読めません: {CONFIG_PATH} ({e})") from e
    if not isinstance(data, dict):
        raise SheetsBridgeError(f"Google Sheets連携設定の形式が不正です: {CONFIG_PATH}")
    return data


def load_config() -> dict[str, Any]:
    file_cfg = _load_file_config()
    backend = os.environ.get("SAGI_SHEETS_BACKEND") or file_cfg.get("backend") or "auto"
    web_app_url = os.environ.get("SAGI_SHEETS_WEBAPP_URL") or file_cfg.get("web_app_url") or ""
    token = os.environ.get("SAGI_SHEETS_TOKEN") or file_cfg.get("token") or ""
    return {
        "backend": str(backend).strip().lower(),
        "web_app_url": str(web_app_url).strip(),
        "token": str(token).strip(),
    }


def backend_kind() -> str:
    cfg = load_config()
    backend = cfg["backend"]
    if backend == "auto":
        return "apps-script" if cfg["web_app_url"] else "google-api"
    if backend in {"apps-script", "google-api"}:
        return backend
    raise SheetsBridgeError("Google Sheets連携設定の backend は auto / apps-script / google-api のいずれかにしてください")


def describe_backend() -> dict[str, Any]:
    cfg = load_config()
    kind = backend_kind()
    if kind == "apps-script":
        ok = bool(cfg["web_app_url"] and cfg["token"])
        return {
            "backend": kind,
            "ok": ok,
            "summary": "Apps Script経由でGoogle Sheetsに接続します" if ok else "Apps ScriptのURLまたは内部トークンが未設定です",
            "needs_local_google_login": False,
            "config_path": str(CONFIG_PATH),
        }
    credentials_path = _google_credentials_path()
    token_path = _google_token_path()
    return {
        "backend": kind,
        "ok": credentials_path.exists(),
        "summary": (
            f"Google API直接接続を使います: {credentials_path}"
            if credentials_path.exists()
            else f"Google API直接接続の認証ファイルがありません: {credentials_path}"
        ),
        "needs_local_google_login": True,
        "credentials_path": str(credentials_path),
        "token_path": str(token_path),
        "token_exists": token_path.exists(),
    }


def _apps_script_request(action: str, payload: dict[str, Any], *, timeout: int = 90) -> dict[str, Any]:
    import requests

    cfg = load_config()
    url = cfg["web_app_url"]
    if not url:
        raise SheetsBridgeError(
            "Apps Script連携URLが未設定です。管理者が config/sagi_sheets_bridge.json に web_app_url を設定してください。"
        )
    if not cfg["token"]:
        raise SheetsBridgeError(
            "Apps Script連携トークンが未設定です。管理者が config/sagi_sheets_bridge.json に token を設定してください。"
        )
    body = {"action": action, **payload}
    body["token"] = cfg["token"]
    try:
        response = requests.post(url, json=body, timeout=timeout)
        response.raise_for_status()
        data = response.json()
    except requests.RequestException as e:
        raise SheetsBridgeError(f"Apps Scriptへの接続に失敗しました: {e}") from e
    except ValueError as e:
        raise SheetsBridgeError("Apps ScriptからJSON以外の応答が返りました。WebアプリURLを確認してください。") from e
    if not data.get("ok"):
        raise SheetsBridgeError(str(data.get("error") or "Apps Script処理に失敗しました"))
    return data


def _google_credentials_path() -> Path:
    raw = os.environ.get("GOOGLE_OAUTH_CLIENT_FILE")
    if raw:
        return Path(raw).expanduser()
    return Path.home() / ".config" / "google-api" / "credentials.json"


def _google_token_path() -> Path:
    raw = os.environ.get("GOOGLE_SHEETS_TOKEN_FILE")
    if raw:
        return Path(raw).expanduser()
    return Path.home() / ".config" / "google-api" / "sheets_token.json"


def _google_service() -> Any:
    global _SERVICE
    if _SERVICE is not None:
        return _SERVICE
    sys.path.insert(0, str(PROJECT_ROOT / "scripts"))
    from googleapiclient.discovery import build
    from sheets_auth import get_sheets_credentials

    creds = get_sheets_credentials()
    _SERVICE = build("sheets", "v4", credentials=creds, cache_discovery=False)
    return _SERVICE


def get_values(spreadsheet_id: str, range_a1: str, *, account: str | None = None) -> list[list[str]]:
    if backend_kind() == "apps-script":
        data = _apps_script_request("get", {"spreadsheetId": spreadsheet_id, "range": range_a1})
        return data.get("values", []) or []
    try:
        response = (
            _google_service()
            .spreadsheets()
            .values()
            .get(spreadsheetId=spreadsheet_id, range=range_a1)
            .execute()
        )
    except Exception as e:
        raise SheetsBridgeError(_format_google_error(e)) from e
    return response.get("values", []) or []


def update_values(
    spreadsheet_id: str,
    range_a1: str,
    values: list[list[str]],
    *,
    account: str | None = None,
) -> None:
    if backend_kind() == "apps-script":
        _apps_script_request("update", {"spreadsheetId": spreadsheet_id, "range": range_a1, "values": values})
        return
    try:
        (
            _google_service()
            .spreadsheets()
            .values()
            .update(
                spreadsheetId=spreadsheet_id,
                range=range_a1,
                valueInputOption="RAW",
                body={"values": values},
            )
            .execute()
        )
    except Exception as e:
        raise SheetsBridgeError(_format_google_error(e)) from e


def get_metadata(spreadsheet_id: str, *, account: str | None = None) -> dict[str, Any]:
    if backend_kind() == "apps-script":
        data = _apps_script_request("metadata", {"spreadsheetId": spreadsheet_id})
        return data.get("metadata", {}) or {}
    try:
        return (
            _google_service()
            .spreadsheets()
            .get(spreadsheetId=spreadsheet_id, fields="sheets(properties(title,sheetId))")
            .execute()
        )
    except Exception as e:
        raise SheetsBridgeError(_format_google_error(e)) from e


def list_tabs(spreadsheet_id: str, *, account: str | None = None) -> list[str]:
    metadata = get_metadata(spreadsheet_id, account=account)
    return [s.get("properties", {}).get("title", "") for s in metadata.get("sheets", [])]


def _format_google_error(error: Exception) -> str:
    text = str(error)
    status = getattr(getattr(error, "resp", None), "status", None)
    if status in {401, 403}:
        return (
            "Google Sheetsの認証または権限で止まりました。"
            "Google Sheets接続設定でログインし、対象シートの閲覧/編集権限を付けてください。"
            f" 詳細: {text}"
        )
    if status == 404:
        return f"Google Sheetsが見つからない、または権限がありません。URLと共有設定を確認してください。詳細: {text}"
    return f"Google Sheets処理に失敗しました: {text}"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--status", action="store_true", help="Sheets連携方式を表示する")
    args = parser.parse_args()
    if args.status:
        print(json.dumps(describe_backend(), ensure_ascii=False, indent=2))
        return 0
    parser.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
