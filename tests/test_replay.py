"""Replay: re-administering a recorded respondent with forced orders and answers."""

from __future__ import annotations

import warnings
from pathlib import Path

import pytest

from surveyir import load, load_qsf
from surveyir.runtime import (
    Answer,
    ExecutionError,
    RandomAnswerer,
    ReplayAnswerer,
    ReplayChooser,
    Replayer,
    ReplayGap,
    Simulator,
    record,
    replay,
)
from surveyir.runtime.trace import Allowed, Approximation, Display
from tests.conftest import mc, minimal_qsf
from tests.test_runtime import randomizer_survey

FIXTURES = sorted((Path(__file__).parent / "fixtures" / "qualtrics").glob("*.qsf"))


@pytest.mark.parametrize("path", FIXTURES, ids=lambda p: p.stem)
def test_replaying_a_simulated_run_reproduces_it(path):
    """Round trip: record a run's orders, answers and inputs, replay, get the same run."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        survey = load(path)
    runs = Simulator(survey, seed=11, strict=False).run(6, RandomAnswerer(seed=3))
    for run in runs:
        rec = record(run)
        again = replay(
            survey, rec.orders, rec.answers, embedded=rec.embedded, seed=rec.seed, strict=False
        )
        assert again.trace == run.trace
        assert (
            again.displayed,
            again.flow_order,
            again.choice_order,
            again.column_order,
            again.block_order,
            again.loops,
            again.embedded,
            again.answers,
            again.finished,
            again.ended_by,
        ) == (
            run.displayed,
            run.flow_order,
            run.choice_order,
            run.column_order,
            run.block_order,
            run.loops,
            run.embedded,
            run.answers,
            run.finished,
            run.ended_by,
        )
        assert not any(isinstance(e, Approximation) and e.code == "replay.gap" for e in again.audit)


def test_replay_forces_orders_whatever_the_seed():
    survey = randomizer_survey()
    run = Simulator(survey, seed=4).respondent(RandomAnswerer(seed=5))
    rec = record(run)
    for seed in range(5):
        again = replay(survey, rec.orders, rec.answers, seed=seed)
        assert again.flow_order == run.flow_order and again.answers == run.answers


def test_forced_answers_drive_display_logic():
    """QID2 shows only if QID1 is "2"; the replayed answer decides, at that point."""
    from tests.test_runtime import logic, qx

    q2 = mc("QID2", DisplayLogic=logic(qx("q://QID1/SelectableChoice/2")))
    survey = load_qsf(
        minimal_qsf(
            [mc("QID1"), q2],
            block_elements=[
                {"Type": "Question", "QuestionID": "QID1"},
                {"Type": "Page Break"},
                {"Type": "Question", "QuestionID": "QID2"},
            ],
        )
    )
    yes = replay(survey, {}, {("QID1", None): "1", ("QID2", None): "1"})
    no = replay(survey, {}, {("QID1", None): Answer("2")})
    assert yes.displayed == [("QID1", None)] and yes.answers[("QID1", None)].value == "1"
    assert no.displayed == [("QID1", None), ("QID2", None)]
    assert ("QID2", None) not in no.answers  # displayed, no recorded answer: left blank


def test_forced_arm_drives_embedded_data_and_branch():
    survey = randomizer_survey()
    for arm, value in (("FL_3", "A"), ("FL_4", "B"), ("FL_5", "C")):
        run = replay(
            survey,
            {("flow", "FL_2", None): [arm], ("choices", "QID1", None): "21"},
            {("QID1", None): "1"},
        )
        assert run.embedded["arm"] == value
        assert (("QID1", None) in run.answers) == (value == "B")


def test_missing_decision_is_a_gap():
    survey = randomizer_survey()
    with pytest.raises(ReplayGap, match="no recorded flow order for FL_2") as err:
        replay(survey, {}, {})
    assert isinstance(err.value, ExecutionError) and err.value.run is not None
    assert err.value.request.options == ("FL_3", "FL_4", "FL_5")
    # recorded, but naming no offered option
    with pytest.raises(ReplayGap, match="none of which is offered"):
        replay(survey, {("flow", "FL_2", None): ["FL_9"]}, {})
    # allowed: the fallback draws it, and the gap is audited
    run = replay(survey, {}, {}, allow={"replay.gap"}, seed=1)
    assert len(run.flow_order["FL_2"]) == 1
    gaps = [e for e in run.audit if isinstance(e, Approximation) and e.code == "replay.gap"]
    assert [g.location for g in gaps] == ["flow:FL_2"] and gaps[0].affects == "assignment"
    assert Allowed("replay.gap", "flow:FL_2") in run.audit
    # permissive: the same, without the Allowed record
    run = replay(survey, {}, {}, strict=False, seed=1)
    assert [e.code for e in run.audit if isinstance(e, Approximation)] == ["replay.gap"]


def test_fallback_kinds_are_not_gaps():
    survey = load_qsf(minimal_qsf([mc(Randomization={"Type": "All"})]))
    run = replay(survey, {}, {}, fallback_kinds={"choices"}, seed=2)
    assert sorted(run.choice_order[("QID1", None)]) == ["1", "2"]
    assert not any(isinstance(e, Approximation) for e in run.audit)
    with pytest.raises(ReplayGap):
        replay(survey, {}, {})


def test_partial_orders_are_completed_when_everything_is_presented():
    choices = {str(i): {"Display": f"c{i}"} for i in range(1, 5)}
    survey = load_qsf(
        minimal_qsf([mc(Randomization={"Type": "All"}, Choices=choices, ChoiceOrder=[1, 2, 3, 4])])
    )
    run = replay(survey, {("choices", "QID1", None): ["4", "9", "2"]}, {})
    assert run.choice_order[("QID1", None)] == ["4", "2", "1", "3"]


def test_replay_answerer_and_chooser_work_with_a_simulator():
    survey = randomizer_survey()
    chooser = ReplayChooser({("flow", "FL_2", None): ["FL_4"]}, fallback_kinds={"choices"})
    run = Simulator(survey, chooser=chooser).respondent(
        ReplayAnswerer({("QID1", None): Answer("2", {"2": "why"})})
    )
    assert run.answers[("QID1", None)] == Answer("2", {"2": "why"})
    shown = [o.qid for o in run.trace if isinstance(o, Display)]
    assert shown == ["QID1"] and not run.privileged


def test_replay_can_leave_the_balancer_counts_untouched():
    """targeting_fairness's FL_491 is evenly presented: by default a replayed arm is
    counted in the shared balancer (as the recorded history left it); with
    touch_balancer=False the counts stay exactly as they were, for recorded decisions
    and for fallback draws alike."""
    survey = load_qsf(Path(__file__).parent / "fixtures" / "qualtrics" / "targeting_fairness.qsf")
    runs = Simulator(survey, seed=2).run(3)
    recordings = [record(r) for r in runs]
    assert all(("flow", "FL_491", None) in rec.orders for rec in recordings)

    counted = Replayer(survey)
    for rec in recordings:
        counted.respondent(rec.orders, rec.answers, embedded=rec.embedded, seed=rec.seed)
    assert sum(counted.simulator.balancer.counts["FL_491"].values()) == 3

    untouched = Replayer(survey, touch_balancer=False)
    before = {"FL_491": {"FL_493": 7, "FL_497": 2}}
    untouched.simulator.balancer.counts = {k: dict(v) for k, v in before.items()}
    again = [
        untouched.respondent(rec.orders, rec.answers, embedded=rec.embedded, seed=rec.seed)
        for rec in recordings
    ]
    assert untouched.simulator.balancer.counts == before
    assert [r.flow_order for r in again] == [r.flow_order for r in runs]

    # a gap drawn by the fallback (least-filled first: FL_497) leaves the counts alone too
    gap = Replayer(survey, touch_balancer=False, allow={"replay.gap"})
    gap.simulator.balancer.counts = {k: dict(v) for k, v in before.items()}
    orders = {k: v for k, v in recordings[0].orders.items() if k[0] != "flow"}
    run = gap.respondent(orders, recordings[0].answers, seed=recordings[0].seed)
    assert run.flow_order["FL_491"] == ["FL_497"]
    assert gap.simulator.balancer.counts == before
    # replay() passes the option through
    replay(survey, orders, {}, seed=1, allow={"replay.gap"}, touch_balancer=False)
