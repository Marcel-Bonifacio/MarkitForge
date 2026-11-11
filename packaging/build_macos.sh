#!/usr/bin/env bash
# SPDX-FileCopyrightText: 2026-present MarkitForge contributors
#
# SPDX-License-Identifier: Apache-2.0
#
# Build MarkitForge.app and MarkitForge-<version>-macos-<arch>.dmg.
#
#   packaging/build_macos.sh [OUT_DIR]
#
# Run with the build environment's Python first on PATH (or set PYTHON).
# OUT_DIR defaults to packaging/build; dist/ and work/ are created inside it.
# The app is signed ad hoc only ("codesign -s -"); see packaging/README.md for
# Developer ID signing and notarization.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OUT="${1:-$HERE/build}"
PYTHON="${PYTHON:-python3}"
DIST="$OUT/dist"
WORK="$OUT/work"

mkdir -p "$OUT"
"$PYTHON" "$HERE/make_icons.py" --out "$WORK/icons"
LICENSES="$OUT/THIRD_PARTY_LICENSES.txt"
"$PYTHON" "$HERE/collect_licenses.py" --out "$LICENSES"
MARKITFORGE_ICON_DIR="$WORK/icons" MARKITFORGE_LICENSES_FILE="$LICENSES" \
  "$PYTHON" -m PyInstaller --noconfirm --clean \
  --distpath "$DIST" --workpath "$WORK" "$HERE/markitforge_gui.spec"

APP="$DIST/MarkitForge.app"
VERSION="$(/usr/libexec/PlistBuddy -c 'Print :CFBundleShortVersionString' "$APP/Contents/Info.plist")"
ARCH="$(uname -m)"

# Ad hoc signature over the whole bundle (PyInstaller signs the pieces; this
# seals the bundle so Gatekeeper sees one consistent signature).
codesign --force --deep --sign - --timestamp=none "$APP"
codesign --verify --deep --strict "$APP"

STAGE="$WORK/dmg"
rm -rf "$STAGE"
mkdir -p "$STAGE"
cp -R "$APP" "$STAGE/"
cp "$LICENSES" "$STAGE/"
ln -s /Applications "$STAGE/Applications"
DMG="$OUT/MarkitForge-$VERSION-macos-$ARCH.dmg"
rm -f "$DMG"
# hdiutil sometimes fails with "Resource busy" on CI machines: retry.
for attempt in 1 2 3; do
  if hdiutil create -volname "MarkitForge" -srcfolder "$STAGE" -fs HFS+ \
    -format UDZO -imagekey zlib-level=9 -ov "$DMG" >/dev/null; then
    break
  fi
  if [ "$attempt" = 3 ]; then
    echo "hdiutil create failed" >&2
    exit 1
  fi
  sleep 5
done
rm -rf "$STAGE"

echo "App: $APP ($(du -sh "$APP" | cut -f1))"
echo "DMG: $DMG ($(du -h "$DMG" | cut -f1))"
