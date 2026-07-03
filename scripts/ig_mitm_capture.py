"""
mitmproxy addon: Instagram アプリの API traffic を傍受し、
sessionid / UUIDs / headers を抽出して captures/{ds_user_id}.json に保存する。

起動:
    mitmproxy -s scripts/ig_mitm_capture.py --listen-port 8080
    # または GUI不要なら
    mitmdump -s scripts/ig_mitm_capture.py --listen-port 8080

Android 実機/エミュを Mac の 8080 にプロキシさせ、IG アプリを開けば
最初の API リクエストで 1 回だけ captures/{ds_user_id}.json が生成される。
同じ ds_user_id は重複書き出しされない。

mitmproxy の依存は venv 外でもよい（グローバル or venv に任意で入れる）。
"""
from __future__ import annotations

import base64
import json
import os
import tempfile
from datetime import datetime
from pathlib import Path

from mitmproxy import http


def _parse_authorization_header(auth: str) -> dict:
    """IG の 'Authorization: Bearer IGT:2:{base64_json}' をパースする。

    base64 部分を decode すると:
      {"ds_user_id":"...", "sessionid":"...", "should_use_header_over_cookies": ...}
    のような JSON が入っている。
    """
    if not auth:
        return {}
    # "Bearer IGT:2:..." 以外のフォーマットもあるので柔軟に
    if "IGT:" not in auth:
        return {}
    try:
        b64 = auth.split("IGT:", 1)[1].split(":", 1)[-1].strip()
        # padding 補正
        pad = (-len(b64)) % 4
        raw = base64.b64decode(b64 + ("=" * pad))
        return json.loads(raw)
    except Exception as e:
        print(f"[ig-capture] authz parse failed: {type(e).__name__}: {e}")
        return {}

BASE_DIR = Path(__file__).resolve().parent.parent
CAPTURES_DIR = BASE_DIR / "captures"


IG_HOSTS = ("i.instagram.com", "b.i.instagram.com", "graph.instagram.com")


class IGSessionCapture:
    def __init__(self) -> None:
        CAPTURES_DIR.mkdir(parents=True, exist_ok=True)
        self._overwrite = os.environ.get("IG_CAPTURE_OVERWRITE") == "1"
        self._seen: set[str] = set()
        # 起動済みの ds_user_id を restore（再起動でも二重書きしないため）
        if not self._overwrite:
            for p in CAPTURES_DIR.glob("*.json"):
                self._seen.add(p.stem)
        print(f"[ig-capture] ready. captures dir: {CAPTURES_DIR}")
        if self._overwrite:
            print("[ig-capture] overwrite mode enabled")
        if self._seen:
            print(f"[ig-capture] already captured ds_user_ids: {sorted(self._seen)}")

    def request(self, flow: http.HTTPFlow) -> None:
        try:
            if flow.request.pretty_host not in IG_HOSTS:
                return

            # 全 IG request を可視化（診断）
            cookie_raw = flow.request.headers.get("cookie") or ""
            cookie_keys = [c.split("=")[0].strip() for c in cookie_raw.split(";") if c.strip()]
            print(
                f"[ig-capture:seen] {flow.request.method} "
                f"{flow.request.pretty_host}{flow.request.path[:80]} "
                f"cookie_keys={cookie_keys[:8]}"
            )

            if not flow.request.path.startswith("/api/v1/"):
                return

            # cookie header パース（古い IG 版用）
            cookies = dict(flow.request.cookies)
            if not cookies and cookie_raw:
                for part in cookie_raw.split(";"):
                    if "=" in part:
                        k, v = part.strip().split("=", 1)
                        cookies[k] = v

            headers = {k.lower(): v for k, v in flow.request.headers.items(multi=False)}

            # 新 IG 版は Cookie ではなく Authorization header を使う
            authz = headers.get("authorization") or ""
            authz_claims = _parse_authorization_header(authz)

            ds_user_id = cookies.get("ds_user_id") or authz_claims.get("ds_user_id")
            sessionid = cookies.get("sessionid") or authz_claims.get("sessionid")

            if not ds_user_id:
                print(
                    f"[ig-capture:no-dsid] {flow.request.path[:60]} "
                    f"cookie_keys={list(cookies.keys())} "
                    f"authz_keys={list(authz_claims.keys())}"
                )
                return
            if ds_user_id in self._seen:
                return
            if not sessionid:
                print(
                    f"[ig-capture:no-sid] ds_user_id={ds_user_id} "
                    f"authz_keys={list(authz_claims.keys())}"
                )
                return

            capture = {
                "ds_user_id": str(ds_user_id),
                "sessionid": sessionid,
                "mid": (cookies.get("mid")
                        or authz_claims.get("mid")
                        or headers.get("x-mid")),
                "rur": cookies.get("rur") or authz_claims.get("rur"),
                "csrftoken": (cookies.get("csrftoken")
                              or authz_claims.get("csrftoken")),
                "ig_did": (cookies.get("ig-did")
                           or cookies.get("ig_did")
                           or authz_claims.get("ig_did")),
                "x_ig_www_claim": headers.get("x-ig-www-claim"),
                "user_agent": headers.get("user-agent"),
                "x_ig_android_id": headers.get("x-ig-android-id"),
                "x_ig_device_id": headers.get("x-ig-device-id"),
                "x_ig_family_device_id": headers.get("x-ig-family-device-id"),
                "x_ig_app_id": headers.get("x-ig-app-id"),
                "x_bloks_version_id": headers.get("x-bloks-version-id"),
                "authorization_claims": authz_claims,
                "captured_at": datetime.now().isoformat(),
                "source_endpoint": flow.request.path,
                "source_host": flow.request.pretty_host,
            }

            self._write(ds_user_id, capture)
            self._seen.add(ds_user_id)
            print(
                f"[ig-capture] wrote captures/{ds_user_id}.json "
                f"(endpoint={flow.request.path})"
            )
        except Exception as e:
            # mitmproxy を落とさない
            print(f"[ig-capture] extract failed: {type(e).__name__}: {e}")

    @staticmethod
    def _write(ds_user_id: str, capture: dict) -> None:
        dest = CAPTURES_DIR / f"{ds_user_id}.json"
        fd, tmp = tempfile.mkstemp(
            prefix=f"{ds_user_id}.", suffix=".tmp", dir=str(CAPTURES_DIR)
        )
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(capture, f, indent=2, ensure_ascii=False)
            os.replace(tmp, dest)
        except Exception:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise


addons = [IGSessionCapture()]
