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
    """Find the dominant wall normal from the two largest planar faces.

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
    # Collect planar faces with their areas and normals
    planar_faces = []
    for face in wall_shape.Faces:
        if face.Area < TUNNEL_FACE_MIN_AREA:
            continue
        try:
            normal = face.normalAt(0, 0)
        except Exception:
            continue
        # Check if face is planar: all vertex normals should be similar
        # For a truly planar face, the surface type is Plane
        surf_type = face.Surface.__class__.__name__
        if surf_type == "Plane":
            planar_faces.append((face.Area, normal, face))

    if len(planar_faces) < 2:
        raise ValueError("Wall shape has fewer than 2 planar faces")

    # Sort by area descending — the two largest are exterior and interior
    planar_faces.sort(key=lambda x: x[0], reverse=True)

    area1, n1, f1 = planar_faces[0]
    area2, n2, f2 = planar_faces[1]

    # The two wall faces should have opposing normals
    dot = n1.x * n2.x + n1.y * n2.y + n1.z * n2.z
    if dot > 0:
        # Same direction — try to find the actual opposing face
        for i in range(2, len(planar_faces)):
            ai, ni, fi = planar_faces[i]
            di = n1.x * ni.x + n1.y * ni.y + n1.z * ni.z
            if di < -0.5:
                area2, n2, f2 = ai, ni, fi
                break

    # Exterior face is the one with larger offset along its normal
    offset1 = f1.CenterOfMass.x * n1.x + f1.CenterOfMass.y * n1.y + f1.CenterOfMass.z * n1.z
    offset2 = f2.CenterOfMass.x * n2.x + f2.CenterOfMass.y * n2.y + f2.CenterOfMass.z * n2.z

    # Normalize: exterior normal points outward (larger offset)
    if offset1 >= offset2:
        ext_normal = n1
        ext_offset = offset1
    else:
        ext_normal = n2
        ext_offset = offset2

    return (ext_normal.x, ext_normal.y, ext_normal.z), ext_offset


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


def find_openings(wall_shape, wall_normal, wall_label="", tol=None):
    """Detect rectangular openings in a wall solid.

    Identifies tunnel faces (normals perpendicular to wall_normal),
    groups them by spatial proximity, and extracts opening geometry.

    Parameters
    ----------
    wall_shape : Part.Shape
    wall_normal : tuple
        (nx, ny, nz) outward normal of the wall exterior.
    wall_label : str
        Label for provenance.
    tol : float, optional
        Perpendicularity tolerance. Defaults to PERPENDICULAR_TOL.

    Returns
    -------
    list[Opening]
    """
    if tol is None:
        tol = PERPENDICULAR_TOL

    nx, ny, nz = wall_normal

    # Determine wall-parallel axes for grouping
    # wall_length_dir = wall_normal x Z (or wall_normal x X if wall is horizontal)
    up = (0, 0, 1)
    # Cross product: normal x up
    lx = ny * up[2] - nz * up[1]
    ly = nz * up[0] - nx * up[2]
    lz = nx * up[1] - ny * up[0]
    length_mag = math.sqrt(lx*lx + ly*ly + lz*lz)
    if length_mag < 0.01:
        # Wall is horizontal (rare), use X as length direction
        lx, ly, lz = 1, 0, 0
    else:
        lx /= length_mag
        ly /= length_mag
        lz /= length_mag

    # Collect tunnel faces: normals perpendicular to wall normal
    tunnel_faces = []
    for face in wall_shape.Faces:
        if face.Area < TUNNEL_FACE_MIN_AREA:
            continue
        try:
            fn = face.normalAt(0, 0)
        except Exception:
            continue
        dot = fn.x * nx + fn.y * ny + fn.z * nz
        if abs(dot) < tol:
            tunnel_faces.append(face)

    if not tunnel_faces:
        return []

    # Group tunnel faces by spatial proximity using union-find
    n = len(tunnel_faces)
    parent = list(range(n))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(i, j):
        ri, rj = find(i), find(j)
        if ri != rj:
            parent[ri] = rj

    bboxes = [_bbox_tuple(f) for f in tunnel_faces]

    for i in range(n):
        for j in range(i + 1, n):
            # Check if bboxes overlap or are very close in wall-parallel plane
            bi, bj = bboxes[i], bboxes[j]
            # Use all 3 axes for proximity — faces of the same opening
            # will share edges or be adjacent
            close = True
            for k in range(3):
                gap = max(bi[k], bj[k]) - min(bi[k+3], bj[k+3])
                if gap > GROUPING_TOL:
                    close = False
                    break
            if close:
                union(i, j)

    # Collect groups
    groups = {}
    for i in range(n):
        root = find(i)
        if root not in groups:
            groups[root] = []
        groups[root].append(i)

    # Extract opening geometry from each group
    openings = []
    for indices in groups.values():
        if len(indices) < 2:
            # Need at least 2 tunnel faces for a real opening
            continue

        # Aggregate bounding box of all faces in this group
        xmins = [bboxes[i][0] for i in indices]
        ymins = [bboxes[i][1] for i in indices]
        zmins = [bboxes[i][2] for i in indices]
        xmaxs = [bboxes[i][3] for i in indices]
        ymaxs = [bboxes[i][4] for i in indices]
        zmaxs = [bboxes[i][5] for i in indices]

        xmin, ymin, zmin = min(xmins), min(ymins), min(zmins)
        xmax, ymax, zmax = max(xmaxs), max(ymaxs), max(zmaxs)

        center = ((xmin + xmax) / 2, (ymin + ymax) / 2, (zmin + zmax) / 2)

        # Width = extent along wall-length direction
        # Height = extent along Z
        # Wall thickness = extent along wall-normal direction
        dx, dy, dz = xmax - xmin, ymax - ymin, zmax - zmin

        # Project extent onto wall-length direction
        width = abs(dx * lx + dy * ly + dz * lz)
        # Height is Z extent
        height = dz
        # Wall thickness is extent along wall normal
        thickness = abs(dx * nx + dy * ny + dz * nz)

        # For axis-aligned walls, simplify:
        # If normal is along X: thickness=dx, width from ly/lz
        # If normal is along Y: thickness=dy, width from lx/lz
        # Use the more robust projected approach above

        openings.append(Opening(
            center_x=center[0],
            center_y=center[1],
            center_z=center[2],
            width=width,
            height=height,
            wall_thickness=thickness,
            normal_x=nx,
            normal_y=ny,
            normal_z=nz,
            exterior_offset=0.0,  # filled in by caller or find_all_openings
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
        try:
            normal, ext_offset = detect_wall_normal(shape)
        except ValueError:
            logger.debug("Skipping '%s' — cannot detect wall normal", wall_obj.Label)
            continue

        openings = find_openings(shape, normal, wall_label=wall_obj.Label)
        for op in openings:
            op.exterior_offset = ext_offset
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
            # Z is depth — height is the larger of remaining
            if dims[remaining[0]] >= dims[remaining[1]]:
                height_axis = remaining[0]
                width_axis = remaining[1]
            else:
                height_axis = remaining[1]
                width_axis = remaining[0]

        catalog.append(MasterInfo(
            label=obj.Label,
            width=dims[width_axis],
            height=dims[height_axis],
            depth=depth,
            depth_axis=depth_axis,
            bbox_min=(bb.XMin, bb.YMin, bb.ZMin),
            bbox_max=(bb.XMax, bb.YMax, bb.ZMax),
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

    # Rotation: map master depth axis to wall normal
    # FreeCAD.Rotation(from_vec, to_vec) creates the shortest arc rotation
    rot = Rotation(master_depth_dir, wall_n)

    # Master bounding box center in its local frame
    mx = (master.bbox_min[0] + master.bbox_max[0]) / 2
    my = (master.bbox_min[1] + master.bbox_max[1]) / 2
    mz = (master.bbox_min[2] + master.bbox_max[2]) / 2
    master_center = Vector(mx, my, mz)

    # After rotation, the master center moves to:
    rotated_center = rot.multVec(master_center)

    # Master back face (exterior side) after rotation:
    # The back of the master along its depth axis, rotated to wall normal direction.
    # Back face offset from master center along depth = +depth/2
    # After rotation, this becomes +depth/2 along wall_normal
    back_offset = master.depth / 2

    # Target position: opening center, with back face at wall exterior
    # The master center should be at:
    #   - opening center in wall-parallel plane
    #   - along wall normal: exterior_offset - depth/2 (so back face at exterior)
    target = Vector(opening.center_x, opening.center_y, opening.center_z)

    # Adjust along wall normal: place master center so its back is at exterior
    # Currently target is at opening center. The opening center along the normal
    # is at some point inside the wall. We want:
    #   master_center_along_normal = exterior_offset - depth/2
    # Current opening center along normal:
    opening_normal_offset = (opening.center_x * opening.normal_x +
                              opening.center_y * opening.normal_y +
                              opening.center_z * opening.normal_z)
    desired_normal_offset = opening.exterior_offset - back_offset
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

def place_master(doc, opening, master, mode='separate'):
    """Place a single master into an opening.

    Parameters
    ----------
    doc : FreeCAD.Document
    opening : Opening
    master : MasterInfo
    mode : str
        'fuse' — boolean fuse master into wall solid.
        'separate' — position master as an independent Part::Feature.

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

        label = f"{master.label}_in_{opening.wall_label}"
        doc.openTransaction(f"Fuse master '{master.label}' into '{opening.wall_label}'")
        try:
            fused = wall_obj.Shape.fuse(placed_shape)
            wall_obj.Shape = fused
            doc.recompute()
            doc.commitTransaction()
        except Exception:
            doc.abortTransaction()
            raise

        return PlacementResult(
            opening=opening, master=master,
            gap_w=gap_w, gap_h=gap_h,
            mode='fuse', placed_label=opening.wall_label,
        )

    else:  # separate
        label = f"{master.label}_placed"
        doc.openTransaction(f"Place master '{master.label}'")
        try:
            new_obj = doc.addObject("Part::Feature", label)
            new_obj.Shape = placed_shape
            doc.recompute()
            doc.commitTransaction()
        except Exception:
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
    for opening, master in matches:
        try:
            result = place_master(doc, opening, master, mode)
            results.append(result)
            print(f"  Placed '{master.label}' at ({opening.center_x:.1f}, "
                  f"{opening.center_y:.1f}, {opening.center_z:.1f}) "
                  f"gap: {result.gap_w:.2f} x {result.gap_h:.2f} mm")
        except Exception as e:
            logger.error("Failed to place '%s': %s", master.label, e)
            print(f"  FAILED '{master.label}': {e}")

    print(f"Placed {len(results)}/{len(matches)} masters successfully.")
    return results
