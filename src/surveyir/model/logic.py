"""Boolean conditions used by display logic, branches, skip logic and validation.

Every platform-specific logic format is normalized into one small expression
tree: ``And`` / ``Or`` / ``Not`` nodes over ``Comparison`` leaves. Precedence has
already been resolved by the loader (Qualtrics evaluates AND before OR), so
consumers evaluate the tree as written.

Logic a ``Comparison`` cannot express (arithmetic, function calls, string
handling: LimeSurvey's ExpressionScript, for example) is an ``Expression`` leaf:
the source text, the language it is written in, and its parsed syntax tree
(``Expr``). Loaders lower whatever they can to ``Comparison`` leaves, so a
consumer that reads comparisons reads most conditions; ``Expression`` leaves are
for the rest.
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
    "relevant",  # the question was not hidden by its own (or its group's) logic
    "not_relevant",
    "other",
]


class Operand(IRModel):
    """The left-hand side of a comparison: what the condition looks at."""

    kind: OperandKind
    question_id: str | None = None
    selector: str | None = Field(
        default=None,
        description=(
            "Which part of the question response, in the source platform's terms: "
            "Qualtrics 'SelectableChoice', 'ChoiceTextEntryValue', "
            "'ChoiceNumericEntryValue', 'SelectableAnswer', 'Displayed', ...; loaders for "
            "other platforms use the same names for the same parts."
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


class Not(IRModel):
    type: Literal["not"] = "not"
    term: Condition


# --------------------------------------------------------------------------- expressions

VarKind = Literal[
    "question",  # a question's answer, or one part of it (a subquestion, 'other' text, ...)
    "participant",  # a participant (token) attribute, e.g. TOKEN:FIRSTNAME
    "survey",  # a survey-level value, e.g. SID or LANG
    "self",  # the question the expression belongs to (LimeSurvey 'self' / 'that')
    "unknown",
]


class Var(IRModel):
    """A variable reference, resolved by the loader where it can be."""

    node: Literal["var"] = "var"
    name: str = Field(description="As written, e.g. 'Q1_SQ001.NAOK' or 'TOKEN:FIRSTNAME'.")
    kind: VarKind = "unknown"
    question_id: str | None = None
    part: str | None = Field(
        default=None,
        description="Which part of the question's answer: a subquestion code ('SQ001'), "
        "'SQ001_SQ002' (text and number arrays), 'SQ001#1' (dual scale), 'other', "
        "'comment', 'SQ001comment', or a rank ('1').",
    )
    attribute: str | None = Field(
        default=None,
        description="Suffix after the dot: 'NAOK', 'shown', 'code', 'value', "
        "'relevanceStatus', 'qid', 'question', ...",
    )
    field: str | None = Field(
        default=None, description="Participant attribute or survey value name, e.g. 'FIRSTNAME'."
    )


class Const(IRModel):
    node: Literal["const"] = "const"
    value: str = Field(description="The literal as text: numbers stay as written.")
    quoted: bool = Field(description="True for a string literal, false for a number.")


class Unary(IRModel):
    node: Literal["unary"] = "unary"
    op: Literal["!", "-", "+"]
    arg: Expr


class Binary(IRModel):
    node: Literal["binary"] = "binary"
    op: Literal["or", "and", "==", "!=", "<", "<=", ">", ">=", "+", "-", "*", "/"]
    source_op: str = Field(description="As written: 'eq', '&&', ...")
    left: Expr
    right: Expr


class Call(IRModel):
    node: Literal["call"] = "call"
    fn: str
    args: list[Expr] = Field(default_factory=list)


class Assign(IRModel):
    """``target = value``: sets a variable (LimeSurvey equations can)."""

    node: Literal["assign"] = "assign"
    target: Var
    value: Expr


class Sequence(IRModel):
    """Comma-separated expressions; the value is the last one's."""

    node: Literal["sequence"] = "sequence"
    items: list[Expr]


Expr = Annotated[
    Var | Const | Unary | Binary | Call | Assign | Sequence, Field(discriminator="node")
]


class Expression(Extensible):
    """A condition written in an expression language, kept whole.

    ``tree`` is None when the source could not be parsed (a loader diagnostic says
    why); the runtime then treats the condition as false and records it.
    """

    type: Literal["expression"] = "expression"
    language: Literal["expression_script"] = Field(
        description="'expression_script' is LimeSurvey's ExpressionScript (Expression Manager)."
    )
    source: str = Field(description="The expression exactly as written in the source.")
    tree: Expr | None = None
    location: str | None = Field(
        default=None,
        description="Where it belongs (a question or group id): where the runtime reports "
        "what it could not evaluate.",
    )


Condition = Annotated[Comparison | And | Or | Not | Expression, Field(discriminator="type")]

for _model in (And, Or, Not, Unary, Binary, Call, Assign, Sequence, Expression):
    _model.model_rebuild()


def iter_comparisons(condition: Condition):
    """Yield every ``Comparison`` leaf in ``condition``, depth first."""
    if isinstance(condition, Comparison):
        yield condition
    elif isinstance(condition, Not):
        yield from iter_comparisons(condition.term)
    elif isinstance(condition, (And, Or)):
        for term in condition.terms:
            yield from iter_comparisons(term)


def iter_expressions(condition: Condition):
    """Yield every ``Expression`` leaf in ``condition``, depth first."""
    if isinstance(condition, Expression):
        yield condition
    elif isinstance(condition, Not):
        yield from iter_expressions(condition.term)
    elif isinstance(condition, (And, Or)):
        for term in condition.terms:
            yield from iter_expressions(term)


def iter_nodes(expr: Expr | None):
    """Yield every node of an expression tree, depth first."""
    if expr is None:
        return
    yield expr
    if isinstance(expr, Unary):
        yield from iter_nodes(expr.arg)
    elif isinstance(expr, Binary):
        yield from iter_nodes(expr.left)
        yield from iter_nodes(expr.right)
    elif isinstance(expr, Call):
        for a in expr.args:
            yield from iter_nodes(a)
    elif isinstance(expr, Assign):
        yield expr.target
        yield from iter_nodes(expr.value)
    elif isinstance(expr, Sequence):
        for a in expr.items:
            yield from iter_nodes(a)
