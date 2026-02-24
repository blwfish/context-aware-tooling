"""
Context-aware support generation and model preparation utilities for resin
MSLA printing.

Designed to be imported inside FreeCAD's Python environment.
Implements the pipeline described in context-aware-supports.md.

Usage (from FreeCAD MCP execute_python or generate_building_print.py):
    from support_utils import (
        Contact, build_tapered_support, build_supports, build_raft,
        check_build_fit, classify_faces, tilt_for_printing,
        tilted_wall_outward_normal, generate_all_overhang_supports,
    )

For model splitting, see split_utils.py.

All dimensions are in print-scale mm (not prototype scale).
"""

import Part
import FreeCAD
from FreeCAD import Vector
import math
import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Contact dataclass — replaces ad-hoc tuples for support contact points
# ---------------------------------------------------------------------------

@dataclass
class Contact:
    """A support contact point on the model surface.

    Unifies the previously separate 3-tuple (x,y,z), 6-tuple (x,y,z,nx,ny,nz),
    and 7-tuple (x,y,z,base_z,nx,ny,nz) representations.

    Attributes
    ----------
    x, y, z : float
        Contact point where support tip touches the model.
    nx, ny, nz : float
        Unit normal of the contact face (points away from solid surface).
        Default (0, 0, -1) = downward-facing horizontal face.
    base_z : float
        Z coordinate where the support column starts.
        0.0 = raft-based (column grows from raft top).
        >0  = model-resting (column starts on another surface).
    """
    x: float
    y: float
    z: float
    nx: float = 0.0
    ny: float = 0.0
    nz: float = -1.0
    base_z: float = 0.0

    @property
    def face_normal(self):
        """Face normal as a tuple (nx, ny, nz)."""
        return (self.nx, self.ny, self.nz)

    @property
    def position(self):
        """Contact position as a tuple (x, y, z)."""
        return (self.x, self.y, self.z)

    @property
    def is_model_resting(self):
        """True if this support rests on the model rather than the raft."""
        return self.base_z > 0.1


# ---------------------------------------------------------------------------
# Constants (from context-aware-supports.md)
# ---------------------------------------------------------------------------

FRAGILE_THRESHOLD = 0.6        # mm -- features thinner than this are fragile
COSMETIC_AREA_MAX = 1.0        # mm^2 -- overhang faces smaller than this are cosmetic
COSMETIC_DEPTH_MAX = 1.0       # mm -- overhang depth shallower than this is cosmetic
OVERHANG_DOT_THRESHOLD = -0.3  # normal.z below this = downward-facing overhang
WALL_DOT_THRESHOLD = 0.7       # abs(dot with wall normal) above this = wall surface
                                # 0.5 was too loose: brick step overhangs had dot ~0.59

RAFT_MARGIN = 2.0              # mm beyond footprint
RAFT_THICKNESS = 1.5           # mm
RAFT_CHAMFER = 0.4             # mm

MODEL_RAISE = 2.0              # mm to raise model off raft

# Dual-axis tilt defaults
DEFAULT_X_TILT = 18.0          # degrees around X axis (peel reduction)
DEFAULT_Z_TILT = 0.0           # degrees around Z axis (diagonal peel line)

TIP_RADIUS = 0.4               # mm (contact point; 0.8mm diameter on interior)
TIP_HEIGHT = 0.8               # mm (vertical tip transition; used when no face_normal)
NECK_HEIGHT = 4.0              # mm (angled approach section; long taper keeps column
                                #     clear of thin walls — like commercial slicers)
COLUMN_RADIUS = 0.7            # mm (1.4mm diameter; resists peel-force buckling)
BASE_PAD_RADIUS = 1.5          # mm (3.0mm diameter base pad)
BASE_PAD_HEIGHT = 0.8          # mm (taller base for shear resistance)

BOTTOM_SUPPORT_SPACING = 2.0   # mm along longest axis
BOTTOM_SUPPORT_MIN_DEPTH = 3.0 # mm -- use front+back rows if depth exceeds this


# ---------------------------------------------------------------------------
# Build Volume Check
# ---------------------------------------------------------------------------

# Known printer build volumes (x, y, z) in mm
PRINTER_VOLUMES = {
    'm7_pro': (218.0, 123.0, 260.0),
    'm7_max': (298.0, 164.0, 300.0),
}


def check_build_fit(shape, printer='m7_pro', margin=2.0):
    """
    Check if a shape (with supports/raft) fits a printer's build volume.

    Parameters
    ----------
    shape : Part.Shape
        The complete print (model + supports + raft).
    printer : str
        Printer key from PRINTER_VOLUMES.
    margin : float
        Safety margin from build volume edges.

    Returns
    -------
    dict with keys:
        'fits': bool
        'model_size': (x, y, z)
        'build_volume': (x, y, z)
        'overflow': (dx, dy, dz) -- positive values mean doesn't fit
    """
    vol = PRINTER_VOLUMES.get(printer)
    if vol is None:
        raise ValueError(f"Unknown printer '{printer}'. "
                         f"Known: {list(PRINTER_VOLUMES.keys())}")

    bb = shape.BoundBox
    model_size = (bb.XLength, bb.YLength, bb.ZLength)
    available = (vol[0] - 2*margin, vol[1] - 2*margin, vol[2] - 2*margin)
    overflow = (model_size[0] - available[0],
                model_size[1] - available[1],
                model_size[2] - available[2])
    fits = all(o <= 0 for o in overflow)

    status = "FITS" if fits else "DOES NOT FIT"
    print(f"Build volume check ({printer}): {status}")
    print(f"  Model:  {model_size[0]:.1f} x {model_size[1]:.1f} x "
          f"{model_size[2]:.1f} mm")
    print(f"  Volume: {vol[0]:.1f} x {vol[1]:.1f} x {vol[2]:.1f} mm")
    if not fits:
        axes = ['X', 'Y', 'Z']
        for i, o in enumerate(overflow):
            if o > 0:
                print(f"  {axes[i]} overflow: {o:.1f}mm")

    return {
        'fits': fits,
        'model_size': model_size,
        'build_volume': vol,
        'overflow': overflow,
    }


