"""
Auto-slice: determine how to cut a model so each piece fits a printer's
build volume, accounting for tilt-induced footprint inflation.

Pure-math planning functions (compute_tilt_envelope, plan_cuts,
avoid_detail_zones) have no FreeCAD dependency and are fully testable
standalone.  FreeCAD-dependent functions (analyze_model, find_detail_zones,
execute_slice_plan) live below the pure-math layer.

Usage (from FreeCAD MCP execute_python):
    from auto_slice import plan_cuts, compute_tilt_envelope, auto_slice

All dimensions are in print-scale mm (not prototype scale).
"""

import math
import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_TILT_X = 18.0          # degrees — standard X tilt for peel reduction
DEFAULT_TILT_Z = 0.0           # degrees — Z tilt (0 unless diagonal peel)
CUT_MARGIN = 2.0               # mm — safety margin from build volume edges
DETAIL_CLEARANCE = 3.0         # mm — min distance from cut to detail zone
MIN_PIECE_DIMENSION = 15.0     # mm — don't create slivers
VOLUME_THRESHOLD = 10.0        # mm³ — solids below this are "detail"

# Printer build volumes (duplicated from support_utils to keep this module
# importable without FreeCAD)
PRINTER_VOLUMES = {
    'm7_pro': (218.0, 123.0, 260.0),
    'm7_max': (298.0, 164.0, 300.0),
}

# Axis name → index mapping
_AXIS_INDEX = {'x': 0, 'y': 1, 'z': 2}


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------

@dataclass
class CutSpec:
    """A single planned cut along one axis."""
    axis: str              # 'x', 'y', or 'z'
    position: float        # coordinate along axis
    index: int             # 0-based cut number on this axis
    total: int             # total cuts on this axis

    def __repr__(self):
        return (f"CutSpec(axis='{self.axis}', position={self.position:.1f}, "
                f"{self.index+1}/{self.total})")


@dataclass
class SlicePlan:
    """Complete plan for slicing a model to fit a printer."""
    model_dims: tuple          # (x, y, z) original bounding box
    build_volume: tuple        # (x, y, z) printer
    tilt_envelope: tuple       # (x, y, z) worst-case post-tilt per piece
    cuts: list                 # list of CutSpec, sorted by axis then position
    piece_count: int           # len(cuts) + 1
    detail_zones_avoided: list = field(default_factory=list)

    @property
    def axes_cut(self):
        """Set of axes that have cuts."""
        return {c.axis for c in self.cuts}

    def cuts_on_axis(self, axis):
        """Return cuts for a single axis, sorted by position."""
        return sorted([c for c in self.cuts if c.axis == axis],
                      key=lambda c: c.position)


@dataclass
class DetailZone:
    """A region along an axis to avoid cutting through."""
    axis: str        # which axis this zone spans
    lo: float        # lower bound
    hi: float        # upper bound
    label: str = ""  # optional description


@dataclass
class ModelAnalysis:
    """Shape analysis results for auto-slice planning."""
    dims: tuple                    # (xlen, ylen, zlen)
    origin: tuple                  # (xmin, ymin, zmin)
    main_solid_volume: float
    detail_zones: list             # list of DetailZone
    solid_count: int


# ---------------------------------------------------------------------------
# Pure-math functions (no FreeCAD dependency)
# ---------------------------------------------------------------------------

def compute_tilt_envelope(dims, tilt_x_deg=DEFAULT_TILT_X,
                          tilt_z_deg=DEFAULT_TILT_Z):
    """Compute the bounding box of a piece after tilting.

    Tilting around X rotates the YZ plane:
      new_y = y*cos(θx) + z*sin(θx)
      new_z = y*sin(θx) + z*cos(θx)

    Tilting around Z rotates the XY plane:
      new_x = x*cos(θz) + y'*sin(θz)
      new_y' = x*sin(θz) + y'*cos(θz)

    Parameters
    ----------
    dims : tuple of (x, y, z)
        Piece dimensions in mm.
    tilt_x_deg : float
        Tilt angle around X axis in degrees.
    tilt_z_deg : float
        Tilt angle around Z axis in degrees.

    Returns
    -------
    tuple of (x, y, z)
        Inflated bounding box dimensions after tilting.
    """
    x, y, z = dims

    # Step 1: tilt around X (affects Y and Z)
    rx = math.radians(tilt_x_deg)
    cos_x, sin_x = math.cos(rx), math.sin(rx)
    y1 = y * cos_x + z * sin_x
    z1 = y * sin_x + z * cos_x

    # Step 2: tilt around Z (affects X and the already-tilted Y)
    rz = math.radians(tilt_z_deg)
    cos_z, sin_z = math.cos(rz), math.sin(rz)
    x2 = x * cos_z + y1 * sin_z
    y2 = x * sin_z + y1 * cos_z

    return (x2, y2, z1)


