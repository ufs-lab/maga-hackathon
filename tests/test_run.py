"""Run the real CLI orchestration with synthetic input and stub model/gate boundaries."""

from datetime import UTC, datetime
import json
from pathlib import Path
import sys
from typing import Literal

import pytest

from maga import __main__ as cli
from maga import finder, gate2, generator, triage, verifier
from maga.schemas import Candidate, Contract, Package, Verdict

P1 = sorted((Path(__file__).parent / "fixtures/claude_code/p1").glob("*.jsonl"))


@pytest.fixture
def pipeline(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    monkeypatch.chdir(tmp_path)
    seen: list[str] = []

    def decide(candidate: Candidate, _tools: list[str]) -> triage.Decision:
        seen.append("DECIDE")
        contract = Contract.model_validate_json(triage.GOLDEN).model_copy(
            update={"candidate_id": candidate.candidate_id}
        )
        return triage.Decision(outcome="generate", reason="synthetic", contract=contract)

    def approve(_prompt: str) -> str:
        seen.append("APPROVE")
        return "y"

    def tests(_contract: Contract, staged: Path) -> Path:
        seen.append("TESTS")
        path = staged / "tests/test_start.py"
        path.parent.mkdir(parents=True)
        path.write_text("def test_synthetic(): assert True\n")
        return path

    def script(contract: Contract, staged: Path, _failure: str = "") -> Package:
        seen.append("SCRIPT")
        path = staged / "scripts/start.py"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("print('synthetic, never executed')\n")
        (staged / "SKILL.md").write_text("Synthetic test artifact\n")
        return Package(
            candidate_id=contract.candidate_id,
            contract=contract,
            script_path=str(path),
            test_path=str(staged / "tests/test_start.py"),
            skill_path=str(staged / "SKILL.md"),
        )

    def gate1(package: Package, revisions: int) -> Verdict:
        seen.append("GATE1")
        return _verdict(package, revisions, 1, "pass")

    def reuse(package: Package, repo: Path, _runner: gate2.Runner, revisions: int) -> Verdict:
        assert repo == Path("fixtures/demo-monorepo")
        seen.append("GATE2")
        return _verdict(package, revisions, 2, "pass")

    monkeypatch.setattr(triage, "decide", decide)
    monkeypatch.setattr("builtins.input", approve)
    monkeypatch.setattr(generator, "write_tests", tests)
    monkeypatch.setattr(generator, "write_script", script)
    monkeypatch.setattr(verifier, "gate1_verdict", gate1)
    monkeypatch.setattr(gate2, "gate2_verdict", reuse)
    return seen


def _verdict(
    package: Package, revisions: int, gate: Literal[1, 2], outcome: verifier.Outcome
) -> Verdict:
    return Verdict(
        candidate_id=package.candidate_id,
        gate_number=gate,
        outcome=outcome,
        total_revisions=revisions,
        test_results={},
        stdout_log="",
        stderr_log="",
        execution_duration_ms=0,
        timestamp=datetime.now(UTC),
    )


def _run(monkeypatch: pytest.MonkeyPatch, *extra: str) -> int:
    monkeypatch.setattr(sys, "argv", ["maga", "run", *map(str, P1), *extra])
    return cli.main()


def test_run_preserves_approval_and_gate_order(
    pipeline: list[str], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _run(monkeypatch) == 0
    assert pipeline == ["DECIDE", "APPROVE", "TESTS", "SCRIPT", "GATE1", "GATE2"]
    output = capsys.readouterr().out
    for stage in ("READ", "FIND", "DECIDE", "BUILD", "CHECK Gate 1", "CHECK Gate 2"):
        assert stage in output
    candidate = next(Path(".maga/state/candidates").glob("*.json")).stem
    assert f"Selected candidate: {candidate}" in output
    assert f".maga/artifacts/staged/{candidate}" in output
    assert ".maga/state" in output
    assert "Pipeline passed" in output
    assert (
        json.loads(Path(f".maga/state/verification/{candidate}_verdict.json").read_text())[
            "gate_number"
        ]
        == 2
    )


def test_run_denied_approval_stops_before_build(
    pipeline: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    def deny(_prompt: str) -> str:
        return "n"

    monkeypatch.setattr("builtins.input", deny)
    assert _run(monkeypatch) == 1
    assert pipeline == ["DECIDE"]
    assert not Path(".maga/artifacts").exists()
    approval = next(Path(".maga/state/approvals").glob("*.json"))
    assert json.loads(approval.read_text())["approved"] is False


def test_run_unknown_selection_is_usage_error(
    pipeline: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    assert _run(monkeypatch, "--candidate-id", "cand_missing") == 2
    assert pipeline == []


def test_run_empty_candidates_stops(
    pipeline: list[str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    empty = tmp_path / "empty.jsonl"
    empty.write_text("")
    monkeypatch.setattr(sys, "argv", ["maga", "run", str(empty)])
    assert cli.main() == 1
    assert pipeline == []


@pytest.mark.parametrize("selection", [None, "cand_second"])
def test_run_selects_current_rank_or_explicit_candidate(
    pipeline: list[str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    selection: str | None,
) -> None:
    original = finder.run

    def find(state: Path) -> list[Candidate]:
        template = original(state)[0]
        candidates = finder.rank(
            [
                template.model_copy(update={"candidate_id": "cand_second"}),
                template.model_copy(update={"candidate_id": "cand_first"}),
            ]
        )
        for candidate in candidates:
            (state / "candidates" / f"{candidate.candidate_id}.json").write_text(
                candidate.model_dump_json()
            )
        return candidates

    monkeypatch.setattr(finder, "run", find)
    args = ["--candidate-id", selection] if selection else []
    assert _run(monkeypatch, *args) == 0
    assert pipeline[-1] == "GATE2"
    expected = selection or "cand_first"
    assert f"Selected candidate: {expected}" in capsys.readouterr().out
    assert Path(f".maga/state/approvals/{expected}.json").exists()


@pytest.mark.parametrize("stage", ["read", "find", "decide", "build", "approval"])
def test_run_stage_exception_stops_with_failure(
    pipeline: list[str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    stage: str,
) -> None:
    def fail(*_args: object) -> None:
        if stage == "approval":
            raise EOFError
        message = "synthetic stage failure"
        raise ValueError(message)

    target, name = {
        "read": (cli.reader, "read"),
        "find": (finder, "run"),
        "decide": (triage, "decide"),
        "build": (generator, "write_tests"),
        "approval": (__import__("builtins"), "input"),
    }[stage]
    monkeypatch.setattr(target, name, fail)
    assert _run(monkeypatch) == 1
    assert "GATE1" not in pipeline
    assert "SCRIPT" not in pipeline
    assert "Pipeline passed" not in capsys.readouterr().out


@pytest.mark.parametrize("outcome", ["fail", "inconclusive"])
@pytest.mark.parametrize("gate", [1, 2])
def test_run_stops_on_gate_outcome(
    pipeline: list[str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    gate: Literal[1, 2],
    outcome: verifier.Outcome,
) -> None:
    def reject(package: Package, revisions: int) -> Verdict:
        pipeline.append(f"STOP{gate}")
        return _verdict(package, revisions, gate, outcome)

    def reuse(package: Package, _repo: Path, _runner: gate2.Runner, revisions: int) -> Verdict:
        return reject(package, revisions)

    if gate == 1:
        monkeypatch.setattr(verifier, "gate1_verdict", reject)
    else:
        monkeypatch.setattr(gate2, "gate2_verdict", reuse)
    assert _run(monkeypatch) == 1
    attempts = 4 if outcome == "fail" else 1
    assert pipeline.count(f"STOP{gate}") == attempts
    assert pipeline.count("SCRIPT") == attempts
    if gate == 1:
        assert "GATE2" not in pipeline
    output = capsys.readouterr().out
    assert f'"outcome": "{outcome}"' in output
    assert "Pipeline passed" not in output


@pytest.mark.parametrize("outcome", ["rejected", "reuse_existing", "fix_at_source", "clarify"])
def test_run_non_generate_decision_stops(
    pipeline: list[str],
    monkeypatch: pytest.MonkeyPatch,
    outcome: str,
) -> None:
    def decide(_candidate: Candidate, _tools: list[str]) -> triage.Decision:
        return triage.Decision.model_validate({"outcome": outcome, "reason": "synthetic"})

    monkeypatch.setattr(triage, "decide", decide)
    assert _run(monkeypatch) == 1
    assert pipeline == []
