"""
Orientation optimization for 3D print preparation.

Given a part (mesh or shape) and a direction indicating the non-display side,
score candidate orientations and pick the best one for MSLA resin printing.

Scoring components (lower total is better):
  - Display-down penalty: display-side surface area pointing toward the plate.
    Supports would mar visible detail -- heavy penalty.
  - Overhang area: total area of downward-facing surfaces (needs supports).
  - Footprint area: XY projection size (peel force scales with this).
  - Fit gate: orientations that exceed the printer build volume are rejected.

Works on Mesh.Mesh inputs (per-facet normal/area).  No wall-shape assumptions.

Usage:
    from orientation import (
        OrientationCandidate, generate_candidates, score_candidate,
        pick_best_orientation,
    )

    cands = generate_candidates()
    result = pick_best_orientation(mesh, non_display_dir=(0, 0, -1),
                                    candidates=cands, printer='m7_pro')
    print(result.best)
"""

from __future__ import annotations

import math
import logging
from dataclasses import dataclass, field
from typing import Iterable, Sequence

import FreeCAD
from FreeCAD import Vector, Rotation

from constants import PRINTER_VOLUMES

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Scoring weights (tunable)
# ---------------------------------------------------------------------------

# Heavy penalty: 1 mm^2 of display surface facing down costs this many points.
DISPLAY_DOWN_WEIGHT = 10.0

# Overhang area: 1 mm^2 of downward-facing non-display surface.
# Needed for supports; less is better but not catastrophic.  Set low
# because supports on the non-display side are invisible and cleaned off
# after printing — the only real cost is peel force (scales with
# projected horizontal area, handled by FOOTPRINT) and extra resin.
OVERHANG_WEIGHT = 0.1

# Footprint: 1 mm^2 of XY bounding area.  Peel force scales with this.
FOOTPRINT_WEIGHT = 0.05

# Peel-force proxy.  On MSLA, each layer peels from the FEP separately.
# A big near-horizontal facet concentrates all its projected area into
# one layer's peel event; tilting spreads that same area across many
# layers, so per-layer peel is dramatically lower.
#
# Implementation: Z-bin the projected peel area of each downward facet
# (area * |nz|^POWER) by the world-frame Z it lives at.  A facet whose
# vertices span multiple 1mm Z-bands distributes its area across those
# bands.  peel_force = MAX bin value = worst single-layer peel.
#
# The max-not-sum is critical: scattered shingle step-undersides (many
# facets at different Z) shouldn't all count as peel risk, only their
# local concentration at any given Z matters.
#
# |nz|^POWER weights:
#   |nz|=1.00 (flat)     → 1.00
#   |nz|=0.95 (18° tilt) → 0.74
#   |nz|=0.87 (30°)      → 0.40
#   |nz|=0.71 (45°)      → 0.13
PEEL_FORCE_WEIGHT = 0.5
PEEL_FORCE_POWER = 6.0
PEEL_Z_BIN_MM = 1.0

# Non-display-not-down penalty.  Supports in MSLA grow upward from the plate,
# so they can only contact surfaces that face downward.  When the non-display
# direction points toward the plate (-Z world), supports hit non-display
# cleanly.  When it points sideways or upward, supports are forced onto the
# display side — a much worse outcome than numbers alone suggest, because
# the cosmetic cost is hidden in tiny per-facet contributions.  Penalty
# is proportional to (1 + non_display_world.z), ranging 0 (ideal, -Z) to 2
# (worst, +Z).  The weight is set high enough to break ties between
# sideways and upright orientations in favor of upright.
NON_DISPLAY_NOT_DOWN_WEIGHT = 2000.0

# Classification thresholds
DOWNWARD_NZ_THRESHOLD = -0.2        # normal.z below this = downward-facing at all
SEVERE_OVERHANG_NZ = -0.6           # near-flat downward: true support-requiring overhang
                                    # Mild downward (-0.6..-0.2) on display side = cosmetic
                                    # (shingle step-lips, brick courses -- self-supporting)

# Non-display region is defined in the PART frame as a band near the extreme
# along non_display_dir (e.g., for a roof with non_display=(0,0,-1), the
# non-display band is the lowest Z).  Default is an absolute mm band — a tight
# value (~0.5mm) is appropriate for planar non-display regions (roof underside,
# slab mating face).  Fraction fallback is used only if the absolute value is
# None.
NON_DISPLAY_BAND_MM = 0.5           # absolute band width in mm
NON_DISPLAY_BAND_FRACTION = 0.05    # fallback: 5% of extent along non_display_dir

