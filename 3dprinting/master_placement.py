"""
master_placement.py — Auto-place window/door masters into wall openings.

Detects rectangular openings in wall solids (from boolean cuts), matches
them to pre-built master assemblies by size, computes placement transforms,
and either fuses masters into walls or positions them as separate objects.

Usage (from FreeCAD MCP execute_python):
    import sys; sys.path.insert(0, '/Volumes/Files/claude/tooling/3dprinting')
    from master_placement import place_all_masters
    results = place_all_masters(FreeCAD.ActiveDocument, mode='fuse')

All dimensions are in print-scale mm.
"""

import logging
import math
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

PERPENDICULAR_TOL = 0.1       # dot product threshold for "perpendicular"
DEFAULT_MAX_MARGIN = 1.0      # mm max gap between master and opening
DEFAULT_MIN_MARGIN = 0.05     # mm min gap (~laser kerf clearance)
TUNNEL_FACE_MIN_AREA = 0.1    # mm² skip boolean artifacts
GROUPING_TOL = 0.5            # mm max gap for grouping tunnel faces
NORMAL_CLUSTER_TOL = 0.02     # 1-dot threshold when bucketing face normals (~11°)
THICKNESS_SPAN_TOL = 0.15     # fraction: tunnel face must span wall thickness within 15%
PERIMETER_MARGIN = 1.0        # mm: cluster bbox must stay this far inside wall silhouette


# ---------------------------------------------------------------------------
# Data Structures
# ---------------------------------------------------------------------------

@dataclass
class Opening:
    """A rectangular opening detected in a wall solid."""
    center_x: float
    center_y: float
    center_z: float
    width: float              # extent along wall-length direction
    height: float             # extent along Z
    wall_thickness: float     # depth through wall
    normal_x: float           # wall exterior outward normal
    normal_y: float
    normal_z: float
    exterior_offset: float    # signed distance from origin to exterior plane
    wall_label: str = ""      # label of the wall object

    @property
    def center(self):
        return (self.center_x, self.center_y, self.center_z)

    @property
    def wall_normal(self):
        return (self.normal_x, self.normal_y, self.normal_z)


@dataclass
class MasterInfo:
    """Catalog entry for a master assembly."""
    label: str
    width: float
    height: float
    depth: float              # thinnest bbox dimension
    depth_axis: int           # 0=X, 1=Y, 2=Z
    bbox_min: tuple = (0, 0, 0)
    bbox_max: tuple = (0, 0, 0)
    casing_thickness: float = 0.0  # depth of frame/casing step from exterior


@dataclass
class PlacementResult:
    """Result of placing one master into one opening."""
    opening: Opening
    master: MasterInfo
    gap_w: float
    gap_h: float
    mode: str                 # 'fuse' or 'separate'
    placed_label: str = ""    # label of the placed object in the document


# ---------------------------------------------------------------------------
# Phase 1: Opening Detection
# ---------------------------------------------------------------------------