# ---------------------------------------------------------------------------
# Face Classification
# ---------------------------------------------------------------------------

def classify_faces(shape, wall_outward_normal, window_bounds=None,
                   tilt_angle_deg=0):
    """
    Classify every face in a shape for support decisions.

    Parameters
    ----------
    shape : Part.Shape
        The model shape (already oriented/tilted for printing).
    wall_outward_normal : Vector
        Unit vector pointing from interior toward display surface
        (in the *original* untilted frame). Used to distinguish
        display vs interior faces.
    window_bounds : dict or None
        If provided, keys: x_min, x_max, z_min, z_max (in original frame).
        Faces fully inside these bounds and thinner than FRAGILE_THRESHOLD
        are classified as mullion/fragile.
    tilt_angle_deg : float
        Tilt angle applied to the model (for reference; classification
        operates on the already-tilted geometry).

    Returns
    -------
    dict
        Keys are face indices (int), values are dicts with:
        - 'category': str (display, interior, vertical, brick_side,
          cosmetic_overhang, structural_overhang, fragile)
        - 'normal': Vector
        - 'area': float
        - 'bbox': BoundBox
    """
    results = {}

    for i, face in enumerate(shape.Faces):
        try:
            n = face.normalAt(0.5, 0.5)
        except Exception as e:
            logger.debug("normalAt failed for face: %s", e)
            n = Vector(0, 0, 0)
        bb = face.BoundBox
        area = face.Area
        dims = sorted([bb.XLength, bb.YLength, bb.ZLength])

        cat = _classify_single_face(n, bb, area, dims, wall_outward_normal,
                                     window_bounds)
        results[i] = {
            'category': cat,
            'normal': n,
            'area': area,
            'bbox': bb,
        }

    return results


def _classify_single_face(normal, bbox, area, dims_sorted,
                           wall_outward_normal, window_bounds):
    """Classify a single face. Returns category string."""

    nz = normal.z

    # --- Wall-aligned faces first (display/interior) ---
    # Check alignment with wall outward normal BEFORE overhang detection.
    # A tilted wall's interior surface can have nz = -0.3 which would
    # otherwise trip the overhang threshold, but dot with wall_outward_normal
    # reveals it's clearly a wall surface, not an overhang.
    dot_wall = (normal.x * wall_outward_normal.x +
                normal.y * wall_outward_normal.y +
                normal.z * wall_outward_normal.z)

    if abs(dot_wall) > WALL_DOT_THRESHOLD:
        # Strongly aligned with wall normal -- this is a wall surface.
        # Check for brick_side (thin face) first.
        if dims_sorted[0] < FRAGILE_THRESHOLD:
            if window_bounds and _is_inside_window(bbox, window_bounds):
                return 'fragile'
            else:
                return 'brick_side'
        if dot_wall > 0.5:
            return 'display'
        else:
            return 'interior'

    # --- Overhang detection (downward-facing) ---
    if nz < OVERHANG_DOT_THRESHOLD:
        # Is it inside a window opening? Could be mullion.
        if window_bounds and _is_inside_window(bbox, window_bounds):
            if dims_sorted[0] < FRAGILE_THRESHOLD:
                return 'fragile'

        # Cosmetic vs structural overhang classification.
        # Brick course steps have very thin depth after tilt (~0.07-0.12mm).
        # Structural overhangs (lintels, bay bottoms) have larger depth
        # (~0.3mm for 1.2mm walls, ~0.5mm for 2mm, ~1.5mm for 4.8mm).
        # Threshold 0.15mm separates brick steps from structural faces
        # across all practical wall thicknesses.
        overhang_depth = dims_sorted[0]
        if overhang_depth < 0.15:
            # Brick course step -- always cosmetic
            return 'cosmetic_overhang'
        if area < COSMETIC_AREA_MAX:
            return 'cosmetic_overhang'
        return 'structural_overhang'

    # --- Vertical / near-vertical faces ---
    if abs(nz) < 0.3:
        if dims_sorted[0] < FRAGILE_THRESHOLD:
            if window_bounds and _is_inside_window(bbox, window_bounds):
                return 'fragile'
            else:
                return 'brick_side'
        return 'vertical'

    # --- Upward-facing ---
    return 'vertical'  # top surfaces, etc.


def _is_inside_window(bbox, wb):
    """Check if a face bbox is fully inside window opening bounds."""
    return (bbox.XMin >= wb['x_min'] and bbox.XMax <= wb['x_max'] and
            bbox.ZMin >= wb['z_min'] and bbox.ZMax <= wb['z_max'])


