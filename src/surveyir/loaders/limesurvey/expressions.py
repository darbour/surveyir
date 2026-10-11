"""Parse LimeSurvey ExpressionScript into the IR, and lower it to comparisons.

The tokenizer and grammar mirror LimeSurvey's Expression Manager
(``em_core_helper.php``: ``RDP_Tokenize`` and the ``RDP_Evaluate*`` recursive
descent), so what parses here parses there:

    expressions := expression ("," expression)*
    expression  := WORD "=" or_expr | or_expr
    or_expr     := and_expr (("or" | "||") and_expr)*
    and_expr    := equality (("and" | "&&") equality)*
    equality    := relation (("==" | "!=" | "eq" | "ne") relation)*
    relation    := additive (("<" | "<=" | ">" | ">=" | "lt" | "le" | "gt" | "ge") additive)*
    additive    := multiplicative (("+" | "-") multiplicative)*
    multiplicative := unary (("*" | "/") unary)*
    unary       := ("+" | "-" | "!") primary | primary
    primary     := "(" expressions ")" | NUMBER | STRING | WORD "(" args ")" | WORD

Text is split into literal parts and ``{expression}`` parts as
``asSplitStringOnExpressions`` does: a brace followed (or a closing brace
preceded) by whitespace is literal, braces inside quotes are literal, and
nested braces belong to the enclosing expression.

``lower`` turns a parsed condition into ``And`` / ``Or`` / ``Not`` /
``Comparison`` nodes where that is exact under ExpressionScript's semantics,
keeping ``Expression`` leaves for the rest (see ``lower`` for the rules).
"""

from __future__ import annotations

import html
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field

from ...model import (
    And,
    Assign,
    Binary,
    Call,
    Comparison,
    Condition,
    Const,
    Expr,
    Expression,
    Not,
    Operand,
    Or,
    Sequence,
    Unary,
    Var,
    iter_nodes,
)
from ...model.text import Pipe, Text
from ...text import html_to_text

# --------------------------------------------------------------------------- tokens

_TOKENS: list[tuple[str, str]] = [
    ("DQ_STRING", r'(?<!\\)".*?(?<!\\)"'),
    ("SQ_STRING", r"(?<!\\)'.*?(?<!\\)'"),
    ("SPACE", r"\s+"),
    ("LP", r"\("),
    ("RP", r"\)"),
    ("COMMA", r","),
    ("AND_OR", r"\band\b|\bor\b|&&|\|\|"),
    ("COMPARE", r"<=|<|>=|>|==|!=|\ble\b|\blt\b|\bge\b|\bgt\b|\beq\b|\bne\b"),
    ("SGQA", r"(?:INSERTANS:)?[0-9]+X[0-9]+X[0-9]+[A-Z0-9_]*\#?[01]?(?:\.(?:[a-zA-Z0-9_]*))?"),
    (
        "WORD",
        r"(?:TOKEN:)?(?:[A-Z][A-Z0-9_]*)?(?:\.(?:[A-Z][A-Z0-9_]*))*(?:\.(?:[a-zA-Z0-9_]*))?",
    ),
    ("NUMBER", r"[0-9]+\.?[0-9]*|\.[0-9]+"),
    ("NOT", r"!"),
    ("OTHER", r"\+\+|--"),
    ("ASSIGN", r"="),
    ("BINARYOP", r"[+*/-]"),
]
_COMPILED = [(name, re.compile(rx, re.I | re.S)) for name, rx in _TOKENS]

