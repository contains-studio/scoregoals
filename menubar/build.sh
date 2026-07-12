#!/usr/bin/env bash
#
# Build ScoreGoals.app from the SwiftPM executable.
#
#   1. swift build -c release            -> .build/release/ScoreGoals
#   2. assemble ScoreGoals.app bundle    (Contents/MacOS + Info.plist)
#   3. ad-hoc codesign                   (codesign -s -)
#   4. print how to run it
#
# No Xcode / xcodegen required — pure SwiftPM.

set -euo pipefail

# Resolve this script's directory so the build works from anywhere.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

APP_NAME="ScoreGoals"
BUNDLE_ID="com.scoregoals.menubar"
VERSION="0.1.0"
CONFIG="release"

BUILD_DIR=".build/${CONFIG}"
APP_DIR="${SCRIPT_DIR}/${APP_NAME}.app"
MACOS_DIR="${APP_DIR}/Contents/MacOS"
RES_DIR="${APP_DIR}/Contents/Resources"

echo "==> swift build -c ${CONFIG}"
swift build -c "${CONFIG}"

BIN_PATH="${BUILD_DIR}/${APP_NAME}"
if [[ ! -x "${BIN_PATH}" ]]; then
  # SwiftPM may place the product under an arch-specific path; find it.
  BIN_PATH="$(swift build -c "${CONFIG}" --show-bin-path)/${APP_NAME}"
fi
if [[ ! -x "${BIN_PATH}" ]]; then
  echo "!! built binary not found at ${BIN_PATH}" >&2
  exit 1
fi
echo "==> built binary: ${BIN_PATH}"

echo "==> assembling ${APP_NAME}.app"
rm -rf "${APP_DIR}"
mkdir -p "${MACOS_DIR}" "${RES_DIR}"
cp "${BIN_PATH}" "${MACOS_DIR}/${APP_NAME}"

cat > "${APP_DIR}/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleName</key>            <string>${APP_NAME}</string>
    <key>CFBundleDisplayName</key>     <string>ScoreGoals</string>
    <key>CFBundleExecutable</key>      <string>${APP_NAME}</string>
    <key>CFBundleIdentifier</key>      <string>${BUNDLE_ID}</string>
    <key>CFBundlePackageType</key>     <string>APPL</string>
    <key>CFBundleShortVersionString</key> <string>${VERSION}</string>
    <key>CFBundleVersion</key>         <string>${VERSION}</string>
    <key>CFBundleInfoDictionaryVersion</key> <string>6.0</string>
    <key>LSMinimumSystemVersion</key>  <string>14.0</string>
    <key>NSHighResolutionCapable</key> <true/>
    <!-- Menu bar accessory app: no Dock icon, no app menu. -->
    <key>LSUIElement</key>             <true/>
</dict>
</plist>
PLIST

echo "==> writing PkgInfo"
printf 'APPL????' > "${APP_DIR}/Contents/PkgInfo"

echo "==> ad-hoc codesign"
codesign --force --sign - --timestamp=none "${APP_DIR}"
codesign --verify --verbose "${APP_DIR}" || true

echo
echo "======================================================================"
echo " Built: ${APP_DIR}"
echo
echo " Run it:"
echo "   open \"${APP_DIR}\""
echo
echo " Or run the binary directly (logs to stderr):"
echo "   \"${MACOS_DIR}/${APP_NAME}\""
echo
echo " Debug (log every engine call to a file):"
echo "   SCOREGOALS_BAR_DEBUG=/tmp/scoregoalsbar.log \"${MACOS_DIR}/${APP_NAME}\""
echo
echo " Look for the gauge + score in your menu bar (top-right)."
echo "======================================================================"
