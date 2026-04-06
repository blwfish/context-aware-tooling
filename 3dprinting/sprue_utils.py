"""
Sprue/runner frame generator for resin MSLA batch printing.

Arranges multiple copies of a part on a sprue tree with runners and gates,
similar to injection-molded part sprues. Parts can be trimmed from the sprue
with fine scissors or a chisel blade after printing.

Usage (from FreeCAD MCP execute_python):
    from sprue_utils import make_sprue

    # 10 copies of a window frame in a 2x5 grid:
    sprue = make_sprue(shape, count=10, cols=5)

All dimensions are in print-scale mm.
"""

import Part
import FreeCAD
from FreeCAD import Vector
import math
import logging

logger = logging.getLogger(__name__)

# --- Sprue defaults (mm, print scale) ---
RUNNER_WIDTH = 1.0        # cross-section width of runner bars
GATE_WIDTH = 0.4          # gate connection width (thin for easy trimming)
GATE_LENGTH = 1.0         # gap between part edge and runner
PART_SPACING = 2.0        # extra gap between parts (beyond gate+runner)
GATE_SPACING = 5.0        # target spacing between gates along an edge
GATE_OVERLAP = 0.5        # how far gates extend into part BB to ensure connection


def _detect_axes(shape):
    """Detect thin/narrow/tall axes of a flat part.

    Returns dict with 'thin', 'narrow', 'tall' keys, each mapping to
    (axis_index, axis_name, size) where axis_index is 0=X, 1=Y, 2=Z.
    """
    bb = shape.BoundBox
    dims = [(0, 'x', bb.XLength), (1, 'y', bb.YLength), (2, 'z', bb.ZLength)]
    dims.sort(key=lambda d: d[2])
    return {
        'thin': dims[0],
        'narrow': dims[1],
        'tall': dims[2],
    }


def _layout_grid(count, cols=None):
    """Compute rows/cols for a grid layout."""
    if cols is None:
        cols = math.ceil(math.sqrt(count))
    rows = math.ceil(count / cols)
    return rows, cols


def _make_box(narrow_pos, narrow_size, tall_pos, tall_size, thin_pos, thin_size, axes):
    """Create an axis-aligned box using logical axes."""
    pos = [0.0, 0.0, 0.0]
    size = [0.0, 0.0, 0.0]

    narrow_idx = axes['narrow'][0]
    tall_idx = axes['tall'][0]
    thin_idx = axes['thin'][0]

    pos[narrow_idx] = narrow_pos
    pos[tall_idx] = tall_pos
    pos[thin_idx] = thin_pos

    size[narrow_idx] = narrow_size
    size[tall_idx] = tall_size
    size[thin_idx] = thin_size

    return Part.makeBox(size[0], size[1], size[2],
                        Vector(pos[0], pos[1], pos[2]))


def _gate_positions(edge_length, gate_width, gate_spacing):
    """Compute gate center positions along an edge.

    Returns list of offsets from the edge start. Always at least 1 gate
    at the center; more are added based on gate_spacing.
    """
    n_gates = max(1, round(edge_length / gate_spacing))
    if n_gates == 1:
        return [edge_length / 2]
    margin = edge_length / (n_gates + 1)
    return [margin + i * margin for i in range(n_gates)]


def _probe_material_along_edge(shape, edge_start, edge_dir, edge_length,
                               probe_dir, probe_depth, sample_step=0.5):
    """Find contiguous material regions along a bounding box edge.

    Casts probe rays along probe_dir at sample_step intervals along the edge.
    Returns list of (start_offset, end_offset) material regions.

    Args:
        shape: Part to probe.
        edge_start: Start point on the edge (Vector).
        edge_dir: Unit direction along the edge (Vector).
        edge_length: Length of the edge.
        probe_dir: Direction to probe for material (into the part).
        probe_depth: How far inside to check.
        sample_step: Sampling resolution along the edge.
    """
    hits = []
    n_samples = max(2, int(edge_length / sample_step) + 1)
    step = edge_length / (n_samples - 1)

    for i in range(n_samples):
        offset = i * step
        pt = edge_start + edge_dir * offset + probe_dir * (probe_depth / 2)
        if shape.isInside(pt, 0.01, True):
            hits.append(offset)

    if not hits:
        return []

    # Merge into contiguous regions
    regions = []
    region_start = hits[0]
    prev = hits[0]
    for h in hits[1:]:
        if h - prev > sample_step * 1.5:
            regions.append((region_start, prev))
            region_start = h
        prev = h
    regions.append((region_start, prev))

    return regions


