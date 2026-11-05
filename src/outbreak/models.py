"""The interface every model sits behind, whether it is scripted or real."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol


@dataclass
class Reply:
    blocks: list[dict]  # Plain dicts, by content type, for the loop and the logs.
    stop_reason: str
    usage: dict[str, int]
    replay: Any  # Goes back into the next request unchanged, so thinking blocks survive.
    stop_detail: str | None = None
    served_by: str | None = None  # The model the API says answered.


class Model(Protocol):
    name: str
    price_model: str

    def reply(self, system: str, tools: list[dict], messages: list[dict]) -> Reply: ...


class ModelError(Exception):
    """A failed call. The episode is dropped and a resumed run tries it again."""


class ModelFatal(Exception):
    """A failure every later call would hit too, so the whole run stops."""
