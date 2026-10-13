"""Evaluate LimeSurvey ExpressionScript (``Expression`` leaves and ``{...}`` pipes).

The semantics follow LimeSurvey's Expression Manager (``em_core_helper.php`` and
``em_manager_helper.php``, 7.x), which evaluates on the server in PHP:

* Values are strings unless computed; a question's value is its answer code
  (``Q1``), one subquestion's (``Q1_SQ001``: ``Y`` or empty for multiple choice),
  or its text (free text, numbers). ``.shown`` is the answer as displayed,
  ``.value`` its assessment value, ``.NAOK`` the value even if the question is
  irrelevant.
* A variable whose question is irrelevant (hidden by its own or its group's
  relevance) has no value, and any operation on it fails, so the whole expression
  fails: a relevance equation is then false, and piped text renders empty. Only
  ``.NAOK``, ``.valueNAOK``, ``.shown``, ``.relevanceStatus`` and static
  attributes (``.qid``, ``.question``, ...) are exempt.
* Comparisons follow PHP: ``==`` is PHP's loose equality (numeric strings compare
  as numbers); ``<`` and friends compare numbers when both sides are numeric,
  strings when both are strings, and are false on a mix (so ``"" < 3`` is false).
  A quoted literal forces a string comparison. ``+`` adds numbers and
  concatenates anything else; other arithmetic on non-numbers is NaN.

What the runtime cannot reproduce is recorded as an approximation:
``expression.parse_error`` (the source did not parse), ``expression.unknown_function``,
``expression.unknown_variable`` and ``expression.clock`` (the current date or time
was read). In a strict run these stop the walk, like every other gap.
"""

from __future__ import annotations

import html as _html
import math
import random
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Final

from ..model import (
    Assign,
    Binary,
    Call,
    Const,
    Expr,
    Expression,
    Question,
    Survey,
    Unary,
    Var,
    iter_nodes,
)
from ..model import (
    Sequence as Seq,
)
from ..model.question import (
    ChoiceQuestion,
    EquationQuestion,
    MatrixQuestion,
    RankOrderQuestion,
    SideBySideQuestion,
    TextEntryQuestion,
)
from .state import RespondentState
from .trace import Affects

#: attributes that may be read on an irrelevant question (EM ProcessBooleanExpression)
NAOK_ATTRIBUTES: Final = frozenset({"NAOK", "valueNAOK", "shown", "relevanceStatus"})
#: attributes that never change on a page (EM aRDP_regexpStaticAttribute)
STATIC_ATTRIBUTES: Final = frozenset(
    {
        "help",
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
    }
)
#: survey-level values the runtime knows
SURVEY_VALUES: Final = frozenset(
    {"SID", "LANG", "SURVEYNAME", "STARTDATE", "SUBMITDATE", "DATESTAMP", "STARTTIME"}
)
#: survey values that read the clock
CLOCK_VALUES: Final = frozenset({"STARTDATE", "SUBMITDATE", "DATESTAMP", "STARTTIME"})
#: functions that read the clock
CLOCK_FUNCTIONS: Final = frozenset({"date", "gmdate", "idate", "mktime", "time", "strtotime"})
#: question types whose values are numbers (EM 'onlynum')
NUMERIC_TYPES: Final = frozenset({"N", "K"})

#: an EM value: (value, type), type one of NUMBER / WORD / STRING (a quoted literal)
Value = tuple[Any, str]


class _Fail(Exception):
    """The expression cannot be evaluated (EM would report an error): it is false."""


@dataclass
class Context:
    state: RespondentState
    survey: Survey | None
    #: (code, detail) -> None, with location and affects bound by the caller
    record: Callable[[str, str], None]
    rng: random.Random | None = None
    #: the subquestion ``self`` means, inside per-item validation
    item: str | None = None
    #: variables read, as (name, question id, attribute), for the relevance check
    used: list[Var] = field(default_factory=list)


# --------------------------------------------------------------------------- PHP values


def php_truthy(v: Any) -> bool:
    if v is None or v is False:
        return False
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return v != 0
    return v not in ("", "0")


_NUMERIC = re.compile(r"^\s*[+-]?(\d+(\.\d*)?|\.\d+)([eE][+-]?\d+)?\s*$")
_LEADING = re.compile(r"^\s*[+-]?(\d+(\.\d*)?|\.\d+)([eE][+-]?\d+)?")


def is_numeric(v: Any) -> bool:
    """PHP ``is_numeric``."""
    if isinstance(v, bool) or v is None:
        return False
    if isinstance(v, (int, float)):
        return True
    return bool(_NUMERIC.match(str(v)))


def floatval(v: Any) -> float:
    """PHP ``floatval``: the leading number of a string, else 0."""
    if v is None or v is False:
        return 0.0
    if v is True:
        return 1.0
    if isinstance(v, (int, float)):
        return float(v)
    m = _LEADING.match(str(v))
    return float(m.group(0)) if m else 0.0