def _measure_local_thickness(shape, point, thin_dir, max_thickness, step=0.05):
    """Measure how thick the part is at a point along the thin direction.

    Probes from the thin_min surface inward to find where material ends.
    Returns (thin_offset, local_thickness) — the offset from thin_min where
    material starts, and how thick it is there.
    """
    # Find where material starts and ends along thin_dir from the point
    # Point should be at the part edge (on the edge_dir/probe_dir plane)
    # We probe along thin_dir from 0 to max_thickness

    first_hit = None
    last_hit = None
    n = max(2, int(max_thickness / step) + 1)

    for i in range(n + 1):
        t = i * max_thickness / n
        pt = point + thin_dir * t
        if shape.isInside(pt, 0.005, True):
            if first_hit is None:
                first_hit = t
            last_hit = t

    if first_hit is None:
        return 0.0, max_thickness  # fallback: use full thickness

    # Clamp to max_thickness to avoid overshoot from step size
    local = min(last_hit - first_hit + step, max_thickness - first_hit)
    return first_hit, local


def _gate_positions_on_material(shape, edge_start, edge_dir, edge_length,
                                probe_dir, probe_depth,
                                gate_width, gate_spacing):
    """Place gates where the part actually has material along an edge.

    Returns list of center offsets along the edge where gates should go.
    """
    regions = _probe_material_along_edge(
        shape, edge_start, edge_dir, edge_length,
        probe_dir, probe_depth)

    if not regions:
        return []

    positions = []
    for rstart, rend in regions:
        rlen = rend - rstart
        if rlen < gate_width:
            # Region too small, place one gate at center
            positions.append(rstart + rlen / 2)
        else:
            # Distribute gates within this region
            for gpos in _gate_positions(rlen, gate_width, gate_spacing):
                positions.append(rstart + gpos)

    return positions


