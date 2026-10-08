"""What a run records: the respondent's trace, the audit, and page-level administration."""

from __future__ import annotations

import dataclasses

import pytest

from surveyir import load_qsf
from surveyir.model import Survey
from surveyir.runtime import (
    ExecutionError,
    RandomAnswerer,
    Simulator,
    executability,
    transcript,
)
from surveyir.runtime.trace import (
    Allowed,
    Approximation,
    BranchEval,
    ChoiceHidden,
    Display,
    EmbeddedSet,
    End,
    Hidden,
    PageStart,
    PageSubmit,
    RandomizerDecision,
    Response,
    SkipTaken,
)
from tests.conftest import mc, minimal_qsf
from tests.test_runtime import ed, logic, qx


class Fixed:
    """A respondent that answers every question with the same value, recording each ctx."""

    def __init__(self, value: object = "2") -> None:
        self.value, self.contexts = value, []

    def answer(self, ctx):
        self.contexts.append(ctx)
        return self.value


def descriptive(qid: str, text: str) -> dict:
    return {
        "QuestionID": qid,
        "QuestionType": "DB",
        "Selector": "TB",
        "DataExportTag": qid.replace("QID", "D"),
        "QuestionText": text,
    }


def page(*qids: str) -> list[dict]:
    """Block elements: questions, with a page break between ``|`` markers."""
    return [
        {"Type": "Page Break"} if q == "|" else {"Type": "Question", "QuestionID": q} for q in qids
    ]


def of(run, kind):
    return [e for e in (*run.trace, *run.audit) if isinstance(e, kind)]


# ------------------------------------------------------------------ trace


def test_descriptive_screen_is_displayed_not_answered():
    survey = load_qsf(
        minimal_qsf(
            [descriptive("QID1", "<p>Read this.</p>"), mc("QID2")],
            block_elements=page("QID1", "|", "QID2"),
        )
    )
    who = Fixed()
    run = Simulator(survey, seed=0).respondent(who)
    assert [type(o).__name__ for o in run.trace] == [
        "PageStart",
        "Display",
        "PageSubmit",
        "PageStart",
        "Display",
        "Response",
        "PageSubmit",
        "End",
    ]
    screen = of(run, Display)[0]
    assert screen.text == "Read this." and not screen.responds
    assert ("QID1", None) not in run.answers and run.answers[("QID2", None)].value == "2"
    assert [c.view.qid for c in who.contexts] == ["QID2"]  # never asked about the screen
    # respondents get the trace without block ids; the run's trace keeps them
    assert screen.block_id == "BL_1"
    assert who.contexts[0].history[1] == dataclasses.replace(screen, block_id="")
    assert of(run, End) == [End("flow_end", True)] and not run.privileged


def test_pages_are_numbered_globally_and_columns_are_rendered():
    matrix = {
        "QuestionID": "QID2",
        "QuestionType": "Matrix",
        "Selector": "Likert",
        "SubSelector": "SingleAnswer",
        "DataExportTag": "Q2",
        "QuestionText": "Rate",
        "Choices": {"1": {"Display": "Row"}},
        "ChoiceOrder": [1],
        "Answers": {
            "1": {"Display": "Like ${q://QID1/ChoiceGroup/SelectedChoices}"},
            "2": {"Display": "Dislike"},
        },
        "AnswerOrder": [1, 2],
        "Validation": {"Settings": {"ForceResponse": "ON", "Type": "None"}},
    }
    survey = load_qsf(
        minimal_qsf(
            [mc("QID1"), matrix],
            block_elements=page("QID1", "|", "QID2"),
            block_options={
                "Looping": "Static",
                "LoopingOptions": {"Static": {"1": {"1": "a"}, "2": {"1": "b"}}},
            },
        )
    )
    run = Simulator(survey, seed=0).respondent(RandomAnswerer(seed=1))
    starts = of(run, PageStart)
    assert [p.page for p in starts] == [1, 2, 3, 4] == [s.page for s in of(run, PageSubmit)]
    assert [(p.loop_id, p.loop_number, p.loop_total) for p in starts][:2] == [("1", 1, 2)] * 2
    grid = next(d for d in of(run, Display) if d.qid == "QID2")
    picked = run.answers[("QID1", grid.loop_id)].value
    assert grid.columns[0].text == f"Like {'Yes' if picked == '1' else 'No'}"
    assert grid.required == "force" and grid.mode == "single" and grid.multiple is False


