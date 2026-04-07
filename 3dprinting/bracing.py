"""
Temporary bracing (sprue runner) utilities for split model printing.

Adds thin runners connecting pin bases along interior wall edges of
split faces. Runners have neck-down sections at each connection point
for easy snap-off after printing.

Usage (from FreeCAD MCP execute_python):
    from bracing import add_bracing, add_bracing_both

All dimensions are in print-scale mm (not prototype scale).
"""

import Part
import FreeCAD
from FreeCAD import Vector
import logging

from registration import (
    PIN_RADIUS,
    analyze_cut_face,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants -- temporary bracing (sprue runners)
# ---------------------------------------------------------------------------

BRACE_WIDTH = 1.5              # mm -- runner width (perpendicular to run direction)
BRACE_DEPTH = 1.0              # mm -- runner depth (along plane normal, straddles split)
BRACE_NECK_WIDTH = 0.4         # mm -- thin neck at pin connections for snap-off
BRACE_NECK_LENGTH = 1.5        # mm -- length of neck-down zone at each end
BRACE_OFFSET = 0.0             # mm -- offset from wall into hollow (0 = flush with wall)


# ---------------------------------------------------------------------------
# Geometry helpers
# ---------------------------------------------------------------------------

def _project_point_to_edge(point, edge):
    """
    Project a point onto an edge and return (parameter, projected_point, distance).
    """
    try:
        dist_info = edge.distToShape(Part.Vertex(point))
        closest_pt = dist_info[1][0][1]
        param = edge.Curve.parameter(closest_pt)
        return param, closest_pt, dist_info[0]
    except Exception:
        return None, None, float('inf')


def _find_pins_near_edge(pin_positions, edge, max_dist=3.0):
    """
    Find pin positions that are close to a given edge.

    Returns list of (pin_index, parameter_on_edge, projected_point)
    sorted by parameter along the edge.
    """
    results = []
    for i, pos in enumerate(pin_positions):
        param, proj_pt, dist = _project_point_to_edge(pos, edge)
        if dist <= max_dist and param is not None:
            results.append((i, param, proj_pt))

    results.sort(key=lambda x: x[1])
    return results


def _interior_direction_at(point, edge, plane_normal, bb_center):
    """
    Compute the direction from an interior edge point into the hollow.

    Returns a unit vector pointing into the hollow (toward BB center),
    or None if degenerate.
    """
    n = Vector(plane_normal)
    n.normalize()

    to_center = bb_center - point
    to_center = to_center - n * to_center.dot(n)
    try:
        param = edge.Curve.parameter(point)
        tangent = edge.tangentAt(param)
        to_center = to_center - tangent * to_center.dot(tangent)
    except Exception:
        pass
    if to_center.Length < 1e-6:
        return None
    to_center.normalize()
    return to_center


def _make_runner_segment(start, end, plane_normal, interior_dir,
                         width=BRACE_WIDTH, depth=BRACE_DEPTH):
    """
    Make a single runner segment (rectangular bar) between two points.

    The bar runs from start to end, with cross-section width x depth.
    Width is measured perpendicular to both the run direction and
    plane_normal (i.e., into the hollow / along the wall).
    Depth straddles the split plane along plane_normal.
    """
    n = Vector(plane_normal)
    n.normalize()
    d = Vector(interior_dir)
    d.normalize()

    run_dir = end - start
    length = run_dir.Length
    if length < 0.01:
        return None
    run_dir.normalize()

    hd = depth / 2

    c1 = start - n * hd + d * BRACE_OFFSET
    c2 = start + n * hd + d * BRACE_OFFSET
    c3 = start + n * hd + d * (BRACE_OFFSET + width)
    c4 = start - n * hd + d * (BRACE_OFFSET + width)

    wire = Part.makePolygon([c1, c2, c3, c4, c1])
    face = Part.Face(wire)
    bar = face.extrude(run_dir * length)
    return bar


def _make_neck_notch(point, run_dir, plane_normal, interior_dir,
                     width=BRACE_WIDTH, depth=BRACE_DEPTH,
                     neck_width=BRACE_NECK_WIDTH,
                     neck_length=BRACE_NECK_LENGTH):
    """
    Make a pair of notch solids that, when subtracted from a runner,
    create a thin neck at a connection point.

    The notches cut from both sides of the runner (along interior_dir),
    leaving only neck_width of material in the center.

    Returns a list of Part.Shape (notch solids to subtract).
    """
    n = Vector(plane_normal)
    n.normalize()
    d = Vector(interior_dir)
    d.normalize()
    r = Vector(run_dir)
    r.normalize()

    hd = depth / 2
    hl = neck_length / 2
    notch_depth = (width - neck_width) / 2

    if notch_depth < 0.05:
        return []

    notches = []

    # Wall-side notch
    notch_profile_start = point - r * hl + d * BRACE_OFFSET
    notch_profile_end = point - r * hl + d * (BRACE_OFFSET + notch_depth)
    notch_c1 = notch_profile_start - n * (hd + 0.1)
    notch_c2 = notch_profile_start + n * (hd + 0.1)
    notch_c3 = notch_profile_end + n * (hd + 0.1)
    notch_c4 = notch_profile_end - n * (hd + 0.1)
    notch_wire = Part.makePolygon([notch_c1, notch_c2, notch_c3, notch_c4, notch_c1])
    notch_face = Part.Face(notch_wire)
    wall_notch = notch_face.extrude(r * neck_length)
    notches.append(wall_notch)

    # Hollow-side notch
    notch_profile_start2 = point - r * hl + d * (BRACE_OFFSET + width - notch_depth)
    notch_profile_end2 = point - r * hl + d * (BRACE_OFFSET + width + 0.1)
    notch_c5 = notch_profile_start2 - n * (hd + 0.1)
    notch_c6 = notch_profile_start2 + n * (hd + 0.1)
    notch_c7 = notch_profile_end2 + n * (hd + 0.1)
    notch_c8 = notch_profile_end2 - n * (hd + 0.1)
    notch_wire2 = Part.makePolygon([notch_c5, notch_c6, notch_c7, notch_c8, notch_c5])
    notch_face2 = Part.Face(notch_wire2)
    hollow_notch = notch_face2.extrude(r * neck_length)
    notches.append(hollow_notch)

    return notches


def _is_floor_edge(edge, plane_normal, floor_normal_threshold=0.7):
    """
    Determine if an interior edge is a floor/ceiling edge vs a wall edge.

    Currently a stub -- returns False. Reserved for future heuristics.
    """
    return False


# ---------------------------------------------------------------------------
# Bracing pipeline
# ---------------------------------------------------------------------------

def add_bracing(piece, plane_point, plane_normal, pin_positions,
                exclude_floor=True):
    """
    Add temporary sprue-like bracing connecting pin bases along interior walls.

    Braces are thin runners that connect adjacent pin positions along
    interior wall edges of the split face. They provide structural
    support during printing and are designed for snap-off removal via
    thin neck-down sections at each pin connection.

    Call this AFTER pin/socket placement, passing the pin positions used.

    Parameters
    ----------
    piece : Part.Shape
        The split half to add bracing to (typically the piece with pins).
    plane_point : Vector
        A point on the split plane.
    plane_normal : Vector
        Normal of the split plane.
    pin_positions : list of Vector
        Pin center positions on the split face.
    exclude_floor : bool
        If True, skip edges classified as floor edges (reserved).

    Returns
    -------
    Part.Shape
        The piece with bracing runners fused on.
    """
    if len(pin_positions) < 2:
        logger.info("Need at least 2 pins for bracing -- skipping")
        return piece

    n = Vector(plane_normal)
    n.normalize()

    analysis = analyze_cut_face(piece, plane_point, n)
    if analysis is None:
        logger.warning("Could not find split face for bracing")
        return piece

    interior_edges = analysis.interior_edges
    if not interior_edges:
        logger.warning("No interior edges found -- skipping bracing")
        return piece

    runner_shapes = []
    notch_shapes = []
    connections_made = 0

    for edge in interior_edges:
        if exclude_floor and _is_floor_edge(edge, n):
            continue

        pins_on_edge = _find_pins_near_edge(pin_positions, edge,
                                             max_dist=PIN_RADIUS + BRACE_WIDTH + 1.0)
        if len(pins_on_edge) < 2:
            continue

        edge_mid = edge.valueAt(
            (edge.FirstParameter + edge.LastParameter) / 2)
        interior_dir = _interior_direction_at(
            edge_mid, edge, n, analysis.bb_center)
        if interior_dir is None:
            continue

        for j in range(len(pins_on_edge) - 1):
            idx_a, param_a, proj_a = pins_on_edge[j]
            idx_b, param_b, proj_b = pins_on_edge[j + 1]

            start = pin_positions[idx_a]
            end = pin_positions[idx_b]

            seg_length = (end - start).Length
            if seg_length < BRACE_NECK_LENGTH * 2 + 0.5:
                continue

            runner = _make_runner_segment(start, end, n, interior_dir)
            if runner is None:
                continue
            runner_shapes.append(runner)

            run_dir = end - start
            run_dir.normalize()

            notches_start = _make_neck_notch(start, run_dir, n, interior_dir)
            notches_end = _make_neck_notch(end, run_dir, n, interior_dir)
            notch_shapes.extend(notches_start)
            notch_shapes.extend(notches_end)
            connections_made += 1

    if not runner_shapes:
        logger.info("No brace connections possible between pins")
        return piece

    if len(runner_shapes) == 1:
        runner_compound = runner_shapes[0]
    else:
        runner_compound = runner_shapes[0]
        for rs in runner_shapes[1:]:
            runner_compound = runner_compound.fuse(rs)

    if notch_shapes:
        notch_compound = Part.Compound(notch_shapes)
        runner_compound = runner_compound.cut(notch_compound)

    result = piece.fuse(runner_compound)

    print(f"Added {connections_made} brace runner(s) connecting "
          f"{len(pin_positions)} pin positions")
    return result


def add_bracing_both(neg_half, pos_half, plane_point, plane_normal,
                     pin_positions):
    """
    Add bracing to both halves of a split model.

    Parameters
    ----------
    neg_half, pos_half : Part.Shape
        The two registered halves.
    plane_point : Vector
        A point on the split plane.
    plane_normal : Vector
        Normal of the split plane.
    pin_positions : list of Vector
        Pin positions used during registration.

    Returns
    -------
    tuple of (Part.Shape, Part.Shape)
        (neg_with_bracing, pos_with_bracing)
    """
    neg_result = add_bracing(neg_half, plane_point, plane_normal,
                              pin_positions)
    pos_result = add_bracing(pos_half, plane_point, plane_normal,
                              pin_positions)
    return neg_result, pos_result
