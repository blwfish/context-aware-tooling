# Tooling

Settings, profiles, calibration data, and workflow notes for all fabrication tools.

## Directories

| Directory | Tool(s) |
|-----------|---------|
| [3dprinting/](3dprinting/) | AnyCubic M7 Pro/Max, Chitubox |
| [cnc/](cnc/) | Genmitsu 4030 ProVerXL2 (GRBL) |
| [laser/](laser/) | — |
| [ecad/](ecad/) | KiCad |
| [freecad/](freecad/) | FreeCAD (custom build) |
| [blender/](blender/) | Blender 5.x |

## Manufacturing Assistant

Claude orchestrates fabrication workflows across all tools in this repo. See **[manufacturing-assistant.md](manufacturing-assistant.md)** for the full spec: architecture, semantic constraint system, output processes, heuristics file conventions, and implementation status.

## Philosophy

- Record what actually worked, not just nominal settings
- Note failure modes alongside successes — they're equally useful
- Track date and context when something changes significantly
- Prefer small focused test prints/cuts over full-size trial-and-error

## Architecture

This repo is the **canonical source of truth** for fabrication knowledge. Design goals:

- **Markdown as source** — human-readable, git-diffable, LLM-agnostic. Any LLM (or human) can read it.
- **SQLite as artifact** — CI generates a SQLite database from the markdown on every merge. Faster to query than parsing markdown at runtime.
- **Claude as primary consumer** — structured to be obvious for Claude, but not exclusive to it.
- **Community-contributable** — PRs are the contribution mechanism; Claude can act as a submission front-end for non-git users.

### Confidence Tiers

Every settings entry carries a confidence tier:

| Tier | Meaning |
|------|---------|
| **Verified** | Multiple independent submissions agree within tolerance, or maintainer-confirmed |
| **Submitted** | Single source, passed automated plausibility check (physically reasonable values) |
| **Unverified** | First/only data for this machine or combination — useful, but caveat emptor |

### Data Model

The underlying model is a **hypergraph**: each settings record is a relationship between machine × tool/head/nozzle × material × operation. The compatibility layer (what works with what — e.g. laser wavelength vs material) is graph-structured and handled through Claude's reasoning. The settings/measurements layer is relational and lives in SQLite.

Note: Kuzu (embedded graph DB) was evaluated but was acquired by Apple (Oct 2024) and its repos archived. SQLite with recursive CTEs + Claude reasoning covers the graph layer adequately at hobby scale.