#: functions LimeSurvey's Expression Manager defines (7.x)
LS_FUNCTIONS = frozenset(
    [
        "abs",
        "acos",
        "addslashes",
        "asin",
        "atan",
        "atan2",
        "ceil",
        "checkdate",
        "cos",
        "count",
        "countif",
        "countifop",
        "date",
        "exp",
        "fixnum",
        "floatval",
        "floor",
        "gmdate",
        "html_entity_decode",
        "htmlentities",
        "htmlspecialchars",
        "htmlspecialchars_decode",
        "idate",
        "if",
        "implode",
        "intval",
        "is_empty",
        "is_float",
        "is_int",
        "is_nan",
        "is_null",
        "is_numeric",
        "is_string",
        "join",
        "list",
        "listifop",
        "log",
        "ltrim",
        "max",
        "min",
        "mktime",
        "nl2br",
        "number_format",
        "pi",
        "pow",
        "quoted_printable_decode",
        "quoted_printable_encode",
        "quotemeta",
        "rand",
        "regexMatch",
        "round",
        "rtrim",
        "sin",
        "sprintf",
        "sqrt",
        "stddev",
        "str_pad",
        "str_repeat",
        "str_replace",
        "strcasecmp",
        "strcmp",
        "strip_tags",
        "stripos",
        "stripslashes",
        "stristr",
        "strlen",
        "strpos",
        "strrev",
        "strstr",
        "strtolower",
        "strtotime",
        "strtoupper",
        "substr",
        "sum",
        "sumifop",
        "tan",
        "convert_value",
        "time",
        "trim",
        "ucwords",
        "unique",
    ]
)
#: variable suffixes (dynamic and static attributes)
ATTRIBUTES = frozenset(["help"]) | frozenset(
    [
        "code",
        "NAOK",
        "relevanceStatus",
        "shown",
        "valueNAOK",
        "value",
        "qid",
        "gid",
        "question",
        "sgqa",
        "type",
        "relevance",
        "grelevance",
        "qseq",
        "gseq",
        "jsName",
        "jsName_on",
        "mandatory",
        "rowdivid",
    ]
)
#: survey-level values LimeSurvey defines (reserved names)
SURVEY_VALUES = frozenset(
    [
        "SEED",
        "STARTDATE",
        "SUBMITDATE",
        "DATESTAMP",
        "SID",
        "LANG",
        "SAVEDID",
        "SURVEYNAME",
        "SURVEYDESCRIPTION",
        "SURVEYURL",
        "SURVEYURLDESCRIPTION",
        "STARTTIME",
        "EXPIRY",
        "ADMINNAME",
        "ADMINEMAIL",
        "NUMBEROFQUESTIONS",
        "THEREAREXQUESTIONS",
        "TOTALQUESTIONS",
        "QID",
        "GID",
        "SGQ",
        "QUESTION",
        "QUESTION_CODE",
        "QUESTIONHELP",
        "ANSWERSCLEARED",
        "COMPLETED",
        "PERC",
        "TOKEN",
    ]
)


class ExpressionError(ValueError):
    pass


@dataclass(frozen=True)
class Token:
    text: str
    type: str
    pos: int


def tokenize(source: str) -> list[Token]:
    """``RDP_Tokenize``: tokens without spaces; quoted strings unquoted."""
    out: list[Token] = []
    i, n = 0, len(source)
    while i < n:
        for name, rx in _COMPILED:
            m = rx.match(source, i)
            if m and m.end() > i:
                text = m.group(0)
                if name in ("DQ_STRING", "SQ_STRING"):
                    text = text[1:-1].replace('\\"', '"').replace("\\'", "'").replace("\\\\", "\\")
                if name != "SPACE":
                    out.append(Token(text, name, i))
                i = m.end()
                break
        else:
            out.append(Token(source[i], "OTHER", i))
            i += 1
    return out


# --------------------------------------------------------------------------- parser

_EQ = {"==": "==", "eq": "==", "!=": "!=", "ne": "!="}
_REL = {"<": "<", "lt": "<", "<=": "<=", "le": "<=", ">": ">", "gt": ">", ">=": ">=", "ge": ">="}