# Visibility-buffer parameters (facets hidden from the display direction
# by other geometry should not count as display_down penalty).
#
# Tuned for stepped-shingle roofs where step height ~0.7mm.  The tolerance
# must accept exterior slope facets that fall slightly below their upper
# neighbor's peak while still flagging deeper interior cavities as hidden.
VISIBILITY_GRID_MM = 1.0            # cell size in projection plane
VISIBILITY_TOL_MM = 1.0             # depth tolerance for "close to topmost"


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class OrientationCandidate:
    """A candidate print orientation expressed as a FreeCAD Rotation.

    The rotation maps part-local coordinates to print-frame coordinates
    (Z up, build plate at Z=0).
    """
    name: str
    rotation: Rotation

    def apply_to_vector(self, v):
        """Rotate a vector (no translation) into the print frame."""
        return self.rotation.multVec(Vector(v[0], v[1], v[2]))


@dataclass
class OrientationScore:
    """Scoring result for one orientation."""
    candidate: OrientationCandidate
    fits: bool
    footprint_xy: tuple  # (x_len, y_len) after rotation
    z_height: float
    overhang_area: float          # mm^2 of downward-facing non-display
    display_down_area: float      # mm^2 of downward-facing display (penalty)
    total_area: float
    peel_force: float             # sum(area * |nz|^PEEL_FORCE_POWER)
    penalty: float

    @property
    def summary(self):
        return (f"{self.candidate.name}: "
                f"fits={self.fits} "
                f"penalty={self.penalty:.1f} "
                f"(overhang={self.overhang_area:.0f}mm2, "
                f"display_down={self.display_down_area:.0f}mm2, "
                f"peel={self.peel_force:.0f}, "
                f"XY={self.footprint_xy[0]:.0f}x{self.footprint_xy[1]:.0f})")


@dataclass
class OrientationResult:
    """Full orientation pick result."""
    best: OrientationScore
    all_scored: list[OrientationScore]
    non_display_dir_rotated: Vector   # non-display direction after best rotation

    def report(self, top=5):
        lines = [f"Orientation picks (top {top} of {len(self.all_scored)}):"]
        for s in self.all_scored[:top]:
            lines.append(f"  {s.summary}")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Candidate generation
# ---------------------------------------------------------------------------

def _rot(axis, deg):
    return Rotation(Vector(*axis), deg)


def _compose(*rots):
    """Compose rotations left-to-right: result = r1 then r2 then r3."""
    out = Rotation()
    for r in rots:
        out = r.multiply(out)
    return out


def generate_candidates(include_tilts=True):
    """Generate a reasonable candidate set for MSLA print orientation.

    Starts with the 6 axis-aligned orientations (each of +X, -X, +Y, -Y,
    +Z, -Z pointing down).  Optionally adds mild tilts (X-tilt 10/18 deg,
    Z-twist 0/8 deg) to the "base down" orientation, which is usually
    the winner for roof-shaped parts.

    Returns
    -------
    list[OrientationCandidate]
    """
    cands = []

    # 6 axis-aligned: name describes which part-frame direction ends up pointing
    # DOWN toward the plate (i.e. the direction parallel to -Z after rotation).
    axis_orients = [
        ("part-Zdown",  Rotation()),                         # identity: part -Z → print -Z
        ("part+Zdown",  _rot((1, 0, 0), 180)),               # flip Z: part +Z → print -Z
        ("part+Ydown",  _rot((1, 0, 0), 90)),                # lay on +Y face
        ("part-Ydown",  _rot((1, 0, 0), -90)),               # lay on -Y face
        ("part+Xdown",  _rot((0, 1, 0), -90)),               # lay on +X face
        ("part-Xdown",  _rot((0, 1, 0), 90)),                # lay on -X face
    ]
    for name, rot in axis_orients:
        cands.append(OrientationCandidate(name=name, rotation=rot))

    if include_tilts:
        # Tilts applied on top of "part-Zdown" (part sits upright) and
        # "part+Zdown" (flipped).  We tilt on BOTH X and Y so the slab
        # peels diagonally from one corner rather than from a whole
        # edge at once — single-axis tilt still peels a full edge
        # simultaneously, two-axis tilt peels a growing triangular
        # wedge per layer.  X covers 10-30°, Y adds a milder secondary
        # tilt of 0-10°.
        x_tilts = (10, 18, 25, 30)
        y_tilts = (0, 10)
        base = _rot((0, 0, 1), 0)          # identity
        base_flip = _rot((1, 0, 0), 180)   # upside-down
        for base_name, base_rot in (("part-Zdown", base),
                                     ("part+Zdown", base_flip)):
            for tilt_x in x_tilts:
                for tilt_y in y_tilts:
                    rot = _compose(base_rot,
                                   _rot((1, 0, 0), tilt_x),
                                   _rot((0, 1, 0), tilt_y))
                    suffix = f"x{tilt_x}" + (f"y{tilt_y}" if tilt_y else "")
                    cands.append(OrientationCandidate(
                        name=f"{base_name}+tilt{suffix}",
                        rotation=rot,
                    ))

    return cands


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

