#!/usr/bin/env bash
# rootable AVD を作成する。Play Store image は root 不可なので google_apis を使う。
# Mac 側の手作業用。1回だけ実行すれば AVD が使い回せる。
#
# 使い方:
#   bash scripts/setup_ig_capture_avd.sh
#   # 起動:
#   bash scripts/setup_ig_capture_avd.sh run

set -euo pipefail

ANDROID_HOME="${ANDROID_HOME:-$HOME/Library/Android/sdk}"
export ANDROID_HOME
export ANDROID_SDK_ROOT="$ANDROID_HOME"
export ANDROID_USER_HOME="${ANDROID_USER_HOME:-$HOME/.android}"
export ANDROID_AVD_HOME="${ANDROID_AVD_HOME:-$ANDROID_USER_HOME/avd}"
LOCAL_JDK_HOME="$HOME/Library/Application Support/UnariSagiOperator/jdk/temurin-17"
if [[ -x "$LOCAL_JDK_HOME/bin/java" ]]; then
    export JAVA_HOME="$LOCAL_JDK_HOME"
    export PATH="$JAVA_HOME/bin:$PATH"
fi
API_LEVEL="${IG_CAP_API:-33}"   # Android 13 が Frida/IG 互換性◎
ABI="${IG_CAP_ABI:-arm64-v8a}"  # Apple Silicon 前提
TAG="google_apis"               # NOT google_apis_playstore (root必須)
AVD_NAME="${IG_CAP_AVD:-ig_capture}"
DEVICE_PROFILE="${IG_CAP_DEVICE:-pixel_6}"
AVD_DIR="$ANDROID_AVD_HOME/$AVD_NAME.avd"
AVD_TEMP_RUNNING_DIR="$HOME/Library/Caches/TemporaryItems/avd/running"

SDKMANAGER="$ANDROID_HOME/cmdline-tools/latest/bin/sdkmanager"
AVDMANAGER="$ANDROID_HOME/cmdline-tools/latest/bin/avdmanager"
EMULATOR="$ANDROID_HOME/emulator/emulator"

# fallback paths (cmdline-tools が別バージョンに入っていることがある)
if [[ ! -x "$SDKMANAGER" ]]; then
    SDKMANAGER=$(ls -1 $ANDROID_HOME/cmdline-tools/*/bin/sdkmanager 2>/dev/null | head -1 || true)
fi
if [[ ! -x "$AVDMANAGER" ]]; then
    AVDMANAGER=$(ls -1 $ANDROID_HOME/cmdline-tools/*/bin/avdmanager 2>/dev/null | head -1 || true)
fi

: "${SDKMANAGER:?sdkmanager not found. Install Android cmdline-tools first.}"
: "${AVDMANAGER:?avdmanager not found.}"

IMAGE="system-images;android-${API_LEVEL};${TAG};${ABI}"

action="${1:-setup}"

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

case "$action" in
  setup)
    echo "== Installing system image: $IMAGE =="
    printf 'y\n' | "$SDKMANAGER" "$IMAGE"

    echo "== Creating AVD: $AVD_NAME =="
    if "$AVDMANAGER" list avd | grep -q "Name: $AVD_NAME"; then
        echo "AVD '$AVD_NAME' は既に存在します。削除する場合:"
        echo "  $AVDMANAGER delete avd -n $AVD_NAME"
    else
        echo no | "$AVDMANAGER" create avd \
            -n "$AVD_NAME" \
            -k "$IMAGE" \
            -d "$DEVICE_PROFILE"
        echo "AVD '$AVD_NAME' 作成完了"
    fi
    echo
    echo "次のステップ: AVDを起動（-writable-system 必須）"
    echo "  bash scripts/setup_ig_capture_avd.sh run"
    ;;

  run)
    echo "== Launching $AVD_NAME with -writable-system =="
    echo "(別ターミナルで動かしてください。起動後 emulator-5554 で adb 接続されます)"
    cleanup_stale_avd_locks
    exec "$EMULATOR" -avd "$AVD_NAME" -writable-system -no-snapshot-load -no-snapshot-save -gpu swiftshader_indirect
    ;;

  *)
    echo "usage: bash $0 [setup|run]"
    exit 1
    ;;
esac
