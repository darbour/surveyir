"""Respondent models behind one interface: ``answer(ctx: ResponseContext)``.

* ``MockRespondent``: deterministic, no API. Its rating of the outcome moves with a
  phrase it can *see* in its context (a planted effect of known size), so the
  pipeline's ability to recover an effect can be checked end to end.
* ``IsolatedBaseline``: wraps any respondent and hands it each question on its own,
  without the history or the rest of the page: the simplified "ask the questions in
  isolation" baseline, with the same underlying model.
* ``LLMRespondent``: an adapter around ``examples/llm_respondents.ClaudeRespondent``.
  Constructed only behind ``--model claude --i-approve-spend`` in run.py.
* ``PromptMeter``: wraps a respondent and measures the prompts ``ClaudeRespondent``
  would send for the same contexts, for the cost estimate. Sends nothing.
"""

from __future__ import annotations

import json
import random
import sys
from dataclasses import dataclass, field, replace
from typing import Any

from study import ARMS, REPO

from surveyir.runtime import ResponseContext, transcript

sys.path.insert(0, str(REPO))  # for examples/
from examples.llm_respondents import (  # noqa: E402
    PERSONAS,
    ClaudeRespondent,
    answer_schema,
    conversation,
)


def visible_text(ctx: ResponseContext) -> str:
    """Everything the respondent can read when answering: the history (which already
    holds the current page's displays) plus anything on the page not yet in it."""
    extra = tuple(d for d in ctx.page if d not in ctx.history)
    return transcript(ctx.history + extra, include_ids=False)


def treatment_seen(ctx: ResponseContext) -> tuple[str, ...]:
    """Labels of the treatment vignettes whose text is in the respondent's context."""
    text = visible_text(ctx)
    return tuple(a.label for a in ARMS.values() if a.phrase in text)


@dataclass
class MockRespondent:
    """Deterministic rule-based respondent with a planted treatment effect.

    For a 1..9 rating it draws a base rating uniformly from 3..7 (seeded by the
    respondent index, so two conditions with the same index share a base), then adds
    ``+effect/2`` if the *broad* vignette is in its context and ``-effect/2`` if the
    *targeted* one is. It never reads assignment fields (it can't: ``ctx`` holds only
    what was displayed). With the vignette visible the planted contrast
    broad - targeted is exactly ``effect``; with the vignette hidden it is 0.
    """

    seed: int = 0
    effect: float = 2.0
    name: str = "mock"
    #: (respondent index, qid) -> treatment labels in context when answering
    seen: dict[tuple[int, str], tuple[str, ...]] = field(default_factory=dict)
    #: (respondent index, qid) -> the full text the model could read when answering
    context: dict[tuple[int, str], str] = field(default_factory=dict)

    def answer(self, ctx: ResponseContext) -> Any:
        view = ctx.view
        rng = random.Random(f"{self.seed}:{ctx.respondent_index}:{view.qid}")
        seen = treatment_seen(ctx)
        self.seen[ctx.respondent_index, view.qid] = seen
        self.context[ctx.respondent_index, view.qid] = visible_text(ctx)
        if view.kind == "text_entry":
            # the attention check asks for an even number in ALL-CAPS; the image shows 30
            return "THIRTY" if "ALL-CAPS" in view.text else "No comments."
        if view.kind != "choice" or not view.choices:
            return None
        if len(view.choices) < 9:
            return view.choices[0].id  # consent and other single-option screens
        shift = 0.0
        if "broad" in seen:
            shift += self.effect / 2
        if "targeted" in seen:
            shift -= self.effect / 2
        rating = rng.randint(3, 7) + shift
        rating = int(min(9, max(1, round(rating))))
        by_text = {c.text.split(":")[0].strip(): c.id for c in view.choices}
        return by_text.get(str(rating), view.choices[rating - 1].id)


@dataclass
class IsolatedBaseline:
    """Ask ``inner`` each question in isolation: no history, no other page content.

    The instrument is administered exactly as before (same trace, same arms); only
    the information passed to the model changes. This is what a context-free
    pipeline that sends questions one at a time effectively does.
    """

    inner: Any
    name: str = "isolated"

    def answer(self, ctx: ResponseContext) -> Any:
        return self.inner.answer(replace(ctx, history=(ctx.view,), page=(ctx.view,)))

    @property
    def seen(self) -> dict[tuple[int, str], tuple[str, ...]]:
        return getattr(self.inner, "seen", {})


class LLMRespondent:
    """One ``ClaudeRespondent`` conversation per simulated respondent, persona cycled
    from the example's ``PERSONAS``. Every call costs money: run.py only builds this
    behind ``--i-approve-spend``.
    """

    name = "claude"

    def __init__(self, client: Any, model: str) -> None:
        self.client, self.model = client, model
        self.seen: dict[tuple[int, str], tuple[str, ...]] = {}
        self._by_index: dict[int, ClaudeRespondent] = {}

    def answer(self, ctx: ResponseContext) -> Any:
        self.seen[ctx.respondent_index, ctx.view.qid] = treatment_seen(ctx)
        i = ctx.respondent_index
        if i not in self._by_index:
            persona = PERSONAS[i % len(PERSONAS)]
            self._by_index[i] = ClaudeRespondent(self.client, persona, self.model)
        return self._by_index[i].answer(ctx)


@dataclass
class PromptMeter:
    """Delegates to ``inner`` and records the size of the request ``ClaudeRespondent``
    would send for each context (system + conversation + schema), in characters.
    Questions the example does not send (no schema) cost nothing."""

    inner: Any
    isolated: bool = False
    requests: list[int] = field(default_factory=list)

    def answer(self, ctx: ResponseContext) -> Any:
        sent = replace(ctx, history=(ctx.view,), page=(ctx.view,)) if self.isolated else ctx
        schema = answer_schema(sent.view)
        if schema is not None:
            persona = PERSONAS[ctx.respondent_index % len(PERSONAS)]
            system = ClaudeRespondent(None, persona).system
            messages = conversation(sent)
            chars = len(system) + len(json.dumps(schema)) + sum(len(m["content"]) for m in messages)
            self.requests.append(chars)
        return self.inner.answer(ctx)

    @property
    def seen(self) -> dict[tuple[int, str], tuple[str, ...]]:
        return getattr(self.inner, "seen", {})
