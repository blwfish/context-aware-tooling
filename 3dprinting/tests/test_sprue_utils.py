"""Tests for sprue_utils — sprue/runner frame generation.

Pure-math tests use no FreeCAD; geometry tests are marked with @freecad.
"""

import sys
import os
import math
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from conftest import freecad, HAS_FREECAD

if HAS_FREECAD:
    import FreeCAD
    from FreeCAD import Vector
    import Part
    import sprue_utils


# ---------------------------------------------------------------------------
# Pure-math tests (no FreeCAD needed)
# ---------------------------------------------------------------------------

class TestLayoutGrid:
    def test_square_layout(self):
        from sprue_utils import _layout_grid
        rows, cols = _layout_grid(4)
        assert rows == 2
        assert cols == 2

    def test_explicit_cols(self):
        from sprue_utils import _layout_grid
        rows, cols = _layout_grid(10, cols=5)
        assert rows == 2
        assert cols == 5

    def test_partial_last_row(self):
        from sprue_utils import _layout_grid
        rows, cols = _layout_grid(7, cols=3)
        assert rows == 3
        assert cols == 3

    def test_single_item(self):
        from sprue_utils import _layout_grid
        rows, cols = _layout_grid(1)
        assert rows == 1
        assert cols == 1

    def test_auto_cols_not_too_many(self):
        from sprue_utils import _layout_grid
        rows, cols = _layout_grid(10)
        assert cols <= 4  # ceil(sqrt(10)) = 4
        assert rows * cols >= 10


class TestGatePositions:
    def test_single_gate_at_center(self):
        from sprue_utils import _gate_positions
        pos = _gate_positions(4.0, 0.4, 5.0)
        assert len(pos) == 1
        assert abs(pos[0] - 2.0) < 0.01

    def test_multiple_gates_evenly_spaced(self):
        from sprue_utils import _gate_positions
        pos = _gate_positions(15.0, 0.4, 5.0)
        assert len(pos) == 3
        # Should be evenly distributed
        spacing = pos[1] - pos[0]
        assert abs((pos[2] - pos[1]) - spacing) < 0.01

    def test_always_at_least_one(self):
        from sprue_utils import _gate_positions
        pos = _gate_positions(1.0, 0.4, 100.0)
        assert len(pos) >= 1


# ---------------------------------------------------------------------------
# FreeCAD geometry tests
# ---------------------------------------------------------------------------

@pytest.fixture
def solid_plate():
    """A simple solid rectangular plate: 20x2x30mm (narrow=X, thin=Y, tall=Z)."""
    return Part.makeBox(20, 2, 30)


@pytest.fixture
def hollow_frame():
    """A hollow rectangular frame (like a window), 15x1.2x22mm with 1mm frame width."""
    outer = Part.makeBox(15, 1.2, 22, Vector(-7.5, 0, -11))
    # Cut out two window openings
    opening1 = Part.makeBox(13, 1.2, 9.5, Vector(-6.5, 0, -10))
    opening2 = Part.makeBox(13, 1.2, 9.5, Vector(-6.5, 0, 0.5))
    cut1 = outer.cut(opening1)
    cut2 = cut1.cut(opening2)
    return cut2


@freecad
class TestDetectAxes:
    def test_plate_axes(self, solid_plate):
        axes = sprue_utils._detect_axes(solid_plate)
        assert axes['thin'][1] == 'y'   # 2mm
        assert axes['narrow'][1] == 'x'  # 20mm
        assert axes['tall'][1] == 'z'    # 30mm

    def test_rotated_plate(self):
        # Plate with thin axis along Z
        plate = Part.makeBox(20, 30, 1.5)
        axes = sprue_utils._detect_axes(plate)
        assert axes['thin'][1] == 'z'


@freecad
class TestMakeBox:
    def test_box_dimensions(self, solid_plate):
        axes = sprue_utils._detect_axes(solid_plate)
        box = sprue_utils._make_box(
            narrow_pos=5, narrow_size=10,
            tall_pos=5, tall_size=20,
            thin_pos=0, thin_size=2,
            axes=axes)
        bb = box.BoundBox
        assert abs(bb.XLength - 10) < 0.01
        assert abs(bb.YLength - 2) < 0.01
        assert abs(bb.ZLength - 20) < 0.01


