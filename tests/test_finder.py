"""TT-NRM, TT-SEQ, TT-EFX, TT-COR: normalisation, sequence counting, pairs, and corrections."""

from pathlib import Path

import pytest

from maga.finder import find, normalise, rank, steps
from maga.reader import parse_session
from maga.schemas import Candidate, Entry, Evidence

P1_DIR = Path(__file__).parent / "fixtures" / "claude_code" / "p1"
P1 = [
    "pnpm --dir $REPO_ROOT/apps/web install",
    "pnpm --dir $REPO_ROOT/apps/web exec vite --port $PORT_LIST --strictPort",
    'curl -s -H "Origin: http://localhost:$PORT_LIST" http://localhost:$PORT_LIST/api/health',
]


def _p1(name: str) -> list[Entry]:
    return parse_session(P1_DIR / f"sess-{name}.jsonl")[0]


def _session(session_id: str, *items: tuple[str, int | None] | str) -> list[Entry]:
    """A tuple is a command with its exit code. A string is a human message."""
    return [
        Entry.model_validate(
            {
                "entry_id": f"{session_id}-{index}",
                "session_id": session_id,
                "step_index": index,
                "timestamp": "2026-01-05T10:00:00Z",
                "working_dir": "/home/dev/repo",
            }
            | (
                {"source": "user", "entry_type": "user_input", "content": item}
                if isinstance(item, str)
                else {
                    "source": "model",
                    "entry_type": "tool_call",
                    "tool_name": "Bash",
                    "command_line": item[0],
                    "exit_code": item[1],
                }
            )
        )
        for index, item in enumerate(items)
    ]


def test_nrm_001_an_absolute_path_becomes_a_repository_token() -> None:
    command = "ls /home/user/workspace/apps/web"
    assert normalise(command, "/home/user/workspace") == "ls $REPO_ROOT/apps/web"


@pytest.mark.parametrize(
    ("first", "second", "kept"),
    [
        ("vite --port 5173 --strictPort", "vite --port 5174 --strictPort", "$PORT_LIST --strict"),
        (
            'curl -s -H "Origin: http://localhost:5173" http://localhost:4000/api/health',
            'curl -s -H "Origin: http://localhost:5174" http://localhost:4000/api/health',
            "/api/health",
        ),
        (
            "tar -czf backup-2026-01-05T10:00:00Z.tgz dist",
            "tar -czf backup-2026-02-09T18:30:00Z.tgz dist",
            "tar -czf backup-<TS>.tgz",
        ),
        ("kill 4242", "kill 17001", "kill <PID>"),
        (
            "git show 3f9a1c2",
            "git show b7e44d0a9c1f5e6d7a8b9c0d1e2f3a4b5c6d7e8f",
            "git show <SHA>",
        ),
        (
            "cat logs/0b9d6c2e-1f4a-4b7c-9d3e-5a6f7b8c9d0e.log",
            "cat logs/7c1e2d3f-4a5b-4c6d-8e7f-9a0b1c2d3e4f.log",
            "cat logs/<UUID>.log",
        ),
        ("git checkout -b fix/DEMO-101-login", "git checkout -b fix/DEMO-202-login", "fix/<ID>-"),
        ("gh pr view 12", "gh pr view 345", "gh pr view <ID>"),
    ],
)
def test_nrm_003_004_005_009_010_variable_values_do_not_split_a_command(
    first: str, second: str, kept: str
) -> None:
    assert normalise(first, None) == normalise(second, None)
    assert kept in normalise(first, None)


def test_nrm_a_sibling_directory_is_not_inside_the_repository() -> None:
    command = "git worktree remove /home/dev_a/workspace-wt"
    assert "$REPO_ROOT" not in normalise(command, "/home/dev_a/workspace")


def test_nrm_002_two_repository_roots_give_the_same_command() -> None:
    first = normalise("pnpm --dir /home/dev_a/workspace/apps/web install", "/home/dev_a/workspace")
    second = normalise(
        "pnpm --dir /home/dev_b/projects/repo/apps/web install", "/home/dev_b/projects/repo"
    )
    assert first == second == P1[0]


@pytest.mark.parametrize(
    ("first", "second"),
    [
        ("vite --port 5173 --strictPort", "vite --port 5173"),
        (
            "git worktree add /home/dev_a/workspace-wt demo",
            "git worktree remove /home/dev_a/workspace-wt",
        ),
        ("rm -rf /home/dev_a/workspace/dist", "rm -rf /home/dev_a/workspace/src"),
        ("git reset --hard 3f9a1c2", "git revert 3f9a1c2"),
        ("git log -n 5 --oneline", "git log -n 50 --oneline"),
    ],
)
def test_nrm_006_010_different_commands_stay_different(first: str, second: str) -> None:
    assert normalise(first, "/home/dev_a/workspace") != normalise(second, "/home/dev_a/workspace")


def test_nrm_008_normalisation_is_idempotent() -> None:
    for entry in _p1("a"):
        if entry.command_line:
            once = normalise(entry.command_line, entry.working_dir)
            assert normalise(once, entry.working_dir) == once


