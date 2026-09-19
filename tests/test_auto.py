"""Unattended mode: only a package that passes the gates is installed, and no record claims a person."""

import json
from pathlib import Path

from pydantic_ai.messages import ModelMessage, ModelResponse, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
import pytest

from maga.auto import Auto
from maga.finder import find
from maga.reader import parse_session
from maga.schemas import Package, Verdict
from maga.triage import GOLDEN
from maga.verifier import Gate

P1 = sorted((Path(__file__).parent / "fixtures" / "claude_code" / "p1").glob("*.jsonl"))
SKILL = (Path(__file__).parent / "fixtures" / "model_responses" / "SKILL.md").read_text()


def _state(tmp_path: Path) -> tuple[Path, str]:
    (candidate,) = find([parse_session(path)[0] for path in P1])
    folder = tmp_path / "state" / "candidates"
    folder.mkdir(parents=True)
    (folder / f"{candidate.candidate_id}.json").write_text(candidate.model_dump_json())
    return tmp_path / "state", candidate.candidate_id


def _model(outcome: str, candidate_id: str, calls: list[str]) -> FunctionModel:
    contract = json.loads(GOLDEN) | {"candidate_id": candidate_id}
    decision: dict[str, object] = {"outcome": outcome, "reason": "recorded"}
    if outcome == "generate":
        decision["contract"] = contract
    answers: dict[str, dict[str, object]] = {
        "outcome": decision,
        "test_code": {"test_code": "def test_a(): pass\n"},
        "script": {"script": "print('ok')\n", "skill_md": SKILL},
    }

    def respond(_messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        tool = info.output_tools[0]
        kind = next(key for key in answers if key in tool.parameters_json_schema["properties"])
        calls.append(kind)
        return ModelResponse(parts=[ToolCallPart(tool.name, answers[kind])])

    return FunctionModel(respond)


def _gate(outcome: str) -> list[Gate]:
    def gate(package: Package, total_revisions: int) -> Verdict:
        return Verdict.model_validate(
            {
                "candidate_id": package.candidate_id,
                "gate_number": 1,
                "outcome": outcome,
                "total_revisions": total_revisions,
                "test_results": {},
                "stdout_log": "LOG",
                "stderr_log": "",
                "execution_duration_ms": 1,
                "timestamp": "2026-01-05T10:00:06Z",
            }
        )

    return [gate]


def test_a_passing_package_is_installed_with_no_question_and_only_once(tmp_path: Path) -> None:
    state, candidate_id = _state(tmp_path)
    calls: list[str] = []
    lines: list[str] = []
    repo = tmp_path / "repo"
    run = Auto(
        state,
        tmp_path / "staged",
        repo,
        _gate("pass"),
        _model("generate", candidate_id, calls),
        lines.append,
    )
    (skill,) = run.run()
    assert skill == repo / ".claude" / "skills" / "vite-safe-dev-server"
    assert (skill / "SKILL.md").read_text() == SKILL
    assert (skill / "scripts" / "start.py").read_text() == "print('ok')\n"
    assert not (skill / "tests").exists()
    approval = json.loads((state / "approvals" / f"{candidate_id}.json").read_text())
    assert (approval["approved"], approval["approved_by"]) == (True, "automatic")
    assert calls == ["outcome", "test_code", "script"]
    assert run.run() == []  # the candidate is decided, so a second run asks the model nothing
    assert calls == ["outcome", "test_code", "script"]


@pytest.mark.parametrize("gate", ["fail", "inconclusive"])
def test_a_package_that_does_not_pass_is_never_installed(tmp_path: Path, gate: str) -> None:
    state, candidate_id = _state(tmp_path)
    lines: list[str] = []
    run = Auto(
        state,
        tmp_path / "staged",
        tmp_path / "repo",
        _gate(gate),
        _model("generate", candidate_id, []),
        lines.append,
    )
    assert run.run() == []
    assert not (tmp_path / "repo").exists()
    assert "not installed" in lines[-1]


def test_a_rejected_candidate_costs_one_model_call_and_builds_nothing(tmp_path: Path) -> None:
    state, candidate_id = _state(tmp_path)
    calls: list[str] = []
    lines: list[str] = []
    run = Auto(
        state,
        tmp_path / "staged",
        tmp_path / "repo",
        _gate("pass"),
        _model("rejected", candidate_id, calls),
        lines.append,
    )
    assert run.run() == []
    assert calls == ["outcome"]
    assert not (tmp_path / "staged").exists()
    approval = json.loads((state / "approvals" / f"{candidate_id}.json").read_text())
    assert (approval["approved"], approval["approved_by"]) == (False, "automatic")


def test_a_decided_candidate_stays_decided_when_find_runs_again(tmp_path: Path) -> None:
    state, candidate_id = _state(tmp_path)
    calls: list[str] = []
    lines: list[str] = []
    model = _model("rejected", candidate_id, calls)
    Auto(state, tmp_path / "staged", tmp_path / "repo", _gate("pass"), model, lines.append).run()
    _state_again = find([parse_session(path)[0] for path in P1])[0]  # FIND writes it as pending
    (state / "candidates" / f"{candidate_id}.json").write_text(_state_again.model_dump_json())
    Auto(state, tmp_path / "staged", tmp_path / "repo", _gate("pass"), model, lines.append).run()
    assert calls == ["outcome"]  # one model call in total, not one for each run
