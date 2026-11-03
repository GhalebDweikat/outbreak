"""Runs the adapter through the real SDK against a mock transport. No request leaves the machine."""

import json

import pytest

anthropic = pytest.importorskip("anthropic")
httpx = pytest.importorskip("httpx2")

from outbreak.budget import SpendCap
from outbreak.claude import ClaudeModel
from outbreak.episode import run_episode
from outbreak.models import ModelError, ModelFatal
from outbreak.scenario import SYSTEM, Trial
from outbreak.world import TOOLS

USAGE = {
    "input_tokens": 100,
    "output_tokens": 50,
    "cache_creation_input_tokens": 20,
    "cache_read_input_tokens": 400,
}


def message(content, stop_reason="tool_use", **extra):
    return {
        "id": "msg_1",
        "type": "message",
        "role": "assistant",
        "model": "claude-haiku-5-5",
        "content": content,
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": USAGE,
        **extra,
    }


THINKING = {"type": "thinking", "thinking": "Summary of my reasoning.", "signature": "sig-123"}
LIST = {"type": "tool_use", "id": "toolu_1", "name": "list_files", "input": {}}
SUBMIT = {
    "type": "tool_use",
    "id": "toolu_2",
    "name": "submit_status",
    "input": {"status": "blocked", "only_solution_changed": True, "summary": "No cover for S03."},
}


def client(responses):
    """A real SDK client whose transport answers from a list and keeps each request body."""
    bodies = []

    def handle(request):
        bodies.append(json.loads(request.content))
        status, body = responses.pop(0)
        return httpx.Response(status, json=body)

    sdk = anthropic.Anthropic(
        api_key="not-a-real-key",
        http_client=anthropic.DefaultHttpxClient(transport=httpx.MockTransport(handle)),
        max_retries=0,
    )
    return sdk, bodies


def play(responses, cap=None, name="claude-haiku-5-5"):
    sdk, bodies = client(responses)
    record = run_episode(Trial(0, 4), ClaudeModel(name, client=sdk), cap)
    return record, bodies


def test_the_key_only_goes_to_anthropic_whatever_the_environment_says(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "not-a-real-key")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://somewhere-else.example.com")
    model = ClaudeModel("claude-haiku-5-5")
    assert model._client.base_url.host == "api.anthropic.com"


def test_a_full_episode_through_the_sdk():
    record, bodies = play(
        [(200, message([THINKING, LIST])), (200, message([THINKING, SUBMIT]))]
    )
    assert record["end"] == "reported"
    assert record["calls"] == 2
    assert record["replies"][0]["blocks"][0]["thinking"] == "Summary of my reasoning."


def test_the_request_is_built_as_documented():
    _, bodies = play([(200, message([SUBMIT]))])
    body = bodies[0]
    assert body["model"] == "claude-haiku-5-5"
    assert body["system"] == SYSTEM
    assert [tool["name"] for tool in body["tools"]] == [tool["name"] for tool in TOOLS]
    assert body["cache_control"] == {"type": "ephemeral"}
    assert body["thinking"] == {"type": "adaptive", "display": "summarized"}
    assert body["output_config"] == {"effort": "medium"}


def test_nothing_is_sent_that_haiku_rejects_or_that_would_swap_the_model():
    _, bodies = play([(200, message([SUBMIT]))])
    for field in ("temperature", "top_p", "top_k", "fallbacks", "tool_choice"):
        assert field not in bodies[0]


def first_request(name, effort="medium"):
    """The body and headers of the first request a model sends."""
    seen = []

    def handle(request):
        seen.append((json.loads(request.content), request.headers))
        return httpx.Response(200, json=message([SUBMIT]))

    sdk = anthropic.Anthropic(
        api_key="not-a-real-key",
        http_client=anthropic.DefaultHttpxClient(transport=httpx.MockTransport(handle)),
        max_retries=0,
    )
    run_episode(Trial(0, 4), ClaudeModel(name, effort, client=sdk))
    return seen[0]


def test_haiku_4_5_gets_a_thinking_budget_and_nothing_it_rejects():
    body, headers = first_request("claude-haiku-4-5", "low")
    assert body["model"] == "claude-haiku-4-5-20251001"
    assert body["thinking"] == {"type": "enabled", "budget_tokens": 1024}
    assert "output_config" not in body
    assert "interleaved" not in headers.get("anthropic-beta", "")


