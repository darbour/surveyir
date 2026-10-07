"""Evaluate IR conditions against a respondent's state.

Semantics follow Qualtrics where they are documented and are checked against
real response data (branch replay in tests/test_runtime_validation.py):

* Comparisons are numeric when both sides parse as numbers, else textual
  (case-sensitive unless ``ignore_case``).
* "Selected" on a matrix cell (``choice_id`` row, ``answer_id`` column) means
  that column was picked in that row.
* Anything the state cannot answer (unknown operator, GeoIP, missing question)
  evaluates False and leaves a note on the state; evaluation never raises.
"""

from __future__ import annotations

import re
from typing import Any

from ..model import And, Comparison, Condition, Or, Survey
from .state import RespondentState


def _num(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(str(value).strip())
    except ValueError:
        return None


def _as_list(value: Any) -> list:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return list(value)
    return [value]


def _operand_value(c: Comparison, state: RespondentState, survey: Survey | None) -> Any:
    left = c.left
    if left.kind == "embedded_data":
        return state.embedded.get(left.name or "", "")
    if left.kind == "loop_field":
        if state.loop is None:
            return ""
        name = (left.name or "").split("/")[-1]
        return state.loop.fields.get(name, "")
    if left.kind == "device":
        return state.device
    if left.kind != "question" or not left.question_id:
        state.note(f"cannot evaluate {left.kind} operand {left.raw!r}; treated as false")
        return None
    answer = state.answer(left.question_id, left.loop_iteration)
    value = answer.value if answer else None
    sel = left.selector or ""
    if sel == "ChoiceTextEntryValue":
        if left.choice_id is None:
            return value if isinstance(value, str) else (answer.text.get("") if answer else None)
        if answer and left.choice_id in answer.text:
            return answer.text[left.choice_id]
        if isinstance(value, dict):
            return value.get(left.choice_id)
        return None
    if sel == "ChoiceNumericEntryValue":
        return value.get(left.choice_id) if isinstance(value, dict) else value
    if sel == "SelectedChoicesCount":
        return len(_as_list(value)) if not isinstance(value, dict) else len(value)
    if sel == "SelectedAnswerCount":
        if isinstance(value, dict) and left.choice_id is not None:
            return len(_as_list(value.get(left.choice_id)))
        return 0
    return value


def _selected(c: Comparison, state: RespondentState) -> bool:
    left = c.left
    answer = state.answer(left.question_id or "", left.loop_iteration)
    if answer is None or answer.value is None:
        return False
    value = answer.value
    if left.selector == "SelectableAnswer":  # a scale point, in a given row or any row
        if isinstance(value, dict):
            rows = [value.get(left.choice_id)] if left.choice_id else list(value.values())
            return any(left.answer_id in _as_list(r) for r in rows)
        return left.answer_id in _as_list(value)
    if isinstance(value, dict):  # matrix-like: row -> column(s)
        cell = value.get(left.choice_id)
        if left.answer_id is None:
            return bool(_as_list(cell))
        return left.answer_id in [str(x) for x in _as_list(cell)]
    return left.choice_id in [str(x) for x in _as_list(value)]


def _displayed(c: Comparison, state: RespondentState) -> bool:
    left = c.left
    qid = left.question_id or ""
    if left.selector == "ChoiceDisplayed" and left.choice_id:
        shown = state.displayed_choices.get(state.key(qid, left.loop_iteration), [])
        return left.choice_id in shown
    return state.was_displayed(qid, left.loop_iteration)


def _compare(op: str, value: Any, right: str | None, ignore_case: bool) -> bool:
    if op in ("empty", "not_empty"):
        empty = value is None or value == "" or value == [] or value == {}
        return empty if op == "empty" else not empty
    a, b = _num(value), _num(right)
    if op in ("greater_than", "greater_than_or_equal", "less_than", "less_than_or_equal"):
        if a is None or b is None:
            return False
        return {
            "greater_than": a > b,
            "greater_than_or_equal": a >= b,
            "less_than": a < b,
            "less_than_or_equal": a <= b,
        }[op]
    text = "" if value is None else str(value)
    target = "" if right is None else str(right)
    if ignore_case:
        text, target = text.lower(), target.lower()
    if op in ("equal", "not_equal", "is", "is_not"):
        same = (a == b) if (a is not None and b is not None) else (text == target)
        return same if op in ("equal", "is") else not same
    if op == "contains":
        return target in text
    if op == "does_not_contain":
        return target not in text
    if op == "matches_regex":
        try:
            return re.search(target, text) is not None
        except re.error:
            return False
    return False


def evaluate(
    condition: Condition | None, state: RespondentState, survey: Survey | None = None
) -> bool:
    """True if ``condition`` holds for ``state``; ``None`` (no condition) is True."""
    if condition is None:
        return True
    if isinstance(condition, And):
        return all(evaluate(t, state, survey) for t in condition.terms)
    if isinstance(condition, Or):
        return any(evaluate(t, state, survey) for t in condition.terms)
    c = condition
    op = c.operator
    if op in ("selected", "not_selected"):
        hit = _selected(c, state)
        return hit if op == "selected" else not hit
    if op in ("displayed", "not_displayed"):
        hit = _displayed(c, state)
        return hit if op == "displayed" else not hit
    if op in ("quota_met", "quota_not_met"):
        met = (c.left.name or c.left.raw) in state.quotas_met
        return met if op == "quota_met" else not met
    if op == "other":
        state.note(f"cannot evaluate operator {c.source_operator!r}; treated as false")
        return False
    if c.left.kind == "constant":
        return _compare(op, c.left.name or c.left.raw, c.right, c.ignore_case)
    return _compare(op, _operand_value(c, state, survey), c.right, c.ignore_case)
