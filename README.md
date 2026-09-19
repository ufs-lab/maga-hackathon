[Click here for the one-page demo presentation](https://your-team-already-solved-this.ledger-rocket.here.now/) (ledgerrocket.com sign-in required).

# MAGA

Coding agents repeatedly pay to rediscover the same project-specific fixes. Our background agent finds those repeated corrections in session transcripts, checks the evidence, and turns them into tested scripts and skills, and proposes them for human review.

## Quickstart: synthetic sessions, no API key

Install [Git](https://git-scm.com/downloads) and [uv](https://docs.astral.sh/uv/getting-started/installation/). MAGA uses Python 3.13; `uv` can download it if needed. Run these commands in a shell that expands `*.jsonl` (for example, Bash):

```bash
git clone https://github.com/ufs-lab/maga-hackathon.git
cd maga-hackathon
uv sync --locked
uv run --locked python -m maga --help
uv run --locked python -m maga read tests/fixtures/claude_code/p1/*.jsonl
uv run --locked python -m maga find
```

On a fresh checkout, this imports three synthetic sessions (21 entries, no malformed lines) and finds one repeated procedure. It needs no `.env`, model key, Docker, or Claude Code. Keep `LOGFIRE_TOKEN` unset for this local-only smoke test. Installation downloads dependencies; the smoke commands do not call a model.

`uv sync` creates `.venv` without activating it. Use `uv run --locked` from the repository root for every command below. Local state and staged files live under `.maga/`, which Git ignores. Never commit private transcripts.

## Run the pipeline

Input: Claude Code session transcripts. `read` with no paths scans `~/.claude/projects/*/*.jsonl`; use explicit paths to limit the import.
Model: `google:gemini-3.8-flash` through Pydantic AI for correction confirmation, decisions, generation, and repairs. Gate 2 uses Claude Code separately.
Output: a script, acceptance tests, and a `SKILL.md`, staged under `.maga/artifacts/staged/`.

Generation needs a Google API key. Copy `.env.example` to the ignored `.env`, then set `GOOGLE_API_KEY` there. The CLI loads it automatically. `find` also needs the key when corrections reach the three-session threshold; the synthetic quickstart contains only a repeated procedure. Optional `LOGFIRE_TOKEN` enables tracing of model calls.

Gate 1 needs Docker installed and a running daemon. Its first image build needs network access to fetch Python and pytest; test containers run with `--network none`. Repairs can call Gemini, so `check` and `verify` can incur model charges.

Gate 2 needs an installed, authenticated `claude` CLI and access to its model API. It runs generated code on the developer host, inherits the host environment, and has no credential-free sandbox or network restriction. Review the package before running `verify`, and use a disposable environment without unrelated credentials. A temporary directory is not a security boundary.

Replace `CANDIDATE_ID` with the ID printed by `find`. `decide` asks a person to approve the contract and acceptance checks before `build` can proceed.

```bash
cp .env.example .env                 # then set GOOGLE_API_KEY in .env
uv run --locked python -m maga read  # imports your local transcripts; not the synthetic smoke test
uv run --locked python -m maga find
uv run --locked python -m maga decide CANDIDATE_ID
uv run --locked python -m maga build CANDIDATE_ID
uv run --locked python -m maga check CANDIDATE_ID   # Gate 1 only
uv run --locked python -m maga verify CANDIDATE_ID  # Gate 1, then Gate 2
```

`verify` defaults to `fixtures/demo-monorepo`; an optional second argument selects another demo directory. It runs both gates under one budget: the first attempt plus at most three revisions. Each revision starts again at Gate 1. Running `check` first is optional, not a prerequisite for `verify`. Gate 2 requires at least four of five fresh agent runs to discover and use the skill.

After verification, `uv run --locked python -m maga propose CANDIDATE_ID` asks for approval of the exact package before publishing. It requires a matching Gate 2 pass, Git push access, and authenticated `gh` access to create and read back the PR. It writes the skill into the demo directory in the current Git repository. Use only a designated demo repository, never an unsolicited upstream target.

The interactive commands exit 0 on success, 1 on failure, and 2 on a usage error. `read` returns 1 when it imports no files; `find` returns 1 when it finds no candidates.

For unattended operation, `uv run --locked python -m maga auto` imports local transcripts, finds candidates, and processes the top five pending candidates without asking for approval. It needs the Google key and Docker. Skills that pass Gate 1 are installed below `.claude/skills/` in the current directory; an optional `repo` argument selects the destination. This CLI path does not run Gate 2 or publish a PR. Read its per-candidate results: an exit code of 0 can include failed candidates or zero installed skills.

## Run end to end with contract approval

After `uv sync --locked`, configure `GOOGLE_API_KEY` in the ignored `.env`, start Docker,
and authenticate the `claude` CLI. Gate 2 executes generated code on the developer host,
not in a security sandbox. Use a disposable environment for this run.

```bash
uv run --locked python -m maga run tests/fixtures/claude_code/p1/*.jsonl
```

`run` imports the inputs, mines the stored entries, prints the highest-ranked candidate ID,
and asks you to approve its contract and acceptance checks before BUILD.
Omit the paths to use the same transcript discovery as `read`.
Use `--candidate-id <candidate_id>` to select another candidate from that FIND result;
use `--demo-repo <path>` to change the Gate 2 fixture from `fixtures/demo-monorepo`.
Both CHECK gates share the existing three-revision budget. Rejection, a failed stage,
or an inconclusive gate stops the run with a nonzero exit. The command prints state and
artifact locations, and never installs or publishes the package.

## What each control does

- **Redaction before every Gemini call.** `maga.reader.redact` runs when an entry is stored and again in `maga.llm.ask`. Pattern redaction is incomplete protection.
- **Transcript text is data.** `maga.llm.ask` sends it inside `<data>` tags, and the instructions tell the model never to follow it.
- **Interactive contract approval.** `decide` asks a person to approve the contract and acceptance checks. `auto` records `approved_by: automatic` without asking. `build`, `check`, and `verify` require approval bound to the SHA-256 of the exact contract text.
- **Independent tests.** Call A sees the contract and the harness description only. A revision repeats call B only, so the tests and the contract stay fixed.
- **Gate 1 proves the suite first.** The suite must fail a no-op script and a script that skips the `Origin` check, in a container with no network, before it grades the generated script.
- **One first attempt plus three revisions per verification invocation.** An infrastructure failure is `inconclusive`: it uses no revision, and nothing moves forward.

## Status

| Stage | Module | State |
| :--- | :--- | :--- |
| READ | `maga.reader` | Built. Imports `user` and `assistant` lines, joins calls to results, counts malformed lines. |
| FIND | `maga.finder` | Built. Counts repetition locally; Gemini confirms corrections after a keyword prefilter and three-session threshold. |
| DECIDE | `maga.triage` | Built. Calls Gemini and records human contract approval. |
| BUILD | `maga.generator` | Built. Calls Gemini separately for tests and script/skill generation. |
| CHECK Gate 1 | `maga.verifier` | Built. Runs acceptance tests and bad-script probes in Docker. |
| CHECK Gate 2 | `maga.gate2` | Built as an unconfined host run. The credential-free container of `ARCHITECTURE.md` 9.2 is not built. |
| PROPOSE | `maga.publisher` | Built. Checks package approval and matching verification, opens a PR, and reads back its head. |

Optional Logfire instrumentation and import checkpoints are built. The Pydantic AI Gateway route, chunking of long sessions, the Modal stretch, and the worktree scenario are not built.

## Development

Install [gitleaks](https://github.com/gitleaks/gitleaks#installing) first on systems without Homebrew. The setup script installs missing `uv` and `just`, installs missing gitleaks through Homebrew when available, syncs the locked environment, and installs Git hooks. It does not install Docker or Claude Code.

```bash
bash scripts/setup-tools.sh   # once: tools, locked environment, git hooks
just check                    # lock check, Ruff, Pyright strict, pytest (including Docker tests)
```

Read `CONTRIBUTING.md` for the hooks and the PR format, and `AGENTS.md` for the agent workflow.
`llms.txt` indexes the documentation.
