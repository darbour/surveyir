"""Rich text with structured piped-text references.

Survey platforms let text reference earlier answers, embedded data and loop
fields (Qualtrics calls this *piped text*: ``${q://QID5/ChoiceTextEntryValue}``;
LimeSurvey writes an ExpressionScript expression in braces: ``{Q1.shown}``).
The IR keeps the original markup, a plain-text rendering, and a parsed list of
every reference so simulators can substitute values deliberately.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Literal

from pydantic import Field

from .base import IRModel
from .logic import Expr

PipeKind = Literal[
    "question",  # an earlier answer: q://QID5/ChoiceTextEntryValue
    "embedded_data",  # e://Field/name
    "loop_merge",  # lm://Field/1, lm://CurrentLoopNumber
    "scoring",  # gr://SC_x/Score
    "random",  # rand://int/1:10
    "date",  # date://CurrentDate/...
    "expression",  # an expression evaluated in place: LimeSurvey {Q1.shown}, {if(...)}
    "other",
]


class Pipe(IRModel):
    """One piped-text reference inside a text string."""

    raw: str = Field(description="The reference exactly as written, e.g. '${e://Field/x}'.")
    kind: PipeKind
    scheme: str = Field(description="Source scheme without '://', e.g. 'q', 'e', 'lm'.")
    path: str = Field(description="Everything after '://'.")
    question_id: str | None = None
    loop_iteration: int | None = Field(
        default=None, description="Loop & Merge iteration prefix, e.g. 3 for 'q://3_QID9/...'."
    )
    selector: str | None = Field(
        default=None, description="What is piped, e.g. 'ChoiceTextEntryValue', 'SelectedChoices'."
    )
    choice_id: str | None = None
    name: str | None = Field(
        default=None, description="Embedded-data field name, loop field number, etc."
    )
    expression: Expr | None = Field(
        default=None, description="Parsed expression (kind 'expression'); its source is `path`."
    )


PIPE_PATTERN = re.compile(r"\$\{(?P<scheme>[A-Za-z]+)://(?P<path>[^}]*)\}")
_NEVER = re.compile(r"(?!x)x")


class Text(IRModel):
    """Displayed text in three forms: source markup, plain text, and references."""

    html: str = Field(default="", description="Original markup from the source file.")
    plain: str = Field(default="", description="Readable plain text; piped references kept as-is.")
    pipes: list[Pipe] = Field(default_factory=list)
    syntax: Literal["qualtrics", "expression_script"] = Field(
        default="qualtrics",
        description="How references are written: Qualtrics '${scheme://path}', or "
        "LimeSurvey ExpressionScript '{expression}' (each pipe's `raw` is what is replaced).",
    )

    def pattern(self) -> re.Pattern[str]:
        """What a reference looks like in this text."""
        if self.syntax == "qualtrics":
            return PIPE_PATTERN
        raws = sorted({p.raw for p in self.pipes}, key=len, reverse=True)
        return re.compile("|".join(map(re.escape, raws))) if raws else _NEVER

    def render(self, resolve: Callable[[Pipe], str | None], *, as_typed: bool = False) -> str:
        """Return ``plain`` (or, with ``as_typed``, ``html``: the text exactly as entered,
        markup and whitespace kept) with each reference replaced by ``resolve(pipe)``.

        References for which ``resolve`` returns ``None`` are left untouched.
        """
        by_raw = {p.raw: p for p in self.pipes}

        def sub(match: re.Match[str]) -> str:
            pipe = by_raw.get(match.group(0))
            if pipe is None:
                return match.group(0)
            value = resolve(pipe)
            return match.group(0) if value is None else value

        source = self.html if as_typed and self.html else self.plain
        return self.pattern().sub(sub, source)

    def __str__(self) -> str:
        return self.plain
