"""The entry point: exit 0 pass, 1 fail, 2 usage, for each stage that needs no model."""

from pathlib import Path
import sys

import pytest

from maga import triage
from maga.__main__ import main
from maga.schemas import Candidate
from maga.triage import Decision

P1 = sorted((Path(__file__).parent / "fixtures" / "claude_code" / "p1").glob("*.jsonl"))
CANDIDATE = Candidate.model_validate(
    {
        "candidate_id": "cand_repetition_x",
        "title": "uv sync --group tests",
        "command_sequence": ["uv sync --group tests"],
        "normalized_template": "uv sync --group tests",
        "frequency": 3,
        "evidence_type": "repetition",
        "evidence": {
            "session_ids": ["sess-a", "sess-b", "sess-c"],
            "observed_occurrences": 3,
            "observed_turns_mean": 1.0,
            "observed_tokens_mean": 0,
        },
    }
)


def _maga(monkeypatch: pytest.MonkeyPatch, *args: str) -> int:
    monkeypatch.setattr(sys, "argv", ["maga", *args])
    return main()


def test_read_then_find_on_the_synthetic_sessions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    assert _maga(monkeypatch, "find") == 1  # no entries yet: a fail, not a crash
    assert _maga(monkeypatch, "read", *map(str, P1)) == 0
    assert '"new_entries": 21' in capsys.readouterr().out
    assert _maga(monkeypatch, "find") == 0
    assert "vite --port $PORT_LIST --strictPort" in capsys.readouterr().out
    assert len(list((tmp_path / ".maga/state/candidates").glob("*.json"))) == 1


@pytest.mark.parametrize("stage", ["build", "check", "verify", "propose"])
def test_a_stage_with_no_approved_contract_fails_and_does_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stage: str
) -> None:
    monkeypatch.chdir(tmp_path)
    assert _maga(monkeypatch, stage, "cand_missing") == 1
    assert not (tmp_path / ".maga/artifacts").exists()


def test_decide_with_no_candidate_is_a_usage_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    assert _maga(monkeypatch, "decide", "cand_missing") == 2


def test_decide_checks_the_given_repo_not_the_working_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A justfile in cwd must not leak into a decision about a different target repo."""
    here, target = tmp_path / "here", tmp_path / "target"
    here.mkdir()
    target.mkdir()
    (here / "justfile").write_text("test:\n\techo hi\n")
    candidates = here / ".maga/state/candidates"
    candidates.mkdir(parents=True)
    (candidates / f"{CANDIDATE.candidate_id}.json").write_text(CANDIDATE.model_dump_json())
    monkeypatch.chdir(here)

    seen: list[Path] = []

    def _existing_tools(repo: Path, _home: Path) -> list[str]:
        seen.append(repo)
        return []

    def _decide(_candidate: Candidate, _tools: list[str]) -> Decision:
        return Decision(outcome="rejected", reason="test")

    monkeypatch.setattr(triage, "existing_tools", _existing_tools)
    monkeypatch.setattr(triage, "decide", _decide)

    assert _maga(monkeypatch, "decide", CANDIDATE.candidate_id, str(target)) == 1
    assert seen == [target]
    assert f"target repository: {target.resolve()}" in capsys.readouterr().out


@pytest.mark.parametrize("args", [[], ["publish"], ["check"]])
def test_a_usage_error_exits_2(monkeypatch: pytest.MonkeyPatch, args: list[str]) -> None:
    with pytest.raises(SystemExit) as caught:
        _maga(monkeypatch, *args)
    assert caught.value.code == 2
