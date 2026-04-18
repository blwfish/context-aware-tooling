"""
Generic per-part 3D print pipeline.

Takes a part (mesh or Part.Shape) plus a direction that indicates which side
is NON-DISPLAY (where supports may contact).  Produces an oriented part with
supports and a raft, ready for STL export.

This is the "universal" path: no wall-specific conventions.  Works on any
geometry where the non-display region can be described by a single outward
direction (roof underside, slab top, figurine base, etc.).

Usage:
    from part_pipeline import PartSpec, process_part

    spec = PartSpec(
        stl_path="/path/to/roof_part_a.stl",
        non_display_dir=(0, 0, -1),   # underside is non-display
        name="RoofA",
    )
    result = process_part(spec, printer='m7_pro')
    # result.oriented_mesh, result.supports, result.raft, ...

"""

from __future__ import annotations

import math
import logging
from dataclasses import dataclass, field
from typing import Optional, Sequence

import FreeCAD
import Part
import Mesh
from FreeCAD import Vector

from orientation import (
    OrientationCandidate, OrientationResult,
    generate_candidates, pick_best_orientation, apply_rotation_to_mesh,
    _mesh_facet_normal_area, DOWNWARD_NZ_THRESHOLD,
    compute_non_display_threshold, NON_DISPLAY_BAND_FRACTION,
)
from support_utils import (
    Contact, build_tapered_support, build_raft, MODEL_RAISE,
    TIP_RADIUS, COLUMN_RADIUS, BASE_PAD_RADIUS, BASE_PAD_HEIGHT,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# PartSpec and result
# ---------------------------------------------------------------------------

@dataclass
class PartSpec:
    """Description of one part to process through the print pipeline.

    Parameters
    ----------
    stl_path : str or None
        Path to an STL file.  Either this or `mesh` must be provided.
    mesh : Mesh.Mesh or None
        A pre-loaded mesh.  Overrides stl_path if given.
    non_display_dir : tuple[float, float, float]
        Unit vector in the PART frame pointing outward from the non-display
        region.  Examples:
          - Roof with shingled top: (0, 0, -1) — bottom is non-display.
          - Slab whose top mates with another piece: (0, 0, +1) — top is
            non-display.
          - Building wall with display facing -Y: (0, +1, 0) — interior at +Y.
    name : str
        Label for the part (used in naming exported objects).
    candidates : list[OrientationCandidate] or None
        If None, uses the default candidate set.
    band_mm : float or None
        Override for the non-display band width (default 0.5 mm, tight
        enough for a flush mating face).  Parts with DEEP recessed
        mating features (tabs/recesses that go 2-3 mm into the part)
        need a larger band so the recessed interior still counts as
        non-display and gets supports.  E.g., for a slab with a 3 mm
        deep mating recess, set band_mm=3.5.
    """
    non_display_dir: Sequence[float] = (0.0, 0.0, -1.0)
    name: str = "part"
    stl_path: Optional[str] = None
    mesh: Optional[Mesh.Mesh] = None
    candidates: Optional[list] = None
    band_mm: Optional[float] = None


@dataclass
class PartResult:
    """Output of process_part()."""
    spec: PartSpec
    oriented_mesh: Mesh.Mesh
    support_compound: Optional[Part.Compound]
    raft_shape: Optional[Part.Shape]
    raft_top_z: float
    contacts: list
    orientation: OrientationResult


# ---------------------------------------------------------------------------
# Support contact generation
# ---------------------------------------------------------------------------

def collect_downward_facets(mesh_world, mesh_part, non_display_dir_part,
                            require_non_display=True,
                            band_mm=None, band_fraction=None):
    """Return list of (facet_index, centroid_world, normal_world, area) for
    downward overhangs on the non-display side.

    Non-display is classified by POSITION in the PART frame: a facet counts
    as non-display iff its centroid in the part frame projects within the
    outer band along `non_display_dir_part`.  The two meshes must have the
    same facet ordering (one is just the rotated+shifted version of the
    other).

    Parameters
    ----------
    mesh_world : Mesh.Mesh
        Mesh in the PRINT frame (after rotation and plate-shift).  Used to
        get world-frame centroids and normals.
    mesh_part : Mesh.Mesh
        Mesh in the PART frame (original).  Used for non-display position
        classification.
    non_display_dir_part : sequence[3]
        Non-display direction in the PART frame.
    require_non_display : bool
        If True, skip facets that aren't in the non-display region.
    band_fraction : float or None
        Override for NON_DISPLAY_BAND_FRACTION.
    """
    out = []
    nd_threshold, _ = compute_non_display_threshold(
        mesh_part, non_display_dir_part, band_mm=band_mm,
        band_fraction=band_fraction)
    ndp = Vector(non_display_dir_part[0], non_display_dir_part[1],
                 non_display_dir_part[2])
    if ndp.Length > 1e-9:
        ndp.normalize()

    facets_w = mesh_world.Facets
    facets_p = mesh_part.Facets
    n_facets = len(facets_w)
    if n_facets != len(facets_p):
        raise ValueError("world/part meshes must have matching facet count")

    for i in range(n_facets):
        fw = facets_w[i]
        n_w, area = _mesh_facet_normal_area(fw)
        if area == 0.0:
            continue
        if n_w.z > DOWNWARD_NZ_THRESHOLD:
            continue

        if require_non_display and ndp.Length > 1e-9:
            fp = facets_p[i]
            pts = fp.Points
            cx = (pts[0][0] + pts[1][0] + pts[2][0]) / 3.0
            cy = (pts[0][1] + pts[1][1] + pts[2][1]) / 3.0
            cz = (pts[0][2] + pts[1][2] + pts[2][2]) / 3.0
            proj = cx * ndp.x + cy * ndp.y + cz * ndp.z
            if proj < nd_threshold:
                continue

        pts_w = fw.Points
        cx = (pts_w[0][0] + pts_w[1][0] + pts_w[2][0]) / 3.0
        cy = (pts_w[0][1] + pts_w[1][1] + pts_w[2][1]) / 3.0
        cz = (pts_w[0][2] + pts_w[1][2] + pts_w[2][2]) / 3.0
        out.append((i, Vector(cx, cy, cz), n_w, area))
    return out


def _point_in_triangle_xy(px, py, p0, p1, p2):
    """Barycentric point-in-triangle test in XY (ignore Z)."""
    x0, y0 = p0[0], p0[1]
    x1, y1 = p1[0], p1[1]
    x2, y2 = p2[0], p2[1]
    # Sign of each edge-half-plane
    d = (y1 - y2) * (x0 - x2) + (x2 - x1) * (y0 - y2)
    if abs(d) < 1e-12:
        return False
    a = ((y1 - y2) * (px - x2) + (x2 - x1) * (py - y2)) / d
    b = ((y2 - y0) * (px - x2) + (x0 - x2) * (py - y2)) / d
    c = 1.0 - a - b
    return a >= -1e-9 and b >= -1e-9 and c >= -1e-9


def _interpolate_z_on_triangle(px, py, p0, p1, p2):
    """Barycentric Z interpolation for a point inside the triangle's XY projection."""
    x0, y0, z0 = p0[0], p0[1], p0[2]
    x1, y1, z1 = p1[0], p1[1], p1[2]
    x2, y2, z2 = p2[0], p2[1], p2[2]
    d = (y1 - y2) * (x0 - x2) + (x2 - x1) * (y0 - y2)
    if abs(d) < 1e-12:
        return (z0 + z1 + z2) / 3.0
    a = ((y1 - y2) * (px - x2) + (x2 - x1) * (py - y2)) / d
    b = ((y2 - y0) * (px - x2) + (x0 - x2) * (py - y2)) / d
    c = 1.0 - a - b
    return a * z0 + b * z1 + c * z2


def rasterize_facets_to_contacts(down_facets, mesh_world, grid_spacing=4.0):
    """Rasterize downward facets onto an XY grid; one Contact per covered cell.

    Unlike centroid-based clustering, this handles coarsely-tessellated flat
    faces correctly: a single 500 mm^2 triangle still produces a support in
    every grid cell it covers, guaranteeing no magic islands.  For overlapping
    triangles in a cell, the LOWEST Z wins (support terminates at the first
    downward surface).

    Parameters
    ----------
    down_facets : list of (idx, centroid, normal, area)
        From collect_downward_facets.  `idx` indexes into mesh_world.Facets.
    mesh_world : Mesh.Mesh
        The world-frame mesh (post-rotation, post-shift).
    grid_spacing : float
        Cell size in mm.

    Returns
    -------
    list[Contact]
    """
    if not down_facets:
        return []

    facets = mesh_world.Facets
    cells = {}   # (gx, gy) -> (x, y, z, normal)

    # Pass 1: rasterize.  For each facet, mark every grid cell whose
    # CENTER falls inside the facet's XY projection.  Guarantees full
    # coverage of large flat regions (prevents magic islands).
    for idx, _, n, _ in down_facets:
        pts = facets[idx].Points
        p0, p1, p2 = pts[0], pts[1], pts[2]
        x_min = min(p0[0], p1[0], p2[0])
        x_max = max(p0[0], p1[0], p2[0])
        y_min = min(p0[1], p1[1], p2[1])
        y_max = max(p0[1], p1[1], p2[1])
        gx_lo = int(math.floor(x_min / grid_spacing))
        gx_hi = int(math.floor(x_max / grid_spacing))
        gy_lo = int(math.floor(y_min / grid_spacing))
        gy_hi = int(math.floor(y_max / grid_spacing))
        for gx in range(gx_lo, gx_hi + 1):
            for gy in range(gy_lo, gy_hi + 1):
                cx = (gx + 0.5) * grid_spacing
                cy = (gy + 0.5) * grid_spacing
                if not _point_in_triangle_xy(cx, cy, p0, p1, p2):
                    continue
                cz = _interpolate_z_on_triangle(cx, cy, p0, p1, p2)
                existing = cells.get((gx, gy))
                if existing is None or cz < existing[2]:
                    cells[(gx, gy)] = (cx, cy, cz, n)

    # Pass 2: ensure every facet contributes at least one contact.
    # Narrow slivers (rim triangles) may not cover any cell center, yet
    # still need support.  If a facet's centroid-cell isn't already
    # covered (from pass 1 OR by another facet in this pass), place a
    # contact at the centroid itself (not snapped to cell center).
    for idx, c, n, a in down_facets:
        gx = int(math.floor(c.x / grid_spacing))
        gy = int(math.floor(c.y / grid_spacing))
        if (gx, gy) in cells:
            continue
        cells[(gx, gy)] = (c.x, c.y, c.z, n)

    # Pass 3: extremal-point guard.  The absolute lowest vertex of any
    # downward facet is the magic-island hot spot — the first layer
    # lands there.  Even with rasterization, the true corner can sit
    # BETWEEN grid cell centers and never get a support.  Walk every
    # downward facet vertex, find the minimum-Z one, and place a
    # support directly under it if no existing contact is close enough.
    low_x, low_y, low_z, low_n = None, None, float('inf'), None
    for idx, _, n, _ in down_facets:
        pts = facets[idx].Points
        for p in pts:
            if p[2] < low_z:
                low_x, low_y, low_z, low_n = p[0], p[1], p[2], n
    if low_n is not None:
        # Is any existing contact within grid_spacing of the low point?
        near_r2 = grid_spacing * grid_spacing
        has_neighbor = any((x - low_x) ** 2 + (y - low_y) ** 2 < near_r2
                           for (x, y, _, _) in cells.values())
        if not has_neighbor:
            cells[("extremum", "low")] = (low_x, low_y, low_z, low_n)

    return [Contact(x=x, y=y, z=z, nx=n.x, ny=n.y, nz=n.z, base_z=0.0)
            for (x, y, z, n) in cells.values()]


# Legacy name — kept as an alias in case any caller refers to the old
# centroid-clustering behavior.  Prefer rasterize_facets_to_contacts.
def cluster_facets_to_contacts(down_facets, grid_spacing=4.0, pick='lowest'):
    """Deprecated: use rasterize_facets_to_contacts for proper coverage.

    This centroid-based clustering undercounts coarsely-tessellated flat
    faces (a 500 mm^2 triangle contributes ONE contact, missing entire
    regions).  Kept only for backward compatibility.
    """
    if not down_facets:
        return []
    buckets = {}
    for idx, c, n, a in down_facets:
        gx = int(math.floor(c.x / grid_spacing))
        gy = int(math.floor(c.y / grid_spacing))
        buckets.setdefault((gx, gy), []).append((idx, c, n, a))
    contacts = []
    for key, items in buckets.items():
        items.sort(key=lambda t: t[1].z)
        idx, c, n, a = items[0]
        contacts.append(Contact(x=c.x, y=c.y, z=c.z,
                                nx=n.x, ny=n.y, nz=n.z, base_z=0.0))
    return contacts


# ---------------------------------------------------------------------------
# Pipeline entry point
# ---------------------------------------------------------------------------

def load_mesh(spec):
    if spec.mesh is not None:
        return spec.mesh
    if not spec.stl_path:
        raise ValueError("PartSpec requires either mesh or stl_path")
    m = Mesh.Mesh()
    m.read(spec.stl_path)
    return m


def process_part(spec, printer='m7_pro', raise_amount=MODEL_RAISE,
                 grid_spacing=4.0, verbose=True):
    """Orient → place supports → build raft for one part.

    Returns
    -------
    PartResult
    """
    mesh = load_mesh(spec)

    # 1. Orientation pick
    orient = pick_best_orientation(
        mesh, spec.non_display_dir,
        candidates=spec.candidates or generate_candidates(),
        printer=printer,
    )
    if verbose:
        print(f"[{spec.name}] orientation:")
        print(orient.report(top=5))

    # 2. Rotate mesh into print frame, shift so min-Z = 0, then raise
    rotated = apply_rotation_to_mesh(mesh, orient.best.candidate.rotation,
                                     shift_to_plate=True)
    # Raise model off plate so all contact is through support tips
    if raise_amount > 0:
        rotated.translate(0, 0, raise_amount)

    # 3. Generate support contacts on downward non-display facets.
    #    Position classification uses the ORIGINAL part-frame mesh; direction
    #    (downward) uses the ROTATED world-frame mesh.
    down_facets = collect_downward_facets(
        mesh_world=rotated, mesh_part=mesh,
        non_display_dir_part=spec.non_display_dir,
        require_non_display=True,
        band_mm=spec.band_mm,
    )
    if verbose:
        total_down = len(down_facets)
        total_area = sum(t[3] for t in down_facets)
        print(f"[{spec.name}] downward non-display facets: {total_down} "
              f"(total area {total_area:.1f} mm^2)")

    contacts = rasterize_facets_to_contacts(down_facets, rotated,
                                            grid_spacing=grid_spacing)
    if verbose:
        print(f"[{spec.name}] placed {len(contacts)} support contacts "
              f"(grid={grid_spacing}mm)")

    # 4. Build support geometry
    support_compound = None
    if contacts:
        shapes = []
        for c in contacts:
            shapes.extend(build_tapered_support(c, raft_top_z=0.0,
                                                include_base_pad=True))
        support_compound = Part.Compound(shapes)

    # 5. Build raft — needs a Part.Shape for the model footprint.
    #    Cheap shortcut: wrap the mesh bbox in a thin box.
    bb = rotated.BoundBox
    footprint_box = Part.makeBox(
        bb.XLength, bb.YLength, 0.1,
        Vector(bb.XMin, bb.YMin, bb.ZMin),
    )
    raft_shape = build_raft(footprint_box, contacts=contacts)

    return PartResult(
        spec=spec,
        oriented_mesh=rotated,
        support_compound=support_compound,
        raft_shape=raft_shape,
        raft_top_z=0.0,
        contacts=contacts,
        orientation=orient,
    )


# ---------------------------------------------------------------------------
# FreeCAD-document helpers (for visualization)
# ---------------------------------------------------------------------------

def add_result_to_doc(result, doc=None, prefix=None):
    """Create FreeCAD document objects for an oriented mesh + supports + raft.

    Handy for visually inspecting in the FreeCAD GUI.
    """
    if doc is None:
        doc = FreeCAD.ActiveDocument
    if doc is None:
        doc = FreeCAD.newDocument("PartResult")
    prefix = prefix or result.spec.name

    mesh_obj = doc.addObject("Mesh::Feature", f"{prefix}_mesh")
    mesh_obj.Mesh = result.oriented_mesh

    sup_obj = None
    if result.support_compound is not None:
        sup_obj = doc.addObject("Part::Feature", f"{prefix}_supports")
        sup_obj.Shape = result.support_compound

    raft_obj = None
    if result.raft_shape is not None:
        raft_obj = doc.addObject("Part::Feature", f"{prefix}_raft")
        raft_obj.Shape = result.raft_shape

    doc.recompute()
    return {"mesh": mesh_obj, "supports": sup_obj, "raft": raft_obj}