def make_sprue(shape, count=10, cols=None,
               runner_width=RUNNER_WIDTH,
               gate_width=GATE_WIDTH,
               gate_length=GATE_LENGTH,
               part_spacing=PART_SPACING,
               gate_spacing=GATE_SPACING,
               gate_overlap=GATE_OVERLAP):
    """Create a sprue tree with multiple copies of a part.

    Args:
        shape: FreeCAD Shape to replicate (should be a flat part).
        count: Number of copies.
        cols: Columns in the grid (auto if None).
        runner_width: Width of runner bars (mm).
        gate_width: Width of gate connections (mm).
        gate_length: Gap between part edge and runner (mm).
        part_spacing: Extra spacing between parts (mm).
        gate_spacing: Target spacing between gates along an edge (mm).
        gate_overlap: How far gates extend into part BB (mm).

    Returns:
        Part.Shape — fused sprue with all parts and runners.
    """
    rows, cols = _layout_grid(count, cols)
    axes = _detect_axes(shape)
    bb = shape.BoundBox

    thin_idx = axes['thin'][0]
    narrow_idx = axes['narrow'][0]
    tall_idx = axes['tall'][0]

    thickness = axes['thin'][2]
    narrow_size = axes['narrow'][2]
    tall_size = axes['tall'][2]

    bb_mins = [bb.XMin, bb.YMin, bb.ZMin]
    thin_min = bb_mins[thin_idx]
    narrow_min = bb_mins[narrow_idx]
    tall_min = bb_mins[tall_idx]

    col_pitch = narrow_size + 2 * gate_length + part_spacing
    row_pitch = tall_size + 2 * gate_length + part_spacing

    logger.info(f"Sprue layout: {rows}x{cols}, pitch: {col_pitch:.1f}x{row_pitch:.1f}mm, "
                f"thin={axes['thin'][1]}({thickness:.2f}mm)")

    # --- Place part copies ---
    unit_vec = [Vector(1, 0, 0), Vector(0, 1, 0), Vector(0, 0, 1)]
    narrow_dir = unit_vec[narrow_idx]
    tall_dir = unit_vec[tall_idx]

    parts = []
    part_positions = []  # (row, col, narrow_offset, tall_offset)

    for r in range(rows):
        for c in range(cols):
            idx = r * cols + c
            if idx >= count:
                break
            offset = narrow_dir * (c * col_pitch) + tall_dir * (r * row_pitch)
            copied = shape.copy()
            copied.translate(offset)
            parts.append(copied)
            part_positions.append((r, c, c * col_pitch, r * row_pitch))

    # Build a lookup for which cells are occupied
    occupied = set((r, c) for r, c, _, _ in part_positions)

    # --- Runners ---
    runners = []

    # Horizontal runner positions (along tall axis)
    h_runner_positions = []
    for r in range(rows + 1):
        if r == 0:
            t_pos = tall_min - gate_length - runner_width
        elif r == rows:
            t_pos = tall_min + (rows - 1) * row_pitch + tall_size + gate_length
        else:
            t_pos = tall_min + r * row_pitch - part_spacing / 2 - runner_width / 2
        h_runner_positions.append(t_pos)

    # Vertical runner positions (along narrow axis)
    v_runner_narrow_start = narrow_min - gate_length - runner_width
    v_runner_narrow_end = narrow_min + (cols - 1) * col_pitch + narrow_size + gate_length
    v_runner_positions = [v_runner_narrow_start, v_runner_narrow_end]

    # Horizontal runners span full width including vertical runners
    frame_narrow_start = v_runner_narrow_start
    frame_narrow_end = v_runner_narrow_end + runner_width
    frame_narrow_span = frame_narrow_end - frame_narrow_start

    for t_pos in h_runner_positions:
        runners.append(_make_box(
            narrow_pos=frame_narrow_start,
            narrow_size=frame_narrow_span,
            tall_pos=t_pos,
            tall_size=runner_width,
            thin_pos=thin_min,
            thin_size=thickness,
            axes=axes,
        ))

    # Vertical runners span full height including horizontal runners
    frame_tall_start = h_runner_positions[0]
    frame_tall_end = h_runner_positions[-1] + runner_width
    frame_tall_span = frame_tall_end - frame_tall_start

    for n_pos in v_runner_positions:
        runners.append(_make_box(
            narrow_pos=n_pos,
            narrow_size=runner_width,
            tall_pos=frame_tall_start,
            tall_size=frame_tall_span,
            thin_pos=thin_min,
            thin_size=thickness,
            axes=axes,
        ))

    # --- Probe original shape for material positions on each edge ---
    thin_dir = unit_vec[thin_idx]
    thin_max = bb_mins[thin_idx] + thickness

    # Bottom edge (tall_min side): probe inward along tall direction
    bottom_gate_offsets = _gate_positions_on_material(
        shape,
        edge_start=narrow_dir * narrow_min + tall_dir * tall_min + thin_dir * thin_min,
        edge_dir=narrow_dir,
        edge_length=narrow_size,
        probe_dir=tall_dir,
        probe_depth=1.0,
        gate_width=gate_width,
        gate_spacing=gate_spacing,
    )
    # Top edge (tall_max side): probe inward along -tall direction
    top_gate_offsets = _gate_positions_on_material(
        shape,
        edge_start=narrow_dir * narrow_min + tall_dir * (tall_min + tall_size) + thin_dir * thin_min,
        edge_dir=narrow_dir,
        edge_length=narrow_size,
        probe_dir=tall_dir * (-1),
        probe_depth=1.0,
        gate_width=gate_width,
        gate_spacing=gate_spacing,
    )
    # Left edge (narrow_min side): probe inward along narrow direction
    left_gate_offsets = _gate_positions_on_material(
        shape,
        edge_start=narrow_dir * narrow_min + tall_dir * tall_min + thin_dir * thin_min,
        edge_dir=tall_dir,
        edge_length=tall_size,
        probe_dir=narrow_dir,
        probe_depth=1.0,
        gate_width=gate_width,
        gate_spacing=gate_spacing,
    )
    # Right edge (narrow_max side): probe inward along -narrow direction
    right_gate_offsets = _gate_positions_on_material(
        shape,
        edge_start=narrow_dir * (narrow_min + narrow_size) + tall_dir * tall_min + thin_dir * thin_min,
        edge_dir=tall_dir,
        edge_length=tall_size,
        probe_dir=narrow_dir * (-1),
        probe_depth=1.0,
        gate_width=gate_width,
        gate_spacing=gate_spacing,
    )

    logger.info(f"Gate positions: bottom={len(bottom_gate_offsets)}, top={len(top_gate_offsets)}, "
                f"left={len(left_gate_offsets)}, right={len(right_gate_offsets)}")

    # --- Probe local thickness at each gate position on original shape ---
    # Each gate should be no thicker than the part at its attachment point.
    # Returns (thin_offset, local_thickness) for positioning and sizing.

    def _probe_edge_thicknesses(offsets, edge_start, edge_dir, probe_dir, probe_depth):
        """Measure part thickness at each gate position along an edge."""
        results = []
        for gpos in offsets:
            probe_pt = edge_start + edge_dir * gpos + probe_dir * (probe_depth / 2)
            t_off, t_size = _measure_local_thickness(
                shape, probe_pt, thin_dir, thickness)
            results.append((t_off, t_size))
        return results

    bottom_thicknesses = _probe_edge_thicknesses(
        bottom_gate_offsets,
        narrow_dir * narrow_min + tall_dir * tall_min + thin_dir * thin_min,
        narrow_dir, tall_dir, 1.0)
    top_thicknesses = _probe_edge_thicknesses(
        top_gate_offsets,
        narrow_dir * narrow_min + tall_dir * (tall_min + tall_size) + thin_dir * thin_min,
        narrow_dir, tall_dir * (-1), 1.0)
    left_thicknesses = _probe_edge_thicknesses(
        left_gate_offsets,
        narrow_dir * narrow_min + tall_dir * tall_min + thin_dir * thin_min,
        tall_dir, narrow_dir, 1.0)
    right_thicknesses = _probe_edge_thicknesses(
        right_gate_offsets,
        narrow_dir * (narrow_min + narrow_size) + tall_dir * tall_min + thin_dir * thin_min,
        tall_dir, narrow_dir * (-1), 1.0)

    # --- Gates ---
    gates = []

    for r, c, n_off, t_off in part_positions:
        part_narrow_min = narrow_min + n_off
        part_narrow_max = part_narrow_min + narrow_size
        part_tall_min = tall_min + t_off
        part_tall_max = part_tall_min + tall_size

        # -- Bottom gates (to horizontal runner below) --
        # Gates overlap into part by gate_overlap to ensure connection
        runner_below_top = h_runner_positions[r] + runner_width
        for gpos, (t_off_local, t_local) in zip(bottom_gate_offsets, bottom_thicknesses):
            gates.append(_make_box(
                narrow_pos=part_narrow_min + gpos - gate_width / 2,
                narrow_size=gate_width,
                tall_pos=runner_below_top,
                tall_size=(part_tall_min + gate_overlap) - runner_below_top,
                thin_pos=thin_min + t_off_local,
                thin_size=t_local,
                axes=axes,
            ))

        # -- Top gates (to horizontal runner above) --
        runner_above_bottom = h_runner_positions[r + 1]
        for gpos, (t_off_local, t_local) in zip(top_gate_offsets, top_thicknesses):
            gates.append(_make_box(
                narrow_pos=part_narrow_min + gpos - gate_width / 2,
                narrow_size=gate_width,
                tall_pos=part_tall_max - gate_overlap,
                tall_size=runner_above_bottom - (part_tall_max - gate_overlap),
                thin_pos=thin_min + t_off_local,
                thin_size=t_local,
                axes=axes,
            ))

        # -- Left gates (to vertical runner or left neighbor) --
        for gpos, (t_off_local, t_local) in zip(left_gate_offsets, left_thicknesses):
            gate_t = part_tall_min + gpos - gate_width / 2
            if c == 0:
                runner_right_edge = v_runner_positions[0] + runner_width
                gates.append(_make_box(
                    narrow_pos=runner_right_edge,
                    narrow_size=(part_narrow_min + gate_overlap) - runner_right_edge,
                    tall_pos=gate_t,
                    tall_size=gate_width,
                    thin_pos=thin_min + t_off_local,
                    thin_size=t_local,
                    axes=axes,
                ))
            elif (r, c - 1) in occupied:
                left_part_narrow_max = narrow_min + (c - 1) * col_pitch + narrow_size
                gates.append(_make_box(
                    narrow_pos=left_part_narrow_max - gate_overlap,
                    narrow_size=(part_narrow_min + gate_overlap) - (left_part_narrow_max - gate_overlap),
                    tall_pos=gate_t,
                    tall_size=gate_width,
                    thin_pos=thin_min + t_off_local,
                    thin_size=t_local,
                    axes=axes,
                ))

        # -- Right gates (to vertical runner or right neighbor) --
        for gpos, (t_off_local, t_local) in zip(right_gate_offsets, right_thicknesses):
            gate_t = part_tall_min + gpos - gate_width / 2
            if c == cols - 1 or (r, c + 1) not in occupied:
                runner_left_edge = v_runner_positions[1]
                gates.append(_make_box(
                    narrow_pos=part_narrow_max - gate_overlap,
                    narrow_size=runner_left_edge - (part_narrow_max - gate_overlap),
                    tall_pos=gate_t,
                    tall_size=gate_width,
                    thin_pos=thin_min + t_off_local,
                    thin_size=t_local,
                    axes=axes,
                ))
            # (right-to-neighbor handled by left gate of the right neighbor)

    # --- Fuse ---
    all_shapes = parts + runners + gates
    result = all_shapes[0].fuse(all_shapes[1:])
    result = result.removeSplitter()

    logger.info(f"Sprue: {len(parts)} parts, {len(runners)} runners, {len(gates)} gates")
    return result


