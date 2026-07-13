"""HTTP service layer that exposes the Nester (tube + sheet) engines to Harriet.

The CLI (`nester.tube` / `nester.sheet`) stays the source of truth for the
algorithms; this package is a thin FastAPI wrapper that:

  * downloads input CAD files from Cloudflare R2 (S3 API) into a temp dir,
    preserving the ORIGINAL filename (profile + qty are parsed from it),
  * runs the existing extract / pack pipelines,
  * maps the tube result onto Harriet's `NestResult` contract
    (packages/domain/src/nesting/types.ts) so the 1D `cut_plan` card is unchanged,
  * uploads the shop artifacts (PDF / nested DXF / IGES) back to R2 and returns
    their object keys for Harriet to register as record attachments.

Nothing here is business-specific: it takes files + specs, returns geometry +
plans. Deterministic engine, no model, no DB.
"""
