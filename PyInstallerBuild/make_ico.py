"""One-off utility: convert logo.png → logo.ico with multiple resolutions.

Usage:
    python make_ico.py

Run from the directory containing logo.png.  Produces logo.ico next to it.

Why multiple sizes?
    Windows picks the best-matching resolution from the .ico file for each
    context — 16×16 for the title bar, 32×32 for the taskbar, 48×48 for
    Explorer's medium icons, 256×256 for Explorer's extra-large thumbnails.
    Embedding all sizes in one .ico avoids blurry scaling.
"""

from PIL import Image
from pathlib import Path

SRC = Path("logo.png")
DST = Path("logo.ico")

# Windows icon file format supports sizes up to 256×256.  Listing the full
# standard set here; Pillow will downscale from the source PNG for each.
SIZES = [(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]

if not SRC.exists():
    raise SystemExit(f"Source file not found: {SRC.resolve()}")

img = Image.open(SRC)

# Pillow writes .ico best from RGBA.  Convert up front so transparency is
# preserved in every embedded size.
if img.mode != "RGBA":
    img = img.convert("RGBA")

img.save(DST, format="ICO", sizes=SIZES)
print(f"Wrote {DST.resolve()} with sizes {SIZES}")
