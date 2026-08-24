"""HTTP service layer that exposes the Nester (tube + sheet) engines.

The CLI (`nester.tube` / `nester.sheet`) stays the source of truth for the
algorithms; this package is a thin FastAPI wrapper that:

  * downloads input CAD files from Cloudflare R2 (S3 API) into a temp dir,
    preserving the ORIGINAL filename (profile + qty are parsed from it),
  * runs the existing extract / pack pipelines,
  * uploads the shop artifacts (PDF / nested DXF / IGES) back to R2 and returns
    their object keys for the caller to register however it likes.

Layout — one deployment, several contracts:

  core/       shared internals: client registry + key scoping, R2 I/O, engine
              orchestration returning the NATIVE snake_case result.
  v1/         the neutral contract every new client speaks.
  harriet/    frozen compat shim for the first client (camelCase NestResult).
  app.py      entrypoint; mounts both routers.

Nothing here is business-specific: it takes files + specs, returns geometry +
plans. Object keys are opaque strings chosen by the caller. Deterministic
engine, no model, no DB.
"""