def summarize_classification(classified):
    """Print a summary of face classification results."""
    counts = {}
    for info in classified.values():
        cat = info['category']
        counts[cat] = counts.get(cat, 0) + 1

    print(f"Face classification ({len(classified)} faces):")
    for cat in sorted(counts.keys()):
        print(f"  {cat}: {counts[cat]}")
    return counts


# ---------------------------------------------------------------------------
# Support Point Generation
# ---------------------------------------------------------------------------

def generate_lintel_supports(shape, classified, window_bounds,
                              mullion_x=None):
    """
    Generate support contact points for structural overhangs (lintels).

    Finds structural_overhang faces and places contacts at structural
    junctions (jamb corners, near mullion cross-points).

    Parameters
    ----------
    shape : Part.Shape
    classified : dict from classify_faces()
    window_bounds : dict with x_min, x_max, z_min, z_max
    mullion_x : float or None
        X position of vertical mullion center (if present).

    Returns
    -------
    list of (x, y, z) tuples -- contact points on the lintel underside.
    """
    # Find the lintel face(s) -- structural overhangs near window top
    lintel_faces = []
    for idx, info in classified.items():
        if info['category'] != 'structural_overhang':
            continue
        bb = info['bbox']
        # Must overlap with window X range and be near window top Z
        if (bb.XMin >= window_bounds['x_min'] - 1 and
            bb.XMax <= window_bounds['x_max'] + 1 and
            bb.ZMin > window_bounds['z_min']):
            lintel_faces.append((idx, info))

    if not lintel_faces:
        print("No lintel faces found")
        return []

    # Use the largest lintel face
    lintel_faces.sort(key=lambda x: x[1]['area'], reverse=True)
    lintel_idx, lintel_info = lintel_faces[0]
    lintel_face = shape.Faces[lintel_idx]
    bb = lintel_info['bbox']
    lintel_normal = lintel_face.normalAt(0.5, 0.5)
    lintel_cog = lintel_face.CenterOfGravity

    print(f"Lintel face {lintel_idx}: area={lintel_info['area']:.1f} "
          f"X=[{bb.XMin:.1f},{bb.XMax:.1f}] "
          f"Y=[{bb.YMin:.1f},{bb.YMax:.1f}] "
          f"Z=[{bb.ZMin:.1f},{bb.ZMax:.1f}]")

    # Z interpolation using the face's plane equation.
    # Works correctly for both single-axis and dual-axis tilt.
    def lintel_z(x, y):
        return _face_z_at_xy(lintel_normal, lintel_cog, x, y)

    # X positions: jamb edges + near mullion
    x_positions = [bb.XMin + 0.5, bb.XMax - 0.5]  # jamb corners
    if mullion_x is not None:
        # Flank the mullion (avoid the mullion hole itself)
        x_positions.extend([mullion_x - 1.5, mullion_x + 1.5])
    x_positions.sort()

    # Y positions: front and back rows
    y_margin = 0.5
    y_front = bb.YMin + y_margin
    y_back = bb.YMax - y_margin
    y_positions = [y_front, y_back]

    contacts = []
    for x in x_positions:
        for y in y_positions:
            z = lintel_z(x, y)
            contacts.append((x, y, z))

    print(f"Generated {len(contacts)} lintel support points")
    return contacts


def generate_bottom_supports(shape, classified, raise_amount=MODEL_RAISE,
                             interior_y_side=None):
    """
    Generate support contact points for the model's bottom face.

    Called after the model has been raised off the raft.

    Parameters
    ----------
    shape : Part.Shape
        The raised model shape.
    classified : dict from classify_faces()
    raise_amount : float
        How far the model was raised.
    interior_y_side : str or None
        'min' or 'max' -- which Y side of overhang faces is interior.
        Determined by tilt direction. If None, uses both rows.

    Returns
    -------
    list of (x, y, z) tuples -- contact points on the bottom face.
    """
    # Find the bottom face -- the downward-facing face whose ZMin is closest
    # to raise_amount (i.e., the lowest overhang, which is the model's base).
    # We filter to structural_overhang category and pick by lowest ZMin,
    # with a minimum area to avoid brick course fragments.
    bottom_candidates = []
    for idx, info in classified.items():
        if info['category'] != 'structural_overhang':
            continue
        bb = info['bbox']
        if bb.ZMin < raise_amount + 2.0 and info['area'] > 10.0:
            bottom_candidates.append((idx, info))

    if not bottom_candidates:
        print("No bottom face found")
        return []

    # Pick the face with the lowest ZMin (closest to the raft)
    bottom_candidates.sort(key=lambda x: x[1]['bbox'].ZMin)
    bottom_idx, bottom_info = bottom_candidates[0]
    bb = bottom_info['bbox']

    bottom_face = shape.Faces[bottom_idx]
    bottom_normal = bottom_face.normalAt(0.5, 0.5)
    bottom_cog = bottom_face.CenterOfGravity

    print(f"Bottom face {bottom_idx}: area={bottom_info['area']:.1f} "
          f"X=[{bb.XMin:.1f},{bb.XMax:.1f}] "
          f"Y=[{bb.YMin:.1f},{bb.YMax:.1f}] "
          f"Z=[{bb.ZMin:.1f},{bb.ZMax:.1f}]")

    # Z interpolation using the face's plane equation.
    # Works correctly for both single-axis and dual-axis tilt.
    def bottom_z(x, y):
        return _face_z_at_xy(bottom_normal, bottom_cog, x, y)

    # X positions: distribute at ~BOTTOM_SUPPORT_SPACING intervals
    x_count = max(2, int(bb.XLength / BOTTOM_SUPPORT_SPACING) + 1)
    x_margin = 1.5
    x_positions = []
    for i in range(x_count):
        t = (i + 0.5) / x_count
        x_positions.append(bb.XMin + x_margin + t * (bb.XLength - 2 * x_margin))

    # Y positions -- INTERIOR SIDE ONLY
    # The interior side depends on tilt direction. After correct tilt
    # (interior toward plate), the interior is at the lower or higher Y
    # of the overhang face. We default to the side further from the
    # display surface.
    y_margin = 0.5
    y_row_offset = 2.0  # second row 2mm inboard from edge row
    if interior_y_side == 'max':
        y_positions = [bb.YMax - y_margin]
        if bb.YLength > BOTTOM_SUPPORT_MIN_DEPTH:
            y_positions.append(bb.YMax - y_margin - y_row_offset)
    elif interior_y_side == 'min':
        y_positions = [bb.YMin + y_margin]
        if bb.YLength > BOTTOM_SUPPORT_MIN_DEPTH:
            y_positions.append(bb.YMin + y_margin + y_row_offset)
    else:
        # Fallback: use both rows if depth allows
        if bb.YLength > BOTTOM_SUPPORT_MIN_DEPTH:
            y_positions = [bb.YMin + y_margin, bb.YMax - y_margin]
        else:
            y_positions = [(bb.YMin + bb.YMax) / 2.0]

    contacts = []
    for x in x_positions:
        for y in y_positions:
            z = bottom_z(x, y)
            # After dual-axis tilt, bbox overestimates face extent.
            # Snap to nearest point on face if outside.
            snapped = _snap_to_face(bottom_face, x, y, z, interior_y_side)
            if snapped is not None:
                contacts.append(snapped)

    print(f"Generated {len(contacts)} bottom support points")
    return contacts


