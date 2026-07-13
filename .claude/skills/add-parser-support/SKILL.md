---
name: add-parser-support
description: Extend the IGES parser to handle a real CAD export it can't yet read. Fire when read_tube raises "recognized no point-bearing geometry" / "entity types present", when extracted lengths look wrong, or when the user points to a new IGES file from their CAD system and you need to confirm/extend parsing before nesting.
---

# Add IGES parser support for a real file

The parser (`nester/tube/iges.py`) extracts cut length as the longest
bounding-box axis from a known set of entity types: Line (110), Point (116),
Circular Arc (100), Rational B-Spline curve (126), Vertex List (502). Real CAD
exports may use other entities. This flow adds them safely.

## 1. See what the file actually contains

```bash
cd ~/Documents/Nester
.venv/bin/python - <<'PY'
import sys, collections
from nester.tube.iges import read_tube
path = "<REAL_FILE>"
try:
    g = read_tube(path)
    print("OK  length=", g.cut_length, "mm  cross=", g.cross_section,
          "unit_flag=", g.unit_flag, "pts=", g.point_count,
          "types=", g.entity_types_seen)
except Exception as e:
    print("PARSE FAILED:", e)
PY
```

If it parsed: **verify the length** against the part's known real length. If it
matches, you're done — nest it. If it's wrong (e.g. picked up a stray axis),
inspect the geometry below.

## 2. Inspect the entity mix

If it failed or looked wrong, dump the Directory Entry entity-type histogram so
you know which IGES types carry this file's geometry:

```bash
.venv/bin/python - <<'PY'
import collections
path = "<REAL_FILE>"
lines = open(path, errors="replace").read().splitlines()
d = [l for l in lines if len(l) >= 73 and l[72] == "D"]
types = collections.Counter()
for i in range(0, len(d)-1, 2):
    try: types[int(d[i][0:8])] += 1
    except: pass
print("entity types (IGES numbers):", dict(types))
PY
```

Common straight-tube carriers: 110 Line, 116 Point, 126 B-Spline curve, 502
Vertex List, 510 Face / 514 Shell / 186 MSBO (B-rep solids — geometry is in the
referenced 502 vertex lists / 126 curves), 144 Trimmed Surface, 128 B-Spline
surface (control points are coordinate triples).

## 3. Add a handler

For a new point-bearing type, add a branch to `_extract_points` in
`nester/tube/iges.py` that reads its coordinate triples from the parameter
tokens (`t[0]` is the repeated type number; data starts at `t[1]`). Look up the
entity's parameter layout in the IGES 5.3 spec. For surfaces (128) and solids,
extracting **control points / vertices** is enough — the bounding box only needs
the extreme points, not exact geometry.

If a real file is available, drop it in `samples/` and add a regression test that
asserts the expected length (mirror `tools/make_sample_iges.py` if you need a
synthetic stand-in for an entity type).

## 4. Re-verify

Re-run step 1, confirm the length is right, run the test suite
(`.venv/bin/python -m pytest -q`), then proceed to `/nest`.

## Guardrail

Never "fix" a wrong length by fudging numbers. If the geometry is ambiguous
(bent tube, multi-axis), say so and ask the user for the intended cut length —
this tool is straight-tubes-only by design.
