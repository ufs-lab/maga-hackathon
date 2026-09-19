"""BUILD: two independent model calls from one approved Contract.

Call A writes the acceptance tests and sees the contract only.
Call B writes the script and the skill. A revision repeats call B only, so the tests stay fixed.
"""

from hashlib import sha256
import json
from pathlib import Path
import re

from pydantic import BaseModel, field_validator
from pydantic_ai.models import Model

from maga import llm
from maga.schemas import Contract, Package

# The repository constraints that both calls receive. Gate 1 provides exactly this harness.
CONSTRAINTS = """
Repository constraints:
- The script is `scripts/start.py`: Python 3.13, standard library only, no network beyond localhost.
- `python scripts/start.py` starts the workflow. `python scripts/start.py stop` stops only the
  process that the script started, by the PID in the tracking file of the contract.
- The working directory is the repository root. `packages/config/ports.json` holds
  {"frontend_ports": [...]}. The frontend is in `apps/web`. `vite` is on PATH; start it in
  `apps/web` as `vite --port <port> --strictPort`.
- `node`, `pnpm`, and `vite` are on PATH where the tests run. Check a tool that the contract
  names as a precondition with `shutil.which`; never install a package and never use the network.
- The last line of stdout is one JSON object. Error reasons: all_permitted_ports_exhausted,
  cors_origin_rejected, precondition_failed.
Test harness (pytest fixtures that Gate 1 provides; a test must not define them):
- `repo`: Path of a fresh repository root with the files above.
- `backend`: the backend stub on http://localhost:4000. `backend.origins` lists each Origin
  header that `/api/health` received. It answers 200 for a permitted Origin and 403 otherwise.
  The backend runs only inside a test that requests this fixture, so each test that starts the
  workflow must request `backend`. Set `backend.allowed = []` to make it reject every Origin.
- Each test gets a fresh `repo`, and the harness stops the process in the PID tracking file
  after each test. Ports 5173 and 5174 are free at the start of each test.
- `occupy(port)`: binds the port with an unrelated listener and returns its socket.
- `repo` is a plain `pathlib.Path`. `run`, `backend`, and `occupy` are separate fixtures: request
  each one as a test argument and call `run()`. Never write `repo.run()` or `repo.path`.
- `run(*args)`: runs the script under test in `repo` and returns subprocess.CompletedProcess
  with text stdout and stderr. Never import the script and never read its source.
"""
_TESTS = (
    "Write one pytest module of acceptance tests for the contract in the data. "
    "Write one test for each acceptance check, and one for each invariant that a test can observe. "
    "A script that does nothing, and a script that skips a postcondition, must fail the module. "
    "Test the contract only, never an implementation detail." + CONSTRAINTS
)
# The shape of each generated SKILL.md. Script._follows_the_template refuses an answer without it.
SKILL_TEMPLATE = """\
---
name: <workflow_name>
description: Use when <user goals and trigger phrases>. Never <the manual action to avoid>.
---

## When to use

<One line for each situation in which the agent must run this skill.>

## Run

Run this exact command from the repository root.

```bash
python .claude/skills/<workflow_name>/scripts/start.py
```

## Output

The last line of stdout is one JSON object.

- Ready: `<the ready JSON of the contract, with example values>`. <What the agent does next.>
- Error: `{"status": "error", "reason": "precondition_failed"}`, with one of these reasons.
  Report the reason to the user and stop. Never work around it.
  - `all_permitted_ports_exhausted`: <what it means, and what the agent must not do>
  - `cors_origin_rejected`: <what it means, and what the agent must not do>
  - `precondition_failed`: <what it means, and what the agent must not do>

## Rules

<One bullet for each contract invariant: a direct negative instruction that starts with Never.>

## Stop

```bash
python .claude/skills/<workflow_name>/scripts/start.py stop
```
"""
_HEADINGS = ("## When to use", "## Run", "## Output", "## Rules", "## Stop")
_SCRIPT = (
    "Write the script and the SKILL.md for the contract in the data. "
    "SKILL.md follows the template at the end of these instructions: the same two front matter "
    "keys, the same five headings in the same order, and the same commands. "
    "Fill every `<...>` placeholder from the contract and leave no `<...>` text in the answer. "
    "Where the contract itself has such a field, write an example value, such as 5173 for a port. "
    "`name` is the `workflow_name` of the contract. "
    "Claude Code reads only the `description` to decide whether to load the skill, so make it "
    "specific: one line, third person, no colon, under 1024 characters. "
    "It starts with `Use when`. It names each concrete user goal and trigger phrase. "
    "It ends with what the agent must never do by hand. An example for a dev server: "
    "`Use when the user wants to start the web frontend, run the dev server, start Vite, check "
    "that the backend accepts requests, or fix a CORS or port problem. Never start vite by hand.` "
    "Claude Code installs the skill at `.claude/skills/<workflow_name>/`, so the agent runs the "
    "script by that path from the repository root, never as `python scripts/start.py`. "
    "If the data has a `gate_failure`, repair the script or the skill. "
    "The contract is fixed." + CONSTRAINTS + "SKILL.md template:\n" + SKILL_TEMPLATE
)


