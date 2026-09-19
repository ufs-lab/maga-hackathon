# MAGA

Coding agents repeatedly pay to rediscover the same project-specific fixes. Our background agent finds those repeated corrections in session transcripts, checks the evidence, and turns them into tested scripts and skills, and proposes them for human review.

## Run the pipeline

Input: Claude Code session transcripts (`~/.claude/projects/*/*.jsonl`).
Model: Gemini, through Pydantic AI, for every model call.
Output: a tested script and a `SKILL.md` that calls it, staged under `.maga/artifacts/staged/`.

```bash
cp .env.example .env                  # then add GOOGLE_API_KEY
python -m maga read                   # READ: parse and redact the transcripts into .maga/state/entries/
python -m maga find                   # FIND: rank repeated procedures, no model; prints the top 10
python -m maga decide <candidate_id>  # DECIDE: one Gemini call, then a person answers y/N on the contract
python -m maga build <candidate_id>   # BUILD: call A writes the tests, call B writes the script and skill
python -m maga check <candidate_id>   # CHECK, Gate 1: the tests in Docker with --network none
python -m maga verify <candidate_id>  # CHECK, both gates, one budget: Gate 2 is 5 fresh `claude -p` runs
python -m maga propose <candidate_id> # PROPOSE: a person approves the exact package, then a PR is opened and read back
```

Or run all of it with no question. Skills that pass Gate 1 appear below `<repo>/.claude/skills/`:

```bash
python -m maga auto [repo]            # READ, FIND, then DECIDE, BUILD, Gate 1, and install for the top 5 candidates
```

Each command exits 0 on a pass, 1 on a fail, and 2 on a usage error.
All state is JSON under `.maga/`, which Git ignores. No transcript text enters this repository.

To try it with no private data, read the three synthetic sessions:
`python -m maga read tests/fixtures/claude_code/p1/*.jsonl && python -m maga find`.

## What each control does

- **Redaction before every model call.** `maga.reader.redact` runs when an entry is stored and again in `maga.llm.ask`. Pattern redaction is incomplete protection.
- **Transcript text is data.** `maga.llm.ask` sends it inside `<data>` tags, and the instructions tell the model never to follow it.
- **Unattended mode has no approval question.** `auto` installs a skill only after its package passes the gates, and its approval record says `approved_by: automatic`.
- **A person approves the contract before BUILD.** `build` and `check` refuse unless the approval record holds the SHA-256 of the exact contract text.
- **Independent tests.** Call A sees the contract and the harness description only. A revision repeats call B only, so the tests and the contract stay fixed.
- **Gate 1 proves the suite first.** The suite must fail a no-op script and a script that skips the `Origin` check, in a container with no network, before it grades the generated script.
- **One first attempt plus three revisions.** An infrastructure failure is `inconclusive`: it uses no revision, and nothing moves forward.

## Status

| Stage | Module | State |
| :--- | :--- | :--- |
| READ | `maga.reader` | Built. Imports `user` and `assistant` lines and the messages that a person types mid-turn, joins calls to results, counts malformed lines, and skips unchanged files through import checkpoints. |
| FIND | `maga.finder` | Built. Sequence counting uses no model. Gemini judges a correction before its promotion. On real data no correction reaches 3 sessions, because the key is the exact command before the message. |
| DECIDE | `maga.triage` | Built. Verified against the real Gemini API. |
| BUILD | `maga.generator` | Built. Verified against the real Gemini API. |
| CHECK Gate 1 | `maga.verifier` | Built. Runs in CI on a clean machine. |
| CHECK Gate 2 | `maga.gate2` | Built as a host run, under one revision budget with Gate 1. Verified with 5 of 5 real agent runs. The credential-free container of `ARCHITECTURE.md` 9.2 is not built. |
| PROPOSE | `maga.publisher` | Built and tested against a local Git remote. Not verified: a real pull request through `gh`. |
| Unattended | `maga.auto` | Built. No approval question: only a package that passes Gate 1 is installed, and the record says `approved_by: automatic`. |

Logfire traces each Gemini call when `LOGFIRE_TOKEN` is set. Not verified: a trace in a real Logfire project.

Not built: chunking of long sessions with a model route for procedures that exact matching misses, the Pydantic AI Gateway route, the Modal stretch, the Gate 2 sandbox, and the worktree scenario.

## Development

```bash
bash scripts/setup-tools.sh   # once: tools, locked environment, git hooks
just check                    # lock check, Ruff, Pyright strict, pytest
```

Read `CONTRIBUTING.md` for the hooks and the PR format, and `AGENTS.md` for the agent workflow.
`llms.txt` indexes the documentation.
