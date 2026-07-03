from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data" / "ops_dashboard"
HISTORY_DIR = DATA_DIR / "history"
LATEST_PATH = DATA_DIR / "latest.json"
JST = timezone(timedelta(hours=9))

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))
if str(ROOT / "dm_dashboard") not in sys.path:
    sys.path.insert(0, str(ROOT / "dm_dashboard"))


def now_jst() -> datetime:
    return datetime.now(JST)


def iso_now() -> str:
    return now_jst().isoformat(timespec="seconds")


def rel(path: Path) -> str:
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)


def section(ok: bool, level: str, summary: str, items: list[dict[str, Any]] | None = None, errors: list[str] | None = None) -> dict[str, Any]:
    return {
        "ok": ok,
        "level": level,
        "updated_at": iso_now(),
        "summary": summary,
        "items": items or [],
        "errors": errors or [],
    }


def safe_run(cmd: list[str], timeout: int = 120, cwd: Path | None = None) -> tuple[bool, str]:
    try:
        result = subprocess.run(cmd, cwd=cwd or ROOT, capture_output=True, text=True, timeout=timeout)
    except Exception as e:
        return False, str(e)
    text = (result.stdout or result.stderr or "").strip()
    return result.returncode == 0, text


def first_lines(text: str, limit: int = 8) -> list[str]:
    out = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped:
            out.append(stripped)
        if len(out) >= limit:
            break
    return out


def heading_lines(text: str, limit: int = 4) -> list[str]:
    out = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            out.append(stripped)
        if len(out) >= limit:
            break
    return out


def latest_file(pattern: str) -> Path | None:
    files = sorted(ROOT.glob(pattern), key=lambda p: p.stat().st_mtime if p.exists() else 0, reverse=True)
    return files[0] if files else None


def collect_sales_ops() -> dict[str, Any]:
    ok, output = safe_run([sys.executable, "scripts/ops_report.py", "morning", "--include-pdca"], timeout=300)
    if not ok:
        return section(False, "red", "朝レポート取得失敗", errors=[output[:500]])
    lines = output.splitlines()
    items = []
    wanted = {"ビハインド検知", "効率化時間バジェット", "今日の予定", "HubSpot更新漏れ", "詰まり検知"}
    current = ""
    bucket: list[str] = []
    for line in lines:
        if line.startswith("## "):
            if current in wanted:
                items.append({"title": current, "lines": bucket[:8]})
            current = line.replace("## ", "", 1).strip()
            bucket = []
            continue
        if current in wanted and line.strip():
            bucket.append(line.strip())
    if current in wanted:
        items.append({"title": current, "lines": bucket[:8]})
    items.append({
        "title": "データ元",
        "lines": [
            "入口: scripts/ops_report.py morning --include-pdca",
            "獲得目標/実値: Google Sheets ランキングタブ",
            "Spreadsheet: 1A3rzoDMQJ-68OxzxfjrlUeQlLQ9mutFcqEMq8wU9P3o",
            "対象行: 藤巻 空",
            "表示: 実値 / 目標合計 / 昨日までの獲得目標 / ビハインド",
        ],
    })
    level = "red" if "状態: 赤" in output else "yellow" if "状態: 黄" in output else "green"
    summary = next((line for line in lines if "当月アポ獲得:" in line), "朝レポート取得済み")
    return section(True, level, summary, items)


def _read_sales_log(path: Path) -> dict[str, Any]:
    text = path.read_text(errors="replace")
    return {
        "path": rel(path),
        "mtime": datetime.fromtimestamp(path.stat().st_mtime, JST).isoformat(timespec="seconds"),
        "check_empty": "- 結果サマリ:" in text and "- 結果サマリ: \n" not in text,
    }


def collect_pdca() -> dict[str, Any]:
    errors: list[str] = []
    items: list[dict[str, Any]] = []
    sales_log = latest_file("docs/sales_log/20*.md")
    weekly = latest_file("docs/sales_log/weekly/*.md")
    if sales_log:
        try:
            items.append({"title": "最新日次ログ", **_read_sales_log(sales_log)})
        except Exception as e:
            errors.append(f"sales_log: {e}")
    if weekly:
        try:
            items.append({"title": "最新週次ログ", **_read_sales_log(weekly)})
        except Exception as e:
            errors.append(f"weekly: {e}")
    goal = ROOT / "docs" / "2026-05_goal_plan.md"
    if goal.exists():
        items.append({"title": "5月目標", "path": rel(goal), "head": first_lines(goal.read_text(errors="replace"), 5)})
    summary = "PDCAログ未検出" if not items else f"PDCA関連 {len(items)}件"
    return section(not errors, "yellow" if errors else "green", summary, items, errors)


