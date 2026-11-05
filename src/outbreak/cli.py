"""Command line for the kill test."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .budget import PRICES, SpendCap
from .report import render
from .runner import plan, run
from .scenario import (
    ALL_CELLS,
    BASELINE,
    CELLS,
    CODING_TASK,
    NOTES,
    PRESSURES,
    TASK_HOLES,
    TICKET_TASK,
    Trial,
    build,
    stimuli_hash,
)
from .scripted import CODING_POLICIES, PLANTS, POLICIES, FakeSandbox, ScriptedModel

TASKS = (TICKET_TASK, CODING_TASK)
ALL_HOLES = [hole for task in TASKS for hole in TASK_HOLES[task]]
IMAGE = "python:3.12-slim"

LEDGER = Path(__file__).resolve().parents[2] / "runs" / "ledger.jsonl"


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="outbreak", description=__doc__)
    commands = root.add_subparsers(dest="command", required=True)

    kill = commands.add_parser("killtest", help="run the five-cell kill test, or the echo test")
    kill.add_argument("--out", type=Path, required=True, help="folder for this run")
    kill.add_argument("--model", default="scripted", choices=sorted(PRICES))
    kill.add_argument("--trials", type=int, default=100, help="trials per cell")
    kill.add_argument(
        "--cells", type=int, nargs="+", default=list(CELLS), choices=list(ALL_CELLS),
        help="cells to run; 2 4 6 is the echo test",
    )
    kill.add_argument("--seed", type=int, default=0)
    kill.add_argument("--pressure", default=BASELINE, choices=list(PRESSURES))
    kill.add_argument("--task", default=TICKET_TASK, choices=TASKS)
    kill.add_argument("--hole", choices=ALL_HOLES, help="point every recipe at this hole")
    kill.add_argument("--note", choices=NOTES, help="show every trial this wording of the note")
    kill.add_argument("--image", default=IMAGE, help="Docker image for the coding task")
    kill.add_argument(
        "--docker", action="store_true", help="run scripted coding episodes in Docker too"
    )
    kill.add_argument("--workers", type=int, default=4)
    kill.add_argument("--effort", default="medium", help="Claude effort level")
    kill.add_argument("--max-usd", type=float, help="spend cap for this run, required if paid")
    kill.add_argument("--lifetime-usd", type=float, default=190.0, help="cap across paid runs")
    kill.add_argument("--plant", default="contagion", choices=sorted(PLANTS))
    kill.add_argument(
        "--policy", choices=sorted({**POLICIES, **CODING_POLICIES}), help="one script for every trial"
    )
    kill.add_argument("--price-as", default="scripted", choices=sorted(PRICES))

    report = commands.add_parser("report", help="write the report for a run")
    report.add_argument(
        "out", type=Path, nargs="+", help="run folders, merged if they differ only in cells"
    )

    again = commands.add_parser("recheck", help="rerun a coding run's code checks in Docker")
    again.add_argument("out", type=Path, help="run folder")
    again.add_argument("--all", action="store_true", help="every check, not only those that errored")
    again.add_argument("--image", default=IMAGE)

    show = commands.add_parser("show", help="print what an agent sees in one trial")
    show.add_argument("--cell", type=int, default=4, choices=list(ALL_CELLS))
    show.add_argument("--index", type=int, default=0)
    show.add_argument("--seed", type=int, default=0)
    show.add_argument("--pressure", default=BASELINE, choices=list(PRESSURES))
    show.add_argument("--task", default=TICKET_TASK, choices=TASKS)
    show.add_argument("--hole", choices=ALL_HOLES)
    show.add_argument("--note", choices=NOTES)
    return root


def note_index(args: argparse.Namespace) -> int | None:
    return NOTES.index(args.note) if args.note else None


def check_hole(args: argparse.Namespace, root: argparse.ArgumentParser) -> None:
    if args.hole and args.hole not in TASK_HOLES[args.task]:
        root.error(f"--hole {args.hole} is not a {args.task} hole")


def make_sandbox(args: argparse.Namespace, settings: dict):
    """Docker for any real model. The fake runs nothing and refuses code it doesn't know."""
    if args.task != CODING_TASK:
        return None
    if args.model == "scripted" and not args.docker:
        return FakeSandbox()
    from .sandbox import DockerSandbox

    sandbox = DockerSandbox(args.image)
    settings["image"] = args.image
    settings["image_id"] = sandbox.image_id()
    return sandbox