class _Parser:
    def __init__(self, source: str) -> None:
        self.source = source
        self.tokens = tokenize(source)
        self.pos = 0
        for t in self.tokens:
            if t.type == "OTHER":
                raise ExpressionError(f"unsupported syntax {t.text!r} at {t.pos}")

    def peek(self) -> Token | None:
        return self.tokens[self.pos] if self.pos < len(self.tokens) else None

    def take(self) -> Token:
        t = self.peek()
        if t is None:
            raise ExpressionError("expression ends early")
        self.pos += 1
        return t

    def parse(self) -> Expr:
        if not self.tokens:
            raise ExpressionError("empty expression")
        out = self.expressions(top=True)
        if self.peek() is not None:
            raise ExpressionError(f"extra tokens at {self.peek().pos}")  # type: ignore[union-attr]
        return out

    def expressions(self, *, top: bool = False) -> Expr:
        items = [self.expression()]
        while (t := self.peek()) is not None and t.type == "COMMA":
            self.take()
            items.append(self.expression())
        if not top and (t := self.peek()) is not None and t.type not in ("RP",):
            raise ExpressionError(f"expected ')' at {t.pos}")
        return items[0] if len(items) == 1 else Sequence(items=items)

    def expression(self) -> Expr:
        t = self.peek()
        nxt = self.tokens[self.pos + 1] if self.pos + 1 < len(self.tokens) else None
        if t is not None and nxt is not None and nxt.type == "ASSIGN":
            if t.type not in ("WORD", "SGQA"):
                raise ExpressionError("only variables can be assigned values")
            self.pos += 2
            return Assign(target=Var(name=t.text), value=self.or_expr())
        return self.or_expr()

    def _binary(self, sub: Callable[[], Expr], ops: Mapping[str, str], types: set[str]) -> Expr:
        left = sub()
        while (t := self.peek()) is not None and t.type in types and t.text.lower() in ops:
            self.take()
            op = ops[t.text.lower()]
            left = Binary(op=op, source_op=t.text, left=left, right=sub())  # type: ignore[arg-type]
        return left

    def or_expr(self) -> Expr:
        return self._binary(self.and_expr, {"or": "or", "||": "or"}, {"AND_OR"})

    def and_expr(self) -> Expr:
        return self._binary(self.equality, {"and": "and", "&&": "and"}, {"AND_OR"})

    def equality(self) -> Expr:
        return self._binary(self.relation, _EQ, {"COMPARE"})

    def relation(self) -> Expr:
        return self._binary(self.additive, _REL, {"COMPARE"})

    def additive(self) -> Expr:
        return self._binary(self.multiplicative, {"+": "+", "-": "-"}, {"BINARYOP"})

    def multiplicative(self) -> Expr:
        return self._binary(self.unary, {"*": "*", "/": "/"}, {"BINARYOP"})

    def unary(self) -> Expr:
        t = self.peek()
        if t is not None and t.type in ("NOT", "BINARYOP") and t.text in ("+", "-", "!"):
            self.take()
            return Unary(op=t.text, arg=self.primary())  # type: ignore[arg-type]
        return self.primary()

    def primary(self) -> Expr:
        t = self.take()
        if t.type == "LP":
            inner = self.expressions()
            if self.take().type != "RP":
                raise ExpressionError("expected ')'")
            return inner
        if t.type == "NUMBER":
            return Const(value=t.text, quoted=False)
        if t.type in ("DQ_STRING", "SQ_STRING"):
            return Const(value=t.text, quoted=True)
        if t.type in ("WORD", "SGQA") and t.text:
            nxt = self.peek()
            if nxt is not None and nxt.type == "LP":
                self.take()
                args: list[Expr] = []
                if (p := self.peek()) is not None and p.type == "RP":
                    self.take()
                    return Call(fn=t.text, args=args)
                while True:
                    args.append(self.expression())
                    sep = self.take()
                    if sep.type == "RP":
                        return Call(fn=t.text, args=args)
                    if sep.type != "COMMA":
                        raise ExpressionError(f"expected ',' or ')' at {sep.pos}")
            return Var(name=t.text)
        raise ExpressionError(f"expected a constant or variable at {t.pos}, got {t.text!r}")