def generate_all_overhang_supports(shape, classified, wall_outward_normal,
                                   interior_y_side='min'):
    """
    Generate support contact points for all structural overhangs.

    Handles multi-bay walls: finds all structural overhang faces, separates
    them into bottom faces (grid supports) and lintels/features (jamb-corner
    supports), and places contacts on the interior side only.

    Parameters
    ----------
    shape : Part.Shape
        The tilted, raised model.
    classified : dict from classify_faces()
    wall_outward_normal : Vector
        Points from interior toward display (in tilted frame).
    interior_y_side : str
        'min' or 'max' -- which Y side of overhang faces is interior.
        After correct tilt (interior toward plate), this is 'min' if
        display was originally at -Y, 'max' if display was at +Y.

    Returns
    -------
    list of (x, y, z) tuples -- contact points.
    """
    contacts = []
    y_margin = 0.5
    x_margin = 1.5

    # Collect structural overhangs
    overhangs = []
    for idx, info in classified.items():
        if info['category'] != 'structural_overhang':
            continue
        overhangs.append((idx, info))

    if not overhangs:
        print("No structural overhangs found")
        return contacts

    # Separate by area: large faces are bottom/floor (grid supports),
    # smaller faces are lintels (jamb-corner supports)
    areas = [info['area'] for _, info in overhangs]
    area_threshold = max(areas) * 0.5  # rough split

    for idx, info in overhangs:
        bb = info['bbox']

        # Interior Y position
        if interior_y_side == 'min':
            y_int = bb.YMin + y_margin
        else:
            y_int = bb.YMax - y_margin

        # Note: projection-based display-side filtering was removed because
        # multi-depth compounds (thin front bays + deep side returns) have
        # no single midplane that works. The interior_y_side parameter
        # determines which Y edge to use; trust it.

        # Z interpolation using face plane equation (works for any tilt).
        face = shape.Faces[idx]
        face_n = face.normalAt(0.5, 0.5)
        face_cog = face.CenterOfGravity

        def z_at_xy(x, y, fn=face_n, fc=face_cog):
            return _face_z_at_xy(fn, fc, x, y)

        if info['area'] >= area_threshold:
            # Large face (bottom/floor) -- grid of supports, dual Y-rows
            y_row_offset = 2.0
            y_positions_ovh = [y_int]
            if bb.YLength > BOTTOM_SUPPORT_MIN_DEPTH:
                if interior_y_side == 'max':
                    y_positions_ovh.append(y_int - y_row_offset)
                else:
                    y_positions_ovh.append(y_int + y_row_offset)
            x_count = max(2, int(bb.XLength / BOTTOM_SUPPORT_SPACING) + 1)
            for i in range(x_count):
                t = (i + 0.5) / x_count
                x = bb.XMin + x_margin + t * (bb.XLength - 2 * x_margin)
                for y_pos in y_positions_ovh:
                    z = z_at_xy(x, y_pos)
                    # After dual-axis tilt, bbox overestimates face extent.
                    # Snap to nearest point on face if outside.
                    snapped = _snap_to_face(face, x, y_pos, z, interior_y_side)
                    if snapped is not None:
                        contacts.append(snapped)
        else:
            # Lintel/feature -- supports at jamb corners only
            x_left = bb.XMin + 0.5
            x_right = bb.XMax - 0.5
            for x in [x_left, x_right]:
                z = z_at_xy(x, y_int)
                snapped = _snap_to_face(face, x, y_int, z, interior_y_side)
                if snapped is not None:
                    contacts.append(snapped)

    print(f"Generated {len(contacts)} overhang support points "
          f"(all on {'YMin' if interior_y_side == 'min' else 'YMax'} / interior side)")
    return contacts


