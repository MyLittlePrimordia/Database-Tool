#!/usr/bin/env bash
# Build a portable "Database_Tool.AppImage".
# Run this ON LINUX (real machine, VM, or CI) â€” PyInstaller can't
# cross-build a Linux binary from Windows/Mac.
set -e

BIN_NAME="Database_Tool"          # internal binary/AppDir name, no spaces
DISPLAY_NAME="Database Tool"      # human-readable name
ICON_PNG="assets/icon.png"

echo "== Building '$DISPLAY_NAME' AppImage for Linux =="

# 1. Sanity checks
python3 -c "import tkinter" 2>/dev/null || {
    echo "ERROR: tkinter not found for this Python."
    echo "Install it first, e.g.: sudo apt install python3-tk"
    exit 1
}
python3 -m pip show pyinstaller >/dev/null 2>&1 || python3 -m pip install pyinstaller

# 2. Clean old build artifacts
rm -rf build dist "${BIN_NAME}.spec" AppDir

# 3. Build a onefile Linux binary
python3 -m PyInstaller --onefile --windowed --name "$BIN_NAME" \
    --add-data "assets:assets" \
    main.py

# 4. Assemble the AppDir that appimagetool expects
mkdir -p AppDir/usr/bin
cp "dist/$BIN_NAME" AppDir/usr/bin/
cp -r assets AppDir/usr/bin/assets
cp "$ICON_PNG" "AppDir/${BIN_NAME}.png"

cat > AppDir/AppRun <<EOF
#!/bin/sh
HERE="\$(dirname "\$(readlink -f "\$0")")"
exec "\$HERE/usr/bin/$BIN_NAME" "\$@"
EOF
chmod +x AppDir/AppRun

cat > "AppDir/${BIN_NAME}.desktop" <<EOF
[Desktop Entry]
Name=$DISPLAY_NAME
Exec=$BIN_NAME
Icon=$BIN_NAME
Type=Application
Categories=Utility;
EOF

# 5. Fetch appimagetool if it's not already sitting next to this script
if [ ! -f appimagetool.AppImage ]; then
    echo "Downloading appimagetool..."
    curl -L -o appimagetool.AppImage \
      https://github.com/AppImage/appimagetool/releases/download/continuous/appimagetool-x86_64.AppImage
    chmod +x appimagetool.AppImage
fi

# 6. Build the AppImage
#    --appimage-extract-and-run avoids needing a working FUSE mount to
#    run appimagetool itself (appimagetool ships as an AppImage too) â€”
#    needed on most CI runners (e.g. GitHub Actions) and some desktop
#    Linux setups where FUSE isn't available/permitted. ARCH must be
#    set explicitly for appimagetool to know what to build.
export ARCH=x86_64
./appimagetool.AppImage --appimage-extract-and-run AppDir "dist/${DISPLAY_NAME// /_}.AppImage"

echo ""
echo "Done: dist/${DISPLAY_NAME// /_}.AppImage"

