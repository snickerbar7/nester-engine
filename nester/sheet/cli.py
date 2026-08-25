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

from .dxf_read import DxfReadError, read_parts
from .model import FlatPart, NestResult, SheetSpec
from .pack import ROTATION_MODES, NestError, nest
from .report import _slug, _part_base, write_reports

# quantity-from-filename is shared with the tube tool.
try:
    from nester.tube.profile import DEFAULT_QTY_REGEX, quantity_from_filename
except Exception:  # pragma: no cover - keep nester.sheet usable standalone
    DEFAULT_QTY_REGEX = r"[_\-](?P<qty>\d+)\s*(?:pz|pcs|pza|x)\b"

    def quantity_from_filename(path: str, pattern: str = DEFAULT_QTY_REGEX) -> int:
        import re
        m = re.search(pattern, os.path.splitext(os.path.basename(path))[0], re.IGNORECASE)
        return max(int(m.group("qty")), 1) if m else 1


def main(argv: List[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    paths = _expand_inputs(args.inputs)
    if not paths:
        print("No .dxf files found in the given inputs.", file=sys.stderr)
        return 2

    sw, sh = _parse_sheet(args.sheet)
    parts, errors, warnings = _load_parts(paths, None if args.no_qty else args.qty_regex)
    for e in errors:
        print(f"  ! {e}", file=sys.stderr)
    for w in warnings:
        print(f"  ~ {w}", file=sys.stderr)
    if not parts:
        print("No parts could be read.", file=sys.stderr)
        return 1

    spec = SheetSpec(width=sw, height=sh, material=args.material, thickness=args.thickness,
                     margin=args.margin, part_gap=args.gap)
    try:
        result = nest(parts, spec, rotation=args.rotate, time_per_sheet=args.time, seed=args.seed)
    except NestError as e:
        print(str(e), file=sys.stderr)
        return 1

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
        out_warnings: List[str] = []
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
    p.add_argument("--json", action="store_true", help="emit machine-readable JSON to stdout")
    p.add_argument("--out", metavar="DIR", help="write PDF + JSON + nested DXFs into DIR/<job>/")
    p.add_argument("--name", help="job name for the output folder + report title")
    p.add_argument("--lang", choices=("es", "en"), default="es", help="report language (default es)")
    p.add_argument("--no-dxf", action="store_true", help="skip the nested DXF-per-sheet output")
    return p


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


def _load_parts(paths: List[str], qty_regex):
    parts: List[FlatPart] = []
    errors: List[str] = []
    warnings: List[str] = []
    for path in paths:
        name = os.path.basename(path)
        qty = quantity_from_filename(path, qty_regex) if qty_regex else 1
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
    header = (f"Flat nesting — {result.sheet_count} sheet(s) @ {spec.width:g}×{spec.height:g}mm"
              f"  ·  yield {result.yield_pct:.1f}%  ·  {placed} part(s)")
    if errors_count:
        header += f"  ({errors_count} file(s) skipped)"
    for s in result.sheets:
        lines.append(f"    sheet {s.index + 1}: {s.part_count} part(s)  ·  util {s.utilization * 100:.1f}%")
    if result.unplaceable:
        names = ", ".join(f"{_part_base(p.name)}({p.size[0]:.0f}×{p.size[1]:.0f})"
                          for p in result.unplaceable)
        lines.append(f"    ⚠ too big for sheet: {names}")
    return header + "\n" + "\n".join(lines)


if __name__ == "__main__":
    raise SystemExit(main())
