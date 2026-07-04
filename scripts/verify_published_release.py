#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

USER_AGENT = "UnariSagiOperatorReleaseVerifier/1.0"


class ReleaseVerificationError(RuntimeError):
    def __init__(self, errors: list[str], *, summary: dict[str, Any] | None = None):
        super().__init__("\n".join(errors))
        self.errors = errors
        self.summary = summary or {}


def _load_json(source: str, timeout: int) -> dict[str, Any]:
    path = Path(source).expanduser()
    if path.exists():
        data = json.loads(path.read_text(encoding="utf-8"))
    else:
        req = urllib.request.Request(source, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=timeout) as res:
            data = json.loads(res.read(2 * 1024 * 1024).decode("utf-8"))
    if not isinstance(data, dict):
        raise ValueError("latest.json must contain a JSON object")
    return data


def _probe_url(url: str, timeout: int) -> dict[str, Any]:
    req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as res:
            return {
                "ok": 200 <= int(res.status) < 400,
                "status": int(res.status),
                "final_url": res.geturl(),
                "content_length": res.headers.get("Content-Length", ""),
            }
    except urllib.error.HTTPError as e:
        if e.code not in {403, 405, 501}:
            return {"ok": False, "status": e.code, "error": str(e)}
    except urllib.error.URLError as e:
        return {"ok": False, "error": str(e)}

    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Range": "bytes=0-0",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as res:
            res.read(1)
            return {
                "ok": int(res.status) in {200, 206},
                "status": int(res.status),
                "final_url": res.geturl(),
                "content_length": res.headers.get("Content-Length", ""),
            }
    except urllib.error.URLError as e:
        return {"ok": False, "error": str(e)}


def _asset_errors(
    *,
    label: str,
    item: Any,
    version: str,
    base_url: str,
    check_url: bool,
    timeout: int,
) -> tuple[list[str], dict[str, Any]]:
    errors: list[str] = []
    summary: dict[str, Any] = {"label": label}
    if not isinstance(item, dict):
        return [f"assets.{label} is missing"], summary

    expected_name = f"UnariSagiOperator-{version}.{label}"
    name = str(item.get("name") or "")
    url = str(item.get("url") or "")
    sha256 = str(item.get("sha256") or "")
    size = item.get("size_bytes")
    summary.update({"name": name, "url": url, "sha256": sha256, "size_bytes": size})

    if version and name != expected_name:
        errors.append(f"assets.{label}.name must be {expected_name}, got {name!r}")
    if not url:
        errors.append(f"assets.{label}.url is empty")
    if base_url and url and not url.startswith(f"{base_url.rstrip('/')}/"):
        errors.append(f"assets.{label}.url must start with {base_url.rstrip('/')}/")
    if not re.fullmatch(r"[0-9a-fA-F]{64}", sha256):
        errors.append(f"assets.{label}.sha256 must be a 64-char hex digest")
    if not isinstance(size, int) or size <= 0:
        errors.append(f"assets.{label}.size_bytes must be a positive integer")

    if check_url and url:
        probe = _probe_url(url, timeout)
        summary["probe"] = probe
        if not probe.get("ok"):
            errors.append(f"assets.{label}.url is not reachable: {probe}")

    return errors, summary


def verify_manifest(args: argparse.Namespace) -> dict[str, Any]:
    manifest = _load_json(args.latest_url, args.timeout)
    errors: list[str] = []

    version = str(manifest.get("version") or "")
    build = str(manifest.get("build") or "")
    download_url = str(manifest.get("download_url") or "")
    assets = manifest.get("assets")
    if not isinstance(assets, dict):
        assets = {}
        errors.append("assets must be a JSON object")

    if args.version and version != args.version:
        errors.append(f"version must be {args.version}, got {version!r}")
    if args.build and build != args.build:
        errors.append(f"build must be {args.build}, got {build!r}")
    if not version:
        errors.append("version is empty")
    if not build:
        errors.append("build is empty")
    if not download_url:
        errors.append("download_url is empty")

    asset_summaries = []
    for label in ("dmg", "zip"):
        item_errors, item_summary = _asset_errors(
            label=label,
            item=assets.get(label),
            version=version,
            base_url=args.base_url,
            check_url=args.check_assets,
            timeout=args.timeout,
        )
        errors.extend(item_errors)
        asset_summaries.append(item_summary)

    dmg_url = str((assets.get("dmg") or {}).get("url") or "") if isinstance(assets, dict) else ""
    if download_url and dmg_url and download_url != dmg_url:
        errors.append("download_url must match assets.dmg.url")

    summary = {
        "latest_url": args.latest_url,
        "version": version,
        "build": build,
        "download_url": download_url,
        "assets": asset_summaries,
    }
    if errors:
        raise ReleaseVerificationError(errors, summary=summary)
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify the public latest.json for a member release")
    parser.add_argument("--latest-url", required=True)
    parser.add_argument("--version", default="")
    parser.add_argument("--build", default="")
    parser.add_argument("--base-url", default="")
    parser.add_argument("--check-assets", action="store_true", help="HEAD/range-check DMG and ZIP URLs")
    parser.add_argument("--timeout", type=int, default=15)
    parser.add_argument("--retries", type=int, default=1)
    parser.add_argument("--retry-delay", type=float, default=5.0)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    last_error: ReleaseVerificationError | Exception | None = None
    for attempt in range(1, max(args.retries, 1) + 1):
        try:
            summary = verify_manifest(args)
            payload = {"ok": True, **summary}
            if args.json:
                print(json.dumps(payload, ensure_ascii=False, indent=2))
            else:
                print(f"[OK] published release {summary['version']} ({summary['build']})")
                print(f"latest: {summary['latest_url']}")
                print(f"dmg: {summary['download_url']}")
            return 0
        except (OSError, ValueError, json.JSONDecodeError, urllib.error.URLError, ReleaseVerificationError) as e:
            last_error = e
            if attempt < max(args.retries, 1):
                print(f"[WAIT] published release verification failed; retrying ({attempt}/{args.retries}): {e}")
                time.sleep(args.retry_delay)

    if isinstance(last_error, ReleaseVerificationError):
        payload = {"ok": False, "errors": last_error.errors, **last_error.summary}
    else:
        payload = {"ok": False, "errors": [f"{type(last_error).__name__}: {last_error}"]}
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print("[NG] published release verification failed", file=sys.stderr)
        for error in payload["errors"]:
            print(f"- {error}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
