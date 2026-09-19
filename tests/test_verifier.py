"""TT-ACC, TT-G1I-001, and the revision budget of TT-REV for Gate 1."""

from collections.abc import Callable
from pathlib import Path
import shutil

import pytest

from maga.schemas import Contract, Package, Verdict
from maga.triage import GOLDEN
from maga.verifier import PROBES, Gate, Run, check, gate1_verdict, run_suite

FIXTURES = Path(__file__).parent / "fixtures" / "model_responses"
SUITE = FIXTURES / "test_start.py.txt"
PROBE = Path(__file__).parents[1] / "src" / "maga" / "gate1" / "probe_start.py"
PACKAGE = Package(
    candidate_id="cand_vite_strict_port_001",
    script_path=str(PROBE),
    skill_path=str(FIXTURES / "SKILL.md"),  # its folder is the package that the verdict hashes
    test_path=str(SUITE),
    contract=Contract.model_validate_json(GOLDEN),
)
needs_docker = pytest.mark.skipif(shutil.which("docker") is None, reason="Gate 1 needs Docker")
FAILED: Run = (1, "FAILED t.py::test_case_c", "")
PASSED: Run = (0, "", "")
NO_CONTAINER: Run = (None, "", "infrastructure: down")


def _stub(script_runs: list[Run], probe: Run = FAILED) -> tuple[list[str], Callable[..., Run]]:
    calls: list[str] = []

    def suite(_script: Path, _tests: Path, variant: str = "") -> Run:
        calls.append(variant or "script")
        return probe if variant else script_runs.pop(0)

    return calls, suite


@needs_docker
@pytest.mark.parametrize(
    ("variant", "failed"),
    [
        ("", set[str]()),  # TT-ACC-001: the positive control
        ("noop", {"test_case_a", "test_case_b", "test_case_d"}),  # TT-ACC-002
        ("autoincrement", {"test_case_c"}),  # TT-ACC-003
        ("skip_origin", {"test_a_rejected_origin"}),  # TT-ACC-004
        ("duplicate", {"test_case_d"}),  # TT-ACC-005
    ],
)
def test_acc_the_suite_passes_the_correct_script_and_fails_each_defect(
    variant: str, failed: set[str]
) -> None:
    code, out, err = run_suite(PROBE, SUITE, variant)
    assert code == (1 if failed else 0), out + err
    assert "7 passed" in out or failed
    for name in failed:
        assert any(line.startswith("FAILED") and name in line for line in out.splitlines()), out


@needs_docker
def test_g1i_001_no_network_is_reachable(tmp_path: Path) -> None:
    online = tmp_path / "test_online.py"
    online.write_text(
        "import socket\n\n\ndef test_dns_and_route():\n"
        "    socket.create_connection(('1.1.1.1', 53), timeout=3).close()\n"
    )
    code, out, err = run_suite(PROBE, online)
    assert code == 1, out + err
    assert "Network is unreachable" in out or "OSError" in out


def test_the_probes_run_before_the_script_and_a_pass_stores_the_results() -> None:
    calls, suite = _stub([(0, "PASSED t.py::test_case_a\nPASSED t.py::test_case_b", "")])
    verdict = gate1_verdict(PACKAGE, 0, suite)
    assert calls == [*PROBES, "script"]
    assert verdict.outcome == "pass"
    assert verdict.test_results == {
        "probe:noop": "rejected",
        "probe:skip_origin": "rejected",
        "test_case_a": "PASSED",
        "test_case_b": "PASSED",
    }


def test_a_suite_that_passes_a_probe_is_invalid_and_never_grades_the_script(
    tmp_path: Path,
) -> None:
    calls, suite = _stub([PASSED], probe=PASSED)
    revisions: list[str] = []
    gates: list[Gate] = [lambda package, n: gate1_verdict(package, n, suite)]
    verdict = check(PACKAGE, tmp_path, lambda log: revisions.append(log) or PACKAGE, gates)
    assert verdict.outcome == "fail"
    assert verdict.stderr_log.startswith("suite_invalid")
    assert verdict.test_results["probe:noop"] == "NOT rejected"
    assert "probe:skip_origin" not in verdict.test_results
    assert len(verdict.test_results["package_sha256"]) == 64
    assert "script" not in calls
    assert revisions == []  # no revision of the script can repair a weak suite


@pytest.mark.parametrize(
    ("runs", "outcome", "revisions"),
    [
        ([PASSED], "pass", 0),
        ([FAILED, FAILED, PASSED], "pass", 2),
        ([FAILED, FAILED, FAILED, PASSED], "pass", 3),  # TT-REV-001: the third revision is allowed
        ([FAILED, FAILED, FAILED, FAILED, PASSED], "fail", 3),  # the fourth failure ends the loop
        ([FAILED, NO_CONTAINER], "inconclusive", 1),  # infrastructure: stop, use no revision
    ],
)
def test_rev_one_first_attempt_plus_three_revisions(
    tmp_path: Path, runs: list[Run], outcome: str, revisions: int
) -> None:
    logs: list[str] = []
    _, suite = _stub(list(runs))
    gates: list[Gate] = [lambda package, n: gate1_verdict(package, n, suite)]
    verdict = check(PACKAGE, tmp_path, lambda log: logs.append(log) or PACKAGE, gates)
    assert (verdict.outcome, verdict.total_revisions, len(logs)) == (outcome, revisions, revisions)
    assert all("FAILED t.py::test_case_c" in log for log in logs)  # TT-REV-006: the failure log
    stored = (tmp_path / "verification" / f"{PACKAGE.candidate_id}_verdict.json").read_text()
    assert f'"outcome": "{outcome}"' in stored
    assert PACKAGE.contract == Contract.model_validate_json(GOLDEN)


