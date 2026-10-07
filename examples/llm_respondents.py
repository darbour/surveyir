"""Simulate survey respondents with Claude.

    ANTHROPIC_API_KEY=... uv run --with anthropic python examples/llm_respondents.py

Each simulated respondent gets a persona. For every question the survey shows
them, Claude sees the question as displayed (piped text filled in, choices in
the order shown) and answers in a JSON shape constrained to the valid choice
ids, so every answer can be recorded. The survey's own randomization, branches
and display logic decide which questions each persona sees.

Requests opt in to server-side refusal fallbacks (``fallbacks="default"``): if a
request is declined, the API retries it on a fallback model inside the same
call. Remove ``betas`` and ``fallbacks`` to turn that off. A request that is
still refused is recorded as no answer.
"""

from __future__ import annotations

import csv
import json
import sys
from typing import Any

import surveyir
from surveyir.model import ChoiceQuestion, MatrixQuestion, TextEntryQuestion
from surveyir.runtime import QuestionView, RespondentState, Simulator

MODEL = "claude-opus-5-5"


def answer_schema(view: QuestionView) -> dict | None:
    """JSON schema for a valid answer to ``view``, or None for unsupported kinds."""
    q = view.question
    ids = [c.id for c in view.choices]
    if isinstance(q, ChoiceQuestion) and ids:
        if q.multiple:
            answer = {"type": "array", "items": {"type": "string", "enum": ids}}
        else:
            answer = {"type": "string", "enum": ids}
    elif isinstance(q, MatrixQuestion) and q.mode in ("single", "bipolar", "dropdown") and ids:
        cols = [c.id for c in view.columns]
        answer = {
            "type": "object",
            "properties": {row: {"type": "string", "enum": cols} for row in ids},
            "required": ids,
            "additionalProperties": False,
        }
    elif isinstance(q, TextEntryQuestion) and q.mode != "form":
        answer = {"type": "string"}
    else:
        return None  # sliders, rank order, forms, ...: not covered by this example
    return {
        "type": "object",
        "properties": {"answer": answer},
        "required": ["answer"],
        "additionalProperties": False,
    }


def render_question(view: QuestionView) -> str:
    """The question as the respondent sees it, with choice ids to answer with."""
    lines = [view.text]
    if isinstance(view.question, MatrixQuestion):
        lines.append("Rate each statement:")
        lines += [f"  [{c.id}] {c.text}" for c in view.choices]
        lines.append("Scale: " + "; ".join(f"[{c.id}] {c.text}" for c in view.columns))
    else:
        lines += [f"  [{c.id}] {c.text}" for c in view.choices]
    return "\n".join(lines)


def claude_answerer(client: Any, persona: str, model: str = MODEL):
    """An answerer for ``Simulator`` that asks Claude to respond as ``persona``.

    ``client`` is an ``anthropic.Anthropic()`` instance (or anything with the same
    ``beta.messages.create`` method, which keeps this testable offline).
    """
    system = (
        f"You are taking part in a survey. {persona} Answer every question as this "
        "person would, honestly and in character. Use the bracketed ids to pick "
        "choices; for text questions, write what this person would type."
    )

    def answer(view: QuestionView, state: RespondentState) -> Any:
        schema = answer_schema(view)
        if schema is None:
            return None
        response = client.beta.messages.create(
            model=model,
            max_tokens=1024,  # answers are short JSON objects
            system=system,
            messages=[{"role": "user", "content": render_question(view)}],
            output_config={"format": {"type": "json_schema", "schema": schema}},
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
        )
        if response.stop_reason == "refusal":
            return None
        text = next(b.text for b in response.content if b.type == "text")
        return json.loads(text)["answer"]

    return answer


PERSONAS = [
    "You are a 34-year-old nurse in Ohio who follows politics casually.",
    "You are a 61-year-old retired engineer in Arizona with strong opinions.",
    "You are a 22-year-old college student in Oregon who is skeptical of surveys.",
]


def main(path: str = "tests/fixtures/qualtrics/hiring_algorithms.qsf") -> None:
    import anthropic

    client = anthropic.Anthropic()
    survey = surveyir.load(path)
    sim = Simulator(survey, seed=1)
    columns = [c.name for c in surveyir.response_columns(survey)]
    writer = csv.writer(sys.stdout)
    writer.writerow(columns)
    for persona in PERSONAS:
        run = sim.respondent(claude_answerer(client, persona))
        writer.writerow([value for _, value in run.cells(survey)])


if __name__ == "__main__":
    main(*sys.argv[1:])