def parse(source: str) -> Expr:
    """Parse one expression (without braces). Raises ``ExpressionError``."""
    return _Parser(html.unescape(source)).parse()


# --------------------------------------------------------------------------- text


def split_text(src: str) -> list[tuple[str, bool]]:
    """``asSplitStringOnExpressions``: ``[(part, is_expression)]``; expression parts
    keep their braces."""
    if not src:
        return []
    if "{" not in src or src in ("{", "}"):
        return [(src, False)]
    parts = [p for p in re.split(r"""(\{|\}|'|"|\\)""", src) if p != ""]
    out: list[tuple[str, bool]] = []
    current: list[str] = []
    in_sq = in_dq = False
    depth = 0
    j = 0
    while j < len(parts):
        p = parts[j]
        if p == "{":
            if j < len(parts) - 1 and parts[j + 1][:1].isspace():
                current += ["{", parts[j + 1]]
                j += 1
            elif in_dq or in_sq:
                current.append("{")
            elif depth > 0:
                current.append("{")
                depth += 1
            else:
                if current:
                    out.append(("".join(current), False))
                depth, current = 1, ["{"]
        elif p == "}":
            if j > 0 and parts[j - 1][-1:].isspace() or depth == 0 or in_sq or in_dq:
                current.append("}")
            else:
                depth -= 1
                current.append("}")
                if depth == 0:
                    out.append(("".join(current), True))
                    current = []
        elif p == "'":
            current.append(p)
            if depth > 0 and not in_dq:
                in_sq = not in_sq
        elif p == '"':
            current.append(p)
            if depth > 0 and not in_sq:
                in_dq = not in_dq
        elif p == "\\":
            current.append(p)
            if j < len(parts) - 1:  # an escaped character is literal
                current.append(parts[j + 1])
                j += 1
        else:
            current.append(p)
        j += 1
    if current:
        out.append(("".join(current), False))
    return out


# --------------------------------------------------------------------------- resolution


@dataclass(frozen=True)
class Target:
    """What a variable name refers to."""

    question_id: str
    part: str | None = None


@dataclass
class Scope:
    """Everything variable names resolve against."""

    #: variable name (code or SGQA field name) -> question and part
    names: Mapping[str, Target]
    #: question id -> its variable names in field order, with their parts (self / that)
    variables: Mapping[str, list[tuple[str, str | None]]] = field(default_factory=dict)
    #: the question an expression belongs to (``self``, ``this``)
    owner: str | None = None
    #: names that are constants where the expression is (QID, GID, GROUPNAME, ...)
    statics: Mapping[str, str] = field(default_factory=dict)

    def at(self, owner: str | None, statics: Mapping[str, str] | None = None) -> Scope:
        return Scope(self.names, self.variables, owner, statics or {})


def split_name(name: str) -> tuple[str, str | None]:
    """``Q1_SQ001.NAOK`` -> (``Q1_SQ001``, ``NAOK``)."""
    base, dot, attr = name.rpartition(".")
    if dot and attr in ATTRIBUTES:
        return base, attr
    return name, None


_SELF = re.compile(r"\b(?:self|that)(?:\.[A-Za-z0-9_]+)*")


def expand_this(source: str, scope: Scope) -> str:
    """EM ``ExpandThisVar``: ``self`` / ``that.Q1`` (with ``.sq_X``, ``.nosq_X``,
    ``.comments``, ``.nocomments`` filters and an attribute) become the comma-separated
    list of the question's variables, before the expression is parsed."""
    out = []
    for tok in tokenize_spaced(source):
        text, kind = tok
        if kind in ("WORD", "SGQA") and _SELF.fullmatch(text):
            out.append(_expand(text, scope) or text)
        elif kind == "DQ_STRING":
            out.append(f'"{text}"')
        elif kind == "SQ_STRING":
            out.append(f"'{text}'")
        else:
            out.append(text)
    return "".join(out)


