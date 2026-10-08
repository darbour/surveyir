"""What a simulated respondent experienced, and what the runtime knew.

A run produces two ordered streams:

* the **trace**: observations the respondent could perceive, i.e. pages,
  rendered question text, the choices and columns shown and their order, media,
  their own responses. A respondent model is given exactly this history, and
  nothing else.
* the **audit**: everything the runtime did that the respondent could not see,
  i.e. randomizer decisions and the probabilities they were drawn from, branch
  evaluations, hidden questions and choices, embedded data, skips, and every
  approximation the runtime made, with what it could affect.

Both are immutable records: auditing a simulated experiment means reading them,
not re-deriving them from the instrument.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

Affects = Literal["assignment", "exposure", "routing", "outcome", "none"]
#: what an approximation can change; any of these but "none" makes it execution-affecting
EXECUTION_AFFECTING: frozenset[str] = frozenset({"assignment", "exposure", "routing", "outcome"})


# --------------------------------------------------------------------------- observations


@dataclass(frozen=True)
class ChoiceShown:
    id: str
    text: str
    #: (source question id, source choice id) for carried-forward choices
    carried_from: tuple[str, str] | None = None


@dataclass(frozen=True)
class MediaRef:
    kind: str
    url: str | None = None
    id: str | None = None


@dataclass(frozen=True)
class PageStart:
    page: int  # global page number for this respondent, from 1
    block_id: str
    loop_id: str | None = None
    loop_number: int | None = None
    loop_total: int | None = None


@dataclass(frozen=True)
class Display:
    """A question (or a text-only screen) as the respondent saw it."""

    page: int
    seq: int  # position among displays for this respondent, from 1
    qid: str
    loop_id: str | None
    kind: str
    block_id: str
    text: str  # rendered: piped values substituted
    html: str = ""
    choices: tuple[ChoiceShown, ...] = ()  # in displayed order
    columns: tuple[ChoiceShown, ...] = ()  # matrix scale points, in displayed order
    media: tuple[MediaRef, ...] = ()
    flipped: bool = False
    multiple: bool | None = None
    required: Literal["off", "force", "request"] = "off"
    revealed_in_page: bool = False  # shown by in-page display logic after an answer
    #: answer constraints the respondent is told about (e.g. min/max selections)
    validation: Mapping[str, Any] = field(default_factory=dict)

    @property
    def responds(self) -> bool:
        """Whether this display asks for a response."""
        from ..model.question import NON_RESPONSE_KINDS

        return self.kind not in NON_RESPONSE_KINDS


@dataclass(frozen=True)
class Response:
    page: int
    seq: int  # the Display's seq
    qid: str
    loop_id: str | None
    value: Any
    text: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class PageSubmit:
    page: int


@dataclass(frozen=True)
class End:
    reason: Literal["flow_end", "end_survey", "skip_logic", "quota"]
    finished: bool
    flow_id: str | None = None


Observation = PageStart | Display | Response | PageSubmit | End


# --------------------------------------------------------------------------- audit


@dataclass(frozen=True)
class RandomizerDecision:
    node_id: str  # flow id, or block/question id for order randomization
    kind: Literal["flow", "block", "choices", "columns", "loop"]
    options: tuple[str, ...]
    shown: tuple[str, ...]  # chosen options, in presentation order
    #: nominal inclusion share of each option (k/n)
    p_nominal: Mapping[str, float]
    #: inclusion probability of each option given the balancing history, when the
    #: draw depended on it (least-filled presentation); None for plain random draws
    p_given_history: Mapping[str, float] | None = None
    balancer_before: Mapping[str, int] | None = None
    loop_id: str | None = None


@dataclass(frozen=True)
class BranchEval:
    node_id: str
    result: bool
    unknown: bool = False  # depended on something the runtime could not evaluate


@dataclass(frozen=True)
class Hidden:
    qid: str
    loop_id: str | None
    reason: Literal["display_logic", "in_page_display_logic", "skip", "end"]


@dataclass(frozen=True)
class ChoiceHidden:
    qid: str
    loop_id: str | None
    choice_id: str


@dataclass(frozen=True)
class EmbeddedSet:
    name: str
    value: str
    source: str  # flow id, "web_service:<id>", "javascript:<qid>", "respondent", ...


@dataclass(frozen=True)
class SkipTaken:
    qid: str
    loop_id: str | None
    destination: str


@dataclass(frozen=True)
class Approximation:
    """Something the runtime could not reproduce exactly from the instrument."""

    code: str  # stable identifier, e.g. "web_service", "logic.geo_ip"
    location: str  # flow id, question id, ...
    affects: Affects
    detail: str

    @property
    def execution_affecting(self) -> bool:
        return self.affects in EXECUTION_AFFECTING


@dataclass(frozen=True)
class Allowed:
    """An execution-affecting approximation the caller explicitly accepted."""

    code: str
    location: str


AuditEvent = (RandomizerDecision | BranchEval | Hidden | ChoiceHidden | EmbeddedSet | SkipTaken
              | Approximation | Allowed)