def detect_wall_normal(wall_shape):
    """Find the dominant wall normal via area-weighted normal clustering.

    Bucket all planar faces by axis direction (treating n and -n as the
    same axis), sum areas within each bucket, and pick the bucket with
    the largest total area — those are the wall front + back faces,
    which sum to roughly 2 × wall_area even when clapboard/trim detail
    has shattered each slab into many small pieces.

    Returns (normal_tuple, exterior_offset) where normal points outward
    from the wall exterior and exterior_offset is the signed distance
    from origin to the exterior face along that normal.

    Parameters
    ----------
    wall_shape : Part.Shape

    Returns
    -------
    tuple : ((nx, ny, nz), exterior_offset)
    """
    planar_faces = []
    for face in wall_shape.Faces:
        if face.Area < TUNNEL_FACE_MIN_AREA:
            continue
        if face.Surface.__class__.__name__ != "Plane":
            continue
        try:
            normal = face.normalAt(0, 0)
        except Exception:
            continue
        planar_faces.append((face.Area, normal, face))

    if not planar_faces:
        raise ValueError("Wall shape has no planar faces")

    # Bucket by axis direction. Canonicalize each normal so n and -n
    # land in the same bucket (orient first nonzero component positive).
    def canonical(n):
        for c in (n.x, n.y, n.z):
            if abs(c) > 1e-6:
                sign = 1.0 if c > 0 else -1.0
                return (sign * n.x, sign * n.y, sign * n.z)
        return (n.x, n.y, n.z)

    buckets = []  # list of {axis, total_area, faces: [(area, normal, face)...]}
    for area, normal, face in planar_faces:
        c = canonical(normal)
        matched = None
        for b in buckets:
            ax, ay, az = b["axis"]
            # 1 - dot of unit vectors; both are unit length
            delta = 1.0 - (c[0]*ax + c[1]*ay + c[2]*az)
            if delta < NORMAL_CLUSTER_TOL:
                matched = b
                break
        if matched is None:
            buckets.append({"axis": c, "total_area": area,
                            "faces": [(area, normal, face)]})
        else:
            matched["total_area"] += area
            matched["faces"].append((area, normal, face))

    # Largest-area bucket = the wall front+back axis
    buckets.sort(key=lambda b: b["total_area"], reverse=True)
    wall_bucket = buckets[0]

    # Within the winning bucket, split faces into +direction and -direction
    # based on their actual normal (canonicalization collapsed them).
    ax, ay, az = wall_bucket["axis"]
    pos_faces, neg_faces = [], []
    for area, normal, face in wall_bucket["faces"]:
        if normal.x * ax + normal.y * ay + normal.z * az > 0:
            pos_faces.append((area, normal, face))
        else:
            neg_faces.append((area, normal, face))

    def offset_of(faces, axis):
        # Area-weighted offset of face centroids along the axis.
        if not faces:
            return None
        ax_, ay_, az_ = axis
        total_a = sum(a for a, _, _ in faces)
        acc = 0.0
        for a, _, f in faces:
            com = f.CenterOfMass
            acc += a * (com.x * ax_ + com.y * ay_ + com.z * az_)
        return acc / total_a

    pos_along_axis = offset_of(pos_faces, wall_bucket["axis"])
    neg_along_axis = offset_of(neg_faces, wall_bucket["axis"])

    # Exterior = face that is further from origin along its own outward
    # normal. For pos faces (normal = +axis), that distance equals
    # pos_along_axis. For neg faces (normal = -axis), it equals
    # -neg_along_axis. Pick whichever is larger.
    pos_outward = pos_along_axis
    neg_outward = -neg_along_axis if neg_along_axis is not None else None

    if pos_outward is None:
        ext_axis = (-ax, -ay, -az)
        ext_offset = neg_outward
    elif neg_outward is None or pos_outward >= neg_outward:
        ext_axis = (ax, ay, az)
        ext_offset = pos_outward
    else:
        ext_axis = (-ax, -ay, -az)
        ext_offset = neg_outward

    return ext_axis, ext_offset


def _faces_overlap_2d(bb1, bb2, axis_indices, tol):
    """Check if two bounding boxes overlap in the given 2 axis dimensions."""
    for idx in axis_indices:
        mins = [bb1[idx], bb2[idx]]
        maxs = [bb1[idx + 3], bb2[idx + 3]]
        if min(maxs) < max(mins) - tol:
            return False
    return True


def _bbox_tuple(face):
    """Extract bounding box as (xmin, ymin, zmin, xmax, ymax, zmax)."""
    bb = face.BoundBox
    return (bb.XMin, bb.YMin, bb.ZMin, bb.XMax, bb.YMax, bb.ZMax)


