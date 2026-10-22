#!/bin/bash
# Builds YourAnalyst.app (Dock app) from app.js + the logo, and installs it in /Applications.
#   desktop/build_app.sh            build and install
#   desktop/build_app.sh --no-install   build into desktop/build only
set -euo pipefail
cd "$(dirname "$0")"
LOGO="../../your analyst dashboard içi logo.png"   # the blue X mark, same as the dashboard header
OUT=build/YourAnalyst.app

rm -rf build && mkdir -p build/icon.iconset
[ -f icon_1024.png ] || osascript -l JavaScript make_icon.js "$PWD/$LOGO" "$PWD/icon_1024.png" 1024 markicon
for s in 16 32 128 256 512; do
  sips -z $s $s icon_1024.png --out build/icon.iconset/icon_${s}x${s}.png >/dev/null
  sips -z $((s*2)) $((s*2)) icon_1024.png --out build/icon.iconset/icon_${s}x${s}@2x.png >/dev/null
done
iconutil -c icns build/icon.iconset -o build/YourAnalyst.icns

osacompile -l JavaScript -s -o "$OUT" app.js
cp build/YourAnalyst.icns "$OUT/Contents/Resources/applet.icns"
rm -f "$OUT/Contents/Resources/Assets.car"   # newer osacompile ships its icon here; drop it so applet.icns wins

P="$OUT/Contents/Info.plist"
plutil -replace CFBundleName -string YourAnalyst "$P"
plutil -replace CFBundleIdentifier -string com.canoktay.youranalyst "$P"
plutil -replace CFBundleIconFile -string applet "$P"
plutil -remove CFBundleIconName "$P" 2>/dev/null || true
plutil -replace NSHighResolutionCapable -bool true "$P"
plutil -replace NSAppTransportSecurity -json '{"NSAllowsLocalNetworking":true}' "$P"
codesign --force --deep -s - "$OUT"

if [ "${1:-}" != "--no-install" ]; then
  rm -rf /Applications/YourAnalyst.app
  cp -R "$OUT" /Applications/
  touch /Applications/YourAnalyst.app
  echo "Installed /Applications/YourAnalyst.app"
fi
