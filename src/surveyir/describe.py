"""Plain-English descriptions of IR conditions and flow, for docs and LLM prompts."""

from __future__ import annotations

from .model import And, Comparison, Condition, Survey

_OPERATOR_WORDS = {
    "selected": "is selected",
    "not_selected": "is not selected",
    "displayed": "was displayed",
    "not_displayed": "was not displayed",
    "equal": "=",
    "not_equal": "≠",
    "greater_than": ">",
    "greater_than_or_equal": "≥",
    "less_than": "<",
    "less_than_or_equal": "≤",
    "empty": "is empty",
    "not_empty": "is not empty",
    "contains": "contains",
    "does_not_contain": "does not contain",
    "matches_regex": "matches regex",
    "is": "is",
    "is_not": "is not",
}


def _short(text: str, limit: int = 60) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def describe_operand(c: Comparison, survey: Survey | None) -> str:
    left = c.left
    if left.kind == "question" and left.question_id:
        q = survey.questions.get(left.question_id) if survey else None
        name = q.export_tag if q else left.question_id
        prefix = f"loop {left.loop_iteration} " if left.loop_iteration else ""
        if left.selector in ("SelectableChoice", "SelectableAnswer") and left.choice_id:
            label = None
            if q is not None:
                for attr in ("choices", "items", "rows"):
                    for ch in getattr(q, attr, None) or []:
                        if ch.id == left.choice_id:
                            label = ch.text.plain
            choice = f"“{_short(label, 40)}”" if label else f"choice {left.choice_id}"
            return f"{prefix}{name}: {choice}"
        if left.selector == "ChoiceTextEntryValue":
            return f"{prefix}{name} text" + (
                f" (choice {left.choice_id})" if left.choice_id else ""
            )
        if left.selector == "ChoiceNumericEntryValue":
            return f"{prefix}{name} value" + (
                f" (choice {left.choice_id})" if left.choice_id else ""
            )
        return f"{prefix}{name}"
    if left.kind == "embedded_data":
        return f"embedded data `{left.name}`"
    if left.kind == "device":
        return "device type"
    return f"{left.kind} `{left.name or left.raw}`"


def describe_condition(condition: Condition | None, survey: Survey | None = None) -> str:
    """Render a condition as a short English sentence fragment."""
    if condition is None:
        return "(no condition)"
    if isinstance(condition, Comparison):
        op = _OPERATOR_WORDS.get(condition.operator, condition.source_operator)
        right = f" {condition.right}" if condition.right not in (None, "") else ""
        return f"{describe_operand(condition, survey)} {op}{right}"
    joiner = " AND " if isinstance(condition, And) else " OR "
    parts = []
    for term in condition.terms:
        text = describe_condition(term, survey)
        parts.append(f"({text})" if not isinstance(term, Comparison) else text)
    return joiner.join(parts)
