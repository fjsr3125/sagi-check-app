#!/usr/bin/env bash
# 起動中のAVD/実機に対して、capture用セットアップを一気に流す:
# 1) mitmproxy CA を system CA に push
# 2) Frida server を push して起動
# 3) プロキシを mitmdump に向ける
#
# 事前条件:
#   - AVD が -writable-system で起動済み、または rooted 実機が adb 接続済み
#   - tools/{hash}.0  が存在（スクリプトが自動で mitmproxy CA から生成）
#   - tools/frida-multiple-unpinning.js が存在（setup済み）
#
# 使い方:
#   bash scripts/setup_ig_capture_device.sh             # フル実行
#   bash scripts/setup_ig_capture_device.sh ca          # CA 配置だけ
#   bash scripts/setup_ig_capture_device.sh proxy       # プロキシ設定だけ
#   bash scripts/setup_ig_capture_device.sh proxy-off   # プロキシ解除

set -euo pipefail

BASE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TOOLS_DIR="$BASE_DIR/tools"
ADB="${ADB:-$HOME/Library/Android/sdk/platform-tools/adb}"

MITM_CA_SRC="$HOME/.mitmproxy/mitmproxy-ca-cert.pem"
PROXY_PORT="${IG_CAP_PORT:-8080}"
# Frida server のバージョンを環境変数で上書き可能
FRIDA_VERSION="${FRIDA_VERSION:-17.9.1}"
PYTHON_BIN="${PYTHON_BIN:-$BASE_DIR/venv/bin/python}"
if [[ ! -x "$PYTHON_BIN" ]]; then
    PYTHON_BIN="$(command -v python3 || true)"
fi

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

require_device() {
    local devs
    devs=$("$ADB" devices | awk 'NR>1 && $2=="device" {print $1}')
    if [[ -z "$devs" ]]; then
        echo "✗ adb に接続されているデバイスがありません。AVD起動中か確認してください" >&2
        exit 1
    fi
    echo "== device: $devs"
}

wait_boot_completed() {
    echo "== boot完了待ち"
    "$ADB" wait-for-device
    for _ in $(seq 1 90); do
        local boot
        boot=$("$ADB" shell getprop sys.boot_completed 2>/dev/null | tr -d '\r')
        if [[ "$boot" == "1" ]]; then
            echo "✓ boot完了"
            return 0
        fi
        sleep 2
    done
    echo "✗ boot完了待ちがタイムアウトしました" >&2
    exit 1
}

mac_lan_ip() {
    local ip
    for iface in en0 en1 en2; do
        ip=$(ipconfig getifaddr "$iface" 2>/dev/null || true)
        [[ -n "$ip" ]] && echo "$ip" && return 0
    done
    echo "✗ Mac の LAN IP が取得できません" >&2
    exit 1
}

current_device() {
    "$ADB" devices | awk 'NR>1 && $2=="device" {print $1; exit}'
}

proxy_host() {
    local dev
    dev="$(current_device)"
    if [[ "$dev" == emulator-* ]]; then
        # Android Emulator から見たMac側localhost。LAN IPはWi-Fi変更で古くなりやすい。
        echo "10.0.2.2"
        return 0
    fi
    mac_lan_ip
}

sync_frida_config() {
    local config="$TOOLS_DIR/config.js"
    local host fp
    if [[ ! -f "$config" ]]; then
        echo "✗ $config が無い" >&2
        exit 1
    fi
    if [[ ! -f "$MITM_CA_SRC" ]]; then
        echo "✗ $MITM_CA_SRC が無い。一度 mitmdump を起動してCAを生成してください" >&2
        exit 1
    fi
    if [[ -z "$PYTHON_BIN" || ! -x "$PYTHON_BIN" ]]; then
        echo "✗ Python が見つからず Frida config を同期できません" >&2
        exit 1
    fi
    host=$(proxy_host)
    "$PYTHON_BIN" - "$config" "$MITM_CA_SRC" "$host" "$PROXY_PORT" <<'PY'
import re
import sys
from pathlib import Path

config_path = Path(sys.argv[1])
ca_path = Path(sys.argv[2])
host = sys.argv[3]
port = int(sys.argv[4])

pem = ca_path.read_text(encoding="utf-8").strip()
if not pem.startswith("-----BEGIN CERTIFICATE-----") or not pem.endswith("-----END CERTIFICATE-----"):
    raise SystemExit(f"invalid mitmproxy CA PEM: {ca_path}")

text = config_path.read_text(encoding="utf-8")
updated, cert_count = re.subn(
    r"const CERT_PEM = `-----BEGIN CERTIFICATE-----.*?-----END CERTIFICATE-----`;",
    "const CERT_PEM = `" + pem + "`;",
    text,
    count=1,
    flags=re.S,
)
if cert_count != 1:
    raise SystemExit("CERT_PEM block not found in tools/config.js")
updated, host_count = re.subn(r"const PROXY_HOST = '[^']+';", f"const PROXY_HOST = '{host}';", updated, count=1)
updated, port_count = re.subn(r"const PROXY_PORT = \d+;", f"const PROXY_PORT = {port};", updated, count=1)
if host_count != 1 or port_count != 1:
    raise SystemExit("PROXY_HOST/PROXY_PORT not found in tools/config.js")
if updated != text:
    config_path.write_text(updated, encoding="utf-8")
PY
    fp=$(openssl x509 -noout -fingerprint -sha256 -in "$MITM_CA_SRC" 2>/dev/null | cut -d= -f2 || true)
    if [[ -n "$fp" ]]; then
        echo "✓ Frida config synced to mitmproxy CA ($fp) and proxy ${host}:${PROXY_PORT}"
    else
        echo "✓ Frida config synced to mitmproxy CA and proxy ${host}:${PROXY_PORT}"
    fi
}

