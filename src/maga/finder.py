"""FIND: count repeated command sequences, error-and-fix pairs, and user corrections.

No model runs here (ARCHITECTURE.md 5.1).
"""

from collections import defaultdict
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from hashlib import sha1
import json
from pathlib import Path
import re

from pydantic import BaseModel, TypeAdapter

from maga import llm
from maga.schemas import Candidate, Entry, Evidence

THRESHOLD = 3  # distinct sessions
_LENGTHS = range(2, 6)
_MAX_NOTES = 10  # each note is at most 300 characters
_FIX_WINDOW = 3  # tool actions after the failed step
_TYPE_ORDER = {"correction": 0, "error_fix": 1, "repetition": 2}
_ENTRIES = TypeAdapter(list[Entry])

_HEX = r"[0-9a-f]"
_RULES = [
    (re.compile(rf"\b{_HEX}{{8}}-{_HEX}{{4}}-{_HEX}{{4}}-{_HEX}{{4}}-{_HEX}{{12}}\b"), "<UUID>"),
    (
        re.compile(
            r"\b\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2})?(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?)?"
        ),
        "<TS>",
    ),
    (re.compile(r"(--port[= ]|\bPORT=|(?<![\d:/]):)\d{4,5}\b"), r"\1$PORT_LIST"),
    (re.compile(r"(\bkill(?:\s+-\S+)*\s+|\b(?:lsof|ps)\s+-p\s*)\d+\b"), r"\1<PID>"),
    (re.compile(rf"\b(?={_HEX}*[a-f])(?={_HEX}*\d){_HEX}{{7,40}}\b"), "<SHA>"),
    (re.compile(r"\b[A-Z][A-Z0-9]+-\d{2,}\b"), "<ID>"),
    (re.compile(r"(\bgh pr \w+ |#|/pull/)\d+\b"), r"\1<ID>"),
    (re.compile(r"(?<![\w$.~>:/])/(?:[\w.@+-]+/)+[\w.@+-]*"), "<PATH>"),
]
_HEREDOC = re.compile(r"<<-?\s*(['\"]?)(\w+)\1.*?\n\2\b", re.DOTALL)
_SPLIT = re.compile(r"&&|\|\||;|\n|(?<![>&])&(?![>&\d])")
# Output handling says nothing about the procedure: redirections and a trailing head or tail.
_TRIM = re.compile(r"\s*\d?>>?\s*(?:&\d|\S+)|\s*\|\s*(?:tail|head)\b[^|]*$")
# A quoted text over several lines is an inline script, the same as a heredoc.
_SCRIPT = re.compile(r"""(["'])(?:(?!\1).)*\n(?:(?!\1).)*\1""", re.DOTALL)
# Runners that start the same program: `pnpm exec vite`, `npx vite`, `./node_modules/.bin/vite`.
_WRAPPER = re.compile(
    r"^\s*(?:(?:nohup|exec|setsid|timeout\s+\d+|pnpm exec|npx|\(|\w+=\S*\s|(?:\./)?node_modules/\.bin/)\s*)+"
)
_INLINE = ("<<HEREDOC", "<SCRIPT>")
# A quoted text never splits a step, whatever operator it holds.
_QUOTED = re.compile(r"'[^']*'" + r'|"(?:[^"\\]|\\.)*"', re.DOTALL)
# A compound command is one step: an agent that polls in a loop performs one procedure.
# ponytail: no nesting of the same keyword; an inner `done` ends the outer loop.
_BLOCK = re.compile(
    r"\b(?:while|until|for)\b.*?\bdo\b.*?\bdone\b|\bif\b.*?\bthen\b.*?\bfi\b|\bcase\b.*?\besac\b",
    re.DOTALL,
)
# `node node_modules/vite/bin/vite.js` and `pnpm exec vite` start the same program.
_VITE_JS = re.compile(r"\bnode\s+\S*vite/bin/vite\.js\b")
_ASSIGNMENT = re.compile(r"(?:export\s+)?\w+=\S*")
_NOT_A_STEP = {
    "echo",
    "sleep",
    "cd",
    "true",
    ":",
    "do",
    "done",
    "then",
    "else",
    "fi",
    "esac",
    "}",
    "{",
    "break",
    "continue",
}
# A free prefilter. The model judges only the corrections that reach the threshold (5.1 rule 3).
_CORRECTION = re.compile(
    r"^\s*(no\b|nope\b|don'?t\b|do not\b|stop\b|wrong\b|never\b|that'?s (?:wrong|not)\b"
    r"|you (?:missed|forgot|should)\b|why did you\b|check\b)",
    re.IGNORECASE,
)
_MAX_CORRECTION_CHARS = 300


