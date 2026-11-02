"""Runs the trials of an experiment, keeps every finished episode, and can resume."""

from __future__ import annotations

import json
import random
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

from .budget import BudgetExceeded, SpendCap
from .episode import run_episode
from .models import Model, ModelError, ModelFatal
from .scenario import CELLS, Trial, stimuli_hash

EPISODES = "episodes.jsonl"
ERRORS = "errors.jsonl"
MANIFEST = "manifest.json"
MAX_ERRORS_IN_A_ROW = 5


def plan(trials_per_cell: int, seed: int = 0, cells: Iterable[int] = CELLS) -> list[Trial]:
    """Every cell gets the same indexes, shuffled together so no cell runs first."""
    trials = [Trial(index, cell, seed) for cell in cells for index in range(trials_per_cell)]
    random.Random(f"{seed}:order").shuffle(trials)
    return trials


@dataclass
class Outcome:
    finished: int
    remaining: int
    stopped: str | None = None


def check_manifest(out: Path, settings: dict) -> None:
    """Refuse to add episodes to a run made with a different model or different stimuli."""
    settings = {**settings, "stimuli": stimuli_hash()}
    path = out / MANIFEST
    if path.exists():
        existing = json.loads(path.read_text(encoding="utf-8"))
        if existing != settings:
            raise ValueError(f"{out} was made with different settings: {existing}")
        return
    path.write_text(json.dumps(settings, indent=2), encoding="utf-8")


def read_records(path: Path) -> list[dict]:
    """Every complete record. A crash can leave the last line cut short."""
    if not path.exists():
        return []
    records = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            records.append(json.loads(line))
        except ValueError:
            continue
    return records


def trim_partial_line(path: Path) -> None:
    """Cut off a half-written last line, so the next record starts on its own line."""
    if path.exists():
        data = path.read_bytes()
        if data and not data.endswith(b"\n"):
            path.write_bytes(data[: data.rfind(b"\n") + 1])


def finished_ids(path: Path) -> set[str]:
    return {record["id"] for record in read_records(path)}


def run(
    trials: list[Trial],
    make_model: Callable[[Trial], Model],
    out: Path,
    settings: dict,
    cap: SpendCap | None = None,
    workers: int = 1,
) -> Outcome:
    cap = cap or SpendCap()
    out.mkdir(parents=True, exist_ok=True)
    check_manifest(out, settings)
    episodes = out / EPISODES
    trim_partial_line(episodes)
    done = finished_ids(episodes)
    pending = [trial for trial in trials if trial.id not in done]

    lock = threading.Lock()
    halt = threading.Event()
    state = {"finished": 0, "errors_in_a_row": 0, "stopped": None}

    def stop(reason: str) -> None:
        state["stopped"] = state["stopped"] or reason
        halt.set()

    def work(trial: Trial) -> None:
        if halt.is_set():
            return
        try:
            record = run_episode(trial, make_model(trial), cap)
        except BudgetExceeded as error:
            stop(str(error))
        except ModelFatal as error:
            stop(f"model error: {error}")
        except ModelError as error:
            with lock:
                with (out / ERRORS).open("a", encoding="utf-8") as file:
                    file.write(json.dumps({"id": trial.id, "error": str(error)}) + "\n")
                state["errors_in_a_row"] += 1
                if state["errors_in_a_row"] >= MAX_ERRORS_IN_A_ROW:
                    stop(f"{MAX_ERRORS_IN_A_ROW} model errors in a row")
        else:
            with lock:
                with episodes.open("a", encoding="utf-8") as file:
                    file.write(json.dumps(record) + "\n")
                state["finished"] += 1
                state["errors_in_a_row"] = 0

    with ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(work, pending))

    return Outcome(state["finished"], len(pending) - state["finished"], state["stopped"])
