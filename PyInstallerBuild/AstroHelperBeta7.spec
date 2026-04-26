# -*- mode: python ; coding: utf-8 -*-
#
# PyInstaller spec for AstroApp — Lightbucket Astro Helper Pro.
#
# Build commands (run in the directory containing this file):
#
#   macOS  : pyinstaller AstroApp.spec
#   Windows: pyinstaller AstroApp.spec
#
# Outputs (in ./dist):
#
#   macOS   -> AstroApp.app         (double-clickable bundle)
#   Windows -> AstroApp\AstroApp.exe (one-folder layout, ships the whole folder)
#
# This spec defaults to one-folder mode on both platforms. One-folder is the
# PyInstaller-recommended default — it's faster to launch, easier to debug
# when something goes wrong, and lets you ship only the changed .exe/.app
# when the rest of the bundle hasn't changed. To switch to one-file mode
# (single self-extracting executable), see the notes in the EXE() block.
#
# Expected layout next to this spec file:
#
#   AstroApp.spec
#   AstroApp-Beta4_5.py     <-- main script (rename in SCRIPT below if yours differs)
#   logo.png                <-- cross-platform taskbar icon (required by app)
#   logo.ico                <-- Windows window icon + .exe icon
#   logo.icns               <-- macOS .app icon
#   ngc_catalog.csv         <-- OPTIONAL seed catalog (app will download if absent)
#
# All "logo.*" files are bundled so the app's _resource_path() helper finds
# them via sys._MEIPASS at runtime. The catalog is intentionally NOT bundled
# here — the app downloads it on first launch into the user's data folder
# (~/Astroapp on macOS, %LOCALAPPDATA%\Astroapp on Windows), and bundling
# a stale copy would prevent updates.

import sys
from pathlib import Path

# ── Config ─────────────────────────────────────────────────────────────
# Change these if you rename the script or app.
SCRIPT   = 'AstroHelperBeta7.py'
APP_NAME = 'LightbucketAstroPlanner'

# Macs: .app bundle gets an .icns. Windows: .exe gets an .ico.
ICON_MAC = 'logo.icns'
ICON_WIN = 'logo.ico'

# Files bundled INSIDE the app (accessed at runtime via _resource_path).
# Tuple form is (source_path, dest_path_in_bundle). "." puts the file at
# the bundle root, which is what _resource_path() expects.
BUNDLED_DATA = [
    ('logo.png', '.'),
    ('logo.ico', '.'),
    ('logo.icns', '.'),
]

# Filter out any bundled files that aren't actually present on disk — lets
# the spec work even if, say, logo.icns is missing during a Windows build.
BUNDLED_DATA = [(src, dst) for (src, dst) in BUNDLED_DATA if Path(src).exists()]

# Per-platform icon selection for Analysis/EXE/BUNDLE.
if sys.platform == 'darwin':
    ICON = ICON_MAC if Path(ICON_MAC).exists() else None
elif sys.platform == 'win32':
    ICON = ICON_WIN if Path(ICON_WIN).exists() else None
else:
    ICON = None   # Linux — no icon file needed for the executable itself.


# ── Analysis ───────────────────────────────────────────────────────────
# PyInstaller walks imports from SCRIPT. AstroApp uses only stdlib plus
# PIL (Pillow), both of which PyInstaller detects automatically — there
# are no hidden imports to declare. If you later add a library that uses
# plugins or dynamic imports (astropy, scipy, etc.), list their modules
# in hiddenimports below.

a = Analysis(
    [SCRIPT],
    pathex=[],
    binaries=[],
    datas=BUNDLED_DATA,
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # These bloat the bundle and AstroApp never uses them. Exclude to
        # trim ~40-60 MB from the output. If you add features that pull
        # these in, remove the relevant lines.
        'numpy',
        'scipy',
        'pandas',
        'matplotlib',
        'pytest',
        'IPython',
        'notebook',
    ],
    noarchive=False,
)

pyz = PYZ(a.pure)


# ── EXE ────────────────────────────────────────────────────────────────
# One-folder mode (default): EXE references a.binaries/a.zipfiles/a.datas
# through the COLLECT() call below, which assembles the final dist folder.
#
# One-file mode (single self-extracting exe): remove the COLLECT() call
# and change this block so EXE() receives a.binaries/a.zipfiles/a.datas
# directly, with the `exclude_binaries=False` argument. One-file mode
# produces a slower-launching but more portable single file. Documented
# in the PyInstaller manual under "Bundling to One Folder" vs "One File".

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,          # one-folder mode
    name=APP_NAME,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,                      # UPX is optional; off by default for
                                    # cleaner antivirus behaviour on Windows.
    console=False,                  # windowed app — no console window.
                                    # Crash handler in AstroApp routes
                                    # uncaught exceptions to a log file.
    disable_windowed_traceback=False,
    argv_emulation=False,           # True only if you handle "Open With"
                                    # events via AppleEvents. AstroApp
                                    # doesn't, so leave False.
    target_arch=None,               # None = build for the current arch.
                                    # Set to 'universal2' on an M-series
                                    # Mac to ship a universal binary that
                                    # runs natively on Intel + Apple
                                    # Silicon (requires universal2 Python).
    codesign_identity=None,         # Fill in if/when you get an Apple
                                    # Developer ID cert. Leaving it None
                                    # produces an unsigned .app that
                                    # triggers Gatekeeper on first launch.
    entitlements_file=None,
    icon=ICON,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name=APP_NAME,
)


# ── macOS .app bundle ──────────────────────────────────────────────────
# On macOS, PyInstaller additionally wraps the COLLECT output into an
# .app bundle when the BUNDLE() constructor is used. This block is
# ignored on Windows / Linux.

if sys.platform == 'darwin':
    app = BUNDLE(
        coll,
        name=f'{APP_NAME}.app',
        icon=ICON,
        bundle_identifier='com.lightbucket.astroapp',
        info_plist={
            # Semantic version shown in Finder -> Get Info.
            'CFBundleShortVersionString': '0.4.5',
            'CFBundleVersion': '0.4.5',
            # Retina / HiDPI support on macOS.
            'NSHighResolutionCapable': True,
            # Copyright line shown in the app's About dialog.
            'NSHumanReadableCopyright': '© Jerry / Lightbucket Astro',
        },
    )
