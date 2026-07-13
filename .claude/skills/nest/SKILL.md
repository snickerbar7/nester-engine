---
name: nest
description: Run a tube-nesting / cutting-stock job. Fire when the user wants to nest tubes, compute a cut plan, work out how many stock bars they need, or generate a cut-plan PDF from IGES files. Takes IGES files/dir + stock length + saw allowances, produces output/<job>/<job>_Plan_de_Corte.pdf + <job>_corte.json (and solids with --solids), and returns a summary.
---

# Run a nesting job

You are the UI layer for Nester. The user gives you a set of IGES files and
their stock/saw parameters; you run the tool and hand back a summary plus written
artifacts.

## 1. Gather inputs

You need:
- **Input path(s)** — files, a glob, or a directory of `.igs`/`.iges` files.
- **Stock length (mm)** — required. One length per profile; ask for per-profile
  overrides only if the user mentions different bar lengths.
- **Kerf (mm)** — saw cut width. Default 0 if the user doesn't say, but ask once.
- **Front trim / back trim (mm)** — clamp dead zone + far-end remnant. Default 0.

If any of stock length / kerf / trims are missing and the user hasn't given a
"just use sensible defaults" steer, ask once, concisely, then proceed. Don't
re-ask every run — if they gave params earlier in the conversation, reuse them.

## 2. Run

```bash
cd ~/Documents/Nester
.venv/bin/python -m nester.tube <inputs> \
  --stock-length <mm> [--stock PROFILE=mm ...] \
  --kerf <mm> --front-trim <mm> --back-trim <mm> \
  --out output --name <job-name> --lang es
```

Pick a clear `<job-name>` (slug from what the user called the job, else the input
folder name). `--lang es` (default) makes a Spanish cut order; use `en` if the
user wants English. Pieces-per-file are read from the filename (`_4pz`); pass
`--no-qty` to disable, or `--profile-regex` / `--qty-regex` for a different
naming convention. Output lands at
`output/<job>/<job>_Plan_de_Corte.pdf` + `<job>_corte.json`.

Add `--shop-package` to also export **what the shop cuts**: one clean IGES per
unique part + `manifest.csv` (a 3D tube laser nests these itself — pre-nested
files are non-standard; see `/solid-export`). `--solids` (+ `--spare N`) adds a
nested solid *preview* per bar — for eyeballing only, not the shop file. Both
need `.venv-cad`. The cut-plan PDF is the user's procurement doc (how many bars
to buy).

## 3. Verify the parse before trusting numbers

On the **first** run against real files, sanity-check the extracted lengths in
`output/<job>/<job>_corte.json` against any part lengths the user knows. If `read_tube`
raised "recognized no point-bearing geometry … entity types present: [...]",
switch to the `/add-parser-support` flow — do not report a guessed result.

## 4. Show the result

- Render the PDF to an image so you can eyeball it and confirm it looks right:
  `qlmanage -t -s 1800 -o /tmp output/<job>/<job>_Plan_de_Corte.pdf` →
  `/tmp/<job>_Plan_de_Corte.pdf.png`, then read it. (QuickLook renders the
  font; the raw PDF→image harness preview does not.)
- Send the PDF to the user with SendUserFile (the cut plan is the deliverable).
- In chat, give the headline: total stock bars, per-profile bar count + yield,
  and flag any `unplaceable` parts (too long for stock).

## Notes

- Mixed profiles in one folder are grouped automatically by filename.
- Everything is mm internally; the IGES unit flag is honored.
- If the user wants to compare scenarios (e.g. 6 m vs 12 m stock), run twice with
  different `--name` and contrast the bar counts / yield.
