#!/usr/bin/env bash
# AVD (ig_capture) / mitmdump / frida-server が生きているか確認し、死んでたら再起動する。
# 冪等。launchd keepalive から 30 分おきに叩く想定。
#
# 使い方:
#   bash scripts/ensure_capture_infra.sh            # full check
#   bash scripts/ensure_capture_infra.sh --status          # 状態表示のみ、起動はしない
#   bash scripts/ensure_capture_infra.sh --prepare-device  # 初回device設定前にAVD/mitmdumpだけ起動
#
# exit code:
#   0: 全部OK (or 全部起動できた)
#   1: どれかが起動できない (AVD立ち上がらない等)

set -u  # 未定義変数は致命、ただしエラー継続を個別にみたいので -e は使わない

BASE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ANDROID_HOME="${ANDROID_HOME:-$HOME/Library/Android/sdk}"
ANDROID_USER_HOME="${ANDROID_USER_HOME:-$HOME/.android}"
ANDROID_AVD_HOME="${ANDROID_AVD_HOME:-$ANDROID_USER_HOME/avd}"
ADB="$ANDROID_HOME/platform-tools/adb"
EMULATOR="$ANDROID_HOME/emulator/emulator"
AVD_NAME="${IG_CAP_AVD:-ig_capture}"
AVD_DIR="$ANDROID_AVD_HOME/$AVD_NAME.avd"
AVD_TEMP_RUNNING_DIR="$HOME/Library/Caches/TemporaryItems/avd/running"
MITM="$BASE_DIR/venv/bin/mitmdump"
MITM_SCRIPT="$BASE_DIR/scripts/ig_mitm_capture.py"
MITM_CA_SRC="$HOME/.mitmproxy/mitmproxy-ca-cert.pem"
SETUP_DEVICE="$BASE_DIR/scripts/setup_ig_capture_device.sh"
LOG_DIR="$BASE_DIR/logs"
PROXY_PORT="${IG_CAP_PORT:-8080}"
PYTHON_BIN="${PYTHON_BIN:-$BASE_DIR/venv/bin/python}"
if [[ ! -x "$PYTHON_BIN" ]]; then
    PYTHON_BIN="$(command -v python3 || true)"
fi

# capture は基本 direct。外部proxyへは自動では流さない。
# どうしても検証で upstream が必要な場合だけ IG_CAP_USE_UPSTREAM=1 を明示する。
UPSTREAM_ENV="$BASE_DIR/config/soax_upstream.env"
UPSTREAM_ARGS=()
UPSTREAM_MODE="direct"
if [[ "${IG_CAP_USE_UPSTREAM:-0}" == "1" && -f "$UPSTREAM_ENV" ]]; then
    # shellcheck disable=SC1090
    source "$UPSTREAM_ENV"
    if [[ -n "${SOAX_PROXY_HOST:-}" && -n "${SOAX_PROXY_USER:-}" && -n "${SOAX_PROXY_PASS:-}" ]]; then
        UPSTREAM_ARGS=(
            --mode "upstream:http://${SOAX_PROXY_HOST}:${SOAX_PROXY_PORT}"
            --set "upstream_auth=${SOAX_PROXY_USER}:${SOAX_PROXY_PASS}"
        )
        UPSTREAM_MODE="soax:${SOAX_SESSIONID:-?}"
    fi
fi

mkdir -p "$LOG_DIR"
status_only=0
prepare_device=0
case "${1:-}" in
    --status)
        status_only=1
        ;;
    --prepare-device)
        prepare_device=1
        ;;
    "")
        ;;
    *)
        echo "usage: bash $0 [--status|--prepare-device]"
        exit 1
        ;;
esac

echo "== ensure_capture_infra.sh $(date '+%Y-%m-%d %H:%M:%S')"

current_device() {
    "$ADB" devices 2>/dev/null | awk 'NR>1 && $2=="device" {print $1; exit}'
}