# ---------------------------------------------------------------------------
# Face Z Interpolation
# ---------------------------------------------------------------------------

def _face_z_at_xy(face_normal, face_cog, x, y):
    """
    Compute Z at (x, y) on a planar face using the plane equation.

    For a plane with normal (nx, ny, nz) passing through (cx, cy, cz):
        nx*(x-cx) + ny*(y-cy) + nz*(z-cz) = 0
        z = cz - (nx*(x-cx) + ny*(y-cy)) / nz

    Works correctly for any tilt combination (single-axis or dual-axis).
    Falls back to face CoG's Z if the face is horizontal (nz ≈ 0 would
    make the formula degenerate, but horizontal faces have constant Z).

    Parameters
    ----------
    face_normal : Vector or tuple
        Face normal from face.normalAt(0.5, 0.5).
    face_cog : Vector or tuple
        Face center of gravity from face.CenterOfGravity.
    x, y : float
        Position to evaluate.

    Returns
    -------
    float
        Z coordinate on the face plane at (x, y).
    """
    nx = face_normal.x if hasattr(face_normal, 'x') else face_normal[0]
    ny = face_normal.y if hasattr(face_normal, 'y') else face_normal[1]
    nz = face_normal.z if hasattr(face_normal, 'z') else face_normal[2]
    cx = face_cog.x if hasattr(face_cog, 'x') else face_cog[0]
    cy = face_cog.y if hasattr(face_cog, 'y') else face_cog[1]
    cz = face_cog.z if hasattr(face_cog, 'z') else face_cog[2]

    if abs(nz) < 1e-10:
        # Face is vertical or near-vertical; Z doesn't vary with XY
        return cz
    return cz - (nx * (x - cx) + ny * (y - cy)) / nz


def _snap_to_face(face, x, y, z, interior_y_side='min', tolerance=0.5):
    """
    Validate a contact point against a face; snap to interior edge if outside.

    After dual-axis tilt, rectangular faces become parallelograms whose
    bounding boxes overestimate the actual face extent.  Grid-based contact
    placement uses the bbox, so some points land outside the face polygon.

    - Within tolerance: return point unchanged.
    - Outside but within snap_limit (3mm): snap to nearest point on face,
      but only if the snapped point stays on the interior half (prevents
      contacts poking through to the display side).
    - Beyond snap_limit or snapped to display side: discard (return None).

    Parameters
    ----------
    face : Part.Face
    x, y, z : float
    interior_y_side : str
        'min' or 'max' -- which Y side is interior.
    tolerance : float
        Points within this distance are accepted as-is.

    Returns
    -------
    (x, y, z) tuple or None
    """
    snap_limit = 3.0
    pt = Part.Vertex(Vector(x, y, z))
    try:
        dist, pts, _info = face.distToShape(pt)
        if dist <= tolerance:
            return (x, y, z)
        if dist <= snap_limit and len(pts) > 0:
            # pts is list of (point_on_face, point_on_vertex) pairs
            nearest = pts[0][0]  # closest point on face
            # Reject if snap moved contact past face center (display side).
            cog_y = face.CenterOfGravity.y
            if interior_y_side == 'min' and nearest.y > cog_y:
                return None  # would land on display side
            if interior_y_side == 'max' and nearest.y < cog_y:
                return None  # would land on display side
            return (nearest.x, nearest.y, nearest.z)
        return None  # too far, discard
    except Exception as e:
        logger.debug("snap_to_face check failed: %s", e)
        return (x, y, z)  # if check fails, keep original


# ---------------------------------------------------------------------------
# Geometry Builders
# ---------------------------------------------------------------------------

