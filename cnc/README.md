# CNC

## Machine

- **[Genmitsu 4030 ProVerXL2](genmitsu-4030-proverxl2.md)** — GRBL, 400×300 mm, 4th axis available

## Tools

Bit library is in [tools/](tools/). Track per-bit: diameter, type, material suitability, feeds/speeds, and remaining life.

## CAM

FreeCAD Path workbench is the primary CAM tool. See [../freecad/README.md](../freecad/README.md).

- Post processor: `grbl`
- Feed units in FreeCAD: mm/min → GRBL outputs mm/min (OCL surface ops: mm/s internally, ×60 in post)

## Materials

| Material | Status | Notes |
|----------|--------|-------|
| MDF | Working | Good for terrain bases |
| Hardwood | — | |
| Aluminum | — | Slow feeds, flood coolant ideal |
| Foam | — | |