def intval(v: Any) -> int:
    f = floatval(v)
    if math.isnan(f) or math.isinf(f):
        return 0
    return int(f)


def strval(v: Any) -> str:
    """PHP ``strval`` (PHP 8 float formatting: shortest round trip, ``1.0E+25``)."""
    if v is None or v is False:
        return ""
    if v is True:
        return "1"
    if isinstance(v, float):
        if math.isnan(v):
            return "NAN"
        if math.isinf(v):
            return "INF" if v > 0 else "-INF"
        if v.is_integer() and abs(v) < 1e15:
            return str(int(v)) if v != 0 or math.copysign(1, v) > 0 else "-0"
        r = repr(v)
        if "e" in r:
            mant, exp = r.split("e")
            if "." not in mant:
                mant += ".0"
            sign = "-" if exp.startswith("-") else "+"
            return f"{mant}E{sign}{exp.lstrip('+-').lstrip('0') or '0'}"
        return r
    return str(v)


def _number(v: Any) -> int | float:
    if isinstance(v, bool):
        return int(v)
    if isinstance(v, (int, float)):
        return v
    f = floatval(v)
    return int(f) if f.is_integer() and abs(f) < 2**63 and "." not in str(v) else f


def loose_eq(a: Any, b: Any) -> bool:
    """PHP 8 ``==``."""
    if isinstance(a, bool) or isinstance(b, bool) or a is None or b is None:
        if a is None and b is None:
            return True
        if a is None or b is None:
            other = b if a is None else a
            if isinstance(other, str):
                return other == ""
            return not php_truthy(other)
        return php_truthy(a) == php_truthy(b)
    a_num = isinstance(a, (int, float))
    b_num = isinstance(b, (int, float))
    if a_num and b_num:
        return float(a) == float(b)
    if a_num or b_num:
        s = b if a_num else a
        n = a if a_num else b
        if is_numeric(s):
            return float(n) == floatval(s)
        return strval(n) == s
    if is_numeric(a) and is_numeric(b):
        return floatval(a) == floatval(b)
    return a == b


def _loose_cmp(a: Any, b: Any) -> int:
    """PHP 8 ``<=>`` for the values EM compares."""
    if isinstance(a, bool) or isinstance(b, bool):
        x, y = php_truthy(a), php_truthy(b)
        return (x > y) - (x < y)
    a_num = isinstance(a, (int, float)) or is_numeric(a)
    b_num = isinstance(b, (int, float)) or is_numeric(b)
    if a_num and b_num:
        x, y = floatval(a), floatval(b)
        return (x > y) - (x < y)
    x, y = strval(a), strval(b)
    return (x > y) - (x < y)


def _mismatch(arg1: Value, arg2: Value) -> tuple[bool, bool, bool]:
    """EM ``getMismatchInformation``: (mismatch, both numeric, both string)."""

    def norm(arg: Value) -> Any:
        v, t = arg
        s = strval(v) if not isinstance(v, str) else v
        if t == "NUMBER" and "." in s[1:] if s else False:
            return s.rstrip("0").rstrip(".")
        return v

    def numeric(v: Any) -> bool:
        if v == "" and isinstance(v, str):
            return False
        return (not php_truthy(v)) or strval(floatval(v)) == strval(v)

    v1, v2 = norm(arg1), norm(arg2)
    n1, n2 = numeric(v1), numeric(v2)
    s1, s2 = (not php_truthy(v1)) or not n1, (not php_truthy(v2)) or not n2
    both_num, both_str = n1 and n2, s1 and s2
    return (not both_num and not both_str), both_num, both_str


def _strcmp(a: Any, b: Any) -> int:
    x, y = strval(a), strval(b)
    return (x > y) - (x < y)


# --------------------------------------------------------------------------- answers


def _choice_text(items: Sequence[Any], cid: Any) -> str:
    for c in items:
        if c.id == str(cid):
            return c.text.plain
    return "" if cid is None else str(cid)


def _assessment(items: Sequence[Any], cid: Any) -> Any:
    for c in items:
        if c.id == str(cid):
            return c.extras.get("assessment_value", c.id)
    return cid


