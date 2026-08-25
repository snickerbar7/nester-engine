# CLAUDE.md — Nester

A nesting tool with **two modes**:

- **1D tubes** (`nester.tube`) — the original: feed IGES files (one straight
  tube per file) + stock length + saw allowances; it reports how many stock bars
  are needed per profile and exactly where to cut.
- **2D flat sheet** (`nester.sheet`) — feed **DXF** files of flat parts + sheet
  size + material/thickness; it nests the irregular shapes onto stock sheets
  (best-yield, real rotation) and reports how many sheets are needed, producing a
  nested DXF per sheet for the cutter.

**This is a standalone project — nothing to do with Harriet.** Everything runs in
one **Python 3.13** venv at `.venv` (migrated up from 3.9 so the flat-nesting
engine `spyrrow` — which needs 3.11+ — shares the env with the tube tool).

## Your role: be the UI layer

The user drives this conversationally. They prompt ("nest these files, 6 m stock,
3 mm kerf"); you run the tool and hand back both a chat summary **and** written
artifacts (a cut-plan PDF + JSON) in an output folder they can open. Every fresh
conversation starts here — this file + the skills below are the context.

When the user gives you a **tube** nesting job (IGES), the default flow is the
`/nest` skill; when a real IGES file fails to parse, it's `/add-parser-support`.
When the user gives you a **flat sheet** job (DXF), it's `/nest-flat`. If it's
ambiguous which they want, ask: tubes-from-bars (IGES) or flat-parts-from-sheets
(DXF)?

## The pipeline (1D tubes)

```
IGES files ──▶ extract cut length ──▶ group by profile ──▶ pack onto bars ──▶ report
              (bounding box,           (from filename)      (First Fit         (PDF + JSON)
               longest axis = length)                        Decreasing)
```

- **Cut length** = longest axis of the part's bounding box (correct for straight
  tubes). Units come from the IGES Global section, normalized to **mm**.
- **Profile** is read from the **filename** via a configurable regex. Default
  matches `40x40x2`, `50X30X3`, `D32`, `OD25.4`. Mixed profiles per job are
  grouped and nested independently.
- **Stock** = one full-bar (tramo) length per profile (global `--stock-length`,
  with optional `--stock PROFILE=MM` overrides).
- **Retazos (E9)**: the shop's leftovers are extra stock. Pass them with
  `--remnant PROFILE=MM[:LABEL]` (repeatable; label defaults to `R-000n`) — or
  `extra_stock: [{profile, length_mm, label}]` on `POST /v1/nest`. They're a
  finite pool: each piece is usable **once**, gets the same kerf/trims as a
  tramo, and the solver spends the **smallest fitting** one before buying a new
  bar (big retazos stay free for big parts). The plan then reports
  `new_bars_needed` (what to BUY) separately from total bars, names every
  retazo it consumed, and draws each bar at its own length. A retazo for a
  profile that isn't in the job is a warning, never an error.
- **Allowances**: `--kerf` per cut, `--front-trim` (clamp dead zone),
  `--back-trim` (far-end remnant). Usable = bar length − front − back (for a
  retazo too). Each part reserves `length + kerf`.
- **Solver**: First Fit Decreasing — fast, deterministic, strong yield.

## Run a job

```bash
cd ~/Documents/Nester
.venv/bin/python -m nester.tube <files|globs|dir> \
  --stock-length 6000 --kerf 0.2 --front-trim 0 --back-trim 0 \
  --out output --name <job-name> --lang es
```

Writes three files into `output/<job-name>/`:
- `<job>_Plan_de_Corte.pdf` (Cut_Plan if `--lang en`) — the shop cut order,
  laid out to the Harriet Nester "Plan de corte" artboard (A4/letter landscape,
  white paper + graphite ink, Helvetica for prose / Courier for every figure).
  Four kinds of sheet:
  1. **Portada** — resumen de compra (only NEW tramos to buy, retazos used
     listed separately), *cómo quedó el material* (every bar as a mini strip,
     drawn at its own length, with yield + drop), *antes de cortar* (numbered
     amber cards for unplaceable parts and artifact warnings — omitted when
     there are none), *cuentas del material* (comprado · de retazo · en piezas ·
     kerf · zona muerta · sobrante · **aprov.** with its formula), plus nesting
     parameters, source files and four signature lines.
  2. **Dibujo de tramos** — up to 3 bars per sheet, each with a metre ruler,
     pieces colored by length and labeled `P-xx` + length, hatched dead zones,
     dashed drop, running positions under every cut, and a tickable
     **secuencia** chip row.
  3. **Lista de cortes** — every cut in machine order, two columns with tick
     boxes, banded by profile on multi-profile jobs, ending in the **parts
     guide** (`RESUMEN POR PIEZA`: color → which source files that length comes
     from) and a total.
  4. **Etiquetas** — one cut-out label per piece (color bar, `P-xx`, source
     file, length, tramo·pos, folio `JOB-Pxx-nn`).
