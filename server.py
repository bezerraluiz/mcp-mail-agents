"""
AI Mailbox MCP Server

Async communication system between AI agents via file-based mailboxes.
Each agent has its own inbox with an index file for efficient context usage.

Configure via env var:
  AGENTS_ROOT  — path to the project root where .agents/ lives (default: cwd)
"""

from __future__ import annotations

import asyncio
import fcntl
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Callable, List

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

def _validate_agent_id(agent_id: str, require_role: bool = False) -> str | None:
    if not agent_id:
        return "agent_id cannot be empty"
    if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", agent_id):
        return (
            f"invalid agent_id '{agent_id}'. "
            "Use lowercase letters, numbers and hyphens only."
        )
    if require_role and "-" not in agent_id:
        return (
            f"invalid subagent id '{agent_id}'. "
            "Use the format 'agentname-role', for example 'claude-backend-senior'."
        )
    return None

def _validate_subagent_ids(agent_ids: List[str]) -> str | None:
    for agent_id in agent_ids:
        error = _validate_agent_id(agent_id, require_role=True)
        if error:
            return error
    return None

def _ensure_inbox(agent_id: str) -> None:
    d = _inbox_dir(agent_id)
    d.mkdir(parents=True, exist_ok=True)
    idx = _inbox_index(agent_id)
    if not idx.exists():
        idx.write_text("messages: []\n", encoding="utf-8")

def _read_index(agent_id: str) -> dict:
    idx = _inbox_index(agent_id)
    if not idx.exists():
        return {"messages": []}
    raw = idx.read_text(encoding="utf-8")
    return yaml.safe_load(raw) or {"messages": []}

def _write_index(agent_id: str, data: dict) -> None:
    _inbox_index(agent_id).write_text(
        yaml.dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )

def _mutate_yaml_index(idx: Path, mutate: Callable[[dict], None]) -> dict:
    idx.parent.mkdir(parents=True, exist_ok=True)
    with open(idx, "a+", encoding="utf-8") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        fh.seek(0)
        data = yaml.safe_load(fh.read()) or {"messages": []}
        mutate(data)
        fh.seek(0)
        fh.truncate()
        fh.write(yaml.dump(data, allow_unicode=True, sort_keys=False))
        fh.flush()
        return data

def _append_to_index(agent_id: str, entry: dict) -> None:
    def mutate(data: dict) -> None:
        data.setdefault("messages", []).append(entry)

    _mutate_yaml_index(_inbox_index(agent_id), mutate)

def _read_dir_index(directory: Path) -> dict:
    idx = directory / "inbox-index.yaml"
    if not idx.exists():
        return {"messages": []}
    return yaml.safe_load(idx.read_text(encoding="utf-8")) or {"messages": []}

