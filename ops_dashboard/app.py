#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import socket
from pathlib import Path

from flask import Flask, jsonify, render_template, request

try:
    from .collectors import build_snapshot, load_latest_or_build, write_snapshot
    from .capture_jobs import (
        collect_capture_status,
        get_job,
        start_capture_all_job,
        start_import_latest_job,
        start_infra_job,
        start_verify_job,
    )
    from .setup_jobs import collect_setup_status, start_setup_job
    from .update_check import collect_update_status, download_latest_update
    from .check_jobs import (
        collect_sagi_status,
        start_check_job,
        start_dryrun_job,
        start_extract_input_job,
        start_inventory_job,
        start_notify_test_job,
        start_sheet_check_job,
        start_writeback_job,
    )
except ImportError:
    from collectors import build_snapshot, load_latest_or_build, write_snapshot
    from capture_jobs import (
        collect_capture_status,
        get_job,
        start_capture_all_job,
        start_import_latest_job,
        start_infra_job,
        start_verify_job,
    )
    from setup_jobs import collect_setup_status, start_setup_job
    from update_check import collect_update_status, download_latest_update
    from check_jobs import (
        collect_sagi_status,
        start_check_job,
        start_dryrun_job,
        start_extract_input_job,
        start_inventory_job,
        start_notify_test_job,
        start_sheet_check_job,
        start_writeback_job,
    )

app = Flask(__name__)
app.json.ensure_ascii = False

BUSY_ERROR_PREFIX = "実行中のジョブがあります"


def _runtime_root() -> Path:
    root = os.environ.get("UNARI_ROOT", "").strip()
    if root:
        return Path(root).expanduser().resolve()
    return Path(__file__).resolve().parent.parent


def _runtime_version() -> dict:
    version_path = _runtime_root() / "config" / "sagi_operator_version.json"
    try:
        data = json.loads(version_path.read_text(encoding="utf-8"))
    except Exception as exc:
        return {"error": str(exc), "path": str(version_path)}
    if not isinstance(data, dict):
        return {"error": "version metadata is not an object", "path": str(version_path)}
    data.setdefault("path", str(version_path))
    return data


def _job_error_response(error: str):
    status = 409 if error.startswith(BUSY_ERROR_PREFIX) else 400
    return jsonify({"ok": False, "error": error}), status


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/runtime/status")
def api_runtime_status():
    version = _runtime_version()
    return jsonify(
        {
            "ok": True,
            "pid": os.getpid(),
            "user": os.environ.get("USER") or os.environ.get("LOGNAME") or "",
            "root": str(_runtime_root()),
            "version": version.get("version", ""),
            "build": version.get("build", ""),
            "version_info": version,
        }
    )


@app.route("/api/status")
def api_status():
    return jsonify(load_latest_or_build())


@app.route("/api/update/status")
def api_update_status():
    return jsonify(collect_update_status())


@app.route("/api/update/download", methods=["POST"])
def api_update_download():
    return jsonify(download_latest_update(open_after=True))


@app.route("/api/refresh", methods=["POST"])
def api_refresh():
    data = build_snapshot()
    path = write_snapshot(data)
    return jsonify({"ok": True, "path": str(path), "data": data})


@app.route("/api/section/<name>")
def api_section(name: str):
    data = load_latest_or_build()
    if name not in data:
        return jsonify({"ok": False, "error": f"unknown section: {name}"}), 404
    return jsonify(data[name])


@app.route("/api/capture/status")
def api_capture_status():
    return jsonify(collect_capture_status())


@app.route("/api/capture/start-infra", methods=["POST"])
def api_capture_start_infra():
    job, error = start_infra_job()
    if error:
        return _job_error_response(error)
    return jsonify({"ok": True, "job": job})


@app.route("/api/capture/run-all", methods=["POST"])
def api_capture_run_all():
    data = request.get_json(silent=True) or {}
    job, error = start_capture_all_job(
        username=str(data.get("username", "")),
        password=str(data.get("password", "")),
        confirm_tethering=bool(data.get("confirm_tethering")),
        skip_accounts_check=bool(data.get("skip_accounts_check", True)),
        interval=int(data.get("interval") or 120),
        manual_login=bool(data.get("manual_login", True)),
    )
    if error:
        return _job_error_response(error)
    return jsonify({"ok": True, "job": job})


@app.route("/api/capture/import-latest", methods=["POST"])
def api_capture_import_latest():
    data = request.get_json(silent=True) or {}
    job, error = start_import_latest_job(str(data.get("username", "")))
    if error:
        return _job_error_response(error)
    return jsonify({"ok": True, "job": job})


