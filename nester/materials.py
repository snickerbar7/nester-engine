"""Material densities — the table that turns geometry into kilos (E8).

The engine refuses to *guess* a density: a shop that cuts 304 and 6061 on the
same machine gets a 3x error if the tool assumes "metal". So this module is a
lookup, not an estimator. Either the material name resolves to a known entry
(or the caller passes an explicit density) and the plan reports kilos, or it
does not and the plan says plainly that it cannot weigh the job.

Densities are kg/m3 at room temperature, from standard mill data. They are
nominal: real coil varies by ~1%, which is far below the tolerance anybody
buys steel at.

Resolution is deliberately conservative:

* the name is normalized (accents stripped, case folded, punctuation to spaces)
  because shops write "Acero Inoxidable", "acero inox.", "A. INOX 304";
* an alias only matches on a WHOLE-WORD boundary, so "acero" never matches
  inside some unrelated token;
* when several aliases match, the one ending FURTHEST RIGHT wins, and a longer
  alias breaks a tie. Trade names narrow left to right — "acero inoxidable
  **430**", "aluminio **6061**" — so the rightmost match is the most specific
  one. That is what keeps "acero inoxidable" from beating "acero" (7850 vs
  8000) and, just as importantly, keeps "acero inoxidable 430" from being read
  as 304: the grade is the last word, and the grade is the answer.

Nothing here knows about money. Price per kg is the caller's business (and
quoting is Harriet's, not this product's).
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple


@dataclass(frozen=True)
class Material:
    """One known material: canonical key, display label, density in kg/m3."""

    key: str
    label: str
    density: float          # kg/m3
    aliases: Tuple[str, ...] = ()

    def weight_kg(self, area_mm2: float, thickness_mm: float) -> float:
        return weight_kg(area_mm2, thickness_mm, self.density)


# --------------------------------------------------------------------------- #
# The catalog
# --------------------------------------------------------------------------- #
# Ordered by how often a Mexican fab shop actually cuts them. Aliases carry the
# trade names the shop types ("lamina negra", "cal 14 galvanizada", "A36").
MATERIALS: Tuple[Material, ...] = (
    Material(
        "acero", "Acero al carbón", 7850,
        ("acero", "acero al carbon", "acero al carbono", "acero negro",
         "lamina negra", "negra", "hierro", "fierro", "steel", "mild steel",
         "carbon steel", "a36", "astm a36", "1018", "hr", "cr",
         "rolado en caliente", "rolado en frio", "hot rolled", "cold rolled"),
    ),
    Material(
        "galvanizado", "Acero galvanizado", 7850,
        ("galvanizado", "galvanizada", "lamina galvanizada", "galva", "galv",
         "galvanized", "galvanized steel", "zintro", "zintroalum"),
    ),
    Material(
        "inoxidable", "Acero inoxidable", 8000,
        ("inoxidable", "acero inoxidable", "inox", "acero inox", "a inox",
         "stainless", "stainless steel", "304", "304l", "316", "316l", "301",
         "ss304", "ss316", "aisi 304", "aisi 316"),
    ),
    Material(
        "inoxidable_430", "Acero inoxidable 430", 7700,
        ("430", "ss430", "aisi 430", "inoxidable 430", "inox 430",
         "ferritico", "ferritica"),
    ),
    Material(
        "aluminio", "Aluminio", 2700,
        ("aluminio", "alum", "alu", "aluminum", "aluminium", "6061", "6063",
         "5052", "3003", "1100", "al 6061", "al 5052"),
    ),
    Material(
        "cobre", "Cobre", 8960,
        ("cobre", "copper", "cu", "c110"),
    ),
    Material(
        "laton", "Latón", 8500,
        ("laton", "brass", "c260", "cartucho"),
    ),
    Material(
        "bronce", "Bronce", 8800,
        ("bronce", "bronze", "sae 64", "c932"),
    ),
    Material(
        "titanio", "Titanio", 4510,
        ("titanio", "titanium", "ti", "grado 2", "gr2"),
    ),
    Material(
        "acrilico", "Acrílico", 1180,
        ("acrilico", "acrylic", "pmma", "plexiglas", "plexiglass"),
    ),
    Material(
        "policarbonato", "Policarbonato", 1200,
        ("policarbonato", "polycarbonate", "pc", "lexan"),
    ),
    Material(
        "mdf", "MDF", 750,
        ("mdf", "fibracel", "tablero de fibra"),
    ),
    Material(
        "triplay", "Triplay", 600,
        ("triplay", "contrachapado", "plywood", "madera"),
    ),
)

_BY_KEY: Dict[str, Material] = {m.key: m for m in MATERIALS}

# (normalized alias, material). Order here does not decide the winner —
# :func:`find_material` scores every match by where it lands in the input.
_ALIAS_INDEX: List[Tuple[str, Material]] = []


def _normalize(text: str) -> str:
    """Fold to plain lowercase ASCII words separated by single spaces.

    'Lámina  INOX-304' -> 'lamina inox 304'. Accents are stripped because half
    the shop types them and half does not; punctuation becomes whitespace so
    'inox.' and 'inox' are the same token.
    """
    s = unicodedata.normalize("NFKD", str(text or ""))
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    s = re.sub(r"[^0-9a-zA-Z]+", " ", s).strip().lower()
    return re.sub(r"\s+", " ", s)


for _m in MATERIALS:
    for _a in (_m.key,) + _m.aliases:
        _ALIAS_INDEX.append((_normalize(_a), _m))


def find_material(name: str) -> Optional[Material]:
    """Resolve a free-text material name, or None when it is not recognized.

    Every alias that appears as a whole word run is scored, and the one ending
    FURTHEST RIGHT wins (longer alias breaks a tie). A trade name narrows as it
    goes — "acero inoxidable 430", "aluminio 6061", "lámina inox 316" — so the
    last thing said is the most specific thing said. Scoring by alias length
    alone gets this wrong in the expensive direction: "acero inoxidable" is a
    longer string than "inoxidable 430", so 430 stock would be weighed as 304.

    None is a real answer and callers must honor it: it means "this job cannot
    be weighed", not "assume steel".
    """
    norm = _normalize(name)
    if not norm:
        return None
    padded = f" {norm} "
    best: Optional[Tuple[int, int, Material]] = None
    for alias, material in _ALIAS_INDEX:
        pos = padded.rfind(f" {alias} ")
        if pos < 0:
            continue
        # Rightmost END of the match, then the longer alias.
        score = (pos + len(alias), len(alias))
        if best is None or score > best[:2]:
            best = (score[0], score[1], material)
    return best[2] if best else None


def density_for(name: str) -> Optional[float]:
    """Density in kg/m3 for a material name, or None if unknown."""
    m = find_material(name)
    return m.density if m else None


def material_label(name: str) -> Optional[str]:
    m = find_material(name)
    return m.label if m else None


def by_key(key: str) -> Optional[Material]:
    return _BY_KEY.get(_normalize(key).replace(" ", "_"))


def weight_kg(area_mm2: float, thickness_mm: float, density_kg_m3: float) -> float:
    """Mass of a flat piece: area x thickness x density, in kg.

    ``area_mm2`` is NET area (outer minus holes) for a part, or the full
    rectangle for a stock sheet. Returns 0.0 when any input is missing, so a
    job with no thickness or no known density simply reports no weight instead
    of reporting a wrong one.
    """
    if area_mm2 <= 0 or thickness_mm <= 0 or density_kg_m3 <= 0:
        return 0.0
    volume_m3 = (area_mm2 * 1e-6) * (thickness_mm * 1e-3)
    return volume_m3 * density_kg_m3


def known_materials() -> List[Tuple[str, str, float]]:
    """(key, label, density) for every entry — for a catalog endpoint or --help."""
    return [(m.key, m.label, m.density) for m in MATERIALS]
