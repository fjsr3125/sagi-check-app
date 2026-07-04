from __future__ import annotations

import csv
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

try:
    from .capture_jobs import PYTHON, ROOT, _active_job, _new_job, _quick_run
except ImportError:
    from capture_jobs import PYTHON, ROOT, _active_job, _new_job, _quick_run

JST = timezone(timedelta(hours=9))
LOG_DIR = ROOT / "logs"


def _ts() -> str:
    return datetime.now(JST).strftime("%Y%m%d_%H%M%S")


def _rel(path: Path) -> str:
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)


def _busy_error() -> str | None:
    active = _active_job()
    if active:
        return f"実行中のジョブがあります: {active['label']}"
    return None


def _count_csv_rows(path: Path) -> int:
    if not path.exists():
        return 0
    with path.open() as f:
        return sum(1 for row in csv.DictReader(f) if (row.get("account_id") or "").strip())


def _latest_csv_files(prefix: str, limit: int = 6) -> list[dict[str, Any]]:
    if not LOG_DIR.exists():
        return []
    files = sorted(LOG_DIR.glob(f"{prefix}*.csv"), key=lambda p: p.stat().st_mtime, reverse=True)[:limit]
    items: list[dict[str, Any]] = []
    for path in files:
        row_count = _count_csv_rows(path)
        items.append(
            {
                "name": path.name,
                "path": _rel(path),
                "mtime": datetime.fromtimestamp(path.stat().st_mtime, JST).isoformat(timespec="seconds"),
                "row_count": row_count,
            }
        )
    return items


def _capacity_check_code(input_csv: Path, *, result_csv: Path | None = None, resume: bool = False) -> str:
    result_literal = str(_rel(result_csv)) if result_csv else ""
    return f"""
import csv
import sys
from pathlib import Path

sys.path.insert(0, str(Path("scripts").resolve()))
from strong_session_pool import count_healthy

path = Path({str(_rel(input_csv))!r})
with path.open() as f:
    rows = [r for r in csv.DictReader(f) if (r.get("account_id") or "").strip()]
checked = set()
result_path = Path({result_literal!r}) if {bool(result_csv)!r} else None
if {resume!r} and result_path and result_path.exists():
    with result_path.open() as f:
        for row in csv.DictReader(f):
            if (row.get("account_id") or "").strip() and (row.get("error") or "") == "":
                checked.add(row["account_id"].strip())
if checked:
    rows = [r for r in rows if (r.get("account_id") or "").strip() not in checked]
target_count = len(rows)
needed = max(1, (target_count + 49) // 50) if target_count else 0
print(f"入力CSV: {{path}}")
if result_path:
    print(f"結果CSV: {{result_path}}")
if checked:
    print(f"チェック済み: {{len(checked)}}件スキップ")
print(f"対象アカウント: {{target_count}}件")
print("1つのチェック用ログインの上限: 50件")
print(f"必要なチェック用ログイン: {{needed}}個")
if target_count == 0:
    print("対象アカウントが0件です。シートURL、タブ名、A列のアカウントIDを確認してください。")
    raise SystemExit(4)
healthy = count_healthy(probe=True)
print(f"今使えるチェック用ログイン: {{healthy}}個")
if healthy < needed:
    missing = needed - healthy
    print(f"NEEDS_SUPPLEMENT target_count={{target_count}} needed_sessions={{needed}} healthy_sessions={{healthy}} missing_sessions={{missing}}")
    print("チェック用ログインが足りません。新しいInstagramアカウントで不足分以上のチェック用ログインを作ってから、同じシートで「① まず件数を確認」を押し直してください。")
    raise SystemExit(5)
print("チェック用ログイン必要数チェックOK")
""".strip()


def _validate_rel_path(value: str, *, must_exist: bool = True) -> tuple[Path | None, str | None]:
    if not value:
        return None, "path を指定してください"
    path = (ROOT / value).resolve() if not value.startswith("/") else Path(value).resolve()
    try:
        path.relative_to(ROOT)
    except ValueError:
        return None, "repo配下のファイルだけ指定できます"
    if must_exist and not path.exists():
        return None, f"ファイルがありません: {_rel(path)}"
    return path, None


