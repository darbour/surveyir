"""Strict execution: what the runtime does when it cannot reproduce the instrument.

Every gap the runtime can hit is an ``Approximation`` with a stable code (the
canonical list is ``APPROXIMATIONS``). A gap that can change who is assigned to
what, what they see, where they go, or what is recorded (``EXECUTION_AFFECTING``)
stops a strict run with ``ExecutionError``, unless the caller

* supplies an implementation (``implementations=``), which removes the gap;
* accepts it (``allow=``, by ``"code"`` or ``"code:location"``), which records
  an ``Allowed`` audit event; or
* runs permissively (``strict=False``), which records it and carries on.

Locations (what ``"code:location"`` matches):

==============================  ============================================
web_service, library_block,     flow id (``FL_12``)
flow.unsupported,
flow.authenticator,
branch.no_condition
flow.missing_block              the missing block id
question.unsupported,           question id
javascript, columns.unbalanced,
carry_forward.*, answer.invalid
embedded.unset                  embedded-data field name
loop.*                          block id
quota.action_ignored,           quota id
quota.snapshot
pipe.*                          the reference path (``CountryName`` for
                                ``${loc://CountryName}``)
replay.gap,                     ``kind:node_id`` (``flow:FL_3``, ``block:BL_x``),
respondent.out_of_order         with ``#loop_id`` inside a loop; the quota id
                                for a quota checked out of order
logic.*                         the operand's question id, else its raw locator
                                (``loc://CountryName``); see ``logic.operand_location``
==============================  ============================================

Implementations (``implementations=``), by key:

==============  ===========================================================
web_service     ``callable(node, state) -> dict`` of embedded fields, or a
                dict of such callables keyed by flow id
javascript      ``{qid: callable(state) -> dict}`` of embedded fields the
                question's JavaScript would set
embedded        ``{field: value}`` for panel / recipient / URL fields
location        ``{name: value}`` for ``${loc://...}`` and GeoIP logic, e.g.
                ``{"CountryName": "Australia"}``
==============  ===========================================================
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from .design import JS_RANDOM
from .trace import EXECUTION_AFFECTING, Affects, Allowed, Approximation

#: every approximation code the runtime records: (default affects, meaning)
APPROXIMATIONS: dict[str, tuple[Affects, str]] = {
    # flow
    "web_service": ("assignment", "web service not called; the fields it sets are left empty"),
    "library_block": ("exposure", "library block is not in the file; skipped"),
    "flow.unsupported": ("routing", "unsupported flow element skipped; its children are run"),
    "flow.authenticator": ("routing", "authenticator assumed passed by every respondent"),
    "flow.missing_block": ("exposure", "flow references a block that is not in the file"),
    "branch.no_condition": ("routing", "branch has no readable condition; skipped"),
    "quota.action_ignored": ("routing", "quota action other than ending the survey not executed"),
    "quota.snapshot": ("routing", "quota status taken at respondent start, not updated mid-walk"),
    # questions
    "question.unsupported": ("outcome", "unsupported question type: shown but not answered"),
    "answer.invalid": (
        "outcome",
        "answer violates the question's validation, which Qualtrics "
        "would not have accepted; recorded as given",
    ),
    "javascript": ("exposure", "question JavaScript not run"),
    "columns.unbalanced": ("exposure", "evenly presented column subset drawn without balancing"),
    "loop.unknown_mode": ("exposure", "loop source mode not handled; looped over every choice"),
    "loop.no_displayed_choices": ("exposure", "loop over displayed choices of an unshown question"),
    "carry_forward.unsupported_mode": ("exposure", "carry-forward mode not handled; all carried"),
    "carry_forward.reference": ("exposure", "carry forward from a reference list; nothing carried"),
    # data
    "embedded.unset": ("routing", "panel, recipient or URL field not supplied; read as empty"),
    "pipe.loc": ("exposure", "location reference with no supplied location; rendered empty"),
    "pipe.date": ("exposure", "date reference rendered as today's ISO date, not its format"),
    "pipe.random_unsupported": ("exposure", "random reference other than rand://int left as is"),
    "pipe.unsupported": ("exposure", "reference the runtime cannot resolve (gr://, m://, ...)"),
    # logic
    "logic.geo_ip": ("routing", "GeoIP condition with no supplied location; false"),
    "logic.scoring": ("routing", "scoring condition (scores are not computed); false"),
    "logic.quota": ("routing", "quota operand with an operator other than met/not met; false"),
    "logic.other_operand": ("routing", "condition on an operand the runtime cannot read; false"),
    "logic.other_operator": ("routing", "condition with an unknown or invalid operator; false"),
    "logic.regex_invalid": ("routing", "invalid regular expression; the condition is false"),
    "logic.non_numeric": ("routing", "ordering comparison on a non-numeric value; false"),
    # replay
    "replay.gap": (
        "exposure",
        "a replay has no recorded decision for a randomization point; "
        "drawn by the fallback chooser (assignment for flow randomizers)",
    ),
    "respondent.out_of_order": (
        "assignment",
        "respondent walked out of order (respondent(index=...)): a balanced draw or a "
        "quota check uses the simulator's current counts, not those left by the "
        "respondents before it (exposure for order draws, routing for quotas)",
    ),
}

#: approximation code -> the ``implementations`` key that removes it
IMPLEMENTED_BY: dict[str, str] = {
    "web_service": "web_service",
    "javascript": "javascript",
    "embedded.unset": "embedded",
    "pipe.loc": "location",
    "logic.geo_ip": "location",
}

#: question-sourced loop modes the runtime administers exactly (last locator segment)
LOOP_MODES: frozenset[str] = frozenset(
    {
        "SelectedChoices",
        "SelectedChoicesTextEntry",
        "EnteredChoicesTextEntry",
        "UnselectedChoices",
        "DisplayedChoices",
        "AllChoices",
        "MergeOnNumericResponse",
    }
)
#: carry-forward modes the runtime administers exactly
CARRY_FORWARD_MODES: frozenset[str] = frozenset(
    {
        "SelectedChoices",
        "UnselectedChoices",
        "NotSelectedChoices",
        "DisplayedChoices",
        "NotDisplayedChoices",
        "SelectedChoicesTextEntry",
        "EnteredChoicesTextEntry",
        "AllChoices",
    }
)

_JS_NOISE = re.compile(
    r"/\*.*?\*/|(?<!:)//[^\n]*"  # comments, but not the // of e:// or https://
    r"|Qualtrics\.SurveyEngine\.addOn(?:load|Ready|Unload|PageSubmit)\s*\(\s*"
    r"function\s*\(\s*\w*\s*\)\s*\{\s*\}\s*\)\s*;?",
    re.S,
)

_JS_ASSIGNS = re.compile(r"setJSEmbeddedData", re.I)  # Qualtrics' newer API; not in JS_RANDOM


def javascript_affects(js: str | None) -> Affects | None:
    """What a question's JavaScript can change: None if only comments and empty handlers.

    ``"assignment"`` when it randomizes or sets embedded data (``design.JS_RANDOM``,
    plus ``setJSEmbeddedData``),
    else ``"exposure"`` (it can change what is shown).
    """
    if not js:
        return None
    body = js
    while (stripped := _JS_NOISE.sub("", body)) != body:  # nested empty handlers
        body = stripped
    if not body.strip():
        return None
    return "assignment" if JS_RANDOM.search(body) or _JS_ASSIGNS.search(body) else "exposure"


def approximation(
    code: str, location: str, detail: str | None = None, affects: Affects | None = None
) -> Approximation:
    """An ``Approximation`` for a known code, with its default ``affects`` and meaning."""
    default, meaning = APPROXIMATIONS[code]
    return Approximation(code, location, affects or default, detail or meaning)


class ExecutionError(RuntimeError):
    """A strict run reached something it cannot administer exactly.

    ``approximation`` is the gap; ``run`` the respondent's partial run (its trace
    and audit up to the gap) when raised by ``Simulator``.
    """

    run: Any = None

    def __init__(self, approximation: Approximation) -> None:
        self.approximation = a = approximation
        key = IMPLEMENTED_BY.get(a.code)
        fix = f"implementations={{{key!r}: ...}}, " if key else ""
        super().__init__(
            f"{a.code} at {a.location} (affects {a.affects}): {a.detail}. To proceed, pass "
            f"{fix}allow={{{a.code!r}}} or allow={{'{a.code}:{a.location}'}}, or strict=False."
        )


@dataclass
class ExecutionPolicy:
    """How a run treats approximations; the walker calls ``handle`` for each one.

    ``allow`` entries are ``"code"`` (everywhere) or ``"code:location"``; codes
    must be in ``APPROXIMATIONS`` (so every code the runtime emits is registered).
    ``implementations`` is keyed as in the module docstring.
    """

    strict: bool = True
    allow: frozenset[str] = frozenset()
    implementations: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.allow = frozenset(self.allow)
        unknown = {a.partition(":")[0] for a in self.allow} - APPROXIMATIONS.keys()
        if unknown:
            raise ValueError(f"unknown approximation code(s) in allow: {sorted(unknown)}")

    @classmethod
    def from_args(
        cls,
        strict: bool | None = True,
        allow: Iterable[str] | None = None,
        implementations: Mapping[str, Any] | None = None,
    ) -> ExecutionPolicy:
        """Build a policy from CLI-style values (``allow`` may contain comma lists)."""
        entries = {e.strip() for a in allow or () for e in a.split(",") if e.strip()}
        return cls(
            strict=True if strict is None else strict,
            allow=frozenset(entries),
            implementations=dict(implementations or {}),
        )

    def allows(self, approx: Approximation) -> bool:
        """Whether ``approx`` was explicitly accepted (by code, or by code and location)."""
        return approx.code in self.allow or f"{approx.code}:{approx.location}" in self.allow

    def permits(self, approx: Approximation) -> bool:
        """Whether a run under this policy may continue past ``approx``."""
        return not (self.strict and approx.execution_affecting) or self.allows(approx)

    def handle(self, approx: Approximation) -> Allowed | None:
        """Decide on one approximation as the walker meets it.

        Returns an ``Allowed`` record whenever an explicitly accepted,
        execution-affecting approximation is met (stateless: the caller dedups
        into each respondent's audit, as ``RespondentState`` does for
        approximations). Raises ``ExecutionError`` if strict and not accepted.
        Gaps that affect nothing never raise.
        """
        if approx.affects not in EXECUTION_AFFECTING:
            return None
        if self.allows(approx):
            return Allowed(approx.code, approx.location)
        if self.strict:
            raise ExecutionError(approx)
        return None

    def implementation(self, key: str, location: str) -> Any:
        """The implementation for ``key`` at ``location``, or None.

        ``key`` is an ``implementations`` key or an approximation code
        (``IMPLEMENTED_BY``). A callable entry applies everywhere; a mapping
        entry is looked up by location (``location`` keys also match the
        ``loc://`` form used by GeoIP logic).
        """
        entry = self.implementations.get(IMPLEMENTED_BY.get(key, key))
        if entry is None:
            return None
        if callable(entry):
            return entry
        if isinstance(entry, Mapping):
            if location in entry:
                return entry[location]
            return entry.get(location.removeprefix("loc://"))
        return None
