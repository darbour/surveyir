"""Strict execution in the simulator: the default, allow, implementations, and the
cheap fixes (column balancing, quota status, JavaScript classification, validation)."""

from __future__ import annotations

import warnings
from collections import Counter

import pytest

from surveyir import load_qsf
from surveyir.model import MatrixQuestion, QuotaNode, Randomization
from surveyir.runtime import (
    ExecutionError,
    ExecutionPolicy,
    RandomAnswerer,
    ScreenerAwareAnswerer,
    Simulator,
    executability,
)
from surveyir.runtime.trace import Allowed, Approximation, Display, EmbeddedSet
from surveyir.runtime.walker import validation_errors
from tests.conftest import QSF_FILES, mc, minimal_qsf
from tests.test_runtime import ed, logic

JS = "Qualtrics.SurveyEngine.setEmbeddedData('arm', Math.random() < .5 ? 'A' : 'B');"


def of(run, kind):
    return [e for e in (*run.trace, *run.audit) if isinstance(e, kind)]


def js_survey(js: str = JS):
    """QID1 runs JavaScript that sets ``arm``; a branch on ``arm`` shows QID2."""
    flow = [
        {"Type": "Block", "ID": "BL_1", "FlowID": "FL_2"},
        {
            "Type": "Branch",
            "FlowID": "FL_3",
            "Description": "b",
            "BranchLogic": logic(ed("arm", "EqualTo", "A")),
            "Flow": [{"Type": "Block", "ID": "BL_2", "FlowID": "FL_4"}],
        },
    ]
    doc = minimal_qsf([mc("QID1", QuestionJS=js), mc("QID2")], flow=flow)
    doc["SurveyElements"][0]["Payload"][0]["BlockElements"] = [
        {"Type": "Question", "QuestionID": "QID1"}
    ]
    doc["SurveyElements"][0]["Payload"].append(
        {
            "Type": "Standard",
            "ID": "BL_2",
            "Description": "Arm A",
            "BlockElements": [{"Type": "Question", "QuestionID": "QID2"}],
        }
    )
    return load_qsf(doc)


# ------------------------------------------------------------------ the default


def test_strict_by_default_raises_with_the_partial_run():
    with pytest.raises(ExecutionError, match="javascript at QID1") as err:
        Simulator(js_survey()).respondent()
    run = err.value.run
    assert [d.qid for d in of(run, Display)] == ["QID1"] and not of(run, Approximation)


def test_allow_records_allowed_once_per_respondent():
    for allow in ({"javascript"}, {"javascript:QID1"}, "javascript"):
        runs = Simulator(js_survey(), allow=allow).run(3)
        for run in runs:
            assert of(run, Allowed) == [Allowed("javascript", "QID1")]
            assert [a.code for a in of(run, Approximation)] == ["javascript"]
    with pytest.raises(ExecutionError):
        Simulator(js_survey(), allow={"javascript:QID9"}).respondent()


def test_permissive_and_policy():
    run = Simulator(js_survey(), strict=False).respondent()
    assert [(a.code, a.affects) for a in run.state.approximations] == [("javascript", "assignment")]
    assert not of(run, Allowed)
    policy = ExecutionPolicy(allow=frozenset({"javascript"}))
    assert Simulator(js_survey(), policy=policy).respondent().finished
    with pytest.raises(ValueError, match="either policy"):
        Simulator(js_survey(), policy=policy, strict=False)


def test_strict_rejects_one_seed_for_every_respondent():
    sim = Simulator(js_survey(), allow={"javascript"})
    with pytest.raises(ValueError, match="reuses one seed"):
        sim.run(3, seed=1)
    assert len(sim.run(2)) == 2 and sim.respondent(seed=1)  # one respondent may be seeded
    assert len(Simulator(js_survey(), strict=False).run(2, seed=1)) == 2


def test_comment_only_javascript_is_not_a_gap():
    template = (
        "Qualtrics.SurveyEngine.addOnload(function()\n{\n\t/*Place your JavaScript here*/\n});"
    )
    run = Simulator(js_survey(template)).respondent()
    assert not run.state.approximations


# ------------------------------------------------------------------ implementations


def test_javascript_implementation_runs_when_displayed():
    calls = []

    def script(state):
        calls.append(sorted(k for k, _ in state.answers))
        return {"arm": "A"}

    run = Simulator(js_survey(), implementations={"javascript": {"QID1": script}}).respondent(
        RandomAnswerer(seed=1)
    )
    assert calls == [[]]  # at display, before QID1 is answered
    assert EmbeddedSet("arm", "A", "javascript:QID1") in run.audit
    assert run.embedded["arm"] == "A" and ("QID2", None) in run.displayed
    assert not run.state.approximations


