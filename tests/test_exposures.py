"""exposures(run, survey): what each respondent was shown, kept apart from recorded fields."""

from __future__ import annotations

import pytest

from surveyir import load_qsf
from surveyir.runtime import RandomAnswerer, Simulator, stimulus_for_arm
from surveyir.runtime.design import ExposureHistory, design, exposures
from surveyir.runtime.trace import BranchEval, RandomizerDecision
from tests.conftest import FIXTURES
from tests.test_exact_traces import (
    a_treatment_screens,
    ask,
    block,
    embedded,
    group,
    qsf,
    randomizer,
    text_screen,
)
from tests.test_runtime import ed, logic, qx, randomizer_survey

GROUP_BLOCKS = {  # privacy.qsf: the branch on Group that shows each group's block
    "CT": ("FL_5", "BL_bvxPSu0c5lIGR2l"),
    "BT": ("FL_6", "BL_a04RwD5N5xfvn5c"),
    "PSA": ("FL_7", "BL_8cvFpTEL1qbF9FY"),
    "PSB": ("FL_8", "BL_bgbw7op96Sxkpts"),
    "UA": ("FL_35", "BL_5gx8VGSOXemhIge"),
    "NANT": ("FL_41", "BL_6SuXZqrMoRJ9Wf4"),
}
#: panel fields, and an empty placeholder field the Qualtrics editor left in two arms
PANEL = {
    "PROLIFIC_PID": "p1",
    "STUDY_ID": "s1",
    "SESSION_ID": "x1",
    "Create New Field or Choose From Dropdown...": "",
}


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
        shown_groups = [
            g for g, (_, b) in GROUP_BLOCKS.items() if b in {bid for bid, _ in history.blocks}
        ]
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
        [
            text_screen("QID10", "Story one."),
            ask("QID11", "Rate story one"),
            text_screen("QID20", "Story two."),
            ask("QID21", "Rate story two"),
            ask("QID1", "Before"),
        ],
        {"BL_0": ["QID1"], "BL_one": ["QID10", "|", "QID11"], "BL_two": ["QID20", "QID21"]},
        [
            block("FL_2", "BL_0"),
            {
                "Type": "BlockRandomizer",
                "FlowID": "FL_3",
                "SubSet": 2,
                "EvenPresentation": False,
                "Flow": [
                    group("FL_4", block("FL_41", "BL_one")),
                    group("FL_5", block("FL_51", "BL_two")),
                ],
            },
        ],
    )
    (factor,) = design(survey).factors
    assert factor.contrast == "order"
    orders = set()
    sim = Simulator(survey, seed=3)
    for _ in range(8):
        history = exposures(sim.respondent(RandomAnswerer(seed=1)), survey)
        fl3 = history.factor("FL_3")
        expected = {"FL_4": ["QID10", "QID11"], "FL_5": ["QID20", "QID21"]}
        assert [[d.qid for d in a.displays] for a in fl3.arms] == [
            expected[k] for k in fl3.arms_shown_in_order
        ]
        assert [d.qid for d in history.displays] == ["QID1", *[d.qid for d in fl3.displays]]
        orders.add(fl3.arms_shown_in_order)
    assert orders == {("FL_4", "FL_5"), ("FL_5", "FL_4")}


def test_exposures_accepts_a_precomputed_design():
    survey = a_treatment_screens()
    run = Simulator(survey, seed=0).respondent(RandomAnswerer(seed=1))
    assert exposures(run, design(survey)) == exposures(run, survey)


# ------------------------------------------------------------------ stimulus_for_arm


def test_stimulus_for_arm_follows_display_logic_on_the_assigned_field():
    """targeting_fairness: FL_491's arms show nothing themselves; they set Segment, and
    display logic on Segment picks the screen (FL_493 -> QID1459, FL_497 -> QID1460).
    The expected screens and phrases are read from the instrument by hand (as in
    studies/targeting_fairness/study.py), not produced by surveyir."""

    survey = load_qsf(FIXTURES / "targeting_fairness.qsf")
    expected = {
        "FL_493": ("QID1459", "advertise the snacks to women directly"),
        "FL_497": ("QID1460", "advertise the snacks broadly to the general public"),
    }
    for arm, (qid, phrase) in expected.items():
        s = stimulus_for_arm(survey, "FL_491", arm)
        assert s.walks == 5 and s.arm == arm and s.sometimes == frozenset()
        assert [d.qid for d in s.displays] == [qid]
        assert s.reasons == {(qid, None): "only"}
        assert phrase in s.text and [d.qid for d in s.screens] == [qid]
        other = next(p for a, (_, p) in expected.items() if a != arm)
        assert other not in s.text


