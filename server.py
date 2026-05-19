"""
AI Mailbox MCP Server

Async communication system between AI agents via file-based mailboxes.
Each agent has its own inbox with an index file for efficient context usage.

Configure via env var:
  AGENTS_ROOT  — path to the project root where .agents/ lives (default: cwd)
"""

from __future__ import annotations

import os
import re
from datetime import datetime
from pathlib import Path
from typing import List

import yaml
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("AI Mailbox")


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

def _agents_dir() -> Path:
    return Path(os.environ.get("AGENTS_ROOT", ".")) / ".agents"

def _mail_dir() -> Path:
    return _agents_dir() / "mail"

def _tasks_dir() -> Path:
    return _agents_dir() / "tasks"

def _inbox_dir(agent_id: str) -> Path:
    return _mail_dir() / agent_id

def _inbox_index(agent_id: str) -> Path:
    return _inbox_dir(agent_id) / "inbox-index.yaml"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now_display() -> str:
    return datetime.now().strftime("%d-%m-%Y %H:%M")

def _now_file() -> str:
    return datetime.now().strftime("%d-%m-%Y-%H%M")

def _safe_slug(text: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_-]", "-", text).strip("-")

def _ensure_inbox(agent_id: str) -> None:
    d = _inbox_dir(agent_id)
    d.mkdir(parents=True, exist_ok=True)
    idx = _inbox_index(agent_id)
    if not idx.exists():
        idx.write_text("messages: []\n", encoding="utf-8")

def _read_index(agent_id: str) -> dict:
    _ensure_inbox(agent_id)
    raw = _inbox_index(agent_id).read_text(encoding="utf-8")
    return yaml.safe_load(raw) or {"messages": []}

def _write_index(agent_id: str, data: dict) -> None:
    _inbox_index(agent_id).write_text(
        yaml.dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )

def _append_to_index(agent_id: str, entry: dict) -> None:
    data = _read_index(agent_id)
    data.setdefault("messages", []).append(entry)
    _write_index(agent_id, data)

def _read_dir_index(directory: Path) -> dict:
    idx = directory / "inbox-index.yaml"
    if not idx.exists():
        return {"messages": []}
    return yaml.safe_load(idx.read_text(encoding="utf-8")) or {"messages": []}

