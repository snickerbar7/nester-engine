"""Command-line entry point for flat-sheet nesting.

    python -m nester.sheet parts/*.dxf --sheet 2440x1220 --material steel \
        --thickness 2 --margin 8 --gap 3 --rotate free --out output --name job

Inputs may be DXF files, globs, or directories (searched for .dxf). Each file's
outer contour(s) become parts; quantity per file is read from the filename
(e.g. ``bracket_4pz.dxf``) or defaults to 1. All parts nest as one stock group
(single material/thickness) in this MVP.
"""

from __future__ import annotations

import argparse
import datetime
import glob
import json
import os
import sys
from typing import List

from ..materials import density_for, material_label
from .dxf_read import DxfReadError, read_parts
from .model import ExtraSheet, FlatPart, NestResult, SheetSpec
from .pack import (
    DEFAULT_MAX_NEW_SHEETS, DEFAULT_MIN_HOLE_SIDE, ROTATION_MODES, NestError, nest,
)
from .report import _slug, _part_base, write_reports

# quantity-from-filename (and the juegos multiplier) are shared with the tube tool.
try:
    from nester.tube.profile import (
        DEFAULT_QTY_REGEX,
        parse_sets_args,
        quantity_from_filename,
        resolve_qty,
    )
except Exception:  # pragma: no cover - keep nester.sheet usable standalone
    DEFAULT_QTY_REGEX = r"[_\-](?P<qty>\d+)\s*(?:pz|pcs|pza|x)\b"

    def quantity_from_filename(path: str, pattern: str = DEFAULT_QTY_REGEX) -> int:
        import re
        m = re.search(pattern, os.path.splitext(os.path.basename(path))[0], re.IGNORECASE)
        return max(int(m.group("qty")), 1) if m else 1

    def parse_sets_args(items) -> dict:
        out = {}
        for item in items or []:
            key, _, val = item.rpartition("=")
            out[key.strip()] = max(int(val), 1)
        return out

    def resolve_qty(path, pattern=DEFAULT_QTY_REGEX, sets=None):
        qty = quantity_from_filename(path, pattern) if pattern else 1
        n = max(int((sets or {}).get(os.path.basename(path), 1)), 1)
        return qty, n, qty * n


