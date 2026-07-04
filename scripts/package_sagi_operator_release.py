#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
JST = timezone(timedelta(hours=9))
APP_DIR = ROOT / "dist" / "Unari Sagi Operator.app"


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


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


def default_version() -> str:
    return datetime.now(JST).strftime("%Y.%m.%d.%H%M")


def make_zip(app_dir: Path, zip_path: Path) -> None:
    zip_path.unlink(missing_ok=True)
    subprocess.run(
        ["/usr/bin/zip", "-qryX", str(zip_path), app_dir.name],
        cwd=app_dir.parent,
        check=True,
        timeout=600,
    )


def make_dmg(app_dir: Path, dmg_path: Path) -> None:
    dmg_path.unlink(missing_ok=True)
    with tempfile.TemporaryDirectory(prefix="unari_sagi_release_dmg_") as td:
        stage = Path(td)
        shutil.copytree(app_dir, stage / app_dir.name, symlinks=True)
        env = os.environ.copy()
        env["COPYFILE_DISABLE"] = "1"
        tmp_dmg = stage.parent / f"{dmg_path.stem}.tmp.dmg"
        cmd = [
            "hdiutil",
            "create",
            "-volname",
            "Unari Sagi Operator",
            "-srcfolder",
            str(stage),
            "-ov",
            "-format",
            "UDZO",
            str(tmp_dmg),
        ]
        last_error: subprocess.CalledProcessError | None = None
        for attempt in range(1, 4):
            tmp_dmg.unlink(missing_ok=True)
            result = subprocess.run(
                cmd,
                cwd=ROOT,
                timeout=600,
                env=env,
                text=True,
                capture_output=True,
            )
            if result.returncode == 0:
                tmp_dmg.replace(dmg_path)
                return
            last_error = subprocess.CalledProcessError(
                result.returncode,
                cmd,
                output=result.stdout,
                stderr=result.stderr,
            )
            print(result.stdout, end="")
            print(result.stderr, end="")
            if "Resource busy" not in f"{result.stdout}\n{result.stderr}":
                raise last_error
            print(f"hdiutil create failed with Resource busy; retrying ({attempt}/3)")
            time.sleep(5 * attempt)
        assert last_error is not None
        raise last_error


def asset_entry(path: Path, url: str) -> dict:
    return {
        "name": path.name,
        "url": url,
        "sha256": sha256(path),
        "size_bytes": path.stat().st_size,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Unari Sagi Operatorの配布用DMG/ZIP/latest.jsonを生成")
    parser.add_argument("--version", default=os.environ.get("SAGI_OPERATOR_VERSION") or default_version())
    parser.add_argument("--base-url", default=os.environ.get("SAGI_OPERATOR_RELEASE_BASE_URL", "").rstrip("/"))
    parser.add_argument("--output-dir", default=str(ROOT / "dist" / "sagi_operator_release"))
    parser.add_argument("--skip-build", action="store_true", help="既存のdist appからDMG/ZIPだけ作る")
    parser.add_argument("--release-notes", default="")
    args = parser.parse_args()

    output_dir = Path(args.output_dir).expanduser()
    output_dir.mkdir(parents=True, exist_ok=True)

    if not args.skip_build:
        env = os.environ.copy()
        env["SAGI_OPERATOR_VERSION"] = args.version
        env.setdefault("SAGI_OPERATOR_REQUIRE_INSTAGRAM_PACKAGE", "1")
        env.setdefault("SAGI_OPERATOR_REQUIRE_MEMBERS_CONFIG", "1")
        env.setdefault("SAGI_OPERATOR_REQUIRE_SHEETS_BRIDGE_CONFIG", "1")
        env.setdefault("SAGI_OPERATOR_REQUIRE_CAPTURE_TOOLS", "1")
        subprocess.run(["make", "sagi-operator-install-app"], cwd=ROOT, check=True, timeout=900, env=env)

    if not APP_DIR.exists():
        raise FileNotFoundError(f"appがありません: {APP_DIR}")

    version_slug = args.version.replace("/", "-")
    zip_path = output_dir / f"UnariSagiOperator-{version_slug}.zip"
    dmg_path = output_dir / f"UnariSagiOperator-{version_slug}.dmg"

    make_zip(APP_DIR, zip_path)
    make_dmg(APP_DIR, dmg_path)

    base_url = args.base_url
    dmg_url = f"{base_url}/{dmg_path.name}" if base_url else ""
    zip_url = f"{base_url}/{zip_path.name}" if base_url else ""
    manifest = {
        "app": "Unari Sagi Operator",
        "version": args.version,
        "build": git_short_sha(),
        "published_at": datetime.now(JST).isoformat(timespec="seconds"),
        "download_url": dmg_url or zip_url,
        "release_notes": args.release_notes,
        "assets": {
            "dmg": asset_entry(dmg_path, dmg_url),
            "zip": asset_entry(zip_path, zip_url),
        },
    }
    latest_path = output_dir / "latest.json"
    latest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print(f"version: {args.version}")
    print(f"dmg: {dmg_path}")
    print(f"zip: {zip_path}")
    print(f"latest: {latest_path}")
    print(f"dmg sha256: {manifest['assets']['dmg']['sha256']}")
    print(f"zip sha256: {manifest['assets']['zip']['sha256']}")
    if not base_url:
        print("base-url未指定: latest.jsonのurl欄は空です。アップロード先が決まったら --base-url を付けて再生成してください。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
