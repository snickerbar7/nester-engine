"""Command-line entry point for tube nesting.

    python -m nester.tube samples/*.igs --stock-length 6000 --kerf 2 --front-trim 30

Inputs may be files, globs, or directories (searched for .igs/.iges). Profiles
are read from filenames; stock length is per profile (one global default, with
optional per-profile overrides).
"""

from __future__ import annotations

import argparse
import datetime
import glob
import json
import os
import sys
from typing import Dict, List

from .iges import IgesParseError, check_straight, read_tube
from .model import ExtraStock, Part, ProfileResult, StockSpec
from .packing import pack_all
from .profile import (
    DEFAULT_PROFILE_REGEX,
    DEFAULT_QTY_REGEX,
    MAX_SETS,
    ProfileParseError,
    normalize_profile,
    parse_profile_dims,
    parse_sets_args,
    profile_from_filename,
    resolve_qty,
)
from .report import write_reports

_IGES_EXTS = (".igs", ".iges")

# Why a remnant offered to the job was never opened, in the shop's words.
_REASON_ES = {
    "no_gain": "abrirlo no habría quitado ningún tramo de la compra",
    "no_fit": "ninguna pieza del trabajo cabe en él",
    "too_small_for_trims": "más corto que las zonas muertas",
    "job_ended": "el trabajo terminó antes",
}


