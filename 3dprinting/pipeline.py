"""
Pipeline infrastructure for the 3D print preparation pipeline.

Working-copy creation, provenance tracking, and build-volume checking.
These are general-purpose pipeline utilities that don't depend on any
specific domain (supports, splitting, etc.).

Requires FreeCAD for create_working_copy() and check_build_fit();
record_pipeline_step() works with any FreeCAD document object.
"""

import logging

from constants import (
    PRINTER_VOLUMES,
    PIPELINE_NAME,
    PIPELINE_VERSION,
    _METADATA_GROUP,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Working-copy creation (pipeline entry point)
# ---------------------------------------------------------------------------

def create_working_copy(source, name=None, doc=None):
    """Deep-copy a FreeCAD object into a pipeline working copy.

    Creates a new Part::Feature with an independent shape copy and adds
    provenance properties following the same Metadata convention as the
    brick generator (GeneratorName, GeneratorVersion, SourceObject, plus
    PipelineSteps for tracking downstream operations).

    The entire operation is wrapped in a single FreeCAD transaction, so
    Ctrl+Z undoes it in one step.

    Metadata properties added to the new object:

    - **GeneratorName** (str): ``"print_pipeline"``
    - **GeneratorVersion** (str): ``"1.0.0"``
    - **SourceObject** (str): Label of the original object.
    - **PipelineSteps** (str): Semicolon-separated log of steps applied,
      e.g. ``"copy;orient;supports;raft"``.  Append via
      `record_pipeline_step()`.

    Parameters
    ----------
    source : str or App::DocumentObject
        Object name (looked up in *doc*) or an existing document object.
    name : str, optional
        Label for the working copy.  Defaults to ``<source_label>_print``.
    doc : App.Document, optional
        Document to operate in.  Defaults to ``FreeCAD.ActiveDocument``.

    Returns
    -------
    App::DocumentObject
        The newly created Part::Feature with Metadata properties.
    """
    import FreeCAD

    if doc is None:
        doc = FreeCAD.ActiveDocument
    if isinstance(source, str):
        obj = doc.getObject(source)
        if obj is None:
            raise ValueError(f"Object '{source}' not found in document")
    else:
        obj = source

    copy_label = name or f"{obj.Label}_print"

    doc.openTransaction(f"Create working copy '{copy_label}'")
    try:
        copy_obj = doc.addObject("Part::Feature", copy_label)
        copy_obj.Shape = obj.Shape.copy()

        # Standard generator metadata (matches brick_generator convention)
        copy_obj.addProperty(
            "App::PropertyString", "GeneratorName", _METADATA_GROUP,
            "Generator name")
        copy_obj.GeneratorName = PIPELINE_NAME

        copy_obj.addProperty(
            "App::PropertyString", "GeneratorVersion", _METADATA_GROUP,
            "Generator version")
        copy_obj.GeneratorVersion = PIPELINE_VERSION

        copy_obj.addProperty(
            "App::PropertyString", "SourceObject", _METADATA_GROUP,
            "Original object this copy was made from")
        copy_obj.SourceObject = obj.Label

        copy_obj.addProperty(
            "App::PropertyString", "PipelineSteps", _METADATA_GROUP,
            "Semicolon-separated log of pipeline steps applied")
        copy_obj.PipelineSteps = "copy"

        doc.recompute()
        doc.commitTransaction()
    except Exception:
        doc.abortTransaction()
        raise

    logger.info("Working copy '%s' <- '%s' (%d solids)",
                copy_label, obj.Label, len(copy_obj.Shape.Solids))
    return copy_obj


def record_pipeline_step(obj, step):
    """Append a step name to an object's PipelineSteps provenance log.

    Parameters
    ----------
    obj : App::DocumentObject
        A working copy created by `create_working_copy()`.
    step : str
        Short token for the step, e.g. ``"orient"``, ``"split"``,
        ``"supports"``, ``"pins"``, ``"raft"``.
    """
    existing = getattr(obj, "PipelineSteps", None)
    if existing is None:
        logger.warning("Object '%s' has no PipelineSteps property -- skipping",
                       obj.Label)
        return
    obj.PipelineSteps = f"{existing};{step}" if existing else step


# ---------------------------------------------------------------------------
# Build Volume Check
# ---------------------------------------------------------------------------

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
