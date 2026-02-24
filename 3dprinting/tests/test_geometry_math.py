"""Pure-math tests for geometry helpers — no FreeCAD needed.

Tests _face_z_at_xy, tilted_wall_outward_normal, and pipeline math
(clustering, inward nudge) using the same FreeCAD mock as test_contact.py.
"""

import sys
import os
import math
import pytest

# Reuse FreeCAD mock from test_contact (conftest handles import order)
# If test_contact hasn't been imported yet, set up the mock here too.
if 'FreeCAD' not in sys.modules:
    import types
    _fc_mock = types.ModuleType('FreeCAD')

    class _MockVector:
        def __init__(self, x=0, y=0, z=0):
            self.x, self.y, self.z = x, y, z
        def normalize(self):
            ln = math.sqrt(self.x**2 + self.y**2 + self.z**2)
            if ln > 1e-10:
                self.x /= ln; self.y /= ln; self.z /= ln
        def cross(self, other):
            return _MockVector(
                self.y * other.z - self.z * other.y,
                self.z * other.x - self.x * other.z,
                self.x * other.y - self.y * other.x)
        def dot(self, other):
            return self.x * other.x + self.y * other.y + self.z * other.z
        def __sub__(self, other):
            return _MockVector(self.x - other.x, self.y - other.y, self.z - other.z)
        def __add__(self, other):
            return _MockVector(self.x + other.x, self.y + other.y, self.z + other.z)
        def __mul__(self, scalar):
            return _MockVector(self.x * scalar, self.y * scalar, self.z * scalar)
        def __rmul__(self, scalar):
            return self.__mul__(scalar)
        @property
        def Length(self):
            return math.sqrt(self.x**2 + self.y**2 + self.z**2)

    _fc_mock.Vector = _MockVector

    class _MockRotation:
        def __init__(self, axis=None, angle=0):
            pass
        def multiply(self, other):
            return self

    _fc_mock.Rotation = _MockRotation
    _fc_mock.Matrix = type('Matrix', (), {
        '__init__': lambda self: None,
        '__setattr__': lambda self, k, v: object.__setattr__(self, k, v),
    })
    _fc_mock.Placement = lambda *a: None
    sys.modules['FreeCAD'] = _fc_mock
    sys.modules['Part'] = types.ModuleType('Part')

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from support_utils import _face_z_at_xy, tilted_wall_outward_normal, Contact


# ---------------------------------------------------------------------------
# _face_z_at_xy tests
# ---------------------------------------------------------------------------