def test_location_feeds_pipes_and_geoip():
    geo = {
        "LogicType": "GeoIP",
        "LeftOperand": "CountryName",
        "Operator": "EqualTo",
        "RightOperand": "Australia",
        "Type": "Expression",
    }
    flow = [
        {
            "Type": "Branch",
            "FlowID": "FL_3",
            "Description": "b",
            "BranchLogic": logic(geo),
            "Flow": [{"Type": "Block", "ID": "BL_1", "FlowID": "FL_4"}],
        }
    ]
    survey = load_qsf(minimal_qsf([mc(QuestionText="Hello from ${loc://City}")], flow=flow))
    report = executability(survey)
    assert {f.code for f in report.blocking(ExecutionPolicy())} == {"logic.geo_ip", "pipe.loc"}
    with pytest.raises(ExecutionError, match="logic.geo_ip"):
        Simulator(survey).respondent()
    where = {"CountryName": "Australia", "City": "Perth"}
    for sim in (
        Simulator(survey, location=where),
        Simulator(survey, implementations={"location": where}),
    ):
        run = sim.respondent()
        assert of(run, Display)[0].text == "Hello from Perth" and not run.state.approximations
    assert report.blocking(ExecutionPolicy(implementations={"location": where})) == []


def test_embedded_implementation_merges_with_respondent_fields():
    flow = [
        {
            "Type": "EmbeddedData",
            "FlowID": "FL_2",
            "EmbeddedData": [
                {"Field": "pid", "Type": "Recipient"},
                {"Field": "src", "Type": "Recipient"},
            ],
        },
        {"Type": "Block", "ID": "BL_1", "FlowID": "FL_3"},
    ]
    survey = load_qsf(
        minimal_qsf([mc(QuestionText="${e://Field/pid} ${e://Field/src}")], flow=flow)
    )
    sim = Simulator(survey, implementations={"embedded": {"pid": "p0", "src": "panel"}})
    run = sim.respondent(embedded={"pid": "p1"})
    assert of(run, Display)[0].text == "p1 panel" and not run.state.approximations
    assert EmbeddedSet("src", "panel", "respondent") in run.audit


def test_unset_field_piped_into_another_field_affects_what_that_field_affects():
    def survey(read_by_branch: bool):
        flow = [
            {
                "Type": "EmbeddedData",
                "FlowID": "FL_2",
                "EmbeddedData": [
                    {"Field": "pid", "Type": "Recipient"},
                    {"Field": "tag", "Type": "Custom", "Value": "id-${e://Field/pid}"},
                ],
            },
            {"Type": "Block", "ID": "BL_1", "FlowID": "FL_3"},
        ]
        if read_by_branch:
            flow.append(
                {
                    "Type": "Branch",
                    "FlowID": "FL_4",
                    "Description": "b",
                    "BranchLogic": logic(ed("tag", "EqualTo", "id-")),
                    "Flow": [],
                }
            )
        return load_qsf(minimal_qsf([mc()], flow=flow))

    unread = survey(False)
    assert executability(unread).blocking(ExecutionPolicy()) == []
    run = Simulator(unread).respondent()  # recorded, affecting nothing
    assert [(a.code, a.affects) for a in run.state.approximations] == [("embedded.unset", "none")]
    branched = survey(True)
    [row] = executability(branched).blocking(ExecutionPolicy())
    assert (row.code, row.location, row.affects) == ("embedded.unset", "pid", "routing")
    with pytest.raises(ExecutionError) as err:
        Simulator(branched).respondent()
    assert (err.value.approximation.location, err.value.approximation.affects) == ("pid", "routing")


# ------------------------------------------------------------------ check agrees with runs


@pytest.mark.parametrize("path", QSF_FILES, ids=[p.stem for p in QSF_FILES])
def test_check_strict_agrees_with_strict_runs(path):
    """``executability(s).blocking(policy)`` is empty exactly when a strict run completes.

    Both directions hold for every fixture with ``ScreenerAwareAnswerer``, which gets
    through text screeners (with random text, promiscuous_donors' JavaScript sits
    behind a screener 20 respondents never pass). In general ``blocking`` is static
    and can list gaps a sample never reaches, and a run can meet two gaps no static
    check predicts: ``answer.invalid`` (depends on answers; ``RandomAnswerer``
    respects validation) and ``loop.no_displayed_choices``.
    """
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        survey = load_qsf(path)
    blocking = executability(survey).blocking(ExecutionPolicy())
    sim = Simulator(survey, seed=1)
    try:
        sim.run(20, ScreenerAwareAnswerer(survey, seed=2))
    except ExecutionError as e:
        assert (e.approximation.code, e.approximation.location) in {
            (f.code, f.location) for f in blocking
        }
    else:
        assert blocking == []
    allow = {f"{f.code}:{f.location}" for f in blocking}
    Simulator(survey, seed=1, allow=allow).run(20, ScreenerAwareAnswerer(survey, seed=2))


# ------------------------------------------------------------------ cheap fixes


def test_evenly_presented_columns_are_balanced():
    matrix = {
        "QuestionID": "QID1",
        "QuestionType": "Matrix",
        "Selector": "Likert",
        "SubSelector": "SingleAnswer",
        "DataExportTag": "Q1",
        "QuestionText": "Rate",
        "Choices": {"1": {"Display": "Row"}},
        "ChoiceOrder": [1],
        "Answers": {str(i): {"Display": f"C{i}"} for i in range(1, 5)},
        "AnswerOrder": [1, 2, 3, 4],
    }
    survey = load_qsf(minimal_qsf([matrix]))
    q = survey.questions["QID1"]
    assert isinstance(q, MatrixQuestion)
    q.column_randomization = Randomization(mode="subset", subset_size=1, even_presentation=True)
    runs = Simulator(survey, seed=3).run(40)
    assert Counter(r.column_order[("QID1", None)][0] for r in runs) == dict.fromkeys(
        ["1", "2", "3", "4"], 10
    )
    assert not any(r.state.approximations for r in runs)
    assert "columns.unbalanced" not in {f.code for f in executability(survey).features}


