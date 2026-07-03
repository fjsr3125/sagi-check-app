#!/usr/bin/env python3
"""Google Sheets API用のOAuth認証（初回のみブラウザ認証が必要）"""
import argparse
import json
import os
from pathlib import Path

from google.auth.transport.requests import Request
from google.auth.exceptions import RefreshError
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

PROJECT_ROOT = Path(os.environ.get("UNARI_ROOT", str(Path(__file__).resolve().parent.parent)))
DEFAULT_CREDENTIALS_PATH = Path.home() / ".config" / "google-api" / "credentials.json"
DEFAULT_TOKEN_PATH = Path.home() / ".config" / "google-api" / "sheets_token.json"
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]


def credentials_path() -> Path:
    return Path(os.environ.get("GOOGLE_OAUTH_CLIENT_FILE", str(DEFAULT_CREDENTIALS_PATH))).expanduser()


def token_path() -> Path:
    return Path(os.environ.get("GOOGLE_SHEETS_TOKEN_FILE", str(DEFAULT_TOKEN_PATH))).expanduser()


def get_sheets_credentials(*, allow_interactive: bool = True) -> Credentials:
    """認証済みCredentialsを返す。初回はブラウザ認証"""
    credentials_file = credentials_path()
    token_file = token_path()
    creds = None
    if token_file.exists():
        try:
            creds = Credentials.from_authorized_user_file(str(token_file), SCOPES)
        except (ValueError, json.JSONDecodeError):
            # 壊れたトークンは無視して再認証へ
            creds = None

    if not creds or not creds.valid:
        should_run_flow = True
        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
                should_run_flow = False
            except RefreshError:
                # refresh_token が無効化/失効している場合は再認証へフォールバック
                creds = None
                should_run_flow = True

        if should_run_flow:
            if not allow_interactive:
                raise RuntimeError("Google Sheets認証が未完了です。初回セットアップのGoogle Sheets接続設定を押してください。")
            if not credentials_file.exists():
                raise FileNotFoundError(
                    "Google Sheets認証に必要なOAuth設定ファイルがありません。"
                    "管理者がApps Script連携設定済みの最新版アプリを配布するか、"
                    f"{credentials_file} を配置してください。"
                )
            # credentials.jsonの形式を確認して適切に読み込む
            with open(credentials_file) as f:
                cred_data = json.load(f)
            # installed形式に変換（google-apiのcredentials.jsonはフラットな場合がある）
            if "installed" not in cred_data and "web" not in cred_data:
                cred_data = {"installed": cred_data}
                tmp_path = token_file.parent / "sheets_credentials_tmp.json"
                token_file.parent.mkdir(parents=True, exist_ok=True)
                with open(tmp_path, "w") as f:
                    json.dump(cred_data, f)
                flow = InstalledAppFlow.from_client_secrets_file(str(tmp_path), SCOPES)
                tmp_path.unlink()
            else:
                flow = InstalledAppFlow.from_client_secrets_file(str(credentials_file), SCOPES)
            # Codex/CIなどブラウザ自動起動が不安定な環境では open_browser=False を使う
            if os.environ.get("SHEETS_AUTH_CONSOLE", "1") == "1":
                creds = flow.run_local_server(port=0, open_browser=False)
            else:
                creds = flow.run_local_server(port=0)

        token_file.parent.mkdir(parents=True, exist_ok=True)
        with open(token_file, "w") as f:
            f.write(creds.to_json())
        print(f"トークン保存: {token_file}")

    return creds


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true", help="ブラウザ認証を起動せず、認証済みかだけ確認する")
    args = parser.parse_args()
    try:
        creds = get_sheets_credentials(allow_interactive=not args.check)
    except FileNotFoundError as e:
        print(str(e))
        raise SystemExit(2)
    except Exception as e:
        print(f"Google Sheets認証に失敗しました: {e}")
        raise SystemExit(1)
    print(f"認証成功: {creds.valid}")
