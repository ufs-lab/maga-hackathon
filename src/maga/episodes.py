"""FIND, model route: Gemini names the procedures in session chunks (ARCHITECTURE.md 5.1 rule 5).

Exact matching sees text. This route sees meaning: sessions that start Vite in five spellings
count as one procedure. Each chunk keeps a tool call with its result, adjacent chunks overlap
by one call-and-result pair, and an occurrence counts one time for each session by entry id.
"""

import asyncio
from collections import defaultdict
from collections.abc import Sequence
from hashlib import sha1
import json
from pathlib import Path

from pydantic import BaseModel, Field
from pydantic_ai.models import Model

from maga import finder, llm
from maga.finder import THRESHOLD
from maga.schemas import Candidate, Entry, Evidence

BUDGET = 8000  # tokens for one chunk; the counter is characters divided by 4
OVERLAP = 1  # call-and-result pairs shared by adjacent chunks
_CONCURRENCY = 6
_NAME = (
    "Name each multi-step procedure that the agent performed to reach one goal in this excerpt "
    "of a coding session. Give each a short canonical kebab-case name that another session with "
    "the same goal would also get, such as start-vite-dev-server-on-permitted-port or "
    "publish-pr-and-wait-for-chain. Ignore read-only orientation: git status, git log, git diff, "
    "ls, cat, grep, wc. List the steps as the commands in the excerpt, and the entry ids of "
    "those steps. Report an empty list when the excerpt has no such procedure."
)
_GROUP = (
    "The data lists procedure names with their goals, from several coding sessions. Group the "
    "names that denote the same procedure with the same goal. Return each group as a list of "
    "names, and name each group with the best kebab-case name of its members. A name that "
    "matches no other stays in a group of its own."
)


class Procedure(BaseModel):
    name: str
    goal: str
    steps: list[str]
    entry_ids: list[str]


class Procedures(BaseModel):
    procedures: list[Procedure] = Field(default_factory=list[Procedure])


class Group(BaseModel):
    name: str
    members: list[str]


class Groups(BaseModel):
    groups: list[Group]


def tokens(entries: Sequence[Entry]) -> int:
    return sum(len(render(entry)) for entry in entries) // 4


def render(entry: Entry) -> str:
    """One compact line for the model. A tool result shows through its call."""
    if entry.entry_type == "user_input":
        return f"[{entry.entry_id}] USER: {(entry.content or '')[:300]}"
    if entry.entry_type == "generic_message":
        return f"[{entry.entry_id}] AGENT: {(entry.content or '')[:200]}"
    if entry.entry_type == "tool_call":
        command = entry.command_line or json.dumps(entry.args or {})[:200]
        code = "" if entry.exit_code is None else f" exit={entry.exit_code}"
        return f"[{entry.entry_id}] {entry.tool_name}{code}: {command[:400]}"
    return ""


def _units(entries: Sequence[Entry]) -> list[list[Entry]]:
    """A run of tool calls with the results that follow is one unit; no chunk splits it."""
    units: list[list[Entry]] = []
    for entry in entries:
        joins = (
            units and entry.entry_type == "tool_result" and units[-1][0].entry_type == "tool_call"
        )
        joins = joins or (
            units and entry.entry_type == "tool_call" and units[-1][-1].entry_type == "tool_call"
        )
        if joins:
            units[-1].append(entry)
        else:
            units.append([entry])
    return units


def chunks(
    entries: Sequence[Entry], budget: int = BUDGET, overlap: int = OVERLAP
) -> list[list[Entry]]:
    """Split a session so that no chunk exceeds `budget` tokens, with `overlap` pairs shared."""
    units = _units(entries)
    result: list[list[list[Entry]]] = []
    current: list[list[Entry]] = []
    for unit in units:
        if current and tokens([e for u in [*current, unit] for e in u]) > budget:
            result.append(current)
            current = [u for u in current[-overlap:] if u[0].entry_type == "tool_call"]
        current.append(unit)
    if current:
        result.append(current)
    return [[entry for unit in chunk for entry in unit] for chunk in result]


async def _name_all(texts: list[str], model: Model | str) -> list[Procedures]:
    gate = asyncio.Semaphore(_CONCURRENCY)

    async def one(text: str) -> Procedures:
        async with gate:
            return await asyncio.to_thread(llm.ask, Procedures, _NAME, text, model)

    return list(await asyncio.gather(*(one(text) for text in texts)))


def find(
    sessions: Sequence[Sequence[Entry]], model: Model | str = llm.MODEL, budget: int = BUDGET
) -> list[Candidate]:
    """Candidates for the procedures that Gemini names in 3 or more distinct sessions."""
    texts: list[str] = []
    owners: list[str] = []
    for entries in sessions:
        for chunk in chunks(entries, budget):
            texts.append("\n".join(line for line in map(render, chunk) if line))
            owners.append(chunk[0].session_id)
    named = asyncio.run(_name_all(texts, model))
    found: list[tuple[str, Procedure]] = [
        (session_id, procedure)
        for session_id, answer in zip(owners, named, strict=True)
        for procedure in answer.procedures
    ]
    if not found:
        return []
    distinct = {p.name: p.goal for _, p in found}
    data = json.dumps([{"name": n, "goal": g} for n, g in distinct.items()])
    groups = llm.ask(Groups, _GROUP, data, model).groups
    canonical = {member: group.name for group in groups for member in group.members}
    seen: dict[str, list[tuple[str, Procedure]]] = defaultdict(list)
    for session_id, procedure in found:
        seen[canonical.get(procedure.name, procedure.name)].append((session_id, procedure))
    candidates = [_candidate(name, items) for name, items in seen.items()]
    return finder.rank(c for c in candidates if c.frequency >= THRESHOLD)


def _candidate(name: str, items: list[tuple[str, Procedure]]) -> Candidate:
    # The overlap can report one occurrence in two chunks: shared entry ids count one time.
    occurrences: list[tuple[str, Procedure]] = []
    claimed: set[tuple[str, str]] = set()
    for session_id, procedure in items:
        ids = {(session_id, entry_id) for entry_id in procedure.entry_ids}
        if not ids & claimed:
            occurrences.append((session_id, procedure))
        claimed |= ids
    steps = max((p.steps for _, p in occurrences), key=len)
    template = "\n".join(finder.normalise(step, None) for step in steps)
    return Candidate(
        candidate_id=f"cand_episode_{sha1(name.encode(), usedforsecurity=False).hexdigest()[:10]}",
        title=name,
        command_sequence=steps[:10],
        normalized_template=template,
        frequency=len({session_id for session_id, _ in occurrences}),
        evidence_type="repetition",
        evidence=Evidence(
            session_ids=sorted({session_id for session_id, _ in occurrences}),
            observed_occurrences=len(occurrences),
            observed_turns_mean=float(len(steps)),
            observed_tokens_mean=0,
            common_pitfalls=sorted({p.goal for _, p in occurrences})[:10],
        ),
    )


def run(state: Path, sessions: Sequence[Sequence[Entry]]) -> list[Candidate]:
    """Store the episode candidates next to the exact-match ones; replace only their own kind."""
    target = state / "candidates"
    target.mkdir(parents=True, exist_ok=True)
    for old in target.glob("cand_episode_*.json"):
        old.unlink()
    candidates = find(sessions)
    for candidate in candidates:
        (target / f"{candidate.candidate_id}.json").write_text(candidate.model_dump_json(indent=2))
    return candidates
