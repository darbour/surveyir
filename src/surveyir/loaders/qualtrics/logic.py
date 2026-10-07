"""Parse Qualtrics logic (display, branch, skip, validation) into IR conditions.

Qualtrics stores logic as nested numeric-keyed dicts::

    {"0": {"0": expr, "1": expr, "Type": "If"},       # logic set 1
     "1": {"0": expr, "Type": "OrIf"},                # logic set 2
     "Type": "BooleanExpression", "inPage": false}

Expressions after the first in a set carry ``Conjuction`` (sic) "And"/"Or";
sets after the first carry Type "AndIf"/"OrIf". Qualtrics evaluates AND before
OR, at both levels, and we encode that precedence in the resulting tree.
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import unquote

from ...model import And, Comparison, Condition, Operand, Or
from ...model.logic import Operator
from ...text import html_to_text
from ._util import Diagnostics, ordered_keys, truthy

OPERATORS: dict[str, Operator] = {
    "Selected": "selected",
    "NotSelected": "not_selected",
    "Displayed": "displayed",
    "NotDisplayed": "not_displayed",
    "EqualTo": "equal",
    "NotEqualTo": "not_equal",
    "GreaterThan": "greater_than",
    "GreaterThanOrEqual": "greater_than_or_equal",
    "LessThan": "less_than",
    "LessThanOrEqual": "less_than_or_equal",
    "Empty": "empty",
    "NotEmpty": "not_empty",
    "Contains": "contains",
    "DoesNotContain": "does_not_contain",
    "MatchesRegex": "matches_regex",
    "Is": "is",
    "IsNot": "is_not",
    "QuotaMet": "quota_met",
    "QuotaNotMet": "quota_not_met",
}

_EXPRESSION_KEYS = {
    "Type",
    "Conjuction",
    "Conjunction",
    "LogicType",
    "LeftOperand",
    "Operator",
    "RightOperand",
    "QuestionID",
    "QuestionIDFromLocator",
    "ChoiceLocator",
    "QuestionIsInLoop",
    "Description",
    "IgnoreCase",
    "_HiddenExpression",
}

_LOCATOR = re.compile(r"^q://(?:(?P<loop>\d+)_)?(?P<qid>[^/]+)(?:/(?P<rest>.*))?$")


def parse_question_locator(raw: str) -> Operand:
    """Parse a question locator into an ``Operand``.

    Shapes seen in real exports (``QID`` may carry a loop prefix ``3_QID5`` or
    a URL-encoded side-by-side suffix ``QID5%232`` = ``QID5#2``)::

        q://QID/SelectableChoice/<choice>            choice selected
        q://QID/SelectableChoice/<row>/<column>      matrix cell selected
        q://QID/SelectableChoice/Group/<choice>/<g>  pick-group-rank placement
        q://QID/SelectableAnswer/<answer>            scale point selected
        q://QID/ChoiceTextEntryValue[/<choice>]      text entered
        q://QID/ChoiceNumericEntryValue/<choice>     number entered
        q://QID/ChoiceDisplayed[/<choice>]           choice shown
        q://QID/QuestionDisplayed                    question shown
        q://QID/SelectedChoicesCount                 number of choices selected
        q://QID/SelectedAnswerCount/<row>            number of answers selected in a row
    """
    m = _LOCATOR.match(raw or "")
    if not m:
        return Operand(kind="question", raw=raw)
    parts = [unquote(p) for p in (m.group("rest") or "").split("/") if p != ""]
    selector = parts[0] if parts else None
    args = parts[1:]
    choice_id = answer_id = None
    if selector == "SelectableAnswer":
        answer_id = args[-1] if args else None
        choice_id = args[0] if len(args) > 1 else None
    elif selector == "SelectableChoice" and args[:1] == ["Group"]:
        choice_id = args[1] if len(args) > 1 else None
        answer_id = args[2] if len(args) > 2 else None
    else:
        choice_id = args[0] if args else None
        answer_id = args[1] if len(args) > 1 else None
    qid = unquote(m.group("qid"))
    return Operand(
        kind="question",
        question_id=None if qid == "undefined" else qid,
        loop_iteration=int(m.group("loop")) if m.group("loop") else None,
        selector=None if selector == "undefined" else selector,
        choice_id=choice_id,
        answer_id=answer_id,
        args=args,
        raw=raw,
    )


def _operand(expr: dict[str, Any]) -> Operand:
    logic_type = expr.get("LogicType")
    left = expr.get("LeftOperand")
    left_str = "" if left is None else str(left)
    if logic_type == "Question" or left_str.startswith("q://"):
        op = parse_question_locator(left_str)
        if op.question_id is None and expr.get("QuestionID"):
            op = op.model_copy(update={"question_id": expr["QuestionID"]})
        return op
    if logic_type == "EmbeddedField":
        return Operand(kind="embedded_data", name=left_str, raw=left_str)
    if logic_type == "DeviceType":
        # The device value ("mobile") is stored as the left operand; see parse_expression.
        return Operand(kind="device", raw=left_str)
    if logic_type == "Quota":
        return Operand(kind="quota", name=left_str, raw=left_str)
    if logic_type in {"LoopAndMerge", "LoopAndMergeField"}:
        return Operand(kind="loop_field", name=left_str, raw=left_str)
    if logic_type == "Scoring":
        return Operand(kind="scoring", name=left_str, raw=left_str)
    if logic_type == "GeoIP":
        return Operand(kind="geo_ip", name=left_str or None, raw=left_str)
    if logic_type == "BooleanValue":
        return Operand(kind="constant", name=left_str or None, raw=left_str)
    return Operand(kind="other", name=logic_type, raw=left_str)


def parse_expression(expr: dict[str, Any], diags: Diagnostics, where: str) -> Comparison:
    source_op = str(expr.get("Operator") or "")
    operator: Operator | None = OPERATORS.get(source_op)
    if not source_op or "undefined" in str(expr.get("LeftOperand", "")):
        # Qualtrics shows these as "Invalid Logic" in the editor.
        diags.warn("invalid-logic", "Logic expression is incomplete in the source", where)
        operator = "other"
    elif operator is None:
        diags.warn("unknown-operator", f"Unknown logic operator {source_op!r}", where)
        operator = "other"
    right = expr.get("RightOperand")
    if expr.get("LogicType") == "DeviceType" and right is None:
        right = expr.get("LeftOperand")
    description = expr.get("Description")
    return Comparison(
        left=_operand(expr),
        operator=operator,
        source_operator=source_op,
        right=None if right is None else str(right),
        ignore_case=truthy(expr.get("IgnoreCase", False)),
        description=html_to_text(description) if isinstance(description, str) else None,
        extras={k: v for k, v in expr.items() if k not in _EXPRESSION_KEYS},
    )


def _combine(items: list[tuple[str, Condition]]) -> Condition:
    """Combine (conjunction, term) pairs with AND binding tighter than OR."""
    groups: list[list[Condition]] = [[]]
    for i, (conj, term) in enumerate(items):
        if i > 0 and conj == "or":
            groups.append([])
        groups[-1].append(term)
    ands: list[Condition] = [g[0] if len(g) == 1 else And(terms=g) for g in groups]
    return ands[0] if len(ands) == 1 else Or(terms=ands)


def parse_logic(logic: Any, diags: Diagnostics, where: str) -> Condition | None:
    """Parse a Qualtrics BooleanExpression. Returns None for empty logic."""
    if not isinstance(logic, dict) or not logic:
        return None
    sets: list[tuple[str, Condition]] = []
    for set_key in ordered_keys(logic):
        logic_set = logic[set_key]
        if not isinstance(logic_set, dict):
            continue
        terms: list[tuple[str, Condition]] = []
        for expr_key in ordered_keys(logic_set):
            expr = logic_set[expr_key]
            if not isinstance(expr, dict):
                continue
            if truthy(expr.get("_HiddenExpression", False)):
                diags.info("hidden-expression", "Hidden (disabled) logic expression ignored", where)
                continue
            conj = str(expr.get("Conjuction") or expr.get("Conjunction") or "And").lower()
            terms.append((conj, parse_expression(expr, diags, where)))
        if not terms:
            continue
        set_type = str(logic_set.get("Type", "If"))
        if set_type in {"OrIf", "ElseIf"}:
            set_conj = "or"
        elif set_type in {"If", "AndIf"}:
            set_conj = "and"
        else:
            diags.warn("unknown-logic-set", f"Unknown logic set type {set_type!r}", where)
            set_conj = "and"
        sets.append((set_conj, _combine(terms)))
    if not sets:
        return None
    if logic.get("inPage"):
        diags.info("in-page-logic", "In-page display logic (evaluated on the same page)", where)
    return _combine(sets)


SKIP_CONDITIONS: dict[str, Operator] = {
    "Selected": "selected",
    "NotSelected": "not_selected",
    "Displayed": "displayed",
    "NotDisplayed": "not_displayed",
}


def parse_skip_condition(item: dict[str, Any], diags: Diagnostics, where: str) -> Condition:
    """Skip logic stores one condition as {Locator, Condition, QuestionID, ...}."""
    locator = item.get("ChoiceLocator") or item.get("Locator") or ""
    source_op = str(item.get("Condition", ""))
    operator: Operator | None = SKIP_CONDITIONS.get(source_op) or OPERATORS.get(source_op)
    if operator is None:
        diags.warn("unknown-operator", f"Unknown skip condition {source_op!r}", where)
        operator = "other"
    left = parse_question_locator(locator)
    if left.question_id is None and item.get("QuestionID"):
        left = left.model_copy(update={"question_id": item["QuestionID"]})
    description = item.get("Description")
    value = item.get("Value")
    return Comparison(
        left=left,
        operator=operator,
        source_operator=source_op,
        right=None if value is None else str(value),
        description=html_to_text(description) if isinstance(description, str) else None,
    )