def test_steps_split_a_compound_line_and_drop_the_output_handling() -> None:
    line = (
        "cd /home/dev/repo/apps/web && nohup pnpm exec vite --port 5200 --strictPort"
        " > /tmp/x/dev.log 2>&1 & sleep 12; curl -s http://localhost:5200 | tail -5"
    )
    assert steps(line, "/home/dev/repo") == [
        "vite --port $PORT_LIST --strictPort",
        "curl -s http://localhost:$PORT_LIST",
    ]


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        (  # a polling loop is one procedure, not the fragments `fi`, `done`, `n=$m`
            (
                "n=0; while true; do m=$(wc -l < log); if [ $m -gt $n ]; then n=$m; fi; "
                "grep -q '^chain exit' log && break; sleep 5; done; git status --short"
            ),
            [
                (
                    "while true; do m=$(wc -l < log); if [ $m -gt $n ]; then n=$m; fi; "
                    "grep -q '^chain exit' log && break; sleep 5; done"
                ),
                "git status --short",
            ],
        ),
        (  # an operator inside quotes does not split the step
            "python3 -c \"import json, sys; print(1)\"; awk '{print $1; print $2}' f",
            ['python3 -c "import json, sys; print(1)"', "awk '{print $1; print $2}' f"],
        ),
        (  # three runner forms of the same Vite launch give one step
            (
                "pnpm exec vite --port 5173 --strictPort; node node_modules/vite/bin/vite.js "
                "--port 5174 --strictPort; ./node_modules/.bin/vite --port 5173 --strictPort"
            ),
            ["vite --port $PORT_LIST --strictPort"] * 3,
        ),
        (  # a bare assignment, `export`, and a stray block keyword are not steps
            "export FOO=bar; X=1; then break; fi; done; git fetch -q origin main",
            ["git fetch -q origin main"],
        ),
    ],
)
def test_steps_keep_a_shell_block_and_a_quoted_text_whole(line: str, expected: list[str]) -> None:
    assert steps(line, None) == expected


def test_steps_mask_an_inline_script() -> None:
    assert steps("python3 - <<'EOF'\nprint(1); print(2)\nEOF\ngit status", None) == [
        "python3 - <<HEREDOC",
        "git status",
    ]


def test_seq_001_007_three_sessions_flag_p1_and_the_stored_entries_do_not_change() -> None:
    sessions = [_p1(name) for name in "abc"]
    before = [entry.model_dump_json() for session in sessions for entry in session]
    (candidate,) = find(sessions)
    assert candidate.normalized_template.splitlines() == P1
    assert (candidate.frequency, candidate.evidence_type) == (3, "repetition")
    assert candidate.command_sequence[1].endswith("exec vite --port 5173 --strictPort")
    assert find(sessions) == [candidate]
    assert before == [entry.model_dump_json() for session in sessions for entry in session]


def test_seq_002_003_005_fewer_than_three_distinct_sessions_flag_nothing() -> None:
    assert find([_p1("a"), _p1("b")]) == []
    assert find([_p1("a"), _p1("b"), _p1("a")]) == []
    assert find([_p1("a") * 5]) == []


def test_seq_004_repeats_in_one_session_count_one_session() -> None:
    (candidate,) = find([_p1("a") * 5, _p1("b"), _p1("c")])
    assert candidate.frequency == 3
    assert candidate.evidence.session_ids == ["sess-a", "sess-b", "sess-c"]
    assert candidate.evidence.observed_occurrences == 7


def test_seq_006_a_different_order_is_a_different_sequence() -> None:
    reverse = [e for e in reversed(_p1("c")) if e.entry_type == "tool_call"]
    assert all(len(c.command_sequence) < 3 for c in find([_p1("a"), _p1("b"), reverse]))


def test_seq_008_ranking_order_and_tie_break() -> None:
    def candidate(name: str, kind: str, frequency: int) -> Candidate:
        evidence = Evidence(
            session_ids=[], observed_occurrences=0, observed_turns_mean=0, observed_tokens_mean=0
        )
        return Candidate.model_validate(
            {
                "candidate_id": name,
                "title": name,
                "command_sequence": [],
                "normalized_template": "",
                "frequency": frequency,
                "evidence_type": kind,
                "evidence": evidence,
            }
        )

    given = [
        candidate("cand_f", "repetition", 9),
        candidate("cand_e", "repetition", 3),
        candidate("cand_d", "error_fix", 3),
        candidate("cand_c", "error_fix", 4),
        candidate("cand_b", "correction", 3),
        candidate("cand_a", "correction", 3),
    ]
    expected = ["cand_a", "cand_b", "cand_c", "cand_d", "cand_f", "cand_e"]
    assert [c.candidate_id for c in rank(given)] == expected
    assert [c.candidate_id for c in rank(reversed(given))] == expected