def collect_sagi() -> dict[str, Any]:
    try:
        from sagi_status_service import collect_status

        raw = collect_status()
        items = [
            {"label": "強session", "value": raw.get("strong_count", 0)},
            {"label": "cooldown中", "value": len(raw.get("cooldowns", []))},
            {"label": "稼働プロセス", "value": len(raw.get("processes", []))},
            {"label": "本日チェック", "value": raw.get("today_total", 0)},
            {"label": "SCAM検出", "value": raw.get("today_scam", 0)},
        ]
        if raw.get("today_scam", 0):
            level = "red"
        elif raw.get("cooldowns"):
            level = "yellow"
        else:
            level = "green"
        return section(True, level, f"本日チェック {raw.get('today_total', 0)}件 / SCAM {raw.get('today_scam', 0)}件", items)
    except Exception as e:
        return section(False, "red", "詐欺チェック状態取得失敗", errors=[str(e)])


def _count_tap_rows(rows: list[list[str]]) -> dict[str, Any]:
    today = now_jst().strftime("%Y-%m-%d")
    actions: Counter[str] = Counter()
    devices: Counter[str] = Counter()
    total = 0
    for row in rows:
        if len(row) < 5:
            continue
        ts = str(row[0])
        if today not in ts:
            continue
        device = str(row[1])
        action = str(row[3])
        try:
            count = int(float(row[4] or 1))
        except Exception:
            count = 1
        actions[action] += count
        devices[device] += count
        total += count
    return {"total": total, "actions": dict(actions), "top_devices": devices.most_common(8)}


def collect_tap_recorder() -> dict[str, Any]:
    errors: list[str] = []
    items: list[dict[str, Any]] = []

    cf_dir = ROOT / "cloudflare" / "tap_recorder"
    if cf_dir.exists():
        cmd = [
            "wrangler",
            "d1",
            "execute",
            "tap_recorder",
            "--remote",
            "--json",
            "--command",
            "SELECT ts, device, account, action, count, target_username, note FROM tap_log ORDER BY id DESC LIMIT 1000",
        ]
        ok, out = safe_run(cmd, timeout=60, cwd=cf_dir)
        if ok:
            try:
                payload = json.loads(out)
                rows = []
                for block in payload if isinstance(payload, list) else [payload]:
                    for r in block.get("results", []):
                        rows.append([r.get("ts"), r.get("device"), r.get("account"), r.get("action"), r.get("count"), "", ""])
                items.append({"source": "cloudflare", **_count_tap_rows(rows)})
            except Exception as e:
                errors.append(f"cloudflare parse: {e}")
        else:
            errors.append("cloudflare未接続")

    sheet_id = "1fPvnpWTMAHm_Z12HUCMIKkAGZbEyoo646xpoqldFCzg"
    ok, out = safe_run(["gog", "sheets", "get", sheet_id, "tap_log!A:G", "--account=work", "--json"], timeout=90)
    if ok:
        try:
            payload = json.loads(out)
            rows = payload.get("values", [])
            items.append({"source": "sheets", **_count_tap_rows(rows)})
        except Exception as e:
            errors.append(f"sheets parse: {e}")
    else:
        errors.append("sheets未接続")

    total = sum(item.get("total", 0) for item in items)
    level = "green" if total else "yellow"
    summary = f"本日tap合計 {total}件" if items else "tap-recorder未接続"
    return section(bool(items), level, summary, items, errors)


def collect_account_health() -> dict[str, Any]:
    report = latest_file("data/account_health/reports/*.md")
    spec = ROOT / "docs" / "account_health_spec.md"
    readme = ROOT / "scripts" / "account_health" / "README.md"
    items = []
    if report:
        text = report.read_text(errors="replace")
        counts = {}
        for label in ["健全", "要監視", "詐欺疑い", "育成", "永久停止"]:
            m = re.search(rf"{label}[^0-9]*(\d+)", text)
            if m:
                counts[label] = int(m.group(1))
        items.append({"title": "最新レポート", "path": rel(report), "counts": counts, "head": heading_lines(text, 3)})
    if spec.exists():
        items.append({"title": "仕様", "path": rel(spec)})
    if readme.exists():
        items.append({"title": "GAS運用", "path": rel(readme)})
    if report:
        return section(True, "green", "ローカルのaccount_healthレポートあり", items)
    return section(
        True,
        "yellow",
        "ローカルのaccount_healthレポート未生成（Slack通知状態は未判定）",
        items,
    )


