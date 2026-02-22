# Manufacturing Assistant

Claude acts as a manufacturing assistant, orchestrating fabrication workflows across multiple source tools and output processes. The tooling database (this repo) is the primary knowledge source. Claude provides the semantic layer — knowledge of what parts *mean* and what constraints apply — which geometry-blind tools fundamentally lack.

## The Core Insight

Every fabrication post-processing step has constraints that derive from semantics, not geometry alone:

- A slicer rotating parts for better packing doesn't know that clapboard walls must stay upright
- An auto-support algorithm doesn't know that mullions are minimum-printable features that must not be touched
- A nesting tool doesn't know that photo etch tabs must not land on fold lines
- A CAM tool doesn't know that cutting across wood grain causes tear-out

Claude knows these things. The tools don't.

## Architecture

```
  Sources                   Claude                    Outputs
  ───────               ─────────────               ─────────
  FreeCAD ──MCP──┐     ┌─────────────┐     ┌──── STL → resin printer
  Blender ──MCP──┤────▶│  orchestrate │────▶├──── G-code → CNC router
  QCAD ──file────┤     │  + semantic  │     ├──── nested DXF → laser
  STL/DXF files ─┘     │    layer     │     ├──── artwork PDF → photo etch
                        │             │     └──── Gerbers → PCB fab
                        │  tooling DB │
                        │  heuristics │
                        └─────────────┘
```

**Claude** — orchestrator and semantic layer. Reads tooling database and heuristics files, drives MCPs, moves geometry between tools, enforces constraints.

**MCP servers** — programmatic access to source tools:

| Tool | MCP | Status |
|------|-----|--------|
| FreeCAD | freecad-mcp | Active |
| Blender | blender-mcp-bridge | Active |

Non-MCP sources (QCAD, files on disk) are accessed via file handoff.

**Tooling database** — this repo. Machine profiles, material properties, process parameters, confidence tiers. Ground truth for physical constraints.

**Heuristics files** — project-type-specific domain knowledge (see below).

## Source Tools

### FreeCAD
Parametric solid modeling and CAM. Primary source for engineering parts, buildings, structures. Full MCP access.
- Exports: STL (for mesh handoff to Blender or printer), DXF (for laser/photo etch), G-code (via Path workbench for CNC)

### Blender
Procedural geometry generation and mesh post-processing. Primary source for organic geometry (trees, terrain, textures). Full MCP access.
- Generates: tree armatures (Sapling), terrain meshes, displacement textures
- Post-processes: support generation, build plate packing, mesh analysis (BVHTree raycast)
- Exports: STL

### QCAD / other DXF sources
2D drafting. No MCP — file handoff only. DXF output feeds laser and photo etch workflows.

### Files on disk
STL, DXF, OBJ, STEP. Any workflow can accept files directly as inputs alongside MCP-sourced geometry.

## Inter-tool Geometry Handoff

Geometry moves between tools via temp files as needed. Direction depends on the workflow.

| From | To | Format | Path |
|------|----|--------|------|
| FreeCAD | Blender | STL | `/tmp/` |
| Blender | FreeCAD | STL/OBJ | `/tmp/` |
| FreeCAD | laser / photo etch | DXF | direct export |
| FreeCAD | CNC | (internal) | FC Path workbench |
| Blender | printer | STL | direct export |

The FC→Blender→printer path is the canonical print pipeline. The Blender→FC→CNC path enables procedural geometry (terrain, textures) to be machined.

## Output Processes

### Resin Printer (MSLA)
- **Input**: STL
- **Post-processing**: support generation, build plate packing
- **Output**: single STL (model + supports merged)
- **Consumer**: Chitubox → AnyCubic M7 Pro/Max
- **Semantic constraints**: feature fragility (mullions, thin walls), support avoidance zones, orientation requirements
- **Machines**: [`3dprinting/anycubic-m7pro-max.md`](3dprinting/anycubic-m7pro-max.md)
- **Project heuristics**: [`3dprinting/project-types/`](3dprinting/project-types/) *(to be populated)*

### FDM Printer
- **Input**: STL
- **Post-processing**: support generation, orientation, bed adhesion
- **Output**: STL
- **Consumer**: slicer → printer
- **Semantic constraints**: layer adhesion direction, bridging limits, seam placement
- *(No machine currently; pattern established for future use)*

### CNC Router
- **Input**: solid model (FC) or mesh (Blender → FC import)
- **Post-processing**: CAM toolpath generation in FC Path workbench
- **Output**: G-code (GRBL post-processor)
- **Consumer**: Genmitsu 4030 ProVerXL2
- **Semantic constraints**: grain direction, climb vs. conventional by material, feature fragility, step-down limits
- **Machines**: [`cnc/genmitsu-4030-proverxl2.md`](cnc/genmitsu-4030-proverxl2.md)