def _mesh_facet_normal_area(facet):
    """Compute normal (unit) and area for a Mesh facet."""
    pts = facet.Points
    v0 = Vector(*pts[0])
    v1 = Vector(*pts[1])
    v2 = Vector(*pts[2])
    e1 = v1 - v0
    e2 = v2 - v0
    n = e1.cross(e2)
    L = n.Length
    if L < 1e-12:
        return Vector(0, 0, 0), 0.0
    return Vector(n.x / L, n.y / L, n.z / L), L * 0.5


def _rotated_bbox(mesh, rot):
    """Compute AABB of mesh points after rotation."""
    xs, ys, zs = [], [], []
    for pt in mesh.Points:
        v = rot.multVec(Vector(pt.Vector.x, pt.Vector.y, pt.Vector.z))
        xs.append(v.x); ys.append(v.y); zs.append(v.z)
    return (min(xs), max(xs), min(ys), max(ys), min(zs), max(zs))


BACK_FACE_THRESHOLD = 0.05  # dot(normal, display_dir) > this = viewer-facing


def classify_facet_visibility(mesh, display_dir_part,
                              grid=VISIBILITY_GRID_MM,
                              tol=VISIBILITY_TOL_MM):
    """Classify each facet as visible (True) or hidden (False) from
    the display direction (the direction the finished part is viewed from).

    Combines two cheap tests in the PART frame:

    1. Back-face culling.  Any facet whose normal does not point toward the
       viewer is a back-face and cannot be visible from the display
       direction.  This alone catches interior cavity ceilings, underside
       bottom flats, and stray inside-facing geometry — they all face
       away from the display direction, so `dot(normal, display_dir) <= 0`.

    2. Front-face z-buffer.  Among the remaining front-facing (viewer-
       facing) facets, run a coarse orthographic z-buffer: at each grid
       cell, track the maximum depth among front faces whose projected
       triangle covers that cell.  A facet is visible iff any cell in its
       bbox has this facet as the topmost front-face within tolerance.

    Works for axis-aligned display directions (±X, ±Y, ±Z).  For
    non-axis-aligned directions, returns all-True (orientation scoring
    will fall back to position-band classification).

    Parameters
    ----------
    mesh : Mesh.Mesh
    display_dir_part : sequence[3]
        Unit vector in the PART frame pointing OUT of the display surface.
        Typically `-non_display_dir`.
    grid : float
        Cell size in the projection plane, mm.
    tol : float
        Depth tolerance.  Larger values mark more facets as visible.

    Returns
    -------
    list[bool] of length mesh.CountFacets
    """
    dx, dy, dz = display_dir_part

    if abs(dz) > 0.99:
        u_idx, v_idx, d_idx = 0, 1, 2
        d_sign = 1.0 if dz > 0 else -1.0
    elif abs(dy) > 0.99:
        u_idx, v_idx, d_idx = 0, 2, 1
        d_sign = 1.0 if dy > 0 else -1.0
    elif abs(dx) > 0.99:
        u_idx, v_idx, d_idx = 1, 2, 0
        d_sign = 1.0 if dx > 0 else -1.0
    else:
        return [True] * mesh.CountFacets

    points, faces = mesh.Topology
    pts_tup = [(p.x, p.y, p.z) for p in points]
    n = len(faces)

    # Pass 1: compute normals and centroids; mark front-faces.
    # Also record centroid depth per facet for pass 2.
    front = [False] * n
    centroids = [None] * n
    normals = [None] * n
    for i in range(n):
        a, b, c = faces[i]
        p0 = pts_tup[a]; p1 = pts_tup[b]; p2 = pts_tup[c]
        # Normal via cross product
        e1x = p1[0] - p0[0]; e1y = p1[1] - p0[1]; e1z = p1[2] - p0[2]
        e2x = p2[0] - p0[0]; e2y = p2[1] - p0[1]; e2z = p2[2] - p0[2]
        nvx = e1y * e2z - e1z * e2y
        nvy = e1z * e2x - e1x * e2z
        nvz = e1x * e2y - e1y * e2x
        L2 = nvx*nvx + nvy*nvy + nvz*nvz
        if L2 < 1e-24:
            continue
        # dot(normal, display_dir).  display_dir has magnitude 1 along one axis.
        dot = (nvx * dx + nvy * dy + nvz * dz) / (L2 ** 0.5)
        cu = (p0[u_idx] + p1[u_idx] + p2[u_idx]) / 3.0
        cv = (p0[v_idx] + p1[v_idx] + p2[v_idx]) / 3.0
        cd = (p0[d_idx] + p1[d_idx] + p2[d_idx]) / 3.0 * d_sign
        centroids[i] = (cu, cv, cd)
        normals[i] = (nvx, nvy, nvz, L2 ** 0.5)
        if dot > BACK_FACE_THRESHOLD:
            front[i] = True

    # Pass 2: build per-cell max depth from front-facing facets only,
    # using centroid depth (simple and sufficient: back-face culling
    # already removes the problem cases).
    max_depth = {}
    for i in range(n):
        if not front[i] or centroids[i] is None:
            continue
        cu, cv, cd = centroids[i]
        gu = int(cu // grid); gv = int(cv // grid)
        key = (gu, gv)
        prev = max_depth.get(key)
        if prev is None or cd > prev:
            max_depth[key] = cd

    # Pass 3: a facet is visible iff it's front-facing AND within tol
    # of the cell's max front-face depth.
    visible = [False] * n
    for i in range(n):
        if not front[i] or centroids[i] is None:
            continue
        cu, cv, cd = centroids[i]
        gu = int(cu // grid); gv = int(cv // grid)
        best = -1e18
        for ddu in (-1, 0, 1):
            for ddv in (-1, 0, 1):
                b = max_depth.get((gu + ddu, gv + ddv))
                if b is not None and b > best:
                    best = b
        if cd >= best - tol:
            visible[i] = True
    return visible


def compute_non_display_threshold(mesh, non_display_dir, band_mm=None,
                                  band_fraction=None):
    """Return (threshold, max_proj) for non-display region in the PART frame.

    A facet centroid is "non-display" iff proj_centroid >= threshold.

    The band width prefers an absolute mm value (NON_DISPLAY_BAND_MM) since
    most non-display regions are planar (roof underside, slab top).  The
    fractional fallback is only used if both band_mm and
    NON_DISPLAY_BAND_MM are None.

    Parameters
    ----------
    mesh : Mesh.Mesh
    non_display_dir : sequence[3]
        Direction in part frame pointing OUT of the non-display region.
    band_mm : float or None
        Absolute band width in mm.  Defaults to NON_DISPLAY_BAND_MM.  If
        explicitly set to 0 or negative, falls back to band_fraction.
    band_fraction : float or None
        Fraction of part extent along non_display_dir.

    Returns
    -------
    (threshold, max_proj) : tuple of float
    """
    nd = Vector(non_display_dir[0], non_display_dir[1], non_display_dir[2])
    if nd.Length < 1e-9:
        return (float('-inf'), 0.0)
    nd.normalize()
    projections = []
    for pt in mesh.Points:
        v = pt.Vector
        projections.append(v.x * nd.x + v.y * nd.y + v.z * nd.z)
    if not projections:
        return (float('-inf'), 0.0)
    pmin = min(projections)
    pmax = max(projections)

    effective_mm = band_mm if band_mm is not None else NON_DISPLAY_BAND_MM
    if effective_mm is not None and effective_mm > 0:
        band_width = effective_mm
    else:
        frac = band_fraction if band_fraction is not None else NON_DISPLAY_BAND_FRACTION
        band_width = (pmax - pmin) * frac

    threshold = pmax - band_width
    return (threshold, pmax)


def score_candidate(mesh, non_display_dir, candidate, printer='m7_pro',
                    build_margin=2.0, band_mm=None, band_fraction=None,
                    visibility=None):
    """Score one orientation candidate against a mesh.

    Non-display classification is POSITION-BASED in the part frame: a facet
    centroid is non-display iff its projection onto `non_display_dir` falls
    within the outer band (default 15%) of the part's extent along that
    direction.  This correctly handles textured surfaces (shingles, brick)
    where many small facets point "outward" but are still on the display side.

    Parameters
    ----------
    mesh : Mesh.Mesh
        The part geometry.
    non_display_dir : Vector or sequence
        Unit vector in the PART frame pointing outward from the non-display
        region.  For a roof with shingled top, non_display = (0, 0, -1)
        (bottom is non-display).  For a slab whose top mates with another
        piece, non_display = (0, 0, +1).
    candidate : OrientationCandidate
    printer : str or None
        If not None, penalize orientations that don't fit.
    build_margin : float
    band_fraction : float or None
        Override for NON_DISPLAY_BAND_FRACTION.

    Returns
    -------
    OrientationScore
    """
    rot = candidate.rotation

    nd = Vector(non_display_dir[0], non_display_dir[1], non_display_dir[2])
    if nd.Length > 1e-9:
        nd.normalize()

    nd_threshold, _ = compute_non_display_threshold(
        mesh, non_display_dir, band_mm=band_mm, band_fraction=band_fraction)

    # AABB after rotation
    xmin, xmax, ymin, ymax, zmin, zmax = _rotated_bbox(mesh, rot)
    footprint_xy = (xmax - xmin, ymax - ymin)
    z_height = zmax - zmin

    # Build-volume fit
    fits = True
    if printer is not None:
        vol = PRINTER_VOLUMES.get(printer)
        if vol is None:
            raise ValueError(f"Unknown printer '{printer}'")
        fits = (footprint_xy[0] <= vol[0] - 2 * build_margin and
                footprint_xy[1] <= vol[1] - 2 * build_margin and
                z_height <= vol[2] - 2 * build_margin)

    # Per-facet classification and area accounting
    overhang_area = 0.0
    display_down_area = 0.0
    total_area = 0.0
    peel_bins = {}   # key = int(world_z / PEEL_Z_BIN_MM), value = summed peel area at that layer

    # Pull topology in bulk for speed
    points, faces = mesh.Topology
    pts_tup = [(p.x, p.y, p.z) for p in points]
    n_faces = len(faces)

    for i in range(n_faces):
        a, b, c = faces[i]
        p0 = pts_tup[a]; p1 = pts_tup[b]; p2 = pts_tup[c]
        # Centroid in PART frame (pre-rotation)
        cx = (p0[0] + p1[0] + p2[0]) / 3.0
        cy = (p0[1] + p1[1] + p2[1]) / 3.0
        cz = (p0[2] + p1[2] + p2[2]) / 3.0
        centroid_proj = cx * nd.x + cy * nd.y + cz * nd.z
        is_non_display_region = centroid_proj >= nd_threshold

        # Compute normal and area inline (avoid Facet object overhead)
        v0 = Vector(*p0); v1 = Vector(*p1); v2 = Vector(*p2)
        e1 = v1 - v0; e2 = v2 - v0
        nv = e1.cross(e2)
        L = nv.Length
        if L < 1e-12:
            continue
        area = L * 0.5
        total_area += area
        n_part = Vector(nv.x / L, nv.y / L, nv.z / L)

        # Rotate normal direction-only into world frame
        n_world = rot.multVec(n_part)
        downward = n_world.z < DOWNWARD_NZ_THRESHOLD
        if not downward:
            continue

        # Peel-force Z-binning: each downward facet contributes
        # area * |nz|^POWER of projected peel area to the Z-bins it
        # occupies.  A flat facet (three vertices at same Z) concentrates
        # into one bin — huge peel risk.  A tilted facet spreads across
        # multiple bins — small peel per layer.
        peel_contrib = area * (abs(n_world.z) ** PEEL_FORCE_POWER)
        z0 = rot.multVec(v0).z
        z1 = rot.multVec(v1).z
        z2 = rot.multVec(v2).z
        zmin_f = min(z0, z1, z2)
        zmax_f = max(z0, z1, z2)
        bin_lo = int(zmin_f // PEEL_Z_BIN_MM)
        bin_hi = int(zmax_f // PEEL_Z_BIN_MM)
        nbins = bin_hi - bin_lo + 1
        per_bin = peel_contrib / nbins
        for b in range(bin_lo, bin_hi + 1):
            peel_bins[b] = peel_bins.get(b, 0.0) + per_bin

        # Visibility is from the display direction.  Three cases for a
        # downward-facing facet in the current orientation:
        #   1. Non-display region: supports will land here — real cost
        #      is only peel force + material (low weight).
        #   2. Visible display region: supports would mar visible detail.
        #      This is the main penalty.
        #   3. Hidden (not visible) and not in non-display band: interior
        #      cavity facet that supports cannot reach and cannot mar any
        #      surface.  Ignore entirely.
        is_visible = True if visibility is None else bool(visibility[i])

        if is_non_display_region:
            overhang_area += area
        elif is_visible:
            # On visible display: only penalize severe (near-flat) downward
            # facets.  Use PROJECTED horizontal area (area * |nz|) so that
            # steep textured undersides (shingle-step facets at 30-45° slope)
            # contribute far less than true near-horizontal flats.  A flat
            # eave (nz=-1) counts fully; a shingle underside (nz~-0.85)
            # counts at 85% of its area; a 45° slope (nz=-0.71) at 71%.
            if n_world.z < SEVERE_OVERHANG_NZ:
                display_down_area += area * abs(n_world.z)
        # else: hidden interior facet — ignored

    # Non-display orientation: reward placing the non-display band toward
    # the plate, since only downward-facing surfaces receive MSLA supports.
    nd_world = rot.multVec(nd) if nd.Length > 1e-9 else Vector(0, 0, -1)
    nd_not_down = max(0.0, 1.0 + nd_world.z)   # 0 when nd points -Z, up to 2 when +Z

    # Peel-force: worst-case single layer.  Empty bins → zero peel.
    peel_force = max(peel_bins.values()) if peel_bins else 0.0

    footprint_area = footprint_xy[0] * footprint_xy[1]
    penalty = (DISPLAY_DOWN_WEIGHT * display_down_area
               + OVERHANG_WEIGHT * overhang_area
               + FOOTPRINT_WEIGHT * footprint_area
               + NON_DISPLAY_NOT_DOWN_WEIGHT * nd_not_down
               + PEEL_FORCE_WEIGHT * peel_force)
    if not fits:
        penalty += 1e9  # soft-rejected, but still ranked among unfit

    return OrientationScore(
        candidate=candidate,
        fits=fits,
        footprint_xy=footprint_xy,
        z_height=z_height,
        overhang_area=overhang_area,
        display_down_area=display_down_area,
        total_area=total_area,
        peel_force=peel_force,
        penalty=penalty,
    )


def pick_best_orientation(mesh, non_display_dir, candidates=None,
                          printer='m7_pro', build_margin=2.0,
                          use_visibility=True):
    """Score all candidates and pick the lowest-penalty one that fits.

    If `use_visibility` is True, a one-shot visibility classification is
    computed in the PART frame (display_dir = -non_display_dir) and shared
    across all candidates.  This removes interior / hidden facets from the
    display_down penalty, which is critical for hollow-shell inputs.

    Returns
    -------
    OrientationResult
    """
    if candidates is None:
        candidates = generate_candidates()

    visibility = None
    if use_visibility:
        display_dir = (-non_display_dir[0], -non_display_dir[1],
                       -non_display_dir[2])
        visibility = classify_facet_visibility(mesh, display_dir)

    scored = [score_candidate(mesh, non_display_dir, c, printer, build_margin,
                              visibility=visibility)
              for c in candidates]
    scored.sort(key=lambda s: s.penalty)

    best = scored[0]
    nd = Vector(non_display_dir[0], non_display_dir[1], non_display_dir[2])
    if nd.Length > 1e-9:
        nd.normalize()
    nd_rot = best.candidate.rotation.multVec(nd)

    return OrientationResult(
        best=best,
        all_scored=scored,
        non_display_dir_rotated=nd_rot,
    )


# ---------------------------------------------------------------------------
# Mesh rotation helper
# ---------------------------------------------------------------------------

def apply_rotation_to_mesh(mesh, rotation, shift_to_plate=True):
    """Return a new Mesh.Mesh rotated by `rotation`, optionally shifted
    so its min-Z sits at 0 (plate level).
    """
    import Mesh
    m = mesh.copy()
    placement = FreeCAD.Placement(Vector(0, 0, 0), rotation)
    m.transform(placement.toMatrix())
    if shift_to_plate:
        bb = m.BoundBox
        m.translate(-bb.XMin - (bb.XLength / 2), -bb.YMin - (bb.YLength / 2), -bb.ZMin)
    return m
