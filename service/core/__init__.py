"""Shared internals behind every contract the service exposes.

Nothing in here knows about a specific caller. It is the engine's own
vocabulary and nothing else:

  * ``auth``   — client registry (per-client API keys + key-prefix scoping),
  * ``r2``     — Cloudflare R2 object I/O; keys are OPAQUE strings from the caller,
  * ``engine`` — orchestration over ``nester.tube`` / ``nester.sheet``, returning
    the NATIVE result shape (snake_case, engine vocabulary: ``bars_needed``,
    ``yield_pct``, ``stock_length_mm``, ...).

Contract packages (``service.v1``, ``service.harriet``) adapt this shape; they
never re-implement it.
"""
