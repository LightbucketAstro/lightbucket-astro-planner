"""
Lightbucket Astro Planner — astrophotography planner.

A Tkinter desktop application for planning deep-sky astrophotography sessions.
Calculates recommended sub-exposure times, tonight's imaging window, target
visibility, framing, and moon/twilight conditions based on the observer's
location, equipment, and sky conditions.  Exports target lists to NINA
(Nighttime Imaging 'N' Astronomy) as .ninaTargetSet files.

Major components
----------------
Module-level astronomy helpers :
    Julian date, alt/az, moon phase & position, twilight, transit, and
    the nightly imaging-window scan.
AstroApp (Tk class) :
    Five-tab UI — Target Planner, Targets List, Tonight's Plan,
    Manage Equipment, Settings — plus sidebar navigation, day/night
    theming, DSS image thumbnail with FOV overlay, and catalog search.

File layout on disk
-------------------
~/LightbucketAstroPlanner/astro_gear.json        — equipment inventory, preferences, last session
~/LightbucketAstroPlanner/ngc_catalog.csv        — NGC/IC catalog (downloaded on first run)
~/LightbucketAstroPlanner/sessions/*.json        — saved session plans
%LOCALAPPDATA%\\LightbucketAstroPlanner\\...       — equivalent paths on Windows
"""

import tkinter as tk
from tkinter import ttk, messagebox, filedialog, simpledialog
import json
import os
import sys
import platform
import math
import csv
import urllib.request
import urllib.error
import subprocess
import re
import webbrowser
import ssl
import threading
import io
import traceback
from datetime import datetime, timezone
from pathlib import Path
from PIL import Image, ImageTk

__version__ = "1.1.1"

# ── Sky-map catalog tiers (optional downloads) ─────────────────────────────
# Pinned to the d3-celestial commit our bundled engine + stars.6 came from, so
# downloaded data matches the renderer.  Files land in the user data dir and
# are served (overriding the bundled lean set) by the local sky-map server.
SKYMAP_CATALOG_PIN = "7e720a3de062059d4c5400a379146a601d9010e0"
SKYMAP_CATALOG_BASE = ("https://raw.githubusercontent.com/ofrohn/d3-celestial/"
                       + SKYMAP_CATALOG_PIN + "/data/")
SKYMAP_TIERS = {
    "lean": {
        "label": "Lean", "blurb": "stars ≤ 6, Messier", "bundled": True,
        "stars": "stars.6.json", "dsos": "messier.json", "maglimit": 6, "files": [],
    },
    "extended": {
        "label": "Extended", "blurb": "stars ≤ 8, DSOs ≤ 6  ·  ~7 MB", "bundled": False,
        "stars": "stars.8.json", "dsos": "dsos.6.json", "maglimit": 8,
        "files": ["stars.8.json", "dsos.6.json", "starnames.json", "dsonames.json"],
    },
    "full": {
        "label": "Full", "blurb": "stars ≤ 14, DSOs ≤ 14  ·  ~19 MB", "bundled": False,
        "stars": "stars.14.json", "dsos": "dsos.14.json", "maglimit": 14,
        "files": ["stars.14.json", "dsos.14.json", "starnames.json", "dsonames.json"],
    },
}


def _resource_path(relative):
    """Return an absolute path to a bundled resource.

    Works in three contexts:
      • Dev / ``python AstroHelperBeta7.py`` — resolves relative to this file.
      • PyInstaller one-file bundle — resolves into ``sys._MEIPASS``, the
        temp folder PyInstaller extracts data files into at launch.
      • PyInstaller one-folder bundle — ``sys._MEIPASS`` is still set and
        points at the bundle's internal folder, so the same code works.

    Use this for any file shipped INSIDE the app (logo, icons, default
    catalog seed, etc.).  Do NOT use it for user data — that still goes
    through ``get_data_path()`` / ``LOCALAPPDATA`` / ``~/LightbucketAstroPlanner``.
    """
    base = getattr(sys, "_MEIPASS", None) or Path(__file__).resolve().parent
    return Path(base) / relative


def _run_skymap_child(url):
    """Sky-map viewer subprocess entry point.

    The main app relaunches itself with ``--skymap-url <url>`` so the
    interactive sky map runs in its own process.  pywebview must own the
    main thread (Cocoa requires it), so a separate process is the only way
    for it to coexist with Tkinter.  If pywebview is unavailable or its
    renderer can't start (e.g. missing WebView2 on Windows), fall back to
    the default browser — the URL is served over localhost either way.
    """
    try:
        import webview
        webview.create_window("Lightbucket Sky Map", url,
                              width=1100, height=750, resizable=True)
        webview.start()
    except Exception:
        # Record why the embedded window couldn't open — a silent fallback is
        # otherwise impossible to diagnose — then open the browser instead.
        try:
            import tempfile, traceback
            logp = os.path.join(tempfile.gettempdir(), "lightbucket_skymap.log")
            with open(logp, "w", encoding="utf-8") as fh:
                fh.write("Embedded sky map (pywebview) could not start; "
                         "fell back to the browser.\n\n")
                fh.write(traceback.format_exc())
        except Exception:
            pass
        try:
            webbrowser.open(url)
        except Exception:
            pass


def _macos_should_force_clam():
    """Return True when the app should switch to the ``clam`` ttk theme on macOS.

    The native macOS ``aqua`` theme draws Entry and Combobox fields using
    AppKit, which ignores ``fieldbackground`` overrides and adapts the field
    color to the OS appearance setting. In dark mode, that cooperates with
    our white foreground — fields go dark, white text reads fine. In light
    mode, the field stays white while our explicit ``foreground=#ffffff``
    setting still applies, producing white-on-white text in every dropdown
    and entry. Switching to ``clam`` (a pure-Python renderer) makes ttk
    honor every color override, fixing the bug at the cost of the native
    aqua look.

    Detection runs ``defaults read -g AppleInterfaceStyle``: on macOS this
    prints ``Dark`` in dark mode and exits with status 1 (key not found) in
    light mode — the standard system signal. Returns True when light mode
    is detected, False when dark mode is confirmed.

    If detection fails (timeout, missing binary, unexpected error) we
    return True and force clam anyway — losing the aqua look in dark mode
    is a smaller cost than leaving the white-on-white bug in place for a
    user whose detection happened to fail.

    Always returns False on non-macOS platforms; callers handle Windows
    and Linux separately.
    """
    if platform.system() != "Darwin":
        return False
    try:
        result = subprocess.run(
            ["defaults", "read", "-g", "AppleInterfaceStyle"],
            capture_output=True, text=True, timeout=1.0,
        )
        # "Dark" in stdout => dark mode (keep aqua). Anything else
        # (exit 1, empty stdout, unexpected value) => treat as light mode.
        return "Dark" not in result.stdout
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        # Detection failed — fall back to forcing clam (safe default).
        return True


# --- SSL FIX ---
# Some corporate networks and a few macOS/Linux Python builds lack an
# up-to-date system CA bundle, which breaks the NGC catalog download.
# Using an unverified context here is a deliberate tradeoff for
# reliability on a read-only public-data fetch.
ssl._create_default_https_context = ssl._create_unverified_context

# --- PLANNING DATE ---
# None means "use today".  Set to a datetime.date to plan for a future night.
_PLANNING_DATE = None  # datetime.date | None

def _planning_local_noon():
    """Return a naive local datetime at noon on the active planning date (or today).
    Used by all astronomical scan functions so they scan from the right night."""
    real_now = datetime.now()
    if _PLANNING_DATE is not None:
        return real_now.replace(year=_PLANNING_DATE.year,
                                month=_PLANNING_DATE.month,
                                day=_PLANNING_DATE.day,
                                hour=12, minute=0, second=0, microsecond=0)
    return real_now


def _utc_offset_hours():
    """Return the local-time UTC offset in fractional hours (e.g. −5.0 for EST).

    Computed from the difference between ``datetime.now()`` (local) and
    ``datetime.now(timezone.utc)`` (UTC).  Used by every function that
    converts a JD timestamp into a local HH:MM string.
    """
    real_now = datetime.now()
    now_utc  = datetime.now(timezone.utc).replace(tzinfo=None)
    return (real_now - now_utc).total_seconds() / 3600.0


def _jd_to_local_hours(jd, utc_offset_h):
    """Convert a Julian Date to local fractional hours (0–24)."""
    return (((jd + 0.5) % 1.0) * 24.0 + utc_offset_h) % 24.0


def _jd_local_noon():
    """Return the Julian Date at local noon on the active planning date.

    All nightly scan functions start their 30-hour sweep from noon so that
    the entire dark window (dusk through dawn) falls within the scan range.
    This helper consolidates the 'back up from _jd_now() to noon' arithmetic
    that was previously duplicated in five places.
    """
    plan = _planning_local_noon()
    elapsed_h = (plan.hour - 12) + plan.minute / 60.0 + plan.second / 3600.0
    return _jd_now() - (elapsed_h % 24.0) / 24.0


# --- TOOLTIP HELPER ---
class ToolTip:
    """Lightweight hover tooltip for any tkinter widget."""
    def __init__(self, widget, text):
        self.widget  = widget
        self.text    = text
        self._win    = None
        widget.bind("<Enter>",  self._show, add="+")
        widget.bind("<Leave>",  self._hide, add="+")
        widget.bind("<Destroy>", self._hide, add="+")

    def _show(self, event=None):
        """Show the tooltip window anchored below the widget."""
        if self._win:
            return
        x = self.widget.winfo_rootx() + 20
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 6
        self._win = tw = tk.Toplevel(self.widget)
        tw.wm_overrideredirect(True)
        tw.wm_geometry(f"+{x}+{y}")
        tk.Label(tw, text=self.text, justify="left",
                 background=DAY_TAB_BG, foreground=DAY_FG,
                 relief="solid", borderwidth=1,
                 font=("Helvetica", 9)).pack(ipadx=6, ipady=3)

    def _hide(self, event=None):
        """Destroy the tooltip window if one is currently shown."""
        if self._win:
            self._win.destroy()
            self._win = None


# ═══════════════════════════════════════════════════════════════════════
# THEME PALETTE
# ═══════════════════════════════════════════════════════════════════════
# Centralising every colour literal here makes palette tweaks a one-line
# change.  DAY_* values match the dark-navy startup theme; NIGHT_* values
# are the red preserve-dark-adaptation overlay.  ACCENT_* colours are
# shared across both themes (they're only used in backgrounds/borders
# that night-mode overrides anyway).
#
# Any new UI code should pull its colours from here rather than hard-code
# hex strings inline.

# Day-mode (default) palette — dark navy
DAY_BG            = "#0e1a28"   # root window background
DAY_PANEL_BG      = "#131f2e"   # sidebar, cards, chip bars
DAY_TAB_BG        = "#1e2d3e"   # tab/header dark navy
DAY_FIELD_BG      = "#263545"   # entry / combobox field
DAY_CARD_INNER    = "#1a2b3c"   # inner card colour for metric tiles
DAY_SEL_BG        = "#3a5a7a"   # selection highlight
DAY_FG            = "#ffffff"   # primary foreground text
DAY_FG_DIM        = "#778899"   # secondary / dimmed text
DAY_FG_MUTED      = "#556677"   # tertiary / muted text
DAY_SEP           = "#2e4a63"   # thin separator / accent-line
DAY_ACCENT_BLUE   = "#38bdf8"   # primary accent (session highlight)
DAY_ACCENT_AMBER  = "#f59e0b"   # queue / warning accent
DAY_ACCENT_GREEN  = "#4caf50"   # good / optimal accent
DAY_ACCENT_RED    = "#ef5350"   # bad / error accent
DAY_ACCENT_PURPLE = "#8b5cf6"   # data-management accent
DAY_ACCENT_CYAN   = "#7eb8d4"   # light steel-blue for section titles
DAY_ACCENT_ICE    = "#aaddff"   # bright highlight for selected circle

# Night-mode palette — red preserve-dark-adaptation theme
NIGHT_BG          = "#1a0000"
NIGHT_FIELD_BG    = "#200000"
NIGHT_PANEL_BG    = "#2a0000"
NIGHT_SEL_BG      = "#330000"
NIGHT_CANVAS_BG   = "#0d0000"
NIGHT_FG          = "#cc0000"
NIGHT_FG_DIM      = "#882222"
NIGHT_FG_MUTED    = "#993300"
NIGHT_ACCENT      = "#cc4400"

# Filter-badge colour map — (background, foreground) per filter name.
# Used in the Tonight's Plan card view to render a small coloured label
# next to each entry's filter.  Case-insensitive lookup plus substring
# matching is handled by _get_filter_color() (see _refresh_plan_tree).
# Hoisted to module level so it isn't rebuilt on every plan refresh.
FILTER_BADGE_COLORS = {
    # Narrowband
    "Ha":               ("#3a2040", "#ce93d8"),
    "H-alpha":          ("#3a2040", "#ce93d8"),
    "Halpha":           ("#3a2040", "#ce93d8"),
    "H-a":              ("#3a2040", "#ce93d8"),
    "O3":               ("#1e2a3a", "#64b5f6"),
    "OIII":             ("#1e2a3a", "#64b5f6"),
    "O-III":            ("#1e2a3a", "#64b5f6"),
    "S2":               ("#2a3520", "#8bc34a"),
    "SII":              ("#2a3520", "#8bc34a"),
    "S-II":             ("#2a3520", "#8bc34a"),
    "Hb":               ("#1e2a3a", "#81d4fa"),
    "H-beta":           ("#1e2a3a", "#81d4fa"),
    "Hbeta":            ("#1e2a3a", "#81d4fa"),
    "H-b":              ("#1e2a3a", "#81d4fa"),
    # LRGB — short and long names
    "L":                ("#222830", "#aabbcc"),
    "Lum":              ("#222830", "#aabbcc"),
    "Luminance":        ("#222830", "#aabbcc"),
    "R":                ("#301818", "#ff6b6b"),
    "Red":              ("#301818", "#ff6b6b"),
    "G":                ("#183018", "#66d966"),
    "Green":            ("#183018", "#66d966"),
    "Grn":              ("#183018", "#66d966"),
    "B":                ("#181830", "#6b9bff"),
    "Blue":             ("#181830", "#6b9bff"),
    "Blu":              ("#181830", "#6b9bff"),
    # Filter-mode strings (single-filter entries)
    "Mono Lum":         ("#222830", "#aabbcc"),
    "LRGB":             ("#222830", "#aabbcc"),
}


# ═══════════════════════════════════════════════════════════════════════
# CONSTANTS & DATA
# ═══════════════════════════════════════════════════════════════════════
C_VALUE = 10
NGC_URL = "https://raw.githubusercontent.com/mattiaverga/OpenNGC/master/database_files/NGC.csv"
ADDENDUM_URL = "https://raw.githubusercontent.com/mattiaverga/OpenNGC/master/database_files/addendum.csv"
MESSIER_XREF_URL = "http://www.messier.seds.org/m-cross.html"

# Filename of the bundled Sharpless catalog (313 H II regions, derived from
# VizieR VII/20 with B1900→J2000 precession applied).  Shipped as a data
# file next to AstroPlanner.py and loaded at startup via _resource_path().
SHARPLESS_CATALOG_FILE = "sharpless_catalog.csv"

# Caldwell cross-reference table — maps each Caldwell number to its NGC/IC
# designation as published by Patrick Moore in Sky & Telescope, Dec 1995.
# The 4 Caldwell objects that are NOT in NGC/IC (C9 Cave/Sh2-155, C14 Double
# Cluster, C41 Hyades, C99 Coalsack) are omitted here — they're supplied by
# OpenNGC's addendum.csv where they live as Name="C009", "C014", "C041", "C099".
# At load time we inject "Cnn" into the Common names field of the matching
# NGC/IC row, so the existing alias system picks up Caldwell-number searches
# the same way it handles Messier numbers.
CALDWELL_TABLE = [
    (  1, "NGC 188"),   (  2, "NGC 40"),    (  3, "NGC 4236"), (  4, "NGC 7023"),
    (  5, "IC 342"),    (  6, "NGC 6543"),  (  7, "NGC 2403"), (  8, "NGC 559"),
    ( 10, "NGC 663"),   ( 11, "NGC 7635"),  ( 12, "NGC 6946"), ( 13, "NGC 457"),
    ( 15, "NGC 6826"),  ( 16, "NGC 7243"),  ( 17, "NGC 147"),  ( 18, "NGC 185"),
    ( 19, "IC 5146"),   ( 20, "NGC 7000"),  ( 21, "NGC 4449"), ( 22, "NGC 7662"),
    ( 23, "NGC 891"),   ( 24, "NGC 1275"),  ( 25, "NGC 2419"), ( 26, "NGC 4244"),
    ( 27, "NGC 6888"),  ( 28, "NGC 752"),   ( 29, "NGC 5005"), ( 30, "NGC 7331"),
    ( 31, "IC 405"),    ( 32, "NGC 4631"),  ( 33, "NGC 6992"), ( 34, "NGC 6960"),
    ( 35, "NGC 4889"),  ( 36, "NGC 4559"),  ( 37, "NGC 6885"), ( 38, "NGC 4565"),
    ( 39, "NGC 2392"),  ( 40, "NGC 3626"),  ( 42, "NGC 7006"), ( 43, "NGC 7814"),
    ( 44, "NGC 7479"),  ( 45, "NGC 5248"),  ( 46, "NGC 2261"), ( 47, "NGC 6934"),
    ( 48, "NGC 2775"),  ( 49, "NGC 2237"),  ( 50, "NGC 2244"), ( 51, "IC 1613"),
    ( 52, "NGC 4697"),  ( 53, "NGC 3115"),  ( 54, "NGC 2506"), ( 55, "NGC 7009"),
    ( 56, "NGC 246"),   ( 57, "NGC 6822"),  ( 58, "NGC 2360"), ( 59, "NGC 3242"),
    ( 60, "NGC 4038"),  ( 61, "NGC 4039"),  ( 62, "NGC 247"),  ( 63, "NGC 7293"),
    ( 64, "NGC 2362"),  ( 65, "NGC 253"),   ( 66, "NGC 5694"), ( 67, "NGC 1097"),
    ( 68, "NGC 6729"),  ( 69, "NGC 6302"),  ( 70, "NGC 300"),  ( 71, "NGC 2477"),
    ( 72, "NGC 55"),    ( 73, "NGC 1851"),  ( 74, "NGC 3132"), ( 75, "NGC 6124"),
    ( 76, "NGC 6231"),  ( 77, "NGC 5128"),  ( 78, "NGC 6541"), ( 79, "NGC 3201"),
    ( 80, "NGC 5139"),  ( 81, "NGC 6352"),  ( 82, "NGC 6193"), ( 83, "NGC 4945"),
    ( 84, "NGC 5286"),  ( 85, "IC 2391"),   ( 86, "NGC 6397"), ( 87, "NGC 1261"),
    ( 88, "NGC 5823"),  ( 89, "NGC 6087"),  ( 90, "NGC 2867"), ( 91, "NGC 3532"),
    ( 92, "NGC 3372"),  ( 93, "NGC 6752"),  ( 94, "NGC 4755"), ( 95, "NGC 6025"),
    ( 96, "NGC 2516"),  ( 97, "NGC 3766"),  ( 98, "NGC 4609"), (100, "IC 2944"),
    (101, "NGC 6744"),  (102, "IC 2602"),   (103, "NGC 2070"), (104, "NGC 362"),
    (105, "NGC 4833"),  (106, "NGC 104"),   (107, "NGC 6101"), (108, "NGC 4372"),
    (109, "NGC 3195"),
]

BORTLE_FACTORS = {
    "1 (Excellent)": {"mono": 1.0, "color": 0.33},
    "2 (Typical Truly Dark)": {"mono": 1.5, "color": 0.5},
    "3 (Rural)": {"mono": 2.5, "color": 0.8},
    "4 (Rural/Suburban)": {"mono": 4.5, "color": 1.5},
    "5 (Suburban)": {"mono": 8.0, "color": 2.7},
    "6 (Bright Suburban)": {"mono": 15.0, "color": 5.0},
    "7 (Suburban/Urban)": {"mono": 25.0, "color": 8.3},
    "8 (City)": {"mono": 45.0, "color": 15.0},
    "9 (Inner-City)": {"mono": 80.0, "color": 26.0}
}

# Seasonal recommendations mapping
SEASONAL_TARGETS = {
    "Winter": ["M42", "M45", "NGC 2244", "M1", "IC 434", "NGC 2024", "M35", "NGC 2264", "M78", "NGC 1499",
               # Caldwell + Sharpless additions
               "C41", "C46", "Sh2-240", "Sh2-264", "Sh2-308"],
    "Spring": ["M81", "M82", "M51", "M101", "M63", "M104", "M64", "NGC 4565", "M87", "NGC 4631",
               # Caldwell + Sharpless additions (Sharpless is sparse in spring — Milky Way out of frame)
               "C53", "C77", "C7"],
    "Summer": ["M8", "M20", "M16", "M17", "M27", "M57", "NGC 6960", "NGC 7000", "IC 1396", "M31",
               # Caldwell + Sharpless additions
               "C27", "C33", "Sh2-101", "Sh2-129"],
    "Autumn": ["M31", "M33", "NGC 7293", "NGC 253", "M52", "NGC 7331", "NGC 891", "M74", "NGC 7635", "NGC 281",
               # Caldwell + Sharpless additions
               "C22", "C9", "Sh2-155", "Sh2-157"]
}

# NGC type → human-readable category
NGC_TYPE_CATEGORIES = {
    "G":   "Galaxy", "GG": "Galaxy", "IG": "Galaxy", "S0": "Galaxy",
    "Sa":  "Galaxy", "Sb": "Galaxy", "Sc": "Galaxy", "Sd": "Galaxy",
    "SBa": "Galaxy", "SBb": "Galaxy", "SBc": "Galaxy", "SBd": "Galaxy",
    "E":   "Galaxy", "E-S0": "Galaxy", "S": "Galaxy",
    "GPair": "Galaxy", "GTrpl": "Galaxy", "GGroup": "Galaxy",
    "OC":  "Open Cluster", "OCl": "Open Cluster",
    "GC":  "Globular Cluster", "GCl": "Globular Cluster",
    "EN":  "Nebula", "RN": "Nebula", "RfN": "Nebula", "PN": "Planetary Nebula",
    "SNR": "Nebula", "HII": "Nebula", "EmN": "Nebula", "EN+RN": "Nebula",
    "DrkN": "Nebula", "Neb": "Nebula",
    # "Cl+N" and "C+N" mean "Star cluster + Nebula" in OpenNGC.
    # These are nebula complexes with embedded clusters (e.g. Orion Nebula/M42,
    # Eagle Nebula/M16, Lagoon Nebula/M8). Map to Nebula, not Open Cluster.
    "Cl+N": "Nebula", "C+N": "Nebula",
    "Ast": "Asterism", "Star": "Star", "D*": "Star",
}

OBJECT_TYPE_FILTERS = ["All Types", "Galaxy", "Nebula", "Planetary Nebula", "Open Cluster", "Globular Cluster"]

# Built-in Messier catalog used as offline fallback when NGC download fails.
# Columns: (name, type, RA h:m:s, Dec ±d:m:s, majax', minax', Vmag, SurfBr, common_names)
# Types must match NGC_TYPE_CATEGORIES keys: E, Sa/Sb/Sc, S0, IG, OC, GC, EN, RN, PN, SNR, D*, Ast
_FALLBACK_CATALOG = [
    ("NGC 1952",  "SNR", "05:34:32", "+22:01:00",  7.0,  5.0,   8.4,  "",    "M1; Crab Nebula"),
    ("NGC 7089",  "GC",  "21:33:27", "-00:49:23",  16.0, 16.0,  6.5,  "",    "M2"),
    ("NGC 5272",  "GC",  "13:42:11", "+28:22:38",  18.0, 18.0,  6.2,  "",    "M3"),
    ("NGC 6121",  "GC",  "16:23:35", "-26:31:32",  26.0, 26.0,  5.6,  "",    "M4"),
    ("NGC 5904",  "GC",  "15:18:34", "+02:04:58",  23.0, 23.0,  5.6,  "",    "M5"),
    ("NGC 6405",  "OC",  "17:40:20", "-32:15:12",  25.0, 25.0,  4.2,  "",    "M6; Butterfly Cluster"),
    ("NGC 6475",  "OC",  "17:53:51", "-34:47:34",  80.0, 80.0,  3.3,  "",    "M7; Ptolemy Cluster"),
    ("NGC 6523",  "EN",  "18:03:37", "-24:23:12",  90.0, 40.0,  6.0,  "",    "M8; Lagoon Nebula"),
    ("NGC 6333",  "GC",  "17:19:12", "-18:30:58",  9.3,  9.3,   7.7,  "",    "M9"),
    ("NGC 6254",  "GC",  "16:57:09", "-04:05:58",  20.0, 20.0,  6.6,  "",    "M10"),
    ("NGC 6705",  "OC",  "18:51:05", "-06:16:12",  14.0, 14.0,  5.8,  "",    "M11; Wild Duck Cluster"),
    ("NGC 6218",  "GC",  "16:47:14", "-01:56:52",  16.0, 16.0,  6.7,  "",    "M12"),
    ("NGC 6205",  "GC",  "16:41:41", "+36:27:37",  20.0, 20.0,  5.8,  "",    "M13; Great Globular Cluster in Hercules"),
    ("NGC 6402",  "GC",  "17:37:36", "-03:14:45",  11.7, 11.7,  7.6,  "",    "M14"),
    ("NGC 7078",  "GC",  "21:29:58", "+12:10:01",  18.0, 18.0,  6.2,  "",    "M15"),
    ("NGC 6611",  "OC",  "18:18:48", "-13:47:00",  7.0,  7.0,   6.0,  "",    "M16; Eagle Nebula; Star Queen Nebula"),
    ("NGC 6618",  "EN",  "18:20:26", "-16:10:36",  46.0, 37.0,  6.0,  "",    "M17; Omega Nebula; Swan Nebula"),
    ("NGC 6613",  "OC",  "18:19:58", "-17:08:00",  9.0,  9.0,   6.9,  "",    "M18"),
    ("NGC 6273",  "GC",  "17:02:38", "-26:16:05",  17.0, 17.0,  6.8,  "",    "M19"),
    ("NGC 6514",  "EN",  "18:02:23", "-23:01:48",  28.0, 28.0,  6.3,  "",    "M20; Trifid Nebula"),
    ("NGC 6531",  "OC",  "18:04:13", "-22:29:24",  13.0, 13.0,  5.9,  "",    "M21"),
    ("NGC 6656",  "GC",  "18:36:24", "-23:54:17",  32.0, 32.0,  5.1,  "",    "M22; Sagittarius Cluster"),
    ("NGC 6494",  "OC",  "17:56:54", "-19:01:00",  27.0, 27.0,  5.5,  "",    "M23"),
    ("IC 4715",   "OC",  "18:16:09", "-18:29:00",  90.0, 90.0,  4.6,  "",    "M24; Sagittarius Star Cloud"),
    ("IC 4725",   "OC",  "18:31:47", "-19:15:00",  32.0, 32.0,  4.6,  "",    "M25"),
    ("NGC 6694",  "OC",  "18:45:18", "-09:23:00",  15.0, 15.0,  8.0,  "",    "M26"),
    ("NGC 6853",  "PN",  "19:59:36", "+22:43:16",  8.0,  5.7,   7.4,  "",    "M27; Dumbbell Nebula"),
    ("NGC 6626",  "GC",  "18:24:33", "-24:52:12",  11.2, 11.2,  6.8,  "",    "M28"),
    ("NGC 6913",  "OC",  "20:23:58", "+38:30:28",  7.0,  7.0,   6.6,  "",    "M29"),
    ("NGC 7099",  "GC",  "21:40:22", "-23:10:47",  11.0, 11.0,  7.2,  "",    "M30"),
    ("NGC 224",   "Sb",  "00:42:44", "+41:16:09",  178.0, 63.0, 3.4,  13.5,  "M31; Andromeda Galaxy"),
    ("NGC 221",   "E",   "00:42:42", "+40:51:55",  8.0,  6.0,   8.7,  12.4,  "M32"),
    ("NGC 598",   "Sc",  "01:33:51", "+30:39:37",  73.0, 45.0,  5.7,  14.2,  "M33; Triangulum Galaxy"),
    ("NGC 1039",  "OC",  "02:42:07", "+42:47:00",  35.0, 35.0,  5.2,  "",    "M34"),
    ("NGC 2168",  "OC",  "06:08:54", "+24:20:00",  28.0, 28.0,  5.1,  "",    "M35"),
    ("NGC 1960",  "OC",  "05:36:18", "+34:08:00",  12.0, 12.0,  6.0,  "",    "M36"),
    ("NGC 2099",  "OC",  "05:52:18", "+32:33:12",  24.0, 24.0,  5.6,  "",    "M37"),
    ("NGC 1912",  "OC",  "05:28:42", "+35:50:54",  21.0, 21.0,  6.4,  "",    "M38"),
    ("NGC 7092",  "OC",  "21:32:12", "+48:26:00",  32.0, 32.0,  4.6,  "",    "M39"),
    ("M40",       "D*",  "12:22:12", "+58:04:00",  0.8,  0.0,   9.0,  "",    "M40; Winnecke 4"),
    ("NGC 2287",  "OC",  "06:46:01", "-20:46:00",  38.0, 38.0,  4.5,  "",    "M41"),
    ("NGC 1976",  "EN",  "05:35:17", "-05:23:28",  85.0, 60.0,  4.0,  "",    "M42; Orion Nebula; Great Nebula in Orion"),
    ("NGC 1982",  "EN",  "05:35:31", "-05:16:00",  20.0, 15.0,  9.0,  "",    "M43; De Mairan's Nebula"),
    ("NGC 2632",  "OC",  "08:40:22", "+19:40:00",  95.0, 95.0,  3.1,  "",    "M44; Beehive Cluster; Praesepe"),
    ("M45",       "OC",  "03:47:24", "+24:07:00",  110.0,110.0, 1.6,  "",    "M45; Pleiades; Seven Sisters"),
    ("NGC 2437",  "OC",  "07:41:46", "-14:48:36",  27.0, 27.0,  6.1,  "",    "M46"),
    ("NGC 2422",  "OC",  "07:36:35", "-14:29:00",  30.0, 30.0,  4.4,  "",    "M47"),
    ("NGC 2548",  "OC",  "08:13:43", "-05:45:00",  54.0, 54.0,  5.8,  "",    "M48"),
    ("NGC 4472",  "E",   "12:29:47", "+07:59:59",  9.0,  7.4,   8.4,  13.0,  "M49"),
    ("NGC 2323",  "OC",  "07:02:42", "-08:22:00",  16.0, 16.0,  5.9,  "",    "M50"),
    ("NGC 5194",  "Sc",  "13:29:53", "+47:11:43",  11.0, 7.0,   8.4,  13.6,  "M51; Whirlpool Galaxy"),
    ("NGC 7654",  "OC",  "23:24:48", "+61:35:36",  13.0, 13.0,  6.9,  "",    "M52"),
    ("NGC 5024",  "GC",  "13:12:55", "+18:10:09",  13.0, 13.0,  7.6,  "",    "M53"),
    ("NGC 6715",  "GC",  "18:55:03", "-30:28:47",  12.0, 12.0,  7.6,  "",    "M54"),
    ("NGC 6809",  "GC",  "19:39:59", "-30:57:53",  19.0, 19.0,  6.3,  "",    "M55"),
    ("NGC 6779",  "GC",  "19:16:36", "+30:11:01",  7.1,  7.1,   8.3,  "",    "M56"),
    ("NGC 6720",  "PN",  "18:53:36", "+33:01:45",  2.5,  2.1,   8.8,  "",    "M57; Ring Nebula"),
    ("NGC 4579",  "Sb",  "12:37:44", "+11:49:05",  5.9,  4.7,   9.7,  "",    "M58"),
    ("NGC 4621",  "E",   "12:42:02", "+11:38:49",  5.0,  3.5,   9.6,  "",    "M59"),
    ("NGC 4649",  "E",   "12:43:40", "+11:33:09",  7.0,  6.0,   8.8,  "",    "M60"),
    ("NGC 4303",  "Sc",  "12:21:55", "+04:28:26",  6.0,  5.9,   9.7,  "",    "M61"),
    ("NGC 6266",  "GC",  "17:01:13", "-30:06:44",  15.0, 15.0,  6.5,  "",    "M62"),
    ("NGC 5055",  "Sb",  "13:15:49", "+42:01:45",  12.0, 7.6,   8.6,  13.6,  "M63; Sunflower Galaxy"),
    ("NGC 4826",  "Sb",  "12:56:44", "+21:40:58",  10.0, 5.4,   8.5,  13.0,  "M64; Black Eye Galaxy"),
    ("NGC 3623",  "Sa",  "11:18:56", "+13:05:32",  10.0, 3.3,   9.3,  "",    "M65"),
    ("NGC 3627",  "Sb",  "11:20:15", "+12:59:30",  9.1,  4.2,   8.9,  "",    "M66"),
    ("NGC 2682",  "OC",  "08:51:18", "+11:49:00",  30.0, 30.0,  6.1,  "",    "M67"),
    ("NGC 4590",  "GC",  "12:39:28", "-26:44:34",  12.0, 12.0,  7.8,  "",    "M68"),
    ("NGC 6637",  "GC",  "18:31:23", "-32:20:53",  9.8,  9.8,   7.6,  "",    "M69"),
    ("NGC 6681",  "GC",  "18:43:12", "-32:17:31",  8.0,  8.0,   7.9,  "",    "M70"),
    ("NGC 6838",  "GC",  "19:53:46", "+18:46:45",  7.2,  7.2,   8.2,  "",    "M71"),
    ("NGC 6981",  "GC",  "20:53:28", "-12:32:14",  6.6,  6.6,   9.3,  "",    "M72"),
    ("NGC 6994",  "Ast", "20:58:54", "-12:38:00",  2.8,  2.8,   9.0,  "",    "M73"),
    ("NGC 628",   "Sc",  "01:36:42", "+15:47:01",  10.5, 9.5,   9.4,  14.8,  "M74; Phantom Galaxy"),
    ("NGC 6864",  "GC",  "20:06:05", "-21:55:17",  6.8,  6.8,   8.5,  "",    "M75"),
    ("NGC 650",   "PN",  "01:42:20", "+51:34:31",  2.7,  1.8,   10.1, "",    "M76; Little Dumbbell Nebula"),
    ("NGC 1068",  "Sb",  "02:42:41", "-00:00:48",  7.0,  6.0,   8.9,  "",    "M77; Cetus A"),
    ("NGC 2068",  "RN",  "05:46:46", "+00:04:00",  8.0,  6.0,   8.3,  "",    "M78"),
    ("NGC 1904",  "GC",  "05:24:11", "-24:31:27",  9.6,  9.6,   7.7,  "",    "M79"),
    ("NGC 6093",  "GC",  "16:17:03", "-22:58:30",  10.0, 10.0,  7.3,  "",    "M80"),
    ("NGC 3031",  "Sb",  "09:55:33", "+69:03:55",  26.0, 14.0,  6.9,  13.6,  "M81; Bode's Galaxy"),
    ("NGC 3034",  "IG",  "09:55:52", "+69:40:47",  11.0, 4.3,   8.4,  13.1,  "M82; Cigar Galaxy"),
    ("NGC 5236",  "Sc",  "13:37:01", "-29:51:57",  13.0, 11.0,  7.5,  13.2,  "M83; Southern Pinwheel Galaxy"),
    ("NGC 4374",  "E",   "12:25:04", "+12:53:13",  6.5,  5.6,   9.1,  "",    "M84"),
    ("NGC 4382",  "S0",  "12:25:24", "+18:11:28",  7.1,  5.5,   9.1,  "",    "M85"),
    ("NGC 4406",  "E",   "12:26:12", "+12:56:45",  8.9,  5.8,   8.9,  "",    "M86"),
    ("NGC 4486",  "E",   "12:30:49", "+12:23:28",  8.3,  6.6,   8.6,  13.2,  "M87; Virgo A"),
    ("NGC 4501",  "Sb",  "12:31:59", "+14:25:14",  6.9,  3.7,   9.5,  "",    "M88"),
    ("NGC 4552",  "E",   "12:35:40", "+12:33:23",  5.1,  4.7,   9.8,  "",    "M89"),
    ("NGC 4569",  "Sb",  "12:36:50", "+13:09:46",  9.5,  4.4,   9.5,  "",    "M90"),
    ("NGC 4548",  "Sb",  "12:35:26", "+14:29:47",  5.4,  4.3,   10.2, "",    "M91"),
    ("NGC 6341",  "GC",  "17:17:07", "+43:08:09",  14.0, 14.0,  6.4,  "",    "M92"),
    ("NGC 2447",  "OC",  "07:44:30", "-23:51:24",  22.0, 22.0,  6.2,  "",    "M93"),
    ("NGC 4736",  "Sb",  "12:50:53", "+41:07:14",  14.0, 12.0,  8.2,  13.0,  "M94; Croc's Eye Galaxy"),
    ("NGC 3351",  "Sb",  "10:43:58", "+11:42:14",  7.4,  5.1,   9.7,  "",    "M95"),
    ("NGC 3368",  "Sa",  "10:46:46", "+11:49:12",  7.8,  5.2,   9.2,  "",    "M96"),
    ("NGC 3587",  "PN",  "11:14:48", "+55:01:09",  3.4,  3.3,   9.9,  "",    "M97; Owl Nebula"),
    ("NGC 4192",  "Sb",  "12:13:48", "+14:54:01",  9.8,  2.8,   10.1, "",    "M98"),
    ("NGC 4254",  "Sc",  "12:18:50", "+14:24:59",  5.4,  4.8,   9.9,  "",    "M99; Coma Pinwheel"),
    ("NGC 4321",  "Sc",  "12:22:55", "+15:49:21",  7.4,  6.3,   9.3,  "",    "M100"),
    ("NGC 5457",  "Sc",  "14:03:13", "+54:20:57",  28.8, 26.9,  7.9,  14.7,  "M101; Pinwheel Galaxy"),
    ("NGC 5866",  "S0",  "15:06:30", "+55:45:48",  6.5,  3.1,   9.9,  "",    "M102; Spindle Galaxy"),
    ("NGC 581",   "OC",  "01:33:23", "+60:41:36",  6.0,  6.0,   7.4,  "",    "M103"),
    ("NGC 4594",  "Sa",  "12:39:59", "-11:37:23",  8.7,  3.5,   8.0,  12.8,  "M104; Sombrero Galaxy"),
    ("NGC 3379",  "E",   "10:47:49", "+12:34:54",  5.4,  4.8,   9.3,  "",    "M105"),
    ("NGC 4258",  "Sb",  "12:18:58", "+47:18:14",  18.0, 8.0,   8.4,  13.5,  "M106"),
    ("NGC 6171",  "GC",  "16:32:32", "-13:03:13",  10.0, 10.0,  7.9,  "",    "M107"),
    ("NGC 3556",  "Sc",  "11:11:31", "+55:40:27",  8.6,  2.4,   10.0, "",    "M108; Surfboard Galaxy"),
    ("NGC 3992",  "Sb",  "11:57:36", "+53:22:28",  7.6,  4.7,   9.8,  "",    "M109"),
    ("NGC 205",   "E",   "00:40:22", "+41:41:07",  21.9, 11.0,  8.1,  13.4,  "M110"),
]


def _jd_now():
    """Julian Date for the planning reference time.
    Returns JD at noon on the active planning date, or JD for right now (UTC) if no
    planning date is set.  All nightly scan functions use this as their scan-start."""
    if _PLANNING_DATE is not None:
        # Use noon on the planning date (local time used as proxy for UTC — error < 1 day,
        # which is fine for nightly planning purposes).
        ref = _planning_local_noon()
    else:
        ref = datetime.now(timezone.utc).replace(tzinfo=None)
    a   = (14 - ref.month) // 12
    y   = ref.year + 4800 - a
    m   = ref.month + 12 * a - 3
    jdn = ref.day + (153*m+2)//5 + 365*y + y//4 - y//100 + y//400 - 32045
    return jdn + (ref.hour - 12) / 24 + ref.minute / 1440 + ref.second / 86400


def _ra_dec_to_altaz(ra_deg, dec_deg, lat_deg, lon_deg, jd=None):
    """Return altitude (degrees) for given RA/Dec, observer location, and time."""
    if jd is None:
        jd = _jd_now()
    # Greenwich Mean Sidereal Time
    T = (jd - 2451545.0) / 36525.0
    gmst_deg = (280.46061837 + 360.98564736629 * (jd - 2451545.0)
                + T * T * 0.000387933 - T**3 / 38710000.0) % 360
    lst_deg = (gmst_deg + lon_deg) % 360
    ha_deg = (lst_deg - ra_deg) % 360

    ha = math.radians(ha_deg)
    dec = math.radians(dec_deg)
    lat = math.radians(lat_deg)

    sin_alt = math.sin(dec)*math.sin(lat) + math.cos(dec)*math.cos(lat)*math.cos(ha)
    alt = math.degrees(math.asin(max(-1.0, min(1.0, sin_alt))))
    return alt


def _calc_moon_phase():
    """
    Return (phase_name, illumination_pct, emoji, age_days, days_to_new, days_to_full) for today.
    Uses a simple synodic period calculation from a known New Moon epoch.
    """
    # Known New Moon: 2000-01-06 18:14 UTC  (JD 2451550.259)
    NEW_MOON_EPOCH_JD = 2451550.259
    SYNODIC_PERIOD    = 29.53058867

    jd    = _jd_now()
    age   = (jd - NEW_MOON_EPOCH_JD) % SYNODIC_PERIOD   # days since last new moon
    frac  = age / SYNODIC_PERIOD                          # 0.0 – 1.0 through cycle

    # Illumination: fraction of disc lit
    illum = (1 - math.cos(2 * math.pi * frac)) / 2
    illum_pct = round(illum * 100)

    # Phase name + emoji — mapped from the fraction through the synodic cycle.
    if frac < 0.025 or frac >= 0.975:
        name, emoji = "New Moon",        "🌑"
    elif frac < 0.25:
        name, emoji = "Waxing Crescent", "🌒"
    elif frac < 0.275:
        name, emoji = "First Quarter",   "🌓"
    elif frac < 0.50:
        name, emoji = "Waxing Gibbous",  "🌔"
    elif frac < 0.525:
        name, emoji = "Full Moon",       "🌕"
    elif frac < 0.75:
        name, emoji = "Waning Gibbous",  "🌖"
    elif frac < 0.775:
        name, emoji = "Last Quarter",    "🌗"
    else:
        name, emoji = "Waning Crescent", "🌘"

    # Days until next New Moon and Full Moon
    days_to_new  = SYNODIC_PERIOD - age
    days_to_full = (SYNODIC_PERIOD * 0.5 - age) % SYNODIC_PERIOD

    return name, illum_pct, emoji, round(age, 1), round(days_to_new, 1), round(days_to_full, 1)


def _calc_moon_separation(target_ra_deg, target_dec_deg):
    """Return the angular separation in degrees between the Moon and the target right now.

    Uses the spherical law of cosines:
        cos(d) = sin(d1)*sin(d2) + cos(d1)*cos(d2)*cos(ra1-ra2)

    Also returns (moon_ra_deg, moon_dec_deg) so callers don't need to re-query.
    """
    moon_ra, moon_dec = _calc_moon_position(_jd_now())

    ra1  = math.radians(moon_ra)
    dec1 = math.radians(moon_dec)
    ra2  = math.radians(target_ra_deg)
    dec2 = math.radians(target_dec_deg)

    cos_d = (math.sin(dec1) * math.sin(dec2)
             + math.cos(dec1) * math.cos(dec2) * math.cos(ra1 - ra2))
    # clamp to [-1, 1] to guard against tiny floating-point overruns
    sep_deg = math.degrees(math.acos(max(-1.0, min(1.0, cos_d))))
    return sep_deg, moon_ra, moon_dec


def _calc_twilight(lat_deg, lon_deg, altitude_deg=-18.0):
    """
    Return (dusk_str, dawn_str) local-time strings for the moment the sun
    crosses `altitude_deg` on the current date.
    Default -18° = astronomical twilight.
    Returns (None, None) if the sun never crosses that altitude (polar day/night).
    """
    utc_offset_h = _utc_offset_hours()

    plan_local = _planning_local_noon()
    n = plan_local.timetuple().tm_yday  # day of year

    # Solar declination
    L   = (280.460 + 0.9856474 * n) % 360
    g   = math.radians((357.528 + 0.9856003 * n) % 360)
    lam = math.radians(L + 1.915 * math.sin(g) + 0.020 * math.sin(2 * g))
    eps = math.radians(23.439 - 0.0000004 * n)
    dec = math.asin(math.sin(eps) * math.sin(lam))

    # Equation of time (minutes) — Spencer approximation
    B   = math.radians(360 / 365 * (n - 81))
    eot = 9.87 * math.sin(2 * B) - 7.53 * math.cos(B) - 1.5 * math.sin(B)

    # Solar noon in local clock hours
    solar_noon = 12.0 - lon_deg / 15.0 - eot / 60.0 + utc_offset_h

    lat = math.radians(lat_deg)
    alt = math.radians(altitude_deg)

    cos_H = (math.sin(alt) - math.sin(lat) * math.sin(dec)) / (
             math.cos(lat) * math.cos(dec) + 1e-10)

    if cos_H < -1.0 or cos_H > 1.0:
        return None, None  # midnight sun or polar night

    H_hours = math.degrees(math.acos(cos_H)) / 15.0

    def _fmt(h):
        h = h % 24
        return f"{int(h):02d}:{int((h % 1) * 60):02d}"

    return _fmt(solar_noon + H_hours), _fmt(solar_noon - H_hours)


def _sun_ra_dec(jd):
    """Approximate solar RA (degrees) and Dec (degrees) at given JD."""
    n   = jd - 2451545.0
    L   = (280.460 + 0.9856474 * n) % 360
    g   = math.radians((357.528 + 0.9856003 * n) % 360)
    lam = math.radians(L + 1.915 * math.sin(g) + 0.020 * math.sin(2 * g))
    eps = math.radians(23.439 - 0.0000004 * n)
    ra  = math.degrees(math.atan2(math.cos(eps) * math.sin(lam), math.cos(lam))) % 360
    dec = math.degrees(math.asin(math.sin(eps) * math.sin(lam)))
    return ra, dec


def _calc_best_imaging_window(ra_deg, dec_deg, lat_deg, lon_deg, min_alt=20.0):
    """
    Return (start_str, end_str, window_hrs, peak_str, peak_alt, diag_str) for tonight.

    Scans a 30-hour window in 5-minute steps starting from local noon today.
    At each step checks: sun altitude < -12° (nautical darkness) AND target >= min_alt.
    All times are returned as local HH:MM strings.
    """
    utc_offset_h = _utc_offset_hours()

    plan_local = _planning_local_noon()
    jd_scan_start = _jd_local_noon()

    step_jd = 5.0 / 1440.0
    n_steps = 360

    start_jd = end_jd = peak_jd = None
    peak_alt_found = -90.0
    been_dark = False
    diag_samples = []

    for i in range(n_steps + 1):
        jd = jd_scan_start + i * step_jd

        sun_ra, sun_dec = _sun_ra_dec(jd)
        sun_alt = _ra_dec_to_altaz(sun_ra, sun_dec, lat_deg, lon_deg, jd)
        target_alt = _ra_dec_to_altaz(ra_deg, dec_deg, lat_deg, lon_deg, jd)

        # Capture a sample every 2 hours (24 steps) for diagnostics
        if i % 24 == 0:
            loc_h = (((jd + 0.5) % 1.0) * 24.0 + utc_offset_h) % 24.0
            diag_samples.append(f"{int(loc_h):02d}:{int((loc_h%1)*60):02d} ☀{sun_alt:.0f}° 🎯{target_alt:.0f}°")

        if sun_alt >= -12.0:
            if been_dark:
                break
            continue

        been_dark = True

        if target_alt >= min_alt:
            if start_jd is None:
                start_jd = jd
            end_jd = jd
        if target_alt > peak_alt_found:
            peak_alt_found = target_alt
            peak_jd = jd

    def _jd_to_local_str(jd):
        lh = (((jd + 0.5) % 1.0) * 24.0 + utc_offset_h) % 24.0
        return f"{int(lh):02d}:{int((lh % 1) * 60):02d}"

    start_str  = _jd_to_local_str(start_jd) if start_jd else None
    end_str    = _jd_to_local_str(end_jd)   if end_jd   else None
    peak_str   = _jd_to_local_str(peak_jd)  if peak_jd  else None
    window_hrs = round((end_jd - start_jd) * 24.0, 1) if (start_jd and end_jd) else 0.0

    scan_local_h = (((jd_scan_start + 0.5) % 1.0) * 24.0 + utc_offset_h) % 24.0
    diag = (f"now={plan_local.strftime('%H:%M')} utcOff={utc_offset_h:+.0f}h "
            f"scanFrom={int(scan_local_h):02d}:{int((scan_local_h%1)*60):02d} "
            f"dark={been_dark} peak={peak_alt_found:.0f}° || " + "  ".join(diag_samples))

    return start_str, end_str, window_hrs, peak_str, round(peak_alt_found, 1), diag


def _max_altitude_tonight(ra_deg, dec_deg, lat_deg, lon_deg, dark_range=None):
    """Return the maximum altitude (degrees) the object reaches during nautical darkness tonight.

    If *dark_range* is supplied as ``(jd_dusk, jd_dawn, jd_scan_start)`` (from
    :func:`_dark_jd_range`), the sun-position scan is skipped entirely —
    the function only evaluates target alt/az over the pre-computed dark
    window.  This makes batch calls (e.g. Visible Tonight scanning 13 000
    objects) ~3-4× faster because the sun scan is done once instead of
    once per object.

    When *dark_range* is ``None`` the function falls back to computing the
    darkness window itself (original single-object behaviour).
    """
    step_jd = 5.0 / 1440.0  # 5-minute resolution

    if dark_range is not None:
        # Fast path — darkness bounds pre-computed by caller.
        jd_dusk, jd_dawn, _ = dark_range
        if jd_dusk is None or jd_dawn is None:
            return -90.0  # no darkness tonight (polar day)
        max_alt = -90.0
        jd = jd_dusk
        while jd <= jd_dawn:
            alt = _ra_dec_to_altaz(ra_deg, dec_deg, lat_deg, lon_deg, jd)
            if alt > max_alt:
                max_alt = alt
            jd += step_jd
        return max_alt

    # Slow path — full scan including sun position (backward compat).
    jd_scan_start = _jd_local_noon()

    n_steps  = 360   # 30 hours
    max_alt  = -90.0
    been_dark = False

    for i in range(n_steps + 1):
        jd = jd_scan_start + i * step_jd
        sun_ra, sun_dec = _sun_ra_dec(jd)
        sun_alt = _ra_dec_to_altaz(sun_ra, sun_dec, lat_deg, lon_deg, jd)

        if sun_alt >= -12.0:
            if been_dark:
                break      # sun has risen again — tonight is over
            continue

        been_dark = True
        alt = _ra_dec_to_altaz(ra_deg, dec_deg, lat_deg, lon_deg, jd)
        if alt > max_alt:
            max_alt = alt

    return max_alt


def _alt_rise_tonight(ra_deg, dec_deg, lat_deg, lon_deg, min_alt, dark_range):
    """Return (max_alt, rise_label) for tonight's nautical-darkness window.

    *max_alt* is the highest altitude (deg) reached while dark.  *rise_label*
    is the observable rise — the moment the object becomes usable:
      • local "HH:MM" when it first climbs above *min_alt* during darkness,
      • "up" when it is already above *min_alt* at the start of darkness
        (rose before dusk, or circumpolar), or
      • None when it never clears *min_alt* while dark.

    Computed in the same single dark-window scan as the altitude, reusing the
    pre-computed *dark_range* from :func:`_dark_jd_range`, so the batch
    'Visible Tonight' search stays fast.
    """
    if not dark_range:
        return -90.0, None
    jd_dusk, jd_dawn, _ = dark_range
    if jd_dusk is None or jd_dawn is None:
        return -90.0, None   # no darkness tonight (polar day)

    step_jd      = 5.0 / 1440.0  # 5-minute resolution
    utc_offset_h = _utc_offset_hours()

    max_alt    = -90.0
    rise_jd    = None
    already_up = False
    first      = True

    jd = jd_dusk
    while jd <= jd_dawn:
        alt = _ra_dec_to_altaz(ra_deg, dec_deg, lat_deg, lon_deg, jd)
        if alt > max_alt:
            max_alt = alt
        if rise_jd is None and not already_up and alt >= min_alt:
            if first:
                already_up = True          # above the line before dark began
            else:
                rise_jd = jd               # first upward crossing while dark
        first = False
        jd += step_jd

    if already_up:
        rise_label = "up"
    elif rise_jd is not None:
        lh = (((rise_jd + 0.5) % 1.0) * 24.0 + utc_offset_h) % 24.0
        rise_label = f"{int(lh):02d}:{int((lh % 1) * 60):02d}"
    else:
        rise_label = None
    return max_alt, rise_label


def _dark_jd_range(lat_deg, lon_deg):
    """Pre-compute the JD bounds of nautical darkness for tonight.

    Returns ``(jd_dusk, jd_dawn, jd_scan_start)`` where *jd_dusk* and
    *jd_dawn* bracket the period when the sun is below −12°.

    If the sun never drops below −12° (polar day / midnight sun) both
    *jd_dusk* and *jd_dawn* are ``None``.

    The returned tuple can be passed directly to :func:`_max_altitude_tonight`
    as the *dark_range* keyword to skip the per-object sun scan.
    """
    jd_scan_start = _jd_local_noon()

    step_jd = 5.0 / 1440.0
    n_steps = 360  # 30 hours

    jd_dusk = jd_dawn = None
    been_dark = False

    for i in range(n_steps + 1):
        jd = jd_scan_start + i * step_jd
        sun_ra, sun_dec = _sun_ra_dec(jd)
        sun_alt = _ra_dec_to_altaz(sun_ra, sun_dec, lat_deg, lon_deg, jd)

        if sun_alt < -12.0:
            if not been_dark:
                jd_dusk = jd
                been_dark = True
            jd_dawn = jd  # keep updating — last dark step becomes dawn
        else:
            if been_dark:
                break  # sun has risen — tonight is over

    return jd_dusk, jd_dawn, jd_scan_start


def _calc_moon_position(jd):
    """
    Return approximate (ra_deg, dec_deg) for the Moon.
    Uses truncated ELP2000 series — accuracy ~1°, sufficient for rise/set.
    """
    T  = (jd - 2451545.0) / 36525.0
    L  = (218.3164477 + 481267.88123421 * T) % 360   # mean longitude
    D  = (297.8501921 + 445267.1114034  * T) % 360   # mean elongation
    M  = (357.5291092 +  35999.0502909  * T) % 360   # Sun's mean anomaly
    Mm = (134.9633964 + 477198.8675055  * T) % 360   # Moon's mean anomaly
    F  = ( 93.2720950 + 483202.0175233  * T) % 360   # argument of latitude

    Dr, Mr, Mmr, Fr, Lr = [math.radians(x) for x in [D, M, Mm, F, L]]

    dLon = (6.288774 * math.sin(Mmr)
          + 1.274027 * math.sin(2*Dr - Mmr)
          + 0.658314 * math.sin(2*Dr)
          + 0.213618 * math.sin(2*Mmr)
          - 0.185116 * math.sin(Mr)
          - 0.114332 * math.sin(2*Fr)
          + 0.058793 * math.sin(2*Dr - 2*Mmr)
          + 0.053322 * math.sin(2*Dr + Mmr)
          + 0.045758 * math.sin(2*Dr - Mr))

    dLat = (5.128122 * math.sin(Fr)
          + 0.280602 * math.sin(Mmr + Fr)
          + 0.277693 * math.sin(Mmr - Fr)
          + 0.173237 * math.sin(2*Dr - Fr)
          + 0.055413 * math.sin(2*Dr - Mmr + Fr)
          + 0.046272 * math.sin(2*Dr - Mmr - Fr))

    lam  = math.radians((L + dLon) % 360)
    beta = math.radians(dLat)
    eps  = math.radians(23.439291 - 0.013004 * T)

    ra  = math.degrees(math.atan2(
              math.sin(lam) * math.cos(eps) - math.tan(beta) * math.sin(eps),
              math.cos(lam))) % 360
    dec = math.degrees(math.asin(
              math.sin(beta) * math.cos(eps) + math.cos(beta) * math.sin(eps) * math.sin(lam)))
    return ra, dec


def _calc_moon_riseset(lat_deg, lon_deg):
    """
    Scan a 30-hour window starting from local noon today in 5-minute steps
    to find the next moonrise and moonset events.
    Returns (rise_str, set_str) as local HH:MM strings, or (None, None) if not found.
    """
    utc_offset_h = _utc_offset_hours()

    # Start scanning from local noon on the planning date
    jd_noon  = _jd_local_noon()
    n_steps  = 360                  # 30 hours at 5-minute resolution
    step_jd  = 5.0 / 1440.0

    rise_jd = set_jd = None
    prev_alt = None

    for i in range(n_steps + 1):
        jd      = jd_noon + i * step_jd
        ra, dec = _calc_moon_position(jd)
        alt     = _ra_dec_to_altaz(ra, dec, lat_deg, lon_deg, jd)

        if prev_alt is not None:
            if prev_alt < 0 <= alt and rise_jd is None:
                frac    = -prev_alt / (alt - prev_alt + 1e-9)
                rise_jd = jd - (1.0 - frac) * step_jd
            elif prev_alt >= 0 > alt and set_jd is None:
                frac   = prev_alt / (prev_alt - alt + 1e-9)
                set_jd = jd - (1.0 - frac) * step_jd
        prev_alt = alt

    def _to_local(j):
        h = ((j + 0.5) % 1.0) * 24.0 + utc_offset_h
        h %= 24.0
        return f"{int(h):02d}:{int((h % 1) * 60):02d}"

    return (_to_local(rise_jd) if rise_jd else None,
            _to_local(set_jd)  if set_jd  else None)


def _calc_transit_time(ra_deg, lon_deg):
    """Return local HH:MM string for the next meridian transit of ra_deg."""
    utc_offset_h = _utc_offset_hours()

    plan_local = _planning_local_noon()
    jd       = _jd_now()
    T        = (jd - 2451545.0) / 36525.0
    gmst_deg = (280.46061837 + 360.98564736629 * (jd - 2451545.0)
                + T*T*0.000387933 - T**3/38710000.0) % 360
    lst_deg  = (gmst_deg + lon_deg) % 360
    ha_deg   = (lst_deg - ra_deg) % 360       # current hour angle
    # Hours until HA wraps back to 0 (next transit)
    hours_to = ((360.0 - ha_deg) % 360.0) / 15.0
    now_h    = plan_local.hour + plan_local.minute / 60.0 + plan_local.second / 3600.0
    t_h      = (now_h + hours_to) % 24.0
    return f"{int(t_h):02d}:{int((t_h % 1) * 60):02d}"


def _write_fallback_catalog(path):
    """Write the built-in 110-object Messier catalog to path as a semicolon-delimited CSV.

    The file uses the same column structure as the downloaded NGC catalog so
    load_target_catalog() can read both formats without any changes.
    Called automatically when the NGC download fails on first run.
    """
    headers = ["Name", "Type", "RA", "Dec", "MajAx", "MinAx", "V-Mag", "SurfBr", "Common names"]
    with open(path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=headers, delimiter=";", extrasaction="ignore")
        writer.writeheader()
        for name, typ, ra, dec, majax, minax, vmag, surfbr, common in _FALLBACK_CATALOG:
            writer.writerow({
                "Name":         name,
                "Type":         typ,
                "RA":           ra,
                "Dec":          dec,
                "MajAx":        majax,
                "MinAx":        minax,
                "V-Mag":        vmag   if vmag   != "" else "",
                "SurfBr":       surfbr if surfbr != "" else "",
                "Common names": common,
            })


class AstroApp:
    """Main Tk application — Lightbucket Astro Planner.

    Five-tab desktop planner for deep-sky astrophotography sessions.
    Holds all UI state, equipment inventory, catalog, and the shared
    Tk root.  Instantiated once at startup from ``__main__``.

    Tabs (in display order via sidebar)
        Target Planner  — analyse a single object: FOV, exposure, window
        Targets List — multi-target queue + per-target integration plan
        Tonight's Plan  — the final list, with Gantt schedule + exports
        Manage Equipment — cameras, telescopes, reducers
        Settings        — observer location, preferences, catalog, NINA
    """

    # ═══════════════════════════════════════════════════════════════════
    # CONSTRUCTION
    # ═══════════════════════════════════════════════════════════════════

    def __init__(self, root):
        """Initialise the app: load data, build UI, schedule deferred startup tasks."""
        self.root = root
        self.root.title(f"Lightbucket Astro Planner · v{__version__}")
        # Windows renders the header bar widgets slightly wider than macOS (the
        # default ttk Windows theme has more generous padding around buttons
        # and the date picker), so the planning-date adjuster gets clipped at
        # 1210px.  Nudge the startup width up on Windows to give it room.
        if platform.system() == "Windows":
            self.root.geometry("1320x855")
        else:
            self.root.geometry("1210x855")

        # ── Window icon ──────────────────────────────────────────────────
        # Sets the taskbar / dock / title-bar / alt-tab icon.  Without this
        # the packaged app shows the generic Tk feather icon.  We prefer
        # platform-native formats when available (.ico on Windows, .icns on
        # macOS) and fall back to the PNG logo via iconphoto, which works
        # cross-platform.  All three lookups go through _resource_path so
        # they resolve correctly in dev and in a PyInstaller bundle.
        try:
            ico_path = _resource_path("logo.ico")
            png_path = _resource_path("logo.png")
            if platform.system() == "Windows" and ico_path.exists():
                self.root.iconbitmap(default=str(ico_path))
            elif png_path.exists():
                self._icon_img = tk.PhotoImage(file=str(png_path))
                self.root.iconphoto(True, self._icon_img)
        except (tk.TclError, OSError):
            # Icon assets missing or unusable — the app runs fine without.
            pass
        
        self.data_path = self.get_data_path()
        self.catalog_path = self.data_path.parent / "ngc_catalog.csv"
        self.addendum_path = self.data_path.parent / "ngc_addendum.csv"

        # ── Crash logging ────────────────────────────────────────────────
        # When packaged with --windowed (no console), uncaught exceptions
        # would otherwise disappear silently — the app just closes.  Route
        # both Python-level unhandled exceptions (sys.excepthook) AND Tk
        # widget-callback exceptions (Tk.report_callback_exception, which
        # Tk catches before they reach excepthook) to a log file in the
        # user's data folder.  Testers can share that file for debugging.
        self._install_crash_logger()

        self.first_run = not self.data_path.exists()
        self.data = self.load_data()

        # Restore saved settings
        global C_VALUE
        _saved_settings = self.data.get("settings", {})
        try:
            _saved_c = int(_saved_settings.get("c_value", C_VALUE))
            if _saved_c > 0:
                C_VALUE = _saved_c
        except (ValueError, TypeError):
            pass
        
        self.targets = {}
        self.common_names_map = {}
        self.searchable_names = []
        self.current_target_info = None  
        self.auto_update_enabled = _saved_settings.get("auto_update", False)

        # Catalog filter state — controls which catalogs are searched.
        # 'Other' covers addendum extras (PGC, ESO, Mel, Barnard, MWSC, HCG,
        # UGC, etc. — ~58 entries that don't carry an NGC/IC/M/C/Sh tag).
        # Persisted to disk; restored each launch.
        self.catalog_filter = _saved_settings.get("catalog_filter") or {
            "NGC": True, "IC": True, "Messier": True,
            "Caldwell": True, "Sharpless": True, "Other": True,
        }
        # Forward-compatibility normaliser: older saved settings may lack
        # newer keys (e.g. 'Other' added later). Default any missing key
        # to True so the user sees everything until they explicitly filter.
        for _cat in ("NGC", "IC", "Messier", "Caldwell", "Sharpless", "Other"):
            self.catalog_filter.setdefault(_cat, True)
        self._catalog_filter_popup = None

        # State shared between analyze_framing and the integration planner
        self._last_exp_s    = None   # most recent recommended sub-exposure (seconds)
        self._last_win_hrs  = None   # most recent dark imaging window length (hours)
        self._last_win_start = None  # imaging window start string (local HH:MM)
        self._last_win_end   = None  # imaging window end string (local HH:MM)
        self._last_sky_flux = None   # sky flux value — used for noise regime note

        # Tonight's Plan — list of dicts, one per planned target
        self._plan_entries: list = []
        self._queue_entries: list = []
        self._editing_queue_idx = None   # index of queue entry being edited, or None
        self._loading_queue_row = False  # True while _queue_load_target is populating fields
        # Backup of the Planner's target search + equipment state, captured
        # when the user switches to the Targets tab.  Restored on return to
        # the Planner tab so the queue-card auto-load doesn't clobber the
        # Planner's in-progress target.  Cleared by _queue_card_click when
        # the user explicitly pivots to a queue entry.
        self._planner_state_backup = None

        # Dirty-flag system for deferred analysis.  Instead of calling
        # analyze_framing immediately (which can be flushed by macOS Tk's
        # update_idletasks into a re-entrant cascade), callers set the dirty
        # flag via _mark_analysis_dirty().  The actual analysis is then
        # scheduled via after() only when the Planner or Session tab is visible.
        self._analysis_dirty = False
        self._analysis_after_id = None   # tracks a pending after() so we don't stack them

        # Plan Gantt drag state
        self._gantt_drag_idx      = None   # plan entry index being dragged
        self._gantt_drag_x0       = None   # mouse x at drag start
        self._gantt_drag_start_h0 = None   # entry start_h at drag start (float hours)
        self._gantt_row_info      = []     # list of (y_top, y_bot, idx, bar_x1, bar_x2, start_h_norm)
        self._gantt_drag_bounds   = {}     # per-row (lo_h, hi_h) drag limits
        self._gantt_ax_start      = None   # axis left edge (float hours)
        self._gantt_ax_range      = None   # axis span (float hours)
        self._gantt_lm            = 74     # left margin pixels
        self._gantt_pw            = 0      # plot width pixels
        self._sidebar_switching   = False  # True while sidebar is initiating a tab switch
        
        self.setup_header()

        # ── Outer layout: sidebar + content area ──────────────────────────
        self._main_frame = tk.Frame(root, bg="#0e1a28")
        self._main_frame.pack(expand=1, fill="both")

        # Sidebar — narrow icon bar on the left
        self._sidebar = tk.Frame(self._main_frame, bg="#131f2e", width=62)
        self._sidebar.pack(side="left", fill="y")
        self._sidebar.pack_propagate(False)

        # Notebook — hide built-in tab bar, controlled from sidebar
        self.tab_control = ttk.Notebook(self._main_frame)
        self.tab_equip = ttk.Frame(self.tab_control)
        self.tab_planner = ttk.Frame(self.tab_control)
        self.tab_session = ttk.Frame(self.tab_control)
        self.tab_plan    = ttk.Frame(self.tab_control)
        self.tab_settings = ttk.Frame(self.tab_control)

        self.tab_control.add(self.tab_planner, text='Target Planner')
        self.tab_control.add(self.tab_equip,   text='Manage Equipment')
        self.tab_control.add(self.tab_session, text='Targets List')
        self.tab_control.add(self.tab_plan,    text="Tonight's Plan")
        self.tab_control.add(self.tab_settings, text='Settings')
        self.tab_control.pack(expand=1, fill="both")
        self.tab_control.bind("<<NotebookTabChanged>>", self._on_tab_changed)

        self._build_sidebar()

        self.setup_input_tab()
        self.setup_planner_tab()
        self.setup_session_tab()
        self.setup_plan_tab()
        self.setup_settings_tab()
        self._setup_day_styles()   # paint dark bg + white text on tabs before snapshot
        
        self.refresh_inventory_tables()
        self.refresh_dropdowns()
        
        self.root.after(100, self.ensure_catalog_exists)
        self.root.after(200, self.refresh_twilight_header)
        self.root.after(250, self.refresh_moon_header)
        # Snapshot widget colours AND ttk styles AFTER dynamic labels have updated
        self._orig_colors: dict = {}
        self._orig_styles: dict = {}
        self.root.after(350, self._snapshot_original_colors)
        self.root.after(360, self._snapshot_original_styles)
        # Route to Equip tab if inventory is empty — sidesteps a macOS Cocoa
        # Tk paint stall that keeps the Planner black on first launch, and
        # lands the user exactly where they need to be to add equipment.
        # Scheduled before show_welcome_message so the dialog appears on
        # top of the Equip tab.
        self.root.after(450, self._route_to_equip_if_empty)
        if self.first_run:
            self.root.after(500, self.show_welcome_message)
        # Kick off background IP-geolocation on launch if no location has
        # been set yet.  Runs off-thread so the UI stays responsive — the
        # main-thread save + header refresh is scheduled back via after().
        self.root.after(600, self._auto_detect_location_if_unset)

        # macOS Cocoa Tk resets ttk styles when a modal dialog (messagebox)
        # returns focus to the main window.  Bind <FocusIn> on root so we
        # re-apply night mode whenever the app regains focus.
        self._night_reapply_id = None
        self.root.bind("<FocusIn>", self._on_focus_in, add="+")

        # Intercept window close so we can prompt to save the session.
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    def _route_to_equip_if_empty(self):
        """Switch to the Equip tab if the user has no scopes or cameras.

        Runs early in startup to sidestep the macOS Cocoa Tk paint stall
        on the empty-state Planner tab, and to land the user on the tab
        they need anyway: you can't plan a session without equipment.
        Only reroutes if the inventory is genuinely empty — users with
        saved gear start on the Planner as before.
        """
        scopes  = self.data.get("scopes", {})
        cameras = self.data.get("cameras", {})
        if not scopes or not cameras:
            try:
                # Find the Equip entry in the sidebar tab list.  Using
                # _sidebar_select (not tab_control.select directly) so the
                # sidebar highlight tracks the active tab correctly.
                for i, (_, tab, _) in enumerate(self._sidebar_tabs):
                    if tab is self.tab_equip:
                        self._sidebar_select(i)
                        break
            except Exception:
                pass

    def _auto_detect_location_if_unset(self):
        """Fire off a background IP-geolocation lookup if no location is saved.

        Runs on a daemon thread so the 5-second-per-provider timeout in
        ``_autodetect_location`` can't block the UI.  On success, schedules
        the save + header refresh back on the main thread via ``after(0)``.
        On failure, silently leaves the location unset — the user can set
        it manually in Settings whenever they need it.
        """
        lat, lon = self._get_saved_location()
        if lat is not None and lon is not None:
            return   # Already set — respect the user's existing value

        def _bg():
            new_lat, new_lon = self._autodetect_location()
            if new_lat is None:
                return   # Both providers failed; no-op
            def _apply():
                # Re-check in case the user entered a manual location while
                # the background detection was running.
                existing_lat, existing_lon = self._get_saved_location()
                if existing_lat is not None:
                    return
                self.data["location"] = {"lat": new_lat, "lon": new_lon}
                self._persist_data()
                self.refresh_twilight_header()
                self.refresh_moon_header()
                # If the settings tab was already built, update its fields too
                try:
                    if hasattr(self, "_settings_lat_var"):
                        self._settings_lat_var.set(f"{new_lat:.4f}")
                        self._settings_lon_var.set(f"{new_lon:.4f}")
                    if hasattr(self, "_settings_loc_status"):
                        self._settings_loc_status.config(
                            text="✅ Estimated from IP — verify in Settings",
                            foreground="#f59e0b")
                except Exception:
                    pass
            self.root.after(0, _apply)

        threading.Thread(target=_bg, daemon=True).start()

    def show_welcome_message(self):
        """Pop up the first-run welcome message directing the user to Manage Equipment."""
        messagebox.showinfo("Welcome!", 
            "Welcome to Lightbucket Astro Planner!\n\n"
            "Your inventory is currently empty. Add at least one camera and "
            "one telescope on this tab to get started, then switch to the "
            "Planner tab to begin planning a session.\n\n"
            "Your observer location is being estimated from your IP address "
            "in the background — you can verify or change it on the Settings "
            "tab.\n\n"
            "Tip: if the entry fields below appear blank, click once in this "
            "tab to activate them.")
        # Kick the currently-visible tab's widgets after the dialog closes.
        # This paints Canvas/Text widgets reliably; Entry/Treeview widgets
        # on Cocoa Tk occasionally still require a click (see docstring
        # in _kick_paint) — the welcome hint above covers that case.
        try:
            current = self.tab_control.select()
            if current == str(self.tab_equip):
                self.root.after(100, self._kick_equip_paint)
            elif current == str(self.tab_planner):
                self.root.after(100, self._kick_planner_paint)
        except Exception:
            pass

    def _on_close(self):
        """Handle the window-close event — prompt to save the session if plan has entries."""
        if self._plan_entries:
            resp = messagebox.askyesnocancel(
                "Save Session?",
                f"Tonight's plan has {len(self._plan_entries)} target(s).\n\n"
                "Would you like to save the session before closing?")
            if resp is None:
                # Cancel — don't close
                return
            if resp:
                # Yes — save; abort close if user cancels the file picker or save fails
                if not self._save_session_as():
                    return
        self._close_skymap()
        self.root.destroy()

    def _close_skymap(self):
        """Close the sky-map viewer window when the planner exits.

        The local static server runs in a daemon thread and stops with the
        process, but the viewer is an independent child process, so it must be
        terminated explicitly or it would outlive the planner.
        """
        proc = getattr(self, "_skymap_proc", None)
        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
            except Exception:
                pass
        self._skymap_proc = None

    # ═══════════════════════════════════════════════════════════════════
    # SIDEBAR NAVIGATION
    # ═══════════════════════════════════════════════════════════════════

    def _build_sidebar(self):
        """Build the vertical icon sidebar that replaces horizontal notebook tabs."""
        SB_BG = "#131f2e"

        self._sidebar_btns = []
        self._sidebar_active_idx = 0

        # Tab mapping: (label, tab_frame, icon_name)
        self._sidebar_tabs = [
            ("Planner",  self.tab_planner, "crosshair"),
            ("Targets",  self.tab_session, "star_list"),
            ("Tonight's\nPlan",  self.tab_plan,    "moon"),
            ("Equip",    self.tab_equip,   "telescope"),
        ]
        self._sidebar_settings_tab = ("Settings", self.tab_settings, "gear")

        top_frame = tk.Frame(self._sidebar, bg=SB_BG)
        top_frame.pack(side="top", fill="x", pady=(10, 0))

        for i, (label, tab, icon) in enumerate(self._sidebar_tabs):
            btn_frame = tk.Frame(top_frame, bg=SB_BG, cursor="hand2")
            btn_frame.pack(pady=2, padx=6)

            icon_cv = tk.Canvas(btn_frame, width=50, height=50,
                                bg=SB_BG, highlightthickness=0, cursor="hand2")
            icon_cv.pack()

            # Store references
            self._sidebar_btns.append((icon_cv, btn_frame, label, icon, tab, i))

            # Bind click
            for w in (icon_cv, btn_frame):
                w.bind("<Button-1>", lambda e, idx=i: self._sidebar_select(idx))

        # Settings button at bottom
        bot_frame = tk.Frame(self._sidebar, bg=SB_BG)
        bot_frame.pack(side="bottom", fill="x", pady=(0, 10))
        settings_frame = tk.Frame(bot_frame, bg=SB_BG, cursor="hand2")
        settings_frame.pack(pady=2, padx=6)
        settings_cv = tk.Canvas(settings_frame, width=50, height=50,
                                bg=SB_BG, highlightthickness=0, cursor="hand2")
        settings_cv.pack()
        settings_idx = len(self._sidebar_tabs)
        self._sidebar_btns.append((settings_cv, settings_frame,
                                   self._sidebar_settings_tab[0],
                                   self._sidebar_settings_tab[1],
                                   self._sidebar_settings_tab[2],
                                   settings_idx))
        for w in (settings_cv, settings_frame):
            w.bind("<Button-1>", lambda e, idx=settings_idx: self._sidebar_select(idx))

        self._sidebar_draw_all()
        # Select first tab
        self._sidebar_select(0)

    def _sidebar_draw_btn(self, i):
        """Redraw a single sidebar button by index."""
        nm = getattr(self, "night_mode", False)
        SB_BG = "#1a0000" if nm else "#131f2e"
        SB_ACTIVE = "#cc0000" if nm else "#38bdf8"
        SB_ICON_FG = "#882222" if nm else "#7eb8d4"
        R = 8

        btn_data = self._sidebar_btns[i]
        cv = btn_data[0]
        if i < len(self._sidebar_tabs):
            label, tab, icon = self._sidebar_tabs[i]
        else:
            label, tab, icon = self._sidebar_settings_tab
        is_active = (i == self._sidebar_active_idx)

        cv.delete("all")
        cv.configure(bg=SB_BG)
        W, H = 50, 50

        if is_active:
            cv.create_oval(0, 0, 2*R, 2*R, fill=SB_ACTIVE, outline="")
            cv.create_oval(W-2*R, 0, W, 2*R, fill=SB_ACTIVE, outline="")
            cv.create_oval(0, H-2*R, 2*R, H, fill=SB_ACTIVE, outline="")
            cv.create_oval(W-2*R, H-2*R, W, H, fill=SB_ACTIVE, outline="")
            cv.create_rectangle(R, 0, W-R, H, fill=SB_ACTIVE, outline="")
            cv.create_rectangle(0, R, W, H-R, fill=SB_ACTIVE, outline="")
            icon_color = "#0e1a28" if not nm else "#1a0000"
            label_color = icon_color
            bg_fill = SB_ACTIVE
        else:
            icon_color = SB_ICON_FG
            label_color = SB_ICON_FG
            bg_fill = SB_BG

        # Draw line-art icon
        cx, cy_icon = W // 2, H // 2 - 6
        self._sidebar_draw_icon(cv, icon, icon_color, cx, cy_icon, bg_fill)

        # Label below icon — shift up slightly for multi-line labels so
        # the second line doesn't bleed past the canvas into the next button.
        _label_y = H // 2 + (13 if "\n" in label else 17)
        cv.create_text(W // 2, _label_y, text=label,
                       fill=label_color, font=("Helvetica", 8), justify="center")

    def _sidebar_draw_icon(self, cv, icon_name, color, cx, cy, bg_fill):
        """Draw a line-art icon centred at (cx, cy) on the given canvas."""
        if icon_name == "crosshair":
            # Targeting reticle — outer ring, centre dot, crosshair lines
            cv.create_oval(cx-8, cy-8, cx+8, cy+8, outline=color, width=1.5)
            cv.create_oval(cx-2, cy-2, cx+2, cy+2, fill=color, outline="")
            cv.create_line(cx, cy-12, cx, cy-9, fill=color, width=1.5)
            cv.create_line(cx, cy+9, cx, cy+12, fill=color, width=1.5)
            cv.create_line(cx-12, cy, cx-9, cy, fill=color, width=1.5)
            cv.create_line(cx+9, cy, cx+12, cy, fill=color, width=1.5)

        elif icon_name == "star_list":
            # Three-row "list of targets" — each row is a small four-point
            # star bullet (centre dot + cross spikes) followed by a short
            # horizontal line, evoking a list of celestial objects.
            for row_y in (cy - 8, cy, cy + 8):
                # Star spikes
                cv.create_line(cx - 10, row_y - 3, cx - 10, row_y + 3,
                               fill=color, width=1)
                cv.create_line(cx - 13, row_y, cx - 7, row_y,
                               fill=color, width=1)
                # Star centre dot
                cv.create_oval(cx - 11, row_y - 1, cx - 9, row_y + 1,
                               fill=color, outline="")
                # Row line
                cv.create_line(cx - 3, row_y, cx + 10, row_y,
                               fill=color, width=1.5)

        elif icon_name == "moon":
            # Crescent moon with stars
            cv.create_oval(cx-9, cy-9, cx+5, cy+9, outline=color, width=1.5)
            # Overlay to carve crescent
            cv.create_oval(cx-3, cy-10, cx+11, cy+10, fill=bg_fill, outline=bg_fill)
            # Stars
            cv.create_oval(cx+6, cy-7, cx+8, cy-5, fill=color, outline="")
            cv.create_oval(cx+3, cy-11, cx+5, cy-9, fill=color, outline="")

        elif icon_name == "telescope":
            # Telescope on mount — lens, tube, tripod legs, crossbar
            cv.create_oval(cx-4, cy-11, cx+4, cy-4, outline=color, width=1.5)
            cv.create_line(cx, cy-4, cx, cy+2, fill=color, width=1.5)
            cv.create_line(cx, cy+2, cx-7, cy+11, fill=color, width=1.5)
            cv.create_line(cx, cy+2, cx+7, cy+11, fill=color, width=1.5)
            cv.create_line(cx-4, cy+7, cx+4, cy+7, fill=color, width=1.5)

        elif icon_name == "gear":
            # Gear cog — inner circle + radiating teeth
            cv.create_oval(cx-4, cy-4, cx+4, cy+4, outline=color, width=1.5)
            for angle_deg in range(0, 360, 45):
                rad = math.radians(angle_deg)
                x1 = cx + 6 * math.cos(rad)
                y1 = cy + 6 * math.sin(rad)
                x2 = cx + 10 * math.cos(rad)
                y2 = cy + 10 * math.sin(rad)
                cv.create_line(x1, y1, x2, y2, fill=color, width=2.5,
                               capstyle="round")

    def _sidebar_draw_all(self):
        """Redraw all sidebar buttons with current active state."""
        for i in range(len(self._sidebar_btns)):
            self._sidebar_draw_btn(i)

    def _sidebar_select(self, idx):
        """Select a sidebar tab by index, updating the Notebook and sidebar visuals."""
        old_idx = self._sidebar_active_idx
        if idx == old_idx:
            return  # already active — no-op
        self._sidebar_active_idx = idx

        # Only redraw the two changed buttons (not all 5)
        self._sidebar_draw_btn(old_idx)
        self._sidebar_draw_btn(idx)

        # Flag so _on_tab_changed doesn't redundantly redraw sidebar
        self._sidebar_switching = True
        if idx < len(self._sidebar_tabs):
            _, tab, _ = self._sidebar_tabs[idx]
        else:
            _, tab, _ = self._sidebar_settings_tab
        self.tab_control.select(tab)
        self._sidebar_switching = False

    # ═══════════════════════════════════════════════════════════════════
    # DATA STORAGE (JSON gear file + NGC catalog)
    # ═══════════════════════════════════════════════════════════════════

    def get_data_path(self):
        """Return the path to astro_gear.json, creating the parent folder if needed."""
        base_dir = Path(os.getenv('LOCALAPPDATA')) / "LightbucketAstroPlanner" if platform.system() == "Windows" else Path.home() / "LightbucketAstroPlanner"
        base_dir.mkdir(parents=True, exist_ok=True)
        return base_dir / "astro_gear.json"

    def _install_crash_logger(self):
        """Route uncaught exceptions to ``<data_dir>/crash.log``.

        Two hooks are installed because they catch different failure modes:

          * ``sys.excepthook`` — catches exceptions that bubble out of the
            main thread's call stack (e.g. a crash during startup, or in
            code invoked from mainloop but not wrapped in a Tk callback).

          * ``tk.Tk.report_callback_exception`` — Tk catches exceptions
            raised by widget callbacks (button ``command=``, ``bind``
            handlers, ``after`` callbacks) internally and prints them to
            stderr, so they never reach ``sys.excepthook``.  Overriding
            this on the root instance redirects them to our log instead.

        Errors from the logger itself are swallowed — we don't want a
        write failure in the crash handler to mask the original crash.
        """
        log_path = self.data_path.parent / "crash.log"

        def _write(exc_type, exc_value, tb, source):
            try:
                with open(log_path, "a", encoding="utf-8") as f:
                    f.write("=" * 72 + "\n")
                    f.write(f"{datetime.now().isoformat(timespec='seconds')}  "
                            f"[{source}]  {platform.system()} "
                            f"Python {sys.version.split()[0]}  "
                            f"Lightbucket Astro Planner v{__version__}\n")
                    traceback.print_exception(exc_type, exc_value, tb, file=f)
                    f.write("\n")
            except Exception:
                pass

        def _sys_hook(exc_type, exc_value, tb):
            _write(exc_type, exc_value, tb, "sys.excepthook")
            # Preserve the default behaviour in dev so tracebacks still
            # appear on the console when running from a terminal.
            sys.__excepthook__(exc_type, exc_value, tb)

        def _tk_hook(exc_type, exc_value, tb):
            _write(exc_type, exc_value, tb, "tk.callback")

        sys.excepthook = _sys_hook
        self.root.report_callback_exception = _tk_hook

    def ensure_catalog_exists(self):
        """Download the NGC catalog if missing, else fall back to the built-in Messier list.

        Also downloads the OpenNGC addendum (small file containing notable
        objects outside NGC/IC — supplies C9/C14/C41/C99 plus M40 and the
        Pleiades).  During the same one-time download pass, Messier AND
        Caldwell cross-references are written into the main NGC.csv's
        Common-names column so the existing alias system surfaces them
        automatically on every subsequent load.
        """
        if not self.catalog_path.exists():
            # ── Try to download the full NGC catalog ──────────────────────────
            try:
                urllib.request.urlretrieve(NGC_URL, self.catalog_path)
                with urllib.request.urlopen(MESSIER_XREF_URL) as response:
                    html = response.read().decode('utf-8', errors='ignore')

                xref_matches = re.findall(r'M\s*(\d+).*?(NGC|IC)\s*(\d+)', html, re.DOTALL | re.IGNORECASE)
                messier_map = {f"{c.upper()}{int(n)}": f"M{m}" for m, c, n in xref_matches}

                # Caldwell xref map: same lookup-key shape as messier_map.
                # Keys look like "NGC188" or "IC342", values like "C1".
                caldwell_map = {}
                for c_num, ref in CALDWELL_TABLE:
                    m = re.match(r'([A-Z]+)\s*(\d+)', ref.upper())
                    if m:
                        caldwell_map[f"{m.group(1)}{int(m.group(2))}"] = f"C{c_num}"

                updated_rows = []
                with open(self.catalog_path, 'r', encoding='utf-8-sig') as f:
                    reader = csv.DictReader(f, delimiter=';')
                    fieldnames = reader.fieldnames
                    for row in reader:
                        match = re.match(r'([A-Z]+)\s*(\d+)', row['Name'].strip().upper())
                        if match:
                            cat_type, cat_num = match.groups()
                            lookup_key = f"{cat_type}{int(cat_num)}"
                            existing_common = row.get('Common names', '').strip()
                            existing_upper = existing_common.upper()

                            # Inject Messier designation if applicable
                            if lookup_key in messier_map:
                                m_designation = messier_map[lookup_key]
                                if m_designation.upper() not in existing_upper:
                                    existing_common = f"{existing_common}{'; ' if existing_common else ''}{m_designation}"
                                    existing_upper = existing_common.upper()

                            # Inject Caldwell designation if applicable
                            if lookup_key in caldwell_map:
                                c_designation = caldwell_map[lookup_key]
                                if c_designation.upper() not in existing_upper:
                                    existing_common = f"{existing_common}{'; ' if existing_common else ''}{c_designation}"

                            row['Common names'] = existing_common
                        updated_rows.append(row)

                with open(self.catalog_path, 'w', encoding='utf-8', newline='') as f:
                    writer = csv.DictWriter(f, fieldnames=fieldnames, delimiter=';')
                    writer.writeheader()
                    writer.writerows(updated_rows)

            except Exception:
                # ── Download failed — write the built-in Messier fallback ─────
                # Delete any partial download so we don't leave a corrupt file.
                try:
                    self.catalog_path.unlink(missing_ok=True)
                except Exception:
                    pass
                try:
                    _write_fallback_catalog(self.catalog_path)
                except Exception as e:
                    messagebox.showerror("Catalog Error",
                        f"Could not load catalog:\n{e}")
                    return
                messagebox.showwarning(
                    "Offline — Limited Catalog",
                    "The full NGC catalog could not be downloaded.\n\n"
                    "A built-in catalog of all 110 Messier objects has been "
                    "loaded so you can still plan sessions.\n\n"
                    "Connect to the internet and restart the app to download "
                    "the full 13,000-object NGC/IC catalog.")

        # ── Try to download the OpenNGC addendum (small, ~16 KB) ──────────
        # Failure is non-fatal: only 64 objects involved, and the only
        # observably-missing Caldwell entries without it are C9/C14/C41/C99.
        if not self.addendum_path.exists():
            try:
                urllib.request.urlretrieve(ADDENDUM_URL, self.addendum_path)
            except Exception:
                # Silent — the rest of the catalog still works fine without it.
                try:
                    self.addendum_path.unlink(missing_ok=True)
                except Exception:
                    pass

        self.load_target_catalog()

        # The catalog is now available.  If a previous session was restored by
        # refresh_dropdowns() (which ran before the catalog existed), the
        # initial analysis would have failed silently because self.targets was
        # empty.  Mark dirty and schedule so the planner populates on startup.
        sess = self.data.get("session", {})
        if sess.get("target") and sess.get("scope") and sess.get("camera"):
            self._mark_analysis_dirty()

    # Current gear-file schema version.  Bump this and add a new migration
    # step in _migrate_data whenever the shape of astro_gear.json changes.
    _DATA_SCHEMA = 6

    def load_data(self):
        """Load the gear JSON file, applying schema migrations; return an empty default on any failure."""
        if self.data_path.exists():
            try:
                with open(self.data_path, 'r') as f:
                    data = json.load(f)
                data = self._migrate_data(data)
                return data
            except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError):
                # Corrupted or unreadable file — fall through to the empty default
                # below so the app can still start and the user can re-enter data.
                pass
        return self._empty_data()

    @staticmethod
    def _empty_data():
        """Return the canonical empty gear-file structure with current schema version."""
        return {
            "schema":       AstroApp._DATA_SCHEMA,
            "cameras":      {},
            "scopes":       {},
            "location":     {"lat": None, "lon": None},
            "locations":    [],
            "active_location": "",
            "session":      {},
            "nina_filters": {},
            "settings":     {"rigs": [], "active_rig": ""},
        }

    @staticmethod
    def _migrate_data(data):
        """Apply forward migrations so older gear files work with the current app.

        Each migration is guarded by a version check, runs in order, and bumps
        the schema number so the same migration is never applied twice.

        Migration history
        -----------------
        schema 0 → 1  (Beta1)
            • Rename ``eff_fl`` → ``native_fl`` in scope records
            • Ensure ``location``, ``session``, ``nina_filters``, ``settings``
              top-level keys exist
            • Stamp ``schema: 1``
        schema 1 → 2  (Beta4_5)
            • Add ``settings["rigs"]`` (list) and ``settings["active_rig"]`` (str)
              for named rig presets
            • Stamp ``schema: 2``
        schema 2 → 3  (Beta4_5)
            • Rewrite filter_mode strings from ``"Narrowband (Xnm)"`` to
              ``"NB (Xnm)"`` in the saved session and all rig snapshots
              (UI dropdown was abbreviated to free up row space)
            • Stamp ``schema: 3``
        schema 3 → 4  (Beta4_5)
            • Rewrite filter_mode strings from ``"None (Luminance)"`` to
              ``"Mono Lum"`` in the saved session and all rig snapshots
              (UI dropdown was further abbreviated to fit one row on macOS)
            • Stamp ``schema: 4``
        schema 4 → 5  (Beta4_5)
            • Blank the ``filter`` field on rig snapshots whose camera is a
              color sensor — the filter dropdown is hidden for color cameras
              so any non-empty value stored there is a stale leftover from
              the StringVar before the camera was switched.
            • Stamp ``schema: 5``
        schema 5 → 6  (named locations)
            • Add top-level ``locations`` (list) and ``active_location`` (str)
              for named location profiles; fold the existing single
              ``location`` into a default "Home" profile.
            • Stamp ``schema: 6``
        """
        v = data.get("schema", 0)

        if v < 1:
            # ── 0 → 1: rename legacy scope key, ensure top-level sections ──
            if "scopes" in data:
                for scope in data["scopes"].values():
                    if "eff_fl" in scope and "native_fl" not in scope:
                        scope["native_fl"] = scope["eff_fl"]
            data.setdefault("location",     {"lat": None, "lon": None})
            data.setdefault("session",      {})
            data.setdefault("nina_filters", {})
            data.setdefault("settings",     {})
            data["schema"] = 1
            v = 1

        if v < 2:
            # ── 1 → 2: add rig-preset storage ──
            settings = data.setdefault("settings", {})
            settings.setdefault("rigs", [])
            settings.setdefault("active_rig", "")
            data["schema"] = 2
            v = 2

        if v < 3:
            # ── 2 → 3: abbreviate "Narrowband (Xnm)" → "NB (Xnm)" ──
            def _abbrev(fm):
                if isinstance(fm, str) and fm.startswith("Narrowband "):
                    return "NB " + fm[len("Narrowband "):]
                return fm
            sess = data.get("session", {})
            if "filter_mode" in sess:
                sess["filter_mode"] = _abbrev(sess["filter_mode"])
            for rig in data.get("settings", {}).get("rigs", []):
                if "filter" in rig:
                    rig["filter"] = _abbrev(rig["filter"])
            data["schema"] = 3
            v = 3

        if v < 4:
            # ── 3 → 4: abbreviate "None (Luminance)" → "Mono Lum" ──
            sess = data.get("session", {})
            if sess.get("filter_mode") == "None (Luminance)":
                sess["filter_mode"] = "Mono Lum"
            for rig in data.get("settings", {}).get("rigs", []):
                if rig.get("filter") == "None (Luminance)":
                    rig["filter"] = "Mono Lum"
            data["schema"] = 4
            v = 4

        if v < 5:
            # ── 4 → 5: blank filter field on rigs with color cameras ──
            cameras = data.get("cameras", {})
            for rig in data.get("settings", {}).get("rigs", []):
                cam = cameras.get(rig.get("camera", ""), {})
                if cam.get("is_color", True):   # color sensor (or camera missing)
                    rig["filter"] = ""
            data["schema"] = 5
            v = 5

        if v < 6:
            # ── 5 → 6: introduce named location profiles ──
            # Fold any existing single observer location into a default
            # "Home" profile so dark-site travelers can save more sites.
            data.setdefault("locations", [])
            data.setdefault("active_location", "")
            loc = data.get("location", {}) or {}
            if (not data["locations"]
                    and loc.get("lat") is not None and loc.get("lon") is not None):
                data["locations"].append(
                    {"name": "Home", "lat": loc["lat"], "lon": loc["lon"]})
                data["active_location"] = "Home"
            data["schema"] = 6
            v = 6

        # Future migrations go here:
        # if v < 7:
        #     ...
        #     data["schema"] = 7
        #     v = 7

        return data

    def _persist_data(self):
        """Write self.data to disk with schema stamp — no UI refresh.

        Use this instead of :meth:`save_data` when the caller will handle
        its own UI updates (or when triggering ``refresh_dropdowns`` would
        cause re-entrancy through parameter-change traces).
        """
        self.data["schema"] = self._DATA_SCHEMA
        with open(self.data_path, 'w') as f:
            json.dump(self.data, f, indent=4)

    def save_data(self):
        """Persist the gear JSON file, then refresh inventory tables and dropdowns."""
        self._persist_data()
        self.refresh_inventory_tables()
        self.refresh_dropdowns()

    # ═══════════════════════════════════════════════════════════════════
    # HEADER BAR (logo, title, twilight, moon, planning-date picker)
    # ═══════════════════════════════════════════════════════════════════

    def setup_header(self):
        """Build the top header bar: logo, title, twilight/moon readouts, planning-date picker."""
        HDR_BG  = "#1e2d3e"   # dark navy
        SEP_COL = "#2e4a63"   # separator / accent-line colour
        ACC_FG  = "#7eb8d4"   # light steel-blue for section titles

        # Outer wrapper gives us a clean bottom-border line
        wrapper = tk.Frame(self.root, bg=HDR_BG)
        wrapper.pack(fill="x", padx=0, pady=0)
        tk.Frame(wrapper, bg=SEP_COL, height=2).pack(side="bottom", fill="x")

        header = tk.Frame(wrapper, bg=HDR_BG)
        header.pack(fill="x")

        # ── Logo ──────────────────────────────────────────────────────────
        # Use _resource_path so logo.png is found both in dev (next to this
        # source file) and when packaged with PyInstaller (extracted into
        # sys._MEIPASS at launch).  Bundle the logo via:
        #     pyinstaller --add-data "logo.png:." AstroHelperBeta7.py     (macOS/Linux)
        #     pyinstaller --add-data "logo.png;." AstroHelperBeta7.py     (Windows)
        try:
            _logo_path = _resource_path("logo.png")
            if _logo_path.exists():
                img = Image.open(_logo_path).resize((80, 80), Image.Resampling.LANCZOS)
                self.logo_img = ImageTk.PhotoImage(img)
                tk.Label(header, image=self.logo_img, bg=HDR_BG).pack(
                    side="left", padx=(16, 8), pady=10)
        except (OSError, tk.TclError, AttributeError):
            # logo.png missing, unreadable, or Pillow can't decode it — the
            # app runs fine without the logo.
            pass

        # ── App title ─────────────────────────────────────────────────────
        tk.Label(header, text="LIGHTBUCKET\nASTRO PLANNER",
                 font=("Helvetica", 13, "bold"), justify="left",
                 bg=HDR_BG, fg="#ffffff").pack(side="left", padx=(0, 4), pady=10)

        # ── Right-side button group (packed before left items to anchor right) ──
        right_frame = tk.Frame(header, bg=HDR_BG)
        right_frame.pack(side="right", padx=(8, 16), pady=10)

        self.night_mode = False
        self.night_btn = ttk.Button(right_frame, text="🔴 Night Mode",
                                    command=self.toggle_night_mode, style="Night.TButton")
        self.night_btn.pack(fill="x", pady=(0, 5))
        ToolTip(self.night_btn, "Toggle red night-vision mode to preserve\nyour dark adaptation while observing")

        data_btn = ttk.Button(right_frame, text="📁 Open Data Folder",
                   command=self.open_data_folder, style="HDR.TButton")
        data_btn.pack(fill="x", pady=(0, 5))
        ToolTip(data_btn, "Open the folder where your equipment\nprofiles and catalog are stored")

        clearoutside_btn = ttk.Button(right_frame, text="🌤 Clear Outside",
                   command=self.open_clearoutside, style="HDR.TButton")
        clearoutside_btn.pack(fill="x")
        ToolTip(clearoutside_btn, "Open Clear Outside forecast for your\nsaved observer location")

        # Helper: thin vertical separator between header sections
        def _sep(px=18):
            tk.Frame(header, bg=SEP_COL, width=1).pack(
                side="left", fill="y", padx=(px, px), pady=8)

        # ── Astronomical Twilight ──────────────────────────────────────────
        _sep(px=8)
        twi_frame = tk.Frame(header, bg=HDR_BG)
        twi_frame.pack(side="left", pady=10)

        twi_title_row = tk.Frame(twi_frame, bg=HDR_BG)
        twi_title_row.pack(anchor="w")
        tk.Label(twi_title_row, text="ASTRONOMICAL TWILIGHT",
                 font=("Helvetica", 9, "bold"), bg=HDR_BG, fg=ACC_FG).pack(side="left")

        self.twi_label = tk.Label(twi_frame, text="⏳ Calculating…",
                                  font=("Helvetica", 14), bg=HDR_BG, fg="#ffffff",
                                  width=32, anchor="w")
        self.twi_label.pack(anchor="w", pady=(3, 1))
        self.twi_date_label = tk.Label(twi_frame, text="",
                                       font=("Helvetica", 9), bg=HDR_BG, fg="#a0b8cc",
                                       width=52, anchor="w")
        self.twi_date_label.pack(anchor="w")

        # ── Moon Phase ────────────────────────────────────────────────────
        _sep(px=8)
        moon_frame = tk.Frame(header, bg=HDR_BG)
        moon_frame.pack(side="left", pady=10)

        tk.Label(moon_frame, text="MOON PHASE",
                 font=("Helvetica", 9, "bold"), bg=HDR_BG, fg=ACC_FG).pack(anchor="w")

        moon_body = tk.Frame(moon_frame, bg=HDR_BG)
        moon_body.pack(anchor="w", pady=(2, 0))

        self.moon_emoji_label = tk.Label(moon_body, text="🌑",
                                         font=("Helvetica", 28), bg=HDR_BG, fg="#ffffff")
        self.moon_emoji_label.pack(side="left", padx=(0, 8))

        moon_text_frame = tk.Frame(moon_body, bg=HDR_BG)
        moon_text_frame.pack(side="left")
        self.moon_name_label = tk.Label(moon_text_frame, text="",
                                        font=("Helvetica", 12, "bold"), bg=HDR_BG, fg="#ffffff",
                                        width=22, anchor="w")
        self.moon_name_label.pack(anchor="w")
        self.moon_detail_label = tk.Label(moon_text_frame, text="",
                                          font=("Helvetica", 9), bg=HDR_BG, fg="#ffffff",
                                          width=28, anchor="w")
        self.moon_detail_label.pack(anchor="w", pady=(1, 0))
        self.moon_riseset_label = tk.Label(moon_text_frame, text="",
                                           font=("Helvetica", 9), bg=HDR_BG, fg="#aaddff",
                                           width=30, anchor="w")
        self.moon_riseset_label.pack(anchor="w")

        # ── Planning Date picker (Option C) ───────────────────────────────
        # Layout: tall ◀ button | center tile (mini-label + date) | tall ▶ button
        # with a "↩ Today" link sitting beneath the whole group.
        _sep()
        date_frame = tk.Frame(header, bg=HDR_BG)
        date_frame.pack(side="left", pady=10)

        nav_row = tk.Frame(date_frame, bg=HDR_BG)
        nav_row.pack(anchor="w")

        def _make_arrow(parent, text, cmd):
            lbl = tk.Label(parent, text=text, bg="#2a3f55", fg="#ffffff",
                           font=("Helvetica", 8, "bold"), padx=4, pady=3, cursor="hand2")
            lbl.bind("<Button-1>",        lambda e: cmd())
            lbl.bind("<Enter>",           lambda e: lbl.configure(bg="#3a5a7a"))
            lbl.bind("<Leave>",           lambda e: lbl.configure(
                                              bg="#2a0000" if self.night_mode else "#2a3f55"))
            lbl.bind("<ButtonRelease-1>", lambda e: lbl.configure(
                                              bg="#2a0000" if self.night_mode else "#2a3f55"))
            return lbl

        self._date_prev_btn = _make_arrow(nav_row, "◀", lambda: self._shift_planning_date(-1))
        self._date_prev_btn.pack(side="left")

        # Center tile: mini section-label on top, editable date below
        center_tile = tk.Frame(nav_row, bg="#1a2b3c", padx=8, pady=3)
        center_tile.pack(side="left", padx=3)
        tk.Label(center_tile, text="PLANNING DATE",
                 font=("Helvetica", 8, "bold"), bg="#1a2b3c", fg=ACC_FG).pack()

        self._date_entry_var = tk.StringVar(value=datetime.now().strftime("%Y-%m-%d"))
        date_entry = tk.Entry(center_tile, textvariable=self._date_entry_var,
                              width=10, font=("Helvetica", 12),
                              bg="#1a2b3c", fg="#ffffff", insertbackground="#ffffff",
                              relief="flat", justify="center")
        date_entry.pack()
        date_entry.bind("<Return>",   lambda e: self._apply_planning_date())
        date_entry.bind("<FocusOut>", lambda e: self._apply_planning_date())
        ToolTip(date_entry, "Enter a date (YYYY-MM-DD) and press Enter\nto plan for a future night")

        self._date_next_btn = _make_arrow(nav_row, "▶", lambda: self._shift_planning_date(+1))
        self._date_next_btn.pack(side="left")

        self._today_btn = ttk.Button(date_frame, text="↩ Today",
                   command=self._reset_planning_date,
                   style="NavToday.TButton", cursor="hand2")
        self._today_btn.pack(anchor="center", pady=(2, 0))
        ToolTip(self._today_btn, "Return to tonight's date")

    def _apply_planning_date(self):
        """Parse the date entry and set the global planning date, then refresh everything.
        Only triggers a refresh if the date actually changed — prevents the FocusOut
        binding from running a full analyze_framing on every tab switch."""
        global _PLANNING_DATE
        old_effective = _PLANNING_DATE or datetime.now().date()
        raw = self._date_entry_var.get().strip()
        try:
            import datetime as _dt
            parsed = _dt.date.fromisoformat(raw)
            _PLANNING_DATE = parsed
            self._date_entry_var.set(parsed.strftime("%Y-%m-%d"))
        except ValueError:
            active = _PLANNING_DATE or datetime.now().date()
            self._date_entry_var.set(active.strftime("%Y-%m-%d"))
        new_effective = _PLANNING_DATE or datetime.now().date()
        if new_effective != old_effective:
            self._on_planning_date_changed()

    def _shift_planning_date(self, delta_days):
        """Move the planning date forward or backward by delta_days."""
        global _PLANNING_DATE
        import datetime as _dt
        base = _PLANNING_DATE or datetime.now().date()
        _PLANNING_DATE = base + _dt.timedelta(days=delta_days)
        self._date_entry_var.set(_PLANNING_DATE.strftime("%Y-%m-%d"))
        self._on_planning_date_changed()

    def _reset_planning_date(self):
        """Reset to today (live mode)."""
        global _PLANNING_DATE
        _PLANNING_DATE = None
        self._date_entry_var.set(datetime.now().strftime("%Y-%m-%d"))
        self._on_planning_date_changed()

    def _on_planning_date_changed(self):
        """Refresh all date-sensitive displays after a planning date change."""
        self.refresh_twilight_header()
        self.refresh_moon_header()
        if self.auto_update_enabled:
            self._mark_analysis_dirty()

    def refresh_twilight_header(self):
        """Recompute and display astronomical twilight times for tonight."""
        lat, lon = self._get_saved_location()
        today_str = _planning_local_noon().strftime("%a %d %b %Y")
        self.twi_date_label.config(text=today_str)

        if lat is None or lon is None:
            self.twi_label.config(text="📍 No location saved",
                                  fg="#cc4400" if self.night_mode else "#ffcc66")
            return

        dusk, dawn = _calc_twilight(lat, lon, altitude_deg=-18.0)

        if dusk is None:
            # Check for polar night vs midnight sun
            _, check = _calc_twilight(lat, lon, altitude_deg=0.0)
            if check is None and lat > 0:
                msg = "☀️ Midnight sun — no dark"
            else:
                msg = "🌑 Polar night — always dark"
            self.twi_label.config(text=msg,
                                  fg="#cc4400" if self.night_mode else "#ffcc66")
        else:
            self.twi_label.config(
                text=f"🌆 Dusk  {dusk}   ·   🌅 Dawn  {dawn}",
                fg="#ff6633" if self.night_mode else "#ffffff"
            )

        # Also show civil & nautical for context in a tooltip-style update
        c_dusk, c_dawn = _calc_twilight(lat, lon, altitude_deg=-6.0)
        n_dusk, n_dawn = _calc_twilight(lat, lon, altitude_deg=-12.0)
        parts = []
        if c_dusk:
            parts.append(f"Civil {c_dusk}–{c_dawn}")
        if n_dusk:
            parts.append(f"Nautical {n_dusk}–{n_dawn}")
        self.twi_date_label.config(text=f"{today_str}   ({' | '.join(parts)})" if parts else today_str)

    def refresh_moon_header(self):
        """Recompute and display current moon phase + rise/set in the header."""
        name, illum_pct, emoji, age_days, days_to_new, days_to_full = _calc_moon_phase()
        self.moon_emoji_label.config(text=emoji)
        self.moon_name_label.config(text=f"{name}  ({illum_pct}%)")

        if illum_pct >= 50:
            # Waxing toward or past full — show days to next new
            detail = f"Age {age_days}d  ·  New in {days_to_new:.0f}d"
        else:
            detail = f"Age {age_days}d  ·  Full in {days_to_full:.0f}d"

        # Imaging impact hint
        if illum_pct <= 25:
            hint = "🟢 Good for imaging"
        elif illum_pct <= 60:
            hint = "🟡 Moderate interference"
        else:
            hint = "🔴 High interference"

        self.moon_detail_label.config(text=f"{detail}\n{hint}")

        # Moon rise / set times
        lat, lon = self._get_saved_location()
        if lat is not None:
            def _fetch_riseset():
                """Compute moon rise/set (off the UI thread) and push result back via after()."""
                rise, mset = _calc_moon_riseset(lat, lon)
                parts = []
                if rise:
                    parts.append(f"🌕 Rise {rise}")
                if mset:
                    parts.append(f"🌑 Set  {mset}")
                text = "  ·  ".join(parts) if parts else "Above horizon all night" if rise is None and mset is None else "—"
                self.root.after(0, lambda: self.moon_riseset_label.config(text=text))
            threading.Thread(target=_fetch_riseset, daemon=True).start()
        else:
            self.moon_riseset_label.config(text="📍 No location — rise/set unavailable")

    # ═══════════════════════════════════════════════════════════════════
    # DAY / NIGHT THEMING
    # ═══════════════════════════════════════════════════════════════════
    # The app supports a red night-mode overlay for dark-adapted observing.
    # Day-mode is captured in a startup snapshot (_snapshot_original_colors
    # and _snapshot_original_styles) so that toggling back to day doesn't
    # disturb layout — only colours are changed, never geometry.

    def _setup_day_styles(self):
        """Configure ttk styles and plain-tk tab widgets for the dark day-mode palette.
        Called once at startup so the snapshot captures exactly these values."""
        TAB_BG    = "#1e2d3e"   # dark navy — matches the header
        FIELD_BG  = "#263545"   # slightly lighter for entry/combobox fields
        FG        = "#ffffff"   # white text throughout
        SEL_BG    = "#3a5a7a"   # medium blue for selections

        style = ttk.Style()

        # Windows uses the "vista"/"winnative" theme by default, which renders
        # Entry, Combobox and Button widgets using native Win32 controls that
        # completely ignore ttk style colour overrides (fieldbackground, fg, etc.).
        # Switching to "clam" gives us a pure-Python renderer that respects every
        # style option we set — appearance and layout are unchanged; only the
        # colour plumbing now works correctly on Windows.
        #
        # macOS has the same class of problem, but only in light mode: the
        # native aqua theme adapts Entry/Combobox field colors to the OS
        # appearance setting, so our white foreground sits unreadably on a
        # white field. We detect that case at startup via
        # _macos_should_force_clam() and switch to clam only when needed —
        # macOS users in dark mode keep the native aqua look they're used to.
        if platform.system() == "Windows" or _macos_should_force_clam():
            try:
                style.theme_use("clam")
            except tk.TclError:
                pass  # fall back silently if clam is somehow unavailable
        style.configure(".",                 background=TAB_BG,   foreground=FG)
        style.configure("TFrame",            background=TAB_BG)
        style.configure("TLabel",            background=TAB_BG,   foreground=FG)
        style.configure("TLabelframe",       background=TAB_BG,   foreground=FG)
        style.configure("TLabelframe.Label", background=TAB_BG,   foreground=FG)
        style.configure("TButton",           background=FIELD_BG, foreground=FG)
        style.map("TButton",                 background=[("active", SEL_BG)])

        # Named styles for the custom-coloured header buttons — keeping them
        # inside the ttk style system prevents the Tk option-database from
        # resetting their colours on redraws.
        style.configure("Night.TButton",    background="#222222", foreground="#cc0000",
                                            relief="raised", borderwidth=2)
        style.map("Night.TButton",          background=[("active", "#330000")],
                                            foreground=[("active", "#ff0000")])
        style.configure("HDR.TButton",        background="#2a3f55", foreground="#ffffff",
                                            relief="flat", borderwidth=0, padding=4)
        style.map("HDR.TButton",            background=[("active", "#3a5a7a")])
        style.configure("DateNav.TButton",  background="#2a3f55", foreground="#ffffff",
                                            relief="flat", borderwidth=0,
                                            font=("Helvetica", 8, "bold"), padding=(1, 2))
        style.map("DateNav.TButton",        background=[("active", "#3a5a7a")])
        style.configure("Nav.TButton",      background="#2a3f55", foreground="#ffffff",
                                            relief="flat", borderwidth=0,
                                            font=("Helvetica", 13, "bold"), padding=2)
        style.map("Nav.TButton",            background=[("active", "#3a5a7a")])
        style.configure("NavToday.TButton", background="#2a3f55", foreground="#aaddff",
                                            relief="flat", borderwidth=0,
                                            font=("Helvetica", 9), padding=1)
        style.map("NavToday.TButton",       background=[("active", "#3a5a7a")])
        style.configure("TCheckbutton",      background=TAB_BG,   foreground=FG)
        style.configure("TNotebook",         background=TAB_BG)
        # Hide the built-in tab bar — navigation is handled by the sidebar.
        # Belt-and-suspenders: remove tab element from layout AND zero out tab geometry.
        style.layout("TNotebook", [("TNotebook.client", {"sticky": "nswe"})])
        style.configure("TNotebook.Tab", padding=0)
        style.layout("TNotebook.Tab", [])
        style.configure("TNotebook.Tab",     background=FIELD_BG, foreground=FG)
        style.map("TNotebook.Tab",           background=[("selected", SEL_BG)])
        style.configure("TScrollbar",        background=FIELD_BG, troughcolor=TAB_BG)

        style.configure("TCombobox",
                        fieldbackground=FIELD_BG, background=FIELD_BG,
                        foreground=FG, selectbackground=SEL_BG, selectforeground=FG,
                        arrowcolor=FG)
        style.map("TCombobox",
                  fieldbackground=[("readonly", FIELD_BG), ("disabled", FIELD_BG),
                                   ("active",   FIELD_BG), ("focus",    FIELD_BG),
                                   ("",         FIELD_BG)],
                  foreground=[     ("readonly", FG), ("disabled", FG), ("", FG)],
                  selectforeground=[("readonly", FG), ("", FG)],
                  selectbackground=[("readonly", SEL_BG), ("", SEL_BG)],
                  background=[     ("readonly", FIELD_BG), ("", FIELD_BG)])

        style.configure("TEntry",
                        fieldbackground=FIELD_BG, foreground=FG,
                        insertcolor=FG, insertbackground=FG,
                        selectbackground=SEL_BG, selectforeground=FG)
        style.map("TEntry",
                  fieldbackground=[("disabled", FIELD_BG), ("active", FIELD_BG),
                                   ("focus",    FIELD_BG), ("!disabled", FIELD_BG),
                                   ("",         FIELD_BG)],
                  foreground=[     ("disabled", FG), ("focus", FG),
                                   ("!disabled", FG), ("", FG)])

        style.configure("Treeview",         background=FIELD_BG, foreground=FG,
                                            fieldbackground=FIELD_BG)
        style.configure("Treeview.Heading", background=SEL_BG,   foreground=FG)
        style.map("Treeview",               background=[("selected", SEL_BG)])

        # Combobox dropdown popup listbox
        self.root.option_add("*TCombobox*Listbox.background",       FIELD_BG)
        self.root.option_add("*TCombobox*Listbox.foreground",       FG)
        self.root.option_add("*TCombobox*Listbox.selectBackground", SEL_BG)
        self.root.option_add("*TCombobox*Listbox.selectForeground", FG)

        # Plain tk widgets on the tabs (not covered by ttk styles)
        try:
            self.suggestion_list.configure(bg=FIELD_BG, fg=FG,
                                           selectbackground=SEL_BG, selectforeground=FG)
        except Exception:
            pass  # suggestion_list may be hidden
        self.results_txt.configure(bg=FIELD_BG, fg=FG, insertbackground=FG)

    def _snapshot_original_colors(self):
        """Walk the full widget tree and store every tk widget's original colours.
        Called once at startup (after dynamic labels have rendered) so day-mode
        can restore the exact original appearance pixel-for-pixel."""
        _prop_map = {
            "Label":   ("bg", "fg"),
            "Button":  ("bg", "fg", "activebackground", "activeforeground"),
            "Listbox": ("bg", "fg", "selectbackground", "selectforeground"),
            "Text":    ("bg", "fg", "insertbackground"),
            "Canvas":  ("bg",),
            "Entry":   ("bg", "fg", "insertbackground"),
            "Frame":   ("bg",),
        }
        def _snap(widget):
            cls = widget.__class__.__name__
            props = _prop_map.get(cls)
            if props:
                saved = {}
                for p in props:
                    try:
                        saved[p] = widget.cget(p)
                    except Exception:
                        pass
                if saved:
                    self._orig_colors[widget] = saved
                elif cls == "Label" and isinstance(widget, ttk.Label):
                    # ttk.Label — bg/fg shorthand failed; capture the long-form
                    # option names so day-mode restore can clear night overrides.
                    ttk_saved = {}
                    for p in ("foreground", "background"):
                        try:
                            ttk_saved[p] = str(widget.cget(p))
                        except Exception:
                            pass
                    if ttk_saved:
                        self._orig_colors[widget] = ttk_saved
            for child in widget.winfo_children():
                _snap(child)
        _snap(self.root)

    def _snapshot_original_styles(self):
        """Capture every ttk style element's configure() and map() values at startup.
        Night mode overlays colours on top of these; day mode restores them exactly,
        without ever calling theme_use() — so layout, padding and geometry are never disturbed."""
        style = ttk.Style()
        elements = [
            ".", "TFrame", "TLabel", "TLabelframe", "TLabelframe.Label",
            "TButton", "TNotebook", "TNotebook.Tab",
            "Treeview", "Treeview.Heading",
            "TCheckbutton", "TScrollbar", "TCombobox", "TEntry",
            "Night.TButton", "Nav.TButton", "NavToday.TButton", "HDR.TButton", "DateNav.TButton",
        ]
        for el in elements:
            self._orig_styles[el] = {
                "configure": dict(style.configure(el) or {}),
                "map":       dict(style.map(el)       or {}),
            }

    def toggle_night_mode(self):
        """Flip between day (dark navy) and night (red) themes, redrawing the altitude chart."""
        self.night_mode = not self.night_mode
        self._apply_night_mode()
        # Redraw altitude chart with updated text colours
        if self.current_target_info is not None:
            lat, lon = self._get_saved_location()
            if lat is not None:
                self.root.after(10, lambda: self._draw_altitude_chart(
                    self.current_target_info, lat, lon))

    def _apply_night_mode(self):
        """Apply the currently-selected theme to every ttk style and tk widget.

        Night mode overlays red colours on top of the existing theme without
        calling ``theme_use()``.  Day mode restores from the startup colour and
        style snapshots so layout and geometry are preserved exactly.
        """
        style = ttk.Style()

        if self.night_mode:
            # ── NIGHT MODE ────────────────────────────────────────────────────
            bg, fg, entry_bg, select_bg = "#1a0000", "#cc0000", "#200000", "#330000"
            canvas_bg = "#0d0000"

            self.night_btn.configure(text="☀️ Day Mode")
            style.configure("Night.TButton",    background="#330000", foreground="#ff4444")
            style.map("Night.TButton",          background=[("active", "#440000")],
                                                foreground=[("active", "#ff6666")])
            style.configure("HDR.TButton",      background="#2a0000", foreground="#cc0000")
            style.map("HDR.TButton",            background=[("active", "#3a0000")])
            style.configure("DateNav.TButton",  background="#2a0000", foreground="#cc0000")
            style.map("DateNav.TButton",        background=[("active", "#3a0000")])
            style.configure("Nav.TButton",      background="#2a0000", foreground="#cc0000")
            style.map("Nav.TButton",            background=[("active", "#3a0000")])
            style.configure("NavToday.TButton", background="#2a0000", foreground="#993300")
            style.map("NavToday.TButton",       background=[("active", "#3a0000")])

            # Apply colour overrides directly on the current theme — no theme_use()
            # so layout, padding, tab alignment and geometry are completely unchanged.
            style.configure(".",                 background=bg,       foreground=fg)
            style.configure("TFrame",            background=bg)
            style.configure("TLabel",            background=bg,       foreground=fg)
            style.configure("TLabelframe",       background=bg,       foreground=fg)
            style.configure("TLabelframe.Label", background=bg,       foreground=fg)
            style.configure("TButton",           background=entry_bg, foreground=fg)
            style.map("TButton",                 background=[("active", select_bg)])
            style.configure("TNotebook",         background=bg)
            style.configure("TNotebook.Tab",     background=entry_bg, foreground=fg)
            style.map("TNotebook.Tab",           background=[("selected", select_bg)])
            style.configure("Treeview",          background=entry_bg, foreground=fg,
                                                 fieldbackground=entry_bg)
            style.configure("Treeview.Heading",  background=select_bg, foreground=fg)
            style.map("Treeview",                background=[("selected", select_bg)])
            style.configure("TCheckbutton",      background=bg,       foreground=fg)
            style.configure("TScrollbar",        background=entry_bg, troughcolor=bg)

            # Combobox: configure() alone misses "readonly" state — style.map is required
            style.configure("TCombobox",
                            fieldbackground=entry_bg, background=entry_bg,
                            foreground=fg, selectbackground=select_bg, selectforeground=fg,
                            arrowcolor=fg)
            style.map("TCombobox",
                      fieldbackground=[("readonly", entry_bg), ("disabled", entry_bg),
                                       ("active",   entry_bg), ("focus",    entry_bg),
                                       ("",         entry_bg)],
                      foreground=[     ("readonly", fg), ("disabled", fg), ("", fg)],
                      selectforeground=[("readonly", fg), ("", fg)],
                      selectbackground=[("readonly", select_bg), ("", select_bg)],
                      background=[     ("readonly", entry_bg), ("", entry_bg)])

            # Entry fields
            style.configure("TEntry", fieldbackground=entry_bg, foreground=fg,
                            insertcolor=fg, selectbackground=select_bg, selectforeground=fg,
                            insertbackground=fg)
            style.map("TEntry",
                      fieldbackground=[("disabled", entry_bg), ("active", entry_bg),
                                       ("focus",    entry_bg), ("!disabled", entry_bg),
                                       ("",         entry_bg)],
                      foreground=[     ("disabled", fg), ("focus", fg),
                                       ("!disabled", fg), ("", fg)])

            # Combobox dropdown popup listbox (created fresh on each open — needs option_add)
            self.root.option_add("*TCombobox*Listbox.background",       entry_bg)
            self.root.option_add("*TCombobox*Listbox.foreground",       fg)
            self.root.option_add("*TCombobox*Listbox.selectBackground", select_bg)
            self.root.option_add("*TCombobox*Listbox.selectForeground", fg)

            # Walk tk (non-ttk) widgets and recolour them directly
            def _recolor(widget):
                cls = widget.__class__.__name__
                try:
                    if cls in ("Frame", "Toplevel"):
                        widget.configure(bg=bg)
                    elif cls == "Label":
                        try:
                            widget.configure(bg=bg, fg=fg)
                        except tk.TclError:
                            # ttk.Label — bg/fg shorthand not supported; set
                            # foreground as an instance-level override so it
                            # survives Cocoa Tk style resets after messageboxes.
                            try:
                                widget.configure(foreground=fg)
                            except tk.TclError:
                                pass
                        # Fine-tune specific header labels for legibility in red light
                        if widget is self.twi_label:
                            widget.configure(fg="#ff6633")
                        elif widget is self.moon_emoji_label:
                            widget.configure(fg="#cc5500")
                        elif widget is self.moon_name_label:
                            widget.configure(fg="#cc4400")
                        elif widget is self.moon_riseset_label:
                            widget.configure(fg="#993300")
                        elif widget in (self._date_prev_btn, self._date_next_btn):
                            widget.configure(bg="#2a0000", fg="#cc0000")
                    elif cls == "Button":
                        widget.configure(bg=bg, fg=fg, activebackground=select_bg)
                    elif cls == "Listbox":
                        widget.configure(bg=entry_bg, fg=fg,
                                         selectbackground=select_bg, selectforeground=fg)
                    elif cls == "Text":
                        widget.configure(bg=entry_bg, fg=fg, insertbackground=fg)
                        widget.tag_configure("optimal",   foreground="#ff4444", font=("Helvetica", 11, "bold"))
                        widget.tag_configure("warning",   foreground="#ff2200", font=("Helvetica", 11, "bold"))
                        widget.tag_configure("highlight", foreground="#ff6600", font=("Helvetica", 11, "bold"))
                        widget.tag_configure("header",    foreground="#cc0000", font=("Helvetica", 13, "bold"))
                        widget.tag_configure("sec_framing",  foreground="#cc4400", font=("Helvetica", 10, "bold"))
                        widget.tag_configure("sec_exposure", foreground="#cc3300", font=("Helvetica", 10, "bold"))
                        widget.tag_configure("sec_window",   foreground="#cc2200", font=("Helvetica", 10, "bold"))
                        widget.tag_configure("sec_moon",     foreground="#cc1100", font=("Helvetica", 10, "bold"))
                        widget.tag_configure("dim",          foreground="#882222", font=("Helvetica", 11))
                        widget.tag_configure("amber",        foreground="#cc4400", font=("Helvetica", 11, "bold"))
                    elif cls == "Canvas":
                        widget.configure(bg=canvas_bg)
                    elif cls == "Entry":
                        widget.configure(bg=entry_bg, fg=fg, insertbackground=fg)
                except tk.TclError:
                    pass  # ttk widgets raise TclError on bg/fg — handled via style above
                for child in widget.winfo_children():
                    _recolor(child)

            _recolor(self.root)
            self.search_hint_label.configure(foreground="#cc0000")
            self._queue_btn.configure(bg="#1a0000")
            self._draw_queue_btn("#2a0000", "#cc4400", "#cc4400")

            # Rig preset buttons — explicit overrides because the _recolor
            # walker's Label branch applies generic bg/fg but we want the
            # richer red "button-like" palette matching _queue_btn.
            try:
                self.rig_save_btn.configure(bg="#2a0000", fg="#cc4400")
                self.rig_manage_btn.configure(bg="#2a0000", fg="#cc4400")
            except Exception:
                pass

            # Sidebar night mode
            try:
                self._sidebar.configure(bg="#1a0000")
                for btn_data in self._sidebar_btns:
                    cv = btn_data[0]
                    frame = btn_data[1]
                    cv.configure(bg="#1a0000")
                    frame.configure(bg="#1a0000")
                self._sidebar_draw_all()
            except Exception:
                pass

            # Chips bar night mode
            try:
                for w in (self._equip_summary_label,):
                    w.configure(bg="#1a0000", fg="#cc0000")
            except Exception:
                pass

        else:
            # ── DAY MODE — restore exactly to startup state ───────────────────
            self.night_btn.configure(text="🔴 Night Mode")
            # Named header-button styles are restored along with all other ttk styles below

            # Restore every ttk style element from the startup snapshot — no theme_use()
            # so layout is never disturbed in either direction.
            for el, saved in self._orig_styles.items():
                try:
                    if saved["configure"]:
                        style.configure(el, **saved["configure"])
                    if saved["map"]:
                        style.map(el, **saved["map"])
                except Exception:
                    pass

            # Restore Combobox dropdown popup to day colours (dark navy palette —
            # NOT system white/black, since the whole app uses the custom dark theme)
            self.root.option_add("*TCombobox*Listbox.background",       "#263545")
            self.root.option_add("*TCombobox*Listbox.foreground",       "#ffffff")
            self.root.option_add("*TCombobox*Listbox.selectBackground", "#3a5a7a")
            self.root.option_add("*TCombobox*Listbox.selectForeground", "#ffffff")

            # Restore every tk widget's exact original colour from startup snapshot
            for widget, props in self._orig_colors.items():
                try:
                    widget.configure(**props)
                except Exception:
                    pass

            # Explicitly re-pin the date arrow labels and queue button
            for btn in (self._date_prev_btn, self._date_next_btn):
                try:
                    btn.configure(bg="#2a3f55", fg="#ffffff")
                except Exception:
                    pass
            try:
                self._queue_btn.configure(bg="#1e2d3e")
                self._draw_queue_btn("#1e3a5f", "#7eb8d4", "#7eb8d4")
            except Exception:
                pass

            # Restore rig preset buttons to day-mode colours
            try:
                self.rig_save_btn.configure(bg="#1e2d3e", fg="#7eb8d4")
                self.rig_manage_btn.configure(bg="#1e2d3e", fg="#7eb8d4")
            except Exception:
                pass

            # Text-widget tag colours back to day palette
            try:
                self.results_txt.tag_configure("optimal",   foreground="#4caf50", font=("Helvetica", 11, "bold"))
                self.results_txt.tag_configure("warning",   foreground="#ef5350", font=("Helvetica", 11, "bold"))
                self.results_txt.tag_configure("highlight", foreground="#38bdf8", font=("Helvetica", 11, "bold"))
                self.results_txt.tag_configure("header",    foreground="#ffffff", font=("Helvetica", 13, "bold"))
                self.results_txt.tag_configure("sec_framing",  foreground="#38bdf8", font=("Helvetica", 10, "bold"))
                self.results_txt.tag_configure("sec_exposure", foreground="#f59e0b", font=("Helvetica", 10, "bold"))
                self.results_txt.tag_configure("sec_window",   foreground="#4caf50", font=("Helvetica", 10, "bold"))
                self.results_txt.tag_configure("sec_moon",     foreground="#ef5350", font=("Helvetica", 10, "bold"))
                self.results_txt.tag_configure("dim",          foreground="#778899", font=("Helvetica", 11))
                self.results_txt.tag_configure("amber",        foreground="#f59e0b", font=("Helvetica", 11, "bold"))
            except Exception:
                pass
            self.search_hint_label.configure(foreground="#ffffff")

            # Sidebar day mode
            try:
                self._sidebar.configure(bg="#131f2e")
                for btn_data in self._sidebar_btns:
                    cv = btn_data[0]
                    frame = btn_data[1]
                    cv.configure(bg="#131f2e")
                    frame.configure(bg="#131f2e")
                self._sidebar_draw_all()
            except Exception:
                pass

            # Chips bar day mode
            try:
                self._equip_summary_label.configure(bg="#131f2e", fg="#556677")
            except Exception:
                pass

            # Rebuild dynamic card lists (queue cards on the Targets tab,
            # plan cards on Tonight's Plan tab).  These widgets are created
            # after startup so they aren't in _orig_colors and the day-mode
            # restore loop above can't reach them — without this, they'd
            # stay red after a Night → Day toggle.  The refresh methods
            # recreate the tk.Frame / tk.Label widgets with fresh day-mode
            # colors; guarded because _refresh_queue_tree short-circuits if
            # the Targets tab hasn't been built yet.
            try:
                self._refresh_queue_tree()
            except Exception:
                pass
            try:
                self._refresh_plan_tree()
            except Exception:
                pass

    def _on_focus_in(self, event):
        """Re-apply night mode when the root window regains focus.

        macOS Cocoa Tk resets ttk style overrides when a modal dialog
        (messagebox) returns focus to the main window.  This handler
        debounces repeated FocusIn events and re-applies the night theme
        once, 20 ms after the last event — enough for Cocoa to finish
        its own style reset so ours sticks.
        """
        if not self.night_mode:
            return
        if self._night_reapply_id is not None:
            self.root.after_cancel(self._night_reapply_id)
        self._night_reapply_id = self.root.after(20, self._deferred_night_reapply)

    def _deferred_night_reapply(self):
        """Callback for the debounced night-mode re-apply."""
        self._night_reapply_id = None
        if self.night_mode:
            self._apply_night_mode()

    def _theme_popup(self, popup):
        """Apply the current day/night colour theme to a Toplevel popup window.

        ttk styles are global so all ttk widgets inside the popup already inherit
        the correct palette.  What this method does:
          1. Sets the Toplevel background (shows as the window border/gap colour).
          2. Walks any plain-tk widgets (tk.Label, tk.Frame, etc.) and recolours them.
        Call this once, after all widgets have been added to the popup."""
        if self.night_mode:
            bg, fg = "#1a0000", "#cc0000"
        else:
            bg, fg = "#1e2d3e", "#ffffff"

        popup.configure(bg=bg)

        def _walk(widget):
            cls = widget.__class__.__name__
            try:
                if cls in ("Frame", "Toplevel"):
                    widget.configure(bg=bg)
                elif cls == "Label":
                    widget.configure(bg=bg, fg=fg)
                elif cls == "Button":
                    widget.configure(bg=bg, fg=fg)
            except tk.TclError:
                pass   # ttk widgets raise TclError on bg/fg — already handled by style
            for child in widget.winfo_children():
                _walk(child)

        _walk(popup)

    # ═══════════════════════════════════════════════════════════════════
    # EXTERNAL LAUNCHERS (open folders, web forecasts)
    # ═══════════════════════════════════════════════════════════════════

    def open_data_folder(self):
        """Open the ~/LightbucketAstroPlanner data folder in the OS file manager."""
        path = self.data_path.parent
        if platform.system() == "Windows":
            os.startfile(path)
        else:
            subprocess.Popen(["open" if platform.system() == "Darwin" else "xdg-open", str(path)])

    def open_clearoutside(self):
        """Open clearoutside.com forecast pre-filled with the saved observer location."""
        lat, lon = self._get_saved_location()
        if lat is None:
            messagebox.showwarning("No Location Saved",
                "No observer location is saved yet.\n\n"
                "Run 'Visible Tonight' and use Auto-detect (or enter\n"
                "coordinates manually) to save your location first.")
            return
        webbrowser.open(f"https://clearoutside.com/forecast/{lat:.4f}/{lon:.4f}")

    # ═══════════════════════════════════════════════════════════════════
    # TAB BUILDERS (called once from __init__ to construct each tab)
    # ═══════════════════════════════════════════════════════════════════

    def setup_input_tab(self):
        """Build the Manage Equipment tab — camera/scope entry forms + inventory trees."""
        container = ttk.Frame(self.tab_equip)
        container.pack(fill="x")
        container.columnconfigure(0, weight=1)
        container.columnconfigure(1, weight=1)
        cam_frame = ttk.LabelFrame(container, text="Camera Settings")
        cam_frame.grid(row=0, column=0, padx=20, pady=10, sticky="nsew")
        self.cam_entries = {}
        fields = ["Name:", "Pixel Size (μm):", "Read Noise (e-):", "QE (0-1):", "Sensor Width (mm):", "Sensor Height (mm):"]
        for i, f in enumerate(fields):
            ttk.Label(cam_frame, text=f).grid(row=i, column=0, sticky="e", padx=5)
            e = ttk.Entry(cam_frame)
            e.grid(row=i, column=1, padx=5, pady=2)
            self.cam_entries[f] = e
        self.is_color = tk.BooleanVar(value=True)
        ttk.Checkbutton(cam_frame, text="Color Sensor?", variable=self.is_color).grid(row=6, columnspan=2)
        save_cam_btn = ttk.Button(cam_frame, text="Save Camera", command=self.add_camera)
        save_cam_btn.grid(row=7, columnspan=2, pady=5)
        ToolTip(save_cam_btn, "Save this camera to your equipment profile.\nDouble-click a camera in the inventory to edit it.")

        scope_frame = ttk.LabelFrame(container, text="Telescope Settings")
        scope_frame.grid(row=0, column=1, padx=20, pady=10, sticky="nsew")
        self.scope_entries = {}
        for i, f in enumerate(["Name:", "Aperture (mm):", "Native Focal Length (mm):"]):
            ttk.Label(scope_frame, text=f).grid(row=i, column=0, sticky="e", padx=5)
            e = ttk.Entry(scope_frame)
            e.grid(row=i, column=1, padx=5, pady=2)
            self.scope_entries[f] = e
        save_scope_btn = ttk.Button(scope_frame, text="Save Scope", command=self.add_scope)
        save_scope_btn.grid(row=4, columnspan=2, pady=5)
        ToolTip(save_scope_btn, "Save this telescope to your equipment profile.\nDouble-click a scope in the inventory to edit it.")

        inv_frame = ttk.LabelFrame(self.tab_equip, text="Equipment Inventory")
        inv_frame.pack(fill="both", expand=True, padx=20, pady=10)

        self.cam_tree = ttk.Treeview(inv_frame, columns=("Name", "Pixel", "Sensor", "Res", "Type"), show='headings', height=4)
        for col in ("Name", "Pixel", "Sensor", "Res", "Type"):
            self.cam_tree.heading(col, text=col)
        self.cam_tree.pack(fill="x", padx=10, pady=5)
        self.cam_tree.bind("<Double-1>", self.on_camera_select)
        del_cam_btn = ttk.Button(inv_frame, text="Delete Selected Camera", command=lambda: self.delete_item("cameras", self.cam_tree))
        del_cam_btn.pack(padx=10, anchor="w")
        ToolTip(del_cam_btn, "Permanently delete the selected camera\nfrom your equipment profile")

        self.scope_tree = ttk.Treeview(inv_frame, columns=("Name", "Aperture", "Native FL", "Native F-Ratio"), show='headings', height=4)
        for col in ("Name", "Aperture", "Native FL", "Native F-Ratio"):
            self.scope_tree.heading(col, text=col)
        self.scope_tree.pack(fill="x", padx=10, pady=(15, 5))
        self.scope_tree.bind("<Double-1>", self.on_scope_select)
        del_scope_btn = ttk.Button(inv_frame, text="Delete Selected Scope", command=lambda: self.delete_item("scopes", self.scope_tree))
        del_scope_btn.pack(padx=10, anchor="w")
        ToolTip(del_scope_btn, "Permanently delete the selected telescope\nfrom your equipment profile")

    def setup_planner_tab(self):
        """Build the Target Planner tab — equipment chips, target search, FOV preview, results."""
        # ── Equipment chips bar ───────────────────────────────────────────
        ctrl_frame = tk.Frame(self.tab_planner, bg="#131f2e")
        ctrl_frame.pack(padx=0, pady=0, fill="x")
        
        self.scope_choice, self.camera_choice = tk.StringVar(), tk.StringVar()
        self.bortle_choice = tk.StringVar(value=self.data.get("settings", {}).get(
            "default_bortle", "4 (Rural/Suburban)"))
        self.filter_mode = tk.StringVar(value="Mono Lum")
        self.reduction_factor = tk.StringVar(value="1.0×")

        for var in [self.scope_choice, self.camera_choice, self.bortle_choice, self.filter_mode, self.reduction_factor]:
            var.trace_add('write', self.on_parameter_change)

        chips_row = tk.Frame(ctrl_frame, bg="#131f2e")
        chips_row.pack(fill="x", padx=16, pady=8)

        # ── Rig preset chip (selects populate all equipment chips below) ────
        rig_label = tk.Label(chips_row, text="RIG", bg="#131f2e",
                             fg="#cc8833", font=("Helvetica", 9, "bold"))
        rig_label.pack(side="left", padx=(0, 6))

        self.rig_choice = tk.StringVar()
        self.rig_dropdown = ttk.Combobox(chips_row, textvariable=self.rig_choice,
                                          state="readonly", width=16)
        self.rig_dropdown.pack(side="left", padx=(0, 4))
        self.rig_dropdown.bind("<<ComboboxSelected>>", self._on_rig_selected)
        ToolTip(self.rig_dropdown, "Select a saved rig to apply its equipment\n"
                                   "snapshot to the chips. Individual chips\n"
                                   "remain editable — tweaks show as 'Custom…'.")

        self.rig_save_btn = tk.Label(chips_row, text="☆",
                                      bg="#1e2d3e", fg="#7eb8d4",
                                      font=("Helvetica", 11, "bold"),
                                      width=2, cursor="hand2",
                                      relief="flat", borderwidth=1,
                                      padx=4, pady=2)
        self.rig_save_btn.pack(side="left", padx=(0, 2))
        # Hover feedback — swap bg/fg to the "hover" shade
        def _rs_enter(e, _w=self.rig_save_btn):
            _w.configure(bg="#2a0000" if self.night_mode else "#2a5280",
                         fg="#ff6633" if self.night_mode else "#aaddff")
        def _rs_leave(e, _w=self.rig_save_btn):
            _w.configure(bg="#2a0000" if self.night_mode else "#1e2d3e",
                         fg="#cc4400" if self.night_mode else "#7eb8d4")
        self.rig_save_btn.bind("<Enter>",    _rs_enter)
        self.rig_save_btn.bind("<Leave>",    _rs_leave)
        self.rig_save_btn.bind("<Button-1>", lambda e: self._open_save_rig_dialog())
        ToolTip(self.rig_save_btn, "Save the current equipment combination\nas a named rig preset")

        self.rig_manage_btn = tk.Label(chips_row, text="⚙",
                                        bg="#1e2d3e", fg="#7eb8d4",
                                        font=("Helvetica", 11, "bold"),
                                        width=2, cursor="hand2",
                                        relief="flat", borderwidth=1,
                                        padx=4, pady=2)
        self.rig_manage_btn.pack(side="left", padx=(0, 10))
        def _rm_enter(e, _w=self.rig_manage_btn):
            _w.configure(bg="#2a0000" if self.night_mode else "#2a5280",
                         fg="#ff6633" if self.night_mode else "#aaddff")
        def _rm_leave(e, _w=self.rig_manage_btn):
            _w.configure(bg="#2a0000" if self.night_mode else "#1e2d3e",
                         fg="#cc4400" if self.night_mode else "#7eb8d4")
        self.rig_manage_btn.bind("<Enter>",    _rm_enter)
        self.rig_manage_btn.bind("<Leave>",    _rm_leave)
        self.rig_manage_btn.bind("<Button-1>", lambda e: self._open_manage_rigs_dialog())
        ToolTip(self.rig_manage_btn, "Manage saved rigs — rename, update, or delete")

        # Vertical divider separating rig preset from individual equipment chips
        tk.Frame(chips_row, bg="#2a3642", width=1, height=22).pack(side="left", fill="y", padx=(0, 12))

        # Scope chip
        self.scope_dropdown = ttk.Combobox(chips_row, textvariable=self.scope_choice,
                                            state="readonly", width=16)
        self.scope_dropdown.pack(side="left", padx=(0, 6))

        # Reducer chip
        _reduction_values = ["0.63×", "0.67×", "0.70×", "0.80×", "1.0×", "1.5×", "2.0×", "2.5×", "3.0×"]
        self.reduction_entry = ttk.Combobox(chips_row, textvariable=self.reduction_factor,
                                            values=_reduction_values, state="readonly", width=6)
        self.reduction_entry.pack(side="left", padx=(0, 6))

        # Camera chip
        self.camera_dropdown = ttk.Combobox(chips_row, textvariable=self.camera_choice,
                                             state="readonly", width=18)
        self.camera_dropdown.pack(side="left", padx=(0, 6))

        # Bortle chip
        self.bortle_dropdown = ttk.Combobox(chips_row, textvariable=self.bortle_choice,
                                             values=list(BORTLE_FACTORS.keys()),
                                             state="readonly", width=16)
        self.bortle_dropdown.pack(side="left", padx=(0, 6))

        # Filter chip (visibility toggled by camera type)
        self.filter_label = ttk.Label(chips_row, text="")  # hidden placeholder
        self.filter_dropdown = ttk.Combobox(chips_row, textvariable=self.filter_mode,
                                             values=["Mono Lum", "LRGB",
                                                     "NB (3nm)", "NB (7nm)",
                                                     "NB (12nm)"],
                                             state="readonly", width=12)
        self.camera_choice.trace_add('write', self.toggle_filter_visibility)

        # Right-side info summary
        self._equip_summary_label = tk.Label(chips_row, text="", bg="#131f2e",
                                              fg="#556677", font=("Helvetica", 9))
        self._equip_summary_label.pack(side="right", padx=(10, 0))

        # Bottom separator for equipment bar
        tk.Frame(ctrl_frame, bg="#1e2d3e", height=1).pack(fill="x")

        # ── Target Selection area (no LabelFrame border) ─────────────────
        search_frame = ttk.Frame(self.tab_planner)
        search_frame.pack(padx=20, pady=5, fill="both", expand=True)

        left_box = ttk.Frame(search_frame); left_box.pack(side="left", padx=(0, 10), pady=6, fill="both", expand=True)
        self.search_hint_label = ttk.Label(left_box, text="Search by catalog ID (M42, NGC 224) or name (Orion Nebula)",
                  font=("Helvetica", 10), foreground="#778899")
        self.search_hint_label.pack(anchor="w", pady=(0, 4))

        search_row = ttk.Frame(left_box)
        search_row.pack(anchor="w", fill="x")

        # ── Canvas-based pill button (tk.Label can't do rounded corners) ──
        # Horizontal pill: star + label inline, aligns with entry-field height.
        # Width includes a comfortable margin for wider Windows Helvetica rendering.
        _QB_W, _QB_H, _QB_R = 150, 32, 16   # width, height, corner radius (pill: r = h/2)
        _QB_BG      = "#1e3a5f"
        _QB_BORDER  = "#7eb8d4"
        _QB_FG      = "#7eb8d4"
        _QB_BG_HOV  = "#2a5280"
        _QB_BOR_HOV = "#aaddff"
        _QB_FG_HOV  = "#aaddff"

        def _draw_queue_btn(bg, border, fg):
            self._queue_btn.delete("all")
            r = _QB_R
            w, h = _QB_W, _QB_H
            # Pill fill: two semicircle pieslices + middle rectangle (no outlines)
            self._queue_btn.create_arc(0, 0, 2*r, h, start=90, extent=180,
                                       style="pieslice", fill=bg, outline="")
            self._queue_btn.create_arc(w-2*r-1, 0, w-1, h, start=270, extent=180,
                                       style="pieslice", fill=bg, outline="")
            self._queue_btn.create_rectangle(r, 0, w-r, h, fill=bg, outline="")
            # Pill border: two semicircle arcs + top/bottom connecting lines
            self._queue_btn.create_arc(0, 0, 2*r-1, h-1, start=90, extent=180,
                                       style="arc", outline=border)
            self._queue_btn.create_arc(w-2*r, 0, w-1, h-1, start=270, extent=180,
                                       style="arc", outline=border)
            self._queue_btn.create_line(r, 0,     w-1-r, 0,     fill=border)
            self._queue_btn.create_line(r, h-1,   w-1-r, h-1,   fill=border)
            # Star + label inline, horizontally arranged
            self._queue_btn.create_text(16, h//2, text="☆",
                                        fill=fg, font=("Helvetica", 13, "bold"), anchor="center")
            self._queue_btn.create_text(30, h//2, text="Add to Targets",
                                        fill=fg, font=("Helvetica", 11, "bold"), anchor="w")

        self._queue_btn = tk.Canvas(search_row, width=_QB_W, height=_QB_H,
                                    bg="#1e2d3e", highlightthickness=0, cursor="hand2")
        self._queue_btn.pack(side="right")
        _draw_queue_btn(_QB_BG, _QB_BORDER, _QB_FG)

        def _qb_enter(e):
            _draw_queue_btn(_QB_BG_HOV, _QB_BOR_HOV, _QB_FG_HOV)
        def _qb_leave(e):
            if self.night_mode:
                _draw_queue_btn("#2a0000", "#cc4400", "#cc4400")
            else:
                _draw_queue_btn(_QB_BG, _QB_BORDER, _QB_FG)
        def _qb_click(e):
            self._add_to_queue()
        def _qb_release(e):
            _qb_leave(e)

        self._queue_btn.bind("<Enter>",           _qb_enter)
        self._queue_btn.bind("<Leave>",           _qb_leave)
        self._queue_btn.bind("<Button-1>",        _qb_click)
        self._queue_btn.bind("<ButtonRelease-1>", _qb_release)
        self._draw_queue_btn = _draw_queue_btn   # store for night-mode redraws
        ToolTip(self._queue_btn, "Add this target to the Targets List\nfor side-by-side window comparison\nand suggested shoot order.")

        # ── Full-width search entry ──────────────────────────────────────
        self.target_search = ttk.Entry(search_row, width=50, font=("Helvetica", 12))
        self.target_search.pack(side="left", fill="x", expand=True, padx=(0, 4))

        # ── Catalog filter button ────────────────────────────────────────
        # Small icon-only button next to the search entry that opens a popup
        # with checkboxes for NGC / IC / Messier / Caldwell / Sharpless / Other.
        # Shows a dot indicator when any catalog is filtered out.
        self._catalog_filter_btn = ttk.Button(
            search_row, text="▽",
            command=self._toggle_catalog_filter_popup,
            width=3)
        self._catalog_filter_btn.pack(side="left", padx=(0, 8))
        ToolTip(self._catalog_filter_btn,
                "Limit the search and suggestion popup to\nspecific catalogs (NGC, IC, Messier,\nCaldwell, Sharpless).")
        self._update_catalog_filter_btn_label()

        # ── Floating suggestion popup (replaces fixed Listbox) ───────────
        self._suggestion_popup = None
        self.suggestion_list = tk.Listbox(left_box, height=0, width=0)  # hidden — kept for API compat
        self.target_search.bind("<KeyRelease>", self._on_search_key)
        self.target_search.bind("<Return>", lambda e: (self._hide_suggestions(), self.analyze_framing()))
        self.target_search.bind("<Escape>", lambda e: self._hide_suggestions())
        self.target_search.bind("<FocusOut>", lambda e: self.root.after(150, self._hide_suggestions))

        # ── Action button row ─────────────────────────────────────────────
        btn_frame = ttk.Frame(left_box)
        btn_frame.pack(anchor="w", pady=(6, 2))
        analyze_btn = ttk.Button(btn_frame, text="Analyze Target", command=self.analyze_framing)
        analyze_btn.pack(side="left", padx=(0, 4))
        ToolTip(analyze_btn, "Calculate FOV, image scale, recommended\nexposure, and tonight's imaging window\nfor the selected target and equipment")
        visible_btn = ttk.Button(btn_frame, text="🌙 Visible Tonight", command=self.show_visible_tonight)
        visible_btn.pack(side="left", padx=(0, 4))
        ToolTip(visible_btn, "Search the full catalog for objects\nvisible tonight from your location,\nfiltered by type, altitude, and season")
        skymap_btn = ttk.Button(btn_frame, text="Show Sky Map", command=self.open_sky_map)
        skymap_btn.pack(side="left", padx=(0, 4))
        ToolTip(skymap_btn, "Open the interactive sky map\ncentred on the current target")

        results_frame = ttk.Frame(left_box)
        results_frame.pack(anchor="w", fill="both", expand=True, pady=(6, 0))
        self.results_txt = tk.Text(results_frame, font=("Helvetica", 11), wrap="word",
                                    width=40, height=14, padx=10, pady=8,
                                    relief="flat", borderwidth=0)
        results_sb = ttk.Scrollbar(results_frame, orient="vertical", command=self.results_txt.yview)
        self.results_txt.configure(yscrollcommand=results_sb.set)
        self.results_txt.pack(side="left", fill="both", expand=True)
        results_sb.pack(side="right", fill="y")
        self.results_txt.tag_configure("optimal",   foreground="#4caf50", font=("Helvetica", 11, "bold"))
        self.results_txt.tag_configure("warning",   foreground="#ef5350", font=("Helvetica", 11, "bold"))
        self.results_txt.tag_configure("highlight", foreground="#38bdf8", font=("Helvetica", 11, "bold"))
        self.results_txt.tag_configure("header",    foreground="#ffffff", font=("Helvetica", 13, "bold"))
        # Section header tags — color-coded
        self.results_txt.tag_configure("sec_framing",  foreground="#38bdf8", font=("Helvetica", 10, "bold"))
        self.results_txt.tag_configure("sec_exposure", foreground="#f59e0b", font=("Helvetica", 10, "bold"))
        self.results_txt.tag_configure("sec_window",   foreground="#4caf50", font=("Helvetica", 10, "bold"))
        self.results_txt.tag_configure("sec_moon",     foreground="#ef5350", font=("Helvetica", 10, "bold"))
        self.results_txt.tag_configure("dim",          foreground="#778899", font=("Helvetica", 11))
        self.results_txt.tag_configure("amber",        foreground="#f59e0b", font=("Helvetica", 11, "bold"))


        # ── Compact FOV preview (right side) ─────────────────────────────
        fov_box = ttk.Frame(search_frame)
        fov_box.pack(side="right", padx=0, pady=6)
        ttk.Label(fov_box, text="FOV Preview", font=("Helvetica", 9, "bold"),
                  foreground="#7eb8d4").pack(anchor="w")
        ttk.Label(fov_box, text="drag to pan · corners to rotate",
                  font=("Helvetica", 8), foreground="#556677").pack(anchor="w")
        self.preview_canvas = tk.Canvas(fov_box, width=240, height=240, bg="black",
                                         highlightthickness=1, highlightbackground="#2e4a63")
        self.preview_canvas.pack(pady=(3, 0))
        self.preview_canvas.create_text(120, 120, text="No target loaded", fill="gray", font=("Helvetica", 9))

        # Controls row — compact single-line bar
        ctrl_row = tk.Frame(fov_box, bg="#131f2e")
        ctrl_row.pack(fill="x", pady=(3, 0))
        tk.Label(ctrl_row, text="Rot:", bg="#131f2e", fg="#778899",
                 font=("Helvetica", 9)).pack(side="left", padx=(6, 2))
        self._rot_label = tk.Label(ctrl_row, text="0.0°", bg="#131f2e", fg="#ffffff",
                                    font=("Helvetica", 9), width=5)
        self._rot_label.pack(side="left")
        tk.Label(ctrl_row, text="|", bg="#131f2e", fg="#2e4a63",
                 font=("Helvetica", 9)).pack(side="left", padx=4)
        reset_btn = ttk.Button(ctrl_row, text="⌖ Reset", width=6, command=self._reset_fov_framing)
        reset_btn.pack(side="left", padx=2)
        ToolTip(reset_btn, "Reset the FOV overlay to centred\nwith no rotation")
        tk.Label(ctrl_row, text="|", bg="#131f2e", fg="#2e4a63",
                 font=("Helvetica", 9)).pack(side="left", padx=4)
        tk.Label(ctrl_row, text="Zoom:", bg="#131f2e", fg="#778899",
                 font=("Helvetica", 9)).pack(side="left", padx=(0, 2))
        ttk.Button(ctrl_row, text="−", width=2,
                   command=lambda: self._apply_zoom(1 / 1.25)).pack(side="left", padx=1)
        self._zoom_label = tk.Label(ctrl_row, text="1.0×", bg="#131f2e", fg="#ffffff",
                                     font=("Helvetica", 9), width=4)
        self._zoom_label.pack(side="left")
        ttk.Button(ctrl_row, text="+", width=2,
                   command=lambda: self._apply_zoom(1.25)).pack(side="left", padx=1)
        ToolTip(self._zoom_label, "Scroll wheel on the image to zoom,\nor use + / − buttons")

        # Mouse bindings — smart start decides pan vs rotate
        self.preview_canvas.bind("<ButtonPress-1>",  self._fov_mouse_down)
        self.preview_canvas.bind("<B1-Motion>",      self._fov_mouse_drag)
        self.preview_canvas.bind("<ButtonRelease-1>",self._fov_mouse_up)
        self.preview_canvas.bind("<Motion>",         self._fov_mouse_hover)
        self.preview_canvas.bind("<MouseWheel>",     self._fov_mousewheel)   # Windows / macOS
        self.preview_canvas.bind("<Button-4>",       self._fov_mousewheel)   # Linux scroll up
        self.preview_canvas.bind("<Button-5>",       self._fov_mousewheel)   # Linux scroll down

        # FOV transform state
        self._fov_photo = None
        self._fov_params = None
        self._fov_offset_x = 0.0
        self._fov_offset_y = 0.0
        self._fov_angle    = 0.0
        self._zoom_level   = 1.0
        self._drag_mode    = None   # 'pan' | 'rotate'
        self._drag_start   = None   # (x, y) for pan
        self._rotate_start_angle = None  # angle at drag start (degrees)
        self._fov_corners  = []     # current corner positions for hit-testing
        self._cached_dss_img = None
        self._cached_dss_target = None
        self._cached_dss_survey_deg = None

        # ── Bottom: altitude chart (compact inline strip) ─────────────────
        self.alt_canvas = tk.Canvas(self.tab_planner, height=168, bg="#050810",
                                    highlightthickness=1, highlightbackground="#2e4a63")
        self.alt_canvas.pack(padx=20, pady=(0, 8), fill="x")
        self.alt_canvas.create_text(400, 84, text="Altitude chart — analyze a target to populate",
                                    fill="#334455", font=("Helvetica", 10), justify="center")

        # Moon bar hover state
        self._moon_bar_hits = []   # list of (x_pixel, tooltip_text)
        self._chart_tip_win = None
        self.alt_canvas.bind("<Motion>",  self._alt_canvas_motion)
        self.alt_canvas.bind("<Leave>",   self._alt_canvas_leave)

    def setup_session_tab(self):
        """Build the Targets List tab — Integration Plan preview + Tonight's Plan list."""
        # Scrollable container so the whole tab (analysis + queue + schedule
        # graph) scrolls as one page on shorter windows.  The inner queue-card
        # list keeps its own wheel scroll — it's excluded from the page binding
        # at the end of this method.
        _ss_bg = ttk.Style().lookup("TFrame", "background") or "#1e2d3e"
        self._session_canvas = tk.Canvas(self.tab_session, bg=_ss_bg,
                                         highlightthickness=0)
        _session_sb = ttk.Scrollbar(self.tab_session, orient="vertical",
                                    command=self._session_canvas.yview)
        self._session_canvas.configure(yscrollcommand=_session_sb.set)
        _session_sb.pack(side="right", fill="y")
        self._session_canvas.pack(side="left", fill="both", expand=True)

        outer = ttk.Frame(self._session_canvas, padding=(20, 10))
        outer.bind("<Configure>", lambda e: self._session_canvas.configure(
            scrollregion=self._session_canvas.bbox("all")))
        self._session_canvas.create_window((0, 0), window=outer,
                                           anchor="nw", tags="inner")
        def _session_canvas_config(e):
            self._session_canvas.itemconfig("inner", width=e.width)
            self._session_canvas.configure(
                scrollregion=self._session_canvas.bbox("all"))
        self._session_canvas.bind("<Configure>", _session_canvas_config)

        # ── Integration Plan (preview of current analysis) ────────────────
        plan_card = tk.Frame(outer, bg="#131f2e", highlightthickness=0)
        plan_card.pack(fill="x", pady=(0, 10))
        tk.Frame(plan_card, bg="#38bdf8", width=3).pack(side="left", fill="y")
        plan_outer = ttk.Frame(plan_card)
        plan_outer.pack(side="left", fill="both", expand=True, padx=(8, 0))
        ttk.Label(plan_outer, text="CURRENT TARGET ANALYSIS",
                  font=("Helvetica", 9, "bold"), foreground="#38bdf8").pack(
            anchor="w", padx=10, pady=(8, 0))

        # Current target — large, updates automatically on every analysis
        self._plan_target_var = tk.StringVar(value="No target analysed yet — run Analyze Target first")
        ttk.Label(plan_outer, textvariable=self._plan_target_var,
                  font=("Helvetica", 13, "bold")).pack(anchor="w", padx=10, pady=(8, 4))

        # ── All inputs on one horizontal row ─────────────────────────────
        input_row = ttk.Frame(plan_outer)
        input_row.pack(anchor="w", padx=10, pady=(2, 6))

        # Allocated hours
        ttk.Label(input_row, text="Allocated hours:").pack(side="left")
        self.session_hours_var = tk.StringVar(value="4.0")
        self.session_hours_var.trace_add("write", lambda *_: (
            self.show_integration_plan(silent=True),
            self._commit_queue_edit(silent=True)))
        session_entry = ttk.Entry(input_row, textvariable=self.session_hours_var, width=6,
                                  state="disabled")
        session_entry.pack(side="left", padx=(4, 2))
        self._session_entry = session_entry
        ttk.Label(input_row, text="h").pack(side="left", padx=(0, 10))
        ToolTip(session_entry, "Hours allocated to this target.\nAuto-fills from tonight's dark window\nwhen you run Analyze Target.")

        ttk.Separator(input_row, orient="vertical").pack(side="left", fill="y", pady=2, padx=(0, 10))

        # Sub-exposure
        ttk.Label(input_row, text="Sub-exp:").pack(side="left")
        self.manual_exp_var = tk.StringVar(value="")
        self.manual_exp_var.trace_add("write", lambda *_: (
            self.show_integration_plan(silent=True),
            self._commit_queue_edit(silent=True)))
        manual_exp_entry = ttk.Entry(input_row, textvariable=self.manual_exp_var, width=7,
                                     state="disabled")
        manual_exp_entry.pack(side="left", padx=(4, 2))
        self._manual_exp_entry = manual_exp_entry
        ttk.Label(input_row, text="s").pack(side="left", padx=(0, 4))
        self._recommended_exp_label = ttk.Label(input_row, text="", font=("Helvetica", 9),
                                                foreground="#aaaaaa")
        self._recommended_exp_label.pack(side="left", padx=(0, 10))
        ToolTip(manual_exp_entry, "Enter a custom sub-exposure time in seconds.\nLeave blank to use the calculated recommendation.")

        ttk.Separator(input_row, orient="vertical").pack(side="left", fill="y", pady=2, padx=(0, 10))

        # Overhead per sub
        ttk.Label(input_row, text="Overhead:").pack(side="left")
        self.overhead_per_sub_var = tk.StringVar(value="5")
        self.overhead_per_sub_var.trace_add("write", lambda *_: (
            self.show_integration_plan(silent=True),
            self._commit_queue_edit(silent=True)))
        overhead_entry = ttk.Entry(input_row, textvariable=self.overhead_per_sub_var, width=5)
        overhead_entry.pack(side="left", padx=(4, 2))
        ttk.Label(input_row, text="s/sub").pack(side="left", padx=(0, 10))
        ToolTip(overhead_entry,
                "Extra time lost per sub-frame, in seconds.\n"
                "Accounts for:\n"
                "  • Camera sensor readout time\n"
                "  • Dithering between frames\n"
                "  • Filter wheel rotation\n"
                "  • Autofocuser runs (amortised)\n"
                "  • Plate-solve / re-centre pauses\n"
                "  • Guider settling time\n\n"
                "Default: 5s — a reasonable estimate for most\n"
                "modern CMOS setups with dithering enabled.")

        # Stat cards — 4 across, modern metric card style
        cards_frame = ttk.Frame(plan_outer)
        cards_frame.pack(fill="x", padx=10, pady=(0, 6))
        self._plan_subs_var     = tk.StringVar(value="—")
        self._plan_total_var    = tk.StringVar(value="—")
        self._plan_snr_var      = tk.StringVar(value="—")
        self._plan_overhead_var = tk.StringVar(value="—")
        for col, (var, lbl) in enumerate([
            (self._plan_subs_var,     "Subframes"),
            (self._plan_total_var,    "Integration Time"),
            (self._plan_snr_var,      "SNR Improvement"),
            (self._plan_overhead_var, "Overhead Time"),
        ]):
            card = tk.Frame(cards_frame, bg="#1a2b3c", highlightthickness=0)
            card.grid(row=0, column=col, padx=(0, 8), pady=2, sticky="nsew")
            cards_frame.columnconfigure(col, weight=1)
            tk.Label(card, text=lbl.upper(), bg="#1a2b3c", fg="#556677",
                     font=("Helvetica", 8, "bold")).pack(anchor="w", padx=10, pady=(8, 2))
            tk.Label(card, textvariable=var, bg="#1a2b3c", fg="#ffffff",
                     font=("Helvetica", 16, "bold")).pack(anchor="w", padx=10, pady=(0, 8))

        self._plan_noise_var = tk.StringVar(value="")
        ttk.Label(plan_outer, textvariable=self._plan_noise_var,
                  font=("Helvetica", 11), wraplength=700).pack(anchor="w", padx=10, pady=(0, 6))

        # ── Target Queue ────────────────────────────────────────────────────
        queue_card = tk.Frame(outer, bg="#131f2e", highlightthickness=0)
        queue_card.pack(fill="x", pady=(0, 8))
        tk.Frame(queue_card, bg="#f59e0b", width=3).pack(side="left", fill="y")
        queue_outer = ttk.Frame(queue_card)
        queue_outer.pack(side="left", fill="both", expand=True, padx=(8, 0))
        ttk.Label(queue_outer, text="TARGET QUEUE",
                  font=("Helvetica", 9, "bold"), foreground="#f59e0b").pack(
            anchor="w", padx=8, pady=(8, 0))

        q_btn_row = ttk.Frame(queue_outer)
        q_btn_row.pack(fill="x", padx=8, pady=(6, 4))
        ttk.Button(q_btn_row, text="↑ Move Up",
                   command=self._queue_move_up).pack(side="left", padx=(0, 4))
        ttk.Button(q_btn_row, text="↓ Move Down",
                   command=self._queue_move_down).pack(side="left", padx=(0, 4))
        ttk.Button(q_btn_row, text="✕ Remove",
                   command=self._queue_remove).pack(side="left", padx=(0, 16))
        ttk.Button(q_btn_row, text="★ Order by Transit",
                   command=self._queue_suggest_order).pack(side="left", padx=(0, 8))
        add_selected_btn = ttk.Button(q_btn_row, text="▶ Add to Tonight's Plan",
                   command=self._queue_move_selected_to_plan)
        add_selected_btn.pack(side="left", padx=(0, 8))
        ToolTip(add_selected_btn, "Move the selected queue target\nto Tonight's Plan.")
        ttk.Button(q_btn_row, text="▶▶ Add All to Tonight's Plan",
                   command=self._queue_move_all_to_plan).pack(side="left", padx=(0, 8))
        ttk.Button(q_btn_row, text="✕ Clear All",
                   command=self._queue_clear).pack(side="left")

        q_tree_frame = ttk.Frame(queue_outer)
        q_tree_frame.pack(fill="x", padx=8, pady=(0, 4))

        # Scrollable card container (replaces ttk.Treeview)
        self._queue_cards_canvas = tk.Canvas(q_tree_frame, height=200, bg="#0e1a28",
                                              highlightthickness=0)
        self._queue_cards_scrollbar = ttk.Scrollbar(q_tree_frame, orient="vertical",
                                                      command=self._queue_cards_canvas.yview)
        self._queue_cards_inner = tk.Frame(self._queue_cards_canvas, bg="#0e1a28")
        self._queue_cards_inner.bind("<Configure>",
            lambda e: self._queue_cards_canvas.configure(
                scrollregion=self._queue_cards_canvas.bbox("all")))
        self._queue_cards_canvas.create_window((0, 0), window=self._queue_cards_inner,
                                                anchor="nw", tags="inner")
        self._queue_cards_canvas.bind("<Configure>",
            lambda e: self._queue_cards_canvas.itemconfig("inner", width=e.width))
        self._queue_cards_canvas.configure(yscrollcommand=self._queue_cards_scrollbar.set)
        self._queue_cards_canvas.pack(side="left", fill="both", expand=True)
        self._queue_cards_scrollbar.pack(side="right", fill="y")

        # Mousewheel scrolling — bind on the canvas AND the embedded inner
        # frame.  Card widgets created later in _refresh_queue_tree get the
        # same bindings recursively, because Tk routes MouseWheel events to
        # the widget under the cursor; once cards fill the visible area the
        # canvas itself never sees the wheel.
        self._bind_queue_mousewheel(self._queue_cards_canvas)
        self._bind_queue_mousewheel(self._queue_cards_inner)

        # Selection state for cards
        self._queue_card_selected = None  # index of selected card
        self._queue_card_widgets = []     # list of card frame references

        # Keep queue_tree as a dummy attribute so hasattr checks don't break
        self.queue_tree = None

        self.queue_gantt = tk.Canvas(queue_outer, height=200, bg="#050810",
                                     highlightthickness=1, highlightbackground="#334455")
        self.queue_gantt.pack(fill="x", padx=8, pady=(0, 2))
        self.queue_gantt.create_text(430, 100,
            text="Add targets to the queue and move them to Tonight's Plan to see the schedule timeline",
            fill="#445566", font=("Helvetica", 9))
        ttk.Label(queue_outer, text="Target Scheduling  —  drag a bar to set its start time",
                  font=("Helvetica", 11, "italic")).pack(anchor="w", padx=8, pady=(0, 2))
        self._gantt_note_lbl = ttk.Label(queue_outer, text="",
                                         font=("Helvetica", 9, "italic"), foreground="#cc8800")
        self._gantt_note_lbl.pack(anchor="w", padx=8, pady=(0, 4))

        self.queue_gantt.bind("<ButtonPress-1>",   self._gantt_press)
        self.queue_gantt.bind("<B1-Motion>",       self._gantt_drag)
        self.queue_gantt.bind("<ButtonRelease-1>", self._gantt_release)
        self.queue_gantt.bind("<Motion>",          self._gantt_motion)
        self.queue_gantt.bind("<Configure>",       lambda e: self._draw_queue_gantt())
        self.queue_gantt.bind("<Map>",             lambda e: self.root.after(20, self._draw_queue_gantt))

        # Wheel-scroll the whole tab as one page, but leave the inner queue-card
        # list to its own wheel scroll (drag the gantt bars still works — that's
        # Button-1, not the wheel).
        self._bind_session_mousewheel(self._session_canvas)
        self._bind_session_mousewheel_recursive(
            outer, exclude=(self._queue_cards_canvas,))
        self._session_canvas.after_idle(lambda: self._session_canvas.configure(
            scrollregion=self._session_canvas.bbox("all")))

    def setup_plan_tab(self):
        """Build the Tonight's Plan tab — the final output of the planning workflow."""
        outer = ttk.Frame(self.tab_plan)
        outer.pack(fill="both", expand=True, padx=20, pady=10)

        # Empty-state message (shown when no entries)
        self._plan_empty_label = ttk.Label(outer,
            text="No targets in tonight's plan yet.\n\n"
                 "Add targets from the Targets List using\n"
                 "\"▶ Add to Tonight's Plan\" or \"▶▶ Add All\".",
            font=("Helvetica", 12), foreground="#556677", justify="center")
        self._plan_empty_label.pack(pady=(60, 0))

        # Scrollable card container (replaces ttk.Treeview)
        tree_frame = ttk.Frame(outer)
        self._plan_tree_frame = tree_frame
        tree_frame.pack(fill="both", expand=True)

        self._plan_cards_canvas = tk.Canvas(tree_frame, height=300, bg="#0e1a28",
                                             highlightthickness=0)
        self._plan_cards_scrollbar = ttk.Scrollbar(tree_frame, orient="vertical",
                                                     command=self._plan_cards_canvas.yview)
        self._plan_cards_inner = tk.Frame(self._plan_cards_canvas, bg="#0e1a28")
        self._plan_cards_inner.bind("<Configure>",
            lambda e: self._plan_cards_canvas.configure(
                scrollregion=self._plan_cards_canvas.bbox("all")))
        self._plan_cards_canvas.create_window((0, 0), window=self._plan_cards_inner,
                                               anchor="nw", tags="inner")
        self._plan_cards_canvas.bind("<Configure>",
            lambda e: self._plan_cards_canvas.itemconfig("inner", width=e.width))
        self._plan_cards_canvas.configure(yscrollcommand=self._plan_cards_scrollbar.set)
        self._plan_cards_canvas.pack(side="left", fill="both", expand=True)
        self._plan_cards_scrollbar.pack(side="right", fill="y")

        # Mousewheel scrolling — bind on the canvas AND the embedded inner
        # frame.  Card widgets created later in _refresh_plan_view get the
        # same bindings recursively, because Tk routes MouseWheel events to
        # the widget under the cursor; once cards fill the visible area the
        # canvas itself never sees the wheel.
        self._bind_plan_mousewheel(self._plan_cards_canvas)
        self._bind_plan_mousewheel(self._plan_cards_inner)

        # Card selection state
        self._plan_card_selected = None
        self._plan_card_widgets = []

        # Keep plan_tree as dummy for hasattr checks
        self.plan_tree = None

        # Totals row
        totals_frame = ttk.Frame(outer)
        totals_frame.pack(fill="x", pady=(4, 4))
        self._plan_total_alloc_var = tk.StringVar(value="")
        self._plan_total_int_var   = tk.StringVar(value="")
        self._plan_total_subs_var  = tk.StringVar(value="")
        ttk.Label(totals_frame, text="Totals:", font=("Helvetica", 11, "bold")).pack(side="left", padx=(0, 10))
        ttk.Label(totals_frame, textvariable=self._plan_total_alloc_var, font=("Helvetica", 11)).pack(side="left", padx=(0, 14))
        ttk.Label(totals_frame, textvariable=self._plan_total_int_var,   font=("Helvetica", 11)).pack(side="left", padx=(0, 14))
        ttk.Label(totals_frame, textvariable=self._plan_total_subs_var,  font=("Helvetica", 11)).pack(side="left")

        # Action buttons
        btn_frame = ttk.Frame(outer)
        btn_frame.pack(fill="x", pady=(4, 0))
        remove_btn = ttk.Button(btn_frame, text="🗑 Remove Selected",
                                command=self._remove_from_plan)
        remove_btn.pack(side="left", padx=(0, 6))
        ToolTip(remove_btn, "Remove the selected entry from\ntonight's plan.")
        clear_btn = ttk.Button(btn_frame, text="✕ Clear All",
                               command=self._clear_plan)
        clear_btn.pack(side="left", padx=(0, 20))
        ToolTip(clear_btn, "Remove all entries from\ntonight's plan.")
        save_btn = ttk.Button(btn_frame, text="💾 Save Session",
                              command=self._save_session_as)
        save_btn.pack(side="left", padx=(0, 6))
        ToolTip(save_btn, "Save tonight's plan to a named\n.json file for future reference.")
        load_btn = ttk.Button(btn_frame, text="📂 Load Session",
                              command=self._load_session)
        load_btn.pack(side="left", padx=(0, 20))
        ToolTip(load_btn, "Load a previously saved session\ninto tonight's plan.")
        export_btn = ttk.Button(btn_frame, text="📄 Export Session",
                                command=self._export_session)
        export_btn.pack(side="left", padx=(0, 6))
        ToolTip(export_btn, "Save tonight's plan as a formatted\ntext report (.txt) for printing or sharing.")
        nina_btn = ttk.Button(btn_frame, text="🔭 Export as NINA",
                              command=self._export_nina)
        nina_btn.pack(side="left")
        ToolTip(nina_btn, "Export tonight's plan as a NINA target set\n(.ninaTargetSet) for import into N.I.N.A.\nEach target becomes a CaptureSequenceList\nwith its sub-exposure and sub-count filled in.")


    def _bind_plan_mousewheel(self, widget):
        """Bind mousewheel / Linux scroll-button events on a widget so the
        wheel scrolls the Tonight's Plan card list.  Called on the canvas
        and inner frame at setup time, and recursively on every card and
        descendant in _refresh_plan_tree (cards absorb wheel events, so
        each child needs its own binding for the wheel to keep working
        once the list fills up).

        Handles the cross-platform <MouseWheel> delta quirk: Windows reports
        deltas as +/- 120 per notch; macOS reports tiny ints (often 1-3) per
        event.  A naive `delta / 120` rounds to 0 on macOS and the wheel
        never moves, which is why an earlier attempt at this fix appeared to
        do nothing on Mac.
        """
        def _on_wheel(e):
            if abs(e.delta) >= 120:
                # Windows-style: one notch = 120
                steps = int(-1 * (e.delta / 120))
            elif e.delta:
                # macOS-style: one event per tick, sign gives direction
                steps = -1 if e.delta > 0 else 1
            else:
                steps = 0
            if steps:
                self._plan_cards_canvas.yview_scroll(steps, "units")
        widget.bind("<MouseWheel>", _on_wheel)
        widget.bind("<Button-4>",
            lambda e: self._plan_cards_canvas.yview_scroll(-1, "units"))
        widget.bind("<Button-5>",
            lambda e: self._plan_cards_canvas.yview_scroll(1, "units"))

    def _bind_plan_mousewheel_recursive(self, widget):
        """Recursively apply _bind_plan_mousewheel to a widget and all of
        its descendants."""
        self._bind_plan_mousewheel(widget)
        for child in widget.winfo_children():
            self._bind_plan_mousewheel_recursive(child)

    def _bind_queue_mousewheel(self, widget):
        """Bind mousewheel / Linux scroll-button events on a widget so the
        wheel scrolls the queue card list on the Targets List tab.  Mirrors
        _bind_plan_mousewheel — see that method for the macOS delta quirk
        explanation."""
        def _on_wheel(e):
            if abs(e.delta) >= 120:
                steps = int(-1 * (e.delta / 120))
            elif e.delta:
                steps = -1 if e.delta > 0 else 1
            else:
                steps = 0
            if steps:
                self._queue_cards_canvas.yview_scroll(steps, "units")
        widget.bind("<MouseWheel>", _on_wheel)
        widget.bind("<Button-4>",
            lambda e: self._queue_cards_canvas.yview_scroll(-1, "units"))
        widget.bind("<Button-5>",
            lambda e: self._queue_cards_canvas.yview_scroll(1, "units"))

    def _bind_queue_mousewheel_recursive(self, widget):
        """Recursively apply _bind_queue_mousewheel to a widget and all of
        its descendants."""
        self._bind_queue_mousewheel(widget)
        for child in widget.winfo_children():
            self._bind_queue_mousewheel_recursive(child)

    def _bind_settings_mousewheel(self, widget):
        """Bind mousewheel / Linux scroll events so the wheel scrolls the
        Settings panel.  Mirrors _bind_queue_mousewheel (see it for the
        macOS delta quirk)."""
        def _on_wheel(e):
            if abs(e.delta) >= 120:
                steps = int(-1 * (e.delta / 120))
            elif e.delta:
                steps = -1 if e.delta > 0 else 1
            else:
                steps = 0
            if steps:
                self._settings_canvas.yview_scroll(steps, "units")
        widget.bind("<MouseWheel>", _on_wheel)
        widget.bind("<Button-4>",
            lambda e: self._settings_canvas.yview_scroll(-1, "units"))
        widget.bind("<Button-5>",
            lambda e: self._settings_canvas.yview_scroll(1, "units"))

    def _bind_settings_mousewheel_recursive(self, widget):
        """Recursively bind settings wheel-scroll on a widget and descendants."""
        self._bind_settings_mousewheel(widget)
        for child in widget.winfo_children():
            self._bind_settings_mousewheel_recursive(child)

    def _bind_session_mousewheel(self, widget):
        """Bind mousewheel / Linux scroll so the wheel scrolls the Targets List
        tab.  Mirrors _bind_queue_mousewheel."""
        def _on_wheel(e):
            if abs(e.delta) >= 120:
                steps = int(-1 * (e.delta / 120))
            elif e.delta:
                steps = -1 if e.delta > 0 else 1
            else:
                steps = 0
            if steps:
                self._session_canvas.yview_scroll(steps, "units")
        widget.bind("<MouseWheel>", _on_wheel)
        widget.bind("<Button-4>",
            lambda e: self._session_canvas.yview_scroll(-1, "units"))
        widget.bind("<Button-5>",
            lambda e: self._session_canvas.yview_scroll(1, "units"))

    def _bind_session_mousewheel_recursive(self, widget, exclude=()):
        """Recursively bind session wheel-scroll, skipping any widget (and its
        subtree) in `exclude` so the inner queue list keeps its own scroll."""
        if widget in exclude:
            return
        self._bind_session_mousewheel(widget)
        for child in widget.winfo_children():
            self._bind_session_mousewheel_recursive(child, exclude)


    def setup_settings_tab(self):
        """Build the Settings panel — observer location, analysis preferences, data management."""
        # Scrollable container so the panel fits shorter windows (the catalog
        # section can run past the bottom).  `outer` stays a ttk.Frame, so the
        # cards/labels render exactly as before — it just lives in the canvas.
        _sb_bg = ttk.Style().lookup("TFrame", "background") or "#1e2d3e"
        self._settings_canvas = tk.Canvas(self.tab_settings, bg=_sb_bg,
                                          highlightthickness=0)
        _settings_sb = ttk.Scrollbar(self.tab_settings, orient="vertical",
                                     command=self._settings_canvas.yview)
        self._settings_canvas.configure(yscrollcommand=_settings_sb.set)
        _settings_sb.pack(side="right", fill="y")
        self._settings_canvas.pack(side="left", fill="both", expand=True)

        outer = ttk.Frame(self._settings_canvas, padding=(20, 10))
        outer.bind("<Configure>", lambda e: self._settings_canvas.configure(
            scrollregion=self._settings_canvas.bbox("all")))
        self._settings_canvas.create_window((0, 0), window=outer,
                                            anchor="nw", tags="inner")
        self._settings_canvas.bind("<Configure>", lambda e:
            self._settings_canvas.itemconfig("inner", width=e.width))

        # Title
        ttk.Label(outer, text="Settings",
                  font=("Helvetica", 16, "bold")).pack(anchor="w", pady=(0, 2))
        ttk.Label(outer, text="Observer location, analysis preferences, data management",
                  font=("Helvetica", 10), foreground="#556677").pack(anchor="w", pady=(0, 14))

        # ── OBSERVER LOCATION ─────────────────────────────────────────────
        loc_card = self._make_settings_card(outer, "OBSERVER LOCATION", "#38bdf8")

        # Location-profile selector — named sites for dark-site travelers
        prof_row = ttk.Frame(loc_card)
        prof_row.pack(fill="x", padx=8, pady=(4, 0))
        ttk.Label(prof_row, text="Location:").grid(row=0, column=0, padx=4, pady=2, sticky="e")
        self._settings_loc_choice = tk.StringVar(value=self.data.get("active_location", ""))
        self._settings_loc_combo = ttk.Combobox(
            prof_row, textvariable=self._settings_loc_choice, state="readonly",
            width=22, values=self._location_names())
        self._settings_loc_combo.grid(row=0, column=1, padx=4, pady=2, sticky="w")
        self._settings_loc_combo.bind("<<ComboboxSelected>>", self._on_settings_location_selected)
        ttk.Button(prof_row, text="☆ Save",
                   command=self._open_save_location_dialog).grid(row=0, column=2, padx=(8, 2), pady=2)
        ttk.Button(prof_row, text="⚙ Manage",
                   command=self._open_manage_locations_dialog).grid(row=0, column=3, padx=2, pady=2)
        ToolTip(self._settings_loc_combo,
                "Switch between saved observing sites.\n"
                "Save the current coordinates as a profile with ☆,\n"
                "or rename/delete saved sites with ⚙.")

        loc_row = ttk.Frame(loc_card)
        loc_row.pack(fill="x", padx=8, pady=4)

        ttk.Label(loc_row, text="Latitude:").grid(row=0, column=0, padx=4, pady=2, sticky="e")
        self._settings_lat_var = tk.StringVar()
        lat_entry = ttk.Entry(loc_row, textvariable=self._settings_lat_var, width=14)
        lat_entry.grid(row=0, column=1, padx=4, pady=2)

        ttk.Label(loc_row, text="Longitude:").grid(row=0, column=2, padx=(12, 4), pady=2, sticky="e")
        self._settings_lon_var = tk.StringVar()
        lon_entry = ttk.Entry(loc_row, textvariable=self._settings_lon_var, width=14)
        lon_entry.grid(row=0, column=3, padx=4, pady=2)

        ttk.Button(loc_row, text="📍 Auto-detect",
                   command=self._settings_autodetect_location).grid(row=0, column=4, padx=(12, 4), pady=2)

        # Manual save — only needed if user types coordinates by hand
        ttk.Button(loc_row, text="Save",
                   command=self._settings_save_location).grid(row=0, column=5, padx=4, pady=2)
        ToolTip(lat_entry, "Edit manually and press Save, or use Auto-detect")

        self._settings_loc_status = ttk.Label(loc_card, text="", font=("Helvetica", 9))
        self._settings_loc_status.pack(anchor="w", padx=8, pady=(0, 6))

        # Load saved location into the fields and show status
        saved_lat, saved_lon = self._get_saved_location()
        if saved_lat is not None:
            self._settings_lat_var.set(f"{saved_lat:.4f}")
            self._settings_lon_var.set(f"{saved_lon:.4f}")
            self._settings_loc_status.config(text="✅ Location saved", foreground="#4caf50")

        # ── ANALYSIS PREFERENCES ──────────────────────────────────────────
        pref_card = self._make_settings_card(outer, "ANALYSIS PREFERENCES", "#f59e0b")

        pref_grid = ttk.Frame(pref_card)
        pref_grid.pack(fill="x", padx=8, pady=4)

        # C-constant
        ttk.Label(pref_grid, text="C-constant (sub-exposure factor):").grid(
            row=0, column=0, padx=4, pady=4, sticky="w")
        self._settings_c_var = tk.StringVar(value=str(C_VALUE))
        c_entry = ttk.Entry(pref_grid, textvariable=self._settings_c_var, width=8)
        c_entry.grid(row=0, column=1, padx=4, pady=4, sticky="w")
        ttk.Label(pref_grid, text="Controls the sky-noise multiplier for recommended exposure",
                  font=("Helvetica", 9), foreground="#556677").grid(
            row=0, column=2, padx=(8, 4), pady=4, sticky="w")

        # Default Bortle
        ttk.Label(pref_grid, text="Default Bortle class:").grid(
            row=1, column=0, padx=4, pady=4, sticky="w")
        self._settings_default_bortle = tk.StringVar(
            value=self.data.get("settings", {}).get("default_bortle", "4 (Rural/Suburban)"))
        bortle_combo = ttk.Combobox(pref_grid, textvariable=self._settings_default_bortle,
                                     values=list(BORTLE_FACTORS.keys()), state="readonly", width=22)
        bortle_combo.grid(row=1, column=1, columnspan=2, padx=4, pady=4, sticky="w")

        # Default allocated hours
        ttk.Label(pref_grid, text="Default allocated hours:").grid(
            row=2, column=0, padx=4, pady=4, sticky="w")
        self._settings_default_alloc_hrs = tk.StringVar(
            value=str(self.data.get("settings", {}).get("default_alloc_hrs", "4.0")))
        alloc_entry = ttk.Entry(pref_grid, textvariable=self._settings_default_alloc_hrs, width=8)
        alloc_entry.grid(row=2, column=1, padx=4, pady=4, sticky="w")
        ttk.Label(pref_grid, text="h   (set to 0 to always use tonight's dark-window duration)",
                  font=("Helvetica", 9), foreground="#556677").grid(
            row=2, column=2, padx=(8, 4), pady=4, sticky="w")

        # Minimum altitude slider
        ttk.Label(pref_grid, text="Min altitude for Visible Tonight:").grid(
            row=3, column=0, padx=4, pady=4, sticky="w")
        self._settings_min_alt = tk.IntVar(
            value=int(self.data.get("settings", {}).get("min_alt", 20)))
        min_alt_scale = ttk.Scale(pref_grid, from_=10, to=60,
                                   variable=self._settings_min_alt, orient="horizontal",
                                   length=160, command=lambda v: self._settings_min_alt_label.config(
                                       text=f"{int(float(v))}°"))
        min_alt_scale.grid(row=3, column=1, padx=4, pady=4, sticky="w")
        self._settings_min_alt_label = ttk.Label(pref_grid,
            text=f"{self._settings_min_alt.get()}°", width=5, anchor="w")
        self._settings_min_alt_label.grid(row=3, column=2, padx=4, pady=4, sticky="w")

        # Auto-update toggle
        self._settings_auto_update = tk.BooleanVar(
            value=self.data.get("settings", {}).get("auto_update", False))
        ttk.Checkbutton(pref_grid, text="Auto-update analysis on equipment change",
                         variable=self._settings_auto_update).grid(
            row=4, column=0, columnspan=3, padx=4, pady=4, sticky="w")

        # Save preferences button
        ttk.Button(pref_card, text="Save Preferences",
                   command=self._settings_save_preferences).pack(anchor="w", padx=8, pady=(4, 8))

        # ── DATA MANAGEMENT ───────────────────────────────────────────────
        data_card = self._make_settings_card(outer, "DATA MANAGEMENT", "#8b5cf6")

        data_grid = ttk.Frame(data_card)
        data_grid.pack(fill="x", padx=8, pady=4)

        # NINA profile import
        ttk.Label(data_grid, text="NINA profile import:").grid(
            row=0, column=0, padx=4, pady=4, sticky="w")
        ttk.Button(data_grid, text="Import NINA Profile",
                   command=self._import_nina_profile).grid(
            row=0, column=1, padx=4, pady=4, sticky="w")
        self._settings_nina_profile_status = ttk.Label(data_grid, text="",
            font=("Helvetica", 9), foreground="#556677")
        self._settings_nina_profile_status.grid(row=0, column=2, padx=(8, 4), pady=4, sticky="w")
        self._update_nina_profile_status()

        # Catalog re-download
        ttk.Label(data_grid, text="NGC/IC catalog:").grid(
            row=1, column=0, padx=4, pady=4, sticky="w")
        ttk.Button(data_grid, text="Re-download",
                   command=self._settings_redownload_catalog).grid(
            row=1, column=1, padx=4, pady=4, sticky="w")
        self._settings_catalog_status = ttk.Label(data_grid, text="",
            font=("Helvetica", 9), foreground="#4caf50")
        self._settings_catalog_status.grid(row=1, column=2, padx=(8, 4), pady=4, sticky="w")
        self._update_catalog_status()

        # DSS cache clear
        ttk.Label(data_grid, text="DSS image cache:").grid(
            row=2, column=0, padx=4, pady=4, sticky="w")
        ttk.Button(data_grid, text="Clear cache",
                   command=self._settings_clear_dss_cache).grid(
            row=2, column=1, padx=4, pady=4, sticky="w")
        self._settings_dss_label = ttk.Label(data_grid, text="",
            font=("Helvetica", 9), foreground="#556677")
        self._settings_dss_label.grid(row=2, column=2, padx=(8, 4), pady=4, sticky="w")

        # ── Sky-map catalog tiers ─────────────────────────────────────────
        sky_card = self._make_settings_card(outer, "SKY-MAP CATALOG", "#5dcaa5")
        ttk.Label(sky_card,
                  text="Extra star & deep-sky detail for the embedded sky map.",
                  font=("Helvetica", 9), foreground="#556677").pack(
            anchor="w", padx=8, pady=(0, 2))
        sky_grid = ttk.Frame(sky_card)
        sky_grid.pack(fill="x", padx=8, pady=4)
        self._skymap_tier_var = tk.StringVar(value=self._skymap_active_tier())
        self._skymap_rows = {}
        for _i, _tier in enumerate(("lean", "extended", "full")):
            _spec = SKYMAP_TIERS[_tier]
            _rb = ttk.Radiobutton(sky_grid, text=_spec["label"], value=_tier,
                                  variable=self._skymap_tier_var,
                                  command=lambda tr=_tier: self._skymap_select_tier(tr))
            _rb.grid(row=_i, column=0, padx=4, pady=4, sticky="w")
            ttk.Label(sky_grid, text=_spec["blurb"], font=("Helvetica", 9),
                      foreground="#556677").grid(row=_i, column=1, padx=4, pady=4, sticky="w")
            _btn = ttk.Button(sky_grid, text="Download",
                              command=lambda tr=_tier: self._skymap_download_tier(tr))
            _btn.grid(row=_i, column=2, padx=4, pady=4, sticky="w")
            _prog = ttk.Progressbar(sky_grid, length=130, mode="determinate")
            _prog.grid(row=_i, column=2, padx=4, pady=4, sticky="w")
            _prog.grid_remove()
            _status = ttk.Label(sky_grid, text="", font=("Helvetica", 9))
            _status.grid(row=_i, column=3, padx=(8, 4), pady=4, sticky="w")
            self._skymap_rows[_tier] = {"rb": _rb, "status": _status,
                                        "prog": _prog, "btn": _btn}
        self._update_skymap_catalog_ui()

        # About line
        ttk.Label(outer, text=f"Lightbucket Astro Planner  ·  Data: {self.data_path}",
                  font=("Helvetica", 9), foreground="#334455").pack(anchor="center", pady=(12, 0))

        # Wheel-scroll the whole panel (canvas + every card widget).
        self._bind_settings_mousewheel(self._settings_canvas)
        self._bind_settings_mousewheel_recursive(outer)

    # ═══════════════════════════════════════════════════════════════════
    # SETTINGS PANEL HANDLERS
    # ═══════════════════════════════════════════════════════════════════

    def _make_settings_card(self, parent, title, accent_color):
        """Create a settings section card with a left accent line and section title."""
        card = tk.Frame(parent, bg="#131f2e", highlightthickness=0)
        card.pack(fill="x", pady=(0, 10))

        # Left accent line using a thin frame
        accent = tk.Frame(card, bg=accent_color, width=3)
        accent.pack(side="left", fill="y")

        inner = ttk.Frame(card)
        inner.pack(side="left", fill="both", expand=True, padx=(8, 0))

        ttk.Label(inner, text=title, font=("Helvetica", 10, "bold"),
                  foreground=accent_color).pack(anchor="w", padx=8, pady=(8, 2))

        return inner

    def _settings_autodetect_location(self):
        """Auto-detect location, fill fields, and save immediately."""
        self._settings_loc_status.config(text="Detecting…", foreground="orange")
        self.root.update_idletasks()
        lat, lon = self._autodetect_location()
        if lat is not None:
            self._settings_lat_var.set(f"{lat:.4f}")
            self._settings_lon_var.set(f"{lon:.4f}")
            # Save immediately
            self.data["location"] = {"lat": lat, "lon": lon}
            self.save_data()
            self.refresh_twilight_header()
            self.refresh_moon_header()
            self._settings_loc_status.config(
                text="✅ Detected — press ☆ Save to keep it as a named profile",
                foreground="#4caf50")
        else:
            self._settings_loc_status.config(text="❌ Failed — enter manually and press Save", foreground="red")

    def _settings_save_location(self):
        """Save the observer location from manually-entered settings fields."""
        try:
            lat = float(self._settings_lat_var.get())
            lon = float(self._settings_lon_var.get())
        except ValueError:
            messagebox.showerror("Invalid", "Please enter valid latitude and longitude.")
            return
        self.data["location"] = {"lat": lat, "lon": lon}
        # If a named profile is active, keep its stored coordinates in sync.
        active = self.data.get("active_location", "")
        prof = self._find_location(active) if active else None
        if prof is not None:
            prof["lat"], prof["lon"] = lat, lon
        self.save_data()
        self.refresh_twilight_header()
        self.refresh_moon_header()
        self._refresh_location_dropdowns()
        self._settings_loc_status.config(
            text=("✅ Location saved" + (f" to '{active}'" if prof is not None else "")),
            foreground="#4caf50")

    def _settings_save_preferences(self):
        """Save analysis preferences to the data file."""
        global C_VALUE
        try:
            c_val = int(self._settings_c_var.get())
            if c_val <= 0:
                raise ValueError
            C_VALUE = c_val
        except ValueError:
            messagebox.showerror("Invalid", "C-constant must be a positive integer.")
            return

        self.data.setdefault("settings", {})
        self.data["settings"]["default_bortle"] = self._settings_default_bortle.get()
        self.data["settings"]["default_alloc_hrs"] = self._settings_default_alloc_hrs.get()
        self.data["settings"]["min_alt"] = self._settings_min_alt.get()
        self.data["settings"]["auto_update"] = self._settings_auto_update.get()
        self.data["settings"]["c_value"] = C_VALUE

        # Apply default bortle to the active Bortle choice
        if self._settings_default_bortle.get() in BORTLE_FACTORS:
            self.bortle_choice.set(self._settings_default_bortle.get())

        # Apply auto-update setting
        if self._settings_auto_update.get():
            self.auto_update_enabled = True

        self.save_data()
        messagebox.showinfo("Saved", "Preferences saved successfully.")

    def _settings_redownload_catalog(self):
        """Delete existing catalog and re-download."""
        try:
            self.catalog_path.unlink(missing_ok=True)
        except Exception:
            pass
        try:
            self.addendum_path.unlink(missing_ok=True)
        except Exception:
            pass
        self.ensure_catalog_exists()
        self._update_catalog_status()

    def _update_catalog_status(self):
        """Update the catalog status label in the settings panel.

        Shows a per-catalog breakdown when the catalog is loaded — e.g.
        "✓ Catalog loaded (14,300 objects)
            8,026 NGC · 5,566 IC · 109 Messier · 109 Caldwell · 313 Sharpless"
        """
        if hasattr(self, '_settings_catalog_status'):
            count = len(self.targets) if self.targets else 0
            if count > 0:
                counts = self._count_targets_by_catalog()
                # Build breakdown line — only show catalogs with non-zero counts
                # so a fallback-only load (110 Messier objects, nothing else)
                # doesn't show a wall of zeros.
                segments = []
                for cat in ("NGC", "IC", "Messier", "Caldwell", "Sharpless"):
                    if counts.get(cat, 0):
                        segments.append(f"{counts[cat]:,} {cat}")
                # Total unique objects (deduped by id) — usually a few hundred
                # less than len(self.targets) because of regex-alias keys.
                seen_ids = {t.get("id") for t in self.targets.values() if t.get("id")}
                unique = len(seen_ids)
                header = f"✓ Catalog loaded ({unique:,} objects)"
                breakdown = "   " + "  ·  ".join(segments) if segments else ""
                full = f"{header}\n{breakdown}" if breakdown else header
                self._settings_catalog_status.config(text=full, foreground="#4caf50")
            elif self.catalog_path.exists():
                self._settings_catalog_status.config(
                    text="Catalog file present (loading…)", foreground="#f59e0b")
            else:
                self._settings_catalog_status.config(
                    text="No catalog loaded", foreground="#f59e0b")

    def _settings_clear_dss_cache(self):
        """Clear the cached DSS image."""
        self._cached_dss_img = None
        self._cached_dss_target = None
        self._cached_dss_survey_deg = None
        messagebox.showinfo("Cache Cleared", "DSS image cache has been cleared.")

    def _update_nina_profile_status(self):
        """Update the NINA profile status label in the settings panel.

        Shows the imported filter configuration (LRGB / narrowband + bandwidth)
        and, when present, the most recently imported telescope.  Scope info is
        read from ``nina_filters['scope']`` — a small breadcrumb dropped by the
        importer so we can report it here without keeping an entirely separate
        ``nina_profile`` block in the JSON.
        """
        if not hasattr(self, '_settings_nina_profile_status'):
            return
        nf = self.data.get("nina_filters", {})
        imported_date = nf.get("imported_date")
        lrgb = nf.get("lrgb")
        nb = nf.get("narrowband")
        scope = nf.get("scope")  # {"name": ..., ...} or None — written by importer
        if imported_date or lrgb or nb or scope:
            parts = []
            if scope and scope.get("name"):
                parts.append(f"Scope: {scope['name']}")
            if lrgb:
                parts.append(f"LRGB: {', '.join(lrgb)}")
            if nb:
                bw = nf.get("bandwidth_nm")
                bw_str = f" ({int(bw)}nm)" if bw else ""
                parts.append(f"NB: {', '.join(nb)}{bw_str}")
            filter_list = "  ·  ".join(parts) if parts else ""
            date_str = imported_date or "unknown date"
            self._settings_nina_profile_status.config(
                text=f"✓ Profile imported {date_str}" +
                     (f"\n   {filter_list}" if filter_list else ""),
                foreground="#4caf50")
        else:
            self._settings_nina_profile_status.config(
                text="No profile imported yet",
                foreground="#556677")

    def _refresh_settings_display(self):
        """Refresh dynamic status labels on the Settings panel.

        Touching widget content forces the Cocoa Tk backend to repaint the
        settings frame, fixing the blank-tab issue on macOS.
        """
        self._update_catalog_status()
        self._update_nina_profile_status()
        # Reload saved location into the fields
        saved_lat, saved_lon = self._get_saved_location()
        if saved_lat is not None:
            self._settings_lat_var.set(f"{saved_lat:.4f}")
            self._settings_lon_var.set(f"{saved_lon:.4f}")

    def _get_default_alloc_hrs(self):
        """Return the default allocated hours from settings, or 4.0 as final fallback."""
        try:
            val = float(self.data.get("settings", {}).get("default_alloc_hrs", "0"))
            return val if val > 0 else 4.0
        except (ValueError, TypeError):
            return 4.0


    def _get_sessions_dir(self):
        """Return (and create) the sessions subfolder inside the data folder."""
        d = self.data_path.parent / "sessions"
        d.mkdir(parents=True, exist_ok=True)
        return d

    # ═══════════════════════════════════════════════════════════════════
    # TONIGHT'S PLAN — card rendering, selection, totals
    # ═══════════════════════════════════════════════════════════════════

    def _refresh_plan_tree(self):
        """Repopulate the plan cards and update totals.

        For multi-filter targets (consecutive rows with the same target_id),
        the Start column shows a running cumulative offset so each filter
        reflects its approximate actual start time rather than all sharing
        the same target start time.
        """
        if not hasattr(self, "_plan_cards_inner"):
            return

        # Toggle empty-state message
        if hasattr(self, "_plan_empty_label"):
            if self._plan_entries:
                self._plan_empty_label.pack_forget()
            elif not self._plan_empty_label.winfo_ismapped():
                self._plan_empty_label.pack(before=self._plan_tree_frame, pady=(20, 10))

        # Clear existing cards
        for w in self._plan_cards_inner.winfo_children():
            w.destroy()
        self._plan_card_widgets = []

        # Pre-compute per-row display start times using a running offset
        display_starts = []
        running_offset_h = 0.0
        prev_group_key = None

        for e in self._plan_entries:
            base_str = e.get("start_time") or e.get("win_start") or ""
            group_key = (e["target_id"], e.get("scope", ""))

            if group_key != prev_group_key:
                running_offset_h = 0.0
                prev_group_key = group_key

            if base_str and running_offset_h > 0:
                try:
                    parts = base_str.split(":")
                    base_h = int(parts[0]) + int(parts[1]) / 60.0
                    disp_h = (base_h + running_offset_h) % 24.0
                    disp_str = f"{int(disp_h):02d}:{int(round((disp_h % 1) * 60)):02d}"
                except (ValueError, IndexError):
                    disp_str = base_str
            else:
                disp_str = base_str or "—"

            display_starts.append(disp_str)
            running_offset_h += e.get("total_int_hrs", 0.0)

        old_sel = self._plan_card_selected

        FILTER_COLORS = {
            # Narrowband
            "Ha": ("#3a2040", "#ce93d8"), "H-alpha": ("#3a2040", "#ce93d8"),
            "Halpha": ("#3a2040", "#ce93d8"), "H-a": ("#3a2040", "#ce93d8"),
            "O3": ("#1e2a3a", "#64b5f6"), "OIII": ("#1e2a3a", "#64b5f6"),
            "O-III": ("#1e2a3a", "#64b5f6"),
            "S2": ("#2a3520", "#8bc34a"), "SII": ("#2a3520", "#8bc34a"),
            "S-II": ("#2a3520", "#8bc34a"),
            "Hb": ("#1e2a3a", "#81d4fa"), "H-beta": ("#1e2a3a", "#81d4fa"),
            "Hbeta": ("#1e2a3a", "#81d4fa"), "H-b": ("#1e2a3a", "#81d4fa"),
            # LRGB — short names
            "L": ("#222830", "#aabbcc"), "Lum": ("#222830", "#aabbcc"),
            "Luminance": ("#222830", "#aabbcc"),
            "R": ("#301818", "#ff6b6b"), "Red": ("#301818", "#ff6b6b"),
            "G": ("#183018", "#66d966"), "Green": ("#183018", "#66d966"),
            "Grn": ("#183018", "#66d966"),
            "B": ("#181830", "#6b9bff"), "Blue": ("#181830", "#6b9bff"),
            "Blu": ("#181830", "#6b9bff"),
            # Filter mode strings (when single filter)
            "Mono Lum": ("#222830", "#aabbcc"),
            "LRGB": ("#222830", "#aabbcc"),
        }

        def _get_filter_color(filt_name):
            """Look up filter badge colors with case-insensitive fallback."""
            if not filt_name or filt_name == "—":
                return None, None
            # Exact match first
            result = FILTER_COLORS.get(filt_name)
            if result:
                return result
            # Case-insensitive match
            filt_lower = filt_name.strip().lower()
            for key, val in FILTER_COLORS.items():
                if key.lower() == filt_lower:
                    return val
            # Substring match for narrowband with bandwidth suffix like "Ha (7nm)"
            for key in ("Ha", "O3", "S2", "Hb"):
                if key.lower() in filt_lower:
                    return FILTER_COLORS[key]
            # Color keyword match
            if "red" in filt_lower:
                return ("#301818", "#ff6b6b")
            if "green" in filt_lower or "grn" in filt_lower:
                return ("#183018", "#66d966")
            if "blue" in filt_lower or "blu" in filt_lower:
                return ("#181830", "#6b9bff")
            if "lum" in filt_lower:
                return ("#222830", "#aabbcc")
            if "narrow" in filt_lower:
                return ("#3a2040", "#ce93d8")
            # Default gray
            return ("#222830", "#aabbcc")

        for i, (e, start_str) in enumerate(zip(self._plan_entries, display_starts)):
            is_selected = (i == old_sel)
            border_col = "#38bdf8" if is_selected else "#1e2d3e"
            accent_col = "#38bdf8" if is_selected else "#2e4a63"

            card = tk.Frame(self._plan_cards_inner, bg="#131f2e",
                            highlightthickness=1, highlightbackground=border_col)
            card.pack(fill="x", padx=2, pady=2)

            # Left accent
            tk.Frame(card, bg=accent_col, width=3).pack(side="left", fill="y")

            content = tk.Frame(card, bg="#131f2e")
            content.pack(side="left", fill="both", expand=True, padx=(8, 10), pady=5)

            row = tk.Frame(content, bg="#131f2e")
            row.pack(fill="x")

            # Number circle
            circle_bg = "#38bdf8" if is_selected else "#2e4a63"
            circle_fg = "#0e1a28" if is_selected else "#aaddff"
            num_cv = tk.Canvas(row, width=26, height=26, bg="#131f2e", highlightthickness=0)
            num_cv.pack(side="left", padx=(0, 8))
            num_cv.create_oval(1, 1, 25, 25, fill=circle_bg, outline="")
            num_cv.create_text(13, 13, text=str(i + 1), fill=circle_fg,
                               font=("Helvetica", 10, "bold"))

            # Target + filter + equipment
            info = tk.Frame(row, bg="#131f2e")
            info.pack(side="left", fill="x", expand=True)

            name_row = tk.Frame(info, bg="#131f2e")
            name_row.pack(anchor="w")
            tk.Label(name_row, text=e["target_id"], bg="#131f2e", fg="#ffffff",
                     font=("Helvetica", 11, "bold")).pack(side="left", padx=(0, 5))
            if e.get("common"):
                tk.Label(name_row, text=e["common"], bg="#131f2e", fg="#7eb8d4",
                         font=("Helvetica", 10)).pack(side="left", padx=(0, 5))
            # Filter badge
            filt = e.get("filter") or ""
            if filt and filt != "—":
                fb, ff = _get_filter_color(filt)
                if fb:
                    tk.Label(name_row, text=filt, bg=fb, fg=ff,
                             font=("Helvetica", 8), padx=5, pady=0).pack(side="left")

            # Equipment line
            equip_parts = [p for p in [e.get("scope", ""), e.get("camera", "")] if p]
            if equip_parts:
                tk.Label(info, text="  ·  ".join(equip_parts), bg="#131f2e",
                         fg="#445566", font=("Helvetica", 9)).pack(anchor="w")

            # Right-side metrics
            metrics = tk.Frame(row, bg="#131f2e")
            metrics.pack(side="right")

            # Window
            win_str = f"{e['win_start']}–{e['win_end']}" if e.get("win_start") else "—"
            _alloc = e.get("allocated_hrs", 0)
            win_col = "#4caf50" if _alloc >= 2 else ("#f59e0b" if _alloc >= 0.5 else "#556677")
            tk.Label(metrics, text=win_str, bg="#131f2e", fg=win_col,
                     font=("Helvetica", 10, "bold")).grid(row=0, column=0, padx=(0, 12))
            tk.Label(metrics, text="window", bg="#131f2e", fg="#445566",
                     font=("Helvetica", 8)).grid(row=1, column=0, padx=(0, 12))

            # Start
            tk.Label(metrics, text=start_str, bg="#131f2e", fg="#ffffff",
                     font=("Helvetica", 10)).grid(row=0, column=1, padx=(0, 10))
            tk.Label(metrics, text="start", bg="#131f2e", fg="#445566",
                     font=("Helvetica", 8)).grid(row=1, column=1, padx=(0, 10))

            # Sub-exp
            tk.Label(metrics, text=f"{e['exp_s']}s", bg="#131f2e", fg="#f59e0b",
                     font=("Helvetica", 10)).grid(row=0, column=2, padx=(0, 10))
            tk.Label(metrics, text="sub", bg="#131f2e", fg="#445566",
                     font=("Helvetica", 8)).grid(row=1, column=2, padx=(0, 10))

            # Alloc
            tk.Label(metrics, text=f"{e['allocated_hrs']}h", bg="#131f2e", fg="#ffffff",
                     font=("Helvetica", 10), width=4).grid(row=0, column=3, padx=(0, 8))
            tk.Label(metrics, text="alloc", bg="#131f2e", fg="#445566",
                     font=("Helvetica", 8)).grid(row=1, column=3, padx=(0, 8))

            # Subs
            tk.Label(metrics, text=str(e["n_subs"]), bg="#131f2e", fg="#ffffff",
                     font=("Helvetica", 10), width=3).grid(row=0, column=4, padx=(0, 8))
            tk.Label(metrics, text="subs", bg="#131f2e", fg="#445566",
                     font=("Helvetica", 8)).grid(row=1, column=4, padx=(0, 8))

            # Integration time
            tk.Label(metrics, text=f"{e['total_int_hrs']:.2f}h", bg="#131f2e", fg="#38bdf8",
                     font=("Helvetica", 10, "bold"), width=5).grid(row=0, column=5, padx=(0, 0))
            tk.Label(metrics, text="integ", bg="#131f2e", fg="#445566",
                     font=("Helvetica", 8)).grid(row=1, column=5, padx=(0, 0))

            self._plan_card_widgets.append(card)

            # Click and double-click bindings
            def _bind_all(widget, idx=i):
                widget.bind("<Button-1>", lambda e, ii=idx: self._plan_card_click(ii))
                widget.bind("<Double-1>", lambda e, ii=idx: self._plan_card_dblclick(ii))
            _bind_all(card)
            _bind_all(content)
            _bind_all(row)
            _bind_all(info)
            _bind_all(name_row)
            _bind_all(metrics)
            _bind_all(num_cv)
            for child in name_row.winfo_children():
                _bind_all(child)
            for child in metrics.winfo_children():
                _bind_all(child)

            # Mousewheel bindings — must be applied to every descendant,
            # otherwise the wheel stops working once cards cover the canvas.
            self._bind_plan_mousewheel_recursive(card)

        # Restore selection
        if old_sel is not None and old_sel < len(self._plan_entries):
            self._plan_card_selected = old_sel
        elif self._plan_entries:
            self._plan_card_selected = 0
        else:
            self._plan_card_selected = None

        self._update_plan_totals()
        self._draw_queue_gantt()

        # Newly created tk widgets have hardcoded day-mode colours.  When night
        # mode is active, re-apply it so both the fresh plan cards and any
        # Session-tab widgets disturbed by update_idletasks() are re-themed.
        if self.night_mode:
            self._apply_night_mode()

    def _plan_card_click(self, idx):
        """Single-click on a plan card — select it."""
        old = self._plan_card_selected
        self._plan_card_selected = idx
        self._plan_card_update_highlight(old)
        self._plan_card_update_highlight(idx)

    def _plan_card_dblclick(self, idx):
        """Double-click on a plan card — select and open edit dialog."""
        self._plan_card_click(idx)
        self._plan_load_target(None)

    def _plan_card_update_highlight(self, idx):
        """Update the visual highlight state of a single plan card."""
        if idx is None or idx >= len(self._plan_card_widgets):
            return
        card = self._plan_card_widgets[idx]
        is_selected = (idx == self._plan_card_selected)
        border_col = "#38bdf8" if is_selected else "#1e2d3e"
        accent_col = "#38bdf8" if is_selected else "#2e4a63"
        circle_bg = "#38bdf8" if is_selected else "#2e4a63"
        circle_fg = "#0e1a28" if is_selected else "#aaddff"
        try:
            card.configure(highlightbackground=border_col)
            children = card.winfo_children()
            if children:
                children[0].configure(bg=accent_col)
            content = children[1] if len(children) > 1 else None
            if content:
                top_row = content.winfo_children()[0] if content.winfo_children() else None
                if top_row:
                    for w in top_row.winfo_children():
                        if isinstance(w, tk.Canvas) and w.winfo_reqwidth() <= 28:
                            w.delete("all")
                            w.create_oval(1, 1, 25, 25, fill=circle_bg, outline="")
                            w.create_text(13, 13, text=str(idx + 1), fill=circle_fg,
                                          font=("Helvetica", 10, "bold"))
                            break
        except Exception:
            pass

    def _update_plan_totals(self):
        """Recalculate and display totals below the plan treeview."""
        if not hasattr(self, "_plan_total_alloc_var"):
            return
        if not self._plan_entries:
            self._plan_total_alloc_var.set("")
            self._plan_total_int_var.set("")
            self._plan_total_subs_var.set("")
            return
        total_alloc = sum(e["allocated_hrs"] for e in self._plan_entries)
        total_int   = sum(e["total_int_hrs"] for e in self._plan_entries)
        total_subs  = sum(e["n_subs"]        for e in self._plan_entries)
        self._plan_total_alloc_var.set(f"Allocated: {total_alloc:.1f}h")
        self._plan_total_int_var.set(  f"Integration: {total_int:.2f}h")
        self._plan_total_subs_var.set( f"Total subs: {total_subs}")

    def _remove_from_plan(self):
        """Remove the selected card from tonight's plan."""
        idx = self._plan_card_selected
        if idx is None or idx < 0 or idx >= len(self._plan_entries):
            return
        del self._plan_entries[idx]
        # Adjust selection
        if self._plan_entries:
            self._plan_card_selected = min(idx, len(self._plan_entries) - 1)
        else:
            self._plan_card_selected = None
        self._refresh_plan_tree()

    def _clear_plan(self):
        """Clear all entries from tonight's plan after confirmation."""
        if not self._plan_entries:
            return
        if messagebox.askyesno("Clear Plan",
                "Remove all entries from tonight's plan?"):
            self._plan_entries.clear()
            self._plan_card_selected = None
            self._refresh_plan_tree()

    # ═══════════════════════════════════════════════════════════════════
    # SESSION I/O & NINA EXPORT
    # ═══════════════════════════════════════════════════════════════════

    # Current session-file schema version.  Bump and add migration logic
    # in _migrate_session when the entry dict shape changes.
    _SESSION_SCHEMA = 3

    @staticmethod
    def _migrate_session(data):
        """Apply forward migrations to a loaded session dict, return the entries list.

        Accepts both old (no session_schema key) and new session files.

        Migration history
        -----------------
        session_schema 0 → 1  (Beta1)
            • No entry-level changes — this version just stamps the schema
              so future entry-shape changes can be detected and migrated.
        session_schema 1 → 2  (Beta4_5)
            • Abbreviate ``filter_mode`` values from ``"Narrowband (Xnm)"``
              to ``"NB (Xnm)"`` in every plan entry to match the new UI labels.
        session_schema 2 → 3  (Beta4_5)
            • Abbreviate ``filter_mode`` values from ``"None (Luminance)"``
              to ``"Mono Lum"`` in every plan entry to match the new UI labels.
        """
        v = data.get("session_schema", 0)
        entries = data.get("entries", [])

        if v < 2:
            for e in entries:
                fm = e.get("filter_mode")
                if isinstance(fm, str) and fm.startswith("Narrowband "):
                    e["filter_mode"] = "NB " + fm[len("Narrowband "):]
            v = 2

        if v < 3:
            for e in entries:
                if e.get("filter_mode") == "None (Luminance)":
                    e["filter_mode"] = "Mono Lum"
            v = 3

        return entries

    def _save_session_as(self):
        """Prompt for a session name and save to a named .json file.

        Returns ``True`` if the file was written successfully, ``False`` if the
        user cancelled the dialog or the write failed.  The return value is used
        by :meth:`_on_close` to decide whether to proceed with shutdown.
        """
        if not self._plan_entries:
            messagebox.showwarning("Empty Plan",
                "Tonight's plan is empty — add some targets first.")
            return False
        default = datetime.now().strftime("Session_%Y-%m-%d")
        path = filedialog.asksaveasfilename(
            title="Save Session",
            initialdir=str(self._get_sessions_dir()),
            initialfile=default,
            defaultextension=".json",
            filetypes=[("Session files", "*.json"), ("All files", "*.*")],
        )
        if not path:
            return False
        try:
            payload = {
                "session_schema": self._SESSION_SCHEMA,
                "name":    Path(path).stem,
                "saved":   datetime.now().strftime("%Y-%m-%d %H:%M"),
                "entries": self._plan_entries,
            }
            with open(path, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2)
            messagebox.showinfo("Saved", f"Session saved to:\n{path}")
            return True
        except OSError as e:
            messagebox.showerror("Save Failed", f"Could not write file.\n\nDetail: {e}")
            return False

    def _export_session(self):
        """Prompt for a file name and save tonight's plan as a formatted text report."""
        if not self._plan_entries:
            messagebox.showwarning("Empty Plan",
                "Tonight's plan is empty — add some targets first.")
            return
        default = datetime.now().strftime("Session_%Y-%m-%d")
        path = filedialog.asksaveasfilename(
            title="Save Session",
            initialdir=str(self._get_sessions_dir()),
            initialfile=default,
            defaultextension=".txt",
            filetypes=[("Text files", "*.txt"), ("All files", "*.*")],
        )
        if not path:
            return
        try:
            now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
            date_str = datetime.now().strftime("%Y-%m-%d")
            session_name = Path(path).stem
            lines = []
            lines.append(f"Lightbucket Astro Planner  —  Session: {session_name}")
            lines.append(f"Saved: {now_str}")
            lines.append("=" * 80)
            lines.append("")

            # Group entries by (target_id, scope) so that the same object imaged
            # with two different telescopes gets its own section in the report.
            # Entries sharing the same target_id AND scope are treated as one setup
            # (e.g. multiple filters on the same rig) and appear under one header.
            seen_groups = []   # list of (target_id, scope) tuples in encounter order
            for e in self._plan_entries:
                key = (e["target_id"], e.get("scope", ""))
                if key not in seen_groups:
                    seen_groups.append(key)

            total_targets = len(seen_groups)
            target_num = 0

            for (tid, scope_key) in seen_groups:
                target_num += 1
                # Grab all entries for this target+scope combo
                target_entries = [e for e in self._plan_entries
                                  if e["target_id"] == tid and e.get("scope", "") == scope_key]
                first = target_entries[0]

                lines.append("━" * 80)
                lines.append(f"TARGET {target_num} of {total_targets}")
                lines.append("━" * 80)
                lines.append("")

                # Report snapshot (strip and re-emit)
                report = first.get("report_text", "").strip()
                if report:
                    lines.append(report)
                else:
                    lines.append(f"TARGET: {first['target_id']} | {first.get('common', '')}")
                lines.append("")

                # Plan summary rows for each filter within this rig
                for e in target_entries:
                    filt_str = f"  Filter:   {e['filter']}" if e.get("filter") else ""
                    win_str  = f"{e['win_start']}–{e['win_end']}" if e.get("win_start") else "—"
                    ra_str   = self._fmt_ra_hms(e["ra_deg"])  if e.get("ra_deg")  is not None else "—"
                    dec_str  = self._fmt_dec_dms(e["dec_deg"]) if e.get("dec_deg") is not None else "—"
                    lines.append(f"  RA / Dec: {ra_str}  /  {dec_str}")
                    lines.append(f"  Scope:    {e['scope']}")
                    lines.append(f"  Camera:   {e['camera']}")
                    if filt_str:
                        lines.append(filt_str)
                    lines.append(f"  Bortle:   {e['bortle']}")
                    lines.append(f"  Exposure: {e['exp_s']}s × {e['n_subs']} subs  =  {e['total_int_hrs']:.2f}h integration")
                    lines.append(f"  Window:   {win_str}")
                    if len(target_entries) > 1:
                        lines.append("")

                lines.append("─" * 80)
                lines.append("")

            # Totals block
            total_subs  = sum(e["n_subs"]        for e in self._plan_entries)
            total_alloc = sum(e["allocated_hrs"]  for e in self._plan_entries)
            total_int   = sum(e["total_int_hrs"]  for e in self._plan_entries)
            lines.append("TOTALS")
            lines.append(f"  Targets:             {total_targets}")
            lines.append(f"  Total subs:          {total_subs}")
            lines.append(f"  Total allocated:     {total_alloc:.2f}h")
            lines.append(f"  Total integration:   {total_int:.2f}h")
            lines.append("=" * 80)

            with open(path, "w", encoding="utf-8") as f:
                f.write("\n".join(lines) + "\n")
            messagebox.showinfo("Saved", f"Session saved to:\n{path}")
        except OSError as e:
            messagebox.showerror("Save Failed", f"Could not write file.\n\nDetail: {e}")


    def _build_nina_xml(self, entries):
        """Build NINA .ninaTargetSet XML content for the given plan entries.

        Returns (xml_string, n_targets).
        """
        def _ra_parts(ra_deg):
            ra_h = (ra_deg % 360.0) / 15.0
            h    = int(ra_h)
            m    = int((ra_h - h) * 60)
            s    = (ra_h - h - m / 60.0) * 3600.0
            return h, m, s

        def _dec_parts(dec_deg):
            negative = dec_deg < 0
            dec_abs  = abs(dec_deg)
            d = int(dec_abs)
            m = int((dec_abs - d) * 60)
            s = (dec_abs - d - m / 60.0) * 3600.0
            return d, m, s, negative

        seen_groups = []
        for e in entries:
            key = (e["target_id"], e.get("scope", ""))
            if key not in seen_groups:
                seen_groups.append(key)

        lines = ['<?xml version="1.0" encoding="utf-8"?>']
        lines.append('<ArrayOfCaptureSequenceList '
                     'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" '
                     'xmlns:xsd="http://www.w3.org/2001/XMLSchema">')

        for (tid, scope_key) in seen_groups:
            target_entries = [e for e in entries
                              if e["target_id"] == tid and e.get("scope", "") == scope_key]
            first = target_entries[0]

            # Prefer the FOV-framed centre (catalog centre adjusted for any pan
            # of the framing box); fall back to the catalog centre when absent.
            ra_deg  = first.get("framed_ra_deg")
            if ra_deg is None:
                ra_deg = first.get("ra_deg", 0.0)
            dec_deg = first.get("framed_dec_deg")
            if dec_deg is None:
                dec_deg = first.get("dec_deg", 0.0)
            rah, ram, ras             = _ra_parts(ra_deg)
            decd, decm, decs, neg_dec = _dec_parts(dec_deg)
            pos_angle = first.get("rotation_angle", 0.0)

            common = first.get("common", "").strip()
            target_name = f"{tid} {common}".strip() if common else tid
            for ch, esc in [("&", "&amp;"), ("<", "&lt;"), (">", "&gt;"), ('"', "&quot;")]:
                target_name = target_name.replace(ch, esc)

            lines.append(
                f'  <CaptureSequenceList TargetName="{target_name}" Mode="STANDARD" '
                f'RAHours="{rah}" RAMinutes="{ram}" RASeconds="{ras:.2f}" '
                f'DecDegrees="{decd}" DecMinutes="{decm}" DecSeconds="{decs:.1f}" '
                f'PositionAngle="{pos_angle:.1f}" '
                f'Delay="0" SlewToTarget="false" AutoFocusOnStart="false" '
                f'CenterTarget="false" RotateTarget="false" StartGuiding="false" '
                f'AutoFocusOnFilterChange="false" AutoFocusAfterSetTime="false" '
                f'AutoFocusSetTime="30" AutoFocusAfterSetExposures="false" '
                f'AutoFocusSetExposures="10" AutoFocusAfterTemperatureChange="false" '
                f'AutoFocusAfterTemperatureChangeAmount="5" AutoFocusAfterHFRChange="false" '
                f'AutoFocusAfterHFRChangeAmount="10">'
            )

            for e in target_entries:
                exp_s  = e.get("exp_s", 1.0) or 1.0
                n_subs = e.get("n_subs", 1)  or 1
                filt_name = e.get("filter", "") or ""
                for ch, esc in [("&", "&amp;"), ("<", "&lt;"), (">", "&gt;"), ('"', "&quot;")]:
                    filt_name = filt_name.replace(ch, esc)
                lines.append('    <CaptureSequence>')
                lines.append('      <Enabled>true</Enabled>')
                lines.append(f'      <ExposureTime>{exp_s}</ExposureTime>')
                lines.append('      <ImageType>LIGHT</ImageType>')
                if filt_name:
                    lines.append('      <FilterType>')
                    lines.append(f'        <Name>{filt_name}</Name>')
                    lines.append('      </FilterType>')
                lines.append('      <Binning>')
                lines.append('        <X>1</X>')
                lines.append('        <Y>1</Y>')
                lines.append('      </Binning>')
                lines.append('      <Gain>-1</Gain>')
                lines.append('      <Offset>-1</Offset>')
                lines.append(f'      <TotalExposureCount>{n_subs}</TotalExposureCount>')
                lines.append('      <ProgressExposureCount>0</ProgressExposureCount>')
                lines.append('      <Dither>false</Dither>')
                lines.append('      <DitherAmount>1</DitherAmount>')
                lines.append('    </CaptureSequence>')

            neg_str = "true" if neg_dec else "false"
            lines.append('    <Coordinates>')
            lines.append(f'      <RA>{(ra_deg % 360.0) / 15.0:.10f}</RA>')
            lines.append(f'      <Dec>{dec_deg:.5f}</Dec>')
            lines.append('      <Epoch>J2000</Epoch>')
            lines.append('    </Coordinates>')
            lines.append(f'    <NegativeDec>{neg_str}</NegativeDec>')
            lines.append('  </CaptureSequenceList>')

        lines.append('</ArrayOfCaptureSequenceList>')
        return "\n".join(lines) + "\n", len(seen_groups)

    def _export_nina(self):
        """Export tonight's plan as N.I.N.A. CaptureSequenceList (.ninaTargetSet).

        If entries use more than one scope, a separate file is written per scope
        (the user picks a directory and filenames are auto-generated).  When all
        entries share a single scope the original single-file dialog is used.
        """
        if not self._plan_entries:
            messagebox.showwarning("Empty Plan",
                "Tonight's plan is empty \u2014 add some targets first.")
            return

        # Determine distinct scopes in the plan
        distinct_scopes = []
        for e in self._plan_entries:
            s = e.get("scope", "") or ""
            if s not in distinct_scopes:
                distinct_scopes.append(s)

        date_tag = datetime.now().strftime("Session_%Y-%m-%d")

        if len(distinct_scopes) <= 1:
            # ---------- single-scope: original behaviour ----------
            path = filedialog.asksaveasfilename(
                title="Export as NINA Target Set",
                initialdir=str(self._get_sessions_dir()),
                initialfile=date_tag,
                defaultextension=".ninaTargetSet",
                filetypes=[("NINA target set", "*.ninaTargetSet"), ("All files", "*.*")],
            )
            if not path:
                return
            xml, n_targets = self._build_nina_xml(self._plan_entries)
            try:
                with open(path, "w", encoding="utf-8") as f:
                    f.write(xml)
                messagebox.showinfo("Exported",
                    f"NINA target set exported with {n_targets} target(s):\n{path}")
            except OSError as exc:
                messagebox.showerror("Export Failed",
                    f"Could not write file.\n\nDetail: {exc}")
        else:
            # ---------- multi-scope: one file per scope ----------
            out_dir = filedialog.askdirectory(
                title="Choose folder for per-scope NINA exports",
                initialdir=str(self._get_sessions_dir()),
            )
            if not out_dir:
                return

            saved_paths = []
            for scope_name in distinct_scopes:
                scope_entries = [e for e in self._plan_entries
                                 if (e.get("scope", "") or "") == scope_name]
                # Build a filesystem-safe version of the scope name
                safe = re.sub(r'[<>:"/\\|?*]', '_', scope_name) if scope_name else "NoScope"
                fname = f"{date_tag}_{safe}.ninaTargetSet"
                fpath = os.path.join(out_dir, fname)

                xml, _ = self._build_nina_xml(scope_entries)
                try:
                    with open(fpath, "w", encoding="utf-8") as f:
                        f.write(xml)
                    saved_paths.append(fpath)
                except OSError as exc:
                    messagebox.showerror("Export Failed",
                        f"Could not write file for scope '{scope_name}'.\n\nDetail: {exc}")
                    return

            summary = "\n".join(os.path.basename(p) for p in saved_paths)
            messagebox.showinfo("Exported",
                f"Exported {len(saved_paths)} NINA target set(s) "
                f"to {out_dir}:\n\n{summary}")

    def _load_session(self):
        """Load a saved session file, prompting to save the current plan first."""
        if self._plan_entries:
            resp = messagebox.askyesnocancel(
                "Unsaved Plan",
                "Tonight's plan has entries that will be replaced.\n\n"
                "Save the current plan before loading?")
            if resp is None:        # Cancel
                return
            if resp:                # Yes — save first
                self._save_session_as()

        path = filedialog.askopenfilename(
            title="Load Session",
            initialdir=str(self._get_sessions_dir()),
            filetypes=[("Session files", "*.json"), ("All files", "*.*")],
        )
        if not path:
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            self._plan_entries = self._migrate_session(data)
            self._refresh_plan_tree()
            name = data.get("name", Path(path).stem)
            messagebox.showinfo("Loaded", f"Session '{name}' loaded with "
                                f"{len(self._plan_entries)} target(s).")
        except (OSError, json.JSONDecodeError) as e:
            messagebox.showerror("Load Failed", f"Could not read session file.\n\nDetail: {e}")

    # ═══════════════════════════════════════════════════════════════════
    # LOCATION & ALTITUDE-CHART HELPERS
    # ═══════════════════════════════════════════════════════════════════

    def _get_saved_location(self):
        """Return the observer's saved (lat, lon) in degrees, or (None, None) if unset."""
        loc = self.data.get("location", {})
        lat, lon = loc.get("lat"), loc.get("lon")
        if lat is not None and lon is not None:
            return float(lat), float(lon)
        return None, None

    # ── Named location profiles ───────────────────────────────────────────

    def _location_names(self):
        """Return saved location-profile names, in order."""
        return [p.get("name", "") for p in self.data.get("locations", []) if p.get("name")]

    def _find_location(self, name):
        """Return the saved location profile dict with this name, or None."""
        for p in self.data.get("locations", []):
            if p.get("name") == name:
                return p
        return None

    def _apply_location(self, name):
        """Make the named profile the active observer location.

        Copies the profile's lat/lon into ``data['location']`` — the single
        resolved location every calculation already reads — records it as the
        active profile, persists, and refreshes the twilight/moon headers.
        Keeps the Settings fields/dropdown in sync when that tab exists.
        Returns (lat, lon), or (None, None) if the name is unknown.
        """
        p = self._find_location(name)
        if p is None:
            return None, None
        lat, lon = p.get("lat"), p.get("lon")
        self.data["location"] = {"lat": lat, "lon": lon}
        self.data["active_location"] = name
        self._persist_data()
        self.refresh_twilight_header()
        self.refresh_moon_header()
        if hasattr(self, "_settings_lat_var") and lat is not None:
            self._settings_lat_var.set(f"{float(lat):.4f}")
            self._settings_lon_var.set(f"{float(lon):.4f}")
        if hasattr(self, "_settings_loc_choice"):
            self._settings_loc_choice.set(name)
        return (float(lat), float(lon)) if lat is not None else (None, None)

    def _refresh_location_dropdowns(self):
        """Repopulate location dropdowns (Settings + any open Visible Tonight popup)."""
        names  = self._location_names()
        active = self.data.get("active_location", "")
        if hasattr(self, "_settings_loc_combo"):
            try:
                self._settings_loc_combo["values"] = names
                self._settings_loc_choice.set(active)
            except tk.TclError:
                pass
        combo = getattr(self, "_vt_loc_combo", None)
        if combo is not None:
            try:
                combo["values"] = names
            except tk.TclError:
                pass

    def _on_settings_location_selected(self, event=None):
        """Settings location dropdown changed — activate the picked profile."""
        name = self._settings_loc_choice.get()
        if not name:
            return
        lat, lon = self._apply_location(name)
        if lat is not None:
            self._settings_loc_status.config(
                text=f"✅ Active location: {name}", foreground="#4caf50")

    def _alt_canvas_motion(self, event):
        """Crosshair + tooltip showing time & altitude at the hovered chart position."""
        meta = getattr(self, "_chart_meta", None)

        # Remove previous crosshair and on-canvas tooltip
        self.alt_canvas.delete("_crosshair")
        self.alt_canvas.delete("_chtip")

        # Also hide any leftover Toplevel from moon-bar hovers
        if self._chart_tip_win:
            self._chart_tip_win.destroy()
            self._chart_tip_win = None

        if meta is None:
            return

        lm = meta["lm"]; tm = meta["tm"]
        pw = meta["pw"]; ph = meta["ph"]
        x, y = event.x, event.y

        # Only act when inside the plot area
        if not (lm <= x <= lm + pw and tm <= y <= tm + ph):
            return

        # ── Crosshair lines ──────────────────────────────────────────────────
        nm = self.night_mode
        ch_col = "#880000" if nm else "#aabbcc"
        self.alt_canvas.create_line(x, tm, x, tm + ph,
                                    fill=ch_col, width=1, dash=(3, 3),
                                    tags="_crosshair")
        self.alt_canvas.create_line(lm, y, lm + pw, y,
                                    fill=ch_col, width=1, dash=(3, 3),
                                    tags="_crosshair")

        # ── Interpolate time and altitude ────────────────────────────────────
        frac     = (x - lm) / pw
        time_h   = (meta["scan_start_localh"] + frac * meta["total_jd_h"]) % 24.0
        time_str = f"{int(time_h):02d}:{int((time_h % 1) * 60):02d}"

        times_h = meta["times_h"]; alts = meta["alts"]
        idx     = max(0, min(len(times_h) - 1, int(round(frac * (len(times_h) - 1)))))
        alt_val = alts[idx]

        tip = f" {time_str}   {alt_val:.1f}° "

        # Check moon bars and prepend if near one
        for bar_x, moon_tip in self._moon_bar_hits:
            if abs(x - bar_x) <= 8:
                tip = moon_tip.replace("\n", "  |  ") + "\n" + tip
                break

        # ── Draw tooltip as canvas items (avoids Toplevel Leave interference) ─
        tip_x = x + 12
        tip_y = y - 14
        # Keep tooltip inside canvas bounds
        tip_x = min(tip_x, lm + pw - 80)
        tip_y = max(tip_y, tm + 4)

        # Shadow + background box, then text
        self.alt_canvas.create_text(tip_x + 1, tip_y + 1, text=tip,
                                    fill="#000000", anchor="nw",
                                    font=("Helvetica", 9, "bold"), tags="_chtip")
        self.alt_canvas.create_text(tip_x, tip_y, text=tip,
                                    fill="#ffee44", anchor="nw",
                                    font=("Helvetica", 9, "bold"), tags="_chtip")

    def _alt_canvas_leave(self, event=None):
        """Hide the chart crosshair and on-canvas tooltip."""
        self.alt_canvas.delete("_crosshair")
        self.alt_canvas.delete("_chtip")
        if self._chart_tip_win:
            self._chart_tip_win.destroy()
            self._chart_tip_win = None

    def _autodetect_location(self):
        """Try to get lat/lon from IP geolocation. Returns (lat, lon) or (None, None)."""
        try:
            with urllib.request.urlopen("https://ipapi.co/json/", timeout=5) as resp:
                data = json.loads(resp.read().decode())
                return float(data["latitude"]), float(data["longitude"])
        except (urllib.error.URLError, OSError,
                json.JSONDecodeError, KeyError, ValueError):
            # ipapi unreachable, rate-limited, or returned malformed JSON — fall through
            # to the second provider.
            pass
        try:
            with urllib.request.urlopen("https://ipinfo.io/json", timeout=5) as resp:
                data = json.loads(resp.read().decode())
                lat, lon = data.get("loc", "0,0").split(",")
                return float(lat), float(lon)
        except (urllib.error.URLError, OSError,
                json.JSONDecodeError, KeyError, ValueError):
            # Both providers failed — caller will prompt the user to enter coords manually.
            return None, None

    # ═══════════════════════════════════════════════════════════════════
    # VISIBLE TONIGHT DIALOG
    # ═══════════════════════════════════════════════════════════════════

    def show_visible_tonight(self):
        """Open the 'Visible Tonight' popup — scans the catalog for objects above min altitude."""
        popup = tk.Toplevel(self.root)
        popup.title("🌙 Visible Tonight")
        popup.geometry("740x620")
        popup.resizable(True, True)

        # --- Location frame ---
        loc_frame = ttk.LabelFrame(popup, text="Observer Location")
        loc_frame.pack(fill="x", padx=12, pady=8)

        saved_lat, saved_lon = self._get_saved_location()

        ttk.Label(loc_frame, text="Location:").grid(row=0, column=0, padx=5, pady=(6,2), sticky="e")
        loc_prof_var = tk.StringVar(value=self.data.get("active_location", ""))
        self._vt_loc_combo = ttk.Combobox(loc_frame, textvariable=loc_prof_var,
                                          state="readonly", width=20,
                                          values=self._location_names())
        self._vt_loc_combo.grid(row=0, column=1, columnspan=3, padx=5, pady=(6,2), sticky="w")
        ToolTip(self._vt_loc_combo, "Switch saved observing sites — re-runs the search.\n"
                                    "Manage sites in Settings → Observer Location.")

        ttk.Label(loc_frame, text="Latitude:").grid(row=1, column=0, padx=5, pady=2, sticky="e")
        lat_var = tk.StringVar(value=str(saved_lat) if saved_lat is not None else "")
        lat_entry = ttk.Entry(loc_frame, textvariable=lat_var, width=12)
        lat_entry.grid(row=1, column=1, padx=5, pady=2)

        ttk.Label(loc_frame, text="Longitude:").grid(row=1, column=2, padx=5, pady=2, sticky="e")
        lon_var = tk.StringVar(value=str(saved_lon) if saved_lon is not None else "")
        lon_entry = ttk.Entry(loc_frame, textvariable=lon_var, width=12)
        lon_entry.grid(row=1, column=3, padx=5, pady=2)

        ttk.Button(loc_frame, text="Auto-detect", command=lambda: do_autodetect()).grid(
            row=1, column=4, padx=(10,5), pady=2)

        def _on_vt_loc_selected(event=None):
            name = loc_prof_var.get()
            p = self._find_location(name)
            if not p:
                return
            lat_var.set(f"{float(p['lat']):.4f}")
            lon_var.set(f"{float(p['lon']):.4f}")
            self._apply_location(name)
            _run_search()
        self._vt_loc_combo.bind("<<ComboboxSelected>>", _on_vt_loc_selected)

        # Status row — pre-allocated so the dialog never shifts when text appears
        loc_status = ttk.Label(loc_frame, text="", width=30)
        loc_status.grid(row=2, column=0, columnspan=5, padx=8, pady=(0,6), sticky="w")

        def do_autodetect():
            loc_status.config(text="  Detecting…", foreground="orange")
            popup.update_idletasks()
            lat, lon = self._autodetect_location()
            if lat is not None:
                lat_var.set(f"{lat:.4f}")
                lon_var.set(f"{lon:.4f}")
                loc_status.config(text="  ✅ Auto-detected", foreground="green")
            else:
                loc_status.config(text="  ❌ Failed — enter manually", foreground="red")

        # Tracking flag so auto-rerun can guard against concurrent searches
        _search_running = [False]
        # Track the last lat/lon/alt that was actually searched, to detect manual edits
        _last_searched_loc = [None, None]
        _last_searched_alt = [None]

        def _auto_detect_then_run():
            loc_status.config(text="  Detecting…", foreground="orange")
            popup.update_idletasks()
            lat, lon = self._autodetect_location()
            if lat is not None:
                lat_var.set(f"{lat:.4f}")
                lon_var.set(f"{lon:.4f}")
                loc_status.config(text="  ✅ Auto-detected", foreground="green")
            else:
                loc_status.config(text="  ❌ Failed — enter manually", foreground="red")
            popup.after(100, _run_search)

        def _on_location_focusout(event):
            """Prompt user to confirm re-search if lat/lon changed since last search."""
            new_lat = lat_var.get().strip()
            new_lon = lon_var.get().strip()
            if [new_lat, new_lon] == _last_searched_loc:
                return  # nothing changed
            if _last_searched_loc[0] is None:
                return  # first run not done yet, auto-run will handle it
            if _search_running[0]:
                return
            if messagebox.askyesno(
                    "Location changed",
                    f"Location has changed to ({new_lat}, {new_lon}).\nRe-run the search with the new coordinates?",
                    parent=popup):
                _run_search()

        lat_entry.bind("<FocusOut>", _on_location_focusout)
        lon_entry.bind("<FocusOut>", _on_location_focusout)
        lat_entry.bind("<Return>", _on_location_focusout)
        lon_entry.bind("<Return>", _on_location_focusout)

        if saved_lat is not None:
            _initial_autorun = True
        else:
            _initial_autorun = False

        # --- Filter frame ---
        filt_frame = ttk.LabelFrame(popup, text="Filters")
        filt_frame.pack(fill="x", padx=12, pady=4)

        ttk.Label(filt_frame, text="Min. Altitude:").grid(row=0, column=0, padx=5, pady=4, sticky="e")
        # Seed the filter's min-altitude from the saved settings value so it
        # matches what the rest of the app uses (horizon line on the altitude
        # chart, imaging-window calculation, etc.).
        _saved_min_alt = int(self.data.get("settings", {}).get("min_alt", 20))
        min_alt_var = tk.StringVar(value=str(_saved_min_alt))
        min_alt_entry = ttk.Entry(filt_frame, textvariable=min_alt_var, width=6)
        min_alt_entry.grid(row=0, column=1, padx=5)
        ttk.Label(filt_frame, text="°  above horizon").grid(row=0, column=2, sticky="w")

        def _on_alt_focusout(event):
            """Prompt user to confirm re-search if min altitude changed since last search."""
            new_alt = min_alt_var.get().strip()
            if new_alt == _last_searched_alt[0]:
                return
            if _last_searched_alt[0] is None:
                return
            if _search_running[0]:
                return
            if messagebox.askyesno(
                    "Altitude changed",
                    f"Minimum altitude changed to {new_alt}°.\nRe-run the search?",
                    parent=popup):
                _run_search()

        min_alt_entry.bind("<FocusOut>", _on_alt_focusout)
        min_alt_entry.bind("<Return>", _on_alt_focusout)

        ttk.Label(filt_frame, text="Object Type:").grid(row=0, column=3, padx=10, sticky="e")
        type_var = tk.StringVar(value="All Types")
        type_combo = ttk.Combobox(filt_frame, textvariable=type_var, values=OBJECT_TYPE_FILTERS,
                                  state="readonly", width=18)
        type_combo.grid(row=0, column=4, padx=5)

        # --- Magnitude filter row ---
        ttk.Label(filt_frame, text="Max magnitude:").grid(row=1, column=0, padx=5, pady=4, sticky="e")
        mag_limit_var = tk.StringVar(value="")
        mag_limit_entry = ttk.Entry(filt_frame, textvariable=mag_limit_var, width=6)
        mag_limit_entry.grid(row=1, column=1, padx=5)
        ToolTip(mag_limit_entry, "Leave blank to show all magnitudes.\nBrighter objects have lower numbers (e.g. 9.0).\nFainter objects have higher numbers (e.g. 14.0).")

        use_surf_br_var = tk.BooleanVar(value=False)
        surf_br_chk = ttk.Checkbutton(filt_frame, text="Use surface brightness",
                                      variable=use_surf_br_var)
        surf_br_chk.grid(row=1, column=2, columnspan=2, padx=10, sticky="w")
        ToolTip(surf_br_chk, "V-mag: visual magnitude of the whole object.\n"
                              "Surface brightness: mag/arcmin\u00b2 \u2014 more useful\n"
                              "for extended objects like galaxies and nebulae.")

        mag_hint_var = tk.StringVar(value="V-mag — brighter objects have lower numbers (e.g. 9.0)")
        mag_hint_label = ttk.Label(filt_frame, textvariable=mag_hint_var,
                                   font=("Helvetica", 8), foreground="gray")
        mag_hint_label.grid(row=2, column=0, columnspan=5, padx=5, pady=(0, 4), sticky="w")

        def _on_surf_br_toggle():
            mag_limit_var.set("")
            if use_surf_br_var.get():
                mag_hint_var.set("Surface brightness (mag/arcmin\u00b2) \u2014 higher = fainter (e.g. 22.5)")
            else:
                mag_hint_var.set("V-mag \u2014 brighter objects have lower numbers (e.g. 9.0)")
            if not _search_running[0]:
                _run_search()

        use_surf_br_var.trace_add("write", lambda *_: _on_surf_br_toggle())

        # --- Seasonal checkbox ---
        chk_row = ttk.Frame(popup)
        chk_row.pack(pady=(4, 0))
        season_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(chk_row, text="Seasonal targets only", variable=season_var).pack()

        # --- Results frame ---
        res_frame = ttk.Frame(popup)
        res_frame.pack(fill="both", expand=True, padx=12, pady=4)

        cols = ("Name", "Common Name", "Type", "Max Alt", "Rise", "Mag", "Size")
        tree = ttk.Treeview(res_frame, columns=cols, show="headings", height=16)
        tree.heading("Name", text="Name")
        tree.heading("Common Name", text="Common Name")
        tree.heading("Type", text="Type")
        tree.heading("Max Alt", text="Max Alt °")
        tree.heading("Rise", text="Rise")
        tree.heading("Mag", text="Mag")
        tree.heading("Size", text="Size (′)")
        tree.column("Name", width=90)
        tree.column("Common Name", width=150)
        tree.column("Type", width=110)
        tree.column("Max Alt", width=70, anchor="center")
        tree.column("Rise", width=70, anchor="center")
        tree.column("Mag", width=55, anchor="center")
        tree.column("Size", width=70, anchor="center")

        vsb = ttk.Scrollbar(res_frame, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=vsb.set)
        tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")

        # ── Click-to-sort on column headers (single click; toggles asc/desc) ──
        _sort_state = {"col": "Max Alt", "desc": True}

        def _natural_key(s):
            parts = re.split(r'(\d+)', s or "")
            return [int(p) if p.isdigit() else p.lower() for p in parts]

        def _col_key(col, val):
            if col == "Max Alt":
                try:
                    return float(val.rstrip("°"))
                except ValueError:
                    return -999.0
            if col == "Rise":
                if val == "up":
                    return -1.0                  # observable from dusk -> earliest
                if val in ("—", ""):
                    return 1e9                   # never rises while dark -> last
                try:
                    hh, mm = val.split(":")
                    mins = int(hh) * 60 + int(mm)
                    if mins < 12 * 60:           # after-midnight times come later
                        mins += 24 * 60
                    return float(mins)
                except ValueError:
                    return 1e9
            if col == "Mag":
                try:
                    return float(val)
                except ValueError:
                    return 1e9                   # unknown magnitude -> last
            if col == "Size":
                try:
                    return float(val.split("×")[0].strip())
                except (ValueError, IndexError):
                    return -1.0
            return _natural_key(val)             # Name / Common Name / Type

        def _update_sort_indicators():
            mag_label = "SB" if use_surf_br_var.get() else "Mag"
            base = {"Name": "Name", "Common Name": "Common Name", "Type": "Type",
                    "Max Alt": "Max Alt °", "Rise": "Rise",
                    "Mag": mag_label, "Size": "Size (′)"}
            arrow = " ▼" if _sort_state["desc"] else " ▲"
            for c, txt in base.items():
                tree.heading(c, text=txt + (arrow if c == _sort_state["col"] else ""))

        def _apply_sort():
            col = _sort_state["col"]
            rows = [(_col_key(col, tree.set(iid, col)), iid)
                    for iid in tree.get_children("")]
            rows.sort(key=lambda r: r[0], reverse=_sort_state["desc"])
            for idx, (_, iid) in enumerate(rows):
                tree.move(iid, "", idx)
            _update_sort_indicators()

        def _sort_by(col):
            if _sort_state["col"] == col:
                _sort_state["desc"] = not _sort_state["desc"]
            else:
                _sort_state["col"] = col
                _sort_state["desc"] = False      # new column defaults to ascending
            _apply_sort()

        for _c in cols:
            tree.heading(_c, command=lambda cc=_c: _sort_by(cc))

        prog_label = ttk.Label(popup, text="")
        prog_label.pack()

        def _run_search():
            if _search_running[0]:
                return
            _search_running[0] = True
            tree.delete(*tree.get_children())
            prog_label.config(text="")
            try:
                lat = float(lat_var.get())
                lon = float(lon_var.get())
            except ValueError:
                messagebox.showwarning("Location needed",
                    "Please enter valid latitude and longitude.", parent=popup)
                _search_running[0] = False
                return

            # Save location directly — avoid save_data() which calls refresh_dropdowns()
            # and triggers on_parameter_change traces that queue analyze_framing callbacks.
            # Those callbacks would be flushed by update_idletasks() below, causing a
            # re-entrant _run_search call that clears the tree mid-search.
            self.data["location"] = {"lat": lat, "lon": lon}
            self._persist_data()
            self.refresh_twilight_header()
            self.refresh_moon_header()

            try:
                min_alt = float(min_alt_var.get())
            except ValueError:
                min_alt = 20.0

            type_filter = type_var.get()
            seasonal_only = season_var.get()
            mag_limit_str = mag_limit_var.get().strip()
            try:
                mag_limit = float(mag_limit_str)
            except ValueError:
                mag_limit = None   # no magnitude filter
            use_surf_br = use_surf_br_var.get()

            # Build candidate list
            month = _planning_local_noon().month
            if month in [12, 1, 2]:
                season = "Winter"
            elif month in [3, 4, 5]:
                season = "Spring"
            elif month in [6, 7, 8]:
                season = "Summer"
            else:
                season = "Autumn"

            if seasonal_only:
                seasonal_ids = set()
                for sid in SEASONAL_TARGETS[season]:
                    key = sid.replace(" ", "").upper()
                    t = self.targets.get(key) or self.common_names_map.get(key)
                    if t:
                        seasonal_ids.add(t["id"])
                candidates = [t for t in self.targets.values() if t["id"] in seasonal_ids]
                # deduplicate by id
                seen = set(); unique = []
                for t in candidates:
                    if t["id"] not in seen:
                        seen.add(t["id"]); unique.append(t)
                candidates = unique
            else:
                # Use all targets with known RA/Dec and reasonable size
                seen = set(); candidates = []
                for t in self.targets.values():
                    if t["id"] not in seen and t.get("ra_deg", 0) != 0 and t.get("size_maj", 0) > 0:
                        seen.add(t["id"]); candidates.append(t)

            # Apply type filter
            fallback_note = ""
            if type_filter != "All Types":
                filtered = [t for t in candidates if t.get("obj_type") == type_filter]
                # If seasonal mode + type filter yields nothing (e.g. Spring has no nebulae),
                # fall back to the full catalog for that type so results are never silently empty.
                if not filtered and seasonal_only:
                    seen = set()
                    filtered = []
                    for t in self.targets.values():
                        if t["id"] not in seen and t.get("obj_type") == type_filter:
                            seen.add(t["id"])
                            filtered.append(t)
                    fallback_note = f"ℹ️ No {type_filter} in seasonal list — searched full catalog.  "
                else:
                    fallback_note = ""
                candidates = filtered

            if not candidates:
                prog_label.config(text="No candidates matched your filters.", foreground="red")
                _search_running[0] = False
                return
            prog_label.config(text=f"Checking {len(candidates)} objects…", foreground="orange")

            def _compute():
                results = []
                # Pre-compute the darkness window once — avoids re-scanning
                # the sun's position for each of the ~13 000 candidate objects.
                dark_range = _dark_jd_range(lat, lon)
                for i, t in enumerate(candidates):
                    if i % 20 == 0:
                        popup.after(0, lambda v=i: prog_label.config(
                            text=f"Checking {v}/{len(candidates)}…", foreground="orange"))

                    # Magnitude filter (applied before the expensive altitude calc)
                    if mag_limit is not None:
                        mag_val = t.get("surf_br") if use_surf_br else t.get("v_mag")
                        if mag_val is None or mag_val > mag_limit:
                            continue

                    max_alt, rise_label = _alt_rise_tonight(
                        t["ra_deg"], t["dec_deg"], lat, lon, min_alt, dark_range)
                    if max_alt >= min_alt:
                        results.append((t, max_alt, rise_label))

                # Sort by altitude descending
                results.sort(key=lambda x: x[1], reverse=True)

                def _show():
                    tree.delete(*tree.get_children())
                    mag_col_label = "SB" if use_surf_br else "Mag"
                    tree.heading("Mag", text=mag_col_label)
                    for t, alt, rise_label in results:
                        common = t["common"].split(";")[0].strip() if t["common"] else ""
                        size = f"{t['size_maj']:.1f} × {t['size_min']:.1f}" if t["size_maj"] else "—"
                        mag_val = t.get("surf_br") if use_surf_br else t.get("v_mag")
                        mag_str = f"{mag_val:.1f}" if mag_val is not None else "—"
                        rise_str = rise_label if rise_label else "—"
                        tree.insert("", "end", values=(
                            t["id"], common, t.get("obj_type", ""),
                            f"{alt:.0f}°", rise_str, mag_str, size))
                    _apply_sort()
                    prog_label.config(
                        text=f"{fallback_note}Found {len(results)} visible target{'s' if len(results) != 1 else ''} tonight.",
                        foreground="green")
                    _search_running[0] = False
                    _last_searched_loc[0] = lat_var.get().strip()
                    _last_searched_loc[1] = lon_var.get().strip()
                    _last_searched_alt[0] = min_alt_var.get().strip()

                popup.after(0, _show)

            threading.Thread(target=_compute, daemon=True).start()

        # Auto-rerun when filters change (only if not already searching)
        def _auto_rerun(*args):
            if not _search_running[0]:
                _run_search()

        type_combo.bind("<<ComboboxSelected>>", _auto_rerun)
        season_var.trace_add("write", _auto_rerun)
        mag_limit_entry.bind("<Return>", lambda e: _auto_rerun())
        mag_limit_entry.bind("<FocusOut>", lambda e: _auto_rerun())

        # Now safe to schedule auto-run — _run_search is defined
        if _initial_autorun:
            popup.after(200, _run_search)
        else:
            popup.after(200, _auto_detect_then_run)

        def on_select(event):
            sel = tree.selection()
            if sel:
                name = tree.item(sel[0], "values")[0]
                self.target_search.delete(0, tk.END)
                self.target_search.insert(0, name)
                popup.destroy()
                self.analyze_framing()

        tree.bind("<Double-1>", on_select)
        ttk.Label(popup,
                  text="Click a column header to sort  ·  Double-click a target to load it  ·  “up” = already above the horizon at dark",
                  font=("Helvetica", 8)).pack(pady=(0, 6))

        # Apply current day/night theme to the popup window and its widgets
        self._theme_popup(popup)

    # ═══════════════════════════════════════════════════════════════════
    # INTEGRATION PLAN & NINA PROFILE IMPORT
    # ═══════════════════════════════════════════════════════════════════

    def _rf_for_filter_mode(self, mode):
        """Return the sky-background reduction factor for a given filter_mode string.

        Broadband:  None (Luminance) → 1.0,  LRGB → 3.0
        Narrowband: any string containing a number followed by 'nm' →  348 / nm
                    (derived from the physics of bandwidth vs sky flux suppression)
        Unknown:    falls back to 1.0 (no suppression)
        """
        if mode == "Mono Lum" or mode == "None (Luminance)":
            return 1.0
        if mode == "LRGB":
            return 3.0
        import re as _re
        m = _re.search(r'(\d+(?:\.\d+)?)\s*nm', mode, _re.IGNORECASE)
        if m:
            nm = float(m.group(1))
            return 348.0 / max(nm, 0.1)
        return 1.0

    def _get_filter_names(self):
        """Return the list of individual filter names for the current camera/filter selection.

        For a color camera or luminance-only mono, returns ['Lum'].
        For LRGB returns the imported NINA names if available, else ['L', 'R', 'G', 'B'].
        For narrowband returns the imported NINA narrowband names if available, else ['Ha', 'O3', 'S2'].
        """
        cam = self.data["cameras"].get(self.camera_choice.get(), {})
        if cam.get("is_color", True):
            return ["Lum"]                          # color — single-pass

        nf = self.data.get("nina_filters", {})
        mode = self.filter_mode.get()
        if mode == "LRGB":
            return nf.get("lrgb") or ["L", "R", "G", "B"]
        elif "NB" in mode or "Narrowband" in mode:
            return nf.get("narrowband") or ["Ha", "O3", "S2"]
        else:
            return ["Lum"]                          # "Mono Lum" — single luminance channel

    def show_integration_plan(self, silent=False):
        """Populate the Targets List stat cards with integration plan figures.

        Uses the sub-exposure and sky-flux values stored by the most recent
        analyze_framing() run, and the session duration entered in the Session Plan
        field.  All values are purely computational — no network calls needed.
        Pass silent=True (auto-calls from analyze_framing) to suppress warning dialogs.
        """
        if self._last_exp_s is None:
            if not silent:
                messagebox.showwarning("No Analysis",
                    "Please run Analyze Target first so the sub-exposure\n"
                    "recommendation is available.")
            return

        try:
            session_hrs = float(self.session_hours_var.get())
            if session_hrs <= 0:
                raise ValueError("Session hours must be positive.")
        except ValueError as e:
            if not silent:
                messagebox.showerror("Invalid Input", f"Session hours: {e}")
            return

        # Use manual override if provided, else fall back to calculated recommendation
        manual_str = self.manual_exp_var.get().strip()
        try:
            exp = float(manual_str)
            if exp <= 0:
                raise ValueError
        except ValueError:
            exp = self._last_exp_s

        # Update the hint label to always show the recommended value
        self._recommended_exp_label.config(text=f"(recommended: {self._last_exp_s:.1f}s)")

        # Read overhead per sub (default 5s if blank or invalid)
        try:
            overhead_s = float(self.overhead_per_sub_var.get())
            if overhead_s < 0:
                raise ValueError
        except (ValueError, AttributeError):
            overhead_s = 5.0

        time_per_sub = exp + overhead_s
        session_s    = session_hrs * 3600.0
        n_subs_all   = int(session_s / time_per_sub)

        if n_subs_all < 1:
            if not silent:
                messagebox.showwarning("Session Too Short",
                    f"With {exp:.1f}s subs, a {session_hrs:.1f}h session gives less\n"
                    f"than one complete sub-exposure.\n\n"
                    f"Try a longer session or a shorter sub-exposure.")
            return

        # ── Multi-filter allocation ───────────────────────────────────────
        filters      = self._get_filter_names()
        n_filters    = len(filters)
        multi_filter = n_filters > 1

        # Divide subs equally among filters; each filter gets a whole-number count
        subs_per_filter  = n_subs_all // n_filters
        total_subs_used  = subs_per_filter * n_filters       # may be < n_subs_all due to int division
        total_int_s      = total_subs_used * exp
        total_int_hrs    = total_int_s / 3600.0
        int_per_filter_h = (subs_per_filter * exp) / 3600.0

        snr_gain         = math.sqrt(subs_per_filter) if multi_filter else math.sqrt(total_subs_used)
        overhead_total_s = n_subs_all * overhead_s
        overhead_pct     = (overhead_total_s / session_s) * 100

        noise_note = ""
        if self._last_sky_flux is not None and self.current_target_info:
            c = self.data["cameras"].get(self.camera_choice.get(), {})
            rn  = float(c.get("read_noise", 3))
            sky_e_per_sub = self._last_sky_flux * exp
            if sky_e_per_sub > rn ** 2 * 5:
                noise_note = "ℹ️  Sky-noise limited — more subs always help."
            elif sky_e_per_sub > rn ** 2:
                noise_note = "ℹ️  Transitional regime — subs & darks both matter."
            else:
                noise_note = "ℹ️  Read-noise limited — consider longer subs if possible."

        # Build per-filter breakdown line
        if multi_filter:
            filter_breakdown = (
                f"{n_filters} filters ({', '.join(filters)})  ·  "
                f"{subs_per_filter} subs × {int_per_filter_h:.2f}h each  "
                f"=  {total_subs_used} total subs / {total_int_hrs:.2f}h total\n"
                + noise_note
            )
        else:
            filter_breakdown = noise_note

        # ── Update Targets List stat cards ────────────────────────────
        target_id = self.current_target_info["id"] if self.current_target_info else "unknown"
        common    = (self.current_target_info.get("common", "").split(";")[0].strip()
                     if self.current_target_info else "")
        subtitle  = (f"Target: {target_id}"
                     + (f"  —  {common}" if common else "")
                     + f"   ·   Sub-exposure: {exp:.1f}s")
        self._plan_target_var.set(subtitle)

        # Stat cards: show per-filter values when multiple filters are in use
        if multi_filter:
            self._plan_subs_var.set(f"{subs_per_filter}\nper filter")
            self._plan_total_var.set(f"{int_per_filter_h:.2f}h\nper filter")
        else:
            self._plan_subs_var.set(str(total_subs_used))
            self._plan_total_var.set(f"{total_int_hrs:.2f}h")

        self._plan_snr_var.set(f"{snr_gain:.1f}×")
        overhead_min = overhead_total_s / 60.0
        self._plan_overhead_var.set(f"{overhead_pct:.0f}%  ({overhead_min:.0f} min)")
        self._plan_noise_var.set(filter_breakdown)

    # Canonical narrowband filter names (upper-case for case-insensitive matching)
    _NARROWBAND_NAMES = {"HA", "H-A", "HALPHA", "H-ALPHA",
                         "O3", "OIII", "O-III",
                         "S2", "SII", "S-II",
                         "HB", "H-B", "HBETA", "H-BETA"}

    @staticmethod
    def _is_narrowband(name):
        """Return True if the filter name looks like a narrowband filter."""
        return name.strip().upper() in AstroApp._NARROWBAND_NAMES

    def _import_nina_profile(self):
        """Open a NINA .profile file, extract the filter wheel definitions and
        the configured telescope, then present a unified diff confirmation
        dialog before persisting either.

        The scope is read from ``<TelescopeSettings>``.  NINA does not store
        aperture, so we compute it from ``FocalLength / FocalRatio``.  If the
        scope name is blank or either numeric value is zero/missing we treat
        the profile as having no scope configured and skip it silently — only
        the filters are imported in that case.
        """

        # ── 1. File picker ────────────────────────────────────────────────────
        default_dir = ""
        if platform.system() == "Windows":
            local_app_data = os.getenv("LOCALAPPDATA", "")
            nina_dir = os.path.join(local_app_data, "NINA", "profiles")
            if os.path.isdir(nina_dir):
                default_dir = nina_dir

        path = filedialog.askopenfilename(
            title="Select NINA Profile",
            initialdir=default_dir or None,
            filetypes=[("NINA Profile", "*.profile"), ("All files", "*.*")]
        )
        if not path:
            return

        # ── 2. Read file ──────────────────────────────────────────────────────
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                content = fh.read()
        except OSError as exc:
            messagebox.showerror("Read Error", f"Could not read profile file.\n\n{exc}")
            return

        import re

        # ── 3. Parse FilterWheelSettings → filter names ──────────────────────
        fw_match = re.search(
            r'<FilterWheelSettings\b.*?</FilterWheelSettings>', content, re.DOTALL
        )
        search_text = fw_match.group() if fw_match else content
        filters = re.findall(r'<a:_name>(.*?)</a:_name>', search_text)

        # ── 4. Parse TelescopeSettings → scope_info | None ───────────────────
        # NINA always emits the <TelescopeSettings> block even when no scope
        # is configured.  We treat a blank Name or non-positive FL/FR as "no
        # scope" and skip it — only filters get imported in that case.
        scope_info = None
        ts_match = re.search(
            r'<TelescopeSettings\b.*?</TelescopeSettings>', content, re.DOTALL
        )
        if ts_match:
            ts = ts_match.group()
            name_m = re.search(r'<Name>([^<]*)</Name>', ts)
            fl_m   = re.search(r'<FocalLength>([^<]*)</FocalLength>', ts)
            fr_m   = re.search(r'<FocalRatio>([^<]*)</FocalRatio>', ts)
            nm = name_m.group(1).strip() if name_m else ""
            try:
                fl = float(fl_m.group(1)) if fl_m and fl_m.group(1).strip() else 0.0
            except (ValueError, TypeError):
                fl = 0.0
            try:
                fr = float(fr_m.group(1)) if fr_m and fr_m.group(1).strip() else 0.0
            except (ValueError, TypeError):
                fr = 0.0
            if nm and fl > 0 and fr > 0:
                scope_info = {
                    "name":          nm,
                    "focal_length":  fl,    # mm
                    "focal_ratio":   fr,
                    "aperture":      fl / fr,   # mm — derived
                }

        if not filters and scope_info is None:
            messagebox.showinfo(
                "Nothing to Import",
                "No filter definitions or telescope settings were found in "
                "the selected profile.\n\n"
                "Make sure the file is a valid NINA .profile."
            )
            return

        # ── 5. Bandwidth prompt for narrowband filters ───────────────────────
        nb_filters    = [f for f in filters if self._is_narrowband(f)]
        broad_filters = [f for f in filters if not self._is_narrowband(f)]
        chosen_bw     = None

        if nb_filters:
            bw_dlg = tk.Toplevel(self.root)
            bw_dlg.title("Narrowband Filter Bandwidth")
            bw_dlg.resizable(False, False)
            bw_dlg.grab_set()

            nb_list = ", ".join(nb_filters)
            ttk.Label(bw_dlg,
                      text="Narrowband filter(s) detected:",
                      font=("Helvetica", 11, "bold")).pack(padx=24, pady=(18, 2))
            ttk.Label(bw_dlg,
                      text=nb_list,
                      font=("Helvetica", 11), foreground="#7eb8d4").pack(padx=24, pady=(0, 10))
            ttk.Label(bw_dlg,
                      text="What is the filter bandwidth (nm)?",
                      font=("Helvetica", 11)).pack(padx=24, pady=(0, 6))

            bw_var = tk.StringVar(value="7")
            bw_combo = ttk.Combobox(bw_dlg, textvariable=bw_var,
                                    values=["3", "5", "7", "10", "12", "15"],
                                    width=8, state="normal")
            bw_combo.pack(padx=24, pady=(0, 4))

            hint = ttk.Label(bw_dlg,
                             text="Common values: 3 nm (ultra-narrow), 7 nm (standard), 12 nm (wide)",
                             font=("Helvetica", 9), foreground="#aaaaaa")
            hint.pack(padx=24, pady=(0, 12))

            _bw_result = [None]

            def _bw_ok():
                raw = bw_var.get().strip()
                try:
                    val = float(raw)
                    if val <= 0:
                        raise ValueError
                except ValueError:
                    messagebox.showerror("Invalid", "Please enter a positive number.", parent=bw_dlg)
                    return
                _bw_result[0] = raw
                bw_dlg.destroy()

            def _bw_cancel():
                bw_dlg.destroy()

            bw_btn_row = ttk.Frame(bw_dlg)
            bw_btn_row.pack(pady=(0, 18))
            ttk.Button(bw_btn_row, text="OK",     command=_bw_ok).pack(side="left", padx=6)
            ttk.Button(bw_btn_row, text="Cancel", command=_bw_cancel).pack(side="left", padx=6)
            bw_dlg.bind("<Return>", lambda e: _bw_ok())
            bw_dlg.bind("<Escape>", lambda e: _bw_cancel())
            self._theme_popup(bw_dlg)

            bw_dlg.wait_window()

            if _bw_result[0] is None:
                return  # user cancelled bandwidth prompt → abort entire import

            chosen_bw = _bw_result[0]

        # ── 6. Build display labels for narrowband filters ───────────────────
        def _fmt_bw(val):
            try:
                return str(int(float(val))) if float(val) == int(float(val)) else val
            except Exception:
                return val

        bw_display = _fmt_bw(chosen_bw) if chosen_bw else None

        # ── 7. Compute scope diff state (NEW / UPDATE / MATCH) ───────────────
        # Used by the dialog row + the success summary at the end.  MATCH means
        # the profile's scope is byte-for-byte identical to what we already
        # have in inventory — we still display it (so the user sees the import
        # was processed) but in muted grey, and the badge says EXISTS.
        scope_state = None
        scope_prev  = None
        if scope_info is not None:
            existing = self.data.get("scopes", {}).get(scope_info["name"])
            if existing is None:
                scope_state = "NEW"
            else:
                ex_ap = existing.get("aperture")
                ex_fl = existing.get("native_fl")
                ex_fr = existing.get("native_f_ratio")
                identical = (
                    ex_ap is not None and abs(ex_ap - scope_info["aperture"])    < 0.01 and
                    ex_fl is not None and abs(ex_fl - scope_info["focal_length"]) < 0.01 and
                    ex_fr is not None and abs(ex_fr - scope_info["focal_ratio"])  < 0.01
                )
                scope_state = "MATCH" if identical else "UPDATE"
                if scope_state == "UPDATE":
                    scope_prev = {"aperture": ex_ap, "native_fl": ex_fl,
                                  "native_f_ratio": ex_fr}

        # ── 7b. Compute per-filter diff state (NEW / UPDATE / MATCH) ─────────
        # Compares each incoming filter against the existing nina_filters
        # block.  MATCH = same name (and, for narrowband, same bandwidth) is
        # already in inventory.  UPDATE = narrowband filter name is already
        # in inventory but the bandwidth differs — we show the old bandwidth
        # inline on the row.  NEW = filter wasn't in inventory before.
        existing_nf       = self.data.get("nina_filters", {}) or {}
        existing_lrgb_set = set(existing_nf.get("lrgb")       or [])
        existing_nb_set   = set(existing_nf.get("narrowband") or [])
        existing_bw       = existing_nf.get("bandwidth_nm")

        def _bw_match(a, b):
            """Compare two bandwidths tolerantly (handles None and float dust)."""
            if a is None or b is None:
                return False
            try:
                return abs(float(a) - float(b)) < 0.01
            except (ValueError, TypeError):
                return False

        new_bw_val = float(chosen_bw) if chosen_bw is not None else None

        def _filter_state(name, is_nb):
            if is_nb:
                if name in existing_nb_set:
                    return "MATCH" if _bw_match(new_bw_val, existing_bw) else "UPDATE"
                return "NEW"
            return "MATCH" if name in existing_lrgb_set else "NEW"

        def _fmt_bw_simple(val):
            """Format a bandwidth float as '5' or '7.5' (no trailing .0)."""
            try:
                v = float(val)
                return str(int(v)) if v == int(v) else f"{v:g}"
            except (ValueError, TypeError):
                return str(val)

        # ── 8. Build the unified diff dialog ─────────────────────────────────
        # Palette (matches the rest of the app in day mode; falls back to the
        # night-mode red theme via the manual-color path below).
        if self.night_mode:
            DLG_BG     = "#1a0000"
            LIST_BG    = "#0d0000"
            HEAD_FG    = "#ff7878"
            BODY_FG    = "#ffb0b0"
            MUTED_FG   = "#a06060"
            SEP_COL    = "#3a0000"
            NB_FG      = "#ffa060"
            SCOPE_FG   = "#ff8080"
        else:
            DLG_BG     = "#1e2d3e"
            LIST_BG    = "#131f2e"
            HEAD_FG    = "#ffffff"
            BODY_FG    = "#e2e8f0"
            MUTED_FG   = "#94a3b8"
            SEP_COL    = "#2e4a63"
            NB_FG      = "#f0c060"
            SCOPE_FG   = "#7eb8d4"

        # State-badge colors — same in both modes; readable on the row bg
        BADGE = {
            "NEW":    ("#4ade80", "#062e1a"),
            "UPDATE": ("#fbbf24", "#3a2a08"),
            "MATCH":  ("#94a3b8", "#202833"),
        }

        dlg = tk.Toplevel(self.root)
        dlg.title("NINA Profile Import")
        dlg.configure(bg=DLG_BG)
        dlg.resizable(False, False)
        dlg.grab_set()

        body = tk.Frame(dlg, bg=DLG_BG)
        body.pack(padx=18, pady=14, fill="both", expand=True)

        # Header row — title + item count
        item_total = (1 if scope_info else 0) + len(filters)
        head = tk.Frame(body, bg=DLG_BG)
        head.pack(fill="x", pady=(0, 8))
        tk.Label(head, text="NINA Profile Import", bg=DLG_BG, fg=HEAD_FG,
                 font=("Helvetica", 13, "bold")).pack(side="left")
        tk.Label(head, text=f"{item_total} item{'s' if item_total != 1 else ''}",
                 bg=DLG_BG, fg=MUTED_FG,
                 font=("Helvetica", 9)).pack(side="right")

        # List container (no ttk — we need exact bg control for the rows)
        list_outer = tk.Frame(body, bg=LIST_BG, padx=6, pady=4)
        list_outer.pack(fill="both", expand=True)

        def _add_row(icon, icon_color, name_text, detail_text, badge,
                     name_color=None, detail_color=None, is_last=False):
            """Build a single row inside list_outer."""
            row = tk.Frame(list_outer, bg=LIST_BG)
            row.pack(fill="x", pady=(2, 2))

            # icon column (fixed width)
            tk.Label(row, text=icon, bg=LIST_BG, fg=icon_color,
                     width=2, font=("Helvetica", 11, "bold")).pack(side="left", padx=(2, 6))

            # badge column (fixed) — packed BEFORE the expanding text column
            # so it gets its full width before the text column claims the rest
            if badge:
                fg, bg = BADGE.get(badge, BADGE["NEW"])
                tk.Label(row, text=f" {badge} ", bg=bg, fg=fg,
                         font=("Helvetica", 8, "bold"),
                         padx=4, pady=1).pack(side="right", padx=(6, 4))

            # name + detail column (expanding)
            text_col = tk.Frame(row, bg=LIST_BG)
            text_col.pack(side="left", fill="x", expand=True)
            tk.Label(text_col, text=name_text, bg=LIST_BG,
                     fg=name_color or BODY_FG,
                     font=("Helvetica", 11), anchor="w").pack(anchor="w", fill="x")
            if detail_text:
                tk.Label(text_col, text=detail_text, bg=LIST_BG,
                         fg=detail_color or MUTED_FG,
                         font=("Helvetica", 9), anchor="w").pack(anchor="w", fill="x")

            # row separator (skip after the last row)
            if not is_last:
                tk.Frame(list_outer, bg=SEP_COL, height=1).pack(fill="x")

        # Build row list — scope first, then filters
        rows_total = (1 if scope_info else 0) + len(filters)
        rows_drawn = 0

        if scope_info is not None:
            rows_drawn += 1
            ap = scope_info["aperture"]
            fl = scope_info["focal_length"]
            fr = scope_info["focal_ratio"]
            detail = f"{ap:.0f} mm aperture · {fl:.0f} mm FL · f/{fr:.1f}"
            if scope_state == "UPDATE" and scope_prev:
                detail += (f"   (was {scope_prev['aperture']:.0f} mm · "
                           f"{scope_prev['native_fl']:.0f} mm · "
                           f"f/{scope_prev['native_f_ratio']:.1f})")
            _add_row(
                icon="🔭", icon_color=SCOPE_FG,
                name_text=scope_info["name"],
                detail_text=detail,
                badge=scope_state,
                name_color=HEAD_FG,
                is_last=(rows_drawn == rows_total),
            )

        for f in filters:
            rows_drawn += 1
            is_nb = self._is_narrowband(f)
            if is_nb and bw_display:
                name_text = f"{f}  ({bw_display} nm)"
            else:
                name_text = f
            f_state = _filter_state(f, is_nb)
            # On UPDATE (narrowband bandwidth change) note the previous value
            f_detail = ""
            if f_state == "UPDATE" and existing_bw is not None:
                f_detail = f"previously {_fmt_bw_simple(existing_bw)} nm"
            _add_row(
                icon=("★" if is_nb else "✦"),
                icon_color=(NB_FG if is_nb else MUTED_FG),
                name_text=name_text, detail_text=f_detail,
                badge=f_state,
                name_color=(NB_FG if is_nb else BODY_FG),
                is_last=(rows_drawn == rows_total),
            )

        # Profile filename footer
        tk.Label(body, text="Profile: " + os.path.basename(path),
                 bg=DLG_BG, fg=MUTED_FG,
                 font=("Helvetica", 9)).pack(anchor="w", pady=(8, 0))

        # Optional informational line if a scope was found but is identical
        # to what's already in inventory (so the user understands the no-op).
        if scope_state == "MATCH":
            tk.Label(body,
                     text="Scope already in inventory — no change needed.",
                     bg=DLG_BG, fg=MUTED_FG,
                     font=("Helvetica", 9, "italic")).pack(anchor="w", pady=(2, 0))

        # ── classify the raw filter names into lrgb / narrowband buckets ─────
        lrgb_names = [f for f in filters if not self._is_narrowband(f)]
        nb_names   = [f for f in filters if self._is_narrowband(f)]

        def _do_import():
            # Build the filter_mode key for the narrowband bandwidth
            nb_mode = None
            if nb_names and chosen_bw is not None:
                try:
                    bw_val = float(chosen_bw)
                    bw_int = int(bw_val) if bw_val == int(bw_val) else bw_val
                    nb_mode = f"NB ({bw_int}nm)"
                except (TypeError, ValueError):
                    pass

            # 1) Persist the scope first (if any) so the inventory refresh below
            #    picks it up.  We do NOT touch the active scope selection —
            #    the user keeps whatever was selected before the import.
            scope_written = False
            if scope_info is not None and scope_state in ("NEW", "UPDATE"):
                self.data.setdefault("scopes", {})[scope_info["name"]] = {
                    "aperture":       scope_info["aperture"],
                    "native_fl":      scope_info["focal_length"],
                    "native_f_ratio": scope_info["focal_ratio"],
                }
                scope_written = True

            # 2) Persist filters.  Always overwrites the previous nina_filters
            #    block — that's the historical behavior.  We also stash a
            #    scope summary inside the same block so the Settings status
            #    label can report it without a separate JSON section.
            scope_breadcrumb = None
            if scope_info is not None:
                scope_breadcrumb = {
                    "name":         scope_info["name"],
                    "aperture":     scope_info["aperture"],
                    "focal_length": scope_info["focal_length"],
                    "focal_ratio":  scope_info["focal_ratio"],
                }

            if filters:
                self.data["nina_filters"] = {
                    "lrgb":          lrgb_names  or None,
                    "narrowband":    nb_names    or None,
                    "bandwidth_nm":  float(chosen_bw) if chosen_bw is not None else None,
                    "imported_date": datetime.now().strftime("%Y-%m-%d %H:%M"),
                    "scope":         scope_breadcrumb,
                }
            elif scope_breadcrumb is not None:
                # No filters in profile but we still want the scope reported
                # in the Settings status — merge into whatever's there now.
                nf = self.data.setdefault("nina_filters", {})
                nf["scope"]         = scope_breadcrumb
                nf["imported_date"] = datetime.now().strftime("%Y-%m-%d %H:%M")

            self._persist_data()

            # 3) Refresh UI — inventory tables pick up the new scope, dropdowns
            #    pick up the new filter bandwidth entry.
            self.refresh_inventory_tables()
            self.refresh_dropdowns()

            # If a narrowband bandwidth was specified, select it in the dropdown
            if nb_mode and nb_mode in self.filter_dropdown["values"]:
                self.filter_mode.set(nb_mode)

            # Update the settings panel status label
            self._update_nina_profile_status()

            dlg.destroy()

            # Build the success summary
            parts = []
            if scope_written:
                verb = "Updated" if scope_state == "UPDATE" else "Added"
                parts.append(
                    f"{verb} scope: {scope_info['name']} "
                    f"({scope_info['aperture']:.0f} mm · "
                    f"{scope_info['focal_length']:.0f} mm FL · "
                    f"f/{scope_info['focal_ratio']:.1f})"
                )
            elif scope_info is not None and scope_state == "MATCH":
                parts.append(f"Scope unchanged: {scope_info['name']}")
            if lrgb_names:
                parts.append(f"LRGB filters: {', '.join(lrgb_names)}")
            if nb_names:
                bw_note = f" @ {bw_display} nm" if bw_display else ""
                parts.append(f"Narrowband filters: {', '.join(nb_names)}{bw_note}")

            messagebox.showinfo(
                "Profile Imported",
                "Import complete.\n\n" + "\n\n".join(parts)
            )

        # Action buttons — right-aligned
        btn_row = tk.Frame(body, bg=DLG_BG)
        btn_row.pack(fill="x", pady=(10, 0))
        ttk.Button(btn_row, text="Cancel", command=dlg.destroy).pack(side="right", padx=(6, 0))
        ttk.Button(btn_row, text="Import all", command=_do_import).pack(side="right")
        dlg.bind("<Return>", lambda e: _do_import())
        dlg.bind("<Escape>", lambda e: dlg.destroy())

    # ═══════════════════════════════════════════════════════════════════
    # ANALYSIS PLUMBING (deferred/dirty-flag dispatch)
    # ═══════════════════════════════════════════════════════════════════
    # Equipment changes mark the analysis "dirty" rather than re-running
    # immediately; a timer-based after() then fires _run_deferred_analysis
    # if the Planner or Session tab is visible.  This avoids re-entrancy
    # through macOS Cocoa Tk's update_idletasks() cascade.

    def toggle_filter_visibility(self, *args):
        """Show or hide the filter-mode chip based on whether the selected camera is mono."""
        c = self.data["cameras"].get(self.camera_choice.get())
        if c and not c.get("is_color", True):
            # Show filter dropdown in the chips bar (pack before NINA button)
            if not self.filter_dropdown.winfo_ismapped():
                self.filter_dropdown.pack(side="left", padx=(0, 6),
                                          before=self._equip_summary_label)
        else:
            self.filter_dropdown.pack_forget()

    def _mark_analysis_dirty(self):
        """Flag that input data has changed and analysis needs to re-run.

        If the Target Planner or Session tab is currently visible, schedules
        analyze_framing via after() so the tab can finish rendering first.
        If a different tab is active, just sets the flag — _on_tab_changed
        will pick it up when the user returns to the Planner tab.
        """
        self._analysis_dirty = True
        try:
            current = self.tab_control.select()
            if current in (str(self.tab_planner), str(self.tab_session)):
                self._schedule_analysis()
        except Exception:
            pass

    def _schedule_analysis(self):
        """Schedule analyze_framing via after() if not already scheduled.

        Uses after(100) — NOT after_idle — so the callback cannot be flushed
        prematurely by update_idletasks() inside _draw_queue_gantt or
        _draw_altitude_chart (which is what causes the blank-planner bug on macOS).
        """
        if self._analysis_after_id is not None:
            return  # already scheduled — don't stack
        self._analysis_after_id = self.root.after(100, self._run_deferred_analysis)

    def _run_deferred_analysis(self):
        """Execute the deferred analysis and clear the scheduling state."""
        self._analysis_after_id = None
        if not self._analysis_dirty:
            return
        # Only run if the Planner or Session tab is still active — the user
        # may have switched away during the after(100) delay.  If so, leave
        # the dirty flag set; _on_tab_changed will pick it up later.
        try:
            current = self.tab_control.select()
            if current not in (str(self.tab_planner), str(self.tab_session)):
                return  # dirty flag stays set
        except Exception:
            pass
        self.analyze_framing()

    # ═══════════════════════════════════════════════════════════════════
    # RIG PRESETS — named snapshots of the equipment chip combination
    # ═══════════════════════════════════════════════════════════════════

    def _camera_is_color(self, name):
        """Return True if the given camera name is a color sensor (default True if unknown).

        A camera missing from inventory is treated as color — this matches the
        ``cam.get('is_color', True)`` default used elsewhere and safely suppresses
        the filter field for rigs whose camera has been deleted.
        """
        cam = self.data.get("cameras", {}).get(name, {})
        return cam.get("is_color", True)

    def _current_rig_snapshot(self):
        """Capture current equipment-chip values as a rig-state dict (no name).

        The filter field is only populated when the current camera is mono,
        since the filter dropdown is hidden for color cameras and the retained
        StringVar value would otherwise be a stale leftover.
        """
        cam_name = self.camera_choice.get()
        is_mono  = not self._camera_is_color(cam_name)
        return {
            "scope":     self.scope_choice.get(),
            "reduction": self.reduction_factor.get(),
            "camera":    cam_name,
            "bortle":    self.bortle_choice.get(),
            "filter":    self.filter_mode.get() if is_mono else "",
        }

    def _chips_match_rig(self, rig):
        """Return True if current chip values match every key in the rig snapshot."""
        snap = self._current_rig_snapshot()
        for key in ("scope", "reduction", "camera", "bortle", "filter"):
            if snap.get(key) != rig.get(key):
                return False
        return True

    def _find_rig(self, name):
        """Return the rig dict with the given name, or None if not found."""
        for rig in self.data.get("settings", {}).get("rigs", []):
            if rig.get("name") == name:
                return rig
        return None

    def _refresh_rig_dropdown(self):
        """Repopulate the rig dropdown values from saved rigs and refresh the indicator."""
        rigs = self.data.get("settings", {}).get("rigs", [])
        names = [r.get("name", "") for r in rigs if r.get("name")]
        self.rig_dropdown["values"] = names
        self._update_rig_indicator()

    def _update_rig_indicator(self):
        """Set the rig dropdown display text based on whether chips match the active rig.

        Shows the rig's name when chips are a clean match, 'Custom…' when chips
        diverge, and empty string when no rig is active or it no longer exists.
        No-op while ``_applying_rig`` is True so rig-selection doesn't fight itself.
        """
        if getattr(self, "_applying_rig", False):
            return
        if not hasattr(self, "rig_choice"):
            return   # Called before the planner tab was built
        active_name = self.data.get("settings", {}).get("active_rig", "")
        if not active_name:
            self.rig_choice.set("")
            return
        rig = self._find_rig(active_name)
        if rig is None:
            # Active rig was deleted or gear file is stale — clear the reference
            self.rig_choice.set("")
            return
        if self._chips_match_rig(rig):
            self.rig_choice.set(active_name)
        else:
            self.rig_choice.set("Custom…")

    def _on_rig_selected(self, event=None):
        """Dropdown selection changed — apply the picked rig's snapshot to the chips."""
        name = self.rig_choice.get()
        if not name or name == "Custom…":
            return
        rig = self._find_rig(name)
        if rig is None:
            return
        self._apply_rig(rig)

    def _apply_rig(self, rig):
        """Apply a rig snapshot to the equipment chips and mark it as active.

        Only applies values still valid against the current inventory (so a
        deleted scope/camera in the rig doesn't corrupt the chip state).
        Triggers an analyze refresh via :meth:`_mark_analysis_dirty` since
        chip values have changed.
        """
        self._applying_rig = True
        try:
            if rig.get("scope") in self.data.get("scopes", {}):
                self.scope_choice.set(rig["scope"])
            if rig.get("camera") in self.data.get("cameras", {}):
                self.camera_choice.set(rig["camera"])
            if rig.get("bortle") in BORTLE_FACTORS:
                self.bortle_choice.set(rig["bortle"])
            if rig.get("reduction"):
                self.reduction_factor.set(rig["reduction"])
            if rig.get("filter") in self.filter_dropdown["values"]:
                self.filter_mode.set(rig["filter"])
        finally:
            self._applying_rig = False

        self.data.setdefault("settings", {})["active_rig"] = rig.get("name", "")
        self._persist_data()
        self._update_rig_indicator()
        if getattr(self, "auto_update_enabled", False):
            self._mark_analysis_dirty()

    def _open_save_rig_dialog(self):
        """Open the modal 'Save as rig' dialog to name and save the current chip combination."""
        dlg = tk.Toplevel(self.root)
        dlg.title("Save Rig")
        dlg.resizable(False, False)
        dlg.grab_set()
        dlg.transient(self.root)

        ttk.Label(dlg, text="Name this rig:",
                  font=("Helvetica", 11, "bold")).pack(padx=20, pady=(14, 4), anchor="w")

        # Default the entry to the active rig name if we're overwriting a
        # clean match; otherwise blank.
        current = self.rig_choice.get()
        default_name = current if current and current != "Custom…" else ""
        name_var = tk.StringVar(value=default_name)
        name_entry = ttk.Entry(dlg, textvariable=name_var, width=34, font=("Helvetica", 11))
        name_entry.pack(padx=20, pady=(0, 12), fill="x")
        name_entry.focus_set()
        name_entry.select_range(0, tk.END)

        # Snapshot preview — shows exactly what will be saved
        ttk.Label(dlg, text="Will snapshot:",
                  font=("Helvetica", 9, "bold"),
                  foreground="#778899").pack(padx=20, pady=(2, 4), anchor="w")

        snap = self._current_rig_snapshot()
        preview = tk.Frame(dlg, bg="#0e1a28")
        preview.pack(padx=20, pady=(0, 10), fill="x")
        # Filter is blank when camera is color — display "—" to make that clear
        filter_display = snap["filter"] if snap["filter"] else "— (color sensor)"
        rows = [
            ("Scope",   snap["scope"]     or "(none)"),
            ("Reducer", snap["reduction"] or "1.0×"),
            ("Camera",  snap["camera"]    or "(none)"),
            ("Bortle",  snap["bortle"]    or "(none)"),
            ("Filter",  filter_display),
        ]
        for i, (k, v) in enumerate(rows):
            tk.Label(preview, text=f"{k}:", bg="#0e1a28", fg="#8a94a3",
                     font=("Helvetica", 10), anchor="w", width=9
                     ).grid(row=i, column=0, sticky="w", padx=(8, 4), pady=1)
            tk.Label(preview, text=v, bg="#0e1a28", fg="#cfd4dc",
                     font=("Helvetica", 10), anchor="w"
                     ).grid(row=i, column=1, sticky="w", padx=(0, 8), pady=1)

        btn_row = ttk.Frame(dlg)
        btn_row.pack(padx=20, pady=(6, 14), fill="x")

        def _do_save():
            name = name_var.get().strip()
            if not name:
                messagebox.showerror("Invalid name",
                                     "Please enter a name for this rig.", parent=dlg)
                return
            if name == "Custom…":
                messagebox.showerror("Reserved name",
                                     "\"Custom…\" is a reserved label — please choose another name.",
                                     parent=dlg)
                return
            settings = self.data.setdefault("settings", {})
            rigs = settings.setdefault("rigs", [])
            existing = next((r for r in rigs if r.get("name") == name), None)
            if existing is not None:
                if not messagebox.askyesno("Overwrite rig?",
                                            f"A rig named '{name}' already exists.\n\n"
                                            "Overwrite its saved snapshot with the current equipment settings?",
                                            parent=dlg):
                    return
                existing.update(self._current_rig_snapshot())
                toast_msg = f"Updated rig: {name}"
            else:
                rigs.append({"name": name, **self._current_rig_snapshot()})
                toast_msg = f"Saved rig: {name}"
            settings["active_rig"] = name
            self._persist_data()
            self._refresh_rig_dropdown()
            self._show_toast(toast_msg)
            dlg.destroy()

        ttk.Button(btn_row, text="Cancel", command=dlg.destroy).pack(side="right", padx=(6, 0))
        ttk.Button(btn_row, text="Save",   command=_do_save   ).pack(side="right")
        dlg.bind("<Return>", lambda e: _do_save())
        dlg.bind("<Escape>", lambda e: dlg.destroy())

        self._theme_popup(dlg)

    def _open_manage_rigs_dialog(self):
        """Open the modal 'Manage Rigs' dialog for rename/update/delete operations."""
        dlg = tk.Toplevel(self.root)
        dlg.title("Manage Rigs")
        dlg.resizable(False, False)
        dlg.grab_set()
        dlg.transient(self.root)

        main = ttk.Frame(dlg)
        main.pack(padx=16, pady=14, fill="both", expand=True)

        # ── Left column: list of saved rigs ─────────────────────────────
        left = ttk.Frame(main)
        left.pack(side="left", fill="y", padx=(0, 14))

        ttk.Label(left, text="Saved rigs:",
                  font=("Helvetica", 9, "bold"),
                  foreground="#778899").pack(anchor="w", pady=(0, 4))

        lb = tk.Listbox(left, height=8, width=22,
                        font=("Helvetica", 11),
                        bg="#1e2d3e", fg="#ffffff",
                        selectbackground="#1e3a5f", selectforeground="#aaddff",
                        relief="flat", borderwidth=1,
                        exportselection=False, activestyle="none")
        lb.pack(fill="y")

        name_list = []   # parallel index → rig name (for reliable lookup)

        # ── Right column: details of selected rig ───────────────────────
        right = ttk.Frame(main)
        right.pack(side="left", fill="both", expand=True)

        ttk.Label(right, text="Details:",
                  font=("Helvetica", 9, "bold"),
                  foreground="#778899").pack(anchor="w", pady=(0, 4))

        details = tk.Frame(right, bg="#0e1a28", width=220, height=150)
        details.pack(fill="both", expand=True)
        details.pack_propagate(False)

        detail_labels = {}
        for i, k in enumerate(["Scope", "Reducer", "Camera", "Bortle", "Filter"]):
            tk.Label(details, text=f"{k}:", bg="#0e1a28", fg="#8a94a3",
                     font=("Helvetica", 10), anchor="w", width=8
                     ).grid(row=i, column=0, sticky="w", padx=(10, 4), pady=3)
            lbl = tk.Label(details, text="—", bg="#0e1a28", fg="#cfd4dc",
                           font=("Helvetica", 10), anchor="w")
            lbl.grid(row=i, column=1, sticky="w", padx=(0, 10), pady=3)
            detail_labels[k.lower()] = lbl

        # ── Button row ──────────────────────────────────────────────────
        btn_row = ttk.Frame(dlg)
        btn_row.pack(padx=16, pady=(0, 14), fill="x")

        def _selected_name():
            sel = lb.curselection()
            if not sel or sel[0] >= len(name_list):
                return None
            return name_list[sel[0]]

        def _refresh_list(preserve_name=None):
            rigs = self.data.get("settings", {}).get("rigs", [])
            active = self.data.get("settings", {}).get("active_rig", "")
            lb.delete(0, tk.END)
            name_list.clear()
            for r in rigs:
                name = r.get("name", "")
                if not name:
                    continue
                prefix = "★ " if name == active else "   "
                lb.insert(tk.END, f"{prefix}{name}")
                name_list.append(name)
            # Restore selection to the requested rig if still present
            if preserve_name and preserve_name in name_list:
                idx = name_list.index(preserve_name)
                lb.selection_set(idx)
                lb.see(idx)
            elif name_list:
                lb.selection_set(0)
            _on_select()

        def _on_select(event=None):
            sel = lb.curselection()
            if not sel or sel[0] >= len(name_list):
                for lbl in detail_labels.values():
                    lbl.configure(text="—")
                return
            rig = self._find_rig(name_list[sel[0]])
            if rig is None:
                return
            detail_labels["scope"].configure(text=rig.get("scope")     or "(none)")
            detail_labels["reducer"].configure(text=rig.get("reduction") or "1.0×")
            detail_labels["camera"].configure(text=rig.get("camera")    or "(none)")
            detail_labels["bortle"].configure(text=rig.get("bortle")    or "(none)")
            # Filter only applies to mono cameras; show em-dash for color rigs
            rig_cam = rig.get("camera", "")
            if rig_cam and self._camera_is_color(rig_cam):
                detail_labels["filter"].configure(text="— (color sensor)")
            else:
                detail_labels["filter"].configure(text=rig.get("filter") or "(none)")

        lb.bind("<<ListboxSelect>>", _on_select)

        def _do_rename():
            name = _selected_name()
            if not name:
                return
            rig = self._find_rig(name)
            if not rig:
                return
            new_name = simpledialog.askstring("Rename Rig",
                                               f"Rename '{name}' to:",
                                               parent=dlg, initialvalue=name)
            if new_name is None:
                return
            new_name = new_name.strip()
            if not new_name or new_name == name:
                return
            if new_name == "Custom…":
                messagebox.showerror("Reserved name",
                                     "\"Custom…\" is a reserved label.", parent=dlg)
                return
            if self._find_rig(new_name):
                messagebox.showerror("Name exists",
                                     f"A rig named '{new_name}' already exists.",
                                     parent=dlg)
                return
            rig["name"] = new_name
            settings = self.data.setdefault("settings", {})
            if settings.get("active_rig") == name:
                settings["active_rig"] = new_name
            self._persist_data()
            _refresh_list(preserve_name=new_name)
            self._refresh_rig_dropdown()

        def _do_update():
            name = _selected_name()
            if not name:
                return
            rig = self._find_rig(name)
            if not rig:
                return
            if not messagebox.askyesno("Update Rig",
                                        f"Overwrite '{name}' with the current equipment settings?\n\n"
                                        "This replaces the saved snapshot with whatever's selected in "
                                        "the planner right now.",
                                        parent=dlg):
                return
            rig.update(self._current_rig_snapshot())
            # Rig now matches current chips → make it active
            self.data.setdefault("settings", {})["active_rig"] = name
            self._persist_data()
            _refresh_list(preserve_name=name)
            self._refresh_rig_dropdown()
            self._show_toast(f"Updated rig: {name}")

        def _do_delete():
            name = _selected_name()
            if not name:
                return
            if not messagebox.askyesno("Delete Rig",
                                        f"Delete the rig '{name}'?\n\nThis cannot be undone.",
                                        parent=dlg):
                return
            settings = self.data.setdefault("settings", {})
            rigs = settings.setdefault("rigs", [])
            rigs[:] = [r for r in rigs if r.get("name") != name]
            if settings.get("active_rig") == name:
                settings["active_rig"] = ""
            self._persist_data()
            _refresh_list()
            self._refresh_rig_dropdown()
            self._show_toast(f"Deleted rig: {name}")

        ttk.Button(btn_row, text="Rename", command=_do_rename).pack(side="left")
        ttk.Button(btn_row, text="Update", command=_do_update).pack(side="left", padx=(6, 0))
        ttk.Button(btn_row, text="Delete", command=_do_delete).pack(side="left", padx=(6, 0))
        ttk.Button(btn_row, text="Close",  command=dlg.destroy).pack(side="right")
        dlg.bind("<Escape>", lambda e: dlg.destroy())

        # Initial populate — select the active rig if any, else the first
        active = self.data.get("settings", {}).get("active_rig", "")
        _refresh_list(preserve_name=active if active else None)

        self._theme_popup(dlg)

    def _open_save_location_dialog(self):
        """Modal 'Save Location' dialog — name the current coordinates as a profile."""
        try:
            lat = float(self._settings_lat_var.get())
            lon = float(self._settings_lon_var.get())
        except (ValueError, AttributeError):
            messagebox.showerror(
                "No coordinates",
                "Enter a latitude and longitude (or use Auto-detect) before saving a location.")
            return

        dlg = tk.Toplevel(self.root)
        dlg.title("Save Location")
        dlg.resizable(False, False)
        dlg.grab_set()
        dlg.transient(self.root)

        ttk.Label(dlg, text="Name this location:",
                  font=("Helvetica", 11, "bold")).pack(padx=20, pady=(14, 4), anchor="w")

        name_var = tk.StringVar(value=self.data.get("active_location", ""))
        name_entry = ttk.Entry(dlg, textvariable=name_var, width=34, font=("Helvetica", 11))
        name_entry.pack(padx=20, pady=(0, 12), fill="x")
        name_entry.focus_set()
        name_entry.select_range(0, tk.END)

        ttk.Label(dlg, text="Will save:", font=("Helvetica", 9, "bold"),
                  foreground="#778899").pack(padx=20, pady=(2, 4), anchor="w")
        preview = tk.Frame(dlg, bg="#0e1a28")
        preview.pack(padx=20, pady=(0, 10), fill="x")
        for i, (k, vv) in enumerate([("Latitude", f"{lat:.4f}"), ("Longitude", f"{lon:.4f}")]):
            tk.Label(preview, text=f"{k}:", bg="#0e1a28", fg="#8a94a3",
                     font=("Helvetica", 10), anchor="w", width=9
                     ).grid(row=i, column=0, sticky="w", padx=(8, 4), pady=1)
            tk.Label(preview, text=vv, bg="#0e1a28", fg="#cfd4dc",
                     font=("Helvetica", 10), anchor="w"
                     ).grid(row=i, column=1, sticky="w", padx=(0, 8), pady=1)

        btn_row = ttk.Frame(dlg)
        btn_row.pack(padx=20, pady=(6, 14), fill="x")

        def _do_save():
            name = name_var.get().strip()
            if not name:
                messagebox.showerror("Invalid name",
                                     "Please enter a name for this location.", parent=dlg)
                return
            locations = self.data.setdefault("locations", [])
            existing = next((p for p in locations if p.get("name") == name), None)
            if existing is not None:
                if not messagebox.askyesno(
                        "Overwrite location?",
                        f"A location named '{name}' already exists.\n\n"
                        "Overwrite its coordinates with the current values?",
                        parent=dlg):
                    return
                existing["lat"], existing["lon"] = lat, lon
                toast_msg = f"Updated location: {name}"
            else:
                locations.append({"name": name, "lat": lat, "lon": lon})
                toast_msg = f"Saved location: {name}"
            self.data["active_location"] = name
            self.data["location"] = {"lat": lat, "lon": lon}
            self._persist_data()
            self.refresh_twilight_header()
            self.refresh_moon_header()
            self._refresh_location_dropdowns()
            if hasattr(self, "_settings_loc_status"):
                self._settings_loc_status.config(
                    text=f"✅ Active location: {name}", foreground="#4caf50")
            self._show_toast(toast_msg)
            dlg.destroy()

        ttk.Button(btn_row, text="Cancel", command=dlg.destroy).pack(side="right", padx=(6, 0))
        ttk.Button(btn_row, text="Save",   command=_do_save   ).pack(side="right")
        dlg.bind("<Return>", lambda e: _do_save())
        dlg.bind("<Escape>", lambda e: dlg.destroy())

        self._theme_popup(dlg)

    def _open_manage_locations_dialog(self):
        """Modal 'Manage Locations' dialog — set active / rename / update / delete."""
        dlg = tk.Toplevel(self.root)
        dlg.title("Manage Locations")
        dlg.resizable(False, False)
        dlg.grab_set()
        dlg.transient(self.root)

        main = ttk.Frame(dlg)
        main.pack(padx=16, pady=14, fill="both", expand=True)

        left = ttk.Frame(main)
        left.pack(side="left", fill="y", padx=(0, 14))
        ttk.Label(left, text="Saved locations:", font=("Helvetica", 9, "bold"),
                  foreground="#778899").pack(anchor="w", pady=(0, 4))
        lb = tk.Listbox(left, height=8, width=24, font=("Helvetica", 11),
                        bg="#1e2d3e", fg="#ffffff",
                        selectbackground="#1e3a5f", selectforeground="#aaddff",
                        relief="flat", borderwidth=1, exportselection=False, activestyle="none")
        lb.pack(fill="y")
        name_list = []

        right = ttk.Frame(main)
        right.pack(side="left", fill="both", expand=True)
        ttk.Label(right, text="Coordinates:", font=("Helvetica", 9, "bold"),
                  foreground="#778899").pack(anchor="w", pady=(0, 4))
        details = tk.Frame(right, bg="#0e1a28", width=200, height=90)
        details.pack(fill="both", expand=True)
        details.pack_propagate(False)
        detail_labels = {}
        for i, k in enumerate(["Latitude", "Longitude"]):
            tk.Label(details, text=f"{k}:", bg="#0e1a28", fg="#8a94a3",
                     font=("Helvetica", 10), anchor="w", width=9
                     ).grid(row=i, column=0, sticky="w", padx=(10, 4), pady=4)
            lbl = tk.Label(details, text="—", bg="#0e1a28", fg="#cfd4dc",
                           font=("Helvetica", 10), anchor="w")
            lbl.grid(row=i, column=1, sticky="w", padx=(0, 10), pady=4)
            detail_labels[k.lower()] = lbl

        btn_row = ttk.Frame(dlg)
        btn_row.pack(padx=16, pady=(0, 14), fill="x")

        def _selected_name():
            sel = lb.curselection()
            if not sel or sel[0] >= len(name_list):
                return None
            return name_list[sel[0]]

        def _refresh_list(preserve_name=None):
            active = self.data.get("active_location", "")
            lb.delete(0, tk.END)
            name_list.clear()
            for p in self.data.get("locations", []):
                name = p.get("name", "")
                if not name:
                    continue
                prefix = "★ " if name == active else "   "
                lb.insert(tk.END, f"{prefix}{name}")
                name_list.append(name)
            if preserve_name and preserve_name in name_list:
                idx = name_list.index(preserve_name)
                lb.selection_set(idx)
                lb.see(idx)
            elif name_list:
                lb.selection_set(0)
            _on_select()

        def _on_select(event=None):
            sel = lb.curselection()
            if not sel or sel[0] >= len(name_list):
                for lbl in detail_labels.values():
                    lbl.configure(text="—")
                return
            p = self._find_location(name_list[sel[0]])
            if p is None:
                return
            lat, lon = p.get("lat"), p.get("lon")
            detail_labels["latitude"].configure(text=f"{float(lat):.4f}" if lat is not None else "—")
            detail_labels["longitude"].configure(text=f"{float(lon):.4f}" if lon is not None else "—")

        lb.bind("<<ListboxSelect>>", _on_select)

        def _do_set_active():
            name = _selected_name()
            if not name:
                return
            self._apply_location(name)
            self._refresh_location_dropdowns()
            _refresh_list(preserve_name=name)
            self._show_toast(f"Active location: {name}")

        def _do_rename():
            name = _selected_name()
            if not name:
                return
            new_name = simpledialog.askstring("Rename Location",
                                              f"Rename '{name}' to:",
                                              parent=dlg, initialvalue=name)
            if new_name is None:
                return
            new_name = new_name.strip()
            if not new_name or new_name == name:
                return
            if self._find_location(new_name):
                messagebox.showerror("Name exists",
                                     f"A location named '{new_name}' already exists.",
                                     parent=dlg)
                return
            p = self._find_location(name)
            p["name"] = new_name
            if self.data.get("active_location") == name:
                self.data["active_location"] = new_name
            self._persist_data()
            _refresh_list(preserve_name=new_name)
            self._refresh_location_dropdowns()

        def _do_update():
            name = _selected_name()
            if not name:
                return
            try:
                lat = float(self._settings_lat_var.get())
                lon = float(self._settings_lon_var.get())
            except (ValueError, AttributeError):
                messagebox.showerror(
                    "No coordinates",
                    "The Settings latitude/longitude fields don't contain valid numbers to save.",
                    parent=dlg)
                return
            if not messagebox.askyesno(
                    "Update Location",
                    f"Overwrite '{name}' with the coordinates currently in Settings "
                    f"({lat:.4f}, {lon:.4f})?", parent=dlg):
                return
            p = self._find_location(name)
            p["lat"], p["lon"] = lat, lon
            if self.data.get("active_location") == name:
                self.data["location"] = {"lat": lat, "lon": lon}
                self.refresh_twilight_header()
                self.refresh_moon_header()
            self._persist_data()
            _refresh_list(preserve_name=name)
            self._show_toast(f"Updated location: {name}")

        def _do_delete():
            name = _selected_name()
            if not name:
                return
            if not messagebox.askyesno(
                    "Delete Location",
                    f"Delete the location '{name}'?\n\nThis cannot be undone.", parent=dlg):
                return
            locations = self.data.setdefault("locations", [])
            locations[:] = [p for p in locations if p.get("name") != name]
            if self.data.get("active_location") == name:
                self.data["active_location"] = ""
            self._persist_data()
            _refresh_list()
            self._refresh_location_dropdowns()
            self._show_toast(f"Deleted location: {name}")

        ttk.Button(btn_row, text="Set Active", command=_do_set_active).pack(side="left")
        ttk.Button(btn_row, text="Rename",     command=_do_rename    ).pack(side="left", padx=(6, 0))
        ttk.Button(btn_row, text="Update",     command=_do_update    ).pack(side="left", padx=(6, 0))
        ttk.Button(btn_row, text="Delete",     command=_do_delete    ).pack(side="left", padx=(6, 0))
        ttk.Button(btn_row, text="Close",      command=dlg.destroy   ).pack(side="right")
        dlg.bind("<Escape>", lambda e: dlg.destroy())

        active = self.data.get("active_location", "")
        _refresh_list(preserve_name=active if active else None)

        self._theme_popup(dlg)

    def on_parameter_change(self, *args):
        """Called whenever a planner Combobox changes — clears queue-edit link, marks dirty if auto-update is on."""
        # Skip if we're programmatically restoring equipment from a queue row
        if getattr(self, "_loading_queue_row", False):
            return
        # Clear the queue editing link when equipment changes on the Planner tab —
        # prevents the new exposure/settings from bleeding into a previously-clicked
        # queue entry that was configured with different equipment.
        try:
            if self.tab_control.select() != str(self.tab_session):
                self._editing_queue_idx = None
        except Exception:
            pass
        # Update the rig chip indicator (shows "Custom…" if chips diverge from
        # the active rig's saved snapshot)
        self._update_rig_indicator()
        if self.auto_update_enabled:
            self._mark_analysis_dirty()

    # ═══════════════════════════════════════════════════════════════════
    # MAIN ANALYSIS — framing, exposure, imaging window, altitude chart
    # ═══════════════════════════════════════════════════════════════════

    def analyze_framing(self):
        """Public entry point for the main analysis pipeline (re-entrancy-guarded wrapper)."""
        # Re-entrancy guard — if update_idletasks() flushes a stacked call,
        # this prevents it from clearing results_txt mid-draw.
        if getattr(self, "_analyzing", False):
            return
        self._analyzing = True
        try:
            self._analyze_framing_impl()
        finally:
            self._analyzing = False

    def _analyze_framing_impl(self):
        """Compute FOV, scale, exposure, imaging window, moon separation for the selected target.

        Populates self.results_txt with the analysis report, renders the DSS thumbnail
        and altitude chart, and updates the Targets List's integration-plan preview.
        """
        raw = self.target_search.get().strip().upper().replace(" ", "")
        raw = self._normalize_catalog_key(raw)

        t = self.targets.get(raw) or self.common_names_map.get(raw)
        s_name, c_name = self.scope_choice.get(), self.camera_choice.get()
        s, c = self.data["scopes"].get(s_name), self.data["cameras"].get(c_name)
        
        try:
            dynamic_reduction = float(self.reduction_factor.get().rstrip("×x"))
        except ValueError:
            dynamic_reduction = 1.0

        if not t or not s or not c:
            if not self.auto_update_enabled:
                messagebox.showwarning("Incomplete", "Please select Scope, Camera, and a valid Target.")
            return

        # Analysis will proceed — clear the dirty flag
        self._analysis_dirty = False

        self.auto_update_enabled = True
        self.current_target_info = t
        # Persist session so it's restored on next launch
        self.data["session"] = {
            "scope":       s_name,
            "camera":      c_name,
            "bortle":      self.bortle_choice.get(),
            "reduction":   self.reduction_factor.get(),
            "target":      self.target_search.get().strip(),
            "filter_mode": self.filter_mode.get(),
        }
        self._persist_data()
        self.results_txt.delete('1.0', tk.END)

        native_fl = float(s.get("native_fl", 1))
        aperture = float(s.get("aperture", 1))
        eff_fl = native_fl * dynamic_reduction
        eff_f_ratio = eff_fl / aperture
        
        ps = float(c.get("pixel_size", 1))
        sw, sh = float(c.get("sensor_w", 1)), float(c.get("sensor_h", 1))
        
        fw, fh = 2*math.degrees(math.atan(sw/(2*eff_fl))), 2*math.degrees(math.atan(sh/(2*eff_fl)))
        # Remember the framed FOV (deg) for the sky-map window; the position
        # angle is read live from self._fov_angle when the map is opened.
        self._skymap_fovw, self._skymap_fovh = fw, fh
        scale = (ps / eff_fl) * 206.265
        t_maj, t_min = t['size_maj']/60.0, t['size_min']/60.0

        if scale < 0.67:
            status, tag = "Over-sampled", "warning"
        elif 0.67 <= scale <= 2.0:
            status, tag = "Optimal", "optimal"
        else:
            status, tag = "Under-sampled", "warning"

        b_data = BORTLE_FACTORS.get(self.bortle_choice.get())
        is_color = c.get("is_color", True)
        
        rf = 1.0 
        if not is_color:
            rf = self._rf_for_filter_mode(self.filter_mode.get())

        sky_flux = (((b_data["color"] if is_color else b_data["mono"]) * (c.get("qe", 0.5) * (ps**2))) / (eff_f_ratio**2)) / rf
        exp = (C_VALUE * (c.get("read_noise", 1)**2)) / (sky_flux + 1e-5)

        _mag_parts = []
        if t.get("v_mag") is not None:
            _mag_parts.append(f"V-mag: {t['v_mag']:.1f}")
        if t.get("surf_br") is not None:
            _mag_parts.append(f"SB: {t['surf_br']:.1f}")
        _mag_suffix = ("  ·  " + "  ·  ".join(_mag_parts)) if _mag_parts else ""
        ra_str  = self._fmt_ra_hms(t["ra_deg"])
        dec_str = self._fmt_dec_dms(t["dec_deg"])

        # ── TARGET header ─────────────────────────────────────────────────
        self.results_txt.insert(tk.END, f"{t['id']}", "header")
        if t['common']:
            self.results_txt.insert(tk.END, f"  —  {t['common'].split(';')[0].strip()}", "header")
        self.results_txt.insert(tk.END, f"\n")
        self.results_txt.insert(tk.END, f"RA {ra_str}  ·  Dec {dec_str}{_mag_suffix}\n", "dim")
        self.results_txt.insert(tk.END, "\n")

        # ── FRAMING section ───────────────────────────────────────────────
        self.results_txt.insert(tk.END, "━ FRAMING ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n", "sec_framing")
        self.results_txt.insert(tk.END, "  Image scale: ", "dim")
        self.results_txt.insert(tk.END, f"{scale:.2f}\"/px ", "highlight")
        self.results_txt.insert(tk.END, f"({status})\n", tag)
        self.results_txt.insert(tk.END, "  Focal length: ", "dim")
        self.results_txt.insert(tk.END, f"{eff_fl:.0f}mm (f/{eff_f_ratio:.1f})\n")
        self.results_txt.insert(tk.END, "  FOV: ", "dim")
        self.results_txt.insert(tk.END, f"{fw:.2f}° × {fh:.2f}°\n")
        f_fits = (t_maj < fw and t_min < fh)
        self.results_txt.insert(tk.END, "  Framing: ", "dim")
        self.results_txt.insert(tk.END, "✅ Fits sensor\n" if f_fits else "❌ Too big for sensor\n",
                                "optimal" if f_fits else "warning")
        self.results_txt.insert(tk.END, "\n")

        # ── EXPOSURE section ──────────────────────────────────────────────
        self.results_txt.insert(tk.END, "━ EXPOSURE ━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n", "sec_exposure")
        self.results_txt.insert(tk.END, "  Recommended sub: ", "dim")
        self.results_txt.insert(tk.END, f"{exp:.1f}s\n", "amber")
        self.results_txt.insert(tk.END, "  Sky flux: ", "dim")
        self.results_txt.insert(tk.END, f"{sky_flux:.1f} e⁻/px/s\n")
        self.results_txt.insert(tk.END, "  Read noise: ", "dim")
        self.results_txt.insert(tk.END, f"{c.get('read_noise', '?')} e⁻\n")
        # Regime note
        if sky_flux * exp > float(c.get("read_noise", 3))**2 * 5:
            regime_text, regime_tag = "Sky-limited ✓", "amber"
        elif sky_flux * exp > float(c.get("read_noise", 3))**2:
            regime_text, regime_tag = "Transitional", "highlight"
        else:
            regime_text, regime_tag = "Read-noise limited", "warning"
        self.results_txt.insert(tk.END, "  Regime: ", "dim")
        self.results_txt.insert(tk.END, f"{regime_text}\n", regime_tag)
        self.results_txt.insert(tk.END, "\n")

        # ── Moon separation ───────────────────────────────────────────────
        _, illum_pct, _, _, _, _ = _calc_moon_phase()
        sep_deg, _, _ = _calc_moon_separation(t["ra_deg"], t["dec_deg"])
        if illum_pct <= 10:
            moon_tag  = "optimal"
            moon_icon = "🟢"
            moon_note = f"Moon {illum_pct}% — negligible"
            moon_impact = "Negligible interference"
        elif sep_deg >= 45:
            moon_tag  = "optimal"
            moon_icon = "🟢"
            moon_note = f"Moon {sep_deg:.0f}° away ({illum_pct}%)"
            moon_impact = "Minimal interference"
        elif sep_deg >= 20:
            moon_tag  = "highlight"
            moon_icon = "🟡"
            moon_note = f"Moon {sep_deg:.0f}° away ({illum_pct}%)"
            moon_impact = "Moderate — consider NB filters"
        else:
            moon_tag  = "warning"
            moon_icon = "🔴"
            moon_note = f"Moon {sep_deg:.0f}° away ({illum_pct}%)"
            moon_impact = "Significant interference"

        # Store values the integration planner needs
        self._last_exp_s    = exp
        self._last_sky_flux = sky_flux

        # ── Best imaging window ───────────────────────────────────────────
        lat, lon = self._get_saved_location()
        if lat is not None:
            _min_alt = float(self.data.get("settings", {}).get("min_alt", 20))
            win_start, win_end, win_hrs, peak_t, peak_a, _ = _calc_best_imaging_window(
                t["ra_deg"], t["dec_deg"], lat, lon, min_alt=_min_alt)
            self._last_win_hrs   = win_hrs if win_hrs else None
            self._last_win_start = win_start
            self._last_win_end   = win_end
            # Auto-fill the Targets List with tonight's actual dark window,
            # but only if the active queue entry doesn't already have a user-set alloc_hrs.
            if win_hrs and win_hrs > 0:
                editing_idx = getattr(self, "_editing_queue_idx", None)
                has_user_alloc = (
                    editing_idx is not None
                    and editing_idx < len(self._queue_entries)
                    and self._queue_entries[editing_idx].get("alloc_hrs")
                )
                if not has_user_alloc:
                    # Use default_alloc_hrs from settings if configured, else window
                    default_alloc = self.data.get("settings", {}).get("default_alloc_hrs", "0")
                    try:
                        default_alloc_f = float(default_alloc)
                    except (ValueError, TypeError):
                        default_alloc_f = 0
                    if default_alloc_f > 0:
                        self.session_hours_var.set(f"{default_alloc_f:.1f}")
                    else:
                        self.session_hours_var.set(f"{win_hrs:.1f}")

            # ── TONIGHT'S WINDOW section ──────────────────────────────────
            self.results_txt.insert(tk.END, "━ TONIGHT'S WINDOW ━━━━━━━━━━━━━━━━━━━━\n", "sec_window")
            if win_start:
                self.results_txt.insert(tk.END, "  Window: ", "dim")
                self.results_txt.insert(tk.END, f"{win_start} – {win_end}", "optimal")
                self.results_txt.insert(tk.END, f"  ({win_hrs}h)\n", "optimal")
                self.results_txt.insert(tk.END, "  Peak alt: ", "dim")
                self.results_txt.insert(tk.END, f"{peak_a}° at {peak_t}\n")
                try:
                    transit_str = _calc_transit_time(t["ra_deg"], lon)
                    self.results_txt.insert(tk.END, "  Transit: ", "dim")
                    self.results_txt.insert(tk.END, f"{transit_str}\n")
                except Exception:
                    pass
            else:
                self.results_txt.insert(tk.END, "  ")
                self.results_txt.insert(tk.END,
                    f"⚠️ Target below {int(_min_alt)}° during all dark hours tonight\n",
                    "warning")
            self.results_txt.insert(tk.END, "\n")

            # ── MOON section ──────────────────────────────────────────────
            self.results_txt.insert(tk.END, "━ MOON ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n", "sec_moon")
            self.results_txt.insert(tk.END, "  Separation: ", "dim")
            self.results_txt.insert(tk.END, f"{sep_deg:.0f}°\n", moon_tag)
            self.results_txt.insert(tk.END, "  Illumination: ", "dim")
            self.results_txt.insert(tk.END, f"{illum_pct}%\n", moon_tag)
            self.results_txt.insert(tk.END, "  Impact: ", "dim")
            self.results_txt.insert(tk.END, f"{moon_impact}\n", moon_tag)

            # Draw altitude chart
            self.root.after(10, lambda tgt=t, la=lat, lo=lon: self._draw_altitude_chart(tgt, la, lo))

        cw, ch = 240, 240

        # Store FOV params so the image callback can draw the overlay
        self._fov_params = (fw, fh, t_maj, t_min)

        needed_survey = min(max(fw, fh, 0.1) * 1.6, 5.0)

        cached_ok = (self._cached_dss_img is not None
                     and self._cached_dss_target == t['id']
                     and self._cached_dss_survey_deg is not None
                     and 0.5 <= needed_survey / self._cached_dss_survey_deg <= 2.0)

        if cached_ok:
            self._draw_fov_overlay(self._cached_dss_img, self._cached_dss_survey_deg, t['id'])
        else:
            # New target or FOV scale changed significantly — reset framing and fetch fresh DSS
            self._fov_offset_x = 0.0
            self._fov_offset_y = 0.0
            self._fov_angle    = 0.0
            self._zoom_level   = 1.0
            self._rot_label.config(text="0°")
            self._zoom_label.config(text="1.0×")
            self.preview_canvas.delete("all")
            self.preview_canvas.create_text(cw//2, ch//2, text="Loading DSS image…", fill="yellow", font=("Helvetica", 9))
            self.root.after(50, lambda: self.fetch_thumbnail(t, fw, fh))

        # Auto-refresh the Targets List tab so it always reflects the current target
        self.root.after(20, lambda: self.show_integration_plan(silent=True))

    def _draw_altitude_chart(self, t, lat, lon):
        """Draw an altitude-vs-time chart covering tonight's nautical dark window."""
        canvas = self.alt_canvas
        canvas.update_idletasks()
        cw = canvas.winfo_width() or 860
        ch = canvas.winfo_height() or 180
        canvas.delete("all")
        self._moon_bar_hits = []   # reset for this draw

        # Text colours depend on night mode
        nm = self.night_mode
        fg_main   = "#cc0000" if nm else "#ffffff"
        fg_dim    = "#880000" if nm else "#aabbcc"
        fg_label  = "#aa0000" if nm else "#ccaa00"

        # ── Scan full night in 5-min steps to find nautical dark bounds ────
        real_now      = datetime.now()   # kept for the "Now" marker below
        utc_offset_h  = _utc_offset_hours()

        jd_noon      = _jd_local_noon()

        step_jd  = 5.0 / 1440.0
        n_full   = int(30 * 60 / 5) + 1   # 30-hour window

        dark_start_jd = dark_end_jd = None
        been_dark = False
        for i in range(n_full):
            jd = jd_noon + i * step_jd
            sun_ra, sun_dec = _sun_ra_dec(jd)
            sun_alt = _ra_dec_to_altaz(sun_ra, sun_dec, lat, lon, jd)
            if sun_alt < -12.0:
                been_dark = True
                if dark_start_jd is None:
                    dark_start_jd = jd
                dark_end_jd = jd
            elif been_dark:
                break   # sun has risen again

        if dark_start_jd is None:
            # No dark window — draw message
            canvas.create_text(cw // 2, ch // 2,
                text="No nautical dark window tonight at this location",
                fill=fg_main, font=("Helvetica", 9))
            return

        # Add 30-min padding on each side so curve doesn't clip at edges
        pad_jd = 30.0 / 1440.0
        scan_start = dark_start_jd - pad_jd
        scan_end   = dark_end_jd   + pad_jd
        total_jd   = scan_end - scan_start

        # Sample the target altitude across this window
        n_steps = max(120, int(total_jd * 1440 / 5))
        step    = total_jd / (n_steps - 1)

        times_h, alts = [], []
        for i in range(n_steps):
            jd      = scan_start + i * step
            local_h = (((jd + 0.5) % 1.0) * 24.0 + utc_offset_h) % 24.0
            alt     = _ra_dec_to_altaz(t["ra_deg"], t["dec_deg"], lat, lon, jd)
            times_h.append(local_h)
            alts.append(alt)

        # ── Moon rise/set within the scan window ────────────────────────────
        moon_rise_x = moon_set_x = None
        moon_rise_label = moon_set_label = ""
        prev_moon_alt = None
        for i in range(n_steps):
            jd = scan_start + i * step
            m_ra, m_dec = _calc_moon_position(jd)
            m_alt = _ra_dec_to_altaz(m_ra, m_dec, lat, lon, jd)
            if prev_moon_alt is not None:
                frac_x = i / (n_steps - 1)
                if prev_moon_alt < 0 <= m_alt and moon_rise_x is None:
                    moon_rise_x = frac_x
                    lh = (((jd + 0.5) % 1.0) * 24.0 + utc_offset_h) % 24.0
                    moon_rise_label = f"🌕 {int(lh):02d}:{int((lh%1)*60):02d}"
                elif prev_moon_alt >= 0 > m_alt and moon_set_x is None:
                    moon_set_x = frac_x
                    lh = (((jd + 0.5) % 1.0) * 24.0 + utc_offset_h) % 24.0
                    moon_set_label = f"🌑 {int(lh):02d}:{int((lh%1)*60):02d}"
            prev_moon_alt = m_alt

        # ── Layout ─────────────────────────────────────────────────────────
        lm, rm, tm, bm = 46, 16, 22, 28
        pw = cw - lm - rm
        ph = ch - tm - bm

        y_min = max(-10, min(alts) - 5)
        y_max = min(90,  max(alts) + 8)
        y_range = y_max - y_min or 1

        def ix(i):   return lm + (i / (n_steps - 1)) * pw
        def iy(alt): return tm + ph - ((alt - y_min) / y_range) * ph
        def clamp_y(y): return max(tm - 2, min(tm + ph + 2, y))
        def fx(frac): return lm + frac * pw

        # ── Dark window shading ─────────────────────────────────────────────
        dark_x0 = lm + ((dark_start_jd - scan_start) / total_jd) * pw
        dark_x1 = lm + ((dark_end_jd   - scan_start) / total_jd) * pw
        canvas.create_rectangle(dark_x0, tm, dark_x1, tm + ph, fill="#0a1020", outline="")
        canvas.create_rectangle(lm, tm, dark_x0, tm + ph, fill="#1a2638", outline="")
        canvas.create_rectangle(dark_x1, tm, lm + pw, tm + ph, fill="#1a2638", outline="")

        # ── Moon rise/set bars (drawn over background, under the curve) ─────
        for mx, mlabel, side in [
            (moon_rise_x, moon_rise_label, "rise"),
            (moon_set_x,  moon_set_label,  "set"),
        ]:
            if mx is not None:
                x = fx(mx)
                # Translucent yellow bar spanning full plot height
                canvas.create_rectangle(x - 2, tm, x + 2, tm + ph,
                                        fill="#aaaa00", outline="")
                # Label above the bar
                anchor = "sw" if side == "rise" else "se"
                ox = 5 if side == "rise" else -5
                canvas.create_text(x + ox + 1, tm + 2, text=mlabel,
                                   fill="#000000", font=("Helvetica", 8, "bold"),
                                   anchor=anchor)
                canvas.create_text(x + ox, tm + 1, text=mlabel,
                                   fill="#ffee00", font=("Helvetica", 8, "bold"),
                                   anchor=anchor)
                # Store for hover tooltip
                action = "rises above" if side == "rise" else "sets below"
                time_str = mlabel.split(" ", 1)[-1] if " " in mlabel else mlabel
                tip = f"🌕 Moon {action} the horizon\n    at {time_str} local time"
                self._moon_bar_hits.append((x, tip))

        # ── Grid lines ─────────────────────────────────────────────────────
        tick_alts = [a for a in range(0, 91, 15) if y_min <= a <= y_max]
        for a in tick_alts:
            y = iy(a)
            canvas.create_line(lm, y, lm + pw, y, fill="#1e2e40", width=1)
            canvas.create_text(lm - 5, y, text=f"{a}°", fill=fg_main,
                               font=("Helvetica", 10, "bold"), anchor="e")

        # ── Min-altitude line (from Settings → min_alt) ──────────────────
        _min_alt = int(self.data.get("settings", {}).get("min_alt", 20))
        if y_min <= _min_alt <= y_max:
            y_ma = iy(_min_alt)
            canvas.create_line(lm, y_ma, lm + pw, y_ma,
                               fill="#887700", width=1, dash=(4, 5))
            canvas.create_text(lm + 4, y_ma - 3, text=f"{_min_alt}° min",
                               fill=fg_label, font=("Helvetica", 9, "bold"), anchor="sw")

        # ── Horizon line ────────────────────────────────────────────────────
        if y_min <= 0 <= y_max:
            yh = iy(0)
            canvas.create_line(lm, yh, lm + pw, yh,
                               fill="#993333", width=1, dash=(5, 4))

        # ── X-axis time labels ───────────────────────────────────────────────
        dark_span_h = (dark_end_jd - dark_start_jd) * 24.0
        label_step_h = 1 if dark_span_h <= 8 else 2
        scan_start_localh = (((scan_start + 0.5) % 1.0) * 24.0 + utc_offset_h) % 24.0
        first_label_h = math.ceil(scan_start_localh / label_step_h) * label_step_h
        h = first_label_h
        while True:
            h_frac = ((h - scan_start_localh) % 24) / (total_jd * 24.0)
            if h_frac > 1.0:
                break
            x = lm + h_frac * pw
            canvas.create_line(x, tm, x, tm + ph, fill="#1a2a3a", width=1)
            canvas.create_text(x, tm + ph + 4, text=f"{int(h) % 24:02d}:00",
                               fill=fg_main, font=("Helvetica", 10, "bold"), anchor="n")
            h = (h + label_step_h) % 24
            if h_frac > 0.98:
                break

        # ── "Now" marker (only shown when viewing today's date) ─────────────
        if _PLANNING_DATE is None:
            now_h_local = (real_now.hour + real_now.minute / 60.0)
            now_frac    = ((now_h_local - scan_start_localh) % 24) / (total_jd * 24.0)
            if 0.0 <= now_frac <= 1.0:
                now_x = lm + now_frac * pw
                canvas.create_line(now_x, tm, now_x, tm + ph,
                                   fill=fg_main, width=1, dash=(2, 3))
                canvas.create_text(now_x, tm - 3, text="Now", fill=fg_dim,
                                   font=("Helvetica", 9, "bold"), anchor="s")

        # ── Altitude line ────────────────────────────────────────────────────
        line_col  = "#cc2200" if nm else "#00ccff"
        line_col2 = "#551100" if nm else "#336688"
        fill_col  = "#220500" if nm else "#002838"   # soft fill under curve

        # ── Blue fill under the curve (polygon from curve down to baseline) ─
        baseline_y = clamp_y(iy(0)) if y_min <= 0 <= y_max else tm + ph
        fill_pts = []
        for i in range(n_steps):
            x = ix(i)
            alt = alts[i]
            if alt >= 0:
                y = clamp_y(iy(alt))
                fill_pts.extend([x, y])
        if len(fill_pts) >= 4:
            # Close the polygon down to the baseline
            first_x = fill_pts[0]
            last_x  = fill_pts[-2]
            fill_poly = list(fill_pts) + [last_x, baseline_y, first_x, baseline_y]
            canvas.create_polygon(fill_poly, fill=fill_col, outline="", smooth=True)

        # ── Imaging window dashed vertical lines ─────────────────────────
        if self._last_win_start and self._last_win_end:
            for win_time_str, win_label in [
                (self._last_win_start, "▼ start"),
                (self._last_win_end,   "▼ end"),
            ]:
                try:
                    parts = win_time_str.split(":")
                    win_h = float(parts[0]) + float(parts[1]) / 60.0
                    win_frac = ((win_h - scan_start_localh) % 24) / (total_jd * 24.0)
                    if 0.0 <= win_frac <= 1.0:
                        wx = lm + win_frac * pw
                        win_line_col = "#661100" if nm else "#4caf50"
                        canvas.create_line(wx, tm, wx, tm + ph,
                                           fill=win_line_col, width=1, dash=(4, 4))
                except (ValueError, IndexError):
                    pass

        # ── Altitude curve lines ─────────────────────────────────────────
        above, below = [], []
        for i in range(n_steps):
            x = ix(i)
            y = clamp_y(iy(alts[i]))
            if alts[i] >= 0:
                above.extend([x, y])
                if len(below) >= 4:
                    canvas.create_line(below, fill=line_col2, width=1.5, smooth=True)
                below = []
            else:
                below.extend([x, y])
                if len(above) >= 4:
                    canvas.create_line(above, fill=line_col, width=2, smooth=True)
                above = []
        if len(above) >= 4:
            canvas.create_line(above, fill=line_col, width=2, smooth=True)
        if len(below) >= 4:
            canvas.create_line(below, fill=line_col2, width=1.5, smooth=True)

        # ── Peak marker ──────────────────────────────────────────────────────
        peak_i   = max(range(n_steps), key=lambda i: alts[i])
        peak_alt = alts[peak_i]
        peak_x   = ix(peak_i)
        peak_y   = clamp_y(iy(peak_alt))
        ph_val   = int(times_h[peak_i]) % 24
        pm_val   = int((times_h[peak_i] % 1) * 60)

        ref_y = iy(_min_alt) if y_min <= _min_alt <= y_max else iy(0)
        canvas.create_line(peak_x, peak_y, peak_x, ref_y,
                           fill="#886600", width=1, dash=(2, 4))
        canvas.create_oval(peak_x - 6, peak_y - 6, peak_x + 6, peak_y + 6,
                           fill="#332200", outline="#ffcc00", width=2)
        canvas.create_oval(peak_x - 3, peak_y - 3, peak_x + 3, peak_y + 3,
                           fill="#ffff00", outline="")

        label  = f"▲ {peak_alt:.0f}°  @  {ph_val:02d}:{pm_val:02d}"
        anchor = "sw" if peak_x < lm + pw * 0.65 else "se"
        ox     = 9 if anchor == "sw" else -9
        canvas.create_text(peak_x + ox + 1, peak_y - 7, text=label,
                           fill="#000000", font=("Helvetica", 9, "bold"), anchor=anchor)
        canvas.create_text(peak_x + ox,     peak_y - 8, text=label,
                           fill="#ffee44",  font=("Helvetica", 9, "bold"), anchor=anchor)

        # ── Border & title ───────────────────────────────────────────────────
        canvas.create_rectangle(lm, tm, lm + pw, tm + ph, outline="#334455", width=1)
        common = t['common'].split(';')[0].strip() if t.get('common') else ""
        title  = f"{t['id']}" + (f"  —  {common}" if common else "") + "  │  Tonight's Imaging Window"
        canvas.create_text(lm + pw // 2, 4, text=title,
                           fill=fg_main, font=("Helvetica", 10, "bold"), anchor="n")
        canvas.create_text(10, tm + ph // 2, text="Alt\n(°)",
                           fill=fg_main, font=("Helvetica", 9, "bold"), anchor="center", justify="center")

        # Store chart geometry so the hover handler can interpolate time/altitude
        self._chart_meta = {
            "lm": lm, "tm": tm, "pw": pw, "ph": ph,
            "y_min": y_min, "y_range": y_range,
            "times_h": times_h, "alts": alts,
            "scan_start_localh": scan_start_localh,
            "total_jd_h": total_jd * 24.0,
        }

    # ═══════════════════════════════════════════════════════════════════
    # FOV OVERLAY — DSS thumbnail with draggable sensor frame
    # ═══════════════════════════════════════════════════════════════════

    def fetch_thumbnail(self, target_info, fov_w_deg, fov_h_deg):
        """Fetch a DSS image sized to show the FOV in context, then overlay the sensor frame.

        Stale-result guard: the image is fetched on a background thread, and
        the user may switch targets (via Planner search or a queue-card auto-
        load) before the download completes.  The ``_apply`` callback checks
        that ``self.current_target_info`` still matches the fetched target
        before writing the cache or drawing the overlay; a mismatch means the
        user has moved on and this result is discarded.  Without this guard,
        a slow-arriving fetch for a previous target will overwrite the FOV
        canvas with the wrong galaxy.
        """
        target_id = target_info['id']

        # Survey size: show enough sky so the FOV fits with some padding.
        # We fetch a square patch; the larger of FOV dims sets the size, +40% padding.
        survey_size = max(fov_w_deg, fov_h_deg, 0.1) * 1.6
        survey_size = min(survey_size, 5.0)  # SkyView cap

        def _fetch():
            try:
                pixels = 600  # fetch high-res, we'll downscale
                url = (
                    f"https://skyview.gsfc.nasa.gov/current/cgi/runquery.pl"
                    f"?Survey=DSS2+Red&Position={urllib.request.quote(target_id)}"
                    f"&Size={survey_size:.4f}&Pixels={pixels}&Return=GIF&Catalog=none"
                )
                req = urllib.request.Request(url, headers={"User-Agent": f"LightbucketAstroPlanner/{__version__}"})
                with urllib.request.urlopen(req, timeout=15) as resp:
                    raw = resp.read()

                img = Image.open(io.BytesIO(raw)).convert("RGB")

                def _apply():
                    # Discard if the user has navigated to a different target
                    # while this fetch was in flight.
                    cur = self.current_target_info
                    if cur is None or cur.get("id") != target_id:
                        return
                    self._cached_dss_img = img
                    self._cached_dss_target = target_id
                    self._cached_dss_survey_deg = survey_size
                    self._draw_fov_overlay(img, survey_size, target_id)

                self.root.after(0, _apply)
            except Exception:
                # Same stale-result check for the error path — don't blank
                # the canvas with "DSS image unavailable" if the user has
                # already moved on to a target whose fetch may yet succeed.
                def _apply_err():
                    cur = self.current_target_info
                    if cur is None or cur.get("id") != target_id:
                        return
                    self._fov_image_error()
                self.root.after(0, _apply_err)

        threading.Thread(target=_fetch, daemon=True).start()

    def _draw_fov_overlay(self, dss_img, survey_deg, target_id):
        """Scale DSS image to the canvas and draw the sensor FOV rectangle (with pan/rotate) on top."""
        cw, ch = 240, 240

        # Apply zoom: center-crop a 1/zoom fraction of the image then resize to canvas
        zoom = max(1.0, self._zoom_level)
        orig_w, orig_h = dss_img.size
        crop_w = orig_w / zoom
        crop_h = orig_h / zoom
        left  = (orig_w - crop_w) / 2
        top   = (orig_h - crop_h) / 2
        dss_cropped = dss_img.crop((left, top, left + crop_w, top + crop_h))
        dss_resized = dss_cropped.resize((cw, ch), Image.Resampling.LANCZOS)
        photo = ImageTk.PhotoImage(dss_resized)
        self._fov_photo = photo

        self.preview_canvas.delete("all")
        self.preview_canvas.create_image(0, 0, anchor="nw", image=photo)

        px_per_deg = cw / survey_deg * zoom

        if self._fov_params:
            fw, fh, t_maj, t_min = self._fov_params
            rw = fw * px_per_deg / 2
            rh = fh * px_per_deg / 2

            cx = cw / 2 + self._fov_offset_x
            cy = ch / 2 + self._fov_offset_y

            angle_rad = math.radians(self._fov_angle)
            cos_a, sin_a = math.cos(angle_rad), math.sin(angle_rad)

            def rot(dx, dy):
                return cx + dx * cos_a - dy * sin_a, cy + dx * sin_a + dy * cos_a

            corners = [rot(-rw, -rh), rot(+rw, -rh), rot(+rw, +rh), rot(-rw, +rh)]
            self._fov_corners = corners  # save for hit-testing

            flat = [coord for pt in corners for coord in pt]
            self.preview_canvas.create_polygon(flat, outline="white", fill="", width=2, dash=(6, 3))

            # Corner grab handles — filled circles
            hr = 6  # handle radius
            for px, py in corners:
                self.preview_canvas.create_oval(px-hr, py-hr, px+hr, py+hr,
                                                outline="yellow", fill="#333300", width=2)

            # Crosshair at FOV centre
            arm = 8
            self.preview_canvas.create_line(cx-arm, cy, cx+arm, cy, fill="yellow", width=1)
            self.preview_canvas.create_line(cx, cy-arm, cx, cy+arm, fill="yellow", width=1)

        fits = self._fov_params and self._fov_params[0] >= self._fov_params[2] and self._fov_params[1] >= self._fov_params[3]
        label = f"{target_id}  {'✅ fits' if fits else '❌ too big'}  {self._fov_angle:.1f}°"
        self.preview_canvas.create_text(cw//2+1, ch-9,  text=label, fill="black", font=("Helvetica", 8, "bold"))
        self.preview_canvas.create_text(cw//2,   ch-10, text=label, fill="white", font=("Helvetica", 8, "bold"))

    # --- Pan / rotate helpers ---

    def _fov_corner_hit(self, x, y, radius=12):
        """Return index of corner within radius of (x,y), or None."""
        for i, (cx, cy) in enumerate(self._fov_corners):
            if math.hypot(x - cx, y - cy) <= radius:
                return i
        return None

    def _fov_centre(self):
        """Return the current (x, y) centre of the FOV overlay on the preview canvas."""
        cw, ch = 240, 240
        return cw / 2 + self._fov_offset_x, ch / 2 + self._fov_offset_y

    def _framed_center(self, ra0_deg, dec0_deg):
        """Return (ra_deg, dec_deg) of the FOV box centre after any pan.

        The preview maps sky to canvas as px_per_deg = 240 / survey_deg * zoom,
        and the pan is accumulated in canvas pixels (self._fov_offset_x/y).
        SkyView DSS2 images are North-up / East-left, so screen +x is West and
        screen +y is South.  The box centre is positioned before the box
        rotation is applied, so the rotation angle is not needed here.

        Falls back to the catalog centre when there is no pan or the survey
        scale is unknown.
        """
        offset_x   = getattr(self, "_fov_offset_x", 0.0) or 0.0
        offset_y   = getattr(self, "_fov_offset_y", 0.0) or 0.0
        survey_deg = getattr(self, "_cached_dss_survey_deg", None)
        zoom       = getattr(self, "_zoom_level", 1.0) or 1.0

        # No pan, or no scale to convert pixels to degrees -> catalog centre.
        if (offset_x == 0.0 and offset_y == 0.0) or not survey_deg:
            return ra0_deg, dec0_deg

        deg_per_px  = survey_deg / (240.0 * zoom)
        d_east_deg  = -offset_x * deg_per_px   # screen +x (right) = West  = -East
        d_north_deg = -offset_y * deg_per_px   # screen +y (down)  = South = -North

        framed_dec = max(-90.0, min(90.0, dec0_deg + d_north_deg))
        cos_dec    = math.cos(math.radians(dec0_deg))
        if abs(cos_dec) < 1e-6:                # guard near the poles
            framed_ra = ra0_deg % 360.0
        else:
            framed_ra = (ra0_deg + d_east_deg / cos_dec) % 360.0
        return framed_ra, framed_dec

    def _fov_mouse_down(self, event):
        """Begin a pan or rotate drag — picks rotate if the mouse is over a corner handle."""
        if self._fov_corners and self._fov_corner_hit(event.x, event.y) is not None:
            # Rotate mode: record the angle from FOV centre to mouse at drag start
            cx, cy = self._fov_centre()
            self._drag_mode = 'rotate'
            self._rotate_start_angle = math.degrees(math.atan2(event.y - cy, event.x - cx))
            self._rotate_fov_start = self._fov_angle
        else:
            # Pan mode
            self._drag_mode = 'pan'
            self._drag_start = (event.x, event.y)

    def _fov_mouse_drag(self, event):
        """Update pan offset or rotation angle based on the current drag mode."""
        if self._drag_mode == 'pan' and self._drag_start:
            dx = event.x - self._drag_start[0]
            dy = event.y - self._drag_start[1]
            self._fov_offset_x += dx
            self._fov_offset_y += dy
            self._drag_start = (event.x, event.y)
            self._redraw_fov_overlay()
        elif self._drag_mode == 'rotate':
            cx, cy = self._fov_centre()
            current_angle = math.degrees(math.atan2(event.y - cy, event.x - cx))
            delta = current_angle - self._rotate_start_angle
            self._fov_angle = (self._rotate_fov_start + delta) % 360
            self._rot_label.config(text=f"{self._fov_angle:.1f}°")
            self._redraw_fov_overlay()

    def _fov_mouse_up(self, event):
        """End the current pan/rotate drag."""
        self._drag_mode = None
        self._drag_start = None

    def _fov_mouse_hover(self, event):
        """Change cursor to indicate grab handles."""
        if self._fov_corners and self._fov_corner_hit(event.x, event.y) is not None:
            self.preview_canvas.config(cursor="exchange")
        else:
            self.preview_canvas.config(cursor="fleur")

    def _apply_zoom(self, factor):
        """Multiply zoom by factor, clamp to 1×–16×, then redraw."""
        self._zoom_level = max(1.0, min(16.0, self._zoom_level * factor))
        self._zoom_label.config(text=f"{self._zoom_level:.1f}×")
        self._redraw_fov_overlay()

    def _fov_mousewheel(self, event):
        """Zoom on scroll wheel (cross-platform)."""
        if event.num == 4 or (hasattr(event, 'delta') and event.delta > 0):
            self._apply_zoom(1.25)
        else:
            self._apply_zoom(1 / 1.25)

    def _reset_fov_framing(self):
        """Reset the FOV overlay to centred, no rotation, 1× zoom."""
        self._fov_offset_x = 0.0
        self._fov_offset_y = 0.0
        self._fov_angle    = 0.0
        self._zoom_level   = 1.0
        self._rot_label.config(text="0.0°")
        self._zoom_label.config(text="1.0×")
        self._redraw_fov_overlay()

    def _redraw_fov_overlay(self):
        """Redraw the overlay using the cached DSS image (no network call)."""
        if self._cached_dss_img is not None and self._cached_dss_target is not None:
            self._draw_fov_overlay(self._cached_dss_img, self._cached_dss_survey_deg,
                                   self._cached_dss_target)

    def _fov_image_error(self):
        """Display 'DSS image unavailable' fallback on the preview canvas."""
        cw, ch = 240, 240
        self.preview_canvas.delete("all")
        self.preview_canvas.create_text(cw//2, ch//2 - 10, text="DSS image unavailable", fill="gray", font=("Helvetica", 9))
        self.preview_canvas.create_text(cw//2, ch//2 + 10, text="(check network)", fill="gray", font=("Helvetica", 8))
        # Fall back to geometric preview
        if self._fov_params:
            fw, fh, t_maj, t_min = self._fov_params
            px_scale = (min(cw, ch) * 0.8) / max(fw, fh, t_maj, t_min)
            self.preview_canvas.create_rectangle(cw/2-fw*px_scale/2, ch/2-fh*px_scale/2,
                                                  cw/2+fw*px_scale/2, ch/2+fh*px_scale/2, outline="white", width=2)
            self.preview_canvas.create_oval(cw/2-t_maj*px_scale/2, ch/2-t_min*px_scale/2,
                                             cw/2+t_maj*px_scale/2, ch/2+t_min*px_scale/2, outline="cyan", width=2)

    def open_sky_map(self):
        """Open the interactive sky map centred on the current target.

        Feeds the embedded d3-celestial explorer (a pywebview window in a
        separate process) the target centre, framed FOV, position angle and
        theme via a localhost URL.  Coordinates — not the object id — are
        passed, so Sharpless/Caldwell targets resolve correctly.  Falls back
        to the default browser when pywebview/WebView2 is unavailable.
        """
        t = self.current_target_info
        if not t:
            messagebox.showinfo(
                "No Target",
                "Select and analyze a target first, then open the sky map.")
            return
        ra, dec = t.get("ra_deg"), t.get("dec_deg")
        if ra is None or dec is None:
            messagebox.showinfo(
                "No Coordinates",
                "This target has no coordinates to centre the sky map on.")
            return

        skymap_dir = _resource_path("skymap")
        if not skymap_dir.is_dir() or not (skymap_dir / "skymap.html").exists():
            messagebox.showwarning(
                "Sky Map Assets Missing",
                "The sky-map files were not found next to the app.\n"
                "Expected a 'skymap' folder containing skymap.html.")
            return

        import urllib.parse
        params = {
            "ra":      f"{float(ra):.5f}",
            "dec":     f"{float(dec):.5f}",
            "name":    t.get("id", ""),
            "common":  t.get("common", "").split(";")[0].strip(),
            "catalog": self._skymap_catalog_label(t.get("id", "")),
            "fovw":    f"{getattr(self, '_skymap_fovw', 0.0) or 0.0:.4f}",
            "fovh":    f"{getattr(self, '_skymap_fovh', 0.0) or 0.0:.4f}",
            "pa":      f"{getattr(self, '_fov_angle', 0.0) or 0.0:.1f}",
            "theme":   "night" if getattr(self, "night_mode", False) else "day",
        }
        # Active catalog tier → which star/DSO files the map loads + mag ceiling.
        tier = SKYMAP_TIERS.get(self._skymap_active_tier(), SKYMAP_TIERS["lean"])
        params["stars"] = tier["stars"]
        params["dsos"] = tier["dsos"]
        params["maglimit"] = str(tier["maglimit"])
        port = self._ensure_skymap_server(skymap_dir)
        if not port:
            return
        url = (f"http://127.0.0.1:{port}/skymap.html?"
               + urllib.parse.urlencode(params))
        self._spawn_skymap_viewer(url)

    @staticmethod
    def _skymap_catalog_label(name):
        """Best-effort catalog name for the sky-map badge, from the object id."""
        n = (name or "").upper().replace(" ", "")
        if n.startswith("SH2"):
            return "Sharpless"
        if n.startswith("NGC"):
            return "NGC"
        if n.startswith("IC"):
            return "IC"
        if n.startswith("M") and n[1:].isdigit():
            return "Messier"
        if n.startswith("C") and n[1:].isdigit():
            return "Caldwell"
        return ""

    def _ensure_skymap_server(self, skymap_dir):
        """Lazily start a localhost static server rooted at the sky-map folder.

        d3-celestial loads its catalog JSON via XHR, which file:// blocks, so
        the assets must be served over http.  One server (daemon thread, alive
        for the app's lifetime) feeds both the embedded window and any browser
        fallback.  Returns the bound port, or None on failure.
        """
        port = getattr(self, "_skymap_port", None)
        if port:
            return port
        try:
            import functools, http.server, os
            bundled_root = str(skymap_dir)
            user_root = str(self._skymap_catalog_root())

            class _QuietHandler(http.server.SimpleHTTPRequestHandler):
                def log_message(self, *args):
                    pass

                def copyfile(self, source, outputfile):
                    # The viewer may navigate away / close mid-transfer; treat
                    # a dropped connection as normal rather than logging a trace.
                    try:
                        super().copyfile(source, outputfile)
                    except (BrokenPipeError, ConnectionResetError):
                        pass

                def translate_path(self, path):
                    # Prefer a downloaded catalog file (Extended/Full, in the
                    # user data dir) over the bundled lean asset of the same
                    # name; fall back to bundled for everything else.
                    bundled = super().translate_path(path)
                    try:
                        rel = os.path.relpath(bundled, bundled_root)
                        cand = os.path.join(user_root, rel)
                        if os.path.isfile(cand):
                            return cand
                    except Exception:
                        pass
                    return bundled

            handler = functools.partial(_QuietHandler, directory=bundled_root)
            httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
            httpd.daemon_threads = True
            self._skymap_httpd = httpd
            self._skymap_port = httpd.server_address[1]
            threading.Thread(target=httpd.serve_forever, daemon=True).start()
            return self._skymap_port
        except Exception as exc:
            messagebox.showerror(
                "Sky Map Error",
                f"Could not start the local sky-map server:\n{exc}")
            return None

    def _spawn_skymap_viewer(self, url):
        """Launch the sky-map viewer in a separate process (single instance).

        Relaunches this app with ``--skymap-url`` (frozen: the exe itself;
        dev: the interpreter + this script).  The child shows the pywebview
        window or, on failure, opens the browser.  A direct browser open is
        the last-ditch fallback if the relaunch itself fails.

        Only one sky-map window is kept: if one is already open, it is closed
        first so re-opening (same or new target) always yields exactly one
        window showing the current target.  (Browser-fallback tabs can't be
        deduped — we don't control the browser — but that's the degraded path.)
        """
        import subprocess
        prev = getattr(self, "_skymap_proc", None)
        if prev is not None and prev.poll() is None:
            try:
                prev.terminate()
            except Exception:
                pass
        try:
            if getattr(sys, "frozen", False):
                cmd = [sys.executable, "--skymap-url", url]
            else:
                cmd = [sys.executable, os.path.abspath(__file__),
                       "--skymap-url", url]
            self._skymap_proc = subprocess.Popen(cmd)
        except Exception:
            self._skymap_proc = None
            try:
                webbrowser.open(url)
            except Exception:
                pass

    # ─── Sky-map catalog tiers (optional Extended / Full downloads) ──────
    def _skymap_catalog_root(self):
        """User-writable folder holding downloaded sky-map catalog files."""
        base = (Path(os.getenv("LOCALAPPDATA")) / "LightbucketAstroPlanner"
                if platform.system() == "Windows"
                else Path.home() / "LightbucketAstroPlanner")
        root = base / "skymap_catalog"
        (root / "data").mkdir(parents=True, exist_ok=True)
        return root

    def _skymap_tier_installed(self, tier):
        """True if a tier's files are present (bundled tiers are always so)."""
        spec = SKYMAP_TIERS.get(tier)
        if not spec:
            return False
        if spec["bundled"]:
            return True
        data_dir = self._skymap_catalog_root() / "data"
        return all((data_dir / f).exists() for f in spec["files"])

    def _skymap_active_tier(self):
        """Active catalog tier, falling back to lean if it isn't installed."""
        tier = "lean"
        try:
            manifest = self._skymap_catalog_root() / "manifest.json"
            if manifest.exists():
                tier = json.loads(manifest.read_text()).get("active", "lean")
        except Exception:
            tier = "lean"
        if tier not in SKYMAP_TIERS or not self._skymap_tier_installed(tier):
            tier = "lean"
        return tier

    def _skymap_set_active_tier(self, tier):
        """Persist the active catalog tier to the catalog manifest."""
        try:
            manifest = self._skymap_catalog_root() / "manifest.json"
            manifest.write_text(json.dumps({"active": tier}))
        except Exception:
            pass

    def _skymap_select_tier(self, tier):
        """Radio handler — activate a tier only if it's installed."""
        if not self._skymap_tier_installed(tier):
            self._skymap_tier_var.set(self._skymap_active_tier())
            return
        self._skymap_set_active_tier(tier)
        self._update_skymap_catalog_ui()

    def _skymap_download_tier(self, tier):
        """Begin a background download of an Extended/Full catalog tier."""
        spec = SKYMAP_TIERS.get(tier)
        if not spec or spec["bundled"] or self._skymap_tier_installed(tier):
            return
        row = self._skymap_rows.get(tier)
        if row:
            row["btn"].grid_remove()
            row["status"].config(text="Starting…", foreground="#556677")
            row["prog"]["value"] = 0
            row["prog"].grid()
        self._skymap_last_pct = {}
        threading.Thread(target=self._skymap_download_worker,
                         args=(tier,), daemon=True).start()

    def _skymap_download_worker(self, tier):
        """Download a tier's files with progress (runs off the UI thread)."""
        spec = SKYMAP_TIERS[tier]
        data_dir = self._skymap_catalog_root() / "data"
        ua = {"User-Agent": f"LightbucketAstroPlanner/{__version__}"}
        try:
            total = 0
            for f in spec["files"]:
                req = urllib.request.Request(SKYMAP_CATALOG_BASE + f,
                                             method="HEAD", headers=ua)
                with urllib.request.urlopen(req, timeout=30) as resp:
                    total += int(resp.headers.get("Content-Length", 0) or 0)
            total = total or 1
            done = 0
            for f in spec["files"]:
                tmp = data_dir / (f + ".part")
                req = urllib.request.Request(SKYMAP_CATALOG_BASE + f, headers=ua)
                with urllib.request.urlopen(req, timeout=120) as resp, open(tmp, "wb") as out:
                    while True:
                        chunk = resp.read(65536)
                        if not chunk:
                            break
                        out.write(chunk)
                        done += len(chunk)
                        self._skymap_progress(tier, done, total)
                tmp.replace(data_dir / f)
            self.root.after(0, lambda: self._skymap_download_done(tier, None))
        except Exception as exc:
            self.root.after(0, lambda e=exc: self._skymap_download_done(tier, e))

    def _skymap_progress(self, tier, done, total):
        """Throttled, thread-safe progress update for a downloading tier."""
        pct = int(done * 100 / total)
        d = getattr(self, "_skymap_last_pct", None)
        if d is None:
            d = {}
            self._skymap_last_pct = d
        if d.get(tier) == pct:
            return
        d[tier] = pct
        mb, tmb = done / 1048576.0, total / 1048576.0

        def _apply():
            row = self._skymap_rows.get(tier)
            if row:
                row["prog"]["value"] = pct
                row["status"].config(text=f"{mb:.1f} / {tmb:.1f} MB",
                                     foreground="#556677")
        self.root.after(0, _apply)

    def _skymap_download_done(self, tier, error):
        """Finalize a tier download on the UI thread (success or failure)."""
        row = self._skymap_rows.get(tier)
        if row:
            row["prog"].grid_remove()
        if error is not None:
            if row:
                row["status"].config(text="✗ download failed",
                                     foreground="#e05555")
                row["btn"].grid()
            messagebox.showerror(
                "Catalog Download Failed",
                f"Could not download the {SKYMAP_TIERS[tier]['label']} "
                f"catalog:\n{error}")
            return
        # Auto-activate the freshly downloaded tier.
        self._skymap_set_active_tier(tier)
        self._skymap_tier_var.set(tier)
        self._update_skymap_catalog_ui()

    def _update_skymap_catalog_ui(self):
        """Refresh tier rows: status text, radio enablement, download buttons."""
        rows = getattr(self, "_skymap_rows", None)
        if not rows:
            return
        active = self._skymap_active_tier()
        for tier, row in rows.items():
            spec = SKYMAP_TIERS[tier]
            installed = self._skymap_tier_installed(tier)
            row["rb"].config(state="normal" if installed else "disabled")
            row["btn"].grid_remove()
            row["prog"].grid_remove()
            if spec["bundled"]:
                row["status"].config(
                    text="bundled · active" if tier == active else "bundled",
                    foreground="#4caf50")
            elif installed:
                row["status"].config(
                    text="✓ installed · active" if tier == active else "✓ installed",
                    foreground="#4caf50")
            else:
                row["status"].config(text="", foreground="#556677")
                row["btn"].config(state="normal")
                row["btn"].grid()
        self._skymap_tier_var.set(active)

    # ═══════════════════════════════════════════════════════════════════
    # COORDINATE PARSING HELPERS
    # ═══════════════════════════════════════════════════════════════════

    @staticmethod
    def _normalize_catalog_key(raw):
        """Normalise a user-entered target string into a catalog lookup key.

        Examples: '7293' → 'NGC7293', 'M042' → 'M42', 'NGC 224' → 'NGC224'.
        Input should already be ``.strip().upper().replace(" ", "")``.
        """
        if raw.isdigit():
            raw = f"NGC{raw}"
        if raw.startswith('M') and raw[1:].isdigit():
            raw = f"M{int(raw[1:])}"
        return raw

    def _parse_ra(self, s):
        """Parse RA from decimal hours OR 'HH:MM:SS.SS' → degrees."""
        if not s:
            return 0.0
        s = str(s).strip()
        try:
            return float(s) * 15.0          # already decimal hours
        except ValueError:
            pass
        parts = s.split(':')
        try:
            h = abs(float(parts[0]))
            m = float(parts[1]) if len(parts) > 1 else 0.0
            sec = float(parts[2]) if len(parts) > 2 else 0.0
            return (h + m / 60.0 + sec / 3600.0) * 15.0
        except Exception:
            return 0.0

    def _parse_dec(self, s):
        """Parse Dec from decimal degrees OR '+DD:MM:SS.SS' → degrees."""
        if not s:
            return 0.0
        s = str(s).strip()
        try:
            return float(s)                  # already decimal degrees
        except ValueError:
            pass
        negative = s.startswith('-')
        parts = s.lstrip('+-').split(':')
        try:
            d = float(parts[0])
            m = float(parts[1]) if len(parts) > 1 else 0.0
            sec = float(parts[2]) if len(parts) > 2 else 0.0
            val = d + m / 60.0 + sec / 3600.0
            return -val if negative else val
        except Exception:
            return 0.0

    def _fmt_ra_hms(self, ra_deg):
        """Convert RA degrees → 'HH:MM:SS' string."""
        ra_h = (ra_deg % 360.0) / 15.0
        h    = int(ra_h)
        m    = int((ra_h - h) * 60)
        s    = (ra_h - h - m / 60.0) * 3600.0
        return f"{h:02d}:{m:02d}:{s:05.2f}"

    def _fmt_dec_dms(self, dec_deg):
        """Convert Dec degrees → '±DD:MM:SS' string."""
        sign    = "-" if dec_deg < 0 else "+"
        dec_abs = abs(dec_deg)
        d = int(dec_abs)
        m = int((dec_abs - d) * 60)
        s = (dec_abs - d - m / 60.0) * 3600.0
        return f"{sign}{d:02d}:{m:02d}:{s:04.1f}"

    # ═══════════════════════════════════════════════════════════════════
    # CATALOG LOADER
    # ═══════════════════════════════════════════════════════════════════

    def _identify_catalogs(self, name, common):
        """Return the set of catalog tags a target belongs to.

        Used during catalog load to pre-tag each entry so the suggestion popup
        and filter logic don't have to re-derive this on every render. Tags
        used elsewhere in the UI: 'NGC', 'IC', 'Messier', 'Caldwell',
        'Sharpless', 'Other'.

        Detection rules:
          NGC/IC/Sharpless — primary name starts with the catalog prefix.
          Messier/Caldwell — primary name OR any common-name alias matches
                             the M<n> / C<n> shape. (Most M/C designations
                             live in the Common names field as aliases on
                             the corresponding NGC/IC row.)
        """
        cats = set()
        n = (name or "").upper().strip()
        if n.startswith("NGC"):
            cats.add("NGC")
        if n.startswith("IC"):
            cats.add("IC")
        if n.startswith("SH2-"):
            cats.add("Sharpless")
        # Addendum-form primary names like 'M040' or 'C009'
        if re.match(r"^M\d+$", n):
            cats.add("Messier")
        if re.match(r"^C\d{1,3}$", n):
            cats.add("Caldwell")
        # Common-names aliases (e.g. "Andromeda Galaxy; M31; C..." )
        for token in re.split(r"[;,]", (common or "").upper()):
            token = token.strip()
            if re.match(r"^M\s*\d+$", token):
                cats.add("Messier")
            elif re.match(r"^C\s*\d{1,3}$", token):
                cats.add("Caldwell")
        if not cats:
            cats.add("Other")
        return cats

    def load_target_catalog(self):
        """Read the NGC/IC CSV plus addendum and bundled Sharpless into the catalog dicts.

        Populates three parallel structures from up to three CSV files:
          targets          — keyed by compact catalog ID (e.g. 'NGC224')
          common_names_map — keyed by uppercase common name (e.g. 'ANDROMEDAGALAXY')
          searchable_names — sorted list used by the suggestion popup

        Sources, all using the same 32-column OpenNGC schema:
          1. self.catalog_path           — NGC + IC (~14,000 entries, downloaded)
          2. self.addendum_path          — non-NGC/IC notable objects (~64 entries,
                                            downloaded; supplies C9/C14/C41/C99,
                                            M40, M45, etc.)
          3. SHARPLESS_CATALOG_FILE      — 313 H II regions (bundled with the app)

        Each source is optional: missing files are silently skipped so a
        partial / offline install still works.  Silently swallows I/O and
        parse errors so startup can still complete; _update_catalog_status()
        will then display a warning in the Settings panel.
        """
        self.targets, self.common_names_map, self.searchable_names = {}, {}, []

        # Build list of sources to ingest in priority order. Later sources
        # cannot overwrite an earlier source's primary key (we check with
        # `setdefault`-style logic), which keeps NGC.csv canonical.
        sources = [self.catalog_path, self.addendum_path]
        bundled_sharpless = _resource_path(SHARPLESS_CATALOG_FILE)
        if bundled_sharpless.exists():
            sources.append(bundled_sharpless)

        for src in sources:
            try:
                if not src.exists():
                    continue
                self._ingest_catalog_csv(src)
            except (OSError, csv.Error, UnicodeDecodeError, ValueError, KeyError):
                # Missing, unreadable, or malformed CSV — keep going; remaining
                # sources may still load successfully.
                pass

        self.searchable_names = sorted(list(set(self.searchable_names)))
        # Update settings panel status if it exists
        self._update_catalog_status()

    def _ingest_catalog_csv(self, path):
        """Parse one OpenNGC-schema CSV and merge its rows into the catalog dicts.

        Used by load_target_catalog() to handle the main NGC.csv, the
        addendum.csv, and the bundled Sharpless catalog through a single
        shared code path.  All three files use the same 32-column schema.
        """
        with open(path, 'r', encoding='utf-8-sig') as f:
            reader = csv.DictReader(f, delimiter=';')
            if reader.fieldnames:
                reader.fieldnames = [n.strip().lower() for n in reader.fieldnames]
            for row in reader:
                name = row.get('name', '').strip().upper()
                if not name:
                    continue
                ra_deg  = self._parse_ra(row.get('ra', '') or '')
                dec_deg = self._parse_dec(row.get('dec', '') or '')
                obj_type_raw = row.get('type', '').strip()
                obj_category = NGC_TYPE_CATEGORIES.get(obj_type_raw, "Other")
                def _try_float(val):
                    """Coerce val to float, returning None for empty/invalid input."""
                    try:
                        return float(val) if val and str(val).strip() else None
                    except (ValueError, TypeError):
                        return None
                info = {
                    "id": name,
                    "common": row.get('common names', ''),
                    "size_maj": float(row.get('majax') or 0),
                    "size_min": float(row.get('minax') or 0),
                    "ra_deg": ra_deg,
                    "dec_deg": dec_deg,
                    "obj_type": obj_category,
                    "v_mag": _try_float(row.get('v-mag') or row.get('vmag') or ''),
                    "surf_br": _try_float(row.get('surfbr') or row.get('surf_br') or ''),
                }
                # Tag with catalog memberships for the search filter + badges.
                info["catalogs"] = self._identify_catalogs(name, info["common"])
                # Don't let later sources overwrite earlier primary keys
                # (NGC.csv wins over addendum wins over Sharpless).
                primary_key = name.replace(" ", "")
                if primary_key not in self.targets:
                    self.targets[primary_key] = info
                self.searchable_names.append(name)
                match = re.match(r'([A-Z]+)\s*(\d+)', name)
                if match:
                    alias_key = f"{match.group(1)}{int(match.group(2))}"
                    if alias_key not in self.targets:
                        self.targets[alias_key] = info
                if info["common"]:
                    for n in re.split(';|,', info["common"]):
                        c = n.strip().upper()
                        compact = c.replace(" ", "")
                        if c:
                            if c not in self.searchable_names:
                                self.searchable_names.append(c)
                            self.common_names_map[compact] = info
                            if compact.startswith('M') and compact[1:].isdigit():
                                norm = f"M{int(compact[1:])}"
                                self.common_names_map[norm] = info
                                if norm not in self.searchable_names:
                                    self.searchable_names.append(norm)
                            # Same normalization for Caldwell ("C09" → "C9")
                            elif compact.startswith('C') and compact[1:].isdigit():
                                norm = f"C{int(compact[1:])}"
                                self.common_names_map[norm] = info
                                if norm not in self.searchable_names:
                                    self.searchable_names.append(norm)

    # ═══════════════════════════════════════════════════════════════════
    # TAB CHANGE & MISC UI HELPERS
    # ═══════════════════════════════════════════════════════════════════

    def _on_tab_changed(self, event=None):
        """Redraw the queue Gantt whenever the Targets List tab becomes visible,
        and the plan Gantt whenever Tonight's Plan tab becomes visible.
        Also auto-selects and loads the first queue row when entering the Targets List.
        Syncs the sidebar highlight to match the active tab."""
        try:
            current = self.tab_control.select()

            # Sync sidebar only if a programmatic tab switch happened
            # (not when _sidebar_select already handled it)
            if hasattr(self, "_sidebar_tabs") and not getattr(self, "_sidebar_switching", False):
                for i, (_, tab, _) in enumerate(self._sidebar_tabs):
                    if current == str(tab):
                        if self._sidebar_active_idx != i:
                            old = self._sidebar_active_idx
                            self._sidebar_active_idx = i
                            self._sidebar_draw_btn(old)
                            self._sidebar_draw_btn(i)
                        break
                else:
                    if current == str(self._sidebar_settings_tab[1]):
                        settings_idx = len(self._sidebar_tabs)
                        if self._sidebar_active_idx != settings_idx:
                            old = self._sidebar_active_idx
                            self._sidebar_active_idx = settings_idx
                            self._sidebar_draw_btn(old)
                            self._sidebar_draw_btn(settings_idx)

            if current == str(self.tab_session):
                # Snapshot the Planner's current target + equipment state.
                # The queue-card auto-load below will overwrite these, and
                # they'll be restored on return to the Planner tab so the
                # user's in-progress search isn't clobbered.
                self._planner_state_backup = {
                    "search":    self.target_search.get(),
                    "scope":     self.scope_choice.get(),
                    "camera":    self.camera_choice.get(),
                    "bortle":    self.bortle_choice.get(),
                    "filter":    self.filter_mode.get(),
                    "reduction": self.reduction_factor.get(),
                }
                self.root.after(10, self._draw_queue_gantt)
                self.root.after(20, self._auto_load_first_queue_row)
            else:
                if current == str(self.tab_planner):
                    # Restore the Planner snapshot taken when the user
                    # entered the Targets tab, so their in-progress search
                    # survives the round trip.  A queue-card click during
                    # the Targets visit clears the backup (see
                    # _queue_card_click) — in that case the user is pivoting
                    # to the clicked target and we leave it in place.
                    if self._planner_state_backup is not None:
                        self._restore_planner_state(self._planner_state_backup)
                        self._planner_state_backup = None
                        self._editing_queue_idx = None
                    # Always re-run analysis when entering the Planner tab.
                    # This is the simplest reliable fix for macOS Tk's Cocoa
                    # backend, which can blank Text/Canvas widgets after tab
                    # switches.  The DSS image is cached so only the text
                    # output and altitude chart are recomputed — fast enough
                    # to feel instant.
                    self._analysis_dirty = False
                    if (self.current_target_info is not None
                            and self.scope_choice.get()
                            and self.camera_choice.get()):
                        self.root.after(50, self.analyze_framing)
                    else:
                        # Empty-state Planner entry (no analysis runnable).
                        # macOS Cocoa Tk leaves the tab black until the
                        # mouse enters the frame — the paint pipeline is
                        # waiting on a Motion event to refresh tracking
                        # areas on the newly-visible widgets.  Synthesize
                        # one to un-stick the paint without requiring the
                        # user to move the cursor.
                        self.root.after(50, self._kick_planner_paint)
                elif current == str(self.tab_plan):
                    self.root.after(10, self._draw_queue_gantt)
                    self.root.after(20, self._refresh_plan_tree)
                elif current == str(self.tab_equip):
                    # Force layout refresh for grid-based equipment panel
                    self.root.after(10, self.refresh_inventory_tables)
                    # Same Cocoa Tk paint-stall workaround as the Planner
                    # tab — Entry/Treeview widgets render black until a
                    # Motion event hits them.
                    self.root.after(50, self._kick_equip_paint)
                elif current == str(self.tab_settings):
                    # Force Cocoa Tk to render the settings panel — same
                    # blank-tab issue as other tabs on macOS.
                    self.root.after(10, self._refresh_settings_display)
        except Exception:
            pass

    def _kick_paint(self, widgets):
        """Simulate the mouse-enter that un-sticks widgets on macOS Cocoa Tk.

        Cocoa Tk sometimes leaves Canvas/Text/Entry/Treeview widgets black
        after a tab switch until the pointer physically enters and (for
        some widget types) the user clicks.  We do three things to cover
        the observed cases:

          1. ``<Motion>`` event at the widget's center — un-sticks Canvas
             and Text widgets on tab switch.
          2. A focus-flick (``focus_set`` then focus back to root) —
             Entry and Treeview widgets respond to focus changes.
          3. ``update_idletasks`` to flush the resulting paint requests.

        Known limitation: on the deleted-JSON auto-route-to-Equip path,
        Cocoa ignores these synthetic events entirely and only a real
        mouse click repaints.  The welcome-dialog hint documents the
        workaround for that rare case.

        The focus flick is brief enough (sub-millisecond) that the user
        doesn't see any focused state — we save and restore whatever had
        focus to begin with so text selection isn't lost.
        """
        try:
            # Remember the currently-focused widget so we can restore it
            saved_focus = self.root.focus_get()
            for w in widgets:
                try:
                    ww = w.winfo_width()
                    wh = w.winfo_height()
                    if ww > 0 and wh > 0:
                        w.event_generate("<Motion>", warp=False,
                                         x=ww // 2, y=wh // 2)
                        # Focus flick — Entry and Treeview respond to this
                        w.focus_set()
                except Exception:
                    pass
            # Restore focus so we don't leave a random widget highlighted
            try:
                if saved_focus is not None:
                    saved_focus.focus_set()
                else:
                    self.root.focus_set()
            except Exception:
                self.root.focus_set()
            self.root.update_idletasks()
        except Exception:
            pass

    def _kick_planner_paint(self):
        """Kick the finicky widgets on the Planner tab into painting."""
        self._kick_paint([self.preview_canvas, self.alt_canvas, self.results_txt])

    def _kick_equip_paint(self):
        """Kick the finicky widgets on the Equipment tab into painting.

        Covers both the camera/scope entry fields (Entry widgets that go
        black on fresh tab show) and the inventory Treeviews.
        """
        widgets = []
        widgets.extend(self.cam_entries.values())
        widgets.extend(self.scope_entries.values())
        widgets.extend([self.cam_tree, self.scope_tree])
        self._kick_paint(widgets)

    def _auto_load_first_queue_row(self):
        """Select & fully load the appropriate queue card on Targets-tab entry.

        Runs the full ``_queue_load_target`` so the Targets panel (stat
        cards, integration plan, altitude chart) reflects the queued
        target, not whatever the Planner was last analysing.  The
        Planner's pre-visit search + equipment is already snapshotted
        into ``_planner_state_backup`` by ``_on_tab_changed`` and will
        be restored when the user tabs back.

        Early-exits if the user tabbed away before the scheduled
        ``after(20)`` callback fired.  Idempotent — if the fields are
        already bound to the selected card, nothing happens.
        """
        # Bail if the user tabbed away during the after() delay
        try:
            if self.tab_control.select() != str(self.tab_session):
                return
        except Exception:
            return

        if not self._queue_entries:
            return
        # Pick the first card if nothing (valid) is currently selected
        if (self._queue_card_selected is None
                or self._queue_card_selected >= len(self._queue_entries)):
            self._queue_card_selected = 0
        # Skip redundant reloads (e.g. Targets → Equip → Targets)
        if self._editing_queue_idx == self._queue_card_selected:
            return

        self._queue_load_target(None)
        # Apply the visual selection highlight — the card may have been
        # rebuilt without highlighting when it was first added from the
        # Planner tab.
        self._queue_card_update_highlight(self._queue_card_selected)

    def _restore_planner_state(self, state):
        """Restore the Planner's search + equipment dropdowns from a backup dict.

        Wrapped in ``_loading_queue_row`` to suppress the Combobox
        ``on_parameter_change`` trace, so restoring values doesn't
        schedule a duplicate analysis (the caller schedules its own)
        and doesn't mutate ``_editing_queue_idx``.
        """
        self._loading_queue_row = True
        try:
            self.target_search.delete(0, tk.END)
            self.target_search.insert(0, state.get("search", ""))
            for attr, key in (
                ("scope_choice",     "scope"),
                ("camera_choice",    "camera"),
                ("bortle_choice",    "bortle"),
                ("filter_mode",      "filter"),
                ("reduction_factor", "reduction"),
            ):
                val = state.get(key, "")
                if val:
                    try:
                        getattr(self, attr).set(val)
                    except Exception:
                        pass
        finally:
            self._loading_queue_row = False


    def _show_toast(self, message, duration_ms=2000):
        """Show a brief non-blocking status message near the top of the window."""
        toast = tk.Toplevel(self.root)
        toast.wm_overrideredirect(True)
        toast.attributes("-topmost", True)

        # Position: centred horizontally, just below the header
        self.root.update_idletasks()
        rx = self.root.winfo_rootx()
        ry = self.root.winfo_rooty()
        rw = self.root.winfo_width()
        tk.Label(toast, text=f"  ✔  {message}  ",
                 font=("Helvetica", 11, "bold"),
                 bg="#1a3a1a", fg="#66dd66",
                 relief="flat", padx=10, pady=6).pack()
        toast.update_idletasks()
        tw = toast.winfo_width()
        tx = rx + (rw - tw) // 2
        ty = ry + 120
        toast.wm_geometry(f"+{tx}+{ty}")
        toast.after(duration_ms, toast.destroy)


    # ═══════════════════════════════════════════════════════════════════
    # QUEUE MANAGEMENT — add, remove, reorder, move-to-plan
    # ═══════════════════════════════════════════════════════════════════

    def _add_to_queue(self):
        """Look up the current target and add it to the queue, computing window data in background."""
        raw = self.target_search.get().strip().upper().replace(" ", "")
        raw = self._normalize_catalog_key(raw)
        if not raw:
            messagebox.showwarning("No Target", "Enter a target name in the search box first.")
            return
        t = self.targets.get(raw) or self.common_names_map.get(raw)
        if not t:
            messagebox.showwarning("Target Not Found",
                f"Could not find '{self.target_search.get().strip()}' in the catalog.")
            return
        # Block exact duplicate (same target AND same scope) — but allow the same
        # target under a different scope so two telescopes can image it simultaneously.
        scope_name_chk = self.scope_choice.get()
        if any(e["target_id"] == t["id"] and e.get("scope") == scope_name_chk
               for e in self._queue_entries):
            messagebox.showinfo(
                "Already Queued",
                f"{t['id']} with scope '{scope_name_chk}' is already in the queue.\n\n"
                "To add the same target for a second telescope, switch to that "
                "scope in the Target Planner and add again.")
            return

        report_text = ""
        if self.current_target_info and self.current_target_info.get("id") == t["id"]:
            report_text = self.results_txt.get("1.0", tk.END).strip()

        scope_name  = self.scope_choice.get()
        cam_name    = self.camera_choice.get()
        bortle_key  = self.bortle_choice.get()
        filter_mode = self.filter_mode.get()
        try:
            reduction = float(self.reduction_factor.get().rstrip("×x"))
        except ValueError:
            reduction = 1.0

        framed_ra, framed_dec = self._framed_center(t["ra_deg"], t["dec_deg"])

        entry = {
            "target_id":      t["id"],
            "common":         t.get("common", "").split(";")[0].strip(),
            "obj_type":       t.get("obj_type", ""),
            "ra_deg":         t["ra_deg"],
            "dec_deg":        t["dec_deg"],
            "v_mag":          t.get("v_mag"),
            "surf_br":        t.get("surf_br"),
            "win_start":      None,
            "win_end":        None,
            "win_hrs":        0.0,
            "start_time":     None,
            "peak_time":      None,
            "peak_alt":       -90.0,
            "transit_time":   None,
            "f_fits":         None,
            "report_text":    report_text,
            "scope":          scope_name,
            "camera":         cam_name,
            "bortle":         bortle_key,
            "filter_mode":    filter_mode,
            "reduction":      reduction,
            "exp_s":          None,
            "computed":       False,
            "rotation_angle": getattr(self, "_fov_angle", 0.0),
            "framed_ra_deg":  framed_ra,
            "framed_dec_deg": framed_dec,
        }
        # ── Short-window / no-window warning ──────────────────────────────
        win_hrs_now = self._last_win_hrs or 0.0  # None means no window tonight
        if win_hrs_now < 0.5:
            if win_hrs_now == 0.0:
                detail = f"{t['id']} has no imaging window tonight.’"
            else:
                win_min = int(round(win_hrs_now * 60))
                detail  = (f"{t['id']} has an imaging window of only {win_min} min "
                           f"tonight — less than 30 minutes.")
            resp = messagebox.askyesno(
                "No Imaging Window" if win_hrs_now == 0.0 else "Short Imaging Window",
                f"{detail}\n\nAdd it to the session queue anyway?",
                icon="warning")
            if not resp:
                return
        self._queue_entries.append(entry)
        self._editing_queue_idx = None   # detach from any previously-clicked entry
        self._refresh_queue_tree()
        self._show_toast(f"{t['id']} added to queue")

        lat, lon = self._get_saved_location()
        b_data = BORTLE_FACTORS.get(bortle_key, BORTLE_FACTORS.get("4 (Rural/Suburban)", {}))

        def _compute():
            s = self.data["scopes"].get(scope_name, {})
            c = self.data["cameras"].get(cam_name, {})
            exp_s_c = f_fits_c = None
            if s and c:
                native_fl = float(s.get("native_fl", 1))
                ap        = float(s.get("aperture", 1))
                eff_fl    = native_fl * reduction
                eff_f     = eff_fl / max(ap, 1)
                ps        = float(c.get("pixel_size", 1))
                sw, sh    = float(c.get("sensor_w", 1)), float(c.get("sensor_h", 1))
                fw = 2 * math.degrees(math.atan(sw / (2 * max(eff_fl, 1))))
                fh = 2 * math.degrees(math.atan(sh / (2 * max(eff_fl, 1))))
                is_color  = c.get("is_color", True)
                rf        = 1.0 if is_color else self._rf_for_filter_mode(filter_mode)
                sky_flux  = (((b_data.get("color" if is_color else "mono", 1.0))
                               * c.get("qe", 0.5) * ps**2) / max(eff_f**2, 1e-6)) / max(rf, 1)
                exp_s_c   = (C_VALUE * c.get("read_noise", 1)**2) / (sky_flux + 1e-5)
                f_fits_c  = (t.get("size_maj", 0) / 60.0 < fw and
                             t.get("size_min", 0) / 60.0 < fh)

            win_start = win_end = peak_t = transit_str = None
            win_hrs = peak_a = 0.0
            if lat is not None and lon is not None:
                try:
                    _min_alt = float(self.data.get("settings", {}).get("min_alt", 20))
                    win_start, win_end, win_hrs, peak_t, peak_a, _ = _calc_best_imaging_window(
                        t["ra_deg"], t["dec_deg"], lat, lon, min_alt=_min_alt)
                    win_hrs = win_hrs or 0.0
                    peak_a  = peak_a  or 0.0
                except Exception:
                    pass
                try:
                    transit_str = _calc_transit_time(t["ra_deg"], lon)
                except Exception:
                    pass

            def _update():
                for e in self._queue_entries:
                    if (e["target_id"] == t["id"] and e.get("scope") == scope_name
                            and not e["computed"]):
                        e.update({
                            "win_start": win_start, "win_end": win_end,
                            "win_hrs": win_hrs, "peak_time": peak_t,
                            "peak_alt": peak_a, "transit_time": transit_str,
                            "f_fits": f_fits_c, "exp_s": exp_s_c, "computed": True,
                        })
                        # Default start_time to win_start if not already set by the user
                        if e.get("start_time") is None:
                            e["start_time"] = win_start
                        # Apply default allocated hours from settings if not already user-set
                        if not e.get("alloc_hrs"):
                            try:
                                default_alloc = float(self.data.get("settings", {}).get(
                                    "default_alloc_hrs", "0"))
                            except (ValueError, TypeError):
                                default_alloc = 0
                            if default_alloc > 0:
                                e["alloc_hrs"] = round(default_alloc, 2)
                            elif win_hrs and win_hrs > 0:
                                e["alloc_hrs"] = round(win_hrs, 2)
                        break
                self._refresh_queue_tree()
                # Note: _refresh_queue_tree() already calls _draw_queue_gantt()
            self.root.after(0, _update)

        threading.Thread(target=_compute, daemon=True).start()

    def _refresh_queue_tree(self):
        """Rebuild the queue card list from self._queue_entries, preserving selection."""
        if not hasattr(self, "_queue_cards_inner"):
            return

        # Preserve selected index
        old_sel = self._queue_card_selected

        # Clear existing cards
        for w in self._queue_cards_inner.winfo_children():
            w.destroy()
        self._queue_card_widgets = []

        TYPE_COLORS = {
            "Nebula": ("#2a3520", "#8bc34a"), "Planetary Nebula": ("#2a3520", "#8bc34a"),
            "Galaxy": ("#1e2a3a", "#64b5f6"), "Open Cluster": ("#2a2035", "#ce93d8"),
            "Globular Cluster": ("#2a2035", "#ce93d8"),
        }

        for i, e in enumerate(self._queue_entries):
            computing = not e["computed"]
            is_selected = (i == old_sel)

            # Card frame
            border_col = "#38bdf8" if is_selected else "#1e2d3e"
            card = tk.Frame(self._queue_cards_inner, bg="#131f2e",
                            highlightthickness=1, highlightbackground=border_col)
            card.pack(fill="x", padx=2, pady=2)

            # Left accent border (via a thin frame inside the card)
            accent_col = "#38bdf8" if is_selected else "#2e4a63"
            tk.Frame(card, bg=accent_col, width=3).pack(side="left", fill="y")

            # Content area
            content = tk.Frame(card, bg="#131f2e")
            content.pack(side="left", fill="both", expand=True, padx=(8, 10), pady=6)

            # Top row: number circle + target info + metrics
            top_row = tk.Frame(content, bg="#131f2e")
            top_row.pack(fill="x")

            # Numbered circle
            circle_bg = "#38bdf8" if is_selected else "#2e4a63"
            circle_fg = "#0e1a28" if is_selected else "#aaddff"
            num_cv = tk.Canvas(top_row, width=28, height=28, bg="#131f2e",
                               highlightthickness=0)
            num_cv.pack(side="left", padx=(0, 8))
            num_cv.create_oval(1, 1, 27, 27, fill=circle_bg, outline="")
            num_cv.create_text(14, 14, text=str(i + 1), fill=circle_fg,
                               font=("Helvetica", 11, "bold"))

            # Target name + common name + type badge
            name_frame = tk.Frame(top_row, bg="#131f2e")
            name_frame.pack(side="left", fill="x", expand=True)

            name_row = tk.Frame(name_frame, bg="#131f2e")
            name_row.pack(anchor="w")
            tk.Label(name_row, text=e["target_id"], bg="#131f2e", fg="#ffffff",
                     font=("Helvetica", 12, "bold")).pack(side="left", padx=(0, 6))
            if e.get("common"):
                tk.Label(name_row, text=e["common"], bg="#131f2e", fg="#7eb8d4",
                         font=("Helvetica", 10)).pack(side="left", padx=(0, 6))
            # Type badge
            obj_type = e.get("obj_type", "Other")
            badge_bg, badge_fg = TYPE_COLORS.get(obj_type, ("#222830", "#778899"))
            tk.Label(name_row, text=obj_type, bg=badge_bg, fg=badge_fg,
                     font=("Helvetica", 8), padx=6, pady=1).pack(side="left")

            # Second line: scope · camera
            if e.get("scope") or e.get("camera"):
                meta_parts = []
                if e.get("scope"):
                    meta_parts.append(e["scope"])
                if e.get("camera"):
                    meta_parts.append(e.get("camera", ""))
                tk.Label(name_frame, text="  ·  ".join(meta_parts), bg="#131f2e",
                         fg="#556677", font=("Helvetica", 9)).pack(anchor="w")

            # Right-side metrics
            metrics = tk.Frame(top_row, bg="#131f2e")
            metrics.pack(side="right")

            # Window time range
            if e.get("win_start") and e.get("win_end"):
                _dur_h = e.get("alloc_hrs") or e.get("win_hrs") or 0
                win_color = "#4caf50" if _dur_h >= 3 else ("#f59e0b" if _dur_h >= 1 else "#ef5350")
                tk.Label(metrics, text=f"{e['win_start']} – {e['win_end']}",
                         bg="#131f2e", fg=win_color,
                         font=("Helvetica", 10, "bold")).grid(row=0, column=0, padx=(0, 12))
                tk.Label(metrics, text=f"{_dur_h:.1f}h" if _dur_h else "—",
                         bg="#131f2e", fg="#556677",
                         font=("Helvetica", 9)).grid(row=1, column=0, padx=(0, 12))
            elif computing:
                tk.Label(metrics, text="computing…", bg="#131f2e", fg="#556677",
                         font=("Helvetica", 9, "italic")).grid(row=0, column=0, padx=(0, 12))
            else:
                tk.Label(metrics, text="—", bg="#131f2e", fg="#334455",
                         font=("Helvetica", 10)).grid(row=0, column=0, padx=(0, 12))

            # Transit
            transit = e.get("transit_time") or ("…" if computing else "—")
            tk.Label(metrics, text=transit, bg="#131f2e", fg="#ffffff",
                     font=("Helvetica", 10)).grid(row=0, column=1, padx=(0, 8))
            tk.Label(metrics, text="transit", bg="#131f2e", fg="#556677",
                     font=("Helvetica", 8)).grid(row=1, column=1, padx=(0, 8))

            # Peak altitude
            if e["computed"] and e.get("peak_alt", -90) > -89:
                peak_val = e["peak_alt"]
                peak_col = "#4caf50" if peak_val >= 40 else ("#f59e0b" if peak_val >= 20 else "#ef5350")
                peak_str = f"{peak_val:.0f}°"
            else:
                peak_col = "#556677"
                peak_str = "…" if computing else "—"
            tk.Label(metrics, text=peak_str, bg="#131f2e", fg=peak_col,
                     font=("Helvetica", 10)).grid(row=0, column=2, padx=(0, 8))
            tk.Label(metrics, text="peak", bg="#131f2e", fg="#556677",
                     font=("Helvetica", 8)).grid(row=1, column=2, padx=(0, 8))

            # Status dot
            status_col = "#4caf50"
            if e.get("f_fits") is False:
                status_col = "#ef5350"
            elif not e["computed"]:
                status_col = "#556677"
            elif e.get("peak_alt", 0) < 20:
                status_col = "#f59e0b"
            dot_cv = tk.Canvas(metrics, width=10, height=10, bg="#131f2e",
                               highlightthickness=0)
            dot_cv.grid(row=0, column=3, rowspan=2, padx=(0, 0), pady=4)
            dot_cv.create_oval(1, 1, 9, 9, fill=status_col, outline="")

            # Click binding — all widgets in card trigger selection
            self._queue_card_widgets.append(card)
            def _bind_click(widget, idx=i):
                widget.bind("<Button-1>", lambda e, ii=idx: self._queue_card_click(ii))
            _bind_click(card)
            _bind_click(content)
            _bind_click(top_row)
            _bind_click(name_frame)
            _bind_click(name_row)
            _bind_click(metrics)
            _bind_click(num_cv)
            for child in name_row.winfo_children():
                _bind_click(child)
            for child in metrics.winfo_children():
                _bind_click(child)

            # Mousewheel bindings — must be applied to every descendant,
            # otherwise the wheel stops working once cards cover the canvas.
            self._bind_queue_mousewheel_recursive(card)

        # Restore selection
        if old_sel is not None and old_sel < len(self._queue_entries):
            self._queue_card_selected = old_sel
        elif self._queue_entries:
            self._queue_card_selected = 0
        else:
            self._queue_card_selected = None

        self._draw_queue_gantt()

        # Same as _refresh_plan_tree: re-theme newly created widgets when in
        # night mode so they don't flash with day-mode white text.
        if self.night_mode:
            self._apply_night_mode()

    def _queue_card_click(self, idx):
        """Handle a click on a queue card — select it and load its target."""
        # User is committing to this queue entry — clear the Planner backup
        # so returning to the Planner after this click pivots to the clicked
        # target rather than restoring whatever search was active before
        # this Targets-tab visit.
        self._planner_state_backup = None

        old = self._queue_card_selected
        self._queue_card_selected = idx

        # Update visual state of old and new cards
        self._queue_card_update_highlight(old)
        self._queue_card_update_highlight(idx)

        # Trigger load
        self._queue_load_target(None)

    def _queue_card_update_highlight(self, idx):
        """Update the visual highlight state of a single queue card."""
        if idx is None or idx >= len(self._queue_card_widgets):
            return
        card = self._queue_card_widgets[idx]
        is_selected = (idx == self._queue_card_selected)

        border_col = "#38bdf8" if is_selected else "#1e2d3e"
        accent_col = "#38bdf8" if is_selected else "#2e4a63"
        circle_bg = "#38bdf8" if is_selected else "#2e4a63"
        circle_fg = "#0e1a28" if is_selected else "#aaddff"

        try:
            card.configure(highlightbackground=border_col)
            # Update accent line (first child)
            children = card.winfo_children()
            if children:
                children[0].configure(bg=accent_col)
            # Update number circle (find the Canvas in content > top_row)
            content = children[1] if len(children) > 1 else None
            if content:
                top_row = content.winfo_children()[0] if content.winfo_children() else None
                if top_row:
                    for w in top_row.winfo_children():
                        if isinstance(w, tk.Canvas) and w.winfo_width() <= 30:
                            w.delete("all")
                            w.create_oval(1, 1, 27, 27, fill=circle_bg, outline="")
                            w.create_text(14, 14, text=str(idx + 1), fill=circle_fg,
                                          font=("Helvetica", 11, "bold"))
                            break
        except Exception:
            pass

    def _parse_h(self, s):
        """Parse 'HH:MM' string to fractional hours float, or None on failure."""
        if not s:
            return None
        try:
            parts = s.split(":")
            return float(parts[0]) + float(parts[1]) / 60.0
        except Exception:
            return None

    # ═══════════════════════════════════════════════════════════════════
    # GANTT TIMELINE — schedule visualisation with draggable bars
    # ═══════════════════════════════════════════════════════════════════

    def _gantt_show_note(self, msg):
        """Show a temporary notification below the Gantt chart."""
        if hasattr(self, "_gantt_note_lbl"):
            self._gantt_note_lbl.config(text=msg)
        if hasattr(self, "_gantt_note_after_id"):
            self.root.after_cancel(self._gantt_note_after_id)
        self._gantt_note_after_id = self.root.after(10000, self._gantt_clear_note)

    def _gantt_clear_note(self):
        """Clear the Gantt notification label."""
        if hasattr(self, "_gantt_note_lbl"):
            self._gantt_note_lbl.config(text="")

    def _draw_queue_gantt(self):
        """Draw the schedule timeline on the Targets List tab.

        When Tonight's Plan has entries, shows those as draggable bars with
        altitude curves so start times can be set by dragging.
        Falls back to showing queue imaging-window bars when the plan is empty.
        """
        if not hasattr(self, "queue_gantt"):
            return
        canvas = self.queue_gantt
        canvas.update_idletasks()
        cw = canvas.winfo_width()
        ch = canvas.winfo_height() or 200

        lm, rm = 74, 12          # left/right margins — needed for the pw guard below
        pw = cw - lm - rm

        # Canvas hasn't been laid out yet (returns 1 before the window is first drawn).
        # Defer without touching any saved axis state so drag maths stays valid.
        if pw <= 20:
            self.root.after(80, self._draw_queue_gantt)
            return

        canvas.delete("all")

        nm       = self.night_mode
        fg_main  = "#cc0000" if nm else "#ffffff"    # matches altitude chart fg_main
        fg_dim   = "#880000" if nm else "#aabbcc"    # matches altitude chart fg_dim
        fg_now   = "#aa0000" if nm else "#ccaa00"    # matches altitude chart fg_label

        # ── Mode: always queue entries ────────────────────────────────────
        if not self._queue_entries:
            canvas.create_text(cw // 2, ch // 2,
                text="Add targets to the queue to see the imaging window timeline",
                fill=fg_dim, font=("Helvetica", 11))
            return

        lat, lon = self._get_saved_location()
        if lat is None:
            canvas.create_text(cw // 2, ch // 2,
                text="Save a location to see the schedule timeline",
                fill=fg_dim, font=("Helvetica", 11))
            return

        # ── Find tonight's nautical dark window ──────────────────────────
        real_now      = datetime.now()   # kept for the "Now" marker below
        utc_offset_h  = _utc_offset_hours()
        jd_noon       = _jd_local_noon()

        dark_start_h = dark_end_h = None
        been_dark = False
        for i in range(361):
            jd = jd_noon + i * 5.0 / 1440.0
            s_ra, s_dec = _sun_ra_dec(jd)
            if _ra_dec_to_altaz(s_ra, s_dec, lat, lon, jd) < -12.0:
                been_dark = True
                lh = (((jd + 0.5) % 1.0) * 24.0 + utc_offset_h) % 24.0
                if dark_start_h is None:
                    dark_start_h = lh
                dark_end_h = lh
            elif been_dark:
                break

        if dark_start_h is None:
            canvas.create_text(cw // 2, ch // 2,
                text="No dark window tonight at this location",
                fill=fg_dim, font=("Helvetica", 9))
            return

        if dark_end_h < dark_start_h:
            dark_end_h += 24.0

        ax_start = dark_start_h - 0.33
        ax_end   = dark_end_h   + 0.33
        ax_range = max(ax_end - ax_start, 0.1)

        tm, bm = 14, 22
        ph = ch - tm - bm

        # Save axis params for drag calculations
        self._gantt_ax_start = ax_start
        self._gantt_ax_range = ax_range
        self._gantt_lm       = lm
        self._gantt_pw       = pw

        def h_to_x(h):
            h_adj = h if h >= ax_start - 0.01 else h + 24.0
            return lm + (h_adj - ax_start) / ax_range * pw

        # Dark-band tint
        canvas.create_rectangle(h_to_x(dark_start_h), tm,
                                 h_to_x(dark_end_h), tm + ph,
                                 fill="#080e1a", outline="")

        # Hour grid + axis labels
        h = math.ceil(ax_start)
        while h <= ax_end + 0.5:
            x = h_to_x(h)
            if lm - 1 <= x <= cw - rm + 1:
                canvas.create_line(x, tm, x, tm + ph, fill=fg_dim, dash=(2, 4))
                canvas.create_text(x, ch - bm + 10,
                                   text=f"{int(h) % 24:02d}:00",
                                   fill=fg_main, font=("Helvetica", 10, "bold"), anchor="n")
            h += 1
        canvas.create_line(lm, tm + ph, cw - rm, tm + ph, fill=fg_dim)

        # ── Draw rows (always queue entries) ──────────────────────────────
        COLORS  = ["#1D9E75", "#378ADD", "#7F77DD", "#D85A30",
                   "#BA7517", "#D4537E", "#639922"]
        MAX_ALT = 90.0

        def _dim(hex_col, factor=0.25):
            """Mix hex_col toward the canvas bg (#050810) — avoids alpha hex."""
            r = int(int(hex_col[1:3], 16) * factor + 0x05 * (1 - factor))
            g = int(int(hex_col[3:5], 16) * factor + 0x08 * (1 - factor))
            b = int(int(hex_col[5:7], 16) * factor + 0x10 * (1 - factor))
            return f"#{r:02x}{g:02x}{b:02x}"

        entries  = self._queue_entries
        max_rows = min(len(entries), 8)
        row_h    = ph / max(max_rows, 1)
        self._gantt_row_info   = []   # (y_top, y_bot, entry_idx, alloc_x1, alloc_x2, start_h_norm)
        self._gantt_drag_bounds = {}

        for i, e in enumerate(entries[:max_rows]):
            color    = COLORS[i % len(COLORS)]
            dim_col  = _dim(color)
            y_top    = tm + i * row_h + 1
            y_bot    = tm + (i + 1) * row_h - 1
            y_mid    = (y_top + y_bot) / 2
            alt_base = y_bot - 2
            alt_span = row_h * 0.82
            is_drag  = (i == self._gantt_drag_idx)

            # Target label — always include scope so users can tell apart entries at a glance.
            scope_abbr = (e.get("scope") or "")
            if scope_abbr:
                row_label  = f"{e['target_id']}\n{scope_abbr[:12]}"
                label_font = ("Helvetica", 8, "bold")
            else:
                row_label  = e["target_id"]
                label_font = ("Helvetica", 10, "bold")
            canvas.create_text(lm - 4, y_mid, text=row_label,
                               fill=fg_main, font=label_font, anchor="e")

            if not e["computed"] or not e.get("win_start"):
                canvas.create_rectangle(lm, y_top, cw - rm, y_bot,
                                        fill="#111820", outline=fg_dim, dash=(3, 3))
                msg = "computing…" if not e["computed"] else "not visible tonight"
                canvas.create_text((lm + cw - rm) // 2, y_mid,
                                   text=msg, fill=fg_dim, font=("Helvetica", 9))
                self._gantt_row_info.append((y_top, y_bot, i, lm, lm, dark_start_h))
                continue

            ws = self._parse_h(e.get("win_start"))
            we = self._parse_h(e.get("win_end"))
            if ws is None or we is None:
                canvas.create_text((lm + cw - rm) // 2, y_mid,
                                   text="not visible tonight", fill=fg_dim, font=("Helvetica", 9))
                self._gantt_row_info.append((y_top, y_bot, i, lm, lm, dark_start_h))
                continue
            # Align ws/we to the plotting timeline: post-midnight times (e.g. 01:00)
            # are numerically less than ax_start (~21h) and need +24 so that
            # comparisons with start_h and dark_end_h work correctly.
            if ws < ax_start - 0.01:
                ws += 24.0
            if we < ws:
                we += 24.0

            # ── Background: full available-window bar (faint) ─────────────
            x_ws = max(lm,      h_to_x(ws))
            x_we = min(cw - rm, h_to_x(we))
            if x_we > x_ws:
                canvas.create_rectangle(x_ws, y_top + 3, x_we, y_bot - 3,
                                        fill=dim_col, outline="")

            # ── Altitude curve over the whole available window ─────────────
            ra_deg  = e.get("ra_deg", 0)
            dec_deg = e.get("dec_deg", 0)
            N_ALT   = 50
            alt_pts = []
            for k in range(N_ALT + 1):
                jd_k = jd_noon + k * 30.0 / N_ALT / 24.0
                lh_k = (((jd_k + 0.5) % 1.0) * 24.0 + utc_offset_h) % 24.0
                alt_k = _ra_dec_to_altaz(ra_deg, dec_deg, lat, lon, jd_k)
                alt_pts.append((lh_k, alt_k))

            seg = []
            for lh_k, alt_k in alt_pts:
                x = h_to_x(lh_k)
                if x < lm or x > cw - rm:
                    if len(seg) > 1:
                        canvas.create_line(*[c for pt in seg for c in pt],
                                           fill=color, width=1, dash=(3, 3))
                    seg = []
                    continue
                y = alt_base - max(0.0, min(alt_k, MAX_ALT)) / MAX_ALT * alt_span
                seg.append((x, y))
            if len(seg) > 1:
                canvas.create_line(*[c for pt in seg for c in pt],
                                   fill=color, width=1, dash=(3, 3))

            # ── Foreground: draggable allocated bar ────────────────────────
            start_str = e.get("start_time") or e.get("win_start") or ""
            start_h   = self._parse_h(start_str)
            if start_h is None:
                start_h = ws if ws is not None else dark_start_h
            if start_h < ax_start - 0.01:
                start_h += 24.0

            alloc_h = e.get("alloc_hrs") or e.get("win_hrs") or 1.0

            # Clip bar so it never extends past the imaging window end (we).
            # If the bar would overflow, reduce alloc_hrs and notify the user.
            # If less than 30 min remain, show a message and skip the bar.
            effective_h = min(start_h + alloc_h, we) - start_h
            if effective_h < 0.5:
                canvas.create_text((lm + cw - rm) // 2, y_mid,
                    text=f"{e['target_id']}: less than 30 min in imaging window",
                    fill=fg_dim, font=("Helvetica", 9))
                self._gantt_row_info.append((y_top, y_bot, i, lm, lm, dark_start_h))
                self._gantt_drag_bounds[i] = (ws, we)
                continue
            if effective_h < alloc_h:
                e["alloc_hrs"] = round(effective_h, 2)
                alloc_h        = effective_h
                self.root.after(0, lambda tid=e["target_id"], ah=round(effective_h, 2):
                    self._gantt_show_note(
                        f"⚠  {tid}: allocated hours reduced to {ah:.2f}h"
                        f" to fit the imaging window"))

            end_h  = start_h + alloc_h
            x_al_s = max(lm,      h_to_x(start_h))
            x_al_e = min(cw - rm, h_to_x(end_h))

            # Store drag limits: bar must stay within [ws, we]
            self._gantt_drag_bounds[i] = (ws, we)

            self._gantt_row_info.append(
                (y_top, y_bot, i, x_al_s, x_al_e, start_h)
            )

            if x_al_e > x_al_s:
                outline_col = "#ffffff" if is_drag else ""
                outline_w   = 2        if is_drag else 0
                canvas.create_rectangle(x_al_s, y_top + 2, x_al_e, y_bot - 2,
                                        fill=color, outline=outline_col, width=outline_w)

                # Altitude overlay inside alloc bar (bright solid line)
                bright = []
                for lh_k, alt_k in alt_pts:
                    x = h_to_x(lh_k)
                    if x < x_al_s or x > x_al_e:
                        if len(bright) > 1:
                            canvas.create_line(*[c for pt in bright for c in pt],
                                               fill="#ffffff", width=1)
                        bright = []
                        continue
                    y = alt_base - max(0.0, min(alt_k, MAX_ALT)) / MAX_ALT * alt_span
                    bright.append((x, y))
                if len(bright) > 1:
                    canvas.create_line(*[c for pt in bright for c in pt],
                                       fill="#ffffff", width=1)

                # Target label inside bar — bold 9pt
                # When the same target is queued twice (two telescopes), include
                # a short scope name so bars are distinguishable at a glance.
                if x_al_e - x_al_s > 50:
                    scope_s = (e.get("scope") or "")[:8]
                    bar_label = (f"{e['target_id']} ({scope_s})" if scope_s
                                 else e["target_id"])
                    canvas.create_text((x_al_s + x_al_e) / 2, y_mid,
                                       text=bar_label, fill="#ffffff",
                                       font=("Helvetica", 9, "bold"))

                # Start-time stamp top-left of bar — 9pt dim to match planner "Now" label
                disp_h = int(start_h) % 24
                disp_m = int(round((start_h % 1) * 60)) % 60
                canvas.create_text(x_al_s + 4, y_top + 3,
                                   text=f"{disp_h:02d}:{disp_m:02d}",
                                   fill=fg_dim, font=("Helvetica", 9, "bold"), anchor="nw")

                # Grip tick (left edge of alloc bar)
                canvas.create_line(x_al_s, y_top + 3, x_al_s, y_bot - 3,
                                   fill=fg_main, width=2)

            # Transit tick inside available-window bar
            tr = self._parse_h(e.get("transit_time"))
            if tr is not None:
                if tr < ax_start - 0.01:
                    tr += 24.0
                x_tr = h_to_x(tr)
                if lm <= x_tr <= cw - rm:
                    canvas.create_line(x_tr, y_top, x_tr, y_bot,
                                       fill="#ffffff", width=1, dash=(2, 2))

        # ── "Now" line (only shown when viewing today's date) ────────────
        if _PLANNING_DATE is None:
            now_h = real_now.hour + real_now.minute / 60.0 + real_now.second / 3600.0
            if now_h < ax_start - 0.01:
                now_h += 24.0
            if ax_start <= now_h <= ax_end + 0.25:
                x_now = h_to_x(now_h)
                canvas.create_line(x_now, tm, x_now, tm + ph,
                                   fill=fg_now, width=1, dash=(4, 2))
                canvas.create_text(x_now, tm - 3, text="Now",
                                   fill=fg_dim, font=("Helvetica", 9, "bold"), anchor="s")

    # ── Gantt drag interactions ───────────────────────────────────────────────

    def _gantt_hit(self, x, y):
        """Return (entry_idx, start_h_norm) if (x,y) hits a draggable alloc bar."""
        for y_top, y_bot, entry_idx, bar_x1, bar_x2, start_h_norm in self._gantt_row_info:
            if y_top <= y <= y_bot and bar_x1 - 12 <= x <= bar_x2 + 12:
                return entry_idx, start_h_norm
        return None, None

    def _gantt_press(self, event):
        """Begin a Gantt-bar drag if the mouse hit a draggable bar."""
        idx, start_h = self._gantt_hit(event.x, event.y)
        if idx is not None:
            self._gantt_drag_idx      = idx
            self._gantt_drag_x0       = event.x
            self._gantt_drag_start_h0 = start_h

    def _gantt_drag(self, event):
        """Update the plan entry's start_time while the user drags a Gantt bar."""
        if self._gantt_drag_idx is None or not self._gantt_pw:
            return
        dx    = event.x - self._gantt_drag_x0
        dh    = dx / self._gantt_pw * self._gantt_ax_range
        new_h = self._gantt_drag_start_h0 + dh

        idx = self._gantt_drag_idx
        if idx >= len(self._queue_entries):
            return
        e = self._queue_entries[idx]

        # Clamp within the available window
        ws = self._parse_h(e.get("win_start"))
        we = self._parse_h(e.get("win_end"))
        if ws is not None and ws < self._gantt_ax_start - 0.01:
            ws += 24.0
        if we is not None and we < ws:
            we += 24.0
        alloc_h = e.get("alloc_hrs") or e.get("win_hrs") or 1.0
        # Use stored per-row bounds so drag respects the imaging window
        bounds = getattr(self, "_gantt_drag_bounds", {}).get(idx)
        if bounds:
            b_lo, b_hi = bounds
        else:
            b_lo = ws if ws is not None else self._gantt_ax_start
            b_hi = we if we is not None else (self._gantt_ax_start + self._gantt_ax_range)
        lo    = b_lo
        hi    = b_hi - alloc_h
        new_h = max(lo, min(hi, new_h))

        h_int = int(new_h) % 24
        m_int = int(round((new_h % 1) * 60)) % 60
        e["start_time"] = f"{h_int:02d}:{m_int:02d}"
        self._draw_queue_gantt()

    def _gantt_release(self, event):
        """End the Gantt-bar drag and clear drag state."""
        if self._gantt_drag_idx is not None:
            self._gantt_drag_idx      = None
            self._gantt_drag_x0       = None
            self._gantt_drag_start_h0 = None

    def _gantt_motion(self, event):
        """Show a move cursor when hovering over a draggable Gantt bar."""
        idx, _ = self._gantt_hit(event.x, event.y)
        self.queue_gantt.config(cursor="fleur" if idx is not None else "")

    def _queue_selected_index(self):
        """Return the index of the selected queue card, or None."""
        idx = getattr(self, "_queue_card_selected", None)
        if idx is not None and 0 <= idx < len(self._queue_entries):
            return idx
        return None

    def _queue_move_up(self):
        """Swap the selected queue entry one position earlier."""
        idx = self._queue_selected_index()
        if idx is None or idx == 0:
            return
        self._queue_entries[idx - 1], self._queue_entries[idx] = \
            self._queue_entries[idx], self._queue_entries[idx - 1]
        self._queue_card_selected = idx - 1
        self._refresh_queue_tree()

    def _queue_move_down(self):
        """Swap the selected queue entry one position later."""
        idx = self._queue_selected_index()
        if idx is None or idx >= len(self._queue_entries) - 1:
            return
        self._queue_entries[idx], self._queue_entries[idx + 1] = \
            self._queue_entries[idx + 1], self._queue_entries[idx]
        self._queue_card_selected = idx + 1
        self._refresh_queue_tree()

    def _queue_remove(self):
        """Remove the selected entry from the queue."""
        idx = self._queue_selected_index()
        if idx is None:
            return
        del self._queue_entries[idx]
        # Adjust selection
        if self._queue_entries:
            self._queue_card_selected = min(idx, len(self._queue_entries) - 1)
        else:
            self._queue_card_selected = None
        self._refresh_queue_tree()

    def _queue_clear(self):
        """Clear all entries from the queue after confirmation."""
        if not self._queue_entries:
            return
        if messagebox.askyesno("Clear Queue", "Remove all targets from the queue?"):
            self._queue_entries.clear()
            self._queue_card_selected = None
            self._refresh_queue_tree()

    def _queue_suggest_order(self):
        """Sort the queue by each target's transit time (earliest tonight first)."""
        if not self._queue_entries:
            return

        def _sort_key(e):
            t_str = e.get("transit_time")
            if not t_str:
                return 99.0
            try:
                p = t_str.split(":")
                h = float(p[0]) + float(p[1]) / 60.0
                # Treat pre-noon times as next-day (add 24) so sort is chronological from evening
                if h < 12.0:
                    h += 24.0
                return h
            except Exception:
                return 99.0

        self._queue_entries.sort(key=_sort_key)
        self._refresh_queue_tree()

    def _queue_move_all_to_plan(self):
        """Move all computed queue entries into Tonight's Plan."""
        if not self._queue_entries:
            messagebox.showwarning("Empty Queue", "The target queue is empty.")
            return
        not_ready = [e for e in self._queue_entries if not e["computed"]]
        if not_ready:
            resp = messagebox.askyesno(
                "Some Targets Still Computing",
                f"{len(not_ready)} target(s) are still computing.\n"
                "Move only the ready targets now?")
            if not resp:
                return

        # ── No/short-window check for all entries ──────────────────────
        short = [e for e in self._queue_entries
                 if e["computed"] and (e.get("win_hrs") or 0.0) < 0.5]
        if short:
            names    = ", ".join(e["target_id"] for e in short)
            def _win_desc(e):
                wh = e.get("win_hrs") or 0.0
                return f"  • {e['target_id']}: no window" if wh == 0.0 \
                       else f"  • {e['target_id']}: {int(round(wh*60))} min"
            win_strs = "\n".join(_win_desc(e) for e in short)
            resp = messagebox.askyesnocancel(
                "No or Short Imaging Windows",
                f"The following target(s) have no imaging window or under 30 minutes:\n\n"
                f"{win_strs}\n\n"
                f"Yes — add all targets (including short-window ones)\n"
                f"No — skip short-window targets, add the rest\n"
                f"Cancel — cancel the operation",
                icon="warning")
            if resp is None:   # Cancel
                return
            skip_short = (resp is False)  # No = skip those entries
        else:
            skip_short = False

        added = 0
        for e in self._queue_entries:
            if not e["computed"] or e["exp_s"] is None:
                continue
            if skip_short and (e.get("win_hrs") or 0.0) < 0.5:
                continue
            t = (self.targets.get(e["target_id"].replace(" ", "")) or
                 self.common_names_map.get(e["target_id"].replace(" ", "")))
            if not t:
                continue

            cam      = self.data["cameras"].get(e["camera"], {})
            is_color = cam.get("is_color", True)
            fm       = e.get("filter_mode", "Mono Lum")
            nf       = self.data.get("nina_filters", {})
            if is_color:
                filters = [""]
            elif fm == "LRGB":
                filters = nf.get("lrgb") or ["L", "R", "G", "B"]
            elif "NB" in fm or "Narrowband" in fm:
                filters = nf.get("narrowband") or ["Ha", "O3", "S2"]
            else:
                filters = [""]

            n_filters      = len(filters)
            # Prefer user-set alloc_hrs over the computed window length
            alloc_hrs      = e.get("alloc_hrs") or e.get("win_hrs") or self._get_default_alloc_hrs()
            total_subs_all = int(alloc_hrs * 3600.0 / e["exp_s"])
            subs_per_filt  = total_subs_all // max(n_filters, 1)
            int_per_filt_h = round((subs_per_filt * e["exp_s"]) / 3600.0, 2)

            for filt_name in filters:
                filt_label = filt_name if n_filters > 1 else (fm if not is_color else "")
                self._plan_entries.append({
                    "target_id":     e["target_id"],
                    "common":        e["common"],
                    "ra_deg":        e.get("ra_deg", 0.0),
                    "dec_deg":       e.get("dec_deg", 0.0),
                    "scope":         e["scope"],
                    "camera":        e["camera"],
                    "filter":        filt_label,
                    "bortle":        e["bortle"],
                    "exp_s":         round(e["exp_s"], 1),
                    "win_start":     e.get("win_start") or "",
                    "win_end":       e.get("win_end") or "",
                    "start_time":    e.get("start_time") or e.get("win_start") or "",
                    "allocated_hrs": round(alloc_hrs / max(n_filters, 1), 2),
                    "n_subs":        subs_per_filt,
                    "total_int_hrs": int_per_filt_h,
                    "added":         datetime.now().strftime("%Y-%m-%d %H:%M"),
                    "report_text":   e.get("report_text", ""),
                    "rotation_angle": e.get("rotation_angle", 0.0),
                    "framed_ra_deg":  e.get("framed_ra_deg"),
                    "framed_dec_deg": e.get("framed_dec_deg"),
                })
            added += 1

        if added:
            self._refresh_plan_tree()
            messagebox.showinfo("Moved to Plan",
                f"{added} target(s) added to Tonight's Plan.")
        else:
            messagebox.showwarning("Nothing Added",
                "No computed targets were found to add.")

    def _queue_move_selected_to_plan(self):
        """Move the currently selected queue entry to Tonight's Plan."""
        idx = self._queue_selected_index()
        if idx is None:
            messagebox.showwarning("No Selection", "Select a target in the queue first.")
            return
        e = self._queue_entries[idx]
        if not e["computed"]:
            messagebox.showwarning("Still Computing",
                f"{e['target_id']} is still computing its imaging window. Please wait a moment.")
            return
        if e["exp_s"] is None:
            messagebox.showwarning("Incomplete",
                f"{e['target_id']} could not be computed (missing scope/camera data).")
            return

        # ── Short/no-window warning ──────────────────────────────────────
        win_hrs_e = e.get("win_hrs") or 0.0
        if win_hrs_e < 0.5:
            if win_hrs_e == 0.0:
                detail = f"{e['target_id']} has no imaging window tonight."
                title  = "No Imaging Window"
            else:
                win_min = int(round(win_hrs_e * 60))
                detail  = (f"{e['target_id']} has an imaging window of only "
                           f"{win_min} min tonight — less than 30 minutes.")
                title   = "Short Imaging Window"
            resp = messagebox.askyesnocancel(
                title,
                f"{detail}\n\n"
                f"Yes — add to Tonight's Plan anyway\n"
                f"No — skip this target\n"
                f"Cancel — cancel the operation",
                icon="warning")
            if resp is None:   # Cancel
                return
            if resp is False:  # No — skip
                return
            # Yes — fall through and add

        t = (self.targets.get(e["target_id"].replace(" ", "")) or
             self.common_names_map.get(e["target_id"].replace(" ", "")))
        if not t:
            messagebox.showwarning("Target Not Found",
                f"Could not find '{e['target_id']}' in the catalog.")
            return

        cam      = self.data["cameras"].get(e["camera"], {})
        is_color = cam.get("is_color", True)
        fm       = e.get("filter_mode", "Mono Lum")
        nf       = self.data.get("nina_filters", {})
        if is_color:
            filters = [""]
        elif fm == "LRGB":
            filters = nf.get("lrgb") or ["L", "R", "G", "B"]
        elif "NB" in fm or "Narrowband" in fm:
            filters = nf.get("narrowband") or ["Ha", "O3", "S2"]
        else:
            filters = [""]

        n_filters      = len(filters)
        alloc_hrs      = e.get("alloc_hrs") or e.get("win_hrs") or self._get_default_alloc_hrs()
        total_subs_all = int(alloc_hrs * 3600.0 / e["exp_s"])
        subs_per_filt  = total_subs_all // max(n_filters, 1)
        int_per_filt_h = round((subs_per_filt * e["exp_s"]) / 3600.0, 2)

        for filt_name in filters:
            filt_label = filt_name if n_filters > 1 else (fm if not is_color else "")
            self._plan_entries.append({
                "target_id":     e["target_id"],
                "common":        e["common"],
                "ra_deg":        e.get("ra_deg", 0.0),
                "dec_deg":       e.get("dec_deg", 0.0),
                "scope":         e["scope"],
                "camera":        e["camera"],
                "filter":        filt_label,
                "bortle":        e["bortle"],
                "exp_s":         round(e["exp_s"], 1),
                "win_start":     e.get("win_start") or "",
                "win_end":       e.get("win_end") or "",
                "start_time":    e.get("start_time") or e.get("win_start") or "",
                "allocated_hrs": round(alloc_hrs / max(n_filters, 1), 2),
                "n_subs":        subs_per_filt,
                "total_int_hrs": int_per_filt_h,
                "added":         datetime.now().strftime("%Y-%m-%d %H:%M"),
                "report_text":   e.get("report_text", ""),
                "rotation_angle": e.get("rotation_angle", 0.0),
                "framed_ra_deg":  e.get("framed_ra_deg"),
                "framed_dec_deg": e.get("framed_dec_deg"),
            })

        self._refresh_plan_tree()
        self._show_toast(f"{e['target_id']} added to Tonight's Plan")

    def _queue_load_target(self, event):
        """Click on a queue card: populate Current Target Analysis fields for inline editing."""
        idx = getattr(self, "_queue_card_selected", None)
        if idx is None or idx < 0 or idx >= len(self._queue_entries):
            return
        e = self._queue_entries[idx]

        # Remember which entry we're editing so the commit button can write back
        self._editing_queue_idx = idx

        # Populate fields — guard flag prevents the trace callbacks on session_hours_var
        # and manual_exp_var from calling _commit_queue_edit with stale values mid-load.
        # Also prevents on_parameter_change from firing analyze_framing for each dropdown.
        self._loading_queue_row = True
        try:
            hrs = e.get("alloc_hrs") or e.get("win_hrs") or self._get_default_alloc_hrs()
            self.session_hours_var.set(f"{hrs:.1f}")

            if e.get("exp_s"):
                self.manual_exp_var.set(f"{e['exp_s']:.1f}")
            else:
                self.manual_exp_var.set("")

            # Restore the equipment dropdowns to match this entry's stored settings.
            # This ensures analyze_framing runs with the correct scope/camera/filter,
            # not whatever happens to be selected in the Planner dropdowns.
            if e.get("scope") and e["scope"] in self.scope_dropdown.cget("values"):
                self.scope_choice.set(e["scope"])
            if e.get("camera") and e["camera"] in self.camera_dropdown.cget("values"):
                self.camera_choice.set(e["camera"])
            if e.get("bortle") and e["bortle"] in self.bortle_dropdown.cget("values"):
                self.bortle_choice.set(e["bortle"])
            if e.get("filter_mode") and e["filter_mode"] in self.filter_dropdown.cget("values"):
                self.filter_mode.set(e["filter_mode"])
            if e.get("reduction") is not None:
                red_str = e["reduction"]
                # Stored as float — convert back to "1.0×" format
                if isinstance(red_str, (int, float)):
                    red_str = f"{red_str:.2f}".rstrip('0').rstrip('.') + "×"
                if red_str in self.reduction_entry.cget("values"):
                    self.reduction_factor.set(red_str)
        finally:
            self._loading_queue_row = False

        # Enable the analysis fields for editing
        self._session_entry.configure(state="normal")
        self._manual_exp_entry.configure(state="normal")

        # Load target into search and trigger analysis so stat cards update
        self.target_search.delete(0, tk.END)
        self.target_search.insert(0, e["target_id"])
        # Only switch tabs if not already on the Session tab
        try:
            if self.tab_control.select() != str(self.tab_session):
                self.tab_control.select(self.tab_session)
        except Exception:
            pass

        self._mark_analysis_dirty()


    def _commit_queue_edit(self, silent=False):
        """Write the current allocated hours and sub-exposure back to the active queue entry."""
        if self._loading_queue_row:
            return  # fields are mid-population — don't commit stale values
        # Only commit when actually on the Session tab — prevents equipment changes
        # on the Planner tab from overwriting a queue entry's stored values.
        try:
            if self.tab_control.select() != str(self.tab_session):
                return
        except Exception:
            pass
        idx = self._editing_queue_idx
        if idx is None or idx >= len(self._queue_entries):
            return
        e = self._queue_entries[idx]

        try:
            hrs = float(self.session_hours_var.get())
            if hrs <= 0:
                raise ValueError
        except ValueError:
            if not silent:
                messagebox.showerror("Invalid", "Allocated hours must be a positive number.")
            return

        manual = self.manual_exp_var.get().strip()
        if manual:
            try:
                exp = float(manual)
                if exp <= 0:
                    raise ValueError
            except ValueError:
                if not silent:
                    messagebox.showerror("Invalid", "Sub-exposure must be a positive number.")
                return
            e["exp_s"] = round(exp, 1)
        elif self._last_exp_s:
            e["exp_s"] = round(self._last_exp_s, 1)

        e["alloc_hrs"] = round(hrs, 2)

        self._refresh_queue_tree()
        if not silent:
            self._show_toast(f"{e['target_id']} queue entry updated")


    def _plan_load_target(self, event):
        """Double-click in Tonight's Plan: edit allocated hours and/or sub-exposure for that row."""
        idx = self._plan_card_selected
        if idx is None or idx < 0 or idx >= len(self._plan_entries):
            return

        entry_idx = idx
        e = self._plan_entries[entry_idx]

        dlg = tk.Toplevel(self.root)
        dlg.title(f"Edit — {e['target_id']}")
        dlg.resizable(False, False)
        dlg.grab_set()

        ttk.Label(dlg, text=f"{e['target_id']}",
                  font=("Helvetica", 12, "bold")).grid(row=0, column=0, columnspan=2,
                  padx=16, pady=(14, 2), sticky="w")
        if e.get("common"):
            ttk.Label(dlg, text=e["common"],
                      font=("Helvetica", 9), foreground="#aaaaaa").grid(
                      row=1, column=0, columnspan=2, padx=16, pady=(0, 10), sticky="w")

        ttk.Label(dlg, text="Allocated hours:").grid(row=2, column=0, padx=(16, 8), pady=6, sticky="e")
        hrs_var = tk.StringVar(value=f"{e['allocated_hrs']:.2f}")
        hrs_entry = ttk.Entry(dlg, textvariable=hrs_var, width=10)
        hrs_entry.grid(row=2, column=1, padx=(0, 16), pady=6, sticky="w")
        hrs_entry.focus_set()

        ttk.Label(dlg, text="Sub-exposure (s):").grid(row=3, column=0, padx=(16, 8), pady=6, sticky="e")
        exp_var = tk.StringVar(value=f"{e['exp_s']:.1f}")
        exp_entry = ttk.Entry(dlg, textvariable=exp_var, width=10)
        exp_entry.grid(row=3, column=1, padx=(0, 16), pady=6, sticky="w")

        ttk.Label(dlg, text="Start time (HH:MM):").grid(row=4, column=0, padx=(16, 8), pady=6, sticky="e")
        st_default = e.get("start_time") or e.get("win_start") or ""
        st_var = tk.StringVar(value=st_default)
        st_entry = ttk.Entry(dlg, textvariable=st_var, width=10)
        st_entry.grid(row=4, column=1, padx=(0, 16), pady=6, sticky="w")
        ToolTip(st_entry, "Or drag the bar in the Schedule Timeline\nto set this visually.")

        ttk.Label(dlg, text="Position angle (°):").grid(row=5, column=0, padx=(16, 8), pady=6, sticky="e")
        rot_var = tk.StringVar(value=f"{e.get('rotation_angle', 0.0):.1f}")
        rot_entry = ttk.Entry(dlg, textvariable=rot_var, width=10)
        rot_entry.grid(row=5, column=1, padx=(0, 16), pady=6, sticky="w")
        ToolTip(rot_entry, "Camera rotation / position angle (degrees).\nUsed when exporting to NINA as PositionAngle.\nCaptures the FOV rotation set in the Target Planner.")

        def _apply():
            try:
                new_hrs = float(hrs_var.get())
                new_exp = float(exp_var.get())
                if new_hrs <= 0 or new_exp <= 0:
                    raise ValueError
            except ValueError:
                messagebox.showerror("Invalid", "Both values must be positive numbers.", parent=dlg)
                return
            # Validate start time if provided
            new_st = st_var.get().strip()
            if new_st:
                try:
                    parts = new_st.split(":")
                    if len(parts) != 2 or not (0 <= int(parts[0]) <= 23) or not (0 <= int(parts[1]) <= 59):
                        raise ValueError
                    new_st = f"{int(parts[0]):02d}:{int(parts[1]):02d}"
                except (ValueError, IndexError):
                    messagebox.showerror("Invalid", "Start time must be HH:MM (e.g. 21:30).", parent=dlg)
                    return
            new_subs  = int(new_hrs * 3600.0 / new_exp)
            new_int_h = round((new_subs * new_exp) / 3600.0, 2)
            e["allocated_hrs"] = round(new_hrs, 2)
            e["exp_s"]         = round(new_exp, 1)
            e["n_subs"]        = new_subs
            e["total_int_hrs"] = new_int_h
            e["start_time"]    = new_st
            try:
                e["rotation_angle"] = float(rot_var.get())
            except ValueError:
                e["rotation_angle"] = 0.0
            dlg.destroy()
            self._refresh_plan_tree()

        btn_row = ttk.Frame(dlg)
        btn_row.grid(row=5, column=0, columnspan=2, padx=16, pady=(4, 14))
        ttk.Button(btn_row, text="OK", command=_apply).pack(side="left", padx=(0, 8))
        ttk.Button(btn_row, text="Cancel", command=dlg.destroy).pack(side="left")
        dlg.bind("<Return>", lambda ev: _apply())
        dlg.bind("<Escape>", lambda ev: dlg.destroy())
        self._theme_popup(dlg)

    # ═══════════════════════════════════════════════════════════════════
    # SEARCH SUGGESTIONS — floating auto-complete popup
    # ═══════════════════════════════════════════════════════════════════

    def _on_search_key(self, event):
        """Handle key release in the search entry — update the floating suggestion popup."""
        # Ignore navigation keys
        if event.keysym in ("Up", "Down", "Return", "Escape", "Tab"):
            return
        self._update_floating_suggestions()

    def _update_floating_suggestions(self):
        """Build and show/hide the floating suggestion popup below the search entry.

        Respects self.catalog_filter — targets whose `catalogs` set has no
        overlap with the user's enabled catalogs are excluded from results.
        """
        typed = self.target_search.get().upper().replace(" ", "")
        if not typed:
            self._hide_suggestions()
            return
        if typed.startswith('M') and typed[1:].isdigit():
            typed = f"M{int(typed[1:])}"

        enabled = {c for c, on in self.catalog_filter.items() if on}

        matches = []
        for name in self.searchable_names:
            if typed in name.upper().replace(" ", ""):
                clean = name.upper().replace(" ", "")
                lkp = f"M{int(clean[1:])}" if clean.startswith('M') and clean[1:].isdigit() else clean
                t = self.targets.get(lkp) or self.common_names_map.get(lkp)
                if t and t not in [m[1] for m in matches]:
                    # Catalog filter: include only when the target's catalog
                    # tags have at least one entry in the user's enabled set.
                    # No "rides along" exception — if the user unchecks
                    # everything except Messier, only Messier entries show.
                    t_cats = t.get("catalogs") or {"Other"}
                    if not (t_cats & enabled):
                        continue
                    display = f"{t['id']}  —  {t['common'].split(';')[0].strip()}" if t['common'] else t['id']
                    matches.append((display, t))
            if len(matches) >= 12:
                break

        if not matches:
            self._hide_suggestions()
            return

        self._show_suggestion_popup(matches)

    def _show_suggestion_popup(self, matches):
        """Show or update the floating suggestion Toplevel below the search entry."""
        # Destroy old popup if it exists
        if self._suggestion_popup and self._suggestion_popup.winfo_exists():
            self._suggestion_popup.destroy()

        popup = tk.Toplevel(self.root)
        popup.wm_overrideredirect(True)
        popup.wm_attributes("-topmost", True)
        self._suggestion_popup = popup

        # Position below the search entry
        self.target_search.update_idletasks()
        x = self.target_search.winfo_rootx()
        y = self.target_search.winfo_rooty() + self.target_search.winfo_height()
        # Extra width: ~80px for type badge + magnitude, plus ~100px for the
        # catalog badges that can appear (Messier/Caldwell/NGC/IC/Sharpless)
        w = self.target_search.winfo_width() + 180

        popup.wm_geometry(f"{w}x{min(len(matches) * 28 + 4, 340)}+{x}+{y}")
        popup.configure(bg="#1e2d3e")

        container = tk.Frame(popup, bg="#1e2d3e", highlightthickness=1,
                              highlightbackground="#2e4a63")
        container.pack(fill="both", expand=True)

        TYPE_COLORS = {
            "Nebula": "#8bc34a", "Planetary Nebula": "#8bc34a",
            "Galaxy": "#64b5f6", "Open Cluster": "#ce93d8",
            "Globular Cluster": "#ce93d8", "Other": "#778899",
        }
        # Per-catalog badge colors (fg / bg pairs). Picked to read well on
        # the dark popup chrome and to be distinct from the type badge palette.
        CATALOG_COLORS = {
            "NGC":      ("#aed0ed", "#1d4972"),
            "IC":       ("#9fd8c7", "#1d6258"),
            "Messier":  ("#fad29a", "#8a5a18"),
            "Caldwell": ("#d6c5ef", "#5d3b8a"),
            "Sharpless":("#efb6a0", "#8c4528"),
        }
        # Show badges in a stable order so multi-catalog targets read consistently
        # (e.g. M31 always shows M then NGC; never NGC then M one row, M then NGC the next).
        CATALOG_ORDER = ["Messier", "Caldwell", "NGC", "IC", "Sharpless"]
        # Short labels for the badges (saves horizontal space in the popup)
        CATALOG_LABELS = {
            "NGC": "NGC", "IC": "IC", "Messier": "M",
            "Caldwell": "C", "Sharpless": "Sh2",
        }

        for i, (display, t) in enumerate(matches):
            bg = "#263545" if i == 0 else "#1e2d3e"
            row = tk.Frame(container, bg=bg, cursor="hand2")
            row.pack(fill="x", padx=2, pady=1)

            # Target name and common name
            name_lbl = tk.Label(row, text=display, bg=bg,
                                 fg="#ffffff" if i == 0 else "#cccccc",
                                 font=("Helvetica", 11), anchor="w")
            name_lbl.pack(side="left", padx=(8, 4), pady=3)

            # Magnitude
            mag_str = ""
            if t.get("v_mag") is not None:
                mag_str = f"{t['v_mag']:.1f}"
            if mag_str:
                mag_lbl = tk.Label(row, text=mag_str, bg=bg, fg="#556677",
                                    font=("Helvetica", 9))
                mag_lbl.pack(side="right", padx=(0, 8))

            # Type badge
            obj_type = t.get("obj_type", "Other")
            badge_color = TYPE_COLORS.get(obj_type, "#778899")
            badge_bg = "#2a3520" if "Nebula" in obj_type else (
                "#1e2a3a" if obj_type == "Galaxy" else "#2a2035" if "Cluster" in obj_type else "#222830")
            type_lbl = tk.Label(row, text=obj_type, bg=badge_bg, fg=badge_color,
                                 font=("Helvetica", 8), padx=6, pady=1)
            type_lbl.pack(side="right", padx=(0, 4))

            # Catalog badges (small, between name and type badge).
            # Pack right-to-left so they appear in CATALOG_ORDER reading
            # left-to-right against the type badge.
            cats = t.get("catalogs") or set()
            cat_widgets = []
            for cat in reversed(CATALOG_ORDER):
                if cat in cats:
                    fg, bgc = CATALOG_COLORS[cat]
                    cat_lbl = tk.Label(row, text=CATALOG_LABELS[cat],
                                        bg=bgc, fg=fg,
                                        font=("Helvetica", 8, "bold"),
                                        padx=4, pady=1)
                    cat_lbl.pack(side="right", padx=(0, 3))
                    cat_widgets.append(cat_lbl)

            # Click binding — bind to all sub-widgets
            target_id = t['id']
            for widget in (row, name_lbl, type_lbl, *cat_widgets):
                widget.bind("<Button-1>", lambda e, tid=target_id: self._select_suggestion(tid))
            if mag_str:
                mag_lbl.bind("<Button-1>", lambda e, tid=target_id: self._select_suggestion(tid))

    def _select_suggestion(self, target_id):
        """User clicked a suggestion — fill the search entry and optionally analyze."""
        self._hide_suggestions()
        self.target_search.delete(0, tk.END)
        self.target_search.insert(0, target_id)
        if self.auto_update_enabled:
            self._mark_analysis_dirty()

    def _hide_suggestions(self):
        """Destroy the floating suggestion popup if it exists."""
        if self._suggestion_popup and self._suggestion_popup.winfo_exists():
            self._suggestion_popup.destroy()
        self._suggestion_popup = None

    # ─────────────────────────────────────────────────────────────────────
    # CATALOG FILTER POPUP
    # ─────────────────────────────────────────────────────────────────────
    def _update_catalog_filter_btn_label(self):
        """Refresh the catalog filter button's label to reflect current state.

        Shows just the funnel icon "▽" when every catalog is enabled,
        and "▽•" (with a small dot indicator) when any catalog is filtered
        out. Stays compact and unobtrusive next to the search entry.
        """
        if not hasattr(self, "_catalog_filter_btn"):
            return
        total = len(self.catalog_filter) if self.catalog_filter else 6
        on = sum(1 for v in (self.catalog_filter or {}).values() if v)
        if on == total:
            self._catalog_filter_btn.config(text="▽")
        else:
            self._catalog_filter_btn.config(text="▽•")

    def _toggle_catalog_filter_popup(self):
        """Show or hide the catalog filter checkbox popup."""
        if self._catalog_filter_popup and self._catalog_filter_popup.winfo_exists():
            self._catalog_filter_popup.destroy()
            self._catalog_filter_popup = None
            return
        self._show_catalog_filter_popup()

    def _show_catalog_filter_popup(self):
        """Show the catalog filter checkbox popup beneath the filter button.

        Five checkboxes (NGC / IC / Messier / Caldwell / Sharpless / Other),
        each with the live entry count for that catalog. Toggling immediately
        re-runs the suggestion popup, persists the choice to disk, and
        updates the button label.

        Dismissal: click anywhere outside the popup, click the filter
        button again, or press Escape.

        Implementation note: we use an embedded tk.Frame placed with .place()
        instead of a Toplevel + wm_overrideredirect. On macOS, two
        overrideredirect Toplevels (this popup and the filter button's
        tooltip) fight over z-order in non-fullscreen mode, causing the
        popup to disappear when the mouse moves toward it. An embedded
        Frame is just a normal widget inside the main window so none of
        the Toplevel window-server quirks apply.
        """
        # Close any prior instance
        if self._catalog_filter_popup and self._catalog_filter_popup.winfo_exists():
            self._close_catalog_filter_popup()

        # Embedded Frame, parented to root so it can overlay any tab content
        # ── Pick the right palette for the current theme ─────────────────
        # Hardcoded colors here (rather than the DAY_*/NIGHT_* module
        # constants) because the popup uses several intermediate shades
        # that aren't named in the global palette.
        nm = getattr(self, "night_mode", False)
        bg_panel    = "#2a0000" if nm else "#1e2d3e"   # popup body + rows
        bg_active   = "#330000" if nm else "#263545"   # checkbox hover/active
        border_col  = "#882222" if nm else "#2e4a63"   # popup outline
        fg_primary  = "#cc0000" if nm else "#cbd9e5"   # checkbox text
        fg_dim      = "#882222" if nm else "#6a8aa8"   # header, links, counts
        fg_muted    = "#552222" if nm else "#445566"   # center dot separator

        popup = tk.Frame(self.root, bg=bg_panel,
                          highlightthickness=1, highlightbackground=border_col)
        self._catalog_filter_popup = popup

        # Position via place() using absolute coords *relative to root*.
        # rootx/rooty are screen coords; subtract root's own screen origin
        # to get coords within the root window.
        self.root.update_idletasks()
        self._catalog_filter_btn.update_idletasks()
        btn_x = self._catalog_filter_btn.winfo_rootx() - self.root.winfo_rootx()
        btn_y = self._catalog_filter_btn.winfo_rooty() - self.root.winfo_rooty()
        btn_h = self._catalog_filter_btn.winfo_height()
        popup.place(x=btn_x, y=btn_y + btn_h + 2, width=220, height=245)
        popup.lift()  # ensure it overlays sibling widgets

        # Header row: section label on the left, "All" / "None" quick-action
        # mini-buttons on the right. The mini-buttons are styled as small
        # underlined labels (link aesthetic) rather than ttk.Buttons so they
        # don't dominate the popup chrome.
        header = tk.Frame(popup, bg=bg_panel)
        header.pack(fill="x", padx=10, pady=(8, 4))

        tk.Label(header, text="LIMIT SEARCH TO",
                 bg=bg_panel, fg=fg_dim,
                 font=("Helvetica", 8, "bold")).pack(side="left")

        # 'None' on far right; 'All' just left of it. Hover brightens to
        # signal interactivity (color flip handled in _bind_link_hover).
        none_lbl = tk.Label(header, text="None",
                             bg=bg_panel, fg=fg_dim,
                             font=("Helvetica", 8, "underline"),
                             cursor="hand2")
        none_lbl.pack(side="right")
        none_lbl.bind("<Button-1>", lambda e: self._set_all_catalog_filters(False))
        self._bind_link_hover(none_lbl, normal=fg_dim, hover=fg_primary)

        tk.Label(header, text="·", bg=bg_panel, fg=fg_muted,
                 font=("Helvetica", 8)).pack(side="right", padx=5)

        all_lbl = tk.Label(header, text="All",
                            bg=bg_panel, fg=fg_dim,
                            font=("Helvetica", 8, "underline"),
                            cursor="hand2")
        all_lbl.pack(side="right")
        all_lbl.bind("<Button-1>", lambda e: self._set_all_catalog_filters(True))
        self._bind_link_hover(all_lbl, normal=fg_dim, hover=fg_primary)

        # Pre-compute live counts so users see catalog sizes alongside the toggle
        counts = self._count_targets_by_catalog()

        cat_order = ["NGC", "IC", "Messier", "Caldwell", "Sharpless", "Other"]
        self._catalog_filter_vars = {}
        for cat in cat_order:
            row = tk.Frame(popup, bg=bg_panel, cursor="hand2")
            row.pack(fill="x", padx=4, pady=1)

            var = tk.BooleanVar(value=self.catalog_filter.get(cat, True))
            self._catalog_filter_vars[cat] = var

            # Native Checkbutton with theme-aware styling
            cb = tk.Checkbutton(
                row, text=cat, variable=var,
                bg=bg_panel, fg=fg_primary, selectcolor=bg_panel,
                activebackground=bg_active, activeforeground=fg_primary,
                font=("Helvetica", 11), anchor="w", padx=4,
                command=lambda c=cat: self._on_catalog_filter_change(c))
            cb.pack(side="left", padx=(4, 0))

            count = counts.get(cat, 0)
            count_lbl = tk.Label(row, text=f"{count:,}",
                                  bg=bg_panel, fg=fg_dim,
                                  font=("Helvetica", 9))
            count_lbl.pack(side="right", padx=(0, 10))

        # ── Dismissal: click outside the popup ───────────────────────────
        # Bind on root with add="+" so any existing handlers still fire.
        # The handler checks whether the click landed inside the popup or
        # on the filter button itself; if not, it closes.
        self._popup_click_binding = self.root.bind(
            "<Button-1>", self._maybe_close_catalog_filter_popup, add="+")

        # Escape closes too (keyboard users). Frames don't capture keyboard
        # focus the way Toplevels do, so bind on root with the same guard
        # pattern — the binding gets removed when the popup is dismissed.
        self._popup_escape_binding = self.root.bind(
            "<Escape>", lambda e: self._close_catalog_filter_popup(), add="+")

    def _maybe_close_catalog_filter_popup(self, event):
        """Close the filter popup if the click landed outside its bounds.

        Bound to root <Button-1>. Tkinter dispatches the click to the most
        specific widget first (e.g. a checkbox inside the popup, which
        toggles itself), and the event then propagates up to root. By the
        time this handler runs the checkbox interaction is already done,
        so we just need to decide whether to dismiss.
        """
        popup = self._catalog_filter_popup
        if not popup or not popup.winfo_exists():
            return

        # Was the click inside the popup? Then keep it open.
        px = popup.winfo_rootx()
        py = popup.winfo_rooty()
        pw = popup.winfo_width()
        ph = popup.winfo_height()
        if px <= event.x_root <= px + pw and py <= event.y_root <= py + ph:
            return

        # Was the click on the filter button itself? Let _toggle_catalog_filter_popup
        # handle it — that path also closes us. Avoid a double-close race.
        if hasattr(self, "_catalog_filter_btn"):
            btn = self._catalog_filter_btn
            bx = btn.winfo_rootx()
            by = btn.winfo_rooty()
            bw = btn.winfo_width()
            bh = btn.winfo_height()
            if bx <= event.x_root <= bx + bw and by <= event.y_root <= by + bh:
                return

        self._close_catalog_filter_popup()

    def _close_catalog_filter_popup(self):
        """Tear down the filter popup and remove its root-level bindings."""
        # Remove the root click handler we installed when the popup opened
        if getattr(self, "_popup_click_binding", None):
            try:
                self.root.unbind("<Button-1>", self._popup_click_binding)
            except (tk.TclError, AttributeError):
                pass
            self._popup_click_binding = None
        # And the Escape handler
        if getattr(self, "_popup_escape_binding", None):
            try:
                self.root.unbind("<Escape>", self._popup_escape_binding)
            except (tk.TclError, AttributeError):
                pass
            self._popup_escape_binding = None
        if self._catalog_filter_popup and self._catalog_filter_popup.winfo_exists():
            self._catalog_filter_popup.destroy()
        self._catalog_filter_popup = None

    def _on_catalog_filter_change(self, catalog):
        """User toggled a catalog checkbox — persist + re-run suggestions."""
        if hasattr(self, "_catalog_filter_vars") and catalog in self._catalog_filter_vars:
            self.catalog_filter[catalog] = self._catalog_filter_vars[catalog].get()
        # Persist to settings so the choice survives restart
        self.data.setdefault("settings", {})["catalog_filter"] = dict(self.catalog_filter)
        try:
            self.save_data()
        except Exception:
            pass
        # Refresh button label and the suggestion popup
        self._update_catalog_filter_btn_label()
        if self.target_search.get().strip():
            self._update_floating_suggestions()

    def _set_all_catalog_filters(self, value):
        """Set every catalog filter to value (True for All, False for None).

        Backs the "All" / "None" mini-buttons in the filter popup header.
        Updates both the live BooleanVars (so the checkboxes redraw) and
        self.catalog_filter, then runs the same persist + refresh path as
        a single-checkbox toggle.
        """
        for cat in self.catalog_filter:
            self.catalog_filter[cat] = value
        # Sync the visible BooleanVars if the popup is currently shown
        if hasattr(self, "_catalog_filter_vars"):
            for var in self._catalog_filter_vars.values():
                var.set(value)
        # Persist + UI refresh — same code path as _on_catalog_filter_change
        self.data.setdefault("settings", {})["catalog_filter"] = dict(self.catalog_filter)
        try:
            self.save_data()
        except Exception:
            pass
        self._update_catalog_filter_btn_label()
        if self.target_search.get().strip():
            self._update_floating_suggestions()

    def _bind_link_hover(self, label_widget,
                          normal="#6a8aa8", hover="#cbd9e5"):
        """Wire Enter/Leave to brighten a Label that's acting as a link.

        Used by the All/None mini-buttons in the catalog filter popup.
        Two-color flip — same `add="+"` discipline as ToolTip so it
        coexists with any other bindings on the label.
        """
        label_widget.bind("<Enter>",
                          lambda e: label_widget.config(fg=hover), add="+")
        label_widget.bind("<Leave>",
                          lambda e: label_widget.config(fg=normal), add="+")

    def _count_targets_by_catalog(self):
        """Return a dict of catalog → unique-target count.

        Walks self.targets (deduped by id) and tallies by `catalogs` tag.
        Skips OpenNGC letter-suffixed sub-components (NGC0247A/B/C/D, which
        are sub-knots of NGC 247) so the displayed count matches the
        canonical catalog sizes astronomers expect — e.g. 109 Caldwell,
        not 122 (the +13 are all sub-components of objects already counted).

        Used both by the filter popup row counts and by the settings panel
        per-catalog status breakdown.
        """
        counts = {"NGC": 0, "IC": 0, "Messier": 0,
                  "Caldwell": 0, "Sharpless": 0, "Other": 0}
        seen = set()
        for t in self.targets.values():
            tid = t.get("id")
            if not tid or tid in seen:
                continue
            seen.add(tid)
            # OpenNGC sub-component names end with one or more letters
            # following digits (e.g. NGC0247A). The primary object is
            # already counted; skip the variants.
            if re.search(r'\d+[A-Z]+$', tid):
                continue
            for cat in (t.get("catalogs") or {"Other"}):
                if cat in counts:
                    counts[cat] += 1
        return counts

    # Keep legacy methods for API compatibility (called by Visible Tonight popup etc.)
    def update_suggestions(self, event):
        """Legacy wrapper — delegates to _on_search_key."""
        self._on_search_key(event)

    def on_suggestion_select(self, event):
        """Legacy: load the selected suggestion into the search box."""
        if self.suggestion_list.curselection():
            self.target_search.delete(0, tk.END)
            self.target_search.insert(0, self.suggestion_list.get(
                self.suggestion_list.curselection()[0]).split(" - ")[0])
            if self.auto_update_enabled:
                self._mark_analysis_dirty()

    # ═══════════════════════════════════════════════════════════════════
    # EQUIPMENT MANAGEMENT — add / edit / delete cameras & scopes
    # ═══════════════════════════════════════════════════════════════════

    def on_camera_select(self, event):
        """Double-click handler: populate the camera form from the selected inventory row."""
        sel = self.cam_tree.selection()
        if not sel:
            return
        n = self.cam_tree.item(sel[0], "values")[0]
        c = self.data["cameras"].get(n, {})
        for f, v in zip(
            ["Name:", "Pixel Size (μm):", "Read Noise (e-):", "QE (0-1):",
             "Sensor Width (mm):", "Sensor Height (mm):"],
            [n, c.get("pixel_size"), c.get("read_noise"), c.get("qe"),
             c.get("sensor_w"), c.get("sensor_h")],
        ):
            self.cam_entries[f].delete(0, tk.END)
            self.cam_entries[f].insert(0, str(v))
        self.is_color.set(c.get("is_color", True))

    def on_scope_select(self, event):
        """Double-click handler: populate the scope form from the selected inventory row."""
        sel = self.scope_tree.selection()
        if not sel:
            return
        n = self.scope_tree.item(sel[0], "values")[0]
        s = self.data["scopes"].get(n, {})
        for f, v in zip(
            ["Name:", "Aperture (mm):", "Native Focal Length (mm):"],
            [n, s.get("aperture"), s.get("native_fl")],
        ):
            self.scope_entries[f].delete(0, tk.END)
            self.scope_entries[f].insert(0, str(v))

    def add_camera(self):
        """Validate the camera form, save to the gear JSON, and refresh the inventory."""
        try:
            n = self.cam_entries["Name:"].get()
            ps = float(self.cam_entries["Pixel Size (μm):"].get())
            sw = float(self.cam_entries["Sensor Width (mm):"].get())
            sh = float(self.cam_entries["Sensor Height (mm):"].get())
            px_w = int((sw * 1000) / ps)
            px_h = int((sh * 1000) / ps)
            mp = (px_w * px_h) / 1_000_000
            self.data["cameras"][n] = {
                "pixel_size": ps,
                "read_noise": float(self.cam_entries["Read Noise (e-):"].get()),
                "qe": float(self.cam_entries["QE (0-1):"].get()),
                "sensor_w": sw, "sensor_h": sh,
                "px_w": px_w, "px_h": px_h, "mp": round(mp, 1),
                "is_color": self.is_color.get(),
            }
            self.save_data()
            # Clear form so the user can add another camera without manually
            # wiping fields.  To edit an existing camera, double-click it in
            # the inventory table — that re-populates the form.
            for entry in self.cam_entries.values():
                entry.delete(0, tk.END)
            self.is_color.set(True)
            self.cam_entries["Name:"].focus_set()
        except Exception as e:
            messagebox.showerror("Error", f"Invalid input: {e}")

    def add_scope(self):
        """Validate the scope form, save to the gear JSON, and refresh the inventory."""
        try:
            n = self.scope_entries["Name:"].get()
            fl = float(self.scope_entries["Native Focal Length (mm):"].get())
            ap = float(self.scope_entries["Aperture (mm):"].get())
            self.data["scopes"][n] = {
                "aperture": ap, "native_fl": fl, "native_f_ratio": fl / ap,
            }
            self.save_data()
            # Clear form so the user can add another scope without manually
            # wiping fields.  To edit an existing scope, double-click it in
            # the inventory table — that re-populates the form.
            for entry in self.scope_entries.values():
                entry.delete(0, tk.END)
            self.scope_entries["Name:"].focus_set()
        except (ValueError, TypeError, ZeroDivisionError):
            messagebox.showerror("Error", "Inputs must be numeric.")

    def delete_item(self, category, tree):
        """Delete the selected equipment item after confirmation."""
        sel = tree.selection()
        if not sel:
            return
        name = tree.item(sel[0], "values")[0]
        if messagebox.askyesno("Confirm Delete", f"Delete '{name}'?"):
            if name in self.data[category]:
                del self.data[category][name]
                self.save_data()

    def refresh_inventory_tables(self):
        """Repopulate the camera and scope Treeview widgets from the gear JSON."""
        for t in [self.cam_tree, self.scope_tree]:
            for i in t.get_children():
                t.delete(i)
        for n in sorted(self.data["cameras"].keys()):
            v = self.data["cameras"][n]
            res_str = f"{v.get('px_w', '?')}x{v.get('px_h', '?')} ({v.get('mp', '?')}MP)"
            self.cam_tree.insert("", "end", values=(n, v["pixel_size"], f"{v['sensor_w']}x{v['sensor_h']}", res_str, "Color" if v["is_color"] else "Mono"))
        for n in sorted(self.data["scopes"].keys()):
            v = self.data["scopes"][n]
            self.scope_tree.insert("", "end", values=(n, v.get("aperture", 0), v.get("native_fl", 0), f"f/{v.get('native_f_ratio', 0):.1f}"))

    def refresh_dropdowns(self):
        """Repopulate the scope/camera/filter Combobox values and restore last-session choices."""
        self.scope_dropdown['values'] = sorted(list(self.data["scopes"].keys()))
        self.camera_dropdown['values'] = sorted(list(self.data["cameras"].keys()))

        # Rebuild filter dropdown — start with the permanent entries, then inject
        # any imported narrowband bandwidth that isn't already in the list
        _base_filter_values = ["Mono Lum", "LRGB",
                               "NB (3nm)", "NB (7nm)", "NB (12nm)"]
        nf = self.data.get("nina_filters", {})
        bw = nf.get("bandwidth_nm")
        if bw is not None:
            try:
                bw_int = int(float(bw)) if float(bw) == int(float(bw)) else float(bw)
                nb_key = f"NB ({bw_int}nm)"
                if nb_key not in _base_filter_values:
                    _base_filter_values.append(nb_key)
            except (TypeError, ValueError):
                pass
        self.filter_dropdown["values"] = _base_filter_values

        # Restore last session if values are still valid
        sess = self.data.get("session", {})
        if sess.get("scope") in self.data["scopes"]:
            self.scope_choice.set(sess["scope"])
        if sess.get("camera") in self.data["cameras"]:
            self.camera_choice.set(sess["camera"])
        if sess.get("bortle") in BORTLE_FACTORS:
            self.bortle_choice.set(sess["bortle"])
        if sess.get("reduction"):
            saved_red = sess["reduction"]
            # Migrate old plain-numeric saves (e.g. "1.0") to the new "×" format
            if not saved_red.endswith("×"):
                try:
                    val = float(saved_red)
                    _red_map = {0.63: "0.63×", 0.67: "0.67×", 0.70: "0.70×", 0.80: "0.80×",
                                1.0: "1.0×", 1.5: "1.5×", 2.0: "2.0×", 2.5: "2.5×", 3.0: "3.0×"}
                    saved_red = _red_map.get(round(val, 2), "1.0×")
                except ValueError:
                    saved_red = "1.0×"
            self.reduction_factor.set(saved_red)
        if sess.get("target"):
            self.target_search.delete(0, tk.END)
            self.target_search.insert(0, sess["target"])
        if sess.get("filter_mode") in self.filter_dropdown["values"]:
            self.filter_mode.set(sess["filter_mode"])
        self._refresh_rig_dropdown()
        self._refresh_plan_tree()

if __name__ == "__main__":
    # Sky-map viewer subprocess: when relaunched with --skymap-url, show the
    # interactive map in its own pywebview window (separate process so its GUI
    # loop doesn't collide with Tkinter's), then exit before any Tk starts.
    if "--skymap-url" in sys.argv:
        _run_skymap_child(sys.argv[sys.argv.index("--skymap-url") + 1])
        sys.exit(0)

    root = tk.Tk()
    app = AstroApp(root)
    root.mainloop()
