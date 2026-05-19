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
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import List

import yaml
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("AI Mailbox")

_ANSI_RE = re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])')


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
    idx = _inbox_index(agent_id)
    if not idx.exists():
        return {"messages": []}
    raw = idx.read_text(encoding="utf-8")
    return yaml.safe_load(raw) or {"messages": []}

def _write_index(agent_id: str, data: dict) -> None:
    _inbox_index(agent_id).write_text(
        yaml.dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )

def _append_to_index(agent_id: str, entry: dict) -> None:
    idx = _inbox_index(agent_id)
    with open(idx, "a+", encoding="utf-8") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        fh.seek(0)
        data = yaml.safe_load(fh.read()) or {"messages": []}
        data.setdefault("messages", []).append(entry)
        fh.seek(0)
        fh.truncate()
        fh.write(yaml.dump(data, allow_unicode=True, sort_keys=False))

def _read_dir_index(directory: Path) -> dict:
    idx = directory / "inbox-index.yaml"
    if not idx.exists():
        return {"messages": []}
    return yaml.safe_load(idx.read_text(encoding="utf-8")) or {"messages": []}

def _write_dir_index(directory: Path, data: dict) -> None:
    idx = directory / "inbox-index.yaml"
    idx.write_text(yaml.dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")

def _append_dir_index(directory: Path, entry: dict) -> None:
    idx = directory / "inbox-index.yaml"
    with open(idx, "a+", encoding="utf-8") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        fh.seek(0)
        data = yaml.safe_load(fh.read()) or {"messages": []}
        data.setdefault("messages", []).append(entry)
        fh.seek(0)
        fh.truncate()
        fh.write(yaml.dump(data, allow_unicode=True, sort_keys=False))

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


def _mark_md_read(path: Path) -> None:
    """Update status: unread → status: read in a message file's frontmatter."""
    if not path.exists():
        return
    text = path.read_text(encoding="utf-8")
    updated = re.sub(r"(?m)^status: unread$", "status: read", text, count=1)
    if updated != text:
        path.write_text(updated, encoding="utf-8")


@mcp.tool()
def mailbox_read_message(agent_id: str, filename: str) -> str:
    """Read a specific message from an agent's inbox and automatically mark it as read."""
    path = _inbox_dir(agent_id) / filename
    if not path.exists():
        return f"ERROR: message not found — {filename}"
    content = path.read_text(encoding="utf-8")
    _mark_md_read(path)
    data = _read_index(agent_id)
    for msg in data.get("messages", []):
        if msg.get("file") == filename:
            msg["status"] = "read"
            break
    _write_index(agent_id, data)
    return content


@mcp.tool()
def mailbox_mark_read(agent_id: str, filename: str) -> str:
    """Mark a message as read in the inbox index and in the message file's frontmatter."""
    _mark_md_read(_inbox_dir(agent_id) / filename)
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
    _cleanup_dead_pids(agents_root)
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
def mailbox_write_snapshot(note: str = "") -> str:
    """
    Append a timestamped snapshot of the current session status to
    .agents/session-log.md in the project root.

    Call this periodically during monitoring to build an audit trail of
    the session progress that persists in the repository.

    note: optional free-text annotation added to this snapshot entry
    """
    status = mailbox_session_status()
    timestamp = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    log_path = _mail_dir() / "session-log.md"
    log_path.parent.mkdir(parents=True, exist_ok=True)

    entry_lines = [f"\n## Snapshot — {timestamp}"]
    if note:
        entry_lines.append(f"\n> {note}\n")
    entry_lines.append(f"\n{status}\n")
    entry_lines.append("---")

    with log_path.open("a", encoding="utf-8") as f:
        f.write("\n".join(entry_lines) + "\n")

    return f"Snapshot written to {log_path} at {timestamp}."


@mcp.tool()
def mailbox_read_rules() -> str:
    """Read .agents/mail/mailbox-rules.md (the condensed rules file for agents)."""
    path = _mail_dir() / "mailbox-rules.md"
    if not path.exists():
        return "mailbox-rules.md not found. Create it at .agents/mail/mailbox-rules.md"
    return path.read_text(encoding="utf-8")


@mcp.tool()
def mailbox_log_tokens(agent_id: str, tokens_in: int, tokens_out: int) -> str:
    """
    Log token usage for an agent. Call this whenever completing a significant task step.
    tokens_in:  input/prompt tokens consumed
    tokens_out: output/completion tokens generated
    """
    log_path = _mail_dir() / "token-log.yaml"
    data: dict = {"entries": []}
    if log_path.exists():
        data = yaml.safe_load(log_path.read_text(encoding="utf-8")) or data

    entry = {
        "agent_id": agent_id,
        "timestamp": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "total": tokens_in + tokens_out,
    }
    data["entries"].append(entry)
    log_path.write_text(yaml.dump(data, allow_unicode=True), encoding="utf-8")
    return f"Logged: {agent_id} — in={tokens_in} out={tokens_out} total={tokens_in + tokens_out}"


@mcp.tool()
def mailbox_token_stats() -> str:
    """
    Return a structured token usage report for the current session.
    Includes per-agent totals, averages, and session-wide highlights.
    """
    log_path = _mail_dir() / "token-log.yaml"
    if not log_path.exists():
        return "No token data recorded yet."

    data = yaml.safe_load(log_path.read_text(encoding="utf-8")) or {}
    entries = data.get("entries", [])
    if not entries:
        return "No token data recorded yet."

    from collections import defaultdict
    agents: dict = defaultdict(lambda: {"calls": 0, "tokens_in": 0, "tokens_out": 0, "total": 0, "peak": 0})

    for e in entries:
        aid = e["agent_id"]
        agents[aid]["calls"] += 1
        agents[aid]["tokens_in"] += e["tokens_in"]
        agents[aid]["tokens_out"] += e["tokens_out"]
        agents[aid]["total"] += e["total"]
        agents[aid]["peak"] = max(agents[aid]["peak"], e["total"])

    lines = ["## Token Usage Report", ""]
    grand_total = 0
    highest_agent = max(agents.items(), key=lambda x: x[1]["total"])

    for aid, s in sorted(agents.items(), key=lambda x: x[1]["total"], reverse=True):
        avg = s["total"] // s["calls"] if s["calls"] else 0
        lines.append(f"### {aid}")
        lines.append(f"  calls   : {s['calls']}")
        lines.append(f"  in      : {s['tokens_in']:,}")
        lines.append(f"  out     : {s['tokens_out']:,}")
        lines.append(f"  total   : {s['total']:,}")
        lines.append(f"  avg/call: {avg:,}")
        lines.append(f"  peak    : {s['peak']:,}")
        lines.append("")
        grand_total += s["total"]

    lines.append("---")
    lines.append(f"grand total : {grand_total:,}")
    lines.append(f"highest agent: {highest_agent[0]} ({highest_agent[1]['total']:,} tokens)")

    return "\n".join(lines)


@mcp.tool()
def mailbox_init_session(leader_id: str, agent_ids: List[str]) -> str:
    """
    Initialize a multi-agent session: create inboxes for all participants,
    ensure review/ and all/ directories exist, and write mailbox-rules.md.

    leader_id: the agent acting as leader/orchestrator
    agent_ids: list of all other agent IDs (workers + QA)
    """
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
        "2. O líder delega trabalho **exclusivamente via `mailbox_send_message`** — nunca criando ou editando arquivos em `.agents/tasks/`.\n"
        "3. Workers aguardam com `mailbox_watch_inbox` e executam conforme cargo definido pelo líder.\n"
        "4. Workers enviam resultado ao QA via `mailbox_send_message`.\n"
        "5. O QA consolida, posta em `review/` via `mailbox_create_review` e notifica o líder.\n"
        "6. O líder lê o review do QA, faz revisão final e posta relatório consolidado em `review/`.\n\n"
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
    deadline = time.time() + timeout
    while time.time() < deadline:
        inbox = _inbox_index(agent_id)
        if inbox.exists():
            data = yaml.safe_load(inbox.read_text(encoding="utf-8")) or {"messages": []}
            for msg in data.get("messages", []):
                if msg.get("status") == "unread":
                    return (
                        f"New message: {msg['file']} "
                        f"(from: {msg.get('from')}, subject: {msg.get('subject')})"
                    )
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
    agents_root = Path(os.environ.get("AGENTS_ROOT", ".")).resolve()
    pids_dir = agents_root / ".agents" / "pids"
    pids_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    hb = pids_dir / f"{agent_id}.heartbeat"
    hb.write_text(f"{ts} — {note}\n", encoding="utf-8")
    return f"Heartbeat registered: {agent_id} at {ts}."


def _agent_log_path(agents_root: Path, agent_id: str) -> Path:
    return agents_root / ".agents" / "logs" / f"{agent_id}.log"


def _agent_pid_path(agents_root: Path, agent_id: str) -> Path:
    return agents_root / ".agents" / "pids" / f"{agent_id}.pid"


@mcp.tool()
async def mailbox_spawn_agents(agents: List[dict]) -> str:
    """
    Spawn worker and QA agents as background processes (no terminal window needed).
    The leader calls this right after mailbox_init_session.

    Each item in `agents`:
      agent_id  — ID of the agent in this session (e.g. "gemini")
      cli       — CLI to use: "claude" | "gemini" | "codex"
      role      — "worker" | "qa"
      context   — session context string (leader_id, qa_id, worker_count, AGENTS_ROOT, etc.)

    Logs are written to .agents/logs/<agent_id>.log — read with mailbox_read_agent_log.

    Example:
      mailbox_spawn_agents(agents=[
        {"agent_id": "claude", "cli": "claude", "role": "worker",
         "context": "Líder: codex. QA: gemini. AGENTS_ROOT: /path/to/project."},
        {"agent_id": "gemini", "cli": "gemini", "role": "qa",
         "context": "Líder: codex. Workers: [claude]. AGENTS_ROOT: /path/to/project."},
      ])
    """
    _CLI_CMD = {
        "claude": "claude -p --dangerously-skip-permissions",
        "gemini": "gemini --model auto --approval-mode yolo --prompt",
        "codex":  "codex",
    }

    prompts_dir = Path(__file__).parent / "prompts"
    agents_root = Path(os.environ.get("AGENTS_ROOT", ".")).resolve()
    logs_dir = agents_root / ".agents" / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)

    # Ensure .mcp.json exists in the project root so claude subagents load the
    # mail-agents MCP server when spawned with `claude -p` (non-interactive mode
    # does not read ~/.claude/mcp.json, only the project-level .mcp.json).
    mcp_json_path = agents_root / ".mcp.json"
    if not mcp_json_path.exists():
        import json as _json
        mcp_json_path.write_text(
            _json.dumps({
                "mcpServers": {
                    "mail-agents": {
                        "command": "uvx",
                        "args": ["mcp-master-of-puppets"],
                        "env": {"AGENTS_ROOT": str(agents_root)},
                    }
                }
            }, indent=2),
            encoding="utf-8",
        )

    spawned = []

    for spec in agents:
        agent_id = spec["agent_id"]
        cli_type = spec.get("cli", "claude").lower()
        role = spec.get("role", "worker")
        context = spec.get("context", "")

        role_file = prompts_dir / f"{role}.md"
        if not role_file.exists():
            return f"ERROR: prompt file not found — {role_file}"

        role_prompt = role_file.read_text(encoding="utf-8")
        full_prompt = (
            f"{role_prompt}\n\n"
            "---\n\n"
            "## Contexto da Sessão\n\n"
            f"{context}\n\n"
            f"Inicie agora chamando `mailbox_watch_inbox` com `agent_id=\"{agent_id}\"` e `timeout=600`."
        )

        tmp_prompt = Path(tempfile.mktemp(suffix=f"-{agent_id}-prompt.txt"))
        tmp_prompt.write_text(full_prompt, encoding="utf-8")

        log_path = _agent_log_path(agents_root, agent_id)
        log_path.write_text(f"[agent {agent_id} started at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}]\n")

        pid_path = _agent_pid_path(agents_root, agent_id)
        pid_path.parent.mkdir(parents=True, exist_ok=True)

        cli_cmd = _CLI_CMD.get(cli_type, cli_type)

        cli_binary = cli_cmd.split()[0]
        if not shutil.which(cli_binary):
            return f"ERROR: CLI '{cli_binary}' não encontrado no PATH para agente '{agent_id}'"

        # Inner script: exec replaces bash so the PID captured by the launcher
        # ($!) remains valid for the entire lifetime of the CLI process.
        # stdbuf -oL forces line-buffered stdout without a PTY, keeping logs
        # clean and up-to-date for all CLI types (claude, gemini, codex).
        inner_script = (
            f"#!/bin/bash\n"
            f"export AGENTS_ROOT='{agents_root}'\n"
            f"cd '{agents_root}'\n"
            f"if command -v stdbuf >/dev/null 2>&1; then\n"
            f"    exec stdbuf -oL -eL {cli_cmd} \"$(cat '{tmp_prompt}')\"\n"
            f"else\n"
            f"    exec {cli_cmd} \"$(cat '{tmp_prompt}')\"\n"
            f"fi\n"
        )
        inner = Path(tempfile.mktemp(suffix=f"-{agent_id}-inner.sh"))
        inner.write_text(inner_script, encoding="utf-8")
        inner.chmod(0o755)

        # Launcher backgrounds inner and saves $! immediately — that PID
        # belongs to the exec chain (bash → stdbuf → cli) so it stays valid.
        launcher_script = (
            f"#!/bin/bash\n"
            f"bash '{inner}' >> '{log_path}' 2>&1 &\n"
            f"echo $! > '{pid_path}'\n"
            f"wait $!\n"
            f"echo \"[agent {agent_id} exited at $(date)]\" >> '{log_path}'\n"
            f"rm -f '{pid_path}'\n"
            f"rmdir --ignore-fail-on-non-empty '{pid_path.parent}' 2>/dev/null || true\n"
        )

        launcher = Path(tempfile.mktemp(suffix=f"-{agent_id}-launch.sh"))
        launcher.write_text(launcher_script, encoding="utf-8")
        launcher.chmod(0o755)

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

    return (
        f"Spawned {len(spawned)} agent(s): {', '.join(spawned)}. "
        f"Logs in {logs_dir}/ — use mailbox_read_agent_log to monitor."
    )


