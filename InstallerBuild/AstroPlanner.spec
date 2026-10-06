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
        skymap/                       <- interactive sky-map assets (build fails if missing)
            skymap.html, skymap.js, vendor/, data/
        InstallerBuild/
            AstroPlanner.spec    <- THIS FILE
            (PyInstaller writes build/ and dist/ here)

Build command (run from inside InstallerBuild/):
    pyinstaller AstroPlanner.spec --clean --noconfirm

The app version (macOS bundle version below) is read from __version__ in
AstroPlanner.py, so bumping that one line is all a release needs.

The build environment must have pywebview installed:
    pip install pywebview
On Windows that also pulls pythonnet / clr_loader (the WebView2 bridge);
on macOS it uses the built-in WKWebView via pyobjc.
"""

import re
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

# ---- App version ------------------------------------------------------------
# Single source of truth: __version__ in AstroPlanner.py (also shown in the
# title bar and used by the update checker).  Read as text — the script is
# not imported, so building needs none of its runtime dependencies here.
_m = re.search(r"^__version__\s*=\s*[\"']([^\"']+)[\"']",
               Path(SCRIPT).read_text(encoding="utf-8"), re.MULTILINE)
if not _m:
    raise SystemExit("\n[AstroPlanner.spec] Couldn't find __version__ in " + SCRIPT + "\n")
APP_VERSION = _m.group(1)
print(f"[AstroPlanner.spec] Building Lightbucket Astro Planner {APP_VERSION}")

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
#
# The sky map is a shipped feature, so a missing or incomplete skymap/ folder
# stops the build here rather than producing a release that only fails at
# runtime with "Sky Map Assets Missing".  data/ holds the bundled Lean tier
# (Extended/Full are downloaded at runtime into the user data dir).
SKYMAP_REQUIRED = [
    "skymap.html",
    "skymap.js",
    "vendor/d3.min.js",
    "vendor/d3.geo.projection.min.js",
    "vendor/celestial.min.js",
    "data/stars.6.json",
    "data/messier.json",
    "data/starnames.json",
    "data/dsonames.json",
    "data/constellations.json",
    "data/constellations.lines.json",
    "data/constellations.bounds.json",
    "data/mw.json",
]
_missing = [f for f in SKYMAP_REQUIRED if not (SKYMAP_DIR / f).is_file()]
if _missing:
    raise SystemExit(
        "\n[AstroPlanner.spec] Sky-map assets missing under "
        + str(SKYMAP_DIR) + ":\n    " + "\n    ".join(_missing)
        + "\nRestore them before building (see the layout at the top of this file).\n")

# Keep OS/editor litter and scratch files out of the release — the Tree
# otherwise copies everything in the folder.  Patterns match file and
# directory names at any depth.
SKYMAP_EXCLUDES = [
    ".DS_Store", "Thumbs.db", "desktop.ini",     # OS metadata
    "*.bak", "*.orig", "*.tmp", "*.swp", "*~",    # editor / backup copies
    ".git", "__pycache__", "*.map",               # VCS, caches, source maps
]
skymap_tree = Tree(str(SKYMAP_DIR), prefix="skymap", excludes=SKYMAP_EXCLUDES)

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
            "CFBundleShortVersionString": APP_VERSION,
            "CFBundleVersion":           APP_VERSION,
            "NSHighResolutionCapable":   True,
        },
    )