class TestFaceZAtXY:
    """Test Z interpolation on a planar face."""

    def test_horizontal_face(self):
        """Horizontal face (nz=-1): Z is constant regardless of XY."""
        # Face normal pointing straight down, CoG at (5, 5, 10)
        z = _face_z_at_xy((0, 0, -1), (5, 5, 10), x=0, y=0)
        assert z == pytest.approx(10.0)
        z2 = _face_z_at_xy((0, 0, -1), (5, 5, 10), x=100, y=200)
        assert z2 == pytest.approx(10.0)

    def test_tilted_18deg_x(self):
        """Face tilted 18 deg around X: Z varies with Y, constant in X."""
        # After 18deg X-tilt, a originally-horizontal face has:
        # normal ≈ (0, sin18, -cos18) = (0, 0.309, -0.951)
        tilt = math.radians(18)
        nx, ny, nz = 0, math.sin(tilt), -math.cos(tilt)
        cog = (50, 30, 20)

        # At CoG, Z should equal cog.z
        z_at_cog = _face_z_at_xy((nx, ny, nz), cog, 50, 30)
        assert z_at_cog == pytest.approx(20.0, abs=1e-10)

        # Moving +10 in Y should change Z by -(ny/nz)*10
        z_offset = _face_z_at_xy((nx, ny, nz), cog, 50, 40)
        expected_dz = -(ny / nz) * 10  # = -(0.309/-0.951)*10 = +3.249
        assert z_offset == pytest.approx(20.0 + expected_dz, abs=1e-6)

        # Moving in X only: Z unchanged (nx=0)
        z_x_only = _face_z_at_xy((nx, ny, nz), cog, 100, 30)
        assert z_x_only == pytest.approx(20.0, abs=1e-10)

    def test_vertical_face_returns_cog_z(self):
        """Near-vertical face (nz~0): Z is undefined, returns CoG Z."""
        z = _face_z_at_xy((1, 0, 0), (10, 20, 30), 0, 0)
        assert z == pytest.approx(30.0)

    def test_dual_axis_tilt(self):
        """Face tilted in both X and Z: Z varies with both X and Y."""
        # Arbitrary tilted normal
        nx, ny, nz = 0.15, 0.30, -0.94
        cog = (40, 25, 15)

        z_at_cog = _face_z_at_xy((nx, ny, nz), cog, 40, 25)
        assert z_at_cog == pytest.approx(15.0, abs=1e-10)

        # Moving +5 in X and +3 in Y
        dx, dy = 5, 3
        expected_z = 15.0 - (nx * dx + ny * dy) / nz
        z_moved = _face_z_at_xy((nx, ny, nz), cog, 45, 28)
        assert z_moved == pytest.approx(expected_z, abs=1e-6)

    def test_accepts_objects_with_xyz_attrs(self):
        """Function should work with objects that have .x .y .z attributes."""
        FreeCAD = sys.modules['FreeCAD']
        normal = FreeCAD.Vector(0, 0, -1)
        cog = FreeCAD.Vector(5, 5, 10)
        z = _face_z_at_xy(normal, cog, 0, 0)
        assert z == pytest.approx(10.0)


# ---------------------------------------------------------------------------
# tilted_wall_outward_normal tests
# ---------------------------------------------------------------------------

class TestTiltedWallNormal:
    """Test wall normal computation after tilt rotations."""

    def test_single_axis_18deg_display_neg_y(self):
        """Front wall (display at -Y), 18deg X-tilt."""
        n = tilted_wall_outward_normal(18.0, display_faces_negative_y=True)
        tilt = math.radians(18)
        assert n.x == pytest.approx(0.0, abs=1e-10)
        assert n.y == pytest.approx(-math.cos(tilt), abs=1e-10)
        assert n.z == pytest.approx(math.sin(tilt), abs=1e-10)

    def test_single_axis_18deg_display_pos_y(self):
        """Back wall (display at +Y), 18deg X-tilt."""
        n = tilted_wall_outward_normal(18.0, display_faces_negative_y=False)
        tilt = math.radians(18)
        assert n.x == pytest.approx(0.0, abs=1e-10)
        assert n.y == pytest.approx(math.cos(tilt), abs=1e-10)
        assert n.z == pytest.approx(-math.sin(tilt), abs=1e-10)

    def test_dual_axis_18_8(self):
        """Front wall, 18deg X + 8deg Z tilt. Normal gains X component."""
        n = tilted_wall_outward_normal(18.0, display_faces_negative_y=True,
                                       z_tilt_deg=8.0)
        # After X-tilt: (0, -cos18, sin18)
        # After Z-rotation by 8deg: x' = 0*cos8 - (-cos18)*sin8
        tilt = math.radians(18)
        zr = math.radians(8)
        ny_after_x = -math.cos(tilt)
        expected_nx = -ny_after_x * math.sin(zr)  # = cos18 * sin8
        expected_ny = ny_after_x * math.cos(zr)   # = -cos18 * cos8

        assert n.x == pytest.approx(expected_nx, abs=1e-10)
        assert n.y == pytest.approx(expected_ny, abs=1e-10)
        assert n.z == pytest.approx(math.sin(tilt), abs=1e-10)

    def test_normal_is_unit_length(self):
        """Result should be approximately unit length."""
        n = tilted_wall_outward_normal(18.0, True, 8.0)
        length = math.sqrt(n.x**2 + n.y**2 + n.z**2)
        assert length == pytest.approx(1.0, abs=1e-10)

    def test_zero_tilt(self):
        """No tilt: normal should be (0, -1, 0) for display-neg-y."""
        n = tilted_wall_outward_normal(0.0, display_faces_negative_y=True)
        assert n.x == pytest.approx(0.0)
        assert n.y == pytest.approx(-1.0)
        assert n.z == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# Clustering logic tests (from generate_building_print.py pipeline)