def question_value(q: Question, state: RespondentState, part: str | None, attr: str | None) -> Any:
    """The value of ``q`` (or one part of it) as ExpressionScript reads it."""
    answer = state.answer(q.id)
    value = answer.value if answer else None
    texts = answer.text if answer else {}
    shown = attr == "shown"
    if isinstance(q, EquationQuestion):
        return "" if value is None else value
    if isinstance(q, ChoiceQuestion):
        if q.multiple:
            selected = [str(v) for v in value] if isinstance(value, list) else []
            if part is None:
                return ""
            if part == "other":
                return texts.get("other", "")
            if part.endswith("comment") and part[:-7] in {c.id for c in q.choices}:
                return texts.get(part[:-7], "")
            hit = part in selected
            if shown:
                return _choice_text(q.choices, part) if hit else ""
            return "Y" if hit else ""
        if part == "other":
            return texts.get("-oth-", "")
        if part == "comment":
            return texts.get("comment", "")
        if value is None:
            return ""
        if shown:
            return texts.get("-oth-", "") if value == "-oth-" else _choice_text(q.choices, value)
        if attr in ("value", "valueNAOK"):
            return _assessment(q.choices, value)
        return str(value)
    if isinstance(q, MatrixQuestion):
        if not isinstance(value, dict) or part is None:
            return ""
        if q.mode == "text":
            row, _, col = part.partition("_")
            cells = value.get(row)
            return cells.get(col, "") if isinstance(cells, dict) else ""
        cell = value.get(part)
        if cell is None:
            return ""
        if shown:
            return _choice_text(q.columns, cell)
        if attr in ("value", "valueNAOK"):
            return _assessment(q.columns, cell)
        return str(cell)
    if isinstance(q, SideBySideQuestion):
        if not isinstance(value, dict) or part is None:
            return ""
        row, _, scale = part.partition("#")
        sub = next((s for s in q.questions if s.id == f"{q.id}#{scale or '0'}"), None)
        cells = value.get(f"{q.id}#{scale or '0'}")
        cell = cells.get(row) if isinstance(cells, dict) else None
        if cell is None:
            return ""
        if shown and isinstance(sub, MatrixQuestion):
            return _choice_text(sub.columns, cell)
        return str(cell)
    if isinstance(q, RankOrderQuestion):
        if not isinstance(value, dict) or part is None:
            return ""
        item = next((i for i, r in value.items() if str(r) == part), None)
        if item is None:
            return ""
        return _choice_text(q.items, item) if shown else str(item)
    if isinstance(q, TextEntryQuestion):
        if q.mode == "form":
            if not isinstance(value, dict) or part is None:
                return ""
            v = value.get(part)
            return "" if v is None else v
        if isinstance(value, dict):
            return value.get(part or "", "")
        return "" if value is None else value
    if part == "filecount":
        return 1 if value else 0
    if value is None or isinstance(value, (dict, list)):
        return ""
    return value


def item_ids(q: Question) -> list[str]:
    """Subquestion codes ``self`` expands to (empty: the question itself)."""
    for attr in ("choices", "rows", "items", "fields"):
        items = getattr(q, attr, None)
        if items and not (isinstance(q, ChoiceQuestion) and not q.multiple):
            return [c.id for c in items]
    return []


def _onlynum(q: Question) -> bool:
    if q.origin.format != "limesurvey":
        return False
    if q.origin.type in NUMERIC_TYPES:
        return True
    attrs = q.extras.get("attributes") or {}
    return q.origin.type == ":" and attrs.get("multiflexible_checkbox") not in ("1", 1)


# --------------------------------------------------------------------------- evaluation


def _relevant(ctx: Context, qid: str) -> bool:
    return ctx.state.is_relevant(qid)


def _var(v: Var, ctx: Context) -> Value:
    """A variable's value; raises ``_Fail`` if it is irrelevant (and not exempt)."""
    attr = v.attribute
    if v.kind == "participant":
        return ctx.state.read_embedded(f"TOKEN:{v.field}", "routing"), "WORD"
    if v.kind == "survey":
        return _survey_value(v, ctx), "WORD"
    qid = v.question_id
    part = v.part
    if v.kind == "self" and ctx.item is not None:
        part = ctx.item
    if part == "*":
        part = None
    q = ctx.survey.questions.get(qid) if (ctx.survey is not None and qid) else None
    if q is None:  # LimeSurvey: an undefined variable is an error, so the expression fails
        ctx.record(
            "expression.unknown_variable",
            f"variable {v.name!r} does not name a question the runtime knows; the expression fails",
        )
        raise _Fail(f"unknown variable {v.name}")
    ctx.used.append(v)
    if attr in STATIC_ATTRIBUTES:
        return _static(q, part, attr, ctx), "WORD"
    if attr == "relevanceStatus":
        return (1 if _relevant(ctx, q.id) else 0), "NUMBER"
    if attr not in NAOK_ATTRIBUTES and not _relevant(ctx, q.id):
        raise _Fail(f"{v.name} is irrelevant")
    if attr in ("NAOK", "valueNAOK", "shown") and not _relevant(ctx, q.id):
        return "", "WORD"
    value = question_value(q, ctx.state, part, attr)
    return value, ("NUMBER" if _onlynum(q) and attr != "shown" else "WORD")


