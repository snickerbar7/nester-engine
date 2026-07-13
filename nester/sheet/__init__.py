"""2D flat (sheet) nesting for laser/plasma/waterjet-cut parts.

Sibling to the 1D ``nester.tube`` package. Where the tube tool packs straight
tubes end-to-end on bars, this packs irregular flat shapes onto rectangular
stock sheets, maximizing material yield.

Pipeline::

    DXF files ──▶ extract contours ──▶ group by material ──▶ nest on sheets ──▶ report
                  (outer + holes,       (thickness/material)  (spyrrow /        (PDF + nested
                   arcs flattened)                             sparrow engine)    DXF per sheet)

The heavy nesting is delegated to ``spyrrow`` (the Rust sparrow/jagua-rs
irregular-strip-packing engine); everything else — DXF reading, the multi-sheet
bin-packing wrapper, and output — lives here in pure Python + shapely.
"""
