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
        skymap/                       <- interactive sky-map assets  (NEW)
            skymap.html, skymap.js, vendor/, data/
        InstallerBuild/
            AstroPlanner.spec    <- THIS FILE
            (PyInstaller writes build/ and dist/ here)

Build command (run from inside InstallerBuild/):
    pyinstaller AstroPlanner.spec --clean --noconfirm

The build environment must have pywebview installed:
    pip install pywebview
On Windows that also pulls pythonnet / clr_loader (the WebView2 bridge);
on macOS it uses the built-in WKWebView via pyobjc.
"""

import sys
from pathlib import Path
from PyInstaller.utils.hooks import collect_all

# SPECPATH is auto-defined by PyInstaller as the directory containing this
# spec file.  The main script and assets live one level up.
PROJECT_DIR = Path(SPECPATH).parent
SCRIPT      = str(PROJECT_DIR / "AstroPlanner.py")
LOGO_PNG    = PROJECT_DIR / "logo.png"
LOGO_ICO    = PROJECT_DIR / "logo.ico"
LOGO_ICNS   = PROJECT_DIR / "logo.icns"
SHARPLESS_CSV = PROJECT_DIR / "sharpless_catalog.csv"
SKYMAP_DIR  = PROJECT_DIR / "skymap"

# ---- Data files bundled inside the frozen app -----------------------------
# logo.png is loaded at runtime via _resource_path() and must always be
# included.  logo.ico is bundled on Windows so root.iconbitmap() can find it;
# macOS uses the .icns at the bundle level instead.  sharpless_catalog.csv is
# the bundled 313-entry Sharpless catalog, also loaded via _resource_path().
datas = []
if LOGO_PNG.exists():
    datas.append((str(LOGO_PNG), "."))
if sys.platform == "win32" and LOGO_ICO.exists():
    datas.append((str(LOGO_ICO), "."))
if SHARPLESS_CSV.exists():
    datas.append((str(SHARPLESS_CSV), "."))

binaries = []
hiddenimports = ["webview"]

# ---- pywebview / WebView2 backend -----------------------------------------
# pywebview's renderer modules and (on Windows) the pythonnet/clr bridge plus
# the WebView2 interop DLLs are loaded dynamically, so PyInstaller misses them
# without help.  collect_all() pulls webview's data files, binaries and
# submodules; the platform backend + clr bridge are added explicitly below.
#
# NOTE: the main Tkinter app never imports webview — only the sky-map child
# process does, inside a try/except.  So if any of this bundling is
# incomplete, the worst case is the sky map opening in the default browser
# instead of an embedded window — never a crash.
try:
    wv_datas, wv_binaries, wv_hidden = collect_all("webview")
    datas += wv_datas
    binaries += wv_binaries
    hiddenimports += wv_hidden
except Exception:
    pass

if sys.platform == "win32":
    hiddenimports += [
        "webview.platforms.edgechromium",
        "webview.platforms.winforms",
        "clr_loader",
        "pythonnet",
    ]
    # pythonnet/clr_loader are notoriously fiddly to freeze; pull everything.
    for _pkg in ("clr_loader", "pythonnet"):
        try:
            _d, _b, _h = collect_all(_pkg)
            datas += _d
            binaries += _b
            hiddenimports += _h
        except Exception:
            pass
elif sys.platform == "darwin":
    hiddenimports += ["webview.platforms.cocoa"]

# Trim GUI backends pywebview would otherwise drag in if they happen to be
# installed in the build environment (we use EdgeChromium on Windows and
# Cocoa/WKWebView on macOS).
excludes = ["PyQt5", "PyQt6", "PySide2", "PySide6", "gi", "cefpython3"]

# ---- Sky-map asset tree ---------------------------------------------------
# Bundle skymap/ (skymap.html, skymap.js, vendor/, data/) under "skymap" so
# the app's _resource_path("skymap") resolves it at runtime; it is served over
# localhost to the embedded (or fallback browser) viewer.
skymap_tree = Tree(str(SKYMAP_DIR), prefix="skymap") if SKYMAP_DIR.exists() else []

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
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
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
    skymap_tree,                   # bundle skymap/ alongside the rest
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
            "CFBundleShortVersionString": "1.2.0",
            "CFBundleVersion":           "1.2.0",
            "NSHighResolutionCapable":   True,
        },
    )