def find_openings(wall_shape, wall_normal=None, wall_label="", tol=None):
    """Detect openings per-face, one opening per inner wire.

    For each planar face F with inner wire(s), treat F as an "interior"
    wall face:
      - exterior normal = -F.normalAt()
      - wall_thickness = extent of F's parent solid along exterior normal
      - exterior_offset = max projection of that solid along exterior normal
      - each inner wire becomes one Opening

    This handles single slabs, multi-solid compounds (each solid has its
    own interior face), mirrors, and multi-facet walls (bays) where each
    facet has a different normal — all via the same per-face logic.

    Parameters
    ----------
    wall_shape : Part.Shape
    wall_normal : tuple, optional
        (nx, ny, nz) — if given, used as an alignment hint to prune faces
        whose -outward_normal doesn't roughly match it.  Useful for clean
        slab walls (tests) where both sides carry inner wires.  For
        clapboarded real walls, the exterior face is shattered and won't
        pass the inner-wire test anyway, so the hint is unnecessary.
    wall_label : str
        Label for provenance.
    tol : float, optional
        Ignored — kept for backward compatibility.

    Returns
    -------
    list[Opening]
    """
    openings = []
    seen = set()  # dedup by (rounded center, rounded dims)

    for solid in wall_shape.Solids:
        verts = [(v.X, v.Y, v.Z) for v in solid.Vertexes]

        for face in solid.Faces:
            if face.Area < TUNNEL_FACE_MIN_AREA:
                continue
            if face.Surface.__class__.__name__ != "Plane":
                continue
            try:
                outer = face.OuterWire
            except Exception:
                continue
            inner_wires = [w for w in face.Wires if not w.isSame(outer)]
            if not inner_wires:
                continue
            try:
                fn = face.normalAt(0, 0)
            except Exception:
                continue
            # Skip near-horizontal faces (floor/ceiling, not wall).
            if abs(fn.z) > 0.9:
                continue

            # Wall exterior normal: face is assumed interior, so exterior
            # points opposite face's outward normal.
            ex_nx, ex_ny, ex_nz = -fn.x, -fn.y, -fn.z

            # Alignment hint: skip faces whose exterior doesn't roughly
            # match the hinted wall_normal (lets tests on clean slabs
            # disambiguate between the two faces that both carry wires).
            if wall_normal is not None:
                hx, hy, hz = wall_normal
                if ex_nx*hx + ex_ny*hy + ex_nz*hz < 0.5:
                    continue

            # Per-solid wall_thickness along this face's exterior normal.
            projs = [v[0]*ex_nx + v[1]*ex_ny + v[2]*ex_nz for v in verts]
            solid_max_proj = max(projs)
            fc = face.CenterOfMass
            face_proj = fc.x*ex_nx + fc.y*ex_ny + fc.z*ex_nz
            wall_thickness = solid_max_proj - face_proj
            exterior_offset = solid_max_proj

            # In-plane "length" direction: perpendicular to ex_normal and world Z.
            lx = ex_ny * 1.0 - ex_nz * 0.0
            ly = ex_nz * 0.0 - ex_nx * 1.0
            lz = ex_nx * 0.0 - ex_ny * 0.0
            lm = math.sqrt(lx*lx + ly*ly + lz*lz)
            if lm < 0.01:
                lx, ly, lz = 1.0, 0.0, 0.0
            else:
                lx /= lm; ly /= lm; lz /= lm

            for wire in inner_wires:
                # Project wire vertices onto tangent and vertical axes to
                # get true in-plane extents (bbox extents are axis-aligned
                # and can't represent tilted-wall rectangles correctly).
                wv = [(v.X, v.Y, v.Z) for v in wire.Vertexes]
                if not wv:
                    continue
                t_projs = [p[0]*lx + p[1]*ly + p[2]*lz for p in wv]
                width = max(t_projs) - min(t_projs)
                z_projs = [p[2] for p in wv]
                height = max(z_projs) - min(z_projs)

                # Center on the face plane (midpoint in tangent + vertical,
                # preserving the face's plane offset).
                t_mid = (max(t_projs) + min(t_projs)) / 2
                z_mid = (max(z_projs) + min(z_projs)) / 2
                # Face plane offset along exterior normal.
                n_mid = face_proj
                cx = t_mid * lx + n_mid * ex_nx
                cy = t_mid * ly + n_mid * ex_ny
                cz = z_mid

                key = (round(cx, 2), round(cy, 2), round(cz, 2),
                       round(width, 2), round(height, 2))
                if key in seen:
                    continue
                seen.add(key)

                openings.append(Opening(
                    center_x=cx, center_y=cy, center_z=cz,
                    width=width, height=height,
                    wall_thickness=wall_thickness,
                    normal_x=ex_nx, normal_y=ex_ny, normal_z=ex_nz,
                    exterior_offset=exterior_offset,
                    wall_label=wall_label,
                ))

    logger.info("Found %d openings in '%s'", len(openings), wall_label)
    return openings


