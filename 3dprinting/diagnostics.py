"""Post-run diagnostics for the support-generation pipeline.

Walks every downward-facing facet and flags those the pipeline left
unsupported.  For each orphan, tags the most likely reason so that
tuning decisions (tighten SEVERE_OVERHANG_NZ? widen non-display band?
allow display-side corner supports?) can be made against the whole
population of failures, not one failed print at a time.

Typical usage:

    result = part_pipeline.process_part(spec)
    report = diagnostics.find_orphan_facets(result, grid_spacing=4.0)
    print(diagnostics.summarize(report))
    diagnostics.add_orphans_to_doc(result, report)   # visual overlay
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Optional

import FreeCAD
import Mesh
from FreeCAD import Vector

from orientation import (
    DOWNWARD_NZ_THRESHOLD, SEVERE_OVERHANG_NZ,
    compute_non_display_threshold, NON_DISPLAY_BAND_FRACTION,
)


# Classification tags for orphaned facets.  Ordered roughly by priority:
# if a facet matches multiple reasons, the first one wins.
ORPHAN_DISPLAY_SIDE = "display-side"           # filtered out by non-display band
ORPHAN_DISPLAY_SIDE_MILD = "display-side-mild" # display-side AND in scorer's blind spot
ORPHAN_NON_DISPLAY_GAP = "non-display-gap"     # passed band filter but no contact landed
ORPHAN_TINY = "tiny"                           # area below size-of-interest threshold


@dataclass
class OrphanFacet:
    """One downward-facing facet that received no support contact nearby."""
    facet_index: int
    centroid_world: tuple          # (x, y, z)
    centroid_part: tuple           # (x, y, z) — lets you see WHERE on the part
    normal_z_world: float
    area: float
    nearest_contact_dist_xy: float # inf if no contacts
    classification: str            # one of ORPHAN_* tags above
    in_scorer_blind_spot: bool     # -0.6 < nz < -0.2 at render time


@dataclass
class OrphanReport:
    """Collected findings from one diagnostic pass."""
    part_name: str
    total_downward_facets: int
    total_downward_area: float
    orphans: list
    supported_area: float
    orphan_area_by_class: dict = field(default_factory=dict)

    @property
    def orphan_count(self):
        return len(self.orphans)

    @property
    def orphan_area(self):
        return sum(o.area for o in self.orphans)


# ---------------------------------------------------------------------------
# Core analysis
# ---------------------------------------------------------------------------

def find_orphan_facets(result, grid_spacing=4.0, min_interesting_area=0.5):
    """Return an OrphanReport for one PartResult.

    A downward-facing facet (nz < DOWNWARD_NZ_THRESHOLD) is considered
    supported if there is at least one support contact whose XY position
    is within `grid_spacing` of the facet centroid.  This mirrors the
    rasterizer's own reachability assumption (one contact per grid cell).

    Facets with area < min_interesting_area are still counted but tagged
    ORPHAN_TINY so they don't dominate the headline numbers.
    """
    spec = result.spec
    oriented = result.oriented_mesh     # world frame (post-rotation, post-shift)
    contacts = result.contacts or []

    # To classify display-side vs non-display, we need part-frame positions
    # of each facet centroid.  The world frame = rotation.multVec(part) + shift.
    # We don't have 'shift' stored explicitly, but we can recover part-frame
    # coords by undoing the rotation from world centroids ... EXCEPT the
    # Z-shift (model raise + shift-to-plate) would make untransformed Y
    # unreliable.  Simpler: load the source mesh again in part frame.
    from part_pipeline import load_mesh
    mesh_part = load_mesh(spec)
    facets_part = mesh_part.Facets
    facets_world = oriented.Facets
    if len(facets_part) != len(facets_world):
        raise ValueError(
            f"[{spec.name}] part/world mesh facet count mismatch "
            f"({len(facets_part)} vs {len(facets_world)}); diagnostics skipped"
        )

    # Non-display band threshold in part frame.
    ndp = Vector(*spec.non_display_dir) if spec.non_display_dir else Vector(0, 0, -1)
    if ndp.Length > 1e-9:
        ndp.normalize()
        nd_threshold, _ = compute_non_display_threshold(
            mesh_part, spec.non_display_dir, band_mm=spec.band_mm,
            band_fraction=NON_DISPLAY_BAND_FRACTION,
        )
    else:
        nd_threshold = float('-inf')   # no filter

    # Build XY list of contacts for nearest-neighbor query (linear scan — fine
    # at typical contact counts of 10-200).
    contact_xy = [(c.x, c.y) for c in contacts]

    def nearest_contact_dist(x, y):
        if not contact_xy:
            return float('inf')
        best = float('inf')
        for cx, cy in contact_xy:
            d2 = (cx - x) ** 2 + (cy - y) ** 2
            if d2 < best:
                best = d2
        return math.sqrt(best)

    orphans = []
    total_down = 0
    total_down_area = 0.0
    supported_area = 0.0

    for i in range(len(facets_world)):
        fw = facets_world[i]
        nx, ny, nz = fw.Normal
        if nz > DOWNWARD_NZ_THRESHOLD:
            continue
        area = fw.Area
        if area == 0.0:
            continue
        pts_w = fw.Points
        cwx = (pts_w[0][0] + pts_w[1][0] + pts_w[2][0]) / 3.0
        cwy = (pts_w[0][1] + pts_w[1][1] + pts_w[2][1]) / 3.0
        cwz = (pts_w[0][2] + pts_w[1][2] + pts_w[2][2]) / 3.0

        total_down += 1
        total_down_area += area

        dist = nearest_contact_dist(cwx, cwy)
        if dist <= grid_spacing:
            supported_area += area
            continue

        # Orphan.  Now classify.
        fp = facets_part[i]
        pts_p = fp.Points
        cpx = (pts_p[0][0] + pts_p[1][0] + pts_p[2][0]) / 3.0
        cpy = (pts_p[0][1] + pts_p[1][1] + pts_p[2][1]) / 3.0
        cpz = (pts_p[0][2] + pts_p[1][2] + pts_p[2][2]) / 3.0

        on_display_side = False
        if ndp.Length > 1e-9:
            proj = cpx * ndp.x + cpy * ndp.y + cpz * ndp.z
            on_display_side = proj < nd_threshold

        in_scorer_blind = SEVERE_OVERHANG_NZ < nz < DOWNWARD_NZ_THRESHOLD

        if area < min_interesting_area:
            klass = ORPHAN_TINY
        elif on_display_side and in_scorer_blind:
            klass = ORPHAN_DISPLAY_SIDE_MILD
        elif on_display_side:
            klass = ORPHAN_DISPLAY_SIDE
        else:
            klass = ORPHAN_NON_DISPLAY_GAP

        orphans.append(OrphanFacet(
            facet_index=i,
            centroid_world=(cwx, cwy, cwz),
            centroid_part=(cpx, cpy, cpz),
            normal_z_world=nz,
            area=area,
            nearest_contact_dist_xy=dist,
            classification=klass,
            in_scorer_blind_spot=in_scorer_blind,
        ))

    area_by_class = {}
    for o in orphans:
        area_by_class[o.classification] = area_by_class.get(o.classification, 0.0) + o.area

    return OrphanReport(
        part_name=spec.name,
        total_downward_facets=total_down,
        total_downward_area=total_down_area,
        orphans=orphans,
        supported_area=supported_area,
        orphan_area_by_class=area_by_class,
    )


# ---------------------------------------------------------------------------
# Human-readable summary
# ---------------------------------------------------------------------------

def summarize(report):
    """Return a short multi-line summary of the report."""
    lines = []
    lines.append(f"[{report.part_name}] orphan-facet diagnostic")
    lines.append(f"  downward facets: {report.total_downward_facets} "
                 f"(total {report.total_downward_area:.1f} mm^2)")
    lines.append(f"  supported:       {report.supported_area:.1f} mm^2 "
                 f"({_pct(report.supported_area, report.total_downward_area)}%)")
    lines.append(f"  orphans:         {report.orphan_count} "
                 f"({report.orphan_area:.1f} mm^2, "
                 f"{_pct(report.orphan_area, report.total_downward_area)}%)")
    if report.orphan_area_by_class:
        for klass in (ORPHAN_DISPLAY_SIDE_MILD, ORPHAN_DISPLAY_SIDE,
                      ORPHAN_NON_DISPLAY_GAP, ORPHAN_TINY):
            area = report.orphan_area_by_class.get(klass, 0.0)
            if area > 0:
                count = sum(1 for o in report.orphans if o.classification == klass)
                lines.append(f"    {klass:<22} {count:3d} facets, {area:7.1f} mm^2")

    # Surface the worst offenders so the user can go look at them.
    if report.orphans:
        worst = sorted(report.orphans, key=lambda o: -o.area)[:5]
        lines.append("  worst orphans (by area):")
        for o in worst:
            lines.append(
                f"    area={o.area:6.2f}  nz={o.normal_z_world:+.2f}  "
                f"world=({o.centroid_world[0]:+6.1f},{o.centroid_world[1]:+6.1f},{o.centroid_world[2]:6.2f})  "
                f"part=({o.centroid_part[0]:+6.1f},{o.centroid_part[1]:+6.1f},{o.centroid_part[2]:+6.1f})  "
                f"[{o.classification}]"
            )
    return "\n".join(lines)


def _pct(num, denom):
    if denom <= 0:
        return " —"
    return f"{100.0 * num / denom:5.1f}"


# ---------------------------------------------------------------------------
# FreeCAD overlay
# ---------------------------------------------------------------------------

def add_orphans_to_doc(result, report, doc=None, prefix=None):
    """Create Mesh::Feature objects (one per classification) containing only
    the orphan facets.  View them in the FreeCAD GUI alongside the support
    compound to see exactly what was missed and why.
    """
    if doc is None:
        doc = FreeCAD.ActiveDocument
    if doc is None:
        return {}
    prefix = prefix or result.spec.name
    oriented = result.oriented_mesh

    by_class = {}
    for o in report.orphans:
        by_class.setdefault(o.classification, []).append(o)

    created = {}
    for klass, facets in by_class.items():
        sub = Mesh.Mesh()
        # Append the facets to a fresh mesh.  We build a raw triangle list
        # because Mesh.Mesh doesn't expose a direct copy-facet-by-index API.
        tris = []
        for orf in facets:
            pts = oriented.Facets[orf.facet_index].Points
            tris.append([
                (pts[0][0], pts[0][1], pts[0][2]),
                (pts[1][0], pts[1][1], pts[1][2]),
                (pts[2][0], pts[2][1], pts[2][2]),
            ])
        sub.addFacets(tris)
        obj = doc.addObject("Mesh::Feature", f"{prefix}_orphans_{klass.replace('-', '_')}")
        obj.Mesh = sub
        created[klass] = obj

    doc.recompute()
    return created
