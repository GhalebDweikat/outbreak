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


class ClaudeModel:
    def __init__(self, name: str, effort: str = "medium", client=None):
        import anthropic

        self._sdk = anthropic
        self._client = client or anthropic.Anthropic(base_url=API, max_retries=6, timeout=120.0)
        self.name = name
        self.price_model = name
        self.effort = effort

    def reply(self, system: str, tools: list[dict], messages: list[dict]) -> Reply:
        sdk = self._sdk
        try:
            response = self._client.messages.create(
                model=self.name,
                max_tokens=MAX_TOKENS,
                cache_control={"type": "ephemeral"},
                system=system,
                tools=tools,
                messages=messages,
                thinking={"type": "adaptive", "display": "summarized"},
                output_config={"effort": self.effort},
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
        return {"effort": self.effort, "max_tokens": MAX_TOKENS, "sdk": self._sdk.__version__}