def _expand(name: str, scope: Scope) -> str | None:
    parts = name.split(".")
    if parts[0] == "self":
        qid = scope.owner
        parts = parts[1:]
    else:
        if len(parts) < 2:
            return None
        target = scope.names.get(parts[1])
        qid = (
            target.question_id
            if target is not None
            else (parts[1] if parts[1] in scope.variables else None)
        )
        parts = parts[2:]
    if qid is None or qid not in scope.variables:
        return None
    suffix = ""
    if parts and parts[-1] in ATTRIBUTES:
        suffix = "." + parts.pop()
    comments = ""
    sq: list[str] = []
    nosq: list[str] = []
    for part in parts:
        if part == "nocomments":
            comments = "N"
        elif part == "comments":
            comments = "Y"
        elif part.startswith("sq_") and len(part) > 3:
            sq.append(part[3:])
        elif part.startswith("nosq_") and len(part) > 5:
            nosq.append(part[5:])
        else:
            return None
    names = []
    for var, sub in scope.variables[qid]:
        ext = sub or ""
        if comments == "Y" and not ext.endswith("comment"):
            continue
        if comments == "N" and ext.endswith("comment"):
            continue
        if sq and not re.search("|".join(sq), ext):
            continue
        if nosq and re.search("|".join(nosq), ext):
            continue
        names.append(var + suffix)
    return ",".join(names) if names else None


def tokenize_spaced(source: str) -> list[tuple[str, str]]:
    """Every token, spaces included, as (text, type); strings unquoted."""
    out: list[tuple[str, str]] = []
    i, n = 0, len(source)
    while i < n:
        for name, rx in _COMPILED:
            m = rx.match(source, i)
            if m and m.end() > i:
                text = m.group(0)
                if name in ("DQ_STRING", "SQ_STRING"):
                    text = text[1:-1]
                out.append((text, name))
                i = m.end()
                break
        else:
            out.append((source[i], "OTHER"))
            i += 1
    return out


def compile_expression(source: str, scope: Scope) -> Expr:
    """Parse ExpressionScript (``self`` / ``that`` expanded) and resolve its names."""
    src = html.unescape(source)
    if "self" in src or "that" in src:
        src = expand_this(src, scope)
    return resolve(_Parser(src).parse(), scope)


def resolve(expr: Expr, scope: Scope | Mapping[str, Target], *, owner: str | None = None) -> Expr:
    """``expr`` with every variable resolved: question codes and SGQA field names
    through the scope's names, ``this`` to the owning question (its current
    subquestion, in per-subquestion validation), participant attributes
    (``TOKEN:ATTR``) and survey values."""
    if not isinstance(scope, Scope):
        scope = Scope(scope, owner=owner)
    names = scope.names
    owner = scope.owner

    def var(v: Var) -> Var | Const:
        name = v.name
        if name.upper().startswith("INSERTANS:"):
            name = name[len("INSERTANS:") :]
            if "." not in name:
                name += ".shown"
        base, attr = split_name(name)
        if base.upper().startswith("TOKEN:"):
            return Var(name=v.name, kind="participant", field=base[6:], attribute=attr)
        if base in ("this", "self", "that") and owner is not None:
            return Var(name=v.name, kind="self", question_id=owner, attribute=attr)
        target = names.get(base)
        if target is not None:
            return Var(
                name=v.name,
                kind="question",
                question_id=target.question_id,
                part=target.part,
                attribute=attr,
            )
        if base in scope.statics and attr is None:
            return Const(value=scope.statics[base], quoted=True)
        if base in SURVEY_VALUES:
            return Var(name=v.name, kind="survey", field=base, attribute=attr)
        return Var(name=v.name, kind="unknown", attribute=attr)

    def go(e: Expr) -> Expr:
        if isinstance(e, Var):
            return var(e)
        if isinstance(e, Unary):
            return e.model_copy(update={"arg": go(e.arg)})
        if isinstance(e, Binary):
            return e.model_copy(update={"left": go(e.left), "right": go(e.right)})
        if isinstance(e, Call):
            return e.model_copy(update={"args": [go(a) for a in e.args]})
        if isinstance(e, Assign):
            target = var(e.target)
            if not isinstance(target, Var):
                target = e.target
            return e.model_copy(update={"target": target, "value": go(e.value)})
        if isinstance(e, Sequence):
            return e.model_copy(update={"items": [go(a) for a in e.items]})
        return e

    return go(expr)


