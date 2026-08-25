"""`/v1` — the neutral contract.

snake_case, the engine's own vocabulary (`bars_needed`, `yield_pct`,
`stock_length_mm`), units spelled out in the field names. This is what
nester-web and any future client speak; it is shaped like the engine, not like
any one caller.

Sync vs async is decided by how long the solver actually runs, not by taste:

  * **Tube (1D) is synchronous** — First Fit Decreasing solves instantly, so
    `POST /v1/nest` returns the plan in the response.
  * **Sheet (2D) is a job** — a solve is `time_per_sheet` seconds per sheet,
    several sheets, i.e. minutes. It goes through `POST /v1/jobs` (202) +
    `GET /v1/jobs/{id}` + `DELETE /v1/jobs/{id}`; `POST /v1/nest` with
    mode=sheet answers 501 and points there. See `jobs.py` for how jobs run,
    what survives a restart, and why it is built the way it is.

`POST /v1/extract` is synchronous in both modes (reading geometry is fast), and
in sheet mode it returns each part's REAL contour so a client can draw the
silhouette rather than a bounding rectangle.
"""