def build_tapered_support(contact, raft_top_z=0.0, include_base_pad=True):
    """
    Build a single tapered support column with sphere tip.

    If the contact has a non-default face normal, the taper+sphere approach
    along the face normal direction (perpendicular to wall surface),
    minimizing the cross-section through thin walls.  Otherwise, approaches
    vertically.

    Parameters
    ----------
    contact : Contact
        Contact point and face normal.  If contact.base_z > 0 and
        include_base_pad is False, the column starts at base_z
        (model-resting support).
    raft_top_z : float
        Z coordinate of raft top surface.
    include_base_pad : bool
        If True, add a cylindrical base pad on the raft.  Set False for
        model-resting supports where the pad would protrude through
        thin walls.

    Returns
    -------
    list of Part.Shape
    """
    cx, cy, cz = contact.x, contact.y, contact.z
    shapes = []

    # Determine column start Z
    if not include_base_pad and contact.base_z > 0:
        col_start_z = contact.base_z
    else:
        col_start_z = raft_top_z + BASE_PAD_HEIGHT

    # Base pad on raft (skip for model-resting supports)
    if include_base_pad:
        pad = Part.makeCylinder(BASE_PAD_RADIUS, BASE_PAD_HEIGHT,
                                Vector(cx, cy, raft_top_z), Vector(0, 0, 1))
        shapes.append(pad)

    fnx, fny, fnz = contact.face_normal
    fn_len = math.sqrt(fnx*fnx + fny*fny + fnz*fnz)
    has_normal = fn_len > 0.01 and not (fnx == 0 and fny == 0)

    if has_normal:
        # Normalize
        fnx, fny, fnz = fnx/fn_len, fny/fn_len, fnz/fn_len

        # Angled approach: column stands at offset XY, neck sweeps along
        # face normal to reach the contact.  Like commercial slicers.

        # Sphere center: contact + TIP_RADIUS along face normal
        sc = Vector(cx + TIP_RADIUS * fnx,
                    cy + TIP_RADIUS * fny,
                    cz + TIP_RADIUS * fnz)
        # Neck base: NECK_HEIGHT below the sphere, displaced toward building
        # INTERIOR in XY.  Face normal XY points toward exterior (away from
        # solid), so flip XY to get interior direction.  Keep Z (downward).
        nb = Vector(sc.x - NECK_HEIGHT * fnx,
                    sc.y - NECK_HEIGHT * fny,
                    sc.z + NECK_HEIGHT * fnz)

        # Column is vertical at neck_base XY position
        col_x, col_y = nb.x, nb.y

        # For raft-based supports with base pad, place pad at column XY
        if include_base_pad:
            # Override the pad position to be at the displaced column XY
            shapes[0] = Part.makeCylinder(BASE_PAD_RADIUS, BASE_PAD_HEIGHT,
                                          Vector(col_x, col_y, raft_top_z),
                                          Vector(0, 0, 1))

        col_top = nb.z
        if col_top > col_start_z:
            col = Part.makeCylinder(COLUMN_RADIUS, col_top - col_start_z,
                                    Vector(col_x, col_y, col_start_z),
                                    Vector(0, 0, 1))
            shapes.append(col)
        else:
            col_top = col_start_z

        # Neck: angled cone from column top toward sphere center
        neck_start = Vector(col_x, col_y, col_top)
        nv = sc - neck_start
        neck_len = nv.Length
        if neck_len > 0.1:
            neck_dir = Vector(nv.x/neck_len, nv.y/neck_len, nv.z/neck_len)
            taper = Part.makeCone(COLUMN_RADIUS, TIP_RADIUS, neck_len,
                                  neck_start, neck_dir)
            shapes.append(taper)
        sphere = Part.makeSphere(TIP_RADIUS, sc)
        shapes.append(sphere)
    else:
        # Vertical approach (no meaningful XY face normal)
        col_top = cz - TIP_HEIGHT
        if col_top > col_start_z:
            col = Part.makeCylinder(COLUMN_RADIUS, col_top - col_start_z,
                                    Vector(cx, cy, col_start_z),
                                    Vector(0, 0, 1))
            shapes.append(col)
        else:
            col_top = col_start_z
        taper_height = TIP_HEIGHT - TIP_RADIUS
        if taper_height > 0.01:
            taper = Part.makeCone(COLUMN_RADIUS, TIP_RADIUS, taper_height,
                                  Vector(cx, cy, col_top), Vector(0, 0, 1))
            shapes.append(taper)
        sphere = Part.makeSphere(TIP_RADIUS,
                                 Vector(cx, cy, cz - TIP_RADIUS))
        shapes.append(sphere)

    return shapes


def build_supports(contacts, raft_top_z=0.0):
    """
    Build all supports as a single compound.

    Parameters
    ----------
    contacts : list of Contact
    raft_top_z : float

    Returns
    -------
    Part.Compound
    """
    all_shapes = []
    for c in contacts:
        all_shapes.extend(build_tapered_support(c, raft_top_z))

    compound = Part.Compound(all_shapes)
    print(f"Built {len(contacts)} supports ({len(all_shapes)} shapes)")
    return compound


def build_raft(shape, contacts=None, margin=RAFT_MARGIN,
               thickness=RAFT_THICKNESS, chamfer=RAFT_CHAMFER):
    """
    Build a raft under the model with chamfered bottom edges.

    The raft is sized to cover the model footprint AND all support
    base pad positions (whichever extent is larger).

    Parameters
    ----------
    shape : Part.Shape
        The model shape (used to compute footprint).
    contacts : list of Contact or None
        Support contacts.  Raft extends to cover all base pads.
    margin : float
        Extension beyond footprint.
    thickness : float
        Raft thickness.
    chamfer : float
        Chamfer size on bottom edges.

    Returns
    -------
    Part.Shape
    """
    bb = shape.BoundBox

    x0 = bb.XMin
    x1 = bb.XMax
    y0 = bb.YMin
    y1 = bb.YMax

    # Expand to cover all support base pad positions
    if contacts:
        for c in contacts:
            x0 = min(x0, c.x - BASE_PAD_RADIUS)
            x1 = max(x1, c.x + BASE_PAD_RADIUS)
            y0 = min(y0, c.y - BASE_PAD_RADIUS)
            y1 = max(y1, c.y + BASE_PAD_RADIUS)

    # Apply margin
    x0 -= margin
    x1 += margin
    y0 -= margin
    y1 += margin

    raft = Part.makeBox(x1 - x0, y1 - y0, thickness,
                        Vector(x0, y0, -thickness))

    # Chamfer bottom edges
    if chamfer > 0:
        bottom_edges = [e for e in raft.Edges
                        if (abs(e.BoundBox.ZMin + thickness) < 0.01 and
                            abs(e.BoundBox.ZMax + thickness) < 0.01)]
        if bottom_edges:
            try:
                raft = raft.makeChamfer(chamfer, chamfer, bottom_edges)
            except Exception as e:
                logger.warning("Raft chamfer failed: %s", e)

    print(f"Raft: {x1-x0:.1f} x {y1-y0:.1f} x {thickness} "
          f"at Z=[{-thickness:.1f}, 0]")
    return raft


