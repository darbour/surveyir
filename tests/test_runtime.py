"""Runtime: logic evaluation, piped text, ordering, the respondent walker, rows, design."""

from __future__ import annotations

import random
from collections import Counter

import pytest

from surveyir import load_qsf
from surveyir.model import Randomization
from surveyir.runtime import (
    Answer,
    Counterbalancer,
    RandomAnswerer,
    RespondentState,
    Simulator,
    arrange,
    design,
    evaluate,
    render,
)
from surveyir.text import make_text
from tests.conftest import mc, minimal_qsf


def logic(*exprs: dict, kind: str = "If") -> dict:
    s = {str(i): e for i, e in enumerate(exprs)}
    s["Type"] = kind
    return {"0": s, "Type": "BooleanExpression"}


def ed(field: str, op: str, right: str, **kw) -> dict:
    return {
        "LogicType": "EmbeddedField",
        "LeftOperand": field,
        "Operator": op,
        "RightOperand": right,
        "Type": "Expression",
        **kw,
    }


def qx(locator: str, op: str = "Selected", right: str | None = None, **kw) -> dict:
    e = {"LogicType": "Question", "LeftOperand": locator, "Operator": op, "Type": "Expression"}
    if right is not None:
        e["RightOperand"] = right
    e.update(kw)
    return e


def branch_survey(*exprs: dict) -> object:
    flow = [
        {
            "Type": "Branch",
            "FlowID": "FL_2",
            "BranchLogic": logic(*exprs),
            "Flow": [{"Type": "Block", "ID": "BL_1", "FlowID": "FL_3"}],
        }
    ]
    return load_qsf(minimal_qsf([mc()], flow=flow))


def cond(*exprs: dict):
    return branch_survey(*exprs).flow[0].condition


# ------------------------------------------------------------------ logic


@pytest.mark.parametrize(
    "expr,embedded,expected",
    [
        (ed("arm", "EqualTo", "2"), {"arm": "2"}, True),
        (ed("arm", "EqualTo", "2"), {"arm": "2.0"}, True),  # numeric when both parse
        (ed("arm", "EqualTo", "A"), {"arm": "a"}, False),  # textual is case-sensitive
        (ed("arm", "EqualTo", "A", IgnoreCase=1), {"arm": "a"}, True),
        (ed("arm", "NotEqualTo", "A"), {}, True),
        (ed("n", "GreaterThan", "10"), {"n": "9"}, False),
        (ed("n", "GreaterThan", "10"), {"n": "x"}, False),
        (ed("n", "LessThanOrEqual", "10"), {"n": "10"}, True),
        (ed("g", "MatchesRegex", "C.T"), {"g": "aCxTb"}, True),
        (ed("g", "Contains", "CT"), {"g": "NANT"}, False),
        (ed("g", "DoesNotContain", "CT"), {"g": "NANT"}, True),
        (
            {
                "LogicType": "EmbeddedField",
                "LeftOperand": "g",
                "Operator": "Empty",
                "Type": "Expression",
            },
            {},
            True,
        ),
    ],
)
def test_embedded_comparisons(expr, embedded, expected):
    assert evaluate(cond(expr), RespondentState(embedded=embedded)) is expected


def test_choice_matrix_text_and_display_operands():
    state = RespondentState()
    state.answers[("QID1", None)] = Answer(value="2")
    state.answers[("QID2", None)] = Answer(value=["1", "3"])
    state.answers[("QID3", None)] = Answer(value={"1": "4", "2": "5"})
    state.answers[("QID4", None)] = Answer(value="hello", text={})
    state.answers[("QID5", None)] = Answer(value="9", text={"9": "other text"})
    state.displayed |= {("QID1", None)}
    cases = [
        (qx("q://QID1/SelectableChoice/2"), True),
        (qx("q://QID1/SelectableChoice/1"), False),
        (qx("q://QID1/SelectableChoice/1", "NotSelected"), True),
        (qx("q://QID2/SelectableChoice/3"), True),
        (qx("q://QID2/SelectedChoicesCount", "GreaterThan", "1"), True),
        (qx("q://QID3/SelectableChoice/2/5"), True),
        (qx("q://QID3/SelectableChoice/2/4"), False),
        (qx("q://QID3/SelectableAnswer/4"), True),
        (qx("q://QID4/ChoiceTextEntryValue", "Contains", "ell"), True),
        (qx("q://QID5/ChoiceTextEntryValue/9", "EqualTo", "other text"), True),
        (qx("q://QID1/QuestionDisplayed", "Displayed"), True),
        (qx("q://QID9/QuestionDisplayed", "Displayed"), False),
    ]
    for expr, expected in cases:
        assert evaluate(cond(expr), state) is expected, expr


