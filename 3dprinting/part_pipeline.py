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
    """
    non_display_dir: Sequence[float] = (0.0, 0.0, -1.0)
    name: str = "part"
    stl_path: Optional[str] = None
    mesh: Optional[Mesh.Mesh] = None
    candidates: Optional[list] = None


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


def cluster_facets_to_contacts(down_facets, grid_spacing=4.0,
                               pick='lowest'):
    """Bin downward facets into an XY grid and emit one Contact per cell.

    Parameters
    ----------
    down_facets : list of (idx, centroid, normal, area)
    grid_spacing : float
        Cell size in mm for XY binning.  ~2-5 mm is typical.
    pick : str
        Which facet in a cell becomes the contact.  'lowest' picks min Z
        (the most urgent overhang near the plate).  'centroid' averages
        centroids of all facets in the cell.

    Returns
    -------
    list[Contact]
    """
    if not down_facets:
        return []
    # Bin by (grid_x, grid_y)
    buckets = {}
    for idx, c, n, a in down_facets:
        gx = int(math.floor(c.x / grid_spacing))
        gy = int(math.floor(c.y / grid_spacing))
        buckets.setdefault((gx, gy), []).append((idx, c, n, a))

    contacts = []
    for key, items in buckets.items():
        if pick == 'lowest':
            items.sort(key=lambda t: t[1].z)
            idx, c, n, a = items[0]
        else:
            # area-weighted centroid
            total_a = sum(t[3] for t in items)
            cx = sum(t[1].x * t[3] for t in items) / total_a
            cy = sum(t[1].y * t[3] for t in items) / total_a
            cz = sum(t[1].z * t[3] for t in items) / total_a
            # pick the facet closest to that centroid for its normal
            items.sort(key=lambda t: (t[1].x - cx) ** 2 + (t[1].y - cy) ** 2)
            _, _, n, _ = items[0]
            c = Vector(cx, cy, cz)
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
    )
    if verbose:
        total_down = len(down_facets)
        total_area = sum(t[3] for t in down_facets)
        print(f"[{spec.name}] downward non-display facets: {total_down} "
              f"(total area {total_area:.1f} mm^2)")

    contacts = cluster_facets_to_contacts(down_facets,
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
