"""`python -m maga <stage>`. Exit 0 pass, 1 fail, 2 usage."""

import argparse
import json
from pathlib import Path
import sys
from typing import TYPE_CHECKING

import logfire
from pydantic_ai.exceptions import AgentRunError, UserError

if TYPE_CHECKING:
    from collections.abc import Callable

from maga import auto, episodes, finder, gate2, generator, llm, publisher, reader, triage, verifier
from maga.schemas import Candidate, Package, Verdict

STATE = Path(".maga/state")
STAGED = Path(".maga/artifacts/staged")


def main() -> int:
    parser = argparse.ArgumentParser(prog="maga")
    stages = parser.add_subparsers(dest="stage", required=True)
    read = stages.add_parser("read", help="import Claude Code session files")
    read.add_argument("paths", nargs="*", type=Path, help="default: ~/.claude/projects/*/*.jsonl")
    stages.add_parser("find", help="rank the repeated procedures in the stored entries")
    decide = stages.add_parser("decide", help="triage one candidate and ask for contract approval")
    decide.add_argument("candidate_id")
    decide.add_argument("repo", type=Path, nargs="?", default=Path())
    build = stages.add_parser("build", help="generate tests, script, and skill from the contract")
    build.add_argument("candidate_id")
    check = stages.add_parser("check", help="Gate 1: acceptance tests in a container, no network")
    check.add_argument("candidate_id")
    verify = stages.add_parser("verify", help="Gate 1, then Gate 2, with one revision budget")
    verify.add_argument("candidate_id")
    verify.add_argument("demo_repo", type=Path, nargs="?", default=Path("fixtures/demo-monorepo"))
    propose = stages.add_parser("propose", help="ask for package approval, then open the PR")
    propose.add_argument("candidate_id")
    propose.add_argument("demo_repo", type=Path, nargs="?", default=Path("fixtures/demo-monorepo"))
    unattended = stages.add_parser(
        "auto", help="read, find, then build and install with no question"
    )
    unattended.add_argument("repo", type=Path, nargs="?", default=Path())
    model_route = stages.add_parser(
        "episodes", help="FIND with the model: Gemini names the procedures in the given sessions"
    )
    model_route.add_argument("paths", nargs="+", type=Path, help="session files to read")
    unattended.add_argument(
        "paths", nargs="*", type=Path, help="session files; default: ~/.claude/projects/*/*.jsonl"
    )
    run = stages.add_parser("run", help="READ through both CHECK gates, with human approval")
    run.add_argument("paths", nargs="*", type=Path, help="default: ~/.claude/projects/*/*.jsonl")
    run.add_argument("--candidate-id", help="default: highest-ranked candidate from FIND")
    run.add_argument("--demo-repo", type=Path, default=Path("fixtures/demo-monorepo"))
    args = parser.parse_args()

    llm.load_env()
    # With no LOGFIRE_TOKEN, nothing leaves the machine.
    logfire.configure(send_to_logfire="if-token-present", service_name="maga", console=False)

    stage: str = args.stage
    dispatch: dict[str, Callable[[], int]] = {
        "auto": lambda: _auto_stage(args.repo, args.paths),
        "episodes": lambda: _episodes(args.paths),
        "run": lambda: _run_stage(args.paths, args.candidate_id, args.demo_repo),
        "read": lambda: _read(args.paths),
        "find": _find,
        "verify": lambda: _check(args.candidate_id, args.demo_repo),
        "propose": lambda: _propose(args.candidate_id, args.demo_repo),
        "decide": lambda: _decide(args.candidate_id, args.repo),
        "build": lambda: _build(args.candidate_id),
        "check": lambda: _check(args.candidate_id),
    }
    return dispatch[stage]()


def _episodes(paths: list[Path]) -> int:
    _read(paths)
    sessions = [reader.parse_session(path)[0] for path in paths]
    candidates = episodes.run(STATE, sessions)
    for c in candidates:
        sys.stdout.write(f"{c.frequency} sessions  {c.title}\n")
        sys.stdout.write(
            "".join(f"      {line[:150]}\n" for line in c.normalized_template.splitlines())
        )
    sys.stdout.write(f"{len(candidates)} episode candidates in {STATE / 'candidates'}\n")
    return 0 if candidates else 1


