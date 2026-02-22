# Blender

## Installation

| | |
|---|---|
| Version | 5.0.1 |
| Location | `/Applications/Blender.app` |
| MCP bridge addon | `~/Library/Application Support/Blender/5.0/scripts/addons/blender_mcp_bridge.py` |

## MCP Server

Blender is controlled from Claude via the Blender MCP bridge.

- Bridge must be running (enabled in addon prefs, port 9876)
- `manage_connection(action='reconnect')` if connection drops

## Scene Defaults

- Units: **mm** (`scale_length=0.001`, `length_unit='MILLIMETERS'`)
- Saved in `startup.blend` — STL exports are in mm, no fudge factor needed

## Addons

| Addon | Source | Status | Notes |
|-------|--------|--------|-------|
| Blender MCP Bridge | local | Active | Port 9876 |
| Sapling Tree Gen | extensions.blender.org (`sapling_tree_gen` v0.3.7) | Installed | Not bundled in 5.0; installed via extensions API |

### Installing Extensions in 5.0

Sapling and other ex-bundled addons are now on `extensions.blender.org`.
Not bundled — must install via Extensions panel or via API:

```python
# Enable online access first
bpy.context.preferences.system.use_online_access = True
# Install
bpy.ops.extensions.package_install_files(
    filepath="/path/to/addon.zip",
    repo="user_default",
    enable_on_install=True,
)
```

## Workflows

### Tree Generation (Sapling)

- Use `bpy.ops.curve.tree_add(bevel=True, ...)` — **`bevel=True` is required**, defaults to False
- After generation: convert curve → mesh, then Voxel Remesh to merge branch tubes into watertight solid
- Key parameters: `shape` (0=conical, 1=spherical), `segSplits` (forking), `downAngle` (branch spread)
- Conifer: `downAngle level-1 ≈ 88°`, `useParentAngle=False`, `shape='0'`
- Oak: `shape='1'`, `segSplits=(0, 0.4, 0.3, 0)`, `attractUp` slightly negative for droop

### STL Export for Printing

- Export at scale=1 (scene is already in mm)
- Always check manifold before export: `check_mesh_printability()`
- Voxel remesh voxel size: ~0.5 mm for HO trees at 40–60 mm height

## Notes

- `EXEC_DEFAULT` flag skips operator confirmation dialogs
- `read_factory_settings()` resets all addons — **do not use**
