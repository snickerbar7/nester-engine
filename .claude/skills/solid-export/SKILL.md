---
name: solid-export
description: Export solid CAD files via OpenCASCADE (.venv-cad). Two modes — (1) --shop-package, the per-part IGES + manifest a 3D tube-laser shop actually wants (the shop nests them itself); (2) --solids, a nested solid preview per bar for eyeballing. Fire after /nest when the user wants the shop deliverable or a 3D solid view, not the wireframe.
---

# Export solid CAD files

**Key principle (researched):** a 3D tube laser wants **one solid file per unique
part** (holes/copes/end-cuts modeled) and **nests them itself** on its machine.
Pre-nested files are non-standard for 3D tube (only 2D sheet/DXF gets pre-nested).
So the real shop deliverable is the **shop package**, not the nested bars.

## Shop package (the deliverable) — `--shop-package`

```bash
.venv-cad/bin/python solid_nest.py --shop-package --src "<parts dir>" \
  --out output/<job>/<job>          # -> output/<job>/shop_package/
```

One clean B-rep solid IGES per unique part (solids only — stray construction
curves stripped) + `manifest.csv` (part · profile · length · qty · material).
`--material` sets the manifest material string. This shop uses **IGES, not STEP**.

## Nested solid preview (optional) — `--solids`

`solid_nest.py` (no `--shop-package`) takes each part's REAL solid and places it
at its nested position, preserving every feature — for eyeballing, not the shop.

## Prereqs

Needs OpenCASCADE in a Python-3.13 venv (the main tool is 3.9 and can't host it):

```bash
cd ~/Documents/Nester
[ -d .venv-cad ] || /opt/homebrew/bin/python3.13 -m venv .venv-cad
.venv-cad/bin/python -c "import OCP" 2>/dev/null || .venv-cad/bin/pip install cadquery-ocp
```

And a completed `/nest` run (you need its `<job>_corte.json`).

## Run

```bash
.venv-cad/bin/python solid_nest.py \
  --nest output/<job>/<job>_corte.json \
  --src "<dir of the original part IGES files>" \
  --out output/<job>/<job>
```

- Default: **one file per stock bar** → `output/<job>/bars/<job>_<profile>_bar<n>.step`
  and `.igs`. Each bar is a 6 m tube at the origin, pieces in a line.
- `--combined`: a single file with all bars stacked in Y.
- `--no-step` / `--no-iges` to emit just one format.

## Verify before handing over (you can't open CAD — check numerically)

Read the output back with OCP and confirm:
- solid count == piece count for that bar,
- `free_edges == 0` (no stray construction curves imported as sketches),
- bbox: X ≈ used bar length, Y/Z == the profile's cross-section, and for a
  **rectangular** profile Y and Z are the two *different* dims (not both equal —
  that means inconsistent rotation; `place()` normalizes wider side to Y).

Then SendUserFile the per-bar files (lead with the format the shop wants — IGES
unless they prefer STEP) and tell the user to open one in Fusion to confirm.

## Notes / gotchas

- Source filenames may be NFD on macOS (decomposed ñ) — matching is NFC-normalized.
- A part's long axis may be modeled along X, Y, or Z; `place()` detects and
  rotates it to +X.
- IGES is **B-rep solids (type 186) + BuildCurves3d** by default — verified in
  Fusion to import as clean SOLID bodies (one per piece). 186 WITHOUT
  BuildCurves3d fragments (the old "L-shape" bug); always build 3D curves first.
  `--iges-surfaces` falls back to trimmed surfaces (144 — imports as many separate
  surface bodies, geometry fine but not solids). STEP is also a watertight solid.
