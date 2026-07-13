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


class ProfileParseError(ValueError):
    pass


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
    return _normalize(raw)


def _normalize(raw: str) -> str:
    return raw.replace("X", "x").lower()
