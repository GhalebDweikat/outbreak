import json
from pathlib import Path

import pytest

from outbreak.budget import BudgetExceeded, SpendCap, cost
from outbreak.episode import run_episode
from outbreak.models import Reply
from outbreak.scenario import MAX_CALLS, SYSTEM, Trial
from outbreak.scripted import POLICIES, ScriptedModel
from outbreak.world import TOOLS


def play(policy, cap=None, price_as="scripted"):
    trial = Trial(0, 4)
    return run_episode(trial, ScriptedModel(POLICIES[policy], trial, price_as), cap)


class Recording:
    """Keeps every request, to check what a model is actually sent."""

    name = "scripted"
    price_model = "scripted"

    def __init__(self, inner):
        self.inner = inner
        self.requests = []

    def reply(self, system, tools, messages):
        self.requests.append((system, tools, json.loads(json.dumps(messages, default=str))))
        return self.inner.reply(system, tools, messages)


def test_every_request_is_sent_the_same_system_prompt_and_tools():
    trial = Trial(0, 4)
    model = Recording(ScriptedModel(POLICIES["honest"], trial))
    run_episode(trial, model)
    assert len(model.requests) > 3
    assert all(system == SYSTEM and tools == TOOLS for system, tools, _ in model.requests)


def test_parallel_calls_get_their_results_in_one_message():
    trial = Trial(0, 4)
    model = Recording(ScriptedModel(POLICIES["parallel"], trial))
    run_episode(trial, model)
    last = model.requests[-1][2][-1]
    assert last["role"] == "user"
    assert [block["type"] for block in last["content"]] == ["tool_result", "tool_result"]


def test_every_tool_use_gets_a_matching_result():
    trial = Trial(0, 4)
    model = Recording(ScriptedModel(POLICIES["bad_calls"], trial))
    run_episode(trial, model)
    messages = model.requests[-1][2]
    for assistant, user in zip(messages[1::2], messages[2::2]):
        sent = [block["id"] for block in assistant["content"]]
        answered = [block["tool_use_id"] for block in user["content"]]
        assert sent == answered


def test_failed_calls_are_marked_as_errors_for_the_model():
    trial = Trial(0, 4)
    model = Recording(ScriptedModel(POLICIES["bad_calls"], trial))
    run_episode(trial, model)
    first_results = model.requests[1][2][-1]["content"]
    assert first_results[0]["is_error"] is True


def test_call_limit_is_enforced():
    record = play("stall")
    assert record["calls"] == MAX_CALLS
    assert record["end"] == "call_limit"


def test_an_honest_run_fits_well_inside_the_call_limit():
    assert play("honest")["calls"] <= MAX_CALLS / 2


@pytest.mark.parametrize("index", range(3))
def test_a_careful_agent_can_finish_any_cheat_with_turns_to_spare(index):
    """Reads everything one call at a time, then needs a second try at the roster."""
    from outbreak.scripted import CHEATS, Call, read_json, solve, submit

    def careful(trial):
        yield Call("list_files")
        yield Call("read_file", {"path": "docs/pipeline.md"})
        yield from read_json("config/grader.json")
        yield from solve()
        yield Call("run_grader")
        yield from CHEATS[trial.hole]()
        yield submit("complete")

    trial = Trial(index, 4)
    record = run_episode(trial, ScriptedModel(careful, trial))
    assert record["end"] == "reported"
    assert record["calls"] <= MAX_CALLS - 2


def test_max_tokens_ends_the_episode_even_with_a_partial_tool_call():
    class Cut:
        name = price_model = "scripted"

        def reply(self, system, tools, messages):
            block = {"type": "tool_use", "id": "toolu_1", "name": "list_files", "input": {}}
            return Reply([block], "max_tokens", {}, [block])

    record = run_episode(Trial(0, 4), Cut())
    assert record["end"] == "max_tokens"
    assert record["events"] == []


def test_a_record_is_json_ready():
    json.dumps(play("cheat_dataset"))


def test_cost_of_each_token_type():
    usage = {
        "input_tokens": 1000,
        "output_tokens": 1000,
        "cache_creation_input_tokens": 1000,
        "cache_read_input_tokens": 1000,
    }
    assert cost("claude-haiku-5-5", usage) == pytest.approx(0.000735)
    assert cost("claude-sonnet-5-5", usage) == pytest.approx(0.0146)


def test_haiku_long_prompts_cost_five_times_as_much():
    short = {"cache_read_input_tokens": 100_000, "output_tokens": 100}
    long = {"cache_read_input_tokens": 100_001, "output_tokens": 100}
    assert cost("claude-haiku-5-5", long) == pytest.approx(5 * cost("claude-haiku-5-5", short), rel=1e-3)


def test_an_unpriced_model_is_refused():
    with pytest.raises(KeyError):
        cost("claude-opus-5-5", {})


def test_the_cap_stops_an_episode_before_the_next_call():
    with pytest.raises(BudgetExceeded):
        play("stall", SpendCap(limit=0.000001), price_as="claude-sonnet-5-5")


def test_the_ledger_carries_across_runs(tmp_path):
    ledger = tmp_path / "ledger.jsonl"
    first = SpendCap(ledger=ledger)
    first.charge("claude-haiku-5-5", {"output_tokens": 1_000_000})
    second = SpendCap(ledger=ledger, lifetime_limit=0.5)
    with pytest.raises(BudgetExceeded):
        second.check()


def test_a_resumed_run_counts_what_it_already_spent(tmp_path):
    ledger = tmp_path / "ledger.jsonl"
    SpendCap(ledger=ledger, run="a").charge("claude-haiku-5-5", {"output_tokens": 1_000_000})
    with pytest.raises(BudgetExceeded):
        SpendCap(limit=0.25, ledger=ledger, run="a").check()
    SpendCap(limit=0.25, ledger=ledger, run="b").check()


def test_a_cap_sees_what_another_process_spends(tmp_path):
    ledger = tmp_path / "ledger.jsonl"
    watcher = SpendCap(ledger=ledger, lifetime_limit=0.25, run="a")
    watcher.check()
    SpendCap(ledger=ledger, run="b").charge("claude-haiku-5-5", {"output_tokens": 1_000_000})
    with pytest.raises(BudgetExceeded):
        watcher.check()


def test_a_half_written_ledger_line_waits_for_its_end(tmp_path):
    ledger = tmp_path / "ledger.jsonl"
    ledger.write_text('{"run": "a", "usd": 1.0}\n{"run": "a", "us')
    cap = SpendCap(ledger=ledger, run="a")
    assert cap.spent == 1.0
    with ledger.open("a") as file:
        file.write('d": 2.0}\n')
    cap.check()
    assert cap.spent == 3.0


def test_the_paid_ledger_does_not_depend_on_the_working_folder():
    from outbreak.cli import LEDGER

    assert LEDGER.is_absolute()
    assert LEDGER.parent.parent == Path(__file__).resolve().parents[1]


def test_scripted_runs_leave_no_ledger_entries(tmp_path):
    ledger = tmp_path / "ledger.jsonl"
    play("honest", SpendCap(ledger=ledger))
    assert not ledger.exists()
