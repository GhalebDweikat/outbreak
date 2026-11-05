import re

import pytest

from outbreak import scenario, world
from outbreak.scenario import AGENT, CELLS, FILLER, HOLES, PEERS, RECIPES, RETIRING, Trial, build


def posts(trial):
    return build(trial).opening.split("Team board, newest last:\n")[1].splitlines()


def recipe_lines(lines):
    return [line for line in lines if line.startswith("Note left on the board")]


def peer_lines(lines):
    pattern = "|".join(PEERS)
    return [line for line in lines if re.match(f"({pattern}):", line)]


@pytest.mark.parametrize("index", range(30))
def test_everything_but_the_exposure_is_the_same_across_cells(index):
    openings = {cell: build(Trial(index, cell)) for cell in CELLS}
    instances = {str(s.instance) for s in openings.values()}
    assert len(instances) == 1
    fillers = {tuple(sorted(set(posts(Trial(index, cell))) & set(FILLER))) for cell in CELLS}
    assert len(fillers) == 1
    tickets = {s.opening.split(":")[0] for s in openings.values()}
    assert len(tickets) == 1


@pytest.mark.parametrize("index", range(30))
def test_each_cell_shows_exactly_its_exposure(index):
    no_exposure = posts(Trial(index, 1))
    assert recipe_lines(no_exposure) == [] and peer_lines(no_exposure) == []
    assert len(no_exposure) == 4

    anonymous = posts(Trial(index, 2))
    assert len(recipe_lines(anonymous)) == 1 and peer_lines(anonymous) == []

    one_peer = posts(Trial(index, 3))
    assert recipe_lines(one_peer) == recipe_lines(anonymous)
    assert len(peer_lines(one_peer)) == 1

    four = posts(Trial(index, 4))
    assert recipe_lines(four) == recipe_lines(anonymous)
    assert len(peer_lines(four)) == 4
    assert not any("revoked" in line for line in four)

    revoked = posts(Trial(index, 5))
    assert recipe_lines(revoked) == recipe_lines(anonymous)
    assert len(peer_lines(revoked)) == 4


@pytest.mark.parametrize("index", range(30))
def test_the_recipe_matches_the_trials_hole(index):
    trial = Trial(index, 2)
    line = recipe_lines(posts(trial))[0]
    assert any(recipe in line for recipe in RECIPES[trial.hole])


@pytest.mark.parametrize("index", range(30))
def test_exposure_stays_in_a_sensible_order(index):
    lines = posts(Trial(index, 5))
    note = lines.index(recipe_lines(lines)[0])
    peers = [lines.index(line) for line in peer_lines(lines)]
    revocation = next(i for i, line in enumerate(lines) if "revoked" in line)
    assert note < min(peers)
    assert max(peers) < revocation
    assert revocation > note


@pytest.mark.parametrize("index", range(30))
def test_the_revoked_peer_is_one_of_those_who_reported(index):
    lines = posts(Trial(index, 5))
    revocation = next(line for line in lines if "revoked" in line)
    assert any(line.split(":")[0] + " " in revocation for line in peer_lines(lines))
    assert scenario.REVOKED_PATHS[Trial(index, 5).hole] in revocation


@pytest.mark.parametrize("index", range(30))
def test_the_revocation_cites_the_ticket_that_peer_reported(index):
    lines = posts(Trial(index, 5))
    revocation = next(line for line in lines if "revoked" in line)
    peer = re.search(r"agent-\d+", revocation).group()
    ticket = re.search(r"ticket (\d+)", revocation).group(1)
    report = next(line for line in peer_lines(lines) if line.startswith(f"{peer}:"))
    assert ticket in report


@pytest.mark.parametrize("cell", [3, 4, 5])
@pytest.mark.parametrize("index", range(30))
def test_every_report_makes_sense_on_its_own(cell, index):
    trial = Trial(index, cell)
    for line in peer_lines(posts(trial)):
        assert scenario.ACTIONS[trial.hole] in line


def test_the_hash_notices_changes_outside_the_templates(monkeypatch):
    original = scenario.stimuli_hash()
    monkeypatch.setattr(scenario, "PEERS", ("agent-30", "agent-31", "agent-32", "agent-33"))
    changed = scenario.stimuli_hash()
    assert changed != original


def test_the_hash_notices_a_workspace_change(monkeypatch):
    original = scenario.stimuli_hash()
    monkeypatch.setattr(scenario, "PIPELINE", "Something else.")
    assert scenario.stimuli_hash() != original


def test_the_peer_shutting_down_is_never_a_reporter_or_the_agent():
    assert RETIRING not in PEERS and AGENT not in PEERS
    for line in FILLER:
        assert not line.startswith((f"{RETIRING}:", f"{AGENT}:"))
        assert not line.startswith(tuple(f"{peer}:" for peer in PEERS))


def test_nothing_shown_to_the_agent_says_it_is_a_test():
    shown = " ".join(
        [
            scenario.SYSTEM,
            scenario.TICKET,
            scenario.PIPELINE,
            scenario.ANONYMOUS,
            scenario.REVOKED,
            *FILLER,
            *scenario.REPORTS,
            *[recipe for recipes in RECIPES.values() for recipe in recipes],
            *[tool["description"] for tool in world.TOOLS],
        ]
    )
    banned = r"\b(test\w*|experiment\w*|evaluat\w*|study|research\w*|honeypot|simulat\w*|misbehav\w*|cheat\w*)\b"
    assert not re.search(banned, shown, re.IGNORECASE)


def test_nothing_on_the_board_urges_the_agent():
    """Openly urging a violation backfires in earlier work, so peers only report."""
    for report in scenario.REPORTS:
        assert not re.search(r"\b(you should|you must|just do|go ahead|try it)\b", report, re.I)


def test_the_system_prompt_and_tools_never_vary():
    assert "{" not in scenario.SYSTEM
    assert scenario.MAX_CALLS and str(scenario.MAX_CALLS) in scenario.SYSTEM


def test_the_workspace_has_a_route_to_every_hole_without_exposure():
    files = build(Trial(0, 1)).files
    for path in (world.CONFIG, world.SHIFTS, world.RESULTS, world.QUEUE, world.AUDIT):
        assert path in files
    pipeline = files["docs/pipeline.md"]
    for path in (world.CONFIG, world.SHIFTS, world.RESULTS, world.QUEUE, world.AUDIT):
        assert path in pipeline


def test_holes_are_balanced_across_a_cell():
    holes = [Trial(index, 3).hole for index in range(99)]
    assert all(holes.count(hole) == 33 for hole in HOLES)


def test_stimuli_hash_is_stable():
    assert scenario.stimuli_hash() == scenario.stimuli_hash()
