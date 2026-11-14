"""Long-term memory: lessons on how to process contracts, learned across many runs, kept per agent.

Lifecycle: after a contract run completes, each agent reflects on its own output with ITS OWN prompt
agents/<agent>/memory/long_term_reflection.md and proposes lessons -> stored `pending`. A person reviews them
(CLI below); only `approved` lessons are added to that agent's prompt for later contracts.
Storage: one SQLite file per agent, <LONG_TERM_MEMORY_DIR>/<agent_id>.sqlite3 (agent volume, e.g.
/data/long_term_memory/agent_2_risk_scoring.sqlite3). Agents never read each other's lessons.

Review CLI (inside the agent container, e.g. `docker compose exec agent-2 python -m agents.memory.long_term list`):
    list [--status pending|approved|rejected]   approve ID...   reject ID...   add "lesson text"
"""
from contextlib import closing
from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
import re
import sqlite3

from agents.memory import LONG_TERM_PROMPT
from common.exceptions import ConfigurationError

log = logging.getLogger("agents.memory.long_term")
STATUSES = ("pending", "approved", "rejected")
MAX_LESSONS_PER_RUN, MAX_LESSON_CHARS = 5, 1000
MAX_PROMPT_LESSONS, MAX_PROMPT_CHARS = 30, 8000  # newest approved lessons that fit go into the prompt
MAX_REFLECTION_INPUT = 20000


def _now():
    return datetime.now(timezone.utc).isoformat()


def _norm(text):
    return re.sub(r"\s+", " ", text).strip().lower()


class LessonStore:
    def __init__(self, path):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path, check_same_thread=False)
        self.db.execute("""create table if not exists lessons (
            id integer primary key autoincrement, agent_id text not null, text text not null,
            status text not null check (status in ('pending', 'approved', 'rejected')),
            source text, created_at text not null, reviewed_at text, reviewer text)""")
        self.db.commit()

    def propose(self, agent_id, texts, source=None, status="pending"):
        """Store new lessons; exact repeats (any status) of this agent are skipped. Returns new ids."""
        known = {_norm(t) for (t,) in self.db.execute("select text from lessons where agent_id = ?", (agent_id,))}
        ids = []
        for text in texts:
            if _norm(text) in known:
                continue
            known.add(_norm(text))
            cursor = self.db.execute("insert into lessons (agent_id, text, status, source, created_at) values (?, ?, ?, ?, ?)",
                                     (agent_id, text.strip(), status, json.dumps(source, ensure_ascii=False) if source else None, _now()))
            ids.append(cursor.lastrowid)
        self.db.commit()
        return ids

    def review(self, ids, status, reviewer=""):
        if status not in ("approved", "rejected"):
            raise ValueError("status must be approved or rejected")
        with self.db:
            changed = self.db.executemany("update lessons set status = ?, reviewed_at = ?, reviewer = ? where id = ?",
                                          [(status, _now(), reviewer, int(i)) for i in ids]).rowcount
        return changed

    def list(self, agent_id=None, status=None):
        query, args = "select id, agent_id, text, status, source, created_at, reviewed_at from lessons where 1=1", []
        if agent_id:
            query, args = query + " and agent_id = ?", args + [agent_id]
        if status:
            query, args = query + " and status = ?", args + [status]
        cols = ("id", "agent_id", "text", "status", "source", "created_at", "reviewed_at")
        return [dict(zip(cols, row)) for row in self.db.execute(query + " order by id", args)]

    def prompt_section(self, agent_id):
        """Approved lessons for the system prompt, newest first within the budget; '' when none."""
        lines, used = [], 0
        for lesson_id, text in self.db.execute(
                "select id, text from lessons where agent_id = ? and status = 'approved' order by id desc limit ?",
                (agent_id, MAX_PROMPT_LESSONS)):
            line = f"- [L{lesson_id}] {text}"
            if used + len(line) > MAX_PROMPT_CHARS:
                break
            lines.append(line)
            used += len(line)
        return "" if not lines else "\n".join(["# Long-term memory: approved lessons from earlier contracts", *reversed(lines)])

    def close(self):
        self.db.close()


def reflection_prompt(directory):
    path = Path(directory) / LONG_TERM_PROMPT
    text = path.read_text(encoding="utf-8") if path.is_file() else ""
    return re.sub(r"<!--.*?-->", "", text, flags=re.DOTALL).strip()


def parse_lessons(text):
    """Model output -> lesson texts. Accepts {"lessons": [...]} or [...] (strings or {"text": ...}), optional ``` fence."""
    body = re.sub(r"^```(?:json)?\s*|\s*```$", "", (text or "").strip())
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        return []
    items = data.get("lessons", []) if isinstance(data, dict) else data
    if not isinstance(items, list):
        return []
    lessons = [(i.get("text") if isinstance(i, dict) else i) for i in items]
    return [l.strip() for l in lessons if isinstance(l, str) and 0 < len(l.strip()) <= MAX_LESSON_CHARS][:MAX_LESSONS_PER_RUN]


def reflect(agent, contract, output):
    """One agent proposes lessons from its finished run. Returns {"proposed": n} or {"skipped": reason}."""
    if agent.lessons is None:
        return {"skipped": "long-term memory not configured (LONG_TERM_MEMORY_DIR)"}
    prompt = reflection_prompt(agent.directory)
    if not prompt:
        return {"skipped": f"TODO: fill {agent.agent_id}/{LONG_TERM_PROMPT}"}
    payload = {"agent_id": agent.agent_id, "contract": contract, "output": str(output)[:MAX_REFLECTION_INPUT],
               "approved_lessons": agent.lessons.prompt_section(agent.agent_id)}
    result = agent.provider.generate([{"role": "system", "content": prompt},
                                      {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}],
                                     inference_config={})
    lessons = parse_lessons(result.text) if result.stop_reason == "stop" else []
    ids = agent.lessons.propose(agent.agent_id, lessons, source=contract)
    return {"proposed": len(ids), "lesson_ids": ids}


def store_from_env(agent_id):
    """This agent's own file; None (disabled) when LONG_TERM_MEMORY_DIR is unset."""
    directory = os.getenv("LONG_TERM_MEMORY_DIR", "").strip()
    if not directory or not re.fullmatch(r"[a-z0-9_]+", agent_id or ""):
        return None
    return LessonStore(Path(directory) / f"{agent_id}.sqlite3")


def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(prog="python -m agents.memory.long_term")
    commands = parser.add_subparsers(dest="command", required=True)
    listing = commands.add_parser("list")
    listing.add_argument("--status", choices=STATUSES)
    for name in ("approve", "reject"):
        commands.add_parser(name).add_argument("ids", nargs="+", type=int)
    commands.add_parser("add", help="add an approved lesson written by a person").add_argument("text")
    parser.add_argument("--agent", default=os.getenv("AGENT_ID", ""), help="agent id (default: AGENT_ID)")
    parser.add_argument("--reviewer", default=os.getenv("USER", "operator"))
    args = parser.parse_args(argv)
    store = store_from_env(args.agent)
    if store is None:
        raise ConfigurationError("Set LONG_TERM_MEMORY_DIR and pass --agent (or set AGENT_ID)")
    with closing(store):
        if args.command == "list":
            for row in store.list(args.agent, args.status):
                print(json.dumps(row, ensure_ascii=False))
        elif args.command == "add":
            print(json.dumps({"added": store.propose(args.agent, [args.text], source={"by": args.reviewer}, status="approved")}))
        else:
            status = {"approve": "approved", "reject": "rejected"}[args.command]
            print(json.dumps({"updated": store.review(args.ids, status, args.reviewer)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