@mcp.tool()
def mailbox_read_agent_log(agent_id: str, tail: int = 50) -> str:
    """
    Read the last N lines of a spawned agent's log.
    Call this periodically to monitor agent progress without needing terminal access.

    Args:
      agent_id — the agent whose log to read (e.g. "claude", "gemini")
      tail     — number of lines to return from the end (default 50)
    """
    agents_root = Path(os.environ.get("AGENTS_ROOT", ".")).resolve()
    log_path = _agent_log_path(agents_root, agent_id)

    if not log_path.exists():
        return f"No log found for agent '{agent_id}'. Has it been spawned?"

    content = log_path.read_text(encoding="utf-8", errors="replace")
    lines = [_ANSI_RE.sub('', l).replace('\r', '') for l in content.splitlines()]
    lines = [l for l in lines if l.strip()]
    excerpt = lines[-tail:] if len(lines) > tail else lines
    return "\n".join(excerpt) or "(log vazio)"


def _kill_all_agents(agents_root: Path) -> None:
    """SIGTERM all spawned agents and remove their PID files."""
    import signal
    pids_dir = agents_root / ".agents" / "pids"
    if not pids_dir.exists():
        return
    for pid_file in pids_dir.glob("*.pid"):
        try:
            pid = int(pid_file.read_text().strip())
            os.killpg(os.getpgid(pid), signal.SIGTERM)
        except Exception:
            pass
        pid_file.unlink(missing_ok=True)
    try:
        if pids_dir.exists() and not any(pids_dir.iterdir()):
            pids_dir.rmdir()
    except Exception:
        pass


def _cleanup_dead_pids(agents_root: Path) -> None:
    """Remove PID files for processes that are no longer running."""
    pids_dir = agents_root / ".agents" / "pids"
    if not pids_dir.exists():
        return
    for pid_file in pids_dir.glob("*.pid"):
        try:
            pid = int(pid_file.read_text().strip())
            os.kill(pid, 0)  # signal 0 = existence check only
        except (ProcessLookupError, PermissionError):
            pid_file.unlink(missing_ok=True)
        except Exception:
            pass
    try:
        if not any(pids_dir.iterdir()):
            pids_dir.rmdir()
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
    pids_dir = agents_root / ".agents" / "pids"

    if not pids_dir.exists():
        return "No .agents/pids/ directory found — no agents were spawned."

    if agent_ids == ["*"]:
        agent_ids = [p.stem for p in pids_dir.glob("*.pid")]

    results = []
    for agent_id in agent_ids:
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

    try:
        if pids_dir.exists() and not any(pids_dir.iterdir()):
            pids_dir.rmdir()
    except Exception:
        pass

    return "\n".join(results)


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