def test_and_or_precedence_and_device_and_unknowns():
    c = cond(
        ed("a", "EqualTo", "1"),
        {**ed("b", "EqualTo", "1"), "Conjuction": "Or"},
        {**ed("c", "EqualTo", "1"), "Conjuction": "And"},
    )
    assert evaluate(c, RespondentState(embedded={"a": "1"}))
    assert not evaluate(c, RespondentState(embedded={"b": "1"}))
    assert evaluate(c, RespondentState(embedded={"b": "1", "c": "1"}))
    dev = cond(
        {"LogicType": "DeviceType", "Operator": "Is", "LeftOperand": "mobile", "Type": "Expression"}
    )
    assert evaluate(dev, RespondentState(device="mobile"))
    state = RespondentState()
    geo = cond(
        {
            "LogicType": "GeoIP",
            "LeftOperand": "Country",
            "Operator": "EqualTo",
            "RightOperand": "US",
            "Type": "Expression",
        }
    )
    assert evaluate(geo, state) is False and state.notes


# ------------------------------------------------------------------ pipes


def test_render_pipes():
    state = RespondentState(embedded={"name": "Ada"})
    state.answers[("QID1", None)] = Answer(value="hi there")
    text = make_text(
        "${e://Field/name} said ${q://QID1/ChoiceTextEntryValue} ${rand://int/3:3} ${loc://City}"
    )
    state.location["City"] = "Paris"
    assert render(text, state, rng=random.Random(0)) == "Ada said hi there 3 Paris"


# ------------------------------------------------------------------ ordering


def test_arrange_modes():
    rng = random.Random(1)
    ids = ["a", "b", "c", "d"]
    assert arrange(ids, None, rng) == ids
    assert sorted(arrange(ids, Randomization(mode="all"), rng)) == ids
    sub = arrange(ids, Randomization(mode="subset", subset_size=2), rng)
    assert len(sub) == 2 and set(sub) <= set(ids)
    adv = Randomization(mode="advanced", slots=["a", "*", "*", "d"], randomized=["b", "c"])
    for _ in range(20):
        out = arrange(ids, adv, rng)
        assert out[0] == "a" and out[3] == "d" and sorted(out[1:3]) == ["b", "c"]
    capped = Randomization(
        mode="advanced", slots=["*", "*", "*"], randomized=["a", "b", "c"], subset_size=2
    )
    assert len(arrange(ids, capped, rng)) == 2 + 1  # 2 random + unmentioned "d"


def test_counterbalancer_is_least_filled_and_serializable():
    cb = Counterbalancer()
    rng = random.Random(0)
    picks = Counter(cb.choose("X", ["a", "b", "c"], 1, rng)[0] for _ in range(300))
    assert set(picks.values()) == {100}
    again = Counterbalancer.from_dict(cb.to_dict())
    assert again.counts == cb.counts


# ------------------------------------------------------------------ walker


def randomizer_survey(subset: int = 1, even: bool = True) -> object:
    flow = [
        {
            "Type": "BlockRandomizer",
            "FlowID": "FL_2",
            "SubSet": subset,
            "EvenPresentation": even,
            "Flow": [
                {
                    "Type": "EmbeddedData",
                    "FlowID": "FL_3",
                    "EmbeddedData": [{"Field": "arm", "Type": "Custom", "Value": "A"}],
                },
                {
                    "Type": "EmbeddedData",
                    "FlowID": "FL_4",
                    "EmbeddedData": [{"Field": "arm", "Type": "Custom", "Value": "B"}],
                },
                {
                    "Type": "EmbeddedData",
                    "FlowID": "FL_5",
                    "EmbeddedData": [{"Field": "arm", "Type": "Custom", "Value": "C"}],
                },
            ],
        },
        {
            "Type": "Branch",
            "FlowID": "FL_6",
            "BranchLogic": logic(ed("arm", "EqualTo", "B")),
            "Flow": [{"Type": "Block", "ID": "BL_1", "FlowID": "FL_7"}],
        },
    ]
    return load_qsf(minimal_qsf([mc(Randomization={"Type": "All"})], flow=flow))


