"""Unattended mode: decide, build, and check the top candidates, and install what passes.

No person approves a contract here. The gates are the control: a skill is installed only after
its package passes every gate, and each approval record says `approved_by: automatic`.
"""

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from pydantic_ai.exceptions import AgentRunError
from pydantic_ai.models import Model

from maga import finder, generator, llm, triage, verifier
from maga.gate2 import install_skill
from maga.schemas import Candidate, Package

TOP = 5  # candidates for each run, in rank order


@dataclass
class Auto:
    """One unattended run: where it reads and writes, which gates it uses, and which model."""

    state: Path
    staged_root: Path
    repo: Path  # the skills appear below `repo`/.claude/skills/
    gates: Sequence[verifier.Gate]
    model: Model | str = llm.MODEL
    report: Callable[[str], None] = print

    def run(self) -> list[Path]:
        """Return the skill folders that this run installed."""
        files = sorted((self.state / "candidates").glob("*.json"))
        ranked = finder.rank(Candidate.model_validate_json(path.read_bytes()) for path in files)
        tools = triage.existing_tools(self.repo, Path.home())
        installed: list[Path] = []
        # The approval record marks a decided candidate. FIND rewrites each candidate file as
        # `pending` on every run, so `triage_status` cannot carry that fact.
        decided = {path.stem for path in (self.state / "approvals").glob("*.json")}
        for candidate in [c for c in ranked if c.candidate_id not in decided][:TOP]:
            try:
                skill = self._one(candidate, tools)
            except (AgentRunError, ValueError) as error:  # one bad answer must not stop the run
                self.report(f"{candidate.candidate_id}  error: {str(error)[:200]}")
                continue
            installed += [skill] if skill else []
        return installed

    def _one(self, candidate: Candidate, tools: list[str]) -> Path | None:
        decision = triage.decide(candidate, tools, self.model)
        automatic = decision.contract is not None
        triage.record(self.state, candidate, decision, approved=automatic, approved_by="automatic")
        self.report(f"{candidate.candidate_id}  {decision.outcome}: {decision.reason[:140]}")
        if not decision.contract:
            return None
        contract = generator.approved_contract(self.state, candidate.candidate_id)
        staged = self.staged_root / candidate.candidate_id
        generator.write_tests(contract, staged, self.model)
        package = generator.write_script(contract, staged, model=self.model)

        def revise(gate_failure: str) -> Package:
            return generator.write_script(contract, staged, gate_failure, self.model)

        verdict = verifier.check(package, self.state, revise, self.gates)
        name = candidate.candidate_id
        if verdict.outcome != "pass":
            self.report(f"{name}  gate {verdict.gate_number} {verdict.outcome}: not installed")
            return None
        skill = install_skill(package, self.repo)
        self.report(f"{name}  passed after {verdict.total_revisions} revisions: {skill}")
        return skill