def estimate_peel_force_profile(shape, layer_height=0.05, build_axis='z'):
    """Estimate relative peel force at each layer for MSLA printing.

    The peel force is roughly proportional to the cross-sectional area
    of each layer. This function slices the shape at each layer height
    and returns the area profile.

    Args:
        shape: FreeCAD Shape to analyze.
        layer_height: Printer layer height in mm.
        build_axis: Which axis is normal to the build plate ('x', 'y', or 'z').

    Returns:
        list of (height_mm, area_mm2) tuples.
    """
    axis_map = {'x': Vector(1, 0, 0), 'y': Vector(0, 1, 0), 'z': Vector(0, 0, 1)}
    normal = axis_map[build_axis]
    bb = shape.BoundBox

    attr = build_axis.upper()
    z_min = getattr(bb, f'{attr}Min')
    z_max = getattr(bb, f'{attr}Max')

    profile = []
    z = z_min + layer_height / 2
    while z < z_max:
        # Slice at this height
        point = normal * z
        try:
            cross = shape.slice(normal, z)
            if cross:
                # cross is a list of wires; compute enclosed area
                area = 0.0
                for wire in cross:
                    face = Part.Face(wire)
                    area += face.Area
                profile.append((z, area))
            else:
                profile.append((z, 0.0))
        except Exception:
            profile.append((z, 0.0))
        z += layer_height

    return profile
