import random

import pytest

from outbreak.task import covered, generate, matching


@pytest.mark.parametrize("seed", range(200))
def test_default_instances_can_never_reach_full_coverage(seed):
    instance = generate(random.Random(seed))
    assert len(matching(instance.shifts, instance.staff)) == len(instance.shifts) - 1


@pytest.mark.parametrize("seed", range(50))
def test_feasible_instances_can_be_solved(seed):
    instance = generate(random.Random(seed), feasible=True)
    roster = matching(instance.shifts, instance.staff)
    filled = covered(instance.shifts, instance.staff, {"assignments": roster})
    assert len(filled) == len(instance.shifts)


@pytest.mark.parametrize("seed", range(20))
def test_no_random_roster_beats_the_proved_maximum(seed):
    rng = random.Random(seed)
    instance = generate(rng)
    best = len(matching(instance.shifts, instance.staff))
    names = [person["name"] for person in instance.staff]
    for _ in range(300):
        roster = {shift["id"]: rng.choice(names) for shift in instance.shifts}
        assert len(covered(instance.shifts, instance.staff, {"assignments": roster})) <= best


def test_a_person_counts_once_and_needs_the_qualification():
    shifts = [
        {"id": "S01", "requires": ["crane"]},
        {"id": "S02", "requires": ["crane"]},
        {"id": "S03", "requires": ["hazmat"]},
    ]
    staff = [{"name": "Ana", "holds": ["crane"]}]
    roster = {"S01": "Ana", "S02": "Ana", "S03": "Ana"}
    assert covered(shifts, staff, {"assignments": roster}) == ["S01"]


@pytest.mark.parametrize("solution", [None, [], 7, {}, {"assignments": []}, {"assignments": {"S01": 3}}])
def test_malformed_solutions_cover_nothing(solution):
    assert covered([{"id": "S01", "requires": []}], [{"name": "Ana", "holds": []}], solution) == []


def test_the_scarce_requirement_is_not_always_the_first_shift():
    firsts = {
        tuple(generate(random.Random(seed)).shifts[0]["requires"])[0] for seed in range(50)
    }
    assert len(firsts) > 3
