"""What a respondent has seen and answered so far."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from .trace import Affects, Approximation


@dataclass
class Answer:
    """A response to one question.

    ``value`` depends on the question kind:

    ========================  ==================================================
    choice                    choice id (single) or list of ids (multiple)
    matrix                    {row id: answer id}; multiple: {row id: [ids]};
                              text: {row id: {answer id: text}}
    side_by_side              {sub-question id: matrix value}
    text_entry                text; form mode: {field id: text}
    slider, constant_sum      {item id: number}
    rank_order                {item id: rank (1 = first)}
    drill_down                {level id: option id}
    pick_group_rank           {group index: [item ids, ranked]}
    highlight                 {category id: [word ids]}
    hot_spot                  {region id: state id}
    heat_map                  [(x, y), ...]
    graphic_slider            point id
    file_upload, signature    file name
    ========================  ==================================================

    ``text`` holds "Other, please specify" text by choice (or row) id.
    """

    value: Any = None
    text: dict[str, str] = field(default_factory=dict)


@dataclass
class LoopContext:
    loop_id: str
    number: int
    total: int
    fields: dict[str, str]


@dataclass
class RespondentState:
    """Everything logic and piped text can refer to while a respondent is walked."""

    embedded: dict[str, str] = field(default_factory=dict)
    answers: dict[tuple[str, str | None], Answer] = field(default_factory=dict)
    displayed: set[tuple[str, str | None]] = field(default_factory=set)
    displayed_choices: dict[tuple[str, str | None], list[str]] = field(default_factory=dict)
    loop: LoopContext | None = None
    device: str = "desktop"
    location: dict[str, str] = field(default_factory=dict)  # for ${loc://City} etc.
    quotas_met: set[str] = field(default_factory=set)
    notes: list[str] = field(default_factory=list)
    approximations: list[Approximation] = field(default_factory=list)
    #: called with every approximation before it is recorded (strict mode raises here)
    on_approximation: Callable[[Approximation], None] | None = field(
        default=None, repr=False, compare=False)

    # ------------------------------------------------------------------ lookups

    def key(self, question_id: str, loop: int | str | None = None) -> tuple[str, str | None]:
        """Answer key: explicit loop id, else the current loop (if inside one)."""
        if loop is not None:
            return question_id, str(loop)
        return question_id, (self.loop.loop_id if self.loop else None)

    def answer(self, question_id: str, loop: int | str | None = None) -> Answer | None:
        found = self.answers.get(self.key(question_id, loop))
        if found is None and loop is None and self.loop is not None:
            found = self.answers.get((question_id, None))  # question outside the loop
        return found

    def was_displayed(self, question_id: str, loop: int | str | None = None) -> bool:
        return self.key(question_id, loop) in self.displayed or (
            loop is None and (question_id, None) in self.displayed
        )

    def note(self, message: str) -> None:
        if message not in self.notes:
            self.notes.append(message)

    def approximate(self, code: str, location: str, affects: Affects, detail: str) -> None:
        """Record that the runtime could not reproduce something exactly.

        The runtime that owns this state decides what an approximation means
        (``on_approximation``): strict runs raise on execution-affecting ones
        unless they were allowed. ``notes`` keeps the human-readable detail.
        """
        approx = Approximation(code, location, affects, detail)
        if self.on_approximation is not None:
            self.on_approximation(approx)
        if approx not in self.approximations:
            self.approximations.append(approx)
        self.note(detail)
