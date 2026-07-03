#!/usr/bin/env python3
"""
詐欺チェック依頼 自動処理スクリプト

Google Formの回答シート（受付シート）から未処理依頼を拾い、
対象シートへの詐欺チェック(api_warning_check.py)を実行し、
結果を書き戻してSlack通知する。

cron（scheduleスキル）で30分おきに実行される想定。
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from sheets_bridge import get_metadata, get_values, update_values

PROJECT_ROOT = Path(os.environ.get("UNARI_ROOT", str(Path(__file__).resolve().parent.parent)))
PYTHON = str(PROJECT_ROOT / "venv" / "bin" / "python")
WARNING_CHECK_SCRIPT = str(PROJECT_ROOT / "scripts" / "api_warning_check.py")
MEMBERS_JSON = PROJECT_ROOT / "config" / "members.json"
LOG_DIR = PROJECT_ROOT / "logs"
DEFAULT_MEMBERS_CONFIG = {
    "members": [],
    "slack_webhook_url": "",
    "error_webhook_url": "",
    "_missing": False,
}

REQUEST_SHEET_ID = "1e9iklFzfVDP_2InFQNJoSKt9guzXAJzjueR8KX9xSF0"
REQUEST_TAB = "フォームの回答 1"
ACCOUNT = "sora.fujimaki@unari.co.jp"

# 受付シートの列（0-indexed）
COL_TS = 0
COL_EMAIL = 1
COL_REQUESTER = 2
COL_SHEET_URL = 3
COL_DATE = 4
COL_CONFIRM = 5
COL_STATUS = 6
COL_PROCESSED_AT = 7
COL_SUMMARY = 8
COL_LOG_PATH = 9


@dataclass
class Request:
    row: int  # 1-indexed sheet row
    timestamp: str
    email: str
    requester: str
    sheet_url: str
    date_raw: str
    confirm: str
    # 再開用: 既存のstatus/log_path (stale processing 検出時のみ埋まる)
    existing_status: str = ""
    existing_log_path: str = ""
    existing_summary: str = ""
    is_stale: bool = False  # True ならresumeモード
    is_error_retry: bool = False  # True なら過去errorの自動再試行
    retry_count: int = 0  # error状態からの再試行回数

# stale判定しきい値: processed_at 更新から N 分経過したら crashed とみなす
STALE_PROCESSING_MINUTES = 90

# errorステータスからの自動retry: N時間経過で再試行可、最大MAX_RETRIESまで
ERROR_RETRY_COOLDOWN_HOURS = 2
MAX_ERROR_RETRIES = 3


def load_members() -> dict:
    if not MEMBERS_JSON.exists():
        cfg = dict(DEFAULT_MEMBERS_CONFIG)
        cfg["_missing"] = True
        print(
            f"  [WARN] Slack通知設定なし: {MEMBERS_JSON} がないため、Slack通知はスキップします。",
            file=sys.stderr,
        )
        return cfg
    with MEMBERS_JSON.open() as f:
        return json.load(f)


def sheets_get(spreadsheet_id: str, range_: str, account: str = ACCOUNT) -> list[list[str]]:
    return get_values(spreadsheet_id, range_, account=account)


def sheets_update(spreadsheet_id: str, range_: str, values: list[list[str]], account: str = ACCOUNT) -> None:
    update_values(spreadsheet_id, range_, values, account=account)


def sheets_metadata(spreadsheet_id: str, account: str = ACCOUNT) -> dict:
    return get_metadata(spreadsheet_id, account=account)


def list_tabs(spreadsheet_id: str, account: str = ACCOUNT) -> list[str]:
    meta = sheets_metadata(spreadsheet_id, account)
    return [s.get("properties", {}).get("title", "") for s in meta.get("sheets", [])]


def extract_sheet_id(url: str) -> str | None:
    m = re.search(r"/spreadsheets/d/([a-zA-Z0-9_-]+)", url)
    return m.group(1) if m else None


def normalize_date(raw: str) -> str | None:
    """04_21 -> 4_21 / 12_5 -> 12_5"""
    raw = raw.strip()
    m = re.match(r"^(\d{1,2})_(\d{1,2})$", raw)
    if not m:
        return None
    month = int(m.group(1))
    day = int(m.group(2))
    if not (1 <= month <= 12 and 1 <= day <= 31):
        return None
    return f"{month}_{day}"


def find_matching_tabs(tabs: list[str], normalized_date: str) -> list[str]:
    """normalized_date を含むタブを部分一致で抽出"""
    return [t for t in tabs if normalized_date in t]


def _parse_processed_at(raw: str) -> datetime | None:
    """processed_at (col H) をdatetimeにパース。失敗時None。"""
    raw = (raw or "").strip()
    if not raw:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M:%S"):
        try:
            return datetime.strptime(raw, fmt)
        except ValueError:
            continue
    return None


def _is_stale_processing(status: str, processed_at_raw: str) -> bool:
    """status=processing かつ最終更新が STALE_PROCESSING_MINUTES 経過ならTrue"""
    if status != "processing":
        return False
    pa = _parse_processed_at(processed_at_raw)
    if pa is None:
        return True  # 時刻不明だが processing = 安全側で再開対象
    from datetime import timedelta
    return datetime.now() - pa > timedelta(minutes=STALE_PROCESSING_MINUTES)


def _parse_retry_count(summary: str) -> int:
    """summaryから '[retry N/3]' の N を抜き出す。見つからなければ0。"""
    m = re.search(r"\[retry (\d+)/\d+\]", summary or "")
    return int(m.group(1)) if m else 0


def _is_error_retry(status: str, processed_at_raw: str, summary: str) -> tuple[bool, int]:
    """status=errorで ERROR_RETRY_COOLDOWN_HOURS 経過 かつ retry<MAX なら retry対象。
    戻り値: (is_retry, current_retry_count)
    """
    if status != "error":
        return False, 0
    retry_count = _parse_retry_count(summary)
    if retry_count >= MAX_ERROR_RETRIES:
        return False, retry_count
    pa = _parse_processed_at(processed_at_raw)
    if pa is None:
        return False, retry_count  # 時刻不明errorは無限retry防止で対象外
    from datetime import timedelta
    eligible = datetime.now() - pa > timedelta(hours=ERROR_RETRY_COOLDOWN_HOURS)
    return eligible, retry_count


def fetch_pending_requests() -> list[Request]:
    values = sheets_get(REQUEST_SHEET_ID, f"'{REQUEST_TAB}'!A2:J1000")
    pending: list[Request] = []
    for i, row in enumerate(values, start=2):
        row_padded = row + [""] * (10 - len(row))
        status = row_padded[COL_STATUS].strip()
        processed_at_raw = row_padded[COL_PROCESSED_AT]
        summary = row_padded[COL_SUMMARY]

        # 未処理判定: 空 / queued / stale processing / error retry
        is_stale = _is_stale_processing(status, processed_at_raw)
        is_err_retry, retry_count = _is_error_retry(status, processed_at_raw, summary)
        if status and status != "queued" and not is_stale and not is_err_retry:
            continue
        if not row_padded[COL_SHEET_URL].strip() or not row_padded[COL_DATE].strip():
            continue
        pending.append(Request(
            row=i,
            timestamp=row_padded[COL_TS],
            email=row_padded[COL_EMAIL],
            requester=row_padded[COL_REQUESTER],
            sheet_url=row_padded[COL_SHEET_URL],
            date_raw=row_padded[COL_DATE],
            confirm=row_padded[COL_CONFIRM],
            existing_status=status,
            existing_log_path=row_padded[COL_LOG_PATH].strip(),
            existing_summary=summary,
            is_stale=is_stale,
            is_error_retry=is_err_retry,
            retry_count=retry_count,
        ))
    return pending


def write_result(row: int, status: str, summary: str = "", log_path: str = "") -> None:
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    sheets_update(
        REQUEST_SHEET_ID,
        f"'{REQUEST_TAB}'!G{row}:J{row}",
        [[status, now, summary, log_path]],
    )


def find_slack_user_id(members_cfg: dict, requester_name: str) -> str | None:
    for m in members_cfg.get("members", []):
        if m.get("name") == requester_name:
            return m.get("slack_user_id")
    return None


def notify_slack(webhook_url: str, user_id: str | None, text: str) -> None:
    # webhook 未設定時はスキップ (members.json で空文字にすると通知オフ)
    if not webhook_url:
        return
    import urllib.request
    prefix = f"<@{user_id}> " if user_id else ""
    body = json.dumps({"text": prefix + text}).encode("utf-8")
    req = urllib.request.Request(webhook_url, data=body, headers={"Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=10)
    except Exception as e:
        print(f"  [WARN] Slack通知失敗: {e}", file=sys.stderr)


def extract_accounts_from_tab(sheet_id: str, tab_name: str) -> tuple[list[str], dict[str, int], int]:
    """対象シートのタブからアカウント抽出 + D列書き戻し用row_map構築。
    すでにD列マーク済み(詐欺表示/アカウント不明)のアカウントも再チェック対象にする。
    解除されていた場合に「なし」に戻せるようにするため。
    Returns: (accounts, row_map[account_id]=1-indexed_row, skipped_count)
    """
    values = sheets_get(sheet_id, f"'{tab_name}'!A1:D300")
    header_idx = -1
    for i, row in enumerate(values):
        if row and ("アカウントID" in row[0] or "アカウント" in row[0]):
            header_idx = i
            break
    accounts: list[str] = []
    row_map: dict[str, int] = {}
    seen: set[str] = set()
    skipped = 0  # 今は「重複・無効」のみskipped扱い
    for i, row in enumerate(values[header_idx + 1:], start=header_idx + 2):
        aid = (row[0].strip() if row and len(row) > 0 else "")
        if not aid or aid in seen or not re.match(r"^[a-zA-Z0-9_.]+$", aid):
            if aid and aid in seen:
                skipped += 1  # 重複は skipped にカウント（明示的に）
            continue
        if aid in ("合計", "アカウントID"):
            continue
        # D列マーク済みでも再チェック対象にする（状態変化を拾うため）
        accounts.append(aid)
        row_map[aid] = i
        seen.add(aid)
    return accounts, row_map, skipped


def run_warning_check(input_csv: Path, output_csv: Path | None = None,
                      resume: bool = False) -> Path:
    """api_warning_check.py を実行し、結果CSVのパスを返す。
    output_csv 指定時はそれを使う（再開時は既存ファイルを --resume で継続）。
    """
    if output_csv is None:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        output_csv = LOG_DIR / f"request_result_{ts}.csv"
    cmd = [PYTHON, WARNING_CHECK_SCRIPT,
           "--input", str(input_csv),
           "--output", str(output_csv)]
    # accounts.json で proxy を設定したアカウントはそれを使う。
    # (モバイル回線等の信用度高いIP経由が前提の設計に移行したため no-proxy を撤廃)
    if resume:
        cmd.append("--resume")
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=7200)
    if not output_csv.exists():
        tail = (proc.stderr or proc.stdout)[-800:]
        raise RuntimeError(f"結果CSV未生成 (rc={proc.returncode}): {tail}")
    return output_csv


def writeback_results(sheet_id: str, tab_name: str,
                      results: dict[str, dict], row_map: dict[str, int]) -> dict:
    """各アカウントの判定結果をD列に書き戻す。
    results: {account_id: {"status": "SCAM"|"NOT_FOUND"|"CLEAN"|"ERROR"}}
    - SCAM      → "詐欺表示"
    - NOT_FOUND → "アカウント不明"
    - CLEAN     → "なし"
    - ERROR     → 何も書かない（既存マーク保持）
    """
    VALUE_MAP = {
        "SCAM": "詐欺表示",
        "NOT_FOUND": "アカウント不明",
        "CLEAN": "なし",
    }
    stats = {"scam": 0, "not_found": 0, "clean": 0, "error": 0, "fail": 0, "no_row": 0}
    missing = []
    for acc_id, info in results.items():
        if acc_id not in row_map:
            missing.append(acc_id)
            stats["no_row"] += 1
            continue
        status = info.get("status", "ERROR")
        if status == "ERROR":
            stats["error"] += 1
            continue
        value = VALUE_MAP.get(status)
        if value is None:
            stats["error"] += 1
            continue
        row = row_map[acc_id]
        try:
            sheets_update(sheet_id, f"'{tab_name}'!D{row}:D{row}", [[value]])
            stats[status.lower() if status != "NOT_FOUND" else "not_found"] += 1
        except Exception as e:
            stats["fail"] += 1
            print(f"  [WARN] D{row} {acc_id} 書き戻し失敗: {e}", file=sys.stderr)
    if missing:
        print(f"  [WARN] row_map不一致 {len(missing)}件（先頭10）: {missing[:10]}")
    return stats


def writeback_scam(sheet_id: str, tab_name: str, scam_set: set[str], row_map: dict[str, int]) -> int:
    """後方互換のため残置（呼び出されない想定）"""
    count = 0
    for acc_id in sorted(scam_set):
        if acc_id not in row_map:
            continue
        row = row_map[acc_id]
        try:
            sheets_update(sheet_id, f"'{tab_name}'!D{row}:D{row}", [["詐欺表示"]])
            count += 1
        except Exception as e:
            print(f"  [WARN] D{row} {acc_id} 書き戻し失敗: {e}", file=sys.stderr)
    return count


def result_row_has_scam_flag(row: dict) -> bool:
    """古いCSVのSCAM_V2誤判定も書き戻し時に拾う。"""
    if row.get("has_scam_flag") == "TRUE":
        return True
    harm_type = (row.get("harm_type") or "").strip().upper()
    return harm_type.startswith("SCAM")


def process_request(req: Request, members_cfg: dict, dry_run: bool = False) -> None:
    print(f"\n=== Request row={req.row} requester={req.requester} date={req.date_raw}"
          f"{' [RETRY '+str(req.retry_count+1)+'/'+str(MAX_ERROR_RETRIES)+']' if req.is_error_retry else ''} ===")
    webhook = members_cfg.get("slack_webhook_url", "")
    error_webhook = members_cfg.get("error_webhook_url") or webhook
    slack_uid = find_slack_user_id(members_cfg, req.requester)

    def _fmt_err(msg: str) -> str:
        """error時のsummaryにretry marker(＋前回エラー内容サマリ)を付与。"""
        if req.is_error_retry:
            next_n = req.retry_count + 1
            return f"[retry {next_n}/{MAX_ERROR_RETRIES}] {msg}"
        return msg

    # 1. 日付正規化
    normalized = normalize_date(req.date_raw)
    if not normalized:
        msg = f"日付フォーマット不正: '{req.date_raw}'"
        print(f"  [ERROR] {msg}")
        if not dry_run:
            write_result(req.row, "error", _fmt_err(msg))
            notify_slack(error_webhook, slack_uid, f"❌ 詐欺チェック依頼エラー: {msg}")
        return

    # 2. 対象シートID
    target_sheet_id = extract_sheet_id(req.sheet_url)
    if not target_sheet_id:
        msg = "シートURLからID抽出失敗"
        print(f"  [ERROR] {msg}")
        if not dry_run:
            write_result(req.row, "error", _fmt_err(msg))
            notify_slack(error_webhook, slack_uid, f"❌ 詐欺チェック依頼エラー: {msg}")
        return

    # 3. タブ存在確認 + 部分一致チェック
    try:
        tabs = list_tabs(target_sheet_id)
    except Exception as e:
        msg = f"対象シートアクセス失敗: {e}"
        print(f"  [ERROR] {msg}")
        if not dry_run:
            write_result(req.row, "error", _fmt_err(msg)[:200])
            notify_slack(error_webhook, slack_uid, f"❌ 詐欺チェック依頼エラー: {msg}")
        return

    matches = find_matching_tabs(tabs, normalized)
    if len(matches) == 0:
        msg = f"タブ '{normalized}' が見つかりません"
        print(f"  [ERROR] {msg}")
        if not dry_run:
            write_result(req.row, "error", _fmt_err(msg))
            notify_slack(error_webhook, slack_uid, f"❌ 詐欺チェック依頼エラー: {msg}")
        return
    if len(matches) > 1:
        msg = f"類似タブが複数あります: {matches}"
        print(f"  [ERROR] {msg}")
        if not dry_run:
            write_result(req.row, "error", _fmt_err(msg)[:200])
            notify_slack(error_webhook, slack_uid, f"❌ 詐欺チェック依頼エラー: {msg}")
        return

    tab_name = matches[0]
    print(f"  ✓ target tab: {tab_name}")

    if dry_run:
        print("  [DRY-RUN] ここから先は実行しません")
        return

    # --- 再開フロー: stale processing なら既存ファイル使って続きから ---
    resume_input_csv: Path | None = None
    resume_output_csv: Path | None = None
    if req.is_stale:
        # 過去の input_csv を探す (最新)
        candidates = sorted(
            LOG_DIR.glob(f"request_row{req.row}_*_input.csv"),
            key=lambda p: p.stat().st_mtime, reverse=True,
        )
        if candidates:
            resume_input_csv = candidates[0]
        # col J の log_path を output_csv として利用
        if req.existing_log_path and Path(req.existing_log_path).exists():
            resume_output_csv = Path(req.existing_log_path)
        if resume_input_csv and resume_output_csv:
            print(f"  ♻ 再開: input={resume_input_csv.name} / output={resume_output_csv.name}")
            notify_slack(
                webhook, slack_uid,
                f"♻ 詐欺チェック再開: {tab_name} "
                f"(前回中断から {STALE_PROCESSING_MINUTES}分以上経過)"
            )
        else:
            print(f"  ⚠ stale だが再開用ファイル無し → 通常フローで再実行")
            resume_input_csv = None
            resume_output_csv = None

    # 4. processing に更新
    write_result(req.row, "processing", f"タブ '{tab_name}' 処理中")

    if resume_input_csv and resume_output_csv:
        input_csv = resume_input_csv
        accounts = []
        row_map = {}
        skipped = 0
        # row_map は書き戻し時に使うので再構築が必要
        try:
            accounts, row_map, skipped = extract_accounts_from_tab(target_sheet_id, tab_name)
        except Exception as e:
            msg = f"アカウント抽出失敗: {e}"
            print(f"  [ERROR] {msg}")
            write_result(req.row, "error", _fmt_err(msg)[:200])
            notify_slack(error_webhook, slack_uid, f"❌ 詐欺チェック依頼エラー: {msg}")
            return
    else:
        # 5. アカウント抽出
        try:
            accounts, row_map, skipped = extract_accounts_from_tab(target_sheet_id, tab_name)
        except Exception as e:
            msg = f"アカウント抽出失敗: {e}"
            print(f"  [ERROR] {msg}")
            write_result(req.row, "error", _fmt_err(msg)[:200])
            notify_slack(error_webhook, slack_uid, f"❌ 詐欺チェック依頼エラー: {msg}")
            return

        if not accounts:
            msg = "チェック対象アカウント0件（全て詐欺表示済 or 空）"
            print(f"  [WARN] {msg}")
            write_result(req.row, "done", msg)
            notify_slack(webhook, slack_uid, f"⚠️ 詐欺チェック: {tab_name} 対象0件")
            return

        # 6. input CSV 作成
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        input_csv = LOG_DIR / f"request_row{req.row}_{ts}_input.csv"
        with input_csv.open("w") as f:
            f.write("account_id\n")
            for a in accounts:
                f.write(a + "\n")

    # 7. チェック実行（output_csv を先に決めて col J に記録 → 途中死亡時の再開元になる）
    if resume_output_csv:
        output_csv_path = resume_output_csv
    else:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        output_csv_path = LOG_DIR / f"request_result_{ts}.csv"
    # 実行前に log_path を col J に書いておく（crash時の再開に必須）
    write_result(req.row, "processing", f"タブ '{tab_name}' 処理中", str(output_csv_path))

    try:
        result_csv = run_warning_check(
            input_csv,
            output_csv=output_csv_path,
            resume=bool(resume_output_csv),
        )
    except Exception as e:
        msg = f"チェック実行失敗: {e}"
        print(f"  [ERROR] {msg}")
        write_result(req.row, "error", _fmt_err(msg)[:200])
        notify_slack(error_webhook, slack_uid, f"❌ 詐欺チェック依頼エラー: {msg}")
        return

    # 8. 結果集計: 各アカウントに status を付与
    # SCAM / NOT_FOUND / CLEAN / ERROR の4状態でwritebackする
    results: dict[str, dict] = {}
    total = 0
    scam_count = 0
    with result_csv.open() as f:
        for row in csv.DictReader(f):
            total += 1
            aid = row["account_id"]
            err = (row.get("error") or "").strip()
            scam_flag = row.get("has_scam_flag")
            if result_row_has_scam_flag(row):
                results[aid] = {"status": "SCAM"}
                scam_count += 1
            elif err == "user_not_found":
                results[aid] = {"status": "NOT_FOUND"}
            elif scam_flag == "FALSE":
                results[aid] = {"status": "CLEAN"}
            else:
                # AUTH ERROR / 他エラー → 既存D列を保持
                results[aid] = {"status": "ERROR"}

    # 9. 対象シートのD列書き戻し（CLEAN含めて全状態）
    wb_stats = writeback_results(target_sheet_id, tab_name, results, row_map)

    summary = (
        f"SCAM {scam_count}/{total}件 "
        f"(書戻: 詐欺{wb_stats['scam']}/不明{wb_stats['not_found']}/なし{wb_stats['clean']}, "
        f"重複 {skipped}件, エラー {wb_stats['error']}件)"
    )
    print(f"  ✓ {summary}")
    write_result(req.row, "done", summary, str(result_csv))
    notify_slack(webhook, slack_uid, f"✅ 詐欺チェック完了: {tab_name} → {summary}")


# 1依頼あたりの予測処理時間(分)。過去の実績ベース（96件で50分）
ESTIMATED_MINUTES_PER_REQUEST = 50


def queue_pending(pending: list[Request], members_cfg: dict, dry_run: bool) -> None:
    """先頭以外の未処理依頼を queued 状態にして通知する。
    先頭の1件は即 process_request に進むため queue 表示不要。
    """
    if len(pending) <= 1:
        return
    from datetime import timedelta
    webhook = members_cfg.get("slack_webhook_url", "")
    now = datetime.now()
    for idx, req in enumerate(pending):
        if idx == 0:
            continue  # 先頭は即処理なのでqueue通知しない
        est_start = now + timedelta(minutes=ESTIMATED_MINUTES_PER_REQUEST * idx)
        order_txt = f"順番{idx + 1}番目/{len(pending)}件"
        summary = f"受付済み ({order_txt}, 予測開始 {est_start.strftime('%H:%M')}頃)"
        print(f"  ▶ queued row={req.row} {req.requester}: {summary}")
        if dry_run:
            continue
        try:
            write_result(req.row, "queued", summary)
        except Exception as e:
            print(f"  [WARN] queue status 更新失敗 row={req.row}: {e}", file=sys.stderr)
        slack_uid = find_slack_user_id(members_cfg, req.requester)
        notify_slack(
            webhook,
            slack_uid,
            f"📝 詐欺チェック依頼を受付けました（{order_txt}、予測開始 {est_start.strftime('%H:%M')}頃）",
        )


def preflight_ensure_strong_session(members_cfg: dict) -> None:
    """依頼処理の前に強sessionを確認する。

    SOAX/instagrapi 直接loginの refresh_sessions.py は自動実行しない。
    強sessionは実機/AVD capture で補充し、在庫が無ければ処理本体側で停止する。
    """
    count_cmd = [PYTHON, str(PROJECT_ROOT / "scripts" / "strong_session_pool.py"), "--count"]
    try:
        r = subprocess.run(count_cmd, capture_output=True, text=True, timeout=30)
        healthy = int(r.stdout.strip() or "0")
    except Exception as e:
        print(f"  [preflight] 強session数取得失敗: {e}")
        return
    print(f"  [preflight] healthy strong session: {healthy}件")
    if healthy >= 1:
        return
    print("  [preflight] 強session 0件 → 自動refreshは停止中。実機/AVD captureで補充してください。")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="実処理はせず判定のみ")
    parser.add_argument("--limit", type=int, default=5, help="1回で処理する最大件数")
    args = parser.parse_args()

    LOG_DIR.mkdir(exist_ok=True)
    members_cfg = load_members()

    try:
        pending = fetch_pending_requests()
    except Exception as e:
        print(f"[FATAL] 受付シート取得失敗: {e}", file=sys.stderr)
        sys.exit(1)

    print(f"未処理依頼: {len(pending)}件")
    if not pending:
        return

    # preflight: 強sessionが1本も無ければ先に refresh を走らせる
    # (毎朝のrefresh_sessions cronが落ちてた等のフェイルセーフ)
    if not args.dry_run:
        try:
            preflight_ensure_strong_session(members_cfg)
        except Exception as e:
            print(f"[WARN] preflight失敗: {e}", file=sys.stderr)

    # 受付先行: 複数依頼あれば先に queue 通知
    queue_pending(pending[: args.limit], members_cfg, args.dry_run)

    for req in pending[: args.limit]:
        try:
            process_request(req, members_cfg, dry_run=args.dry_run)
        except Exception as e:
            print(f"[ERROR] row={req.row} 処理中に例外: {e}", file=sys.stderr)
            if not args.dry_run:
                try:
                    ex_msg = f"例外: {e}"
                    if req.is_error_retry:
                        ex_msg = f"[retry {req.retry_count + 1}/{MAX_ERROR_RETRIES}] {ex_msg}"
                    write_result(req.row, "error", ex_msg[:200])
                except Exception:
                    pass


if __name__ == "__main__":
    main()
