# Nester — Product Plan

**Product:** the nesting engine, sold as a conversation. The user opens the web
app, drops their CAD files, and tells the AI what they want in their own words
— *"tubo de 6 metros, sierra, 3 de kerf"* — and gets back exactly what a nester
operator would produce: cut-plan PDF, nested DXF per sheet, IGES, JSON. No
nesting software to learn, no operator bottleneck.

**The differentiator:** every fab shop has a nester bundled with its machine,
but *someone has to know how to drive it*. Here the AI is the operator. The
user speaks shop language; the AI speaks the engine's language. That's the
whole pitch — which means the first 90 seconds (drop files → talk → see a
drawn nest) *is* the product.

**Explicitly NOT this product:** quoting, invoicing, factura/CFDI, commercial
workflow. All of that is **Harriet's territory** and ships there. Nester
includes at most a **material-cost estimate** as an engine output line (user
supplies price per kg / per tramo / per sheet; the engine multiplies — see E8).
If a feature smells like "running the business," it goes to Harriet.

Mexico-fluency (calibre, PTR, tramos de 6 m, lámina 4x8) stays — not as a
quoting feature but as UX: the AI must speak the trade.

---

## 1. Architecture — engine, one service, two contracts

```
  ┌─────────────────────────┐        ┌──────────────────────────┐
  │  Harriet (existing)     │        │  nester-web (NEW repo)   │
  │  TS · quoting/commerce  │        │  Next.js on Vercel       │
  │  lives there            │        │  UI · auth · AI orch.    │
  └───────────┬─────────────┘        └────────────┬─────────────┘
              │ HTTP · compat contract            │ HTTP · /v1 contract
              └───────────────┬───────────────────┘
                              ▼
              ┌──────────────────────────────────┐
              │  nester-service (THIS repo)      │
              │  ONE Render deployment, Docker   │
              │  stateless · per-client API keys │
              ├──────────────────────────────────┤
              │  nester/  — pure engine          │
              │  tube (1D) · sheet (2D)          │
              └──────────────────────────────────┘
```

### This repo's target shape

```
nester/            pure engine — tube/ (1D), sheet/ (2D). No I/O beyond files,
                   no knowledge of any caller. Already right; don't change shape.
service/
  v1/              NEUTRAL contract — snake_case, engine vocabulary
                   (bars_needed, yield_pct). What nester-web (and any future
                   client) speaks. Async job API lives here.
  harriet/         compat shim — today's /extract + /nest, camelCase
                   NestResult mapping, kept so Harriet needs zero coordinated
                   deploys. Thin adapter over v1 internals.
solid_nest.py      CAD solid exports (.venv-cad / OpenCASCADE)
```

**One deployment, two clients — do not pay for two servers.** The service is
stateless; nothing about it is per-tenant except the API key and the R2
prefix. Same URL for both callers.

### Boundary work required (Phase 0)

| # | Change | Why |
|---|--------|-----|
| A1 | Neutral `/v1` contract (snake_case, engine vocab). | Two clients can't share a contract shaped like one of them. |
| A2 | Keep `/nest` + `/extract` as Harriet compat shim over the same internals. | Harriet keeps working untouched. |
| A3 | Per-client API keys (`NESTER_API_KEYS="harriet:tok1,web:tok2"`) → scoped R2 prefix per client. | Service must know who's calling; neither client can read the other's files. |
| A4 | Service treats object keys/prefixes as opaque; callers construct them. Delete Harriet's key scheme from `r2.py` docs. | Keeps caller domain concepts out. |
| A5 ✅ | Async job API in `/v1`. | **DONE** — `POST /v1/jobs` → 202, `GET /v1/jobs/{id}` (status · progress · result · artifacts), `DELETE` to cancel. Tube stays sync on `/v1/nest`; mode=sheet there 501s and points here. |

---

## 2. Engine — gaps by tier

### Tier 0 — blocks charging anyone (trust)

The pitch is "you don't need to understand the software" — so the user CANNOT
sanity-check output the way a CAM operator would. The engine must be unable to
be confidently wrong.

