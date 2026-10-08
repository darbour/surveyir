"""Resolve piped text (``${...}``) against a respondent's state.

References the runtime cannot reproduce exactly are recorded as approximations
(``pipe.*`` codes, located at the reference path; see ``runtime.execution``):
a location reference with no supplied location (``pipe.loc``), a date
(``pipe.date``: rendered as today's ISO date), a random reference other than
``rand://int`` and any other unsupported scheme. Reading a panel, recipient or
URL field that was never supplied records ``embedded.unset``. ``affects`` says
what the rendered text feeds: ``"exposure"`` for displayed text; for the value
of an embedded-data field, what reading that field affects.
"""

from __future__ import annotations

import random
import re
from datetime import date

from ..model import Pipe, Survey, Text
from ..model.text import PIPE_PATTERN
from .state import RespondentState
from .trace import Affects


def _choice_label(survey: Survey | None, question_id: str, choice_id: str) -> str:
    q = survey.questions.get(question_id) if survey else None
    for attr in ("choices", "items", "rows", "fields", "points"):
        for c in getattr(q, attr, None) or []:
            if c.id == choice_id or f"x{c.id}" == choice_id:
                return c.text.plain
    return choice_id


def resolve_pipe(
    pipe: Pipe,
    state: RespondentState,
    survey: Survey | None,
    rng: random.Random,
    affects: Affects = "exposure",
) -> str | None:
    """The value of one reference, or None if it cannot be resolved."""
    if pipe.kind == "embedded_data":
        return state.read_embedded(pipe.name or "", affects)
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
        state.approximate(
            "pipe.random_unsupported",
            pipe.path,
            affects,
            f"random reference {pipe.raw} not supported; left as written",
        )
        return None
    if pipe.scheme == "loc":
        if pipe.path in state.location:
            return state.location[pipe.path]
        state.approximate(
            "pipe.loc",
            pipe.path,
            affects,
            f"location reference {pipe.raw} with no supplied location; rendered empty",
        )
        return ""
    if pipe.kind == "date":
        state.approximate(
            "pipe.date",
            pipe.path,
            affects,
            f"date reference {pipe.raw} rendered as today's ISO date",
        )
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
    state.approximate(
        "pipe.unsupported",
        pipe.path,
        affects,
        f"piped reference {pipe.raw} not supported; left as written",
    )
    return None


def render(
    text: Text,
    state: RespondentState,
    survey: Survey | None = None,
    rng: random.Random | None = None,
    affects: Affects = "exposure",
    *,
    as_typed: bool = False,
) -> str:
    """``text`` with every resolvable reference filled in. ``as_typed`` renders the text
    exactly as entered (markup and whitespace kept), as Qualtrics stores embedded data."""
    rng = rng or random.Random()
    return text.render(lambda p: resolve_pipe(p, state, survey, rng, affects), as_typed=as_typed)


def render_display(
    text: Text, state: RespondentState, survey: Survey | None, rng: random.Random
) -> tuple[str, str]:
    """``text`` as shown, plain and HTML, resolving each reference once.

    Both renderings share the resolved values (a ``rand://`` reference draws once),
    and neither keeps a resolvable reference, so field names stay out of the
    output. References that cannot be resolved are left as written.
    """
    cache: dict[str, str | None] = {}

    def resolve(pipe: Pipe) -> str | None:
        if pipe.raw not in cache:
            cache[pipe.raw] = resolve_pipe(pipe, state, survey, rng)
        return cache[pipe.raw]

    plain = text.render(resolve)
    by_raw = {p.raw: p for p in text.pipes}

    def sub(match: re.Match[str]) -> str:
        pipe = by_raw.get(match.group(0))
        value = resolve(pipe) if pipe is not None else None
        return match.group(0) if value is None else value

    return plain, PIPE_PATTERN.sub(sub, text.html)