def _write_dir_index(directory: Path, data: dict) -> None:
    idx = directory / "inbox-index.yaml"
    idx.write_text(yaml.dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")

def _append_dir_index(directory: Path, entry: dict) -> None:
    def mutate(data: dict) -> None:
        data.setdefault("messages", []).append(entry)

    _mutate_yaml_index(directory / "inbox-index.yaml", mutate)

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
def mailbox_read_inbox_unread(agent_id: str) -> str:
    """
    Return only unread messages from the agent's inbox index.
    More efficient than mailbox_read_inbox when there are many read messages.
    """
    data = _read_index(agent_id)
    unread = [m for m in data.get("messages", []) if m.get("status") == "unread"]
    return yaml.dump({"messages": unread}, allow_unicode=True)


def _mark_md_read(path: Path) -> None:
    """Update status: unread → status: read in a message file's frontmatter."""
    if not path.exists():
        return
    text = path.read_text(encoding="utf-8")
    updated = re.sub(r"(?m)^status: unread$", "status: read", text, count=1)
    if updated != text:
        path.write_text(updated, encoding="utf-8")


def _mark_index_message_read(idx: Path, filename: str) -> bool:
    was_found = False

    def mutate(data: dict) -> None:
        nonlocal was_found
        for msg in data.get("messages", []):
            if msg.get("file") == filename:
                msg["status"] = "read"
                was_found = True
                return

    _mutate_yaml_index(idx, mutate)
    return was_found


@mcp.tool()
def mailbox_read_message(agent_id: str, filename: str) -> str:
    """Read a specific message from an agent's inbox and automatically mark it as read."""
    path = _inbox_dir(agent_id) / filename
    if not path.exists():
        return f"ERROR: message not found — {filename}"
    content = path.read_text(encoding="utf-8")
    _mark_md_read(path)
    _mark_index_message_read(_inbox_index(agent_id), filename)
    return content


@mcp.tool()
def mailbox_mark_read(agent_id: str, filename: str) -> str:
    """Mark a message as read in the inbox index and in the message file's frontmatter."""
    _mark_md_read(_inbox_dir(agent_id) / filename)
    _mark_index_message_read(_inbox_index(agent_id), filename)
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
    for agent_id in (from_id, to_id):
        error = _validate_agent_id(agent_id)
        if error:
            return f"ERROR: {error}"

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
    error = _validate_agent_id(from_id)
    if error:
        return f"ERROR: {error}"
    error = _validate_subagent_ids(recipients)
    if error:
        return f"ERROR: {error}"

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

    if subject.lower() == "session-end":
        agents_root = Path(os.environ.get("AGENTS_ROOT", ".")).resolve()
        _kill_all_agents(agents_root)

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
    error = _validate_agent_id(from_id)
    if error:
        return f"ERROR: {error}"

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
def mailbox_list_tasks(agent_id: str = "*") -> str:
    """
    List task files from .agents/tasks/.
    Pass agent_id to filter by prefix (e.g. "codex"), or "*" to list all tasks.
    Returns filenames — use mailbox_read_task to open one.
    """
    if agent_id != "*":
        error = _validate_agent_id(agent_id)
        if error:
            return f"ERROR: {error}"

    td = _tasks_dir()
    if not td.exists():
        return f"No tasks directory found at {td}"
    pattern = "*" if agent_id == "*" else f"{agent_id}-*"
    files = sorted(td.glob(pattern))
    if not files:
        label = "any agent" if agent_id == "*" else f"agent '{agent_id}'"
        return f"No tasks found for {label}."
    return "\n".join(f.name for f in files)


@mcp.tool()
def mailbox_read_task(filename: str) -> str:
    """Read a specific task file from .agents/tasks/."""
    path = _tasks_dir() / filename
    if not path.exists():
        return f"ERROR: task not found — {filename}"
    return path.read_text(encoding="utf-8")


@mcp.tool()
def mailbox_set_task_todo(filename: str) -> str:
    """
    Reset a task status back to 'todo' by editing only the frontmatter.
    The file is never renamed or recreated — only the status: field changes.
    Use this when returning a task that cannot be completed yet.
    """
    path = _tasks_dir() / filename
    if not path.exists():
        return f"ERROR: task not found — {filename}"
    content = path.read_text(encoding="utf-8")
    updated = re.sub(r"(?m)^status:.*$", "status: todo", content, count=1)
    if updated == content:
        return f"No change — status field not found in {filename}"
    path.write_text(updated, encoding="utf-8")
    return f"Task reset to todo: {filename}"


def mailbox_update_task_status(current_filename: str, new_status: str) -> str:
    """
    Update a task status by renaming its file and updating the frontmatter.
    new_status must be one of: todo, in-progress, done, blocked.

    NOTE: intentionally not exposed as an MCP tool — only the user manages task state.
    Agents communicate progress via mailbox_send_message, not by editing tasks.
    """
    valid = {"todo", "in-progress", "done", "blocked"}
    if new_status not in valid:
        return f"ERROR: invalid status '{new_status}'. Must be one of: {', '.join(sorted(valid))}"

    path = _tasks_dir() / current_filename
    if not path.exists():
        return f"ERROR: task not found — {current_filename}"

    content = path.read_text(encoding="utf-8")
    content = re.sub(r"^status:.*$", f"status: {new_status}", content, flags=re.MULTILINE)

    stem = path.stem
    parts = stem.rsplit("-", 1)
    if len(parts) == 2 and parts[1] in valid:
        new_stem = f"{parts[0]}-{new_status}"
    else:
        new_stem = f"{stem}-{new_status}"

    new_path = _tasks_dir() / f"{new_stem}.md"
    new_path.write_text(content, encoding="utf-8")
    path.unlink()

    return f"Task updated: {current_filename} → {new_path.name}"


def mailbox_create_task(agent_id: str, task_name: str, objective: str, role: str, criteria: str) -> str:
    """Internal helper — not exposed as MCP tool. Tasks are created by the user, not by agents."""
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
def mailbox_session_status() -> str:
    """
    Show the current status of all agents in the session:
    inbox exists, unread/total messages, and assigned tasks with their statuses.
    """
    agents_root = Path(os.environ.get("AGENTS_ROOT", ".")).resolve()
    md = _mail_dir()
    if not md.exists():
        return "No session found — .agents/mail/ does not exist."

    agents = [d.name for d in sorted(md.iterdir()) if d.is_dir() and d.name not in {"all", "review"}]
    if not agents:
        return "No agent inboxes found."

    td = _tasks_dir()
    lines = []
    for agent_id in agents:
        data = _read_index(agent_id)
        messages = data.get("messages", [])
        unread = sum(1 for m in messages if m.get("status") == "unread")

        lines.append(f"### {agent_id}")
        lines.append(f"  inbox: {len(messages)} message(s), {unread} unread")

        hb_path = agents_root / ".agents" / "pids" / f"{agent_id}.heartbeat"
        if hb_path.exists():
            lines.append(f"  heartbeat: {hb_path.read_text(encoding='utf-8').strip()}")
        else:
            lines.append("  heartbeat: nenhum")

        log_path = agents_root / ".agents" / "logs" / f"{agent_id}.log"
        if log_path.exists():
            size = log_path.stat().st_size
            lines.append(f"  log: {log_path.name} ({size} bytes)")
        else:
            lines.append("  log: nenhum")

        if td.exists():
            tasks = sorted(td.glob(f"{agent_id}-*"))
            if tasks:
                for t in tasks:
                    lines.append(f"  task: {t.name}")
            else:
                lines.append("  tasks: none")
        else:
            lines.append("  tasks: none")

    review_dir = md / "review"
    if review_dir.exists():
        reviews = _read_dir_index(review_dir).get("messages", [])
        unread_reviews = sum(1 for m in reviews if m.get("status") == "unread")
        lines.append(f"\n### review/")
        lines.append(f"  {len(reviews)} message(s), {unread_reviews} unread")

    return "\n".join(lines)


@mcp.tool()
def mailbox_repo_changes(since_minutes: int = 10) -> str:
    """
    Show project files changed in the last N minutes (excluding .agents/).
    Use in the monitoring loop to verify workers are making real progress.
    Falls back to filesystem mtime if git is not available.
    """
    agents_root = Path(os.environ.get("AGENTS_ROOT", ".")).resolve()

    git_check = subprocess.run(
        ["git", "rev-parse", "--is-inside-work-tree"],
        cwd=agents_root, capture_output=True, text=True,
    )
    if git_check.returncode == 0:
        status = subprocess.run(
            ["git", "status", "--short"],
            cwd=agents_root, capture_output=True, text=True,
        )
        commits = subprocess.run(
            ["git", "log", f"--since={since_minutes} minutes ago", "--oneline"],
            cwd=agents_root, capture_output=True, text=True,
        )
        changed_files = subprocess.run(
            ["git", "log", f"--since={since_minutes} minutes ago", "--name-only", "--format="],
            cwd=agents_root, capture_output=True, text=True,
        )
        parts = [
            f"## Working tree status\n{status.stdout.strip() or 'clean'}",
            f"\n## Commits in last {since_minutes}min\n{commits.stdout.strip() or 'none'}",
        ]
        if changed_files.stdout.strip():
            parts.append(f"\n## Files changed in commits\n{changed_files.stdout.strip()}")
        return "\n".join(parts)

    # Fallback: filesystem mtime
    cutoff = time.time() - (since_minutes * 60)
    changed = []
    for root_dir, dirs, files in os.walk(agents_root):
        dirs[:] = [d for d in dirs if d not in {".agents", ".git", "__pycache__", "node_modules"}]
        for f in files:
            p = Path(root_dir) / f
            try:
                if p.stat().st_mtime > cutoff:
                    changed.append(str(p.relative_to(agents_root)))
            except OSError:
                pass
    if changed:
        return "\n".join(sorted(changed))
    return f"No files changed in the last {since_minutes} minutes."


@mcp.tool()
def mailbox_read_rules() -> str:
    """Read .agents/mail/mailbox-rules.md (the condensed rules file for agents)."""
    path = _mail_dir() / "mailbox-rules.md"
    if not path.exists():
        return "mailbox-rules.md not found. Create it at .agents/mail/mailbox-rules.md"
    return path.read_text(encoding="utf-8")


@mcp.tool()
def mailbox_init_session(leader_id: str, agent_ids: List[str]) -> str:
    """
    Initialize a multi-agent session: create inboxes for all participants,
    ensure review/ and all/ directories exist, and write mailbox-rules.md.

    leader_id: the agent acting as leader/orchestrator
    agent_ids: list of all other agent IDs (workers + QA), using the format
    'agentname-role' (for example: 'claude-backend-senior')
    """
    error = _validate_agent_id(leader_id)
    if error:
        return f"ERROR: {error}"
    error = _validate_subagent_ids(agent_ids)
    if error:
        return f"ERROR: {error}"

    all_ids = [leader_id] + [a for a in agent_ids if a != leader_id]
    for aid in all_ids:
        _ensure_inbox(aid)

    for special in ("review", "all"):
        d = _mail_dir() / special
        d.mkdir(parents=True, exist_ok=True)
        idx = d / "inbox-index.yaml"
        if not idx.exists():
            idx.write_text("messages: []\n", encoding="utf-8")

    agents_list = "\n".join(f"- `{aid}`" for aid in all_ids)
    rules_content = (
        "# Mailbox Rules — Session Protocol\n\n"
        f"## Agentes nesta sessão\n{agents_list}\n\n"
        f"## Líder\n`{leader_id}` — responsável por ler a task, delegar e fazer a revisão final.\n\n"
        "## Protocolo\n\n"
        "1. O líder lê a task com `mailbox_list_tasks` / `mailbox_read_task` e determina o cargo de cada worker.\n"
        "2. Todo subagent deve usar ID no formato `agentname-role` (ex: `claude-backend-senior`) e seu inbox deve refletir exatamente esse nome.\n"
        "3. O líder delega trabalho **exclusivamente via `mailbox_send_message`** — nunca criando ou editando arquivos em `.agents/tasks/`.\n"
        "4. Workers aguardam com `mailbox_watch_inbox` e executam conforme cargo definido pelo líder.\n"
        "5. Workers enviam resultado ao QA via `mailbox_send_message`.\n"
        "6. O QA consolida, posta em `review/` via `mailbox_create_review` e notifica o líder.\n"
        "7. O líder lê o review do QA, faz revisão final e posta relatório consolidado em `review/`.\n\n"
        "## Pastas\n"
        "- `.agents/mail/{agent_id}/` — inbox de cada agente\n"
        "- `.agents/mail/review/` — fila de revisão (lida pelo usuário ao final)\n"
        "- `.agents/mail/all/` — broadcasts\n"
        "- `.agents/tasks/` — tasks criadas pelo líder\n"
    )
    (_mail_dir() / "mailbox-rules.md").write_text(rules_content, encoding="utf-8")

    return f"Session initialized. Leader: {leader_id}. Agents: {', '.join(all_ids)}."


@mcp.tool()
async def mailbox_watch_inbox(agent_id: str, poll_interval: int = 5, timeout: int = 300) -> str:
    """
    Block until an unread message arrives in the agent's inbox.
    If the inbox directory does not exist yet, waits for it to be created first.

    poll_interval: seconds between checks (default 5)
    timeout:       max seconds to wait before returning a timeout error (default 300)

    Returns the filename and basic metadata of the first unread message found.
    """
    error = _validate_agent_id(agent_id)
    if error:
        return f"ERROR: {error}"

    deadline = time.time() + timeout
    while time.time() < deadline:
        inbox = _inbox_index(agent_id)
        if inbox.exists():
            data = yaml.safe_load(inbox.read_text(encoding="utf-8")) or {"messages": []}
            unread = [m for m in data.get("messages", []) if m.get("status") == "unread"]
            if unread:
                first = unread[0]
                result = (
                    f"New message: {first['file']} "
                    f"(from: {first.get('from')}, subject: {first.get('subject')})"
                )
                if len(unread) > 1:
                    extras = "\n".join(
                        f"  + {m['file']} (from: {m.get('from')}, subject: {m.get('subject')})"
                        for m in unread[1:]
                    )
                    result += f"\n\nAlso unread ({len(unread) - 1} more):\n{extras}"
                return result
        await asyncio.sleep(poll_interval)
    return f"TIMEOUT: no unread messages for '{agent_id}' after {timeout}s."


@mcp.tool()
def mailbox_heartbeat(agent_id: str, note: str = "") -> str:
    """
    Signal that an agent is alive and working.
    Call when starting a task and when finishing.
    The leader reads this via mailbox_session_status to distinguish
    a working agent from a crashed one.
    """
    error = _validate_agent_id(agent_id)
    if error:
        return f"ERROR: {error}"

    agents_root = Path(os.environ.get("AGENTS_ROOT", ".")).resolve()
    pids_dir = agents_root / ".agents" / "pids"
    pids_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    hb = pids_dir / f"{agent_id}.heartbeat"
    new_entry = f"{ts} — {note}"
    existing = hb.read_text(encoding="utf-8").splitlines() if hb.exists() else []
    entries = ([new_entry] + existing)[:5]
    hb.write_text("\n".join(entries) + "\n", encoding="utf-8")
    return f"Heartbeat registered: {agent_id} at {ts}."


def _agent_pid_path(agents_root: Path, agent_id: str) -> Path:
    return agents_root / ".agents" / "logs" / f"{agent_id}.pid"


def _create_temp_file(suffix: str, content: str, mode: int | None = None) -> Path:
    fd, path_str = tempfile.mkstemp(suffix=suffix)
    path = Path(path_str)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(content)
    if mode is not None:
        path.chmod(mode)
    return path


@mcp.tool()
async def mailbox_spawn_agents(agents: List[dict]) -> str:
    """
    Spawn worker and QA agents as background processes (no terminal window needed).
    The leader calls this right after mailbox_init_session.

    Each item in `agents`:
      agent_id  — ID of the agent in this session using 'agentname-role'
                  (e.g. "gemini-qa")
      cli       — CLI to use: "claude" | "gemini" | "codex"
      role      — "worker" | "qa"
      context   — session context string (leader_id, qa_id, worker_count, AGENTS_ROOT, etc.)

    Example:
      mailbox_spawn_agents(agents=[
        {"agent_id": "claude-backend-senior", "cli": "claude", "role": "worker",
         "context": "Líder: codex. QA: gemini-qa. AGENTS_ROOT: /path/to/project."},
        {"agent_id": "gemini-qa", "cli": "gemini", "role": "qa",
         "context": "Líder: codex. Workers: [claude-backend-senior]. AGENTS_ROOT: /path/to/project."},
      ])
    """
    import json as _json

    _CLI_CMD = {
        "claude": "claude -p --dangerously-skip-permissions",
        "gemini": "gemini --model auto --approval-mode yolo --prompt",
        "codex":  "codex -a never",
    }

    skills_dir = Path(__file__).parent / "skills"
    agents_root = Path(os.environ.get("AGENTS_ROOT", ".")).resolve()
    # sys.argv[0] is the running binary — works regardless of PATH or install method.
    _mcp_bin = sys.argv[0] if sys.argv else (shutil.which("mcp-master-of-puppets") or "mcp-master-of-puppets")
    mcp_server_cfg = {
        "command": _mcp_bin,
        "args": [],
        "env": {"AGENTS_ROOT": str(agents_root)},
    }

    # Claude: project-level .mcp.json (always rewrite to keep binary path current)
    mcp_json_path = agents_root / ".mcp.json"
    mcp_json_path.write_text(
        _json.dumps({"mcpServers": {"mail-agents": mcp_server_cfg}}, indent=2),
        encoding="utf-8",
    )

    # Gemini: project-level .gemini/settings.json (always rewrite to keep AGENTS_ROOT current)
    gemini_dir = agents_root / ".gemini"
    gemini_dir.mkdir(parents=True, exist_ok=True)
    (gemini_dir / "settings.json").write_text(
        _json.dumps({"mcpServers": {"mail-agents": mcp_server_cfg}}, indent=2),
        encoding="utf-8",
    )

    # Log directory for all subagents
    log_dir = agents_root / ".agents" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)

    spawned = []

    for spec in agents:
        agent_id = spec["agent_id"]
        error = _validate_agent_id(agent_id, require_role=True)
        if error:
            return f"ERROR: {error}"
        role = spec.get("role", "worker")
        context = spec.get("context", "")

        role_file = skills_dir / f"{role}.md"
        if not role_file.exists():
            return f"ERROR: skill file not found — {role_file}"

        role_prompt = role_file.read_text(encoding="utf-8")
        # Parse frontmatter for skill defaults (recommended_cli, recommended_model).
        skill_meta: dict = {}
        if role_prompt.startswith("---\n"):
            end = role_prompt.find("\n---\n", 4)
            if end != -1:
                for _line in role_prompt[4:end].splitlines():
                    if ": " in _line:
                        _k, _, _v = _line.partition(": ")
                        skill_meta[_k.strip()] = _v.strip()
                # Strip frontmatter so the prompt never starts with '---',
                # which yargs (Gemini CLI) misparses as the end-of-options marker.
                role_prompt = role_prompt[end + 5:]

        # cli: explicit spec > skill frontmatter > "claude"
        cli_type = spec.get("cli", skill_meta.get("recommended_cli", "claude")).lower()
        # model: explicit spec > skill frontmatter (only when cli matches recommended_cli)
        _rec_model = skill_meta.get("recommended_model")
        _rec_cli = skill_meta.get("recommended_cli", "claude")
        model = spec.get("model") or (_rec_model if cli_type == _rec_cli else None)
        role_slug = role.replace("/", "-")
        skill_description = skill_meta.get("description", role_slug)
        context_block = (
            "## Contexto da Sessão\n\n"
            f"{context}\n\n"
            "**IMPORTANTE:** o contexto acima contém apenas metadados da sessão (quem é o líder, "
            "quem é o QA, onde está o projeto). Ele NÃO contém sua tarefa e NÃO autoriza nenhuma "
            "ação técnica. Sua tarefa chegará exclusivamente via inbox."
        )
        kickoff = (
            f"Inicie agora chamando `mailbox_watch_inbox` com "
            f"`agent_id=\"{agent_id}\"` e `timeout=600`."
        )

        log_file = log_dir / f"{agent_id}.log"
        pid_file = log_dir / f"{agent_id}.pid"

        # Inner script: exec replaces bash so the PID captured by the launcher
        # ($!) remains valid for the entire lifetime of the CLI process.
        if cli_type == "claude":
            if not shutil.which("claude"):
                return f"ERROR: CLI 'claude' não encontrado no PATH para agente '{agent_id}'"
            # Write skill to .claude/skills/ — auto-loaded by Claude Code per project
            claude_skills_dir = agents_root / ".claude" / "skills"
            claude_skills_dir.mkdir(parents=True, exist_ok=True)
            (claude_skills_dir / f"{agent_id}.md").write_text(role_prompt, encoding="utf-8")
            # Session context goes in --append-system-prompt; -p carries only the kickoff
            tmp_context = _create_temp_file(
                suffix=f"-{agent_id}-context.txt", content=context_block
            )
            tmp_prompt = _create_temp_file(
                suffix=f"-{agent_id}-prompt.txt", content=kickoff
            )
            _model_flag = f"--model {model} " if model else ""
            inner_script = (
                f"#!/bin/bash\n"
                f"export AGENTS_ROOT='{agents_root}'\n"
                f"cd '{agents_root}'\n"
                f"CONTEXT=$(cat '{tmp_context}')\n"
                f"if command -v stdbuf >/dev/null 2>&1; then\n"
                f"    exec stdbuf -oL -eL claude -p --dangerously-skip-permissions {_model_flag}"
                f"--append-system-prompt \"$CONTEXT\" --mcp-config '{mcp_json_path}' -- "
                f"\"$(cat '{tmp_prompt}')\"\n"
                f"else\n"
                f"    exec claude -p --dangerously-skip-permissions {_model_flag}"
                f"--append-system-prompt \"$CONTEXT\" --mcp-config '{mcp_json_path}' -- "
                f"\"$(cat '{tmp_prompt}')\"\n"
                f"fi\n"
            )

        elif cli_type == "gemini":
            if not shutil.which("gemini"):
                return f"ERROR: CLI 'gemini' não encontrado no PATH para agente '{agent_id}'"
            # Write skill to .agents/skills/{role_slug}/ — auto-loaded by Gemini CLI
            skill_dir = agents_root / ".agents" / "skills" / role_slug
            skill_dir.mkdir(parents=True, exist_ok=True)
            (skill_dir / "SKILL.md").write_text(
                f"---\nname: {role_slug}\ndescription: {skill_description}\n---\n\n{role_prompt}",
                encoding="utf-8",
            )
            tmp_prompt = _create_temp_file(
                suffix=f"-{agent_id}-prompt.txt", content=f"{context_block}\n\n{kickoff}"
            )
            _gemini_model = model or "auto"
            inner_script = (
                f"#!/bin/bash\n"
                f"export AGENTS_ROOT='{agents_root}'\n"
                f"cd '{agents_root}'\n"
                f"if command -v stdbuf >/dev/null 2>&1; then\n"
                f"    exec stdbuf -oL -eL gemini --model {_gemini_model} --approval-mode yolo "
                f"--prompt \"$(cat '{tmp_prompt}')\"\n"
                f"else\n"
                f"    exec gemini --model {_gemini_model} --approval-mode yolo "
                f"--prompt \"$(cat '{tmp_prompt}')\"\n"
                f"fi\n"
            )

        elif cli_type == "codex":
            if not shutil.which("codex"):
                return f"ERROR: CLI 'codex' não encontrado no PATH para agente '{agent_id}'"
            # Write skill to .agents/skills/{role_slug}/ — auto-loaded by Codex CLI
            skill_dir = agents_root / ".agents" / "skills" / role_slug
            skill_dir.mkdir(parents=True, exist_ok=True)
            (skill_dir / "SKILL.md").write_text(
                f"---\nname: {role_slug}\ndescription: {skill_description}\n---\n\n{role_prompt}",
                encoding="utf-8",
            )
            tmp_prompt = _create_temp_file(
                suffix=f"-{agent_id}-prompt.txt", content=f"{context_block}\n\n{kickoff}"
            )
            _effort = spec.get("effort") or skill_meta.get("alternative_effort")
            _effort_flag = f"-c 'model_reasoning_effort=\"{_effort}\"' " if _effort else ""
            _model_flag = f"--model {model} " if model else ""
            inner_script = (
                f"#!/bin/bash\n"
                f"export AGENTS_ROOT='{agents_root}'\n"
                f"cd '{agents_root}'\n"
                f"if command -v stdbuf >/dev/null 2>&1; then\n"
                f"    exec stdbuf -oL -eL codex -a never {_model_flag}{_effort_flag}"
                f"-c 'mcp_servers.mail-agents.env.AGENTS_ROOT={agents_root}' "
                f"\"$(cat '{tmp_prompt}')\"\n"
                f"else\n"
                f"    exec codex -a never {_model_flag}{_effort_flag}"
                f"-c 'mcp_servers.mail-agents.env.AGENTS_ROOT={agents_root}' "
                f"\"$(cat '{tmp_prompt}')\"\n"
                f"fi\n"
            )

        else:
            fallback_cmd = _CLI_CMD.get(cli_type, cli_type)
            cli_binary = fallback_cmd.split()[0]
            if not shutil.which(cli_binary):
                return f"ERROR: CLI '{cli_binary}' não encontrado no PATH para agente '{agent_id}'"
            tmp_prompt = _create_temp_file(
                suffix=f"-{agent_id}-prompt.txt",
                content=f"{role_prompt}\n\n---\n\n{context_block}\n\n{kickoff}",
            )
            inner_script = (
                f"#!/bin/bash\n"
                f"export AGENTS_ROOT='{agents_root}'\n"
                f"cd '{agents_root}'\n"
                f"if command -v stdbuf >/dev/null 2>&1; then\n"
                f"    exec stdbuf -oL -eL {fallback_cmd} \"$(cat '{tmp_prompt}')\"\n"
                f"else\n"
                f"    exec {fallback_cmd} \"$(cat '{tmp_prompt}')\"\n"
                f"fi\n"
            )
        inner = _create_temp_file(
            suffix=f"-{agent_id}-inner.sh",
            content=inner_script,
            mode=0o755,
        )

        # Launcher: PID written by bash (not Python) — only used by mailbox_kill_agents.
        launcher_script = (
            f"#!/bin/bash\n"
            f"bash '{inner}' >> '{log_file}' 2>&1 &\n"
            f"echo $! > '{pid_file}'\n"
            f"wait $!\n"
            f"rm -f '{pid_file}'\n"
        )

        launcher = _create_temp_file(
            suffix=f"-{agent_id}-launch.sh",
            content=launcher_script,
            mode=0o755,
        )

        # asyncio subprocess avoids fork() inside the event loop, which would
        # corrupt the FastMCP transport. nohup fully detaches the agent process.
        proc = await asyncio.create_subprocess_shell(
            f"nohup bash '{launcher}' >/dev/null 2>&1 &",
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await proc.wait()
        spawned.append(agent_id)

    # Liveness check: give agents 10s to start and produce output.
    await asyncio.sleep(10)
    alive, silent = [], []
    for agent_id in spawned:
        lf = log_dir / f"{agent_id}.log"
        if lf.exists() and lf.stat().st_size > 0:
            alive.append(agent_id)
        else:
            silent.append(agent_id)

    result = f"Spawned {len(spawned)} agent(s): {', '.join(spawned)}."
    if alive:
        result += f"\nAlive after 10s: {', '.join(alive)}."
    if silent:
        result += (
            f"\nWARNING — no log output after 10s: {', '.join(silent)}."
            f" Check .agents/logs/ for details."
        )
    return result


def _kill_all_agents(agents_root: Path) -> None:
    """SIGTERM all spawned agents and remove their PID files."""
    import signal
    log_dir = agents_root / ".agents" / "logs"
    if not log_dir.exists():
        return
    for pid_file in log_dir.glob("*.pid"):
        try:
            pid = int(pid_file.read_text().strip())
            os.killpg(os.getpgid(pid), signal.SIGTERM)
        except Exception:
            pass
        pid_file.unlink(missing_ok=True)


def _cleanup_dead_pids(agents_root: Path) -> None:
    """Remove PID files for processes that are no longer running."""
    log_dir = agents_root / ".agents" / "logs"
    if not log_dir.exists():
        return
    for pid_file in log_dir.glob("*.pid"):
        try:
            pid = int(pid_file.read_text().strip())
            os.kill(pid, 0)  # signal 0 = existence check only
        except (ProcessLookupError, PermissionError):
            pid_file.unlink(missing_ok=True)
        except Exception:
            pass


@mcp.tool()
def mailbox_kill_agents(agent_ids: List[str]) -> str:
    """
    Terminate spawned agents by killing their saved PIDs.
    Call this when ending the session abruptly (without session-end broadcast)
    or to force-stop agents that are not responding.

    Args:
      agent_ids — list of agent IDs to kill (e.g. ["claude", "gemini"])
                  pass ["*"] to kill all agents that have a saved PID
    """
    import signal

    agents_root = Path(os.environ.get("AGENTS_ROOT", ".")).resolve()
    _cleanup_dead_pids(agents_root)
    log_dir = agents_root / ".agents" / "logs"

    if not log_dir.exists():
        return "No .agents/logs/ directory found — no agents were spawned."

    if agent_ids == ["*"]:
        agent_ids = [p.stem for p in log_dir.glob("*.pid")]

    results = []
    for agent_id in agent_ids:
        error = _validate_agent_id(agent_id)
        if error:
            results.append(f"{agent_id}: error — {error}")
            continue
        pid_path = _agent_pid_path(agents_root, agent_id)
        if not pid_path.exists():
            results.append(f"{agent_id}: no PID file found")
            continue
        try:
            pid = int(pid_path.read_text().strip())
            os.killpg(os.getpgid(pid), signal.SIGTERM)
            pid_path.unlink(missing_ok=True)
            results.append(f"{agent_id}: killed (PID {pid})")
        except ProcessLookupError:
            pid_path.unlink(missing_ok=True)
            results.append(f"{agent_id}: already exited")
        except Exception as e:
            results.append(f"{agent_id}: error — {e}")

    return "\n".join(results)


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