def _pieces_for_axis(model_len, available, n_cuts):
    """Return piece lengths for n_cuts evenly-spaced cuts along one axis."""
    n_pieces = n_cuts + 1
    return [model_len / n_pieces] * n_pieces


def _min_cuts_for_axis(model_dim_along, model_dims_full, axis,
                       build_volume, tilt_x_deg, tilt_z_deg, margin):
    """Find minimum number of cuts so pieces fit the build volume on this axis.

    Only checks whether reducing *this* axis's dimension (via cuts) makes the
    tilted envelope fit.  Cross-axis overflow is handled when plan_cuts
    processes the other axis.

    Returns (n_cuts, piece_dims_tilted) where piece_dims_tilted is the
    worst-case tilted envelope for pieces from this many cuts.
    """
    ai = _AXIS_INDEX[axis]

    for n_cuts in range(0, 20):  # 0 = no cut needed
        piece_len = model_dim_along / (n_cuts + 1)
        if piece_len < MIN_PIECE_DIMENSION and n_cuts > 0:
            return n_cuts - 1, None

        # Build piece dims (other axes unchanged — they get their own cuts)
        piece_dims = list(model_dims_full)
        piece_dims[ai] = piece_len
        piece_dims = tuple(piece_dims)

        envelope = compute_tilt_envelope(piece_dims, tilt_x_deg, tilt_z_deg)

        # Only check whether the tilted envelope on the axis we're cutting
        # (and axes coupled to it by tilt) fit.  Each axis is responsible
        # for making itself fit.
        if envelope[ai] <= build_volume[ai] - 2 * margin:
            return n_cuts, envelope

    return 20, None  # shouldn't happen in practice


def plan_cuts(model_dims, model_origin, build_volume,
              tilt_x_deg=DEFAULT_TILT_X, tilt_z_deg=DEFAULT_TILT_Z,
              margin=CUT_MARGIN, detail_zones=None):
    """Plan the cuts needed to fit a model into a printer's build volume.

    Parameters
    ----------
    model_dims : tuple of (x, y, z)
        Model bounding box dimensions in mm.
    model_origin : tuple of (xmin, ymin, zmin)
        Lower corner of the model bounding box.
    build_volume : tuple of (x, y, z)
        Printer build volume in mm.
    tilt_x_deg, tilt_z_deg : float
        Expected print tilt angles.
    margin : float
        Safety margin from build volume edges.
    detail_zones : list of DetailZone, optional
        Regions to avoid cutting through.

    Returns
    -------
    SlicePlan
        Complete cut plan with positions and metadata.
    """
    detail_zones = detail_zones or []
    all_cuts = []

    # Determine cuts needed per axis
    for axis in ('x', 'y', 'z'):
        ai = _AXIS_INDEX[axis]
        n_cuts, envelope = _min_cuts_for_axis(
            model_dims[ai], model_dims, axis,
            build_volume, tilt_x_deg, tilt_z_deg, margin)

        if n_cuts == 0:
            continue

        # Compute evenly-spaced cut positions in model coordinates
        piece_len = model_dims[ai] / (n_cuts + 1)
        positions = [model_origin[ai] + piece_len * (i + 1)
                     for i in range(n_cuts)]

        # Nudge away from detail zones
        axis_zones = [dz for dz in detail_zones if dz.axis == axis]
        if axis_zones:
            positions = avoid_detail_zones(positions, axis_zones,
                                           DETAIL_CLEARANCE)

        for i, pos in enumerate(positions):
            all_cuts.append(CutSpec(axis=axis, position=pos,
                                    index=i, total=n_cuts))

    # Compute the tilt envelope for reporting (use the largest piece)
    if all_cuts:
        # Approximate: use dims divided by cuts per axis
        piece_dims = list(model_dims)
        for axis in ('x', 'y', 'z'):
            ai = _AXIS_INDEX[axis]
            axis_cuts = [c for c in all_cuts if c.axis == axis]
            if axis_cuts:
                piece_dims[ai] = model_dims[ai] / (len(axis_cuts) + 1)
        tilt_env = compute_tilt_envelope(tuple(piece_dims),
                                          tilt_x_deg, tilt_z_deg)
    else:
        tilt_env = compute_tilt_envelope(model_dims, tilt_x_deg, tilt_z_deg)

    # Sort by axis then position
    all_cuts.sort(key=lambda c: (_AXIS_INDEX[c.axis], c.position))

    piece_count = 1
    for axis in ('x', 'y', 'z'):
        n = sum(1 for c in all_cuts if c.axis == axis)
        if n:
            piece_count *= (n + 1)

    return SlicePlan(
        model_dims=model_dims,
        build_volume=build_volume,
        tilt_envelope=tilt_env,
        cuts=all_cuts,
        piece_count=piece_count,
        detail_zones_avoided=[dz for dz in detail_zones
                              if dz.axis in {c.axis for c in all_cuts}],
    )