def test_stimulus_for_arm_takes_the_key_or_flow_id_and_finds_piped_content():

    survey = qsf(
        [
            text_screen("QID10", "A neighbour returns your wallet."),
            text_screen("QID11", "A stranger keeps your wallet."),
            text_screen("QID12", "The ${e://Field/who} was seen on camera."),
            ask("QID1", "How much do you trust people around you?"),
        ],
        {"BL_1": ["QID1"], "BL_alpha": ["QID10"], "BL_beta": ["QID11"], "BL_s": ["QID12"]},
        [
            randomizer(
                "FL_2",
                group("FL_3", embedded("FL_30", who="neighbour"), block("FL_31", "BL_alpha")),
                group("FL_4", embedded("FL_40", who="stranger"), block("FL_41", "BL_beta")),
            ),
            block("FL_5", "BL_s"),
            block("FL_6", "BL_1"),
        ],
    )
    s = stimulus_for_arm(survey, "FL_2", "FL_3", walks=2)
    assert [d.qid for d in s.displays] == ["QID10", "QID12"]
    assert s.reasons == {("QID10", None): "only", ("QID12", None): "content"}
    assert "neighbour was seen" in s.text and "stranger" not in s.text

    blocks = qsf(  # arms that are blocks: the key is the block description, not the flow id
        [text_screen("QID10", "Alpha."), text_screen("QID11", "Beta.")],
        {"BL_alpha": ["QID10"], "BL_beta": ["QID11"]},
        [randomizer("FL_2", block("FL_31", "BL_alpha"), block("FL_41", "BL_beta"))],
    )
    by_key = stimulus_for_arm(blocks, "FL_2", "BL_alpha", walks=1)
    by_flow_id = stimulus_for_arm(blocks, "FL_2", "FL_31", walks=1)
    assert by_key == by_flow_id and by_key.arm == "BL_alpha"
    assert [d.qid for d in by_key.displays] == ["QID10"]


def test_stimulus_for_arm_reports_answer_dependent_screens_as_sometimes():
    """QID2 shows under arm FL_3 only to respondents who answer QID1 "Yes": it is
    specific to the arm in the walks that give that answer, and listed as such."""

    survey = qsf(
        [
            ask("QID1", "Do you shop online?"),
            ask(
                "QID2",
                "Would you buy this?",
                DisplayLogic=logic(
                    ed("arm", "EqualTo", "A"),
                    qx("q://QID1/SelectableChoice/1", Conjuction="And"),
                ),
            ),
        ],
        {"BL_1": ["QID1", "|", "QID2"]},
        [
            randomizer(
                "FL_2",
                group("FL_3", embedded("FL_30", arm="A")),
                group("FL_4", embedded("FL_40", arm="B")),
            ),
            block("FL_5", "BL_1"),
        ],
    )

    class Alternating:  # answers "Yes" (choice 1) in even walks, "No" in odd ones
        def __init__(self, walk: int) -> None:
            self.choice = "1" if walk % 2 == 0 else "2"

        def answer(self, ctx):
            return self.choice

    s = stimulus_for_arm(survey, "FL_2", "FL_3", walks=4, make_answerer=Alternating)
    assert [d.qid for d in s.displays] == ["QID2"] and s.sometimes == {("QID2", None)}
    blank = stimulus_for_arm(survey, "FL_2", "FL_3", walks=2)  # no answers: never shown
    assert blank.displays == () and blank.walks == 2


def test_stimulus_for_arm_rejects_order_factors_and_unknown_arms():

    with pytest.raises(ValueError, match="presents 3 of 3"):
        stimulus_for_arm(randomizer_survey(subset=3), "FL_2", "FL_3")
    with pytest.raises(ValueError, match="no arm 'FL_9'"):
        stimulus_for_arm(randomizer_survey(), "FL_2", "FL_9")
    with pytest.raises(ValueError, match="no flow randomizer"):
        stimulus_for_arm(randomizer_survey(), "FL_99", "FL_3")