def test_between_subjects_assignment_is_balanced_and_drives_branches():
    survey = randomizer_survey()
    runs = Simulator(survey, seed=1).run(300, RandomAnswerer(seed=2))
    arms = Counter(r.embedded["arm"] for r in runs)
    assert arms == {"A": 100, "B": 100, "C": 100}  # evenly presented
    shown = [r for r in runs if ("QID1", None) in r.state.displayed]
    assert {r.embedded["arm"] for r in shown} == {"B"} and len(shown) == 100


def test_without_even_presentation_assignment_is_random():
    runs = Simulator(randomizer_survey(even=False), seed=3).run(600)
    counts = Counter(r.embedded["arm"] for r in runs)
    assert set(counts) == {"A", "B", "C"} and max(counts.values()) - min(counts.values()) > 2


def test_runs_are_reproducible_from_seed_and_counter_state():
    survey = randomizer_survey()
    a = Simulator(survey, seed=5).run(10, RandomAnswerer(seed=1))
    b = Simulator(survey, seed=5).run(10, RandomAnswerer(seed=1))
    assert [r.flow_order for r in a] == [r.flow_order for r in b]
    assert [r.answers for r in a] == [r.answers for r in b]


def test_skip_logic_display_logic_and_end_survey():
    q1 = mc("QID1")
    q2 = mc("QID2", DisplayLogic=logic(qx("q://QID1/SelectableChoice/2")))
    q3 = mc("QID3")
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
        {"Type": "Question", "QuestionID": "QID2"},
        {"Type": "Page Break"},
        {"Type": "Question", "QuestionID": "QID3"},
    ]
    survey = load_qsf(minimal_qsf([q1, q2, q3], block_elements=elements))
    sim = Simulator(survey, seed=0)
    yes = sim.respondent(lambda v, s: "1")
    assert yes.displayed == [("QID1", None)] and yes.ended_by == "skip logic"
    no = sim.respondent(lambda v, s: "2")
    assert no.displayed == [("QID1", None), ("QID2", None), ("QID3", None)]


def test_loop_and_merge_and_piped_loop_fields():
    q = mc("QID1", QuestionText="Rate ${lm://Field/1}")
    doc = minimal_qsf(
        [q],
        block_options={
            "Looping": "Static",
            "LoopingOptions": {
                "Static": {"1": {"1": "apples"}, "2": {"1": "pears"}},
                "Randomization": "All",
            },
        },
    )
    survey = load_qsf(doc)
    seen = []
    run = Simulator(survey, seed=0).respondent(lambda v, s: seen.append(v.text) or "1")
    assert sorted(seen) == ["Rate apples", "Rate pears"]
    assert sorted(run.loops["BL_1"]) == ["1", "2"]
    row = run.row(survey)
    assert row["1_Q1"] == 1 and row["2_Q1"] == 1


def test_carry_forward_uses_previous_answer():
    src = mc(
        "QID1",
        selector="MAVR",
        Choices={"1": {"Display": "a"}, "2": {"Display": "b"}, "3": {"Display": "c"}},
        ChoiceOrder=[1, 2, 3],
    )
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
    survey = load_qsf(minimal_qsf([src, dst]))
    views = {}

    def answer(view, state):
        views[view.question.id] = [c.id for c in view.choices]
        return ["1", "3"] if view.question.id == "QID1" else view.choices[0].id

    Simulator(survey, seed=0).respondent(answer)
    assert views["QID2"] == ["x1", "x3"]


def test_rows_record_display_order_positions():
    survey = randomizer_survey()
    run = Simulator(survey, seed=2).respondent(RandomAnswerer(seed=1))
    row = run.row(survey)
    flow_cols = {k: v for k, v in row.items() if k.startswith("FL_2_DO_")}
    assert sorted(v for v in flow_cols.values() if v != "") == [1]
    if ("QID1", None) in run.state.displayed:
        positions = sorted(row[f"Q1_DO_{i}"] for i in ("1", "2"))
        assert positions == [1, 2]


def test_web_service_hook():
    flow = [
        {
            "Type": "WebService",
            "FlowID": "FL_2",
            "URL": "https://x",
            "Method": "GET",
            "ResponseMap": [{"key": "r", "value": "seed"}],
        },
        {"Type": "Block", "ID": "BL_1", "FlowID": "FL_3"},
    ]
    survey = load_qsf(minimal_qsf([mc()], flow=flow))
    plain = Simulator(survey).respondent()
    assert plain.embedded["seed"] == "" and any("web service" in n for n in plain.notes)
    hooked = Simulator(survey, web_service=lambda node, state: {"seed": "42"}).respondent()
    assert hooked.embedded["seed"] == "42"