def _static(q: Question, part: str | None, attr: str, ctx: Context) -> Any:
    info = q.extras.get("limesurvey") or {}
    if attr == "qid":
        return info.get("qid", q.id)
    if attr == "gid":
        return info.get("gid", "")
    if attr == "type":
        return q.origin.type
    if attr == "question":
        if part:
            for a in ("choices", "rows", "items", "fields"):
                for c in getattr(q, a, None) or []:
                    if c.id == part:
                        return c.text.plain
        return q.text.plain
    if attr == "mandatory":
        v = q.validation.required if q.validation else "off"
        return {"force": "Y", "request": "S"}.get(v, "N")
    if attr == "sgqa":
        return info.get("sgqa", "")
    if attr in ("qseq", "gseq"):
        return info.get(attr, "")
    if attr == "relevance":
        return info.get("relevance", "1")
    if attr == "grelevance":
        return info.get("grelevance", "1")
    if attr == "help":
        return info.get("help", "")
    if attr in ("jsName", "jsName_on"):  # the page element's name: static, never compared
        return f"java{info.get('sgqa', q.id)}{part or ''}"
    if attr == "rowdivid":
        return f"javatbd{info.get('sgqa', q.id)}{part or ''}"
    ctx.record("expression.unknown_variable", f"attribute .{attr} is not computed; read as empty")
    return ""


def _survey_value(v: Var, ctx: Context) -> Any:
    name = v.field or v.name
    survey = ctx.survey
    if name == "SID" and survey is not None:
        return survey.id
    if name == "LANG":
        return ctx.state.embedded.get("LANG") or (survey.language if survey else "") or ""
    if name == "SURVEYNAME" and survey is not None:
        return survey.name
    if name in CLOCK_VALUES:
        ctx.record("expression.clock", f"{name} reads the current date and time")
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    ctx.record(
        "expression.unknown_variable",
        f"survey value {name!r} is not available to the simulator; the expression fails",
    )
    raise _Fail(f"unknown survey value {name}")


def _args(args: Sequence[Expr], ctx: Context) -> list[Any]:
    out: list[Any] = []
    for a in args:
        if isinstance(a, Var) and (
            (a.kind == "self" and ctx.item is None and a.part is None) or a.part == "*"
        ):
            q = ctx.survey.questions.get(a.question_id or "") if ctx.survey else None
            items = item_ids(q) if q is not None else []
            if items:  # EM ExpandThisVar: self -> one variable per subquestion
                for code in items:
                    sub = a.model_copy(update={"part": code, "kind": "question"})
                    out.append(_eval(sub, ctx)[0])
                continue
            if a.part == "*":
                a = a.model_copy(update={"part": None})
        out.append(_eval(a, ctx)[0])
    return out


def _eval(e: Expr, ctx: Context) -> Value:
    if isinstance(e, Const):
        if e.quoted:
            return e.value, "STRING"
        return e.value, "NUMBER"
    if isinstance(e, Var):
        return _var(e, ctx)
    if isinstance(e, Unary):
        v, _ = _eval(e.arg, ctx)
        if v is None:
            raise _Fail("unary operand")
        if isinstance(v, str) and v == "":
            v = 0
        if e.op == "!":
            return (not php_truthy(v)), "NUMBER"
        if not is_numeric(v) and not isinstance(v, (int, float)):
            return math.nan, "NUMBER"
        n = _number(v)
        return (-n if e.op == "-" else n), "NUMBER"
    if isinstance(e, Binary):
        return _binary(e, ctx)
    if isinstance(e, Call):
        return _call(e, ctx)
    if isinstance(e, Seq):
        out: Value = ("", "WORD")
        for item in e.items:
            out = _eval(item, ctx)
        return out
    if isinstance(e, Assign):
        value, t = _eval(e.value, ctx)
        target = e.target
        if target.kind == "question" and target.question_id and ctx.survey is not None:
            from .state import Answer

            ctx.state.answers[ctx.state.key(target.question_id)] = Answer(value=strval(value))
        else:
            ctx.record(
                "expression.unknown_variable",
                f"assignment to {target.name!r}, which the runtime cannot set; ignored",
            )
        return value, t
    raise _Fail(f"unknown node {e!r}")