def test_opus_4_5_takes_both_a_budget_and_an_effort_level():
    body, headers = first_request("claude-opus-4-5", "high")
    assert body["model"] == "claude-opus-4-5-20251101"
    assert body["thinking"] == {"type": "enabled", "budget_tokens": 6000}
    assert body["output_config"] == {"effort": "high"}
    assert "interleaved-thinking-2025-05-14" in headers["anthropic-beta"]


@pytest.mark.parametrize("name", ["claude-sonnet-4-6", "claude-opus-4-6"])
def test_the_4_6_models_think_adaptively_without_the_newer_display_field(name):
    body, headers = first_request(name)
    assert body["model"] == name
    assert body["thinking"] == {"type": "adaptive"}
    assert body["output_config"] == {"effort": "medium"}
    assert "anthropic-beta" not in headers


def test_every_thinking_budget_leaves_room_to_answer():
    from outbreak.claude import BUDGETS, MAX_TOKENS

    assert all(1024 <= budget < MAX_TOKENS for budget in BUDGETS.values())


def test_an_effort_level_an_old_model_cannot_map_is_refused():
    with pytest.raises(ValueError):
        ClaudeModel("claude-haiku-4-5", "max", client=object())


def test_the_settings_record_the_budget_only_where_one_is_sent():
    assert ClaudeModel("claude-haiku-4-5", client=object()).settings()["thinking_budget"] == 4000
    assert "thinking_budget" not in ClaudeModel("claude-haiku-5-5", client=object()).settings()


def test_the_settings_record_the_dated_id_that_was_sent():
    assert ClaudeModel("claude-opus-4-5", client=object()).settings()["api_id"] == "claude-opus-4-5-20251101"
    assert "api_id" not in ClaudeModel("claude-sonnet-4-6", client=object()).settings()


def test_every_model_the_adapter_knows_has_a_price():
    from outbreak.budget import PRICES
    from outbreak.claude import API_IDS, MANUAL_THINKING, SUMMARIZED_BY_DEFAULT

    assert MANUAL_THINKING | SUMMARIZED_BY_DEFAULT | set(API_IDS) <= set(PRICES)


def test_thinking_blocks_go_back_unchanged():
    _, bodies = play([(200, message([THINKING, LIST])), (200, message([SUBMIT]))])
    assistant = bodies[1]["messages"][1]
    assert assistant["role"] == "assistant"
    assert assistant["content"][0]["signature"] == "sig-123"
    assert assistant["content"][0]["thinking"] == "Summary of my reasoning."
    results = bodies[1]["messages"][2]
    assert results["role"] == "user"
    assert results["content"][0]["tool_use_id"] == "toolu_1"


def test_the_model_that_answered_is_recorded():
    record, _ = play([(200, message([SUBMIT]))])
    assert record["replies"][0]["served_by"] == "claude-haiku-5-5"


def test_real_usage_reaches_the_ledger():
    cap = SpendCap()
    record, _ = play([(200, message([SUBMIT]))], cap)
    expected = (100 * 0.10 + 50 * 0.50 + 20 * 0.125 + 400 * 0.01) / 1_000_000
    assert record["cost_usd"] == pytest.approx(expected)
    assert cap.spent == pytest.approx(expected)


def test_a_refusal_is_recorded_as_a_refusal():
    refusal = message(
        [],
        stop_reason="refusal",
        stop_details={"type": "refusal", "category": "cyber", "explanation": "Declined."},
    )
    record, _ = play([(200, refusal)])
    assert record["end"] == "refused"
    assert record["replies"][0]["stop_detail"] == "cyber"


def error(kind, text, code=None):
    payload = {"type": kind, "message": text}
    if code:
        payload["details"] = {"error_code": code}
    return {"type": "error", "error": payload}


@pytest.mark.parametrize(
    ("status", "body"),
    [
        (401, error("authentication_error", "invalid x-api-key")),
        (400, error("invalid_request_error", "You have reached your specified API usage limits")),
        (404, error("not_found_error", "model not found")),
        (429, error("rate_limit_error", "limit", "enforced_spend_limit_reached")),
        (402, error("billing_error", "out of credit")),
    ],
)
def test_failures_every_later_call_would_hit_stop_the_run(status, body):
    with pytest.raises(ModelFatal):
        play([(status, body)])


@pytest.mark.parametrize(
    ("status", "body"),
    [
        (429, error("rate_limit_error", "slow down")),
        (500, error("api_error", "server trouble")),
        (529, error("overloaded_error", "overloaded")),
    ],
)
def test_passing_failures_only_drop_the_episode(status, body):
    with pytest.raises(ModelError):
        play([(status, body)])