def avoid_detail_zones(positions, detail_zones, clearance=DETAIL_CLEARANCE):
    """Nudge cut positions away from detail zones.

    For each position that falls within `clearance` of a detail zone,
    push it to the nearer edge (lo - clearance or hi + clearance).

    Parameters
    ----------
    positions : list of float
        Proposed cut positions along one axis.
    detail_zones : list of DetailZone
        Zones to avoid on this axis.
    clearance : float
        Minimum distance from zone boundary.

    Returns
    -------
    list of float
        Adjusted positions (same length, same order).
    """
    result = list(positions)
    for i, pos in enumerate(result):
        for dz in detail_zones:
            if dz.lo - clearance <= pos <= dz.hi + clearance:
                # Which edge is closer?
                dist_lo = abs(pos - (dz.lo - clearance))
                dist_hi = abs(pos - (dz.hi + clearance))
                if dist_lo <= dist_hi:
                    result[i] = dz.lo - clearance
                else:
                    result[i] = dz.hi + clearance
    return result


def verify_plan(plan):
    """Check that every piece in a SlicePlan fits the build volume.

    Returns
    -------
    list of dict
        Per-piece fit report: {'piece': int, 'dims': tuple,
        'tilted': tuple, 'fits': bool, 'overflow': tuple}
    """
    results = []
    bv = plan.build_volume

    for axis in ('x', 'y', 'z'):
        ai = _AXIS_INDEX[axis]
        axis_cuts = plan.cuts_on_axis(axis)
        if not axis_cuts:
            continue

        # Compute piece dimensions along this axis
        positions = [c.position for c in axis_cuts]
        origin = plan.model_dims[ai]  # We'd need origin for real positions
        # For verification, use the planned piece size
        piece_len = plan.model_dims[ai] / (len(axis_cuts) + 1)

        piece_dims = list(plan.model_dims)
        piece_dims[ai] = piece_len
        tilted = compute_tilt_envelope(tuple(piece_dims))
        overflow = tuple(tilted[j] - bv[j] for j in range(3))
        fits = all(o <= 0 for o in overflow)

        results.append({
            'axis': axis,
            'piece_dim': piece_len,
            'tilted_dims': tilted,
            'fits': fits,
            'overflow': overflow,
        })

    return results


# ---------------------------------------------------------------------------
# FreeCAD-dependent functions
# ---------------------------------------------------------------------------

def analyze_model(shape):
    """Extract geometry analysis from a FreeCAD shape for slice planning.

    Parameters
    ----------
    shape : Part.Shape
        The model to analyze (typically a compound or fusion).

    Returns
    -------
    ModelAnalysis
    """
    bb = shape.BoundBox
    dims = (bb.XLength, bb.YLength, bb.ZLength)
    origin = (bb.XMin, bb.YMin, bb.ZMin)

    solids = shape.Solids
    if not solids:
        return ModelAnalysis(
            dims=dims, origin=origin, main_solid_volume=shape.Volume,
            detail_zones=[], solid_count=0)

    # Find main solid (largest by volume)
    volumes = [(s.Volume, i) for i, s in enumerate(solids)]
    volumes.sort(reverse=True)
    main_volume = volumes[0][0]

    # Find detail zones from small solids
    detail_zones = find_detail_zones(solids, main_volume)

    return ModelAnalysis(
        dims=dims, origin=origin, main_solid_volume=main_volume,
        detail_zones=detail_zones, solid_count=len(solids))


