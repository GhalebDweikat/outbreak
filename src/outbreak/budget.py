"""What each call costs, and the cap that stops a run before it overspends."""

from __future__ import annotations

import json
import os
import threading
import time
from contextlib import contextmanager
from pathlib import Path

# Dollars per million tokens, from Anthropic's pricing page on 2026-11-01, and on
# 2026-11-02 for the 4.5 and 4.6 models. Writes use the 5-minute cache, which is the default.
# Sonnet 5.5 reads at 0.05x input.
PRICES = {
    "claude-haiku-5-5": {"input": 0.10, "output": 0.50, "cache_write": 0.125, "cache_read": 0.01},
    "claude-sonnet-5-5": {"input": 2.00, "output": 10.00, "cache_write": 2.50, "cache_read": 0.10},
    "claude-sonnet-4-6": {"input": 3.00, "output": 15.00, "cache_write": 3.75, "cache_read": 0.30},
    "claude-opus-4-6": {"input": 5.00, "output": 25.00, "cache_write": 6.25, "cache_read": 0.50},
    "claude-haiku-4-5": {"input": 1.00, "output": 5.00, "cache_write": 1.25, "cache_read": 0.10},
    "claude-opus-4-5": {"input": 5.00, "output": 25.00, "cache_write": 6.25, "cache_read": 0.50},
    "scripted": {"input": 0.0, "output": 0.0, "cache_write": 0.0, "cache_read": 0.0},
}

# Haiku 5.5 charges five times as much for a prompt over this many tokens.
LONG_PROMPT = 100_000
LONG_PROMPT_MULTIPLIER = 5


class BudgetExceeded(Exception):
    """The spend cap was reached."""


def cost(model: str, usage: dict[str, int]) -> float:
    """Dollars for one call. An unpriced model raises KeyError rather than running free."""
    price = PRICES[model]
    written = usage.get("cache_creation_input_tokens", 0)
    read = usage.get("cache_read_input_tokens", 0)
    fresh = usage.get("input_tokens", 0)
    total = (
        fresh * price["input"]
        + written * price["cache_write"]
        + read * price["cache_read"]
        + usage.get("output_tokens", 0) * price["output"]
    ) / 1_000_000
    if model == "claude-haiku-5-5" and fresh + written + read > LONG_PROMPT:
        total *= LONG_PROMPT_MULTIPLIER
    return total


@contextmanager
def exclusive(path: Path):
    """Holds an OS lock on a file beside `path` until the block ends.

    Windows has no atomic append: each process seeks to the end, then writes. Two screen
    runs writing at once on 2026-11-02 overwrote each other's charges, and a test with four
    processes lost 151 of 1,200 lines before this lock.
    """
    with path.with_name(path.name + ".lock").open("a+b") as handle:
        if os.name == "nt":
            import msvcrt

            handle.seek(0)
            while True:
                try:
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError:
                    time.sleep(0.001)
            try:
                yield
            finally:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)


class SpendCap:
    """Stops a run at `limit` dollars, and everything on a ledger at `lifetime_limit`.

    The ledger is the shared record. A resumed run picks up what it already spent, and
    each check sees what other processes have written. Calls already in flight can pass
    the limit by a fraction of a cent each, so the Console spend limit is the real backstop.
    """

    def __init__(
        self,
        limit: float = float("inf"),
        ledger: Path | None = None,
        lifetime_limit: float = float("inf"),
        run: str = "",
    ):
        self.limit = limit
        self.lifetime_limit = lifetime_limit
        self.ledger = ledger
        self.run = run
        self.spent = 0.0  # This run, across every sitting.
        self.total = 0.0  # Everything on the ledger.
        self._offset = 0
        self._lock = threading.Lock()
        self._refresh()

    def _refresh(self) -> None:
        if self.ledger is None or not self.ledger.exists():
            return
        with self.ledger.open("rb") as file:
            file.seek(self._offset)
            data = file.read()
        complete = data[: data.rfind(b"\n") + 1]
        self._offset += len(complete)
        for line in complete.splitlines():
            try:
                entry = json.loads(line)
            except ValueError as error:
                raise ValueError(f"{self.ledger} has a damaged line: {line[:120]!r}") from error
            self.total += entry["usd"]
            if entry.get("run") == self.run:
                self.spent += entry["usd"]

    def check(self) -> None:
        with self._lock:
            self._refresh()
            if self.spent >= self.limit:
                raise BudgetExceeded(f"run cap reached: ${self.spent:.4f} of ${self.limit:.2f}")
            if self.total >= self.lifetime_limit:
                raise BudgetExceeded(
                    f"lifetime cap reached: ${self.total:.4f} of ${self.lifetime_limit:.2f}"
                )

    def charge(self, model: str, usage: dict[str, int]) -> float:
        usd = cost(model, usage)
        with self._lock:
            if self.ledger is None:
                self.spent += usd
                self.total += usd
            elif usd > 0:
                self.ledger.parent.mkdir(parents=True, exist_ok=True)
                with exclusive(self.ledger), self.ledger.open("a", encoding="utf-8") as file:
                    file.write(json.dumps({"run": self.run, "model": model, "usd": usd}) + "\n")
                self._refresh()
        return usd