# ---------------------------------------------------------------------------

class TestClustering:
    """Test grid-based contact clustering logic."""

    def test_cluster_keeps_minimum_z(self):
        """Within one grid cell, the contact with lowest Z should win."""
        GRID = 8.0
        contacts = [
            Contact(1.0, 1.0, 10.0),
            Contact(1.5, 1.5, 8.0),   # same cell, lower Z
            Contact(1.2, 1.2, 12.0),
        ]
        cells = {}
        for c in contacts:
            key = (round(c.x / GRID) * GRID, round(c.y / GRID) * GRID)
            if key not in cells or c.z < cells[key].z:
                cells[key] = c
        assert len(cells) == 1
        assert cells[list(cells.keys())[0]].z == 8.0

    def test_cluster_separates_distant_contacts(self):
        """Contacts in different grid cells should remain separate."""
        GRID = 8.0
        contacts = [
            Contact(1.0, 1.0, 10.0),
            Contact(20.0, 20.0, 10.0),
        ]
        cells = {}
        for c in contacts:
            key = (round(c.x / GRID) * GRID, round(c.y / GRID) * GRID)
            if key not in cells or c.z < cells[key].z:
                cells[key] = c
        assert len(cells) == 2


# ---------------------------------------------------------------------------
# Inward nudge tests (from generate_building_print.py pipeline)
# ---------------------------------------------------------------------------

class TestInwardNudge:
    """Test contact nudge toward building center."""

    def test_nudge_moves_toward_center(self):
        """Contact should move closer to center after nudge."""
        center_x, center_y = 45.0, 75.0
        INWARD_NUDGE = 0.3
        c = Contact(90.0, 75.0, 5.0)  # right edge, centered Y

        dx = c.x - center_x
        dy = c.y - center_y
        d = math.sqrt(dx*dx + dy*dy)
        new_x = c.x - INWARD_NUDGE * dx / d
        new_y = c.y - INWARD_NUDGE * dy / d

        # Should have moved 0.3mm toward center (in X only, since dy=0)
        assert new_x == pytest.approx(89.7, abs=1e-6)
        assert new_y == pytest.approx(75.0, abs=1e-6)

    def test_nudge_diagonal(self):
        """Diagonal nudge: moves in both X and Y toward center."""
        center_x, center_y = 0, 0
        INWARD_NUDGE = 1.0
        c = Contact(3.0, 4.0, 5.0)

        dx = c.x - center_x
        dy = c.y - center_y
        d = math.sqrt(dx*dx + dy*dy)  # = 5.0
        new_x = c.x - INWARD_NUDGE * dx / d
        new_y = c.y - INWARD_NUDGE * dy / d

        assert new_x == pytest.approx(3.0 - 0.6, abs=1e-6)  # 3/5 = 0.6
        assert new_y == pytest.approx(4.0 - 0.8, abs=1e-6)  # 4/5 = 0.8

    def test_nudge_at_center_no_change(self):
        """Contact at center should not move (d ≈ 0 guard)."""
        center_x, center_y = 45.0, 75.0
        INWARD_NUDGE = 0.3
        c = Contact(45.0, 75.0, 5.0)

        dx = c.x - center_x
        dy = c.y - center_y
        d = math.sqrt(dx*dx + dy*dy)
        if d > 0.01:
            new_x = c.x - INWARD_NUDGE * dx / d
            new_y = c.y - INWARD_NUDGE * dy / d
        else:
            new_x, new_y = c.x, c.y

        assert new_x == pytest.approx(45.0)
        assert new_y == pytest.approx(75.0)
