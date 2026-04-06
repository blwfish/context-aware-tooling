"""Tests for split_utils — model splitting and registration.

Pure-math tests use mocks; geometry tests require FreeCAD and are
marked with @freecad.
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
    import split_utils


# ---------------------------------------------------------------------------
# Pure-math tests (no FreeCAD needed)
# ---------------------------------------------------------------------------

class TestPlaneBasePureMath:
    """Test _plane_basis with a minimal mock."""

    def test_plane_basis_z_normal(self):
        if not HAS_FREECAD:
            pytest.skip("needs FreeCAD Vector")
        u, v = split_utils._plane_basis(Vector(0, 0, 1))
        # u and v should be perpendicular to z and to each other
        assert abs(u.dot(Vector(0, 0, 1))) < 1e-10
        assert abs(v.dot(Vector(0, 0, 1))) < 1e-10
        assert abs(u.dot(v)) < 1e-10
        assert abs(u.Length - 1.0) < 1e-10
        assert abs(v.Length - 1.0) < 1e-10

    def test_plane_basis_x_normal(self):
        if not HAS_FREECAD:
            pytest.skip("needs FreeCAD Vector")
        u, v = split_utils._plane_basis(Vector(1, 0, 0))
        assert abs(u.dot(Vector(1, 0, 0))) < 1e-10
        assert abs(v.dot(Vector(1, 0, 0))) < 1e-10
        assert abs(u.dot(v)) < 1e-10


# ---------------------------------------------------------------------------
# Fixtures — reusable test shapes
# ---------------------------------------------------------------------------

@pytest.fixture
def solid_box():
    """A simple solid box 60x30x20mm centered at origin in Y/Z, from x=0..60."""
    box = Part.makeBox(60, 30, 20, Vector(0, -15, 0))
    return box


@pytest.fixture
def hollow_box():
    """A hollow box (1mm walls) — mimics the simple-pin-test model."""
    outer = Part.makeBox(60, 30, 20, Vector(0, -15, 0))
    inner = Part.makeBox(58, 28, 19, Vector(1, -14, 1))
    shell = outer.cut(inner)
    return shell


@pytest.fixture
def split_solid_box(solid_box):
    """Solid box split at x=30."""
    neg, pos = split_utils.split_model(solid_box, 'x', 30.0)
    return neg, pos


@pytest.fixture
def split_hollow_box(hollow_box):
    """Hollow box split at x=30."""
    neg, pos = split_utils.split_model(hollow_box, 'x', 30.0)
    return neg, pos


# ---------------------------------------------------------------------------
# Splitting tests
# ---------------------------------------------------------------------------

@freecad
class TestSplitModel:
    def test_axis_aligned_split_produces_two_solids(self, solid_box):
        neg, pos = split_utils.split_model(solid_box, 'x', 30.0)
        assert len(neg.Solids) >= 1
        assert len(pos.Solids) >= 1

    def test_split_preserves_volume(self, solid_box):
        neg, pos = split_utils.split_model(solid_box, 'x', 30.0)
        original_vol = solid_box.Volume
        split_vol = neg.Volume + pos.Volume
        assert abs(split_vol - original_vol) < 0.1

    def test_split_plane_arbitrary(self, solid_box):
        point = Vector(30, 0, 0)
        normal = Vector(1, 0, 0)
        neg, pos = split_utils.split_model_plane(solid_box, point, normal)
        assert abs(neg.Volume + pos.Volume - solid_box.Volume) < 0.1

    def test_split_hollow_preserves_volume(self, hollow_box):
        neg, pos = split_utils.split_model(hollow_box, 'x', 30.0)
        assert abs(neg.Volume + pos.Volume - hollow_box.Volume) < 0.1

    def test_invalid_axis_raises(self, solid_box):
        with pytest.raises(ValueError, match="axis must be"):
            split_utils.split_model(solid_box, 'w', 30.0)


# ---------------------------------------------------------------------------
# Split face detection
# ---------------------------------------------------------------------------

@freecad
class TestFindSplitFace:
    def test_finds_face_on_solid(self, split_solid_box):
        neg, _ = split_solid_box
        face = split_utils._find_split_face(neg, Vector(30, 0, 0), Vector(1, 0, 0))
        assert face is not None
        assert face.Surface.TypeId == 'Part::GeomPlane'

    def test_finds_face_on_hollow(self, split_hollow_box):
        neg, _ = split_hollow_box
        face = split_utils._find_split_face(neg, Vector(30, 0, 0), Vector(1, 0, 0))
        assert face is not None


# ---------------------------------------------------------------------------
# Interior/exterior edge classification
# ---------------------------------------------------------------------------

@freecad
class TestClassifyEdges:
    def test_hollow_box_has_interior_edges(self, split_hollow_box):
        neg, _ = split_hollow_box
        face = split_utils._find_split_face(neg, Vector(30, 0, 0), Vector(1, 0, 0))
        classes = split_utils._classify_split_face_edges(neg, face)
        interior = [e for e, c in classes if c == 'interior']
        exterior = [e for e, c in classes if c == 'exterior']
        assert len(interior) > 0
        assert len(exterior) > 0

    def test_solid_box_has_no_interior_edges(self, split_solid_box):
        neg, _ = split_solid_box
        face = split_utils._find_split_face(neg, Vector(30, 0, 0), Vector(1, 0, 0))
        classes = split_utils._classify_split_face_edges(neg, face)
        interior = [e for e, c in classes if c == 'interior']
        # Solid box: all edges are exterior
        assert len(interior) == 0


# ---------------------------------------------------------------------------
# Wall thickness measurement
# ---------------------------------------------------------------------------

@freecad
class TestMeasureWallThickness:
    def test_measures_1mm_wall(self, split_hollow_box):
        neg, _ = split_hollow_box
        # Interior edge at y=-14, probe into wall (toward y=-15)
        point = Vector(30, -14, 10)
        direction = Vector(0, -1, 0)
        thickness = split_utils._measure_wall_thickness(neg, point, direction)
        assert abs(thickness - 1.0) < 0.05

    def test_measures_bottom_wall(self, split_hollow_box):
        neg, _ = split_hollow_box
        point = Vector(30, 0, 1)
        direction = Vector(0, 0, -1)
        thickness = split_utils._measure_wall_thickness(neg, point, direction)
        assert abs(thickness - 1.0) < 0.05


# ---------------------------------------------------------------------------
# Pin geometry
# ---------------------------------------------------------------------------

@freecad
class TestPinGeometry:
    def test_make_pin_is_solid(self):
        pin = split_utils.make_pin(Vector(0, 0, 0), Vector(0, 0, 1))
        assert len(pin.Solids) == 1
        assert pin.Volume > 0

    def test_make_socket_larger_than_pin(self):
        pin = split_utils.make_pin(Vector(0, 0, 0), Vector(0, 0, 1))
        socket = split_utils.make_socket(Vector(0, 0, 0), Vector(0, 0, 1))
        assert socket.Volume > pin.Volume

    def test_pin_height(self):
        pin = split_utils.make_pin(Vector(0, 0, 0), Vector(0, 0, 1))
        assert abs(pin.BoundBox.ZMax - split_utils.PIN_HEIGHT) < 0.01


# ---------------------------------------------------------------------------
# Pin placement on face
# ---------------------------------------------------------------------------

@freecad
class TestPinPositionsOnFace:
    def test_solid_box_places_pins(self, split_solid_box):
        neg, _ = split_solid_box
        face = split_utils._find_split_face(neg, Vector(30, 0, 0), Vector(1, 0, 0))
        positions = split_utils._pin_positions_on_face(face, Vector(1, 0, 0))
        assert len(positions) >= 2

    def test_pin_count_honored(self, split_solid_box):
        neg, _ = split_solid_box
        face = split_utils._find_split_face(neg, Vector(30, 0, 0), Vector(1, 0, 0))
        positions = split_utils._pin_positions_on_face(face, Vector(1, 0, 0), count=5)
        assert len(positions) == 5

    def test_pins_on_face_material(self, split_hollow_box):
        """Pins must be on the actual face, not in the hollow."""
        neg, _ = split_hollow_box
        face = split_utils._find_split_face(neg, Vector(30, 0, 0), Vector(1, 0, 0))
        positions = split_utils._pin_positions_on_face(face, Vector(1, 0, 0), count=4)
        for pos in positions:
            dist = face.distToShape(Part.Vertex(pos))[0]
            assert dist < 0.1, f"Pin at {pos} is {dist:.2f}mm from face"

    def test_count_one_returns_single_pin(self, split_solid_box):
        neg, _ = split_solid_box
        face = split_utils._find_split_face(neg, Vector(30, 0, 0), Vector(1, 0, 0))
        positions = split_utils._pin_positions_on_face(face, Vector(1, 0, 0), count=1)
        assert len(positions) == 1


# ---------------------------------------------------------------------------
# Tab geometry
# ---------------------------------------------------------------------------

@freecad
class TestTabGeometry:
    def test_make_tab_is_solid(self):
        tab = split_utils.make_tab(
            Vector(10, 0, 5), Vector(1, 0, 0), Vector(0, -1, 0))
        assert len(tab.Solids) == 1
        assert tab.Volume > 0

    def test_tab_straddles_split_plane(self):
        tab = split_utils.make_tab(
            Vector(10, 0, 5), Vector(1, 0, 0), Vector(0, -1, 0))
        bb = tab.BoundBox
        assert bb.XMin < 10.0  # extends behind split
        assert bb.XMax > 10.0  # extends in front of split

    def test_slot_covers_forward_half(self):
        """Slot only covers the forward (tongue) side, with clearance."""
        args = (Vector(10, 0, 5), Vector(1, 0, 0), Vector(0, -1, 0))
        slot = split_utils.make_tab_slot(*args)
        bb = slot.BoundBox
        # Slot should extend past the split plane (x=10) forward
        assert bb.XMax > 10.0
        # Slot should be slightly past split plane on the back side (clearance)
        assert bb.XMin < 10.0
        assert bb.XMin > 9.5  # but not far back

    def test_tab_height_parameter(self):
        tab = split_utils.make_tab(
            Vector(10, 0, 5), Vector(1, 0, 0), Vector(0, -1, 0),
            height=0.5)
        bb = tab.BoundBox
        # wall_dir is (0,-1,0), so height is in -Y direction
        y_extent = bb.YMax - bb.YMin
        assert abs(y_extent - 0.5) < 0.01


# ---------------------------------------------------------------------------
# Full registration pipeline
# ---------------------------------------------------------------------------

@freecad
class TestPinRegistration:
    def test_pin_registration_adds_volume(self, split_solid_box):
        neg, pos = split_solid_box
        neg_r, pos_r = split_utils.add_registration_plane(
            neg, pos, Vector(30, 0, 0), Vector(1, 0, 0), pin_count=3)
        # neg gains pins
        assert neg_r.Volume > neg.Volume
        # pos loses sockets
        assert pos_r.Volume < pos.Volume

    def test_pin_registration_preserves_solid(self, split_solid_box):
        neg, pos = split_solid_box
        neg_r, pos_r = split_utils.add_registration_plane(
            neg, pos, Vector(30, 0, 0), Vector(1, 0, 0), pin_count=3)
        assert len(pos_r.Solids) >= 1


@freecad
class TestTabRegistration:
    def test_tab_registration_on_hollow_box(self, split_hollow_box):
        neg, pos = split_hollow_box
        neg_r, pos_r = split_utils.add_tab_registration_plane(
            neg, pos, Vector(30, 0, 0), Vector(1, 0, 0), tab_count=4)
        # neg gains tabs
        assert neg_r.Volume > neg.Volume
        # pos loses slots
        assert pos_r.Volume < pos.Volume

    def test_tab_height_respects_wall_thickness(self, split_hollow_box):
        """Tabs should not exceed half the wall thickness (0.5mm for 1mm walls)."""
        neg, pos = split_hollow_box
        neg_r, _ = split_utils.add_tab_registration_plane(
            neg, pos, Vector(30, 0, 0), Vector(1, 0, 0), tab_count=1)
        # One tab at clamped height (0.5mm): width(2) * depth(1.5)*2 * 0.5 = 3.0
        # Unclamped (1.0mm): 6.0 per tab.  tab_count=1 may yield >1 tab due
        # to proportional distribution across edges, so allow up to ~5.0
        # (still well below 6.0 per-tab unclamped).
        added = neg_r.Volume - neg.Volume
        assert added < 5.0, f"Tab volume {added:.1f} suggests height not clamped"

    def test_tab_falls_back_to_pins_on_solid(self, split_solid_box):
        """Solid cross-section has no interior edges — should fall back to pins."""
        neg, pos = split_solid_box
        neg_r, pos_r = split_utils.add_tab_registration_plane(
            neg, pos, Vector(30, 0, 0), Vector(1, 0, 0))
        # Should still produce registration (pin fallback)
        assert neg_r.Volume > neg.Volume or pos_r.Volume < pos.Volume


# ---------------------------------------------------------------------------
# Convenience wrappers
# ---------------------------------------------------------------------------

@freecad
class TestConvenienceWrappers:
    def test_split_and_register(self, solid_box):
        neg, pos = split_utils.split_and_register(
            solid_box, 'x', 30.0, pin_count=2)
        assert neg.Volume > 0
        assert pos.Volume > 0

    def test_split_and_register_plane(self, solid_box):
        neg, pos = split_utils.split_and_register_plane(
            solid_box, Vector(30, 0, 0), Vector(1, 0, 0), pin_count=2)
        assert neg.Volume > 0
        assert pos.Volume > 0
