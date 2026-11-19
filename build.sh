#!/bin/bash
# Build the macOS .app, a portable zip, and a drag-to-Applications DMG.
# PyInstaller cannot cross-compile. Run this on the architecture being packaged.
set -euo pipefail
cd "$(dirname "$0")"
root="$(pwd)"
if [[ "$(uname -s)" != Darwin ]]; then
  echo "Run build.sh on macOS. Windows packages are built with build.ps1." >&2
  exit 1
fi

py=""
for candidate in python3 python3.11 python3.12 python; do
  if command -v "$candidate" >/dev/null 2>&1 && "$candidate" -c "import tkinter" >/dev/null 2>&1; then
    py="$candidate"
    break
  fi
done
if [[ -z "$py" ]]; then
  echo "Need a Python that includes tkinter. Install Homebrew python@3.11, or the python.org macOS installer." >&2
  exit 1
fi

"$py" -m pip install --disable-pip-version-check -q -r "$root/requirements.txt" "pyinstaller>=6,<7"

name="Tripo-Echo3D-Bridge"
arch="$(uname -m)"
app="$root/dist/$name.app"
archive="$root/dist/${name}-macOS-${arch}.zip"
dmg="$root/dist/${name}-macOS-${arch}.dmg"
dmg_root="$root/build/${name}-dmg-${arch}"
dmg_rw="$root/build/${name}-macOS-${arch}-rw.dmg"
volume_name="Tripo Echo3D Bridge"
mount_point="/Volumes/$volume_name"
background="$root/build/macos-assets/installer-background.png"
"$py" "$root/tools/generate_macos_assets.py" --output "$background"

iconset="$root/build/bridge.iconset"
rm -rf "$iconset"
mkdir -p "$iconset"
make_icon() {
  sips -z "$1" "$1" "$root/assets/logo.png" --out "$iconset/$2" >/dev/null
}
make_icon 16 icon_16x16.png
make_icon 32 icon_16x16@2x.png
make_icon 32 icon_32x32.png
make_icon 64 icon_32x32@2x.png
make_icon 128 icon_128x128.png
make_icon 256 icon_128x128@2x.png
make_icon 256 icon_256x256.png
make_icon 512 icon_256x256@2x.png
make_icon 512 icon_512x512.png
make_icon 1024 icon_512x512@2x.png
iconutil -c icns "$iconset" -o "$root/build/bridge.icns"

"$py" -m PyInstaller --noconfirm --clean --windowed --onedir --collect-all customtkinter \
  --icon "$root/build/bridge.icns" \
  --osx-bundle-identifier ai.tripo.echo3d.bridge \
  --add-data "$root/assets:assets" \
  --add-data "$root/web:web" \
  --name "$name" \
  --paths "$root" \
  --distpath "$root/dist" \
  --workpath "$root/build" \
  --specpath "$root" \
  "$root/desktop_main.py"

codesign --force --deep --sign - "$app"
codesign --verify --deep --strict "$app"
rm -f "$archive"
ditto -c -k --sequesterRsrc --keepParent "$app" "$archive"

if [[ "${TRIPO_BUILD_DMG:-1}" != "1" ]]; then
  echo "Built $app"
  echo "Packaged $archive"
  shasum -a 256 "$archive"
  exit 0
fi

rm -rf "$dmg_root"
mkdir -p "$dmg_root"
ditto "$app" "$dmg_root/$name.app"
ln -s /Applications "$dmg_root/Applications"
mkdir -p "$dmg_root/.background"
ditto "$background" "$dmg_root/.background/installer-background.png"
rm -f "$dmg" "$dmg_rw"
if [[ -e "$mount_point" ]]; then
  echo "DMG mount point is already occupied: $mount_point" >&2
  exit 1
fi

mounted=0
detach_dmg() {
  local attempt
  for attempt in 1 2 3 4 5; do
    if [[ ! -e "$mount_point" ]]; then
      return 0
    fi
    if hdiutil detach "$mount_point" >/dev/null 2>&1; then
      return 0
    fi
    sleep 2
  done
  if [[ -e "$mount_point" ]]; then
    hdiutil detach "$mount_point" -force >/dev/null
  fi
}
cleanup_dmg() {
  if [[ "$mounted" -eq 1 ]]; then
    detach_dmg >/dev/null 2>&1 || true
  fi
}
trap cleanup_dmg EXIT

hdiutil create \
  -volname "$volume_name" \
  -srcfolder "$dmg_root" \
  -ov \
  -format UDRW \
  "$dmg_rw" >/dev/null
hdiutil attach "$dmg_rw" \
  -mountpoint "$mount_point" \
  -readwrite \
  -noverify \
  -noautoopen >/dev/null
mounted=1
open "$mount_point"
sleep 2

osascript <<APPLESCRIPT
tell application "Finder"
    tell disk "$volume_name"
        open
        set current view of container window to icon view
        set toolbar visible of container window to false
        set statusbar visible of container window to false
        set pathbar visible of container window to false
        set the bounds of container window to {120, 120, 840, 560}
        set theViewOptions to the icon view options of container window
        set arrangement of theViewOptions to not arranged
        set icon size of theViewOptions to 112
        set label position of theViewOptions to bottom
        set background color of theViewOptions to {0, 0, 0}
        set background picture of theViewOptions to file ".background:installer-background.png"
        set position of item "$name.app" of container window to {190, 255}
        set position of item "Applications" of container window to {530, 255}
        update without registering applications
        delay 3
        close
    end tell
end tell
APPLESCRIPT

if [[ ! -f "$mount_point/.DS_Store" ]]; then
  echo "Finder did not persist the branded DMG layout." >&2
  exit 1
fi
chflags hidden "$mount_point/.background"
sync
detach_dmg
mounted=0
trap - EXIT
hdiutil convert "$dmg_rw" \
  -format UDZO \
  -imagekey zlib-level=9 \
  -o "$dmg" >/dev/null
rm -f "$dmg_rw"
hdiutil verify "$dmg" >/dev/null

echo "Built $app"
echo "Packaged $archive"
echo "Packaged $dmg"
shasum -a 256 "$archive"
shasum -a 256 "$dmg"
