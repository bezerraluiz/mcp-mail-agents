# mcp-mail-agents

MCP server for async communication between AI agents via file-based mailboxes.

Each agent (Claude, GPT, Gemini, Deepseek…) has its own inbox. Messages are
`.md` files with YAML frontmatter. An `inbox-index.yaml` per inbox keeps context
window usage low — agents read the index first and open only relevant messages.

## What is MCP?

Model Context Protocol (MCP) is an open standard that lets AI models connect to
external tools and data sources through a uniform interface. Instead of each AI
needing a custom integration for every tool, MCP works like a universal adapter:
you build the server once and any MCP-compatible client (Claude Code, Codex CLI,
Cursor, Windsurf, etc.) can use it immediately.

An MCP server exposes **tools** — functions the AI can call — and optionally
**resources** (read-only data) and **prompts** (reusable templates). The AI
decides when and how to call them based on the task at hand.

This package is an MCP server. It exposes tools for reading inboxes, sending
messages, managing tasks, and coordinating review cycles between AI agents.

## Install & run

```bash
uvx mcp-mail-agents
```

Or with pip:

```bash
pip install mcp-mail-agents
mcp-mail-agents
```

## Configure in Claude Code

Add to your project's `.claude/settings.json`:

```json
{
  "mcpServers": {
    "mail-agents": {
      "command": "uvx",
      "args": ["mcp-mail-agents"],
      "env": {
        "AGENTS_ROOT": "."
      }
    }
  }
}
```

`AGENTS_ROOT` is the project root where `.agents/` will be created (defaults to cwd).

## Configure in Codex CLI

Add to `~/.codex/config.toml`:

```toml
[[mcp_servers]]
name = "mail-agents"
command = "uvx"
args = ["mcp-mail-agents"]

[mcp_servers.env]
AGENTS_ROOT = "."
```

## Folder structure created

```
.agents/
  mail/
    mailbox-rules.md     ← rules file (create manually or copy from docs)
    all/                 ← broadcasts
    review/              ← tech lead queue
    claude/              ← per-agent inboxes
    gpt/
    gemini/
  tasks/                 ← agent task files
```

## Available tools (16)

| Tool | Description |
|---|---|
| `mailbox_read_inbox` | Read inbox index for an agent |
| `mailbox_read_message` | Open a specific message |
| `mailbox_mark_read` | Mark message as read |
| `mailbox_send_message` | Send message to another agent |
| `mailbox_send_broadcast` | Broadcast to multiple agents |
| `mailbox_read_broadcast_inbox` | Read the shared `all/` index |
| `mailbox_read_broadcast` | Open a broadcast message |
| `mailbox_create_review` | Post to tech lead review queue |
| `mailbox_read_review_inbox` | Read tech lead queue index |
| `mailbox_read_review_message` | Open a review message |
| `mailbox_list_tasks` | List tasks for an agent |
| `mailbox_read_task` | Read a task file |
| `mailbox_update_task_status` | Update task status + rename file |
| `mailbox_create_task` | Create a new task |
| `mailbox_list_agents` | List known agents (scan inbox dirs) |
| `mailbox_read_rules` | Read mailbox-rules.md |

## Message format

```
DD-MM-YYYY-HHmm-{from}-{to}-{subject}.md
```

```md
---
from: gpt
to: claude
date: 19-05-2026 14:30
subject: auth-refactor
status: unread
---

## Contexto
## Arquivos Alterados
## O Que Foi Feito
## Preciso Que Você Revise
## Problemas Encontrados
## Perguntas
## Próxima Ação Sugerida
```

## Publishing updates

**1. Edit `pyproject.toml` and bump the version:**

```toml
version = "0.1.1"
```

**2. Rebuild and publish to PyPI:**

```bash
uv build
uv publish --token pypi-YOUR_TOKEN
```

**3. Commit and tag the release on GitHub:**

```bash
git add -A
git commit -m "chore: release v0.1.1"
git tag v0.1.1
git push && git push --tags
```

After publishing, users running `uvx mcp-mail-agents` will automatically get the
latest version on their next run.

## License

MIT
