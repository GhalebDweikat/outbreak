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
