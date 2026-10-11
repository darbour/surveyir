"""Administering a LimeSurvey survey: relevance, piping, filters, validation, order, quotas."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from surveyir import RandomAnswerer, Simulator
from surveyir.loaders.limesurvey import load_lss
from surveyir.runtime.execution import ExecutionError
from surveyir.runtime.trace import Display, EmbeddedSet, End, Hidden

FIX = Path(__file__).parent / "fixtures" / "limesurvey"
FIRSTNAME = {"embedded": {"TOKEN:FIRSTNAME": "Ann"}}


@pytest.fixture(scope="module")
def logic():
    return load_lss(FIX / "logic.lss", strict=True)


class Scripted:
    """Answers by question id; records what it was shown."""

    def __init__(self, answers: dict[str, Any]) -> None:
        self.answers = answers
        self.seen: list[Display] = []

    def answer(self, ctx: Any) -> Any:
        self.seen.append(ctx.view)
        return self.answers.get(ctx.view.qid)


BASE = {
    "age": "35",
    "consent": "Y",
    "pets": ["SQ001", "SQ003"],
    "dogname": "Rex",
    "petrate": {"SQ001": "A2", "SQ003": "A1"},
    "likes": "A1",
    "why": "fun",
    "budget": {"SQ001": "6", "SQ002": "4"},
    "o1": "a",
    "o2": "b",
    "o3": "c",
    "fixed": "f",
}


def walk(survey, answers: dict[str, Any], seed: int = 1, **kw: Any):
    sim = Simulator(survey, seed=seed, implementations=FIRSTNAME, **kw)
    who = Scripted(answers)
    return sim.respondent(who), who


def shown(run) -> list[str]:
    return [ob.qid for ob in run.trace if isinstance(ob, Display)]


def test_full_walk(logic):
    run, who = walk(logic, BASE)
    assert run.finished and not who.answers.keys() - set(shown(run)) - {"o1", "o2", "o3"}
    assert run.answers[("npets", None)].value == "2"  # hidden equation, computed
    assert not run.state.approximations


def test_relevance_hides_and_marks_irrelevant(logic):
    run, _ = walk(logic, {**BASE, "pets": ["SQ002"]})
    assert "dogname" not in shown(run)
    assert ("dogname", None) in run.state.irrelevant
    hidden = [ob for ob in run.audit if isinstance(ob, Hidden) and ob.qid == "dogname"]
    assert hidden and hidden[0].reason == "display_logic"


def test_group_relevance_skips_the_group(logic):
    run, _ = walk(logic, {**BASE, "consent": "N"})
    assert not {"dogname", "petrate", "likes", "why"} & set(shown(run))
    assert ("likes", None) in run.state.irrelevant


def test_array_filter_and_subquestion_relevance(logic):
    run, who = walk(logic, BASE)
    view = next(v for v in who.seen if v.qid == "petrate")
    assert [c.id for c in view.choices] == ["SQ001", "SQ003"]  # Cat not picked
    run, who = walk(logic, {**BASE, "age": "20"})
    view = next(v for v in who.seen if v.qid == "petrate")
    assert [c.id for c in view.choices] == ["SQ001"]  # Fish needs age >= 30


def test_piping_and_participant_attributes(logic):
    run, who = walk(logic, BASE)
    hello = next(ob for ob in run.trace if isinstance(ob, Display) and ob.qid == "hello")
    assert hello.text == "Hello Ann, you have 2 pet(s)."
    why = next(v for v in who.seen if v.qid == "why")
    assert why.text == "Why, Rex?"


def test_unsupplied_participant_attribute_stops_a_strict_run(logic):
    with pytest.raises(ExecutionError, match="TOKEN:FIRSTNAME"):
        Simulator(logic, seed=1).respondent(Scripted(BASE))


def test_in_page_relevance_reveals_on_answer(logic):
    run, _ = walk(logic, BASE)
    why = next(ob for ob in run.trace if isinstance(ob, Display) and ob.qid == "why")
    assert why.revealed_in_page
    run, _ = walk(logic, {**BASE, "likes": "A2"})
    assert "why" not in shown(run) and ("why", None) in run.state.irrelevant


def test_validation_expression(logic):
    run, _ = walk(logic, {**BASE, "budget": {"SQ001": "6", "SQ002": "6"}}, strict=False)
    assert [a.code for a in run.state.approximations] == ["answer.invalid"]
    with pytest.raises(ExecutionError, match="answer.invalid"):
        walk(logic, {**BASE, "budget": {"SQ001": "6", "SQ002": "6"}})
    view = next(v for v in walk(logic, BASE)[1].seen if v.qid == "budget")
    assert view.validation["message"] == "The parts must add up to 10."


def test_randomization_groups(logic):
    orders = set()
    fixed = set()
    for seed in range(12):
        run, _ = walk(logic, BASE, seed=seed)
        order = shown(run)
        orders.add(tuple(q for q in order if q in ("armA", "armB")))
        tail = [q for q in order if q in ("o1", "fixed", "o2", "o3")]
        fixed.add(tail.index("fixed"))
    assert orders == {("armA", "armB"), ("armB", "armA")}
    assert fixed == {1}  # the question outside the random group keeps its place


def test_quota_ends_the_survey_once_full(logic):
    sim = Simulator(logic, seed=1, implementations=FIRSTNAME)
    first = sim.respondent(Scripted({**BASE, "likes": "A2"}))
    second = sim.respondent(Scripted({**BASE, "likes": "A2"}))
    third = sim.respondent(Scripted(BASE))
    assert first.finished and not second.finished and third.finished
    assert second.ended_by == "quota quota_1"
    end = [ob for ob in second.trace if isinstance(ob, End)]
    assert end and end[0].reason == "quota"


def test_equation_audited(logic):
    run, _ = walk(logic, BASE)
    sets = [e for e in run.audit if isinstance(e, EmbeddedSet) and e.name == "npets"]
    assert sets and sets[0].value == "2" and sets[0].source == "equation:npets"


@pytest.mark.parametrize("name", ["types_v3", "types_v6", "types_v7"])
def test_random_respondents_walk_every_type(name):
    s = load_lss(FIX / f"{name}.lss", strict=True)
    runs = Simulator(s, seed=3, strict=False).run(10, RandomAnswerer(seed=4))
    assert all(r.finished for r in runs)
    kinds = {ob.kind for r in runs for ob in r.trace if isinstance(ob, Display)}
    assert {"choice", "matrix", "side_by_side", "text_entry", "rank_order"} <= kinds


def test_hidden_equation_uses_other_answers():
    s = load_lss(FIX / "types_v6.lss", strict=True)
    answers = {"colour": "A1", "age": "40"}
    run = Simulator(s, seed=1, strict=False).respondent(Scripted(answers))
    assert run.answers[("score", None)].value == "41"
    info = next(ob for ob in run.trace if isinstance(ob, Display) and ob.qid == "info")
    assert info.text == "Thank you, Red it is."


def test_welcome_page_and_group_info_are_shown(logic):
    run, _ = walk(logic, BASE)
    order = shown(run)
    assert order[:3] == ["welcome", "G10_info", "age"]
    pages = {ob.qid: ob.page for ob in run.trace if isinstance(ob, Display)}
    assert pages["welcome"] < pages["G10_info"] == pages["age"]  # info shares the page