def _binary(e: Binary, ctx: Context) -> Value:
    op = e.op
    a1 = _eval(e.left, ctx)
    a2 = _eval(e.right, ctx)
    if a1[0] is None or a2[0] is None:
        raise _Fail("invalid value on the stack")
    mismatch, both_num, both_str = _mismatch(a1, a2)
    forced = "STRING" in (a1[1], a2[1])
    x, y = a1[0], a2[0]
    if forced and both_num:
        both_num, both_str, mismatch = False, True, False
        x, y = strval(x), strval(y)
    if op == "or":
        return (php_truthy(x) or php_truthy(y)), "NUMBER"
    if op == "and":
        return (php_truthy(x) and php_truthy(y)), "NUMBER"
    if op == "==":
        return loose_eq(x, y), "NUMBER"
    if op == "!=":
        return (not loose_eq(x, y)), "NUMBER"
    if op in ("<", "<=", ">", ">="):
        if mismatch:
            return False, "NUMBER"
        if op in ("<=", ">") and (
            (loose_eq(x, "0") and loose_eq(y, "")) or (loose_eq(x, "") and loose_eq(y, "0"))
        ):
            return op == "<=", "NUMBER"
        c = _strcmp(x, y) if (not both_num and both_str) else _loose_cmp(x, y)
        return {"<": c < 0, "<=": c <= 0, ">": c > 0, ">=": c >= 0}[op], "NUMBER"
    if op == "+":
        if both_num:
            return _number(x) + _number(y), "NUMBER"
        return strval(x) + strval(y), "STRING"
    if not both_num:
        return math.nan, "NUMBER"
    a, b = _number(x), _number(y)
    if op == "-":
        return a - b, "NUMBER"
    if op == "*":
        return float(a) * float(b), "NUMBER"
    if b == 0:
        return math.nan, "NUMBER"
    r = a / b
    return (int(r) if isinstance(a, int) and isinstance(b, int) and r.is_integer() else r), "NUMBER"


def _php_regex(pattern: str) -> re.Pattern[str] | None:
    """A PHP (PCRE) pattern with delimiters, e.g. ``/^[0-9]+$/i``, compiled."""
    if len(pattern) < 2:
        return None
    delim = pattern[0]
    close = {"(": ")", "[": "]", "{": "}", "<": ">"}.get(delim, delim)
    end = pattern.rfind(close)
    if end <= 0:
        return None
    body, flags = pattern[1:end], pattern[end + 1 :]
    f = 0
    for ch in flags:
        f |= {"i": re.I, "m": re.M, "s": re.S, "x": re.X, "u": 0}.get(ch, 0)
    body = re.sub(r"\\h", r"[ \\t]", body)
    try:
        return re.compile(body, f)
    except re.error:
        return None


def _date(fmt: str, ts: float | None = None) -> str:
    d = datetime.fromtimestamp(ts) if ts is not None else datetime.now()
    table = {
        "d": f"{d.day:02d}",
        "j": str(d.day),
        "D": d.strftime("%a"),
        "l": d.strftime("%A"),
        "N": str(d.isoweekday()),
        "w": str(d.isoweekday() % 7),
        "z": str(d.timetuple().tm_yday - 1),
        "m": f"{d.month:02d}",
        "n": str(d.month),
        "M": d.strftime("%b"),
        "F": d.strftime("%B"),
        "Y": str(d.year),
        "y": f"{d.year % 100:02d}",
        "H": f"{d.hour:02d}",
        "G": str(d.hour),
        "h": f"{(d.hour % 12) or 12:02d}",
        "g": str((d.hour % 12) or 12),
        "i": f"{d.minute:02d}",
        "s": f"{d.second:02d}",
        "a": "am" if d.hour < 12 else "pm",
        "A": "AM" if d.hour < 12 else "PM",
        "U": str(int(d.timestamp())),
    }
    out, escape = [], False
    for ch in fmt:
        if escape:
            out.append(ch)
            escape = False
        elif ch == "\\":
            escape = True
        else:
            out.append(table.get(ch, ch))
    return "".join(out)


def _ifop(op: str, a: Any, b: Any) -> bool:
    if op in ("==", "eq"):
        return loose_eq(a, b)
    if op in ("!=", "ne"):
        return not loose_eq(a, b)
    if op == "RX":
        rx = _php_regex(str(b))
        return bool(rx and rx.search(strval(a)))
    c = _loose_cmp(a, b)
    return {
        ">=": c >= 0,
        "ge": c >= 0,
        ">": c > 0,
        "gt": c > 0,
        "<=": c <= 0,
        "le": c <= 0,
        "<": c < 0,
        "lt": c < 0,
    }.get(op, False)


def _minmax(args: list[Any], pick: Callable[..., Any]) -> Any:
    nums = [floatval(a) for a in args if is_numeric(a)]
    if not nums:
        return ""
    r = pick(nums)
    return int(r) if r.is_integer() else r


def _round(v: Any, precision: Any = 0) -> Any:
    p = intval(precision)
    x = floatval(v)
    q = 10**p
    r = math.floor(abs(x) * q + 0.5) / q
    r = math.copysign(r, x)
    return int(r) if p <= 0 and r.is_integer() else r