# ------------------------------------------------------------------ design


def test_design_factors_crossing_and_opaque():
    flow = [
        {
            "Type": "BlockRandomizer",
            "FlowID": "FL_2",
            "SubSet": 1,
            "EvenPresentation": True,
            "Flow": [
                {
                    "Type": "EmbeddedData",
                    "FlowID": "FL_3",
                    "EmbeddedData": [{"Field": "frame", "Type": "Custom", "Value": "gain"}],
                },
                {
                    "Type": "EmbeddedData",
                    "FlowID": "FL_4",
                    "EmbeddedData": [{"Field": "frame", "Type": "Custom", "Value": "loss"}],
                },
            ],
        },
        {
            "Type": "BlockRandomizer",
            "FlowID": "FL_5",
            "SubSet": 1,
            "Flow": [
                {
                    "Type": "EmbeddedData",
                    "FlowID": "FL_6",
                    "EmbeddedData": [{"Field": "source", "Type": "Custom", "Value": "expert"}],
                },
                {
                    "Type": "EmbeddedData",
                    "FlowID": "FL_7",
                    "EmbeddedData": [{"Field": "source", "Type": "Custom", "Value": "peer"}],
                },
                {
                    "Type": "EmbeddedData",
                    "FlowID": "FL_8",
                    "EmbeddedData": [{"Field": "source", "Type": "Custom", "Value": "none"}],
                },
            ],
        },
        {
            "Type": "EmbeddedData",
            "FlowID": "FL_9",
            "EmbeddedData": [{"Field": "draw", "Type": "Custom", "Value": "${rand://int/1:10}"}],
        },
        {"Type": "WebService", "FlowID": "FL_10", "URL": "https://rng", "Method": "GET"},
        {"Type": "Block", "ID": "BL_1", "FlowID": "FL_11"},
    ]
    survey = load_qsf(minimal_qsf([mc(QuestionJS="var x = Math.random();")], flow=flow))
    d = design(survey)
    assert [f.id for f in d.between_subjects] == ["FL_2", "FL_5"]
    assert d.factors[0].treatment_fields == ["frame"]
    assert [a.probability for a in d.factors[1].arms] == pytest.approx([1 / 3] * 3)
    cells, complete = d.cells()
    assert len(cells) == 6 and complete
    assert [g and [f.id for f in g] for g in d.crossed()] == [["FL_2", "FL_5"]]
    assert d.random_values[0].field == "draw"
    assert {o.kind for o in d.opaque} == {"web_service", "javascript"}
    assert "NOT REPRODUCIBLE" in d.summary()


def test_design_detects_last_arm_wins_assignment():
    flow = [
        {
            "Type": "BlockRandomizer",
            "FlowID": "FL_2",
            "SubSet": 2,
            "Flow": [
                {
                    "Type": "EmbeddedData",
                    "FlowID": "FL_3",
                    "EmbeddedData": [{"Field": "Group", "Type": "Custom", "Value": "A"}],
                },
                {
                    "Type": "EmbeddedData",
                    "FlowID": "FL_4",
                    "EmbeddedData": [{"Field": "Group", "Type": "Custom", "Value": "B"}],
                },
            ],
        }
    ]
    survey = load_qsf(minimal_qsf([mc()], flow=flow))
    (factor,) = design(survey).factors
    assert factor.last_shown_assigns == ["Group"] and factor.between_subjects
    runs = Simulator(survey, seed=1).run(200)
    assert {r.embedded["Group"] for r in runs} == {"A", "B"}
    assert all(
        r.embedded["Group"] == {"FL_3": "A", "FL_4": "B"}[r.flow_order["FL_2"][-1]] for r in runs
    )


def test_duplicate_export_tags_keep_both_columns():
    q1, q2 = mc("QID1"), mc("QID2")
    q2["DataExportTag"] = "Q1"  # same tag as QID1
    survey = load_qsf(minimal_qsf([q1, q2]))
    run = Simulator(survey, seed=0).respondent(lambda v, s: "1" if v.question.id == "QID1" else "2")
    cells = [(c.name, c.question_id, v) for c, v in run.cells(survey) if c.part == "response"]
    assert cells == [("Q1", "QID1", 1), ("Q1", "QID2", 2)]