### Laser Cutter
- **Input**: DXF (from FC or QCAD)
- **Post-processing**: nesting/arrangement on sheet
- **Output**: nested DXF → XCS
- **Consumer**: xTool S1 via xTool Creative Space
- **Semantic constraints**: material grain/texture orientation (clapboard walls must not rotate), kerf compensation, cut order (inner cuts before outer), laser type × material compatibility (hard filter — see [`laser/README.md`](laser/README.md))
- **Machines**: [`laser/README.md`](laser/README.md)

### Photo Etch
- **Input**: DXF with distinct layers (cut lines, full-etch, half-etch/fold lines)
- **Post-processing**: nesting on etch sheet, tab placement, registration mark insertion
- **Output**: two print-ready artwork files (front + back), PDF or PNG
- **Consumer**: laser printer → UV exposure → chemical etch
- **Semantic constraints**:
  - Tabs must not land on fine detail or fold lines
  - Acid clearance required between parts
  - Front/back registration must be exact
  - Half-etch and fold-line layers must be preserved through nesting
  - Part orientation may be constrained (asymmetric half-etch detail)
- **Sheet parameters**: material (brass, nickel silver, etc.), thickness, sheet dimensions
- *(Planned; expected within ~1 year)*

### PCB
- **Input**: KiCad project
- **Post-processing**: DRC, Gerber export
- **Output**: Gerber package
- **Consumer**: board fabricator
- **Semantic constraints**: design rules, clearances, layer stack, courtyard violations
- **Tools**: [`ecad/README.md`](ecad/README.md)

## Heuristics Files

Heuristics capture project-type domain knowledge that is not machine-specific. They are Markdown files, written in prose with embedded parameters. Claude reads and interprets them — they are not parsed programmatically. This means they can express rules that have no algorithmic equivalent ("if a support on a mullion is unavoidable, place it at the intersection of mullions so it has support in all directions").

**Hierarchy** (later overrides earlier):
1. Machine profile — physical constraints, always applies
2. Project-type heuristics — domain rules for this category of part
3. Per-session overrides — stated in conversation

**Location convention**: `tooling/<process>/project-types/<type>.md`

**Examples** (to be written):
- `3dprinting/project-types/building.md` — mullion avoidance, corner vs. wall support weight, parameter references to `Skeleton.FCStd`
- `3dprinting/project-types/forest-armatures.md` — aggressive supports acceptable (foliage covers), interlocked pairs, shared sprue design, batch metadata
- `laser/project-types/building-walls.md` — clapboard orientation lock, panel vs. flat stock rules

**Parameters sourced from models**: Heuristics files may reference parameters that live in FC model files (e.g., minimum printable feature size from `Skeleton.FCStd`). Claude reads these via the FC MCP and applies them.

## Workflow

**Trigger**: conversational instruction, e.g.:
> "Print the bracket from FC and the base from Blender on the Max plate"
> "Nest these DXF walls for the xTool — the clapboard sheet must stay upright"
> "Generate a CNC job for the terrain mesh on the Genmitsu, oak, with the grain running along X"

**Claude's steps**:
1. Identify all named objects and their sources
2. Read relevant machine profile(s) and project-type heuristics
3. Move geometry between tools as needed (MCP calls or file handoff)
4. Apply post-processing with semantic constraints enforced
5. Produce fabrication-ready output
6. Report what was done, including any constraint decisions ("declined to rotate `wall_04` — clapboard orientation")

## Implementation Status

| Capability | Status |
|-----------|--------|
| FreeCAD MCP | Active |
| Blender MCP | Active |
| FC → Blender mesh handoff | Designed, not yet exercised |
| Blender → FC mesh handoff | Designed, not yet exercised |
| Auto-support, resin (general framework) | Designed, not yet implemented |
| Auto-support, forest armatures | Partially implemented |
| Sheet nesting, laser | Not yet implemented |
| Photo etch nesting | Not yet implemented |
| Terrain → FC → CNC pipeline | Terrain generator complete; CAM integration partial |
| Project-type heuristics files | Not yet written |

## Related Documents

- [`3dprinting/`](3dprinting/) — resin printer profiles and resin library
- [`cnc/`](cnc/) — CNC machine and tool profiles
- [`laser/`](laser/) — laser machine and material settings
- [`ecad/`](ecad/) — KiCad / PCB workflow
- [`freecad/`](freecad/) — FreeCAD installation, MCP, workbenches
- [`blender/`](blender/) — Blender installation, MCP, workflows
