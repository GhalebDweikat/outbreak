"""Runs an old episode's code checks again with today's sandbox and judge.

Stored episodes are never changed. Each rerun goes to rechecks.jsonl beside them, with the
old and new verdicts, and the report uses the new one. That fixes checks a sandbox fault
left blank, and fills in fields that older runs didn't record, such as which rule broke.
"""

from __future__ import annotations

import json
from pathlib import Path

from . import coding
from .runner import EPISODES, RECHECKS, read_records
from .scenario import CODING_TASK, Trial, build


def final_files(record: dict) -> dict[str, str]:
    """The repo as the episode left it: the starting files with every change applied in order."""
    trial = Trial(record["index"], record["cell"], record["seed"], record["pressure"], record["task"])
    files = dict(build(trial).files)
    for event in record["events"]:
        if event["tool"] in ("write_file", "delete_file") and event.get("changed"):
            if event["after"] is None:
                files.pop(event["path"], None)
            else:
                files[event["path"]] = event["after"]
    return files


def recheck(out: Path, sandbox, everything: bool = False) -> list[dict]:
    """Rerun the checks that errored, or every check, and save what came back."""
    path = out / RECHECKS
    saved = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            entry = json.loads(line)
            saved[entry["id"], entry["write_seq"]] = entry
    fresh = []
    for record in read_records(out / EPISODES):
        if record.get("task") != CODING_TASK:
            continue
        checks = [e for e in record["events"] if e["tool"] == "code_check" and (everything or e["error"])]
        if not checks:
            continue
        writes = {e["seq"]: e["after"] for e in record["events"] if e["tool"] == "write_file"}
        weeks = build(Trial(record["index"], record["cell"], record["seed"], record["pressure"], CODING_TASK)).weeks
        versions = list(dict.fromkeys(writes[e["write_seq"]] for e in checks))
        results = dict(zip(versions, sandbox.check(final_files(record), versions, weeks)))
        for event in checks:
            new = coding.judge(results[writes[event["write_seq"]]], weeks)
            old = {key: event.get(key) for key in new}
            entry = {"id": record["id"], "write_seq": event["write_seq"], "old": old, "new": new}
            saved[record["id"], event["write_seq"]] = entry
            fresh.append(entry)
    if saved:
        path.write_text("".join(json.dumps(entry) + "\n" for entry in saved.values()), encoding="utf-8")
    return fresh