def raise_model(shape, amount=MODEL_RAISE):
    """
    Raise a shape by translating it upward.

    Parameters
    ----------
    shape : Part.Shape
    amount : float
        Distance to raise in Z.

    Returns
    -------
    Part.Shape
    """
    return shape.translated(Vector(0, 0, amount))


def tilt_for_printing(shape, tilt_deg=18.0, display_faces_negative_y=True,
                      z_tilt_deg=0.0):
    """
    Tilt a wall for resin printing: interior toward plate, display away.

    Applies up to two rotations:
    1. **X-axis tilt** (primary): rotates the wall so its interior faces
       the build plate. This is the traditional tilt (15-30 degrees).
    2. **Z-axis tilt** (optional): rotates the wall in the XY plane so
       the peel line sweeps diagonally across the wall instead of parallel
       to layer lines. Benefits:
       - Reduces instantaneous peel area (peel line crosses wall at angle)
       - Gives mullions cross-layer bonding (layers cross the feature)
       - Staggers brick course overhangs across Z heights
       - Typical range: 5-10 degrees

    Rotations are applied X-first, then Z, then shifted to Z=0.

    Parameters
    ----------
    shape : Part.Shape
        The wall shape (oriented with wall plane roughly in XZ, thin in Y).
    tilt_deg : float
        X-axis tilt angle in degrees (15-30 typical). Controls how far
        the wall leans back (interior toward plate).
    display_faces_negative_y : bool
        If True, the display surface is at the -Y side of the wall
        (standard for front walls). If False, display is at +Y side.
    z_tilt_deg : float
        Z-axis rotation in degrees (0-15 typical). Rotates the wall
        in the build plane for diagonal peel. Sign convention:
        positive = CCW when viewed from above. Default 0 (no Z tilt).

    Returns
    -------
    Part.Shape
        Tilted shape with bottom at Z=0.
    """
    # Interior toward plate means:
    # - If display is at -Y: rotate so top tilts toward +Y (negative angle)
    # - If display is at +Y: rotate so top tilts toward -Y (positive angle)
    x_angle = -tilt_deg if display_faces_negative_y else tilt_deg

    import FreeCAD

    # Build combined rotation: X-tilt first, then Z-tilt
    rot_x = FreeCAD.Rotation(Vector(1, 0, 0), x_angle)
    if abs(z_tilt_deg) > 0.01:
        rot_z = FreeCAD.Rotation(Vector(0, 0, 1), z_tilt_deg)
        rot = rot_z.multiply(rot_x)  # apply X first, then Z
    else:
        rot = rot_x

    tilted = shape.copy()
    tilted.Placement = FreeCAD.Placement(Vector(0, 0, 0), rot)

    # Shift so bottom at Z=0
    z_shift = -tilted.BoundBox.ZMin
    tilted = tilted.translated(Vector(0, 0, z_shift))

    if abs(z_tilt_deg) > 0.01:
        print(f"Tilted: X={x_angle:.1f}deg, Z={z_tilt_deg:.1f}deg "
              f"(dual-axis), shifted Z+{z_shift:.1f}")
    else:
        print(f"Tilted: X={x_angle:.1f}deg, shifted Z+{z_shift:.1f}")

    return tilted


def tilted_wall_outward_normal(tilt_deg=18.0, display_faces_negative_y=True,
                               z_tilt_deg=0.0):
    """
    Compute the wall outward (display) normal after tilting.

    Applies the same rotation sequence as tilt_for_printing():
    X-axis rotation first, then Z-axis rotation.

    Parameters
    ----------
    tilt_deg : float
        X-axis tilt angle in degrees.
    display_faces_negative_y : bool
        If True, display is at -Y (front wall convention).
    z_tilt_deg : float
        Z-axis rotation in degrees (0 = single-axis tilt).

    Returns
    -------
    Vector
        Unit normal pointing from interior toward display surface,
        in the tilted frame.
    """
    tilt_rad = math.radians(tilt_deg)
    if display_faces_negative_y:
        # Original normal (0, -1, 0), rotated -tilt_deg around X:
        # Y' = -cos(tilt), Z' = sin(tilt)
        nx, ny, nz = 0, -math.cos(tilt_rad), math.sin(tilt_rad)
    else:
        # Original normal (0, 1, 0), rotated +tilt_deg around X:
        # Y' = cos(tilt), Z' = -sin(tilt)
        nx, ny, nz = 0, math.cos(tilt_rad), -math.sin(tilt_rad)

    # Apply Z rotation if specified
    if abs(z_tilt_deg) > 0.01:
        z_rad = math.radians(z_tilt_deg)
        cz, sz = math.cos(z_rad), math.sin(z_rad)
        nx2 = nx * cz - ny * sz
        ny2 = nx * sz + ny * cz
        nx, ny = nx2, ny2

    return Vector(nx, ny, nz)