OK = ("lsof -i :5173", 0)
EFX_CASES = {
    "001 documented example": ([("vite", 1), OK, ("vite --port 5174", 0)], True),
    "002 third action": ([("vite", 1), OK, ("cat ports.json", 0), ("vite --port 5174", 0)], True),
    "003 fourth action": (
        [("vite", 1), OK, ("cat ports.json", 0), ("git status", 0), ("vite --port 5174", 0)],
        False,
    ),
    "004 identical retry": ([("vite", 1), ("vite", 0)], False),
    "005 fix fails": ([("vite", 1), ("vite --port 5174", 1)], False),
    "005 fix unknown": ([("vite", 1), ("vite --port 5174", None)], False),
    "unknown is not a failure": ([("vite", None), ("vite --port 5174", 0)], False),
}


@pytest.mark.parametrize("case", EFX_CASES)
def test_efx_error_and_fix_pairs(case: str) -> None:
    commands, detected = EFX_CASES[case]
    found = find([_session(f"sess-e{n}", *commands) for n in range(3)])
    pairs = [c for c in found if c.evidence_type == "error_fix"]
    assert [c.normalized_template for c in pairs] == (
        ["vite\nvite --port $PORT_LIST"] if detected else []
    )


P3 = (("kill -9 4242", 0), "don't kill that process", ("lsof -i :5173", 0))


def test_cor_001_004_the_step_before_the_correction_is_flagged_and_ranks_first() -> None:
    found = find([_session(f"sess-u{n}", *P3) for n in range(3)] + [_p1(n) for n in "abc"])
    # P3 is also a repeated sequence of two commands, so it ranks again below the correction.
    assert [c.evidence_type for c in found] == ["correction", "repetition", "repetition"]
    assert found[0].normalized_template == "kill -9 <PID>"
    assert found[0].evidence.common_pitfalls == ["don't kill that process"]


def test_cor_002_an_ordinary_message_is_not_a_correction() -> None:
    ordinary = (("pnpm test", 0), "Thanks. Now run the linter.", ("pnpm lint", 0))
    found = find([_session(f"sess-o{n}", *ordinary) for n in range(3)])
    assert [c.evidence_type for c in found] == ["repetition"]


def test_cor_003_a_classifier_rejection_stops_the_promotion() -> None:
    sessions = [_session(f"sess-u{n}", *P3) for n in range(3)]
    found = find(sessions, confirm=lambda _command, _messages: False)
    assert "correction" not in {c.evidence_type for c in found}


def test_cor_005_a_tool_result_is_never_a_correction_candidate() -> None:
    asked: list[str] = []

    def classify(command: str, messages: list[str]) -> bool:
        asked.extend([command, *messages])
        return True

    sessions: list[list[Entry]] = []
    for n in range(3):
        call, _, after = _session(f"sess-t{n}", *P3)
        result = call.model_copy(
            update={
                "entry_id": f"{call.entry_id}-result",
                "source": "system",
                "entry_type": "tool_result",
                "command_line": None,
                "sanitized_output": "don't kill that process",
            }
        )
        sessions.append([call, result, after])
    assert "correction" not in {c.evidence_type for c in find(sessions, classify)}
    assert asked == []


def test_cor_003_the_model_judges_a_correction_once_and_never_a_repetition() -> None:
    asked: list[tuple[str, list[str]]] = []

    def confirm(command: str, messages: list[str]) -> bool:
        asked.append((command, messages))
        return True

    sessions = [_session(f"sess-u{n}", *P3) for n in range(3)] + [_p1(n) for n in "abc"]
    found = find(sessions, confirm)
    assert asked == [("kill -9 <PID>", ["don't kill that process"])]  # one call, redacted form
    assert found[0].evidence_type == "correction"
    # Below the threshold nothing is promoted, so the model is not asked at all.
    asked.clear()
    find(sessions[:2], confirm)
    assert asked == []


def test_chk_006_the_evidence_that_goes_to_the_model_does_not_grow_with_the_sessions() -> None:
    asked: list[list[str]] = []

    def confirm(_command: str, messages: list[str]) -> bool:
        asked.append(messages)
        return True

    sessions = [
        _session(f"sess-u{n:02d}", ("kill -9 4242", 0), f"don't kill process number {n:02d}")
        for n in range(25)
    ]
    (candidate,) = find(sessions, confirm)
    assert candidate.frequency == 25
    assert len(candidate.evidence.common_pitfalls) == 10
    assert [len(messages) for messages in asked] == [10]
    assert len(candidate.model_dump_json()) < 5000


def test_the_demo_sessions_give_a_correction_first_and_the_vite_sequence() -> None:
    demo = sorted((P1_DIR.parent / "demo").glob("*.jsonl"))
    found = find([parse_session(path)[0] for path in demo])
    assert len(demo) == 4
    assert [c.evidence_type for c in found][:2] == ["correction", "repetition"]
    assert found[0].frequency == 4
    assert all(message.startswith("no, ") for message in found[0].evidence.common_pitfalls)
    assert any("vite --port $PORT_LIST --strictPort" in c.normalized_template for c in found)
