Lightbucket Astro Planner — Wide-field Sky Map (Phase 1 harness)
================================================================

What this is
------------
A standalone, fully-offline d3-celestial explorer in the Option B layout
(full-bleed chart + floating glass controls). This is the visual harness
only — no pywebview, no app integration yet (those are Phases 2 and 3).
It runs in a plain browser so you can eyeball it against the sketch and
fire back changes.

How to run it
-------------
d3-celestial loads its data files (stars, constellations, Milky Way,
Messier) over the network/file layer, and Chrome blocks that over file://.
So serve the folder, don't double-click the HTML:

    cd skymap
    python3 -m http.server 8000

then open:  http://localhost:8000/skymap.html

(Firefox can usually open skymap.html directly via file://, but the local
server is the reliable path — and it mirrors how the app will serve the
bundle through pywebview's built-in server later.)

Trying different targets
------------------------
Everything is driven by URL params, which is exactly the contract the app
will use to launch it. With no params it shows Sh2-155 (the Cave Nebula).
Examples:

  M31 (Andromeda):
  http://localhost:8000/skymap.html?ra=10.68&dec=41.27&name=M31&common=Andromeda%20Galaxy&catalog=Messier&fovw=2.2&fovh=1.5&pa=0

  C14 (Double Cluster):
  http://localhost:8000/skymap.html?ra=34.75&dec=57.13&name=C14&common=Double%20Cluster&catalog=Caldwell&fovw=1.4&fovh=0.9&pa=20

  Night mode:  add  &theme=night

Param contract:
  ra, dec   target in decimal degrees (J2000)   [required in app]
  name      catalog id shown in titlebar (e.g. Sh2-155)
  common    common name (e.g. Cave Nebula)
  catalog   badge text (Sharpless / Caldwell / Messier / NGC / IC)
  fovw,fovh sensor field of view in degrees -> the dashed frame
  pa        position angle in degrees -> frame rotation
  theme     day | night

Coordinate grid (toggle in the top-left panel): on shows the equatorial
graticule (RA in hours / Dec in degrees); off hides it.

What's wired
------------
- Stars to mag 6 + Messier (the locked "lean + Messier" bundle), constellation
  lines/names, RA/Dec grid, Milky Way band — all toggle live (top-left).
- Magnitude slider 3.0–6.0 (capped at 6 because that's the bundled star file).
- Field zoom +/- (top-right) with an approximate field readout.
- Reticle + dashed sensor frame + catalog callout drawn over the target.
- Recenter button; day/night theme (the ◐ button is a test-only flip).

Worth a look when you test
--------------------------
- Does the dashed frame sit correctly and rotate as expected for a few PAs?
- Field readout is an approximation from the projection scale — sanity-check
  the number feels right; we can refine the formula.
- Callout chip tracks the target on pan/zoom (it hides when off-screen).
- Resize redraws after a short pause (canvas needs a reload — by design).

Deferred (not in this phase)
----------------------------
- pywebview window + launching from "Show Sky Map" (Phase 2)
- one-way feed of live center/PA from the in-app FOV window (Phase 2)
- PyInstaller .spec + Inno Setup / WebView2 packaging (Phase 3)
- download Mid/Full catalogs from Settings (Phase 4)

Vendored under vendor/ : d3-celestial by Olaf Frohn, BSD-3-Clause.