def find_all_openings(doc, wall_group=None):
    """Scan wall objects and detect openings in each.

    Parameters
    ----------
    doc : FreeCAD.Document
    wall_group : str, optional
        Label of a group containing wall objects.  If None, scans all
        Part::Feature objects with at least 6 faces (minimum for a
        wall with one opening).

    Returns
    -------
    list[Opening]
    """
    import FreeCAD

    walls = []
    if wall_group:
        grp = None
        for obj in doc.Objects:
            if obj.Label == wall_group and hasattr(obj, 'Group'):
                grp = obj
                break
        if grp is None:
            raise ValueError(f"Group '{wall_group}' not found")
        walls = [o for o in grp.Group if hasattr(o, 'Shape')]
    else:
        walls = [o for o in doc.Objects
                 if hasattr(o, 'Shape') and len(o.Shape.Faces) >= 6]

    all_openings = []
    for wall_obj in walls:
        shape = wall_obj.Shape
        if not shape.Solids:
            continue
        openings = find_openings(shape, wall_label=wall_obj.Label)
        all_openings.extend(openings)

    logger.info("Total openings found: %d across %d walls", len(all_openings), len(walls))
    return all_openings


# ---------------------------------------------------------------------------
# Phase 2: Master Catalog & Matching
# ---------------------------------------------------------------------------

DEFAULT_MASTER_GROUPS = ("Imported Masters", "Local Masters")


def _find_group(doc, label):
    """Find a document group by label, or return None."""
    for obj in doc.Objects:
        if obj.Label == label and hasattr(obj, 'Group'):
            return obj
    return None