def test_randomizer_decisions_record_balancing_history():
    from tests.test_runtime import randomizer_survey

    survey = randomizer_survey()
    assert isinstance(survey, Survey)
    sim = Simulator(survey, seed=1)
    first, second = sim.run(2, RandomAnswerer(seed=2))
    d1 = next(e for e in first.audit if isinstance(e, RandomizerDecision) and e.kind == "flow")
    assert d1.p_nominal == {"FL_3": 1 / 3, "FL_4": 1 / 3, "FL_5": 1 / 3}
    assert d1.p_given_history == d1.p_nominal and d1.balancer_before == dict.fromkeys(d1.options, 0)
    d2 = next(e for e in second.audit if isinstance(e, RandomizerDecision) and e.kind == "flow")
    assert d2.p_given_history is not None and d2.p_given_history[d1.shown[0]] == 0.0
    assert sorted(d2.p_given_history.values()) == [0.0, 0.5, 0.5]
    arm = first.embedded["arm"]
    assert EmbeddedSet("arm", arm, d1.shown[0]) in first.audit
    assert BranchEval("FL_6", arm == "B") in first.audit
    if arm == "B":  # choice order of the shown question is audited too
        assert any(isinstance(e, RandomizerDecision) and e.kind == "choices" for e in first.audit)


def test_approximations_are_audited_and_noted():
    flow = [
        {
            "Type": "WebService",
            "FlowID": "FL_2",
            "URL": "https://x",
            "Method": "GET",
            "ResponseMap": [{"key": "r", "value": "seed"}],
        },
        {
            "Type": "EmbeddedData",
            "FlowID": "FL_3",
            "EmbeddedData": [{"Field": "pid", "Type": "Recipient"}],
        },
        {"Type": "Block", "ID": "BL_1", "FlowID": "FL_4"},
    ]
    survey = load_qsf(
        minimal_qsf([mc(QuestionJS="Qualtrics.SurveyEngine.setEmbeddedData('x')")], flow=flow)
    )
    run = Simulator(survey, strict=False).respondent()
    codes = [(a.code, a.location, a.affects) for a in of(run, Approximation)]
    # pid is declared but never read, so its being unset changes nothing
    assert codes == [("web_service", "FL_2", "assignment"), ("javascript", "QID1", "assignment")]
    assert [a for a in run.audit if isinstance(a, Approximation)] == run.state.approximations
    assert all(a.detail in run.notes for a in run.state.approximations)
    assert not of(run, Allowed)
    supplied = Simulator(survey, strict=False).respondent(embedded={"pid": "p1"})
    assert EmbeddedSet("pid", "p1", "respondent") in supplied.audit
    allowed = Simulator(survey, allow={"web_service", "javascript:QID1"}).run(2)
    for r in allowed:  # each respondent records each allowed gap once
        assert of(r, Allowed) == [Allowed("web_service", "FL_2"), Allowed("javascript", "QID1")]


def test_unset_fields_are_recorded_where_they_are_read():
    flow = [
        {
            "Type": "EmbeddedData",
            "FlowID": "FL_2",
            "EmbeddedData": [
                {"Field": "pid", "Type": "Recipient"},
                {"Field": "src", "Type": "Recipient"},
                {"Field": "unused", "Type": "Recipient"},
            ],
        },
        {
            "Type": "Branch",
            "FlowID": "FL_3",
            "Description": "b",
            "BranchLogic": logic(ed("pid", "EqualTo", "1")),
            "Flow": [{"Type": "Block", "ID": "BL_1", "FlowID": "FL_4"}],
        },
        {"Type": "Block", "ID": "BL_1", "FlowID": "FL_5"},
    ]
    survey = load_qsf(minimal_qsf([mc(QuestionText="From ${e://Field/src}")], flow=flow))
    run = Simulator(survey, strict=False).respondent()
    got = {(a.code, a.location, a.affects) for a in of(run, Approximation)}
    assert got == {("embedded.unset", "pid", "routing"), ("embedded.unset", "src", "exposure")}
    report = executability(survey)
    assert {(f.code, f.location, f.affects) for f in report.features if f.code} == got
    with pytest.raises(ExecutionError, match="embedded.unset at pid"):
        Simulator(survey).respondent()
    done = Simulator(survey, implementations={"embedded": {"pid": "1", "src": "x"}}).respondent()
    assert not of(done, Approximation) and done.trace[1].text == "From x"
    assert EmbeddedSet("pid", "1", "respondent") in done.audit


