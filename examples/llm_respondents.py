"""Simulate survey respondents with Claude.

    ANTHROPIC_API_KEY=... uv run --with anthropic python examples/llm_respondents.py

Each simulated respondent gets a persona and one conversation that grows as they
move through the survey. Every request carries everything this respondent has
been shown so far: text-only screens such as consent forms and treatment
vignettes, earlier questions and their own answers, and the whole current page.
Each answer is a JSON object constrained to the valid choice ids, so it can be
recorded. The survey's own randomization, branches and display logic decide
what each persona sees.

Claude is given only what the respondent saw (``ctx.history``, see
``surveyir.runtime.transcript``): never the assigned condition, embedded data,
or the survey's logic.

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
from surveyir.runtime import Display, ResponseContext, Simulator, transcript
from surveyir.runtime.trace import Response

MODEL = "claude-opus-5-5"


def answer_schema(view: Display) -> dict | None:
    """JSON schema for a valid answer to ``view``, or None for unsupported kinds."""
    ids = [c.id for c in view.choices]
    if view.kind == "choice" and ids:
        if view.multiple:
            answer = {"type": "array", "items": {"type": "string", "enum": ids}}
        else:
            answer = {"type": "string", "enum": ids}
    elif view.kind == "matrix" and view.mode in ("single", "bipolar", "dropdown") and ids:
        cols = [c.id for c in view.columns]
        answer = {
            "type": "object",
            "properties": {row: {"type": "string", "enum": cols} for row in ids},
            "required": ids,
            "additionalProperties": False,
        }
    elif view.kind == "text_entry" and view.mode != "form":
        answer = {"type": "string"}
    else:
        return None  # sliders, rank order, forms, ...: not covered by this example
    return {
        "type": "object",
        "properties": {"answer": answer},
        "required": ["answer"],
        "additionalProperties": False,
    }


def conversation(ctx: ResponseContext) -> list[dict]:
    """The respondent's survey so far as alternating turns.

    What was shown goes in user turns (as ``transcript`` renders it), each earlier
    answer in the assistant turn that gave it, and the last user turn asks for
    the current question, whose page is already in the history.
    """
    messages: list[dict] = []
    shown: list[Any] = []
    for ob in ctx.history:
        if isinstance(ob, Response):
            messages.append({"role": "user", "content": _turn(shown, ob.qid)})
            messages.append({"role": "assistant", "content": json.dumps({"answer": ob.value})})
            shown = []
        else:
            shown.append(ob)
    messages.append({"role": "user", "content": _turn(shown, ctx.view.qid, ctx.view)})
    return messages


def requirements(view: Display) -> str:
    """The question's validation, as the respondent is told it (Qualtrics would
    reject an answer that breaks it)."""
    v, out = view.validation, []
    if "min_chars" in v:
        out.append(f"Write at least {v['min_chars']} characters.")
    if "max_chars" in v:
        out.append(f"Write at most {v['max_chars']} characters.")
    if v.get("content_type") == "ValidNumber" or "number_min" in v or "number_max" in v:
        bounds = [f"from {v['number_min']:g}" if "number_min" in v else "",
                  f"to {v['number_max']:g}" if "number_max" in v else ""]
        out.append(" ".join(["Enter a number", *filter(None, bounds)]) + ".")
    if "min_choices" in v:
        out.append(f"Select at least {v['min_choices']}.")
    if "max_choices" in v:
        out.append(f"Select at most {v['max_choices']}.")
    return " ".join(out)


def _turn(shown: list[Any], qid: str, view: Display | None = None) -> str:
    seen = transcript(shown)
    ask = f"Answer question {qid}."
    if view is not None and (rules := requirements(view)):
        ask += f" {rules}"
    return f"{seen}\n\n{ask}" if seen else ask


class ClaudeRespondent:
    """A ``Respondent`` for ``Simulator`` that asks Claude to answer as ``persona``.

    ``client`` is an ``anthropic.Anthropic()`` instance (or anything with the same
    ``beta.messages.create`` method, which keeps this testable offline).
    """

    def __init__(self, client: Any, persona: str, model: str = MODEL) -> None:
        self.client, self.model = client, model
        self.system = (
            f"You are taking part in a survey. {persona} You will see each screen of the "
            "survey as it is shown to you, including your earlier answers. Answer every "
            "question as this person would, honestly and in character. Use the bracketed "
            "ids to pick choices; for text questions, write what this person would type."
        )

    def answer(self, ctx: ResponseContext) -> Any:
        schema = answer_schema(ctx.view)
        if schema is None:
            return None
        response = self.client.beta.messages.create(
            model=self.model,
            max_tokens=1024,  # answers are short JSON objects
            system=self.system,
            messages=conversation(ctx),
            output_config={"format": {"type": "json_schema", "schema": schema}},
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
        )
        if response.stop_reason == "refusal":
            return None
        text = next(b.text for b in response.content if b.type == "text")
        return json.loads(text)["answer"]


def claude_answerer(client: Any, persona: str, model: str = MODEL) -> ClaudeRespondent:
    """A ``ClaudeRespondent`` (kept under this name for existing callers)."""
    return ClaudeRespondent(client, persona, model)


PERSONAS = [
    "You are a 34-year-old nurse in Ohio who follows politics casually.",
    "You are a 61-year-old retired engineer in Arizona with strong opinions.",
    "You are a 22-year-old college student in Oregon who is skeptical of surveys.",
]


def main(path: str = "tests/fixtures/qualtrics/hiring_algorithms.qsf") -> None:
    import anthropic

    client = anthropic.Anthropic()
    survey = surveyir.load(path)
    # strict (the default): the run stops at anything surveyir cannot administer
    # exactly. A model's answer that breaks a question's validation is recorded and
    # accepted (Qualtrics would have asked again); see `surveyir check`.
    sim = Simulator(survey, seed=1, allow={"answer.invalid"})
    columns = [c.name for c in surveyir.response_columns(survey)]
    writer = csv.writer(sys.stdout)
    writer.writerow(columns)
    for persona in PERSONAS:
        run = sim.respondent(ClaudeRespondent(client, persona))
        writer.writerow([value for _, value in run.cells(survey)])


if __name__ == "__main__":
    main(*sys.argv[1:])