| # | Gap | Fix | Effort |
|---|-----|-----|--------|
| E1 ✅ | 2D `/nest` is synchronous (minutes of solve). | **DONE** — A5 jobs API. Sized to the real deployment (one Render instance, **no DB**): ThreadPoolExecutor (2 concurrent solves) + in-memory registry + durable `<out_prefix>/_job.json` in R2. `pack.nest()` gained a per-sheet progress callback and a between-sheets cancel check. A restart mid-solve is reported as `lost` (client re-submits); a **finished** job is restored from R2 via `?out_prefix=`. Postgres job table + Render worker is the documented upgrade path, and the HTTP contract survives it unchanged. | M |
| E2 | Bent tubes silently return chord length as cut length. | Compare measured cross-section vs filename profile; minor extents exceed nominal → not straight → **raise**. | **S** |
| E3 | 2D nesting is wall-clock stochastic; re-runs give different sheet counts. | Persist the nest result as source of truth; re-nest only as an explicit new revision. | S |
| E4 | `except Exception: pass` around IGES-nest and DXF-per-sheet writes → missing deliverables with no explanation. | Return reason in `warnings[]`; AI must surface it. | **S** |
| E5 | Profile comes only from filename; `Part1.igs` fails or garbage-buckets. | Fall back to grouping by measured cross-section; AI/user names and confirms each group. | M |
| E6 | No per-part QA signal for the AI to relay. | Every part carries `{measured_cross_section, matches_profile, straight, source_entities}`; AI surfaces mismatches before nesting. | M |
| E21 ✅ | 2D nesting could die on a raw Rust panic (`PanicException: … Offset resulted in an empty polygon`) — `PanicException` derives from **BaseException**, so every `except Exception` in the stack missed it and the user saw an unwrap string. Hit in production on a real job (915×2440 sheet, gap 3, three small parts). | **DONE** — two layers in `nester/sheet/pack.py`. (1) **Pre-flight**: `validate_part()` rejects the contours jagua-rs unwraps on (<3 distinct points, zero area, non-finite) into `NestResult.invalid` → the job's `errors[]`, named per file, rest of the job proceeds; all-invalid → clean `NestError`. `_safe_strip_height()` closes the real root cause — jagua-rs seeds the strip at `Σarea/strip_height` then offsets it inward by `sep/2` per side, so a **small job on a tall sheet** collapses the seed (survives iff `Σarea/strip_height > gap`); the guard shortens the strip, or falls back to a deterministic shelf pack when no safe height exists. (2) **Containment**: the solve is wrapped to catch `BaseException` (re-raising `KeyboardInterrupt`/`SystemExit`/`GeneratorExit`) and convert to `NestError` naming sheet, margin, gap, rotation and part counts. | S |

### Tier 1 — speaking the trade + the cost line

| # | Gap | Fix |
|---|-----|-----|
| E7 | No calibre. | Calibre→mm table (negra / galvanizada differ); accept either everywhere. |
| E8 | Material-cost estimate. | Profile → kg/m, sheet → kg/m² tables. User sets price (per kg / tramo / sheet); report adds a cost line + per-piece breakdown. **An output line, not a quoting module** — quoting is Harriet. |
| E9 ✅ | One stock length per profile. | **DONE** — `StockSpec.extra_stock` takes the shop's remnants; FFD opens the smallest fitting retazo before buying a tramo (each usable once). Plan splits `new_bars_needed` from total bars, names the retazos used, draws mixed lengths. `/v1/nest` takes `extra_stock: [{profile, length_mm, label}]`; Harriet contract untouched. |
| E11 | Stock catalog is implicit. | Mexican stock catalog as data (PTR sizes, calibres, lámina 4x8/4x10/5x10, tramo 6 m); pre-fills jobs; the AI reads it. |

### Tier 2 — breadth, after first paying users

STEP input (E12, needs `cadquery-ocp` ~1GB → Render, never Vercel) · DXF beyond
the Fusion layer convention (E13) · mixed material/thickness per job (E10) ·
DWG (E14) · part-in-hole (E15) · remnant inventory (E16) · last-sheet re-nest
(E17) · FFD swap pass (E18) · per-part grain via API (E19) · warn gap<kerf (E20).

---

## 3. The AI layer (lives in nester-web)