def normalise(command: str, cwd: str | None) -> str:
    """Replace the values that differ between sessions; keep everything that carries meaning."""
    if cwd and cwd != "/":
        command = re.sub(re.escape(cwd) + r"(?![\w.-])", "$REPO_ROOT", command)
    for pattern, replacement in _RULES:
        command = pattern.sub(replacement, command)
    return command


def steps(command_line: str, cwd: str | None) -> list[str]:
    """Split one shell command line into its normalised simple commands.

    An agent joins steps with `&&` and `;`, so whole lines almost never repeat; steps do.
    """
    # ponytail: a regex split, so an operator inside quotes also splits; use a shell
    # parser if a real procedure needs it.
    kept: list[str] = []

    def protect(match: re.Match[str]) -> str:
        kept.append(match.group(0))
        return f"\x00{len(kept) - 1}\x00"

    masked = _SCRIPT.sub("<SCRIPT>", _HEREDOC.sub("<<HEREDOC", command_line))
    masked = _BLOCK.sub(protect, _QUOTED.sub(protect, masked))
    found: list[str] = []
    for raw in _SPLIT.split(masked):
        step = _VITE_JS.sub("vite", _WRAPPER.sub("", _TRIM.sub("", raw))).strip()
        while "\x00" in step:  # a quoted text inside a block: one more level to restore
            step = re.sub(
                r"\x00(\d+)\x00", lambda m: " ".join(kept[int(m.group(1))].split()), step
            )
        if step and step.split()[0] not in _NOT_A_STEP and not _ASSIGNMENT.fullmatch(step):
            found.append(normalise(step, cwd))
    return found


def keyword_correction(message: str) -> bool:
    return len(message) <= _MAX_CORRECTION_CHARS and _CORRECTION.match(message) is not None


@dataclass
class _Seen:
    example: list[str]
    sessions: set[str] = field(default_factory=set[str])
    occurrences: int = 0
    tokens: int = 0
    notes: set[str] = field(default_factory=set[str])


def _bounded(notes: set[str]) -> list[str]:
    """Evidence goes to the model, so its size must not grow with the number of sessions."""
    return sorted(notes)[:_MAX_NOTES]


def _candidate(kind: str, key: tuple[str, ...], seen: _Seen) -> Candidate:
    template = "\n".join(key)
    return Candidate.model_validate(
        {
            "candidate_id": f"cand_{kind}_{sha1(template.encode(), usedforsecurity=False).hexdigest()[:10]}",
            "title": " -> ".join(key)[:120],
            "command_sequence": seen.example,
            "normalized_template": template,
            "frequency": len(seen.sessions),
            "evidence_type": kind,
            "evidence": Evidence(
                session_ids=sorted(seen.sessions),
                observed_occurrences=seen.occurrences,
                observed_turns_mean=float(len(key)),
                observed_tokens_mean=seen.tokens // seen.occurrences,
                failure_traces=_bounded(seen.notes) if kind == "error_fix" else [],
                common_pitfalls=_bounded(seen.notes) if kind == "correction" else [],
            ),
        }
    )


def _inline(key: tuple[str, ...]) -> bool:
    return any(marker in step for step in key for marker in _INLINE)


def _contains(longer: tuple[str, ...], shorter: tuple[str, ...]) -> bool:
    size = len(shorter)
    return any(longer[i : i + size] == shorter for i in range(len(longer) - size + 1))


def rank(candidates: Iterable[Candidate]) -> list[Candidate]:
    return sorted(
        candidates,
        key=lambda c: (_TYPE_ORDER[c.evidence_type], -c.frequency, c.candidate_id),
    )


type _Tally = dict[str, dict[tuple[str, ...], _Seen]]


def _note(seen: dict[tuple[str, ...], _Seen], key: tuple[str, ...], calls: list[Entry]) -> _Seen:
    record = seen[key]
    distinct = {call.entry_id: call for call in calls}.values()
    record.example = record.example or [(call.command_line or "")[:300] for call in distinct]
    record.sessions.add(calls[0].session_id)
    record.occurrences += 1
    record.tokens += sum(call.tokens_out or 0 for call in distinct)
    return record


