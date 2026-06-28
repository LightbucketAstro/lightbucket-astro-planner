#!/bin/bash
#
# MakeDmgFile.sh - Build a distribution DMG for Lightbucket Astro Planner.
#
# Run from inside ~/AstroPlannerDev/InstallerBuild/ AFTER PyInstaller
# has produced the .app bundle:
#
#     ./MakeDmgFile.sh
#
# Requires create-dmg.  Install once with Homebrew:
#
#     brew install create-dmg
#
# Output:
#     installer/LightbucketAstroPlanner-1.1.2.dmg
#

set -e

APP_NAME="Lightbucket Astro Planner"
APP_VERSION="1.1.2"
APP_PATH="dist/${APP_NAME}.app"
OUTPUT_DIR="installer"
DMG_NAME="LightbucketAstroPlanner-${APP_VERSION}.dmg"
DMG_PATH="${OUTPUT_DIR}/${DMG_NAME}"
VOLICON="../logo.icns"

# --- Sanity checks ----------------------------------------------------------

if [ ! -d "${APP_PATH}" ]; then
    echo "ERROR: ${APP_PATH} not found."
    echo "Build it first with:"
    echo "    pyinstaller AstroPlanner.spec --clean --noconfirm"
    exit 1
fi

if ! command -v create-dmg >/dev/null 2>&1; then
    echo "ERROR: create-dmg is not installed."
    echo "Install with:  brew install create-dmg"
    exit 1
fi

# --- Build ------------------------------------------------------------------

mkdir -p "${OUTPUT_DIR}"
rm -f "${DMG_PATH}"

# Optional volume icon (mounted-DMG icon in Finder).  Pass it only if the
# .icns exists, otherwise create-dmg will error.
VOLICON_ARG=()
if [ -f "${VOLICON}" ]; then
    VOLICON_ARG=(--volicon "${VOLICON}")
fi

create-dmg \
    --volname        "${APP_NAME}" \
    "${VOLICON_ARG[@]}" \
    --window-pos     200 120 \
    --window-size    600 400 \
    --icon-size      128 \
    --icon           "${APP_NAME}.app" 150 200 \
    --hide-extension "${APP_NAME}.app" \
    --app-drop-link  450 200 \
    --no-internet-enable \
    "${DMG_PATH}" \
    "${APP_PATH}"

echo ""
echo "Created: ${DMG_PATH}"
ls -lh "${DMG_PATH}"
