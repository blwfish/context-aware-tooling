# Boolean Troubleshooting

Boolean operations (Fuse, Cut, Common) combine shapes by computing where
they overlap. This page explains common failures, why they happen, and
what you can do about them.

## The key concept

When you design a model, you think in exact geometry: this face is
*exactly* flat, these two walls meet at *exactly* the same plane, this
array steps by *exactly* 10 mm. Your design intent is mathematically
precise.

But computers represent numbers with limited precision -- about 15
significant digits. Every coordinate, every face boundary, every
intersection point is rounded to fit. Most of the time this doesn't
matter: the errors are smaller than an atom. But when the geometry engine
needs to decide whether two faces are *exactly* aligned, or whether a
point is *exactly* on a surface, these tiny rounding errors can lead to
wrong answers. The engine might see a microscopic gap where there should
be none, or a tiny overlap where two faces should meet cleanly.

This is the root cause of most boolean failures. Your model is correct.
The computer's representation of it has limitations that the engine can't
always handle.

<!-- TODO: diagram showing exact alignment (abstract) vs
floating-point representation with exaggerated gap/overlap -->

## "Boolean operation failed"

### What happened

You asked FreeCAD to combine two shapes, and the geometry engine
(OpenCASCADE) couldn't compute the result.

### Arrangements that cause problems

- **Shapes that share an exact face.** Two walls meeting at a shared
  flat surface. The engine must decide which shape "owns" the boundary,
  and rounding errors make this ambiguous.

  <!-- TODO: diagram -- two boxes sharing a face, works vs fails -->

- **Faces that nearly but don't quite touch.** Two faces within a
  fraction of a micrometer of each other. The engine can't tell if
  they're supposed to be touching, overlapping, or separate.

- **Coplanar faces with partial overlap.** Multiple shapes sharing faces
  in the same plane, but with different extents -- for example, a row of
  planks fused to a wall that's wider than the planks. The engine has
  more boundaries to resolve and more opportunities for rounding errors.

- **Rotated or transformed geometry.** A shape at the origin might have
  clean coordinates (10.0, 20.0, 0.0). Rotate it by an arbitrary angle
  and every vertex becomes an irrational number truncated to 15 digits.
  Two shapes that are mathematically coplanar after rotation may not be
  exactly coplanar in the computer's representation.

These situations arise naturally in real models -- walls meeting at
corners, arrays of parts that tile exactly, assemblies of parts designed
to fit together precisely, or any geometry that's been rotated or
mirrored into position.

### What to try

**Reposition one shape slightly.** If two shapes share an exact face,
offsetting one by a tiny amount eliminates the ambiguous boundary. In the
Properties panel, find the shape's Placement, and add a small offset to
the appropriate axis -- for example, change Z Position from 0.00 to
0.01. Use a value smaller than your manufacturing tolerance so it
doesn't affect the physical result.

<!-- TODO: diagram -- before (exact alignment, fails) vs after
(0.01 offset, succeeds) -->

**Combine in a different order.** If you're fusing many shapes, try
grouping them differently. Sometimes the engine succeeds with A+B first,
then adding C, even though A+B+C all at once fails.

**Simplify the operation.** If you're combining many shapes at once
(MultiFuse), try fusing a few at a time in stages.

**Check your shapes first.** Use Part > Check Geometry on each input
shape individually. If a shape has problems before the boolean, fix
those first -- subsequent operations will inherit and often amplify the
issue.

## "Refine produced invalid geometry"

### What happened

The boolean operation itself succeeded, but the cleanup step afterward
(called "Refine") damaged the result. FreeCAD detected this and kept the
pre-cleanup version instead.

### Why it happens

After a boolean, the result often has extra internal edges where the
original shapes met. Refine tries to merge adjacent faces and remove
these edges. However, the algorithm that does this has a known bug in
the underlying geometry engine (OpenCASCADE): with certain face
arrangements, it produces geometry that folds back on itself. This is
difficult to work around reliably at the FreeCAD level because the
corruption happens deep inside the engine.

<!-- TODO: diagram -- clean boolean result with extra edges vs
corrupted refined result -->

### What this means for your model

The shape FreeCAD kept is geometrically correct -- it has the right
dimensions, volumes, and surfaces. It just has extra edges that Refine
would normally remove. In many cases these extra edges don't matter, but
they can:

- Make the shape look more complex than it needs to be
- Slow down subsequent boolean operations on complex models
- Cause issues with filleting or chamfering across the extra edges

### What to try

**Disable Refine on this feature.** In the Properties panel, select the
feature in the model tree and set its Refine property to False. This
prevents the cleanup step from running. Your boolean result will have
extra edges but will be geometrically correct.

**Restructure your geometry.** If the extra edges cause real problems
downstream, try adjusting your design so the input shapes don't create
the face arrangement that triggers the bug. Typically this means
avoiding shapes that share an exact face while a third shape partially
overlaps that boundary. For example, if you have an array of wedge-shaped
planks fused with a wall, extending the wall to fully cover the planks
(or not overlap at all) may avoid the problem.

## Enabling automatic detection

FreeCAD can automatically check whether the Refine step damaged your
geometry. This check adds some computation time, but catches the problem
immediately instead of letting it cause mysterious failures later.

To enable it: Edit > Preferences > Part Design > General > **"Validate
refine result and warn on failure."**

When enabled, FreeCAD will skip the Refine step if it would produce bad
geometry, keep the correct pre-Refine result, and show a warning in the
Report View explaining what happened.

## FreeCAD's built-in protections

FreeCAD includes a **fuzzy tolerance** mechanism to handle near-
coincident geometry automatically. It computes a tolerance value based on
the size of your model and applies it to boolean operations, so that
points closer together than this tolerance are treated as identical. This
resolves many floating-point issues without any user intervention, but
it cannot handle every case -- particularly when the geometric
arrangement itself (not just the numeric precision) is the problem.

## See also

- [[Part_CheckGeometry|Part Check Geometry]] -- tool to inspect shapes
  for problems
- [[Part_RefineShape|Part Refine Shape]] -- the standalone Refine
  operation
- [[Part_Boolean|Part Boolean]] -- overview of boolean operations