def collect_analysis() -> dict[str, Any]:
    patterns = [
        "data/axis_follower_ff_*.summary.md",
        "data/contract_factor_summary_*.md",
        "data/lost_apo_status_analysis_*.md",
        "data/no_show_profile_traits_*.md",
        "data/profile_icon_factor_summary.md",
        "data/high_following_follower_analysis.md",
    ]
    files: list[Path] = []
    for pattern in patterns:
        files.extend(ROOT.glob(pattern))
    files = sorted(set(files), key=lambda p: p.stat().st_mtime, reverse=True)[:12]
    items = []
    for path in files:
        text = path.read_text(errors="replace")
        items.append({
            "path": rel(path),
            "mtime": datetime.fromtimestamp(path.stat().st_mtime, JST).isoformat(timespec="seconds"),
            "head": heading_lines(text, 4),
        })
    return section(True, "green" if items else "yellow", f"分析ファイル {len(items)}件", items)


def collect_knowledge() -> dict[str, Any]:
    targets = [
        ROOT / "docs" / "apo_knowledge" / "inbox.md",
        ROOT / "docs" / "sales_knowledge" / "inbox.md",
    ]
    items = []
    for path in targets:
        if path.exists():
            text = path.read_text(errors="replace")
            entries = sum(1 for line in text.splitlines() if line.startswith("## 20"))
            items.append({"title": path.parent.name, "path": rel(path), "entries": entries})
    meetings = sorted((ROOT / "docs" / "meetings").glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True)
    summaries = [p for p in meetings if p.name.endswith("_summary.md")]
    fbks = [p for p in meetings if p.name.endswith("_fbk.md")]
    if meetings:
        items.append({
            "title": "商談FBK",
            "summary_count": len(summaries),
            "fbk_count": len(fbks),
        })
    return section(True, "green" if items else "yellow", f"ナレッジ/商談 {len(items)}カテゴリ", items)


def collect_links() -> list[dict[str, str]]:
    paths = [
        ("DMダッシュボード", "dm_dashboard/app.py", "make dashboard"),
        ("Runbook", "docs/context/RUNBOOK.md", "make help"),
        ("ADR", "docs/adr/README.md", "docs/adr/"),
        ("tap-recorder", "cloudflare/tap_recorder/README.md", "Cloudflare /dashboard"),
        ("Apps Script tap-recorder", "apps_script/README.md", "tap_log"),
        ("効率化ロードマップ", "docs/2026-05_efficiency_automation_roadmap.md", ""),
    ]
    return [{"title": title, "path": path, "command": command} for title, path, command in paths]


def build_snapshot() -> dict[str, Any]:
    collectors = {
        "sales_ops": collect_sales_ops,
        "pdca": collect_pdca,
        "tap_recorder": collect_tap_recorder,
        "account_health": collect_account_health,
        "analysis": collect_analysis,
        "knowledge": collect_knowledge,
    }
    data: dict[str, Any] = {"generated_at": iso_now(), "links": collect_links()}
    red = False
    yellow = False
    top_actions: list[dict[str, str]] = []
    level_counts: Counter[str] = Counter()
    for name, fn in collectors.items():
        try:
            data[name] = fn()
        except Exception as e:
            data[name] = section(False, "red", f"{name}取得失敗", errors=[str(e)])
        level = data[name].get("level", "unknown")
        level_counts[level] += 1
        if level == "red":
            red = True
        elif level == "yellow":
            yellow = True
        summary = data[name].get("summary")
        if summary:
            top_actions.append({"section": name, "level": level, "summary": summary})
    data["overall"] = {
        "level": "red" if red else "yellow" if yellow else "green",
        "top_actions": sorted(top_actions, key=lambda x: {"red": 0, "yellow": 1, "green": 2}.get(x["level"], 3))[:6],
        "level_counts": dict(level_counts),
    }
    return data


def write_snapshot(data: dict[str, Any] | None = None) -> Path:
    data = data or build_snapshot()
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    LATEST_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    history_path = HISTORY_DIR / f"{now_jst():%Y-%m-%d}.json"
    history_path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    return LATEST_PATH


def load_latest_or_build() -> dict[str, Any]:
    if LATEST_PATH.exists():
        try:
            return json.loads(LATEST_PATH.read_text())
        except Exception:
            pass
    data = build_snapshot()
    write_snapshot(data)
    return data