mac_lan_ip() {
    local ip iface
    for iface in en0 en1 en2; do
        ip=$(ipconfig getifaddr "$iface" 2>/dev/null || true)
        if [[ -n "$ip" ]]; then
            echo "$ip"
            return 0
        fi
    done
    ifconfig 2>/dev/null | awk '/inet / && $2 != "127.0.0.1" && $2 !~ /^169/ {print $2; exit}'
}

capture_proxy_host() {
    local dev
    dev="$(current_device)"
    if [[ "$dev" == emulator-* ]]; then
        # Android Emulator から見た Mac localhost。MacのLAN IPはWi-Fi変更で古くなりやすい。
        echo "10.0.2.2"
        return 0
    fi
    mac_lan_ip
}

capture_proxy_target() {
    local host
    host="$(capture_proxy_host)"
    if [[ -z "$host" ]]; then
        return 1
    fi
    echo "${host}:${PROXY_PORT}"
}

probe_proxy_from_avd() {
    local target host port
    target="${1:?}"
    host="${target%:*}"
    port="${target##*:}"
    "$ADB" shell "toybox nc -z -w 3 '$host' '$port' >/dev/null 2>&1 || nc -z -w 3 '$host' '$port' >/dev/null 2>&1" >/dev/null 2>&1
}

probe_mac_instagram_dns() {
    if [[ -z "$PYTHON_BIN" || ! -x "$PYTHON_BIN" ]]; then
        echo "[WARN] Pythonが見つからないためInstagram向けDNS確認をスキップ"
        return 0
    fi
    "$PYTHON_BIN" - <<'PY'
import socket
import sys

hosts = [
    "b.i.instagram.com",
    "i.instagram.com",
    "graph.instagram.com",
    "z-p42.graph.instagram.com",
]
failed = []
for host in hosts:
    try:
        socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    except OSError as e:
        failed.append(f"{host}: {e}")

if failed:
    print("[FAIL] MacでInstagram向けDNS解決に失敗")
    for item in failed:
        print(f"  - {item}")
    print("  iPhoneテザリング/Wi-Fiをつなぎ直してから、AVD capture設定をやり直してください。")
    sys.exit(1)

print("[OK] Mac DNS: Instagram接続先を解決できます")
PY
}

wait_for_mitm_ca() {
    local i
    if [[ -f "$MITM_CA_SRC" ]]; then
        echo "[OK] mitmproxy CA = $MITM_CA_SRC"
        return 0
    fi
    for i in $(seq 1 20); do
        sleep 0.5
        if [[ -f "$MITM_CA_SRC" ]]; then
            echo "[OK] mitmproxy CA 生成完了: $MITM_CA_SRC"
            return 0
        fi
    done
    echo "[FAIL] mitmproxy CA が生成されていません: $MITM_CA_SRC"
    echo "       mitmdumpをこのユーザーで起動し直してから、AVD capture設定をやり直してください。"
    return 1
}

any_emulator_process_running() {
    ps -Ao command= 2>/dev/null | awk '
        /[e]mulator/ || /[q]emu-system/ { found=1 }
        END { exit found ? 0 : 1 }
    '
}

avd_process_pids() {
    ps -Ao pid=,command= 2>/dev/null | awk -v avd="$AVD_NAME" '
        /[e]mulator/ || /[q]emu-system/ {
            if ($0 ~ ("-avd[[:space:]]+" avd) || $0 ~ ("-avd-name[[:space:]]+" avd) || $0 ~ ("@" avd "([[:space:]]|$)") || $0 ~ ("/" avd "\\.avd/")) {
                print $1
            }
        }
    '
}

