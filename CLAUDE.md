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

## How work runs here: dispatch, then audit (READ)

**Don't do the changes inline.** This session orchestrates; agents do the work.

| Grade | Model | For |
|-------|-------|-----|
| **Thinking / finding** | quality model (**Opus 5**), effort **high or xhigh** | research, root-cause hunts, "what's wrong with this", auditing a design, choosing an approach |
| **Executing** | **Sonnet** | implementing what the thinkers found, and any mechanical work — but the brief must be EXPLICIT: exact files, exact contract, what NOT to touch, how to verify |

Then **this session audits every agent's output** — read the diff, run the
gates, decide whether it's actually right. An agent's report is input, not a
verdict. (Real example: on the E15/E16/E8 round the test agent surfaced three
genuine bugs, and the service agent routed job notes into `warnings[]`, which
would have changed Harriet's frozen contract. Both only landed correctly
because of the audit.)

Run agents in parallel only over **non-overlapping file sets** — never two
agents in one working tree.

**All UI goes through Claude Design.** Never design or restyle UI here, and
never publish UI artifacts. When a change implies UI, the deliverable from this
repo is the **engine contract + a design brief/prompt**; Marlon runs the canvas
session, and the handoff comes back through `/design-round`.

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
- **Juegos (E22)**: quantity comes from the filename, which the shop can't
  rename — so a per-file multiplier says how many sets to build: `--sets
  FILENAME=N` (repeatable; `sets` on an API FileRef, 1–999). Effective qty =
  filename qty × sets (`_2pz` × 50 juegos = 100 pieces), multiplied **before**
  nesting so the bars to buy scale with it.
- **Stock** = one full-bar (tramo) length per profile (global `--stock-length`,
  with optional `--stock PROFILE=MM` overrides).
- **Retazos / sobrantes (E9 + E24)**: the shop's leftovers are extra stock.
  Pass them with `--remnant PROFILE=MM[:LABEL]` (repeatable; label defaults to
  `R-000n`) — or `extra_stock: [{profile, length_mm, label}]` on
  `POST /v1/nest`. A finite pool: each piece is usable **once**, gets the same
  kerf/trims as a tramo, and the solver spends the **smallest fitting** one
  first (big pieces stay free for big parts) — and, exactly like 2D, **only
  when spending it removes a tramo**. The plan reports `new_bars_needed` (what
  to BUY) separately from total bars, names every piece it consumed, draws each
  bar at its own length, and reports the ones it declined. A retazo for a
  profile that isn't in the job is a warning, never an error.

  > **The decline rule (E24) — the 1D half of the same doctrine.** `minimize_bars`
  > (CLI `--minimize-bars`, **on by default**) packs the job three ways: no rack
  > (`B0`), full rack (`B_full`), and — when the rack wins — a greedy **minimal
  > subset**, dropping the LONGEST pieces whose removal does not make the answer
  > worse. FFD is milliseconds, so the repacks are free. "Better" is
  > `(unplaceable parts, tramos to buy)` lexicographically, so a piece that
  > rescues a part no tramo could hold is also worth opening. The subset is found
  > by re-packing, not by testing pieces one at a time: **two offcuts can remove a
  > tramo together while neither does alone** (pinned in
  > `tests/test_tube_decline.py`).
  >
  > Measured on a 6 m job (BOM 1850×14 · 1200×22 · 900×18 · 2400×9 · 640×26,
  > kerf 0.2, back-trim 300; baseline **20 tramos, 89.0%**), sweeping ONE offered
  > offcut, before → after:
  >
  > | offcut | old rule | new rule |
  > |--------|----------|----------|
  > | 1000 mm | spent · BUY 20 · 88.2% (**−0.7pp**) | declined `no_gain` · BUY 20 · 89.0% |
  > | 2200 mm | spent · BUY 20 · 87.3% (**−1.6pp**) | declined `no_gain` · BUY 20 · 89.0% |
  > | 3000 mm | spent · BUY 20 · 86.8% (**−2.2pp**) | declined `no_gain` · BUY 20 · 89.0% |
  > | 3400 mm | spent · BUY **19** · 90.9% | spent · BUY **19** · 90.9% |
  >
  > Swept 500–6000 mm in 100 mm steps: **6 of 15 sizes used to be spent for
  > nothing, now 0** — every size either removes a tramo or stays on the rack. On
  > a denser BOM the old rule wasted 11 of 15 sizes, up to −7.1pp. The penalty
  > grew monotonically with offcut size, so the bigger the piece a shop offered,
  > the harder the plan punished it.
  >
  > `remnants_unused[].reason` mirrors 2D: `no_gain` · `no_fit` (nothing in the
  > job fits it) · `too_small_for_trims` · `job_ended`. `--no-minimize-bars`
  > restores the old unconditional spending (pinned by a test).
