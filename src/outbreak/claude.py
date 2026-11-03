"""Claude behind the Model interface. This is the only file that imports the SDK.

Refusal fallbacks are left off on purpose. A fallback would swap the model under test
partway through an episode, and the kill test has to count refusals as refusals.
"""

from __future__ import annotations

from .models import ModelError, ModelFatal, Reply

# Named here so ANTHROPIC_BASE_URL can't send the API key somewhere else.
API = "https://api.anthropic.com"
MAX_TOKENS = 8000
SPEND_LIMIT_CODE = "enforced_spend_limit_reached"

# Models from before adaptive thinking take a thinking budget in place of an effort level.
# Anthropic's extended-thinking docs, read 2026-11-02: of these, only Opus 4.5 also takes
# effort, and it thinks between tool calls only with a beta header. Haiku 4.5 can't think
# between tool calls at all. Sonnet 4.5 isn't on this key's model list, so it's left out.
MANUAL_THINKING = {"claude-haiku-4-5", "claude-opus-4-5"}
BUDGETS = {"low": 1024, "medium": 4000, "high": 6000}
EFFORT_WITH_BUDGET = {"claude-opus-4-5"}
INTERLEAVED = {"claude-opus-4-5"}
INTERLEAVED_BETA = "interleaved-thinking-2025-05-14"

# The model list shows the 4.5 models only under dated IDs, so they're sent that way. The
# Haiku 4.5 pilot ran on the short name and was served by claude-haiku-4-5-20251001.
API_IDS = {"claude-haiku-4-5": "claude-haiku-4-5-20251001", "claude-opus-4-5": "claude-opus-4-5-20251101"}

# The 4.6 models summarize thinking by default. The `display` field arrived with Opus 4.7.
SUMMARIZED_BY_DEFAULT = {"claude-sonnet-4-6", "claude-opus-4-6"}


class ClaudeModel:
    def __init__(self, name: str, effort: str = "medium", client=None):
        import anthropic

        if name in MANUAL_THINKING and effort not in BUDGETS:
            raise ValueError(f"{name} takes effort {', '.join(BUDGETS)}, not {effort}")
        self._sdk = anthropic
        self._client = client or anthropic.Anthropic(base_url=API, max_retries=6, timeout=120.0)
        self.name = name
        self.price_model = name
        self.effort = effort

    def thinking_options(self) -> dict:
        if self.name in SUMMARIZED_BY_DEFAULT:
            return {"thinking": {"type": "adaptive"}, "output_config": {"effort": self.effort}}
        if self.name not in MANUAL_THINKING:
            return {
                "thinking": {"type": "adaptive", "display": "summarized"},
                "output_config": {"effort": self.effort},
            }
        options: dict = {"thinking": {"type": "enabled", "budget_tokens": BUDGETS[self.effort]}}
        if self.name in EFFORT_WITH_BUDGET:
            options["output_config"] = {"effort": self.effort}
        if self.name in INTERLEAVED:
            options["extra_headers"] = {"anthropic-beta": INTERLEAVED_BETA}
        return options

    def reply(self, system: str, tools: list[dict], messages: list[dict]) -> Reply:
        sdk = self._sdk
        try:
            response = self._client.messages.create(
                model=API_IDS.get(self.name, self.name),
                max_tokens=MAX_TOKENS,
                cache_control={"type": "ephemeral"},
                system=system,
                tools=tools,
                messages=messages,
                **self.thinking_options(),
            )
        except (
            sdk.AuthenticationError,
            sdk.BadRequestError,
            sdk.NotFoundError,
            sdk.PermissionDeniedError,
        ) as error:
            raise ModelFatal(str(error)) from error
        except sdk.RateLimitError as error:
            if SPEND_LIMIT_CODE in str(error.body):
                raise ModelFatal("the organisation's monthly spend cap was reached") from error
            raise ModelError(str(error)) from error
        except sdk.APIStatusError as error:
            if error.status_code == 402:
                raise ModelFatal("the account is out of credit") from error
            raise ModelError(str(error)) from error
        except sdk.APIError as error:
            raise ModelError(str(error)) from error

        detail = None
        if response.stop_reason == "refusal" and response.stop_details:
            detail = response.stop_details.category
        return Reply(
            blocks=[block.model_dump(mode="json", exclude_none=True) for block in response.content],
            stop_reason=response.stop_reason,
            usage={
                "input_tokens": response.usage.input_tokens,
                "output_tokens": response.usage.output_tokens,
                "cache_creation_input_tokens": response.usage.cache_creation_input_tokens or 0,
                "cache_read_input_tokens": response.usage.cache_read_input_tokens or 0,
            },
            replay=response.content,
            stop_detail=detail,
            served_by=response.model,
        )

    def settings(self) -> dict:
        settings = {"effort": self.effort, "max_tokens": MAX_TOKENS, "sdk": self._sdk.__version__}
        if self.name in MANUAL_THINKING:
            settings["thinking_budget"] = BUDGETS[self.effort]
        if self.name in API_IDS:
            settings["api_id"] = API_IDS[self.name]
        return settings