def _sequences(seen: dict[tuple[str, ...], _Seen], flat: list[tuple[str, Entry]]) -> None:
    for size in _LENGTHS:
        for start in range(len(flat) - size + 1):
            window = flat[start : start + size]
            key = tuple(step for step, _ in window)
            # One command in a loop is polling, and an inline script is not a procedure.
            if len(set(key)) > 1 and not _inline(key):
                _note(seen, key, [call for _, call in window])


def _pairs(
    seen: dict[tuple[str, ...], _Seen], actions: list[Entry], normal: dict[str, str]
) -> None:
    for index, failed in enumerate(actions):
        if not failed.command_line or not failed.exit_code:  # None is unknown, not a failure
            continue
        for fix in actions[index + 1 : index + 1 + _FIX_WINDOW]:
            if (
                fix.command_line
                and fix.exit_code == 0
                and fix.command_line != failed.command_line
                and fix.command_line.split()[0] == failed.command_line.split()[0]
            ):
                key = (normal[failed.entry_id], normal[fix.entry_id])
                if all(key) and not _inline(key):
                    _note(seen, key, [failed, fix]).notes.add(
                        (failed.sanitized_output or "")[:200]
                    )
                break


def _corrections(
    seen: dict[tuple[str, ...], _Seen],
    entries: list[Entry],
    normal: dict[str, str],
) -> None:
    previous: Entry | None = None
    for entry in entries:
        if entry.entry_type == "tool_call":
            previous = entry if normal.get(entry.entry_id) else None
        elif (
            entry.entry_type == "user_input"
            and previous
            and keyword_correction(entry.content or "")
        ):
            _note(seen, (normal[previous.entry_id],), [previous]).notes.add(entry.content or "")
            previous = None


def find(
    sessions: Iterable[list[Entry]],
    confirm: Callable[[str, list[str]], bool] = lambda _command, _messages: True,
) -> list[Candidate]:
    """Return the ranked candidates that appear in at least THRESHOLD distinct sessions.

    `confirm` validates a correction before its promotion: it gets the flagged command and the
    human messages, and a False drops the candidate. Sequence counting never calls it.
    """
    seen: _Tally = {kind: defaultdict(lambda: _Seen(example=[])) for kind in _TYPE_ORDER}
    for entries in sessions:
        actions = [e for e in entries if e.entry_type == "tool_call"]
        commands = [e for e in actions if e.command_line]
        parts = {e.entry_id: steps(e.command_line or "", e.working_dir) for e in commands}
        normal = {entry_id: " && ".join(found) for entry_id, found in parts.items()}
        _sequences(seen["repetition"], [(step, e) for e in commands for step in parts[e.entry_id]])
        _pairs(seen["error_fix"], actions, normal)
        _corrections(seen["correction"], entries, normal)

    flagged = {
        kind: {key: s for key, s in found.items() if len(s.sessions) >= THRESHOLD}
        for kind, found in seen.items()
    }
    flagged["correction"] = {
        key: s for key, s in flagged["correction"].items() if confirm(key[0], _bounded(s.notes))
    }
    # A part of a longer flagged sequence, seen in the same sessions, is the same procedure.
    repeats = flagged["repetition"]
    flagged["repetition"] = {
        key: s
        for key, s in repeats.items()
        if not any(
            len(other) > len(key)
            and _contains(other, key)
            and repeats[other].sessions >= s.sessions
            for other in repeats
        )
    }
    return rank(
        _candidate(kind, key, s) for kind, found in flagged.items() for key, s in found.items()
    )


class _Judgement(BaseModel):
    is_correction: bool
    reason: str


def gemini_confirms(command: str, messages: list[str]) -> bool:
    """Ask Gemini if the human messages correct the agent step. One call for each candidate."""
    instructions = (
        "A coding agent ran `command`. In several sessions a person then wrote one of `messages`. "
        "Answer is_correction true only if the messages tell the agent that this step was wrong "
        "or must be done another way. A new task, thanks, or a question is not a correction."
    )
    data = json.dumps({"command": command, "messages": messages})
    return llm.ask(_Judgement, instructions, data).is_correction


def run(state: Path) -> list[Candidate]:
    """FIND over the stored entries; replace the stored candidates with the result."""
    files = sorted((state / "entries").glob("*.json"))
    sessions = (_ENTRIES.validate_json(path.read_bytes()) for path in files)
    candidates = find(sessions, gemini_confirms)
    target = state / "candidates"
    target.mkdir(parents=True, exist_ok=True)
    for old in target.glob("*.json"):
        old.unlink()
    for candidate in candidates:
        (target / f"{candidate.candidate_id}.json").write_text(candidate.model_dump_json(indent=2))
    return candidates