def _call(e: Call, ctx: Context) -> Value:
    fn = e.fn
    if fn == "if":  # arguments are all evaluated, as EM does
        args = _args(e.args, ctx)
        if any(a is None for a in args):
            raise _Fail("null argument")
        test, yes = args[0], args[1] if len(args) > 1 else ""
        no = args[2] if len(args) > 2 else ""
        return (yes if php_truthy(test) else no), "WORD"
    impl = _FUNCTIONS.get(fn)
    if impl is None and fn not in CLOCK_FUNCTIONS and fn != "rand":
        ctx.record(
            "expression.unknown_function",
            f"function {fn}() is not implemented by the simulator; the expression is false",
        )
        raise _Fail(f"unknown function {fn}")
    args = _args(e.args, ctx)
    if any(a is None for a in args):
        raise _Fail("null argument")
    if fn in CLOCK_FUNCTIONS:
        ctx.record(
            "expression.clock",
            f"{fn}() reads the current date or time; evaluated at simulation time",
        )
        if fn == "time":
            return int(datetime.now().timestamp()), "NUMBER"
        if fn in ("date", "gmdate"):
            ts = floatval(args[1]) if len(args) > 1 else None
            return _date(strval(args[0]), ts), "WORD"
        if fn == "idate":
            return intval(_date(strval(args[0]))), "NUMBER"
        if fn == "strtotime":
            try:
                return int(datetime.fromisoformat(strval(args[0])).timestamp()), "NUMBER"
            except ValueError:
                return False, "NUMBER"
        now = datetime.now()
        parts = [intval(a) for a in args] + [
            now.hour,
            now.minute,
            now.second,
            now.month,
            now.day,
            now.year,
        ][len(args) :]
        h, mi, s, mo, d, y = parts[:6]
        try:
            return int(datetime(y, mo, d, h, mi, s).timestamp()), "NUMBER"
        except ValueError:
            return False, "NUMBER"
    if fn == "rand":
        rng = ctx.rng or random.Random()
        if len(args) >= 2:
            lo, hi = sorted((intval(args[0]), intval(args[1])))
            return rng.randint(lo, hi), "NUMBER"
        return rng.randint(0, 2147483647), "NUMBER"
    assert impl is not None
    try:
        result = impl(*args)
    except (TypeError, ValueError, OverflowError, ZeroDivisionError, IndexError):
        return False, "NUMBER"
    t = "NUMBER" if isinstance(result, (int, float, bool)) else "WORD"
    return result, t


def _fixnum(v: Any) -> Any:
    return v


def _list(*args: Any) -> str:
    return ", ".join(strval(a) for a in args if not loose_eq(a, ""))


def _unique(*args: Any) -> bool:
    seen: set[str] = set()
    for a in args:
        s = strval(a)
        if s.strip() == "":
            continue
        if s in seen:
            return False
        seen.add(s)
    return True


def _stddev(*args: Any) -> float:
    vals = [floatval(a) for a in args if is_numeric(a)]
    if len(vals) <= 1:
        return 0
    m = sum(vals) / len(vals)
    return math.sqrt(sum((v - m) ** 2 for v in vals) / (len(vals) - 1))


def _sum(*args: Any) -> Any:
    total = sum(floatval(a) if isinstance(a, str) else _number(a) for a in args)
    return int(total) if isinstance(total, float) and total.is_integer() else total


def _is_int(v: Any) -> bool:
    s = strval(v)
    if "." in s[1:]:
        s = re.sub(r"\.$", "", s.rstrip("0"))
    return bool(re.fullmatch(r"-?\d+", s))


def _regex_match(pattern: Any, value: Any) -> Any:
    rx = _php_regex(strval(pattern))
    if rx is None:
        return False
    return 1 if rx.search(strval(value)) else 0


def _strpos(h: Any, n: Any, off: Any = 0, *, ci: bool = False) -> Any:
    hs, ns, o = strval(h), strval(n), intval(off)
    if o > len(hs):
        return False
    if ci:
        hs, ns = hs.lower(), ns.lower()
    i = hs.find(ns, o)
    return False if i < 0 else i


def _strstr(h: Any, n: Any, before: Any = False, *, ci: bool = False) -> Any:
    hs, ns = strval(h), strval(n)
    i = (hs.lower().find(ns.lower()) if ci else hs.find(ns)) if ns else 0
    if i < 0:
        return False
    return hs[:i] if php_truthy(before) else hs[i:]


def _substr(s: Any, start: Any, length: Any = None) -> str:
    st, a = strval(s), intval(start)
    if a < 0:
        a = max(0, len(st) + a)
    if length is None:
        return st[a:]
    n = intval(length)
    end = len(st) + n if n < 0 else a + n
    return st[a:end]


def _trim(fn: Callable[[str, str | None], str]) -> Callable[..., str]:
    def go(s: Any, chars: Any = None) -> str:
        return fn(strval(s), None if chars is None else strval(chars))

    return go