def _python(code: str) -> str:
    try:
        compile(code, "<generated>", "exec")
    except SyntaxError as error:
        message = f"the generated code is not Python: {error}"
        raise ValueError(message) from error
    return code


class Tests(BaseModel):
    test_code: str
    _is_python = field_validator("test_code")(_python)


class Script(BaseModel):
    script: str
    skill_md: str
    _is_python = field_validator("script")(_python)

    @field_validator("skill_md")
    @classmethod
    def _follows_the_template(cls, text: str) -> str:
        head = text.split("---")[1] if text.startswith("---") and text.count("---") > 1 else ""
        keys = dict(re.findall(r"^([\w-]+): *(\S.*)$", head, re.MULTILINE))
        lacks = [key for key in ("name", "description") if key not in keys]
        starts = re.match(r"[\"']?Use (this skill )?when\b", keys.get("description", ""))
        places = [text.find(f"\n{heading}\n") for heading in _HEADINGS]
        missing = [heading for heading in _HEADINGS if f"\n{heading}\n" not in text]
        run = text.partition("\n## Run\n")[2].partition("\n## Output\n")[0]
        command = re.search(r"\.claude/skills/\S+/scripts/start\.py", run)
        holes = sorted(set(re.findall(r"<\w[^<>\n]*>", text)))
        faults = [
            (lacks, f"the front matter at the top lacks: {', '.join(lacks)}"),
            (
                not starts,
                "the `description:` line must start with `Use when` or `Use this skill when`",
            ),
            (missing, f"these headings are missing: {', '.join(missing)}"),
            (places != sorted(places), f"the heading order must be {', '.join(_HEADINGS)}"),
            (
                not command,
                (
                    "the `## Run` section must hold the command "
                    "`python .claude/skills/NAME/scripts/start.py`, where NAME is the workflow_name"
                ),
            ),
            (holes, f"fill each placeholder, this text is still in the answer: {holes}"),
        ]
        for fault, message in faults:
            if fault:
                raise ValueError(message)
        return text


def approved_contract(state: Path, candidate_id: str) -> Contract:
    """The contract, only when a person approved exactly this text (AGENTS.md 2.4)."""
    approval = json.loads((state / "approvals" / f"{candidate_id}.json").read_text())
    text = (state / "contracts" / f"{candidate_id}.json").read_text()
    if (
        not approval["approved"]
        or approval["contract_sha256"] != sha256(text.encode()).hexdigest()
    ):
        message = f"no human approval for this exact contract of {candidate_id}"
        raise PermissionError(message)
    return Contract.model_validate_json(text)


def write_tests(contract: Contract, staged: Path, model: Model | str = llm.MODEL) -> None:
    answer = llm.ask(Tests, _TESTS, contract.model_dump_json(), model)
    (staged / "tests").mkdir(parents=True, exist_ok=True)
    (staged / "tests" / "test_start.py").write_text(answer.test_code)


def write_script(
    contract: Contract, staged: Path, gate_failure: str = "", model: Model | str = llm.MODEL
) -> Package:
    data = {"contract": contract.model_dump(mode="json")} | (
        {"gate_failure": gate_failure} if gate_failure else {}
    )
    answer = llm.ask(Script, _SCRIPT, json.dumps(data), model)
    (staged / "scripts").mkdir(parents=True, exist_ok=True)
    (staged / "scripts" / "start.py").write_text(answer.script)
    (staged / "SKILL.md").write_text(answer.skill_md)
    return Package(
        candidate_id=contract.candidate_id,
        script_path=str(staged / "scripts" / "start.py"),
        skill_path=str(staged / "SKILL.md"),
        test_path=str(staged / "tests" / "test_start.py"),
        contract=contract,
    )
