#!/bin/bash
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

name="Tripo-Echo3D-Bridge"
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

ditto -c -k --keepParent "$root/dist/$name.app" "$root/dist/$name-mac.zip"
echo "Built $root/dist/$name.app"
echo "Built $root/dist/$name-mac.zip"
