#!/usr/bin/env bash
# Build "Database Tool.app" and wrap it in a .dmg.
# Run this ON A MAC â€” PyInstaller can't cross-build a Mac app from Windows/Linux.
set -e

APP_NAME="Database Tool"
ICON_ICNS="assets/icon.icns"
PYTHON="${PYTHON:-python3}"   # override with e.g. PYTHON=python3.12 ./build_macos.sh

echo "== Building '$APP_NAME' for macOS (using $PYTHON) =="

# 1. Sanity checks
"$PYTHON" -c "import tkinter" 2>/dev/null || {
    echo "ERROR: tkinter not found for $PYTHON."
    echo "Install the python.org build (it bundles Tk), or: brew install python-tk"
    echo "Then re-run as: PYTHON=python3.12 ./build_macos.sh (matching your Tk-enabled python)"
    exit 1
}
"$PYTHON" -m pip show pyinstaller >/dev/null 2>&1 || "$PYTHON" -m pip install pyinstaller

# 2. Clean old build artifacts
rm -rf build dist "${APP_NAME}.spec"

# 3. Build the .app bundle
#    (No --onefile here: on macOS a plain --windowed build already produces
#    a single self-contained .app, and --onefile just adds slow unpack-on-
#    launch overhead for no benefit.)
"$PYTHON" -m PyInstaller --windowed --name "$APP_NAME" \
    --icon "$ICON_ICNS" \
    --add-data "assets:assets" \
    main.py

# 4. Wrap dist/"$APP_NAME.app" into a .dmg
DMG_NAME="${APP_NAME// /_}.dmg"
rm -f "dist/$DMG_NAME"

if command -v create-dmg >/dev/null 2>&1; then
    # Nicer dmg with a drag-to-Applications layout: brew install create-dmg
    create-dmg \
      --volname "$APP_NAME" \
      --window-size 500 320 \
      --icon-size 100 \
      --icon "${APP_NAME}.app" 120 150 \
      --app-drop-link 380 150 \
      "dist/$DMG_NAME" \
      "dist/${APP_NAME}.app"
else
    echo "(Tip: 'brew install create-dmg' gives a nicer drag-to-Applications .dmg."
    echo " Falling back to a plain hdiutil dmg for now.)"
    hdiutil create -volname "$APP_NAME" -srcfolder "dist/${APP_NAME}.app" \
        -ov -format UDZO "dist/$DMG_NAME"
fi

echo ""
echo "Done: dist/$DMG_NAME"

