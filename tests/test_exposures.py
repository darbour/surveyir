"""exposures(run, survey): what each respondent was shown, kept apart from recorded fields."""

from __future__ import annotations

from surveyir import load_qsf
from surveyir.runtime import RandomAnswerer, Simulator
from surveyir.runtime.design import ExposureHistory, design, exposures
from surveyir.runtime.trace import BranchEval, RandomizerDecision
from tests.conftest import FIXTURES
from tests.test_exact_traces import (
    a_treatment_screens,
    ask,
    block,
    group,
    qsf,
    text_screen,
)

GROUP_BLOCKS = {  # privacy.qsf: the branch on Group that shows each group's block
    "CT": ("FL_5", "BL_bvxPSu0c5lIGR2l"),
    "BT": ("FL_6", "BL_a04RwD5N5xfvn5c"),
    "PSA": ("FL_7", "BL_8cvFpTEL1qbF9FY"),
    "PSB": ("FL_8", "BL_bgbw7op96Sxkpts"),
    "UA": ("FL_35", "BL_5gx8VGSOXemhIge"),
    "NANT": ("FL_41", "BL_6SuXZqrMoRJ9Wf4"),
}
#: panel fields, and an empty placeholder field the Qualtrics editor left in two arms
PANEL = {"PROLIFIC_PID": "p1", "STUDY_ID": "s1", "SESSION_ID": "x1",
         "Create New Field or Choose From Dropdown...": ""}


def test_privacy_order_factor_assigns_by_last_arm_and_exposes_one_group_block():
    """FL_4 shows all six arms in random order, but its arms only set ``Group``: nothing is
    displayed inside them. ``Group`` keeps the last arm's value, and a branch on it then
    shows exactly one group block. Randomizer node, recorded field and exposure differ."""
    survey = load_qsf(FIXTURES / "privacy.qsf")
    (factor,) = design(survey).factors
    value = {a.key: a.assignments["Group"] for a in factor.arms}
    sim = Simulator(survey, seed=1)
    seen = set()
    for _ in range(12):
        run = sim.respondent(RandomAnswerer(seed=2), embedded=PANEL)
        history = exposures(run, survey)
        fl4 = history.factor("FL_4")
        assert sorted(fl4.arms_shown_in_order) == sorted(value)  # all six, in random order
        assert [a.key for a in fl4.arms] == list(fl4.arms_shown_in_order)
        assert all(a.displays == () for a in fl4.arms)  # the arms show nothing
        group_value = run.embedded["Group"]
        assert group_value == value[fl4.arms_shown_in_order[-1]]  # the last arm wins
        shown_groups = [g for g, (_, b) in GROUP_BLOCKS.items()
                        if b in {bid for bid, _ in history.blocks}]
        assert shown_groups == [group_value]  # exactly one group block is seen
        taken = [e.node_id for e in run.audit if isinstance(e, BranchEval) and e.result]
        assert taken == [GROUP_BLOCKS[group_value][0]]
        seen.add(group_value)
    assert len(seen) > 1


def test_exposure_factor_attributes_screens_to_the_assigned_arm():
    survey = a_treatment_screens()
    sim = Simulator(survey, seed=0)
    arms = set()
    for _ in range(2):
        run = sim.respondent(RandomAnswerer(seed=1))
        history = exposures(run, survey)
        assert isinstance(history, ExposureHistory)
        (fl2,) = history.factors
        (arm,) = fl2.arms
        arms.add(arm.key)
        vignette = {"FL_3": "QID10", "FL_4": "QID11"}[arm.key]
        assert [d.qid for d in arm.displays] == [vignette] == [d.qid for d in history.screens]
        assert [d.qid for d in history.displays] == [vignette, "QID1"]  # then the outcome
        assert fl2.p_nominal == {"FL_3": 0.5, "FL_4": 0.5}
        decision = next(e for e in run.audit if isinstance(e, RandomizerDecision))
        assert fl2.arms_shown_in_order == decision.shown
    assert arms == {"FL_3", "FL_4"}


def test_order_factor_keeps_the_presentation_order_of_stimuli():
    """Both arms shown (k = n): each arm's screens, in the order the arms came."""
    survey = qsf(
        [text_screen("QID10", "Story one."), ask("QID11", "Rate story one"),
         text_screen("QID20", "Story two."), ask("QID21", "Rate story two"),
         ask("QID1", "Before")],
        {"BL_0": ["QID1"], "BL_one": ["QID10", "|", "QID11"], "BL_two": ["QID20", "QID21"]},
        [block("FL_2", "BL_0"),
         {"Type": "BlockRandomizer", "FlowID": "FL_3", "SubSet": 2, "EvenPresentation": False,
          "Flow": [group("FL_4", block("FL_41", "BL_one")),
                   group("FL_5", block("FL_51", "BL_two"))]}],
    )
    (factor,) = design(survey).factors
    assert factor.contrast == "order"
    orders = set()
    sim = Simulator(survey, seed=3)
    for _ in range(8):
        history = exposures(sim.respondent(RandomAnswerer(seed=1)), survey)
        fl3 = history.factor("FL_3")
        expected = {"FL_4": ["QID10", "QID11"], "FL_5": ["QID20", "QID21"]}
        assert [[d.qid for d in a.displays] for a in fl3.arms] == \
            [expected[k] for k in fl3.arms_shown_in_order]
        assert [d.qid for d in history.displays] == ["QID1", *[d.qid for d in fl3.displays]]
        orders.add(fl3.arms_shown_in_order)
    assert orders == {("FL_4", "FL_5"), ("FL_5", "FL_4")}


def test_exposures_accepts_a_precomputed_design():
    survey = a_treatment_screens()
    run = Simulator(survey, seed=0).respondent(RandomAnswerer(seed=1))
    assert exposures(run, design(survey)) == exposures(run, survey)
