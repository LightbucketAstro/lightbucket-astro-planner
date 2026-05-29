# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec for Lightbucket Astro Planner (script: AstroPlanner.py).

Project layout assumed:
    ~/AstroPlannerDev/
        AstroPlanner.py               <- main script
	sharpless_catalog.csv         <- included sharpless catalog
        logo.png                      <- runtime header logo
        logo.ico                      <- Windows window/app icon
        logo.icns                     <- macOS .app bundle icon
        InstallerBuild/
            AstroPlanner.spec    <- THIS FILE
            (PyInstaller writes build/ and dist/ here)

Build command (run from inside InstallerBuild/):
    pyinstaller AstroPlanner.spec --clean --noconfirm
"""

import sys
from pathlib import Path

# SPECPATH is auto-defined by PyInstaller as the directory containing this
# spec file.  The main script and assets live one level up.
PROJECT_DIR = Path(SPECPATH).parent
SCRIPT      = str(PROJECT_DIR / "AstroPlanner.py")
LOGO_PNG    = PROJECT_DIR / "logo.png"
LOGO_ICO    = PROJECT_DIR / "logo.ico"
LOGO_ICNS   = PROJECT_DIR / "logo.icns"
SHARPLESS_CSV = PROJECT_DIR / "sharpless_catalog.csv"

# Files to bundle inside the frozen app.  logo.png is loaded at runtime via
# _resource_path() and must always be included.  logo.ico is bundled on
# Windows so root.iconbitmap() can find it; macOS uses the .icns at the
# bundle level instead.  sharpless_catalog.csv is the bundled 313-entry
# Sharpless catalog, also loaded via _resource_path() at startup.
datas = []
if LOGO_PNG.exists():
    datas.append((str(LOGO_PNG), "."))
if sys.platform == "win32" and LOGO_ICO.exists():
    datas.append((str(LOGO_ICO), "."))
if SHARPLESS_CSV.exists():
    datas.append((str(SHARPLESS_CSV), "."))

# Choose the executable icon for the current platform.
if sys.platform == "win32" and LOGO_ICO.exists():
    APP_ICON = str(LOGO_ICO)
elif sys.platform == "darwin" and LOGO_ICNS.exists():
    APP_ICON = str(LOGO_ICNS)
else:
    APP_ICON = None

block_cipher = None

a = Analysis(
    [SCRIPT],
    pathex=[str(PROJECT_DIR)],
    binaries=[],
    datas=datas,
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="AstroPlanner",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,                 # GUI app — no console window on Windows
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=APP_ICON,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="AstroPlanner",
)

# macOS only: wrap the collected output in a proper .app bundle.  The
# bundle name is the product name (what users see in Finder); the inner
# launcher binary keeps the AstroPlanner name from EXE() above.
if sys.platform == "darwin":
    app = BUNDLE(
        coll,
        name="Lightbucket Astro Planner.app",
        icon=str(LOGO_ICNS) if LOGO_ICNS.exists() else None,
        bundle_identifier="com.lightbucketastro.planner",
        info_plist={
            "CFBundleName":              "Lightbucket Astro Planner",
            "CFBundleDisplayName":       "Lightbucket Astro Planner",
            "CFBundleShortVersionString": "1.0.2",
            "CFBundleVersion":           "1.0.2",
            "NSHighResolutionCapable":   True,
        },
    )
