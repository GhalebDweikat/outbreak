"""One agent working one ticket, from the opening message to the end of the loop."""

from __future__ import annotations

from dataclasses import asdict

from .budget import SpendCap
from .detect import detect
from .models import Model
from .scenario import MAX_CALLS, SYSTEM, Trial, build
from .world import SOLUTION, TOOLS, World


def run_episode(
    trial: Trial, model: Model, cap: SpendCap | None = None, feasible: bool = False
) -> dict:
    cap = cap or SpendCap()
    scenario = build(trial, feasible=feasible)
    world = World(scenario.files)
    messages: list[dict] = [{"role": "user", "content": scenario.opening}]
    replies: list[dict] = []
    end = "call_limit"

    for _ in range(MAX_CALLS):
        cap.check()
        reply = model.reply(SYSTEM, TOOLS, messages)
        usd = cap.charge(model.price_model, reply.usage)
        replies.append(
            {
                "blocks": reply.blocks,
                "stop_reason": reply.stop_reason,
                "stop_detail": reply.stop_detail,
                "served_by": reply.served_by,
                "usage": reply.usage,
                "usd": usd,
            }
        )
        if reply.stop_reason == "refusal":
            end = "refused"
            break
        if reply.stop_reason == "max_tokens":
            end = "max_tokens"
            break
        calls = [block for block in reply.blocks if block["type"] == "tool_use"]
        if not calls:
            end = "stopped"
            break

        messages.append({"role": "assistant", "content": reply.replay})
        results = []
        for block in calls:
            text, failed = world.call(block["name"], block["input"])
            result = {"type": "tool_result", "tool_use_id": block["id"], "content": text}
            if failed:
                result["is_error"] = True
            results.append(result)
        if world.report is not None:
            end = "reported"
            break
        messages.append({"role": "user", "content": results})

    record = {
        "id": trial.id,
        "index": trial.index,
        "cell": trial.cell,
        "hole": trial.hole,
        "seed": trial.seed,
        "model": model.name,
        "end": end,
        "calls": len(replies),
        "cost_usd": sum(reply["usd"] for reply in replies),
        "opening": scenario.opening,
        "replies": replies,
        "events": world.events,
        "final_solution": world.files.get(SOLUTION),
        "truth": asdict(scenario.instance),
    }
    record["outcomes"] = detect(record)
    return record
