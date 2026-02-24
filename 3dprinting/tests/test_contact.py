"""Tests for Contact dataclass — pure Python, no FreeCAD needed."""

import sys
import os
import pytest

# Add parent directory so we can import support_utils without FreeCAD.
# The Contact dataclass itself is pure Python, but support_utils.py imports
# FreeCAD at module level.  We mock FreeCAD for these tests.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

# Minimal FreeCAD mock so support_utils can be imported
import types
_fc_mock = types.ModuleType('FreeCAD')


class _MockVector:
    def __init__(self, x=0, y=0, z=0):
        self.x, self.y, self.z = x, y, z

    def normalize(self):
        import math
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
        import math
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

from support_utils import Contact


class TestContactDefaults:
    def test_default_normal_points_down(self):
        c = Contact(1.0, 2.0, 3.0)
        assert c.nx == 0.0
        assert c.ny == 0.0
        assert c.nz == -1.0

    def test_default_base_z_is_raft(self):
        c = Contact(1.0, 2.0, 3.0)
        assert c.base_z == 0.0

    def test_explicit_fields(self):
        c = Contact(1.0, 2.0, 3.0, nx=0.1, ny=-0.9, nz=-0.4, base_z=5.0)
        assert c.x == 1.0
        assert c.ny == -0.9
        assert c.base_z == 5.0


class TestContactProperties:
    def test_face_normal_tuple(self):
        c = Contact(0, 0, 0, nx=0.1, ny=-0.8, nz=-0.5)
        assert c.face_normal == (0.1, -0.8, -0.5)

    def test_position_tuple(self):
        c = Contact(10.0, 20.0, 30.0)
        assert c.position == (10.0, 20.0, 30.0)

    def test_is_model_resting_false_for_raft(self):
        c = Contact(0, 0, 5.0, base_z=0.0)
        assert not c.is_model_resting

    def test_is_model_resting_false_at_threshold(self):
        c = Contact(0, 0, 5.0, base_z=0.05)
        assert not c.is_model_resting

    def test_is_model_resting_true_above_threshold(self):
        c = Contact(0, 0, 5.0, base_z=3.0)
        assert c.is_model_resting


class TestContactEquality:
    def test_equal_contacts(self):
        a = Contact(1, 2, 3, nx=0.1, ny=0.2, nz=-0.9)
        b = Contact(1, 2, 3, nx=0.1, ny=0.2, nz=-0.9)
        assert a == b

    def test_unequal_contacts(self):
        a = Contact(1, 2, 3)
        b = Contact(1, 2, 4)
        assert a != b
