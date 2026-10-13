"""LimeSurvey question types -> IR question kinds.

Every LimeSurvey type maps onto an existing IR kind, using the same conventions
the Qualtrics loader uses for the same shapes (a number entry is a text entry
validated as a number, a multiple numerical input is a text-entry form, ...):

==========================  =================================================
L ! O 5 G Y I               ``choice`` (single; ``!`` as a dropdown)
M P                         ``choice`` (multiple; P's comments are choice text
                            entries)
F H A B C E                 ``matrix`` (single): subquestions are rows, answers
                            (or the fixed 5/10-point, Yes/Uncertain/No and
                            Increase/Same/Decrease scales) are columns
1                           ``side_by_side``: two matrices, one per scale
: ;                         ``matrix`` (text): subquestions of scale 0 are rows,
                            of scale 1 columns; ``:`` is validated as numbers
K Q                         ``text_entry`` (form): one field per subquestion
S T U                       ``text_entry`` (single line, multi line, essay)
N                           ``text_entry`` validated as a number
D                           ``text_entry`` validated as a date
R                           ``rank_order``
X                           ``descriptive``
*                           ``equation``
|                           ``file_upload``
==========================  =================================================

Choice ids are the codes LimeSurvey records: answer codes, subquestion codes,
``-oth-`` for the "Other" option of a list, ``other`` for that of a multiple
choice. A "No answer" option, shown on optional questions when the survey
offers it, is a choice (or column) with the empty id: choosing it records an
empty answer, as in LimeSurvey.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from ...model import (
    Choice,
    ChoiceQuestion,
    DescriptiveQuestion,
    EquationQuestion,
    FileUploadQuestion,
    MatrixQuestion,
    NumberRange,
    Origin,
    Question,
    Randomization,
    RankOrderQuestion,
    ScalePoint,
    SideBySideQuestion,
    TextEntryQuestion,
    Translation,
    UnsupportedQuestion,
    Validation,
)
from ...model.text import Text
from .tables import ARow, QRow

#: type code -> (IR kind, readable name)
TYPES: dict[str, tuple[str, str]] = {
    "L": ("choice", "List (radio)"),
    "!": ("choice", "List (dropdown)"),
    "O": ("choice", "List with comment"),
    "5": ("choice", "5 point choice"),
    "G": ("choice", "Gender"),
    "Y": ("choice", "Yes/No"),
    "I": ("choice", "Language switch"),
    "M": ("choice", "Multiple choice"),
    "P": ("choice", "Multiple choice with comments"),
    "F": ("matrix", "Array"),
    "H": ("matrix", "Array by column"),
    "A": ("matrix", "Array (5 point choice)"),
    "B": ("matrix", "Array (10 point choice)"),
    "C": ("matrix", "Array (Yes/No/Uncertain)"),
    "E": ("matrix", "Array (Increase/Same/Decrease)"),
    "1": ("side_by_side", "Array dual scale"),
    ":": ("matrix", "Array (Numbers)"),
    ";": ("matrix", "Array (Texts)"),
    "K": ("text_entry", "Multiple numerical input"),
    "Q": ("text_entry", "Multiple short text"),
    "S": ("text_entry", "Short free text"),
    "T": ("text_entry", "Long free text"),
    "U": ("text_entry", "Huge free text"),
    "N": ("text_entry", "Numerical input"),
    "D": ("text_entry", "Date/Time"),
    "R": ("rank_order", "Ranking"),
    "X": ("descriptive", "Text display"),
    "*": ("equation", "Equation"),
    "|": ("file_upload", "File upload"),
}

SINGLE = set("L!O5GYI")
ARRAYS = set("FHABCE")
FIXED_SCALES: dict[str, list[tuple[str, str]]] = {
    "A": [(str(i), str(i)) for i in range(1, 6)],
    "B": [(str(i), str(i)) for i in range(1, 11)],
    "C": [("Y", "Yes"), ("U", "Uncertain"), ("N", "No")],
    "E": [("I", "Increase"), ("S", "Same"), ("D", "Decrease")],
}
FIXED_CHOICES: dict[str, list[tuple[str, str]]] = {
    "5": [(str(i), str(i)) for i in range(1, 6)],
    "G": [("F", "Female"), ("M", "Male")],
    "Y": [("Y", "Yes"), ("N", "No")],
}
NO_ANSWER = "No answer"


@dataclass
class Context:
    """What building one question needs from the loader."""

    lang: str
    languages: list[str]
    text: Callable[[str | None, str], Text]  # (markup, owner question id) -> Text
    attrs: Mapping[str, Any]  # this question's attributes
    subquestions: list[QRow]
    answers: list[ARow]
    show_no_answer: bool
    survey_languages: list[str]


def attr(ctx: Context, name: str, lang: str | None = None) -> str:
    v = ctx.attrs.get(name)
    if isinstance(v, dict):
        return v.get(lang or ctx.lang) or v.get(ctx.lang) or next(iter(v.values()), "") or ""
    return "" if v is None else str(v)


def _on(v: str) -> bool:
    return v.strip().lower() in ("1", "y", "yes", "true", "on")


def _num(v: str) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _qtext(q: QRow, lang: str) -> tuple[str, str, str]:
    t = q.texts.get(lang) or next(iter(q.texts.values()), {}) if q.texts else {}
    return t.get("question", ""), t.get("help", ""), t.get("script", "")


def question_markup(q: QRow, lang: str) -> str:
    """The question text as shown: the question, then its help text."""
    text, help_, _ = _qtext(q, lang)
    if help_.strip():
        return f'{text}<div class="ls-questionhelp">{help_}</div>'
    return text


def _choices(rows: list[QRow], ctx: Context, owner: str) -> list[Choice]:
    out = []
    for r in rows:
        text, _, _ = _qtext(r, ctx.lang)
        out.append(
            Choice(
                id=r.title,
                text=ctx.text(text, owner),
                extras={
                    "limesurvey": {
                        "qid": r.qid,
                        **(
                            {"relevance": r.row.get("relevance")}
                            if r.row.get("relevance") not in (None, "", "1")
                            else {}
                        ),
                    },
                    "row": _row_extras(r.row),
                },
            )
        )
    return out


def _row_extras(row: Mapping[str, Any]) -> dict[str, Any]:
    skip = {
        "qid",
        "parent_qid",
        "sid",
        "gid",
        "type",
        "title",
        "question",
        "help",
        "language",
        "question_order",
        "scale_id",
        "relevance",
        "script",
    }
    return {k: v for k, v in row.items() if k not in skip}


def _answer_choices(answers: list[ARow], ctx: Context, owner: str) -> list[Choice]:
    return [
        Choice(
            id=a.code,
            text=ctx.text(a.texts.get(ctx.lang) or next(iter(a.texts.values()), ""), owner),
            recode=_recode(a.code),
            extras={"assessment_value": a.assessment_value}
            if a.assessment_value not in ("", "0")
            else {},
        )
        for a in answers
    ]


def _points(answers: list[ARow], ctx: Context, owner: str) -> list[ScalePoint]:
    return [
        ScalePoint(
            id=a.code,
            text=ctx.text(a.texts.get(ctx.lang) or next(iter(a.texts.values()), ""), owner),
            recode=_recode(a.code),
            extras={"assessment_value": a.assessment_value}
            if a.assessment_value not in ("", "0")
            else {},
        )
        for a in answers
    ]


def _recode(code: str) -> int | float | str | None:
    try:
        f = float(code)
    except ValueError:
        return code or None
    return int(f) if f.is_integer() and "." not in code else f


def _fixed(pairs: list[tuple[str, str]], cls: type) -> list[Any]:
    return [cls(id=c, text=Text(html=t, plain=t), recode=_recode(c)) for c, t in pairs]


def _order(
    items: list[Any], ctx: Context, fixed: set[str], *, answers: bool
) -> tuple[list[Any], Randomization | None]:
    """Apply LimeSurvey ordering attributes: alphabetical order is fixed at load,
    random order becomes a ``Randomization`` (``fixed`` ids keep their place)."""
    if answers:
        mode = attr(ctx, "answer_order")
        if not mode and _on(attr(ctx, "random_order")):
            mode = "random"
    else:
        mode = attr(ctx, "subquestion_order")
        if not mode and _on(attr(ctx, "random_order")):
            mode = "random"
    if mode == "alphabetical":
        movable = sorted((c for c in items if c.id not in fixed), key=lambda c: c.text.plain)
        it = iter(movable)
        items = [c if c.id in fixed else next(it) for c in items]
        return items, None
    if mode == "random":
        ids = [c.id for c in items]
        if any(i in fixed for i in ids):
            return items, Randomization(
                mode="advanced", randomized=[i for i in ids if i not in fixed]
            )
        return items, Randomization(mode="all")
    return items, None


def _place_other(choices: list[Choice], other: Choice, ctx: Context) -> list[Choice]:
    pos = attr(ctx, "other_position") or "default"
    if pos == "beginning":
        return [other, *choices]
    if pos == "specific":
        code = attr(ctx, "other_position_code")
        for i, c in enumerate(choices):
            if c.id == code:
                return [*choices[: i + 1], other, *choices[i + 1 :]]
    return [*choices, other]


def _validation(q: QRow, ctx: Context, kind_type: str) -> Validation | None:
    mandatory = (q.row.get("mandatory") or "N").upper()
    required = {"Y": "force", "S": "request"}.get(mandatory, "off")
    v: dict[str, Any] = {"required": required}
    lo, hi = _num(attr(ctx, "min_answers")), _num(attr(ctx, "max_answers"))
    if kind_type in "MPR" and kind_type:
        if lo is not None:
            v["min_choices"] = int(lo)
        if hi is not None:
            v["max_choices"] = int(hi)
    if kind_type in ("N", "K", ":"):
        v["content_type"] = "ValidNumber"
        lo_n = _num(attr(ctx, "min_num_value_n"))
        hi_n = _num(attr(ctx, "max_num_value_n"))
        decimals = 0 if _on(attr(ctx, "num_value_int_only")) else None
        if lo_n is not None or hi_n is not None or decimals is not None:
            v["number"] = NumberRange(min=lo_n, max=hi_n, decimals=decimals)
    if kind_type == "D":
        v["content_type"] = "ValidDate"
    if kind_type in ("S", "T", "U", "Q", ";", "N", "K"):
        mx = _num(attr(ctx, "maximum_chars"))
        if mx is not None and mx > 0:
            v["max_chars"] = int(mx)
    if q.row.get("preg"):
        v["pattern"] = q.row.get("preg")
    tip = attr(ctx, "em_validation_q_tip")
    if tip:
        v["custom_message"] = tip
    if required == "off" and len(v) == 1:
        return None
    return Validation(**v)


def build(q: QRow, ctx: Context, *, other: bool) -> Question:
    """The IR question for ``q``."""
    t = q.type
    kind, name = TYPES.get(t, ("unsupported", f"type {t!r}"))
    text_markup = question_markup(q, ctx.lang)
    _, _, script = _qtext(q, ctx.lang)
    common: dict[str, Any] = {
        "id": q.title,
        "export_tag": q.title,
        "origin": Origin(
            format="limesurvey", type=t, selector=q.row.get("question_theme_name") or None
        ),
        "text": ctx.text(text_markup, q.title),
        "description": name,
        "validation": _validation(q, ctx, t),
        "javascript": script or None,
        "hidden": _on(attr(ctx, "hidden")),
    }
    sub0 = [s for s in ctx.subquestions if s.scale_id == 0]
    sub1 = [s for s in ctx.subquestions if s.scale_id == 1]
    ans0 = [a for a in ctx.answers if a.scale_id == 0]
    ans1 = [a for a in ctx.answers if a.scale_id == 1]
    no_answer = ctx.show_no_answer and (q.row.get("mandatory") or "N").upper() != "Y"
    owner = q.title

    if t in SINGLE:
        if t in FIXED_CHOICES:
            choices = _fixed(FIXED_CHOICES[t], Choice)
        elif t == "I":
            choices = [Choice(id=x, text=Text(html=x, plain=x)) for x in ctx.survey_languages]
        else:
            choices = _answer_choices(ans0, ctx, owner)
        fixed: set[str] = set()
        if other and t in ("L", "!"):
            label = attr(ctx, "other_replace_text") or "Other"
            oth = Choice(id="-oth-", text=Text(html=label, plain=label), text_entry=True)
            choices = _place_other(choices, oth, ctx)
            fixed.add("-oth-")
        choices, rnd = _order(choices, ctx, fixed | {""}, answers=True)
        if no_answer and t not in ("I",):
            choices.append(Choice(id="", text=Text(html=NO_ANSWER, plain=NO_ANSWER)))
            if rnd is not None and rnd.mode == "all":
                rnd = Randomization(mode="advanced", randomized=[c.id for c in choices if c.id])
        layout = (
            "dropdown"
            if t in ("!", "I") or _on(attr(ctx, "dropdown_dates"))
            else ("horizontal" if t in ("5", "G", "Y") else "vertical")
        )
        return ChoiceQuestion(
            **common,
            multiple=False,
            layout=layout,  # type: ignore[arg-type]
            choices=choices,
            randomization=rnd,
            translations=_translations(q, ctx, choices=choices),
        )
    if t in ("M", "P"):
        choices = _choices(sub0, ctx, owner)
        if t == "P":
            for c in choices:
                c.text_entry = True
        exclusive = {x.strip() for x in attr(ctx, "exclude_all_others").split(";") if x.strip()}
        for c in choices:
            c.exclusive = c.id in exclusive
        fixed = set()
        if other:
            label = attr(ctx, "other_replace_text") or "Other"
            oth = Choice(id="other", text=Text(html=label, plain=label), text_entry=True)
            choices = _place_other(choices, oth, ctx)
            fixed.add("other")
        choices, rnd = _order(choices, ctx, fixed, answers=False)
        cols = _num(attr(ctx, "display_columns"))
        return ChoiceQuestion(
            **common,
            multiple=True,
            layout="columns" if cols and cols > 1 else "vertical",
            columns=int(cols) if cols and cols > 1 else None,
            choices=choices,
            randomization=rnd,
            translations=_translations(q, ctx, choices=choices),
        )
    if t in ARRAYS:
        rows = _choices(sub0, ctx, owner)
        rows, rnd = _order(rows, ctx, set(), answers=False)
        columns = (
            _fixed(FIXED_SCALES[t], ScalePoint) if t in FIXED_SCALES else _points(ans0, ctx, owner)
        )
        col_rnd = None
        if t not in FIXED_SCALES:
            columns, col_rnd = _order(columns, ctx, set(), answers=True)
        if no_answer:
            columns.append(ScalePoint(id="", text=Text(html=NO_ANSWER, plain=NO_ANSWER)))
        return MatrixQuestion(
            **common,
            mode="single",
            rows=rows,
            columns=columns,
            randomization=rnd,
            column_randomization=col_rnd,
            translations=_translations(q, ctx, choices=rows, answers=columns),
        )
    if t == "1":
        rows = _choices(sub0, ctx, owner)
        rows, rnd = _order(rows, ctx, set(), answers=False)
        subs: list[Question] = []
        for scale, ans, header in ((0, ans0, "dualscale_headerA"), (1, ans1, "dualscale_headerB")):
            cols = _points(ans, ctx, owner)
            if no_answer:
                cols.append(ScalePoint(id="", text=Text(html=NO_ANSWER, plain=NO_ANSWER)))
            label = attr(ctx, header)
            subs.append(
                MatrixQuestion(
                    id=f"{q.title}#{scale}",
                    export_tag=f"{q.title}#{scale}",
                    origin=Origin(format="limesurvey", type="1"),
                    text=Text(html=label, plain=label),
                    mode="single",
                    rows=rows,
                    columns=cols,
                )
            )
        return SideBySideQuestion(
            **common,
            rows=rows,
            questions=subs,
            randomization=rnd,
            translations=_translations(q, ctx, choices=rows),
        )
    if t in (":", ";"):
        rows = _choices(sub0, ctx, owner)
        rows, rnd = _order(rows, ctx, set(), answers=False)
        cols = [
            ScalePoint(id=c.id, text=c.text, extras=c.extras) for c in _choices(sub1, ctx, owner)
        ]
        return MatrixQuestion(
            **common,
            mode="text",
            rows=rows,
            columns=cols,
            randomization=rnd,
            translations=_translations(q, ctx, choices=rows, answers=cols),
        )
    if t in ("K", "Q"):
        fields = _choices(sub0, ctx, owner)
        fields, rnd = _order(fields, ctx, set(), answers=False)
        return TextEntryQuestion(
            **common,
            mode="form",
            fields=fields,
            randomization=rnd,
            translations=_translations(q, ctx, choices=fields),
        )
    if t in ("S", "T", "U", "N", "D"):
        mode = {"S": "single_line", "T": "multi_line", "U": "essay"}.get(t, "single_line")
        return TextEntryQuestion(**common, mode=mode, translations=_translations(q, ctx))  # type: ignore[arg-type]
    if t == "R":
        source = sub0 if sub0 else None
        if source:  # LimeSurvey 7: ranking items are subquestions
            items = [
                c.model_copy(update={"recode": _recode(c.id)}) for c in _choices(sub0, ctx, owner)
            ]
        else:
            items = _answer_choices(ans0, ctx, owner)
        items, rnd = _order(items, ctx, set(), answers=not sub0)
        return RankOrderQuestion(
            **common,
            mode="drag_and_drop",
            items=items,
            randomization=rnd,
            translations=_translations(q, ctx, choices=items),
        )
    if t == "X":
        return DescriptiveQuestion(**common, translations=_translations(q, ctx))
    if t == "*":
        source_text = attr(ctx, "equation") or _qtext(q, ctx.lang)[0]
        return EquationQuestion(
            **common, expression=ctx.text(source_text, owner), translations=_translations(q, ctx)
        )
    if t == "|":
        exts = attr(ctx, "allowed_filetypes")
        if exts:
            v = common["validation"] or Validation()
            common["validation"] = v.model_copy(update={"file_type": exts})
        return FileUploadQuestion(**common, translations=_translations(q, ctx))
    return UnsupportedQuestion(**common, extras={"type": t})


def _translations(
    q: QRow,
    ctx: Context,
    *,
    choices: list[Any] | None = None,
    answers: list[Any] | None = None,
) -> dict[str, Translation]:
    """Text in every other language, for the question and its options."""
    out: dict[str, Translation] = {}
    by_code_sub = {s.title: s for s in ctx.subquestions}
    by_code_ans = {a.code: a for a in ctx.answers}
    for lang in ctx.languages[1:]:
        if lang not in q.texts:
            continue
        tr = Translation(text=ctx.text(question_markup(q, lang), q.title))
        for c in choices or []:
            s = by_code_sub.get(c.id)
            a = by_code_ans.get(c.id)
            if s is not None and lang in s.texts:
                tr.choices[c.id] = ctx.text(s.texts[lang].get("question", ""), q.title)
            elif a is not None and lang in a.texts:
                tr.choices[c.id] = ctx.text(a.texts[lang], q.title)
        for c in answers or []:
            a = by_code_ans.get(c.id)
            s = by_code_sub.get(c.id)
            if a is not None and lang in a.texts:
                tr.answers[c.id] = ctx.text(a.texts[lang], q.title)
            elif s is not None and lang in s.texts:
                tr.answers[c.id] = ctx.text(s.texts[lang].get("question", ""), q.title)
        out[lang] = tr
    return out