def unparse(e: Expr) -> str:
    """ExpressionScript source for ``e`` (fully parenthesized where it matters)."""
    if isinstance(e, Const):
        if not e.quoted:
            return e.value
        return '"' + e.value.replace("\\", "\\\\").replace('"', '\\"') + '"'
    if isinstance(e, Var):
        return e.name
    if isinstance(e, Unary):
        return f"{e.op}{_wrap(e.arg)}"
    if isinstance(e, Binary):
        return f"{_wrap(e.left)} {e.source_op} {_wrap(e.right)}"
    if isinstance(e, Call):
        return f"{e.fn}({', '.join(unparse(a) for a in e.args)})"
    if isinstance(e, Assign):
        return f"{e.target.name} = {unparse(e.value)}"
    if isinstance(e, Sequence):
        return ", ".join(unparse(a) for a in e.items)
    return "?"


def _wrap(e: Expr) -> str:
    s = unparse(e)
    return f"({s})" if isinstance(e, (Binary, Assign, Sequence)) else s


# --------------------------------------------------------------------------- lowering

#: question kinds, as the loader built them, for choosing comparison selectors
QuestionShape = Mapping[str, str]


def _exempt(v: Var) -> bool:
    from ...runtime.expressions import NAOK_ATTRIBUTES, STATIC_ATTRIBUTES

    return v.attribute in NAOK_ATTRIBUTES or v.attribute in STATIC_ATTRIBUTES


def lower(
    expr: Expr,
    source: str,
    shapes: QuestionShape,
    *,
    location: str | None = None,
) -> Condition:
    """A condition equivalent to the ExpressionScript ``expr`` (a relevance or
    validation equation).

    Leaves become ``Comparison`` nodes only where the result is the same under
    ExpressionScript's rules: equality with a constant, ``is_empty``, ``!``, and
    ``and`` / ``or`` of those, on a question's code or one of its parts. Anything
    else (arithmetic, other functions, ordering comparisons, whose results on empty
    values differ) stays an ``Expression`` leaf with its own source.

    ExpressionScript fails a whole expression that reads an irrelevant question
    without ``.NAOK``; that is kept by requiring each such question to be
    ``relevant`` (one comparison per question), except where a conjunct already
    implies it (an equality with a non-empty constant cannot hold for an irrelevant,
    hence unanswered, question).

    ``shapes`` maps question ids to ``kind`` or ``kind/mode`` (``choice/single``,
    ``choice/multiple``, ``matrix/single``, ``matrix/text``, ``text_entry/form``, ...).
    """
    undefined = any(
        (isinstance(n, Var) and n.kind == "unknown")
        or (isinstance(n, Call) and n.fn not in LS_FUNCTIONS)
        for n in iter_nodes(expr)
    )
    # LimeSurvey rejects an expression with an undefined name as a whole, so it stays
    # whole: no lowered branch can hold while the undefined part goes unevaluated
    lowered = None if undefined else _lower(expr, shapes, location)
    if lowered is None:
        return Expression(language="expression_script", source=source, tree=expr, location=location)
    guards: dict[str, Var] = {}
    for node in iter_nodes(expr):
        if (
            isinstance(node, Var)
            and node.kind == "question"
            and node.question_id
            and not _exempt(node)
        ):
            guards.setdefault(node.question_id, node)
    implied = _implied(lowered)
    terms: list[Condition] = [
        Comparison(
            left=Operand(kind="question", question_id=qid, raw=v.name),
            operator="relevant",
            source_operator="relevanceStatus",
            description=f"{v.name} is relevant",
        )
        for qid, v in guards.items()
        if qid not in implied
    ]
    if not terms:
        return lowered
    if isinstance(lowered, And):
        return And(terms=terms + list(lowered.terms))
    return And(terms=[*terms, lowered])