def validate_tilt_direction(contact_points, shape, wall_outward_normal):
    """
    Verify that all support contacts are on the interior (non-display) side.

    Projects each contact onto the wall_outward_normal axis and checks
    that it falls on the interior (negative-projection) side relative to
    the display surface. Works correctly with tilted multi-bay geometry
    where a simple center_y threshold fails.

    Parameters
    ----------
    contact_points : list of (x, y, z)
    shape : Part.Shape
        The tilted model.
    wall_outward_normal : Vector
        Points from interior toward display.

    Returns
    -------
    bool
        True if all contacts are safe (interior side).
    """
    n = Vector(wall_outward_normal)
    n.normalize()

    # Find the display-side extreme: the point on the shape with the
    # largest projection onto wall_outward_normal = the outermost display
    # surface coordinate.
    bb = shape.BoundBox
    # Project all 8 bbox corners onto n and find max (display side)
    corners = [
        Vector(bb.XMin, bb.YMin, bb.ZMin), Vector(bb.XMax, bb.YMin, bb.ZMin),
        Vector(bb.XMin, bb.YMax, bb.ZMin), Vector(bb.XMax, bb.YMax, bb.ZMin),
        Vector(bb.XMin, bb.YMin, bb.ZMax), Vector(bb.XMax, bb.YMin, bb.ZMax),
        Vector(bb.XMin, bb.YMax, bb.ZMax), Vector(bb.XMax, bb.YMax, bb.ZMax),
    ]
    projections = [c.dot(n) for c in corners]
    display_proj = max(projections)   # display surface = max projection
    interior_proj = min(projections)  # interior extremity = min projection
    midplane_proj = (display_proj + interior_proj) / 2.0

    bad = 0
    for cx, cy, cz in contact_points:
        p = Vector(cx, cy, cz)
        proj = p.dot(n)
        # Contact should be on interior side (proj < midplane)
        if proj > midplane_proj:
            bad += 1

    if bad > 0:
        print(f"WARNING: {bad}/{len(contact_points)} contacts on display side "
              f"(proj range [{interior_proj:.1f}, {display_proj:.1f}], "
              f"mid={midplane_proj:.1f})")
        return False
    print(f"Tilt validation: all {len(contact_points)} contacts on interior side "
          f"(proj range [{interior_proj:.1f}, {display_proj:.1f}])")
    return True


# ---------------------------------------------------------------------------
# Full Pipeline
# ---------------------------------------------------------------------------

def run_support_pipeline(doc, object_name, wall_outward_normal,
                          window_bounds=None, mullion_x=None,
                          raise_amount=MODEL_RAISE,
                          interior_y_side=None):
    """
    Run the full context-aware support pipeline on a model.

    1. Raise model off raft
    2. Classify all faces
    3. Generate support contact points (lintel + bottom)
    4. Build supports
    5. Build raft

    Parameters
    ----------
    doc : FreeCAD.Document
    object_name : str
        Name of the model object in the document.
    wall_outward_normal : Vector
        Points from interior to display surface (original frame).
    window_bounds : dict or None
        {x_min, x_max, z_min, z_max} in original frame.
    mullion_x : float or None
        X center of vertical mullion.
    raise_amount : float
        How far to raise model off raft.
    interior_y_side : str or None
        'min' or 'max' -- which Y side is interior after tilting.
        If None, uses both sides (legacy behavior for single-bay).

    Returns
    -------
    dict with keys: 'classified', 'lintel_contacts', 'bottom_contacts'
    """
    obj = doc.getObject(object_name)
    if obj is None:
        raise ValueError(f"Object '{object_name}' not found")

    # 1. Raise
    raised_shape = raise_model(obj.Shape, raise_amount)
    obj.Shape = raised_shape
    print(f"Raised {object_name} by {raise_amount}mm")

    # 2. Classify
    classified = classify_faces(raised_shape, wall_outward_normal,
                                 window_bounds)
    summarize_classification(classified)

    # 3. Generate contact points
    all_contacts = []

    if window_bounds:
        lintel_contacts = generate_lintel_supports(
            raised_shape, classified, window_bounds, mullion_x)
        all_contacts.extend(lintel_contacts)
    else:
        lintel_contacts = []

    bottom_contacts = generate_bottom_supports(raised_shape, classified,
                                                raise_amount,
                                                interior_y_side)
    all_contacts.extend(bottom_contacts)

    # 3b. For multi-bay panels, also generate all overhang supports
    overhang_contacts = generate_all_overhang_supports(
        raised_shape, classified, wall_outward_normal,
        interior_y_side or 'min')
    # Deduplicate with any lintel/bottom contacts already generated
    existing = set(all_contacts)
    new_overhang = [c for c in overhang_contacts if c not in existing]
    all_contacts.extend(new_overhang)

    # 4. Validate tilt direction
    if all_contacts:
        validate_tilt_direction(all_contacts, raised_shape,
                                wall_outward_normal)

    # 5. Convert (x,y,z) tuples to Contact objects for build functions
    contact_objs = [Contact(x=c[0], y=c[1], z=c[2]) for c in all_contacts]

    # 6. Build supports
    if contact_objs:
        support_compound = build_supports(contact_objs)
        sup_obj = doc.addObject("Part::Feature", "Supports")
        sup_obj.Shape = support_compound

    # 7. Build raft (sized to cover all support base pads)
    raft_shape = build_raft(raised_shape, contacts=contact_objs)
    raft_obj = doc.addObject("Part::Feature", "Raft")
    raft_obj.Shape = raft_shape

    doc.recompute()
    print("Pipeline complete!")

    return {
        'classified': classified,
        'lintel_contacts': lintel_contacts,
        'bottom_contacts': bottom_contacts,
        'overhang_contacts': new_overhang,
        'all_contacts': all_contacts,
    }