def catalog_masters(doc, group_names=None):
    """Build a catalog of master assemblies from document group(s).

    Searches one or more named groups for master objects.  Depth is the
    thinnest bounding box dimension.  Height is the Z extent (unless Z
    is depth, in which case height is the larger of X/Y).

    Parameters
    ----------
    doc : FreeCAD.Document
    group_names : str or list[str], optional
        Group label(s) to search.  Defaults to DEFAULT_MASTER_GROUPS
        ("Imported Masters", "Local Masters").  Groups that don't exist
        are silently skipped.

    Returns
    -------
    list[MasterInfo]
    """
    if group_names is None:
        group_names = DEFAULT_MASTER_GROUPS
    elif isinstance(group_names, str):
        group_names = (group_names,)

    master_objects = []
    groups_found = []
    for name in group_names:
        grp = _find_group(doc, name)
        if grp is not None:
            groups_found.append(name)
            master_objects.extend(
                o for o in grp.Group if hasattr(o, 'Shape'))

    if not groups_found:
        raise ValueError(
            f"None of the master groups {list(group_names)} found in document")

    catalog = []
    for obj in master_objects:
        if not obj.Shape.Solids:
            continue
        bb = obj.Shape.BoundBox
        dims = [bb.XLength, bb.YLength, bb.ZLength]

        # Check for explicit depth axis override property
        explicit_axis = getattr(obj, 'MasterDepthAxis', None)
        if explicit_axis in ('X', 'Y', 'Z'):
            depth_axis = {'X': 0, 'Y': 1, 'Z': 2}[explicit_axis]
        else:
            depth_axis = dims.index(min(dims))

        depth = dims[depth_axis]
        remaining = [i for i in range(3) if i != depth_axis]

        # Height prefers Z axis; width is the other
        if 2 in remaining:
            height_axis = 2
            width_axis = [i for i in remaining if i != 2][0]
        else:
            if dims[remaining[0]] >= dims[remaining[1]]:
                height_axis = remaining[0]
                width_axis = remaining[1]
            else:
                height_axis = remaining[1]
                width_axis = remaining[0]

        # Match dimensions = the smaller end face along the depth axis.
        # Window masters have a frame on the exterior side and a pane that
        # protrudes through the wall opening on the other side; the pane's
        # end face is what has to fit the opening, not the frame bbox.
        # For masters without a recessed pane (plain boxes, doors), both
        # end faces equal the bbox and pane_width/height fall back to that.
        pane_w, pane_h = dims[width_axis], dims[height_axis]
        bbox_mins = [bb.XMin, bb.YMin, bb.ZMin]
        bbox_maxs = [bb.XMax, bb.YMax, bb.ZMax]
        min_area = float('inf')
        # Also look for an interior step face that faces the exterior
        # (normal in -depth direction, i.e. fn[depth_axis] < 0). That face
        # sits at bbox_min + casing_thickness. Pick the face closest to
        # bbox_min that isn't the bbox_min extreme itself.
        casing_thickness = 0.0
        casing_candidate = float('inf')
        for face in obj.Shape.Faces:
            if face.Surface.__class__.__name__ != "Plane":
                continue
            try:
                fn = face.normalAt(0, 0)
            except Exception:
                continue
            n_comps = (fn.x, fn.y, fn.z)
            # Faces aligned with the depth axis (normal along ±axis)
            if abs(n_comps[depth_axis]) < 0.95:
                continue
            fbb = face.BoundBox
            f_min = [fbb.XMin, fbb.YMin, fbb.ZMin][depth_axis]
            f_max = [fbb.XMax, fbb.YMax, fbb.ZMax][depth_axis]
            at_min = abs(f_min - bbox_mins[depth_axis]) < 0.05
            at_max = abs(f_max - bbox_maxs[depth_axis]) < 0.05

            # Pane-size detection: end faces at a bbox extreme.
            if at_min or at_max:
                f_dims = [fbb.XLength, fbb.YLength, fbb.ZLength]
                area = f_dims[width_axis] * f_dims[height_axis]
                if area < min_area and area > 0:
                    min_area = area
                    pane_w = f_dims[width_axis]
                    pane_h = f_dims[height_axis]
                continue

            # Interior step face: frame (at bbox_min side) back annular
            # face has normal pointing +depth_axis (toward pane/interior).
            # Position is between bbox extremes. Pick the one closest to
            # bbox_min — that's the casing back, at bbox_min + casing_thickness.
            if n_comps[depth_axis] > 0:
                step_offset = f_min - bbox_mins[depth_axis]
                if 0.05 < step_offset < casing_candidate:
                    casing_candidate = step_offset
                    casing_thickness = step_offset

        catalog.append(MasterInfo(
            label=obj.Label,
            width=pane_w,
            height=pane_h,
            depth=depth,
            depth_axis=depth_axis,
            bbox_min=(bb.XMin, bb.YMin, bb.ZMin),
            bbox_max=(bb.XMax, bb.YMax, bb.ZMax),
            casing_thickness=casing_thickness,
        ))

    logger.info("Cataloged %d masters from %s", len(catalog), groups_found)
    return catalog


def match_master(opening, catalog, max_margin=None, min_margin=None):
    """Find the best master for an opening by size.

    Masters must be smaller than the opening within margin bounds.
    Best match minimizes total gap (width_gap + height_gap).

    Parameters
    ----------
    opening : Opening
    catalog : list[MasterInfo]
    max_margin : float
        Maximum allowed gap per dimension. Defaults to DEFAULT_MAX_MARGIN.
    min_margin : float
        Minimum required gap per dimension. Defaults to DEFAULT_MIN_MARGIN.

    Returns
    -------
    MasterInfo or None
    """
    if max_margin is None:
        max_margin = DEFAULT_MAX_MARGIN
    if min_margin is None:
        min_margin = DEFAULT_MIN_MARGIN

    best = None
    best_gap = float('inf')

    for master in catalog:
        gap_w = opening.width - master.width
        gap_h = opening.height - master.height
        if gap_w < min_margin or gap_w > max_margin:
            continue
        if gap_h < min_margin or gap_h > max_margin:
            continue
        total_gap = gap_w + gap_h
        if total_gap < best_gap:
            best_gap = total_gap
            best = master

    return best