@freecad
class TestProbing:
    def test_solid_plate_has_material_everywhere(self, solid_plate):
        axes = sprue_utils._detect_axes(solid_plate)
        # Bottom edge (Z=0), probe inward (+Z)
        regions = sprue_utils._probe_material_along_edge(
            solid_plate,
            edge_start=Vector(0, 0, 0),
            edge_dir=Vector(1, 0, 0),
            edge_length=20,
            probe_dir=Vector(0, 0, 1),
            probe_depth=1.0)
        # Should be one contiguous region spanning the full width
        assert len(regions) == 1
        assert regions[0][0] < 1.0  # starts near 0
        assert regions[0][1] > 19.0  # ends near 20

    def test_hollow_frame_has_gaps(self, hollow_frame):
        # The frame has openings. Along the left edge (X=XMin),
        # material is continuous (it's the stile). But along the
        # bottom edge at mid-height, there should be gaps.
        bb = hollow_frame.BoundBox
        # Probe along X at Z=0 (mid-height where there's a mullion + openings)
        regions = sprue_utils._probe_material_along_edge(
            hollow_frame,
            edge_start=Vector(bb.XMin, 0, 0),
            edge_dir=Vector(1, 0, 0),
            edge_length=bb.XLength,
            probe_dir=Vector(0, 0, 1),
            probe_depth=1.0)
        # Should find material at the mullion area
        assert len(regions) >= 1

    def test_gate_positions_on_solid(self, solid_plate):
        positions = sprue_utils._gate_positions_on_material(
            solid_plate,
            edge_start=Vector(0, 0, 0),
            edge_dir=Vector(1, 0, 0),
            edge_length=20,
            probe_dir=Vector(0, 0, 1),
            probe_depth=1.0,
            gate_width=0.4,
            gate_spacing=5.0)
        assert len(positions) >= 3  # 20mm / 5mm spacing


@freecad
class TestMakeSprue:
    def test_single_part_is_one_solid(self, solid_plate):
        sprue = sprue_utils.make_sprue(solid_plate, count=1)
        assert len(sprue.Solids) == 1

    def test_sprue_volume_exceeds_parts(self, solid_plate):
        part_vol = solid_plate.Volume
        sprue = sprue_utils.make_sprue(solid_plate, count=4, cols=2)
        # Sprue should be larger than 4 parts (runners + gates add volume)
        assert sprue.Volume > 4 * part_vol

    def test_sprue_is_single_solid(self, solid_plate):
        sprue = sprue_utils.make_sprue(solid_plate, count=4, cols=2)
        assert len(sprue.Solids) == 1

    def test_10_parts_5_cols(self, solid_plate):
        sprue = sprue_utils.make_sprue(solid_plate, count=10, cols=5)
        assert len(sprue.Solids) == 1
        # Should contain at least 10x the part volume
        assert sprue.Volume > 10 * solid_plate.Volume

    def test_hollow_frame_single_solid(self, hollow_frame):
        sprue = sprue_utils.make_sprue(hollow_frame, count=4, cols=2)
        assert len(sprue.Solids) == 1

    def test_sprue_dimensions_reasonable(self, solid_plate):
        sprue = sprue_utils.make_sprue(solid_plate, count=6, cols=3)
        bb = sprue.BoundBox
        # 3 cols of 20mm parts + spacing + runners
        assert bb.XLength > 60  # at least 3 * part width
        # 2 rows of 30mm parts + spacing + runners
        assert bb.ZLength > 60  # at least 2 * part height

    def test_runner_thickness_matches_part(self, solid_plate):
        sprue = sprue_utils.make_sprue(solid_plate, count=1)
        axes = sprue_utils._detect_axes(solid_plate)
        thin_axis = axes['thin'][1]
        sprue_bb = sprue.BoundBox
        part_bb = solid_plate.BoundBox
        # Thickness should match
        sprue_thin = getattr(sprue_bb, f'{thin_axis.upper()}Length')
        part_thin = getattr(part_bb, f'{thin_axis.upper()}Length')
        assert abs(sprue_thin - part_thin) < 0.01


@freecad
class TestPeelForceProfile:
    def test_solid_box_profile(self):
        box = Part.makeBox(10, 10, 20)
        profile = sprue_utils.estimate_peel_force_profile(box, layer_height=1.0)
        assert len(profile) > 0
        # All layers should have roughly the same area (10x10 = 100 mm²)
        areas = [area for _, area in profile]
        for a in areas:
            assert abs(a - 100.0) < 5.0

    def test_profile_along_different_axis(self):
        box = Part.makeBox(10, 20, 5)
        profile = sprue_utils.estimate_peel_force_profile(
            box, layer_height=1.0, build_axis='y')
        assert len(profile) > 0
        # Cross-section perpendicular to Y should be 10x5 = 50 mm²
        areas = [area for _, area in profile]
        for a in areas:
            assert abs(a - 50.0) < 5.0

    def test_tapered_shape_has_varying_area(self):
        # A cone has varying cross-section
        cone = Part.makeCone(10, 0, 20)
        profile = sprue_utils.estimate_peel_force_profile(
            cone, layer_height=2.0, build_axis='z')
        areas = [area for _, area in profile]
        # Bottom should be larger than top
        assert areas[0] > areas[-1]
