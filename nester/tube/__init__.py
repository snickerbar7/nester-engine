"""nester.tube — 1D cutting-stock nesting for straight tubes from IGES files."""

from .model import Part, StockSpec, ExtraStock, ProfileResult, BarLayout, Placement
from .packing import pack_profile, pack_all
from .profile import profile_from_filename, normalize_profile
from .iges import read_tube, TubeGeometry

__all__ = [
    "Part", "StockSpec", "ExtraStock", "ProfileResult", "BarLayout", "Placement",
    "pack_profile", "pack_all", "profile_from_filename", "normalize_profile",
    "read_tube", "TubeGeometry",
]