def _auto_stage(repo: Path, paths: list[Path]) -> int:
    _read(paths)
    _find()
    installed = auto.Auto(STATE, STAGED, repo, [verifier.gate1_verdict]).run()
    sys.stdout.write(f"{len(installed)} skills installed below {repo / '.claude/skills'}\n")
    return 0


def _run_stage(paths: list[Path], candidate_id: str | None, demo_repo: Path) -> int:
    try:
        code = _run(paths, candidate_id, demo_repo)
    except (OSError, ValueError, EOFError, AgentRunError, UserError) as error:
        sys.stderr.write(f"Pipeline stopped: {type(error).__name__}: {error}\n")
        code = 1
    sys.stdout.write(f"State: {STATE.resolve()}\nArtifacts: {STAGED.resolve()}\n")
    return code


def _read(paths: list[Path]) -> int:
    paths = paths or sorted((Path.home() / ".claude/projects").glob("*/*.jsonl"))
    report = reader.read(paths, STATE)
    sys.stdout.write(json.dumps(report) + "\n")
    return 0 if report["files"] else 1


def _find() -> int:
    candidates = _find_candidates()
    return 0 if candidates else 1


def _find_candidates() -> list[Candidate]:
    candidates = finder.run(STATE)
    for place, c in enumerate(candidates[:10], 1):
        sys.stdout.write(
            f"{place:>2}. {c.evidence_type:<10} sessions={c.frequency:<3} "
            f"occurrences={c.evidence.observed_occurrences:<4} {c.candidate_id}\n"
            + "".join(f"      {line[:150]}\n" for line in c.normalized_template.splitlines())
        )
    sys.stdout.write(f"{len(candidates)} candidates in {STATE / 'candidates'}\n")
    return candidates


def _run(paths: list[Path], candidate_id: str | None, demo_repo: Path) -> int:
    sys.stdout.write(f"State: {STATE.resolve()}\n")
    sys.stdout.write("READ\n")
    if _read(paths):
        return 1
    sys.stdout.write("FIND\n")
    candidates = _find_candidates()
    if not candidates:
        return 1
    if candidate_id is None:
        candidate_id = candidates[0].candidate_id
    elif candidate_id not in {c.candidate_id for c in candidates}:
        sys.stderr.write(f"No candidate {candidate_id!r} in this FIND result\n")
        return 2
    sys.stdout.write(f"Selected candidate: {candidate_id}\n")
    sys.stdout.write(f"Artifacts (if built): {(STAGED / candidate_id).resolve()}\n")
    sys.stdout.write("DECIDE\n")
    code = _decide(candidate_id, Path())
    if code:
        sys.stdout.write("Pipeline stopped at DECIDE\n")
        return code
    sys.stdout.write("BUILD\n")
    code = _build(candidate_id)
    if code:
        sys.stdout.write("Pipeline stopped at BUILD\n")
        return code
    code = _check(candidate_id, demo_repo)
    sys.stdout.write("Pipeline passed\n" if code == 0 else "Pipeline stopped at CHECK\n")
    return code


def _build(candidate_id: str) -> int:
    try:
        contract = generator.approved_contract(STATE, candidate_id)
        generator.write_tests(contract, STAGED / candidate_id)
        package = generator.write_script(contract, STAGED / candidate_id)
    except (FileNotFoundError, PermissionError, UserError) as error:
        sys.stderr.write(f"{error}\n")
        return 1
    sys.stdout.write(package.model_dump_json(indent=2, exclude={"contract"}) + "\n")
    return 0


