# 3D Print Tooling Roadmap

Status of the automated print pipeline and planned improvements.
Last updated: 2026-04-06 (refactored: split_utils decomposed, constants centralized).

## Pipeline Overview

```
Source model
  → auto_slice (determine cuts for build volume)
  → split_model_plane (cut the model)
  → add_registration (pins/sockets/tabs at cut faces)
  → add_bracing (temporary sprue runners between pins)
  → orient/tilt for printing
  → generate supports (context-aware)
  → export STL
  → slicer
```

## What Exists

| Module | Status | Notes |
|--------|--------|-------|
| `constants.py` | Working | Shared constants (printer volumes, pipeline metadata) |
| `pipeline.py` | Working | Pipeline infrastructure (working copy, provenance, build fit) |
| `split.py` | Working | Model splitting (arbitrary plane, axis-aligned) |
| `registration.py` | Working | Pin/socket, tab/slot, blister registration features |
| `bracing.py` | Working | Temporary sprue runner bracing for split pieces |
| `split_utils.py` | Shim | Re-export shim for backward compatibility |
| `export_utils.py` | Working | STL export pipeline (shift, tessellate, validate, write) |
| `auto_slice.py` | Working | Tilt envelope, detail zone avoidance, cut planning |
| `support_utils.py` | Working | Context-aware supports, face classification, collision detection |
| `sprue_utils.py` | Working | Batch runner/gate frames for multi-copy prints |
| `generate_building_print.py` | Working | SecondaryBuilding-specific orchestration |
| `thin_walls.py` | Working | Test wall panel generator (clapboard, windows) |

## Needed Improvements

### Pipeline gaps

- ~~**STL export function**~~ Done (`export_utils.py`).

- **Pipeline orchestrator** — `generate_building_print.py` is model-specific.
  Need a general pipeline: model → slice → split → register → brace → orient →
  support → export. Should handle N pieces from auto_slice.

- **Cut position validation / nudge** — auto_slice picks positions that can land
  on model feature boundaries (vertices, edges), causing degenerate boolean results.
  Need a probe step that tests the cut and offsets 0.1-0.2mm if it fails.

- **Multi-piece layout** — After splitting, pieces need to be arranged on the
  build plate (or across multiple plates). Currently manual.

- **Piece-specific orientation** — Each split piece may want different tilt angles.
  Currently one tilt applies to the whole model.

### Support generation improvements

- **Tilt optimization** — Tilt angles are currently hand-tuned per model (18° X,
  5° Y, etc.). Need a solver that finds optimal tilt for minimum support contact
  on display surfaces while maintaining liftability.

- **Support density tuning** — Grid spacing is fixed. Should adapt to local
  geometry: denser near thin features, sparser on solid slabs.

- **Drain hole awareness** — Hollow models need drain holes; support placement
  should avoid blocking them.

- **Raft improvements** — Current raft is a simple rectangle. Could be contour-
  following to reduce resin waste and peel force.

- **Support-free zone enforcement** — Explicit "no supports here" regions for
  visible interiors, mating surfaces, etc. Currently implicit via face
  classification but not user-controllable.

- **Liftability check** — Verify that the supported model won't fail peel forces.
  Partially implemented but not integrated into the pipeline.

### Model building tools

- **Clapboard generator** — `thin_walls.py` has a basic implementation but it's
  a test fixture, not a reusable tool. Need a proper parametric clapboard generator
  that works on arbitrary wall faces. Critical because hand-rolled clapboard
  arrays are the primary source of boolean/removeSplitter failures.

- **Imported master placement** — Automating placement of pre-built window and
  door assemblies (masters) into wall openings. The Gordonsville station has
  dozens of windows/doors placed manually. Need: identify openings from wall
  punchout geometry, match master to opening size, place with correct orientation
  and offset. Partially explored but no code yet.

- **Corner board / trim generator** — Corner boards, fascia, water table trim.
  Currently hand-modeled.

- **Foundation generator** — Stone/brick foundation from wall footprint. Currently
  hand-modeled.

### Robustness

- **Invalid solid handling** — Boolean fuse/cut fails silently on invalid input
  solids (unclosed shells from MultiFuse). Options: mesh-level fallback, input
  validation with actionable error messages, auto-repair via sewing.

- **Floor edge filtering** — `_is_floor_edge()` in split_utils is a stub. Braces
  should skip floor-spanning edges but detection isn't implemented.

- **Brace geometry validation** — Runner and neck-down geometry hasn't been
  visually confirmed on a real model yet. Needs testing on a clean (valid) solid.

- **Test coverage** — FreeCAD-dependent functions (support builders, face
  classification, split operations) lack unit tests. Pure-math functions are
  well tested.

### Documentation

- **Boolean Troubleshooting wiki page** — Draft exists in `bugs/`. Needs diagrams
  and submission to FreeCAD wiki (pending PR #29134).

- **Resin profiles** — Template exists but no profiles defined yet. Need to
  document settings for each resin as they're dialed in.

## Filed Issues

- **OCC #1193** — `ShapeUpgrade_UnifySameDomain` self-intersection bug
  (removeSplitter on coplanar wedge geometry)
- **FreeCAD PR #29134** — Improved boolean error messages + refine validation
  (on branch `part-refine-validation`)
- **FreeCAD PR #27760** — Closed (replaced by #29134)
