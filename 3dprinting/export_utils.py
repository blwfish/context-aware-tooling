"""
STL export utilities for resin MSLA printing.

Handles the final pipeline step: shifting geometry to all-positive
coordinates (required by most slicers), mesh tessellation, validation,
and file export.

Usage (from FreeCAD MCP execute_python):
    from export_utils import export_stl, shift_to_positive

    # Export a single shape:
    export_stl(shape, '/path/to/output.stl')

    # Export with build volume check:
    export_stl(shape, '/path/to/output.stl', printer='m7_pro')

    # Export multiple pieces (model + supports + raft):
    export_stl([model, supports, raft], '/path/to/output.stl')

    # Just shift coordinates (for inspection before export):
    shifted = shift_to_positive(shape, margin=0.5)

All dimensions are in print-scale mm (not prototype scale).
"""

import os
import logging

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Tessellation quality — controls mesh density.
# LinearDeflection: max distance (mm) from mesh facet to actual surface.
# AngularDeflection: max angle (radians) between adjacent facet normals.
# These defaults produce good results for HO-scale architectural models
# on MSLA printers with ~50um XY resolution.
DEFAULT_LINEAR_DEFLECTION = 0.05    # mm — fine enough for 50um pixels
DEFAULT_ANGULAR_DEFLECTION = 0.3    # radians (~17°)

# Minimum gap from the build plate origin.  Slicers place the model
# relative to (0,0,0); a small margin avoids edge-clipping artifacts.
DEFAULT_ORIGIN_MARGIN = 0.5         # mm

from constants import PRINTER_VOLUMES


# ---------------------------------------------------------------------------
# Coordinate shifting
# ---------------------------------------------------------------------------

def shift_to_positive(shape, margin=DEFAULT_ORIGIN_MARGIN):
    """
    Translate a shape so all coordinates are positive.

    Slicers (Chitubox, Lychee, etc.) expect geometry in the positive
    octant starting near the origin.  This function shifts the shape's
    bounding box minimum to (margin, margin, margin).

    Parameters
    ----------
    shape : Part.Shape
        The shape to shift.  Can be a Solid, Compound, or any Shape subtype.
    margin : float
        Gap between the shifted shape and the coordinate axes.

    Returns
    -------
    Part.Shape
        A copy of the shape translated to all-positive coordinates.
    """
    from FreeCAD import Vector

    bb = shape.BoundBox
    offset = Vector(
        margin - bb.XMin,
        margin - bb.YMin,
        margin - bb.ZMin,
    )

    # Skip if already positive (within tolerance)
    if bb.XMin >= 0 and bb.YMin >= 0 and bb.ZMin >= 0:
        logger.info("Shape already in positive coordinates, no shift needed")
        return shape.copy()

    shifted = shape.copy()
    shifted.translate(offset)
    logger.info("Shifted by (%.2f, %.2f, %.2f) to all-positive coords",
                offset.x, offset.y, offset.z)
    return shifted


# ---------------------------------------------------------------------------
# Mesh validation
# ---------------------------------------------------------------------------

def validate_mesh(mesh):
    """
    Run basic sanity checks on a tessellated mesh.

    Parameters
    ----------
    mesh : Mesh.Mesh
        The mesh to validate.

    Returns
    -------
    dict with keys:
        'valid': bool — True if mesh passes all checks
        'facets': int — facet count
        'points': int — point count
        'non_manifold_edges': int — edges shared by != 2 facets
        'degenerate_facets': int — zero-area triangles
        'warnings': list of str — human-readable warnings
    """
    warnings = []
    facets = mesh.CountFacets
    points = mesh.CountPoints

    if facets == 0:
        warnings.append("Mesh has zero facets — empty geometry")
        return {'valid': False, 'facets': 0, 'points': 0,
                'non_manifold_edges': 0, 'degenerate_facets': 0,
                'warnings': warnings}

    # Check for non-manifold edges
    non_manifold = mesh.countNonUniformOrientedFacets()

    # Check for degenerate (zero-area) facets
    degenerate = 0
    for facet in mesh.Facets:
        if facet.Area < 1e-10:
            degenerate += 1

    if non_manifold > 0:
        warnings.append(f"{non_manifold} non-manifold oriented facets "
                        "(may cause slicer issues)")
    if degenerate > 0:
        warnings.append(f"{degenerate} degenerate (zero-area) facets")

    # Volume check — negative volume means inverted normals
    try:
        vol = mesh.Volume
        if vol < 0:
            warnings.append(f"Negative mesh volume ({vol:.1f}) — "
                            "normals may be inverted")
        elif vol == 0:
            warnings.append("Zero mesh volume — may be a surface, not a solid")
    except Exception:
        warnings.append("Could not compute mesh volume")

    valid = len(warnings) == 0

    return {
        'valid': valid,
        'facets': facets,
        'points': points,
        'non_manifold_edges': non_manifold,
        'degenerate_facets': degenerate,
        'warnings': warnings,
    }