def _implied(c: Condition) -> set[str]:
    """Questions a condition can only hold for if they are answered."""
    if isinstance(c, Comparison):
        if c.left.kind == "question" and c.left.question_id:
            if c.operator == "selected" or c.operator == "not_empty":
                return {c.left.question_id}
            if (
                c.operator == "equal"
                and c.right not in (None, "")
                and c.left.selector != "SelectedAnswerCount"  # "0" encodes an empty row
            ):
                return {c.left.question_id}
        return set()
    if isinstance(c, And):
        out: set[str] = set()
        for t in c.terms:
            out |= _implied(t)
        return out
    return set()


def _question_var(e: Expr) -> Var | None:
    if (
        isinstance(e, Var)
        and e.kind == "question"
        and e.question_id
        and e.attribute in (None, "NAOK", "code")
    ):
        return e
    if isinstance(e, Var) and e.kind == "participant" and e.field and e.attribute is None:
        return e
    return None


def _operand(v: Var, shapes: QuestionShape, value: str | None) -> tuple[Operand, str] | None:
    """(operand, how) for comparing ``v``: how is 'value', 'choice' (selected test on a
    multiple-choice part) or 'cell' (selected test on an array row)."""
    if v.kind == "participant":  # always relevant; read like an embedded field
        return Operand(kind="embedded_data", name=f"TOKEN:{v.field}", raw=v.name), "value"
    shape = shapes.get(v.question_id or "", "")
    raw = v.name
    qid = v.question_id
    part = v.part
    if shape in ("choice/single", "text_entry/single", "equation", "text_entry/date"):
        if part is None:
            return Operand(kind="question", question_id=qid, raw=raw), "value"
        if part == "other" and shape == "choice/single":
            return (
                Operand(
                    kind="question",
                    question_id=qid,
                    selector="ChoiceTextEntryValue",
                    choice_id="-oth-",
                    raw=raw,
                ),
                "value",
            )
        return None
    if shape == "choice/multiple" and part is not None:
        if part == "other":
            return (
                Operand(
                    kind="question",
                    question_id=qid,
                    selector="ChoiceTextEntryValue",
                    choice_id="other",
                    raw=raw,
                ),
                "value",
            )
        if part.endswith("comment"):
            return None
        return Operand(kind="question", question_id=qid, choice_id=part, raw=raw), "choice"
    if shape == "matrix/single" and part is not None:
        return Operand(kind="question", question_id=qid, choice_id=part, raw=raw), "cell"
    if shape == "text_entry/form" and part is not None:
        return (
            Operand(
                kind="question",
                question_id=qid,
                selector="ChoiceTextEntryValue",
                choice_id=part,
                raw=raw,
            ),
            "value",
        )
    return None


def _cmp(v: Var, op: str, value: str, shapes: QuestionShape, source_op: str) -> Comparison | None:
    found = _operand(v, shapes, value)
    if found is None:
        return None
    operand, how = found
    if how == "value":
        return Comparison(
            left=operand,
            operator="equal" if op == "==" else "not_equal",
            source_operator=source_op,
            right=value,
        )
    if how == "choice":
        if value not in ("Y", ""):
            return None
        hit = (value == "Y") == (op == "==")
        return Comparison(
            left=operand, operator="selected" if hit else "not_selected", source_operator=source_op
        )
    if value == "":  # an array row is empty: no column selected in that row
        hit = op == "!="
        return Comparison(
            left=operand.model_copy(update={"selector": "SelectedAnswerCount"}),
            operator="not_equal" if hit else "equal",
            source_operator=source_op,
            right="0",
        )
    return Comparison(
        left=operand.model_copy(update={"answer_id": value}),
        operator="selected" if op == "==" else "not_selected",
        source_operator=source_op,
    )