@app.route("/api/capture/verify", methods=["POST"])
def api_capture_verify():
    data = request.get_json(silent=True) or {}
    job, error = start_verify_job(str(data.get("username", "")))
    if error:
        return _job_error_response(error)
    return jsonify({"ok": True, "job": job})


@app.route("/api/capture/jobs/<job_id>")
def api_capture_job(job_id: str):
    job = get_job(job_id)
    if not job:
        return jsonify({"ok": False, "error": "job not found"}), 404
    return jsonify({"ok": True, "job": job})


@app.route("/api/setup/status")
def api_setup_status():
    return jsonify(collect_setup_status())


@app.route("/api/setup/run", methods=["POST"])
def api_setup_run():
    data = request.get_json(silent=True) or {}
    job, error = start_setup_job(str(data.get("action", "")))
    if error:
        return _job_error_response(error)
    return jsonify({"ok": True, "job": job})


@app.route("/api/sagi/status")
def api_sagi_status():
    return jsonify(collect_sagi_status(request.args.get("input_csv"), probe=request.args.get("probe") == "1"))


@app.route("/api/sagi/extract", methods=["POST"])
def api_sagi_extract():
    data = request.get_json(silent=True) or {}
    job, error = start_extract_input_job(
        sheet_url=str(data.get("sheet_url", "")),
        sheet_id=str(data.get("sheet_id", "")),
        tab_name=str(data.get("tab_name", "")),
        csv_path=str(data.get("csv_path", "")),
    )
    if error:
        return _job_error_response(error)
    return jsonify({"ok": True, "job": job})


@app.route("/api/sagi/sheet-check", methods=["POST"])
def api_sagi_sheet_check():
    data = request.get_json(silent=True) or {}
    job, error = start_sheet_check_job(
        sheet_url=str(data.get("sheet_url", "")),
        sheet_id=str(data.get("sheet_id", "")),
        tab_name=str(data.get("tab_name", "")),
    )
    if error:
        return _job_error_response(error)
    return jsonify({"ok": True, "job": job})


@app.route("/api/sagi/inventory", methods=["POST"])
def api_sagi_inventory():
    data = request.get_json(silent=True) or {}
    job, error = start_inventory_job(str(data.get("input_csv", "")))
    if error:
        return _job_error_response(error)
    return jsonify({"ok": True, "job": job})


@app.route("/api/sagi/dryrun", methods=["POST"])
def api_sagi_dryrun():
    data = request.get_json(silent=True) or {}
    job, error = start_dryrun_job(str(data.get("input_csv", "")))
    if error:
        return _job_error_response(error)
    return jsonify({"ok": True, "job": job})


@app.route("/api/sagi/check", methods=["POST"])
def api_sagi_check():
    data = request.get_json(silent=True) or {}
    job, error = start_check_job(
        str(data.get("input_csv", "")),
        result_csv=str(data.get("result_csv", "")),
        resume=bool(data.get("resume")),
    )
    if error:
        return _job_error_response(error)
    return jsonify({"ok": True, "job": job})


@app.route("/api/sagi/writeback", methods=["POST"])
def api_sagi_writeback():
    data = request.get_json(silent=True) or {}
    job, error = start_writeback_job(
        result_csv=str(data.get("result_csv", "")),
        sheet_id=str(data.get("sheet_id", "")),
        tab_name=str(data.get("tab_name", "")),
        requester=str(data.get("requester", "")),
        dry_run=bool(data.get("dry_run")),
    )
    if error:
        return _job_error_response(error)
    return jsonify({"ok": True, "job": job})


@app.route("/api/sagi/notify-test", methods=["POST"])
def api_sagi_notify_test():
    data = request.get_json(silent=True) or {}
    job, error = start_notify_test_job(str(data.get("requester", "")))
    if error:
        return _job_error_response(error)
    return jsonify({"ok": True, "job": job})


@app.route("/api/jobs/<job_id>")
def api_job(job_id: str):
    job = get_job(job_id)
    if not job:
        return jsonify({"ok": False, "error": "job not found"}), 404
    return jsonify({"ok": True, "job": job})


if __name__ == "__main__":
    port = int(os.environ.get("OPS_PORT", "5070"))
    host = os.environ.get("OPS_HOST", "localhost")
    print("=== Unari Ops Dashboard ===")
    print(f"URL: http://{host}:{port}")
    print(f"Session Capture: http://{host}:{port}/?capture=1")
    socket.getfqdn = lambda name="": name or "localhost"
    app.run(host=host, port=port, debug=False)
