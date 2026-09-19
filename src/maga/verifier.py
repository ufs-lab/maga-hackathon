"""CHECK, Gate 1 only: the acceptance suite in a container with no network.

The suite grades the generated script only after it has failed the known-bad probes.
"""

from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from functools import cache
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import time
from typing import Literal

from maga.publisher import package_hash
from maga.schemas import Package, Verdict

MAX_TOTAL_REVISIONS = 3
IMAGE = "maga-gate1"
_DOCKERFILE = "FROM python:3.13-slim\nRUN pip install --no-cache-dir pytest==9.1.1\n"
_TIMEOUT_SECONDS = 120
_HARNESS = Path(__file__).parent / "gate1"
# ponytail: found from the source checkout. Ship it as package data if maga is ever installed.
_DEMO_REPO = Path(__file__).parents[2] / "fixtures" / "demo-monorepo"
# The suite must fail each of these before its verdict on the generated script counts.
# ponytail: the probes are the Vite script with defects, so they fit the golden contract
# only. A second workflow needs its own probe script.
PROBES = ("noop", "skip_origin")
_DOCKER_FAILURE = 125  # 125 to 127 are Docker's own failures, not the exit code of pytest
_PYTEST_FAILED = 1  # pytest: tests ran and some failed. 2 to 5 mean the suite itself is unusable.
_RESULT = re.compile(r"^(PASSED|FAILED|ERROR) \S*?::(\S+)", re.MULTILINE)

Outcome = Literal["pass", "fail", "inconclusive"]
Run = tuple[int | None, str, str]  # pytest exit code (None: the container did not give one)


def _docker(args: list[str], stdin: str | None = None) -> Run:
    try:
        done = subprocess.run(
            ["docker", *args],
            input=stdin,
            capture_output=True,
            text=True,
            timeout=_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        return None, "", f"infrastructure: {error}"
    code = None if done.returncode >= _DOCKER_FAILURE else done.returncode
    return code, done.stdout, done.stderr


@cache
def _image() -> Run:
    """Build the Gate 1 image one time in each process. Docker's layer cache makes a rebuild cheap."""
    return _docker(["build", "-q", "-t", IMAGE, "-"], _DOCKERFILE)


def run_suite(script: Path, tests: Path, variant: str = "") -> Run:
    """Run `tests` against `script` in a fresh container with no network."""
    built = _image()
    if built[0] != 0:
        return None, built[1], f"infrastructure: the Gate 1 image did not build\n{built[2]}"
    with tempfile.TemporaryDirectory(prefix="maga_gate1_") as temp:
        # The checks live outside the staged workspace, and the container mounts them read-only.
        suite = Path(temp) / "suite"
        shutil.copytree(_HARNESS, suite, ignore=shutil.ignore_patterns("__pycache__"))
        shutil.copytree(_DEMO_REPO, suite / "demo-monorepo")  # the `repo` fixture copies it
        shutil.copy(tests, suite / "test_start.py")
        shutil.copy(script, Path(temp) / "start.py")
        return _docker(
            [
                "run",
                "--rm",
                "--network",
                "none",
                "-v",
                f"{suite}:/gate:ro",
                "-v",
                f"{temp}/start.py:/pkg/start.py:ro",
                "-e",
                "MAGA_SCRIPT=/pkg/start.py",
                "-e",
                f"MAGA_VARIANT={variant}",
                "-w",
                "/work",
                IMAGE,
                "python",
                "-m",
                "pytest",
                "-rA",
                "-q",
                "-p",
                "no:cacheprovider",
                "/gate/test_start.py",
            ]
        )


def gate1_verdict(
    package: Package, total_revisions: int, suite: Callable[..., Run] = run_suite
) -> Verdict:
    started = time.monotonic()
    tests = Path(package.test_path)
    results: dict[str, str] = {}
    outcome: Outcome = "pass"
    code, out, err = 0, "", ""
    for variant in PROBES:
        code, out, err = suite(_HARNESS / "probe_start.py", tests, variant)
        results[f"probe:{variant}"] = "rejected" if code == _PYTEST_FAILED else "NOT rejected"
        if code is None:
            outcome = "inconclusive"
        elif code != _PYTEST_FAILED:
            outcome, err = (
                "fail",
                f"suite_invalid: the suite did not fail the {variant} probe\n{err}",
            )
        if outcome != "pass":
            break
    if outcome == "pass":
        code, out, err = suite(Path(package.script_path), tests)
        results |= {name: status for status, name in _RESULT.findall(out)}
        outcome = "inconclusive" if code is None else "pass" if code == 0 else "fail"
    return Verdict(
        candidate_id=package.candidate_id,
        gate_number=1,
        outcome=outcome,
        total_revisions=total_revisions,
        test_results=results,
        stdout_log=out[-4000:],
        stderr_log=err[-4000:],
        execution_duration_ms=int((time.monotonic() - started) * 1000),
        timestamp=datetime.now(UTC),
    )


Gate = Callable[[Package, int], Verdict]  # a package and total_revisions give one verdict


def _one_pass(
    package: Package, total_revisions: int, gates: Sequence[Gate], folder: Path
) -> Verdict:
    """Run the gates in order until one does not pass. Store each verdict."""
    verdict = None
    for gate in gates:
        verdict = gate(package, total_revisions)
        # PROPOSE publishes a package only with a Gate 2 pass for this exact content.
        verdict.test_results["package_sha256"] = package_hash(Path(package.skill_path).parent)
        text = verdict.model_dump_json(indent=2)
        name = f"{package.candidate_id}_gate{verdict.gate_number}_verdict.json"
        (folder / name).write_text(text)
        (folder / f"{package.candidate_id}_verdict.json").write_text(text)  # the latest one
        if verdict.outcome != "pass":
            break
    if verdict is None:
        message = "check needs a minimum of one gate"
        raise ValueError(message)
    return verdict


def check(
    package: Package, state: Path, revise: Callable[[str], Package], gates: Sequence[Gate]
) -> Verdict:
    """Run the gates in order, with one revision budget for all of them (ARCHITECTURE.md 6).

    One first attempt plus MAX_TOTAL_REVISIONS revisions. A revised package starts again at the
    first gate. The contract and the tests stay fixed.
    """
    total_revisions = 0
    folder = state / "verification"
    folder.mkdir(parents=True, exist_ok=True)
    while True:
        verdict = _one_pass(package, total_revisions, gates, folder)
        # An inconclusive run uses no revision. A weak suite is not a script defect: no revision fixes it.
        if (
            verdict.outcome != "fail"
            or total_revisions >= MAX_TOTAL_REVISIONS
            or verdict.stderr_log.startswith("suite_invalid")
        ):
            return verdict
        package = revise(f"{verdict.stdout_log}\n{verdict.stderr_log}")
        total_revisions += 1