def _convert_value(v: Any, strict: Any, src: Any, dst: Any) -> Any:
    if not is_numeric(v) or strict is None or src is None or dst is None:
        return None
    a, b = strval(src).split(","), strval(dst).split(",")
    if not a or len(a) != len(b):
        return None
    best, best_i = None, 0
    for i, x in enumerate(a):
        if not is_numeric(x):
            return None
        diff = abs(floatval(x) - floatval(v))
        if diff == 0:
            return b[i]
        if best is None or diff < best:
            best, best_i = diff, i
    return None if intval(strict) == 1 else b[best_i]


def _countif(match: Any, *args: Any) -> int:
    return sum(1 for a in args if loose_eq(a, match))


def _countifop(op: Any, value: Any, *args: Any) -> int:
    return sum(1 for a in args if _ifop(strval(op), a, value))


def _sumifop(op: Any, value: Any, *args: Any) -> Any:
    return _sum(*[a for a in args if _ifop(strval(op), a, value)])


def _log(number: Any = None, base: Any = None) -> float:
    if number is None or not is_numeric(number):
        return math.nan
    b = math.e if base is None else base
    if not is_numeric(b) or floatval(b) <= 0:
        return math.nan
    return math.log(floatval(number), floatval(b))


def _number_format(v: Any, decimals: Any = 0, point: Any = ".", sep: Any = ",") -> str:
    d = intval(decimals)
    s = f"{_round(v, d):,.{d}f}"
    return s.replace(",", "\0").replace(".", strval(point)).replace("\0", strval(sep))


def _str_pad(s: Any, n: Any, pad: Any = " ") -> str:
    st, p = strval(s), strval(pad) or " "
    while len(st) < intval(n):
        st += p
    return st[: max(intval(n), len(strval(s)))]


def _ucwords(s: Any) -> str:
    return re.sub(r"(^|\s)(\S)", lambda m: m.group(1) + m.group(2).upper(), strval(s))


def _strip_tags(s: Any, allowed: Any = None) -> str:
    return re.sub(r"<[^>]*>", "", strval(s))


_FUNCTIONS: dict[str, Callable[..., Any]] = {
    "abs": lambda v: abs(_number(v)) if is_numeric(v) else False,
    "acos": lambda v: math.acos(floatval(v)),
    "addslashes": lambda s: re.sub(r"(['\"\\\\])", r"\\\1", strval(s)),
    "asin": lambda v: math.asin(floatval(v)),
    "atan": lambda v: math.atan(floatval(v)),
    "atan2": lambda a, b: math.atan2(floatval(a), floatval(b)),
    "ceil": lambda v: float(math.ceil(floatval(v))),
    "checkdate": lambda m, d, y: _checkdate(intval(m), intval(d), intval(y)),
    "cos": lambda v: math.cos(floatval(v)),
    "count": lambda *a: sum(1 for x in a if not loose_eq(x, "")),
    "countif": _countif,
    "countifop": _countifop,
    "exp": lambda v: math.exp(floatval(v)),
    "fixnum": _fixnum,
    "floatval": floatval,
    "floor": lambda v: float(math.floor(floatval(v))),
    "html_entity_decode": lambda s: _html.unescape(strval(s)),
    "htmlentities": lambda s: _html.escape(strval(s)),
    "htmlspecialchars": lambda s: _html.escape(strval(s)),
    "htmlspecialchars_decode": lambda s: _html.unescape(strval(s)),
    "implode": lambda *a: strval(a[0]).join(strval(x) for x in a[1:]) if len(a) > 1 else "",
    "intval": lambda v, base=10: intval(v) if intval(base) == 10 else int(strval(v), intval(base)),
    "is_empty": lambda v: v is None or v == "" or v is False,
    "is_float": lambda v: isinstance(v, float),
    "is_int": _is_int,
    "is_nan": lambda v: isinstance(v, float) and math.isnan(v),
    "is_null": lambda v: v is None,
    "is_numeric": is_numeric,
    "is_string": lambda v: isinstance(v, str),
    "join": lambda *a: "".join(strval(x) for x in a),
    "list": _list,
    "log": _log,
    "ltrim": _trim(lambda s, c: s.lstrip(c) if c is not None else s.lstrip(" \t\n\r\0\x0b")),
    "max": lambda *a: _minmax(list(a), max),
    "min": lambda *a: _minmax(list(a), min),
    "nl2br": lambda s: strval(s).replace("\n", "<br />\n"),
    "number_format": _number_format,
    "pi": lambda: math.pi,
    "pow": lambda a, b: floatval(a) ** floatval(b),
    "quotemeta": lambda s: re.sub(r"([.\\+*?\[^\]$()])", r"\\\1", strval(s)),
    "regexMatch": _regex_match,
    "round": _round,
    "rtrim": _trim(lambda s, c: s.rstrip(c) if c is not None else s.rstrip(" \t\n\r\0\x0b")),
    "sin": lambda v: math.sin(floatval(v)),
    "sqrt": lambda v: math.sqrt(floatval(v)) if floatval(v) >= 0 else math.nan,
    "stddev": _stddev,
    "str_pad": _str_pad,
    "str_repeat": lambda s, n: strval(s) * max(0, intval(n)),
    "str_replace": lambda a, b, s: strval(s).replace(strval(a), strval(b)),
    "strcasecmp": lambda a, b: _strcmp(strval(a).lower(), strval(b).lower()),
    "strcmp": _strcmp,
    "strip_tags": _strip_tags,
    "stripos": lambda h, n, o=0: _strpos(h, n, o, ci=True),
    "stripslashes": lambda s: re.sub(r"\\(.)", r"\1", strval(s)),
    "stristr": lambda h, n, b=False: _strstr(h, n, b, ci=True),
    "strlen": lambda s: len(strval(s)),
    "strpos": _strpos,
    "strrev": lambda s: strval(s)[::-1],
    "strstr": _strstr,
    "strtolower": lambda s: strval(s).lower(),
    "strtoupper": lambda s: strval(s).upper(),
    "substr": _substr,
    "sum": _sum,
    "sumifop": _sumifop,
    "tan": lambda v: math.tan(floatval(v)),
    "convert_value": _convert_value,
    "trim": _trim(lambda s, c: s.strip(c) if c is not None else s.strip(" \t\n\r\0\x0b")),
    "ucwords": _ucwords,
    "unique": _unique,
}


