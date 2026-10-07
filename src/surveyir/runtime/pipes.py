"""Resolve piped text (``${...}``) against a respondent's state."""

from __future__ import annotations

import random
import re
from datetime import date

from ..model import Pipe, Survey, Text
from .state import RespondentState


def _choice_label(survey: Survey | None, question_id: str, choice_id: str) -> str:
    q = survey.questions.get(question_id) if survey else None
    for attr in ("choices", "items", "rows", "fields", "points"):
        for c in getattr(q, attr, None) or []:
            if c.id == choice_id or f"x{c.id}" == choice_id:
                return c.text.plain
    return choice_id


def resolve_pipe(
    pipe: Pipe, state: RespondentState, survey: Survey | None, rng: random.Random
) -> str | None:
    """The value of one reference, or None if it cannot be resolved."""
    if pipe.kind == "embedded_data":
        return state.embedded.get(pipe.name or "", "")
    if pipe.kind == "loop_merge":
        loop = state.loop
        if loop is None:
            return ""
        if pipe.name == "CurrentLoopNumber":
            return str(loop.number)
        if pipe.name == "TotalLoops":
            return str(loop.total)
        return loop.fields.get(pipe.name or "", "")
    if pipe.kind == "random":
        m = re.match(r"int/(-?\d+):(-?\d+)", pipe.selector or "")
        if m:
            lo, hi = sorted((int(m.group(1)), int(m.group(2))))
            return str(rng.randint(lo, hi))
        state.note(f"random reference {pipe.raw} not supported")
        return None
    if pipe.scheme == "loc":
        return state.location.get(pipe.path, "")
    if pipe.kind == "date":
        return date.today().isoformat()
    if pipe.kind == "question" and pipe.question_id:
        answer = state.answer(pipe.question_id, pipe.loop_iteration)
        if answer is None:
            return ""
        value, sel = answer.value, pipe.selector or ""
        if sel == "ChoiceTextEntryValue":
            if pipe.choice_id is not None:
                if pipe.choice_id in answer.text:
                    return answer.text[pipe.choice_id]
                return str(value.get(pipe.choice_id, "")) if isinstance(value, dict) else ""
            return value if isinstance(value, str) else ""
        if sel == "ChoiceNumericEntryValue" and isinstance(value, dict):
            return str(value.get(pipe.choice_id, ""))
        if sel.startswith("ChoiceGroup/"):
            ids = value if isinstance(value, list) else ([value] if value is not None else [])
            return ", ".join(_choice_label(survey, pipe.question_id, str(i)) for i in ids)
        if isinstance(value, (str, int, float)):
            return _choice_label(survey, pipe.question_id, str(value))
        return ""
    state.note(f"piped reference {pipe.raw} not supported")
    return None


def render(
    text: Text,
    state: RespondentState,
    survey: Survey | None = None,
    rng: random.Random | None = None,
) -> str:
    """``text`` with every resolvable reference filled in."""
    rng = rng or random.Random()
    return text.render(lambda p: resolve_pipe(p, state, survey, rng))