def _check(candidate_id: str, demo_repo: Path | None = None) -> int:
    """Gate 1, and with a demo repository also Gate 2, under one revision budget."""
    staged = STAGED / candidate_id

    def revise(gate_failure: str) -> Package:
        # The approval is read again for each revision, so an edited contract ends the loop.
        contract = generator.approved_contract(STATE, candidate_id)
        return generator.write_script(contract, staged, gate_failure)

    def acceptance(package: Package, total_revisions: int) -> Verdict:
        sys.stdout.write(f"CHECK Gate 1 (revision {total_revisions})\n")
        return verifier.gate1_verdict(package, total_revisions)

    def reuse(package: Package, total_revisions: int) -> Verdict:
        sys.stdout.write(f"CHECK Gate 2 (revision {total_revisions})\n")
        repo = demo_repo or Path()
        return gate2.gate2_verdict(package, repo, gate2.claude_runner, total_revisions)

    gates: list[verifier.Gate] = [acceptance, *([reuse] if demo_repo else [])]
    try:
        verdict = verifier.check(_package(candidate_id), STATE, revise, gates)
    except (FileNotFoundError, PermissionError, UserError) as error:
        sys.stderr.write(f"{error}\n")
        return 1
    sys.stdout.write(verdict.model_dump_json(indent=2, exclude={"stdout_log"}) + "\n")
    if verdict.outcome != "pass":
        sys.stdout.write(verdict.stdout_log[-2000:] + "\n")
    return 0 if verdict.outcome == "pass" else 1


def _package(candidate_id: str) -> Package:
    staged = STAGED / candidate_id
    return Package(
        candidate_id=candidate_id,
        script_path=str(staged / "scripts" / "start.py"),
        skill_path=str(staged / "SKILL.md"),
        test_path=str(staged / "tests" / "test_start.py"),
        contract=generator.approved_contract(STATE, candidate_id),
    )


def _propose(candidate_id: str, demo_repo: Path) -> int:
    staged = STAGED / candidate_id
    try:
        contract = generator.approved_contract(STATE, candidate_id)
    except (FileNotFoundError, PermissionError) as error:
        sys.stderr.write(f"{error}\n")
        return 1
    files = sorted(str(p.relative_to(staged)) for p in staged.rglob("*") if p.is_file())
    sys.stdout.write(
        f"package {publisher.package_hash(staged)}\n" + "".join(f"  {f}\n" for f in files)
    )
    if input("Approve exactly this package for a pull request? [y/N] ").strip().lower() != "y":
        sys.stdout.write("not approved: nothing is published\n")
        return 1
    publisher.approve_package(STATE, candidate_id, staged)
    target = demo_repo / ".claude" / "skills" / contract.workflow_name
    try:
        destination = publisher.Destination(Path.cwd(), target)
        url = publisher.publish(STATE, candidate_id, staged, destination, publisher.GITHUB)
    except publisher.PublishError as error:
        sys.stderr.write(f"not published: {error}\n")
        return 1
    sys.stdout.write(f"{url}\n")
    return 0


def _decide(candidate_id: str, repo: Path) -> int:
    path = STATE / "candidates" / f"{candidate_id}.json"
    if not path.exists():
        sys.stderr.write(f"no candidate at {path}; run `python -m maga find` first\n")
        return 2
    candidate = Candidate.model_validate_json(path.read_bytes())
    try:
        decision = triage.decide(candidate, triage.existing_tools(repo, Path.home()))
    except UserError as error:  # no API key: Pydantic AI names the variable it needs
        sys.stderr.write(f"{error}\n")
        return 1
    sys.stdout.write(
        f"target repository: {repo.resolve()}\n"
        f"outcome: {decision.outcome}\nreason: {decision.reason}\n"
    )
    approved = False
    if decision.contract:
        sys.stdout.write(decision.contract.model_dump_json(indent=2) + "\n")
        answer = input("Approve this contract and its acceptance checks? [y/N] ")
        approved = answer.strip().lower() == "y"
    approval = triage.record(STATE, candidate, decision, approved=approved)
    sys.stdout.write(f"recorded in {approval}\n")
    return 0 if approved else 1


if __name__ == "__main__":
    sys.exit(main())
