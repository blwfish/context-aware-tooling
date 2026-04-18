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
    NECK_HEIGHT,
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


def _convex_hull_2d(points):
    """Andrew's monotone chain — compute 2D convex hull of a list of
    (x, y) tuples.  Returns hull vertices in counter-clockwise order,
    without repeating the start point.  Handles collinear points by
    excluding interior-colinear ones (strict turn check).
    """
    if len(points) < 2:
        return list(points)
    pts = sorted(set(points))
    if len(pts) < 3:
        return pts

    def cross(o, a, b):
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])

    lower = []
    for p in pts:
        while len(lower) >= 2 and cross(lower[-2], lower[-1], p) <= 0:
            lower.pop()
        lower.append(p)
    upper = []
    for p in reversed(pts):
        while len(upper) >= 2 and cross(upper[-2], upper[-1], p) <= 0:
            upper.pop()
        upper.append(p)
    return lower[:-1] + upper[:-1]


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

    # Pass 3: extremal-point guards.  Supports on the PERIMETER of the
    # downward region matter most — that's where each layer's peel
    # front starts and ends, and where resin shrinkage tugs corners
    # inward.  We approximate "perimeter corners" with the 2D convex
    # hull of all downward-facet vertices, and ensure a support sits
    # within grid_spacing of every hull vertex.
    #
    # For a rectangle: 4 hull vertices → the 4 projected corners.
    # For a hexagon: 6 hull vertices.
    # For a round part: many hull vertices (one per mesh vertex on the
    # perimeter), but most will already have a grid contact nearby so
    # the near-neighbor check filters them out — only actual "gaps"
    # between grid contacts and the true boundary get extra supports.
    # Also explicitly include the lowest-Z vertex (magic-island
    # hot-spot), which may not be on the XY convex hull.
    near_r2 = grid_spacing * grid_spacing

    # Gather unique down-facet vertices, keeping lowest Z per (x, y)
    # and a representative normal.
    vertex_map = {}
    low_key = None
    low_z = float('inf')
    for idx, _, n, _ in down_facets:
        pts = facets[idx].Points
        for p in pts:
            key = (round(p[0], 3), round(p[1], 3))
            existing = vertex_map.get(key)
            if existing is None or p[2] < existing[2]:
                vertex_map[key] = (p[0], p[1], p[2], n)
            if p[2] < low_z:
                low_z = p[2]
                low_key = key

    # 2D convex hull via Andrew's monotone chain
    hull_keys = _convex_hull_2d(list(vertex_map.keys()))
    if low_key is not None and low_key not in hull_keys:
        hull_keys.append(low_key)

    for key in hull_keys:
        bx, by, bz, bn = vertex_map[key]
        has_neighbor = any((x - bx) ** 2 + (y - by) ** 2 < near_r2
                           for (x, y, _, _) in cells.values())
        if not has_neighbor:
            cells[("extremum", key)] = (bx, by, bz, bn)

    # Steer each support's neck toward the centroid of the contact set
    # in XY — but check for COLLISIONS between the column and the model
    # first.  The default centroid-direction neck offset puts the column
    # inside the part's bounding region; for a hollow part (bay window,
    # building shell), that column path can pass through an interior
    # wall or window divider.
    #
    # For each contact, we test 8 candidate directions (the centroid
    # direction plus 7 rotations of it by 45°) and pick the first one
    # whose column (a vertical line at the offset XY) is clear of any
    # part geometry below the tip Z.  Falls back to centroid if none
    # work.
    if len(cells) >= 2:
        cx_mean = sum(v[0] for v in cells.values()) / len(cells)
        cy_mean = sum(v[1] for v in cells.values()) / len(cells)
    else:
        cx_mean = cy_mean = 0.0

    # Pre-index facets by XY bbox for cheaper collision tests.
    facet_data = _build_facet_xy_index(mesh_world)

    contacts = []
    for (x, y, z, n) in cells.values():
        dx = cx_mean - x
        dy = cy_mean - y
        d = math.sqrt(dx * dx + dy * dy)
        if d > 1e-6:
            cx_dir = dx / d
            cy_dir = dy / d
        else:
            cx_dir = cy_dir = 0.0

        chosen = _find_clear_neck_direction(
            x, y, z, cx_dir, cy_dir, facet_data,
            raft_top_z=0.0, neck_offset=NECK_HEIGHT,
        )
        contacts.append(Contact(x=x, y=y, z=z,
                                nx=n.x, ny=n.y, nz=n.z, base_z=0.0,
                                neck_toward_x=chosen[0],
                                neck_toward_y=chosen[1]))
    return contacts


def _build_facet_xy_index(mesh_world):
    """Return a list of (x0,y0,z0, x1,y1,z1, x2,y2,z2, xmin,xmax,ymin,ymax,zmin,zmax)
    tuples for each facet.  Used to cheaply reject facets that can't contain
    a given (col_x, col_y) test point."""
    data = []
    for f in mesh_world.Facets:
        pts = f.Points
        p0, p1, p2 = pts[0], pts[1], pts[2]
        x_min = min(p0[0], p1[0], p2[0])
        x_max = max(p0[0], p1[0], p2[0])
        y_min = min(p0[1], p1[1], p2[1])
        y_max = max(p0[1], p1[1], p2[1])
        z_min = min(p0[2], p1[2], p2[2])
        z_max = max(p0[2], p1[2], p2[2])
        data.append((p0, p1, p2, x_min, x_max, y_min, y_max, z_min, z_max))
    return data


def _column_collides(col_x, col_y, raft_top_z, tip_z, facet_data,
                     clearance=0.5):
    """Return True if a vertical line at (col_x, col_y) passes through any
    facet at Z strictly between raft_top_z and (tip_z - clearance)."""
    tip_z_safe = tip_z - clearance
    for (p0, p1, p2, x_min, x_max, y_min, y_max, z_min, z_max) in facet_data:
        if z_max <= raft_top_z + 0.01 or z_min >= tip_z_safe:
            continue
        if col_x < x_min or col_x > x_max or col_y < y_min or col_y > y_max:
            continue
        if _point_in_triangle_xy(col_x, col_y, p0, p1, p2):
            return True
    return False


def _find_clear_neck_direction(tip_x, tip_y, tip_z, cx_dir, cy_dir,
                               facet_data, raft_top_z, neck_offset):
    """Return a (ntx, nty) unit direction such that a column offset by
    neck_offset*(ntx, nty) from the tip clears the part geometry.
    Prefers directions closest to the centroid direction (cx_dir, cy_dir)."""
    # 8 candidate directions: centroid + rotations by 45°.  The rotations
    # cover all cardinal+diagonal offsets so at least one usually clears.
    import math as _m
    if cx_dir == 0.0 and cy_dir == 0.0:
        return (0.0, 0.0)

    base_angle = _m.atan2(cy_dir, cx_dir)
    # Preference order: centroid direction first, then alternating ±45°, ±90°, ±135°, 180°
    offsets_deg = [0, -45, 45, -90, 90, -135, 135, 180]
    for off in offsets_deg:
        a = base_angle + _m.radians(off)
        dx = _m.cos(a)
        dy = _m.sin(a)
        col_x = tip_x + neck_offset * dx
        col_y = tip_y + neck_offset * dy
        if not _column_collides(col_x, col_y, raft_top_z, tip_z, facet_data):
            return (dx, dy)
    # Nothing clear — return centroid direction and let it collide
    # (the alternative would be no support at all, which is worse).
    return (cx_dir, cy_dir)


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
