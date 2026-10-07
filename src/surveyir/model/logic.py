"""Boolean conditions used by display logic, branches, skip logic and validation.

Every platform-specific logic format is normalized into one small expression
tree: ``And`` / ``Or`` nodes over ``Comparison`` leaves. Precedence has already
been resolved by the loader (Qualtrics evaluates AND before OR), so consumers
evaluate the tree as written.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field

from .base import Extensible, IRModel

OperandKind = Literal[
    "question",  # something about a question's response
    "embedded_data",  # an embedded-data / hidden variable
    "loop_field",  # a Loop & Merge field of the current iteration
    "device",  # respondent device type
    "geo_ip",  # respondent location from IP address
    "quota",  # a quota's state
    "scoring",  # a scoring category
    "constant",  # a fixed true/false value
    "other",
]

Operator = Literal[
    "selected",
    "not_selected",
    "displayed",
    "not_displayed",
    "equal",
    "not_equal",
    "greater_than",
    "greater_than_or_equal",
    "less_than",
    "less_than_or_equal",
    "empty",
    "not_empty",
    "contains",
    "does_not_contain",
    "matches_regex",
    "is",
    "is_not",
    "quota_met",
    "quota_not_met",
    "other",
]


class Operand(IRModel):
    """The left-hand side of a comparison: what the condition looks at."""

    kind: OperandKind
    question_id: str | None = None
    selector: str | None = Field(
        default=None,
        description=(
            "Which part of the question response: 'SelectableChoice', 'ChoiceTextEntryValue', "
            "'ChoiceNumericEntryValue', 'SelectableAnswer', 'Displayed', ..."
        ),
    )
    choice_id: str | None = None
    answer_id: str | None = None
    name: str | None = Field(default=None, description="Embedded-data or loop field name.")
    loop_iteration: int | None = None
    args: list[str] = Field(
        default_factory=list, description="Locator path segments after the selector, verbatim."
    )
    raw: str = Field(description="Source locator string, e.g. 'q://QID5/SelectableChoice/2'.")


class Comparison(Extensible):
    """A single test, e.g. 'Q5 choice 2 is selected' or 'Condition == 3'."""

    type: Literal["comparison"] = "comparison"
    left: Operand
    operator: Operator
    source_operator: str = Field(description="Operator name as written in the source file.")
    right: str | None = Field(default=None, description="Comparison value, if any.")
    ignore_case: bool = False
    description: str | None = Field(
        default=None, description="Human-readable description from the source, as plain text."
    )


class And(IRModel):
    type: Literal["and"] = "and"
    terms: list[Condition]


class Or(IRModel):
    type: Literal["or"] = "or"
    terms: list[Condition]


Condition = Annotated[Comparison | And | Or, Field(discriminator="type")]

And.model_rebuild()
Or.model_rebuild()


def iter_comparisons(condition: Condition):
    """Yield every ``Comparison`` leaf in ``condition``, depth first."""
    if isinstance(condition, Comparison):
        yield condition
    else:
        for term in condition.terms:
            yield from iter_comparisons(term)
