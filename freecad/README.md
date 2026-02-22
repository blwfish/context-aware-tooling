# FreeCAD

## Installation

Custom build with local bug fixes on top of upstream weekly builds.

| | |
|---|---|
| Location | `/Volumes/Files/claude/FC-clone/` |
| Branch | `blw-fixes-v2` |
| Upstream base | `weekly-2026.02.18` |
| Build system | pixi (`~/.pixi/bin/pixi`) |
| Run | `cd FC-clone && pixi run freecad-release` |
| Build | `cd FC-clone && pixi run build-release` |

## Local Bug Fixes

| Fix | File(s) | Upstream issue |
|-----|---------|---------------|
| `BRepMesh_IncrementalMesh` crash on mesh-derived BREP shells | `AppMeshPartPy.cpp`, `Mesher.cpp` | GH #27752 |

## MCP Server

FreeCAD is controlled from Claude via the MCP server in `freecad-mcp/`.

- MCP registered with Claude Code
- AICopilot addon: `freecad-mcp/AICopilot/`
- **Do not call `read_factory_settings()`** — resets all addons including MCP

## Workbenches in Active Use

### Part Design / Part
Standard solid modeling.

### Path (CAM)
- Post processor: `grbl`
- Feed rates: mm/min in UI; OCL surface ops use mm/s internally (post ×60)
- `surface_stl` operation: custom OCL-based op in `AICopilot/ocl_surface_op.py`
- OCL shim: `FC-clone/.pixi/envs/default/lib/python3.11/site-packages/ocl.py`

### Mesh
- Built-in Surface op broken for mesh-derived BREP (GH #27752, use `surface_stl` instead)

## Terrain Generator

Procedural HO terrain mesh generator: `freecad-mcp/terrain_generator/`

- Phase 1 complete, 54 tests
- Dependencies: numpy, scipy; optional trimesh for decimation
- Spec: `terrain_generator/terrain_generator_spec.md`

## Notes

-