detect_abi() {
    "$ADB" shell getprop ro.product.cpu.abi | tr -d '\r'
}

install_ca() {
    require_device
    if [[ ! -f "$MITM_CA_SRC" ]]; then
        echo "✗ $MITM_CA_SRC が無い。一度 mitmdump を起動してCAを生成してください" >&2
        exit 1
    fi
    local hash cert_name
    hash=$(openssl x509 -inform PEM -subject_hash_old -in "$MITM_CA_SRC" | head -1)
    cert_name="${hash}.0"
    cp "$MITM_CA_SRC" "$TOOLS_DIR/$cert_name"
    echo "== CA hash: $hash"
    sync_frida_config

    echo "== adb root / remount"
    "$ADB" root >/dev/null 2>&1 || true
    sleep 1
    "$ADB" wait-for-device
    "$ADB" remount

    echo "== push to /system/etc/security/cacerts/"
    "$ADB" push "$TOOLS_DIR/$cert_name" "/system/etc/security/cacerts/$cert_name"
    "$ADB" shell chmod 644 "/system/etc/security/cacerts/$cert_name"

    # Android 14+ 用 (APEX conscrypt)
    if "$ADB" shell "[ -d /apex/com.android.conscrypt/cacerts ]" 2>/dev/null; then
        echo "== Android 14+ 検出: APEX conscrypt にも配置"
        "$ADB" push "$TOOLS_DIR/$cert_name" "/apex/com.android.conscrypt/cacerts/$cert_name" || true
        "$ADB" shell chmod 644 "/apex/com.android.conscrypt/cacerts/$cert_name" || true
    fi

    echo "== reboot してCAを反映"
    "$ADB" reboot
    echo "端末の再起動を待っています..."
    wait_boot_completed
    echo "✓ CA install 完了"
}

install_frida() {
    require_device
    local abi arch fname url
    abi=$(detect_abi)
    case "$abi" in
        arm64-v8a) arch=arm64 ;;
        armeabi-v7a|armeabi) arch=arm ;;
        x86_64) arch=x86_64 ;;
        x86) arch=x86 ;;
        *) echo "✗ 未知のABI: $abi"; exit 1 ;;
    esac

    fname="frida-server-${FRIDA_VERSION}-android-${arch}"
    url="https://github.com/frida/frida/releases/download/${FRIDA_VERSION}/${fname}.xz"
    mkdir -p "$TOOLS_DIR"
    if [[ ! -f "$TOOLS_DIR/$fname" ]]; then
        echo "== download: $url"
        curl -sL "$url" -o "$TOOLS_DIR/${fname}.xz"
        xz -d "$TOOLS_DIR/${fname}.xz"
    fi

    echo "== push frida-server"
    "$ADB" root >/dev/null 2>&1 || true
    sleep 1
    "$ADB" push "$TOOLS_DIR/$fname" /data/local/tmp/frida-server
    "$ADB" shell chmod 755 /data/local/tmp/frida-server
    echo "== start frida-server (background)"
    "$ADB" shell "killall frida-server 2>/dev/null; nohup /data/local/tmp/frida-server >/dev/null 2>&1 &"
    sleep 2
    echo "✓ frida-server 起動"
}

set_proxy() {
    require_device
    local host
    host=$(proxy_host)
    "$ADB" shell settings put global http_proxy "${host}:${PROXY_PORT}"
    echo "✓ proxy set to ${host}:${PROXY_PORT}"
    sync_frida_config
}

unset_proxy() {
    require_device
    "$ADB" shell settings put global http_proxy :0
    echo "✓ proxy unset"
}

run_ig_with_frida() {
    require_device
    local config="$TOOLS_DIR/config.js"
    local unpin="$TOOLS_DIR/android-unpinning-httptoolkit.js"
    local fallback="$TOOLS_DIR/android-unpinning-fallback.js"
    for f in "$config" "$unpin" "$fallback"; do
        if [[ ! -f "$f" ]]; then echo "✗ $f が無い" >&2; exit 1; fi
    done
    sync_frida_config
    echo "== spawn com.instagram.android with httptoolkit unpinning (native+Java)"
    "$BASE_DIR/venv/bin/frida" -U -f com.instagram.android \
        -l "$config" \
        -l "$unpin" \
        -l "$fallback"
}

# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

action="${1:-all}"
case "$action" in
    ca)         install_ca ;;
    frida)      install_frida ;;
    proxy)      set_proxy ;;
    proxy-off)  unset_proxy ;;
    run)        run_ig_with_frida ;;
    all)
        install_ca
        install_frida
        set_proxy
        echo
        echo "次のステップ:"
        echo "  1. ターミナルA: ./venv/bin/mitmproxy -s scripts/ig_mitm_capture.py --listen-port $PROXY_PORT"
        echo "  2. ターミナルB: bash scripts/setup_ig_capture_device.sh run"
        echo "     → IG app が Frida 経由で spawn され、pin解除済みで起動"
        echo "  3. IGアプリで対象アカウントにログイン→フィードを少しスクロール"
        echo "  4. captures/{ds_user_id}.json が生成されたら"
        echo "     ./venv/bin/python scripts/import_real_session.py --all"
        ;;
    *)
        echo "usage: bash $0 [all|ca|frida|proxy|proxy-off|run]"
        exit 1
        ;;
esac