def test_an_inconclusive_probe_run_is_not_a_failure() -> None:
    _, suite = _stub([PASSED], probe=NO_CONTAINER)
    assert gate1_verdict(PACKAGE, 0, suite).outcome == "inconclusive"


def _gates(script: list[tuple[int, str]]) -> tuple[list[int], list[Gate]]:
    """Two stub gates that replay `script`, a list of (gate number, outcome), and record the calls."""
    calls: list[int] = []

    def gate(number: int) -> Gate:
        def run(package: Package, total_revisions: int) -> Verdict:
            expected, outcome = script.pop(0)
            calls.append(number)
            assert expected == number, (
                f"gate {number} ran, but the script expected gate {expected}"
            )
            return Verdict.model_validate(
                {
                    "candidate_id": package.candidate_id,
                    "gate_number": number,
                    "outcome": outcome,
                    "total_revisions": total_revisions,
                    "test_results": {},
                    "stdout_log": f"GATE{number}_LOG",
                    "stderr_log": "",
                    "execution_duration_ms": 1,
                    "timestamp": "2026-01-05T10:00:06Z",
                }
            )

        return run

    return calls, [gate(1), gate(2)]


def test_rev_002_both_gates_share_one_counter(tmp_path: Path) -> None:
    script = [(1, "fail"), (1, "pass"), (2, "fail"), (1, "fail"), (1, "pass"), (2, "fail")]
    calls, gates = _gates(script)
    logs: list[str] = []
    verdict = check(PACKAGE, tmp_path, lambda log: logs.append(log) or PACKAGE, gates)
    assert calls == [1, 1, 2, 1, 1, 2]
    assert (verdict.gate_number, verdict.outcome, verdict.total_revisions) == (2, "fail", 3)
    assert len(logs) == 3  # with the first attempt, the generator ran 4 times
    assert [log.split("_")[0] for log in logs] == ["GATE1", "GATE2", "GATE1"]


def test_rev_003_a_revised_package_goes_through_gate_1_again(tmp_path: Path) -> None:
    calls, gates = _gates([(1, "pass"), (2, "fail"), (1, "pass"), (2, "pass")])
    verdict = check(PACKAGE, tmp_path, lambda _log: PACKAGE, gates)
    assert calls == [1, 2, 1, 2]
    assert (verdict.gate_number, verdict.outcome, verdict.total_revisions) == (2, "pass", 1)
    stored = sorted(path.name for path in (tmp_path / "verification").iterdir())
    assert stored == [
        f"{PACKAGE.candidate_id}_gate1_verdict.json",
        f"{PACKAGE.candidate_id}_gate2_verdict.json",
        f"{PACKAGE.candidate_id}_verdict.json",
    ]


def test_g2r_012_an_inconclusive_gate_2_uses_no_revision(tmp_path: Path) -> None:
    calls, gates = _gates([(1, "pass"), (2, "inconclusive")])
    revisions: list[str] = []
    verdict = check(PACKAGE, tmp_path, lambda log: revisions.append(log) or PACKAGE, gates)
    assert (calls, verdict.outcome, verdict.total_revisions, revisions) == (
        [1, 2],
        "inconclusive",
        0,
        [],
    )


@needs_docker
def test_the_harness_provides_the_tools_that_a_contract_names_as_preconditions(
    tmp_path: Path,
) -> None:
    script = tmp_path / "start.py"
    script.write_text(
        "import shutil, subprocess, sys\n"
        "assert shutil.which('node') and shutil.which('pnpm') and shutil.which('vite')\n"
        "version = subprocess.run(['node', '--version'], capture_output=True, text=True).stdout\n"
        "assert version.startswith('v'), version\n"
        "install = subprocess.run(['pnpm', '--dir', 'apps/web', 'install'], check=False)\n"
        "via = subprocess.run(['pnpm', '--dir', 'apps/web', 'exec', 'node', '--version'],\n"
        "                     capture_output=True, text=True)\n"
        "sys.exit(0 if install.returncode == 0 and via.stdout.startswith('v') else 1)\n"
    )
    suite = tmp_path / "test_tools.py"
    suite.write_text(
        "import shutil\n\n\ndef test_tools(run):\n"
        "    assert shutil.which('node') and shutil.which('pnpm')  # in the test process too\n"
        "    done = run()\n    assert done.returncode == 0, done.stderr\n"
    )
    code, out, err = run_suite(script, suite)
    assert code == 0, out + err