- **Sobrante recuperable / net yield (E24)**: `--min-remnant MM` (CLI default
  **200**, API/`StockSpec` default 0) is the shortest drop worth keeping. At or
  above it a bar's drop is a recoverable **SOBRANTE** that goes back on the
  rack; below it, **MERMA**. It is NOT `--back-trim` (the chuck dead zone) and
  must never be overloaded onto it. From it come the additive metrics
  `net_yield_pct = parts ÷ (stock − reclaimable)` and `gross_yield_pct`
  (identical to `yield_pct`), plus `reclaimable` / `waste` per profile and
  `leftover_mm` / `waste_mm` per bar. **`yield_pct` keeps its exact name,
  meaning and value** — Harriet's frozen `/nest` reads it, and with no
  `min_remnant` net == gross, so nothing moved for existing callers.
- **Allowances**: `--kerf` per cut, `--front-trim` (clamp dead zone),
  `--back-trim` (far-end remnant). Usable = bar length − front − back (for a
  retazo too). Each part reserves `length + kerf`.
- **Solver**: First Fit Decreasing — fast, deterministic, strong yield.

## Run a job

```bash
cd ~/Documents/Nester
.venv/bin/python -m nester.tube <files|globs|dir> \
  --stock-length 6000 --kerf 0.2 --front-trim 0 --back-trim 0 \
  --min-remnant 200 --remnant 40x40x2=3400:R-0001 \
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
- **Juegos (E22)**: same multiplier as the tube tool — `--sets FILENAME=N`
  (`sets` on an API FileRef) multiplies **every** part in that DXF, so a
  multi-part file scales as one set; sheets are computed for the full count.
- **Rotation** per `--rotate`: `free` / `grain` (0°,180°) / `fixed` (0°) / `ortho`.
  Grain-lock for brushed finish or bend-grain parts.
- **Spacing**: `--margin` (edge) + `--gap` (part-to-part, keep ≥ kerf →
  spyrrow `min_items_separation`). Kerf compensation itself is the CAM's job.
- **Relleno del área libre (E23)** — `--fill-free-area` / `fill_free_area`,
  **on by default, and independent of the search below**. spyrrow 0.9 exposes
  only `StripPackingInstance.solve`: no bin packing, no fixed container, no way
  to hand it a partially-occupied sheet or a set of obstacles. So a part the
  packer leaves in the gaps of sheet 1 is stranded there forever, and *denying*
  a job a sheet does not make it pack denser — it just makes it fail. The
  mechanism is ours: after each solve, `nester/sheet/holes.py` fills
  `sheet usable rect − union(placed ⊕ gap)` with still-unplaced parts, the same
  bounded shapely search E15 uses on holes. Parts landing there are **ordinary
  parts** — no `in_hole_of`, not counted in `parts_in_holes`; only the hole
  container flags a part. It only APPENDS, so a sheet can never come out worse.
  `--min-hole-side MM` (30) is the shortest side a void needs before either
  container will look at it: a bolt hole is not usable surface, and sweeping it
  costs real time. **This pass, not the sheet search, is what raises yield**
  (measured: 60.1% → 63.1% net for **1.10×** the wall clock), which is why it
  has its own switch — turning the search off must not silently turn this off.
- **Menos láminas que comprar (E23)**: `minimize_sheets` (CLI
  `--minimize-sheets`, API field; **on by default**) turns the multi-sheet loop
  from a greedy walk into a **search for the lowest bought-sheet count**. It
  re-nests under a *ceiling* on new sheets — starting at the area floor
  (`(net part area − rack area) / usable sheet area`), jumping on the yield it
  actually observed rather than stepping +1, and descending while the job stays
  feasible. `--max-new-sheets` (40) and `--search-budget SEC` (0 = none) bound
  it; on either limit the best **feasible** nest comes back with
  `totals.search.capped = true`, and if nothing was feasible the fallback is the
  unbounded greedy walk, so a job is never failed for the search running out of
  road. `totals.search` reports `area_floor_sheets · ceiling_tried ·
  ceiling_used · attempts · capped` (`-1` in `ceiling_tried` = the unbounded
  fallback; `0` is a real ceiling meaning "buy nothing"). `--no-minimize-sheets`
  restores the greedy loop **and** unconditional rack spending.

  > **What the ceiling ladder does NOT do — read this before optimizing it.**
  > It never lowered the sheet count. Across ~20 constructions (portrait
  > sheets, L-shapes, free/ortho rotation, hole jobs, 1s and 4s budgets, three
  > seeds each) there was **not one case** where the ladder beat the greedy
  > walk on sheets bought: spyrrow already packs each sheet to the area floor,
  > so greedy is already optimal on count for these jobs. Its two real jobs are
  > (a) **proving** the count is at the floor — `ceiling_used ==
  > area_floor_sheets` means no packing can do better, which the plan is
  > entitled to say — and (b) making the **rack decline rule** possible at all,
  > since deciding whether an offcut is worth opening requires solving the job
  > both ways. Do not assume the ladder is what lowers the count; it is not,
  > and a future engine with a real bin-packing solver is what would change
  > that.
- **Retazos de lámina (E16)**: the shop's sheet offcuts are extra stock. Pass
  them with `--remnant WxH[:LABEL]` (repeatable; label defaults to `R-000n`,
  must be unique) — or `extra_sheets: [{width_mm, height_mm, label}]` on
  `POST /v1/jobs`. A finite pool: each piece is usable **once**, and the solver
  spends the **smallest fitting** one first (big retazos stay free for big
  parts) — and, like 1D since E24, **only when spending it removes a purchase**.

  > **The decline rule (E23) — this is the 2D doctrine, not an optimization.**
  > A retazo is opened only if opening it lowers `sheets_to_buy`. One that would
  > not stays on the rack and comes back in `remnants_unused` with
  > `reason: "no_gain"`.
  >
  > Measured, seed 1, on the reproduction job (net yield): **no rack 60.1%** ·
  > **rack, old always-spend rule 58.8%** · **rack, decline rule 63.1%**. The
  > old rule left the shop *worse off than owning no offcuts at all*: it spent
  > two physical pieces, bought the same three sheets, and put 1.44 m² of
  > already-paid-for material into the denominator. That is not a metric
  > artefact — same purchase, and two offcuts gone. Gross fell 53.7% → 46.3%.
  >
  > Two exceptions, both deliberate: the rack is spent unconditionally when it
  > is **load-bearing** (a part fits an offcut but no new sheet — then "without
  > the rack" is not a job at all), and on **`--no-minimize-sheets`**, which
  > restores the pre-search engine including unconditional spending.
  >
  > `remnants_unused[].reason` is one of `no_gain` · `no_fit` (nothing left
  > fitted it) · `too_small_for_margin` · `job_ended`.

  Sheets in one job therefore need
  not be the same size: every `SheetLayout` carries its own `spec` + `source`,
  and area/weight totals SUM the layouts instead of multiplying a count. The
  plan reports `new_sheets_needed` (what to BUY) separately from total sheets,
  names every retazo consumed, and draws each sheet at its own size. A retazo
  nothing fits is never burned on an empty sheet — it lands in
  `remnants_unused` and stays on the rack. "Too big to nest at all" is measured
  against the **largest** stock on offer, so a part that only the big offcut
  can hold still lands.
- **Sobrante recuperable (E16, other half)**: `--min-remnant MM` (CLI default
  200; API default 0) reports what each sheet has LEFT as a rectangle — the
  larger of the two guillotine bands a bottom-left nest leaves (right of the
  last part, or above it), gap already respected, both sides ≥ the minimum. It
  is drawn on the sheet page and emitted as `sheets[].leftover` +
  `reclaimable[]` so it can be booked straight into a retazo inventory instead
  of written off. Pockets *between* parts are real material but are not
  shearable in one pass, so they stay counted as drop.
- **Piezas en barrenos (E15)**: `--nest-in-holes` runs a second pass that fills
  already-placed parts' holes with still-unplaced parts (`nester/sheet/holes.py`,
  shapely). jagua-rs packs *simple* polygons — a 300 mm hole is solid material
  to it — so this is our own bounded search: candidate translations on a grid,
  exact containment, clearance = the job's part gap, one level deep, first-fit
  (not an optimum). It only ever APPENDS, so enabling it cannot make a sheet
  worse. **Off by default** because it has a machine consequence: those parts
  come out inside a slug, so the plan flags each one (`Placement.in_hole_of`),
  gives them their own `EN BARRENO` row in the sheet's part list, and tells the
  operator not to bin the slug with the skeleton.

  > **Correctness invariant, both top-up passes: overlap is checked
  > unconditionally, gap is an ADDITIONAL constraint on top.** `_try_place` in
  > `nester/sheet/holes.py` places successive candidates in the same hole or
  > free-area region by testing each against every already-placed `blocker` in
  > that region. A guard that reads `if gap > 0 and <the only overlap
  > check>:` is wrong: at `part_gap == 0` — a legal, defaultable value, not an
  > edge case — the condition short-circuits to False and blockers are never
  > consulted, so every copy after the first lands on the identical
  > bottom-left candidate and stacks exactly on top of it. Found on a real
  > repro (700×520×9, 380×300×14, 120×90×40 on 2440×1220, `--gap 0`): 300-435
  > overlapping placement pairs, every seed, and the plan reported it needed
  > FEWER sheets than the true (non-overlapping) answer — the defect made the
  > plan look better while being physically impossible. Fixed in
  > `tests/test_sheet_gap_zero_overlap.py`, which pins that exact repro at
  > `gap=0` (must be zero overlapping pairs, measured by reconstructing
  > placements with `transform` + shapely, never by trusting a field the
  > engine computed about itself) and property-tests random jobs across
  > `gap ∈ {0, 0.5, 3, 8}` for: no overlap, gap respected when `gap > 0`, every
  > placement inside its OWN sheet's usable area (`sheet.spec`, which differs
  > from the job's nominal sheet on a retazo), and quantity conservation. Any
  > future guard in this module conditioned on `gap > 0` (or any other
  > parameter's zero default) needs the same scrutiny: check whether it is
  > protecting something that must hold unconditionally.
- **Kilos (E8, 2D)**: `nester/materials.py` is a density **lookup, not an
  estimator**. `--material` resolves free-text Spanish/English trade names
  ("acero inoxidable 304", "lámina negra", "aluminio 6061", "galvanizada") to
  kg/m³; `--density KG_M3` overrides for an alloy the table doesn't know.
  Matching is whole-word and the **rightmost** match wins (a trade name narrows
  left to right, so "acero inoxidable **430**" is 7700, not 304's 8000). With
  no known density OR no thickness the plan reports **no kilos at all** and says
  why — never a guessed one, because an invented kilo figure becomes a wrong
  purchase order. `weight = area × thickness × density`.
- **Neto vs bruto (E23)**: the design's *Métricas 2D*. `areaTotal` sums each
  sheet's OWN size; `areaDevuelta` is the reclaimable leftovers; **`neto =
  areaPz / (areaTotal − areaDevuelta)`** is the headline and `bruto = areaPz /
  areaTotal` sits beside it. The denominator discounts what goes back on the
  rack — the same rule the tube tool applies — and it is what stops a
  rack-using job from being penalised. In JSON: `totals.net_yield_pct`,
  `gross_yield_pct`, `part_area_mm2`, `stock_area_mm2`, `new_stock_area_mm2`,
  `consumed_area_mm2`, `waste_area_mm2`, plus `from_rack_kg` / `leftover_kg` /
  `waste_kg` when the job can be weighed. **`yield_pct` and `drop_kg` keep their
  original name, meaning and value** — the new keys sit beside them, they never
  redefine them. `--kerf MM` is **reported only** (`params.kerf_mm`) so the shop
  can check the gap clears it; kerf compensation stays the CAM's job.
- **Output**: cut-plan PDF + `_nido.json` + **one nested DXF per sheet**
  (`_S01.dxf`, layers preserved) for the shop's CAM.

```bash
.venv/bin/python -m nester.sheet <dxf files|dir> \
  --sheet 2440x1220 --material acero --thickness 2 \
  --margin 8 --gap 3 --rotate free --time 4 --kerf 0.2 \
  --remnant 1220x600:R-0007 --nest-in-holes --min-remnant 200 \
  --min-hole-side 30 --max-new-sheets 40 --search-budget 0 --fill-free-area \
  --out output --name <job> --lang es
