"""TT-CHK: chunks respect the budget, lose no entry, keep a call with its result, overlap, and
never send a whole transcript; the model route dedups by entry id."""

from itertools import pairwise

from pydantic_ai.messages import ModelMessage, ModelResponse, ToolCallPart, UserPromptPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from maga.episodes import chunks, find, tokens
from maga.schemas import Entry


def _long(session_id: str, count: int = 40) -> list[Entry]:
    """FX-CC-LONG: one human message and `count` commands, each with a result."""
    items: list[tuple[str, str]] = [("user_input", "Run the steps.")]
    for n in range(1, count + 1):
        items += [("tool_call", f"echo step-{n:02d}"), ("tool_result", f"step-{n:02d}")]
    return [
        Entry.model_validate(
            {
                "entry_id": f"{session_id}-{index}",
                "session_id": session_id,
                "step_index": index,
                "source": "user" if kind == "user_input" else "model",
                "entry_type": kind,
                "timestamp": "2026-01-05T10:00:00Z",
                "tool_name": "Bash" if kind == "tool_call" else None,
                "command_line": text if kind == "tool_call" else None,
                "exit_code": 0 if kind != "user_input" else None,
                "content": text if kind == "user_input" else None,
                "sanitized_output": text if kind == "tool_result" else None,
            }
        )
        for index, (kind, text) in enumerate(items)
    ]


SESSION = _long("sess-l")
BUDGET = tokens(SESSION) // 4


def test_chk_001_002_no_chunk_exceeds_the_budget_and_no_entry_is_lost() -> None:
    parts = chunks(SESSION, budget=BUDGET)
    assert len(parts) >= 4
    assert all(tokens(part) <= BUDGET for part in parts)
    assert {e.entry_id for part in parts for e in part} == {e.entry_id for e in SESSION}
    assert len(SESSION) == 81


def test_chk_003_a_tool_call_stays_with_its_result() -> None:
    parts = chunks(SESSION, budget=BUDGET)
    calls = [e for e in SESSION if e.entry_type == "tool_call"]
    for call in calls:
        result_id = f"sess-l-{call.step_index + 1}"
        assert any({call.entry_id, result_id} <= {e.entry_id for e in part} for part in parts), (
            call.entry_id
        )


def test_chk_004_adjacent_chunks_overlap_by_one_call_and_result_pair() -> None:
    parts = chunks(SESSION, budget=BUDGET, overlap=1)
    for before, after in pairwise(parts):
        assert [e.entry_id for e in after[:2]] == [e.entry_id for e in before[-2:]]
        assert after[0].entry_type == "tool_call"


def _recorded(seen: list[str]) -> FunctionModel:
    """Names `run-the-steps` in every chunk, with the ids of all its calls. Adjacent chunks
    share one call through the overlap, so without dedup by entry id one session gives one
    occurrence for each chunk."""

    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        parts = [part for message in messages for part in message.parts]
        text = "\n".join(str(part.content) for part in parts if isinstance(part, UserPromptPart))
        seen.append(text)
        tool = info.output_tools[0]
        if "groups" in tool.parameters_json_schema["properties"]:
            names = sorted({line.split('"')[3] for line in text.splitlines() if '"name"' in line})
            answer: dict[str, object] = {"groups": [{"name": "run-the-steps", "members": names}]}
            return ModelResponse(parts=[ToolCallPart(tool.name, answer)])
        ids = [line[1 : line.index("]")] for line in text.splitlines() if " Bash" in line]
        procedures = (
            [{"name": "run-the-steps", "goal": "run", "steps": ["echo step-05"], "entry_ids": ids}]
            if ids
            else []
        )
        return ModelResponse(parts=[ToolCallPart(tool.name, {"procedures": procedures})])

    return FunctionModel(respond)


def test_chk_005_006_evidence_is_deduplicated_and_no_request_holds_a_whole_session() -> None:
    seen: list[str] = []
    sessions = [_long(f"sess-{n}") for n in ("a", "b", "c")]
    # Small chunks: each session gives several overlapping chunks, each reported as the procedure.
    (candidate,) = find(sessions, _recorded(seen), budget=BUDGET)
    assert candidate.candidate_id.startswith("cand_episode_")
    assert candidate.evidence.session_ids == ["sess-a", "sess-b", "sess-c"]
    assert len(seen) > 3 * 4  # several chunks for each session, so dedup has work to do
    assert (candidate.frequency, candidate.evidence.observed_occurrences) == (3, 3)
    texts = [f"echo step-{n:02d}" for n in range(1, 41)]
    assert all(sum(t in request for t in texts) < 40 for request in seen)
    assert all(len(request) // 4 <= BUDGET + 200 for request in seen[:-1])
