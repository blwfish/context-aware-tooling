# Claude MCP Configuration Reference

Figured out the hard way (18 commands). Written 2026-02-21.

## The Problem: Three Separate Config Systems

Claude has **three independent MCP systems** that do not share configuration:

| System | Config File | Scope |
|--------|-------------|-------|
| Claude **desktop app** (Chat/Cowork/Code tabs) | `~/Library/Application Support/Claude/claude_desktop_config.json` | Desktop app only |
| Claude **CLI** — MCP server definitions | `~/.claude.json` → `mcpServers` key | CLI global |
| Claude **CLI** — permissions/allow rules | `~/.claude/settings.json` and `settings.local.json` | CLI global |

They are **completely independent**. A server defined in one is invisible to the other. This is why you can get "MCP freecad: Server disconnected" in the desktop app while `claude mcp list` shows it connected in the CLI.

## Current Working Config

### Desktop app: `~/Library/Application Support/Claude/claude_desktop_config.json`
```json
{
  "mcpServers": {
    "freecad": {
      "command": "/Volumes/Files/claude/freecad-mcp/venv/bin/python3",
      "args": ["/Volumes/Files/claude/freecad-mcp/working_bridge.py"]
    }
  }
}
```
Add blender/kicad here too if you want them available in Chat/Cowork modes.

### CLI server definitions: `~/.claude.json` → `mcpServers`
Managed by `claude mcp add -s user <name> <command>`. Currently: freecad, blender, kicad.

Do not put server definitions in `~/.claude/settings.json` — that file is for permissions only. We made that mistake; it causes a stale duplicate.

### CLI permissions: `~/.claude/settings.json`
```json
{
  "permissions": {
    "allow": [
      "mcp__freecad__*",
      "mcp__blender__*",
      "mcp__kicad__*"
    ]
  }
}
```
MCP tool permission format: `mcp__<server-name>__<tool-name>` with `*` wildcard.
Same format goes in `settings.local.json` for machine-local overrides.

## Where Stale Configs Hide

`~/.claude.json` has a `projects` key with per-directory overrides. If a project path has `mcpServers`, it overrides the global definition **for sessions in that directory**. Old worktrees and temp directories accumulate stale entries pointing to moved/deleted paths.

To audit: `python3 -c "import json; d=json.load(open('/Users/blw/.claude.json')); [print(k, v['mcpServers']) for k,v in d['projects'].items() if 'mcpServers' in v]"`

## The CLI Binary Problem

The Claude CLI binary moves on every desktop app update:
```
~/Library/Application Support/Claude/claude-code/<version>/claude
```

Fix: stable wrapper at `/opt/homebrew/bin/claude` that globs the latest version:
```zsh
#!/bin/zsh
base="/Volumes/Files/blw/Library/Application Support/Claude/claude-code"
bin=$(ls -d "$base"/*/claude 2>/dev/null | sort -V | tail -1)
exec "$bin" "$@"
```

## MCP Servers (Current)

| Name | Command | Notes |
|------|---------|-------|
| freecad | `/Volumes/Files/claude/freecad-mcp/venv/bin/python3 working_bridge.py` | Requires FreeCAD running with AICopilot workbench |
| blender | `/Users/blw/.platformio/penv/bin/blender-mcp` | Requires Blender running with MCP bridge addon, port 9876 |
| kicad | `/Volumes/Files/claude/kicad-mcp/.venv/bin/kicad-mcp` | Standalone, no running app required |

## Useful Commands

```bash
# List global MCPs and connection status
claude mcp list

# Add a global MCP server
claude mcp add -s user <name> <command> [args...]

# The permission format for settings.json
# Allow all tools from a server:  "mcp__<server>__*"
# Allow one tool:                 "mcp__<server>__<tool>"
```