def main(argv: List[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    paths = _expand_inputs(args.inputs)
    if not paths:
        print("No .dxf files found in the given inputs.", file=sys.stderr)
        return 2

    sw, sh = _parse_sheet(args.sheet)
    try:
        sets = parse_sets_args(getattr(args, "sets", []))
    except ValueError as e:
        raise SystemExit(str(e))
    for name in sets:
        if not any(os.path.basename(p) == name or p == name for p in paths):
            print(f"  ! --sets for '{name}' ignored: no such file in this job", file=sys.stderr)
    parts, errors, warnings = _load_parts(
        paths, None if args.no_qty else args.qty_regex, sets)
    for e in errors:
        print(f"  ! {e}", file=sys.stderr)
    for w in warnings:
        print(f"  ~ {w}", file=sys.stderr)
    if not parts:
        print("No parts could be read.", file=sys.stderr)
        return 1

    # Notes about the JOB's setup. Terminal only, deliberately: ``warnings[]`` in
    # the JSON means "an artifact could not be produced" (E4), and the plan
    # already raises both of these from the result itself — a "no kilos" card
    # and an "unused retazo" card. Duplicating them here would make a clean run
    # look like a failed one.
    job_notes: List[str] = []
    remnants = _parse_remnants(args.remnant)
    density, density_note = _resolve_density(args.material, args.density)
    if density_note:
        job_notes.append(density_note)
    elif density and not args.thickness:
        job_notes.append("sin espesor no hay kilos: pasa --thickness MM")
    for note in job_notes:
        print(f"  ~ {note}", file=sys.stderr)

    spec = SheetSpec(width=sw, height=sh, material=args.material, thickness=args.thickness,
                     margin=args.margin, part_gap=args.gap, density=density)
    try:
        result = nest(parts, spec, rotation=args.rotate, time_per_sheet=args.time,
                      seed=args.seed, extra_sheets=remnants,
                      nest_in_holes=args.nest_in_holes,
                      min_remnant=args.min_remnant,
                      minimize_sheets=args.minimize_sheets,
                      max_new_sheets=args.max_new_sheets,
                      search_budget_s=args.search_budget,
                      min_hole_side=args.min_hole_side)
    except NestError as e:
        print(str(e), file=sys.stderr)
        return 1

    # A retazo for a job that never needed it is information, never an error —
    # same rule the tube tool applies to bar remnants.
    for unused in result.remnants_unused:
        note = (f"retazo {unused.label} ({unused.width:g}×{unused.height:g}) "
                f"no se usó: sigue en el rack")
        job_notes.append(note)
        print(f"  ~ {note}", file=sys.stderr)

    # Parts the engine refused (degenerate contours) ride the same errors[]
    # channel as unreadable files — named, never silently dropped.
    for msg in result.messages:
        errors.append(msg)
        print(f"  ! {msg}", file=sys.stderr)

    if args.json:
        from .report import _as_dict
        print(json.dumps(_as_dict(result, args.name or "nest", _meta(args)), indent=2, ensure_ascii=False))
    else:
        print(_format_report(result, len(errors)))

    if args.out:
        job_name = args.name or _default_job_name(args.inputs)
        out_dir = os.path.join(args.out, job_name)
        # Attempt the per-sheet DXF write BEFORE the JSON so a failure (E4)
        # can be recorded as a warning IN the JSON, not silently dropped. A
        # failure here must not abort the job — the cut-plan PDF + JSON stand.
        # Rejected parts also go into the written artifacts, so the reason
        # survives past the terminal session.
        out_warnings: List[str] = list(result.messages)
        dxf_files: List[str] = []
        if not args.no_dxf:
            from .dxf_out import write_all_sheets
            os.makedirs(out_dir, exist_ok=True)
            try:
                dxf_files = write_all_sheets(result, out_dir, _slug(job_name))
            except Exception as e:
                out_warnings.append(f"nested DXF-per-sheet not written: {e}")
        files = write_reports(result, out_dir, job_name, _meta(args), warnings=out_warnings)
        files += dxf_files
        print("\nWrote:")
        for f in files:
            print(f"  {f}")
        for w in out_warnings:
            print(f"  ! warning: {w}", file=sys.stderr)
    return 0


def _meta(args) -> dict:
    return {
        "generated": datetime.datetime.now().strftime("%Y-%m-%d %H:%M"),
        "lang": args.lang,
        "rotation": args.rotate,
        # traceability: the plan prints the settings a re-run needs to reproduce it
        "seed": args.seed,
        "time_per_sheet": args.time,
        "nest_in_holes": bool(args.nest_in_holes),
        "min_remnant": args.min_remnant,
        "minimize_sheets": bool(args.minimize_sheets),
        "max_new_sheets": args.max_new_sheets,
        "min_hole_side": args.min_hole_side,
        # Reported, never applied: kerf compensation is the CAM's job. It rides
        # the plan so the shop can check the gap really does clear the kerf.
        "kerf": args.kerf,
    }


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="nester.sheet", description="2D flat-sheet nesting.")
    p.add_argument("inputs", nargs="+", help="DXF files, globs, or directories")
    p.add_argument("--sheet", default="2440x1220", metavar="WxH",
                   help="stock sheet size in mm, e.g. 2440x1220 (default 4x8 ft)")
    p.add_argument("--material", default="", help="material name (steel/alu/…) — labels the group")
    p.add_argument("--thickness", type=float, default=0.0, help="sheet thickness (mm)")
    p.add_argument("--margin", type=float, default=8.0, help="edge margin, all sides (mm)")
    p.add_argument("--gap", type=float, default=3.0, help="minimum part-to-part spacing (mm); keep >= kerf")
    p.add_argument("--rotate", choices=sorted(ROTATION_MODES), default="free",
                   help="rotation policy: free / grain (0,180) / fixed (0) / ortho (0,90,180,270)")
    p.add_argument("--time", type=int, default=4, metavar="SEC",
                   help="compute budget per sheet in seconds (more = better yield)")
    p.add_argument("--seed", type=int, default=0, help="RNG seed for reproducible nests")
    p.add_argument("--qty-regex", default=DEFAULT_QTY_REGEX,
                   help="regex with a 'qty' group for pieces-per-file (e.g. _4pz)")
    p.add_argument("--no-qty", action="store_true", help="treat every file as a single part")
    p.add_argument("--sets", action="append", default=[], metavar="FILENAME=N",
                   help="juegos: multiply that file's quantity by N (repeatable). "
                        "'PIEZA_2pz.dxf=50' -> 2 x 50 = 100 pieces to cut.")
    p.add_argument("--json", action="store_true", help="emit machine-readable JSON to stdout")
    p.add_argument("--out", metavar="DIR", help="write PDF + JSON + nested DXFs into DIR/<job>/")
    p.add_argument("--name", help="job name for the output folder + report title")
    p.add_argument("--lang", choices=("es", "en"), default="es", help="report language (default es)")
    p.add_argument("--no-dxf", action="store_true", help="skip the nested DXF-per-sheet output")
    p.add_argument("--remnant", action="append", default=[], metavar="WxH[:LABEL]",
                   help="retazo: a sheet offcut already on the rack, usable once "
                        "(repeatable). '1200x600:R-0007'. Spent smallest-first "
                        "before any new sheet is bought.")
    p.add_argument("--min-remnant", type=float, default=200.0, metavar="MM",
                   help="shortest side worth reclaiming from a sheet's leftover; "
                        "the plan reports that rectangle as a retazo candidate. "
                        "0 turns the reporting off (default 200)")
    p.add_argument("--nest-in-holes", action="store_true",
                   help="also nest small parts INSIDE the holes of placed parts. "
                        "Off by default: those parts come out inside a slug, so "
                        "the operator has to be told (the plan says so).")
    p.add_argument("--minimize-sheets", dest="minimize_sheets",
                   action=argparse.BooleanOptionalAction, default=True,
                   help="search for the FEWEST new sheets the job fits in, instead "
                        "of walking greedily until the parts run out (default on). "
                        "--no-minimize-sheets restores the old greedy loop.")
    p.add_argument("--max-new-sheets", type=int, default=DEFAULT_MAX_NEW_SHEETS,
                   metavar="N",
                   help=f"ceiling the sheet search will never look past "
                        f"(default {DEFAULT_MAX_NEW_SHEETS}). Past it the greedy "
                        f"answer is returned, flagged as capped.")
    p.add_argument("--search-budget", type=float, default=0.0, metavar="SEC",
                   help="wall-clock cap for the WHOLE sheet search; the best "
                        "feasible nest found so far is returned. 0 = no cap.")
    p.add_argument("--min-hole-side", type=float, default=DEFAULT_MIN_HOLE_SIDE,
                   metavar="MM",
                   help=f"shortest side a void must have to be worth nesting into "
                        f"(default {DEFAULT_MIN_HOLE_SIDE:g}). A bolt hole is not "
                        f"usable surface.")
    p.add_argument("--kerf", type=float, default=0.0, metavar="MM",
                   help="the machine's kerf, REPORTED in the plan so the gap can be "
                        "checked against it. Kerf compensation itself is the CAM's "
                        "job — the nest never applies it.")
    p.add_argument("--density", type=float, default=None, metavar="KG_M3",
                   help="material density in kg/m³, for the weight lines. Normally "
                        "resolved from --material; pass this for an alloy the "
                        "table does not know. Without either, no kilos are reported.")
    return p


def _parse_remnants(items: List[str]) -> List[ExtraSheet]:
    """``WxH`` or ``WxH:LABEL`` -> ExtraSheet, labelled R-000n when unnamed.

    Labels must be unique: the label is what the plan tells the operator to pull
    off the rack, so two retazos answering to "R-7" is a picking error waiting
    to happen.
    """
    out: List[ExtraSheet] = []
    seen: set[str] = set()
    for i, raw in enumerate(items or [], start=1):
        spec, _, label = raw.partition(":")
        label = label.strip() or f"R-{i:04d}"
        try:
            w, h = _parse_sheet(spec)
        except SystemExit:
            raise SystemExit(
                f"--remnant expects WxH[:LABEL] (e.g. 1200x600:R-0007), got '{raw}'")
        if w <= 0 or h <= 0:
            raise SystemExit(f"--remnant '{raw}': both sides must be > 0")
        if label in seen:
            raise SystemExit(f"--remnant '{raw}': label '{label}' is used twice")
        seen.add(label)
        out.append(ExtraSheet(width=w, height=h, label=label))
    return out


def _resolve_density(material: str, override: float | None) -> tuple[float, str | None]:
    """(density, note) — an explicit value wins, else the material table, else 0.

    A density of 0 is not a failure, it is an honest "unknown": the plan then
    reports no kilos at all rather than a number nobody can defend.
    """
    if override is not None:
        if override <= 0:
            raise SystemExit(f"--density must be > 0, got {override:g}")
        return override, None
    if not material:
        return 0.0, ("sin material no hay densidad: el plan no reporta kilos "
                     "(usa --material o --density)")
    d = density_for(material)
    if d is None:
        return 0.0, (f"material '{material}' no está en la tabla de densidades: "
                     f"el plan no reporta kilos (usa --density KG_M3)")
    return d, None


def _parse_sheet(text: str) -> tuple[float, float]:
    m = text.lower().replace(" ", "").split("x")
    if len(m) != 2:
        raise SystemExit(f"--sheet expects WxH (e.g. 2440x1220), got '{text}'")
    try:
        return float(m[0]), float(m[1])
    except ValueError:
        raise SystemExit(f"--sheet expects numbers, got '{text}'")


def _expand_inputs(inputs: List[str]) -> List[str]:
    out: List[str] = []
    for raw in inputs:
        if os.path.isdir(raw):
            for root, _dirs, files in os.walk(raw):
                out += [os.path.join(root, f) for f in files if f.lower().endswith(".dxf")]
        elif any(ch in raw for ch in "*?["):
            out += [g for g in glob.glob(raw) if g.lower().endswith(".dxf")]
        elif os.path.isfile(raw):
            out.append(raw)
    seen: set[str] = set()
    uniq: List[str] = []
    for p in sorted(out):
        if p not in seen:
            seen.add(p)
            uniq.append(p)
    return uniq


def _load_parts(paths: List[str], qty_regex, sets=None):
    """Read every DXF into FlatParts.

    ``sets`` (juegos, keyed by path or basename) multiplies the filename-parsed
    quantity of EVERY part in that file — a multi-part DXF scales as a set.
    """
    parts: List[FlatPart] = []
    errors: List[str] = []
    warnings: List[str] = []
    for path in paths:
        name = os.path.basename(path)
        _from_name, _sets, qty = resolve_qty(path, qty_regex, sets)
        try:
            file_parts = read_parts(path, qty=qty)
        except (DxfReadError, OSError) as e:
            errors.append(f"{name}: {e}")
            continue
        warnings += list(getattr(read_parts, "last_warnings", []))
        parts += file_parts
    return parts, errors, warnings


def _default_job_name(inputs: List[str]) -> str:
    first = inputs[0].rstrip("/\\")
    base = os.path.basename(first) or "nest"
    base = base.split("*")[0].split("?")[0] or "nest"
    return os.path.splitext(base)[0] or "nest"


def _format_report(result: NestResult, errors_count: int) -> str:
    spec = result.spec
    lines: List[str] = []
    placed = sum(s.part_count for s in result.sheets)
    # Net first, gross beside it — the same hierarchy the plan prints. Net
    # discounts the offcut that goes back on the rack, so a job that used
    # retazos is not punished for it.
    header = (f"Flat nesting — {result.sheet_count} sheet(s) @ {spec.width:g}×{spec.height:g}mm"
              f"  ·  net {result.net_yield_pct:.1f}% / gross {result.yield_pct:.1f}%"
              f"  ·  {placed} part(s)")
    if errors_count:
        header += f"  ({errors_count} file(s) skipped)"
    # What to BUY is the number procurement acts on; it only differs from the
    # sheet count once retazos are in play, so only say it then.
    if result.remnants_used:
        header += (f"\n    buy {result.new_sheets_needed} new sheet(s)"
                   f"  ·  retazos used: {', '.join(result.remnants_used)}")
    for s in result.sheets:
        tag = "" if not s.is_remnant else f"  [{s.source} {s.spec.width:g}×{s.spec.height:g}]"
        holes = f"  ·  {s.in_hole_count} in holes" if s.in_hole_count else ""
        drop = ""
        if s.leftover:
            drop = f"  ·  offcut {s.leftover.width:.0f}×{s.leftover.height:.0f}"
        lines.append(f"    sheet {s.index + 1}: {s.part_count} part(s)"
                     f"  ·  util {s.utilization * 100:.1f}%{tag}{holes}{drop}")
    if result.can_weigh:
        label = material_label(spec.material) or spec.material
        lines.append(f"    weight ({label} {spec.thickness:g}mm): "
                     f"{result.parts_weight_kg:.1f} kg in parts  ·  "
                     f"{result.new_stock_weight_kg:.1f} kg to buy  ·  "
                     f"{result.drop_weight_kg:.1f} kg drop")
    if result.unplaceable:
        names = ", ".join(f"{_part_base(p.name)}({p.size[0]:.0f}×{p.size[1]:.0f})"
                          for p in result.unplaceable)
        lines.append(f"    ⚠ too big for sheet: {names}")
    return header + "\n" + "\n".join(lines)


if __name__ == "__main__":
    raise SystemExit(main())