def _write_dir_index(directory: Path, data: dict) -> None:
    idx = directory / "inbox-index.yaml"
    idx.write_text(yaml.dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")

def _append_dir_index(directory: Path, entry: dict) -> None:
    data = _read_dir_index(directory)
    data.setdefault("messages", []).append(entry)
    _write_dir_index(directory, data)

def _build_message(from_id: str, to: str, subject: str, body: str) -> str:
    return (
        f"---\nfrom: {from_id}\nto: {to}\ndate: {_now_display()}\n"
        f"subject: {subject}\nstatus: unread\n---\n\n"
        f"# [{from_id} -> {to}]\n\n## Data\n{_now_display()}\n\n{body}\n"
    )


# ---------------------------------------------------------------------------
# Tools — Inbox
# ---------------------------------------------------------------------------

@mcp.tool()
def mailbox_read_inbox(agent_id: str) -> str:
    """
    Read the inbox-index.yaml for an agent. Always call this first before
    opening individual messages — it shows subject, sender, date and status
    without consuming full message content.
    """
    data = _read_index(agent_id)
    return yaml.dump(data, allow_unicode=True)


@mcp.tool()
def mailbox_read_message(agent_id: str, filename: str) -> str:
    """Read a specific message from an agent's inbox by filename."""
    path = _inbox_dir(agent_id) / filename
    if not path.exists():
        return f"ERROR: message not found — {filename}"
    return path.read_text(encoding="utf-8")


@mcp.tool()
def mailbox_mark_read(agent_id: str, filename: str) -> str:
    """Mark a message as read in the inbox index."""
    data = _read_index(agent_id)
    for msg in data.get("messages", []):
        if msg.get("file") == filename:
            msg["status"] = "read"
            break
    _write_index(agent_id, data)
    return f"Marked as read: {filename}"


@mcp.tool()
def mailbox_send_message(from_id: str, to_id: str, subject: str, body: str) -> str:
    """
    Send a message from one agent to another.

    body should be markdown with the standard sections:
      ## Contexto
      ## Arquivos Alterados
      ## O Que Foi Feito
      ## Preciso Que Você Revise
      ## Problemas Encontrados
      ## Perguntas
      ## Próxima Ação Sugerida
    """
    slug = _safe_slug(subject)
    filename = f"{_now_file()}-{from_id}-{to_id}-{slug}.md"
    content = _build_message(from_id, to_id, subject, body)

    _ensure_inbox(to_id)
    (inbox_dir := _inbox_dir(to_id))
    (inbox_dir / filename).write_text(content, encoding="utf-8")

    _append_to_index(to_id, {
        "file": filename,
        "from": from_id,
        "date": _now_display(),
        "subject": subject,
        "status": "unread",
    })
    return f"Message sent: {filename}"


@mcp.tool()
def mailbox_send_broadcast(from_id: str, subject: str, body: str, recipients: List[str]) -> str:
    """
    Send a broadcast message to multiple agents and the shared all/ folder.

    recipients: list of agent IDs (e.g. ["claude", "gpt", "gemini"]).
    The same file is written to each inbox and to .agents/mail/all/.
    """
    slug = _safe_slug(subject)
    filename = f"{_now_file()}-{from_id}-all-{slug}.md"
    content = _build_message(from_id, "all", subject, body)

    all_dir = _mail_dir() / "all"
    all_dir.mkdir(parents=True, exist_ok=True)
    (all_dir / filename).write_text(content, encoding="utf-8")

    entry = {
        "file": filename,
        "from": from_id,
        "date": _now_display(),
        "subject": subject,
        "status": "unread",
    }
    _append_dir_index(all_dir, entry)

    for recipient in recipients:
        _ensure_inbox(recipient)
        (_inbox_dir(recipient) / filename).write_text(content, encoding="utf-8")
        _append_to_index(recipient, entry.copy())

    return f"Broadcast sent to {len(recipients)} agent(s): {filename}"


@mcp.tool()
def mailbox_read_broadcast_inbox() -> str:
    """Read the inbox-index.yaml of the shared all/ broadcasts folder."""
    all_dir = _mail_dir() / "all"
    if not all_dir.exists():
        return "messages: []\n"
    return yaml.dump(_read_dir_index(all_dir), allow_unicode=True)


@mcp.tool()
def mailbox_read_broadcast(filename: str) -> str:
    """Read a specific broadcast message from .agents/mail/all/."""
    path = _mail_dir() / "all" / filename
    if not path.exists():
        return f"ERROR: broadcast not found — {filename}"
    return path.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Tools — Review queue (tech lead inbox)
# ---------------------------------------------------------------------------

@mcp.tool()
def mailbox_create_review(from_id: str, subject: str, body: str) -> str:
    """
    Post a message to .agents/mail/review/ for the tech lead.
    Use when you must pause due to an unresolved doubt or need explicit permission.

    For cycle summaries use subject='cycle-summary' and include:
      ## Resumo do Ciclo
      ## Agentes Envolvidos
      ## Arquivos Alterados
      ## Decisões Tomadas
      ## Problemas Encontrados
      ## Pontos para Revisão
      ## Próxima Ação Sugerida
    """
    review_dir = _mail_dir() / "review"
    review_dir.mkdir(parents=True, exist_ok=True)

    slug = _safe_slug(subject)
    filename = f"{_now_file()}-{slug}.md"
    content = _build_message(from_id, "tech-lead", subject, body)

    (review_dir / filename).write_text(content, encoding="utf-8")
    _append_dir_index(review_dir, {
        "file": filename,
        "from": from_id,
        "date": _now_display(),
        "subject": subject,
        "status": "unread",
    })
    return f"Review created: {filename}"


@mcp.tool()
def mailbox_read_review_inbox() -> str:
    """Read the review inbox index (tech lead's queue)."""
    review_dir = _mail_dir() / "review"
    if not review_dir.exists():
        return "messages: []\n"
    return yaml.dump(_read_dir_index(review_dir), allow_unicode=True)


@mcp.tool()
def mailbox_read_review_message(filename: str) -> str:
    """Read a specific message from the review queue."""
    path = _mail_dir() / "review" / filename
    if not path.exists():
        return f"ERROR: review message not found — {filename}"
    return path.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Tools — Tasks
# ---------------------------------------------------------------------------

@mcp.tool()
def mailbox_list_tasks(agent_id: str) -> str:
    """
    List all task files for an agent from .agents/tasks/.
    Returns filenames — use mailbox_read_task to open one.
    """
    td = _tasks_dir()
    if not td.exists():
        return f"No tasks directory found at {td}"
    files = sorted(td.glob(f"{agent_id}-*"))
    if not files:
        return f"No tasks found for agent '{agent_id}'."
    return "\n".join(f.name for f in files)


@mcp.tool()
def mailbox_read_task(filename: str) -> str:
    """Read a specific task file from .agents/tasks/."""
    path = _tasks_dir() / filename
    if not path.exists():
        return f"ERROR: task not found — {filename}"
    return path.read_text(encoding="utf-8")


@mcp.tool()
def mailbox_update_task_status(current_filename: str, new_status: str) -> str:
    """
    Update a task status by renaming its file and updating the frontmatter.
    new_status must be one of: todo, in-progress, done, blocked.

    Example: claude-auth-refactor-in-progress.md → claude-auth-refactor-done.md
    """
    valid = {"todo", "in-progress", "done", "blocked"}
    if new_status not in valid:
        return f"ERROR: invalid status '{new_status}'. Must be one of: {', '.join(sorted(valid))}"

    path = _tasks_dir() / current_filename
    if not path.exists():
        return f"ERROR: task not found — {current_filename}"

    content = path.read_text(encoding="utf-8")
    content = re.sub(r"^status:.*$", f"status: {new_status}", content, flags=re.MULTILINE)

    stem = path.stem  # filename without .md
    # Replace the trailing status segment (last hyphen-separated token)
    parts = stem.rsplit("-", 1)
    if len(parts) == 2 and parts[1] in valid:
        new_stem = f"{parts[0]}-{new_status}"
    else:
        new_stem = f"{stem}-{new_status}"

    new_path = _tasks_dir() / f"{new_stem}.md"
    new_path.write_text(content, encoding="utf-8")
    path.unlink()

    return f"Task updated: {current_filename} → {new_path.name}"


@mcp.tool()
def mailbox_create_task(agent_id: str, task_name: str, objective: str, role: str, criteria: str) -> str:
    """
    Create a new task file in .agents/tasks/ with status=todo.

    agent_id:  the agent responsible (e.g. 'claude')
    task_name: short kebab-case name (e.g. 'auth-refactor')
    objective: what needs to be done
    role:      the agent's role/function in this cycle
    criteria:  completion criteria (markdown bullet list)
    """
    td = _tasks_dir()
    td.mkdir(parents=True, exist_ok=True)

    slug = _safe_slug(task_name)
    filename = f"{agent_id}-{slug}-todo.md"
    content = (
        f"---\ntask: {task_name}\nagent: {agent_id}\nstatus: todo\n---\n\n"
        f"## Objetivo\n{objective}\n\n"
        f"## Cargo / Função\n{role}\n\n"
        f"## Critérios de Conclusão\n{criteria}\n"
    )
    (td / filename).write_text(content, encoding="utf-8")
    return f"Task created: {filename}"


# ---------------------------------------------------------------------------
# Tools — Utilities
# ---------------------------------------------------------------------------

@mcp.tool()
def mailbox_list_agents() -> str:
    """List all known agents by scanning inbox directories in .agents/mail/."""
    md = _mail_dir()
    if not md.exists():
        return "No .agents/mail/ directory found."
    agents = [
        d.name for d in sorted(md.iterdir())
        if d.is_dir() and d.name not in {"all", "review"}
    ]
    return "\n".join(agents) if agents else "No agent inboxes found."


@mcp.tool()
def mailbox_read_rules() -> str:
    """Read .agents/mail/mailbox-rules.md (the condensed rules file for agents)."""
    path = _mail_dir() / "mailbox-rules.md"
    if not path.exists():
        return "mailbox-rules.md not found. Create it at .agents/mail/mailbox-rules.md"
    return path.read_text(encoding="utf-8")


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