def main(argv: List[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    paths = _expand_inputs(args.inputs)
    if not paths:
        print("No .igs/.iges files found in the given inputs.", file=sys.stderr)
        return 2

    qty_regex = None if args.no_qty else args.qty_regex
    sets = parse_sets(getattr(args, "sets", []))
    for name in sets:
        if not any(os.path.basename(p) == name or p == name for p in paths):
            print(f"  ! --sets for '{name}' ignored: no such file in this job",
                  file=sys.stderr)
    parts, errors, cross_sections = _load_parts(paths, args.profile_regex, qty_regex, sets)
    for e in errors:
        print(f"  ! {e}", file=sys.stderr)
    if not parts:
        print("No parts could be read.", file=sys.stderr)
        return 1

    specs = _build_specs(parts, args)
    try:
        results = pack_all(parts, specs, minimize_bars=args.minimize_bars)
    except KeyError as e:
        print(str(e), file=sys.stderr)
        return 1

    # A remnant the job did not need is information, never an error — same rule
    # the flat-sheet tool applies to retazos de lámina.
    for r in results:
        for e in r.remnants_unused:
            why = _REASON_ES.get(r.remnant_reasons.get(e.label, ""), "no se usó")
            print(f"  ~ sobrante {e.label} ({e.length:g} mm, {r.profile}): "
                  f"{why} — sigue en el rack", file=sys.stderr)

    if args.json:
        print(json.dumps(_as_dict(results), indent=2))
    else:
        print(_format_report(results, errors_count=len(errors)))

    if args.out:
        job_name = args.name or _default_job_name(args.inputs)
        out_dir = os.path.join(args.out, job_name)
        meta = {
            "generated": datetime.datetime.now().strftime("%Y-%m-%d %H:%M"),
            "kerf": args.kerf,
            "front_trim": args.front_trim,
            "back_trim": args.back_trim,
            "min_remnant": args.min_remnant,
            "minimize_bars": bool(args.minimize_bars),
            "lang": args.lang,
        }
        # Attempt the IGES nest-layout write BEFORE the JSON so a failure (E4)
        # can be recorded as a warning IN the JSON, rather than silently
        # dropping the file with no trace. A failure here must not abort the
        # job — the cut-plan PDF + JSON are the deliverable that matters most.
        warnings: List[str] = []
        iges_path = None
        if not args.no_iges:
            from .iges_nest import write_nest_iges
            from .report import _slug
            os.makedirs(out_dir, exist_ok=True)
            iges_path = os.path.join(out_dir, f"{_slug(job_name)}_nest.igs")
            try:
                write_nest_iges(results, iges_path, cross_sections)
            except Exception as e:
                warnings.append(f"IGES nest-layout not written ({os.path.basename(iges_path)}): {e}")
                iges_path = None
        files = write_reports(results, out_dir, job_name, meta, warnings=warnings)
        if iges_path:
            files.append(iges_path)
        print("\nWrote:")
        for f in files:
            print(f"  {f}")
        for w in warnings:
            print(f"  ! warning: {w}", file=sys.stderr)
        if args.shop_package:
            _run_cad("[shop]", out_dir, job_name, paths,
                     lambda py, script, slug, nest_json, src: [
                         py, script, "--shop-package", "--src", src,
                         "--out", os.path.join(out_dir, slug)])
        if args.solids:
            _run_cad("[solids]", out_dir, job_name, paths,
                     lambda py, script, slug, nest_json, src: [
                         py, script, "--nest", nest_json, "--src", src,
                         "--out", os.path.join(out_dir, slug), "--spare", str(args.spare)])
    return 0


def _run_cad(label, out_dir, job_name, paths, build_cmd) -> None:
    """Invoke the OpenCASCADE exporter (solid_nest.py) in the .venv-cad env."""
    import subprocess
    from .report import _slug

    # __file__ is nester/tube/cli.py -> three levels up is the repo root.
    repo = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    py = os.path.join(repo, ".venv-cad", "bin", "python")
    script = os.path.join(repo, "solid_nest.py")
    if not os.path.exists(py):
        print(f"\n{label} skipped — CAD env not found. Set it up once:")
        print("  /opt/homebrew/bin/python3.13 -m venv .venv-cad && "
              ".venv-cad/bin/pip install cadquery-ocp")
        return
    slug = _slug(job_name)
    nest_json = os.path.join(out_dir, f"{slug}_corte.json")
    src_dir = os.path.dirname(os.path.abspath(paths[0]))
    print(f"\n{label} exporting via CAD kernel …")
    r = subprocess.run(build_cmd(py, script, slug, nest_json, src_dir),
                       capture_output=True, text=True)
    for line in r.stdout.splitlines():
        if line.strip() and not line.startswith("\x1b"):
            print(f"  {line}")
    if r.returncode != 0:
        print(f"  {label} failed (exit {r.returncode}). stderr tail:")
        print("   " + "\n   ".join(r.stderr.splitlines()[-3:]))


def _default_job_name(inputs: List[str]) -> str:
    first = inputs[0].rstrip("/\\")
    base = os.path.basename(first) or "nest"
    # strip glob/extension noise
    base = base.split("*")[0].split("?")[0] or "nest"
    return os.path.splitext(base)[0] or "nest"


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="nester.tube", description="1D tube nesting / cutting-stock.")
    p.add_argument("inputs", nargs="+", help="IGES files, globs, or directories")
    p.add_argument("--stock-length", type=float, required=True,
                   help="default stock bar length (mm) for all profiles")
    p.add_argument("--stock", action="append", default=[], metavar="PROFILE=MM",
                   help="per-profile stock length override (repeatable)")
    p.add_argument("--remnant", action="append", default=[], metavar="PROFILE=MM[:LABEL]",
                   help="a leftover piece (retazo) available as extra stock; used "
                        "before buying a new bar (repeatable). LABEL defaults to R-000n.")
    p.add_argument("--min-remnant", type=float, default=200.0, metavar="MM",
                   help="shortest drop worth keeping: at or above it a bar's "
                        "leftover is a recoverable SOBRANTE (and is discounted "
                        "from the net yield), below it MERMA. Not the same thing "
                        "as --back-trim, which is the chuck dead zone. "
                        "0 turns the split off (default 200)")
    p.add_argument("--minimize-bars", dest="minimize_bars",
                   action=argparse.BooleanOptionalAction, default=True,
                   help="open a remnant only when opening it removes a tramo "
                        "from the purchase order (default on); anything that "
                        "would buy nothing stays on the rack, reported with "
                        "reason 'no_gain'. --no-minimize-bars restores the old "
                        "unconditional spending.")
    p.add_argument("--kerf", type=float, default=0.0, help="saw kerf per cut (mm)")
    p.add_argument("--front-trim", type=float, default=0.0, help="clamp/loading dead zone (mm)")
    p.add_argument("--back-trim", type=float, default=0.0, help="far-end dead zone / min remnant (mm)")
    p.add_argument("--profile-regex", default=DEFAULT_PROFILE_REGEX,
                   help="regex with a 'profile' group to read from filenames")
    p.add_argument("--qty-regex", default=DEFAULT_QTY_REGEX,
                   help="regex with a 'qty' group for pieces-per-file (e.g. _4pz)")
    p.add_argument("--no-qty", action="store_true",
                   help="treat every file as a single part (ignore qty in filename)")
    p.add_argument("--sets", action="append", default=[], metavar="FILENAME=N",
                   help="juegos: multiply that file's quantity by N (repeatable). "
                        "'PIEZA_2pz.igs=50' -> 2 x 50 = 100 pieces to cut.")
    p.add_argument("--json", action="store_true", help="emit machine-readable JSON to stdout")
    p.add_argument("--out", metavar="DIR",
                   help="write the cut-plan PDF + JSON into DIR/<job-name>/")
    p.add_argument("--name", help="job name for the output folder + report title")
    p.add_argument("--lang", choices=("es", "en"), default="es",
                   help="report language (default es)")
    p.add_argument("--no-iges", action="store_true",
                   help="skip the IGES nest-layout output")
    p.add_argument("--shop-package", action="store_true",
                   help="export per-part IGES + manifest.csv for the laser shop (needs .venv-cad)")
    p.add_argument("--solids", action="store_true",
                   help="also export a nested solid preview per bar (needs .venv-cad)")
    p.add_argument("--spare", type=int, default=0, metavar="N",
                   help="with --solids: also write N spare uncut stock-tube file(s) per profile")
    return p


def _expand_inputs(inputs: List[str]) -> List[str]:
    out: List[str] = []
    for raw in inputs:
        if os.path.isdir(raw):
            for root, _dirs, files in os.walk(raw):
                out += [os.path.join(root, f) for f in files if f.lower().endswith(_IGES_EXTS)]
        elif any(ch in raw for ch in "*?["):
            out += [g for g in glob.glob(raw) if g.lower().endswith(_IGES_EXTS)]
        elif os.path.isfile(raw):
            out.append(raw)
    # de-dup, stable order
    seen: set[str] = set()
    uniq: List[str] = []
    for p in sorted(out):
        if p not in seen:
            seen.add(p)
            uniq.append(p)
    return uniq


def _load_parts(
    paths: List[str],
    profile_regex: str,
    qty_regex: str | None,
    sets: Dict[str, int] | None = None,
) -> tuple[List[Part], List[str], Dict[str, tuple]]:
    """Read every file into Part copies.

    ``sets`` is the juegos multiplier per file (keyed by path or basename): the
    file contributes ``qty_from_name x sets`` copies, so demand — and therefore
    the bars to buy — scales before the solver ever runs.
    """
    parts: List[Part] = []
    errors: List[str] = []
    cross_sections: Dict[str, tuple] = {}
    for path in paths:
        name = os.path.basename(path)
        try:
            profile = profile_from_filename(path, profile_regex)
        except ProfileParseError as e:
            errors.append(f"{name}: {e}")
            continue
        try:
            geo = read_tube(path)
        except (IgesParseError, OSError) as e:
            errors.append(f"{name}: {e}")
            continue
        try:
            check_straight(geo, parse_profile_dims(profile), path)
        except ValueError as e:
            errors.append(f"{name}: {e}")
            continue
        cross_sections.setdefault(profile, geo.cross_section)
        _from_name, _sets, qty = resolve_qty(path, qty_regex, sets)
        if qty == 1:
            parts.append(Part(name=name, profile=profile, length=geo.cut_length))
        else:
            for i in range(1, qty + 1):
                parts.append(Part(name=f"{name} #{i}/{qty}", profile=profile,
                                  length=geo.cut_length))
    return parts, errors, cross_sections


def _build_specs(parts: List[Part], args: argparse.Namespace) -> Dict[str, StockSpec]:
    overrides: Dict[str, float] = {}
    for item in args.stock:
        if "=" not in item:
            raise SystemExit(f"--stock expects PROFILE=MM, got '{item}'")
        key, val = item.split("=", 1)
        overrides[normalize_profile(key.strip())] = float(val)

    extra = _parse_remnants(getattr(args, "remnant", []))

    specs: Dict[str, StockSpec] = {}
    for profile in {p.profile for p in parts}:
        specs[profile] = StockSpec(
            profile=profile,
            stock_length=overrides.get(profile, args.stock_length),
            kerf=args.kerf,
            front_trim=args.front_trim,
            back_trim=args.back_trim,
            extra_stock=tuple(extra.get(profile, ())),
            min_remnant=getattr(args, "min_remnant", 0.0) or 0.0,
        )
    for profile in extra:
        if profile not in specs:
            print(f"  ! remnants for profile '{profile}' ignored: not in this job",
                  file=sys.stderr)
    return specs


def parse_sets(items: List[str]) -> Dict[str, int]:
    """--sets FILENAME=N -> {filename: n}. Bad input aborts with the reason."""
    try:
        return parse_sets_args(items)
    except ValueError as e:
        raise SystemExit(str(e))


def _parse_remnants(items: List[str]) -> Dict[str, List[ExtraStock]]:
    """--remnant PROFILE=MM[:LABEL] -> {profile: [ExtraStock, ...]}."""
    out: Dict[str, List[ExtraStock]] = {}
    for n, item in enumerate(items, start=1):
        if "=" not in item:
            raise SystemExit(f"--remnant expects PROFILE=MM[:LABEL], got '{item}'")
        key, val = item.split("=", 1)
        length, _, label = val.partition(":")
        try:
            mm = float(length)
        except ValueError:
            raise SystemExit(f"--remnant length must be a number, got '{length}'")
        try:
            piece = ExtraStock(length=mm, label=label.strip() or f"R-{n:04d}")
        except ValueError as e:
            raise SystemExit(f"--remnant {item}: {e}")
        out.setdefault(normalize_profile(key.strip()), []).append(piece)
    return out


# --------------------------------------------------------------------------- #
# Reporting
# --------------------------------------------------------------------------- #

def _format_report(results: List[ProfileResult], errors_count: int) -> str:
    lines: List[str] = []
    total_bars = 0
    for r in results:
        total_bars += r.bar_count
        lines.append("")
        extra = f"  (+ {len(r.remnants_used)} remnant(s))" if r.remnants_used else ""
        yields = (f"yield {r.yield_pct:.1f}%" if not r.reclaimable_length
                  else f"net {r.net_yield_pct:.1f}% / gross {r.yield_pct:.1f}%")
        lines.append(f"● Profile {r.profile}  —  {r.new_bars_needed} new bar(s) "
                     f"@ {r.spec.stock_length:g}mm{extra}"
                     f"  ·  {yields}")
        lines.append(f"  kerf {r.spec.kerf:g}  front-trim {r.spec.front_trim:g}  "
                     f"back-trim {r.spec.back_trim:g}  usable {r.spec.usable_length:g}mm")
        n_new = 0
        for bar in r.bars:
            cuts = ", ".join(f"{p.part.length:g}@{p.start:g}" for p in bar.placements)
            if bar.is_remnant:
                src = f"remnant {bar.source}"
            else:
                n_new += 1                      # numbered over the bars actually bought
                src = f"bar {n_new}"
            lines.append(f"    {src} ({bar.stock_length:g}mm): {cuts}"
                         f"  | drop {bar.remnant:g}mm")
        if r.unplaceable:
            names = ", ".join(f"{p.name}({p.length:g})" for p in r.unplaceable)
            lines.append(f"    ⚠ too long for stock: {names}")
    header = f"Tube nesting — {len(results)} profile(s), {total_bars} stock bar(s) total"
    if errors_count:
        header += f"  ({errors_count} file(s) skipped)"
    return header + "\n" + "\n".join(lines)


def _as_dict(results: List[ProfileResult]) -> dict:
    return {
        "profiles": [
            {
                "profile": r.profile,
                "bars": r.bar_count,
                "new_bars_needed": r.new_bars_needed,
                "remnants_used": r.remnants_used,
                "remnants_unused": [
                    {"label": e.label, "length": e.length,
                     "reason": r.remnant_reasons.get(e.label)}
                    for e in r.remnants_unused
                ],
                "stock_length": r.spec.stock_length,
                "yield_pct": round(r.yield_pct, 2),
                "net_yield_pct": round(r.net_yield_pct, 2),
                "gross_yield_pct": round(r.gross_yield_pct, 2),
                "reclaimable": round(r.reclaimable_length, 3),
                "waste": round(r.waste_length, 3),
                "layout": [
                    {
                        "bar": b.index + 1,
                        "stock_length": b.stock_length,
                        "source": b.source,
                        "remnant": round(b.remnant, 3),
                        "leftover": round(b.leftover, 3),
                        "waste": round(b.waste, 3),
                        "cuts": [
                            {"part": p.part.name, "length": p.part.length,
                             "start": round(p.start, 3), "end": round(p.end, 3)}
                            for p in b.placements
                        ],
                    }
                    for b in r.bars
                ],
                "unplaceable": [{"part": p.name, "length": p.length} for p in r.unplaceable],
            }
            for r in results
        ]
    }


if __name__ == "__main__":
    raise SystemExit(main())
