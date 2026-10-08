"""Evaluate IR conditions against a respondent's state.

Semantics follow Qualtrics where they are documented and are checked against
real response data (branch replay in tests/test_runtime_validation.py):

* Comparisons are numeric when both sides parse as numbers, else textual
  (case-sensitive unless ``ignore_case``).
* "Selected" on a matrix cell (``choice_id`` row, ``answer_id`` column) means
  that column was picked in that row.
* An unanswered or missing question is unanswered, not unknown: ``not_selected``,
  ``not_displayed``, ``empty`` and ``not_equal`` hold for it, as in Qualtrics, and
  ordering comparisons on it are false.
* A comparison the runtime cannot evaluate (GeoIP without a supplied location,
  scoring, a quota tested with anything but met/not met, an unknown operand or
  operator) is ``UNKNOWN``: the comparison itself is false, whatever its
  operator, and ``state.approximate`` records it (``logic.*`` codes, see
  ``runtime.execution.APPROXIMATIONS``). So are invalid regular expressions and
  ordering comparisons against a value that is present but not a number. In a
  strict run the state's ``on_approximation`` hook raises instead.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any, Final

from ..model import And, Comparison, Condition, Operand, Or, Survey
from .state import RespondentState


class _Unknown:
    """A value the runtime cannot know. Distinct from ``None`` (unanswered)."""

    def __repr__(self) -> str:
        return "UNKNOWN"

    def __bool__(self) -> bool:
        return False


UNKNOWN: Final = _Unknown()

_Record = Callable[[str, str], None]  # (code, detail) -> None; location is bound


def operand_location(left: Operand) -> str:
    """Where a ``logic.*`` approximation is reported: the question id, else the raw locator."""
    return left.question_id or left.raw or left.name or left.kind


def unevaluable(c: Comparison, location: dict[str, str] | None = None) -> tuple[str, str] | None:
    """``(code, detail)`` if the runtime cannot evaluate ``c``, else None.

    Static: it depends only on the comparison (and, for GeoIP, on whether a
    respondent location is supplied), never on answers. ``executability`` uses it
    to predict exactly the approximations ``evaluate`` will record.
    """
    left, op = c.left, c.operator
    if op == "other":
        return "logic.other_operator", (
            f"cannot evaluate operator {c.source_operator!r} on {left.raw!r}; treated as false"
        )
    if op in ("quota_met", "quota_not_met"):
        return None
    kind = left.kind
    if kind in ("embedded_data", "loop_field", "device", "constant"):
        return None
    if kind == "question" and left.question_id:
        return None
    if kind == "geo_ip" and location is not None and _geo_name(left) in location:
        return None
    code = {"geo_ip": "logic.geo_ip", "scoring": "logic.scoring", "quota": "logic.quota"}.get(
        kind, "logic.other_operand"
    )
    return code, f"cannot evaluate {kind} operand {left.raw!r}; treated as false"


def _geo_name(left: Operand) -> str:
    return (left.name or left.raw or "").removeprefix("loc://")


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
    """The operand's value; ``None`` if unanswered, ``UNKNOWN`` if it cannot be known."""
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
    if left.kind == "geo_ip" and _geo_name(left) in state.location:
        return state.location[_geo_name(left)]
    if left.kind != "question" or not left.question_id:
        return UNKNOWN
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


def _present(value: Any) -> bool:
    return not (value is None or value == "" or value == [] or value == {})


def _compare(op: str, value: Any, right: str | None, ignore_case: bool, record: _Record) -> bool:
    if op in ("empty", "not_empty"):
        return _present(value) is (op == "not_empty")
    a, b = _num(value), _num(right)
    if op in ("greater_than", "greater_than_or_equal", "less_than", "less_than_or_equal"):
        if a is None or b is None:
            if _present(value) or b is None:  # unanswered is simply false, not unknown
                record(
                    "logic.non_numeric",
                    f"{op} compares {value!r} with {right!r}, not both numbers; treated as false",
                )
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
        except re.error as e:
            record("logic.regex_invalid", f"invalid regular expression {target!r} ({e}); false")
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

    def record(code: str, detail: str) -> None:
        state.approximate(code, operand_location(c.left), "routing", detail)

    reason = unevaluable(c, state.location)
    if reason is not None:  # UNKNOWN: the comparison is false whatever its operator
        record(*reason)
        return False
    if op in ("selected", "not_selected"):
        hit = _selected(c, state)
        return hit if op == "selected" else not hit
    if op in ("displayed", "not_displayed"):
        hit = _displayed(c, state)
        return hit if op == "displayed" else not hit
    if op in ("quota_met", "quota_not_met"):
        met = (c.left.name or c.left.raw) in state.quotas_met
        return met if op == "quota_met" else not met
    if c.left.kind == "constant":
        return _compare(op, c.left.name or c.left.raw, c.right, c.ignore_case, record)
    value = _operand_value(c, state, survey)
    if value is UNKNOWN:  # defensive: unevaluable() should have caught it
        record("logic.other_operand", f"cannot evaluate operand {c.left.raw!r}; treated as false")
        return False
    return _compare(op, value, c.right, c.ignore_case, record)
