# Nester

Nesting for a fab shop, in two modes:

- **1D straight tubes** (IGES) — how many stock **bars** you need per profile,
  where to cut each, a printable cut plan, **plus the solid CAD files the shop
  cuts from**.
- **2D flat sheet** (DXF) — irregular flat parts nested onto stock **sheets** for
  a laser/plasma/waterjet, with a nested DXF per sheet for the cutter. See
  [Flat sheet nesting](#flat-sheet-nesting-2d).

Everything runs in one **Python 3.13** venv (`.venv`).

## 1D tubes

## Pipeline

```
IGES parts ─▶ extract length ─▶ group by profile ─▶ pack (FFD) ─▶ outputs
             (bbox longest      (+ gauge, + qty       one stock     PDF · JSON
              axis, real         from filename)        length        wireframe IGES
              B-rep solids)                             per profile   solid STEP/IGES
```

- **Cut length** = longest bbox axis of the part's real solid (validated against
  Fusion 360 B-rep exports). Cross-section (the two shorter axes) confirms the
  profile.
- **Profile** comes from the filename (e.g. `2x2_C18`), incl. gauge. **Quantity**
  too (`_4pz` → 4 pieces). Mixed profiles per run are grouped + nested separately.
- **Packing** is First Fit Decreasing — fast, deterministic, and provably optimal
  on real jobs (it reports the minimum bar count).
- **Usable region** = `stock_length − front_trim − back_trim`. Use `back_trim`
  for a laser/saw **chuck dead-zone** (the offcut the machine can't reach).

## Quick start

```bash
/opt/homebrew/bin/python3.13 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python -m nester.tube <dir|files|globs> \
  --stock-length 6000 --kerf 0.2 --back-trim 300 \
  --out output --name MyJob --lang es
```

Writes into `output/MyJob/`:
- `MyJob_Plan_de_Corte.pdf` — the shop cut order: shopping summary, per-bar
  diagrams with a metre ruler, parts colored by length, labeled scrap + chuck
  zone, cut list, and a **parts guide** (color → which source files).
- `MyJob_corte.json` — machine-readable layout.
- `MyJob_nest.igs` — quick 3D wireframe of the nest.

Add `--solids` (and `--spare N`) to also export the **real solid** files (see
below). Common flags:

| flag | meaning |
|------|---------|
| `--stock-length MM` | default bar length (required); `--stock PROFILE=MM` to override one |
| `--kerf MM` · `--front-trim MM` · `--back-trim MM` | saw/laser allowances |
| `--lang es\|en` | cut-plan language (default es) |
| `--profile-regex` · `--qty-regex` · `--no-qty` | filename parsing |
| `--solids` · `--spare N` | also export solid CAD files (needs `.venv-cad`) |
| `--json` · `--no-iges` | stdout JSON · skip wireframe |

## What you send the laser shop

A 3D tube laser wants **one solid file per unique part** (with holes/copes/end-cuts
modeled) and **does its own nesting** — pre-nested files are non-standard for 3D
tube (only sheet/DXF gets pre-nested). So the shop deliverable is `--shop-package`:

```bash
.venv-cad/bin/python solid_nest.py --shop-package --src "<parts dir>" \
  --out output/MyJob/MyJob          # -> output/MyJob/shop_package/
```

That writes one clean **B-rep solid IGES per unique part** (free construction
curves stripped) + `manifest.csv` (part · profile · length · qty · material). The
**cut-plan PDF stays on your side** — it tells you how many bars to buy, which the
shop's nesting can't tell you before you order steel.

A nested **solid preview** (parts positioned on each bar, STEP+IGES) is available
via `--solids` (`--spare N` for buffer tubes, `--envelope` to wrap each bar) — for
eyeballing only, not the shop file.

This needs OpenCASCADE, which won't run under Python 3.9, so it lives in a
separate Python-3.13 env:

```bash
/opt/homebrew/bin/python3.13 -m venv .venv-cad
.venv-cad/bin/pip install cadquery-ocp
```

Then `--solids` on the main command runs it automatically, or run it standalone:

```bash
.venv-cad/bin/python solid_nest.py \
  --nest output/MyJob/MyJob_corte.json --src "<parts dir>" \
  --out output/MyJob/MyJob --spare 1     # -> output/MyJob/bars/*.{step,igs}
```

## Flat sheet nesting (2D)

Flat `.dxf` parts nested onto rectangular stock sheets — a separate tool
(`nester.sheet`) sharing the same env. It reads the Fusion 360 flat-pattern layer
convention (`OUTER_PROFILES` cut outline, `INTERIOR_PROFILES` holes, `BEND*`
ignored), flattens arcs, and nests the irregular shapes with real rotation using
the [`spyrrow`](https://pypi.org/project/spyrrow/) engine (Rust `sparrow`/`jagua-rs`).

```bash
.venv/bin/python -m nester.sheet <dxf files|dir> \
  --sheet 2440x1220 --material acero --thickness 2 \
  --margin 8 --gap 3 --rotate free --time 4 \
  --out output --name MyJob --lang es
```

Writes into `output/MyJob/`:
- `MyJob_Plan_de_Corte.pdf` — sheet cut plan: per-sheet layout, parts colored by
  source, yield %, parts guide.
- `MyJob_nido.json` — machine-readable layout.
- `MyJob_S01.dxf`, `_S02.dxf`, … — **one nested DXF per sheet** (layers preserved)
  for the shop's CAM to add lead-ins / kerf comp / cut order.

| flag | meaning |
|------|---------|
| `--sheet WxH` | stock sheet size in mm (default `2440x1220`, i.e. 4×8 ft) |
| `--material` · `--thickness` | one stock group per job (labels the report) |
| `--margin MM` · `--gap MM` | sheet-edge margin · part-to-part spacing (keep ≥ kerf) |
| `--rotate` | `free` · `grain` (0/180, for finish/bend-grain) · `fixed` · `ortho` |
| `--time SEC` · `--seed` | compute budget per sheet (more = denser) · reproducibility |
| `--no-qty` · `--no-dxf` · `--json` | file-per-part · skip nested DXF · stdout JSON |

MVP limits: one material/thickness per job; holes are preserved but not
nested-into; remnants are reported as leftover area only. Nesting is stochastic
within `--time` — bump time and try a couple of seeds for a production nest.

## Filename convention

Default regex matches a cross-section + optional gauge anywhere in the name:

| filename | profile | qty |
|----------|---------|-----|
| `Base_Central_2x2_C18_302_4pz.iges` | `2x2_c18` | 4 |
| `Brazo_3x1.5_C18_151_6pz.iges` | `3x1.5_c18` | 6 |
| `chassis-D32_left.igs` | `d32` | 1 |

Override with `--profile-regex` / `--qty-regex` for a different scheme.

## Dev

```bash
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m pytest -q          # solver, filename parsing, report labels
```

See `CLAUDE.md` for architecture, the IGES parser notes, the flat-nesting design,
and the solid-export internals; skills live under `.claude/skills/` (`/nest`,
`/nest-flat`, `/add-parser-support`, `/solid-export`).