def find_detail_zones(solids, main_volume=None):
    """Identify detail zones from small solids in a compound.

    Small solids (< VOLUME_THRESHOLD or < 1% of main solid) are clustered
    by proximity along each axis, and each cluster becomes a DetailZone.

    Parameters
    ----------
    solids : list of Part.Solid
        All solids in the compound.
    main_volume : float, optional
        Volume of the main solid (for relative threshold).

    Returns
    -------
    list of DetailZone
    """
    if main_volume is None:
        volumes = [s.Volume for s in solids]
        main_volume = max(volumes) if volumes else 0

    threshold = min(VOLUME_THRESHOLD, main_volume * 0.01)

    small_solids = [s for s in solids if s.Volume < threshold]
    if not small_solids:
        return []

    detail_zones = []
    for axis in ('x', 'y', 'z'):
        # Collect axis ranges of small solids
        ranges = []
        for s in small_solids:
            bb = s.BoundBox
            if axis == 'x':
                ranges.append((bb.XMin, bb.XMax))
            elif axis == 'y':
                ranges.append((bb.YMin, bb.YMax))
            else:
                ranges.append((bb.ZMin, bb.ZMax))

        # Merge overlapping/nearby ranges into clusters
        clusters = _merge_ranges(ranges, gap=DETAIL_CLEARANCE)
        for lo, hi in clusters:
            detail_zones.append(DetailZone(axis=axis, lo=lo, hi=hi,
                                           label=f"{len(small_solids)} small solids"))

    return detail_zones


def _merge_ranges(ranges, gap=0):
    """Merge overlapping or nearby ranges.

    Parameters
    ----------
    ranges : list of (lo, hi) tuples
    gap : float
        Ranges within this distance are merged.

    Returns
    -------
    list of (lo, hi) tuples, sorted and non-overlapping.
    """
    if not ranges:
        return []
    sorted_ranges = sorted(ranges)
    merged = [sorted_ranges[0]]
    for lo, hi in sorted_ranges[1:]:
        prev_lo, prev_hi = merged[-1]
        if lo <= prev_hi + gap:
            merged[-1] = (prev_lo, max(prev_hi, hi))
        else:
            merged.append((lo, hi))
    return merged


def execute_slice_plan(shape, plan):
    """Execute a SlicePlan on a FreeCAD shape.

    Applies cuts sequentially along each axis using split_and_register
    from split_utils.

    Parameters
    ----------
    shape : Part.Shape
        The model to slice.
    plan : SlicePlan
        The cut plan from plan_cuts().

    Returns
    -------
    list of Part.Shape
        The resulting pieces, each with registration features.
    """
    import split_utils

    pieces = [shape]

    # Process one axis at a time
    for axis in ('x', 'y', 'z'):
        axis_cuts = plan.cuts_on_axis(axis)
        if not axis_cuts:
            continue

        positions = [c.position for c in axis_cuts]
        new_pieces = []

        for piece in pieces:
            sub_pieces = _split_sequential(piece, axis, positions)
            new_pieces.extend(sub_pieces)

        pieces = new_pieces

    logger.info("Sliced into %d pieces (planned %d)",
                len(pieces), plan.piece_count)
    return pieces


def _split_sequential(shape, axis, positions):
    """Split a shape at multiple positions along one axis, sequentially.

    Positions must be sorted. Each cut splits the remaining positive half.

    Parameters
    ----------
    shape : Part.Shape
        Shape to split.
    axis : str
        'x', 'y', or 'z'.
    positions : list of float
        Sorted cut positions.

    Returns
    -------
    list of Part.Shape
        n+1 pieces for n cuts, each with registration pins/sockets.
    """
    import split_utils

    ai = _AXIS_INDEX[axis]
    bb = shape.BoundBox
    lo = [bb.XMin, bb.YMin, bb.ZMin][ai]
    hi = [bb.XMax, bb.YMax, bb.ZMax][ai]

    # Filter positions that are actually inside this shape's range
    valid_pos = [p for p in sorted(positions) if lo < p < hi]

    if not valid_pos:
        return [shape]

    pieces = []
    remainder = shape
    for pos in valid_pos:
        neg, pos_half = split_utils.split_and_register(
            remainder, axis, pos)
        pieces.append(neg)
        remainder = pos_half

    pieces.append(remainder)
    return pieces


