"""Derive a part's profile key from its IGES filename.

Profile identification is filename-driven. The convention is configurable via a
regex with a named group ``profile`` (or the first capture group). The default
matches common steel-section tokens like ``40x40x2``, ``50X30X3``, or a bare
diameter like ``D32`` / ``OD25.4`` anywhere in the stem.

Examples that match the default::

    bracket_40x40x2_A.igs       -> 40x40x2
    railOD25.4-part7.iges       -> OD25.4
    chassis-D32_left.igs        -> D32

If the default doesn't fit your shop's naming, pass --profile-regex on the CLI.
"""

from __future__ import annotations

import os
import re
from typing import Mapping, Optional, Tuple

# square/rect tube: NxN(xN)   |   round tube: D## / OD##   (optional decimals),
# plus an optional gauge/wall suffix like "_C18" — different wall = different
# stock, so it's part of the profile key when present.
DEFAULT_PROFILE_REGEX = (
    r"(?P<profile>"
    r"\d+(?:\.\d+)?[xX]\d+(?:\.\d+)?(?:[xX]\d+(?:\.\d+)?)?(?:_C\d+)?"
    r"|O?D\d+(?:\.\d+)?(?:_C\d+)?"
    r")"
)

# quantity per file, e.g. "_4pz" / "_8PZ" / "-2 pcs"
DEFAULT_QTY_REGEX = r"[_\-](?P<qty>\d+)\s*(?:pz|pcs|pza|x)\b"

# JUEGOS (sets) — a per-FILE multiplier on the filename-parsed quantity.
#
# The filename says how many pieces ONE set of the drawing carries
# ("PIEZA_2pz.dxf" -> 2). It cannot say how many sets the shop is building: the
# customer's file is not renameable, and a shop that needs 100 of a file that
# says "_2pz" has no way to express it. So the multiplier is supplied
# out-of-band (`sets` on an API FileRef, `--sets FILE=N` on the CLI) and is
# NEVER parsed from the name:
#
#     effective qty = qty_from_name x sets     (2 pieces x 50 juegos = 100)
#
# It multiplies demand BEFORE nesting — raw material scales with it, which is
# the whole point.
#
# The bound is a VALIDATION range, not an arithmetic one: the multiplication
# itself is unbounded. It is 999 because that is the range the shop-facing UI
# offers ("Entrada válida 1–999"), and a number the product accepts and stores
# must not fail later at the engine. `sets` exists only on `/v1`; Harriet's
# frozen contract has no such field.
MAX_SETS = 999


class ProfileParseError(ValueError):
    pass


def sets_for_path(path: str, sets: Optional[Mapping[str, int]] = None) -> int:
    """The juegos multiplier for one file: keyed by full path, else by basename.

    Basename is the practical key — the API materializes uploads under their
    original filename, and that filename is what the user typed on ``--sets``.
    """
    if not sets:
        return 1
    raw = sets.get(path)
    if raw is None:
        raw = sets.get(os.path.basename(path))
    if raw is None:
        return 1
    return max(int(raw), 1)


def parse_sets_args(items) -> dict:
    """``["PIEZA_2pz.igs=50", ...]`` -> ``{"PIEZA_2pz.igs": 50}``.

    Shared by both CLIs (``--sets``, repeatable, mirroring ``--remnant``).
    Raises :class:`ValueError` on a malformed or out-of-range entry; the CLIs
    turn that into a ``SystemExit`` with the same message the API returns.
    """
    out: dict = {}
    for item in items or []:
        if "=" not in item:
            raise ValueError(f"--sets expects FILENAME=N, got '{item}'")
        key, val = item.rsplit("=", 1)
        name = key.strip()
        if not name:
            raise ValueError(f"--sets expects FILENAME=N, got '{item}'")
        try:
            n = int(val.strip())
        except ValueError:
            raise ValueError(f"--sets {name}: N must be a whole number, got '{val}'")
        if n < 1 or n > MAX_SETS:
            raise ValueError(f"--sets {name}: N must be between 1 and {MAX_SETS}, got {n}")
        out[name] = n
    return out


def resolve_qty(
    path: str,
    pattern: Optional[str] = DEFAULT_QTY_REGEX,
    sets: Optional[Mapping[str, int]] = None,
) -> Tuple[int, int, int]:
    """``(qty_from_name, sets, effective_qty)`` for one file.

    ``pattern=None`` means "ignore quantities in filenames" (the ``--no-qty``
    flag / ``qty_regex: null``), which pins ``qty_from_name`` to 1 — sets still
    applies, because it was supplied deliberately.
    """
    qty = quantity_from_filename(path, pattern) if pattern else 1
    n = sets_for_path(path, sets)
    return qty, n, qty * n


def quantity_from_filename(path: str, pattern: str = DEFAULT_QTY_REGEX) -> int:
    """Pieces represented by this file. Defaults to 1 when no count is present."""
    stem = os.path.splitext(os.path.basename(path))[0]
    m = re.search(pattern, stem, re.IGNORECASE)
    if not m:
        return 1
    try:
        n = int(m.groupdict().get("qty") or m.group(1))
    except (ValueError, IndexError):
        return 1
    return max(n, 1)


def profile_from_filename(path: str, pattern: str = DEFAULT_PROFILE_REGEX) -> str:
    """Extract the profile key from a filename using ``pattern``.

    Returns the ``profile`` named group if present, else group(1), normalized to
    lowercase 'x' separators so ``40X40X2`` and ``40x40x2`` group together.
    """
    stem = os.path.splitext(os.path.basename(path))[0]
    m = re.search(pattern, stem)
    if not m:
        raise ProfileParseError(
            f"Could not read a profile from '{stem}' using /{pattern}/. "
            f"Pass --profile-regex to match your naming convention."
        )
    raw = m.groupdict().get("profile") or m.group(1)
    return normalize_profile(raw)


def normalize_profile(raw: str) -> str:
    """Canonical profile key: lowercase, 'x' separators ('40X40X2' -> '40x40x2').

    Anything that keys a profile — parsed filenames, CLI ``--stock`` overrides,
    API-supplied remnant profiles — must go through this, or the same profile
    lands in two buckets.
    """
    return raw.replace("X", "x").lower()


_normalize = normalize_profile  # backwards-compatible alias


# Round profile: "d32" / "od25.4", optionally with a "_c##" gauge suffix.
_ROUND_DIMS_RE = re.compile(r"^o?d(?P<diam>\d+(?:\.\d+)?)")
# Square/rect profile: "40x40x2" / "50x30" (wall thickness, if present, is
# NOT a cross-section extent, so it's dropped here).
_RECT_DIMS_RE = re.compile(r"^(?P<a>\d+(?:\.\d+)?)x(?P<b>\d+(?:\.\d+)?)")


def parse_profile_dims(profile: str) -> Tuple[float, ...] | None:
    """Nominal cross-section dimensions encoded in a normalized profile key.

    Round profiles (``d32`` / ``od25.4``) return a single-element tuple, the
    diameter — compared against BOTH measured minor extents. Square/rect
    profiles (``40x40x2``) return ``(width, height)`` (the wall thickness, if
    present, isn't a cross-section extent and is ignored).

    Returns ``None`` if ``profile`` doesn't match either shape — callers
    should treat that as "can't check, skip" (gap E5), not an error.
    """
    m = _ROUND_DIMS_RE.match(profile)
    if m:
        return (float(m.group("diam")),)
    m = _RECT_DIMS_RE.match(profile)
    if m:
        return (float(m.group("a")), float(m.group("b")))
    return None