cleanup_stale_avd_locks() {
    local lock tmp_item removed pids
    if [[ ! -d "$AVD_DIR" ]]; then
        return 0
    fi
    pids="$(avd_process_pids | tr '\n' ' ' || true)"
    if [[ -n "$pids" ]]; then
        echo "[INFO] AVD $AVD_NAME のemulatorプロセスが残っています (PID: $pids)。lock掃除はスキップします。"
        return 0
    fi

    removed=0
    for lock in "$AVD_DIR"/*.lock; do
        [[ -e "$lock" ]] || continue
        rm -rf "$lock" && removed=1
    done
    if ! any_emulator_process_running && [[ -d "$AVD_TEMP_RUNNING_DIR" ]]; then
        for tmp_item in "$AVD_TEMP_RUNNING_DIR"/*; do
            [[ -e "$tmp_item" ]] || continue
            rm -rf "$tmp_item" && removed=1
        done
    fi
    if [[ $removed -eq 1 ]]; then
        echo "[OK] 古いAVDロックを掃除しました"
    fi
}

tail_avd_launch_log() {
    local log="${1:-}"
    if [[ -n "$log" && -f "$log" ]]; then
        echo "-- AVD起動ログ末尾: $log"
        tail -80 "$log"
    fi
}

# ---------------------------------------------------------------------------
# 1. AVD が動いているか
# ---------------------------------------------------------------------------
avd_running=0
if "$ADB" devices 2>/dev/null | grep -q "^emulator-5554.*device$"; then
    boot=$("$ADB" shell getprop sys.boot_completed 2>/dev/null | tr -d '\r')
    if [[ "$boot" == "1" ]]; then
        avd_running=1
        echo "[OK] AVD $AVD_NAME 稼働中 (emulator-5554)"
    else
        echo "[WARN] emulator-5554 は見えるがboot未完了"
    fi
else
    existing_avd_pids="$(avd_process_pids | tr '\n' ' ' || true)"
    if [[ -n "$existing_avd_pids" ]]; then
        echo "[WARN] AVD $AVD_NAME のemulatorプロセスはありますがADB接続が未完了です (PID: $existing_avd_pids)"
    else
        echo "[NG] AVD が動いていない"
    fi
fi

if [[ $avd_running -eq 0 && $status_only -eq 0 ]]; then
    cleanup_stale_avd_locks
    echo "-- AVD を起動"
    if [[ ! -x "$EMULATOR" ]]; then
        echo "[FAIL] emulator が見つかりません: $EMULATOR"
        exit 1
    fi
    log="$LOG_DIR/avd_keepalive_$(date +%Y%m%d_%H%M%S).log"
    # デタッチ起動
    nohup "$EMULATOR" -avd "$AVD_NAME" -writable-system -no-snapshot-load -no-snapshot-save -gpu swiftshader_indirect \
        >"$log" 2>&1 &
    echo "  PID=$! LOG=$log"
    # boot待ち (最大180秒)
    for i in $(seq 1 90); do
        sleep 2
        boot=$("$ADB" shell getprop sys.boot_completed 2>/dev/null | tr -d '\r')
        if [[ "$boot" == "1" ]]; then
            avd_running=1
            echo "[OK] AVD boot完了 (約 $((i*2)) 秒)"
            break
        fi
    done
    if [[ $avd_running -eq 0 ]]; then
        echo "[FAIL] AVD boot タイムアウト"
        echo "       Android画面が開かない場合は、前回クラッシュのlockやGPU起動失敗が残っている可能性があります。"
        tail_avd_launch_log "$log"
        exit 1
    fi
fi

# ---------------------------------------------------------------------------
# 2. frida-server が動いているか
# ---------------------------------------------------------------------------
frida_running=0
frida_is_root=0
if [[ $prepare_device -eq 1 ]]; then
    echo "[SKIP] 初回device設定前なので frida-server 確認は後続の setup_ig_capture_device.sh に任せます"
elif [[ $avd_running -eq 1 ]]; then
    if "$ADB" shell ps -A 2>/dev/null | grep -q frida-server; then
        frida_running=1
        echo "[OK] frida-server 稼働中"
        # root権限で動いてるか確認 (Failed to spawn 回避に必須)
        fpid=$("$ADB" shell pidof frida-server 2>/dev/null | tr -d '\r')
        if [[ -n "$fpid" ]]; then
            uid=$("$ADB" shell "cat /proc/$fpid/status 2>/dev/null" | awk '/^Uid:/ {print $2}' | tr -d '\r')
            if [[ "$uid" == "0" ]]; then
                frida_is_root=1
                echo "[OK] frida-server は root 権限で稼働"
            else
                echo "[WARN] frida-server が UID=$uid (rootでない) → 再起動が必要"
            fi
        fi
    else
        echo "[NG] frida-server が止まっている"
    fi
fi

# 起動 or 非root再起動が必要な場合
if [[ ($frida_running -eq 0 || $frida_is_root -eq 0) && $avd_running -eq 1 && $status_only -eq 0 && $prepare_device -eq 0 ]]; then
    echo "-- frida-server を root で起動 (adb root → killall → 再start)"
    "$ADB" root >/dev/null 2>&1
    sleep 2
    "$ADB" shell "killall frida-server 2>/dev/null"
    sleep 1
    "$ADB" shell "/data/local/tmp/frida-server &" >/dev/null 2>&1 &
    sleep 3
    if "$ADB" shell ps -A 2>/dev/null | grep -q frida-server; then
        fpid=$("$ADB" shell pidof frida-server 2>/dev/null | tr -d '\r')
        uid=$("$ADB" shell "cat /proc/$fpid/status 2>/dev/null" | awk '/^Uid:/ {print $2}' | tr -d '\r')
        if [[ "$uid" == "0" ]]; then
            frida_running=1; frida_is_root=1
            echo "[OK] frida-server 起動完了 (root, PID=$fpid)"
        else
            frida_running=1
            echo "[WARN] frida-server 起動したが UID=$uid (rootでない) — adb root が効いてない"
        fi
    else
        echo "[WARN] frida-server 直接起動に失敗。AVDへFridaを入れ直します。"
        if ADB="$ADB" bash "$SETUP_DEVICE" frida; then
            sleep 3
            if "$ADB" shell ps -A 2>/dev/null | grep -q frida-server; then
                fpid=$("$ADB" shell pidof frida-server 2>/dev/null | tr -d '\r')
                uid=$("$ADB" shell "cat /proc/$fpid/status 2>/dev/null" | awk '/^Uid:/ {print $2}' | tr -d '\r')
                if [[ "$uid" == "0" ]]; then
                    frida_running=1; frida_is_root=1
                    echo "[OK] frida-server 入れ直し完了 (root, PID=$fpid)"
                else
                    frida_running=1
                    echo "[WARN] frida-server は起動したが UID=$uid (rootでない)"
                fi
            else
                echo "[FAIL] frida-server 入れ直し後も起動確認できません"
            fi
        else
            echo "[FAIL] frida-server 入れ直し失敗 (AVD capture設定をやり直してください)"
        fi
    fi
fi

# ---------------------------------------------------------------------------
# 3. capture用ローカルproxy設定が正しいか
# ---------------------------------------------------------------------------
proxy_ok=0
expected_proxy=""
if [[ $avd_running -eq 1 ]]; then
    expected_proxy="$(capture_proxy_target || true)"
    if [[ -z "$expected_proxy" ]]; then
        echo "[FAIL] capture用proxyの向き先を決められません"
    fi
    proxy=$("$ADB" shell settings get global http_proxy 2>/dev/null | tr -d '\r')
    if [[ -n "$expected_proxy" && "$proxy" == "$expected_proxy" ]]; then
        proxy_ok=1
        echo "[OK] capture proxy設定 = $proxy"
    else
        echo "[WARN] capture proxy設定が違います: current=${proxy:-empty} expect=${expected_proxy:-unknown}"
        if [[ $status_only -eq 0 && -n "$expected_proxy" ]]; then
            "$ADB" shell settings put global http_proxy "$expected_proxy"
            sleep 1
            proxy=$("$ADB" shell settings get global http_proxy 2>/dev/null | tr -d '\r')
            if [[ "$proxy" == "$expected_proxy" ]]; then
                proxy_ok=1
                echo "[OK] capture proxyを $expected_proxy に修正"
            else
                echo "[FAIL] capture proxy設定に失敗: current=$proxy expect=$expected_proxy"
            fi
        fi
    fi
fi

# ---------------------------------------------------------------------------
# 4. mitmdump が listen しているか
# ---------------------------------------------------------------------------
mitm_up=0
mitm_is_correct_mode=0
mitm_owned=0
port_blocked=0
mitm_pid=""
listener_info="$(lsof -nP -iTCP:$PROXY_PORT -sTCP:LISTEN 2>/dev/null || true)"
# 既に listen 中 かつ upstream mode が期待通りかチェック
if [[ -n "$listener_info" ]]; then
    # 現在走ってる listener の argv をチェックして mitmdump か、upstream mode が一致するかを見る。
    # macOS の lsof COMMAND は mitmdump / python3.14 などに揺れるため、COMMAND名だけでは判定しない。
    mitm_pid=$(printf '%s\n' "$listener_info" | awk 'NR>1 {print $2; exit}')
    if [[ -n "$mitm_pid" ]]; then
        listener_user=$(ps -p "$mitm_pid" -o user= 2>/dev/null | awk '{print $1}')
        current_user=$(id -un 2>/dev/null || true)
        current_args=$(ps -p "$mitm_pid" -o args= 2>/dev/null || true)
        if [[ -n "$listener_user" && -n "$current_user" && "$listener_user" != "$current_user" ]]; then
            port_blocked=1
            echo "[NG] port $PROXY_PORT は別ユーザー($listener_user)のプロセスが使用中です。"
            printf '%s\n' "$listener_info" | sed -n '1,3p'
            echo "     Macを再起動してから、Unari Sagi Operatorを開き直してください。"
        elif [[ "$current_args" == *"ig_mitm_capture.py"* || "$current_args" == *"$MITM_SCRIPT"* ]]; then
            mitm_up=1
            mitm_owned=1
        else
            port_blocked=1
            echo "[NG] port $PROXY_PORT は別プロセスが使用中です。mitmdumpを起動できません。"
            printf '%s\n' "$listener_info" | sed -n '1,3p'
            echo "     Macを再起動してから、Unari Sagi Operatorを開き直してください。"
        fi
    fi
    if [[ $mitm_owned -eq 1 ]]; then
        if [[ "$UPSTREAM_MODE" == "direct" ]]; then
            if [[ "$current_args" != *"upstream:"* ]]; then
                mitm_is_correct_mode=1
                echo "[OK] mitmdump listen中 (:$PROXY_PORT, mode=direct)"
            else
                echo "[WARN] mitmdump 稼働中だが upstream mode、期待は direct → 再起動"
            fi
        else
            if [[ "$current_args" == *"upstream:"* && "$current_args" == *"${SOAX_SESSIONID:-}"* ]]; then
                mitm_is_correct_mode=1
                echo "[OK] mitmdump listen中 (:$PROXY_PORT, mode=$UPSTREAM_MODE)"
            else
                echo "[WARN] mitmdump 稼働中だがモード不一致 → 再起動 (expect=$UPSTREAM_MODE)"
            fi
        fi
    fi
else
    echo "[NG] mitmdump が listen していない (expect mode=$UPSTREAM_MODE)"
fi

if [[ $mitm_is_correct_mode -eq 1 && ! -f "$MITM_CA_SRC" ]]; then
    if [[ $status_only -eq 0 ]]; then
        echo "[WARN] mitmdump はlisten中ですが、このユーザーのmitmproxy CAがありません → 再起動して生成します"
        mitm_is_correct_mode=0
    else
        echo "[NG] mitmdump はlisten中ですが、このユーザーのmitmproxy CAがありません: $MITM_CA_SRC"
        mitm_up=0
    fi
fi

# 再起動が必要な場合
if [[ $mitm_is_correct_mode -eq 0 && $status_only -eq 0 && $port_blocked -eq 0 ]]; then
    # 既存 mitm を止める
    if [[ $mitm_owned -eq 1 && -n "${mitm_pid:-}" ]]; then
        kill "$mitm_pid" 2>/dev/null
        sleep 2
    fi
    echo "-- mitmdump 起動 (mode=$UPSTREAM_MODE)"
    log="$LOG_DIR/mitmdump_keepalive_$(date +%Y%m%d_%H%M%S).log"
    if [[ "$UPSTREAM_MODE" == "direct" ]]; then
        nohup "$MITM" -s "$MITM_SCRIPT" --listen-port "$PROXY_PORT" >"$log" 2>&1 &
    else
        nohup "$MITM" -s "$MITM_SCRIPT" --listen-port "$PROXY_PORT" "${UPSTREAM_ARGS[@]}" >"$log" 2>&1 &
    fi
    echo "  PID=$! LOG=$log"
    sleep 3
    listener_info="$(lsof -nP -iTCP:$PROXY_PORT -sTCP:LISTEN 2>/dev/null || true)"
    mitm_pid=$(printf '%s\n' "$listener_info" | awk 'NR>1 {print $2; exit}')
    current_args=""
    if [[ -n "$mitm_pid" ]]; then
        current_args=$(ps -p "$mitm_pid" -o args= 2>/dev/null || true)
    fi
    if [[ "$current_args" == *"ig_mitm_capture.py"* || "$current_args" == *"$MITM_SCRIPT"* ]]; then
        mitm_up=1
        mitm_is_correct_mode=1
        echo "[OK] mitmdump 起動完了 (mode=$UPSTREAM_MODE)"
    else
        echo "[FAIL] mitmdump 起動失敗 (log: $log)"
        mitm_up=0
    fi
elif [[ $port_blocked -eq 1 && $status_only -eq 0 ]]; then
    echo "[FAIL] mitmdump 起動不可: port $PROXY_PORT が別プロセスに使われています"
fi

if [[ $mitm_up -eq 1 ]]; then
    if ! wait_for_mitm_ca; then
        mitm_up=0
        mitm_is_correct_mode=0
    fi
fi

# ---------------------------------------------------------------------------
# 5. MacからInstagram接続先のDNSを解決できるか
# ---------------------------------------------------------------------------
mac_dns_ok=1
if [[ $mitm_up -eq 1 ]]; then
    if probe_mac_instagram_dns; then
        mac_dns_ok=1
    else
        mac_dns_ok=0
    fi
fi

# ---------------------------------------------------------------------------
# 6. AVD から capture用proxyへ実際に届くか
# ---------------------------------------------------------------------------
proxy_reachable="not_checked"
if [[ $avd_running -eq 1 && $proxy_ok -eq 1 && $mitm_up -eq 1 && -n "$expected_proxy" ]]; then
    if probe_proxy_from_avd "$expected_proxy"; then
        proxy_reachable="yes"
        echo "[OK] AVDからcapture proxyへ接続可能 ($expected_proxy)"
    else
        proxy_reachable="unknown"
        echo "[WARN] AVDからcapture proxyへの接続確認はできませんでした ($expected_proxy)"
        echo "       AVD側の確認コマンドが使えない場合があります。proxy設定とmitmdump待受はOKなので次へ進みます。"
    fi
elif [[ $avd_running -eq 1 ]]; then
    if [[ $mitm_up -eq 0 ]]; then
        echo "[WARN] mitmdump未起動のためproxy到達確認をスキップ"
    elif [[ $proxy_ok -eq 0 ]]; then
        echo "[WARN] capture proxy設定が未修正のため到達確認をスキップ"
    fi
fi

# ---------------------------------------------------------------------------
# summary
# ---------------------------------------------------------------------------
if [[ $prepare_device -eq 1 ]]; then
    echo "== summary: AVD=$avd_running frida=skipped mitm=$mitm_up proxy=$proxy_ok dns=$mac_dns_ok reachable=$proxy_reachable"
else
    echo "== summary: AVD=$avd_running frida=$frida_running mitm=$mitm_up proxy=$proxy_ok dns=$mac_dns_ok reachable=$proxy_reachable"
fi
if [[ $prepare_device -eq 1 && $avd_running -eq 1 && $mitm_up -eq 1 && $proxy_ok -eq 1 && $mac_dns_ok -eq 1 ]]; then
    exit 0
elif [[ $avd_running -eq 1 && $frida_running -eq 1 && $mitm_up -eq 1 && $proxy_ok -eq 1 && $mac_dns_ok -eq 1 ]]; then
    exit 0
else
    exit 1
fi
