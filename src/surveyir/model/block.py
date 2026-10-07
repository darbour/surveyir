"""Blocks: ordered groups of questions, split into pages."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field

from .base import Extensible, IRModel
from .logic import Condition
from .question import Randomization


class SkipLogic(Extensible):
    """After answering, jump elsewhere if ``condition`` holds."""

    condition: Condition
    destination: Literal["end_of_block", "end_of_survey", "question"]
    target_question_id: str | None = Field(
        default=None, description="Set when destination is 'question'."
    )


class QuestionRef(IRModel):
    type: Literal["question"] = "question"
    question_id: str
    skip_logic: list[SkipLogic] = Field(default_factory=list)


class PageBreak(IRModel):
    type: Literal["page_break"] = "page_break"


BlockElement = Annotated[QuestionRef | PageBreak, Field(discriminator="type")]


class LoopAndMerge(Extensible):
    """Repeat a block once per loop, substituting per-loop field values.

    ``static`` loops come from a fixed table (``fields``); ``question`` loops run
    once per choice of another question (e.g. once per selected choice).
    """

    source: Literal["static", "question"]
    question_id: str | None = None
    locator: str | None = Field(
        default=None, description="Source locator for question-driven loops."
    )
    fields: dict[str, dict[str, str]] = Field(
        default_factory=dict,
        description="Loop id -> {field number -> value}. Static loops only.",
    )
    randomization: Randomization | None = None


class Block(Extensible):
    id: str
    description: str = ""
    elements: list[BlockElement] = Field(
        default_factory=list, description="Questions and page breaks, in presentation order."
    )
    randomization: Randomization | None = Field(
        default=None, description="Question-order randomization within the block."
    )
    loop: LoopAndMerge | None = None
    is_default: bool = Field(
        default=False, description="The platform's default block (Qualtrics 'Default')."
    )

    @property
    def question_ids(self) -> list[str]:
        return [e.question_id for e in self.elements if isinstance(e, QuestionRef)]

    def pages(self) -> list[list[str]]:
        """Question ids grouped by page, in presentation order."""
        pages: list[list[str]] = [[]]
        for element in self.elements:
            if isinstance(element, PageBreak):
                pages.append([])
            else:
                pages[-1].append(element.question_id)
        return [page for page in pages if page]