def _checkdate(m: int, d: int, y: int) -> bool:
    try:
        datetime(y, m, d)
        return True
    except ValueError:
        return False


#: every function the runtime evaluates (clock functions are recorded)
KNOWN_FUNCTIONS: Final = frozenset(_FUNCTIONS) | CLOCK_FUNCTIONS | {"if", "rand"}


# --------------------------------------------------------------------------- public


def _ctx(
    state: RespondentState,
    survey: Survey | None,
    location: str,
    affects: Affects,
    item: str | None = None,
) -> Context:
    def record(code: str, detail: str) -> None:
        state.approximate(code, location, affects, detail)

    return Context(state, survey, record, rng=state.rng, item=item)


def evaluate_boolean(
    expression: Expression,
    state: RespondentState,
    survey: Survey | None,
    *,
    affects: Affects = "routing",
    item: str | None = None,
) -> bool:
    """EM ``ProcessBooleanExpression``: true if the expression holds; false if it
    fails, or reads an irrelevant question without ``.NAOK``."""
    location = expression.location or expression.source
    if expression.tree is None:
        state.approximate(
            "expression.parse_error",
            location,
            affects,
            f"expression {expression.source!r} could not be parsed; treated as false",
        )
        return False
    ctx = _ctx(state, survey, location, affects, item)
    try:
        value, _ = _eval(expression.tree, ctx)
    except _Fail:
        return False
    if value is None:
        return False
    return php_truthy(value)


def evaluate_value(
    expr: Expr | None,
    state: RespondentState,
    survey: Survey | None,
    location: str,
    affects: Affects = "exposure",
    *,
    rng: random.Random | None = None,
) -> str:
    """EM string processing of one ``{...}``: the value as text; empty if it fails.
    ``rng`` is the stream ``rand()`` draws from (default: the state's)."""
    if expr is None:
        return ""
    ctx = _ctx(state, survey, location, affects)
    if rng is not None:
        ctx.rng = rng
    try:
        value, _ = _eval(expr, ctx)
    except _Fail:
        return ""
    if isinstance(value, bool):
        return "1" if value else ""
    return strval(value)


def static_gaps(expr: Expr | None, source: str) -> list[tuple[str, str]]:
    """``(code, detail)`` for every gap evaluating ``expr`` would record, as far as it
    can be known without answers (``executability``)."""
    if expr is None:
        return [("expression.parse_error", f"expression {source!r} could not be parsed")]
    out: list[tuple[str, str]] = []
    for node in iter_nodes(expr):
        if isinstance(node, Call):
            if node.fn in CLOCK_FUNCTIONS:
                out.append(("expression.clock", f"{node.fn}() reads the current date or time"))
            elif node.fn not in KNOWN_FUNCTIONS:
                out.append(("expression.unknown_function", f"function {node.fn}() in {source!r}"))
        elif isinstance(node, Var) and (
            node.kind == "unknown"
            or (node.kind == "survey" and (node.field or node.name) not in SURVEY_VALUES)
        ):
            out.append(("expression.unknown_variable", f"variable {node.name!r} in {source!r}"))
        elif isinstance(node, Var) and node.kind == "survey" and node.field in CLOCK_VALUES:
            out.append(("expression.clock", f"{node.field} reads the current date and time"))
    return list(dict.fromkeys(out))
