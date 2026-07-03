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
    exec "$EMULATOR" -avd "$AVD_NAME" -writable-system -no-snapshot-load
    ;;

  *)
    echo "usage: bash $0 [setup|run]"
    exit 1
    ;;
esac