**Model:** Claude — Sonnet for intake (fast, cheap), Opus for hard cases
(parse failures, BOM reconciliation, explaining odd results). Prompt-cache the
system prompt + stock catalog.

**The one non-negotiable rule: the AI never produces a number.** Every
quantity — length, count, kilos, cost — comes from an engine tool call. The AI
gathers parameters, explains results, and refuses when it can't be sure.

Tools: `extract_parts` (→ parts + QA flags), `get_stock_catalog`,
`set_job_params` (writes the visible spec panel — never hidden state),
`run_nest`, `estimate_cost`, `flag_for_review` (the refusal path).

Behavior: **asks, doesn't assume** (won't nest until stock length, kerf,
dead-zone are set — asked in shop terms); **refuses loudly** (bent tube,
unreadable file, mismatch → stop; one scrapped batch loses the shop);
**speaks the trade** (PTR, solera, calibre — Spanish written by someone in the
industry); **everything it understood is visible and editable**.

---

## 4. UI

The risk isn't looking cheap — it's looking like a **black box**. Layout:
**conversation left, state right.**

- Left — chat. Drop files, type shop-speak.
- Right top — **spec panel**: every parameter as editable fields; editing a
  field = telling the AI. Makes the AI auditable instead of magic.
- Right bottom — **live nest preview**, bars/sheets drawn out. The single
  biggest trust element: a drawn 6 m bar with pieces on it reads as
  engineering; a number alone reads as a guess.

Credibility: dense/numeric/tabular, monospace figures; job history with IDs
and dates; **their logo on the PDF** (they hand it to their client — retention
hook); a clear statement of what happens to their files.

---

## 5. Infrastructure

| Concern | Choice | Note |
|---------|--------|------|
| Frontend + AI | Next.js on **Vercel** (`nester-web`) | Streaming chat, auth. |
| Engine service | **Existing Render deployment** (this repo) | Shared with Harriet — same URL, per-client keys (A3). Split only if load ever demands it. |
| Jobs/worker | **In-process on the web service** (today) → Render background worker (when scale demands) | E1/A5 shipped in-process: no DB to add, no second service to pay for. Trade-off (documented in `service/v1/jobs.py`): a restart mid-solve loses the run, and this cannot scale to >1 instance. |
| DB | **Neon Postgres** | Users, jobs, params, results, revisions, catalog. |
| Files | **Cloudflare R2** — separate bucket (or prefix) from Harriet | Enforced by A3/A4. |
| Secrets | Doppler | Add a `nester-web` project. |
| Auth | Clerk | Vercel Marketplace. |
| Payments | Later; manual at first | Validate before wiring Stripe. |

---

## 6. Sequencing — fastest credible launch