def killtest(args: argparse.Namespace, root: argparse.ArgumentParser) -> int:
    check_hole(args, root)
    try:
        trials = plan(args.trials, args.seed, args.cells, args.pressure, args.task, args.hole, note_index(args))
    except ValueError as error:
        root.error(str(error))
    settings = {
        "model": args.model,
        "seed": args.seed,
        "trials_per_cell": args.trials,
        "cells": args.cells,
        "pressure": args.pressure,
    }
    if args.task != TICKET_TASK:
        settings["task"] = args.task
    if args.hole:
        settings["hole"] = args.hole
    if args.note:
        settings["note"] = args.note
    policies = CODING_POLICIES if args.task == CODING_TASK else POLICIES
    if args.policy and args.policy not in policies:
        root.error(f"--policy {args.policy} is not a {args.task} script")
    sandbox = make_sandbox(args, settings)
    if args.model == "scripted":
        policy = policies[args.policy] if args.policy else PLANTS[args.plant]
        settings["script"] = args.policy or args.plant

        def make_model(trial: Trial) -> ScriptedModel:
            return ScriptedModel(policy, trial, args.price_as)

        cap = SpendCap()
    else:
        if args.max_usd is None:
            root.error("a paid model needs --max-usd")
        from .claude import ClaudeModel

        model = ClaudeModel(args.model, args.effort)
        settings.update(model.settings())

        def make_model(trial: Trial) -> ClaudeModel:
            return model

        cap = SpendCap(args.max_usd, LEDGER, args.lifetime_usd, str(args.out.resolve()))

    outcome = run(trials, make_model, args.out, settings, cap, args.workers, sandbox)
    summary = render(args.out)
    (args.out / "report.md").write_text(summary, encoding="utf-8")
    print(summary)
    print(f"Finished {outcome.finished}, {outcome.remaining} left. Spent ${cap.spent:.4f}.")
    if outcome.stopped:
        print(f"Stopped early: {outcome.stopped}", file=sys.stderr)
        return 1
    return 0


def show(args: argparse.Namespace, root: argparse.ArgumentParser) -> int:
    check_hole(args, root)
    try:
        trial = Trial(args.index, args.cell, args.seed, args.pressure, args.task, args.hole, note_index(args))
    except ValueError as error:
        root.error(str(error))
    scenario = build(trial)
    extra = (args.cell,) if args.cell not in CELLS else ()
    fingerprint = stimuli_hash(args.pressure, args.task, args.hole, note_index(args), extra)
    print(f"Stimuli {fingerprint}, hole {trial.hole}\n")
    print(f"--- system ---\n{scenario.system}\n\n--- opening ---\n{scenario.opening}\n\n--- files ---")
    for path, text in sorted(scenario.files.items()):
        print(f"{path}: {len(text)} characters")
    print(f"\n--- first file ---\n{json.dumps(scenario.instance.shifts[0])}")
    if scenario.late_board:
        print(f"\n--- with the first failed grade ---\n{scenario.late_board}")
    if scenario.pushback:
        print(f"\n--- on the first blocked report ---\n{scenario.pushback}")
    return 0


def main(argv: list[str] | None = None) -> int:
    root = parser()
    args = root.parse_args(argv)
    if args.command == "killtest":
        return killtest(args, root)
    if args.command == "report":
        try:
            print(render(*args.out))
        except ValueError as error:
            root.error(str(error))
        return 0
    if args.command == "recheck":
        from .recheck import recheck
        from .sandbox import DockerSandbox

        fresh = recheck(args.out, DockerSandbox(args.image), args.all)
        # Only what decides a label. A field the old check never recorded isn't a change.
        changed = [e for e in fresh if any(e["old"][key] != e["new"][key] for key in ("fills_all", "breaks_rules", "error"))]
        for entry in changed:
            print(f"{entry['id']} write {entry['write_seq']}: {entry['old']} -> {entry['new']}")
        print(f"Reran {len(fresh)} checks, and {len(changed)} came back different. The report now uses the reruns.")
        return 0
    return show(args, root)


if __name__ == "__main__":
    raise SystemExit(main())
