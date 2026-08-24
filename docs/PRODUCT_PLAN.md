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
| A5 | Async job API in `/v1` (`POST /v1/jobs` → 202, `GET /v1/jobs/{id}`). | 2D solves run minutes; can't sit behind a web request. Tube jobs are instant and MAY stay sync in v1. |

---

## 2. Engine — gaps by tier

### Tier 0 — blocks charging anyone (trust)

The pitch is "you don't need to understand the software" — so the user CANNOT
sanity-check output the way a CAM operator would. The engine must be unable to
be confidently wrong.

| # | Gap | Fix | Effort |
|---|-----|-----|--------|
| E1 | 2D `/nest` is synchronous (minutes of solve). | A5 async jobs; background worker; job row in Postgres. | M |
| E2 | Bent tubes silently return chord length as cut length. | Compare measured cross-section vs filename profile; minor extents exceed nominal → not straight → **raise**. | **S** |
| E3 | 2D nesting is wall-clock stochastic; re-runs give different sheet counts. | Persist the nest result as source of truth; re-nest only as an explicit new revision. | S |
| E4 | `except Exception: pass` around IGES-nest and DXF-per-sheet writes → missing deliverables with no explanation. | Return reason in `warnings[]`; AI must surface it. | **S** |
| E5 | Profile comes only from filename; `Part1.igs` fails or garbage-buckets. | Fall back to grouping by measured cross-section; AI/user names and confirms each group. | M |
| E6 | No per-part QA signal for the AI to relay. | Every part carries `{measured_cross_section, matches_profile, straight, source_entities}`; AI surfaces mismatches before nesting. | M |

### Tier 1 — speaking the trade + the cost line

| # | Gap | Fix |
|---|-----|-----|
| E7 | No calibre. | Calibre→mm table (negra / galvanizada differ); accept either everywhere. |
| E8 | Material-cost estimate. | Profile → kg/m, sheet → kg/m² tables. User sets price (per kg / tramo / sheet); report adds a cost line + per-piece breakdown. **An output line, not a quoting module** — quoting is Harriet. |
| E9 | One stock length per profile. | `StockSpec` takes a set of lengths (+ optional remnants); FFD picks per bar. |
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
| Jobs/worker | Render background worker | For E1/A5 (2D). Tube can launch sync. |
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
> provenance badges, Retazos v1 (inventory only). Next engine contracts,
> in order: E9 remnant/multi-length stock (the design's "usar retazo"),
> angle/bisel detection (design negotiates 45° ends), E8 weight, mixed
> placas (E13), STEP (E12).

**Phase 0 — Boundary (days).** A1–A4 + second API key on the existing Render
service. A5 can slip if launch is tube-first.

**Phase 1 — Trust minimum (days).** E2 + E4 first (small), then E6. Gate on
letting a stranger run a job.

**Phase 2 — nester-web MVP (week).** New repo: Clerk auth, Neon, R2 upload,
AI intake loop, two-pane UI, results page. **Tube-only at launch** (sync,
instant solves); 2D follows once A5/E1 land. Billing manual.

**Phase 3 — 2D + polish.** Async jobs, sheet nesting in the UI, E3
persistence, E5, calibre/catalog (E7, E11), cost line (E8/E9).

**Phase 4 — Commercial.** Stripe MX, white-label PDF, plans — only after real
usage. Anything quoting-shaped ships in Harriet instead.

### What NOT to build here, ever

Quoting workflow, factura/PAC, customer/price management — Harriet. And not
yet: remnant inventory, part-in-hole, DWG, solver optimality, WhatsApp.
