"""Unevaluable conditions are UNKNOWN: false at the comparison, and recorded."""

from __future__ import annotations

from typing import get_args

import pytest

from surveyir.model import Comparison, Operand, Or
from surveyir.model.logic import Operator
from surveyir.runtime import Answer, RespondentState, evaluate
from surveyir.runtime.execution import APPROXIMATIONS
from surveyir.runtime.logic import UNKNOWN, unevaluable

UNKNOWN_OPERANDS = {
    "logic.geo_ip": Operand(kind="geo_ip", name="loc://CountryName", raw="loc://CountryName"),
    "logic.scoring": Operand(kind="scoring", name="SC_1", raw="SC_1"),
    "logic.quota": Operand(kind="quota", name="QO_1", raw="QO_1"),
    "logic.other_operand": Operand(kind="other", name="Panel", raw="p://x"),
}
OPERATORS = [op for op in get_args(Operator) if op not in ("quota_met", "quota_not_met", "other")]


def comparison(left: Operand, op: str, right: str | None = "1") -> Comparison:
    return Comparison(left=left, operator=op, source_operator=op, right=right)  # type: ignore[arg-type]


@pytest.mark.parametrize("op", OPERATORS)
@pytest.mark.parametrize("code", sorted(UNKNOWN_OPERANDS))
def test_unknown_operand_is_false_and_recorded(code, op):
    state = RespondentState()
    left = UNKNOWN_OPERANDS[code]
    assert evaluate(comparison(left, op), state) is False
    [approx] = state.approximations
    assert (approx.code, approx.location, approx.affects) == (code, left.raw, "routing")
    assert code in APPROXIMATIONS and state.notes


def test_unknown_operator_is_false_and_recorded():
    state = RespondentState()
    left = Operand(kind="question", question_id="QID1", raw="q://QID1/SelectableChoice/1")
    assert evaluate(comparison(left, "other"), state) is False
    assert [(a.code, a.location) for a in state.approximations] == [
        ("logic.other_operator", "QID1")
    ]


def test_question_without_id_is_unknown():
    state = RespondentState()
    left = Operand(kind="question", raw="q://undefined/SelectableChoice/1")
    assert evaluate(comparison(left, "not_selected"), state) is False
    assert state.approximations[0].code == "logic.other_operand"


def test_unanswered_is_not_unknown():
    state = RespondentState()
    q1 = Operand(kind="question", question_id="QID1", choice_id="1", raw="q://QID1/SelectableChoice/1")
    assert evaluate(comparison(q1, "not_selected"), state) is True
    assert evaluate(comparison(q1, "not_displayed"), state) is True
    assert evaluate(comparison(q1, "empty"), state) is True
    assert evaluate(comparison(q1, "greater_than", "3"), state) is False  # unanswered: just false
    assert state.approximations == []


def test_unknown_inside_or_does_not_poison_other_terms():
    state = RespondentState(embedded={"x": "1"})
    known = Comparison(
        left=Operand(kind="embedded_data", name="x", raw="x"), operator="equal",
        source_operator="EqualTo", right="1",
    )
    cond = Or(terms=[comparison(UNKNOWN_OPERANDS["logic.geo_ip"], "not_equal"), known])
    assert evaluate(cond, state) is True
    assert [a.code for a in state.approximations] == ["logic.geo_ip"]


def test_geo_ip_uses_a_supplied_location():
    left = UNKNOWN_OPERANDS["logic.geo_ip"]
    state = RespondentState(location={"CountryName": "Australia"})
    assert evaluate(comparison(left, "equal", "Australia"), state) is True
    assert evaluate(comparison(left, "not_equal", "Australia"), state) is False
    assert state.approximations == []
    assert unevaluable(comparison(left, "equal"), {"CountryName": "x"}) is None


def test_invalid_regex_and_non_numeric_are_recorded():
    state = RespondentState(embedded={"age": "old", "name": "Ada"})
    name = Operand(kind="embedded_data", name="name", raw="name")
    age = Operand(kind="embedded_data", name="age", raw="age")
    assert evaluate(comparison(name, "matches_regex", "("), state) is False
    assert evaluate(comparison(age, "greater_than", "18"), state) is False
    assert evaluate(comparison(name, "less_than", "abc"), state) is False
    assert [(a.code, a.location) for a in state.approximations] == [
        ("logic.regex_invalid", "name"),
        ("logic.non_numeric", "age"),
        ("logic.non_numeric", "name"),
    ]


def test_hook_sees_every_approximation():
    seen = []
    state = RespondentState(on_approximation=seen.append)
    state.answers[("QID1", None)] = Answer(value="x")
    evaluate(comparison(UNKNOWN_OPERANDS["logic.scoring"], "equal"), state)
    assert [a.code for a in seen] == ["logic.scoring"]


def test_unknown_sentinel_is_distinct_from_unanswered():
    assert UNKNOWN is not None and not UNKNOWN and repr(UNKNOWN) == "UNKNOWN"
