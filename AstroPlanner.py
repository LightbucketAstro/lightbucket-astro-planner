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
    Sidebar UI — Target Planner, Tonight's Plan, Explore, Manage
    Equipment, Settings — plus day/night theming, DSS image thumbnail
    with FOV overlay, and catalog search. The Plan tab merges target
    browsing with tonight's committed plan and a collapsible schedule-
    timeline strip (the retired Targets List tab's Gantt/reorder tools,
    reattached to the committed plan).

File layout on disk
-------------------
~/LightbucketAstroPlanner/astro_gear.json        — equipment inventory, preferences, last session
~/LightbucketAstroPlanner/ngc_catalog.csv        — NGC/IC catalog (downloaded on first run)
~/LightbucketAstroPlanner/sessions/*.json        — saved session plans
%LOCALAPPDATA%\\LightbucketAstroPlanner\\...       — equivalent paths on Windows
"""

import tkinter as tk
from tkinter import ttk, messagebox, filedialog, simpledialog, font as tkfont
import json
import os
import sys
import platform
import math
import colorsys
import csv
import urllib.request
import urllib.error
import subprocess
import re
import webbrowser
import ssl
import threading
import io
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from PIL import Image, ImageTk

__version__ = "2.0.0"

# ── Update checking (GitHub Releases) ──────────────────────────────────────
# The app is distributed solely through GitHub Releases, so the public,
# unauthenticated releases API is all we need to find out whether a newer
# build exists.  Rate limit for unauthenticated calls is 60/hour per IP —
# far beyond our once-a-day check.
#
# Policy is deliberately "notify, don't install":  the builds are unsigned,
# so silently downloading and swapping a running app would fight Gatekeeper
# and SmartScreen and risk corrupting an install mid-session.  Instead we
# surface a dismissible banner and hand the user to the release page.
UPDATE_REPO         = "LightbucketAstro/lightbucket-astro-planner"
UPDATE_API_URL      = f"https://api.github.com/repos/{UPDATE_REPO}/releases/latest"
UPDATE_RELEASES_URL = f"https://github.com/{UPDATE_REPO}/releases/latest"
# Minimum seconds between automatic checks (24 h).  Manual "Check now"
# from Settings ignores this throttle.
UPDATE_CHECK_INTERVAL_S = 86400
# Network timeout for the version probe.  Kept short: a slow or captive
# network must never delay startup, and a missed check is harmless.
UPDATE_CHECK_TIMEOUT_S = 10


def _parse_version(text):
    """Parse a version string into a comparable tuple of ints.

    Accepts the shapes GitHub tags actually take — ``v1.2.0``, ``1.2.0``,
    ``1.2``, ``v1.2.0-beta2`` — and returns e.g. ``(1, 2, 0)``.  Any
    pre-release suffix is ignored for ordering purposes, which is the
    conservative choice: a user running ``1.2.0-beta2`` will not be nagged
    to "update" to the ``1.2.0`` final, and will be told about ``1.2.1``.

    Returns ``None`` when nothing numeric can be recovered, so callers can
    treat an unparseable tag as "no update" rather than guessing.
    """
    if not text:
        return None
    m = re.match(r"\s*v?(\d+(?:\.\d+)*)", str(text))
    if not m:
        return None
    try:
        return tuple(int(p) for p in m.group(1).split("."))
    except ValueError:
        return None

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

# Hard cap on how many target cards the Plan tab's browsing grid ever builds
# in one pass. Each card does real per-target work (altitude sparkline,
# imaging-window calc, moon separation) plus several Tk widgets, so building
# thousands of them synchronously on the main thread (e.g. "🔭 All" mode with
# no Type/Magnitude filter, ~13,000 catalog candidates) is what actually
# freezes the UI — the scan itself is already backgrounded and isn't the
# bottleneck. Results are pre-sorted by tonight's altitude descending, so the
# cap keeps the best-placed/most relevant subset; search still reaches any
# target regardless of the cap.
GRID_CARD_CAP = 150

# Fixed palette used to color-code Tonight's Plan cards by rig (scope+camera
# pairing). Assigned on first sight of each distinct pairing, in order, and
# kept stable for the rest of the session (see AstroApp._get_rig_accent_color)
# rather than recomputed fresh on every refresh, so a rig's color doesn't
# shift around as targets are added/removed from the plan.
RIG_ACCENT_COLORS = [
    "#38bdf8",  # blue
    "#c084fc",  # purple
    "#34d399",  # green
    "#f59e0b",  # amber
    "#f472b6",  # pink
    "#2dd4bf",  # teal
    "#fb7185",  # rose
    "#a3e635",  # lime
]

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


def _angular_sep_deg(ra1_deg, dec1_deg, ra2_deg, dec2_deg):
    """Return the angular separation in degrees between two sky positions
    (spherical law of cosines — same formula as ``_calc_moon_separation``,
    kept as its own function since it's used for display purposes only:
    showing how far a confirmed FOV pan moved off the catalog centre)."""
    ra1, dec1 = math.radians(ra1_deg), math.radians(dec1_deg)
    ra2, dec2 = math.radians(ra2_deg), math.radians(dec2_deg)
    cos_d = (math.sin(dec1) * math.sin(dec2)
             + math.cos(dec1) * math.cos(dec2) * math.cos(ra1 - ra2))
    return math.degrees(math.acos(max(-1.0, min(1.0, cos_d))))


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


def _dss_survey_size_deg(fov_w_deg, fov_h_deg):
    """Sky patch size (degrees, square) to fetch for a DSS thumbnail wide
    enough to show a sensor of the given FOV with comfortable padding."""
    return max(fov_w_deg, fov_h_deg, 0.1) * 1.6


def _dss_fetch_plan(survey_size_deg):
    """Build fetch_thumbnail's two-lane DSS fetch plan: a 'fast lane'
    (size_deg, timeout_s) list, capped small enough to render promptly,
    plus an optional 'slow lane' (size_deg, timeout_s) pair for the
    FULL size the FOV actually needs, when that's bigger than the fast
    lane covers.

    An earlier version of this tried the real FOV size FIRST and only
    fell back to something smaller if that timed out. Measured against
    a live 9.81deg request, that single attempt genuinely timed out at
    45s (DSS2 cutouts are assembled server-side from scanned
    photographic plates, and SkyView itself notes optical fields of
    several degrees "can take a long time" to build) — so the user sat
    through a 45s stall and STILL only ended up with the small fallback
    image, i.e. strictly worse than before: same clipped frame, slower.

    Running the two lanes CONCURRENTLY fixes that: the fast lane (same
    small size as before) gives the user a picture almost immediately,
    while the slow lane requests the real size in the background with a
    much longer timeout and — if/when it actually lands — silently
    upgrades the preview to the full, unclipped frame. If it never
    lands, the user still has the fast lane's (possibly clipped)
    picture instead of nothing.
    """
    fast_cap = 5.0
    fast_size = min(survey_size_deg, fast_cap)
    fast_lane = [(fast_size, 30)]
    if fast_size > 3.0:
        fast_lane.append((3.0, 20))
    else:
        # FOV was already small — retry once more at the same size
        # rather than dropping further (3deg would be no faster).
        fast_lane.append((fast_size, 20))
    slow_lane = (survey_size_deg, 90) if survey_size_deg > fast_size else None
    return fast_lane, slow_lane


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

    Desktop planner for deep-sky astrophotography sessions.  Holds all
    UI state, equipment inventory, catalog, and the shared Tk root.
    Instantiated once at startup from ``__main__``.

    Tabs (in display order via sidebar)
        Target Planner   — analyse a single object: FOV, exposure, window
        Tonight's Plan   — browsing grid + tonight's committed plan,
                            with a collapsible Gantt schedule strip
                            (drag-to-reschedule, reorder tools) + exports
        Explore          — compare targets side by side
        Manage Equipment — cameras, telescopes, reducers
        Settings         — observer location, preferences, catalog, NINA
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
        #
        # The Plan tab's browsing grid (left, 2 columns of target cards) plus
        # its fixed-380px Tonight's Plan rail (right) together need more than
        # either of the widths above — measured at ~1360px of natural content
        # width with realistic scope/camera names, vs. only 1210-1320px of
        # actual window width, so the rail (packed second, after the
        # expanding grid) was getting compressed below its own 380px and
        # clipping its cards. Widened both by the same ~180px the Windows
        # branch already added over the non-Windows one, plus headroom.
        if platform.system() == "Windows":
            self.root.geometry("1500x855")
        else:
            self.root.geometry("1400x855")

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

        # ── Update-checker state ─────────────────────────────────────────
        # NOTE: deliberately NOT reusing the `auto_update` key above — that
        # one controls "re-run the analysis when equipment chips change" and
        # has nothing to do with software updates.  Separate key, separate
        # meaning.  Defaults to on; users who dislike any network call can
        # turn it off in Settings.
        self.check_updates_enabled = _saved_settings.get("check_for_updates", True)
        self._update_banner   = None   # tk.Frame when the banner is showing
        self._update_info     = None   # dict of the latest release, once fetched
        self._update_checking = False  # guard against overlapping checks

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
        self._unified_filters_popup = None   # Plan tab's consolidated Filters popup (Option C)

        # State shared between analyze_framing and the integration planner
        self._last_exp_s    = None   # most recent recommended sub-exposure (seconds)
        self._last_win_hrs  = None   # most recent dark imaging window length (hours)
        self._last_win_start = None  # imaging window start string (local HH:MM)
        self._last_win_end   = None  # imaging window end string (local HH:MM)
        self._last_sky_flux = None   # sky flux value — used for noise regime note

        # Tonight's Plan — list of dicts, one per planned target
        self._plan_entries: list = []

        # Dirty-flag system for deferred analysis.  Instead of calling
        # analyze_framing immediately (which can be flushed by macOS Tk's
        # update_idletasks into a re-entrant cascade), callers set the dirty
        # flag via _mark_analysis_dirty().  The actual analysis is then
        # scheduled via after() only when the Planner tab is visible.
        self._analysis_dirty = False
        self._analysis_after_id = None   # tracks a pending after() so we don't stack them
        self._grid_rig_refresh_after_id = None   # ditto, for _schedule_grid_rig_refresh

        # Equipment chips (scope/camera/reduction/filter) are restored from
        # the last-used rig, and Bortle from last session state (it isn't
        # part of a rig), exactly once, on the first refresh_dropdowns()
        # call at startup -- see that method for why this must not repeat.
        self._equipment_chips_restored = False

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

        # Headless analysis state — show_integration_plan/_analyze_framing_impl
        # (shared backbone functions also used by the Frame dialog) read/write
        # these StringVars and this Label. They used to be created as a side
        # effect of setup_session_tab building the (now-retired) Targets
        # List tab's "Current Target Analysis" stat cards; created here
        # instead so those functions keep working with nothing visible.
        self._init_headless_analysis_vars()

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
        self.tab_plan    = ttk.Frame(self.tab_control)
        self.tab_explore = ttk.Frame(self.tab_control)
        self.tab_settings = ttk.Frame(self.tab_control)

        # tab_planner is deliberately never added here — the Planner tab is
        # retired from navigation (Explore + the Plan tab's cards/Frame
        # dialog now cover everything it did), but setup_planner_tab() still
        # runs and builds it below: self.scope_choice/camera_choice/
        # bortle_choice/filter_mode/reduction_factor, self.target_search,
        # self.results_txt, self.preview_canvas, self.alt_canvas, and the
        # saved-rig machinery are all still live global state that the Frame
        # dialog, the Plan tab's cards, and NINA export read/write — moving
        # all of that off this tab's widgets is real future work, not a
        # same-session refactor, so the tab is hidden rather than torn out.
        self.tab_control.add(self.tab_explore, text='Explore')
        self.tab_control.add(self.tab_plan,    text="Tonight's Plan")
        self.tab_control.add(self.tab_equip,   text='Manage Equipment')
        self.tab_control.add(self.tab_settings, text='Settings')
        self.tab_control.pack(expand=1, fill="both")
        self.tab_control.bind("<<NotebookTabChanged>>", self._on_tab_changed)

        self._build_sidebar()

        self.setup_input_tab()
        self.setup_planner_tab()
        self.setup_plan_tab()
        self.setup_explore_tab()
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
        # Update check runs last and off-thread — startup, catalog load, and
        # first paint all finish well before this fires, so a slow or absent
        # network never delays the app becoming usable.
        self.root.after(2500, self._maybe_check_for_updates)

        # macOS Cocoa Tk resets ttk styles when a modal dialog (messagebox)
        # returns focus to the main window.  Bind <FocusIn> on root so we
        # re-apply night mode whenever the app regains focus.
        self._night_reapply_id = None
        self.root.bind("<FocusIn>", self._on_focus_in, add="+")

        # Intercept window close so we can prompt to save the session.
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    def _route_to_equip_if_empty(self):
        """Switch to the Equip tab if the user has no scopes or cameras.

        Runs early in startup to sidestep a macOS Cocoa Tk paint stall on
        an empty-state tab, and to land the user on the tab they need
        anyway: you can't plan a session without equipment. Only reroutes
        if the inventory is genuinely empty — users with saved gear start
        on Explore (the sidebar's first entry) as usual.
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
            "Explore or Tonight's Plan tab to begin planning a session.\n\n"
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
            elif current == str(self.tab_explore):
                self.root.after(100, self._kick_explore_paint)
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
        # Explore is first — and therefore the tab selected at startup by
        # the _sidebar_select(0) call below — now that the Planner tab is
        # retired from navigation (see the tab_control.add(...) calls above).
        self._sidebar_tabs = [
            ("Explore",  self.tab_explore, "compass"),
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

        elif icon_name == "compass":
            # Compass — outer ring, cardinal ticks, needle (two opposed
            # triangles) angled NE/SW to suggest exploration.
            cv.create_oval(cx-9, cy-9, cx+9, cy+9, outline=color, width=1.5)
            for angle_deg in (0, 90, 180, 270):
                rad = math.radians(angle_deg)
                x1 = cx + 9 * math.cos(rad)
                y1 = cy + 9 * math.sin(rad)
                x2 = cx + 12 * math.cos(rad)
                y2 = cy + 12 * math.sin(rad)
                cv.create_line(x1, y1, x2, y2, fill=color, width=1.5)
            # Needle: NE half filled, SW half outline
            cv.create_polygon(cx+5, cy-5, cx-1, cy+2, cx-2, cy-2,
                              fill=color, outline=color)
            cv.create_polygon(cx-5, cy+5, cx+1, cy-2, cx+2, cy+2,
                              fill="", outline=color)

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
    _DATA_SCHEMA = 8

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
            "filter_sets":  {},
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
        schema 6 → 7  (Filter Manager)
            • Add top-level ``filters`` dict — {name: {"type": "narrowband"|
              "lrgb", "bandwidth_nm": float}} — replacing the old model
              where every narrowband filter shared one global
              ``nina_filters["bandwidth_nm"]``. Seeded from whatever
              ``nina_filters`` already had (same bandwidth applied to each
              imported narrowband name — the best available starting point;
              refine per-filter afterward in the new manager), or from
              Ha/OIII/SII @ 7nm + L/R/G/B if nothing was ever imported, so
              no existing install loses its narrowband/LRGB options.
            • Stamp ``schema: 7``
        schema 7 → 8  (Filter Sets)
            • Replace the flat ``filters`` dict (one bandwidth per
              individual narrowband filter) with top-level ``filter_sets``
              dict — {name: {"bandwidth_nm": float|None, "members":
              [{"name": str, "type": "narrowband"|"lrgb"}, ...]}}. A rig now
              points at a whole Filter Set (its filter wheel's contents,
              which may mix narrowband and LRGB channels) instead of one
              filter at a time, so "Add to Tonight" adds every filter the
              set contains in one click. Every filter that existed under
              schema 7 is folded into one set, "My Filters", using the
              bandwidth of its first narrowband entry as that set's shared
              bandwidth (schema 7 rarely had per-filter bandwidths differ in
              practice, so this loses little); any rig that pointed at a
              specific filter or the old generic "LRGB" is repointed at
              "My Filters" (Mono Lum / no-filter rigs are untouched).
            • Stamp ``schema: 8``
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

        if v < 7:
            # ── 6 → 7: introduce a real Filter Manager (data["filters"]) ──
            filters = {}
            nf = data.get("nina_filters", {}) or {}
            nf_bw = nf.get("bandwidth_nm")
            for name in (nf.get("narrowband") or []):
                filters[name] = {"type": "narrowband",
                                  "bandwidth_nm": float(nf_bw) if nf_bw else 7.0}
            for name in (nf.get("lrgb") or []):
                filters[name] = {"type": "lrgb"}
            if not filters:
                filters = {
                    "Ha":   {"type": "narrowband", "bandwidth_nm": 7.0},
                    "OIII": {"type": "narrowband", "bandwidth_nm": 7.0},
                    "SII":  {"type": "narrowband", "bandwidth_nm": 7.0},
                    "L": {"type": "lrgb"}, "R": {"type": "lrgb"},
                    "G": {"type": "lrgb"}, "B": {"type": "lrgb"},
                }
            data["filters"] = filters
            data["schema"] = 7
            v = 7

        if v < 8:
            # ── 7 → 8: replace the flat per-filter dict with Filter Sets ──
            old_filters = data.get("filters", {}) or {}
            filter_sets = {}
            if old_filters:
                members = []
                nb_bw = None
                for fname, finfo in old_filters.items():
                    ftype = finfo.get("type", "lrgb")
                    members.append({"name": fname, "type": ftype})
                    if ftype == "narrowband" and nb_bw is None and finfo.get("bandwidth_nm"):
                        nb_bw = float(finfo["bandwidth_nm"])
                filter_sets["My Filters"] = {"bandwidth_nm": nb_bw, "members": members}
            data["filter_sets"] = filter_sets
            data.pop("filters", None)
            # A rig used to point at one filter (or the old generic "LRGB")
            # — repoint it at the new consolidated set. "Mono Lum"/no-filter
            # rigs need no change.
            default_set_name = "My Filters" if filter_sets else ""
            for rig in data.get("settings", {}).get("rigs", []):
                old_val = rig.get("filter", "")
                if old_val and old_val != "Mono Lum":
                    rig["filter"] = default_set_name
            data["schema"] = 8
            v = 8

        # Future migrations go here:
        # if v < 9:
        #     ...
        #     data["schema"] = 9
        #     v = 8

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

        # ── Sky darkness (Bortle) — sits right under dusk/dawn since it's
        # sky-condition context, not equipment. Global: read by the Frame
        # dialog, the Plan tab's cards/NINA export, Explore's rig reports,
        # everywhere the old Planner-tab chip fed. There is exactly one of
        # these in the whole app now — Explore's Rig Builder and the
        # equipment drawer both used to show their own second copy of this
        # control; both were removed in favor of this single source of
        # truth (see _on_bortle_changed). ────
        sky_row = tk.Frame(twi_frame, bg=HDR_BG)
        sky_row.pack(anchor="w", pady=(4, 0))
        tk.Label(sky_row, text="SKY:", font=("Helvetica", 9, "bold"),
                 bg=HDR_BG, fg=ACC_FG).pack(side="left", padx=(0, 6))
        self.bortle_choice = tk.StringVar(value=self.data.get("settings", {}).get(
            "default_bortle", "4 (Rural/Suburban)"))
        self.bortle_dropdown = ttk.Combobox(sky_row, textvariable=self.bortle_choice,
                                             values=list(BORTLE_FACTORS.keys()),
                                             state="readonly", width=20)
        self.bortle_dropdown.pack(side="left")
        ToolTip(self.bortle_dropdown,
                "Sky darkness (Bortle scale) at your site —\n"
                "feeds the exposure recommendation everywhere\n"
                "it's used (Frame dialog, plan cards, NINA export).")

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
        """Refresh all date-sensitive displays after a planning date change.

        The browsing grid ("search results" on the Plan tab) is its own
        case: each card's peak-altitude and imaging-window figures come
        from a scan done the last time _refresh_visible_grid() ran, not
        recomputed live on render — same category of gap as the Bortle
        setting previously not reaching Tonight's Plan. The scan
        functions themselves are already planning-date-aware (they key
        off the shared _planning_local_noon()/_jd_local_noon() globals),
        so nothing about the astronomy math needs to change here — the
        grid just needs to be told to re-scan, exactly like it already
        is after an equipment or filter change.
        """
        self.refresh_twilight_header()
        self.refresh_moon_header()
        if self.auto_update_enabled:
            self._mark_analysis_dirty()
        self._refresh_visible_grid()

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

        # Explore tab's rotate slider — dark trough + orange thumb to match
        # the tab's own navy/orange palette (the tab draws its own colours
        # directly rather than using TAB_BG, since it's always dark
        # regardless of night mode). Only rendered on the "clam" theme (see
        # the comment above about Windows/macOS-light forcing clam); macOS
        # dark-mode users keep native aqua sliders, which already look fine
        # on a dark background.
        style.configure("Explore.Horizontal.TScale",
                        background="#cc8833", troughcolor="#0e1a28")

        # Explore tab's floating zoom +/- pill — flat, borderless, sized to
        # sit flush inside the pill frame (same "named custom button style"
        # pattern as Nav.TButton / HDR.TButton above).
        style.configure("ExploreZoom.TButton",
                        background="#131f2e", foreground="#e8edf2",
                        relief="flat", borderwidth=0, padding=(0, 3),
                        font=("Helvetica", 11, "bold"))
        style.map("ExploreZoom.TButton",
                  background=[("active", "#2a3f55")],
                  foreground=[("active", "#ffffff")])

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

            # Explore legend cards — dynamic widgets, rebuilt night-aware so
            # they get the deliberate red palette instead of the walker's
            # generic recolor (which flattens their bg/fg distinctions).
            try:
                self._explore_refresh_cards()
            except Exception:
                pass
            # Explore's report panel — same situation: rebuild so its chip
            # row (hardcoded day colours) and header/section-tag text pick
            # up the red night palette too. The report body's own tags
            # ("optimal"/"sec_framing"/etc.) are already caught by the
            # generic _recolor walker above (it re-tags every tk.Text it
            # finds), so only the chip rebuild is needed here.
            try:
                self._explore_refresh_report()
            except Exception:
                pass

            # Update banner — created after the colour snapshot, so rebuild it
            # rather than relying on the recolor walker.
            try:
                self._retheme_update_banner()
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
                # Explore's report panel uses the identical tag set — restore
                # it the same way (the night-mode walker re-tags it generically
                # on the way in, but day-mode restore only special-cases
                # results_txt by name, so this one needs its own copy).
                self._explore_report_txt.tag_configure("optimal",   foreground="#4caf50", font=("Helvetica", 11, "bold"))
                self._explore_report_txt.tag_configure("warning",   foreground="#ef5350", font=("Helvetica", 11, "bold"))
                self._explore_report_txt.tag_configure("highlight", foreground="#38bdf8", font=("Helvetica", 11, "bold"))
                self._explore_report_txt.tag_configure("header",    foreground="#ffffff", font=("Helvetica", 13, "bold"))
                self._explore_report_txt.tag_configure("sec_framing",  foreground="#38bdf8", font=("Helvetica", 10, "bold"))
                self._explore_report_txt.tag_configure("sec_exposure", foreground="#f59e0b", font=("Helvetica", 10, "bold"))
                self._explore_report_txt.tag_configure("sec_window",   foreground="#4caf50", font=("Helvetica", 10, "bold"))
                self._explore_report_txt.tag_configure("sec_moon",     foreground="#ef5350", font=("Helvetica", 10, "bold"))
                self._explore_report_txt.tag_configure("dim",          foreground="#778899", font=("Helvetica", 11))
                self._explore_report_txt.tag_configure("amber",        foreground="#f59e0b", font=("Helvetica", 11, "bold"))
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

            # Rebuild the dynamic plan-card list on Tonight's Plan tab.
            # These widgets are created after startup so they aren't in
            # _orig_colors and the day-mode restore loop above can't reach
            # them — without this, they'd stay red after a Night → Day
            # toggle. The refresh method recreates the tk.Frame / tk.Label
            # widgets with fresh day-mode colors.
            try:
                self._refresh_plan_tree()
            except Exception:
                pass
            # Rebuild the Plan tab's live browsing/search grid too — same
            # dynamic-widget situation as the plan-card list above:
            # _build_target_card's tk.Frame/Label widgets are created after
            # startup, so they're absent from _orig_colors and the restore
            # loop above can't reach them. The night-mode _recolor walker
            # painted them red on the way in (it walks every tk widget
            # under self.root); without this rebuild they'd stay red after
            # a Night -> Day toggle, which is exactly what Jerry reported.
            try:
                self._refresh_visible_grid()
            except Exception:
                pass
            # Explore legend cards — same dynamic-widget situation as the
            # queue/plan cards above: rebuild so they return to day colours.
            try:
                self._explore_refresh_cards()
            except Exception:
                pass
            try:
                self._explore_refresh_report()
            except Exception:
                pass
            # Update banner — same dynamic-widget situation as the cards above.
            try:
                self._retheme_update_banner()
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
        # Scrollable container so the whole tab (Camera/Telescope/Rig Library
        # row, Equipment Inventory, Filter Library) fits shorter windows
        # instead of clipping the bottom sections — same canvas+scrollbar
        # pattern as the Settings tab (see _bind_equip_mousewheel below).
        _eq_bg = ttk.Style().lookup("TFrame", "background") or "#1e2d3e"
        self._equip_canvas = tk.Canvas(self.tab_equip, bg=_eq_bg, highlightthickness=0)
        _equip_sb = ttk.Scrollbar(self.tab_equip, orient="vertical",
                                  command=self._equip_canvas.yview)
        self._equip_canvas.configure(yscrollcommand=_equip_sb.set)
        _equip_sb.pack(side="right", fill="y")
        self._equip_canvas.pack(side="left", fill="both", expand=True)

        outer = ttk.Frame(self._equip_canvas)
        outer.bind("<Configure>", lambda e: self._equip_canvas.configure(
            scrollregion=self._equip_canvas.bbox("all")))
        self._equip_canvas.create_window((0, 0), window=outer, anchor="nw", tags="inner")
        self._equip_canvas.bind("<Configure>", lambda e:
            self._equip_canvas.itemconfig("inner", width=e.width))

        container = ttk.Frame(outer)
        container.pack(fill="x")
        container.columnconfigure(0, weight=1)
        container.columnconfigure(1, weight=1)
        container.columnconfigure(2, weight=1)
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

        # ── Rig Library ─ reclaims the dead space beside Camera/Telescope
        # Settings. Same underlying rig data/functions as the equipment
        # drawer's "Manage Rigs" dialog (_find_rig, _apply_rig,
        # _refresh_rig_dropdown, _build_rig_list_and_details, and the
        # generic _rig_manager_delete helper) — this is just a second,
        # always-visible view onto the same rigs. Renaming lives inside the
        # Edit dialog's own Name field here (no separate Rename button) —
        # the standalone "Manage Rigs" modal keeps its own Rename button.
        # Mocked as "Option 1" (unified rig library) and approved.
        rig_frame = ttk.LabelFrame(container, text="Rig Library")
        rig_frame.grid(row=0, column=2, padx=20, pady=10, sticky="nsew")

        (self._equip_rig_lb, equip_rig_details, self._equip_rig_names,
         self._equip_rig_detail_labels, self._equip_rig_refresh) = \
            self._build_rig_list_and_details(rig_frame, rig_frame, list_height=6, list_width=20)
        self._equip_rig_lb.pack(fill="x", padx=2, pady=(0, 8))
        equip_rig_details.pack(fill="x", padx=2, pady=(0, 8))

        def _equip_rig_apply(event=None):
            sel = self._equip_rig_lb.curselection()
            if not sel or sel[0] >= len(self._equip_rig_names):
                return
            name = self._equip_rig_names[sel[0]]
            rig = self._find_rig(name)
            if rig is None:
                return
            self._apply_rig(rig)
            self._equip_rig_refresh(preserve_name=name)
            self._show_toast(f"Applied rig: {name}")
        self._equip_rig_lb.bind("<Double-1>", _equip_rig_apply)
        ToolTip(self._equip_rig_lb, "Click a rig to preview its details below.\nDouble-click to apply it to the current equipment chips.")

        # Icon-only buttons (not "+ New" / "Edit" / "Rename" / "Delete" text)
        # — this column is only 1/3-width now, and the tooltips below still
        # spell out exactly what each one does.
        rig_btn_row = ttk.Frame(rig_frame)
        rig_btn_row.pack(fill="x", pady=(4, 0))
        new_rig_btn = ttk.Button(
            rig_btn_row, text="+", width=3,
            command=lambda: self._open_edit_rig_dialog(None, self._equip_rig_refresh, self.root))
        new_rig_btn.pack(side="left", expand=True, fill="x", padx=(0, 3))
        ToolTip(new_rig_btn, "New rig — opens a blank editor for\nscope, reducer, camera, and filter")
        edit_rig_btn = ttk.Button(
            rig_btn_row, text="✎", width=3,
            command=lambda: self._rig_manager_edit(
                self._equip_rig_names, self._equip_rig_lb, self._equip_rig_refresh, self.root))
        edit_rig_btn.pack(side="left", expand=True, fill="x", padx=3)
        ToolTip(edit_rig_btn, "Edit — scope, reducer, camera, filter, and\n"
                              "name are all editable here (renaming no longer\n"
                              "needs a separate button) — nothing is changed\n"
                              "until you click Save.")
        delete_rig_btn = ttk.Button(
            rig_btn_row, text="✕", width=3,
            command=lambda: self._rig_manager_delete(
                self._equip_rig_names, self._equip_rig_lb, self._equip_rig_refresh, self.root))
        delete_rig_btn.pack(side="left", expand=True, fill="x", padx=(3, 0))
        ToolTip(delete_rig_btn, "Permanently delete the selected rig")

        self._equip_rig_refresh()

        # Equipment Inventory sits under Camera + Telescope Settings (2/3 of
        # the row's width) so Filter Library can take the column under Rig
        # Library instead of running full-width below everything — the two
        # "library" panels (Rig, Filter) now stack in one column together.
        inv_frame = ttk.LabelFrame(container, text="Equipment Inventory")
        inv_frame.grid(row=1, column=0, columnspan=2, padx=20, pady=10, sticky="nsew")

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

        # ── Filter Library ─ manages Filter Sets (self.data["filter_sets"]),
        # not individual filters. Same "always-visible library" footing as
        # Equipment Inventory above and the Rig Library above it — no
        # modal, no extra click. A rig now points at a whole Filter Set
        # (its filter wheel's contents — can mix LRGB and narrowband
        # channels), built up by adding filters into the set one at a time,
        # similar to how a rig is built from named components. Sits in Rig
        # Library's column (row 1, under it) so the two "library" panels
        # stack together — recoups the horizontal space Equipment Inventory
        # no longer needs at full width.
        filter_frame = ttk.LabelFrame(container, text="Filter Library")
        filter_frame.grid(row=1, column=2, padx=20, pady=10, sticky="nsew")

        # Stacked list-then-details (not side-by-side) — matches how the Rig
        # Library above it is laid out in this same narrow column.
        (self._equip_filter_lb, equip_filter_details, self._equip_filter_names,
         self._equip_filter_detail_labels, self._equip_filter_refresh) = \
            self._build_filter_set_list_and_details(filter_frame, filter_frame,
                                                      list_height=7, list_width=20)
        self._equip_filter_lb.pack(fill="x", padx=2, pady=(0, 8))
        equip_filter_details.pack(fill="x", padx=2, pady=(0, 8))

        def _equip_filter_dblclick(event=None):
            self._filter_manager_edit(self._equip_filter_names, self._equip_filter_lb,
                                       self._equip_filter_refresh, self.root)
        self._equip_filter_lb.bind("<Double-1>", _equip_filter_dblclick)
        ToolTip(self._equip_filter_lb, "Click a filter set to preview its details below.\nDouble-click to edit it.")

        # Icon-only buttons — same reasoning as the Rig Library's button row
        # above (narrow column, tooltips carry the explanation).
        filter_btn_row = ttk.Frame(filter_frame)
        filter_btn_row.pack(fill="x", padx=2, pady=(0, 8))
        new_filter_btn = ttk.Button(
            filter_btn_row, text="+", width=3,
            command=lambda: self._open_edit_filter_dialog(None, self._equip_filter_refresh, self.root))
        new_filter_btn.pack(side="left", expand=True, fill="x", padx=(0, 3))
        ToolTip(new_filter_btn, "New filter set — build a filter wheel's\ncontents (narrowband and/or LRGB)")
        edit_filter_btn = ttk.Button(
            filter_btn_row, text="✎", width=3,
            command=lambda: self._filter_manager_edit(
                self._equip_filter_names, self._equip_filter_lb, self._equip_filter_refresh, self.root))
        edit_filter_btn.pack(side="left", expand=True, fill="x", padx=3)
        ToolTip(edit_filter_btn, "Edit the selected filter set's name\nor its member filters")
        delete_filter_btn = ttk.Button(
            filter_btn_row, text="✕", width=3,
            command=lambda: self._filter_manager_delete(
                self._equip_filter_names, self._equip_filter_lb, self._equip_filter_refresh, self.root))
        delete_filter_btn.pack(side="left", expand=True, fill="x", padx=(3, 0))
        ToolTip(delete_filter_btn, "Permanently delete the selected filter set")

        self._equip_filter_refresh()

        # Wheel-scroll the whole tab (canvas + every widget inside it).
        self._bind_equip_mousewheel(self._equip_canvas)
        self._bind_equip_mousewheel_recursive(outer)

    def setup_planner_tab(self):
        """Build the Target Planner tab — equipment chips, target search, FOV preview, results."""
        # ── Equipment chips bar ───────────────────────────────────────────
        ctrl_frame = tk.Frame(self.tab_planner, bg="#131f2e")
        ctrl_frame.pack(padx=0, pady=0, fill="x")
        
        self.scope_choice, self.camera_choice = tk.StringVar(), tk.StringVar()
        # self.bortle_choice/bortle_dropdown are created in setup_header (it
        # now lives in the header, under the twilight readout, rather than
        # as a chip here — see setup_header) so it reads as a global "sky
        # conditions" setting rather than a per-tab equipment control.
        self.filter_mode = tk.StringVar(value="Mono Lum")
        self.reduction_factor = tk.StringVar(value="1.0×")

        for var in [self.scope_choice, self.camera_choice, self.filter_mode, self.reduction_factor]:
            var.trace_add('write', self.on_parameter_change)
        # Bortle (sky brightness) isn't equipment — it's an environmental
        # fact, not part of a rig — so it gets its own handler instead of
        # riding on_parameter_change's rig-comparison logic, and that
        # handler always forces a fresh recompute rather than only when
        # "auto update" is on (see _on_bortle_changed).
        self.bortle_choice.trace_add('write', self._on_bortle_changed)

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
        _reduction_values = ["0.63×", "0.67×", "0.70×", "0.75×", "0.80×", "1.0×", "1.5×", "2.0×", "2.5×", "3.0×"]
        self.reduction_entry = ttk.Combobox(chips_row, textvariable=self.reduction_factor,
                                            values=_reduction_values, state="readonly", width=6)
        self.reduction_entry.pack(side="left", padx=(0, 6))

        # Camera chip
        self.camera_dropdown = ttk.Combobox(chips_row, textvariable=self.camera_choice,
                                             state="readonly", width=18)
        self.camera_dropdown.pack(side="left", padx=(0, 6))

        # Filter chip (visibility toggled by camera type)
        self.filter_label = ttk.Label(chips_row, text="")  # hidden placeholder
        # Values come from self.data["filter_sets"] (the Filter Library on
        # the Equipment tab) via refresh_dropdowns() — not hardcoded here,
        # same as the scope/camera dropdowns just above.
        self.filter_dropdown = ttk.Combobox(chips_row, textvariable=self.filter_mode,
                                             state="readonly", width=12)
        self.camera_choice.trace_add('write', self.toggle_filter_visibility)

        # Right-side info summary
        self._equip_summary_label = tk.Label(chips_row, text="", bg="#131f2e",
                                              fg="#556677", font=("Helvetica", 9))
        self._equip_summary_label.pack(side="right", padx=(10, 0))

        # Kept so the Phase-3 equipment drawer can borrow these same chip
        # widgets (scope/reducer/camera/filter) and hand them back
        # here when it closes, instead of creating a second, StringVar-
        # duplicated set of controls.
        self._equip_chips_row = chips_row

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
            self._queue_btn.create_text(30, h//2, text="Add to Tonight",
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
            self._add_target_from_planner_search()
        def _qb_release(e):
            _qb_leave(e)

        self._queue_btn.bind("<Enter>",           _qb_enter)
        self._queue_btn.bind("<Leave>",           _qb_leave)
        self._queue_btn.bind("<Button-1>",        _qb_click)
        self._queue_btn.bind("<ButtonRelease-1>", _qb_release)
        self._draw_queue_btn = _draw_queue_btn   # store for night-mode redraws
        ToolTip(self._queue_btn, "Add this target straight to Tonight's Plan,\nusing this tab's current equipment and\nany pan/rotation you've set in the FOV preview.")

        # ── Full-width search entry ──────────────────────────────────────
        self.target_search = ttk.Entry(search_row, width=50, font=("Helvetica", 12))
        self.target_search.pack(side="left", fill="x", expand=True, padx=(0, 4))

        # Catalog filter button lives on the Plan tab now, next to
        # self.plan_search (see setup_plan_tab) — it's shared, global filter
        # state (self.catalog_filter) that _update_floating_suggestions
        # already applies no matter which search box is calling it, so only
        # the button itself needed to move.

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
        # "Show Sky Map" has moved to the Explore tab (above its FOV image) —
        # see _explore_open_sky_map / setup_explore_tab.

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
        self._rot_label = tk.Label(ctrl_row, text=f"{self._screen_to_sky_pa(0.0):.1f}°", bg="#131f2e", fg="#ffffff",
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
        self._pending_frame_restore = None  # staged by _analyze_framing_impl,
                                             # applied by _apply_pending_frame_restore
                                             # once a fresh DSS fetch lands
        self._drag_mode    = None   # 'pan' | 'rotate'
        self._drag_start   = None   # (x, y) for pan
        self._rotate_start_angle = None  # angle at drag start (degrees)
        self._fov_corners  = []     # current corner positions for hit-testing
        self._cached_dss_img = None
        self._cached_dss_target = None
        self._cached_dss_survey_deg = None

        # Wide-field wait state (see fetch_thumbnail / _draw_fov_wait_frame):
        # shown instead of a picture while a DSS request big enough to need
        # the "slow lane" is still in flight, with a live elapsed timer and
        # a "Show 5° field instead" cancel pill drawn on the FOV canvas.
        self._fov_wait_token = 0          # bumped on every fetch_thumbnail() call so a
                                           # stale wait-state loop/callback can tell it's
                                           # been superseded and stop touching the canvas
        self._fov_wait_after_id = None    # scheduled .after() id for the spinner/timer tick
        self._fov_wait_fast_ready = None  # (img, fetched_deg) once the fast lane lands —
                                           # cached so Cancel can show it instantly
        self._fov_wait_cancelled = False
        self._fov_wait_target = None
        self._fov_wait_survey_deg = None
        self._fov_wait_start_ts = None
        self._fov_wait_angle = 0
        self._fov_result_token = None     # token that produced the currently cached/
                                           # displayed DSS image — see _show_dss_result's
                                           # size tie-break, which must only compare
                                           # results racing within the SAME fetch_thumbnail()
                                           # call, never against a stale image left over
                                           # from an earlier call for a different rig/FOV

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

    def _init_headless_analysis_vars(self):
        """Headless state for show_integration_plan/_analyze_framing_impl.

        These StringVars (and one Label) used to live on the Targets List
        tab's "Current Target Analysis" card. That tab is retired now —
        per Jerry, the Gantt timeline and reorder tools moved to the Plan
        tab, but the stat cards themselves were fine to drop — yet the
        Frame dialog still runs analyze_framing, which still calls these
        shared functions and expects them to exist. So they're created
        here with no visible widget: values get computed and stored, just
        never displayed.
        """
        self.session_hours_var    = tk.StringVar(value="4.0")
        self.manual_exp_var       = tk.StringVar(value="")
        self.overhead_per_sub_var = tk.StringVar(value="5")
        self._plan_target_var     = tk.StringVar(value="")
        self._plan_subs_var       = tk.StringVar(value="—")
        self._plan_total_var      = tk.StringVar(value="—")
        self._plan_snr_var        = tk.StringVar(value="—")
        self._plan_overhead_var   = tk.StringVar(value="—")
        self._plan_noise_var      = tk.StringVar(value="")
        self.session_hours_var.trace_add("write", lambda *_: self.show_integration_plan(silent=True))
        self.manual_exp_var.trace_add("write", lambda *_: self.show_integration_plan(silent=True))
        self.overhead_per_sub_var.trace_add("write", lambda *_: self.show_integration_plan(silent=True))
        # A real Label — some code paths call .config(text=...) on it —
        # parented to root but never packed, so it never becomes visible.
        self._recommended_exp_label = ttk.Label(self.root, text="")

    # ═══════════════════════════════════════════════════════════════════
    # TARGET CARD — new browsing-grid card (pill / sparkline / moon line /
    # status tag / actions), and the live grid that renders a row of them.
    # ═══════════════════════════════════════════════════════════════════

    _CARD_PANEL  = "#131f2e"
    _CARD_LINE   = "#1e2d3e"
    _CARD_TEXT   = "#ffffff"
    _CARD_MUTED  = "#aaaaaa"
    _CARD_FAINT  = "#556677"
    _CARD_BLUE   = "#38bdf8"
    _CARD_GREEN  = "#4caf50"
    _CARD_AMBER  = "#f59e0b"
    _CARD_RED    = "#ef5350"

    # Object-type chip (approved "Option C" condensed row design) — its own
    # muted color channel, deliberately not reusing green/amber/red (those
    # already mean imaging condition elsewhere on the row) or blue (the
    # primary-action color). Covers every category _identify_catalogs /
    # NGC_TYPE_CATEGORIES can produce, not just the 5 in OBJECT_TYPE_FILTERS
    # — Asterism/Star/unknown still need a chip, just not a Type-dropdown
    # entry of their own.
    _TYPE_CHIP = {
        "Galaxy":           ("GAL",  "#a78bfa"),
        "Nebula":           ("NEB",  "#2dd4bf"),
        "Planetary Nebula": ("PN",   "#f472b6"),
        "Open Cluster":     ("OC",   "#a3e635"),
        "Globular Cluster": ("GC",   "#818cf8"),
        "Asterism":         ("AST",  "#94a3b8"),
        "Star":             ("STAR", "#94a3b8"),
    }
    _TYPE_CHIP_DEFAULT = ("OTH", "#667788")

    def _build_target_card(self, parent, t, max_alt, rise_label):
        """Build one browsing-grid row for target ``t`` — a dense,
        single-line "list row" (approved "Option C" design, replacing the
        earlier two-column card).

        ``max_alt``/``rise_label`` come straight from a
        ``_scan_visible_targets`` result tuple — ``rise_label`` is "up"
        (already above min-altitude), an "HH:MM" rise time, or ``None``.

        Left to right: accent bar, id + common name, a color-coded
        object-type chip, a flexible middle cluster (altitude sparkline +
        moon-condition icon + a length-capped "peak ##° · HH:MM–HH:MM"
        readout), a visibility pill, and icon-only Frame / Add-to-Tonight
        actions.

        Why this replaces the old stacked card: that layout's sparkline+
        readout row alone needed ~280px, which at a 2-column grid forced
        the whole grid wider than its pane once the toolbar above it grew
        too (the search-box widening that triggered the cropping bug
        report). A single row per target needs roughly half that, and —
        just as importantly — there's only one column left to compete for
        width, so it can't repeat that failure mode. The middle cluster's
        peak/window text is capped to a fixed max length rather than truly
        reflowing with the window (Tk labels have no CSS-style
        text-overflow: ellipsis), which is a deliberately simple,
        deterministic way to guarantee the sparkline/moon-icon/action
        buttons are never the thing that gives up space.

        Every side="right" widget below is packed BEFORE the flexible
        middle cluster (in rightmost-to-leftmost order) precisely so it
        can't happen again: packing an expand=True sibling before a
        fixed-size one lets it greedily claim the fixed one's space too
        (see _open_equip_drawer's docstring for the original lesson this
        mirrors).
        """
        P, LN, TX, MU, FA = (self._CARD_PANEL, self._CARD_LINE, self._CARD_TEXT,
                              self._CARD_MUTED, self._CARD_FAINT)
        BL, GR = self._CARD_BLUE, self._CARD_GREEN

        # "In plan" has to mean "already has an entry under the CURRENTLY
        # selected rig + filter mode" -- the exact same key
        # _add_target_to_plan_direct's duplicate check uses -- not just
        # "this target is on the plan under *some* rig". Otherwise
        # switching rigs leaves every previously-added target showing a
        # stale ✓, even though clicking ✚ for the new rig would actually
        # succeed (it's a different scope/camera, not a duplicate). This
        # also means the icon self-corrects when you switch back to a rig
        # a target really is planned under -- no separate state to track.
        in_plan = any(e["target_id"] == t["id"]
                      and e.get("scope") == self.scope_choice.get()
                      and e.get("camera") == self.camera_choice.get()
                      and e.get("filter_mode") == self.filter_mode.get()
                      for e in self._plan_entries)
        accent  = GR if in_plan else "#3a4a5c"

        row = tk.Frame(parent, bg=P, highlightthickness=1,
                        highlightbackground=LN, highlightcolor=LN)
        tk.Frame(row, bg=accent, width=3).pack(side="left", fill="y")
        body = tk.Frame(row, bg=P)
        body.pack(side="left", fill="both", expand=True, padx=10, pady=7)

        # ── Right cluster (packed first — see docstring) ────────────────
        if in_plan:
            add_text, add_bg, add_fg = "✓", "#142a1a", GR
        else:
            add_text, add_bg, add_fg = "＋", "#0d1826", MU
        add_btn = tk.Label(body, text=add_text, bg=add_bg, fg=add_fg,
                            font=("Helvetica", 11, "bold"), width=2, cursor="hand2")
        add_btn.pack(side="right")
        add_btn.bind("<Button-1>", lambda e, tid=t["id"]: self._add_target_to_tonight(tid))
        ToolTip(add_btn, "Already in tonight's plan" if in_plan else "Add to Tonight's Plan")

        frame_btn = tk.Label(body, text="🖼", bg=BL, fg="#0a1a26",
                              font=("Helvetica", 11), width=2, cursor="hand2")
        frame_btn.pack(side="right", padx=(4, 6))
        frame_btn.bind("<Button-1>", lambda e, tid=t["id"]: self._frame_from_card(tid))
        ToolTip(frame_btn, "Open the Frame dialog for this target.")

        if rise_label == "up":
            pill_text, pill_fg, pill_bg = "up now", GR, "#142a1a"
        elif rise_label:
            pill_text, pill_fg, pill_bg = f"rises {rise_label}", BL, "#0f2233"
        else:
            pill_text, pill_fg, pill_bg = "—", FA, P
        tk.Label(body, text=pill_text, bg=pill_bg, fg=pill_fg,
                 font=("Helvetica", 9), padx=7, pady=2).pack(side="right", padx=(6, 6))

        # ── Left cluster: name + common name + type chip ────────────────
        name_wrap = tk.Frame(body, bg=P)
        name_wrap.pack(side="left")
        tk.Label(name_wrap, text=t["id"], bg=P, fg=TX,
                 font=("Helvetica", 12, "bold")).pack(side="left")
        common = t.get("common", "").split(";")[0].strip()
        if common:
            if len(common) > 20:
                common = common[:19] + "…"
            tk.Label(name_wrap, text="  " + common, bg=P, fg=BL,
                     font=("Helvetica", 9)).pack(side="left")

        abbrev, chip_col = self._TYPE_CHIP.get(t.get("obj_type", ""), self._TYPE_CHIP_DEFAULT)
        chip_wrap = tk.Frame(body, bg="#18232f")
        chip_wrap.pack(side="left", padx=(8, 0))
        tk.Frame(chip_wrap, bg=chip_col, width=3).pack(side="left", fill="y")
        tk.Label(chip_wrap, text=abbrev, bg="#18232f", fg="#cbd5e1",
                 font=("Helvetica", 8, "bold"), padx=5, pady=2).pack(side="left")
        ToolTip(chip_wrap, t.get("obj_type") or "Other")

        # ── Flexible middle cluster: sparkline + moon icon + peak/window ─
        # Packed LAST — the one thing in this row allowed to give up space.
        mid = tk.Frame(body, bg=P)
        mid.pack(side="left", fill="both", expand=True, padx=(10, 6))

        lat, lon = self._get_saved_location()
        spark = tk.Canvas(mid, width=46, height=18, bg=P, highlightthickness=0)
        spark.pack(side="left")
        if lat is not None and lon is not None:
            series = self._sample_altitude_series(t, lat, lon)
            alts = series["alts"]
            if alts:
                lo, hi = min(alts), max(alts)
                rng = (hi - lo) or 1.0
                n = len(alts)
                pts = []
                for i, a in enumerate(alts):
                    x = 1 + (i / (n - 1)) * 44
                    y = 16 - ((a - lo) / rng) * 13
                    pts.extend([x, y])
                line_col = GR if rise_label == "up" else (BL if rise_label else FA)
                if len(pts) >= 4:
                    spark.create_line(*pts, fill=line_col, width=1.6, smooth=True)

        stat_bits = [f"peak {max_alt:.0f}°"]
        if lat is not None and lon is not None:
            _tag, icon, note, _impact, _illum, _sep = self._moon_condition(t["ra_deg"], t["dec_deg"])
            moon_lbl = tk.Label(mid, text=icon, bg=P, font=("Helvetica", 10))
            moon_lbl.pack(side="left", padx=(6, 6))
            ToolTip(moon_lbl, note)
            try:
                _min_alt = float(self.data.get("settings", {}).get("min_alt", 20))
                win_start, win_end, win_hrs, _peak_t, _peak_a, _diag = \
                    _calc_best_imaging_window(t["ra_deg"], t["dec_deg"], lat, lon,
                                               min_alt=_min_alt)
            except Exception:
                win_start = win_end = None
                win_hrs = 0.0
            if win_hrs and win_start and win_end:
                stat_bits.append(f"{win_start}–{win_end}")

        stat_text = " · ".join(stat_bits)
        if len(stat_text) > 24:
            stat_text = stat_text[:23] + "…"
        tk.Label(mid, text=stat_text, bg=P, fg=MU, font=("Helvetica", 9)).pack(side="left")

        return row

    def _frame_from_card(self, target_id):
        """Open the modal Frame dialog for a card's target (chart above the
        FOV framing box, per the approved layout).

        This does not reimplement any astronomy/geometry math — it reuses
        ``analyze_framing``/``_analyze_framing_impl``, ``_draw_altitude_chart``,
        ``_draw_fov_overlay``, and the ``_fov_mouse_*``/``_apply_zoom``/
        ``_reset_fov_framing`` handlers completely unchanged. Those methods
        all read/write shared instance attributes (``self.preview_canvas``,
        ``self.alt_canvas``, ``self._rot_label``, ``self._zoom_label``)
        rather than taking widgets as parameters, so this dialog just points
        those attributes at its own widgets for as long as it's open and
        restores the Planner tab's originals when it closes — the Planner
        tab's own FOV/chart widgets are untouched and keep working as a
        fallback until Phase 5 retires that tab.
        """
        key = target_id.replace(" ", "").upper()
        t = self.targets.get(key) or self.common_names_map.get(key)
        if not t:
            messagebox.showwarning("Target Not Found",
                f"Could not find '{target_id}' in the catalog.")
            return
        # Pin it to the top of the Plan tab's browsing grid — see
        # _mark_grid_touched — so re-finding it later never requires a
        # fresh search.
        self._mark_grid_touched(t["id"])
        if not self.scope_choice.get() or not self.camera_choice.get():
            messagebox.showwarning("Equipment Needed",
                "Choose a scope and camera (via the rig chip) before framing a target.")
            return

        self.target_search.delete(0, tk.END)
        self.target_search.insert(0, target_id)

        dlg = tk.Toplevel(self.root)
        dlg.title(f"Frame — {t['id']}")
        dlg.configure(bg="#0d1620")
        dlg.resizable(False, False)
        dlg.grab_set()
        dlg.transient(self.root)

        header = tk.Frame(dlg, bg="#0d1620")
        header.pack(fill="x", padx=16, pady=(14, 4))
        common = t.get("common", "").split(";")[0].strip()
        title_txt = t["id"] + (f"   ·   {common}" if common else "")
        tk.Label(header, text=title_txt, bg="#0d1620", fg="#e8eef4",
                 font=("Helvetica", 14, "bold")).pack(side="left")

        # ── Altitude / moon chart — reuses _draw_altitude_chart verbatim ──
        chart_canvas = tk.Canvas(dlg, width=400, height=190, bg="#050810",
                                  highlightthickness=1, highlightbackground="#2e4a63")
        chart_canvas.pack(padx=16, pady=(8, 14))

        # ── FOV framing box — reuses _draw_fov_overlay / pan-rotate-zoom verbatim ──
        fov_canvas = tk.Canvas(dlg, width=340, height=340, bg="black",
                                highlightthickness=1, highlightbackground="#2e4a63")
        fov_canvas.pack()
        fov_canvas.create_text(170, 170, text="Loading…", fill="gray", font=("Helvetica", 9))
        tk.Label(dlg, text="drag to pan · corners to rotate", bg="#0d1620",
                 fg="#556677", font=("Helvetica", 8)).pack(pady=(2, 0))

        ctrl_row = tk.Frame(dlg, bg="#0d1620")
        ctrl_row.pack(pady=(6, 4))
        tk.Label(ctrl_row, text="Rot:", bg="#0d1620", fg="#778899",
                 font=("Helvetica", 9)).pack(side="left", padx=(0, 2))
        rot_label = tk.Label(ctrl_row, text=f"{self._screen_to_sky_pa(0.0):.1f}°",
                              bg="#0d1620", fg="#ffffff", font=("Helvetica", 9), width=5)
        rot_label.pack(side="left")
        tk.Label(ctrl_row, text="|", bg="#0d1620", fg="#2e4a63").pack(side="left", padx=6)
        reset_btn = ttk.Button(ctrl_row, text="⌖ Reset", width=7, command=lambda: self._reset_fov_framing())
        reset_btn.pack(side="left", padx=2)
        tk.Label(ctrl_row, text="|", bg="#0d1620", fg="#2e4a63").pack(side="left", padx=6)
        tk.Label(ctrl_row, text="Zoom:", bg="#0d1620", fg="#778899",
                 font=("Helvetica", 9)).pack(side="left", padx=(0, 2))
        ttk.Button(ctrl_row, text="−", width=2,
                   command=lambda: self._apply_zoom(1 / 1.25)).pack(side="left", padx=1)
        zoom_label = tk.Label(ctrl_row, text="1.0×", bg="#0d1620", fg="#ffffff",
                               font=("Helvetica", 9), width=4)
        zoom_label.pack(side="left")
        ttk.Button(ctrl_row, text="+", width=2,
                   command=lambda: self._apply_zoom(1.25)).pack(side="left", padx=1)

        fov_canvas.bind("<ButtonPress-1>",   self._fov_mouse_down)
        fov_canvas.bind("<B1-Motion>",       self._fov_mouse_drag)
        fov_canvas.bind("<ButtonRelease-1>", self._fov_mouse_up)
        fov_canvas.bind("<Motion>",          self._fov_mouse_hover)
        fov_canvas.bind("<MouseWheel>",      self._fov_mousewheel)
        fov_canvas.bind("<Button-4>",        self._fov_mousewheel)
        fov_canvas.bind("<Button-5>",        self._fov_mousewheel)

        # ── Point the shared FOV/chart attributes at this dialog's widgets ──
        _orig_preview_canvas = self.preview_canvas
        _orig_alt_canvas     = self.alt_canvas
        _orig_rot_label      = self._rot_label
        _orig_zoom_label     = self._zoom_label
        self.preview_canvas  = fov_canvas
        self.alt_canvas      = chart_canvas
        self._rot_label      = rot_label
        self._zoom_label     = zoom_label

        def _restore_originals():
            self.preview_canvas = _orig_preview_canvas
            self.alt_canvas     = _orig_alt_canvas
            self._rot_label     = _orig_rot_label
            self._zoom_label    = _orig_zoom_label

        def _on_cancel():
            _restore_originals()
            dlg.destroy()

        def _on_confirm():
            # Capture framing (pan + rotation) while self.preview_canvas/
            # self._fov_angle still point at THIS dialog's widgets/state —
            # _framed_center converts the accumulated pixel pan using the
            # canvas's current width, so it must run before the originals
            # (a different-sized canvas) are restored.
            framed_ra, framed_dec = self._framed_center(t["ra_deg"], t["dec_deg"])
            rotation_angle = self._screen_to_sky_pa(getattr(self, "_fov_angle", 0.0) or 0.0)
            _restore_originals()
            dlg.destroy()
            try:
                reduction = float(self.reduction_factor.get().rstrip("×x"))
            except ValueError:
                reduction = 1.0
            self._add_target_to_plan_direct(
                t, self.scope_choice.get(), self.camera_choice.get(),
                self.bortle_choice.get(), self.filter_mode.get(), reduction,
                rotation_angle=rotation_angle,
                framed_ra_deg=framed_ra, framed_dec_deg=framed_dec,
                report_text="", allow_update_framing=True)
            # _add_target_to_plan_direct's own async completion callback now
            # triggers the grid rescan itself, once the plan entry has
            # actually landed -- calling it again here, immediately, used
            # to race that background computation and could repopulate the
            # grid from _plan_entries before the new entry was in it,
            # leaving the card's "＋" stuck instead of flipping to "✓".
            # allow_update_framing=True: this dialog exists to dial in a
            # real framing, so if the target's already on tonight's plan
            # (e.g. quick-added earlier with no framing), Confirm should
            # update that plan row's pointing/rotation rather than refuse
            # with "Already Planned" -- that refusal was the reported bug.

        # Already on tonight's plan under this exact rig/filter mode? Then
        # Confirm is going to update that row's framing rather than add a
        # new one (see allow_update_framing above) -- label it accordingly
        # so that's not a surprise.
        _already_planned = any(
            pe["target_id"] == t["id"] and pe.get("scope") == self.scope_choice.get()
            and pe.get("camera") == self.camera_choice.get()
            and pe.get("filter_mode") == self.filter_mode.get()
            for pe in self._plan_entries)

        btn_row = tk.Frame(dlg, bg="#0d1620")
        btn_row.pack(fill="x", padx=16, pady=(6, 16))
        ttk.Button(btn_row, text="Update Framing" if _already_planned else "Confirm Framing",
                   command=_on_confirm).pack(side="right")
        ttk.Button(btn_row, text="Cancel", command=_on_cancel).pack(side="right", padx=(0, 6))

        dlg.protocol("WM_DELETE_WINDOW", _on_cancel)

        # Give the dialog a moment to map before running analysis (matches
        # the defer pattern used elsewhere before this dialog existed).
        self.root.after(50, self.analyze_framing)

    def _add_target_to_tonight(self, target_id):
        """Add a card's target straight to Tonight's Plan — no framing
        dialog, no tab jump.

        This is the plain "Add to Tonight" action (as opposed to the
        card's "Frame" button, which opens the modal Frame dialog first).
        Since no interactive framing has happened, it commits the
        catalog's raw RA/Dec with a neutral 0° rotation rather than
        calling ``_framed_center``/reading ``self._fov_angle`` — those
        hold scratch state from whatever was last framed elsewhere in
        the app, and would silently carry over a stale pan/rotation onto
        an unrelated target. Framing first (via the Frame dialog) and
        then confirming is the path that sets a real pan/rotation.
        """
        key = target_id.replace(" ", "").upper()
        t = self.targets.get(key) or self.common_names_map.get(key)
        if not t:
            messagebox.showwarning("Target Not Found",
                f"Could not find '{target_id}' in the catalog.")
            return
        self._mark_grid_touched(t["id"])
        scope_name = self.scope_choice.get()
        cam_name   = self.camera_choice.get()
        if not scope_name or not cam_name:
            messagebox.showwarning("Equipment Needed",
                "Choose a scope and camera (via the rig chip) before adding "
                "a target to tonight's plan.")
            return
        try:
            reduction = float(self.reduction_factor.get().rstrip("×x"))
        except ValueError:
            reduction = 1.0
        self._add_target_to_plan_direct(
            t, scope_name, cam_name, self.bortle_choice.get(),
            self.filter_mode.get(), reduction,
            rotation_angle=self._screen_to_sky_pa(0.0),
            framed_ra_deg=None, framed_dec_deg=None,
            report_text="")

    def _refresh_visible_grid(self):
        """(Re)scan the catalog for tonight's visible targets and rebuild
        the browsing grid. Runs the scan in a background thread — same
        pattern as show_visible_tonight — so the UI doesn't freeze."""
        if getattr(self, "_grid_scan_running", False):
            return
        if not hasattr(self, "_grid_inner"):
            return   # tab not built yet
        self._grid_scan_running = True
        self._grid_hint_var.set("Scanning catalog…")

        lat, lon = self._get_saved_location()
        if lat is None or lon is None:
            self._grid_scan_running = False
            self._grid_hint_var.set("Set an observer location in Settings first.")
            self._populate_visible_grid([])
            return

        obj_type = self._grid_type_var.get() if hasattr(self, "_grid_type_var") else "All Types"

        # Extra filters (Min Altitude / Max Magnitude / Seasonal), set via
        # the toolbar's "Filters" popover — fall back to the old hardcoded
        # behavior if the tab hasn't been rebuilt with them yet.
        try:
            min_alt = float(self._grid_min_alt_var.get()) if hasattr(self, "_grid_min_alt_var") \
                else float(self.data.get("settings", {}).get("min_alt", 20))
        except (TypeError, ValueError):
            min_alt = float(self.data.get("settings", {}).get("min_alt", 20))
        mag_txt = self._grid_mag_limit_var.get().strip() if hasattr(self, "_grid_mag_limit_var") else ""
        try:
            mag_limit = float(mag_txt) if mag_txt else None
        except ValueError:
            mag_limit = None
        use_surf_br   = self._grid_use_surf_br_var.get() if hasattr(self, "_grid_use_surf_br_var") else False
        seasonal_only = self._grid_seasonal_var.get() if hasattr(self, "_grid_seasonal_var") else True

        # "🌙 Tonight" / "🔭 All" mode — All drops the altitude gate so the
        # grid becomes a free browse of the whole catalog (see _set_grid_mode).
        mode = self._grid_mode_var.get() if hasattr(self, "_grid_mode_var") else "tonight"
        require_min_alt = (mode != "all")

        def _compute():
            results, fallback_note = self._scan_visible_targets(
                lat, lon, min_alt=min_alt, obj_type=obj_type,
                mag_limit=mag_limit, use_surf_br=use_surf_br, seasonal_only=seasonal_only,
                require_min_alt=require_min_alt)

            def _show():
                self._grid_scan_running = False
                shown, pinned_n, total = self._populate_visible_grid(results)
                capped = total > shown
                pin_note = f"{pinned_n} pinned + " if pinned_n else ""
                if mode == "all":
                    if capped:
                        hint = (f"{fallback_note}Showing {pin_note}best-placed {shown} of {total} "
                                f"catalog objects — narrow with Type/Magnitude or search directly "
                                f"for a specific target")
                    else:
                        hint = (f"{fallback_note}{total} catalog objects "
                                f"(not altitude-limited) · sorted by tonight's peak altitude")
                else:
                    if capped:
                        hint = (f"{fallback_note}Showing {pin_note}best-placed {shown} of {total} "
                                f"visible tonight — narrow with Type/Magnitude to see the rest")
                    else:
                        hint = f"{fallback_note}{total} visible tonight · sorted by altitude"
                self._grid_hint_var.set(hint)
            self.root.after(0, _show)

        threading.Thread(target=_compute, daemon=True).start()

    def _mark_grid_touched(self, target_id):
        """Record a target as touched (framed, or added to Tonight straight
        from a card) so _populate_visible_grid pins it at the top of the
        Plan tab's browsing grid — no re-searching for something you're
        actively working on. Re-touching moves it back to the front; a
        plain dict preserves insertion order, so "most recent" is just
        "last in" (read back via reversed() below)."""
        if not hasattr(self, "_grid_touched"):
            self._grid_touched = {}
        self._grid_touched.pop(target_id, None)
        self._grid_touched[target_id] = True

    def _unmark_grid_touched_if_unplanned(self, target_id):
        """Drop ``target_id``'s pin (see _mark_grid_touched) once it's no
        longer in tonight's plan.

        Being "touched" is what keeps a target pinned at the top of the
        browsing grid regardless of the active filters — useful while
        you're actively working with it, but once you remove it from the
        plan there's no reason for it to keep ignoring, say, a Type
        restriction that would otherwise exclude it. A target can have
        several plan entries (one per filter channel, e.g. split LRGB), so
        this only clears the pin once none of them reference it any more.
        """
        if not hasattr(self, "_grid_touched") or target_id not in self._grid_touched:
            return
        if any(e["target_id"] == target_id for e in self._plan_entries):
            return
        del self._grid_touched[target_id]

    def _populate_visible_grid(self, results):
        """Rebuild the grid's card widgets from a _scan_visible_targets
        result list.

        Anything touched this session (see _mark_grid_touched) is pinned at
        the top, most-recently-touched first, even if it doesn't match the
        current scan/filters — computing its altitude/rise data fresh in
        that case. The remaining scan results follow in their original
        (altitude-sorted) order. GRID_CARD_CAP (see its comment) still
        bounds the total shown, but only trims the non-pinned tail — a
        touched target is never bumped off by the cap.

        Returns (shown, pinned_count, total) so the caller can build an
        accurate "Showing X of Y" hint.
        """
        for w in self._grid_inner.winfo_children():
            w.destroy()
        # Single column (Option C) — the earlier 2-column grid required
        # each card to fit in half the pane's width, and once the toolbar
        # above it grew (the search-box widening fix), two cards competing
        # for width was what actually got cropped. A single dense row per
        # target has no sibling column to compete with.
        cols = 1
        for c in range(cols):
            self._grid_inner.grid_columnconfigure(c, weight=1, uniform="gridcol")

        touched_ids = list(reversed(self._grid_touched)) if hasattr(self, "_grid_touched") else []
        by_id = {tup[0]["id"]: tup for tup in results}
        pinned, seen = [], set()
        if touched_ids:
            lat, lon = self._get_saved_location()
            min_alt = float(self.data.get("settings", {}).get("min_alt", 20))
            dark_range = _dark_jd_range(lat, lon) if lat is not None and lon is not None else None
            for tid in touched_ids:
                if tid in seen:
                    continue
                seen.add(tid)
                if tid in by_id:
                    pinned.append(by_id[tid])
                elif dark_range is not None:
                    t = self.targets.get(tid) or self.common_names_map.get(tid)
                    if not t:
                        continue
                    max_alt, rise_label = _alt_rise_tonight(
                        t["ra_deg"], t["dec_deg"], lat, lon, min_alt, dark_range)
                    pinned.append((t, max_alt, rise_label))
                # No saved location yet: skip computing a touched-but-unscanned
                # target rather than showing it with meaningless altitude data.

        others = [tup for tup in results if tup[0]["id"] not in seen]
        budget = max(0, GRID_CARD_CAP - len(pinned))
        ordered = pinned + others[:budget]

        row_offset = 0
        if pinned:
            tk.Label(self._grid_inner, text="RECENTLY TOUCHED", bg="#0e1a28", fg="#556677",
                     font=("Helvetica", 8, "bold")).grid(
                row=0, column=0, columnspan=cols, sticky="w", padx=2, pady=(0, 4))
            row_offset = 1

        for idx, (t, max_alt, rise_label) in enumerate(ordered):
            card = self._build_target_card(self._grid_inner, t, max_alt, rise_label)
            r, c = divmod(idx, cols)
            card.grid(row=r + row_offset, column=c, sticky="nsew", padx=6, pady=4)

        return len(ordered), len(pinned), len(pinned) + len(others)

    def _build_plan_left_pane(self, parent):
        """Build the new live "Visible Tonight" browsing grid — the left
        half of the merged Plan tab. The right half (below) is the
        existing Tonight's Plan rail, unchanged.

        Toolbar (left to right): the Tonight/All mode segment, the rig
        chip, a catalog search box that jumps straight to Frame for any
        target, one consolidated "⚙ Filters" popover (Object Type,
        Catalogs, Min Altitude / Max Magnitude / Seasonal — see
        _show_unified_filters_popup), and an icon-only Rescan. That single
        popover replaces what used to be three separate controls (a ▽
        catalog-filter button, a Type dropdown, and a "Filters ▾" button)
        — approved "Option C" consolidation, done specifically to free up
        toolbar width after the search box was widened for readability
        and started forcing the grid wider than its pane."""
        toolbar = ttk.Frame(parent)
        toolbar.pack(fill="x", pady=(0, 8))

        # ── 🌙 Tonight / 🔭 All mode segment ─────────────────────────────
        # Replaces the old static "Visible Tonight" title. "All" drops the
        # altitude gate entirely (see _scan_visible_targets' require_min_alt)
        # so the grid becomes a free browse of the whole catalog — lets a
        # search for something not currently favorable stay on the list
        # instead of only opening Frame and disappearing, and supports
        # planning future sessions. Forces "Seasonal only" off while active,
        # since that's itself a curated tonight-relevant subset.
        self._grid_mode_var = tk.StringVar(value="tonight")
        self._grid_prior_seasonal = True
        seg_wrap = tk.Frame(toolbar, bg="#1a2b3c", highlightthickness=1, highlightbackground="#1e2d3e")
        seg_wrap.pack(side="left")
        self._grid_seg_tonight = tk.Label(seg_wrap, text="🌙 Tonight", font=("Helvetica", 9, "bold"),
                                           padx=10, pady=5, cursor="hand2")
        self._grid_seg_tonight.pack(side="left")
        self._grid_seg_all = tk.Label(seg_wrap, text="🔭 All", font=("Helvetica", 9, "bold"),
                                       padx=10, pady=5, cursor="hand2")
        self._grid_seg_all.pack(side="left")
        self._grid_seg_tonight.bind("<Button-1>", lambda e: self._set_grid_mode("tonight"))
        self._grid_seg_all.bind("<Button-1>", lambda e: self._set_grid_mode("all"))
        ToolTip(self._grid_seg_tonight, "Only objects above your minimum\naltitude tonight.")
        ToolTip(self._grid_seg_all, "Every catalog object, regardless of\n"
                                     "tonight's altitude — for planning ahead.\n"
                                     "Search results also stay on this list.")
        self._update_grid_mode_seg()

        # ── Rig chip — opens/closes the equipment drawer ────────────────
        # "ACTIVE RIG" caption + a glowing pill (blue-tinted fill/border plus
        # a green live-status dot), per the approved design round.
        chip_wrap = tk.Frame(toolbar, bg="#1e2d3e")
        chip_wrap.pack(side="left", padx=(14, 0))

        tk.Label(chip_wrap, text="ACTIVE RIG", bg="#1e2d3e", fg="#7eb8d4",
                 font=("Helvetica", 8, "bold")).pack(side="left", padx=(0, 8))

        self._rig_chip_pill = tk.Frame(chip_wrap, bg="#0f2233", cursor="hand2",
                                        highlightthickness=2, highlightbackground="#1f4a63")
        self._rig_chip_pill.pack(side="left")

        self._rig_chip_dot = tk.Canvas(self._rig_chip_pill, width=8, height=8,
                                        bg="#0f2233", highlightthickness=0, cursor="hand2")
        self._rig_chip_dot.pack(side="left", padx=(10, 6), pady=7)
        self._rig_chip_dot.create_oval(1, 1, 7, 7, fill="#34d399", outline="")

        self._rig_chip_btn = tk.Label(self._rig_chip_pill, bg="#0f2233", fg="#38bdf8",
                                       font=("Helvetica", 9, "bold"),
                                       padx=0, pady=7, cursor="hand2")
        self._rig_chip_btn.pack(side="left", padx=(0, 12))

        for _w in (self._rig_chip_pill, self._rig_chip_dot, self._rig_chip_btn):
            _w.bind("<Button-1>", lambda e: self._toggle_equip_drawer())
            ToolTip(_w, "Active equipment — click to switch\nrigs or tweak scope/camera/filter.")
        self._update_rig_chip_label()
        self.rig_choice.trace_add('write', lambda *a: self._update_rig_chip_label())

        # ── Catalog search — jump straight to any target, whether or not
        # it's in tonight's visible list below. Reuses the exact floating
        # suggestion popup the Planner/Explore search boxes use (see the
        # "Search plumbing" section near _explore_on_search_key) rather
        # than a fresh implementation.
        # Font matches whatever ttk.Button actually renders with (its style
        # is never explicitly given a font, so this is the live theme
        # default) rather than a guessed size — that's the "match the
        # button" ask. A dedicated "PlanSearch.TEntry" style adds vertical
        # padding for a taller box without touching every other Entry in
        # the app; the placeholder style shares that same padding so the
        # box doesn't resize when the placeholder appears/disappears.
        _btn_font = ttk.Style().lookup("TButton", "font") or "TkDefaultFont"
        self.plan_search = ttk.Entry(toolbar, width=26, font=_btn_font,
                                     style="PlanSearch.TEntry")
        self.plan_search.pack(side="left", padx=(14, 0))
        ToolTip(self.plan_search, "Search by catalog ID (M42, NGC 224) or\n"
                                   "common name — opens straight into Frame,\n"
                                   "even if it's not in tonight's list below.")
        self.plan_search.bind("<KeyRelease>", self._plan_on_search_key)
        self.plan_search.bind("<Return>", lambda e: (self._hide_suggestions(), self._plan_search_submit()))
        self.plan_search.bind("<Escape>", lambda e: self._hide_suggestions())
        self.plan_search.bind("<FocusIn>", lambda e: self._plan_search_clear_placeholder())
        self.plan_search.bind("<FocusOut>", self._plan_search_on_focus_out)

        # A plain "it's a text box" placeholder — a dedicated style so the
        # grey only ever applies while the placeholder itself is showing;
        # swapping back to the "PlanSearch.TEntry" style (rather than
        # setting an instance-level foreground override) means the real
        # typed text keeps tracking the normal day/night TEntry color
        # automatically, instead of getting stuck at whichever color was
        # current when the placeholder was last cleared.
        style = ttk.Style()
        style.configure("PlanSearch.TEntry", padding=(8, 6))
        style.configure("PlanSearchPlaceholder.TEntry", foreground="#667788", padding=(8, 6))
        # TEntry's own style.map forces foreground back to the normal text
        # colour in the "!disabled"/"focus"/"" states (see _setup_day_styles),
        # and a derived style inherits that map unless it defines its own —
        # without this override the placeholder text rendered in the normal
        # white/red day/night colour instead of grey, no matter what
        # style.configure() above set as the plain default.
        style.map("PlanSearchPlaceholder.TEntry",
                  foreground=[("!disabled", "#667788"), ("focus", "#667788"), ("", "#667788")])
        self._plan_search_placeholder_active = False
        self._plan_search_show_placeholder()

        # ── Extra filters (Min Altitude / Max Magnitude / Seasonal) ─────
        # Pulled over from the old "Visible Tonight" popup — same override
        # semantics: seeded from the saved settings default but not written
        # back to it, kept in-memory for this session only (matching how
        # show_visible_tonight's own min-altitude field already behaves).
        self._grid_touched         = {}   # target_id -> True, insertion-order = touch order (see _mark_grid_touched)
        self._grid_type_var        = tk.StringVar(value="All Types")
        self._grid_min_alt_var     = tk.StringVar(
            value=str(int(self.data.get("settings", {}).get("min_alt", 20))))
        self._grid_mag_limit_var   = tk.StringVar(value="")
        self._grid_use_surf_br_var = tk.BooleanVar(value=False)
        self._grid_seasonal_var    = tk.BooleanVar(value=True)

        # Packed right-to-left: the first side="right" widget claims the
        # rightmost slot, so Rescan (icon-only, to save width) is packed
        # first to end up rightmost, then the single unified Filters
        # button just left of it.
        rescan_btn = ttk.Button(toolbar, text="🔄", width=3,
                                 command=self._refresh_visible_grid)
        rescan_btn.pack(side="right")
        ToolTip(rescan_btn, "Re-run the scan (e.g. after changing\nyour observer location).")

        self._unified_filters_btn = ttk.Button(toolbar, text="⚙ Filters ▾",
                                                command=self._toggle_unified_filters_popup)
        self._unified_filters_btn.pack(side="right", padx=(6, 0))
        ToolTip(self._unified_filters_btn,
                "Object type, catalogs (NGC/IC/Messier/\nCaldwell/Sharpless), min altitude, max\n"
                "magnitude, and seasonal-only — all in one place.")
        self._update_unified_filters_btn_label()

        self._grid_hint_var = tk.StringVar(value="")
        ttk.Label(parent, textvariable=self._grid_hint_var,
                  font=("Helvetica", 9), foreground="#778899").pack(fill="x", pady=(0, 6))

        grid_frame = ttk.Frame(parent)
        grid_frame.pack(fill="both", expand=True)
        self._grid_canvas = tk.Canvas(grid_frame, bg="#0e1a28", highlightthickness=0)
        grid_scroll = ttk.Scrollbar(grid_frame, orient="vertical",
                                     command=self._grid_canvas.yview)
        self._grid_inner = tk.Frame(self._grid_canvas, bg="#0e1a28")
        self._grid_inner.bind("<Configure>",
            lambda e: self._grid_canvas.configure(scrollregion=self._grid_canvas.bbox("all")))
        self._grid_canvas.create_window((0, 0), window=self._grid_inner, anchor="nw", tags="inner")
        self._grid_canvas.bind("<Configure>",
            lambda e: self._grid_canvas.itemconfig("inner", width=e.width))
        self._grid_canvas.configure(yscrollcommand=grid_scroll.set)
        self._grid_canvas.pack(side="left", fill="both", expand=True)
        grid_scroll.pack(side="right", fill="y")
        self._bind_plan_mousewheel(self._grid_canvas)
        self._bind_plan_mousewheel(self._grid_inner)

        # First fill happens once the tab is actually visible (see
        # _on_tab_changed), same lazy-load pattern as the rest of the app.

    # ── Plan tab search (shares the Planner's floating suggestion popup,
    # same pattern as _explore_on_search_key / _explore_select_suggestion) ──

    _PLAN_SEARCH_PLACEHOLDER = "Search catalog…"

    def _plan_search_show_placeholder(self):
        """Show the grey "Search catalog…" hint text — only when the box
        is actually empty, so this never clobbers real (possibly emptied-
        then-refocused) user input."""
        if self.plan_search.get():
            return
        self._plan_search_placeholder_active = True
        self.plan_search.insert(0, self._PLAN_SEARCH_PLACEHOLDER)
        self.plan_search.configure(style="PlanSearchPlaceholder.TEntry")

    def _plan_search_clear_placeholder(self):
        """Clear the placeholder (called on focus-in) and restore the
        normal (padded) entry style so typed text tracks the current
        day/night color instead of staying grey."""
        if self._plan_search_placeholder_active:
            self._plan_search_placeholder_active = False
            self.plan_search.delete(0, tk.END)
            self.plan_search.configure(style="PlanSearch.TEntry")

    def _plan_search_on_focus_out(self, event):
        """Focus left the search box — keep the existing suggestion-popup
        dismissal, and re-show the placeholder if the box was left empty."""
        self.root.after(150, self._hide_suggestions)
        if not self.plan_search.get():
            self._plan_search_show_placeholder()

    def _plan_on_search_key(self, event):
        """Key release in the Plan tab's catalog search — drive the shared
        floating suggestion popup."""
        if event.keysym in ("Up", "Down", "Return", "Escape", "Tab"):
            return
        self._update_floating_suggestions(entry=self.plan_search,
                                           on_select=self._plan_select_suggestion)

    def _plan_select_suggestion(self, target_id):
        """Suggestion clicked — open that target straight into the Frame
        dialog, whether or not it's in tonight's visible-grid list."""
        self._hide_suggestions()
        self.plan_search.delete(0, tk.END)
        self._frame_from_card(target_id)

    def _plan_search_submit(self):
        """Enter pressed in the Plan tab's search box — same catalog-key
        normalization the Planner tab's search uses, then opens straight
        into Frame (_frame_from_card does its own not-found warning)."""
        raw = self.plan_search.get().strip().upper().replace(" ", "")
        if not raw:
            return
        raw = self._normalize_catalog_key(raw)
        self.plan_search.delete(0, tk.END)
        self._frame_from_card(raw)

    # ── Plan tab's consolidated "⚙ Filters" popover ─────────────────────
    # Object Type, Catalogs (NGC/IC/Messier/Caldwell/Sharpless/Other), and
    # Min Altitude / Max Magnitude / Seasonal — one popup instead of the
    # three separate controls (▽ catalog button, Type dropdown, "Filters
    # ▾" button) this replaces, per the approved "Option C" consolidation.
    # Embedded Frame + .place(), not a Toplevel — two overrideredirect
    # Toplevels (this popup and a tooltip) fight over z-order on macOS.

    def _toggle_unified_filters_popup(self):
        """Show or hide the consolidated Filters popup."""
        if self._unified_filters_popup and self._unified_filters_popup.winfo_exists():
            self._close_unified_filters_popup()
            return
        self._show_unified_filters_popup()

    def _show_unified_filters_popup(self):
        """Build the consolidated Filters popup beneath the Filters button.

        Catalog checkboxes and the Type combobox are rebuilt fresh each
        time the popup opens (cheap, and avoids any dangling-widget
        bookkeeping between opens) — the same pattern the old standalone
        catalog-filter popup already used.
        """
        if self._unified_filters_popup and self._unified_filters_popup.winfo_exists():
            self._close_unified_filters_popup()

        nm = getattr(self, "night_mode", False)
        bg_panel   = "#2a0000" if nm else "#1e2d3e"
        bg_active  = "#330000" if nm else "#263545"
        bg_field   = "#1a0000" if nm else "#132233"
        border_col = "#882222" if nm else "#2e4a63"
        fg_primary = "#cc0000" if nm else "#cbd9e5"
        fg_dim     = "#882222" if nm else "#6a8aa8"
        fg_muted   = "#552222" if nm else "#445566"

        popup = tk.Frame(self.root, bg=bg_panel,
                          highlightthickness=1, highlightbackground=border_col)
        self._unified_filters_popup = popup

        self.root.update_idletasks()
        self._unified_filters_btn.update_idletasks()
        btn_x = self._unified_filters_btn.winfo_rootx() - self.root.winfo_rootx()
        btn_y = self._unified_filters_btn.winfo_rooty() - self.root.winfo_rooty()
        btn_h = self._unified_filters_btn.winfo_height()
        btn_w = self._unified_filters_btn.winfo_width()
        popup_w = 230
        # Right-align under the button — it sits at the toolbar's right
        # edge, so left-aligning could run the popup off-window.
        popup.place(x=btn_x + btn_w - popup_w, y=btn_y + btn_h + 2, width=popup_w, height=440)
        popup.lift()

        # ── Object Type ──────────────────────────────────────────────
        tk.Label(popup, text="OBJECT TYPE", bg=bg_panel, fg=fg_dim,
                 font=("Helvetica", 8, "bold")).pack(anchor="w", padx=10, pady=(8, 4))
        type_combo = ttk.Combobox(popup, textvariable=self._grid_type_var,
                                   values=OBJECT_TYPE_FILTERS, state="readonly")
        type_combo.pack(fill="x", padx=10, pady=(0, 8))
        type_combo.bind("<<ComboboxSelected>>", lambda e: self._on_unified_filters_change())

        tk.Frame(popup, bg=border_col, height=1).pack(fill="x", padx=10, pady=(0, 8))

        # ── Catalogs ──────────────────────────────────────────────────
        cat_header = tk.Frame(popup, bg=bg_panel)
        cat_header.pack(fill="x", padx=10, pady=(0, 4))
        tk.Label(cat_header, text="CATALOGS", bg=bg_panel, fg=fg_dim,
                 font=("Helvetica", 8, "bold")).pack(side="left")
        none_lbl = tk.Label(cat_header, text="None", bg=bg_panel, fg=fg_dim,
                             font=("Helvetica", 8, "underline"), cursor="hand2")
        none_lbl.pack(side="right")
        none_lbl.bind("<Button-1>", lambda e: self._set_all_catalog_filters(False))
        self._bind_link_hover(none_lbl, normal=fg_dim, hover=fg_primary)
        tk.Label(cat_header, text="·", bg=bg_panel, fg=fg_muted,
                 font=("Helvetica", 8)).pack(side="right", padx=5)
        all_lbl = tk.Label(cat_header, text="All", bg=bg_panel, fg=fg_dim,
                            font=("Helvetica", 8, "underline"), cursor="hand2")
        all_lbl.pack(side="right")
        all_lbl.bind("<Button-1>", lambda e: self._set_all_catalog_filters(True))
        self._bind_link_hover(all_lbl, normal=fg_dim, hover=fg_primary)

        counts = self._count_targets_by_catalog()
        cat_order = ["NGC", "IC", "Messier", "Caldwell", "Sharpless", "Other"]
        self._catalog_filter_vars = {}
        for cat in cat_order:
            row = tk.Frame(popup, bg=bg_panel, cursor="hand2")
            row.pack(fill="x", padx=4, pady=1)
            var = tk.BooleanVar(value=self.catalog_filter.get(cat, True))
            self._catalog_filter_vars[cat] = var
            cb = tk.Checkbutton(row, text=cat, variable=var,
                                 bg=bg_panel, fg=fg_primary, selectcolor=bg_panel,
                                 activebackground=bg_active, activeforeground=fg_primary,
                                 font=("Helvetica", 10), anchor="w", padx=4,
                                 command=lambda c=cat: self._on_catalog_filter_change(c))
            cb.pack(side="left", padx=(4, 0))
            count = counts.get(cat, 0)
            tk.Label(row, text=f"{count:,}", bg=bg_panel, fg=fg_dim,
                      font=("Helvetica", 9)).pack(side="right", padx=(0, 10))

        tk.Frame(popup, bg=border_col, height=1).pack(fill="x", padx=10, pady=(6, 8))

        # ── Min Altitude / Max Magnitude / Seasonal ──────────────────
        tk.Label(popup, text="OTHER", bg=bg_panel, fg=fg_dim,
                 font=("Helvetica", 8, "bold")).pack(anchor="w", padx=10, pady=(0, 4))

        row1 = tk.Frame(popup, bg=bg_panel)
        row1.pack(fill="x", padx=10, pady=(0, 6))
        tk.Label(row1, text="Min. Altitude", bg=bg_panel, fg=fg_primary,
                 font=("Helvetica", 10)).pack(side="left")
        tk.Label(row1, text="°", bg=bg_panel, fg=fg_dim,
                 font=("Helvetica", 10)).pack(side="right")
        alt_entry = tk.Entry(row1, textvariable=self._grid_min_alt_var, width=4,
                              bg=bg_field, fg=fg_primary, insertbackground=fg_primary,
                              relief="flat", highlightthickness=1, highlightbackground=border_col)
        alt_entry.pack(side="right", padx=(0, 4))
        alt_entry.bind("<Return>", lambda e: self._on_unified_filters_change())
        alt_entry.bind("<FocusOut>", lambda e: self._on_unified_filters_change())

        row2 = tk.Frame(popup, bg=bg_panel)
        row2.pack(fill="x", padx=10, pady=(0, 2))
        tk.Label(row2, text="Max Magnitude", bg=bg_panel, fg=fg_primary,
                 font=("Helvetica", 10)).pack(side="left")
        mag_entry = tk.Entry(row2, textvariable=self._grid_mag_limit_var, width=4,
                              bg=bg_field, fg=fg_primary, insertbackground=fg_primary,
                              relief="flat", highlightthickness=1, highlightbackground=border_col)
        mag_entry.pack(side="right")
        mag_entry.bind("<Return>", lambda e: self._on_unified_filters_change())
        mag_entry.bind("<FocusOut>", lambda e: self._on_unified_filters_change())
        ToolTip(mag_entry, "Leave blank to show all magnitudes.")

        surf_cb = tk.Checkbutton(popup, text="Use surface brightness",
                                  variable=self._grid_use_surf_br_var,
                                  bg=bg_panel, fg=fg_primary, selectcolor=bg_panel,
                                  activebackground=bg_panel, activeforeground=fg_primary,
                                  font=("Helvetica", 9), anchor="w",
                                  command=self._on_unified_filters_change)
        surf_cb.pack(fill="x", padx=6, pady=(0, 4))

        tk.Frame(popup, bg=border_col, height=1).pack(fill="x", padx=10, pady=(2, 6))

        season_cb = tk.Checkbutton(popup, text="Seasonal targets only",
                                    variable=self._grid_seasonal_var,
                                    bg=bg_panel, fg=fg_primary, selectcolor=bg_panel,
                                    activebackground=bg_panel, activeforeground=fg_primary,
                                    font=("Helvetica", 9), anchor="w",
                                    command=self._on_unified_filters_change)
        # Disabled while the "🔭 All" mode is active — seasonal is itself a
        # curated tonight-relevant subset, which fights "show me everything".
        if self._grid_mode_var.get() == "all":
            season_cb.config(state="disabled")
        season_cb.pack(fill="x", padx=6, pady=(0, 8))
        self._grid_seasonal_cb = season_cb

        # Same Cocoa Tk paint bug _kick_paint already works around elsewhere
        # (see its docstring) — freshly-created Entry widgets on macOS can
        # stay blank until the pointer physically enters them. Kick the two
        # text fields right away instead of waiting for the user's mouse.
        self._kick_paint([alt_entry, mag_entry])

        self._unified_filters_click_binding = self.root.bind(
            "<Button-1>", self._maybe_close_unified_filters_popup, add="+")
        self._unified_filters_escape_binding = self.root.bind(
            "<Escape>", lambda e: self._close_unified_filters_popup(), add="+")

    def _maybe_close_unified_filters_popup(self, event):
        """Close the unified filters popup if the click landed outside it
        (and outside the Filters button, which toggles it itself)."""
        popup = self._unified_filters_popup
        if not popup or not popup.winfo_exists():
            return
        px, py = popup.winfo_rootx(), popup.winfo_rooty()
        pw, ph = popup.winfo_width(), popup.winfo_height()
        if px <= event.x_root <= px + pw and py <= event.y_root <= py + ph:
            return
        if hasattr(self, "_unified_filters_btn"):
            btn = self._unified_filters_btn
            bx, by = btn.winfo_rootx(), btn.winfo_rooty()
            bw, bh = btn.winfo_width(), btn.winfo_height()
            if bx <= event.x_root <= bx + bw and by <= event.y_root <= by + bh:
                return
        self._close_unified_filters_popup()

    def _close_unified_filters_popup(self):
        """Tear down the unified filters popup and its root-level bindings."""
        if getattr(self, "_unified_filters_click_binding", None):
            try:
                self.root.unbind("<Button-1>", self._unified_filters_click_binding)
            except (tk.TclError, AttributeError):
                pass
            self._unified_filters_click_binding = None
        if getattr(self, "_unified_filters_escape_binding", None):
            try:
                self.root.unbind("<Escape>", self._unified_filters_escape_binding)
            except (tk.TclError, AttributeError):
                pass
            self._unified_filters_escape_binding = None
        if self._unified_filters_popup and self._unified_filters_popup.winfo_exists():
            self._unified_filters_popup.destroy()
        self._unified_filters_popup = None

    def _on_unified_filters_change(self):
        """Any control in the unified Filters popup changed — validate,
        refresh the Filters button's active-indicator dot, and re-scan."""
        try:
            float(self._grid_min_alt_var.get())
        except (TypeError, ValueError):
            self._grid_min_alt_var.set(str(int(self.data.get("settings", {}).get("min_alt", 20))))
        mag_txt = self._grid_mag_limit_var.get().strip()
        if mag_txt:
            try:
                float(mag_txt)
            except ValueError:
                self._grid_mag_limit_var.set("")
        self._update_unified_filters_btn_label()
        self._refresh_visible_grid()

    def _update_unified_filters_btn_label(self):
        """Light up the Filters button's "•" indicator when any filter is
        off its default: object type, catalogs, min-altitude setting, mag
        limit, surface-brightness toggle, or seasonal-only.

        Seasonal-only is excluded from this check while "🔭 All" mode is
        active — it's forced off by the mode itself, not a user choice, so
        it shouldn't make the Filters button look "active" on its own.
        """
        if not hasattr(self, "_unified_filters_btn"):
            return
        default_alt = str(int(self.data.get("settings", {}).get("min_alt", 20)))
        in_all_mode = getattr(self, "_grid_mode_var", None) is not None \
            and self._grid_mode_var.get() == "all"
        catalogs_restricted = self.catalog_filter is not None and \
            not all(self.catalog_filter.values())
        type_restricted = getattr(self, "_grid_type_var", None) is not None \
            and self._grid_type_var.get() != "All Types"
        active = (type_restricted
                  or catalogs_restricted
                  or self._grid_min_alt_var.get().strip() != default_alt
                  or bool(self._grid_mag_limit_var.get().strip())
                  or self._grid_use_surf_br_var.get()
                  or (not self._grid_seasonal_var.get() and not in_all_mode))
        self._unified_filters_btn.config(text="⚙ Filters • ▾" if active else "⚙ Filters ▾")

    def _set_grid_mode(self, mode):
        """Switch the Plan tab grid between "tonight" (altitude/seasonal
        gated) and "all" (free browse — every catalog object regardless of
        tonight's altitude, so a search result or a target you're planning
        ahead for stays on the list instead of disappearing)."""
        if mode == self._grid_mode_var.get():
            return
        self._grid_mode_var.set(mode)
        if mode == "all":
            self._grid_prior_seasonal = self._grid_seasonal_var.get()
            self._grid_seasonal_var.set(False)
        else:
            self._grid_seasonal_var.set(getattr(self, "_grid_prior_seasonal", True))
        seasonal_cb = getattr(self, "_grid_seasonal_cb", None)
        if seasonal_cb is not None and seasonal_cb.winfo_exists():
            seasonal_cb.config(state="disabled" if mode == "all" else "normal")
        self._update_unified_filters_btn_label()
        self._update_grid_mode_seg()
        self._refresh_visible_grid()

    def _update_grid_mode_seg(self):
        """Restyle the Tonight/All segment labels to match the active mode."""
        if not hasattr(self, "_grid_seg_tonight"):
            return
        mode = self._grid_mode_var.get()
        for lbl, key in ((self._grid_seg_tonight, "tonight"), (self._grid_seg_all, "all")):
            if mode == key:
                lbl.config(bg="#38bdf8", fg="#0e1a28")
            else:
                lbl.config(bg="#1a2b3c", fg="#556677")

    def _update_rig_chip_label(self):
        """Refresh the Plan tab's rig-chip text to match the active rig."""
        if not hasattr(self, "_rig_chip_btn"):
            return
        name = self.rig_choice.get() or "Custom…"
        self._rig_chip_btn.config(text=f"⚙ {name}")

    def _select_rig_chip(self, name):
        """Apply a saved rig chosen from the equipment drawer's chip row.

        Setting the StringVar alone doesn't fire <<ComboboxSelected>> (that
        only fires on user interaction with the combobox widget itself), so
        this calls the same handler the dropdown uses to actually apply the
        rig's equipment snapshot."""
        self.rig_choice.set(name)
        self._on_rig_selected()
        self._close_equip_drawer()
        self._open_equip_drawer()   # rebuild so the chip row highlights the new active rig

    def _toggle_equip_drawer(self):
        if getattr(self, "_equip_drawer", None) is not None:
            self._close_equip_drawer()
        else:
            self._open_equip_drawer()

    def _open_equip_drawer(self):
        """Open the equipment slide-in drawer (Option 2, approved) over the
        right edge of the Plan tab.

        Earlier version of this method tried to re-parent the Planner tab's
        actual scope/reducer/camera/filter widgets into the drawer via
        pack(in_=...). That fails at the Tk level: pack's "-in" target must be
        the widget's real parent or a descendant of it, and the drawer lives
        under a completely different tab frame — Tk raises
        "can't pack X inside Y" the moment it's tried (confirmed by
        reproducing it directly against this method). So instead, the drawer
        gets its own Combobox widgets bound to the *same* StringVars
        (self.scope_choice, self.reduction_factor, self.camera_choice,
        self.filter_mode) that the Planner tab's chips
        use — Tkinter keeps every widget sharing a StringVar in sync
        automatically, so there's still exactly one source of truth for the
        selected value, just two on-screen views of it. Dropdown *options*
        (values=) are copied from the Planner tab's widgets at open time and
        kept in sync by refresh_dropdowns() while the drawer is open (same
        pattern already used for the Explore tab's own scope/camera
        dropdowns).

        Bortle (sky darkness) is deliberately NOT shown here — it isn't
        equipment, it's a single global "sky conditions" setting that lives
        in the header, and this drawer is scoped to "ACTIVE EQUIPMENT" /
        rig settings. Showing a second view of it under a rig's name would
        wrongly imply it's part of that rig.

        Known simplification: this appears/disappears instantly rather than
        sliding, and there's no dimmed "scrim" over the rest of the tab like
        the mockup — both are cosmetic and cheap to add later if wanted.
        """
        if getattr(self, "_equip_drawer", None) is not None or not hasattr(self, "_plan_split"):
            return

        DRAWER_W = 330
        drawer = tk.Frame(self._plan_split, bg="#131f2e",
                           highlightthickness=1, highlightbackground="#2e4a63")
        drawer.place(relx=1.0, rely=0.0, anchor="ne", relheight=1.0, width=DRAWER_W)
        drawer.lift()
        self._equip_drawer = drawer
        # Resolve the place() geometry now, synchronously — otherwise a
        # drawer rebuilt immediately after a prior one closes (e.g. from
        # _select_rig_chip) can get children packed before Tk has computed
        # this frame's actual height from relheight, and pack silently
        # declines to map anything that doesn't yet fit.
        self.root.update_idletasks()

        head = tk.Frame(drawer, bg="#131f2e")
        head.pack(fill="x", padx=16, pady=(14, 10))
        tk.Label(head, text="ACTIVE EQUIPMENT", bg="#131f2e", fg="#7eb8d4",
                 font=("Helvetica", 10, "bold")).pack(side="left")
        tk.Button(head, text="✕", relief="flat", bd=0, bg="#131f2e", fg="#778899",
                  activebackground="#131f2e", activeforeground="#ffffff",
                  cursor="hand2", command=self._close_equip_drawer).pack(side="right")

        body = tk.Frame(drawer, bg="#131f2e")
        body.pack(fill="both", expand=True, padx=16)

        tk.Label(body, text="SAVED RIGS", bg="#131f2e", fg="#556677",
                 font=("Helvetica", 8, "bold")).pack(anchor="w", pady=(0, 8))
        chips_wrap = tk.Frame(body, bg="#131f2e")
        chips_wrap.pack(fill="x", pady=(0, 16))

        # Chips wrap onto additional rows instead of overflowing the fixed
        # drawer width — plenty of vertical room, no reason to let a rig
        # with several saved chips get clipped. Tk's pack/grid don't wrap
        # on their own, so this measures each chip's rendered width up
        # front (via tkfont, before the widget is ever created — a Label's
        # master can't be changed after construction, so we have to know
        # which row a chip belongs to before building it) and starts a new
        # row Frame whenever the next chip would overflow.
        CHIP_FONT = ("Helvetica", 9)
        CHIP_GAP  = 6
        chip_font_obj = tkfont.Font(family=CHIP_FONT[0], size=CHIP_FONT[1])
        # DRAWER_W matches the width= passed to drawer.place() above; body's
        # own padx=16 (both sides) is the only other thing eating into it.
        available_w = DRAWER_W - 2 * 16 - 6   # small safety margin

        def _chip_width(text, bordered=False):
            # padx=10 both sides (Label's padx param) + ~2px for chips that
            # also carry a 1px highlightthickness border (the "+ New rig" chip).
            return chip_font_obj.measure(text) + 20 + (2 if bordered else 0)

        rows = [tk.Frame(chips_wrap, bg="#131f2e")]
        rows[0].pack(fill="x", anchor="w")
        row_w = [0]

        def _place_chip(name, bordered=False, **label_kwargs):
            w = _chip_width(name, bordered) + CHIP_GAP
            if row_w[0] > 0 and row_w[0] + w > available_w:
                rows.append(tk.Frame(chips_wrap, bg="#131f2e"))
                rows[-1].pack(fill="x", anchor="w")
                row_w[0] = 0
            # A single rig name long enough to exceed the whole drawer width
            # has nowhere to wrap TO — starting a new row wouldn't help a
            # chip that's wider than the drawer itself. Wrap its own text
            # internally instead of letting it overflow sideways.
            wrap_kwargs = {}
            if w - CHIP_GAP > available_w:
                wrap_kwargs["wraplength"] = available_w - 20
                wrap_kwargs["justify"] = "left"
            chip = tk.Label(rows[-1], text=name, font=CHIP_FONT,
                             padx=10, pady=4, cursor="hand2", **wrap_kwargs, **label_kwargs)
            chip.pack(side="left", padx=(0, CHIP_GAP), pady=(0, 6))
            row_w[0] += w
            return chip

        active_rig = self.rig_choice.get()
        for name in self.rig_dropdown.cget("values"):
            is_active = (name == active_rig)
            chip = _place_chip(name,
                                bg=("#0f2233" if is_active else "#1a2836"),
                                fg=("#7eb8d4" if is_active else "#aabbcc"))
            chip.bind("<Button-1>", lambda e, n=name: self._select_rig_chip(n))
        add_chip = _place_chip("+ New rig", bordered=True,
                                bg="#131f2e", fg="#556677",
                                highlightthickness=1, highlightbackground="#2e4a63")
        add_chip.bind("<Button-1>", lambda e: self._open_save_rig_dialog())

        tk.Label(body, text=f"{active_rig or 'CUSTOM'} SETTINGS".upper(),
                 bg="#131f2e", fg="#556677", font=("Helvetica", 8, "bold")
                 ).pack(anchor="w", pady=(0, 8))
        field_stack = tk.Frame(body, bg="#131f2e")
        field_stack.pack(fill="x")

        def _row(label, values, var):
            r = tk.Frame(field_stack, bg="#1a2836")
            r.pack(fill="x", pady=(0, 6))
            tk.Label(r, text=label, bg="#1a2836", fg="#7a8a9a",
                     font=("Helvetica", 8), width=8, anchor="w").pack(
                     side="left", padx=(8, 4), pady=6)
            dd = ttk.Combobox(r, textvariable=var, values=values, state="readonly")
            dd.pack(side="left", fill="x", expand=True, padx=(0, 8), pady=4)
            return dd

        self._drawer_scope_dd  = _row("Scope",   self.scope_dropdown.cget("values"),  self.scope_choice)
        self._drawer_reduc_dd  = _row("Reducer", self.reduction_entry.cget("values"), self.reduction_factor)
        self._drawer_camera_dd = _row("Camera",  self.camera_dropdown.cget("values"), self.camera_choice)

        # Filter row/combobox — built here but shown only for mono cameras;
        # toggle_filter_visibility (already trace-bound to camera_choice)
        # packs/forgets it, same rule as the Planner tab's own filter chip.
        self._drawer_filter_row = tk.Frame(field_stack, bg="#1a2836")
        tk.Label(self._drawer_filter_row, text="Filter", bg="#1a2836", fg="#7a8a9a",
                 font=("Helvetica", 8), width=8, anchor="w").pack(
                 side="left", padx=(8, 4), pady=6)
        self._drawer_filter_dd = ttk.Combobox(
            self._drawer_filter_row, textvariable=self.filter_mode,
            values=self.filter_dropdown.cget("values"), state="readonly")
        self._drawer_filter_dd.pack(side="left", fill="x", expand=True, padx=(0, 8), pady=4)
        self.toggle_filter_visibility()

        foot = tk.Frame(drawer, bg="#131f2e")
        foot.pack(fill="x", padx=16, pady=14)
        ttk.Button(foot, text="Manage Rigs",
                   command=self._open_manage_rigs_dialog).pack(side="left")
        ttk.Button(foot, text="Done", command=self._close_equip_drawer).pack(side="right")

    def _close_equip_drawer(self):
        """Destroy the drawer and its widgets. Nothing needs to be handed
        back anywhere — the Planner tab's own chip widgets were never
        touched (see _open_equip_drawer's note on why)."""
        drawer = getattr(self, "_equip_drawer", None)
        if drawer is None:
            return
        self._drawer_filter_row = None
        drawer.destroy()
        self._equip_drawer = None

    def setup_plan_tab(self):
        """Build the merged Plan tab: a live "Visible Tonight" browsing
        grid (left) beside the Tonight's Plan rail (right), with a
        full-width collapsible schedule-timeline strip below both —
        the Gantt/drag-to-schedule timeline and the reorder tools that
        used to live on the (now retired) Targets List tab, reattached
        to tonight's committed plan (_plan_entries) instead of the old
        pre-commit queue. "Add to Tonight" commits straight to
        _plan_entries — no queue, no tab jump (see
        _add_target_to_plan_direct)."""
        tab_outer = ttk.Frame(self.tab_plan)
        tab_outer.pack(fill="both", expand=True)

        # ── Full-width collapsible schedule-timeline strip ─────────────────
        # Packed BEFORE `split` below, and to the bottom, so it reserves its
        # own height first — the same pack-order lesson as the plan card
        # list's scrollbar (see the comment further down): the sibling
        # packed first with expand=True would otherwise claim the entire
        # cavity and leave nothing for this strip.
        self._gantt_strip = tk.Frame(tab_outer, bg="#050810", highlightthickness=1,
                                      highlightbackground="#2e4a63")

        gantt_header = tk.Frame(self._gantt_strip, bg="#050810")
        gantt_header.pack(fill="x", padx=12, pady=(6, 2))
        tk.Label(gantt_header, text="SCHEDULE TIMELINE", bg="#050810", fg="#7eb8d4",
                 font=("Helvetica", 9, "bold")).pack(side="left")
        tk.Label(gantt_header, text="·  drag a bar to set its start time",
                 bg="#050810", fg="#556677", font=("Helvetica", 9)).pack(side="left", padx=(6, 0))
        self._gantt_collapse_btn = tk.Label(gantt_header, text="⌄ collapse", bg="#050810",
                 fg="#38bdf8", font=("Helvetica", 9, "underline"), cursor="hand2")
        self._gantt_collapse_btn.pack(side="right")
        self._gantt_collapse_btn.bind("<Button-1>", lambda e: self._toggle_gantt_strip())

        self._gantt_body = tk.Frame(self._gantt_strip, bg="#050810")
        self._gantt_body.pack(fill="x")
        self._gantt_collapsed = False

        self.queue_gantt = tk.Canvas(self._gantt_body, height=190, bg="#050810",
                                     highlightthickness=0)
        self.queue_gantt.pack(fill="x", padx=12, pady=(0, 2))
        self._gantt_note_lbl = ttk.Label(self._gantt_body, text="",
                                         font=("Helvetica", 9, "italic"), foreground="#cc8800")
        self._gantt_note_lbl.pack(anchor="w", padx=12, pady=(0, 8))

        self.queue_gantt.bind("<ButtonPress-1>",   self._gantt_press)
        self.queue_gantt.bind("<B1-Motion>",       self._gantt_drag)
        self.queue_gantt.bind("<ButtonRelease-1>", self._gantt_release)
        self.queue_gantt.bind("<Motion>",          self._gantt_motion)
        self.queue_gantt.bind("<Configure>",       lambda e: self._draw_queue_gantt())
        self.queue_gantt.bind("<Map>",             lambda e: self.root.after(20, self._draw_queue_gantt))

        self._gantt_strip.pack(side="bottom", fill="x")

        split = ttk.Frame(tab_outer)
        split.pack(side="top", fill="both", expand=True)
        self._plan_split = split   # equipment drawer (Phase 3) overlays this

        leftpane = ttk.Frame(split)
        leftpane.pack(side="left", fill="both", expand=True, padx=(20, 10), pady=10)
        self._build_plan_left_pane(leftpane)

        rightpane = ttk.Frame(split, width=380)
        rightpane.pack(side="left", fill="y", padx=(0, 20), pady=10)
        rightpane.pack_propagate(False)

        outer = rightpane   # rightpane is already packed above; everything
                             # below just parents its widgets to it, same as
                             # the original single-column layout did to
                             # the old top-level `outer` frame.

        # Empty-state message (shown when no entries)
        self._plan_empty_label = ttk.Label(outer,
            text="No targets in tonight's plan yet.\n\n"
                 "Add targets from the grid on the left using\n"
                 "\"+ Add to Tonight\", or frame one first.",
            font=("Helvetica", 12), foreground="#556677", justify="center")
        self._plan_empty_label.pack(pady=(60, 0))

        # Scrollable card container (replaces ttk.Treeview). NOT packed here
        # — see the bottom-anchored action cluster comment further down,
        # where this is packed last, deliberately, alongside totals_frame/
        # reorder_frame/icon_row.
        tree_frame = ttk.Frame(outer)
        self._plan_tree_frame = tree_frame

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
        # Pack the scrollbar BEFORE the canvas.  With side="left"/expand=True
        # packed first, Tk's packer greedily hands the expanding canvas the
        # *entire* cavity and only then tries to fit the scrollbar into
        # whatever's left — squeezing it down to ~1px instead of its real
        # ~15px, and (worse) leaving the canvas a few px wider than it should
        # be, which is what let card content spill past the rail's edge.
        # Packing the non-expanding scrollbar first reserves its natural
        # width up front, so the canvas correctly gets the remainder.
        self._plan_cards_scrollbar.pack(side="right", fill="y")
        self._plan_cards_canvas.pack(side="left", fill="both", expand=True)

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
        self._plan_drag = None

        # Keep plan_tree as dummy for hasattr checks
        self.plan_tree = None

        # Totals row (not packed yet — see the bottom-anchored action
        # cluster comment below)
        totals_frame = ttk.Frame(outer)
        self._plan_total_alloc_var = tk.StringVar(value="")
        self._plan_total_int_var   = tk.StringVar(value="")
        self._plan_total_subs_var  = tk.StringVar(value="")
        ttk.Label(totals_frame, text="Totals:", font=("Helvetica", 11, "bold")).pack(side="left", padx=(0, 10))
        ttk.Label(totals_frame, textvariable=self._plan_total_alloc_var, font=("Helvetica", 11)).pack(side="left", padx=(0, 14))
        ttk.Label(totals_frame, textvariable=self._plan_total_int_var,   font=("Helvetica", 11)).pack(side="left", padx=(0, 14))
        ttk.Label(totals_frame, textvariable=self._plan_total_subs_var,  font=("Helvetica", 11)).pack(side="left")

        # Reorder controls — act on the selected card above, mirroring the
        # retired queue's Move Up/Down + Order by Transit.
        reorder_frame = ttk.Frame(outer)  # not packed yet — see below
        up_btn = ttk.Button(reorder_frame, text="↑", width=3, command=self._plan_move_up)
        up_btn.pack(side="left", padx=(0, 4))
        ToolTip(up_btn, "Move the selected plan entry\nearlier in the list.")
        down_btn = ttk.Button(reorder_frame, text="↓", width=3, command=self._plan_move_down)
        down_btn.pack(side="left", padx=(0, 10))
        ToolTip(down_btn, "Move the selected plan entry\nlater in the list.")
        order_btn = ttk.Button(reorder_frame, text="★ Order by Transit",
                               command=self._plan_suggest_order)
        order_btn.pack(side="left")
        ToolTip(order_btn, "Sort tonight's plan by each\ntarget's meridian transit time.")

        # No standalone Remove control here anymore — each card now carries
        # its own 🗑 icon (see _refresh_plan_tree), which covers removal
        # without needing a card selected first, so a second copy of the
        # same action on this row was redundant.

        # Action buttons — an icon-only toolbar instead of six labeled
        # buttons. This pane is a fixed 380px rail (it used to be Tonight's
        # Plan's whole tab, with the full window width to work with); six
        # full-width buttons either got clipped or, once split across rows
        # to avoid that, looked crowded. Icons + hover tooltips cover the
        # same six actions (Remove is now above, on the reorder row) in a
        # single tidy row.
        icon_row = ttk.Frame(outer)  # not packed yet — see below
        self._make_icon_button(icon_row, "✕", self._clear_plan,
            "Remove all entries from\ntonight's plan.")
        ttk.Separator(icon_row, orient="vertical").pack(side="left", fill="y", padx=4)
        self._make_icon_button(icon_row, "💾", self._save_session_as,
            "Save tonight's plan to a named\n.json file for future reference.")
        self._make_icon_button(icon_row, "📂", self._load_session,
            "Load a previously saved session\ninto tonight's plan.")
        ttk.Separator(icon_row, orient="vertical").pack(side="left", fill="y", padx=4)
        self._make_icon_button(icon_row, "📄", self._export_session,
            "Save tonight's plan as a text or HTML report.\nThe HTML report embeds a per-target altitude chart;\nchoose the format in the Save dialog's file-type list.\nPrint the HTML to PDF from your browser if needed.")
        self._make_icon_button(icon_row, "🔭", self._export_nina,
            "Export tonight's plan as a NINA target set\n(.ninaTargetSet) for import into N.I.N.A.\nEach target becomes a CaptureSequenceList\nwith its sub-exposure and sub-count filled in.")

        # ── Bottom-anchored action cluster + expanding card list ───────────
        # Packed here, as one block, now that every widget above exists —
        # and in this specific side/order combination, so the always-needed
        # action rows (totals, reorder, icon toolbar) reserve their natural
        # height FIRST, no matter how little vertical room `outer` actually
        # has. Tk's packer carves cavity strictly in packing-call order, so
        # a side="top" tree_frame packed EARLY (as this used to do) claims
        # its own natural size but leaves whatever's left over for whoever
        # comes next; when the plan is empty, the empty-state label's extra
        # ~110px was enough to starve totals_frame/reorder_frame/icon_row
        # down to a sliver (icon_row measured a literal 1x1 px parcel —
        # Jerry: "the buttons shrink to about 1/4 of their height and you
        # can't see the text or icons on them"). Packing these three with
        # side="bottom", in REVERSE visual order (icon_row first so it lands
        # flush at the very bottom, then reorder_frame just above it, then
        # totals_frame above that), reserves their combined height up front;
        # tree_frame (side="top", fill="both", expand=True), packed LAST,
        # then simply takes whatever vertical space remains above them —
        # shrinking gracefully (it already scrolls) instead of the reverse.
        icon_row.pack(side="bottom", fill="x", pady=(4, 4))
        reorder_frame.pack(side="bottom", fill="x", pady=(0, 4))
        totals_frame.pack(side="bottom", fill="x", pady=(4, 4))
        tree_frame.pack(fill="both", expand=True)

    def _make_icon_button(self, parent, icon, command, tooltip_text,
                          base_fg="#7eb8d4", hover_fg="#ffffff"):
        """Build one icon-only "button" (a plain Label with a click binding
        and a hover brighten) for the Plan tab's action toolbar and similar
        icon rows. Returns the Label so callers can restyle it further if
        needed."""
        bg = ttk.Style().lookup("TFrame", "background") or "#1e2d3e"
        lbl = tk.Label(parent, text=icon, bg=bg, fg=base_fg,
                      font=("Helvetica", 13), cursor="hand2", padx=7, pady=3)
        lbl.pack(side="left", padx=(0, 2))
        lbl.bind("<Button-1>", lambda e: command())
        lbl.bind("<Enter>", lambda e: lbl.config(fg=hover_fg))
        lbl.bind("<Leave>", lambda e: lbl.config(fg=base_fg))
        ToolTip(lbl, tooltip_text)
        return lbl

    def _toggle_gantt_strip(self):
        """Collapse/expand the full-width schedule-timeline strip.

        Collapsing hides _gantt_body (canvas + note label) but leaves the
        header (with the collapse/expand toggle) visible. _draw_queue_gantt
        already bails out early via winfo_ismapped() when collapsed, so no
        further guard is needed here.
        """
        self._gantt_collapsed = not self._gantt_collapsed
        if self._gantt_collapsed:
            self._gantt_body.pack_forget()
            self._gantt_collapse_btn.config(text="⌃ expand")
        else:
            self._gantt_body.pack(fill="x")
            self._gantt_collapse_btn.config(text="⌄ collapse")
            self.root.after(20, self._draw_queue_gantt)

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

    def _bind_equip_mousewheel(self, widget):
        """Bind mousewheel / Linux scroll events so the wheel scrolls the
        Manage Equipment tab.  Mirrors _bind_settings_mousewheel (see it for
        the macOS delta quirk)."""
        def _on_wheel(e):
            if abs(e.delta) >= 120:
                steps = int(-1 * (e.delta / 120))
            elif e.delta:
                steps = -1 if e.delta > 0 else 1
            else:
                steps = 0
            if steps:
                self._equip_canvas.yview_scroll(steps, "units")
        widget.bind("<MouseWheel>", _on_wheel)
        widget.bind("<Button-4>",
            lambda e: self._equip_canvas.yview_scroll(-1, "units"))
        widget.bind("<Button-5>",
            lambda e: self._equip_canvas.yview_scroll(1, "units"))

    def _bind_equip_mousewheel_recursive(self, widget):
        """Recursively bind equip wheel-scroll on a widget and descendants."""
        self._bind_equip_mousewheel(widget)
        for child in widget.winfo_children():
            self._bind_equip_mousewheel_recursive(child)

    def _bind_settings_mousewheel(self, widget):
        """Bind mousewheel / Linux scroll events so the wheel scrolls the
        Settings panel.  Mirrors _bind_plan_mousewheel (see it for the
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

        # ── UPDATES ───────────────────────────────────────────────────────
        upd_card = self._make_settings_card(outer, "UPDATES", "#5dcaa5")
        ttk.Label(upd_card,
                  text=("Checks GitHub once a day for a newer release. "
                        "Nothing is downloaded or installed automatically."),
                  font=("Helvetica", 9), foreground="#556677").pack(
            anchor="w", padx=8, pady=(0, 4))

        self._settings_check_updates = tk.BooleanVar(
            value=self.data.get("settings", {}).get("check_for_updates", True))
        ttk.Checkbutton(upd_card, text="Check for updates on launch",
                        variable=self._settings_check_updates,
                        command=self._settings_toggle_update_check).pack(
            anchor="w", padx=8, pady=(0, 4))

        upd_row = ttk.Frame(upd_card)
        upd_row.pack(fill="x", padx=8, pady=(0, 8))
        ttk.Button(upd_row, text="Check now",
                   command=lambda: self._check_for_updates(manual=True)).pack(side="left")
        self._settings_update_status = ttk.Label(
            upd_row, text=f"Current version {__version__}",
            font=("Helvetica", 9), foreground="#556677")
        self._settings_update_status.pack(side="left", padx=(10, 0))

        # About line
        ttk.Label(outer, text=f"Lightbucket Astro Planner  ·  Data: {self.data_path}",
                  font=("Helvetica", 9), foreground="#334455").pack(anchor="center", pady=(12, 0))

        # Wheel-scroll the whole panel (canvas + every card widget).
        self._bind_settings_mousewheel(self._settings_canvas)
        self._bind_settings_mousewheel_recursive(outer)

    # ═══════════════════════════════════════════════════════════════════
    # UPDATE CHECKING
    # ═══════════════════════════════════════════════════════════════════
    #
    # Flow:  _maybe_check_for_updates (throttle)  →  _check_for_updates
    #        →  _update_check_worker (background thread, network)
    #        →  _update_check_finished (back on the UI thread via after)
    #        →  _show_update_banner  →  _show_update_dialog (on demand)
    #
    # Every network operation happens on a daemon thread and every widget
    # touch happens on the Tk main thread.  Failures are silent for the
    # automatic path (offline at a dark site is normal, not an error worth
    # a dialog) and reported inline for the manual "Check now" path.

    def _maybe_check_for_updates(self):
        """Run the automatic update check if enabled and not checked recently."""
        if not getattr(self, "check_updates_enabled", True):
            return
        last = self.data.get("settings", {}).get("last_update_check", 0)
        try:
            last = float(last)
        except (TypeError, ValueError):
            last = 0
        if (datetime.now(timezone.utc).timestamp() - last) < UPDATE_CHECK_INTERVAL_S:
            return
        self._check_for_updates(manual=False)

    def _check_for_updates(self, manual=False):
        """Start a background update check.

        ``manual=True`` comes from the Settings "Check now" button: it
        bypasses the once-a-day throttle, ignores a previously-skipped
        version, and reports its result (including "you're up to date" and
        network errors) instead of failing silently.
        """
        if self._update_checking:
            return
        self._update_checking = True
        if manual:
            self._set_update_status("Checking…", "#7eb8d4")
        threading.Thread(target=self._update_check_worker,
                         args=(manual,), daemon=True).start()

    def _update_check_worker(self, manual):
        """Fetch the latest release from GitHub (runs off the UI thread)."""
        info, error = None, None
        try:
            req = urllib.request.Request(
                UPDATE_API_URL,
                headers={"User-Agent": f"LightbucketAstroPlanner/{__version__}",
                         "Accept": "application/vnd.github+json"})
            with urllib.request.urlopen(req, timeout=UPDATE_CHECK_TIMEOUT_S) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
            info = {
                "tag":   payload.get("tag_name", "") or "",
                "name":  payload.get("name", "") or "",
                "notes": payload.get("body", "") or "",
                "url":   payload.get("html_url", "") or UPDATE_RELEASES_URL,
            }
        except Exception as exc:
            error = exc
        self.root.after(0, lambda: self._update_check_finished(info, error, manual))

    def _update_check_finished(self, info, error, manual):
        """Handle a completed update check on the UI thread."""
        self._update_checking = False

        # Stamp the attempt time even on failure so a machine that is
        # offline every launch doesn't retry the network on every start.
        self.data.setdefault("settings", {})["last_update_check"] = \
            datetime.now(timezone.utc).timestamp()
        try:
            self._persist_data()
        except Exception:
            pass

        if error is not None:
            if manual:
                self._set_update_status("Couldn't reach GitHub — check your connection.",
                                        "#e05555")
            return

        latest  = _parse_version(info.get("tag"))
        current = _parse_version(__version__)
        if latest is None or current is None:
            if manual:
                self._set_update_status("Couldn't read the latest version number.",
                                        "#e05555")
            return

        self._update_info = info

        if latest <= current:
            if manual:
                self._set_update_status(f"You're up to date (v{__version__}).", "#4caf50")
            return

        # A newer release exists.  Honour a skip only on the automatic path —
        # an explicit "Check now" should always show what's out there.
        skipped = self.data.get("settings", {}).get("skipped_version", "")
        if not manual and skipped and _parse_version(skipped) == latest:
            return

        version_txt = ".".join(str(p) for p in latest)
        if manual:
            self._set_update_status(f"Version {version_txt} is available.", "#5dcaa5")
        self._show_update_banner(version_txt)

    # ── Banner (Option A) ────────────────────────────────────────────────

    def _update_banner_colors(self):
        """Return (bg, accent, text, muted) for the banner in the current theme."""
        if getattr(self, "night_mode", False):
            return "#2a0000", "#cc0000", "#ff6633", "#993300"
        return "#1b3a2e", "#5dcaa5", "#d8f0e6", "#7ea99a"

    def _show_update_banner(self, version_txt):
        """Show the dismissible 'update available' strip beneath the header.

        Packed with ``before=self._main_frame`` so it slots between the header
        and the sidebar/notebook area regardless of when it's created — the
        main frame was packed long before this runs.
        """
        self._dismiss_update_banner()   # replace any existing banner

        bg, accent, fg, muted = self._update_banner_colors()

        bar = tk.Frame(self.root, bg=bg)
        try:
            bar.pack(fill="x", before=self._main_frame)
        except Exception:
            # Extremely defensive: if the main frame is gone, don't crash the
            # app over a notification.
            bar.destroy()
            return
        self._update_banner = bar

        # Left accent stripe, mirroring the settings-card visual language.
        tk.Frame(bar, bg=accent, width=3).pack(side="left", fill="y")

        inner = tk.Frame(bar, bg=bg)
        inner.pack(fill="x", padx=(11, 12), pady=7)

        tk.Label(inner, text="⬆", bg=bg, fg=accent,
                 font=("Helvetica", 12, "bold")).pack(side="left", padx=(0, 8))
        tk.Label(inner, text=f"Version {version_txt} is available",
                 bg=bg, fg=fg, font=("Helvetica", 11, "bold")).pack(side="left")
        tk.Label(inner, text=f"you have {__version__}",
                 bg=bg, fg=muted, font=("Helvetica", 10)).pack(side="left", padx=(9, 0))

        # Dismiss sits at the far right; the two actions pack right-to-left
        # beside it so the primary action lands closest to the edge content.
        close = tk.Label(inner, text="✕", bg=bg, fg=muted,
                         font=("Helvetica", 11), cursor="hand2", padx=6)
        close.pack(side="right")
        close.bind("<Button-1>", lambda e: self._dismiss_update_banner())
        ToolTip(close, "Dismiss until the next check")

        def _pill(text, command, primary):
            """Build a flat clickable pill.

            The outline on the secondary pill is drawn with a 1 px wrapper
            Frame rather than ``highlightthickness`` — native macOS Tk
            frequently refuses to paint highlight borders on a Label, which
            would leave 'What's new' looking like unclickable text.
            """
            border = tk.Frame(inner, bg=accent)
            border.pack(side="right", padx=(8, 0))
            fill_bg = accent if primary else bg
            fill_fg = ("#04342C" if not getattr(self, "night_mode", False)
                       else "#1a0000") if primary else fg
            lbl = tk.Label(border, text=text, cursor="hand2",
                           font=("Helvetica", 10, "bold" if primary else "normal"),
                           padx=11, pady=3, bg=fill_bg, fg=fill_fg)
            lbl.pack(padx=0 if primary else 1, pady=0 if primary else 1)
            for _w in (border, lbl):
                _w.bind("<Button-1>", lambda e: command())
            return border

        _pill("Download", self._open_release_page, True)
        _pill("What's new", self._show_update_dialog, False)

    def _dismiss_update_banner(self):
        """Remove the update banner if it's showing."""
        bar = getattr(self, "_update_banner", None)
        if bar is not None:
            try:
                bar.destroy()
            except Exception:
                pass
            self._update_banner = None

    def _retheme_update_banner(self):
        """Rebuild the banner in the current theme after a day/night toggle.

        The banner is created after ``_snapshot_original_colors`` runs, so it
        isn't in ``_orig_colors`` and the theme-restore loop can't reach it —
        same situation as the queue and plan cards, and handled the same way:
        by rebuilding rather than recolouring in place.
        """
        if getattr(self, "_update_banner", None) is None:
            return
        info = getattr(self, "_update_info", None)
        latest = _parse_version((info or {}).get("tag"))
        if latest is None:
            self._dismiss_update_banner()
            return
        self._show_update_banner(".".join(str(p) for p in latest))

    # ── Release-notes dialog (Option B) ──────────────────────────────────

    @staticmethod
    def _plain_release_notes(md):
        """Flatten GitHub-flavoured Markdown release notes to readable text.

        Not a Markdown parser — just enough to stop headings, emphasis, and
        link syntax from showing up as punctuation noise in a Tk Text widget.
        """
        if not md:
            return "No release notes were provided for this version."
        text = md.replace("\r\n", "\n").replace("\r", "\n")
        text = re.sub(r"^\s{0,3}#{1,6}\s*", "", text, flags=re.MULTILINE)
        text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)   # [label](url) → label
        text = re.sub(r"(\*\*|__)(.*?)\1", r"\2", text)        # bold
        text = re.sub(r"`{1,3}([^`]*)`{1,3}", r"\1", text)     # code spans
        text = re.sub(r"^\s*[-*+]\s+", "• ", text, flags=re.MULTILINE)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip() or "No release notes were provided for this version."

    def _show_update_dialog(self):
        """Open the release-notes dialog for the pending update."""
        info = getattr(self, "_update_info", None)
        if not info:
            return
        latest = _parse_version(info.get("tag"))
        version_txt = ".".join(str(p) for p in latest) if latest else info.get("tag", "")

        dlg = tk.Toplevel(self.root)
        dlg.title("Update Available")
        dlg.transient(self.root)
        dlg.grab_set()
        dlg.resizable(False, False)

        head = ttk.Frame(dlg, padding=(16, 14, 16, 6))
        head.pack(fill="x")
        ttk.Label(head, text=info.get("name") or f"Version {version_txt}",
                  font=("Helvetica", 13, "bold")).pack(side="left")
        ttk.Label(head, text=f"{__version__}  →  {version_txt}",
                  font=("Helvetica", 10), foreground="#5dcaa5").pack(side="right")

        body = ttk.Frame(dlg, padding=(16, 0, 16, 6))
        body.pack(fill="both", expand=True)
        ttk.Label(body, text="What's new", font=("Helvetica", 9, "bold"),
                  foreground="#7eb8d4").pack(anchor="w", pady=(0, 4))

        notes_wrap = ttk.Frame(body)
        notes_wrap.pack(fill="both", expand=True)
        notes_sb = ttk.Scrollbar(notes_wrap, orient="vertical")
        notes_sb.pack(side="right", fill="y")
        notes = tk.Text(notes_wrap, width=62, height=13, wrap="word",
                        relief="flat", padx=10, pady=8,
                        font=("Helvetica", 10),
                        yscrollcommand=notes_sb.set)
        notes.pack(side="left", fill="both", expand=True)
        notes_sb.config(command=notes.yview)
        notes.insert("1.0", self._plain_release_notes(info.get("notes")))
        notes.config(state="disabled")
        # Text isn't a ttk widget, so it needs explicit theme colours.
        if getattr(self, "night_mode", False):
            notes.config(bg="#200000", fg="#cc0000", insertbackground="#cc0000")
        else:
            notes.config(bg="#1b2836", fg="#c8d6e2", insertbackground="#c8d6e2")

        foot = ttk.Frame(dlg, padding=(16, 4, 16, 14))
        foot.pack(fill="x")

        skip_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(foot, text="Skip this version", variable=skip_var).pack(side="left")

        def _finish(open_page):
            self.data.setdefault("settings", {})
            if skip_var.get() and latest is not None:
                self.data["settings"]["skipped_version"] = ".".join(str(p) for p in latest)
                self._dismiss_update_banner()
            else:
                self.data["settings"].pop("skipped_version", None)
            try:
                self._persist_data()
            except Exception:
                pass
            dlg.destroy()
            if open_page:
                self._open_release_page()

        ttk.Button(foot, text="Open release page",
                   command=lambda: _finish(True)).pack(side="right", padx=(6, 0))
        ttk.Button(foot, text="Later",
                   command=lambda: _finish(False)).pack(side="right")

        self._theme_popup(dlg)
        dlg.protocol("WM_DELETE_WINDOW", lambda: _finish(False))

    def _open_release_page(self):
        """Open the GitHub release page in the user's default browser."""
        info = getattr(self, "_update_info", None) or {}
        url = info.get("url") or UPDATE_RELEASES_URL
        try:
            webbrowser.open(url)
        except Exception:
            messagebox.showinfo(
                "Download the Update",
                f"Couldn't open your browser automatically.\n\n"
                f"Visit:\n{url}")

    def _set_update_status(self, text, color="#556677"):
        """Update the Settings panel's update-status line, if it's been built."""
        lbl = getattr(self, "_settings_update_status", None)
        if lbl is not None:
            try:
                lbl.config(text=text, foreground=color)
            except Exception:
                pass

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
        if hasattr(self, "_settings_check_updates"):
            self.data["settings"]["check_for_updates"] = self._settings_check_updates.get()
            self.check_updates_enabled = self._settings_check_updates.get()

        # Apply default bortle to the active Bortle choice
        if self._settings_default_bortle.get() in BORTLE_FACTORS:
            self.bortle_choice.set(self._settings_default_bortle.get())

        # Apply auto-update setting
        if self._settings_auto_update.get():
            self.auto_update_enabled = True

        self.save_data()
        messagebox.showinfo("Saved", "Preferences saved successfully.")

    def _settings_toggle_update_check(self):
        """Persist the update-check toggle immediately.

        Saved on click rather than waiting for 'Save Preferences' — a user
        switching this off wants the network call to stop now, not after they
        remember to press another button.  Turning it off also clears any
        visible banner so the opt-out is immediately believable.
        """
        enabled = self._settings_check_updates.get()
        self.check_updates_enabled = enabled
        self.data.setdefault("settings", {})["check_for_updates"] = enabled
        try:
            self._persist_data()
        except Exception:
            pass
        if enabled:
            self._set_update_status(f"Current version {__version__}", "#556677")
        else:
            self._dismiss_update_banner()
            self._set_update_status("Automatic checks are off.", "#556677")

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

    def _get_rig_accent_color(self, scope, camera):
        """Return a stable accent color for a scope+camera pairing.

        Colors are handed out from RIG_ACCENT_COLORS in first-seen order and
        cached on self._rig_color_map for the rest of the session, so a
        rig's card color never shifts just because the plan list was
        reordered, refreshed, or had entries added/removed.
        """
        if not hasattr(self, "_rig_color_map"):
            self._rig_color_map = {}
        key = (scope or "", camera or "")
        color = self._rig_color_map.get(key)
        if color is None:
            color = RIG_ACCENT_COLORS[len(self._rig_color_map) % len(RIG_ACCENT_COLORS)]
            self._rig_color_map[key] = color
        return color

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
            # Camera is part of the group key too — the same target/scope
            # can carry two independent rigs (e.g. one camera on LRGB,
            # another on SHO through the same telescope), and each rig's
            # filters should get their own running offset, not share one.
            group_key = (e["target_id"], e.get("scope", ""), e.get("camera", ""))

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

        # Equipment-line text wraps instead of overflowing the card: a long
        # scope + camera name pair (e.g. "Radian Telescopes Raptor 61 f/5.6
        # · ZWO ASI2600MM Pro") is easily wider than the whole plan rail on
        # its own, and unlike the metrics grid this is a single Label, so a
        # wraplength is all it takes.  380 = rightpane's fixed width; the
        # rest backs out the scrollbar (15) and the card/content chrome
        # (2px card padx *2, 3px accent bar, 8+10px content padx).
        equip_wraplen = 380 - 15 - 4 - 3 - 18

        for i, (e, start_str) in enumerate(zip(self._plan_entries, display_starts)):
            is_selected = (i == old_sel)
            border_col = "#38bdf8" if is_selected else "#1e2d3e"
            # Accent bar identifies the target's rig (scope+camera) instead
            # of selection now — selection is shown by the card border above
            # (already a distinct blue highlight), so the two signals don't
            # compete for the same bar.
            accent_col = self._get_rig_accent_color(e.get("scope", ""), e.get("camera", ""))

            card = tk.Frame(self._plan_cards_inner, bg="#131f2e",
                            highlightthickness=1, highlightbackground=border_col)
            card.pack(fill="x", padx=2, pady=2)

            # Drag handle — its own child widget, same principle as the 🗑
            # icon below: Tk delivers a click to whichever widget is
            # directly under the cursor, so grabbing here never fires the
            # card's click-to-select / double-click-to-open bindings, and
            # dragging from anywhere else on the card still does nothing.
            grip = tk.Label(card, text="⠿", bg="#131f2e", fg="#3a4a5c",
                            font=("Helvetica", 11), cursor="fleur", width=2)
            grip.pack(side="left", fill="y")
            grip.bind("<Enter>", lambda e, w=grip: w.config(fg="#38bdf8", bg="#182636"))
            grip.bind("<Leave>", lambda e, w=grip: w.config(fg="#3a4a5c", bg="#131f2e"))
            grip.bind("<ButtonPress-1>", lambda e, ii=i: self._plan_drag_start(ii, e))
            grip.bind("<B1-Motion>", self._plan_drag_motion)
            grip.bind("<ButtonRelease-1>", self._plan_drag_end)
            ToolTip(grip, "Drag to reorder.")

            # Left accent
            tk.Frame(card, bg=accent_col, width=3).pack(side="left", fill="y")

            content = tk.Frame(card, bg="#131f2e")
            content.pack(side="left", fill="both", expand=True, padx=(8, 10), pady=5)

            # Two lines instead of one — the name/filter/equipment (left)
            # and the metrics grid (right) used to share a single row and
            # would get squeezed or clipped once both sides' natural width
            # exceeded the rightpane's fixed ~380px. Metrics now get their
            # own full-width row below the name instead, indented to align
            # under it (26px circle + 8px gap) rather than pinned to the
            # right edge, so there's room to spread across the card's full
            # width without competing against the name/common/filter text.
            row = tk.Frame(content, bg="#131f2e")
            row.pack(fill="x")
            metrics_row = tk.Frame(content, bg="#131f2e")
            metrics_row.pack(fill="x", pady=(4, 0))

            # Number circle
            circle_bg = "#38bdf8" if is_selected else "#2e4a63"
            circle_fg = "#0e1a28" if is_selected else "#aaddff"
            num_cv = tk.Canvas(row, width=26, height=26, bg="#131f2e", highlightthickness=0)
            num_cv.pack(side="left", padx=(0, 8))
            num_cv.create_oval(1, 1, 25, 25, fill=circle_bg, outline="")
            num_cv.create_text(13, 13, text=str(i + 1), fill=circle_fg,
                               font=("Helvetica", 10, "bold"))

            # Per-card remove icon — top-right corner of the card's name
            # row. Deliberately NOT passed to _bind_all()/_bind_plan_
            # mousewheel below with the select/open handlers: it's a
            # distinct child widget, so a click on it goes to this Label
            # alone and never reaches the card's own click bindings (Tk
            # dispatches a click to whichever widget is directly under the
            # cursor — it does not also fire a parent's binding). That's
            # what lets "click the card opens details, click the icon
            # removes it" coexist with no extra plumbing.
            remove_icon = tk.Label(row, text="🗑", bg="#131f2e", fg="#3a4a5c",
                                   font=("Helvetica", 10), cursor="hand2", padx=4)
            remove_icon.pack(side="right", anchor="n", padx=(4, 0))
            remove_icon.bind("<Button-1>", lambda e, ii=i: self._plan_card_remove_click(ii))
            remove_icon.bind("<Enter>", lambda e, w=remove_icon: w.config(fg="#ff6b66"))
            remove_icon.bind("<Leave>", lambda e, w=remove_icon: w.config(fg="#3a4a5c"))
            ToolTip(remove_icon, "Remove this target from\ntonight's plan.")

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
                         fg="#445566", font=("Helvetica", 9), justify="left",
                         wraplength=equip_wraplen).pack(anchor="w", fill="x")

            # Metrics — own row below the name, indented slightly rather
            # than pinned right.  Kept close to the left edge (not aligned
            # under the full circle+gap) and with tighter inter-column
            # gaps than the old single-row layout so the widest realistic
            # card (long common name + narrowband filter badge + long
            # scope/camera names on line 1; six-column metrics on line 2)
            # still fits inside the plan rail's ~380px fixed width even
            # after the vertical scrollbar takes its share.
            metrics = tk.Frame(metrics_row, bg="#131f2e")
            metrics.pack(side="left", padx=(18, 0))

            # Window
            win_str = f"{e['win_start']}–{e['win_end']}" if e.get("win_start") else "—"
            _alloc = e.get("allocated_hrs", 0)
            win_col = "#4caf50" if _alloc >= 2 else ("#f59e0b" if _alloc >= 0.5 else "#556677")
            tk.Label(metrics, text=win_str, bg="#131f2e", fg=win_col,
                     font=("Helvetica", 10, "bold")).grid(row=0, column=0, padx=(0, 9))
            tk.Label(metrics, text="window", bg="#131f2e", fg="#445566",
                     font=("Helvetica", 8)).grid(row=1, column=0, padx=(0, 9))

            # Start
            tk.Label(metrics, text=start_str, bg="#131f2e", fg="#ffffff",
                     font=("Helvetica", 10)).grid(row=0, column=1, padx=(0, 8))
            tk.Label(metrics, text="start", bg="#131f2e", fg="#445566",
                     font=("Helvetica", 8)).grid(row=1, column=1, padx=(0, 8))

            # Sub-exp
            tk.Label(metrics, text=f"{e['exp_s']}s", bg="#131f2e", fg="#f59e0b",
                     font=("Helvetica", 10)).grid(row=0, column=2, padx=(0, 8))
            tk.Label(metrics, text="sub", bg="#131f2e", fg="#445566",
                     font=("Helvetica", 8)).grid(row=1, column=2, padx=(0, 8))

            # Alloc
            tk.Label(metrics, text=f"{e['allocated_hrs']}h", bg="#131f2e", fg="#ffffff",
                     font=("Helvetica", 10), width=4).grid(row=0, column=3, padx=(0, 6))
            tk.Label(metrics, text="alloc", bg="#131f2e", fg="#445566",
                     font=("Helvetica", 8)).grid(row=1, column=3, padx=(0, 6))

            # Subs
            tk.Label(metrics, text=str(e["n_subs"]), bg="#131f2e", fg="#ffffff",
                     font=("Helvetica", 10), width=3).grid(row=0, column=4, padx=(0, 6))
            tk.Label(metrics, text="subs", bg="#131f2e", fg="#445566",
                     font=("Helvetica", 8)).grid(row=1, column=4, padx=(0, 6))

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
            _bind_all(metrics_row)
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

    def _plan_card_remove_click(self, idx):
        """Remove one specific card via its own corner icon, regardless of
        whether that card happens to be the currently selected one — this
        is now the only removal path (the old Remove Selected button and
        its reorder-row icon were retired once every card got its own)."""
        if idx is None or idx < 0 or idx >= len(self._plan_entries):
            return
        sel = self._plan_card_selected
        removed_id = self._plan_entries[idx]["target_id"]
        del self._plan_entries[idx]
        if sel is not None:
            if sel == idx:
                self._plan_card_selected = (min(idx, len(self._plan_entries) - 1)
                                            if self._plan_entries else None)
            elif sel > idx:
                self._plan_card_selected = sel - 1
        self._refresh_plan_tree()
        # Drop the browsing-grid pin too (if this was its last plan entry),
        # and re-scan so a now-unpinned target that no longer matches the
        # active filters disappears immediately instead of lingering.
        self._unmark_grid_touched_if_unplanned(removed_id)
        self._refresh_visible_grid()

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
        circle_bg = "#38bdf8" if is_selected else "#2e4a63"
        circle_fg = "#0e1a28" if is_selected else "#aaddff"
        try:
            card.configure(highlightbackground=border_col)
            # Note: the accent bar (children[0]) is intentionally left
            # untouched here — it always shows the card's rig color now,
            # not selection state, so selection is signaled by the border
            # (above) and the number circle (below) only.
            children = card.winfo_children()
            # Content is always the LAST child, regardless of how many
            # fixed-width strips (grip, accent bar) precede it — it's the
            # only child packed with expand=True, so it settles wherever
            # the others leave it, but it's created last and Tk's
            # winfo_children() preserves creation order.
            content = children[-1] if children else None
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

    def _clear_plan(self):
        """Clear all entries from tonight's plan after confirmation."""
        if not self._plan_entries:
            return
        if messagebox.askyesno("Clear Plan",
                "Remove all entries from tonight's plan?"):
            removed_ids = {e["target_id"] for e in self._plan_entries}
            self._plan_entries.clear()
            self._plan_card_selected = None
            self._refresh_plan_tree()
            # Same browsing-grid pin cleanup as removing a single card (see
            # _plan_card_remove_click) — every entry is gone now, so this
            # drops the pin unconditionally rather than re-checking
            # membership for each one.
            for tid in removed_ids:
                if hasattr(self, "_grid_touched"):
                    self._grid_touched.pop(tid, None)
            self._refresh_visible_grid()

    def _plan_drag_start(self, idx, event):
        """Begin a drag-to-reorder gesture, started from a card's grip
        handle (see _refresh_plan_tree). Deliberately does NOT touch
        self._plan_entries or trigger a redraw until the button is
        released — _refresh_plan_tree() destroys and rebuilds every card
        from scratch, and doing that mid-drag would destroy the very
        widget that currently holds the mouse grab, aborting the drag."""
        if idx is None or idx >= len(self._plan_card_widgets):
            return
        card = self._plan_card_widgets[idx]
        self._plan_drag = {"idx": idx, "target_idx": idx}
        try:
            card.configure(highlightbackground="#38bdf8", highlightthickness=2)
            card.lift()
        except Exception:
            pass
        # Drop-line indicator — a thin bar showing where the card would
        # land if released now. It lives in the same frame as the cards,
        # so the next _refresh_plan_tree() sweeps it away for free.
        self._plan_drag_line = tk.Frame(self._plan_cards_inner, bg="#38bdf8", height=3)
        self._plan_drag_place_line(idx)

    def _plan_drag_place_line(self, target):
        """Pack the drop-line indicator at the gap `target` refers to,
        where `target` is an index into the plan list with the dragged
        entry already removed (0 = before everything else, len(others) =
        after everything else)."""
        drag = self._plan_drag
        if not drag or not hasattr(self, "_plan_drag_line"):
            return
        others = [w for j, w in enumerate(self._plan_card_widgets) if j != drag["idx"]]
        line = self._plan_drag_line
        line.pack_forget()
        if target >= len(others):
            line.pack(fill="x", padx=6, pady=1)
        else:
            line.pack(fill="x", padx=6, pady=1, before=others[target])

    def _plan_drag_motion(self, event):
        """Track the pointer during a drag and move the drop-line to
        whichever gap between cards it's currently hovering over. Only
        the indicator moves — the cards themselves stay put (and
        un-rebuilt) until the button is released."""
        drag = self._plan_drag
        if not drag:
            return
        y = event.y_root
        target = 0
        for j, w in enumerate(self._plan_card_widgets):
            if j == drag["idx"]:
                continue
            try:
                mid = w.winfo_rooty() + w.winfo_height() / 2
            except Exception:
                continue
            if y > mid:
                target += 1
        if target != drag["target_idx"]:
            drag["target_idx"] = target
            self._plan_drag_place_line(target)

    def _plan_drag_end(self, event):
        """Finish a drag gesture: commit the reorder (if the drop
        position differs from where the drag started) and redraw."""
        drag = self._plan_drag
        self._plan_drag = None
        if not drag:
            return
        if hasattr(self, "_plan_drag_line"):
            try:
                self._plan_drag_line.destroy()
            except Exception:
                pass
            del self._plan_drag_line
        idx, target = drag["idx"], drag["target_idx"]
        if target != idx and 0 <= idx < len(self._plan_entries):
            entry = self._plan_entries.pop(idx)
            self._plan_entries.insert(target, entry)
            self._plan_card_selected = target
        self._refresh_plan_tree()

    def _plan_move_up(self):
        """Move the selected plan entry one position earlier in the list."""
        idx = self._plan_card_selected
        if idx is None or idx <= 0 or idx >= len(self._plan_entries):
            return
        self._plan_entries[idx - 1], self._plan_entries[idx] = (
            self._plan_entries[idx], self._plan_entries[idx - 1])
        self._plan_card_selected = idx - 1
        self._refresh_plan_tree()

    def _plan_move_down(self):
        """Move the selected plan entry one position later in the list."""
        idx = self._plan_card_selected
        if idx is None or idx < 0 or idx >= len(self._plan_entries) - 1:
            return
        self._plan_entries[idx + 1], self._plan_entries[idx] = (
            self._plan_entries[idx], self._plan_entries[idx + 1])
        self._plan_card_selected = idx + 1
        self._refresh_plan_tree()

    def _plan_suggest_order(self):
        """Sort tonight's plan by each target's meridian transit time.

        Plan entries don't store transit_time (only the retired queue
        entries did), so it's computed here on the fly via
        _calc_transit_time — same helper the Gantt strip's transit tick
        uses.
        """
        if len(self._plan_entries) < 2:
            return
        lat, lon = self._get_saved_location()
        if lon is None:
            messagebox.showwarning("No Location",
                "Save a location before sorting by transit time.")
            return

        def _transit_h(e):
            try:
                return self._parse_h(_calc_transit_time(e["ra_deg"], lon)) or 99.0
            except Exception:
                return 99.0

        sel_id = None
        if self._plan_card_selected is not None and 0 <= self._plan_card_selected < len(self._plan_entries):
            sel_id = id(self._plan_entries[self._plan_card_selected])
        self._plan_entries.sort(key=_transit_h)
        if sel_id is not None:
            for i, e in enumerate(self._plan_entries):
                if id(e) == sel_id:
                    self._plan_card_selected = i
                    break
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
        ftype_var = tk.StringVar()
        path = filedialog.asksaveasfilename(
            title="Export Session",
            initialdir=str(self._get_sessions_dir()),
            initialfile=default,
            defaultextension="",
            filetypes=[("Text report", "*.txt"), ("HTML report", "*.html"), ("All files", "*.*")],
            typevariable=ftype_var,
        )
        if not path:
            return
        # A typed, recognised extension wins; otherwise the file-type dropdown
        # selection decides the format and we append the matching extension.
        ext = Path(path).suffix.lower()
        if ext in (".html", ".htm"):
            fmt = "html"
        elif ext == ".txt":
            fmt = "text"
        else:
            fmt = "html" if "html" in (ftype_var.get() or "").lower() else "text"
            path = path + (".html" if fmt == "html" else ".txt")

        if fmt == "html":
            try:
                with open(path, "w", encoding="utf-8") as f:
                    f.write(self._build_session_html(Path(path).stem))
                messagebox.showinfo("Exported", f"Session exported to:\n{path}")
            except OSError as e:
                messagebox.showerror("Export Failed", f"Could not write file.\n\nDetail: {e}")
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

            # Group entries by (target_id, scope, camera) so that the same
            # object imaged with two different rigs — including two
            # cameras sharing one telescope (e.g. LRGB on one camera, SHO
            # on another) — gets its own section in the report. Entries
            # sharing target_id, scope AND camera are treated as one setup
            # (e.g. multiple filters on the same rig) and appear under one
            # header.
            seen_groups = []   # list of (target_id, scope, camera) tuples in encounter order
            for e in self._plan_entries:
                key = (e["target_id"], e.get("scope", ""), e.get("camera", ""))
                if key not in seen_groups:
                    seen_groups.append(key)

            total_targets = len(seen_groups)
            target_num = 0

            for (tid, scope_key, camera_key) in seen_groups:
                target_num += 1
                # Grab all entries for this target+scope+camera combo
                target_entries = [e for e in self._plan_entries
                                  if e["target_id"] == tid and e.get("scope", "") == scope_key
                                  and e.get("camera", "") == camera_key]
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


    def _build_session_html(self, session_name):
        """Build a self-contained, light-theme HTML plan report with a per-target
        altitude chart drawn as inline SVG.

        No external assets and no extra dependencies — the file opens in any
        browser, and a PDF copy is just Print -> Save as PDF.
        """
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
        lat, lon = self._get_saved_location()
        site = (self.data.get("active_location") or "").strip()

        def esc(x):
            return (str(x if x is not None else "")
                    .replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))

        # Group entries by (target_id, scope, camera), in encounter order —
        # same rule as the text report: same object on two rigs (including
        # two cameras sharing one scope) gets two sections.
        seen_groups = []
        for e in self._plan_entries:
            key = (e["target_id"], e.get("scope", ""), e.get("camera", ""))
            if key not in seen_groups:
                seen_groups.append(key)
        total_targets = len(seen_groups)

        total_subs  = sum(e["n_subs"]        for e in self._plan_entries)
        total_alloc = sum(e["allocated_hrs"] for e in self._plan_entries)
        total_int   = sum(e["total_int_hrs"] for e in self._plan_entries)

        if site and lat is not None and lon is not None:
            loc_str = f"{esc(site)} &middot; {lat:.4f}, {lon:.4f}"
        elif lat is not None and lon is not None:
            loc_str = f"{lat:.4f}, {lon:.4f}"
        else:
            loc_str = "location not set"

        css = (
            "*{box-sizing:border-box}"
            "body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;"
            "color:#1f2730;margin:0;padding:28px;background:#f4f5f3;line-height:1.5}"
            ".page{max-width:880px;margin:0 auto;background:#fff;border:1px solid #d8dde3;"
            "border-radius:10px;padding:26px 32px}"
            ".hdr{border-bottom:2px solid #0f6e56;padding-bottom:10px;margin-bottom:16px}"
            ".hdr h1{font-size:20px;font-weight:600;margin:0}"
            ".hdr .sub{font-size:12px;color:#5f6b78;margin-top:4px}"
            ".totals{display:flex;flex-wrap:wrap;gap:20px;font-size:13px;margin-bottom:24px}"
            ".totals .k{color:#7a8593}.totals b{font-weight:600}"
            ".target{margin-bottom:30px;page-break-inside:avoid}"
            ".target h2{font-size:16px;font-weight:600;margin:0 0 10px;color:#16323b}"
            ".fields{display:grid;grid-template-columns:auto 1fr auto 1fr;gap:5px 16px;"
            "font-size:13px;margin-bottom:14px}"
            ".fields .k{color:#7a8593;white-space:nowrap}"
            "table.subs{width:100%;border-collapse:collapse;font-size:12.5px;margin-bottom:14px}"
            "table.subs th{text-align:left;font-weight:600;color:#5f6b78;"
            "border-bottom:1px solid #d8dde3;padding:4px 10px 6px 0}"
            "table.subs td{padding:4px 10px 4px 0;border-bottom:1px solid #eef1f4}"
            ".chart{border:1px solid #e2e7ec;border-radius:6px;background:#fbfcfd;padding:6px}"
            ".note{font-size:12px;color:#9aa5b1;font-style:italic;padding:10px 0}"
            ".foot{border-top:1px solid #e2e7ec;margin-top:18px;padding-top:10px;"
            "font-size:11px;color:#9aa5b1;text-align:right}"
            "@media print{body{background:#fff;padding:0}"
            ".page{border:none;border-radius:0;max-width:none;padding:0}"
            ".target{page-break-inside:avoid}}"
        )

        out = []
        out.append("<!DOCTYPE html>")
        out.append('<html lang="en"><head><meta charset="utf-8">')
        out.append('<meta name="viewport" content="width=device-width, initial-scale=1">')
        out.append(f"<title>{esc(session_name)} &mdash; Lightbucket Astro Planner</title>")
        out.append(f"<style>{css}</style></head><body><div class=\"page\">")
        out.append("<div class=\"hdr\"><h1>Lightbucket Astro Planner</h1>")
        out.append(f"<div class=\"sub\">Session: {esc(session_name)} &middot; "
                   f"generated {now_str} &middot; {loc_str}</div></div>")

        out.append("<div class=\"totals\">"
                   f"<span><span class=\"k\">Targets</span> <b>{total_targets}</b></span>"
                   f"<span><span class=\"k\">Subs</span> <b>{total_subs}</b></span>"
                   f"<span><span class=\"k\">Allocated</span> <b>{total_alloc:.1f} h</b></span>"
                   f"<span><span class=\"k\">Integration</span> <b>{total_int:.1f} h</b></span>"
                   "</div>")

        num = 0
        for (tid, scope_key, camera_key) in seen_groups:
            num += 1
            group = [e for e in self._plan_entries
                     if e["target_id"] == tid and e.get("scope", "") == scope_key
                     and e.get("camera", "") == camera_key]
            first = group[0]
            common = (first.get("common") or "").split(",")[0].strip()
            title = esc(tid) + (f" &mdash; {esc(common)}" if common else "")

            ra  = first.get("ra_deg")
            dec = first.get("dec_deg")
            ra_str  = self._fmt_ra_hms(ra)   if ra  is not None else "&mdash;"
            dec_str = self._fmt_dec_dms(dec)  if dec is not None else "&mdash;"
            pa  = first.get("rotation_angle", 0.0) or 0.0

            out.append("<div class=\"target\">")
            out.append(f"<h2>{num}. {title}</h2>")
            out.append("<div class=\"fields\">"
                       f"<span class=\"k\">RA / Dec</span><span>{ra_str} / {dec_str}</span>"
                       f"<span class=\"k\">Position angle</span><span>{pa:.1f}&deg;</span>"
                       f"<span class=\"k\">Scope</span><span>{esc(first.get('scope', ''))}</span>"
                       f"<span class=\"k\">Camera</span><span>{esc(first.get('camera', ''))}</span>"
                       f"<span class=\"k\">Reduction</span><span>{esc(first.get('reduction', '1.0×'))}</span>"
                       f"<span class=\"k\">Bortle</span><span>{esc(first.get('bortle', ''))}</span>"
                       "</div>")

            out.append("<table class=\"subs\"><thead><tr>"
                       "<th>Filter</th><th>Sub</th><th>Count</th>"
                       "<th>Integration</th><th>Window</th></tr></thead><tbody>")
            for e in group:
                filt = esc(e.get("filter") or "&mdash;") if e.get("filter") else "&mdash;"
                win  = (f"{e['win_start']}&ndash;{e['win_end']}"
                        if e.get("win_start") and e.get("win_end") else "&mdash;")
                out.append(f"<tr><td>{filt}</td>"
                           f"<td>{(e.get('exp_s') or 0):.0f} s</td>"
                           f"<td>{e.get('n_subs', 0)}</td>"
                           f"<td>{e.get('total_int_hrs', 0.0):.2f} h</td>"
                           f"<td>{win}</td></tr>")
            out.append("</tbody></table>")

            if lat is None or lon is None:
                out.append("<div class=\"note\">Observer location not set &mdash; "
                           "altitude chart omitted.</div>")
            else:
                svg = self._altitude_chart_svg(ra, dec, lat, lon,
                                               first.get("win_start"), first.get("win_end"))
                if svg:
                    out.append(f"<div class=\"chart\">{svg}</div>")
                else:
                    out.append("<div class=\"note\">No astronomical-dark window at this "
                               "site tonight &mdash; altitude chart omitted.</div>")
            out.append("</div>")

        out.append(f"<div class=\"foot\">Generated by Lightbucket Astro Planner &middot; "
                   f"{now_str} &middot; coordinates J2000</div>")
        out.append("</div></body></html>")
        return "\n".join(out)

    def _altitude_chart_svg(self, ra_deg, dec_deg, lat, lon,
                            win_start=None, win_end=None, width=760, height=190):
        """Return an inline-SVG altitude-vs-time chart for one target, styled for
        the light HTML report.

        Mirrors the Planner's live chart (nautical-dark window, altitude curve,
        min-altitude and horizon lines, imaging-window markers, moon rise/set,
        peak marker), using the same module-level astronomy helpers.  Returns ""
        when coordinates/location are missing or there is no dark window, so the
        caller can substitute a short note.
        """
        if ra_deg is None or dec_deg is None or lat is None or lon is None:
            return ""

        utc_offset_h = _utc_offset_hours()
        jd_noon = _jd_local_noon()
        step_jd = 5.0 / 1440.0
        n_full  = int(30 * 60 / 5) + 1

        # Nautical-dark bounds (sun below -12 deg)
        dark_start_jd = dark_end_jd = None
        been_dark = False
        for i in range(n_full):
            jd = jd_noon + i * step_jd
            sun_ra, sun_dec = _sun_ra_dec(jd)
            if _ra_dec_to_altaz(sun_ra, sun_dec, lat, lon, jd) < -12.0:
                been_dark = True
                if dark_start_jd is None:
                    dark_start_jd = jd
                dark_end_jd = jd
            elif been_dark:
                break
        if dark_start_jd is None:
            return ""

        pad_jd     = 30.0 / 1440.0
        scan_start = dark_start_jd - pad_jd
        scan_end   = dark_end_jd   + pad_jd
        total_jd   = scan_end - scan_start
        n_steps    = max(120, int(total_jd * 1440 / 5))
        step       = total_jd / (n_steps - 1)

        times_h, alts = [], []
        for i in range(n_steps):
            jd = scan_start + i * step
            times_h.append((((jd + 0.5) % 1.0) * 24.0 + utc_offset_h) % 24.0)
            alts.append(_ra_dec_to_altaz(ra_deg, dec_deg, lat, lon, jd))

        # Moon rise/set within the window
        moon_rise_x = moon_set_x = None
        moon_rise_lbl = moon_set_lbl = ""
        prev = None
        for i in range(n_steps):
            jd = scan_start + i * step
            m_ra, m_dec = _calc_moon_position(jd)
            m_alt = _ra_dec_to_altaz(m_ra, m_dec, lat, lon, jd)
            if prev is not None:
                frac = i / (n_steps - 1)
                lh = (((jd + 0.5) % 1.0) * 24.0 + utc_offset_h) % 24.0
                if prev < 0 <= m_alt and moon_rise_x is None:
                    moon_rise_x = frac
                    moon_rise_lbl = f"{int(lh):02d}:{int((lh % 1) * 60):02d}"
                elif prev >= 0 > m_alt and moon_set_x is None:
                    moon_set_x = frac
                    moon_set_lbl = f"{int(lh):02d}:{int((lh % 1) * 60):02d}"
            prev = m_alt

        # Layout
        lm, rm, tm, bm = 42, 14, 18, 28
        pw = width - lm - rm
        ph = height - tm - bm
        y_min = max(-10, min(alts) - 5)
        y_max = min(90,  max(alts) + 8)
        y_range = (y_max - y_min) or 1
        min_alt = int(self.data.get("settings", {}).get("min_alt", 20))

        def ix(i):  return lm + (i / (n_steps - 1)) * pw
        def iy(a):  return tm + ph - ((a - y_min) / y_range) * ph
        def cyf(y): return max(tm, min(tm + ph, y))
        def fx(fr): return lm + fr * pw

        dark_x0 = lm + ((dark_start_jd - scan_start) / total_jd) * pw
        dark_x1 = lm + ((dark_end_jd   - scan_start) / total_jd) * pw

        s = []
        s.append(f'<svg viewBox="0 0 {width} {height}" xmlns="http://www.w3.org/2000/svg" '
                 f'style="width:100%;height:auto;font-family:sans-serif" role="img" '
                 f'aria-label="Altitude versus time across tonight\'s dark window">')
        # Background: twilight surround + darker nautical-dark band
        s.append(f'<rect x="{lm:.1f}" y="{tm}" width="{pw:.1f}" height="{ph}" fill="#eef2f6"/>')
        s.append(f'<rect x="{dark_x0:.1f}" y="{tm}" width="{(dark_x1 - dark_x0):.1f}" '
                 f'height="{ph}" fill="#d9e2ec"/>')
        # Altitude grid + labels
        for a in range(0, 91, 15):
            if y_min <= a <= y_max:
                y = iy(a)
                s.append(f'<line x1="{lm}" y1="{y:.1f}" x2="{lm + pw:.1f}" y2="{y:.1f}" '
                         f'stroke="#dde4ec" stroke-width="1"/>')
                s.append(f'<text x="{lm - 5}" y="{y + 3:.1f}" fill="#7a8593" '
                         f'font-size="10" text-anchor="end">{a}&#176;</text>')
        # Horizon (0 deg)
        if y_min <= 0 <= y_max:
            yh = iy(0)
            s.append(f'<line x1="{lm}" y1="{yh:.1f}" x2="{lm + pw:.1f}" y2="{yh:.1f}" '
                     f'stroke="#d6a6a6" stroke-width="1" stroke-dasharray="5 4"/>')
        # Minimum-altitude line (from Settings)
        if y_min <= min_alt <= y_max:
            ym = iy(min_alt)
            s.append(f'<line x1="{lm}" y1="{ym:.1f}" x2="{lm + pw:.1f}" y2="{ym:.1f}" '
                     f'stroke="#c79a33" stroke-width="1" stroke-dasharray="4 5"/>')
            s.append(f'<text x="{lm + 4}" y="{ym - 4:.1f}" fill="#9a7a1f" '
                     f'font-size="10">{min_alt}&#176; min</text>')
        # X-axis time labels
        dark_span_h = (dark_end_jd - dark_start_jd) * 24.0
        label_step_h = 1 if dark_span_h <= 8 else 2
        scan_start_localh = (((scan_start + 0.5) % 1.0) * 24.0 + utc_offset_h) % 24.0
        h = math.ceil(scan_start_localh / label_step_h) * label_step_h
        guard = 0
        while guard < 48:
            guard += 1
            h_frac = ((h - scan_start_localh) % 24) / (total_jd * 24.0)
            if h_frac > 1.0:
                break
            x = lm + h_frac * pw
            s.append(f'<line x1="{x:.1f}" y1="{tm}" x2="{x:.1f}" y2="{tm + ph}" '
                     f'stroke="#e6ebf0" stroke-width="1"/>')
            s.append(f'<text x="{x:.1f}" y="{tm + ph + 13}" fill="#7a8593" '
                     f'font-size="10" text-anchor="middle">{int(h) % 24:02d}:00</text>')
            if h_frac > 0.98:
                break
            h = (h + label_step_h) % 24
        # Imaging-window markers
        for wt in (win_start, win_end):
            if wt:
                try:
                    p = wt.split(":")
                    wh = float(p[0]) + float(p[1]) / 60.0
                    wf = ((wh - scan_start_localh) % 24) / (total_jd * 24.0)
                    if 0.0 <= wf <= 1.0:
                        wx = lm + wf * pw
                        s.append(f'<line x1="{wx:.1f}" y1="{tm}" x2="{wx:.1f}" '
                                 f'y2="{tm + ph}" stroke="#3f9d4a" stroke-width="1.2" '
                                 f'stroke-dasharray="4 4"/>')
                except (ValueError, IndexError):
                    pass
        # Moon rise/set bars
        for mx, mlbl, ta, ox in ((moon_rise_x, moon_rise_lbl, "start", 4),
                                 (moon_set_x,  moon_set_lbl,  "end",  -4)):
            if mx is not None:
                x = fx(mx)
                s.append(f'<line x1="{x:.1f}" y1="{tm}" x2="{x:.1f}" y2="{tm + ph}" '
                         f'stroke="#b9a94a" stroke-width="2"/>')
                s.append(f'<text x="{x + ox:.1f}" y="{tm + 11}" fill="#8a7d2a" '
                         f'font-size="10" text-anchor="{ta}">\u263e {mlbl}</text>')
        # Soft fill under the above-horizon curve
        above = [(ix(i), cyf(iy(alts[i]))) for i in range(n_steps) if alts[i] >= 0]
        if len(above) >= 2:
            base_y = cyf(iy(0)) if y_min <= 0 <= y_max else tm + ph
            inner = " ".join(f"{x:.1f},{y:.1f}" for x, y in above)
            poly = f"{above[0][0]:.1f},{base_y:.1f} {inner} {above[-1][0]:.1f},{base_y:.1f}"
            s.append(f'<polygon points="{poly}" fill="#e3f2ec"/>')
        # Altitude curve
        curve = " ".join(f"{ix(i):.1f},{cyf(iy(alts[i])):.1f}" for i in range(n_steps))
        s.append(f'<polyline points="{curve}" fill="none" stroke="#0f6e56" stroke-width="2"/>')
        # Peak marker
        peak_i = max(range(n_steps), key=lambda i: alts[i])
        px, py = ix(peak_i), cyf(iy(alts[peak_i]))
        peak_h = int(times_h[peak_i]) % 24
        peak_m = int((times_h[peak_i] % 1) * 60)
        s.append(f'<circle cx="{px:.1f}" cy="{py:.1f}" r="3.5" fill="#ba7517"/>')
        p_anchor = "start" if px < lm + pw * 0.6 else "end"
        p_ox = 7 if p_anchor == "start" else -7
        s.append(f'<text x="{px + p_ox:.1f}" y="{py - 7:.1f}" fill="#8a560f" '
                 f'font-size="10" font-weight="500" text-anchor="{p_anchor}">'
                 f'\u25b2 {alts[peak_i]:.0f}&#176; @ {peak_h:02d}:{peak_m:02d}</text>')
        # Plot border
        s.append(f'<rect x="{lm:.1f}" y="{tm}" width="{pw:.1f}" height="{ph}" '
                 f'fill="none" stroke="#cdd5de" stroke-width="1"/>')
        s.append('</svg>')
        return "".join(s)

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

        # Group by (target_id, scope, camera) — a CaptureSequenceList is
        # loaded into NINA for one connected camera at a time, so two rigs
        # sharing a scope (e.g. one camera on LRGB, another on SHO) must
        # never be merged into the same list.
        seen_groups = []
        for e in entries:
            key = (e["target_id"], e.get("scope", ""), e.get("camera", ""))
            if key not in seen_groups:
                seen_groups.append(key)

        lines = ['<?xml version="1.0" encoding="utf-8"?>']
        lines.append('<ArrayOfCaptureSequenceList '
                     'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" '
                     'xmlns:xsd="http://www.w3.org/2001/XMLSchema">')

        for (tid, scope_key, camera_key) in seen_groups:
            target_entries = [e for e in entries
                              if e["target_id"] == tid and e.get("scope", "") == scope_key
                              and e.get("camera", "") == camera_key]
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

        If entries use more than one rig \u2014 a scope+camera combo, which
        includes two cameras sharing one scope (e.g. an LRGB camera and a
        narrowband camera riding the same telescope) \u2014 a separate file is
        written per rig (the user picks a directory and filenames are
        auto-generated). When every entry shares a single rig the original
        single-file dialog is used. A CaptureSequenceList file is loaded
        into NINA for whichever camera is currently connected, so two rigs
        must never end up combined into one file even if they share a scope.
        """
        if not self._plan_entries:
            messagebox.showwarning("Empty Plan",
                "Tonight's plan is empty \u2014 add some targets first.")
            return

        # Determine distinct rigs (scope, camera) in the plan.
        distinct_rigs = []
        for e in self._plan_entries:
            rig = (e.get("scope", "") or "", e.get("camera", "") or "")
            if rig not in distinct_rigs:
                distinct_rigs.append(rig)

        date_tag = datetime.now().strftime("Session_%Y-%m-%d")

        if len(distinct_rigs) <= 1:
            # ---------- single-rig: original behaviour ----------
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
            # ---------- multi-rig: one file per scope+camera ----------
            out_dir = filedialog.askdirectory(
                title="Choose folder for per-rig NINA exports",
                initialdir=str(self._get_sessions_dir()),
            )
            if not out_dir:
                return

            saved_paths = []
            for scope_name, camera_name in distinct_rigs:
                rig_entries = [e for e in self._plan_entries
                               if (e.get("scope", "") or "") == scope_name
                               and (e.get("camera", "") or "") == camera_name]
                # Filesystem-safe scope AND camera \u2014 both are needed so two
                # cameras sharing one scope don't collide on one filename.
                safe_scope  = re.sub(r'[<>:"/\\|?*]', '_', scope_name)  if scope_name  else "NoScope"
                safe_camera = re.sub(r'[<>:"/\\|?*]', '_', camera_name) if camera_name else "NoCamera"
                fname = f"{date_tag}_{safe_scope}_{safe_camera}.ninaTargetSet"
                fpath = os.path.join(out_dir, fname)

                xml, _ = self._build_nina_xml(rig_entries)
                try:
                    with open(fpath, "w", encoding="utf-8") as f:
                        f.write(xml)
                    saved_paths.append(fpath)
                except OSError as exc:
                    messagebox.showerror("Export Failed",
                        f"Could not write file for '{scope_name} / {camera_name}'.\n\nDetail: {exc}")
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
            # Pin every loaded target to the top of the browsing grid too —
            # same as a manually-added target (see _mark_grid_touched) —
            # so re-finding one of them right after loading never requires
            # a fresh search. Mark in REVERSE plan order: _mark_grid_touched
            # moves each id to the end of an insertion-ordered dict, and
            # _populate_visible_grid reads that dict newest-marked-first, so
            # marking the plan's LAST target first and its FIRST target last
            # makes the grid's pinned order match the plan's own top-to-
            # bottom order rather than reversing it. Dedup by target_id — a
            # multi-filter target has one plan row per filter.
            seen_ids = set()
            for e in reversed(self._plan_entries):
                tid = e.get("target_id")
                if tid and tid not in seen_ids:
                    seen_ids.add(tid)
                    self._mark_grid_touched(tid)
            self._refresh_plan_tree()
            self._refresh_visible_grid()
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

    def _scan_visible_targets(self, lat, lon, min_alt=20.0, obj_type="All Types",
                               mag_limit=None, use_surf_br=False, seasonal_only=True,
                               require_min_alt=True, progress_cb=None):
        """Scan the catalog, optionally gated on visibility above ``min_alt``.

        Shared by the "Visible Tonight" popup (``show_visible_tonight``) and
        the target-card browsing grid, so both compute visibility identically
        instead of duplicating the catalog scan.

        Also respects ``self.catalog_filter`` (the NGC/IC/Messier/Caldwell/
        Sharpless/Other toggle set edited via the ▽ button next to the Plan
        tab's search box) — same rule as the search suggestion popup: a
        candidate is kept only if its ``catalogs`` tag set overlaps the
        user's enabled catalogs, with no "rides along" exception. This is
        applied here, in the one scan both the grid and the popup share, so
        the catalog restriction is never missed by an individual caller.

        Args mirror the popup's filter controls: ``obj_type`` is a value from
        ``OBJECT_TYPE_FILTERS`` or "All Types"; ``mag_limit`` is a v-mag or
        surface-brightness ceiling (``use_surf_br`` picks which), or ``None``
        for no magnitude filter; ``seasonal_only`` restricts to the current
        season's target list (with an automatic full-catalog fallback if a
        type filter empties the seasonal list). ``require_min_alt`` (default
        True) is the Plan tab grid's "🌙 Tonight" / "🔭 All" mode switch —
        when False, every candidate that survives the type/magnitude/seasonal
        filters is included regardless of tonight's altitude, for browsing or
        planning ahead; ``min_alt`` is still passed to ``_alt_rise_tonight``
        either way, so ``rise_label`` stays meaningful ("rises above 20° at
        22:15") even when it isn't being used to exclude anything.
        ``progress_cb(i, total)``, if given, is called every 20 candidates
        during the (potentially slow, ~13,000-object) altitude scan — callers
        on the UI thread should run this whole method in a background
        thread, same as the popup does.

        Returns ``(results, fallback_note)`` where ``results`` is a list of
        ``(target_dict, max_alt, rise_label)`` tuples sorted by altitude
        descending, and ``fallback_note`` is a user-facing string (possibly
        empty) describing the seasonal-fallback case above.
        """
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
        if obj_type != "All Types":
            filtered = [t for t in candidates if t.get("obj_type") == obj_type]
            # If seasonal mode + type filter yields nothing (e.g. Spring has no nebulae),
            # fall back to the full catalog for that type so results are never silently empty.
            if not filtered and seasonal_only:
                seen = set()
                filtered = []
                for t in self.targets.values():
                    if t["id"] not in seen and t.get("obj_type") == obj_type:
                        seen.add(t["id"])
                        filtered.append(t)
                fallback_note = f"ℹ️ No {obj_type} in seasonal list — searched full catalog.  "
            else:
                fallback_note = ""
            candidates = filtered

        # Apply catalog filter — same rule as the search suggestion popup
        # (_update_floating_suggestions): keep only targets whose catalog
        # tags overlap the user's enabled set. Untagged/unknown entries
        # carry an implicit "Other" tag, matching the popup's fallback.
        enabled_catalogs = {c for c, on in self.catalog_filter.items() if on}
        candidates = [t for t in candidates if (t.get("catalogs") or {"Other"}) & enabled_catalogs]

        if not candidates:
            return [], fallback_note

        dark_range = _dark_jd_range(lat, lon)
        results = []
        for i, t in enumerate(candidates):
            if progress_cb and i % 20 == 0:
                progress_cb(i, len(candidates))

            # Magnitude filter (applied before the expensive altitude calc)
            if mag_limit is not None:
                mag_val = t.get("surf_br") if use_surf_br else t.get("v_mag")
                if mag_val is None or mag_val > mag_limit:
                    continue

            max_alt, rise_label = _alt_rise_tonight(
                t["ra_deg"], t["dec_deg"], lat, lon, min_alt, dark_range)
            if not require_min_alt or max_alt >= min_alt:
                results.append((t, max_alt, rise_label))

        # Sort by altitude descending
        results.sort(key=lambda x: x[1], reverse=True)
        return results, fallback_note

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
                def _report_progress(i, total):
                    popup.after(0, lambda v=i: prog_label.config(
                        text=f"Checking {v}/{total}…", foreground="orange"))

                # Re-filters candidates (cheap — dict iteration over the
                # catalog) before repeating the same expensive per-candidate
                # altitude scan the "No candidates matched" check above already
                # sized; fallback_note is recomputed identically to the outer
                # one from the same inputs, so the outer copy (used in _show
                # below) is kept and this one is discarded.
                results, _fallback_note = self._scan_visible_targets(
                    lat, lon, min_alt=min_alt, obj_type=type_filter,
                    mag_limit=mag_limit, use_surf_br=use_surf_br,
                    seasonal_only=seasonal_only, progress_cb=_report_progress)

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

    def _filter_mode_choices(self):
        """Return the filter_mode dropdown's current options: Mono Lum, then
        every user-managed Filter Set name (see the Filter Library panel /
        self.data["filter_sets"]), alphabetically.

        A rig no longer points at one specific filter or a generic "LRGB"
        preset — it points at a whole Filter Set (what's actually loaded in
        your filter wheel, e.g. Ha/OIII/SII/L/R/G/B all together), so
        "Add to Tonight" can fan out to every filter in it at once. Shared
        by every place that used to hardcode a filter list — the chips row,
        the equipment drawer, Explore's Rig Builder, and the Edit Rig
        dialog — so all of them always agree on what's selectable.
        """
        return ["Mono Lum"] + sorted(self.data.get("filter_sets", {}).keys())

    def _rf_for_filter_mode(self, mode):
        """Return the sky-background reduction factor for a given filter_mode value.

        Mono Lum:     1.0 (no filter wheel).
        A Filter Set: looked up in self.data["filter_sets"]. A set can mix
                      narrowband and LRGB channels in one wheel, but the app
                      only recommends ONE sub-exposure length for the whole
                      set (same simplification it already made for LRGB
                      alone) — so a set containing ANY narrowband member
                      uses that set's shared bandwidth (rf = 348 / nm; a
                      real astro session mixing NB and broadband usually
                      sizes subs for the narrowband requirement anyway,
                      since broadband tolerates shorter subs but narrowband
                      doesn't). A pure-LRGB set uses the flat 3.0 broadband
                      factor.
        Legacy:       a pre-Filter-Set bandwidth-only string (e.g.
                      "NB (7nm)") or a bare "LRGB" from before Filter Sets
                      existed still resolves, so old rigs/plan entries keep
                      working without a forced migration.
        Unknown:      falls back to 1.0 (no suppression)
        """
        if mode == "Mono Lum" or mode == "None (Luminance)":
            return 1.0
        fs = self.data.get("filter_sets", {}).get(mode)
        if fs:
            members = fs.get("members", [])
            has_nb = any(m.get("type") == "narrowband" for m in members)
            if has_nb and fs.get("bandwidth_nm"):
                return 348.0 / max(float(fs["bandwidth_nm"]), 0.1)
            if members:
                return 3.0   # pure-LRGB set
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

        For a color camera or Mono Lum, returns ['Lum']. Otherwise
        filter_mode names a Filter Set, and this returns EVERY member's
        name in the order they were added to that set (a mixed wheel might
        be ['L', 'R', 'G', 'B', 'Ha', 'OIII', 'SII']) — this is what makes
        "Add to Tonight" fan out to one plan row per filter in the set.
        Falls back to ['Lum'] for a legacy value that no longer names a
        real set (e.g. a pre-Filter-Set "NB (7nm)" or "LRGB").
        """
        cam = self.data["cameras"].get(self.camera_choice.get(), {})
        if cam.get("is_color", True):
            return ["Lum"]                          # color — single-pass

        mode = self.filter_mode.get()
        if mode == "Mono Lum":
            return ["Lum"]
        fs = self.data.get("filter_sets", {}).get(mode)
        if fs and fs.get("members"):
            return [m["name"] for m in fs["members"]]
        return ["Lum"]                              # legacy/unrecognized — safe fallback

    def show_integration_plan(self, silent=False):
        """Compute integration-plan figures (subs, total time, SNR, overhead).

        Uses the sub-exposure and sky-flux values stored by the most recent
        analyze_framing() run, and the allocated-hours state in
        session_hours_var.  All values are purely computational — no
        network calls needed. The StringVars this writes into are headless
        now (the Targets List tab that displayed them as stat cards is
        retired) but are still read internally, so this keeps running on
        every analysis. Pass silent=True (auto-calls from analyze_framing)
        to suppress warning dialogs.
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

        # ── Update headless integration-plan state ──────────────────────
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

            # 2b) Merge into the Filter Library (self.data["filter_sets"]) —
            # NINA exports a whole filter wheel in one profile, which maps
            # naturally onto one Filter Set. Named after the imported scope
            # when there is one (a wheel is normally tied to a specific
            # rig/scope), else a generic fallback name. Unlike nina_filters
            # above (a snapshot of the LAST import, wholly overwritten each
            # time), this only ADDS the names this import found — members
            # already in the set from elsewhere are left alone — and
            # refreshes the set's one shared bandwidth for its narrowband
            # members (all NB filters in a set share one bandwidth).
            if lrgb_names or nb_names:
                set_name = scope_info["name"] if scope_info is not None else "Imported Filters"
                filter_sets = self.data.setdefault("filter_sets", {})
                fs = filter_sets.setdefault(set_name, {"bandwidth_nm": None, "members": []})
                existing_member_names = {m["name"] for m in fs["members"]}
                for _name in lrgb_names:
                    if _name not in existing_member_names:
                        fs["members"].append({"name": _name, "type": "lrgb"})
                        existing_member_names.add(_name)
                for _name in nb_names:
                    if _name not in existing_member_names:
                        fs["members"].append({"name": _name, "type": "narrowband"})
                        existing_member_names.add(_name)
                if chosen_bw is not None:
                    fs["bandwidth_nm"] = float(chosen_bw)

            self._persist_data()

            # 3) Refresh UI — inventory tables pick up the new scope, dropdowns
            #    pick up the new filter bandwidth entry.
            self.refresh_inventory_tables()
            self.refresh_dropdowns()

            # We do NOT touch the active filter selection here — same rule as
            # the scope above. This used to auto-select the just-imported
            # bandwidth (e.g. "NB (6.5nm)"), which silently swapped the live
            # filter chip out from under whatever rig was active — surprising
            # since a NINA import is often run just to refresh scope data,
            # not to change which filter you're using right now. The
            # imported bandwidth is still added to the dropdown's values (see
            # refresh_dropdowns) so it's available to pick deliberately.

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
        """Show or hide the filter-mode chip based on whether the selected
        camera is mono — in the Planner tab's chips bar, and (independently)
        the equipment drawer's own filter row/combobox if the drawer
        happens to be open (a separate widget bound to the same
        self.filter_mode StringVar — see _open_equip_drawer for why it
        can't just be the same widget moved back and forth)."""
        c = self.data["cameras"].get(self.camera_choice.get())
        show = bool(c and not c.get("is_color", True))
        if show:
            if not self.filter_dropdown.winfo_ismapped():
                self.filter_dropdown.pack(side="left", padx=(0, 6),
                                          before=self._equip_summary_label)
        else:
            self.filter_dropdown.pack_forget()

        drawer_row = getattr(self, "_drawer_filter_row", None)
        if drawer_row is not None and drawer_row.winfo_exists():
            if show:
                drawer_row.pack(fill="x", pady=(0, 6))
            else:
                drawer_row.pack_forget()

    def _mark_analysis_dirty(self):
        """Flag that input data has changed and analysis needs to re-run.

        If the Target Planner tab is currently visible, schedules
        analyze_framing via after() so the tab can finish rendering first.
        If a different tab is active, just sets the flag — _on_tab_changed
        will pick it up when the user returns to the Planner tab.
        """
        self._analysis_dirty = True
        try:
            current = self.tab_control.select()
            if current == str(self.tab_planner):
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
        # Only run if the Planner tab is still active — the user may have
        # switched away during the after(100) delay.  If so, leave the
        # dirty flag set; _on_tab_changed will pick it up later.
        try:
            current = self.tab_control.select()
            if current != str(self.tab_planner):
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
            "filter":    self.filter_mode.get() if is_mono else "",
        }

    def _chips_match_rig(self, rig):
        """Return True if current chip values match every key in the rig snapshot."""
        snap = self._current_rig_snapshot()
        for key in ("scope", "reduction", "camera", "filter"):
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
        # Explore's saved-rig quick-add mirrors the same list
        if hasattr(self, "_explore_rig_pick"):
            self._explore_rig_pick["values"] = names
        # Equipment tab's inline Rig Library mirrors the same list
        if hasattr(self, "_equip_rig_refresh"):
            self._equip_rig_refresh(preserve_name=self.data.get("settings", {}).get("active_rig", ""))
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

    def _build_rig_list_and_details(self, list_parent, details_parent, *, list_height=8, list_width=22):
        """Build a saved-rigs Listbox plus a Scope/Reducer/Camera/Filter
        details grid, wired together by a shared selection handler.

        Shared by the modal Manage Rigs dialog and the Equipment tab's inline
        Rig Library panel so both present identical widgets and behavior from
        one place, instead of two copies drifting apart. Neither widget is
        packed/gridded here — the caller places them (list_parent and
        details_parent may be the same frame, as in the inline panel, or two
        different frames side by side, as in the dialog). Returns
        ``(listbox, details, name_list, detail_labels, refresh_list)`` — call
        ``refresh_list()`` whenever the underlying rig data changes, and
        ``refresh_list(preserve_name=...)`` to keep a given rig selected
        across the rebuild.
        """
        lb = tk.Listbox(list_parent, height=list_height, width=list_width,
                        font=("Helvetica", 11),
                        bg="#1e2d3e", fg="#ffffff",
                        selectbackground="#1e3a5f", selectforeground="#aaddff",
                        relief="flat", borderwidth=1,
                        exportselection=False, activestyle="none")

        name_list = []   # parallel index → rig name (for reliable lookup)

        details = tk.Frame(details_parent, bg="#0e1a28")
        detail_labels = {}
        for i, k in enumerate(["Scope", "Reducer", "Camera", "Filter"]):
            tk.Label(details, text=f"{k}:", bg="#0e1a28", fg="#8a94a3",
                     font=("Helvetica", 10), anchor="w", width=8
                     ).grid(row=i, column=0, sticky="w", padx=(10, 4), pady=3)
            lbl = tk.Label(details, text="—", bg="#0e1a28", fg="#cfd4dc",
                           font=("Helvetica", 10), anchor="w")
            lbl.grid(row=i, column=1, sticky="w", padx=(0, 10), pady=3)
            detail_labels[k.lower()] = lbl

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
            # Filter only applies to mono cameras; show em-dash for color rigs
            rig_cam = rig.get("camera", "")
            if rig_cam and self._camera_is_color(rig_cam):
                detail_labels["filter"].configure(text="— (color sensor)")
            else:
                filt_val = rig.get("filter") or ""
                fs = self.data.get("filter_sets", {}).get(filt_val)
                if fs:
                    n = len(fs.get("members", []))
                    detail_labels["filter"].configure(
                        text=f"{filt_val}  ({n} filter{'s' if n != 1 else ''})")
                else:
                    detail_labels["filter"].configure(text=filt_val or "(none)")

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

        lb.bind("<<ListboxSelect>>", _on_select)
        return lb, details, name_list, detail_labels, _refresh_list

    def _rig_manager_rename(self, name_list, lb, refresh_list, parent):
        """Rename the rig currently selected in ``lb``.

        Shared by the Manage Rigs dialog and the Equipment tab's inline Rig
        Library — ``parent`` is whichever window should own the rename
        prompt's modal grab (the dialog itself, or ``self.root`` for the
        inline panel).
        """
        sel = lb.curselection()
        if not sel or sel[0] >= len(name_list):
            return
        name = name_list[sel[0]]
        rig = self._find_rig(name)
        if not rig:
            return
        new_name = simpledialog.askstring("Rename Rig",
                                           f"Rename '{name}' to:",
                                           parent=parent, initialvalue=name)
        if new_name is None:
            return
        new_name = new_name.strip()
        if not new_name or new_name == name:
            return
        if new_name == "Custom…":
            messagebox.showerror("Reserved name",
                                 "\"Custom…\" is a reserved label.", parent=parent)
            return
        if self._find_rig(new_name):
            messagebox.showerror("Name exists",
                                 f"A rig named '{new_name}' already exists.",
                                 parent=parent)
            return
        rig["name"] = new_name
        settings = self.data.setdefault("settings", {})
        if settings.get("active_rig") == name:
            settings["active_rig"] = new_name
        self._persist_data()
        refresh_list(preserve_name=new_name)
        self._refresh_rig_dropdown()

    def _rig_manager_update(self, name_list, lb, refresh_list, parent):
        """Overwrite the rig selected in ``lb`` with the current chip values.

        Shared by the Manage Rigs dialog and the Equipment tab's inline Rig
        Library — see :meth:`_rig_manager_rename` for the ``parent`` argument.
        """
        sel = lb.curselection()
        if not sel or sel[0] >= len(name_list):
            return
        name = name_list[sel[0]]
        rig = self._find_rig(name)
        if not rig:
            return
        if not messagebox.askyesno("Update Rig",
                                    f"Overwrite '{name}' with the current equipment settings?\n\n"
                                    "This replaces the saved snapshot with whatever's selected in "
                                    "the planner right now.",
                                    parent=parent):
            return
        rig.update(self._current_rig_snapshot())
        # Rig now matches current chips → make it active
        self.data.setdefault("settings", {})["active_rig"] = name
        self._persist_data()
        refresh_list(preserve_name=name)
        self._refresh_rig_dropdown()
        self._show_toast(f"Updated rig: {name}")

    def _rig_manager_delete(self, name_list, lb, refresh_list, parent):
        """Delete the rig selected in ``lb`` after confirmation.

        Shared by the Manage Rigs dialog and the Equipment tab's inline Rig
        Library — see :meth:`_rig_manager_rename` for the ``parent`` argument.
        """
        sel = lb.curselection()
        if not sel or sel[0] >= len(name_list):
            return
        name = name_list[sel[0]]
        if not messagebox.askyesno("Delete Rig",
                                    f"Delete the rig '{name}'?\n\nThis cannot be undone.",
                                    parent=parent):
            return
        settings = self.data.setdefault("settings", {})
        rigs = settings.setdefault("rigs", [])
        rigs[:] = [r for r in rigs if r.get("name") != name]
        if settings.get("active_rig") == name:
            settings["active_rig"] = ""
        self._persist_data()
        refresh_list()
        self._refresh_rig_dropdown()
        self._show_toast(f"Deleted rig: {name}")

    def _rig_manager_edit(self, name_list, lb, refresh_list, parent):
        """Open a direct editor for the rig selected in ``lb``.

        Unlike :meth:`_rig_manager_update` (which overwrites the rig with
        whatever the live equipment chips currently hold — fine when those
        chips are right there in the same drawer, confusing on the
        Equipment tab where they aren't visible at all), this edits the
        rig's own stored scope/reducer/camera/filter values directly
        and only writes anything on an explicit Save.
        """
        sel = lb.curselection()
        if not sel or sel[0] >= len(name_list):
            return
        name = name_list[sel[0]]
        if not self._find_rig(name):
            return
        self._open_edit_rig_dialog(name, refresh_list, parent)

    def _open_edit_rig_dialog(self, name, refresh_list, parent):
        """Modal add/edit dialog for one saved rig's name/scope/reducer/camera/filter.

        ``name=None`` opens a blank "New Rig" form; otherwise pre-fills from
        that rig's own saved snapshot — never from the live equipment chips
        (self.scope_choice etc.), since those live on the Planner tab /
        Plan-tab drawer, which may not even be open, so adding or editing a
        rig here shouldn't depend on their current state. The Name field is
        editable in BOTH modes — renaming is just "change the Name field and
        Save" here, no separate Rename button (Jerry: "move the rename
        function into the edit function ... remove the 'rename' button").
        Nothing is written until Save is clicked. Mirrors
        :meth:`_open_edit_filter_dialog`'s same add/edit-in-one-dialog shape.
        """
        is_new = name is None
        rig = None if is_new else self._find_rig(name)
        if not is_new and rig is None:
            return

        dlg = tk.Toplevel(parent)
        dlg.title("New Rig" if is_new else f"Edit Rig — {name}")
        dlg.resizable(False, False)
        dlg.grab_set()
        dlg.transient(self.root)

        ttk.Label(dlg, text=("New rig:" if is_new else f"Edit “{name}”:"),
                  font=("Helvetica", 11, "bold")).pack(padx=20, pady=(14, 10), anchor="w")

        form = ttk.Frame(dlg)
        form.pack(padx=20, pady=(0, 4), fill="x")

        _reduction_values = ["0.63×", "0.67×", "0.70×", "0.75×", "0.80×",
                              "1.0×", "1.5×", "2.0×", "2.5×", "3.0×"]
        _filter_values = self._filter_mode_choices()

        name_var = tk.StringVar(value="" if is_new else name)
        ttk.Label(form, text="Name:").grid(row=0, column=0, sticky="e", padx=(0, 8), pady=4)
        name_entry = ttk.Entry(form, textvariable=name_var, width=24)
        name_entry.grid(row=0, column=1, sticky="w", pady=4)
        next_row = 1

        scope_var  = tk.StringVar(value=rig.get("scope", "")     if rig else "")
        reduc_var  = tk.StringVar(value=(rig.get("reduction") if rig else None) or "1.0×")
        camera_var = tk.StringVar(value=rig.get("camera", "")    if rig else "")
        filter_var = tk.StringVar(value=(rig.get("filter") if rig else None) or "Mono Lum")

        rows = [
            ("Scope:",   scope_var,  sorted(self.data.get("scopes", {}).keys())),
            ("Reducer:", reduc_var,  _reduction_values),
            ("Camera:",  camera_var, sorted(self.data.get("cameras", {}).keys())),
        ]
        for i, (label, var, values) in enumerate(rows):
            r = next_row + i
            ttk.Label(form, text=label).grid(row=r, column=0, sticky="e", padx=(0, 8), pady=4)
            ttk.Combobox(form, textvariable=var, values=values,
                         state="readonly", width=22).grid(row=r, column=1, sticky="w", pady=4)

        # Filter row is shown/hidden based on whether the chosen camera is
        # mono — same rule used everywhere else a rig's filter is displayed.
        filter_row_idx = next_row + len(rows)
        filter_label = ttk.Label(form, text="Filter:")
        filter_dd = ttk.Combobox(form, textvariable=filter_var, values=_filter_values,
                                  state="readonly", width=22)

        def _sync_filter_visibility(*_a):
            if camera_var.get() and not self._camera_is_color(camera_var.get()):
                filter_label.grid(row=filter_row_idx, column=0, sticky="e", padx=(0, 8), pady=4)
                filter_dd.grid(row=filter_row_idx, column=1, sticky="w", pady=4)
            else:
                filter_label.grid_forget()
                filter_dd.grid_forget()
        camera_var.trace_add("write", _sync_filter_visibility)
        _sync_filter_visibility()

        btn_row = ttk.Frame(dlg)
        btn_row.pack(padx=20, pady=(10, 14), fill="x")

        def _do_save():
            is_mono = bool(camera_var.get()) and not self._camera_is_color(camera_var.get())

            new_name = name_var.get().strip()
            if not new_name:
                messagebox.showerror("Invalid name",
                                     "Please enter a name for this rig.", parent=dlg)
                return
            if new_name == "Custom…":
                messagebox.showerror("Reserved name",
                                     "\"Custom…\" is a reserved label — please choose another name.",
                                     parent=dlg)
                return
            if new_name != name and self._find_rig(new_name):
                messagebox.showerror("Name exists",
                                     f"A rig named '{new_name}' already exists.", parent=dlg)
                return

            if is_new:
                new_rig = {
                    "name":      new_name,
                    "scope":     scope_var.get(),
                    "reduction": reduc_var.get(),
                    "camera":    camera_var.get(),
                    "filter":    filter_var.get() if is_mono else "",
                }
                self.data.setdefault("settings", {}).setdefault("rigs", []).append(new_rig)
                self._persist_data()
                if refresh_list is not None:
                    refresh_list(preserve_name=new_name)
                self._refresh_rig_dropdown()
                self._show_toast(f"Added rig: {new_name}")
            else:
                renamed = new_name != name
                settings = self.data.setdefault("settings", {})
                was_active = settings.get("active_rig", "") == name
                rig["name"]      = new_name
                rig["scope"]     = scope_var.get()
                rig["reduction"] = reduc_var.get()
                rig["camera"]    = camera_var.get()
                rig["filter"]    = filter_var.get() if is_mono else ""
                if was_active and renamed:
                    settings["active_rig"] = new_name
                self._persist_data()
                if refresh_list is not None:
                    refresh_list(preserve_name=new_name)
                self._refresh_rig_dropdown()
                # If this rig is the currently-active one, push the edit into
                # the live equipment chips too, so "active" doesn't silently
                # drift from what's actually saved.
                if was_active:
                    self._apply_rig(rig)
                self._show_toast(f"Renamed to '{new_name}' and updated" if renamed
                                  else f"Updated rig: {new_name}")
            dlg.destroy()

        ttk.Button(btn_row, text="Cancel", command=dlg.destroy).pack(side="right", padx=(6, 0))
        ttk.Button(btn_row, text="Save",   command=_do_save   ).pack(side="right")
        dlg.bind("<Return>", lambda e: _do_save())
        dlg.bind("<Escape>", lambda e: dlg.destroy())
        name_entry.focus_set()
        if is_new:
            name_entry.select_range(0, tk.END)

        self._theme_popup(dlg)

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

    # ── Filter Library (Filter Sets) ─────────────────────────────────────────
    # Mirrors the Rig Library's list+detail-card+New/Edit/Delete pattern
    # (_build_rig_list_and_details / _rig_manager_* / _open_edit_rig_dialog
    # above) so both "always-visible libraries" on the Equipment tab look
    # and behave the same way. self.data["filter_sets"] is the single
    # source of truth (schema 8+) — a rig points at a whole Filter Set
    # (its filter wheel's contents) rather than one filter at a time; see
    # _filter_mode_choices, _rf_for_filter_mode, _get_filter_names, and
    # _split_entry_into_plan for how a set is consumed.

    def _filter_row_color(self, name, ftype):
        """A small color cue for a filter member's row — recognizable film
        names get their familiar color (Ha red, OIII/B cyan, SII/L/etc.),
        anything custom falls back to a generic narrowband/LRGB tint."""
        palette = {
            "ha": "#ff8a80", "oiii": "#80d8ff", "sii": "#ffcc80",
            "l": "#e5e7eb", "r": "#ff8a80", "g": "#80e5a0", "b": "#80d8ff",
        }
        key = name.strip().lower()
        if key in palette:
            return palette[key]
        return "#c9a8ff" if ftype == "narrowband" else "#cfd4dc"

    def _build_filter_set_list_and_details(self, list_parent, details_parent, *, list_height=7, list_width=20):
        """Build a Filter Set Listbox plus a Name/Bandwidth/Members details
        grid, wired together by a shared selection handler — the Filter
        Library counterpart of :meth:`_build_rig_list_and_details`.

        Each row is one whole Filter Set (e.g. "Wheel A  (7)"), not one
        individual filter — a set is what a rig actually points at now, and
        its member filters (name + narrowband/LRGB type) are added/removed
        from inside the Set's own New/Edit dialog, not managed as a
        separate top-level list.

        Returns ``(listbox, details, name_list, detail_labels, refresh_list)``.
        """
        lb = tk.Listbox(list_parent, height=list_height, width=list_width,
                        font=("Helvetica", 11),
                        bg="#1e2d3e", fg="#ffffff",
                        selectbackground="#1e3a5f", selectforeground="#aaddff",
                        relief="flat", borderwidth=1,
                        exportselection=False, activestyle="none")

        name_list = []   # parallel index → set name, or None for the empty-state row

        details = tk.Frame(details_parent, bg="#0e1a28")
        detail_labels = {}
        for i, k in enumerate(["Name", "Bandwidth", "Members"]):
            tk.Label(details, text=f"{k}:", bg="#0e1a28", fg="#8a94a3",
                     font=("Helvetica", 10), anchor="w", width=8
                     ).grid(row=i, column=0, sticky="nw", padx=(10, 4), pady=3)
            lbl = tk.Label(details, text="—", bg="#0e1a28", fg="#cfd4dc",
                           font=("Helvetica", 10), anchor="w", justify="left", wraplength=170)
            lbl.grid(row=i, column=1, sticky="w", padx=(0, 10), pady=3)
            detail_labels[k.lower()] = lbl

        def _on_select(event=None):
            sel = lb.curselection()
            if not sel or sel[0] >= len(name_list) or name_list[sel[0]] is None:
                for lbl in detail_labels.values():
                    lbl.configure(text="—")
                return
            name = name_list[sel[0]]
            fs = self.data.get("filter_sets", {}).get(name)
            if fs is None:
                return
            members = fs.get("members", [])
            has_nb = any(m.get("type") == "narrowband" for m in members)
            bw = fs.get("bandwidth_nm")
            detail_labels["name"].configure(text=name)
            detail_labels["bandwidth"].configure(text=f"{bw:g} nm" if (has_nb and bw) else "—")
            detail_labels["members"].configure(
                text=", ".join(m["name"] for m in members) if members else "(empty)")

        def _refresh_list(preserve_name=None):
            filter_sets = self.data.get("filter_sets", {})
            lb.delete(0, tk.END)
            name_list.clear()
            for set_name in sorted(filter_sets.keys()):
                n = len(filter_sets[set_name].get("members", []))
                lb.insert(tk.END, f"{set_name}  ({n})")
                name_list.append(set_name)
            if not name_list:
                lb.insert(tk.END, "  (no filter sets yet — click + New)")
                name_list.append(None)
                lb.itemconfig(tk.END, fg="#556677")

            if preserve_name and preserve_name in name_list:
                idx = name_list.index(preserve_name)
                lb.selection_set(idx)
                lb.see(idx)
            else:
                for i, n in enumerate(name_list):
                    if n is not None:
                        lb.selection_set(i)
                        break
            _on_select()

        lb.bind("<<ListboxSelect>>", _on_select)
        return lb, details, name_list, detail_labels, _refresh_list

    def _filter_manager_edit(self, name_list, lb, refresh_list, parent):
        """Open the add/edit dialog for the filter set currently selected in ``lb``."""
        sel = lb.curselection()
        if not sel or sel[0] >= len(name_list) or name_list[sel[0]] is None:
            return
        name = name_list[sel[0]]
        self._open_edit_filter_dialog(name, refresh_list, parent)

    def _filter_manager_delete(self, name_list, lb, refresh_list, parent):
        """Delete the filter set selected in ``lb`` after confirmation.

        No "in use" check: existing rigs/plan entries that reference a
        deleted set's name degrade gracefully (see _rf_for_filter_mode /
        _get_filter_names' legacy fallbacks) — exactly like deleting a
        camera or scope that's still referenced elsewhere isn't blocked
        either.
        """
        sel = lb.curselection()
        if not sel or sel[0] >= len(name_list) or name_list[sel[0]] is None:
            return
        name = name_list[sel[0]]
        if not messagebox.askyesno(
                "Delete Filter Set",
                f"Delete the filter set '{name}'?\n\n"
                "This cannot be undone. Rigs or Tonight's Plan entries that "
                "already reference it keep working — they just won't offer "
                "it as a selectable option going forward.",
                parent=parent):
            return
        filter_sets = self.data.setdefault("filter_sets", {})
        filter_sets.pop(name, None)
        if self.filter_mode.get() == name:
            self.filter_mode.set("Mono Lum")
        self._persist_data()
        self.refresh_dropdowns()
        refresh_list()
        self._show_toast(f"Deleted filter set: {name}")

    def _open_edit_filter_dialog(self, name, refresh_list, parent):
        """Modal add/edit dialog for one Filter Set (Filter Library).

        ``name=None`` opens a blank "New Filter Set" form; otherwise
        pre-fills from ``self.data["filter_sets"][name]`` and allows
        renaming it — same editable-Name-field, no-separate-Rename-button
        pattern as the Rig Library's Edit dialog. Filters are added to and
        removed from the set directly here (name + type), "similar to the
        way you add components to a rig" — they aren't drawn from a
        separate global filter catalog, so the same filter name can appear
        independently in different sets. Nothing is written until Save is
        clicked.

        Adding filters is split into two modes via a segmented LRGB/
        Narrowband toggle (Jerry: adding narrowband filters "feels
        awkward" one at a time):
          • LRGB channel (the default for a new/pure-LRGB set) — a single
            name field + "+", same as before.
          • Narrowband — expands to three named slots (Hα / S2 / O3),
            each pre-filled with the conventional name but freely
            editable, with the set's shared bandwidth field right below
            them (Jerry: "to make it simple, all NB filters in a set must
            have the same bandwidth"). "+ Add narrowband filters" adds
            every non-blank slot at once, all using that one bandwidth.
        Editing a set that already has a narrowband member opens straight
        into Narrowband mode instead, so its shared bandwidth is visible
        (and editable via Save alone, with no new filters added) without
        having to hunt for the toggle first.
        """
        existing = self.data.get("filter_sets", {}).get(name) if name else None
        # Local working copy — nothing touches self.data until Save.
        members = [dict(m) for m in (existing.get("members", []) if existing else [])]
        has_existing_nb = any(m["type"] == "narrowband" for m in members)

        dlg = tk.Toplevel(parent)
        dlg.title(f"Edit Filter Set — {name}" if name else "New Filter Set")
        dlg.resizable(False, False)
        dlg.grab_set()
        dlg.transient(self.root)

        # A plain "hint text" placeholder for the LRGB add field — same
        # dedicated-style-swap pattern as the Planner tab's catalog search
        # box (see _plan_search_show_placeholder): a derived style only
        # ever grey while the placeholder itself is showing, so real typed
        # text keeps tracking the normal day/night Entry color instead of
        # getting stuck grey.
        style = ttk.Style()
        style.configure("FSAdd.TEntry")
        style.configure("FSAddPlaceholder.TEntry", foreground="#667788")
        style.map("FSAddPlaceholder.TEntry",
                  foreground=[("!disabled", "#667788"), ("focus", "#667788"), ("", "#667788")])

        ttk.Label(dlg, text=(f"Edit “{name}”:" if name else "New filter set:"),
                  font=("Helvetica", 11, "bold")).pack(padx=20, pady=(14, 10), anchor="w")

        name_row = ttk.Frame(dlg)
        name_row.pack(padx=20, fill="x")
        ttk.Label(name_row, text="Name:", width=9).pack(side="left")
        name_var = tk.StringVar(value=name or "")
        name_entry = ttk.Entry(name_row, textvariable=name_var, width=26)
        name_entry.pack(side="left", padx=(4, 0))

        ttk.Label(dlg, text="Filters in this set:",
                  font=("Helvetica", 9, "bold"), foreground="#778899"
                  ).pack(padx=20, pady=(14, 4), anchor="w")

        # Listbox + an always-visible Scrollbar (not just once content
        # overflows) — a list with no visible scrollbar reads as "this is
        # all there is" rather than "there's more, scroll for it".
        members_wrap = tk.Frame(dlg, bg="#1e2d3e")
        members_wrap.pack(padx=20, fill="x")
        # A monospace font, not Helvetica — the row text below pads the
        # filter name to a fixed CHARACTER width so the type column lines
        # up, which only actually lines up in pixels with a fixed-width
        # font. In a proportional font, "Ha" and "OIII" pad out to the same
        # character count but very different pixel widths, so the type
        # column reads as trailing a fixed gap after each name instead of
        # sitting in a real aligned column.
        members_lb = tk.Listbox(members_wrap, height=6, width=32, font=("Courier New", 10),
                                bg="#1e2d3e", fg="#ffffff",
                                selectbackground="#1e3a5f", selectforeground="#aaddff",
                                relief="flat", borderwidth=1,
                                exportselection=False, activestyle="none")
        members_lb.pack(side="left", fill="both", expand=True)
        members_scroll = ttk.Scrollbar(members_wrap, orient="vertical", command=members_lb.yview)
        members_scroll.pack(side="right", fill="y")
        members_lb.configure(yscrollcommand=members_scroll.set)

        def _refresh_members_lb():
            members_lb.delete(0, tk.END)
            for m in members:
                label = "LRGB channel" if m["type"] == "lrgb" else "Narrowband"
                members_lb.insert(tk.END, f"  {m['name']:<14}{label}")
                members_lb.itemconfig(tk.END, fg=self._filter_row_color(m["name"], m["type"]))

        def _do_remove_member():
            sel = members_lb.curselection()
            if not sel or sel[0] >= len(members):
                return
            del members[sel[0]]
            _refresh_members_lb()

        remove_row = ttk.Frame(dlg)
        remove_row.pack(padx=20, pady=(4, 0), fill="x")
        remove_btn = ttk.Button(remove_row, text="✕ Remove selected", command=_do_remove_member)
        remove_btn.pack(side="right")
        ToolTip(remove_btn, "Remove the selected filter from this set")

        # ── Segmented LRGB channel / Narrowband toggle ──────────────────
        ttk.Label(dlg, text="Add filters:",
                  font=("Helvetica", 9, "bold"), foreground="#778899"
                  ).pack(padx=20, pady=(10, 4), anchor="w")

        # _theme_popup (called once, at the very end, so night mode's red
        # overlay reaches this dialog's plain-tk backgrounds) repaints
        # EVERY plain tk.Frame/Label to the theme's flat background —
        # including the deliberately-custom-colored ones below (the toggle
        # pills, the narrowband trio's card and band chips). Each is
        # registered here so _reapply_custom_colors() can restore them
        # right after that walk runs.
        _custom_colored = []   # (widget, bg, fg-or-None) pairs

        SEG_ACTIVE_BG, SEG_ACTIVE_FG = "#38bdf8", "#04202e"
        SEG_INACTIVE_BG, SEG_INACTIVE_FG = "#263545", "#8a94a3"
        add_mode_var = tk.StringVar(value="narrowband" if has_existing_nb else "lrgb")

        seg_frame = tk.Frame(dlg, bg="#1e2d3e")
        seg_frame.pack(padx=20, fill="x")
        seg_lrgb = tk.Label(seg_frame, text="LRGB channel", font=("Helvetica", 10, "bold"),
                             padx=10, pady=6, cursor="hand2")
        seg_nb = tk.Label(seg_frame, text="Narrowband", font=("Helvetica", 10, "bold"),
                           padx=10, pady=6, cursor="hand2")
        seg_lrgb.pack(side="left", fill="x", expand=True, padx=(0, 1))
        seg_nb.pack(side="left", fill="x", expand=True)

        # Both add-mode frames are built regardless of which is showing —
        # only one is ever packed at a time via _refresh_add_block().
        lrgb_add_frame = ttk.Frame(dlg)
        nb_add_frame = ttk.Frame(dlg)

        # Created (and packed) here, ahead of the Save/Cancel buttons that
        # get added into it later, purely so _refresh_add_block() below has
        # something to anchor "before" on. Without an anchor, pack_forget()
        # followed by pack() moves a frame to the END of dlg's packing
        # order instead of back to its original spot — which is what made
        # toggling LRGB -> Narrowband -> LRGB walk the add block below the
        # Save/Cancel row (and push that row up) instead of leaving both
        # exactly where they started.
        btn_row = ttk.Frame(dlg)
        btn_row.pack(padx=20, pady=(14, 14), fill="x")

        def _refresh_add_block():
            if add_mode_var.get() == "lrgb":
                nb_add_frame.pack_forget()
                lrgb_add_frame.pack(padx=20, pady=(8, 0), fill="x", before=btn_row)
            else:
                lrgb_add_frame.pack_forget()
                nb_add_frame.pack(padx=20, pady=(8, 0), fill="x", before=btn_row)

        def _update_segmented():
            if add_mode_var.get() == "lrgb":
                seg_lrgb.configure(bg=SEG_ACTIVE_BG, fg=SEG_ACTIVE_FG)
                seg_nb.configure(bg=SEG_INACTIVE_BG, fg=SEG_INACTIVE_FG)
            else:
                seg_lrgb.configure(bg=SEG_INACTIVE_BG, fg=SEG_INACTIVE_FG)
                seg_nb.configure(bg=SEG_ACTIVE_BG, fg=SEG_ACTIVE_FG)

        def _set_add_mode(mode):
            add_mode_var.set(mode)
            _update_segmented()
            _refresh_add_block()

        seg_lrgb.bind("<Button-1>", lambda e: _set_add_mode("lrgb"))
        seg_nb.bind("<Button-1>", lambda e: _set_add_mode("narrowband"))

        # ── LRGB channel add block: one name field + "+" ────────────────
        add_name_var = tk.StringVar(value="")
        add_entry = ttk.Entry(lrgb_add_frame, textvariable=add_name_var, width=22,
                               style="FSAdd.TEntry")
        add_entry.pack(side="left", fill="x", expand=True)

        _FS_ADD_PLACEHOLDER = "Filter name"
        _placeholder_state = {"active": False}

        def _show_add_placeholder():
            if add_name_var.get():
                return
            _placeholder_state["active"] = True
            add_entry.configure(style="FSAddPlaceholder.TEntry")
            add_name_var.set(_FS_ADD_PLACEHOLDER)

        def _clear_add_placeholder(event=None):
            if _placeholder_state["active"]:
                _placeholder_state["active"] = False
                add_name_var.set("")
                add_entry.configure(style="FSAdd.TEntry")

        add_entry.bind("<FocusIn>", _clear_add_placeholder)
        add_entry.bind("<FocusOut>", lambda e: _show_add_placeholder())

        def _do_add_lrgb_member(event=None):
            nm = add_name_var.get().strip()
            if not nm or _placeholder_state["active"]:
                return "break"
            if any(m["name"].lower() == nm.lower() for m in members):
                messagebox.showerror("Already in set",
                                     f"'{nm}' is already in this set.", parent=dlg)
                return "break"
            members.append({"name": nm, "type": "lrgb"})
            add_name_var.set("")
            _refresh_members_lb()
            add_entry.focus_set()
            return "break"

        add_lrgb_btn = ttk.Button(lrgb_add_frame, text="+", width=3, command=_do_add_lrgb_member)
        add_lrgb_btn.pack(side="left", padx=(4, 0))
        ToolTip(add_lrgb_btn, "Add this filter to the set")
        add_entry.bind("<Return>", _do_add_lrgb_member)
        _show_add_placeholder()

        # ── Narrowband add block: Hα / S2 / O3 slots + shared bandwidth ─
        existing_bw = existing.get("bandwidth_nm") if existing else None
        bw_var = tk.StringVar(value=f"{existing_bw:g}" if existing_bw else "7")
        ha_var, s2_var, o3_var = tk.StringVar(value="Ha"), tk.StringVar(value="SII"), tk.StringVar(value="OIII")

        TRIO_BG = "#1a2b3c"
        trio_card = tk.Frame(nb_add_frame, bg=TRIO_BG,
                              highlightbackground="#2e4a63", highlightthickness=1)
        trio_card.pack(fill="x")
        _custom_colored.append((trio_card, TRIO_BG, None))

        def _trio_slot(label_text, chip_bg, chip_fg, var):
            row = tk.Frame(trio_card, bg=TRIO_BG)
            row.pack(fill="x", padx=10, pady=(8, 0))
            _custom_colored.append((row, TRIO_BG, None))
            chip = tk.Label(row, text=label_text, bg=chip_bg, fg=chip_fg, font=("Helvetica", 9, "bold"),
                             width=3, padx=2, pady=2)
            chip.pack(side="left")
            _custom_colored.append((chip, chip_bg, chip_fg))
            e = ttk.Entry(row, textvariable=var, width=16)
            e.pack(side="left", padx=(8, 0), fill="x", expand=True)
            return e

        ha_entry = _trio_slot("Hα", "#3a1a1a", "#ff6b6b", ha_var)
        s2_entry = _trio_slot("S2", "#2a1a3a", "#b98bff", s2_var)
        o3_entry = _trio_slot("O3", "#1a2e3a", "#5fd0e8", o3_var)

        nb_bw_row = tk.Frame(trio_card, bg=TRIO_BG)
        nb_bw_row.pack(fill="x", padx=10, pady=(10, 4))
        _custom_colored.append((nb_bw_row, TRIO_BG, None))
        bw_label = tk.Label(nb_bw_row, text="NB Bandwidth (nm):", bg=TRIO_BG, fg="#8a94a3",
                             font=("Helvetica", 9))
        bw_label.pack(side="left")
        _custom_colored.append((bw_label, TRIO_BG, "#8a94a3"))
        bw_entry = ttk.Entry(nb_bw_row, textvariable=bw_var, width=8)
        bw_entry.pack(side="left", padx=(6, 0))
        trio_note = tk.Label(trio_card, text="Shared by every narrowband filter in this set. Leave any\n"
                                              "slot blank to skip it — you don't have to add all three.",
                              bg=TRIO_BG, fg="#556677", font=("Helvetica", 8), justify="left")
        trio_note.pack(fill="x", padx=10, pady=(0, 8), anchor="w")
        _custom_colored.append((trio_note, TRIO_BG, "#556677"))

        def _do_add_nb_members(event=None):
            slots = [("Hα", ha_var), ("S2", s2_var), ("O3", o3_var)]
            non_blank = [(label, var.get().strip()) for label, var in slots if var.get().strip()]
            if not non_blank:
                return "break"
            dupes = [nm for _, nm in non_blank if any(m["name"].lower() == nm.lower() for m in members)]
            if dupes:
                messagebox.showerror(
                    "Already in set",
                    f"{', '.join(dupes)} {'is' if len(dupes) == 1 else 'are'} already in this set.",
                    parent=dlg)
                return "break"
            for _, nm in non_blank:
                members.append({"name": nm, "type": "narrowband"})
            _refresh_members_lb()
            # Reset to the conventional defaults, ready for another wheel's worth.
            ha_var.set("Ha"); s2_var.set("SII"); o3_var.set("OIII")
            return "break"

        add_nb_btn = ttk.Button(nb_add_frame, text="+ Add narrowband filters", command=_do_add_nb_members)
        add_nb_btn.pack(fill="x", pady=(8, 0))
        for _e in (ha_entry, s2_entry, o3_entry, bw_entry):
            _e.bind("<Return>", _do_add_nb_members)

        _refresh_members_lb()
        _update_segmented()
        _refresh_add_block()

        # btn_row itself was already created and packed above (see the
        # comment by _refresh_add_block) — its Cancel/Save buttons are
        # added into it here, once _do_save is ready to be wired up.

        def _do_save():
            new_name = name_var.get().strip()
            if not new_name:
                messagebox.showerror("Invalid name",
                                     "Please enter a name for this filter set.", parent=dlg)
                return
            if new_name == "Mono Lum":
                messagebox.showerror("Reserved name",
                                     "\"Mono Lum\" is a reserved built-in option — "
                                     "please choose another name.", parent=dlg)
                return
            if new_name != name and new_name in self.data.get("filter_sets", {}):
                messagebox.showerror("Name exists",
                                     f"A filter set named '{new_name}' already exists.", parent=dlg)
                return
            has_nb = any(m["type"] == "narrowband" for m in members)
            bw_val = None
            if has_nb:
                try:
                    bw_val = float(bw_var.get().strip())
                except ValueError:
                    bw_val = None
                if bw_val is None or bw_val <= 0:
                    messagebox.showerror("Invalid bandwidth",
                                         "Please enter a positive bandwidth in nm for "
                                         "this set's narrowband filters.", parent=dlg)
                    return

            filter_sets = self.data.setdefault("filter_sets", {})
            if name and name in filter_sets and name != new_name:
                del filter_sets[name]
            filter_sets[new_name] = {"bandwidth_nm": bw_val, "members": [dict(m) for m in members]}

            # Keep the live filter chip pointed at the right place across a rename.
            if name and name != new_name and self.filter_mode.get() == name:
                self.filter_mode.set(new_name)

            self._persist_data()
            self.refresh_dropdowns()
            if refresh_list is not None:
                refresh_list(preserve_name=new_name)
            self._show_toast(f"{'Updated' if name else 'Added'} filter set: {new_name}")
            dlg.destroy()

        ttk.Button(btn_row, text="Cancel", command=dlg.destroy).pack(side="right", padx=(6, 0))
        ttk.Button(btn_row, text="Save",   command=_do_save   ).pack(side="right")
        dlg.bind("<Return>", lambda e: _do_save())
        dlg.bind("<Escape>", lambda e: dlg.destroy())
        name_entry.focus_set()
        if not name:
            name_entry.select_range(0, tk.END)

        self._theme_popup(dlg)
        # _theme_popup just flattened every plain tk widget above (including
        # the toggle pills and narrowband trio) to the theme's plain
        # background — put their deliberate colors back, and re-highlight
        # whichever toggle pill is actually active.
        for _widget, _bg, _fg in _custom_colored:
            _kwargs = {}
            if _bg is not None:
                _kwargs["bg"] = _bg
            if _fg is not None:
                _kwargs["fg"] = _fg
            if _kwargs:
                _widget.configure(**_kwargs)
        _update_segmented()

    def _open_manage_rigs_dialog(self):
        """Open the modal 'Manage Rigs' dialog for rename/update/delete operations."""
        dlg = tk.Toplevel(self.root)
        dlg.title("Manage Rigs")
        dlg.resizable(False, False)
        dlg.grab_set()
        dlg.transient(self.root)

        main = ttk.Frame(dlg)
        main.pack(padx=16, pady=14, fill="both", expand=True)

        # ── Left column: list of saved rigs ───────────────────────────────────
        left = ttk.Frame(main)
        left.pack(side="left", fill="y", padx=(0, 14))

        ttk.Label(left, text="Saved rigs:",
                  font=("Helvetica", 9, "bold"),
                  foreground="#778899").pack(anchor="w", pady=(0, 4))

        # ── Right column: details of selected rig ─────────────────────
        right = ttk.Frame(main)
        right.pack(side="left", fill="both", expand=True)

        ttk.Label(right, text="Details:",
                  font=("Helvetica", 9, "bold"),
                  foreground="#778899").pack(anchor="w", pady=(0, 4))

        lb, details, name_list, detail_labels, refresh_list = \
            self._build_rig_list_and_details(left, right, list_height=8, list_width=22)
        lb.pack(fill="y")
        details.configure(width=220, height=150)
        details.pack(fill="both", expand=True)
        details.pack_propagate(False)

        # ── Button row ───────────────────────────────────
        btn_row = ttk.Frame(dlg)
        btn_row.pack(padx=16, pady=(0, 14), fill="x")

        ttk.Button(btn_row, text="Rename", command=lambda: self._rig_manager_rename(name_list, lb, refresh_list, dlg)).pack(side="left")
        ttk.Button(btn_row, text="Update", command=lambda: self._rig_manager_update(name_list, lb, refresh_list, dlg)).pack(side="left", padx=(6, 0))
        ttk.Button(btn_row, text="Delete", command=lambda: self._rig_manager_delete(name_list, lb, refresh_list, dlg)).pack(side="left", padx=(6, 0))
        ttk.Button(btn_row, text="Close",  command=dlg.destroy).pack(side="right")
        dlg.bind("<Escape>", lambda e: dlg.destroy())

        # Initial populate — select the active rig if any, else the first
        active = self.data.get("settings", {}).get("active_rig", "")
        refresh_list(preserve_name=active if active else None)

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
        """Called whenever a planner Combobox changes — marks dirty if auto-update is on."""
        # Update the rig chip indicator (shows "Custom…" if chips diverge from
        # the active rig's saved snapshot)
        self._update_rig_indicator()
        if self.auto_update_enabled:
            self._mark_analysis_dirty()
        # A browsing-grid card's ✓/✚ icon is rig-aware now (see
        # _build_target_card) -- it reflects whether THIS target already
        # has a plan entry under the CURRENTLY selected scope/camera/
        # filter mode. So switching rigs has to re-render the grid, or a
        # stale checkmark from the old rig sticks around and makes an
        # addable target look already-planned.
        self._schedule_grid_rig_refresh()

    def _schedule_grid_rig_refresh(self):
        """Debounced rescan of the Plan tab's browsing grid after an
        equipment change.

        Switching rigs via the rig chip fires several StringVar writes
        back to back (scope, camera, filter mode, reduction all change in
        one go), each landing here via on_parameter_change. Collapsing
        those into a single rescan shortly after the last one avoids
        kicking off a full rescan per write -- same debounce shape as
        _mark_analysis_dirty/_schedule_analysis above.
        """
        if getattr(self, "_grid_rig_refresh_after_id", None) is not None:
            return  # already scheduled -- don't stack
        self._grid_rig_refresh_after_id = self.root.after(150, self._run_grid_rig_refresh)

    def _run_grid_rig_refresh(self):
        """Fire the debounced rescan scheduled by _schedule_grid_rig_refresh."""
        self._grid_rig_refresh_after_id = None
        self._refresh_visible_grid()

    def _on_bortle_changed(self, *args):
        """Sky darkness (Bortle) changed — unconditionally refresh everything
        that depends on it.

        Bortle lives at the top of the screen as a standalone "sky
        conditions" setting now, decoupled from rig presets entirely (it's
        an environmental fact, not equipment). Per Jerry: changing it should
        always recalculate everything that uses it, regardless of whether
        Auto Update is on, which tab is currently showing, or whether the
        Target Planner tab is even reachable in navigation right now — never
        just mark it dirty and hope the user revisits a tab that shows it.

        So this bypasses _mark_analysis_dirty/_schedule_analysis (both of
        which gate on tab visibility) and calls the analysis pipeline
        directly. suppress_incomplete_warning=True keeps this silent when no
        target/scope/camera is loaded yet -- it fires on every dropdown
        change, so popping a blocking "Incomplete" dialog just because
        nothing happens to be selected would be intrusive, not helpful.

        The Plan tab's browsing grid is NOT rescanned here — its visibility
        filter runs off each catalog target's static magnitude/surface-
        brightness fields, not the live sky-darkness setting, so there's
        nothing there for a Bortle change to invalidate.

        Explore's Rig Builder also has no Bortle control of its own anymore
        (removed — every analyzed rig now reads this same global value), so
        its currently-shown report is refreshed here too, whether or not the
        Explore tab happens to be the one on screen right now.

        Tonight's Plan is different from those two: its rows aren't a live
        view recomputed from scratch on every render — each row's
        recommended exposure/sub-count was baked in once, at the moment it
        was added, using whatever Bortle setting was active then. Left
        alone, a later Bortle change would make every already-added row
        silently stale (Jerry: "refresh the exposure times on the list of
        targets when I change the sky/bortle setting"), so every row gets
        rescaled here too.
        """
        self.analyze_framing(suppress_incomplete_warning=True)
        if hasattr(self, "_explore_report_txt"):
            self._explore_refresh_report()
        self._rescale_plan_exposures_for_bortle(self.bortle_choice.get())

    def _rescale_plan_exposures_for_bortle(self, new_bortle_key):
        """Rescale every Tonight's Plan row's exp_s/n_subs/total_int_hrs for
        a change in the global sky-darkness (Bortle) setting, then redraw
        the plan list.

        A full from-scratch recompute (like the one _split_entry_into_plan
        does when a target is first added) would need each row's original
        scope geometry and reduction factor — neither of which is stored on
        the row, only its scope/camera NAMES are. Re-deriving isn't needed
        though: exp_s is directly proportional to the Bortle sky-background
        factor and nothing else about a row changes, so each row's new
        exposure is just its OLD exposure scaled by
        (its own old Bortle factor ÷ the new one) — exact, using only what's
        already on the row (its stored "bortle" key from when it was added,
        its camera, its exp_s, and its allocated_hrs share).
        """
        new_b_data = BORTLE_FACTORS.get(new_bortle_key)
        if new_b_data is None or not self._plan_entries:
            return
        changed = False
        for e in self._plan_entries:
            old_bortle_key = e.get("bortle") or new_bortle_key
            if old_bortle_key == new_bortle_key:
                continue
            old_b_data = BORTLE_FACTORS.get(old_bortle_key)
            e["bortle"] = new_bortle_key   # keep this row's own record current either way
            if old_b_data is None:
                continue
            cam = self.data["cameras"].get(e.get("camera", ""), {})
            key = "color" if cam.get("is_color", True) else "mono"
            old_val, new_val = old_b_data.get(key), new_b_data.get(key)
            if not old_val or not new_val:
                continue
            new_exp_s = e.get("exp_s", 0.0) * (old_val / new_val)
            alloc_hrs_this = e.get("allocated_hrs", 0.0)
            n_subs = int(alloc_hrs_this * 3600.0 / max(new_exp_s, 0.1))
            e["exp_s"]         = round(new_exp_s, 1)
            e["n_subs"]        = n_subs
            e["total_int_hrs"] = round((n_subs * new_exp_s) / 3600.0, 2)
            changed = True
        if changed:
            self._refresh_plan_tree()

    # ═══════════════════════════════════════════════════════════════════
    # MAIN ANALYSIS — framing, exposure, imaging window, altitude chart
    # ═══════════════════════════════════════════════════════════════════

    @staticmethod
    def _moon_condition(ra_deg, dec_deg):
        """Categorize how much the Moon interferes with imaging a target
        right now.

        Shared by the "Analyze Target" results panel and the target-card
        moon-interference line, so both use identical thresholds instead of
        duplicating them. Mirrors the exact tiers that used to be inlined in
        ``_analyze_framing_impl``: negligible illumination, then separation
        bands at 45° and 20°.

        Returns ``(tag, icon, note, impact, illum_pct, sep_deg)`` — ``tag``
        is one of "optimal"/"highlight"/"warning" (matches the results
        panel's text-tag names so callers there don't need translating).
        """
        _, illum_pct, _, _, _, _ = _calc_moon_phase()
        sep_deg, _, _ = _calc_moon_separation(ra_deg, dec_deg)
        if illum_pct <= 10:
            return ("optimal", "🟢", f"Moon {illum_pct}% — negligible",
                     "Negligible interference", illum_pct, sep_deg)
        elif sep_deg >= 45:
            return ("optimal", "🟢", f"Moon {sep_deg:.0f}° away ({illum_pct}%)",
                     "Minimal interference", illum_pct, sep_deg)
        elif sep_deg >= 20:
            return ("highlight", "🟡", f"Moon {sep_deg:.0f}° away ({illum_pct}%)",
                     "Moderate — consider NB filters", illum_pct, sep_deg)
        else:
            return ("warning", "🔴", f"Moon {sep_deg:.0f}° away ({illum_pct}%)",
                     "Significant interference", illum_pct, sep_deg)

    def analyze_framing(self, **kwargs):
        """Public entry point for the main analysis pipeline (re-entrancy-guarded wrapper)."""
        # Re-entrancy guard — if update_idletasks() flushes a stacked call,
        # this prevents it from clearing results_txt mid-draw.
        if getattr(self, "_analyzing", False):
            return
        self._analyzing = True
        try:
            self._analyze_framing_impl(**kwargs)
        finally:
            self._analyzing = False

    def _analyze_framing_impl(self, suppress_incomplete_warning=False):
        """Compute FOV, scale, exposure, imaging window, moon separation for the selected target.

        Populates self.results_txt with the analysis report, renders the DSS thumbnail
        and altitude chart, and updates the (now headless) integration-plan state
        that show_integration_plan reads.

        ``suppress_incomplete_warning`` skips the blocking "Incomplete" dialog
        when no target/scope/camera is resolved yet — used by
        :meth:`_on_bortle_changed`, which forces this to run on every sky-
        darkness change regardless of what else is loaded, and shouldn't pop
        a dialog just because nothing happens to be selected right now.
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
            if not self.auto_update_enabled and not suppress_incomplete_warning:
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

        # ── Moon separation (shared with the target-card moon line) ────────
        moon_tag, _moon_icon, _moon_note, moon_impact, illum_pct, sep_deg = \
            self._moon_condition(t["ra_deg"], t["dec_deg"])

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
            # Auto-fill the (headless) allocated-hours state that
            # show_integration_plan reads, from tonight's actual dark
            # window.
            if win_hrs and win_hrs > 0:
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

        needed_survey = _dss_survey_size_deg(fw, fh)

        cached_ok = (self._cached_dss_img is not None
                     and self._cached_dss_target == t['id']
                     and self._cached_dss_survey_deg is not None
                     and 0.5 <= needed_survey / self._cached_dss_survey_deg <= 2.0)

        if cached_ok:
            self._draw_fov_overlay(self._cached_dss_img, self._cached_dss_survey_deg, t['id'])
        else:
            # New target or FOV scale changed significantly — reset framing and fetch fresh DSS.
            #
            # If this exact target/rig/filter is already on tonight's plan
            # with a real framing (a pan and/or a rotation), that framing is
            # what NINA will actually use on export — reopening the Frame
            # dialog on it (e.g. right after loading a saved session, when
            # the DSS cache is always empty so this branch always runs)
            # should show THAT framing, not a blank reset, and "Update
            # Framing" should never silently overwrite a good saved framing
            # with a fresh 0°/centered one just because the dialog was
            # reopened to look at it (Jerry: "saving a session that has
            # been framed and re-loading it causes the framing to reset").
            # The pixel pan can't be computed here — it depends on the DSS
            # image's fetched survey size and the canvas width, neither
            # known until the fetch below completes — so it's only STAGED
            # here; _show_dss_result applies it once those are known.
            existing_framed = next(
                (pe for pe in self._plan_entries
                 if pe["target_id"] == t['id'] and pe.get("scope") == s_name
                 and pe.get("camera") == c_name and pe.get("filter_mode") == self.filter_mode.get()
                 and (pe.get("rotation_angle") or pe.get("framed_ra_deg") is not None)),
                None)
            self._fov_offset_x = 0.0
            self._fov_offset_y = 0.0
            self._fov_angle    = 0.0
            self._zoom_level   = 1.0
            self._pending_frame_restore = {
                "target_id":      t['id'],
                "ra0_deg":        t['ra_deg'],
                "dec0_deg":       t['dec_deg'],
                "framed_ra_deg":  existing_framed.get("framed_ra_deg"),
                "framed_dec_deg": existing_framed.get("framed_dec_deg"),
                "rotation_angle": existing_framed.get("rotation_angle", 0.0) or 0.0,
            } if existing_framed is not None else None
            self._rot_label.config(text=f"{self._screen_to_sky_pa(0.0):.1f}°")
            self._zoom_label.config(text="1.0×")
            self.preview_canvas.delete("all")
            _lw = self.preview_canvas.winfo_width() or cw
            _lh = self.preview_canvas.winfo_height() or ch
            self.preview_canvas.create_text(_lw//2, _lh//2, text="Loading DSS image…", fill="yellow", font=("Helvetica", 9))
            self.root.after(50, lambda: self.fetch_thumbnail(t, fw, fh))

        # Refresh the headless integration-plan state for the current target
        # (read by the Frame dialog / NINA export, not displayed directly)
        self.root.after(20, lambda: self.show_integration_plan(silent=True))

    def _sample_altitude_series(self, t, lat, lon):
        """Sample a target's altitude across tonight's nautical-dark window.

        Shared by ``_draw_altitude_chart`` (the full Planner/Frame-dialog chart)
        and the target-card sparkline, so both draw from identical astronomy
        math instead of duplicating the dark-window scan.

        Returns a dict:
          dark_start_jd, dark_end_jd: JD bounds of nautical dark (None if no
              dark window tonight at this location — other keys are then
              empty/None and callers should bail out early, same as the old
              inline "No nautical dark window" message).
          scan_start, scan_end, total_jd: JD bounds of the padded (±30 min)
              sample window used for times_h/alts below.
          n_steps: number of samples in times_h/alts.
          times_h: local hour-of-day (float, wraps 0-24) per sample.
          alts: target altitude in degrees per sample, same length/order.
          moon_rise_frac, moon_set_frac: fractional position (0..1) of moon
              rise/set within the scan window, or None if it doesn't occur
              in-window. Convert back to local time via
              ``scan_start + frac * total_jd`` -> the usual jd-to-local-hour
              formula, same as the chart does for its rise/set labels.
          utc_offset_h: local UTC offset used for all the hour conversions
              above, so callers don't need to recompute it.
        """
        utc_offset_h = _utc_offset_hours()
        jd_noon      = _jd_local_noon()

        step_jd = 5.0 / 1440.0
        n_full  = int(30 * 60 / 5) + 1   # 30-hour window

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
            return {
                "dark_start_jd": None, "dark_end_jd": None,
                "scan_start": None, "scan_end": None, "total_jd": None,
                "n_steps": 0, "times_h": [], "alts": [],
                "moon_rise_frac": None, "moon_set_frac": None,
                "utc_offset_h": utc_offset_h,
            }

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
        moon_rise_frac = moon_set_frac = None
        prev_moon_alt = None
        for i in range(n_steps):
            jd = scan_start + i * step
            m_ra, m_dec = _calc_moon_position(jd)
            m_alt = _ra_dec_to_altaz(m_ra, m_dec, lat, lon, jd)
            if prev_moon_alt is not None:
                frac_x = i / (n_steps - 1)
                if prev_moon_alt < 0 <= m_alt and moon_rise_frac is None:
                    moon_rise_frac = frac_x
                elif prev_moon_alt >= 0 > m_alt and moon_set_frac is None:
                    moon_set_frac = frac_x
            prev_moon_alt = m_alt

        return {
            "dark_start_jd": dark_start_jd, "dark_end_jd": dark_end_jd,
            "scan_start": scan_start, "scan_end": scan_end, "total_jd": total_jd,
            "n_steps": n_steps, "times_h": times_h, "alts": alts,
            "moon_rise_frac": moon_rise_frac, "moon_set_frac": moon_set_frac,
            "utc_offset_h": utc_offset_h,
        }

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

        # ── Sample tonight's dark window + target altitude (shared helper) ──
        real_now = datetime.now()   # kept for the "Now" marker below

        series = self._sample_altitude_series(t, lat, lon)
        if series["dark_start_jd"] is None:
            # No dark window — draw message
            canvas.create_text(cw // 2, ch // 2,
                text="No nautical dark window tonight at this location",
                fill=fg_main, font=("Helvetica", 9))
            return

        utc_offset_h  = series["utc_offset_h"]
        dark_start_jd = series["dark_start_jd"]
        dark_end_jd   = series["dark_end_jd"]
        scan_start    = series["scan_start"]
        total_jd      = series["total_jd"]
        n_steps       = series["n_steps"]
        times_h       = series["times_h"]
        alts          = series["alts"]

        # ── Moon rise/set within the scan window ────────────────────────────
        moon_rise_x = moon_set_x = None
        moon_rise_label = moon_set_label = ""
        if series["moon_rise_frac"] is not None:
            moon_rise_x = series["moon_rise_frac"]
            jd = scan_start + moon_rise_x * total_jd
            lh = (((jd + 0.5) % 1.0) * 24.0 + utc_offset_h) % 24.0
            moon_rise_label = f"🌕 {int(lh):02d}:{int((lh%1)*60):02d}"
        if series["moon_set_frac"] is not None:
            moon_set_x = series["moon_set_frac"]
            jd = scan_start + moon_set_x * total_jd
            lh = (((jd + 0.5) % 1.0) * 24.0 + utc_offset_h) % 24.0
            moon_set_label = f"🌑 {int(lh):02d}:{int((lh%1)*60):02d}"

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
        load) before the download completes. ``_show_dss_result``/
        ``_show_fov_error`` check that ``self.current_target_info`` still
        matches the fetched target, AND that ``self._fov_wait_token`` still
        matches the token this call captured, before touching the canvas —
        either a target switch or a newer fetch_thumbnail() call for the
        SAME target (e.g. a re-analysis) makes this one's results stale.
        Without this guard, a slow-arriving fetch can overwrite the FOV
        canvas with the wrong picture, or fight a newer fetch for it.

        For a wide-FOV rig whose real size needs the "slow lane" (see
        _dss_fetch_plan), this no longer shows the small fast-lane image
        while waiting — Jerry: that risked being mistaken for the final,
        still-clipped result rather than a placeholder. Instead
        _draw_fov_wait_frame puts up an explicit wait screen (spinner,
        elapsed time, "this can take up to 90s") with a "Show 5° field
        instead" pill, and only actually displays a picture once the full
        request lands, the user cancels, or the slow lane gives up.
        """
        target_id = target_info['id']

        # Survey size: show enough sky so the FOV fits with some padding.
        # We fetch a square patch; the larger of FOV dims sets the size,
        # +40% padding. See _dss_fetch_plan for the fast/slow lane split.
        survey_size = _dss_survey_size_deg(fov_w_deg, fov_h_deg)
        fast_lane, slow_lane = _dss_fetch_plan(survey_size)

        self._fov_wait_token += 1
        token = self._fov_wait_token
        self._cancel_fov_wait_tick()
        self._fov_wait_fast_ready = None
        self._fov_wait_cancelled = False

        if slow_lane is None:
            # Narrow FOV — single lane, no wait screen: this was already
            # fast before the wide-FOV wait state existed, and still is.
            threading.Thread(
                target=self._fetch_dss_lane,
                args=(target_id, fast_lane,
                      lambda img, deg: self._show_dss_result(target_id, img, deg, token),
                      lambda: self._show_fov_error(target_id, token)),
                daemon=True).start()
            return

        # Wide FOV — put up the wait state, then run both lanes
        # CONCURRENTLY underneath it (not fast-then-slow): the fast lane
        # is what Cancel falls back to, the slow lane is the real request.
        self._fov_wait_target = target_id
        self._fov_wait_survey_deg = survey_size
        self._fov_wait_start_ts = time.monotonic()
        self._fov_wait_angle = 0
        self._draw_fov_wait_frame(token)

        def _fast_success(img, deg):
            if token != self._fov_wait_token:
                return  # superseded by a newer fetch_thumbnail() call
            self._fov_wait_fast_ready = (img, deg)
            if self._fov_wait_cancelled:
                self._show_dss_result(target_id, img, deg, token)

        def _fast_failed():
            if token == self._fov_wait_token and self._fov_wait_cancelled:
                # User asked for the small field, and even that failed —
                # nothing left to fall back to.
                self._show_fov_error(target_id, token)

        def _slow_success(img, deg):
            self._show_dss_result(target_id, img, deg, token)

        def _slow_failed():
            if self._fov_wait_fast_ready is not None:
                img, deg = self._fov_wait_fast_ready
                self._show_dss_result(target_id, img, deg, token)
            else:
                self._show_fov_error(target_id, token)

        threading.Thread(target=self._fetch_dss_lane,
                          args=(target_id, fast_lane, _fast_success, _fast_failed),
                          daemon=True).start()
        threading.Thread(target=self._fetch_dss_lane,
                          args=(target_id, [slow_lane], _slow_success, _slow_failed),
                          daemon=True).start()

    def _fetch_dss_lane(self, target_id, lane_attempts, on_success, on_all_failed):
        """Run one DSS fetch lane (an ordered list of (size_deg, timeout_s)
        attempts) on a background thread, trying each size until one
        succeeds. Pure I/O + Tk-safe callback scheduling — does no direct
        Tk calls itself, so it's safe to run from any thread.

        ``on_success(img, fetched_deg)`` / ``on_all_failed()`` are invoked
        on the main thread via ``root.after(0, ...)``.
        """
        for size_deg, timeout_s in lane_attempts:
            try:
                pixels = 600  # fetch high-res, we'll downscale
                url = (
                    f"https://skyview.gsfc.nasa.gov/current/cgi/runquery.pl"
                    f"?Survey=DSS2+Red&Position={urllib.request.quote(target_id)}"
                    f"&Size={size_deg:.4f}&Pixels={pixels}&Return=GIF&Catalog=none"
                )
                req = urllib.request.Request(url, headers={"User-Agent": f"LightbucketAstroPlanner/{__version__}"})
                with urllib.request.urlopen(req, timeout=timeout_s) as resp:
                    raw = resp.read()

                # SkyView returns HTTP 200 with an HTML error/warning page
                # (not a GIF) for a request it can't honor — e.g. a size it
                # won't build, or a malformed query — so a successful HTTP
                # fetch does NOT by itself mean we got a real image.
                # Image.open() is what actually proves it, and its
                # exception (below) is what tells us which case this was.
                img = Image.open(io.BytesIO(raw)).convert("RGB")
                print(f"[DSS fetch] {target_id}: {size_deg:.2f}° request OK "
                      f"— {len(raw)} bytes → {img.size[0]}x{img.size[1]}px image")
                self.root.after(0, lambda img=img, deg=size_deg: on_success(img, deg))
                return
            except Exception as exc:
                # Diagnostic only — printed to the console (visible when
                # running the .py directly) so a report like "still
                # clipped" can be tied to a concrete cause (timeout vs.
                # SkyView rejecting the size vs. something else) instead of
                # guessed at again.
                print(f"[DSS fetch] {target_id}: {size_deg:.2f}° request FAILED "
                      f"(timeout={timeout_s}s): {exc!r}")
                continue  # slow/failed — fall through to this lane's next size
        self.root.after(0, on_all_failed)

    def _show_dss_result(self, target_id, img, fetched_deg, token):
        """Cache and display a fetched DSS image — the shared landing
        point for every fetch_thumbnail() path (narrow-FOV single lane,
        wide-FOV fast lane, wide-FOV slow lane, and the post-cancel
        fast-lane fallback)."""
        cur = self.current_target_info
        if cur is None or cur.get("id") != target_id:
            return  # user moved to a different target while this was in flight
        if token != self._fov_wait_token:
            return  # superseded by a newer fetch_thumbnail() call
        # The fast and slow lanes race — don't let a smaller result that
        # happens to land SECOND clobber a bigger one already on screen
        # (matters if the slow lane wins the race, then the fast lane's
        # own late callback still fires). Scoped to THIS token only: once
        # token == self._fov_wait_token (checked above), any result here
        # belongs to the current fetch_thumbnail() call, but the currently
        # cached image may be a leftover from an EARLIER call (a different
        # rig/FOV) — comparing sizes against that stale image would wrongly
        # suppress a legitimately smaller new result (e.g. switching from a
        # wide rig to a narrow one), which looked like the fetch hanging.
        cur_deg = self._cached_dss_survey_deg
        if (self._fov_result_token == token and cur_deg is not None
                and fetched_deg < cur_deg):
            return
        self._cancel_fov_wait_tick()
        self._cached_dss_img = img
        self._cached_dss_target = target_id
        self._cached_dss_survey_deg = fetched_deg
        self._fov_result_token = token
        self._apply_pending_frame_restore(target_id, fetched_deg)
        self._draw_fov_overlay(img, fetched_deg, target_id)

    def _apply_pending_frame_restore(self, target_id, survey_deg):
        """Restore a previously-saved pan/rotation staged by
        _analyze_framing_impl's reset branch, now that the DSS fetch has
        landed and the survey size (needed to convert the saved RA/Dec back
        into pixels) is finally known. Consumes (clears) the staged restore
        either way, so it's never mistakenly reapplied to a later, unrelated
        fetch for a different target."""
        pending = getattr(self, "_pending_frame_restore", None)
        self._pending_frame_restore = None
        if pending is None or pending["target_id"] != target_id:
            return

        self._fov_angle = self._screen_to_sky_pa(pending["rotation_angle"])
        self._rot_label.config(text=f"{pending['rotation_angle']:.1f}°")

        framed_ra, framed_dec = pending["framed_ra_deg"], pending["framed_dec_deg"]
        if framed_ra is None or framed_dec is None:
            return  # rotation-only framing — no pan to restore

        # Inverse of _framed_center: recover the pixel pan that would have
        # produced this saved (framed_ra, framed_dec) from the catalog
        # centre, using the now-known survey size and current canvas/zoom —
        # same px_per_deg source of truth _framed_center/_draw_fov_overlay
        # already share.
        zoom       = getattr(self, "_zoom_level", 1.0) or 1.0
        canvas_w   = self.preview_canvas.winfo_width() or 240
        deg_per_px = survey_deg / (canvas_w * zoom)
        if deg_per_px <= 0:
            return

        ra0_deg, dec0_deg = pending["ra0_deg"], pending["dec0_deg"]
        d_north_deg = framed_dec - dec0_deg
        d_ra_deg    = ((framed_ra - ra0_deg + 540.0) % 360.0) - 180.0  # shortest signed delta
        cos_dec     = math.cos(math.radians(dec0_deg))
        d_east_deg  = d_ra_deg * cos_dec

        self._fov_offset_x = -d_east_deg / deg_per_px
        self._fov_offset_y = -d_north_deg / deg_per_px

    def _show_fov_error(self, target_id, token):
        """Show the geometric-ellipse fallback — every lane relevant to
        this fetch has now failed with nothing to show."""
        cur = self.current_target_info
        if cur is None or cur.get("id") != target_id:
            return
        if token != self._fov_wait_token:
            return
        self._cancel_fov_wait_tick()
        self._fov_image_error()

    def _cancel_fov_wait_tick(self):
        """Stop the wide-field wait screen's spinner/elapsed-timer redraw
        loop, if one is scheduled. Idempotent — safe to call any time."""
        if self._fov_wait_after_id is not None:
            try:
                self.root.after_cancel(self._fov_wait_after_id)
            except Exception:
                pass
            self._fov_wait_after_id = None

    def _draw_fov_wait_frame(self, token):
        """Draw one frame of the wide-field wait screen (spinner, message,
        elapsed timer, and — unless already cancelled — a 'Show 5° field
        instead' pill) and reschedule itself. Stops on its own once
        superseded (token mismatch) or once the user has navigated away."""
        if token != self._fov_wait_token:
            return
        cur = self.current_target_info
        if cur is None or cur.get("id") != self._fov_wait_target:
            return

        cw = self.preview_canvas.winfo_width() or 240
        ch = self.preview_canvas.winfo_height() or 240
        self.preview_canvas.delete("all")
        self.preview_canvas.create_rectangle(0, 0, cw, ch, fill="#0a1420", outline="")

        cx, cy = cw // 2, ch // 2 - 22
        r = 17
        self._fov_wait_angle = (self._fov_wait_angle + 24) % 360
        self.preview_canvas.create_oval(cx - r, cy - r, cx + r, cy + r,
                                         outline="#1e2d3e", width=3)
        self.preview_canvas.create_arc(cx - r, cy - r, cx + r, cy + r,
                                        start=self._fov_wait_angle, extent=100,
                                        style="arc", outline="#38bdf8", width=3)

        if self._fov_wait_cancelled:
            msg = "Fetching 5° field…"
            sub1 = "showing that instead of the full"
            sub2 = f"{self._fov_wait_survey_deg:.1f}° field"
        else:
            msg = "Fetching wide field…"
            sub1 = f"{self._fov_wait_target} needs a {self._fov_wait_survey_deg:.1f}° image —"
            sub2 = "this can take up to 90s"

        self.preview_canvas.create_text(cx, cy + 32, text=msg,
                                         fill="#ffffff", font=("Helvetica", 10, "bold"))
        self.preview_canvas.create_text(cx, cy + 49, text=sub1,
                                         fill="#7f93a6", font=("Helvetica", 8))
        self.preview_canvas.create_text(cx, cy + 62, text=sub2,
                                         fill="#7f93a6", font=("Helvetica", 8))

        elapsed = int(time.monotonic() - self._fov_wait_start_ts)
        self.preview_canvas.create_text(cx, cy + 79, text=f"{elapsed}s elapsed",
                                         fill="#4a6478", font=("Helvetica", 8))

        if not self._fov_wait_cancelled:
            pill_id = self.preview_canvas.create_text(
                cx, cy + 101, text="Show 5° field instead",
                fill="#aaddff", font=("Helvetica", 8, "bold"))
            bbox = self.preview_canvas.bbox(pill_id)
            if bbox:
                pad = 6
                bg_id = self.preview_canvas.create_rectangle(
                    bbox[0] - pad, bbox[1] - 4, bbox[2] + pad, bbox[3] + 4,
                    fill="#182636", outline="#2a3f55")
                self.preview_canvas.tag_lower(bg_id, pill_id)
                for item in (bg_id, pill_id):
                    self.preview_canvas.tag_bind(
                        item, "<Button-1>", lambda e, t=token: self._fov_wait_cancel(t))
                    self.preview_canvas.tag_bind(
                        item, "<Enter>", lambda e: self.preview_canvas.configure(cursor="hand2"))
                    self.preview_canvas.tag_bind(
                        item, "<Leave>", lambda e: self.preview_canvas.configure(cursor=""))

        self._fov_wait_after_id = self.root.after(90, lambda: self._draw_fov_wait_frame(token))

    def _fov_wait_cancel(self, token):
        """'Show 5° field instead' pill handler: stop waiting on the slow
        lane's full-size request and show the fast lane's result the
        moment it's available (immediately, if it already landed)."""
        if token != self._fov_wait_token:
            return
        self._fov_wait_cancelled = True
        if self._fov_wait_fast_ready is not None:
            img, deg = self._fov_wait_fast_ready
            self._show_dss_result(self._fov_wait_target, img, deg, token)
        # else: the fast lane hasn't landed yet — _draw_fov_wait_frame's
        # next tick switches the message to "Fetching 5° field…", and
        # _fast_success (in fetch_thumbnail) will display it the moment
        # it arrives.

    def _draw_fov_overlay(self, dss_img, survey_deg, target_id):
        """Scale DSS image to the canvas and draw the sensor FOV rectangle (with pan/rotate) on top."""
        cw = self.preview_canvas.winfo_width() or 240
        ch = self.preview_canvas.winfo_height() or 240

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
        # "img N.N°" is the actual sky field the DISPLAYED picture covers —
        # separate from the ✅/❌ above, which is about whether the sensor
        # frame contains the TARGET's own angular size. When the frame
        # overlay itself runs off the edge of the picture, this number is
        # what to check: if it's smaller than expected, a large request
        # fell back to a smaller one (see the [DSS fetch] console log for
        # why) rather than the frame math being wrong.
        label = (f"{target_id}  {'✅ fits' if fits else '❌ too big'}  "
                 f"{self._screen_to_sky_pa(self._fov_angle):.1f}°  ·  img {survey_deg:.1f}°")
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
        cw = self.preview_canvas.winfo_width() or 240
        ch = self.preview_canvas.winfo_height() or 240
        return cw / 2 + self._fov_offset_x, ch / 2 + self._fov_offset_y

    @staticmethod
    def _screen_to_sky_pa(screen_angle):
        """Convert the FOV box's on-screen rotation to NINA's sky position angle.

        The framing box is drawn with a standard rotation matrix on a y-down
        canvas, so a positive ``_fov_angle`` rotates the box *clockwise* on the
        North-up / East-left DSS preview.  NINA — like the IAU position-angle
        convention — measures PA *counter-clockwise*, from North through East,
        starting from 0 with the frame upright (North up).

        Our box is also upright at ``_fov_angle == 0``, so the two conventions
        share a zero and differ only in handedness: ``(360 - angle) % 360``
        (equivalently ``-angle`` mod 360) flips the direction while keeping the
        zeros aligned.  An earlier calibration used ``180 - angle``, which fit a
        single eyeballed test (app 55 ~ NINA 125) only because a rectangular
        sensor frames identically at PA and PA+180 — so visual matching could
        not distinguish 125 from 305 (= 360 - 55).  The upright zero point is
        what actually pins it down: both tools read 0 there.

        This is the single place the conversion lives; the mapping is its own
        inverse, so the same call converts a sky PA back to the screen angle.
        """
        return (360.0 - screen_angle) % 360.0

    def _framed_center(self, ra0_deg, dec0_deg):
        """Return (ra_deg, dec_deg) of the FOV box centre after any pan.

        The preview maps sky to canvas as px_per_deg = canvas_width / survey_deg
        * zoom, and the pan is accumulated in canvas pixels (self._fov_offset_x/y).
        Reads the live preview canvas width so this stays correct regardless of
        which widget self.preview_canvas currently points at (the Planner tab's
        240px canvas, or the Frame dialog's larger one) — same source of truth
        ``_draw_fov_overlay``/``_fov_centre`` use, so all three always agree on
        the same pixel scale.  SkyView DSS2 images are North-up / East-left, so
        screen +x is West and screen +y is South.  The box centre is positioned
        before the box rotation is applied, so the rotation angle is not needed
        here.

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

        canvas_w    = self.preview_canvas.winfo_width() or 240
        deg_per_px  = survey_deg / (canvas_w * zoom)
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
            self._rot_label.config(text=f"{self._screen_to_sky_pa(self._fov_angle):.1f}°")
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
        self._rot_label.config(text=f"{self._screen_to_sky_pa(0.0):.1f}°")
        self._zoom_label.config(text="1.0×")
        self._redraw_fov_overlay()

    def _redraw_fov_overlay(self):
        """Redraw the overlay using the cached DSS image (no network call)."""
        if self._cached_dss_img is not None and self._cached_dss_target is not None:
            self._draw_fov_overlay(self._cached_dss_img, self._cached_dss_survey_deg,
                                   self._cached_dss_target)

    def _fov_image_error(self):
        """Display 'DSS image unavailable' fallback on the preview canvas."""
        cw = self.preview_canvas.winfo_width() or 240
        ch = self.preview_canvas.winfo_height() or 240
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

    # ═══════════════════════════════════════════════════════════════════
    # EXPLORE TAB — compare sensor FOVs of multiple rigs over one target
    # ═══════════════════════════════════════════════════════════════════
    #
    # Each press of Analyze adds the current scope/camera/reduction combo
    # as a colour-coded sensor frame over a shared DSS image, all centred
    # on the target at PA 0°.  A legend card per rig doubles as the
    # visibility/remove control.  Switching targets keeps the rig stack
    # and re-frames it over the new object; when the DSS fetch fails
    # (offline), the same geometric-ellipse fallback as the Planner is
    # drawn and the frames overlay it at true angular scale.

    # (colour, canvas dash pattern) per slot — dash doubles as a
    # colour-blind-safe secondary distinguisher.
    _EXPLORE_PALETTE = [
        ("#4ade80", None),
        ("#f59e0b", (6, 3)),
        ("#f472b6", (2, 3)),
        ("#38bdf8", None),
        ("#a78bfa", (6, 3)),
        ("#fb7185", (2, 3)),
    ]
    _EXPLORE_MAX_RIGS = 6
    _EXPLORE_CANVAS_PX = 560   # square canvas edge, px

    @staticmethod
    def _dim_hex(color, factor=0.45):
        """Return ``color`` (#rrggbb) darkened by ``factor`` — used to dim
        non-highlighted frames while hovering a legend card."""
        try:
            r = int(color[1:3], 16)
            g = int(color[3:5], 16)
            b = int(color[5:7], 16)
            return f"#{int(r*factor):02x}{int(g*factor):02x}{int(b*factor):02x}"
        except Exception:
            return color

    def setup_explore_tab(self):
        """Build the Explore tab — rig-comparison rail on the left, shared FOV canvas on the right."""
        self._explore_rigs = []          # analyzed rigs, draw order = insertion order
        self._explore_target = None      # target info dict (same shape as Planner's)
        self._explore_view = None        # {"img": PIL|None, "deg": span, "target_id": id}
        self._explore_fetch_seq = 0      # stale-fetch guard (monotonic counter)
        self._explore_hilite = None      # key of hover-highlighted rig, or None
        self._explore_active_rig_key = None  # key of the rig whose report shows on the right
        self._explore_photo = None       # keep PhotoImage alive
        self._explore_photo_cache = None # (img ref, deg, zoom, photo) → avoid re-resizing per hover
        self._explore_pa_deg = 0.0       # shared NINA-style sky PA (CCW N→E); applied to every visible rig frame
        self._explore_zoom   = 1.0       # shared zoom (1.0–16.0), crop+resize of the fetched image
        self._explore_redraw_after_id = None  # throttle guard — see _explore_schedule_redraw

        # Wide-field wait state — same idea as the Planner tab's
        # fetch_thumbnail/_draw_fov_wait_frame: shown instead of a picture
        # while a DSS request big enough to need the "slow lane" is still
        # in flight, with a live elapsed timer and a "Show 5° field
        # instead" cancel pill drawn on the Explore canvas.
        self._explore_wait_token = 0
        self._explore_wait_after_id = None
        self._explore_wait_fast_ready = None   # (img, fetched_deg), cached for Cancel
        self._explore_wait_cancelled = False
        self._explore_wait_target_id = None
        self._explore_wait_span_deg = None
        self._explore_wait_start_ts = None
        self._explore_wait_angle = 0
        self._explore_result_token = None  # token that produced the currently cached/
                                            # displayed image — see _explore_show_result's
                                            # size tie-break, which must only compare
                                            # results racing within the SAME _explore_fetch()
                                            # call, never against a stale image left over
                                            # from an earlier call for a different rig set
        self._explore_pan_x  = 0.0       # shared pixel pan — moves every visible frame together,
        self._explore_pan_y  = 0.0       # same as _explore_pa_deg, while the DSS image stays fixed
        self._explore_dragging = False   # click-drag on the canvas pans; rotation stays on the slider

        RAIL_BG = "#131f2e"

        outer = tk.Frame(self.tab_explore, bg="#0e1a28")
        outer.pack(fill="both", expand=True)

        # ── Left rail ─────────────────────────────────────────────────────
        rail = tk.Frame(outer, bg=RAIL_BG, width=276)
        rail.pack(side="left", fill="y")
        rail.pack_propagate(False)

        tk.Label(rail, text="TARGET", bg=RAIL_BG, fg="#cc8833",
                 font=("Helvetica", 9, "bold")).pack(anchor="w", padx=12, pady=(12, 2))
        self.explore_search = ttk.Entry(rail, font=("Helvetica", 11))
        self.explore_search.pack(fill="x", padx=12)
        ToolTip(self.explore_search,
                "Search by catalog ID (M42, NGC 224)\nor common name (Orion Nebula).\n"
                "Respects the Planner's catalog filter.")
        self.explore_search.bind("<KeyRelease>", self._explore_on_search_key)
        self.explore_search.bind("<Return>",
                                 lambda e: (self._hide_suggestions(), self._explore_load_target()))
        self.explore_search.bind("<Escape>",   lambda e: self._hide_suggestions())
        self.explore_search.bind("<FocusOut>", lambda e: self.root.after(150, self._hide_suggestions))

        self._explore_target_lbl = tk.Label(rail, text="No target selected", bg=RAIL_BG,
                                            fg="#778899", font=("Helvetica", 9),
                                            anchor="w", justify="left", wraplength=248)
        self._explore_target_lbl.pack(fill="x", padx=12, pady=(3, 6))

        tk.Frame(rail, bg="#2a3642", height=1).pack(fill="x", padx=12)

        tk.Label(rail, text="RIG BUILDER", bg=RAIL_BG, fg="#cc8833",
                 font=("Helvetica", 9, "bold")).pack(anchor="w", padx=12, pady=(8, 2))
        self.explore_scope_var     = tk.StringVar()
        self.explore_camera_var    = tk.StringVar()
        self.explore_reduction_var = tk.StringVar(value="1.0×")

        self.explore_scope_dropdown = ttk.Combobox(rail, textvariable=self.explore_scope_var,
                                                   state="readonly")
        self.explore_scope_dropdown.pack(fill="x", padx=12, pady=(0, 4))
        self.explore_camera_dropdown = ttk.Combobox(rail, textvariable=self.explore_camera_var,
                                                    state="readonly")
        self.explore_camera_dropdown.pack(fill="x", padx=12, pady=(0, 4))
        self.explore_reduction_dropdown = ttk.Combobox(
            rail, textvariable=self.explore_reduction_var,
            values=["0.63×", "0.67×", "0.70×", "0.75×", "0.80×", "1.0×", "1.5×", "2.0×", "2.5×", "3.0×"],
            state="readonly")
        self.explore_reduction_dropdown.pack(fill="x", padx=12, pady=(0, 6))

        # Bortle (sky darkness) isn't part of the Rig Builder anymore — it's
        # a single global "sky conditions" setting that lives in the header
        # (see setup_header / _on_bortle_changed), and every analyzed rig's
        # report here reads that live value rather than a per-rig snapshot.

        # Filter-mode combo — only shown once a mono camera is selected
        # (mirrors the Planner tab's own filter_mode chip/toggle_filter_visibility,
        # but as a separate StringVar/widget since Explore can hold several
        # rigs with different filter sets analyzed at once).
        self.explore_filter_var = tk.StringVar(value="Mono Lum")
        self.explore_filter_dropdown = ttk.Combobox(
            rail, textvariable=self.explore_filter_var,
            values=self._filter_mode_choices(), state="readonly")
        ToolTip(self.explore_filter_dropdown,
                "Filter set for this rig — narrows the\nsky-flux estimate for narrowband/LRGB")
        self.explore_camera_var.trace_add('write', self._explore_toggle_filter_visibility)

        analyze_row = tk.Frame(rail, bg=RAIL_BG)
        analyze_row.pack(fill="x", padx=12, pady=(0, 4))
        explore_analyze_btn = ttk.Button(analyze_row, text="⊕ Analyze",
                                         command=self._explore_analyze)
        explore_analyze_btn.pack(side="left", fill="x", expand=True)
        ToolTip(explore_analyze_btn,
                "Overlay this scope/camera/reducer\ncombination's sensor frame on the\n"
                "target image (max 6 rigs)")

        self._explore_rig_pick = ttk.Combobox(rail, state="readonly")
        self._explore_rig_pick.set("Add saved rig…")
        self._explore_rig_pick.pack(fill="x", padx=12, pady=(0, 8))
        self._explore_rig_pick.bind("<<ComboboxSelected>>", self._explore_saved_rig_picked)
        ToolTip(self._explore_rig_pick,
                "One-click add: applies a saved rig's\nscope/camera/reducer and analyzes it")

        tk.Frame(rail, bg="#2a3642", height=1).pack(fill="x", padx=12)

        hdr_row = tk.Frame(rail, bg=RAIL_BG)
        hdr_row.pack(fill="x", padx=12, pady=(8, 2))
        tk.Label(hdr_row, text="ANALYZED RIGS", bg=RAIL_BG, fg="#cc8833",
                 font=("Helvetica", 9, "bold")).pack(side="left")
        self._explore_clear_lbl = tk.Label(hdr_row, text="✕ clear all", bg=RAIL_BG,
                                           fg="#556677", font=("Helvetica", 8),
                                           cursor="hand2")
        self._explore_clear_lbl.pack(side="right")
        self._explore_clear_lbl.bind("<Button-1>", lambda e: self._explore_clear())
        ToolTip(self._explore_clear_lbl, "Remove all analyzed rigs")

        self._explore_cards_frame = tk.Frame(rail, bg=RAIL_BG)
        self._explore_cards_frame.pack(fill="both", expand=True, padx=10, pady=(2, 8))

        # ── Right: status row + FOV canvas ────────────────────────────────
        right = tk.Frame(outer, bg="#0e1a28")
        right.pack(side="left", fill="both", expand=True)

        status_row = tk.Frame(right, bg="#0e1a28")
        status_row.pack(fill="x", padx=14, pady=(10, 4))
        self._explore_span_lbl = tk.Label(status_row, text="", bg="#0e1a28",
                                          fg="#7eb8d4", font=("Helvetica", 9))
        self._explore_span_lbl.pack(side="left")
        # "Show Sky Map" — moved here from the (now hidden) Planner tab,
        # above the FOV image, and rewired to Explore's own target/active-
        # rig/rotation state (see _explore_open_sky_map) rather than the
        # Planner tab's separate framing state.
        explore_skymap_btn = ttk.Button(status_row, text="Show Sky Map",
                                        command=self._explore_open_sky_map)
        explore_skymap_btn.pack(side="left", padx=(12, 0))
        ToolTip(explore_skymap_btn, "Open the interactive sky map centred\n"
                                    "on the current target and active rig")
        explore_reload_btn = ttk.Button(status_row, text="⟳", width=3,
                                        command=lambda: self._explore_ensure_image(force=True))
        explore_reload_btn.pack(side="right")
        ToolTip(explore_reload_btn, "Re-download the DSS image\n(sized to the largest analyzed FOV)")
        tk.Label(status_row, text="frames centred on target · N up / E left",
                 bg="#0e1a28", fg="#556677", font=("Helvetica", 8)).pack(side="right", padx=(0, 10))

        cpx = self._EXPLORE_CANVAS_PX

        content_row = tk.Frame(right, bg="#0e1a28")
        content_row.pack(fill="both", expand=True, padx=14, pady=(0, 10))

        canvas_col = tk.Frame(content_row, bg="#0e1a28")
        canvas_col.pack(side="left", anchor="n")

        self.explore_canvas = tk.Canvas(canvas_col, width=cpx, height=cpx, bg="black",
                                        highlightthickness=1, highlightbackground="#2e4a63")
        self.explore_canvas.pack()
        self.explore_canvas.bind("<MouseWheel>", self._explore_mousewheel)   # Windows / macOS
        self.explore_canvas.bind("<Button-4>",   self._explore_mousewheel)   # Linux scroll up
        self.explore_canvas.bind("<Button-5>",   self._explore_mousewheel)   # Linux scroll down
        # Click-drag pans every visible rig frame together over the fixed
        # DSS image (rotation stays on the slider below, which already
        # spins every frame together too — no need to handle frames
        # separately for either).
        self.explore_canvas.bind("<ButtonPress-1>",   self._explore_fov_mouse_down)
        self.explore_canvas.bind("<B1-Motion>",       self._explore_fov_mouse_drag)
        self.explore_canvas.bind("<ButtonRelease-1>", self._explore_fov_mouse_up)

        # Zoom pill — floats on the canvas's own top-right corner via
        # .place(), so it's a real ttk.Button pair (not a canvas item) and
        # is completely untouched by _explore_redraw's cv.delete("all").
        # Each click steps by the same ×1.25 factor the mousewheel uses,
        # clamped to 1.0–16.0, reusing the identical crop/resize redraw —
        # no continuous-drag path left to desync from the mouse.
        zoom_pill = tk.Frame(self.explore_canvas, bg="#131f2e",
                             highlightthickness=1, highlightbackground="#3a5a7a")
        zoom_plus_btn = ttk.Button(zoom_pill, text="+", width=2,
                                   style="ExploreZoom.TButton",
                                   command=lambda: self._explore_apply_zoom(1.25))
        zoom_plus_btn.pack(fill="x")
        tk.Frame(zoom_pill, bg="#3a5a7a", height=1).pack(fill="x")
        zoom_minus_btn = ttk.Button(zoom_pill, text="−", width=2,
                                    style="ExploreZoom.TButton",
                                    command=lambda: self._explore_apply_zoom(1 / 1.25))
        zoom_minus_btn.pack(fill="x")
        tk.Frame(zoom_pill, bg="#3a5a7a", height=1).pack(fill="x")
        self._explore_zoom_label = tk.Label(zoom_pill, text="1.0×", bg="#131f2e", fg="#cc8833",
                                            font=("Helvetica", 8, "bold"))
        self._explore_zoom_label.pack(fill="x", pady=(2, 3))
        zoom_pill.place(in_=self.explore_canvas, relx=1.0, x=-10, y=10, anchor="ne")
        ToolTip(zoom_pill, "Zoom in/out (scroll wheel over the image works too)")

        # Rotate bar — one shared slider spins every visible rig frame
        # together; the DSS image itself stays fixed North-up, same
        # convention as the Planner tab's FOV preview.
        rotate_bar = tk.Frame(canvas_col, bg="#131f2e")
        rotate_bar.pack(fill="x", pady=(6, 0))
        tk.Label(rotate_bar, text="↻ ROTATE", bg="#131f2e", fg="#778899",
                 font=("Helvetica", 9)).pack(side="left", padx=(8, 6))
        self._explore_pa_var = tk.DoubleVar(value=0.0)
        explore_pa_scale = ttk.Scale(rotate_bar, from_=0.0, to=360.0, orient="horizontal",
                                     variable=self._explore_pa_var,
                                     style="Explore.Horizontal.TScale",
                                     command=self._explore_on_rotate)
        explore_pa_scale.pack(side="left", fill="x", expand=True, padx=(0, 8), pady=6)
        self._explore_pa_label = tk.Label(rotate_bar, text="0.0°",
                                          bg="#131f2e", fg="#cc8833",
                                          font=("Helvetica", 9, "bold"), width=6, anchor="e")
        self._explore_pa_label.pack(side="left")
        explore_reset_lbl = tk.Label(rotate_bar, text="reset", bg="#131f2e", fg="#7eb8d4",
                                     font=("Helvetica", 8), cursor="hand2")
        explore_reset_lbl.pack(side="left", padx=(8, 8))
        explore_reset_lbl.bind("<Button-1>", lambda e: self._explore_reset_view())
        ToolTip(explore_pa_scale,
               "Rotate every visible rig frame together\n(the image itself stays fixed North-up)")
        ToolTip(explore_reset_lbl, "Reset rotation, zoom, and pan\nto 0° / 1.0× / centred")

        tk.Label(canvas_col, text="drag the image to pan every frame",
                 bg="#0e1a28", fg="#556677", font=("Helvetica", 8)).pack(pady=(2, 0))

        # ── Report column — the Planner tab's framing/exposure/window/moon
        # report, relocated here so it sits beside the shared image instead
        # of on its own tab. Explore can hold up to 6 analyzed rigs at once,
        # so a row of rig chips picks which one's report is showing; clicking
        # a rig's legend card in the rail (see _explore_refresh_cards) does
        # the same thing. ────────────────────────────────────────────────
        report_col = tk.Frame(content_row, bg="#131f2e",
                              highlightthickness=1, highlightbackground="#2e4a63")
        report_col.pack(side="left", fill="both", expand=True, padx=(14, 0))

        self._explore_chip_frame = tk.Frame(report_col, bg="#131f2e")
        self._explore_chip_frame.pack(fill="x", padx=10, pady=(10, 0))

        report_body = tk.Frame(report_col, bg="#131f2e")
        report_body.pack(fill="both", expand=True, padx=10, pady=(4, 10))
        self._explore_report_txt = tk.Text(report_body, font=("Helvetica", 11), wrap="word",
                                           width=32, height=14, padx=6, pady=4,
                                           relief="flat", borderwidth=0,
                                           bg="#263545", fg="#ffffff", insertbackground="#ffffff")
        report_sb = ttk.Scrollbar(report_body, orient="vertical",
                                  command=self._explore_report_txt.yview)
        self._explore_report_txt.configure(yscrollcommand=report_sb.set)
        self._explore_report_txt.pack(side="left", fill="both", expand=True)
        report_sb.pack(side="right", fill="y")
        self._explore_report_txt.tag_configure("optimal",   foreground="#4caf50", font=("Helvetica", 11, "bold"))
        self._explore_report_txt.tag_configure("warning",   foreground="#ef5350", font=("Helvetica", 11, "bold"))
        self._explore_report_txt.tag_configure("highlight", foreground="#38bdf8", font=("Helvetica", 11, "bold"))
        self._explore_report_txt.tag_configure("header",    foreground="#ffffff", font=("Helvetica", 13, "bold"))
        self._explore_report_txt.tag_configure("sec_framing",  foreground="#38bdf8", font=("Helvetica", 10, "bold"))
        self._explore_report_txt.tag_configure("sec_exposure", foreground="#f59e0b", font=("Helvetica", 10, "bold"))
        self._explore_report_txt.tag_configure("sec_window",   foreground="#4caf50", font=("Helvetica", 10, "bold"))
        self._explore_report_txt.tag_configure("sec_moon",     foreground="#ef5350", font=("Helvetica", 10, "bold"))
        self._explore_report_txt.tag_configure("dim",          foreground="#778899", font=("Helvetica", 11))
        self._explore_report_txt.tag_configure("amber",        foreground="#f59e0b", font=("Helvetica", 11, "bold"))

        # Animated idle graphic shown until the first real target is
        # loaded (see _start_startup_bloom's docstring) -- replaces the
        # plain "Explore — compare rigs on a target" text just for this
        # initial, from-launch empty state; clearing a search later still
        # falls back to that plain text via _explore_draw_empty as before.
        self._start_startup_bloom()
        self._explore_refresh_report()

    # ── Search plumbing (shares the Planner's floating suggestion popup) ──

    def _explore_on_search_key(self, event):
        """Key release in the Explore search — drive the shared suggestion popup."""
        if event.keysym in ("Up", "Down", "Return", "Escape", "Tab"):
            return
        self._update_floating_suggestions(entry=self.explore_search,
                                          on_select=self._explore_select_suggestion)

    def _explore_select_suggestion(self, target_id):
        """Suggestion clicked — fill the Explore search entry and load the target."""
        self._hide_suggestions()
        self.explore_search.delete(0, tk.END)
        self.explore_search.insert(0, target_id)
        self._explore_load_target()

    def _explore_resolve_target(self):
        """Resolve the Explore search entry to a target dict, or None."""
        raw = self.explore_search.get().strip().upper().replace(" ", "")
        if not raw:
            return None
        raw = self._normalize_catalog_key(raw)
        return self.targets.get(raw) or self.common_names_map.get(raw)

    def _explore_load_target(self, warn=True, fetch=True):
        """Load the searched target: update the info label, re-frame the rig
        stack over the new object, and (re)fetch the DSS image if needed.

        ``fetch=False`` defers the image fetch — used by _explore_analyze,
        which is about to change the needed span (new rig) and will call
        _explore_ensure_image itself, avoiding a wasted double-download."""
        t = self._explore_resolve_target()
        if t is None:
            if warn and self.explore_search.get().strip():
                messagebox.showwarning("Not Found",
                                       "No catalog object matches that search.")
            return
        changed = (self._explore_target is None
                   or self._explore_target.get("id") != t.get("id"))
        self._explore_target = t
        # A real target is loaded -- permanently stop the startup bloom
        # animation (see _start_startup_bloom's docstring; one-way flag).
        self._bloom_active = False
        common = t['common'].split(';')[0].strip() if t.get('common') else ""
        size_str = ""
        if t.get("size_maj"):
            size_str = f"  ·  {t['size_maj']:.0f}′×{(t.get('size_min') or t['size_maj']):.0f}′"
        self._explore_target_lbl.config(
            text=f"{t['id']}{('  —  ' + common) if common else ''}\n"
                 f"{t.get('obj_type', 'Object')}{size_str}")
        self._explore_refresh_cards()
        self._explore_refresh_report()
        if fetch:
            self._explore_ensure_image(force=changed)

    def _explore_toggle_filter_visibility(self, *args):
        """Show or hide the Rig Builder's filter-mode dropdown based on
        whether the currently selected camera is mono — mirrors the
        Planner tab's toggle_filter_visibility, but for Explore's own
        separate explore_camera_var/explore_filter_var pair."""
        c = self.data["cameras"].get(self.explore_camera_var.get())
        show = bool(c and not c.get("is_color", True))
        if show:
            if not self.explore_filter_dropdown.winfo_ismapped():
                self.explore_filter_dropdown.pack(fill="x", padx=12, pady=(0, 6),
                                                  after=self.explore_reduction_dropdown)
        else:
            self.explore_filter_dropdown.pack_forget()

    # ── Analysis ──────────────────────────────────────────────────────────

    def _explore_analyze(self):
        """Add the current scope/camera/reduction combo as a colour-coded frame."""
        # Make sure the searched target is loaded (user may have typed it
        # manually without pressing Return).
        t = self._explore_resolve_target()
        if t is not None and (self._explore_target is None
                              or self._explore_target.get("id") != t.get("id")):
            # Load the new target but defer the image fetch — the rig we're
            # about to add may enlarge the needed span, and the finally
            # block below reconciles the image exactly once either way.
            self._explore_load_target(warn=False, fetch=False)
        if self._explore_target is None:
            messagebox.showwarning("No Target",
                                   "Select a target first — search by catalog ID or name.")
            return

        try:
            s_name = self.explore_scope_var.get()
            c_name = self.explore_camera_var.get()
            red_str = self.explore_reduction_var.get() or "1.0×"
            s = self.data["scopes"].get(s_name)
            c = self.data["cameras"].get(c_name)
            if not s or not c:
                messagebox.showwarning("Incomplete", "Please select a Scope and a Camera.")
                return

            key = (s_name, c_name, red_str)
            for r in self._explore_rigs:
                if r["key"] == key:
                    self._show_toast("Rig already analyzed — see its legend card")
                    return
            if len(self._explore_rigs) >= self._EXPLORE_MAX_RIGS:
                self._show_toast(f"Limit of {self._EXPLORE_MAX_RIGS} rigs — remove one first")
                return

            try:
                reduction = float(red_str.rstrip("×x"))
            except ValueError:
                reduction = 1.0
            native_fl = float(s.get("native_fl", 1))
            aperture  = float(s.get("aperture", 1)) or 1.0
            eff_fl    = native_fl * reduction
            ps        = float(c.get("pixel_size", 1))
            sw, sh    = float(c.get("sensor_w", 1)), float(c.get("sensor_h", 1))
            fov_w = 2 * math.degrees(math.atan(sw / (2 * eff_fl)))
            fov_h = 2 * math.degrees(math.atan(sh / (2 * eff_fl)))
            scale = (ps / eff_fl) * 206.265

            used = {r["slot"] for r in self._explore_rigs}
            slot = next(i for i in range(self._EXPLORE_MAX_RIGS) if i not in used)
            color, dash = self._EXPLORE_PALETTE[slot]

            is_color = c.get("is_color", True)
            filter_mode = None if is_color else (self.explore_filter_var.get() or "Mono Lum")

            self._explore_rigs.append({
                "key": key, "scope": s_name, "camera": c_name, "reduction": red_str,
                "fov_w": fov_w, "fov_h": fov_h, "scale": scale,
                "eff_fl": eff_fl, "f_ratio": eff_fl / aperture,
                "slot": slot, "color": color, "dash": dash, "visible": True,
                # Filter mode is still captured per-rig at analyze time (not
                # re-read live later) so a rig's report stays put even if the
                # filter chip changes while comparing — only re-analyzing
                # that combo updates its own report. Bortle is NOT captured
                # here anymore — it's a single global sky-conditions setting
                # now, so every rig's report always reads it live (see
                # _explore_compute_report / _on_bortle_changed).
                "filter_mode": filter_mode,
                "is_color": is_color,
            })
            # Newest rig becomes the one shown in the report panel — same
            # "just analyzed" default the Planner tab's single box always had.
            self._explore_active_rig_key = key
            self._explore_refresh_cards()
            self._explore_refresh_report()
        finally:
            # Runs on success AND on every early return above, so the canvas
            # never shows a stale target after a deferred-fetch load.
            self._explore_ensure_image()

    def _explore_saved_rig_picked(self, event=None):
        """Saved-rig quick add — apply its scope/camera/reducer and analyze."""
        name = self._explore_rig_pick.get()
        self._explore_rig_pick.set("Add saved rig…")
        rig = self._find_rig(name)
        if rig is None:
            return
        if rig.get("scope") in self.data.get("scopes", {}):
            self.explore_scope_var.set(rig["scope"])
        if rig.get("camera") in self.data.get("cameras", {}):
            self.explore_camera_var.set(rig["camera"])
        if rig.get("reduction"):
            self.explore_reduction_var.set(rig["reduction"])
        self._explore_analyze()

    # ── Image management ──────────────────────────────────────────────────

    def _explore_target_span_deg(self):
        """Target's larger angular dimension in degrees (10′ floor for point-ish objects)."""
        t = self._explore_target
        maj = (t.get("size_maj") or 10.0) / 60.0
        mnr = (t.get("size_min") or t.get("size_maj") or 10.0) / 60.0
        return max(maj, mnr, 10.0 / 60.0)

    def _explore_needed_span(self):
        """Sky span (deg) the image should cover: the largest visible FOV or
        the target itself, plus padding.

        No longer capped at a flat 5deg — a wide-FOV rig (or several
        stacked rigs) can legitimately need more sky than that, and
        capping the REQUEST just guaranteed the frames wouldn't fit the
        picture (see _dss_fetch_plan, which is what actually protects
        against a wide request being slow: a fast fallback lane runs
        alongside the real one instead of a request being shrunk
        up front).

        Uses each rig's diagonal (not its raw width/height) so the frame
        still fits the fetched image at any rotation — the rotate slider
        spins frames in screen space without changing the fetched span.
        """
        dims = [math.hypot(r["fov_w"], r["fov_h"]) for r in self._explore_rigs if r["visible"]]
        base = max(dims + [self._explore_target_span_deg() * 1.2, 0.2])
        return base * 1.35

    def _explore_ensure_image(self, force=False):
        """Fetch a DSS image if the current one is missing, for the wrong
        target, too small (frames would clip), or absurdly oversized."""
        t = self._explore_target
        if t is None:
            self._explore_draw_empty()
            return
        span = self._explore_needed_span()
        v = self._explore_view
        if (not force and v is not None
                and v.get("target_id") == t["id"] and v.get("deg")
                and v["deg"] * 0.45 <= span <= v["deg"] * 1.02):
            self._explore_redraw()
            return
        self._explore_fetch(t, span)

    def _explore_fetch(self, t, span_deg):
        """Fetch a DSS image sized to show every visible rig frame, using
        the same fast-lane/slow-lane split as the Planner tab's
        fetch_thumbnail (see _dss_fetch_plan): a small/quick request runs
        alongside the real one instead of after it, so a wide span (needing
        the slow lane) doesn't stall the whole view behind a long timeout
        before showing anything.

        Stale-result guard: self._explore_fetch_seq is a monotonic counter
        bumped on every call — if the user picks a different target or
        rig set while a fetch is in flight, a late-arriving result checks
        its own captured ``seq`` against the current one and discards
        itself on a mismatch, the same principle as the Planner tab's
        current_target_info check.

        For a span needing the slow lane, this no longer shows the small
        fast-lane image while waiting (Jerry: same fix as the Planner
        tab, applied here too — "let's do the same thing for the explore
        panel"). Instead _draw_explore_wait_frame puts up an explicit
        wait screen (spinner, elapsed time, "this can take up to 90s")
        with a "Show 5° field instead" pill, and only actually displays a
        picture once the full request lands, the user cancels, or every
        lane gives up.
        """
        self._explore_fetch_seq += 1
        seq = self._explore_fetch_seq
        target_id = t["id"]

        fast_lane, slow_lane = _dss_fetch_plan(span_deg)

        self._explore_wait_token += 1
        token = self._explore_wait_token
        self._cancel_explore_wait_tick()
        self._explore_wait_fast_ready = None
        self._explore_wait_cancelled = False

        if slow_lane is None:
            # Narrow span — single lane, no wait screen: this was already
            # fast before the wide-field wait state existed, and still is.
            self._explore_view = None          # marks "loading" for _explore_redraw
            self._explore_redraw()
            threading.Thread(
                target=self._fetch_dss_lane,
                args=(target_id, fast_lane,
                      lambda img, deg: self._explore_show_result(target_id, seq, img, deg, token),
                      lambda: self._explore_show_error(target_id, seq, token)),
                daemon=True).start()
            return

        # Wide span — put up the wait state, then run both lanes
        # CONCURRENTLY underneath it (not fast-then-slow): the fast lane
        # is what Cancel falls back to, the slow lane is the real request.
        self._explore_wait_target_id = target_id
        self._explore_wait_span_deg = span_deg
        self._explore_wait_start_ts = time.monotonic()
        self._explore_wait_angle = 0
        self._draw_explore_wait_frame(token, seq)

        def _fast_success(img, deg):
            if token != self._explore_wait_token:
                return  # superseded by a newer _explore_fetch() call
            self._explore_wait_fast_ready = (img, deg)
            if self._explore_wait_cancelled:
                self._explore_show_result(target_id, seq, img, deg, token)

        def _fast_failed():
            if token == self._explore_wait_token and self._explore_wait_cancelled:
                # User asked for the small field, and even that failed.
                self._explore_show_error(target_id, seq, token)

        def _slow_success(img, deg):
            self._explore_show_result(target_id, seq, img, deg, token)

        def _slow_failed():
            if self._explore_wait_fast_ready is not None:
                img, deg = self._explore_wait_fast_ready
                self._explore_show_result(target_id, seq, img, deg, token)
            else:
                self._explore_show_error(target_id, seq, token)

        threading.Thread(target=self._fetch_dss_lane,
                          args=(target_id, fast_lane, _fast_success, _fast_failed),
                          daemon=True).start()
        threading.Thread(target=self._fetch_dss_lane,
                          args=(target_id, [slow_lane], _slow_success, _slow_failed),
                          daemon=True).start()

    def _explore_show_result(self, target_id, seq, img, fetched_deg, token):
        """Cache and display a fetched DSS image — the shared landing
        point for every _explore_fetch() path (narrow-span single lane,
        wide-span fast lane, wide-span slow lane, and the post-cancel
        fast-lane fallback)."""
        if seq != self._explore_fetch_seq:
            return  # user moved on to a different target/rig set
        if token != self._explore_wait_token:
            return  # superseded by a newer _explore_fetch() call
        t = self._explore_target
        if t is None or t.get("id") != target_id:
            return
        # The fast and slow lanes race — don't let a smaller result that
        # happens to land SECOND clobber a bigger one already on screen.
        # Scoped to THIS token only — see _show_dss_result's matching
        # comment: the currently displayed image may be a leftover from an
        # EARLIER _explore_fetch() call (a wider rig set that's since been
        # swapped for a narrower one), and comparing against that stale
        # image would wrongly suppress a legitimately smaller new result.
        v = self._explore_view
        if (self._explore_result_token == token and v is not None
                and v.get("target_id") == target_id
                and v.get("deg") is not None and fetched_deg < v["deg"]):
            return
        self._cancel_explore_wait_tick()
        self._explore_view = {"img": img, "deg": fetched_deg, "target_id": target_id}
        self._explore_result_token = token
        self._explore_redraw()

    def _explore_show_error(self, target_id, seq, token):
        """Show the geometric-ellipse fallback — every lane relevant to
        this fetch has now failed with nothing to show."""
        if seq != self._explore_fetch_seq:
            return
        if token != self._explore_wait_token:
            return
        t = self._explore_target
        if t is None or t.get("id") != target_id:
            return
        self._cancel_explore_wait_tick()
        self._explore_view = {"img": None,
                              "deg": self._explore_wait_span_deg or 5.0,
                              "target_id": target_id}
        self._explore_redraw()

    def _cancel_explore_wait_tick(self):
        """Stop the Explore tab's wide-field wait screen's spinner/
        elapsed-timer redraw loop, if one is scheduled. Idempotent."""
        if self._explore_wait_after_id is not None:
            try:
                self.root.after_cancel(self._explore_wait_after_id)
            except Exception:
                pass
            self._explore_wait_after_id = None

    def _draw_explore_wait_frame(self, token, seq):
        """Draw one frame of the Explore tab's wide-field wait screen
        (spinner, message, elapsed timer, and — unless already cancelled —
        a 'Show 5° field instead' pill) and reschedule itself. Stops on
        its own once superseded (token/seq mismatch) or once the user has
        navigated away from this target."""
        if token != self._explore_wait_token or seq != self._explore_fetch_seq:
            return
        t = self._explore_target
        if t is None or t.get("id") != self._explore_wait_target_id:
            return

        cpx = self._EXPLORE_CANVAS_PX
        cv = self.explore_canvas
        cv.delete("all")
        cv.create_rectangle(0, 0, cpx, cpx, fill="#0a1420", outline="")

        cx, cy = cpx // 2, cpx // 2 - 22
        r = 17
        self._explore_wait_angle = (self._explore_wait_angle + 24) % 360
        cv.create_oval(cx - r, cy - r, cx + r, cy + r, outline="#1e2d3e", width=3)
        cv.create_arc(cx - r, cy - r, cx + r, cy + r,
                      start=self._explore_wait_angle, extent=100,
                      style="arc", outline="#38bdf8", width=3)

        if self._explore_wait_cancelled:
            msg = "Fetching 5° field…"
            sub1 = "showing that instead of the full"
            sub2 = f"{self._explore_wait_span_deg:.1f}° field"
        else:
            msg = "Fetching wide field…"
            sub1 = f"{self._explore_wait_target_id} needs a {self._explore_wait_span_deg:.1f}° image —"
            sub2 = "this can take up to 90s"

        cv.create_text(cx, cy + 32, text=msg, fill="#ffffff", font=("Helvetica", 10, "bold"))
        cv.create_text(cx, cy + 49, text=sub1, fill="#7f93a6", font=("Helvetica", 8))
        cv.create_text(cx, cy + 62, text=sub2, fill="#7f93a6", font=("Helvetica", 8))

        elapsed = int(time.monotonic() - self._explore_wait_start_ts)
        cv.create_text(cx, cy + 79, text=f"{elapsed}s elapsed", fill="#4a6478", font=("Helvetica", 8))

        if not self._explore_wait_cancelled:
            pill_id = cv.create_text(cx, cy + 101, text="Show 5° field instead",
                                     fill="#aaddff", font=("Helvetica", 8, "bold"))
            bbox = cv.bbox(pill_id)
            if bbox:
                pad = 6
                bg_id = cv.create_rectangle(
                    bbox[0] - pad, bbox[1] - 4, bbox[2] + pad, bbox[3] + 4,
                    fill="#182636", outline="#2a3f55")
                cv.tag_lower(bg_id, pill_id)
                for item in (bg_id, pill_id):
                    cv.tag_bind(item, "<Button-1>",
                               lambda e, t=token: self._explore_wait_cancel(t))
                    cv.tag_bind(item, "<Enter>", lambda e: cv.configure(cursor="hand2"))
                    cv.tag_bind(item, "<Leave>", lambda e: cv.configure(cursor=""))

        self._explore_span_lbl.config(text="")
        self._explore_wait_after_id = self.root.after(
            90, lambda: self._draw_explore_wait_frame(token, seq))

    def _explore_wait_cancel(self, token):
        """'Show 5° field instead' pill handler: stop waiting on the slow
        lane's full-size request and show the fast lane's result the
        moment it's available (immediately, if it already landed)."""
        if token != self._explore_wait_token:
            return
        self._explore_wait_cancelled = True
        if self._explore_wait_fast_ready is not None:
            img, deg = self._explore_wait_fast_ready
            self._explore_show_result(self._explore_wait_target_id,
                                      self._explore_fetch_seq, img, deg, token)
        # else: the fast lane hasn't landed yet — _draw_explore_wait_frame's
        # next tick switches the message to "Fetching 5° field…", and
        # _fast_success (in _explore_fetch) will display it once it lands.

    # ── Drawing ───────────────────────────────────────────────────────────

    # ═══════════════════════════════════════════════════════════════════
    # STARTUP GRAPHIC — "Aperture Bloom" idle animation for the Explore
    # tab's canvas (the app's default tab, so this is what's on screen
    # from launch, before anything has been searched)
    # ═══════════════════════════════════════════════════════════════════
    # Approved design: lightbucket-startup-splash-options.html Option C,
    # refined by lightbucket-optionC-colorcycle.html's C1 ("smooth blend").
    # Palette mostly reuses colors already used elsewhere in the app (the
    # type chip's teal/violet/pink, the card accent blue) plus one new
    # warm gold to round the loop back to blue. Ordered by hue (not the
    # order they were first picked in) so every step around the cycle,
    # including the wrap from gold back to teal, is as short a hop as this
    # particular set of colors allows -- _lerp_hex_color_hsv still sweeps
    # through a bit of green on that one longer hop, but a sorted order
    # keeps it brief rather than the worse jump an arbitrary order can hit.
    _BLOOM_PALETTE = ["#2dd4bf", "#38bdf8", "#a78bfa", "#f472b6", "#f0b429"]
    _BLOOM_ROTATION_PERIOD_S = 20.0   # one full spin == one full pass through the palette
    _BLOOM_TICK_MS = 50               # ~20fps -- smooth without burning CPU while idle

    @staticmethod
    def _lerp_hex_color(c1, c2, frac):
        """Blend two '#rrggbb' colors in RGB space; frac in [0, 1]
        (0 -> c1, 1 -> c2). Only used to blend a color toward black/white
        (the glow's halo/bright-core shading) -- fine there since one
        endpoint is grayscale, but see _lerp_hex_color_hsv for blending
        between two saturated palette colors."""
        r1, g1, b1 = int(c1[1:3], 16), int(c1[3:5], 16), int(c1[5:7], 16)
        r2, g2, b2 = int(c2[1:3], 16), int(c2[3:5], 16), int(c2[5:7], 16)
        r = round(r1 + (r2 - r1) * frac)
        g = round(g1 + (g2 - g1) * frac)
        b = round(b1 + (b2 - b1) * frac)
        return f"#{r:02x}{g:02x}{b:02x}"

    @staticmethod
    def _lerp_hex_color_hsv(c1, c2, frac):
        """Blend two '#rrggbb' colors by hue, taking the shorter way
        around the color wheel, rather than straight RGB. Two palette
        stops far apart in hue (e.g. gold and blue, roughly opposite each
        other) produce a muddy gray-green halfway through a plain RGB
        lerp, since each channel just marches independently toward the
        other; a hue-space blend instead sweeps visibly through the
        colors in between, which is what "cycling through a palette"
        should look like."""
        r1, g1, b1 = (int(c1[i:i + 2], 16) / 255.0 for i in (1, 3, 5))
        r2, g2, b2 = (int(c2[i:i + 2], 16) / 255.0 for i in (1, 3, 5))
        h1, s1, v1 = colorsys.rgb_to_hsv(r1, g1, b1)
        h2, s2, v2 = colorsys.rgb_to_hsv(r2, g2, b2)
        dh = h2 - h1
        if dh > 0.5:
            dh -= 1.0
        elif dh < -0.5:
            dh += 1.0
        h = (h1 + dh * frac) % 1.0
        s = s1 + (s2 - s1) * frac
        v = v1 + (v2 - v1) * frac
        r, g, b = colorsys.hsv_to_rgb(h, s, v)
        return f"#{round(r * 255):02x}{round(g * 255):02x}{round(b * 255):02x}"

    def _bloom_color_at(self, phase):
        """Palette color at rotation phase in [0, 1) — one full pass per
        rotation, blended smoothly between adjacent palette stops by hue
        (matches C1 "smooth blend" from the approved color-cycle review)."""
        pal = self._BLOOM_PALETTE
        n = len(pal)
        seg = (phase % 1.0) * n
        i = int(seg) % n
        frac = seg - int(seg)
        return self._lerp_hex_color_hsv(pal[i], pal[(i + 1) % n], frac)

    def _start_startup_bloom(self):
        """Kick off the animated "aperture bloom" idle graphic on
        ``self.explore_canvas`` — shown from launch (Explore is the
        default tab) until the first real target is loaded there.

        ``_explore_load_target`` clears ``self._bloom_active`` (a one-way
        flag) the moment it resolves a real target, and this loop checks
        that flag at the top of every tick rather than being cancelled
        explicitly, so it just quietly stops rescheduling itself at that
        point — it never reactivates even if the target is later cleared
        back to the plain empty state (``_explore_draw_empty``); this is a
        one-time "welcome" graphic, not a recurring empty-state.
        """
        self._bloom_active = True
        self._bloom_frame = 0
        self._tick_startup_bloom()

    def _tick_startup_bloom(self):
        """One frame of the startup bloom; reschedules itself via
        ``after()`` until ``_start_startup_bloom``'s docstring's stop
        condition is met."""
        if not getattr(self, "_bloom_active", False):
            return   # a real target has loaded -- stop for good, no reschedule
        canvas = self.explore_canvas
        try:
            if not canvas.winfo_exists():
                self._bloom_active = False
                return
        except tk.TclError:
            self._bloom_active = False
            return

        cpx = self._EXPLORE_CANVAS_PX
        cw = canvas.winfo_width() or cpx
        ch = canvas.winfo_height() or cpx
        cx, cy = cw / 2.0, ch * 0.46
        R = min(cw, ch) * 0.19   # blade-ring radius

        period_ticks = max(1, int(self._BLOOM_ROTATION_PERIOD_S * 1000 / self._BLOOM_TICK_MS))
        phase  = (self._bloom_frame % period_ticks) / period_ticks
        color  = self._bloom_color_at(phase)
        angle0 = phase * 360.0

        canvas.delete("all")

        # Soft core glow -- Tkinter fills have no alpha channel, so this is
        # faked with three solid concentric ovals, each hand-blended toward
        # the canvas background (dim halo) or toward white (bright core),
        # rather than the SVG mockup's true radial gradient.
        bg = "#050810"
        halo   = self._lerp_hex_color(bg, color, 0.30)
        mid    = self._lerp_hex_color(bg, color, 0.60)
        bright = self._lerp_hex_color(color, "#ffffff", 0.55)
        # Kept comfortably SMALLER than the blade ring's own radius R below
        # (0.72R at most) -- leaving a dark gap between the glow's edge and
        # the blades so the ring reads as a distinct shape around the glow,
        # not just a stroke buried inside a same-color filled disc.
        for r_frac, fill in ((0.72, halo), (0.46, mid), (0.20, bright)):
            r = R * r_frac
            canvas.create_oval(cx - r, cy - r, cx + r, cy + r,
                                outline="", fill=fill, tags="bloom")

        # 6 curved "blades" forming the iris ring, each spanning less than
        # its 60° sector with a clear gap on either side (butt caps, not
        # round, so the caps don't visually bridge that gap) -- rotated
        # together by angle0, each approximated as a smoothed polyline.
        gap_deg = 9
        for k in range(6):
            a0 = math.radians(angle0 + k * 60 + gap_deg)
            a1 = math.radians(angle0 + (k + 1) * 60 - gap_deg)
            coords = []
            for step in range(9):
                a = a0 + (a1 - a0) * step / 8
                coords.extend((cx + R * math.cos(a), cy + R * math.sin(a)))
            canvas.create_line(*coords, fill=color, width=3,
                                smooth=True, capstyle="butt", tags="bloom")

        canvas.create_oval(cx - 3, cy - 3, cx + 3, cy + 3,
                            fill="#f4fbff", outline="", tags="bloom")

        # "Awaiting Target" status label above the bloom, tinted with the
        # same cycling color as the ring/glow this tick so it reads as part
        # of the same live animation rather than a separate static caption.
        canvas.create_text(cw / 2, cy - R * 2.3, text="A W A I T I N G   T A R G E T",
                            fill=color, font=("Helvetica", 10, "bold"), tags="bloom")

        canvas.create_text(cw / 2, cy + R * 2.05, text="LIGHTBUCKET",
                            fill="#e8eef4", font=("Helvetica", 16, "bold"), tags="bloom")
        canvas.create_text(cw / 2, cy + R * 2.05 + 20, text="ASTRO PLANNER",
                            fill="#556677", font=("Helvetica", 9, "bold"), tags="bloom")

        self._bloom_frame += 1
        self.root.after(self._BLOOM_TICK_MS, self._tick_startup_bloom)

    def _explore_draw_empty(self):
        """Empty-state canvas message."""
        cpx = self._EXPLORE_CANVAS_PX
        cv = self.explore_canvas
        cv.delete("all")
        cv.create_text(cpx // 2, cpx // 2 - 12, text="Explore — compare rigs on a target",
                       fill="#556677", font=("Helvetica", 11, "bold"))
        cv.create_text(cpx // 2, cpx // 2 + 10,
                       text="Search a target, pick a scope/camera/reducer, press ⊕ Analyze",
                       fill="#445566", font=("Helvetica", 9))
        self._explore_span_lbl.config(text="")

    def _explore_redraw(self):
        """Redraw the shared image (or fallback ellipse) and every visible rig frame."""
        cpx = self._EXPLORE_CANVAS_PX
        cv = self.explore_canvas
        t = self._explore_target
        if t is None:
            self._explore_draw_empty()
            return
        cv.delete("all")
        v = self._explore_view
        if v is None or v.get("target_id") != t["id"]:
            cv.create_text(cpx // 2, cpx // 2, text="Loading DSS image…",
                           fill="yellow", font=("Helvetica", 10))
            self._explore_span_lbl.config(text="")
            return

        span = v["deg"]
        zoom = max(1.0, min(16.0, self._explore_zoom))
        ppd = cpx / span * zoom   # pixels per degree, after zoom
        cxc = cyc = cpx / 2
        # Frame group's own pivot — offset from the canvas/image centre by
        # the shared pan (see _explore_fov_mouse_drag). The fallback
        # ellipse above uses cxc/cyc directly (it's the target itself, not
        # an FOV frame, so it never pans); only the rig frames below do.
        fcx = cpx / 2 + self._explore_pan_x
        fcy = cpx / 2 + self._explore_pan_y

        if v["img"] is not None:
            # Cache key holds a strong reference to the source image and is
            # compared by identity (``is``).  Never key on id(img): after a
            # target switch the old image is freed and CPython frequently
            # reuses its address for the new one, so (id, span, zoom) collides
            # and the canvas silently shows the previous target's picture.
            cache = self._explore_photo_cache
            if cache and cache[0] is v["img"] and cache[1] == span and cache[2] == zoom:
                photo = cache[3]
            else:
                src = v["img"]
                if zoom > 1.0:
                    # Center-crop a 1/zoom fraction then resize to canvas —
                    # same technique the Planner tab's zoom uses, so no
                    # re-download is ever needed just to zoom in/out.
                    orig_w, orig_h = src.size
                    crop_w, crop_h = orig_w / zoom, orig_h / zoom
                    left, top = (orig_w - crop_w) / 2, (orig_h - crop_h) / 2
                    src = src.crop((left, top, left + crop_w, top + crop_h))
                resized = src.resize((cpx, cpx), Image.Resampling.LANCZOS)
                photo = ImageTk.PhotoImage(resized)
                self._explore_photo_cache = (v["img"], span, zoom, photo)
            self._explore_photo = photo
            cv.create_image(0, 0, anchor="nw", image=photo)
        else:
            # Offline / fetch failed — geometric representation of the target
            t_maj = (t.get("size_maj") or 10.0) / 60.0 * ppd
            t_min = (t.get("size_min") or t.get("size_maj") or 10.0) / 60.0 * ppd
            cv.create_oval(cxc - t_maj / 2, cyc - t_min / 2,
                           cxc + t_maj / 2, cyc + t_min / 2,
                           outline="cyan", width=2)
            cv.create_text(cpx // 2, 16,
                           text="DSS unavailable — geometric view (check network)",
                           fill="#778899", font=("Helvetica", 9))

        # Orientation marker — DSS cutouts are North-up / East-left. Lives in
        # the top-LEFT corner (not top-right) so it doesn't collide with the
        # zoom +/- pill floating over the top-right corner.
        cv.create_line(40, 34, 40, 16, fill="#94a3b8", width=1, arrow=tk.LAST)
        cv.create_text(49, 22, text="N", fill="#94a3b8", font=("Helvetica", 8))
        cv.create_line(40, 34, 22, 34, fill="#94a3b8", width=1, arrow=tk.LAST)
        cv.create_text(14, 34, text="E", fill="#94a3b8", font=("Helvetica", 8))

        # Shared rotation — every visible frame spins together around the
        # image centre while the DSS image itself stays fixed North-up.
        # _explore_pa_deg IS the displayed NINA-style sky PA (unlike the
        # Planner tab's _fov_angle, which is a raw drag angle converted to
        # PA only for display). The standard rotation matrix below is
        # clockwise-positive on this y-down canvas, so the angle is negated
        # here to make increasing PA turn the frame counter-clockwise —
        # North (up) toward East (left) — matching both the astronomical PA
        # convention and NINA's own rotator direction.
        angle_rad = math.radians(-self._explore_pa_deg)
        cos_a, sin_a = math.cos(angle_rad), math.sin(angle_rad)

        def _rot(dx, dy):
            return fcx + dx * cos_a - dy * sin_a, fcy + dx * sin_a + dy * cos_a

        clipped = False
        for r in self._explore_rigs:
            if not r["visible"]:
                continue
            rw = r["fov_w"] * ppd / 2
            rh = r["fov_h"] * ppd / 2
            corners = [_rot(-rw, -rh), _rot(rw, -rh), _rot(rw, rh), _rot(-rw, rh)]
            if any(px < -1 or px > cpx + 1 or py < -1 or py > cpx + 1
                   for px, py in corners):
                clipped = True
            color = r["color"]
            width = 2
            if self._explore_hilite is not None:
                if r["key"] == self._explore_hilite:
                    width = 3
                else:
                    color = self._dim_hex(r["color"])
            kwargs = {"outline": color, "width": width, "fill": ""}
            if r["dash"]:
                kwargs["dash"] = r["dash"]
            flat = [coord for pt in corners for coord in pt]
            cv.create_polygon(flat, **kwargs)
            # Slot number just inside the frame's first corner (clamped on-canvas)
            tx = min(max(corners[0][0], 0) + 11, cpx - 10)
            ty = min(max(corners[0][1], 0) + 10, cpx - 10)
            cv.create_text(tx, ty, text=str(r["slot"] + 1), fill=color,
                           font=("Helvetica", 9, "bold"))

        if clipped:
            cv.create_text(cpx // 2, cpx - 30,
                           text="⚠ some frames exceed the image span — clipped",
                           fill="#f59e0b", font=("Helvetica", 8, "bold"))

        # Target label — shadowed like the Planner preview
        common = t['common'].split(';')[0].strip() if t.get('common') else ""
        label = f"{t['id']}{('  ·  ' + common) if common else ''}"
        cv.create_text(cpx // 2 + 1, cpx - 9, text=label, fill="black",
                       font=("Helvetica", 8, "bold"))
        cv.create_text(cpx // 2, cpx - 10, text=label, fill="white",
                       font=("Helvetica", 8, "bold"))

        self._explore_span_lbl.config(text=f"Image span {span:.2f}° × {span:.2f}°")

    # ── Rotate / zoom ────────────────────────────────────────────────────

    def _explore_on_rotate(self, value):
        """Rotate slider moved — update the shared PA and redraw.

        The slider's own value IS the displayed NINA-style sky PA (no
        conversion needed here); _explore_redraw negates it when building
        the screen rotation matrix so increasing PA turns the frames
        counter-clockwise, matching NINA."""
        self._explore_pa_deg = float(value) % 360.0
        self._explore_pa_label.config(text=f"{self._explore_pa_deg:.1f}°")
        self._explore_schedule_redraw()

    def _explore_schedule_redraw(self):
        """Coalesce rapid rotate-slider events into at most one redraw per
        ~30ms tick (same after()-guard pattern _schedule_analysis uses
        elsewhere in the app).

        _explore_redraw() costs roughly 10-20ms — it crops/resizes the DSS
        image with PIL and rebuilds the whole canvas — and a real drag
        fires far more motion events than that per second. Calling it
        straight from the rotate slider's command on every tick let Tk's
        single main thread fall behind: the slider handle itself visibly
        lagged the mouse because the thread was still busy painting the
        previous frame when the next motion event arrived. The PA label is
        updated immediately in the caller (cheap), so only the actual
        pixel work is throttled.

        Zoom no longer goes through here at all — it's discrete +/- button
        clicks now (see _explore_apply_zoom), not a continuous drag, so
        there's nothing to coalesce and it redraws immediately.
        """
        if self._explore_redraw_after_id is not None:
            return
        self._explore_redraw_after_id = self.root.after(30, self._run_explore_redraw)

    def _run_explore_redraw(self):
        """Fire the throttled redraw scheduled by _explore_schedule_redraw."""
        self._explore_redraw_after_id = None
        self._explore_redraw()

    def _explore_apply_zoom(self, factor):
        """Zoom pill's + / − buttons and scroll wheel — multiply zoom by
        factor, clamp to 1.0-16.0, update the readout, then redraw
        immediately (a button click is discrete, not a continuous drag,
        so there's no need to throttle this one)."""
        self._explore_zoom = max(1.0, min(16.0, self._explore_zoom * factor))
        self._explore_zoom_label.config(text=f"{self._explore_zoom:.1f}×")
        self._explore_redraw()

    def _explore_mousewheel(self, event):
        """Zoom on scroll wheel over the Explore canvas (mirrors the Planner tab)."""
        if event.num == 4 or (hasattr(event, 'delta') and event.delta > 0):
            self._explore_apply_zoom(1.25)
        else:
            self._explore_apply_zoom(1 / 1.25)

    def _explore_fov_mouse_down(self, event):
        """Begin a pan drag — records the start point; rotation stays on
        the slider (see setup_explore_tab's rotate_bar), so there's no
        corner-handle hit-testing to do here."""
        self._explore_dragging = True
        self._explore_drag_start = (event.x, event.y)

    def _explore_fov_mouse_drag(self, event):
        """Accumulate the pan offset and redraw (throttled, same as the
        rotate slider — see _explore_schedule_redraw)."""
        if not self._explore_dragging or self._explore_drag_start is None:
            return
        dx = event.x - self._explore_drag_start[0]
        dy = event.y - self._explore_drag_start[1]
        self._explore_pan_x += dx
        self._explore_pan_y += dy
        self._explore_drag_start = (event.x, event.y)
        self._explore_schedule_redraw()

    def _explore_fov_mouse_up(self, event):
        """End the pan drag."""
        self._explore_dragging = False
        self._explore_drag_start = None

    def _explore_reset_view(self):
        """Reset rotation, zoom, and pan to 0° / 1.0× / centred — leaves
        analyzed rigs and the current target untouched."""
        self._explore_pa_deg = 0.0
        self._explore_zoom = 1.0
        self._explore_pan_x = 0.0
        self._explore_pan_y = 0.0
        self._explore_pa_var.set(0.0)
        self._explore_pa_label.config(text="0.0°")
        self._explore_zoom_label.config(text="1.0×")
        self._explore_redraw()

    # ── Legend cards ──────────────────────────────────────────────────────

    def _explore_refresh_cards(self):
        """Rebuild the legend cards (one per analyzed rig) in the left rail.

        Night-aware: these cards are created after startup so they aren't in
        the ``_orig_colors`` snapshot — instead of relying on the recolor
        walker / day-restore loop, the builder picks its palette from
        ``self.night_mode`` and ``_apply_night_mode`` rebuilds the cards on
        every toggle (same pattern as the queue and plan card lists).
        The rig colour swatch and border are kept in both modes since they
        map 1:1 to the frame colours on the canvas.
        """
        nm = getattr(self, "night_mode", False)
        card_bg    = "#2a0000" if nm else "#1e2d3e"
        border_off = "#331111" if nm else "#2a3642"
        name_on    = "#cc4444" if nm else "#dddddd"
        name_off   = "#661111" if nm else "#667788"
        stats_fg   = "#993333" if nm else "#8899aa"
        eye_on     = "#cc4400" if nm else "#7eb8d4"
        eye_off    = "#661111" if nm else "#556677"
        del_fg     = "#993333" if nm else "#667788"
        fits_fg    = "#cc6600" if nm else "#4caf50"
        nofit_fg   = "#ff2222" if nm else "#ef5350"
        empty_fg   = "#661111" if nm else "#556677"
        empty_bg   = "#1a0000" if nm else self._explore_cards_frame.cget("bg")

        frame = self._explore_cards_frame
        for w in frame.winfo_children():
            w.destroy()

        if not self._explore_rigs:
            tk.Label(frame, text="No rigs analyzed yet", bg=empty_bg,
                     fg=empty_fg, font=("Helvetica", 9)).pack(anchor="w", padx=2, pady=4)
            return

        active_bg = "#440000" if nm else "#16283a"

        t = self._explore_target
        for r in self._explore_rigs:
            is_active = (r["key"] == self._explore_active_rig_key)
            this_bg = active_bg if is_active else card_bg
            card = tk.Frame(frame, bg=this_bg,
                            highlightthickness=2 if is_active else 1,
                            highlightbackground=r["color"] if r["visible"] else border_off)
            card.pack(fill="x", pady=3)

            row1 = tk.Frame(card, bg=this_bg)
            row1.pack(fill="x", padx=6, pady=(4, 0))
            sw = tk.Canvas(row1, width=14, height=10, bg=this_bg,
                           highlightthickness=0)
            sw.pack(side="left", pady=1)
            line_kwargs = {"fill": r["color"], "width": 2}
            if r["dash"]:
                line_kwargs["dash"] = r["dash"]
            sw.create_line(0, 5, 14, 5, **line_kwargs)

            name_fg = name_on if r["visible"] else name_off
            title = f"{r['slot'] + 1}· {r['scope']} · {r['camera']}"
            if len(title) > 34:
                title = title[:33] + "…"
            title += f" · {r['reduction']}"
            name_lbl = tk.Label(row1, text=title, bg=this_bg,
                                fg=name_fg, font=("Helvetica", 9, "bold"), anchor="w")
            name_lbl.pack(side="left", padx=(4, 0), fill="x", expand=True)

            del_lbl = tk.Label(row1, text="✕", bg=this_bg, fg=del_fg,
                               font=("Helvetica", 9, "bold"), cursor="hand2")
            del_lbl.pack(side="right", padx=(2, 0))
            del_lbl.bind("<Button-1>", lambda e, k=r["key"]: self._explore_remove_rig(k))
            ToolTip(del_lbl, "Remove this rig from the comparison")

            eye_lbl = tk.Label(row1, text="👁" if r["visible"] else "‒",
                               bg=this_bg,
                               fg=eye_on if r["visible"] else eye_off,
                               font=("Helvetica", 9), cursor="hand2", width=2)
            eye_lbl.pack(side="right")
            eye_lbl.bind("<Button-1>", lambda e, k=r["key"]: self._explore_toggle_rig(k))
            ToolTip(eye_lbl, "Show / hide this rig's frame")

            stats = (f"{r['fov_w']:.2f}°×{r['fov_h']:.2f}°  ·  "
                     f"{r['scale']:.2f}\"/px  ·  f/{r['f_ratio']:.1f}  ·  "
                     f"{r['eff_fl']:.0f}mm")
            stats_lbl = tk.Label(card, text=stats, bg=this_bg,
                                 fg=stats_fg, font=("Helvetica", 8), anchor="w")
            stats_lbl.pack(fill="x", padx=24, pady=(0, 0))

            extra_widgets = []
            if t is not None:
                t_maj = (t.get("size_maj") or 10.0) / 60.0
                t_min = (t.get("size_min") or t.get("size_maj") or 10.0) / 60.0
                fits = (t_maj <= r["fov_w"] and t_min <= r["fov_h"])
                fill_pct = min(100.0, 100.0 * (math.pi / 4.0 * t_maj * t_min)
                               / max(r["fov_w"] * r["fov_h"], 1e-9))
                fit_txt = (f"✅ fits · target fills {fill_pct:.0f}%" if fits
                           else "❌ too big for sensor")
                fit_lbl = tk.Label(card, text=fit_txt, bg=this_bg,
                                   fg=fits_fg if fits else nofit_fg,
                                   font=("Helvetica", 8), anchor="w")
                fit_lbl.pack(fill="x", padx=24, pady=(0, 4))
                extra_widgets.append(fit_lbl)
            else:
                stats_lbl.pack_configure(pady=(0, 4))

            # Hover — highlight this rig's frame on the canvas
            hover_widgets = [card, row1, sw, name_lbl, stats_lbl, eye_lbl, del_lbl] + extra_widgets
            for w in hover_widgets:
                w.bind("<Enter>", lambda e, k=r["key"]: self._explore_set_hilite(k))
                w.bind("<Leave>", lambda e: self._explore_set_hilite(None))

            # Click (anywhere except the ✕/👁 controls, which have their own
            # actions) — show this rig's report on the right, same as
            # clicking its chip above the report panel.
            select_widgets = [card, row1, sw, name_lbl, stats_lbl] + extra_widgets
            for w in select_widgets:
                w.bind("<Button-1>", lambda e, k=r["key"]: self._explore_select_rig_for_report(k))

    def _explore_set_hilite(self, key):
        """Set / clear the hover-highlighted rig and redraw the frames."""
        if key == self._explore_hilite:
            return
        self._explore_hilite = key
        self._explore_redraw()

    def _explore_toggle_rig(self, key):
        """Toggle a rig frame's visibility (its colour slot is retained)."""
        for r in self._explore_rigs:
            if r["key"] == key:
                r["visible"] = not r["visible"]
                break
        self._explore_refresh_cards()
        self._explore_ensure_image()   # span may shrink/grow with visibility

    def _explore_remove_rig(self, key):
        """Remove a rig from the comparison, freeing its colour slot."""
        self._explore_hilite = None
        self._explore_rigs = [r for r in self._explore_rigs if r["key"] != key]
        if self._explore_active_rig_key == key:
            # Fall back to whichever rig is now last in the list (or none
            # left at all) — same "most recent" default _explore_analyze uses.
            self._explore_active_rig_key = self._explore_rigs[-1]["key"] if self._explore_rigs else None
        self._explore_refresh_cards()
        self._explore_refresh_report()
        self._explore_ensure_image()

    def _explore_clear(self):
        """Remove all analyzed rigs (keeps the target and image)."""
        if not self._explore_rigs:
            return
        self._explore_hilite = None
        self._explore_rigs = []
        self._explore_active_rig_key = None
        self._explore_refresh_cards()
        self._explore_refresh_report()
        self._explore_redraw()

    # ── Report panel ─────────────────────────────────────────────────────

    def _explore_select_rig_for_report(self, key):
        """Rig chip or rail-card clicked — show that rig's report on the
        right. Independent of hover (which only highlights the canvas
        frame) and of the eye toggle (which only affects overlay visibility)."""
        if key == self._explore_active_rig_key:
            return
        self._explore_active_rig_key = key
        self._explore_refresh_cards()
        self._explore_refresh_report()

    def _explore_compute_report(self, r):
        """Compute the same framing/exposure/window/moon figures the
        Planner tab's results_txt shows (see _analyze_framing_impl), for
        one analyzed Explore rig against the currently loaded target.

        Returns a dict of computed values, or None if there's no target
        or camera data to compute from. r's own fov_w/fov_h/scale/eff_fl/
        f_ratio are reused as-is — only the exposure/window/moon figures
        (which need the target, the rig's filter_mode captured at analyze
        time, and the live global Bortle setting) are computed here.
        """
        t = self._explore_target
        c = self.data["cameras"].get(r["camera"])
        if t is None or c is None:
            return None

        t_maj = (t.get("size_maj") or 10.0) / 60.0
        t_min = (t.get("size_min") or t.get("size_maj") or 10.0) / 60.0
        scale = r["scale"]
        if scale < 0.67:
            status, tag = "Over-sampled", "warning"
        elif scale <= 2.0:
            status, tag = "Optimal", "optimal"
        else:
            status, tag = "Under-sampled", "warning"
        f_fits = (t_maj < r["fov_w"] and t_min < r["fov_h"])

        b_data = BORTLE_FACTORS.get(self.bortle_choice.get(),
                                    BORTLE_FACTORS.get("4 (Rural/Suburban)"))
        is_color = r.get("is_color", True)
        rf = 1.0 if is_color else self._rf_for_filter_mode(r.get("filter_mode") or "Mono Lum")
        ps = float(c.get("pixel_size", 1))
        sky_flux = (((b_data["color"] if is_color else b_data["mono"])
                    * (c.get("qe", 0.5) * (ps ** 2))) / (r["f_ratio"] ** 2)) / rf
        read_noise = c.get("read_noise", 1)
        exp = (C_VALUE * (float(read_noise) ** 2)) / (sky_flux + 1e-5)
        if sky_flux * exp > float(read_noise) ** 2 * 5:
            regime_text, regime_tag = "Sky-limited ✓", "amber"
        elif sky_flux * exp > float(read_noise) ** 2:
            regime_text, regime_tag = "Transitional", "highlight"
        else:
            regime_text, regime_tag = "Read-noise limited", "warning"

        moon_tag, _icon, _note, moon_impact, illum_pct, sep_deg = \
            self._moon_condition(t["ra_deg"], t["dec_deg"])

        win_start = win_end = win_hrs = peak_t = peak_a = transit_str = None
        min_alt = float(self.data.get("settings", {}).get("min_alt", 20))
        lat, lon = self._get_saved_location()
        if lat is not None:
            win_start, win_end, win_hrs, peak_t, peak_a, _ = _calc_best_imaging_window(
                t["ra_deg"], t["dec_deg"], lat, lon, min_alt=min_alt)
            try:
                transit_str = _calc_transit_time(t["ra_deg"], lon)
            except Exception:
                transit_str = None

        return dict(
            status=status, tag=tag, f_fits=f_fits, exp=exp, sky_flux=sky_flux,
            read_noise=read_noise, regime_text=regime_text, regime_tag=regime_tag,
            moon_tag=moon_tag, moon_impact=moon_impact, illum_pct=illum_pct, sep_deg=sep_deg,
            win_start=win_start, win_end=win_end, win_hrs=win_hrs, peak_t=peak_t, peak_a=peak_a,
            transit_str=transit_str, min_alt=min_alt,
        )

    def _explore_refresh_report(self):
        """Rebuild the rig-chip row and the analysis report body for
        whichever rig is currently active — the Explore tab's version of
        the Planner tab's results_txt, except it can show a report for any
        one of the up-to-6 analyzed rigs rather than always just one."""
        chip_frame = self._explore_chip_frame
        for w in chip_frame.winfo_children():
            w.destroy()

        txt = self._explore_report_txt
        txt.delete('1.0', tk.END)

        if not self._explore_rigs:
            txt.insert(tk.END, "Analyze a rig on the left to see its\n"
                                "framing, exposure, tonight's window,\n"
                                "and moon report here.", "dim")
            return

        active_key = self._explore_active_rig_key
        if active_key not in {r["key"] for r in self._explore_rigs}:
            active_key = self._explore_rigs[-1]["key"]
            self._explore_active_rig_key = active_key

        for r in self._explore_rigs:
            is_active = (r["key"] == active_key)
            chip = tk.Label(chip_frame, text=f"●  {r['scope']} · {r['camera']}",
                            bg="#0f2233" if is_active else "#1a2b3c",
                            fg=r["color"] if is_active else "#8899aa",
                            font=("Helvetica", 8, "bold" if is_active else "normal"),
                            padx=8, pady=3, cursor="hand2")
            chip.pack(side="left", padx=(0, 5), pady=(0, 6))
            chip.bind("<Button-1>", lambda e, k=r["key"]: self._explore_select_rig_for_report(k))

        r = next(rr for rr in self._explore_rigs if rr["key"] == active_key)
        rep = self._explore_compute_report(r)
        t = self._explore_target

        common = t['common'].split(';')[0].strip() if t and t.get('common') else ""
        txt.insert(tk.END, f"{r['scope']} · {r['camera']} · {r['reduction']}\n", "header")
        if t is not None:
            txt.insert(tk.END, f"{t['id']}{('  —  ' + common) if common else ''}\n", "dim")

        if rep is None:
            txt.insert(tk.END, "\nNo target loaded — search for one to see\n"
                                "this rig's framing, exposure and window report.", "dim")
            return

        txt.insert(tk.END, "\n━ FRAMING ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n", "sec_framing")
        txt.insert(tk.END, "  Image scale: ", "dim")
        txt.insert(tk.END, f"{r['scale']:.2f}\"/px ", "highlight")
        txt.insert(tk.END, f"({rep['status']})\n", rep['tag'])
        txt.insert(tk.END, "  Focal length: ", "dim")
        txt.insert(tk.END, f"{r['eff_fl']:.0f}mm (f/{r['f_ratio']:.1f})\n")
        txt.insert(tk.END, "  FOV: ", "dim")
        txt.insert(tk.END, f"{r['fov_w']:.2f}° × {r['fov_h']:.2f}°\n")
        txt.insert(tk.END, "  Framing: ", "dim")
        txt.insert(tk.END, "✅ Fits sensor\n" if rep['f_fits'] else "❌ Too big for sensor\n",
                  "optimal" if rep['f_fits'] else "warning")

        txt.insert(tk.END, "\n━ EXPOSURE ━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n", "sec_exposure")
        txt.insert(tk.END, "  Recommended sub: ", "dim")
        txt.insert(tk.END, f"{rep['exp']:.1f}s\n", "amber")
        txt.insert(tk.END, "  Sky flux: ", "dim")
        txt.insert(tk.END, f"{rep['sky_flux']:.1f} e⁻/px/s\n")
        txt.insert(tk.END, "  Read noise: ", "dim")
        txt.insert(tk.END, f"{rep['read_noise']} e⁻\n")
        txt.insert(tk.END, "  Regime: ", "dim")
        txt.insert(tk.END, f"{rep['regime_text']}\n", rep['regime_tag'])

        txt.insert(tk.END, "\n━ TONIGHT'S WINDOW ━━━━━━━━━━━━━━━━━━━━\n", "sec_window")
        if rep['win_start']:
            txt.insert(tk.END, "  Window: ", "dim")
            txt.insert(tk.END, f"{rep['win_start']} – {rep['win_end']}", "optimal")
            txt.insert(tk.END, f"  ({rep['win_hrs']}h)\n", "optimal")
            txt.insert(tk.END, "  Peak alt: ", "dim")
            txt.insert(tk.END, f"{rep['peak_a']}° at {rep['peak_t']}\n")
            if rep['transit_str']:
                txt.insert(tk.END, "  Transit: ", "dim")
                txt.insert(tk.END, f"{rep['transit_str']}\n")
        else:
            txt.insert(tk.END, "  ")
            txt.insert(tk.END,
                f"⚠️ Target below {int(rep['min_alt'])}° during all dark hours tonight\n",
                "warning")

        txt.insert(tk.END, "\n━ MOON ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n", "sec_moon")
        txt.insert(tk.END, "  Separation: ", "dim")
        txt.insert(tk.END, f"{rep['sep_deg']:.0f}°\n", rep['moon_tag'])
        txt.insert(tk.END, "  Illumination: ", "dim")
        txt.insert(tk.END, f"{rep['illum_pct']}%\n", rep['moon_tag'])
        txt.insert(tk.END, "  Impact: ", "dim")
        txt.insert(tk.END, f"{rep['moon_impact']}\n", rep['moon_tag'])

    def _kick_explore_paint(self):
        """Kick the finicky Explore widgets into painting (macOS Cocoa Tk)."""
        self._kick_paint([self.explore_canvas, self.explore_search, self._explore_report_txt])

    def open_sky_map(self):
        """Open the interactive sky map centred on the Planner tab's last
        analyzed target (self.current_target_info) — kept as a thin wrapper
        over _launch_sky_map for anything still reading that state; the
        Explore tab's own "Show Sky Map" button calls
        _explore_open_sky_map instead, which reads Explore's own target/
        rig/rotation rather than the Planner tab's.
        """
        t = self.current_target_info
        if not t:
            messagebox.showinfo(
                "No Target",
                "Select and analyze a target first, then open the sky map.")
            return
        self._launch_sky_map(
            t,
            getattr(self, "_skymap_fovw", 0.0) or 0.0,
            getattr(self, "_skymap_fovh", 0.0) or 0.0,
            self._screen_to_sky_pa(getattr(self, "_fov_angle", 0.0) or 0.0))

    def _explore_open_sky_map(self):
        """Explore tab's "Show Sky Map" button — centres the map on
        whatever Explore currently has loaded, framed by the active rig's
        FOV (the one shown in the report panel) rather than the Planner
        tab's own, separate, framing state. _explore_pa_deg is already a
        sky PA (see _explore_redraw's comment), so it's passed straight
        through with no _screen_to_sky_pa conversion needed."""
        t = self._explore_target
        if not t:
            messagebox.showinfo(
                "No Target",
                "Search for a target on the Explore tab first, then open the sky map.")
            return
        fovw = fovh = 0.0
        active_key = self._explore_active_rig_key
        for r in self._explore_rigs:
            if r["key"] == active_key:
                fovw, fovh = r["fov_w"], r["fov_h"]
                break
        self._launch_sky_map(t, fovw, fovh, self._explore_pa_deg)

    def _launch_sky_map(self, t, fovw_deg, fovh_deg, pa_deg):
        """Shared sky-map launcher — feeds the embedded d3-celestial
        explorer (a pywebview window in a separate process) a target
        centre, framed FOV, position angle and theme via a localhost URL.
        Coordinates — not the object id — are passed, so Sharpless/Caldwell
        targets resolve correctly. Falls back to the default browser when
        pywebview/WebView2 is unavailable.
        """
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
            "fovw":    f"{fovw_deg or 0.0:.4f}",
            "fovh":    f"{fovh_deg or 0.0:.4f}",
            "pa":      f"{pa_deg or 0.0:.1f}",
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
        """Redraw the schedule-timeline Gantt and refresh the browsing grid
        whenever Tonight's Plan tab becomes visible. Syncs the sidebar
        highlight to match the active tab."""
        try:
            current = self.tab_control.select()

            # Close the equipment drawer if we're navigating away from the
            # Plan tab — it re-parents the Planner tab's own chip widgets,
            # so leaving it open while switching tabs would leave those
            # widgets stuck off the Planner tab's chip bar.
            if current != str(self.tab_plan) and getattr(self, "_equip_drawer", None) is not None:
                self._close_equip_drawer()

            # Same for the unified filters popover — its .place() coordinates
            # are only meaningful while the Plan tab is the visible one.
            if current != str(self.tab_plan) and getattr(self, "_unified_filters_popup", None) is not None:
                self._close_unified_filters_popup()

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

            if current == str(self.tab_planner):
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
                self.root.after(30, self._refresh_visible_grid)
            elif current == str(self.tab_explore):
                # Redraw the comparison canvas (cheap — image is cached)
                # and kick the Cocoa Tk paint pipeline like other tabs.
                self.root.after(10, self._explore_redraw)
                self.root.after(50, self._kick_explore_paint)
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
    # PLAN COMMIT — add a target straight to Tonight's Plan (no queue)
    # ═══════════════════════════════════════════════════════════════════

    def _split_entry_into_plan(self, e):
        """Split one committed target into per-filter Tonight's Plan rows.

        ``e`` is a plain dict (target_id/common/ra_deg/dec_deg/scope/
        camera/bortle/filter_mode/exp_s/win_start/win_end/start_time/
        alloc_hrs/report_text/rotation_angle/framed_ra_deg/framed_dec_deg).
        A color camera or Mono Lum setup becomes one plan entry; a Filter
        Set splits into one entry per filter IN the set (its whole wheel —
        Ha/OIII/SII/L/R/G/B or whatever it holds), each getting its own
        slice of the total allocated hours. Per Jerry: a rig's filter
        selection is a whole Filter Set now, not one filter at a time, so
        "Add to Tonight" adds every filter the set contains in one click
        (matching how LRGB already batched before Filter Sets existed).
        This is the one place the filter-split math lives now — every
        "commit to tonight's plan" path (a browsing-grid card's Add to
        Tonight, the Frame dialog's Confirm Framing, and the Planner tab's
        own Add to Targets button) funnels through
        :meth:`_add_target_to_plan_direct` into here, instead of each
        duplicating it (as the old queue's move-to-plan functions used to).

        ``e["exp_s"]`` was computed by the caller for one blended rf — the
        WHOLE set's :meth:`_rf_for_filter_mode` value, e.g. narrowband-
        biased for any mixed LRGB+SHO wheel — so reusing it as-is for
        every row gave every LRGB channel a narrowband-length exposure
        (Jerry: "all the recommended exposure times are SHO times"). Since
        the sky-background formula scales exposure time linearly with rf,
        each filter's own correct exposure is that reference value
        re-scaled by (that filter's own rf ÷ the reference rf) — so a
        single-type set (or Mono Lum/color) is unaffected (its members'
        rf already equals the reference), and only a genuinely mixed set
        gets each filter its own, correctly-sized exposure.
        """
        cam      = self.data["cameras"].get(e["camera"], {})
        is_color = cam.get("is_color", True)
        fm       = e.get("filter_mode", "Mono Lum")
        fs       = None if (is_color or fm == "Mono Lum") else self.data.get("filter_sets", {}).get(fm)
        members  = (fs["members"] if (fs and fs.get("members"))
                    else [{"name": "", "type": None}])

        n_filters = len(members)
        alloc_hrs = e.get("alloc_hrs") or self._get_default_alloc_hrs()
        hrs_per_filt = alloc_hrs / max(n_filters, 1)
        ref_rf = 1.0 if is_color else self._rf_for_filter_mode(fm)

        for member in members:
            filt_name = member.get("name", "")
            if fs is None or not filt_name:
                exp_s_this = e["exp_s"]
            else:
                if member.get("type") == "narrowband" and fs.get("bandwidth_nm"):
                    member_rf = 348.0 / max(float(fs["bandwidth_nm"]), 0.1)
                else:
                    member_rf = 3.0   # lrgb member (or a defensively-typeless one)
                exp_s_this = e["exp_s"] * (member_rf / max(ref_rf, 0.001))

            n_subs      = int(hrs_per_filt * 3600.0 / max(exp_s_this, 0.1))
            int_filt_h  = round((n_subs * exp_s_this) / 3600.0, 2)
            filt_label  = filt_name if n_filters > 1 else (fm if not is_color else "")
            self._plan_entries.append({
                "target_id":     e["target_id"],
                "common":        e["common"],
                "ra_deg":        e.get("ra_deg", 0.0),
                "dec_deg":       e.get("dec_deg", 0.0),
                "scope":         e["scope"],
                "camera":        e["camera"],
                "filter":        filt_label,
                "filter_mode":   fm,
                "bortle":        e["bortle"],
                "exp_s":         round(exp_s_this, 1),
                "win_start":     e.get("win_start") or "",
                "win_end":       e.get("win_end") or "",
                "start_time":    e.get("start_time") or e.get("win_start") or "",
                "allocated_hrs": round(hrs_per_filt, 2),
                "n_subs":        n_subs,
                "total_int_hrs": int_filt_h,
                "added":         datetime.now().strftime("%Y-%m-%d %H:%M"),
                "report_text":   e.get("report_text", ""),
                "rotation_angle": e.get("rotation_angle", 0.0),
                "framed_ra_deg":  e.get("framed_ra_deg"),
                "framed_dec_deg": e.get("framed_dec_deg"),
            })
        return n_filters

    def _add_target_to_plan_direct(self, t, scope_name, cam_name, bortle_key,
                                    filter_mode, reduction, rotation_angle=0.0,
                                    framed_ra_deg=None, framed_dec_deg=None,
                                    report_text="", allow_update_framing=False):
        """Compute a target's exposure/window in the background and commit
        it straight to Tonight's Plan — no intermediate queue, no tab jump.

        Same background-thread math ``_add_to_queue`` used to run (sub-
        exposure recommendation + tonight's best imaging window), just
        landing in ``_plan_entries`` via :meth:`_split_entry_into_plan`
        the moment it's ready, instead of sitting in a queue that then
        required a manual "Add to Plan" click on the (now retired)
        Targets List tab.

        ``allow_update_framing`` distinguishes "I just dialed in a framing
        and want to commit it" (the Frame dialog's Confirm, and the
        Planner tab's own Add to Tonight, both of which pass real pan/
        rotation from interactive framing) from a plain, neutral "add to
        tonight" click (the browsing card's ✚, which always passes
        rotation_angle=0/framed_*=None). See the duplicate-handling below.
        """
        # Keyed on (target, scope, camera, filter_mode) — the same target on
        # the same rig legitimately gets added more than once: a single
        # mono camera's filter wheel commonly holds both an LRGB set and an
        # SHO (narrowband) set, and each is its own "Add to Tonight" with
        # its own filter_mode. What must never duplicate is re-adding the
        # exact same filter set on the exact same rig (e.g. two accidental
        # clicks, or forgetting LRGB was already added). Scope/camera alone
        # would also wrongly block that legitimate LRGB-then-SHO sequence
        # on one rig, so filter_mode has to be part of the key too.
        existing = [pe for pe in self._plan_entries
                    if pe["target_id"] == t["id"] and pe.get("scope") == scope_name
                    and pe.get("camera") == cam_name
                    and pe.get("filter_mode") == filter_mode]
        if existing:
            if allow_update_framing:
                # The target's already in tonight's plan (e.g. quick-added
                # via the card's ✚ with no framing yet) and you've now gone
                # and framed it properly — update the pointing/rotation on
                # its existing plan row(s) instead of erroring, since that's
                # clearly the intent, not an accidental double-add. A
                # multi-filter target (LRGB/narrowband) has one row per
                # filter, all sharing the same pointing, so every matching
                # row gets the update.
                for pe in existing:
                    pe["rotation_angle"] = rotation_angle
                    pe["framed_ra_deg"] = framed_ra_deg
                    pe["framed_dec_deg"] = framed_dec_deg
                    if report_text:
                        pe["report_text"] = report_text
                self._mark_grid_touched(t["id"])
                self._refresh_plan_tree()
                self._refresh_visible_grid()
                self._show_toast(f"{t['id']}'s framing updated")
            else:
                messagebox.showinfo(
                    "Already Planned",
                    f"{t['id']} on scope '{scope_name}' with camera '{cam_name}' "
                    f"already has a '{filter_mode}' entry in tonight's plan.\n\n"
                    "To add a different filter set (e.g. LRGB then SHO) for the "
                    "same rig, change the Filter Mode and add again. To add the "
                    "same filter set on a second rig, switch scope/camera first.")
            return

        lat, lon = self._get_saved_location()
        b_data = BORTLE_FACTORS.get(bortle_key, BORTLE_FACTORS.get("4 (Rural/Suburban)", {}))

        def _compute():
            s = self.data["scopes"].get(scope_name, {})
            c = self.data["cameras"].get(cam_name, {})
            exp_s_c = None
            if s and c:
                native_fl = float(s.get("native_fl", 1))
                ap        = float(s.get("aperture", 1))
                eff_fl    = native_fl * reduction
                eff_f     = eff_fl / max(ap, 1)
                ps        = float(c.get("pixel_size", 1))
                is_color  = c.get("is_color", True)
                rf        = 1.0 if is_color else self._rf_for_filter_mode(filter_mode)
                sky_flux  = (((b_data.get("color" if is_color else "mono", 1.0))
                               * c.get("qe", 0.5) * ps**2) / max(eff_f**2, 1e-6)) / max(rf, 1)
                exp_s_c   = (C_VALUE * c.get("read_noise", 1)**2) / (sky_flux + 1e-5)

            win_start = win_end = None
            win_hrs = 0.0
            if lat is not None and lon is not None:
                try:
                    _min_alt = float(self.data.get("settings", {}).get("min_alt", 20))
                    win_start, win_end, win_hrs, _, _, _ = _calc_best_imaging_window(
                        t["ra_deg"], t["dec_deg"], lat, lon, min_alt=_min_alt)
                    win_hrs = win_hrs or 0.0
                except Exception:
                    pass

            def _finish():
                if exp_s_c is None:
                    messagebox.showwarning("Incomplete",
                        f"{t['id']} could not be computed (missing scope/camera data).")
                    return
                if win_hrs < 0.5:
                    if win_hrs == 0.0:
                        detail = f"{t['id']} has no imaging window tonight."
                        title  = "No Imaging Window"
                    else:
                        win_min = int(round(win_hrs * 60))
                        detail  = (f"{t['id']} has an imaging window of only "
                                   f"{win_min} min tonight — less than 30 minutes.")
                        title   = "Short Imaging Window"
                    if not messagebox.askyesno(title,
                            f"{detail}\n\nAdd it to Tonight's Plan anyway?", icon="warning"):
                        return

                try:
                    default_alloc = float(self.data.get("settings", {}).get("default_alloc_hrs", "0") or 0)
                except (ValueError, TypeError):
                    default_alloc = 0.0
                alloc_hrs = (default_alloc if default_alloc > 0
                             else (win_hrs if win_hrs > 0 else self._get_default_alloc_hrs()))

                entry = {
                    "target_id":      t["id"],
                    "common":         t.get("common", "").split(";")[0].strip(),
                    "ra_deg":         t["ra_deg"],
                    "dec_deg":        t["dec_deg"],
                    "scope":          scope_name,
                    "camera":         cam_name,
                    "bortle":         bortle_key,
                    "filter_mode":    filter_mode,
                    "exp_s":          exp_s_c,
                    "win_start":      win_start or "",
                    "win_end":        win_end or "",
                    "start_time":     win_start or "",
                    "alloc_hrs":      alloc_hrs,
                    "report_text":    report_text,
                    "rotation_angle": rotation_angle,
                    "framed_ra_deg":  framed_ra_deg,
                    "framed_dec_deg": framed_dec_deg,
                }
                self._split_entry_into_plan(entry)
                self._refresh_plan_tree()
                self._show_toast(f"{t['id']} added to Tonight's Plan")
                # Re-render the browsing grid's cards now that the entry has
                # actually landed in _plan_entries, so the target's card
                # flips from "＋" to "✓" reliably. This has to happen HERE,
                # inside the async landing point, rather than right after
                # the call that kicked this off -- this whole add runs on a
                # background thread and lands via this root.after(0, ...),
                # so a rescan fired immediately after that call (the old
                # behavior) was a race that could easily finish first and
                # repopulate the grid from the still-stale _plan_entries,
                # leaving the card showing "＋" even though the target was
                # already in tonight's plan.
                self._refresh_visible_grid()

            self.root.after(0, _finish)

        threading.Thread(target=_compute, daemon=True).start()

    def _add_target_from_planner_search(self):
        """Planner tab's own "Add to Tonight" button.

        Resolves whatever's in the search box (independent of whether
        Analyze Target has been run) and commits it straight to
        Tonight's Plan using this tab's current equipment selection and
        any pan/rotation already set in the FOV preview — the same
        framing state the Frame dialog's Confirm uses.
        """
        raw = self.target_search.get().strip().upper().replace(" ", "")
        raw = self._normalize_catalog_key(raw)
        t = self.targets.get(raw) or self.common_names_map.get(raw)
        if not t:
            messagebox.showwarning("No Target", "Please enter a valid target to add.")
            return

        scope_name = self.scope_choice.get()
        cam_name   = self.camera_choice.get()
        if not scope_name or not cam_name:
            messagebox.showwarning("Incomplete", "Please select a Scope and Camera first.")
            return

        try:
            reduction = float(self.reduction_factor.get().rstrip("×x"))
        except ValueError:
            reduction = 1.0

        framed_ra, framed_dec = self._framed_center(t["ra_deg"], t["dec_deg"])
        rotation_angle = self._screen_to_sky_pa(getattr(self, "_fov_angle", 0.0) or 0.0)

        self._add_target_to_plan_direct(
            t, scope_name, cam_name, self.bortle_choice.get(),
            self.filter_mode.get(), reduction,
            rotation_angle=rotation_angle,
            framed_ra_deg=framed_ra, framed_dec_deg=framed_dec,
            report_text="", allow_update_framing=True)

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
        """Draw tonight's plan as a draggable schedule timeline.

        Lives in the Plan tab now (a full-width collapsible strip below
        both panes — see setup_plan_tab), not the retired Targets List
        tab, and draws from ``_plan_entries`` instead of the old queue:
        drag a bar to change that target's start time directly on the
        committed plan, rather than arranging candidates before a
        separate "move to plan" step.

        One row per rig (target_id + scope) rather than one row per
        ``_plan_entries`` row (i.e. per filter) — filters on the same rig
        always shoot sequentially, so a separate slider per filter just
        showed identical/overlapping bars for no reason. Each row's bar
        spans the combined allocated hours of every filter on that rig,
        and dragging it moves every one of those filter rows together
        (see ``_gantt_drag``). Bar color matches the rig's accent color
        from the plan card list (``_get_rig_accent_color``) instead of a
        separate fixed palette, so a rig looks the same in both places.
        """
        if not hasattr(self, "queue_gantt"):
            return
        canvas = self.queue_gantt
        if not canvas.winfo_ismapped():
            return   # strip is collapsed — nothing to draw
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

        if not self._plan_entries:
            canvas.create_text(cw // 2, ch // 2,
                text="Add targets to Tonight's Plan to see the schedule timeline",
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

        # ── Group plan entries by rig (target_id + scope) ─────────────────
        # Filters on the same rig shoot sequentially, so they share one row
        # and one draggable bar spanning their combined allocated hours,
        # instead of one (identical, overlapping) bar per filter.
        MAX_ALT = 90.0

        def _dim(hex_col, factor=0.25):
            """Mix hex_col toward the canvas bg (#050810) — avoids alpha hex."""
            r = int(int(hex_col[1:3], 16) * factor + 0x05 * (1 - factor))
            g = int(int(hex_col[3:5], 16) * factor + 0x08 * (1 - factor))
            b = int(int(hex_col[5:7], 16) * factor + 0x10 * (1 - factor))
            return f"#{r:02x}{g:02x}{b:02x}"

        groups = []
        group_idx_by_key = {}
        for member_idx, e in enumerate(self._plan_entries):
            # Camera is part of the grouping key — two cameras can share a
            # scope on the same target (e.g. LRGB on one camera, SHO on
            # another), and each needs its own Gantt bar, not one shared
            # between two unrelated imaging trains.
            key = (e["target_id"], e.get("scope", ""), e.get("camera", ""))
            gi = group_idx_by_key.get(key)
            if gi is None:
                gi = len(groups)
                group_idx_by_key[key] = gi
                groups.append({
                    "target_id":     e["target_id"],
                    "scope":         e.get("scope", ""),
                    "camera":        e.get("camera", ""),
                    "ra_deg":        e.get("ra_deg", 0.0),
                    "dec_deg":       e.get("dec_deg", 0.0),
                    "win_start":     e.get("win_start") or "",
                    "win_end":       e.get("win_end") or "",
                    "start_time":    e.get("start_time") or e.get("win_start") or "",
                    "total_alloc_h": 0.0,
                    "member_idxs":   [],
                })
            g = groups[gi]
            g["total_alloc_h"] += e.get("allocated_hrs") or 0.0
            g["member_idxs"].append(member_idx)

        # Saved for _gantt_drag, which needs to move every filter row on a
        # rig together when its bar is dragged.
        self._gantt_groups = groups

        max_rows = min(len(groups), 8)
        row_h    = ph / max(max_rows, 1)
        self._gantt_row_info   = []   # (y_top, y_bot, group_idx, alloc_x1, alloc_x2, start_h_norm)
        self._gantt_drag_bounds = {}

        for i, g in enumerate(groups[:max_rows]):
            color    = self._get_rig_accent_color(g["scope"], g["camera"])
            dim_col  = _dim(color)
            y_top    = tm + i * row_h + 1
            y_bot    = tm + (i + 1) * row_h - 1
            y_mid    = (y_top + y_bot) / 2
            alt_base = y_bot - 2
            alt_span = row_h * 0.82
            is_drag  = (i == self._gantt_drag_idx)

            # Target label — always include scope so users can tell apart entries at a glance.
            scope_abbr = (g.get("scope") or "")
            if scope_abbr:
                row_label  = f"{g['target_id']}\n{scope_abbr[:12]}"
                label_font = ("Helvetica", 8, "bold")
            else:
                row_label  = g["target_id"]
                label_font = ("Helvetica", 10, "bold")
            canvas.create_text(lm - 4, y_mid, text=row_label,
                               fill=fg_main, font=label_font, anchor="e")

            if not g.get("win_start"):
                canvas.create_rectangle(lm, y_top, cw - rm, y_bot,
                                        fill="#111820", outline=fg_dim, dash=(3, 3))
                canvas.create_text((lm + cw - rm) // 2, y_mid,
                                   text="not visible tonight", fill=fg_dim, font=("Helvetica", 9))
                self._gantt_row_info.append((y_top, y_bot, i, lm, lm, dark_start_h))
                continue

            ws = self._parse_h(g.get("win_start"))
            we = self._parse_h(g.get("win_end"))
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
            ra_deg  = g.get("ra_deg", 0)
            dec_deg = g.get("dec_deg", 0)
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

            # ── Foreground: draggable allocated bar (whole rig, all filters) ──
            start_str = g.get("start_time") or g.get("win_start") or ""
            start_h   = self._parse_h(start_str)
            if start_h is None:
                start_h = ws if ws is not None else dark_start_h
            if start_h < ax_start - 0.01:
                start_h += 24.0

            alloc_h = g.get("total_alloc_h") or 1.0

            # Clip bar so it never extends past the imaging window end (we).
            # If the bar would overflow, scale every filter row on this rig
            # down proportionally (recomputing n_subs/total_int_hrs to
            # match, since those are what the plan card actually displays)
            # and notify the user. If less than 30 min remain, show a
            # message and skip the bar.
            effective_h = min(start_h + alloc_h, we) - start_h
            if effective_h < 0.5:
                canvas.create_text((lm + cw - rm) // 2, y_mid,
                    text=f"{g['target_id']}: less than 30 min in imaging window",
                    fill=fg_dim, font=("Helvetica", 9))
                self._gantt_row_info.append((y_top, y_bot, i, lm, lm, dark_start_h))
                self._gantt_drag_bounds[i] = (ws, we)
                continue
            if effective_h < alloc_h:
                scale = (effective_h / alloc_h) if alloc_h > 0 else 0.0
                for member_idx in g["member_idxs"]:
                    if member_idx >= len(self._plan_entries):
                        continue
                    me = self._plan_entries[member_idx]
                    new_h = round((me.get("allocated_hrs") or 0.0) * scale, 2)
                    me["allocated_hrs"] = new_h
                    exp_s = me.get("exp_s") or 1.0
                    new_subs = int(round(new_h * 3600.0 / exp_s))
                    me["n_subs"] = new_subs
                    me["total_int_hrs"] = round((new_subs * exp_s) / 3600.0, 2)
                g["total_alloc_h"] = effective_h
                alloc_h = effective_h
                self.root.after(0, lambda tid=g["target_id"], ah=round(effective_h, 2):
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
                    scope_s = (g.get("scope") or "")[:8]
                    bar_label = (f"{g['target_id']} ({scope_s})" if scope_s
                                 else g["target_id"])
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

            # Transit tick inside available-window bar (plan entries don't
            # store transit_time, so compute it on the fly)
            try:
                tr = self._parse_h(_calc_transit_time(g["ra_deg"], lon))
            except Exception:
                tr = None
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
        """Return (group_idx, start_h_norm) if (x,y) hits a draggable alloc bar.

        group_idx indexes ``self._gantt_groups`` (one entry per rig), not
        ``self._plan_entries`` directly — see ``_draw_queue_gantt``.
        """
        for y_top, y_bot, group_idx, bar_x1, bar_x2, start_h_norm in self._gantt_row_info:
            if y_top <= y <= y_bot and bar_x1 - 12 <= x <= bar_x2 + 12:
                return group_idx, start_h_norm
        return None, None

    def _gantt_press(self, event):
        """Begin a Gantt-bar drag if the mouse hit a draggable bar."""
        idx, start_h = self._gantt_hit(event.x, event.y)
        if idx is not None:
            self._gantt_drag_idx      = idx
            self._gantt_drag_x0       = event.x
            self._gantt_drag_start_h0 = start_h

    def _gantt_drag(self, event):
        """Update every filter row on the dragged rig's start_time together.

        The Gantt now draws one bar per rig (target_id + scope), so a drag
        moves every ``_plan_entries`` row that rig's group covers, not just
        one — otherwise filters on the same rig would drift apart.
        """
        if self._gantt_drag_idx is None or not self._gantt_pw:
            return
        dx    = event.x - self._gantt_drag_x0
        dh    = dx / self._gantt_pw * self._gantt_ax_range
        new_h = self._gantt_drag_start_h0 + dh

        idx = self._gantt_drag_idx
        groups = getattr(self, "_gantt_groups", [])
        if idx >= len(groups):
            return
        g = groups[idx]

        # Clamp within the available window
        ws = self._parse_h(g.get("win_start"))
        we = self._parse_h(g.get("win_end"))
        if ws is not None and ws < self._gantt_ax_start - 0.01:
            ws += 24.0
        if we is not None and we < ws:
            we += 24.0
        alloc_h = g.get("total_alloc_h") or 1.0
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
        new_start = f"{h_int:02d}:{m_int:02d}"
        for member_idx in g.get("member_idxs", []):
            if member_idx < len(self._plan_entries):
                self._plan_entries[member_idx]["start_time"] = new_start
        self._draw_queue_gantt()

    def _gantt_release(self, event):
        """End the Gantt-bar drag and clear drag state."""
        if self._gantt_drag_idx is not None:
            self._gantt_drag_idx      = None
            self._gantt_drag_x0       = None
            self._gantt_drag_start_h0 = None
            # Plan cards show start_time too — refresh once, on release,
            # rather than on every drag motion tick.
            self._refresh_plan_tree()

    def _gantt_motion(self, event):
        """Show a move cursor when hovering over a draggable Gantt bar."""
        idx, _ = self._gantt_hit(event.x, event.y)
        self.queue_gantt.config(cursor="fleur" if idx is not None else "")

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
        # transient() + an explicit grab_release() before destroy() (see
        # _close() below) — every other modal dialog in this file sets
        # transient(), this one didn't, and skipping it is a known way for
        # a Toplevel's grab to end up in a bad state once a *second* modal
        # (the sync-across-filters messagebox below) is opened and closed
        # on top of it. That's the likely cause of the app becoming
        # unresponsive after a second edit-dialog round-trip.
        dlg.transient(self.root)
        dlg.grab_set()

        def _close():
            dlg.grab_release()
            dlg.destroy()

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

        # Position angle used to have its own editable field here, but it's
        # captured by framing (Target Planner) already and isn't something
        # meant to be hand-typed per plan entry — removed per Jerry's request.
        # (Whatever rotation_angle the entry already has, from framing, is
        # left untouched — _apply() below no longer writes to it.)

        def _apply():
            try:
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
                old_exp   = e.get("exp_s")
                old_alloc = e.get("allocated_hrs")
                new_exp_r   = round(new_exp, 1)
                new_hrs_r   = round(new_hrs, 2)
                exp_changed   = old_exp is None or abs(new_exp_r - round(float(old_exp), 1)) > 1e-9
                alloc_changed = old_alloc is None or abs(new_hrs_r - round(float(old_alloc), 2)) > 1e-9
                e["allocated_hrs"] = new_hrs_r
                e["exp_s"]         = new_exp_r
                e["n_subs"]        = new_subs
                e["total_int_hrs"] = new_int_h
                e["start_time"]    = new_st

                # LRGB/narrowband targets split into one plan entry per
                # filter (see _split_entry_into_plan) — allocated time and
                # sub-exposure length are each normally kept the same
                # across every filter WITHIN ONE FILTER SET, so offer to
                # sync whichever one just changed rather than leaving the
                # other filters on their old values. Restricted to
                # siblings sharing this entry's filter_mode: a rig can
                # carry more than one filter set now (e.g. an LRGB set and
                # a separate SHO set on the same mono camera), and editing
                # an LRGB filter should never offer to also touch the SHO
                # filters (or vice versa) — they're different sessions with
                # their own timing even though they're on the same rig.
                fm = e.get("filter_mode")

                if alloc_changed:
                    siblings = [s for s in self._plan_entries
                                if s is not e and s["target_id"] == e["target_id"]
                                and s.get("scope", "") == e.get("scope", "")
                                and s.get("camera", "") == e.get("camera", "")
                                and s.get("filter_mode") == fm
                                and round(float(s.get("allocated_hrs", 0.0) or 0.0), 2) != new_hrs_r]
                    if siblings and messagebox.askyesno(
                            "Match Allocated Time Across Filters",
                            f"Apply {new_hrs_r:.2f}h allocated time to the other "
                            f"{len(siblings)} filter(s) on {e['target_id']} too?\n\n"
                            f"Each filter keeps its own sub-exposure length — only "
                            f"the allocated hours (and the sub count/integration "
                            f"time it implies) changes.",
                            parent=dlg):
                        for s in siblings:
                            s["allocated_hrs"] = new_hrs_r
                            s_exp = float(s.get("exp_s", 0.0) or 0.0) or new_exp_r
                            s_subs = int(new_hrs_r * 3600.0 / s_exp)
                            s["n_subs"] = s_subs
                            s["total_int_hrs"] = round((s_subs * s_exp) / 3600.0, 2)

                if exp_changed:
                    siblings = [s for s in self._plan_entries
                                if s is not e and s["target_id"] == e["target_id"]
                                and s.get("scope", "") == e.get("scope", "")
                                and s.get("camera", "") == e.get("camera", "")
                                and s.get("filter_mode") == fm
                                and round(float(s.get("exp_s", 0.0) or 0.0), 1) != new_exp_r]
                    if siblings and messagebox.askyesno(
                            "Match Sub-Exposure Across Filters",
                            f"Apply {new_exp_r:.1f}s sub-exposures to the other "
                            f"{len(siblings)} filter(s) on {e['target_id']} too?\n\n"
                            f"Each filter keeps its own allocated hours — only "
                            f"the sub-exposure length changes.",
                            parent=dlg):
                        for s in siblings:
                            s["exp_s"] = new_exp_r
                            s_subs = int((s.get("allocated_hrs", 0.0) or 0.0) * 3600.0 / new_exp_r)
                            s["n_subs"] = s_subs
                            s["total_int_hrs"] = round((s_subs * new_exp_r) / 3600.0, 2)

                _close()
                self._refresh_plan_tree()
            except Exception as exc:
                # Never let an unhandled exception unwind out of a Tk
                # callback while this dialog's grab (and the sync
                # messagebox's nested grab) are still being torn down —
                # surface it instead of leaving the dialog half-closed.
                try:
                    _close()
                except Exception:
                    pass
                messagebox.showerror("Update Failed",
                    f"Could not apply the change.\n\nDetail: {exc}")

        btn_row = ttk.Frame(dlg)
        btn_row.grid(row=5, column=0, columnspan=2, padx=16, pady=(10, 14))
        ttk.Button(btn_row, text="OK", command=_apply).pack(side="left", padx=(0, 8))
        ttk.Button(btn_row, text="Cancel", command=_close).pack(side="left")
        dlg.bind("<Return>", lambda ev: _apply())
        dlg.bind("<Escape>", lambda ev: _close())
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

    def _update_floating_suggestions(self, entry=None, on_select=None):
        """Build and show/hide the floating suggestion popup below the search entry.

        Respects self.catalog_filter — targets whose `catalogs` set has no
        overlap with the user's enabled catalogs are excluded from results.

        When ``entry`` is the Plan tab's search box, also respects that
        tab's own Type dropdown (``self._grid_type_var`` — Galaxy, Nebula,
        etc.), so the suggestion popup matches what the browsing grid below
        it would show for the same object type. That dropdown has no
        meaning for the other search entries this popup is reused for (the
        legacy Planner search, the Explore tab's search), so it's only
        applied for the Plan tab's own box.

        ``entry`` / ``on_select`` generalise the popup for other search
        fields (the Explore tab reuses it); both default to the Planner's
        search entry and select handler.
        """
        entry = entry if entry is not None else self.target_search
        typed = entry.get().upper().replace(" ", "")
        if not typed:
            self._hide_suggestions()
            return
        if typed.startswith('M') and typed[1:].isdigit():
            typed = f"M{int(typed[1:])}"

        enabled = {c for c, on in self.catalog_filter.items() if on}

        grid_obj_type = None
        if entry is getattr(self, "plan_search", None) and hasattr(self, "_grid_type_var"):
            grid_obj_type = self._grid_type_var.get()
            if grid_obj_type == "All Types":
                grid_obj_type = None

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
                    # Plan tab's Type dropdown, when set to something other
                    # than "All Types" — same field the grid itself filters on.
                    if grid_obj_type and t.get("obj_type") != grid_obj_type:
                        continue
                    display = f"{t['id']}  —  {t['common'].split(';')[0].strip()}" if t['common'] else t['id']
                    matches.append((display, t))
            if len(matches) >= 12:
                break

        if not matches:
            self._hide_suggestions()
            return

        self._show_suggestion_popup(matches, entry=entry, on_select=on_select)

    def _show_suggestion_popup(self, matches, entry=None, on_select=None):
        """Show or update the floating suggestion Toplevel below the search entry."""
        entry = entry if entry is not None else self.target_search
        on_select = on_select if on_select is not None else self._select_suggestion
        # Destroy old popup if it exists
        if self._suggestion_popup and self._suggestion_popup.winfo_exists():
            self._suggestion_popup.destroy()

        popup = tk.Toplevel(self.root)
        popup.wm_overrideredirect(True)
        popup.wm_attributes("-topmost", True)
        self._suggestion_popup = popup

        # Position below the search entry
        entry.update_idletasks()
        x = entry.winfo_rootx()
        y = entry.winfo_rooty() + entry.winfo_height()
        # Extra width: ~80px for type badge + magnitude, plus ~100px for the
        # catalog badges that can appear (Messier/Caldwell/NGC/IC/Sharpless)
        w = entry.winfo_width() + 180

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
                widget.bind("<Button-1>", lambda e, tid=target_id: on_select(tid))
            if mag_str:
                mag_lbl.bind("<Button-1>", lambda e, tid=target_id: on_select(tid))

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
    # CATALOG FILTER — state + persistence only. The checkbox UI itself now
    # lives inside the Plan tab's consolidated "⚙ Filters" popover (see
    # _show_unified_filters_popup) rather than a standalone popup of its
    # own — this section used to also own that popup's build/show/close
    # functions before the "Option C" consolidation folded them in there.
    # ─────────────────────────────────────────────────────────────────────
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
        # Refresh button label and whichever search box has a live suggestion
        # popup open right now.
        self._update_unified_filters_btn_label()
        if self.target_search.get().strip():
            self._update_floating_suggestions()
        if hasattr(self, "plan_search") and self.plan_search.get().strip():
            self._update_floating_suggestions(entry=self.plan_search,
                                               on_select=self._plan_select_suggestion)
        self._refresh_visible_grid()

    def _set_all_catalog_filters(self, value):
        """Set every catalog filter to value (True for All, False for None).

        Backs the "All" / "None" mini-buttons in the filter popup's
        Catalogs section. Updates both the live BooleanVars (so the
        checkboxes redraw) and self.catalog_filter, then runs the same
        persist + refresh path as a single-checkbox toggle.
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
        self._update_unified_filters_btn_label()
        if self.target_search.get().strip():
            self._update_floating_suggestions()
        if hasattr(self, "plan_search") and self.plan_search.get().strip():
            self._update_floating_suggestions(entry=self.plan_search,
                                               on_select=self._plan_select_suggestion)
        self._refresh_visible_grid()

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
        # Rig Library panel's ★ marker can go stale if the active rig changed
        # via the Plan tab's equipment drawer/chip (which updates the chip
        # label but not this panel) — resync it whenever this tab redraws.
        if hasattr(self, "_equip_rig_refresh"):
            self._equip_rig_refresh(preserve_name=self.data.get("settings", {}).get("active_rig", ""))
        # Filter Library panel — same resync-on-redraw reasoning as the Rig
        # Library line above (e.g. a NINA import can add filters while this
        # tab isn't the active one).
        if hasattr(self, "_equip_filter_refresh"):
            self._equip_filter_refresh()

    def refresh_dropdowns(self):
        """Repopulate the scope/camera/filter Combobox values and restore last-session choices."""
        self.scope_dropdown['values'] = sorted(list(self.data["scopes"].keys()))
        self.camera_dropdown['values'] = sorted(list(self.data["cameras"].keys()))

        # Equipment drawer (Phase 3) has its own scope/camera comboboxes
        # bound to the same StringVars — keep their option lists in sync
        # too, same pattern as the Explore tab dropdowns just below.
        if getattr(self, "_drawer_scope_dd", None) is not None and self._drawer_scope_dd.winfo_exists():
            self._drawer_scope_dd['values'] = sorted(list(self.data["scopes"].keys()))
        if getattr(self, "_drawer_camera_dd", None) is not None and self._drawer_camera_dd.winfo_exists():
            self._drawer_camera_dd['values'] = sorted(list(self.data["cameras"].keys()))

        # Explore tab shares the same inventory — keep its dropdowns in sync
        # and seed sensible defaults from the last session when unset.
        if hasattr(self, "explore_scope_dropdown"):
            self.explore_scope_dropdown['values'] = sorted(list(self.data["scopes"].keys()))
            self.explore_camera_dropdown['values'] = sorted(list(self.data["cameras"].keys()))
            _sess = self.data.get("session", {})
            if (not self.explore_scope_var.get()
                    or self.explore_scope_var.get() not in self.data["scopes"]):
                if _sess.get("scope") in self.data["scopes"]:
                    self.explore_scope_var.set(_sess["scope"])
                else:
                    self.explore_scope_var.set("")
            if (not self.explore_camera_var.get()
                    or self.explore_camera_var.get() not in self.data["cameras"]):
                if _sess.get("camera") in self.data["cameras"]:
                    self.explore_camera_var.set(_sess["camera"])
                else:
                    self.explore_camera_var.set("")

        # Rebuild filter dropdown from the Filter Library (self.data["filter_sets"])
        # — Mono Lum plus every user-managed Filter Set name.
        _filter_choices = self._filter_mode_choices()
        self.filter_dropdown["values"] = _filter_choices
        if hasattr(self, "explore_filter_dropdown"):
            self.explore_filter_dropdown["values"] = _filter_choices
        # Equipment tab's inline Filter Library panel mirrors the same data
        if hasattr(self, "_equip_filter_refresh"):
            self._equip_filter_refresh()

        # Restore equipment chips -- ONCE, on the first call at startup.
        #
        # Two sources exist: the named "active rig" (settings.active_rig,
        # kept up to date whenever a saved rig is picked from the rig
        # dropdown) and a legacy per-field "session" snapshot that used to
        # be written by the old Planner tab's analyze step. That tab is
        # retired from navigation and its analysis path never runs anymore,
        # so "session" is frozen wherever it last was and no longer reflects
        # the rig actually last used -- it's kept only as a fallback for
        # installs that predate the rig-preset feature. Preferring
        # active_rig here is what makes the chips (and therefore the rig
        # dropdown's name-vs-"Custom…" indicator) agree at startup.
        #
        # refresh_dropdowns() also runs mid-session, any time equipment is
        # added or edited (via save_data()) -- restoring again there would
        # overwrite whatever rig/chips the user has active *right now* with
        # this stale snapshot, so the whole block is guarded to run only once.
        if not self._equipment_chips_restored:
            sess = self.data.get("session", {})
            active_name = self.data.get("settings", {}).get("active_rig", "")
            active_rig = self._find_rig(active_name) if active_name else None

            if active_rig and active_rig.get("scope") in self.data["scopes"]:
                self.scope_choice.set(active_rig["scope"])
            elif sess.get("scope") in self.data["scopes"]:
                self.scope_choice.set(sess["scope"])

            if active_rig and active_rig.get("camera") in self.data["cameras"]:
                self.camera_choice.set(active_rig["camera"])
            elif sess.get("camera") in self.data["cameras"]:
                self.camera_choice.set(sess["camera"])

            # Bortle isn't part of a rig anymore (it's a decoupled, global
            # "sky conditions" setting) — restore it from session state only.
            if sess.get("bortle") in BORTLE_FACTORS:
                self.bortle_choice.set(sess["bortle"])

            saved_red = (active_rig or {}).get("reduction") or sess.get("reduction")
            if saved_red:
                # Migrate old plain-numeric saves (e.g. "1.0") to the new "×" format
                if not saved_red.endswith("×"):
                    try:
                        val = float(saved_red)
                        _red_map = {0.63: "0.63×", 0.67: "0.67×", 0.70: "0.70×", 0.75: "0.75×",
                                    0.80: "0.80×", 1.0: "1.0×", 1.5: "1.5×", 2.0: "2.0×",
                                    2.5: "2.5×", 3.0: "3.0×"}
                        saved_red = _red_map.get(round(val, 2), "1.0×")
                    except ValueError:
                        saved_red = "1.0×"
                self.reduction_factor.set(saved_red)

            if sess.get("target"):
                self.target_search.delete(0, tk.END)
                self.target_search.insert(0, sess["target"])

            saved_fm = (active_rig or {}).get("filter") or sess.get("filter_mode")
            if saved_fm in self.filter_dropdown["values"]:
                self.filter_mode.set(saved_fm)

            self._equipment_chips_restored = True

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
