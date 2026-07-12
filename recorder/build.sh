#!/usr/bin/env bash
#
# Build ScreenpipeRecorder.app — a stable macOS permission identity for the
# screenpipe CLI recorder (see launcher.swift for why).
#
# Prereqs: swift toolchain; screenpipe CLI installed (npm i -g screenpipe).

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

APP_NAME="ScreenpipeRecorder"
BUNDLE_ID="com.dayloop.screenpipe-recorder"
APP_DIR="${SCRIPT_DIR}/${APP_NAME}.app"
MACOS_DIR="${APP_DIR}/Contents/MacOS"

echo "==> compiling launcher"
# Pin the deployment target: beta toolchains otherwise stamp the binary with
# their own (newer) SDK version and Launch Services refuses to open it (-10825).
ARCH="$(uname -m)"
swiftc -O -target "${ARCH}-apple-macosx13.0" -o "${SCRIPT_DIR}/${APP_NAME}" launcher.swift

echo "==> assembling ${APP_NAME}.app"
rm -rf "${APP_DIR}"
mkdir -p "${MACOS_DIR}"
mv "${SCRIPT_DIR}/${APP_NAME}" "${MACOS_DIR}/${APP_NAME}"

cat > "${APP_DIR}/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleName</key>              <string>${APP_NAME}</string>
    <key>CFBundleDisplayName</key>       <string>Screenpipe Recorder</string>
    <key>CFBundleExecutable</key>        <string>${APP_NAME}</string>
    <key>CFBundleIdentifier</key>        <string>${BUNDLE_ID}</string>
    <key>CFBundlePackageType</key>       <string>APPL</string>
    <key>CFBundleShortVersionString</key><string>1.0.0</string>
    <key>CFBundleVersion</key>           <string>1.0.0</string>
    <key>LSMinimumSystemVersion</key>    <string>13.0</string>
    <key>LSUIElement</key>               <true/>
    <key>NSMicrophoneUsageDescription</key>
    <string>Screenpipe Recorder transcribes meetings and audio locally for your ScoreGoals timeline.</string>
</dict>
</plist>
PLIST

printf 'APPL????' > "${APP_DIR}/Contents/PkgInfo"

echo "==> ad-hoc codesign"
codesign --force --sign - "${APP_DIR}"
codesign --verify --verbose "${APP_DIR}"

echo
echo "Built: ${APP_DIR}"
echo
echo "Next steps:"
echo "  1. open \"${APP_DIR}\"   (first time: right-click -> Open)"
echo "  2. Grant permissions in System Settings -> Privacy & Security:"
echo "       Screen & System Audio Recording -> enable 'Screenpipe Recorder'"
echo "       Microphone                      -> enable 'Screenpipe Recorder'"
echo "     (macOS will also prompt on first capture attempt)"
echo "  3. Add to login: System Settings -> General -> Login Items -> + -> ${APP_NAME}.app"
echo "  4. Verify: curl -s localhost:3030/health | head -c 200"
echo "     Log:    tail -f ~/Library/Logs/screenpipe-recorder.log"