def match_all(openings, catalog, max_margin=None, min_margin=None):
    """Match all openings to masters.

    Returns
    -------
    list[tuple[Opening, MasterInfo]]
        Only includes openings that found a match.
    """
    matches = []
    unmatched = 0
    for opening in openings:
        master = match_master(opening, catalog, max_margin, min_margin)
        if master is not None:
            matches.append((opening, master))
        else:
            unmatched += 1
            logger.warning("No master found for opening at (%.1f, %.1f, %.1f) "
                           "size %.1f x %.1f",
                           opening.center_x, opening.center_y, opening.center_z,
                           opening.width, opening.height)
    if unmatched:
        logger.info("%d openings unmatched out of %d", unmatched, len(openings))
    return matches


# ---------------------------------------------------------------------------
# Phase 3: Placement Transform
# ---------------------------------------------------------------------------

def compute_placement(opening, master):
    """Compute a FreeCAD.Placement to position a master in an opening.

    The transform:
    1. Rotates the master so its depth axis aligns with the wall normal
    2. Centers the master in the opening
    3. Shifts along wall normal so master back face is flush with wall exterior

    Parameters
    ----------
    opening : Opening
    master : MasterInfo

    Returns
    -------
    FreeCAD.Placement
    """
    import FreeCAD
    from FreeCAD import Vector, Placement, Rotation

    # Master's depth axis direction (in its local frame, before placement)
    depth_dirs = [Vector(1, 0, 0), Vector(0, 1, 0), Vector(0, 0, 1)]
    master_depth_dir = depth_dirs[master.depth_axis]

    wall_n = Vector(opening.normal_x, opening.normal_y, opening.normal_z)

    # Rotation: master's local +depth_axis points from frame (casing, at
    # bbox_min[depth_axis]) to pane back (at bbox_max[depth_axis]). The
    # frame sits against the wall exterior and the pane recedes into the
    # opening, so master's +depth axis points AWAY from the exterior.
    # Map +depth_axis to -wall_n so the frame lands on the exterior side.
    rot = Rotation(master_depth_dir, -wall_n)

    # Master bounding box center in its local frame
    mx = (master.bbox_min[0] + master.bbox_max[0]) / 2
    my = (master.bbox_min[1] + master.bbox_max[1]) / 2
    mz = (master.bbox_min[2] + master.bbox_max[2]) / 2
    master_center = Vector(mx, my, mz)

    # After rotation, the master center moves to:
    rotated_center = rot.multVec(master_center)

    # Frame face (exterior side) is at -depth/2 in local frame. After the
    # rotation (+depth_axis -> -wall_n), the frame ends up at +depth/2
    # along wall_n from the rotated master center.
    back_offset = master.depth / 2

    # We want the frame BACK (casing's inside face, at local +casing_thickness
    # from frame front) to sit flush with the clapboard surface at
    # exterior_offset. The frame's FRONT protrudes by casing_thickness.
    # master_center_along_wall_n = exterior_offset - depth/2 + casing_thickness
    target = Vector(opening.center_x, opening.center_y, opening.center_z)

    opening_normal_offset = (opening.center_x * opening.normal_x +
                              opening.center_y * opening.normal_y +
                              opening.center_z * opening.normal_z)
    desired_normal_offset = (opening.exterior_offset
                             - back_offset
                             + master.casing_thickness)
    shift = desired_normal_offset - opening_normal_offset

    target = Vector(
        target.x + shift * opening.normal_x,
        target.y + shift * opening.normal_y,
        target.z + shift * opening.normal_z,
    )

    # Translation: move rotated master center to target
    translation = target - rotated_center

    return Placement(translation, rot)


# ---------------------------------------------------------------------------
# Phase 4: Execution
# ---------------------------------------------------------------------------