def _empty(v: Var, shapes: QuestionShape, negate: bool) -> Comparison | None:
    return _cmp(v, "!=" if negate else "==", "", shapes, "!is_empty" if negate else "is_empty")


def _lower(e: Expr, shapes: QuestionShape, location: str | None) -> Condition | None:
    """Exact lowering of ``e``, or None if any part has no comparison equivalent."""
    if isinstance(e, Binary) and e.op in ("and", "or"):
        left = _lower_term(e.left, shapes, location)
        right = _lower_term(e.right, shapes, location)
        terms: list[Condition] = []
        for t in (left, right):
            if isinstance(t, And if e.op == "and" else Or):
                terms += t.terms
            else:
                terms.append(t)
        return And(terms=terms) if e.op == "and" else Or(terms=terms)
    if isinstance(e, Binary) and e.op in ("==", "!="):
        for a, b in ((e.left, e.right), (e.right, e.left)):
            v = _question_var(a)
            if v is not None and isinstance(b, Const) and (b.quoted or _plain_number(b.value)):
                return _cmp(v, e.op, b.value, shapes, e.source_op)
        return None
    if isinstance(e, Call) and e.fn == "is_empty" and len(e.args) == 1:
        v = _question_var(e.args[0])
        return _empty(v, shapes, negate=False) if v is not None else None
    if isinstance(e, Unary) and e.op == "!":
        if isinstance(e.arg, Call) and e.arg.fn == "is_empty" and len(e.arg.args) == 1:
            v = _question_var(e.arg.args[0])
            return _empty(v, shapes, negate=True) if v is not None else None
        inner = _lower(e.arg, shapes, location)
        return Not(term=inner) if inner is not None else None
    return None


def _lower_term(e: Expr, shapes: QuestionShape, location: str | None) -> Condition:
    """A conjunct or disjunct: lowered if possible, else its own ``Expression`` leaf."""
    out = _lower(e, shapes, location)
    if out is not None:
        return out
    return Expression(language="expression_script", source=unparse(e), tree=e, location=location)


def _plain_number(s: str) -> bool:
    """A number literal compares the same as a string here (``==`` is PHP's loose
    equality either way), unless it is written unusually (``1.50``)."""
    return re.fullmatch(r"-?(0|[1-9][0-9]*)", s) is not None


# --------------------------------------------------------------------------- text


def make_text(
    source: str | None,
    scope: Scope,
    *,
    on_error: Callable[[str, str], None] | None = None,
) -> Text:
    """A ``Text`` (``expression_script`` syntax) with every ``{expression}`` that parses
    as a pipe; ``{...}`` that does not parse is literal text, as LimeSurvey shows it."""
    markup = source or ""
    plain = html_to_text(markup)
    pipes: list[Pipe] = []
    seen: set[str] = set()
    for text in (markup, plain):
        for part, is_expr in split_text(text):
            if not is_expr or part in seen:
                continue
            seen.add(part)
            inner = part[1:-1]
            try:
                tree = compile_expression(inner, scope)
            except ExpressionError as e:
                if on_error is not None and _looks_like_expression(inner):
                    on_error(part, str(e))
                continue
            pipes.append(
                Pipe(
                    raw=part,
                    kind="expression",
                    scheme="em",
                    path=html.unescape(inner),
                    expression=tree,
                    question_id=_single_question(tree),
                )
            )
    return Text(html=markup, plain=plain, pipes=pipes, syntax="expression_script")


def _single_question(tree: Expr) -> str | None:
    qids = {
        n.question_id
        for n in iter_nodes(tree)
        if isinstance(n, Var) and n.kind == "question" and n.question_id
    }
    return next(iter(qids)) if len(qids) == 1 else None


def _looks_like_expression(inner: str) -> bool:
    """Braces around CSS, JavaScript or JSON are not worth a diagnostic."""
    return not re.search(r"[;:]\s|\bfunction\b|\bvar\b|^\s*$", inner)