# ---------------------------------------------------------------------------
# Build volume check (lightweight, no support_utils dependency)
# ---------------------------------------------------------------------------

def check_fits_printer(shape, printer='m7_pro', margin=2.0):
    """
    Quick check if a shape fits a printer's build volume.

    Parameters
    ----------
    shape : Part.Shape
        The shape to check (should already be in final print orientation).
    printer : str
        Printer key.
    margin : float
        Safety margin from build volume edges.

    Returns
    -------
    bool
        True if the shape fits.

    Raises
    ------
    ValueError
        If printer key is unknown.
    """
    vol = PRINTER_VOLUMES.get(printer)
    if vol is None:
        raise ValueError(f"Unknown printer '{printer}'. "
                         f"Known: {list(PRINTER_VOLUMES.keys())}")

    bb = shape.BoundBox
    fits = (bb.XLength <= vol[0] - 2 * margin and
            bb.YLength <= vol[1] - 2 * margin and
            bb.ZLength <= vol[2] - 2 * margin)
    return fits


# ---------------------------------------------------------------------------
# STL export
# ---------------------------------------------------------------------------

def export_stl(shapes, filepath, printer=None, margin=DEFAULT_ORIGIN_MARGIN,
               linear_deflection=DEFAULT_LINEAR_DEFLECTION,
               angular_deflection=DEFAULT_ANGULAR_DEFLECTION,
               validate=True, shift=True):
    """
    Export one or more shapes to STL for slicer import.

    Handles the full export pipeline:
    1. Combine multiple shapes into a compound (if needed)
    2. Shift to all-positive coordinates (slicer requirement)
    3. Optionally check build volume fit
    4. Tessellate to mesh
    5. Optionally validate mesh quality
    6. Write STL file

    Parameters
    ----------
    shapes : Part.Shape or list of Part.Shape
        Shape(s) to export.  If a list, they are combined into a compound.
    filepath : str
        Output STL file path.
    printer : str or None
        If provided, check that the shape fits this printer's build volume.
        Use 'm7_pro' or 'm7_max'.
    margin : float
        Origin margin for coordinate shifting.
    linear_deflection : float
        Mesh quality — max distance from facet to surface (mm).
        Smaller = finer mesh, larger file.
    angular_deflection : float
        Mesh quality — max angle between adjacent facet normals (radians).
        Smaller = smoother curves, more facets.
    validate : bool
        If True, run mesh validation and warn on issues.
    shift : bool
        If True, shift to all-positive coordinates before export.

    Returns
    -------
    dict with keys:
        'filepath': str — absolute path to the written file
        'facets': int — mesh facet count
        'file_size_kb': int — file size in KB
        'shifted_by': tuple or None — (dx, dy, dz) translation applied
        'validation': dict or None — mesh validation results
        'fits_printer': bool or None — build volume check result
    """
    import Part
    import MeshPart

    # 1. Combine shapes
    if isinstance(shapes, (list, tuple)):
        if len(shapes) == 0:
            raise ValueError("No shapes to export")
        elif len(shapes) == 1:
            combined = shapes[0]
        else:
            combined = Part.makeCompound(list(shapes))
    else:
        combined = shapes

    # 2. Shift to positive coordinates
    shifted_by = None
    if shift:
        bb_before = combined.BoundBox
        combined = shift_to_positive(combined, margin)
        bb_after = combined.BoundBox
        shifted_by = (
            bb_after.XMin - bb_before.XMin,
            bb_after.YMin - bb_before.YMin,
            bb_after.ZMin - bb_before.ZMin,
        )
        # Zero out negligible shifts
        if all(abs(s) < 0.001 for s in shifted_by):
            shifted_by = None

    # 3. Build volume check
    fits = None
    if printer is not None:
        fits = check_fits_printer(combined, printer)
        if not fits:
            bb = combined.BoundBox
            vol = PRINTER_VOLUMES[printer]
            logger.warning(
                "Shape does NOT fit %s: %.1f x %.1f x %.1f mm "
                "(build volume: %.0f x %.0f x %.0f mm)",
                printer, bb.XLength, bb.YLength, bb.ZLength,
                vol[0], vol[1], vol[2])
            print(f"WARNING: Shape exceeds {printer} build volume!")
            print(f"  Shape:  {bb.XLength:.1f} x {bb.YLength:.1f} x {bb.ZLength:.1f} mm")
            print(f"  Printer: {vol[0]:.0f} x {vol[1]:.0f} x {vol[2]:.0f} mm")
        else:
            logger.info("Build volume check passed (%s)", printer)

    # 4. Tessellate
    mesh = MeshPart.meshFromShape(
        Shape=combined,
        LinearDeflection=linear_deflection,
        AngularDeflection=angular_deflection,
    )

    # 5. Validate
    validation = None
    if validate:
        import Mesh
        # MeshPart returns a Mesh.Mesh object
        validation = validate_mesh(mesh)
        if validation['warnings']:
            for w in validation['warnings']:
                logger.warning("Mesh: %s", w)
                print(f"  Mesh warning: {w}")
        else:
            logger.info("Mesh validation passed: %d facets", validation['facets'])

    # 6. Ensure output directory exists
    out_dir = os.path.dirname(os.path.abspath(filepath))
    os.makedirs(out_dir, exist_ok=True)

    # 7. Write
    mesh.write(filepath)
    file_size = os.path.getsize(filepath) // 1024

    print(f"Exported STL: {mesh.CountFacets:,} facets, {file_size:,} KB")
    print(f"  -> {os.path.abspath(filepath)}")
    if shifted_by:
        print(f"  Shifted by ({shifted_by[0]:.2f}, {shifted_by[1]:.2f}, {shifted_by[2]:.2f})")

    return {
        'filepath': os.path.abspath(filepath),
        'facets': mesh.CountFacets,
        'file_size_kb': file_size,
        'shifted_by': shifted_by,
        'validation': validation,
        'fits_printer': fits,
    }


def export_pieces(pieces, output_dir, base_name='piece',
                  printer=None, **kwargs):
    """
    Export multiple split pieces as individual STL files.

    Convenience function for post-split export.  Each piece gets its own
    file: {base_name}_1.stl, {base_name}_2.stl, etc.

    Parameters
    ----------
    pieces : list of Part.Shape
        The split pieces to export.
    output_dir : str
        Directory for output files.
    base_name : str
        Filename prefix.
    printer : str or None
        Printer key for build volume checking.
    **kwargs
        Additional arguments passed to export_stl.

    Returns
    -------
    list of dict
        Export results for each piece (from export_stl).
    """
    results = []
    for i, piece in enumerate(pieces):
        filename = f"{base_name}_{i+1}_of_{len(pieces)}.stl"
        filepath = os.path.join(output_dir, filename)
        print(f"\n--- Piece {i+1}/{len(pieces)} ---")
        result = export_stl(piece, filepath, printer=printer, **kwargs)
        results.append(result)

    print(f"\nExported {len(pieces)} pieces to {output_dir}")
    total_facets = sum(r['facets'] for r in results)
    total_kb = sum(r['file_size_kb'] for r in results)
    print(f"  Total: {total_facets:,} facets, {total_kb:,} KB")
    return results