def place_master(doc, opening, master, mode='separate', manage_transaction=True):
    """Place a single master into an opening.

    Parameters
    ----------
    doc : FreeCAD.Document
    opening : Opening
    master : MasterInfo
    mode : str
        'fuse' — boolean fuse master into wall solid.
        'separate' — position master as an independent Part::Feature.
    manage_transaction : bool
        If True, open/commit a transaction around the placement so it
        is individually undoable. Set False when a caller (e.g.
        place_all_masters) already holds an outer transaction.

    Returns
    -------
    PlacementResult
    """
    import FreeCAD

    # Find the master object in the document
    master_obj = None
    for obj in doc.Objects:
        if obj.Label == master.label and hasattr(obj, 'Shape'):
            master_obj = obj
            break
    if master_obj is None:
        raise ValueError(f"Master object '{master.label}' not found in document")

    placement = compute_placement(opening, master)
    placed_shape = master_obj.Shape.copy()
    placed_shape.Placement = placement

    gap_w = opening.width - master.width
    gap_h = opening.height - master.height

    if mode == 'fuse':
        # Find wall object and fuse
        wall_obj = None
        for obj in doc.Objects:
            if obj.Label == opening.wall_label and hasattr(obj, 'Shape'):
                wall_obj = obj
                break
        if wall_obj is None:
            raise ValueError(f"Wall object '{opening.wall_label}' not found")

        if manage_transaction:
            doc.openTransaction(f"Fuse master '{master.label}' into '{opening.wall_label}'")
        try:
            fused = wall_obj.Shape.fuse(placed_shape)
            wall_obj.Shape = fused
            if manage_transaction:
                doc.recompute()
                doc.commitTransaction()
        except Exception:
            if manage_transaction:
                doc.abortTransaction()
            raise

        return PlacementResult(
            opening=opening, master=master,
            gap_w=gap_w, gap_h=gap_h,
            mode='fuse', placed_label=opening.wall_label,
        )

    else:  # separate
        label = f"{master.label}_placed"
        if manage_transaction:
            doc.openTransaction(f"Place master '{master.label}'")
        try:
            new_obj = doc.addObject("Part::Feature", label)
            new_obj.Shape = placed_shape
            if manage_transaction:
                doc.recompute()
                doc.commitTransaction()
        except Exception:
            if manage_transaction:
                doc.abortTransaction()
            raise

        return PlacementResult(
            opening=opening, master=master,
            gap_w=gap_w, gap_h=gap_h,
            mode='separate', placed_label=new_obj.Label,
        )


def place_all_masters(doc, wall_group=None, master_groups=None,
                      mode='separate', max_margin=None, min_margin=None):
    """Full pipeline: detect openings, match masters, place all.

    Parameters
    ----------
    doc : FreeCAD.Document
    wall_group : str, optional
        Label of group containing wall objects.
    master_groups : str or list[str], optional
        Group label(s) to search for masters.  Defaults to
        DEFAULT_MASTER_GROUPS ("Imported Masters", "Local Masters").
    mode : str
        'fuse' or 'separate'.
    max_margin, min_margin : float, optional

    Returns
    -------
    list[PlacementResult]
    """
    openings = find_all_openings(doc, wall_group)
    if not openings:
        print("No openings detected.")
        return []

    catalog = catalog_masters(doc, master_groups)
    if not catalog:
        print("No masters found in catalog.")
        return []

    matches = match_all(openings, catalog, max_margin, min_margin)
    if not matches:
        print("No openings matched to masters.")
        return []

    print(f"Matched {len(matches)} openings to masters. Placing ({mode} mode)...")

    results = []
    doc.openTransaction(f"Place {len(matches)} masters ({mode})")
    try:
        for opening, master in matches:
            try:
                result = place_master(doc, opening, master, mode,
                                      manage_transaction=False)
                results.append(result)
                print(f"  Placed '{master.label}' at ({opening.center_x:.1f}, "
                      f"{opening.center_y:.1f}, {opening.center_z:.1f}) "
                      f"gap: {result.gap_w:.2f} x {result.gap_h:.2f} mm")
            except Exception as e:
                logger.error("Failed to place '%s': %s", master.label, e)
                print(f"  FAILED '{master.label}': {e}")
        doc.recompute()
        doc.commitTransaction()
    except Exception:
        doc.abortTransaction()
        raise

    print(f"Placed {len(results)}/{len(matches)} masters successfully.")
    return results