```

- **Progress / cancel** (additive; the CLI passes neither): `pack.nest()` takes
  `progress=cb` — called with a `NestProgress(sheets_done,
  sheets_total_estimate, parts_placed, parts_total,
  last_sheet_utilization_pct)` after **each sheet is solved** — and
  `should_cancel=fn`, checked **between sheets and between search attempts**,
  which raises `NestCancelled` carrying the best `NestResult` so far.
  `sheets_total_estimate` is projected from net part area landed per sheet vs
  area still queued, and under a ceiling it is bounded by that ceiling instead —
  a real limit, not a projection. With the search on there are several attempts,
  so a tick also carries `attempt` and `new_sheet_ceiling`: `sheets_done`
  restarting at 0 is a NEW pass, not the solve going backwards. Mid-sheet
  interruption is impossible — one spyrrow solve is an opaque, time-budgeted
  call. The async jobs API is the only caller.

Remaining limits: one material/thickness per job; nesting is stochastic within
`--time` (yield varies with the time budget even at a fixed `--seed`, because
the budget is wall-clock — try a couple of seeds for production); hole nesting
and the free-area top-up are both bounded first-fit searches, not optima.

Cost, measured on the reproduction job (2 workers on Render, so this is real
money): the **top-up is nearly free — 1.10x** and it is where the yield comes
from. The **sheet search** costs ~**1.09x with no rack** (one attempt: it
confirms the floor and stops) and ~**1.5x with a rack** (two attempts: it has
to solve the job with and without the offcuts to decide). Worst case seen was
3 attempts / 2.5x on rack + holes. `--search-budget` / `sheet_search_budget_s`
caps the whole search in wall clock; `--no-minimize-sheets` removes it entirely
while `--fill-free-area` keeps the cheap win.

## File map

| What | Where |
|------|-------|
| **Tube (1D)** — data model (Part, StockSpec, BarLayout, ProfileResult) | `nester/tube/model.py` |
| Cutting-stock solver (FFD + the retazo decline rule) | `nester/tube/packing.py` |
| IGES reader (length extraction) | `nester/tube/iges.py` |
| Filename → profile | `nester/tube/profile.py` |
| CLI | `nester/tube/cli.py` (`python -m nester.tube`) |
| PDF + JSON output (+ parts guide, ruler, colors) | `nester/tube/report.py` |
| IGES nest-layout output (3D wireframe) | `nester/tube/iges_nest.py` |
| **Solid** STEP/IGES output (real part bodies) | `solid_nest.py` (runs under `.venv-cad`, OpenCASCADE) |
| **Material densities** — trade-name → kg/m³ (shared, drives all weights) | `nester/materials.py` |
| **Flat (2D)** — data model (FlatPart, SheetSpec, ExtraSheet, Leftover, Placement, NestResult) | `nester/sheet/model.py` |
| DXF reader (contours + holes, layer-classified) | `nester/sheet/dxf_read.py` |
| Irregular nester (spyrrow wrapper + sheet-count search, retazo pool, progress/cancel) | `nester/sheet/pack.py` |
| Void filling: part-in-hole (E15) + free-area top-up (E23), one shapely search | `nester/sheet/holes.py` |
| Part silhouettes for the API (decimation, holes, origin) | `nester/sheet/contour.py` |
| **JSON contract** the web product + jobs API parse (additive only) | `nester/sheet/result_json.py` |
| PDF output (portada + a drawing sheet per lámina, real silhouettes) | `nester/sheet/report.py` |
| Nested DXF-per-sheet output | `nester/sheet/dxf_out.py` |
| CLI | `nester/sheet/cli.py` (`python -m nester.sheet`) |
| Print design tokens + canvas primitives (shared PDF vocabulary) | `nester/_pdfstyle.py` |
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

The 2D engine options ride the same body, all optional: `extra_sheets:
[{width_mm, height_mm, label}]` (retazos, ≤200, unique labels
case-insensitively, 422 otherwise), `nest_in_holes` (bool), `min_remnant_mm`
(≥0), `density_kg_m3` (>0; overrides what `material` resolves to), and the E23
knobs — **`fill_free_area` (bool, default TRUE; independent of the search)**,
**`minimize_sheets` (bool, default TRUE)**, `max_new_sheets`
(1–200, default 40), `sheet_search_budget_s` (0–1800, 0 = unbounded),
`min_hole_side_mm` (≥0, default 30) and `kerf_mm` (≥0, **reported only**,
surfacing as `result.params.kerf_mm` — the engine never applies kerf
compensation). Out-of-range values are 422. `minimize_sheets` defaults ON
deliberately: sheet mode has only this product's own two clients and Harriet's
frozen surface never reaches it. `GET /v1/jobs/{id}`'s `progress` gains
`attempt` + `new_sheet_ceiling`, because a second search attempt restarts
`sheets_done` at zero. **`GET /v1/materials`** returns the density catalog, and
`?name=` resolves one free-text name — `resolved: null` is a real answer
meaning "cannot be weighed", which is what keeps the AI from inventing a
density (the product rule is that the AI never produces a number).

A job carries **`notes[]` alongside `warnings[]`**, and the distinction is
load-bearing: `warnings` means "an artifact could not be produced" (E4) and
Harriet's frozen `/nest` reads it, whereas `notes` are job remarks that are not
failures — no density resolved, no thickness, a retazo nothing fitted. Anything
in `notes` is also recoverable structurally (`params.density_kg_m3` is null,
`result.remnants_unused[]`), so a client never has to parse Spanish prose.

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

**Juegos / sets (E22).** Every `/v1` FileRef takes an optional `sets` (int,
1–999, default 1, 422 outside) on `extract`, `nest` and `jobs`, both modes:
effective qty = filename qty × sets, multiplied before the solver runs.
`/v1/extract` reports `qty_from_name` and `sets` next to the EFFECTIVE `qty`
(unchanged meaning) so a UI can render "2 × 50 = 100". Harriet's contract has
no `sets` and is byte-identical.

**Real contours (2D).** `POST /v1/extract` with DXF files returns, per part,
`contour: {outer: [[x,y],…], holes: [[[x,y],…],…]}` in mm — origin at the
part's bbox min corner, Douglas-Peucker-decimated (0.2 mm, hard cap 200 points
per loop). The nest result carries the same contours **once per unique part**
under `result.parts[]`, and every placement adds `contour_offset_mm` (translate
the rotated contour by this — `x`/`y` translate the RAW part coordinates, which
is NOT the same thing) plus `bbox_mm`. That's what lets the web nest view draw
true silhouettes with holes instead of rectangles. Contours are opt-in in the
engine (`include_contours=`), so the frozen Harriet responses are unchanged.

## Design rounds (canvas -> code)

The product UI is designed in Claude Design and lands as handoff folders.
The full loop — diff vs the web repo's `design/`, staging, agent briefs,
gates, promotion — is the `/design-round` skill in this repo. `design/` in
`~/Documents/harriet-nester` always equals what the app implements.

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