def test_choice_display_logic_and_piped_rand_do_not_shift_seeds():
    plain = mc(
        "QID1",
        Choices={
            "1": {"Display": "Yes"},
            "2": {"Display": "No"},
            "3": {"Display": "Maybe", "DisplayLogic": logic(ed("x", "EqualTo", "1"))},
        },
        ChoiceOrder=[1, 2, 3],
        Randomization={"Type": "All"},
    )
    piped = {**plain, "QuestionText": "Pick ${rand://int/1:1000}"}
    runs = [
        Simulator(load_qsf(minimal_qsf([q])), seed=4).run(5, RandomAnswerer(seed=1))
        for q in (plain, piped)
    ]
    assert [r.choice_order for r in runs[0]] == [r.choice_order for r in runs[1]]
    assert [r.answers for r in runs[0]] == [r.answers for r in runs[1]]
    assert ChoiceHidden("QID1", None, "3") in runs[0][0].audit
    shown = of(runs[1][0], Display)[0]
    assert shown.text.startswith("Pick ") and "rand://" not in shown.text + shown.html


# ------------------------------------------------------------------ pages


def test_same_page_display_logic_without_in_page_flag_is_hidden():
    q2 = mc("QID2", DisplayLogic=logic(qx("q://QID1/SelectableChoice/2")))
    survey = load_qsf(minimal_qsf([mc("QID1"), q2]))
    run = Simulator(survey, seed=0).respondent(Fixed("2"))
    assert run.displayed == [("QID1", None)]
    assert Hidden("QID2", None, "display_logic") in run.audit


def test_in_page_display_logic_reveals_after_the_answer():
    q2 = mc("QID2", InPageDisplayLogic=logic(qx("q://QID1/SelectableChoice/2")))
    survey = load_qsf(minimal_qsf([mc("QID1"), q2, mc("QID3")]))
    who = Fixed("2")
    run = Simulator(survey, seed=0).respondent(who)
    assert run.displayed == [("QID1", None), ("QID3", None), ("QID2", None)]
    revealed = of(run, Display)[-1]
    assert revealed.qid == "QID2" and revealed.revealed_in_page and revealed.page == 1
    assert [c.view.qid for c in who.contexts] == ["QID1", "QID2", "QID3"]  # page order
    assert [d.qid for d in who.contexts[0].page] == ["QID1", "QID3"]
    assert [d.qid for d in who.contexts[1].page] == ["QID1", "QID2", "QID3"]
    hidden = Simulator(survey, seed=0).respondent(Fixed("1"))
    assert ("QID2", None) not in hidden.displayed
    assert Hidden("QID2", None, "in_page_display_logic") in hidden.audit


def test_same_page_carry_forward_is_empty():
    src = mc("QID1", selector="MAVR")
    dst = mc(
        "QID2",
        Choices={},
        ChoiceOrder=[],
        DynamicChoices={
            "Type": "Dynamic",
            "DynamicType": "ChoiceGroup",
            "Locator": "q://QID1/ChoiceGroup/SelectedChoices",
        },
    )
    run = Simulator(load_qsf(minimal_qsf([src, dst])), seed=0).respondent(Fixed(["1"]))
    assert of(run, Display)[1].choices == ()


def test_skip_logic_acts_at_submit():
    q1 = mc("QID1")
    elements = [
        {
            "Type": "Question",
            "QuestionID": "QID1",
            "SkipLogic": [
                {
                    "ChoiceLocator": "q://QID1/SelectableChoice/1",
                    "Condition": "Selected",
                    "QuestionID": "QID1",
                    "SkipToDestination": "ENDOFSURVEY",
                }
            ],
        },
        *page("QID2", "|", "QID3"),
    ]
    survey = load_qsf(minimal_qsf([q1, mc("QID2"), mc("QID3")], block_elements=elements))
    run = Simulator(survey, seed=0).respondent(Fixed("1"))
    assert run.displayed == [("QID1", None), ("QID2", None)]  # same page: still shown
    assert ("QID2", None) in run.answers and run.ended_by == "skip logic"
    assert SkipTaken("QID1", None, "end_of_survey") in run.audit
    assert Hidden("QID3", None, "end") in run.audit
    assert isinstance(run.trace[-2], PageSubmit) and run.trace[-1] == End("skip_logic", True)
    assert isinstance(run.trace[-3], Response)


def test_transcript_is_ordered_and_neutral():
    survey = load_qsf(
        minimal_qsf(
            [descriptive("QID1", "Imagine a rainy day."), mc("QID2")],
            block_elements=page("QID1", "|", "QID2"),
        )
    )
    run = Simulator(survey, seed=0).respondent(Fixed("1"))
    assert transcript(run.trace) == (
        "--- Page 1 ---\n(QID1) Imagine a rainy day.\n\n--- Page 2 ---\n(QID2) Pick one\n"
        "  [1] Yes\n  [2] No\nAnswer (QID2): [1] Yes\n\n--- End of survey ---"
    )
    assert "QID" not in transcript(run.trace, include_ids=False)
