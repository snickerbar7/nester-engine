---
name: nest-flat
description: Run a 2D flat-sheet nesting job for laser/plasma/waterjet-cut parts. Fire when the user wants to nest flat DXF parts onto stock sheets, work out how many sheets they need, or generate a sheet cut-plan from DXF files (as opposed to /nest, which is 1D straight tubes from IGES). Takes DXF files/dir + sheet size + material/thickness + spacing, produces output/<job>/<job>_Plan_de_Corte.pdf + <job>_nido.json + nested DXF per sheet, and returns a summary.
---

# Run a flat-sheet nesting job

You are the UI layer for `nester.sheet` — the 2D sibling of the tube tool. The
user gives you flat **DXF** parts and their sheet/material parameters; you nest
them onto rectangular stock sheets and hand back a summary plus written
artifacts. This is for **flat sheet-metal / plate parts**, not tubes — for
straight tubes cut from bars, use `/nest` instead.

The heavy nesting runs on the `spyrrow` engine (sparrow/jagua-rs) inside the
main `.venv` (Python 3.13). No separate CAD env is needed.

## 1. Gather inputs

You need:
- **Input path(s)** — files, a glob, or a directory of `.dxf` files. Parts are
  read from the `OUTER_PROFILES` layer, holes from `INTERIOR_PROFILES`; bend
  lines (`BEND`, `BEND_EXTENT`) are ignored. This is the Fusion 360 sheet-metal
  flat-pattern convention.
- **Sheet size (mm)** — `WxH`, e.g. `2440x1220` (4×8 ft, the default). Ask if the
  user's stock differs.
- **Material + thickness** — labels the job and (later) the stock group. Optional
  but good on the report.
- **Spacing**: `--margin` (edge, default 8mm) and `--gap` (part-to-part, default
  3mm — keep ≥ kerf).
- **Rotation policy** — `free` (default, best yield), `grain` (0°/180° only, for
  brushed finish or bend-grain), `fixed` (no rotation), `ortho` (0/90/180/270).

Ask once for anything missing that matters, then proceed. Reuse params given
earlier in the conversation.

## 2. Run

```bash
cd ~/Documents/Nester
.venv/bin/python -m nester.sheet <inputs> \
  --sheet 2440x1220 --material acero --thickness 2 \
  --margin 8 --gap 3 --rotate free --time 4 --seed 0 \
  --out output --name <job-name> --lang es
```

Quantity per file is read from the filename (`_4pz` / `_8pz`); pass `--no-qty`
to treat every file as one part. `--time` is the compute budget **per sheet** in
seconds — bump it (10–30) for a denser nest on a big job; the default 4 is quick.
`--seed` keeps runs reproducible. Output lands at:
- `output/<job>/<job>_Plan_de_Corte.pdf` (or `_Cut_Plan.pdf` with `--lang en`)
- `output/<job>/<job>_nido.json` — machine-readable layout
- `output/<job>/<job>_S01.dxf`, `_S02.dxf`, … — **one nested DXF per sheet**, the
  file the shop's CAM opens (layers preserved; no lead-ins/kerf — CAM adds those)

`--no-dxf` skips the nested DXFs (PDF/JSON only).

## 3. Verify before trusting the nest

- On the **first** run against a user's real DXFs, sanity-check part sizes in the
  JSON `sheets[].parts` count and the PDF against what the user expects. If a file
  fails with "no OUTER_PROFILES layer" or "found no usable outer contour", the
  export isn't the standard Fusion flat-pattern — check the layers with
  `ezdxf` (a quick probe) before nesting.
- The nester is stochastic within a time budget: it never overlaps parts or
  exceeds the sheet, but **yield varies with `--time` and `--seed`**. For a real
  production nest, run with a larger `--time`, and don't over-trust one run's
  yield — try a couple of seeds and take the best.
- Parts too big for a single sheet are reported as **unplaceable**, never dropped
  silently.

## 4. Show the result

- Render the PDF to an image to eyeball it:
  `qlmanage -t -s 1800 -o /tmp output/<job>/<job>_Plan_de_Corte.pdf` →
  `/tmp/<job>_Plan_de_Corte.pdf.png`, then read it.
- Send the PDF (procurement/plan) and, if the user cuts from these, the nested
  `_S01.dxf …` files with SendUserFile.
- In chat, give the headline: sheet count, overall yield %, parts placed, and
  flag any unplaceable parts.

## Notes / limits (MVP)

- **One material/thickness per job** — nest different materials as separate jobs.
- **Holes are preserved and drawn but not nested into** (the engine has no
  part-in-hole support yet) — small parts won't be auto-placed inside big holes.
- **Remnants** are reported as leftover area only, not yet tracked as reusable
  inventory.
- The multi-sheet wrapper fills sheet-by-sheet, so the **last sheet is often
  sparse** — that's the leftover, not a bug.
- Everything is mm internally; the DXF `INSUNITS` flag is honored (in→mm, etc.).
