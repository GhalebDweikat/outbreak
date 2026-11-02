"""Command line for the kill test."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .budget import PRICES, SpendCap
from .report import render
from .runner import plan, run
from .scenario import CELLS, SYSTEM, Trial, build, stimuli_hash
from .scripted import PLANTS, POLICIES, ScriptedModel

LEDGER = Path(__file__).resolve().parents[2] / "runs" / "ledger.jsonl"


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="outbreak", description=__doc__)
    commands = root.add_subparsers(dest="command", required=True)

    kill = commands.add_parser("killtest", help="run the five-cell kill test")
    kill.add_argument("--out", type=Path, required=True, help="folder for this run")
    kill.add_argument("--model", default="scripted", choices=sorted(PRICES))
    kill.add_argument("--trials", type=int, default=100, help="trials per cell")
    kill.add_argument("--cells", type=int, nargs="+", default=list(CELLS), choices=list(CELLS))
    kill.add_argument("--seed", type=int, default=0)
    kill.add_argument("--workers", type=int, default=4)
    kill.add_argument("--effort", default="medium", help="Claude effort level")
    kill.add_argument("--max-usd", type=float, help="spend cap for this run, required if paid")
    kill.add_argument("--lifetime-usd", type=float, default=190.0, help="cap across paid runs")
    kill.add_argument("--plant", default="contagion", choices=sorted(PLANTS))
    kill.add_argument("--policy", choices=sorted(POLICIES), help="one script for every trial")
    kill.add_argument("--price-as", default="scripted", choices=sorted(PRICES))

    report = commands.add_parser("report", help="write the report for a run")
    report.add_argument("out", type=Path)

    show = commands.add_parser("show", help="print what an agent sees in one trial")
    show.add_argument("--cell", type=int, default=4, choices=list(CELLS))
    show.add_argument("--index", type=int, default=0)
    show.add_argument("--seed", type=int, default=0)
    return root


def killtest(args: argparse.Namespace, root: argparse.ArgumentParser) -> int:
    trials = plan(args.trials, args.seed, args.cells)
    settings = {
        "model": args.model,
        "seed": args.seed,
        "trials_per_cell": args.trials,
        "cells": args.cells,
    }
    if args.model == "scripted":
        policy = POLICIES[args.policy] if args.policy else PLANTS[args.plant]
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

    outcome = run(trials, make_model, args.out, settings, cap, args.workers)
    summary = render(args.out)
    (args.out / "report.md").write_text(summary, encoding="utf-8")
    print(summary)
    print(f"Finished {outcome.finished}, {outcome.remaining} left. Spent ${cap.spent:.4f}.")
    if outcome.stopped:
        print(f"Stopped early: {outcome.stopped}", file=sys.stderr)
        return 1
    return 0


def show(args: argparse.Namespace) -> int:
    trial = Trial(args.index, args.cell, args.seed)
    scenario = build(trial)
    print(f"Stimuli {stimuli_hash()}, hole {trial.hole}\n")
    print(f"--- system ---\n{SYSTEM}\n\n--- opening ---\n{scenario.opening}\n\n--- files ---")
    for path, text in sorted(scenario.files.items()):
        print(f"{path}: {len(text)} characters")
    print(f"\n--- first file ---\n{json.dumps(scenario.instance.shifts[0])}")
    return 0


def main(argv: list[str] | None = None) -> int:
    root = parser()
    args = root.parse_args(argv)
    if args.command == "killtest":
        return killtest(args, root)
    if args.command == "report":
        print(render(args.out))
        return 0
    return show(args)


if __name__ == "__main__":
    raise SystemExit(main())