def test_quota_status_is_current_at_each_quota_check():
    """A respondent counts toward a quota when their response is recorded (as in
    Qualtrics), so the one who fills it is not screened out by it; the next is, and
    is not counted."""
    doc = minimal_qsf(
        [mc("QID1"), mc("QID2")],
        flow=[
            {"Type": "Block", "ID": "BL_1", "FlowID": "FL_2"},
            {"Type": "Quota", "FlowID": "FL_3", "Description": "check"},
        ],
    )
    doc["SurveyElements"].append(
        {
            "Element": "QO",
            "Payload": {
                "ID": "QO_1",
                "Name": "yes",
                "Occurrences": 1,
                "QuotaAction": "EndCurrentSurvey",
                "Logic": logic(
                    {
                        "LogicType": "Question",
                        "LeftOperand": "q://QID1/SelectableChoice/1",
                        "Operator": "Selected",
                        "Type": "Expression",
                    }
                ),
            },
        }
    )
    survey = load_qsf(doc)
    assert any(isinstance(n, QuotaNode) for n in survey.flow)
    sim = Simulator(survey, seed=0)
    first, second = sim.run(2, lambda view, state: "1")
    assert first.finished and first.ended_by is None
    assert not second.finished and second.ended_by == "quota QO_1"
    assert sim.quota_counts == {"QO_1": 1}


# ------------------------------------------------------------------ validation


def te(qid: str, **settings):
    return {
        "QuestionID": qid,
        "QuestionType": "TE",
        "Selector": "SL",
        "DataExportTag": qid.replace("QID", "T"),
        "QuestionText": "Type",
        "Validation": {"Settings": {"ForceResponse": "ON", **settings}},
    }


def validated_survey():
    age = te(
        "QID1",
        Type="ContentType",
        ContentType="ValidNumber",
        ValidNumber={"Min": "18", "Max": "99", "NumDecimals": ""},
    )
    count = te("QID2", Type="ContentType", ContentType="ValidNumber", ValidNumber={})
    essay = te("QID3", Type="MinChar", MinChars="30")
    email = te("QID4", Type="ContentType", ContentType="ValidEmail")
    pick = mc(
        "QID5",
        selector="MAVR",
        Choices={str(i): {"Display": f"c{i}"} for i in range(1, 7)},
        ChoiceOrder=list(range(1, 7)),
        Validation={
            "Settings": {
                "ForceResponse": "ON",
                "Type": "ChoiceRange",
                "MinChoices": "2",
                "MaxChoices": "3",
            }
        },
    )
    flow = [
        {"Type": "Block", "ID": "BL_1", "FlowID": "FL_2"},
        {
            "Type": "Branch",
            "FlowID": "FL_3",
            "Description": "adult",
            "BranchLogic": logic(
                {
                    "LogicType": "Question",
                    "LeftOperand": "q://QID1/ChoiceTextEntryValue",
                    "Operator": "GreaterThanOrEqual",
                    "RightOperand": "18",
                    "Type": "Expression",
                }
            ),
            "Flow": [],
        },
    ]
    return load_qsf(minimal_qsf([age, count, essay, email, pick], flow=flow))


def test_random_answers_respect_displayed_validation():
    survey = validated_survey()
    for answerer in (RandomAnswerer(seed=1), ScreenerAwareAnswerer(survey, seed=1)):
        for run in Simulator(survey, seed=4).run(30, answerer):
            a = {k[0]: v.value for k, v in run.answers.items()}
            assert 18 <= int(a["QID1"]) <= 99 and 0 <= int(a["QID2"]) <= 100
            assert len(a["QID3"]) >= 30 and "@" in a["QID4"] and 2 <= len(a["QID5"]) <= 3
            assert not run.state.approximations
            for d in of(run, Display):
                assert validation_errors(d, run.answers[(d.qid, None)].value) == []


def test_invalid_answers_are_outcome_approximations():
    survey = validated_survey()
    with pytest.raises(ExecutionError, match="answer.invalid at QID1"):
        Simulator(survey).respondent(lambda view, state: "text")
    run = Simulator(survey, strict=False).respondent(lambda view, state: "text")
    got = {(a.code, a.location, a.affects) for a in run.state.approximations}
    assert ("answer.invalid", "QID1", "outcome") in got
    assert ("logic.non_numeric", "QID1", "routing") in got  # what the bad answer causes
    assert ("answer.invalid", "QID5", "outcome") not in got  # "text" is no selection
    run = Simulator(survey, allow={"answer.invalid"}).respondent(lambda view, state: "42")
    assert {a.location for a in of(run, Allowed)} == {"QID3", "QID4"}