def collect_sagi_status(input_csv: str | None = None, *, probe: bool = False) -> dict[str, Any]:
    count = None
    if input_csv:
        path, error = _validate_rel_path(input_csv)
        if not error and path:
            count = _count_csv_rows(path)
    raw_count = _quick_run([PYTHON, "scripts/strong_session_pool.py", "--count"], timeout=60)
    probe_count = (
        _quick_run([PYTHON, "scripts/strong_session_pool.py", "--count-probe"], timeout=180)
        if probe
        else {"ok": None, "code": None, "output": "在庫確認ボタンでprobeします"}
    )
    sessions = _quick_run([PYTHON, "scripts/strong_session_pool.py", "--list"], timeout=60)
    sheets_bridge = _quick_run([PYTHON, "scripts/sheets_bridge.py", "--status"], timeout=20)
    needed = None
    if count is not None:
        needed = max(1, (count + 49) // 50) if count > 0 else 0
    return {
        "ok": raw_count["ok"],
        "input_count": count,
        "needed_sessions": needed,
        "healthy_sessions": _parse_int(raw_count.get("output")),
        "probe_sessions": _parse_int(probe_count.get("output")),
        "raw_count": raw_count,
        "probe_count": probe_count,
        "sessions": sessions,
        "sheets_bridge": sheets_bridge,
        "latest_inputs": _latest_csv_files("sagi_operator_input_"),
        "latest_results": _latest_csv_files("sagi_operator_result_"),
        "no_proxy": True,
    }


def _parse_int(value: Any) -> int | None:
    try:
        return int(str(value).strip().splitlines()[-1])
    except Exception:
        return None


def start_extract_input_job(
    *,
    sheet_url: str = "",
    sheet_id: str = "",
    tab_name: str = "",
    csv_path: str = "",
) -> tuple[dict[str, Any] | None, str | None]:
    if error := _busy_error():
        return None, error
    output = LOG_DIR / f"sagi_operator_input_{_ts()}.csv"
    cmd = [PYTHON, "-u", "scripts/sagi_operator_extract_input.py", "--output", _rel(output)]
    if csv_path.strip():
        path, error = _validate_rel_path(csv_path.strip())
        if error:
            return None, error
        cmd.extend(["--csv-path", _rel(path)])
    elif sheet_url.strip():
        cmd.extend(["--sheet-url", sheet_url.strip()])
        if not tab_name.strip():
            return None, "tab_name を指定してください"
        cmd.extend(["--tab-name", tab_name.strip()])
    elif sheet_id.strip():
        cmd.extend(["--sheet-id", sheet_id.strip()])
        if not tab_name.strip():
            return None, "tab_name を指定してください"
        cmd.extend(["--tab-name", tab_name.strip()])
    else:
        return None, "シートURL/IDまたはCSV pathを指定してください"
    return _new_job(
        "詐欺チェック: 入力取込",
        [{"name": "対象アカウントをCSV化", "cmd": cmd, "timeout": 180}],
        kind="sagi",
    ), None


def start_sheet_check_job(
    *,
    sheet_url: str = "",
    sheet_id: str = "",
    tab_name: str = "",
) -> tuple[dict[str, Any] | None, str | None]:
    if error := _busy_error():
        return None, error
    tab_name = tab_name.strip()
    sheet_url = sheet_url.strip()
    sheet_id = sheet_id.strip()
    if not tab_name:
        return None, "タブ名を入力してください"
    if not sheet_url and not sheet_id:
        return None, "Google Sheets URLを入力してください"

    ts = _ts()
    input_csv = LOG_DIR / f"sagi_operator_input_{ts}.csv"
    extract_cmd = [PYTHON, "-u", "scripts/sagi_operator_extract_input.py", "--output", _rel(input_csv), "--tab-name", tab_name]
    if sheet_url:
        extract_cmd.extend(["--sheet-url", sheet_url])
    else:
        extract_cmd.extend(["--sheet-id", sheet_id])
    return _new_job(
        "詐欺チェック: ①取込と件数確認",
        [
            {"name": "シートから対象アカウント取込", "cmd": extract_cmd, "timeout": 180},
            {"name": "強session必要本数チェック", "cmd": [PYTHON, "-c", _capacity_check_code(input_csv)], "timeout": 900},
        ],
        success_next_action="対象件数と必要なチェック用ログイン数を確認しました。足りていれば「② 本番チェックを実行」を押してください。足りない場合は、新しいInstagramアカウントでチェック用ログインを作ってから「① まず件数を確認」を押し直してください。",
        kind="sagi",
    ), None


def start_inventory_job(input_csv: str = "") -> tuple[dict[str, Any] | None, str | None]:
    if error := _busy_error():
        return None, error
    commands = [
        {"name": "チェック用ログイン数", "cmd": [PYTHON, "scripts/strong_session_pool.py", "--count"], "timeout": 60},
        {"name": "チェック用ログイン動作確認", "cmd": [PYTHON, "scripts/strong_session_pool.py", "--count-probe"], "timeout": 180},
        {"name": "チェック用ログイン一覧", "cmd": [PYTHON, "scripts/strong_session_pool.py", "--list"], "timeout": 60},
    ]
    if input_csv:
        path, error = _validate_rel_path(input_csv)
        if error:
            return None, error
        commands.insert(0, {"name": "入力CSV確認", "cmd": [PYTHON, "-c", f"import csv; print(sum(1 for r in csv.DictReader(open({str(path)!r})) if r.get('account_id')))"], "timeout": 20})
    return _new_job("詐欺チェック: 在庫確認", commands, kind="sagi"), None


def start_dryrun_job(input_csv: str) -> tuple[dict[str, Any] | None, str | None]:
    if error := _busy_error():
        return None, error
    path, error = _validate_rel_path(input_csv)
    if error:
        return None, error
    return _new_job(
        "詐欺チェック: dry-run",
        [{"name": "api_warning_check dry-run (--no-proxy)", "cmd": [PYTHON, "-u", "scripts/api_warning_check.py", "--input", _rel(path), "--dry-run", "--no-proxy"], "timeout": 900}],
        kind="sagi",
    ), None


def start_check_job(
    input_csv: str,
    *,
    result_csv: str = "",
    resume: bool = False,
) -> tuple[dict[str, Any] | None, str | None]:
    if error := _busy_error():
        return None, error
    path, error = _validate_rel_path(input_csv)
    if error:
        return None, error
    if resume and not result_csv.strip():
        return None, "続きから再開するには結果CSVを指定してください"
    if result_csv.strip():
        output, error = _validate_rel_path(result_csv.strip(), must_exist=resume)
        if error:
            return None, error
        assert output is not None
    else:
        output = LOG_DIR / f"sagi_operator_result_{_ts()}.csv"
    cmd = [PYTHON, "-u", "scripts/api_warning_check.py", "--input", _rel(path), "--output", _rel(output), "--no-proxy"]
    if resume:
        cmd.append("--resume")
    return _new_job(
        "詐欺チェック: 続きから再開" if resume else "詐欺チェック: 本番実行",
        [
            {"name": "強session必要本数チェック", "cmd": [PYTHON, "-c", _capacity_check_code(path, result_csv=output, resume=resume)], "timeout": 900},
            {"name": "dry-run (--no-proxy)", "cmd": [PYTHON, "-u", "scripts/api_warning_check.py", "--input", _rel(path), "--dry-run", "--no-proxy"], "timeout": 900},
            {"name": "api_warning_check 本番 (--no-proxy)", "cmd": cmd, "timeout": 7200},
        ],
        success_next_action="本番チェックが完了しました。次に「③ 書き戻さずに件数だけ確認（安全）」を押してください。Slack通知名が空でもシート反映だけなら進められます。",
        kind="sagi",
    ), None


def start_writeback_job(
    *,
    result_csv: str,
    sheet_id: str,
    tab_name: str,
    requester: str,
    dry_run: bool,
) -> tuple[dict[str, Any] | None, str | None]:
    if error := _busy_error():
        return None, error
    path, error = _validate_rel_path(result_csv)
    if error:
        return None, error
    if not sheet_id.strip() or not tab_name.strip():
        return None, "Google Sheets URLとタブ名を入力してください"
    cmd = [
        PYTHON,
        "-u",
        "scripts/manual_writeback_notify.py",
        "--result-csv",
        _rel(path),
        "--sheet-id",
        sheet_id.strip(),
        "--tab-name",
        tab_name.strip(),
    ]
    if requester.strip():
        cmd.extend(["--requester", requester.strip()])
    if dry_run:
        cmd.append("--dry-run")
    return _new_job(
        "詐欺チェック: 書き戻し確認" if dry_run else "詐欺チェック: シート反映",
        [{"name": "シートD列書き戻し/Slack通知", "cmd": cmd, "timeout": 600}],
        success_next_action=(
            "書き戻し前の件数確認が完了しました。問題なければ「④ シートのD列に反映（確定）」を押してください。"
            if dry_run
            else "シート反映が完了しました。Slack通知名を入れていた場合は通知も送信されています。"
        ),
        kind="sagi",
    ), None


def start_notify_test_job(requester: str) -> tuple[dict[str, Any] | None, str | None]:
    if error := _busy_error():
        return None, error
    if not requester.strip():
        return None, "Slack通知先の登録名を入力してください"
    code = (
        "from sagi_request_processor import load_members, find_slack_user_id, notify_slack; "
        f"requester={requester.strip()!r}; "
        "m=load_members(); uid=find_slack_user_id(m, requester); "
        "notify_slack(m.get('slack_webhook_url',''), uid, 'Unari Sagi Operator 通知テスト'); "
        "print(f'Slack通知テスト: requester={requester} uid={uid}')"
    )
    return _new_job(
        "詐欺チェック: Slack通知テスト",
        [{"name": "Slack通知テスト", "cmd": [PYTHON, "-u", "-c", code], "timeout": 60}],
        kind="sagi",
    ), None
