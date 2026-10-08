"""The chooser seam: every randomization decision goes through ``Simulator(chooser=...)``.

The default chooser reproduces the built-in draws exactly, which the golden
snapshot (tests/test_runtime_golden.py) pins; these tests check that a custom
chooser decides arms, orders, loops and the scale flip, and what is audited.
"""

from __future__ import annotations

import random

import pytest

from surveyir import load_qsf
from surveyir.runtime import (
    ChoiceRequest,
    Counterbalancer,
    DefaultChooser,
    RandomAnswerer,
    Simulator,
    arrange,
)
from surveyir.runtime.trace import Display, RandomizerDecision
from tests.conftest import mc, minimal_qsf
from tests.test_runtime import randomizer_survey


class Forced:
    """Forces orders by (kind, node_id); defers everything else to the default."""

    def __init__(self, forced: dict, p: dict | None = None) -> None:
        self.forced, self.p, self.requests = forced, p, []
        self.default = DefaultChooser()

    def choose(self, req: ChoiceRequest, rng, balancer):
        self.requests.append(req)
        if (req.kind, req.node_id) in self.forced:
            return self.forced[req.kind, req.node_id], self.p
        return self.default.choose(req, rng, balancer)


def decisions(run, kind: str) -> list[RandomizerDecision]:
    return [e for e in run.audit if isinstance(e, RandomizerDecision) and e.kind == kind]


def test_default_chooser_draws_like_arrange():
    """``arrange`` is the default chooser applied to one randomization."""
    from surveyir.model import Randomization

    ids = [str(i) for i in range(8)]
    for r in (Randomization(mode="all"), Randomization(mode="subset", subset_size=3)):
        a, b = random.Random(4), random.Random(4)
        expected = b.sample(ids, len(ids)) if r.mode == "all" else b.sample(b.sample(ids, 3), 3)
        assert arrange(ids, r, a) == expected


def test_custom_chooser_forces_arms_and_records_p_given_history():
    survey = randomizer_survey()
    p = {"FL_3": 0.0, "FL_4": 1.0, "FL_5": 0.0}
    chooser = Forced({("flow", "FL_2"): ("FL_4",)}, p=p)
    runs = Simulator(survey, seed=1, chooser=chooser).run(20, RandomAnswerer(seed=2))
    assert {tuple(r.flow_order["FL_2"]) for r in runs} == {("FL_4",)}
    assert all(r.embedded["arm"] == "B" for r in runs)  # and the branch on it follows
    assert all(("QID1", None) in r.state.displayed for r in runs)
    d = decisions(runs[0], "flow")[0]
    assert d.shown == ("FL_4",) and d.p_given_history == p and d.p_nominal["FL_4"] == 1 / 3
    req = next(q for q in chooser.requests if q.kind == "flow")
    assert (req.options, req.k, req.mode, req.even, req.balance_key) == (
        ("FL_3", "FL_4", "FL_5"), 1, "subset", True, "FL_2")
    assert [q.respondent_index for q in chooser.requests if q.kind == "flow"] == list(range(20))
    # the forced chooser never touched the balancer
    assert Simulator(survey, chooser=chooser).balancer.counts == {}


def test_default_chooser_records_p_given_history_from_the_balancer():
    sim = Simulator(randomizer_survey(), seed=1, balancer=Counterbalancer({"FL_2": {"FL_3": 1}}))
    d = decisions(sim.respondent(), "flow")[0]
    assert d.balancer_before == {"FL_3": 1, "FL_4": 0, "FL_5": 0}
    assert d.p_given_history == {"FL_3": 0.0, "FL_4": 0.5, "FL_5": 0.5}


def test_chooser_answer_is_checked():
    chooser = Forced({("flow", "FL_2"): ("FL_9",)})
    with pytest.raises(ValueError, match="chooser returned"):
        Simulator(randomizer_survey(), chooser=chooser).respondent()


def test_choice_block_and_loop_orders_are_forced():
    q1 = mc("QID1", Randomization={"Type": "All"},
            Choices={str(i): {"Display": f"c{i}"} for i in range(1, 5)}, ChoiceOrder=[1, 2, 3, 4])
    q2 = mc("QID2", QuestionText="Rate ${lm://Field/1}")
    doc = minimal_qsf(
        [q1, q2],
        block_options={
            "Looping": "Static",
            "LoopingOptions": {
                "Static": {str(i): {"1": f"f{i}"} for i in range(1, 4)},
                "Randomization": "All",
            },
            "RandomizeQuestions": "true",
        },
    )
    survey = load_qsf(doc)
    chooser = Forced({
        ("choices", "QID1"): ("3", "1", "4", "2"),
        ("loop", "BL_1"): ("2", "3", "1"),
        ("block", "BL_1"): ("QID2", "QID1"),
    })
    run = Simulator(survey, seed=0, chooser=chooser).respondent(RandomAnswerer(seed=1))
    assert run.loops["BL_1"] == ["2", "3", "1"]
    assert [run.choice_order[("QID1", lp)] for lp in "231"] == [["3", "1", "4", "2"]] * 3
    shown = [(d.qid, d.loop_id) for d in run.trace if isinstance(d, Display)]
    assert shown == [(q, lp) for lp in "231" for q in ("QID2", "QID1")]
    loop_req = next(r for r in chooser.requests if r.kind == "loop")
    assert loop_req.options == ("1", "2", "3") and loop_req.balance_key == "loop:BL_1"
    assert [r.loop_id for r in chooser.requests if r.kind == "choices"] == ["2", "3", "1"]


def test_the_scale_flip_is_a_chooser_decision():
    matrix = {
        "QuestionID": "QID1", "QuestionType": "Matrix", "Selector": "Likert",
        "SubSelector": "SingleAnswer", "DataExportTag": "Q1", "QuestionText": "Rate",
        "Choices": {"1": {"Display": "Row"}}, "ChoiceOrder": [1],
        "Answers": {str(i): {"Display": f"a{i}"} for i in (1, 2, 3)}, "AnswerOrder": [1, 2, 3],
        "Randomization": {"Type": "ScaleReversal"},
    }
    survey = load_qsf(minimal_qsf([matrix]))
    for flip, cols in (("flipped", ["3", "2", "1"]), ("normal", ["1", "2", "3"])):
        run = Simulator(survey, chooser=Forced({("flip", "scale"): (flip,)})).respondent()
        d = next(o for o in run.trace if isinstance(o, Display))
        assert d.flipped == (flip == "flipped") and run.column_order[("QID1", None)] == cols
