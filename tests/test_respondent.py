"""The respondent interface: a respondent model sees what was displayed, and nothing else."""

from __future__ import annotations

import pytest

from surveyir import load_qsf
from surveyir.runtime import RandomAnswerer, ScreenerAwareAnswerer, Simulator, transcript
from surveyir.runtime.trace import Display
from tests.conftest import mc, minimal_qsf

VIGNETTES = {
    "BL_alpha": "A neighbour returns your lost wallet with every banknote inside.",
    "BL_beta": "A stranger keeps the wallet you dropped on the train.",
}
FIELD, VALUES = "treatment_cond", {"BL_alpha": "arm_alpha", "BL_beta": "arm_beta"}


def vignette_survey():
    """A randomizer picks one of two arms. Each sets ``treatment_cond`` and shows a
    text-only vignette in its own block; then everyone answers the same question."""
    screens = [
        {"QuestionID": qid, "QuestionType": "DB", "Selector": "TB",
         "DataExportTag": f"V{n}", "QuestionText": f"<p>{VIGNETTES[block]}</p>"}
        for n, (qid, block) in enumerate([("QID10", "BL_alpha"), ("QID11", "BL_beta")])
    ]
    outcome = mc("QID1", QuestionText="<p>How much do you trust people around you?</p>")
    doc = minimal_qsf([outcome, *screens], flow=[
        {"Type": "BlockRandomizer", "FlowID": "FL_2", "SubSet": 1, "EvenPresentation": True,
         "Flow": [
             {"Type": "Group", "FlowID": fid, "Description": "arm", "Flow": [
                 {"Type": "EmbeddedData", "FlowID": f"{fid}_ed", "EmbeddedData": [
                     {"Field": FIELD, "Type": "Custom", "Value": VALUES[block]}]},
                 {"Type": "Block", "ID": block, "FlowID": f"{fid}_bl"}]}
             for fid, block in (("FL_3", "BL_alpha"), ("FL_4", "BL_beta"))
         ]},
        {"Type": "Block", "ID": "BL_1", "FlowID": "FL_5"},
    ])
    blocks = doc["SurveyElements"][0]["Payload"]
    blocks[0]["BlockElements"] = [{"Type": "Question", "QuestionID": "QID1"}]
    blocks += [{"Type": "Standard", "ID": block, "Description": block, "BlockElements": [
        {"Type": "Question", "QuestionID": qid}]}
        for qid, block in (("QID10", "BL_alpha"), ("QID11", "BL_beta"))]
    return load_qsf(doc)


class Recorder:
    """A mock respondent model: records the prompt and context it was given."""

    def __init__(self) -> None:
        self.prompts: list[str] = []
        self.contexts: list = []

    def answer(self, ctx):
        self.prompts.append(transcript(ctx.history))
        self.contexts.append(ctx)
        return ctx.view.choices[0].id


def test_prompt_holds_the_assigned_vignette_and_no_hidden_state():
    survey = vignette_survey()
    sim = Simulator(survey, seed=3)
    seen = set()
    for _ in range(2):  # evenly presented: the two respondents get one arm each
        model = Recorder()
        run = sim.respondent(model)
        assert not run.privileged
        assigned = run.embedded[FIELD]
        block = next(b for b, v in VALUES.items() if v == assigned)
        other = next(b for b in VALUES if b != block)
        seen.add(block)
        (prompt,), (ctx,) = model.prompts, model.contexts
        assert ctx.view.qid == "QID1" and ctx.respondent_index == run.index
        assert VIGNETTES[block] in prompt and VIGNETTES[other] not in prompt
        assert prompt.index(VIGNETTES[block]) < prompt.index("How much do you trust")
        leaks = [FIELD, *VALUES.values(), "FL_", other]
        assert not [s for s in leaks if s in prompt or s in repr(ctx)]
        # the vignette is in the trace, displayed, but never answered
        screen = next(o for o in ctx.history if isinstance(o, Display) and o.block_id == block)
        assert not screen.responds and (screen.qid, None) not in run.answers
    assert seen == set(VALUES)


@pytest.mark.parametrize("legacy", [
    lambda view, state: None,
    RandomAnswerer(seed=1).__call__,  # a bound (view, state) method is legacy too
])
def test_legacy_answerers_are_privileged(legacy):
    run = Simulator(vignette_survey(), seed=0).respondent(legacy)
    assert run.privileged


def test_builtin_answerers_read_only_the_display_and_match_their_legacy_form():
    survey = load_qsf(minimal_qsf([mc("QID1", selector="MAVR"), mc("QID2")]))
    new = Simulator(survey, seed=5).run(20, ScreenerAwareAnswerer(survey, seed=1))
    old_answerer = ScreenerAwareAnswerer(survey, seed=1)
    old = Simulator(survey, seed=5).run(20, lambda view, state: old_answerer(view, state))
    assert [r.answers for r in new] == [r.answers for r in old]
    assert not any(r.privileged for r in new) and all(r.privileged for r in old)
    assert not Simulator(survey).respondent().privileged  # the default, no_answer