def auto_slice(source, printer='m7_pro', tilt_x_deg=DEFAULT_TILT_X,
               tilt_z_deg=DEFAULT_TILT_Z, margin=CUT_MARGIN, doc=None):
    """Top-level auto-slice: analyze, plan, and execute.

    Creates a working copy, analyzes it, plans cuts, and executes them.
    Each resulting piece is added to the document with Metadata provenance.

    Parameters
    ----------
    source : str or FreeCAD DocumentObject
        Source object name or object.
    printer : str
        Printer key from PRINTER_VOLUMES.
    tilt_x_deg, tilt_z_deg : float
        Expected print tilt angles.
    margin : float
        Safety margin from build volume edges.
    doc : FreeCAD.Document, optional
        Document to operate in.

    Returns
    -------
    dict with keys:
        'pieces': list of Part.Shape
        'plan': SlicePlan
        'analysis': ModelAnalysis
        'working_copy': DocumentObject (the intermediate copy, removed)
    """
    import FreeCAD
    from support_utils import create_working_copy, record_pipeline_step

    if doc is None:
        doc = FreeCAD.ActiveDocument

    bv = PRINTER_VOLUMES.get(printer)
    if bv is None:
        raise ValueError(f"Unknown printer '{printer}'. "
                         f"Known: {list(PRINTER_VOLUMES.keys())}")

    # 1. Create working copy
    wc = create_working_copy(source, doc=doc)

    # 2. Analyze
    analysis = analyze_model(wc.Shape)
    logger.info("Model: %.1f x %.1f x %.1f mm, %d solids",
                *analysis.dims, analysis.solid_count)

    # 3. Plan
    plan = plan_cuts(analysis.dims, analysis.origin, bv,
                     tilt_x_deg, tilt_z_deg, margin,
                     detail_zones=analysis.detail_zones)

    if not plan.cuts:
        logger.info("Model fits printer without slicing")
        record_pipeline_step(wc, "auto_slice(no_cuts)")
        return {'pieces': [wc.Shape], 'plan': plan,
                'analysis': analysis, 'objects': [wc]}

    logger.info("Plan: %d cuts → %d pieces", len(plan.cuts), plan.piece_count)
    for cut in plan.cuts:
        logger.info("  %s", cut)

    # 4. Execute
    record_pipeline_step(wc, "auto_slice")
    pieces = execute_slice_plan(wc.Shape, plan)

    # 5. Create document objects for each piece
    doc.openTransaction("Auto-slice pieces")
    try:
        piece_objects = []
        base_label = wc.Label.replace("_print", "")
        for i, piece_shape in enumerate(pieces):
            label = f"{base_label}_piece_{i+1}_of_{len(pieces)}"
            obj = doc.addObject("Part::Feature", label)
            obj.Shape = piece_shape

            obj.addProperty("App::PropertyString", "GeneratorName",
                           "Metadata", "Generator name")
            obj.GeneratorName = "print_pipeline"
            obj.addProperty("App::PropertyString", "GeneratorVersion",
                           "Metadata", "Generator version")
            obj.GeneratorVersion = "1.0.0"
            obj.addProperty("App::PropertyString", "SourceObject",
                           "Metadata", "Original object")
            obj.SourceObject = wc.SourceObject
            obj.addProperty("App::PropertyString", "PipelineSteps",
                           "Metadata", "Pipeline steps applied")
            obj.PipelineSteps = "copy;auto_slice"
            obj.addProperty("App::PropertyString", "PieceInfo",
                           "Metadata", "Piece number and cut details")
            obj.PieceInfo = f"piece {i+1}/{len(pieces)}"

            piece_objects.append(obj)

        doc.recompute()
        doc.commitTransaction()
    except Exception:
        doc.abortTransaction()
        raise

    return {'pieces': pieces, 'plan': plan,
            'analysis': analysis, 'objects': piece_objects}
