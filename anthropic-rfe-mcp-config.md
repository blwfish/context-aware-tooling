# RFE: MCP Configuration Is Fragmented, Undiscoverable, and Silently Accumulates Stale State

Draft for https://github.com/anthropics/claude-code/issues

---

## Summary

Configuring and maintaining MCPs is an excruciating user experience, due to fragmented, inconsistent control structures. Getting MCP servers correctly configured requires finding and editing up to four separate configuration files across two different applications — while also navigating a third category of platform-injected MCPs that are invisible to the user, unlistable, and undocumented — with no cross-referencing between any of them, no unified diagnostic tool, and no documentation that describes the full system. The permission format for MCP tools is not surfaced in any `--help` output. Stale project-level overrides accumulate silently and can shadow working global configs with broken ones. The result is a debugging experience that is needlessly painful even for technical users.

## The MCP Layers (It's Actually Five)

There are three distinct categories of tools in a Claude session, only one of which is user-configurable:

**Layer 1 — Native built-in tools** (Bash, Read, Write, Edit, Glob, Grep, etc.): compiled into Claude Code. Not MCPs. Not affected by any of the config files below.

**Layer 2 — User-configured MCPs**: what this issue is about.

**Layer 3 — Platform-injected MCPs**: servers silently injected by the Claude.ai platform at session start — `mcp__Claude_in_Chrome__*`, `mcp__Claude_Preview__*`, `mcp__mcp-registry__*`, and potentially others. These do not appear in any config file, are not listed by `claude mcp list`, and cannot be audited or controlled by the user. It is not documented whether user `permissions.allow` entries interact with them or not.

Within Layer 2 alone, there are four separate config locations:

| What | File | Managed by |
|------|------|------------|
| Desktop app MCPs (Chat/Cowork/Code) | `~/Library/Application Support/Claude/claude_desktop_config.json` | Manual edit only |
| CLI MCP server definitions | `~/.claude.json` → `mcpServers` | `claude mcp add` |
| CLI permissions/allow rules | `~/.claude/settings.json` | Manual edit only |
| Per-project MCP overrides | `~/.claude.json` → `projects.<path>.mcpServers` | Manual edit only |

These four locations are **completely independent**. A server defined in one is invisible to the others. There is no documentation that lists all four together, explains their relationships, or describes how any of them interact with the platform-injected Layer 3 servers.

## Specific Problems

### 1. Desktop app and CLI are silently separate

The Claude desktop app ("Chat / Cowork / Code") and the Claude Code CLI both support MCP servers, but they read from different config files and have no awareness of each other.

A user who configures an MCP via `claude mcp add` will find it working in the CLI but missing in the desktop app — and vice versa. The desktop app shows "Server disconnected" with no indication that the server is actually running fine in a parallel config system. There is nothing in either UI that says "hey, these are two separate MCP systems."

**Suggested fix**: Either unify the config (one source of truth for MCP server definitions), or at minimum add a note in both UIs pointing to the other config location when an MCP is missing or failing.

### 2. MCP permission format is completely undiscoverable

To auto-approve MCP tool calls, you add entries like `"mcp__freecad__*"` to `permissions.allow` in `~/.claude/settings.json`. This format does not appear anywhere in `claude --help`, `claude mcp --help`, or the `claude mcp add` output. It is not mentioned in the settings file itself. The only way to discover it is to search the binary or find a community post.

**Suggested fix**: Document the `mcp__<server>__<tool>` format in `claude mcp --help` output and/or in a comment block in the generated settings file.

### 3. Stale project-level MCP overrides accumulate silently and cause confusing failures

`~/.claude.json` stores per-project `mcpServers` overrides keyed by directory path. These accumulate indefinitely — old worktrees, temp directories, deployment staging folders. When a user happens to work from one of these directories, the stale project-level config **silently overrides the working global config** with a broken one.

There is no tool to audit or clean these up. `claude mcp list` shows the current session's resolved servers but doesn't indicate whether they came from global or project scope, or flag shadowed entries.

**Suggested fix**:
- Add a `claude mcp list --all` that shows all configured servers across all scopes, flagging stale paths
- Add a `claude mcp audit` or similar that surfaces project-level overrides pointing to nonexistent paths
- Consider TTL or last-used pruning for project-level entries

### 4. The CLI binary path changes on every desktop app update

The Claude Code CLI binary is installed at a versioned path:
```
~/Library/Application Support/Claude/claude-code/<version>/claude
```

This path changes with every desktop update, breaking any shell alias, script, or tool that references it by full path. The installer does not create a stable symlink in `$PATH`.

Workaround: a wrapper script that globs the latest version at call time. This works but shouldn't be necessary.

**Suggested fix**: Install a stable symlink at a standard location (`/usr/local/bin/claude`, `/opt/homebrew/bin/claude`, or similar) during installation and update it on each version change. Standard practice for versioned CLI tools.

## Impact

These are not obscure edge cases. Every user who wants to:
- Use MCPs in both desktop and CLI contexts
- Auto-approve MCP tool calls without per-session prompts
- Understand why an MCP is failing

...will hit at least one of these. The current state makes MCP setup feel like an undocumented puzzle rather than a supported feature.

The problem also scales badly. With a single MCP, the fragmentation is annoying. With three — FreeCAD, Blender, and KiCad, in our case — each server must be registered and maintained across multiple independent config files, permissions must be granted separately, and stale override entries accumulate in proportion to the number of servers and projects. The complexity is effectively O(n²) in the number of MCPs, which is the wrong shape for something that's supposed to be a first-class extensibility mechanism.

## What Would Help Most (Priority Order)

1. Stable CLI binary path (symlink on install)
2. Document MCP permission format in `--help`
3. Unify or cross-reference desktop/CLI MCP configs
4. `claude mcp audit` to surface stale project-level overrides
5. Document platform-injected MCPs: what they are, how they're controlled, and how they interact with user-configured permissions