> **STATUS (2026-08-24)** — launched. Phase 0 ✅ (`/v1` with health/extract/
> nest-tube/uploads/downloads, per-client keys, Harriet compat frozen).
> Phase 1 partial: E2 ✅ E4 ✅ (E3/E5/E6 pending). Phase 2 ✅ LIVE at
> https://harriet-nester.vercel.app — repo `snickerbar7/harriet-nester`
> (this repo is `snickerbar7/nester-engine`): own auth, Neon, R2 via
> presigned URLs, Sonnet 5 tool loop (cached system prompt + Mexican
> catalog; the AI never produces a number), two-pane UI restyled to the
> Claude Design canvas (saved under that repo's `design/`), SVG nest view,
> artifact downloads. In progress app-side: Historial, Máquinas presets,
> provenance badges, Retazos v1 (inventory only). **E9 ✅ landed in the engine**
> (nesting against retazos — `extra_stock` on `/v1/nest`), so the design's
> "usar retazo" is now a real engine capability the app can wire up.
>
> **Close-out, later 2026-08-24:** everything above marked "in progress" is
> live at **https://nester.harriet.com.mx** — Historial/Máquinas/provenance/
> Retazos, E9 wired end-to-end (remnants consumed in production, lifecycle
> libre→apartado→usado), per-turn AI usage metering (from the API's own
> usage object), design round-2 UI (JobTabs, new composer), the cut-plan
> PDF rebuilt to the "Plan de corte" artboard, and i18n (es-MX default and
> source of truth, en second; the AI and the PDF follow the user's locale;
> regional stock catalogs deferred as their own phase). Design sync flow:
> canvas → handoff export → diff vs the web repo's `design/` snapshot →
> implement deltas → promote snapshot. Next engine contracts, in order:
> angle/bisel detection (design negotiates 45° ends), E8 weight (unblocks
> peso/kg across UI+PDF), mixed placas (E13), STEP (E12), 2D async (E1/A5).
>
> **2D async landed (E1/A5), 2026-08-24.** The engine side of sheet nesting is
> now callable from the web product: `POST /v1/jobs` (202) · `GET /v1/jobs/{id}`
> (status · progress · result · artifacts) · `DELETE /v1/jobs/{id}` (cancel),
> with `nester/sheet/pack.py` reporting progress per finished sheet and checking
> a cancel flag between sheets. `POST /v1/extract` and the nest result now carry
> **real part contours** (outer + holes, decimated, ≤200 pts/loop), so the
> WorkspaceLamina / ResultadosLamina screens can draw true silhouettes and the
> CALCULANDO state has real numbers behind it (`sheets_done`,
> `sheets_total_estimate`, `parts_placed`, `parts_total`, `elapsed_s`, plus the
> per-sheet utilization the chips show). Remaining app-side work: wire the poll
> loop, the cancel button and the SVG nest view. Next engine contracts, in
> order: angle/bisel detection, E8 weight, mixed placas (E13), STEP (E12).
>
> **E21 — engine panic containment, 2026-08-24.** A real customer 2D job died
> in ~1 s with a raw `pyo3_runtime.PanicException` out of jagua-rs. Root cause
> was *not* the thin solera it looked like: jagua-rs seeds its strip rectangle
> at `Σ(item area × demand) / strip_height` and offsets it inward by
> `min_items_separation / 2` per side, so **any** small job on a tall sheet
> (`Σarea/strip_height ≤ gap`) empties the seed before the first placement —
> measured exactly at `Σarea = gap × strip_height`. Item offsets go *outward*,
> so thin parts were never at risk. Fixed in `nester/sheet/pack.py` with a
> pre-flight guard plus a `BaseException` boundary on the solve; the sheet
> nester now has no path that ends in an unreadable Rust string.

**Phase 0 — Boundary (days).** A1–A4 + second API key on the existing Render
service. A5 slipped past launch (tube-first) and landed 2026-08-24. ✅

**Phase 1 — Trust minimum (days).** E2 + E4 first (small), then E6. Gate on
letting a stranger run a job.

**Phase 2 — nester-web MVP (week).** New repo: Clerk auth, Neon, R2 upload,
AI intake loop, two-pane UI, results page. **Tube-only at launch** (sync,
instant solves); 2D follows once A5/E1 land — they have. Billing manual.

**Phase 3 — 2D + polish.** ~~Async jobs~~ ✅ (E1/A5), sheet nesting in the UI,
E3 persistence, E5, calibre/catalog (E7, E11), cost line (E8). *(E9 done.)*

**Phase 4 — Commercial.** Stripe MX (account ready: acct Nester, CLI
authenticated, official Stripe skills installed in the web repo). **Billing
model decided 2026-08-24, measured against real AI cost ($0.134 USD list /
~MX$2.5 per full job, 87% of input cache-served):**
- Unit: **1 crédito = 1 anidado** (each run_nest). Conversation is free,
  backstopped by a fair-use daily turn cap.
- **Gratis:** 8 créditos/mes, full-quality deliverables, no watermarks.
- **Taller: MX$499/mes**, 100 créditos/mes.
- **Extra: 50 créditos = MX$149** (one-time packs, roll over while
  subscribed). Card + OXXO + SPEI. Factura manual at first (Stripe MX has
  no CFDI); PAC automation later.
- Enforcement is honest and in-product: balance visible in the status bar,
  HARRIET announces depletion in shop language, the nest gate refuses.
Build order: after the 2D/async design round. White-label PDF stays a
future paid differentiator. Anything quoting-shaped ships in Harriet.

### What NOT to build here, ever

Quoting workflow, factura/PAC, customer/price management — Harriet. And not
yet: remnant inventory, part-in-hole, DWG, solver optimality, WhatsApp.
