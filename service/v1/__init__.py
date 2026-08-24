"""`/v1` — the neutral contract.

snake_case, the engine's own vocabulary (`bars_needed`, `yield_pct`,
`stock_length_mm`), units spelled out in the field names. This is what
nester-web and any future client speak; it is shaped like the engine, not like
any one caller.

Tube nesting is synchronous — FFD solves instantly. Sheet (2D) nesting is not
exposed here yet: those solves run for minutes and belong behind the async jobs
API (`POST /v1/jobs`), so `POST /v1/nest` with mode=sheet returns 501.
"""
