#!/usr/bin/env bash
# Install Instagram APK/APKM/XAPK into the connected ig_capture AVD.

set -euo pipefail

BASE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ADB="${ADB:-$HOME/Library/Android/sdk/platform-tools/adb}"
APK_DIR="$BASE_DIR/apks"
PKG="com.instagram.android"
AVD_NAME="${IG_CAP_AVD:-ig_capture}"
DEVICE=""

mkdir -p "$APK_DIR"

require_device() {
    local devs=()
    local line serial state
    while IFS= read -r line; do
        serial="$(printf '%s' "$line" | awk '{print $1}')"
        state="$(printf '%s' "$line" | awk '{print $2}')"
        if [[ -n "$serial" && "$state" == "device" ]]; then
            devs+=("$serial")
        fi
    done < <("$ADB" devices | awk 'NR>1')
    if [[ "${#devs[@]}" -eq 0 ]]; then
        echo "Android画面が起動していません。先にAVDを起動してください。"
        exit 2
    fi
    if [[ "${#devs[@]}" -gt 1 ]]; then
        echo "複数のAndroid画面が起動しています。不要なAndroid画面を閉じて、$AVD_NAME だけを起動してください。"
        printf '  - %s\n' "${devs[@]}"
        exit 2
    fi
    DEVICE="${devs[0]}"
    local running_name expected_name
    running_name=$(
        "$ADB" -s "$DEVICE" emu avd name 2>/dev/null |
            awk 'NF && $0!="OK" {print; exit}' |
            tr -d '\r' |
            sed -E 's/[^[:print:]]//g; s/^[[:space:]]+//; s/[[:space:]]+$//'
    )
    expected_name=$(
        printf '%s' "$AVD_NAME" |
            tr -d '\r' |
            sed -E 's/[^[:print:]]//g; s/^[[:space:]]+//; s/[[:space:]]+$//'
    )
    if [[ "$running_name" != "$expected_name" ]]; then
        echo "起動中のAVDが $AVD_NAME ではありません: ${running_name:-不明}"
        echo "先に初回セットアップでAVD作成を実行し、$AVD_NAME を起動してからInstagram導入を押してください。"
        exit 2
    fi
    echo "== device: $DEVICE ($AVD_NAME)"
}

is_installed() {
    "$ADB" -s "$DEVICE" shell pm path "$PKG" >/dev/null 2>&1
}

mtime() {
    stat -f %m "$1" 2>/dev/null || stat -c %Y "$1" 2>/dev/null || echo 0
}

find_instagram_package() {
    if [[ -n "${INSTAGRAM_APK_PATH:-}" ]]; then
        if [[ -f "$INSTAGRAM_APK_PATH" ]]; then
            echo "$INSTAGRAM_APK_PATH"
            return 0
        fi
        echo "INSTAGRAM_APK_PATH が見つかりません: $INSTAGRAM_APK_PATH" >&2
        return 1
    fi

    local dirs=("$APK_DIR" "$HOME/Downloads" "$HOME/Desktop")
    local candidates=()
    local dir
    for dir in "${dirs[@]}"; do
        [[ -d "$dir" ]] || continue
        while IFS= read -r f; do
            candidates+=("$f")
        done < <(
            find "$dir" -maxdepth 1 -type f \( \
                -iname '*instagram*.apk' -o \
                -iname '*instagram*.apkm' -o \
                -iname '*instagram*.xapk' \
            \) 2>/dev/null
        )
    done

    if [[ "${#candidates[@]}" -eq 0 ]]; then
        return 1
    fi

    local newest="${candidates[0]}"
    local newest_mtime
    newest_mtime=$(mtime "$newest")
    local f f_mtime
    for f in "${candidates[@]}"; do
        f_mtime=$(mtime "$f")
        if [[ "$f_mtime" -gt "$newest_mtime" ]]; then
            newest="$f"
            newest_mtime="$f_mtime"
        fi
    done
    echo "$newest"
}

install_apk() {
    local apk="$1"
    echo "== Instagram APKをインストール: $(basename "$apk")"
    "$ADB" -s "$DEVICE" install -r "$apk"
}

install_bundle() {
    local bundle="$1"
    local tmp
    tmp="$(mktemp -d)"
    unzip -q "$bundle" -d "$tmp"
    local apks=()
    while IFS= read -r f; do
        apks+=("$f")
    done < <(find "$tmp" -name '*.apk' -type f | sort)
    if [[ "${#apks[@]}" -eq 0 ]]; then
        rm -rf "$tmp"
        echo "APK bundleの中にAPKが見つかりませんでした。"
        exit 2
    fi
    echo "== Instagram bundleをインストール: $(basename "$bundle")"
    "$ADB" -s "$DEVICE" install-multiple -r "${apks[@]}"
    rm -rf "$tmp"
}

main() {
    require_device

    if is_installed; then
        echo "Instagramは既にインストールされています。"
        "$ADB" -s "$DEVICE" shell monkey -p "$PKG" -c android.intent.category.LAUNCHER 1 >/dev/null 2>&1 || true
        exit 0
    fi

    local package_path
    if ! package_path="$(find_instagram_package)"; then
        cat <<EOF
InstagramのAPK/APKM/XAPKが見つかりません。

やること:
1. 最新の Unari Sagi Operator.dmg を管理者から受け取る
2. アプリを開き直す
3. 初回セットアップの「Instagram導入」をもう一度押す

管理者向け:
- 配布版DMGを作る時に Instagram APK/APKM/XAPK を同梱してください
- 同梱先: $APK_DIR
- 手元で追加する場合だけ、$HOME/Downloads に置いて再実行できます

注意:
- このAVDはPlay Storeなしが正常です。root/CA設定のためにGoogle APIs imageを使っています。
EOF
        exit 2
    fi

    local lower
    lower="$(printf '%s' "$package_path" | tr '[:upper:]' '[:lower:]')"
    case "$lower" in
        *.apk) install_apk "$package_path" ;;
        *.apkm|*.xapk) install_bundle "$package_path" ;;
        *) echo "未対応のファイルです: $package_path"; exit 2 ;;
    esac

    if is_installed; then
        echo "Instagramのインストールが完了しました。"
        "$ADB" -s "$DEVICE" shell monkey -p "$PKG" -c android.intent.category.LAUNCHER 1 >/dev/null 2>&1 || true
    else
        echo "インストール後の確認に失敗しました。APKの種類がAVDに合っていない可能性があります。"
        exit 1
    fi
}

main "$@"