- `<job>_corte.json` — machine-readable layout.
- `<job>_nest.igs` — the whole nest as 3D wireframe tubes (each bar a box of the
  real cross-section, pieces colored by length) to open in CAD. Skip with
  `--no-iges`.

Without `--out` it just prints to the terminal. `--json` emits machine JSON to
stdout. `--lang` selects the PDF language (es default / en). `--back-trim <mm>`
models the laser/saw chuck dead-zone (the offcut the machine can't reach —
typically ~150–300 mm on a tube laser; confirm with the shop).

To **preview the PDF as an image** (so you can eyeball it and show the user) —
use QuickLook, which renders the non-embedded base-14 font correctly:

```bash
qlmanage -t -s 1800 -o /tmp output/<job-name>/<job>_Plan_de_Corte.pdf
# -> /tmp/<job>_Plan_de_Corte.pdf.png   (then Read that PNG)
```

(The base PDF text won't show in the raw PDF→image harness preview because
Helvetica isn't embedded — QuickLook renders it correctly. Real viewers are fine.)

## Two delivery workflows — pick by who nests (READ)

Both are real; the tool supports both. Which one is "right" depends on the user.

1. **You nest, hand the operator per-bar files** (THIS user's workflow). Each
   file is one 6 m stock bar with its pieces nested on it; the operator loads it
   and runs. Deliverable = the per-bar files from **`--solids`** (`bars/`), one
   per stock bar, clean pieces (no envelope — it z-fights the part faces and
   blurs the cut lines). The **cut-plan PDF** is the planning/procurement side.

2. **Outsource to a cutting service that nests for you** (SendCutSend / OSH Cut /
   Fabworks model). They want ONE 3D solid per UNIQUE part (features modeled) and
   nest it themselves. Deliverable = **`--shop-package`** (`shop_package/`): one
   clean IGES per unique part + `manifest.csv` (part·profile·length·qty·material).
   Pre-nested assemblies are non-standard *for this model* — but that's workflow
   2, not a universal rule.

This user uses **IGES, not STEP**, and nests themselves → workflow 1, per-bar
files. Don't replace per-bar with per-part as "the standard" — ask which workflow.

Both modes strip free construction curves (solids only — else they import as
stray sketches) and write **B-rep solid IGES (type 186) + `BuildCurves3d`** —
verified in Fusion to import as clean SOLID bodies, one per piece (4 for bar1, 15
for bar4, etc.). `--iges-surfaces` falls back to trimmed surfaces (144, imports
as many separate surface bodies). They live in `solid_nest.py`, needs a CAD kernel
(OpenCASCADE) — can't run in the Python-3.9 main tool, so it's in a **separate
Python-3.13 venv** at `.venv-cad`:

```bash
/opt/homebrew/bin/python3.13 -m venv .venv-cad
.venv-cad/bin/pip install cadquery-ocp      # OpenCASCADE (~large download)
```

**One command** (canonical): cut plan + shop package together:

```bash
.venv/bin/python -m nester.tube <dir> --stock-length 6000 --back-trim 300 \
  --out output --name <job> --no-iges --shop-package
```

Add `--solids --spare N` for the optional nested solid preview. Both flags shell
out to `.venv-cad`. Or run the exporter directly:

```bash
.venv-cad/bin/python solid_nest.py --shop-package --src "<parts dir>" \
  --out output/<job>/<job>            # -> output/<job>/shop_package/
```

For the nested preview (reads the nest JSON, so it matches the plan):

```bash
.venv-cad/bin/python solid_nest.py \
  --nest output/<job>/<job>_corte.json \
  --src "<dir of original part IGES files>" \
  --out output/<job>/<job> --spare 1   # writes bars/<job>_<profile>_bar<n>.{step,igs}
```

Default: **one file per stock bar** (each a 6 m tube at origin, pieces in a line)
in a `bars/` subfolder, as **STEP** (true watertight solid) **and IGES**. The
IGES is **B-rep solids (type 186)** — default. The earlier "186 fragments in
Fusion" (the L-shape) was a MISSING-3D-CURVES bug, NOT inherent to 186: with
`BRepLib.BuildCurves3d` applied before writing, 186 imports as clean SOLID bodies
(verified in Fusion: bar1→4 solids, bar4→15, 3×1.5→10, spare→1). `--iges-surfaces`
falls back to trimmed surfaces (144), which import as many separate surface bodies
(geometry fine, not solids) — only if a tool can't read solid IGES. Each bar also includes a
**`--envelope`** flag to add a full stock-tube around each bar's pieces — OFF by
default because the continuous 6 m tube coincides with the part faces and
z-fights, making cut lines appear on some faces but not others. Cut bars are
cleanest as pieces-only (each part is a complete segment with a kerf gap = the
cut, consistent on all 4 faces). `--spare N` adds N uncut stock-tube files per
profile (the buffer-stock files — a spare *is* a full tube). `--combined` makes
a single stacked file.

Key correctness points baked in: **solids only** (loose construction curves are
dropped, else they import as stray sketches), longest axis aligned to **+X**, and
the cross-section rotation **normalized** (wider side along Y) so every piece of a
rectangular profile shares one orientation — a single stock tube can only be
clamped one way. Stock-tube wall/radius default to Cal.18 (`--wall 1.2 --radius 2`).

Validate output with OCP: read back, count `TopAbs_SOLID`, check `free_edges==0`
and the bbox (X ≈ used length, Y/Z = cross-section).

## Flat sheet nesting (2D)

For flat parts cut from sheet stock (laser / plasma / waterjet). Separate package
`nester.sheet`, same UI-layer philosophy, driven by the `/nest-flat` skill.

```
DXF files ──▶ extract contours ──▶ (one material/  ──▶ nest on sheets ──▶ report
             (outer + holes,        thickness/job)     (spyrrow engine)   (PDF + nested
              arcs flattened,                                              DXF per sheet)
              layer-classified)
```

- **Contours** come from the **Fusion 360 flat-pattern layer convention**:
  `OUTER_PROFILES` = the cut outline, `INTERIOR_PROFILES` = holes,
  `BEND`/`BEND_EXTENT` = fold lines (dropped). Arcs/splines are flattened; loose
  `LINE`/`ARC` segments are stitched into closed loops by endpoint matching.
  Units from the DXF `INSUNITS` flag → **mm**.
- **Engine**: `spyrrow` (Rust `sparrow`/`jagua-rs`, MIT) — best-yield irregular
  nesting with real rotation. It solves **strip packing**; we wrap it in a greedy
  **multi-sheet** loop (fixed strip height = sheet height, harvest the block that
  fits the sheet width, roll the overflow to the next sheet).
- **Rotation** per `--rotate`: `free` / `grain` (0°,180°) / `fixed` (0°) / `ortho`.
  Grain-lock for brushed finish or bend-grain parts.
- **Spacing**: `--margin` (edge) + `--gap` (part-to-part, keep ≥ kerf →
  spyrrow `min_items_separation`). Kerf compensation itself is the CAM's job.
- **Output**: cut-plan PDF + `_nido.json` + **one nested DXF per sheet**
  (`_S01.dxf`, layers preserved) for the shop's CAM.

```bash
.venv/bin/python -m nester.sheet <dxf files|dir> \
  --sheet 2440x1220 --material acero --thickness 2 \
  --margin 8 --gap 3 --rotate free --time 4 \
  --out output --name <job> --lang es
```

- **Progress / cancel** (additive; the CLI passes neither): `pack.nest()` takes
  `progress=cb` — called with a `NestProgress(sheets_done,
  sheets_total_estimate, parts_placed, parts_total,
  last_sheet_utilization_pct)` after **each sheet is solved** — and
  `should_cancel=fn`, checked **between sheets**, which raises `NestCancelled`
  carrying the partial `NestResult`. `sheets_total_estimate` really is an
  estimate: the multi-sheet loop can't know the total up front, so it's
  projected from net part area landed per sheet vs area still queued. Mid-sheet
  interruption is impossible — one spyrrow solve is an opaque, time-budgeted
  call. The async jobs API is the only caller.

MVP limits: one material/thickness per job; holes are drawn/preserved but not
nested-into (engine has no part-in-hole support); remnants reported as leftover
area, not tracked as reusable inventory; nesting is stochastic within `--time`
(yield varies with time budget + `--seed`; try a couple of seeds for production).

## File map

| What | Where |
|------|-------|
| **Tube (1D)** — data model (Part, StockSpec, BarLayout, ProfileResult) | `nester/tube/model.py` |
| Cutting-stock solver (FFD) | `nester/tube/packing.py` |
| IGES reader (length extraction) | `nester/tube/iges.py` |
| Filename → profile | `nester/tube/profile.py` |
| CLI | `nester/tube/cli.py` (`python -m nester.tube`) |
| PDF + JSON output (+ parts guide, ruler, colors) | `nester/tube/report.py` |
| IGES nest-layout output (3D wireframe) | `nester/tube/iges_nest.py` |
| **Solid** STEP/IGES output (real part bodies) | `solid_nest.py` (runs under `.venv-cad`, OpenCASCADE) |
| **Flat (2D)** — data model (FlatPart, SheetSpec, Placement, NestResult) | `nester/sheet/model.py` |
| DXF reader (contours + holes, layer-classified) | `nester/sheet/dxf_read.py` |
| Irregular nester (spyrrow wrapper + multi-sheet fill, progress/cancel) | `nester/sheet/pack.py` |
| Part silhouettes for the API (decimation, holes, origin) | `nester/sheet/contour.py` |
| PDF + JSON output | `nester/sheet/report.py` |
| Nested DXF-per-sheet output | `nester/sheet/dxf_out.py` |
| CLI | `nester/sheet/cli.py` (`python -m nester.sheet`) |
| **Service** — async sheet jobs (registry, executor, R2 record) | `service/v1/jobs.py` |
| Synthetic IGES generator (tests) | `tools/make_sample_iges.py` |
| Sample files | `samples/` |
| Job outputs | `output/<job-name>/` |
| Tests | `tests/` (pytest) |

## IGES parser status

The parser extracts coordinates from these entity types and takes the
bounding-box longest axis as cut length: **Line (110), Point (116), Circular Arc
(100), Rational B-Spline curve (126), Vertex List (502)**.

**Validated against real CAD exports** — the Pantallas_LED job is full B-rep
solids from Fusion 360 (entities 186/514/510/508/126/128/502); all 14 files
parsed and lengths/cross-sections matched the BOM. Two real-world gotchas fixed
there and now covered: read files as **latin-1** (IGES is a fixed-column *byte*
format — UTF-8 collapses multibyte chars like `ñ` and shifts every column), and
the Global **delimiter parser** only accepts single-char Hollerith (`1Hx`), not
product-id fields like `7Hunknown`.

If a file contains geometry types not in the list above, `read_tube` raises and
**names the entity types it saw** — run `/add-parser-support`. Always
sanity-check the first run's lengths against known part lengths (and the BOM if
there is one) before trusting a job.

## The service layer + the web product (context)

`service/` exposes the engine over HTTP from ONE Render deployment
(https://harriet-nester.onrender.com) serving two clients with per-client
keys: Harriet (frozen compat contract at `/extract` `/nest` `/health`) and
**Harriet Nester**, the AI-operated nesting web product
(https://harriet-nester.vercel.app, repo `snickerbar7/harriet-nester`,
local `~/Documents/harriet-nester`). Neutral contract under `/v1`:
`health` · `extract` · `nest` (tube, **sync**; mode=sheet → 501 pointing at
jobs) · `jobs` (sheet, **async** — see below) · `uploads` / `downloads`
(presigned PUT/GET, caller-constructed keys, prefix-scoped). Product plan +
engine roadmap: `docs/PRODUCT_PLAN.md`.

**Async jobs (E1/A5) — 2D only.** A tube nest is instant (FFD) so it stays on
`POST /v1/nest`. A sheet nest is minutes, so it's a job:

```
POST   /v1/jobs          -> 202 {job_id, status:"queued", out_prefix, job_record_key}
GET    /v1/jobs/{id}     -> {job_id, status, progress{sheets_done,
                             sheets_total_estimate, parts_placed, parts_total,
                             elapsed_s}, result?, artifacts?, errors, warnings, error?}
DELETE /v1/jobs/{id}     -> {job_id, status:"cancelled", pending, detail}
```

Body = the `/v1/nest` envelope + sheet stock (`sheet_width_mm`,
`sheet_height_mm`, `material`, `thickness_mm`, `margin_mm`, `gap_mm`,
`rotate`, `time_per_sheet_s`, `seed`), `mode` must resolve to sheet, and
`out_prefix` is **required** (artifacts *and* the job record land under it).
Statuses: `queued · running · done · error · cancelled · lost`.

The shape of the implementation is dictated by the deployment: **one Render
instance, no database** (`service/v1/jobs.py` — read its docstring before
changing anything). In-process `ThreadPoolExecutor` (2 concurrent solves) +
an in-memory registry + a durable `<out_prefix>/_job.json` in R2 written on
every transition and every finished sheet. Consequences, all deliberate:
a restart **mid-solve loses the run** → `GET` answers `"lost"` with a message
telling the client to re-submit (it never claims `running` for something
nothing is running); a **finished** job survives — poll
`GET /v1/jobs/{id}?out_prefix=<prefix>` and it's restored from R2; cancel is
checked **between sheets**, so the sheet in flight burns its `time_per_sheet`
budget first. Upgrade path (jobs table in Postgres + a Render worker) is in the
module docstring; the HTTP contract is written to survive it unchanged.

**Real contours (2D).** `POST /v1/extract` with DXF files returns, per part,
`contour: {outer: [[x,y],…], holes: [[[x,y],…],…]}` in mm — origin at the
part's bbox min corner, Douglas-Peucker-decimated (0.2 mm, hard cap 200 points
per loop). The nest result carries the same contours **once per unique part**
under `result.parts[]`, and every placement adds `contour_offset_mm` (translate
the rotated contour by this — `x`/`y` translate the RAW part coordinates, which
is NOT the same thing) plus `bbox_mm`. That's what lets the web nest view draw
true silhouettes with holes instead of rectangles. Contours are opt-in in the
engine (`include_contours=`), so the frozen Harriet responses are unchanged.

## Shipping / branch policy (READ)

**One branch. No staging.** `main` local = dev/experiments; **any push to
`origin main` IS a production deploy** — Render auto-deploys this repo, and the
deployed service is called in production by BOTH Harriet and Harriet Nester
(the web product). Use the `/ship` skill to push: it gates on the full test
suite, frozen-Harriet-contract safety, and docs being updated in the same push.
Never push a red suite. CI (`.github/workflows/ci.yml`) runs the suite on every
push; Render should be configured to wait for CI checks before deploying.

New env vars must be added in Doppler before the deploy that reads them.
Current service auth env: `NESTER_API_KEYS="client:token[:key_prefix]"`
(comma-separated; legacy `NESTER_SERVICE_TOKEN` still = unscoped `harriet`).

## Dev

```bash
# One Python 3.13 env for both tools (tube + flat):
/opt/homebrew/bin/python3.13 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m pytest -q          # tube solver + flat model/reader/nester covered
```

## Conventions / invariants

- All lengths in **mm** internally. IGES unit flag → mm via `iges._UNIT_TO_MM`;
  DXF `INSUNITS` → mm via `nester.sheet.dxf_read._UNIT_TO_MM`.
- 1D: `Placement.start` is the offset **within the usable region** (after
  front-trim), kerf-inclusive. The PDF adds front-trim back for absolute positions.
- 2D: a `Placement` is `(x, y, rotation)` in absolute sheet mm (margin already
  added in); reconstruct placed geometry with `nester.sheet.pack.transform`
  (rotate about origin, then translate — spyrrow's convention).
- Parts that don't fit a single usable bar/sheet are reported as `unplaceable`,
  never silently dropped.
- Don't commit `.venv/`, `.venv-cad/`, `output/`, or `__pycache__/` (see
  `.gitignore`).
