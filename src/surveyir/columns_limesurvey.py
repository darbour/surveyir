"""Response columns of a LimeSurvey survey, named as LimeSurvey exports them.

LimeSurvey's response table has one field per answer part, in the order of its
field map (``createFieldMap``): the response metadata (``id``, ``submitdate``,
``lastpage``, ``startlanguage``, ``seed``, then ``token``, ``startdate`` /
``datestamp``, ``ipaddr`` and ``refurl`` when the survey records them), then
every question in group and question order, one field per part:

==============================  ==============================================
single answer (L ! O 5 G Y N    the question; plus ``other`` (L, !) or
S T U D * I X)                  ``comment`` (O)
subquestions (M P F H A B C E   one per subquestion; P adds ``<sq>comment``; M
K Q)                            and P add ``other`` (and P ``othercomment``)
dual scale (1)                  two per subquestion, one per scale
numbers / texts arrays (: ;)    one per row x column
ranking (R)                     one per rank (``max_subquestions`` caps them)
file upload (|)                 the files, and ``filecount``
==============================  ==============================================

Each column is named three ways (``LimeColumnOptions.heading``):

* ``code``: the "question code" heading of a CSV, R or SPSS export:
  ``Q1``, ``Q1[SQ001]``, ``Q1[SQ001_SQ002]``, ``Q1[SQ001][1]`` (dual scale),
  ``Q1[other]``, ``Q1[SQ001comment]``, ``Q1[1]`` (rank 1), ``Q1[filecount]``;
* ``em``: the same with ExpressionScript separators: ``Q1_SQ001``, ``Q1_SQ001_0``;
* ``field``: the database field name, as in ``.lsr`` response files and the
  "question code (database)" heading. Before LimeSurvey 7 these are SGQA codes
  (``123456X12X34SQ001``, ``...SQ001#1``, ``...other``); from 7 on they are
  ``Q34_S56`` (``Q34_S56#1``, ``Q34_Cother``, ``Q34_S56_Ccomment``,
  ``Q34_Cfilecount``). ``naming`` picks one; ``infer_lime_options`` reads it off a
  real export.

Values are LimeSurvey's "answer codes": the code of the chosen answer, ``Y`` (or
empty) for each multiple-choice option, the text of free-text and number entries,
the answer code at each rank, and the computed value of an equation. The
column's ``import_extra`` keeps every name (``code``, ``em``, ``sgqa``,
``qid``) so a column can be matched under any heading.
"""

from __future__ import annotations

import csv
import io
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, Field

from .columns import Column
from .model import (
    ChoiceQuestion,
    EquationQuestion,
    MatrixQuestion,
    Question,
    RankOrderQuestion,
    SideBySideQuestion,
    Survey,
    TextEntryQuestion,
)

if TYPE_CHECKING:
    from .runtime.walker import RespondentRun

#: (field, label, survey setting that adds it or None)
METADATA: list[tuple[str, str, str | None]] = [
    ("id", "Response ID", None),
    ("submitdate", "Date submitted", None),
    ("lastpage", "Last page", None),
    ("startlanguage", "Start language", None),
    ("seed", "Seed", None),
    ("token", "Access code", "token"),
    ("startdate", "Date started", "datestamp"),
    ("datestamp", "Date last action", "datestamp"),
    ("ipaddr", "IP address", "ipaddr"),
    ("refurl", "Referrer URL", "refurl"),
]
#: LimeSurvey records this when a response is not timestamped
UNDATED_SUBMIT = "1980-01-01 00:00:00"


class LimeColumnOptions(BaseModel):
    """How a LimeSurvey export names and orders its columns. None of this is in the
    .lss; it is chosen when the data is exported (``infer_lime_options`` reads it
    off a real export)."""

    heading: Literal["code", "em", "field"] = Field(
        default="code",
        description="'code': question-code headings (Q1[SQ001]); 'em': ExpressionScript "
        "names (Q1_SQ001); 'field': database field names (see `naming`).",
    )
    naming: Literal["auto", "sgqa", "qid"] = Field(
        default="auto",
        description="Database field names: 'sgqa' (123X4X5SQ001, LimeSurvey up to 6), "
        "'qid' (Q5_S6, LimeSurvey 7), or 'auto' (from the file's DBVersion).",
    )
    metadata: bool = Field(default=True, description="Start with the response metadata fields.")
    token: bool | None = Field(
        default=None,
        description="Include the token field; None: when the survey is not anonymized "
        "and uses participants.",
    )
    quota_exit: bool | None = Field(
        default=None,
        description="Include LimeSurvey 7's quota_exit field (None: with 'qid' naming).",
    )
    question_ids: list[str] | None = Field(
        default=None, description="Export only these questions (by code)."
    )


def is_limesurvey(survey: Survey) -> bool:
    return survey.source.format.startswith("limesurvey")


def _naming(survey: Survey, options: LimeColumnOptions) -> str:
    if options.naming != "auto":
        return options.naming
    v = survey.source.format_version
    return "qid" if v is not None and v.isdigit() and int(v) >= 700 else "sgqa"


def _info(q: Question) -> dict[str, Any]:
    return q.extras.get("limesurvey") or {}


def _sub_qid(item: Any) -> str:
    info = item.extras.get("limesurvey") if hasattr(item, "extras") else None
    return str((info or {}).get("qid", item.id))


def _col(
    q: Question,
    sgqa_suffix: str,
    qid_suffix: str,
    aid: str | None,
    scale: int | None,
    *,
    part: str = "response",
    choice_id: str | None = None,
    row_id: str | None = None,
    answer_id: str | None = None,
) -> Column:
    info = _info(q)
    sgqa = f"{info.get('sgqa', q.id)}{sgqa_suffix}"
    qidname = f"Q{info.get('qid', q.id)}{qid_suffix}"
    code = q.id + (f"[{aid}]" if aid else "") + (f"[{scale + 1}]" if scale is not None else "")
    em = q.id + (f"_{aid}" if aid else "") + (f"_{scale}" if scale is not None else "")
    return Column(
        name=code,
        import_id=sgqa,
        import_extra={"code": code, "em": em, "sgqa": sgqa, "qid": qidname},
        question_id=q.id,
        part=part,  # type: ignore[arg-type]
        choice_id=choice_id,
        row_id=row_id,
        answer_id=answer_id,
        evidence="corpus",
    )


def question_columns(q: Question) -> list[Column]:
    """The fields one question adds, in LimeSurvey's order."""
    if _info(q).get("synthetic"):  # a group description or the welcome page: no field
        return []
    t = q.origin.type
    cols: list[Column] = []
    if t in ("M", "P") and isinstance(q, ChoiceQuestion):
        for c in q.choices:
            if c.id == "other":
                continue
            sq = _sub_qid(c)
            cols.append(_col(q, c.id, f"_S{sq}", c.id, None, choice_id=c.id))
            if t == "P":
                cols.append(
                    _col(
                        q,
                        f"{c.id}comment",
                        f"_S{sq}_Ccomment",
                        f"{c.id}comment",
                        None,
                        part="text",
                        choice_id=c.id,
                    )
                )
        if any(c.id == "other" for c in q.choices):
            cols.append(_col(q, "other", "_Cother", "other", None, part="text", choice_id="other"))
            if t == "P":
                cols.append(
                    _col(
                        q,
                        "othercomment",
                        "_Cothercomment",
                        "othercomment",
                        None,
                        part="text",
                        choice_id="othercomment",
                    )
                )
        return cols
    if isinstance(q, MatrixQuestion) and q.mode == "text":
        for r in q.rows:
            for c in q.columns:
                cols.append(
                    _col(
                        q,
                        f"{r.id}_{c.id}",
                        f"_S{_sub_qid(r)}_S{_sub_qid(c)}",
                        f"{r.id}_{c.id}",
                        None,
                        row_id=r.id,
                        answer_id=c.id,
                    )
                )
        return cols
    if isinstance(q, MatrixQuestion):
        return [_col(q, r.id, f"_S{_sub_qid(r)}", r.id, None, row_id=r.id) for r in q.rows]
    if isinstance(q, SideBySideQuestion):
        for r in q.rows:
            for k in (0, 1):
                cols.append(
                    _col(
                        q,
                        f"{r.id}#{k}",
                        f"_S{_sub_qid(r)}#{k}",
                        r.id,
                        k,
                        row_id=r.id,
                        answer_id=str(k),
                    )
                )
        return cols
    if isinstance(q, TextEntryQuestion) and q.mode == "form":
        return [_col(q, f.id, f"_S{_sub_qid(f)}", f.id, None, choice_id=f.id) for f in q.fields]
    if isinstance(q, RankOrderQuestion):
        limit = (q.extras.get("attributes") or {}).get("max_subquestions") or (
            q.extras.get("attributes") or {}
        ).get("max_answers")
        n = len(q.items)
        if isinstance(limit, str) and limit.isdigit() and 0 < int(limit) < n:
            n = int(limit)
        for i in range(1, n + 1):
            item = q.items[i - 1]
            cols.append(_col(q, str(i), f"_S{_sub_qid(item)}", str(i), None, choice_id=str(i)))
        return cols
    if t == "|":
        return [
            _col(q, "", "", None, None),
            _col(q, "_filecount", "_Cfilecount", "filecount", None, part="file"),
        ]
    cols.append(_col(q, "", "", None, None))
    if isinstance(q, ChoiceQuestion) and any(c.id == "-oth-" for c in q.choices):
        cols.append(_col(q, "other", "_Cother", "other", None, part="text", choice_id="-oth-"))
    if t == "O":
        cols.append(
            _col(q, "comment", "_Ccomment", "comment", None, part="text", choice_id="comment")
        )
    return cols


def lime_columns(survey: Survey, options: LimeColumnOptions | None = None) -> list[Column]:
    """Every response field of a LimeSurvey survey, named per ``options``."""
    options = options or LimeColumnOptions()
    naming = _naming(survey, options)
    settings = survey.options
    cols: list[Column] = []
    if options.metadata:
        for name, label, setting in METADATA:
            if setting == "token":
                wanted = options.token
                if wanted is None:
                    wanted = (settings.get("anonymized") or "N") == "N" and (
                        settings.get("usetokens") or "N"
                    ) == "Y"
                if not wanted:
                    continue
            elif setting is not None and (settings.get(setting) or "N") != "Y":
                continue
            cols.append(
                Column(name=name, import_id=name, import_extra={"label": label}, part="metadata")
            )
        quota_exit = options.quota_exit if options.quota_exit is not None else naming == "qid"
        if quota_exit:
            cols.append(
                Column(
                    name="quota_exit",
                    import_id="quota_exit",
                    import_extra={"label": "Quota exit"},
                    part="metadata",
                    evidence="inferred",
                )
            )
    wanted_q = set(options.question_ids) if options.question_ids is not None else None
    for q in _in_field_order(survey):
        if wanted_q is not None and q.id not in wanted_q:
            continue
        for c in question_columns(q):
            name = {
                "code": c.import_extra["code"],
                "em": c.import_extra["em"],
                "field": c.import_extra["qid" if naming == "qid" else "sgqa"],
            }[options.heading]
            cols.append(c.model_copy(update={"name": str(name)}))
    return cols


def _in_field_order(survey: Survey) -> list[Question]:
    """Questions in group order, then question order (the field map's order)."""

    def key(q: Question) -> tuple[int, int]:
        block = survey.blocks.get(f"G{_info(q).get('gid', '')}")
        gorder = (block.extras.get("limesurvey") or {}).get("group_order", 0) if block else 0
        return int(gorder), int((q.extras.get("row") or {}).get("question_order") or 0)

    out: list[Question] = []
    for bid in survey.blocks:
        out += survey.block_questions(bid)
    order = {q.id: i for i, q in enumerate(out)}
    return sorted(out, key=lambda q: (key(q), order[q.id]))


# --------------------------------------------------------------------------- values


def _code(value: Any) -> Any:
    return "" if value is None else value


def answer_value(q: Question, col: Column, value: Any, text: Mapping[str, str]) -> Any:
    """The exported value of one field, from the respondent's answer to ``q``."""
    t = q.origin.type
    if col.part == "file":
        return 1 if value else 0
    if col.part == "text":
        return text.get(col.choice_id or "", "")
    if isinstance(q, ChoiceQuestion):
        if q.multiple:
            chosen = {str(v) for v in value} if isinstance(value, list) else set()
            return "Y" if col.choice_id in chosen else ""
        return _code(value)
    if isinstance(q, MatrixQuestion) and isinstance(value, dict):
        cell = value.get(col.row_id)
        if q.mode == "text":
            return cell.get(col.answer_id, "") if isinstance(cell, dict) else ""
        return _code(cell)
    if isinstance(q, SideBySideQuestion) and isinstance(value, dict):
        scale = value.get(f"{q.id}#{col.answer_id}")
        return _code(scale.get(col.row_id)) if isinstance(scale, dict) else ""
    if isinstance(q, TextEntryQuestion):
        if isinstance(value, dict):
            return _code(value.get(col.choice_id))
        return _code(value)
    if isinstance(q, RankOrderQuestion) and isinstance(value, dict):
        item = next((i for i, r in value.items() if str(r) == col.choice_id), None)
        return _code(item)
    if isinstance(q, EquationQuestion):
        return _code(value)
    if t == "|":
        return value or ""
    if isinstance(value, (dict, list)):
        return ""
    return _code(value)


def lime_cells(
    run: RespondentRun,
    survey: Survey,
    options: LimeColumnOptions | None = None,
    *,
    key: Mapping[str, Any] | None = None,
) -> list[tuple[Column, Any]]:
    """(column, value) for every field, in order. Questions the respondent never
    reached are empty; irrelevant (hidden) questions are empty, as LimeSurvey
    stores them."""
    from .columns import key_columns

    options = options or LimeColumnOptions()
    questions = survey.questions
    out: list[tuple[Column, Any]] = []
    for col in lime_columns(survey, options):
        value: Any = ""
        if col.part == "metadata":
            value = {
                "id": run.index + 1,
                "submitdate": UNDATED_SUBMIT if run.finished else "",
                "lastpage": _last_page(run),
                "startlanguage": run.state.embedded.get("LANG") or survey.language or "",
                "seed": run.seed,
                "token": run.state.embedded.get("token", ""),
                "quota_exit": "",
            }.get(col.import_id, "")
        elif col.question_id is not None:
            q = questions.get(col.question_id)
            answer = run.answers.get((col.question_id, None))
            if q is not None and answer is not None:
                value = answer_value(q, col, answer.value, answer.text)
        out.append((col, value))
    if key:
        out.extend(zip(key_columns(key), key.values(), strict=True))
    return out


def _last_page(run: RespondentRun) -> Any:
    from .runtime.trace import PageStart

    pages = [ob.page for ob in run.trace if isinstance(ob, PageStart)]
    return max(pages) if pages else ""


def lime_header(
    survey: Survey, columns: Sequence[Column], key: Sequence[str] = ()
) -> list[list[str]]:
    """A LimeSurvey CSV export has one header row: the column names."""
    return [[c.name for c in columns] + list(key)]


def lime_label(survey: Survey, col: Column) -> str:
    """The 'full' heading: the question text, then the subquestion and scale."""
    if col.part == "metadata":
        return str(col.import_extra.get("label", col.name))
    q = survey.questions.get(col.question_id or "")
    if q is None:
        return col.name
    label = q.text.plain.split("\n")[0]
    sub = None
    for attr in ("choices", "rows", "fields", "items"):
        for c in getattr(q, attr, None) or []:
            if c.id in (col.choice_id, col.row_id):
                sub = c.text.plain
    if col.part == "text":
        sub = {"other": "Other", "-oth-": "Other", "comment": "Comment"}.get(
            col.choice_id or "", f"{sub} (comment)" if sub else "Comment"
        )
    if sub:
        label = f"{label} [{sub}]"
    if isinstance(q, SideBySideQuestion) and col.answer_id is not None:
        label += f" [Scale {int(col.answer_id) + 1}]"
    return label


# --------------------------------------------------------------------------- headers


def infer_lime_options(names: Sequence[str], survey: Survey | None = None) -> LimeColumnOptions:
    """The options that produce a real export's header (its column names)."""
    import re

    answer = [n for n in names if n not in {m for m, _, _ in METADATA} | {"quota_exit"}]
    if any(re.fullmatch(r"\d+X\d+X\d+.*", n) for n in answer):
        heading, naming = "field", "sgqa"
    elif any(re.fullmatch(r"Q\d+(_[SC].*)?(#\d)?", n) for n in answer) and (
        survey is None or not any(q == n for q in survey.questions for n in answer[:1])
    ):
        heading, naming = "field", "qid"
    elif any("[" in n for n in answer):
        heading, naming = "code", "auto"
    else:
        heading, naming = "em", "auto"
    return LimeColumnOptions(
        heading=heading,  # type: ignore[arg-type]
        naming=naming,  # type: ignore[arg-type]
        token="token" in names,
        quota_exit="quota_exit" in names,
        metadata="id" in names,
    )


def read_lime_csv(source: str | Path | io.TextIOBase) -> tuple[list[str], list[dict[str, str]]]:
    """(column names, rows) of a LimeSurvey CSV export (one header row; LimeSurvey
    writes ``;`` or ``,`` separators, both are read)."""
    text = (
        Path(source).read_text(encoding="utf-8-sig")
        if isinstance(source, (str, Path))
        else (
            source.read()  # type: ignore[union-attr]
        )
    )
    first = text.split("\n", 1)[0]
    delim = ";" if first.count(";") > first.count(",") else ","
    reader = csv.reader(io.StringIO(text), delimiter=delim)
    header = next(reader, [])
    rows = [dict(zip(header, r, strict=False)) for r in reader if r]
    return header, rows


# --------------------------------------------------------------------------- writers


def write_lime_csv(
    survey: Survey,
    runs: Iterable[RespondentRun],
    path: str | Path,
    *,
    options: LimeColumnOptions | None = None,
    keys: Mapping[str, Sequence[Any]] | None = None,
) -> Path:
    """Write ``runs`` as a LimeSurvey CSV export (comma separated, one header row)."""
    runs = list(runs)
    keys = dict(keys or {})
    columns = lime_columns(survey, options)
    path = Path(path)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerows(lime_header(survey, columns, list(keys)))
        for i, run in enumerate(runs):
            key = {name: values[i] for name, values in keys.items()}
            w.writerow([v for _, v in lime_cells(run, survey, options, key=key)])
    return path


def write_lsr(
    survey: Survey,
    runs: Iterable[RespondentRun],
    path: str | Path,
    *,
    naming: Literal["auto", "sgqa", "qid"] = "auto",
) -> Path:
    """Write ``runs`` as a LimeSurvey response file (``.lsr``), which LimeSurvey can
    import into the survey ("Import responses from a deactivated survey table" or
    inside an ``.lsa`` archive). Field names are database names (``naming``)."""
    from xml.sax.saxutils import escape

    options = LimeColumnOptions(heading="field", naming=naming)
    columns = lime_columns(survey, options)
    # the response table's own order: token follows id
    columns.sort(key=lambda c: 0 if c.name == "id" else 1 if c.name == "token" else 2)
    version = survey.source.format_version or ""

    def tag(name: str) -> str:
        n = name.replace("#", "-")
        return f"_{n}" if n[:1].isdigit() else n

    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        "<document>",
        " <LimeSurveyDocType>Responses</LimeSurveyDocType>",
        f" <DBVersion>{escape(version)}</DBVersion>",
        " <languages>",
        f"  <language>{escape(survey.language or 'en')}</language>",
        " </languages>",
        " <responses>",
        "  <fields>",
        *[f"   <fieldname>{escape(c.name)}</fieldname>" for c in columns],
        "  </fields>",
        "  <rows>",
    ]
    for run in runs:
        lines.append("   <row>")
        cells = dict((c.name, v) for c, v in lime_cells(run, survey, options))
        for col in columns:
            value = cells.get(col.name, "")
            if value == "" or value is None:
                continue  # NULL: left out, as LimeSurvey writes it
            text = str(value).replace("]]>", "]] >")
            lines.append(f"    <{tag(col.name)}><![CDATA[{text}]]></{tag(col.name)}>")
        lines.append("   </row>")
    lines += ["  </rows>", " </responses>", "</document>", ""]
    path = Path(path)
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


# --------------------------------------------------------------------------- reading data


def row_answers(
    survey: Survey, row: Mapping[str, Any], options: LimeColumnOptions | None = None
) -> dict[tuple[str, str | None], Any]:
    """The answers one exported response holds, as ``(question id, None) -> Answer``
    (the inverse of ``lime_cells``). Equations are left out: the runtime computes
    them. ``options`` names the row's columns (``infer_lime_options`` reads them
    off a header)."""
    from .runtime.state import Answer

    options = options or LimeColumnOptions()
    by_question: dict[str, list[tuple[Column, Any]]] = {}
    for col in lime_columns(survey, options):
        if col.question_id is None or col.name not in row:
            continue
        v = row[col.name]
        by_question.setdefault(col.question_id, []).append((col, "" if v is None else v))
    out: dict[tuple[str, str | None], Any] = {}
    for qid, cells in by_question.items():
        q = survey.questions.get(qid)
        if q is None or isinstance(q, EquationQuestion):
            continue
        answer = _answer(q, cells)
        if answer is not None:
            value, text = answer
            out[(qid, None)] = Answer(value=value, text=text)
    return out


def _answer(q: Question, cells: list[tuple[Column, Any]]) -> tuple[Any, dict[str, str]] | None:
    text = {c.choice_id or "": str(v) for c, v in cells if c.part == "text" and str(v) != ""}
    main = [(c, str(v)) for c, v in cells if c.part in ("response", "file")]
    filled = [(c, v) for c, v in main if v != ""]
    if not filled and not text:
        return None
    if isinstance(q, ChoiceQuestion):
        if q.multiple:
            chosen = [c.choice_id for c, v in filled if v == "Y" and c.choice_id]
            if "other" in text:
                chosen.append("other")
            return chosen, text
        value = filled[0][1] if filled else None
        return value, text
    if isinstance(q, MatrixQuestion):
        if q.mode == "text":
            grid: dict[str, dict[str, str]] = {}
            for c, v in filled:
                grid.setdefault(c.row_id or "", {})[c.answer_id or ""] = v
            return grid, text
        return {c.row_id or "": v for c, v in filled}, text
    if isinstance(q, SideBySideQuestion):
        scales: dict[str, dict[str, str]] = {}
        for c, v in filled:
            scales.setdefault(f"{q.id}#{c.answer_id}", {})[c.row_id or ""] = v
        return scales, text
    if isinstance(q, TextEntryQuestion):
        if q.mode == "form":
            return {c.choice_id or "": v for c, v in filled}, text
        return (filled[0][1] if filled else None), text
    if isinstance(q, RankOrderQuestion):
        return {v: int(c.choice_id or 0) for c, v in filled}, text
    if q.origin.type == "|":
        files = [v for c, v in filled if c.part == "response"]
        return (files[0] if files else None), text
    return (filled[0][1] if filled else None), text


def _lsr(doc: Any) -> tuple[list[dict[str, Any]], list[str]]:
    """Rows and field names of a response file; the writer spells ``#`` as ``-``."""
    rows = [{k.replace("-", "#"): v for k, v in r.items()} for r in doc.rows("responses")]
    return rows, [h.replace("-", "#") for h in doc.fields.get("responses", [])]


def read_responses(
    source: str | Path, survey: Survey, options: LimeColumnOptions | None = None
) -> list[Any]:
    """Real responses as ``replay.Recording``s (answers, start language as ``LANG``
    and the token, by response): from an ``.lsa`` archive, an ``.lsr`` response file
    or a CSV export (comma or semicolon separated).

    LimeSurvey does not export the order it presented things in, only the seed
    that drove it, so recordings hold no orders: replay them with the order kinds as
    ``fallback_kinds`` (``Replayer(survey, fallback_kinds=("block", "choices",
    "columns", "flow"))``) or allow ``replay.gap``.
    """
    from .loaders.limesurvey.xmlfile import parse_document, read_archive
    from .runtime.replay import Recording

    path = Path(source)
    data = path.read_bytes()
    rows: Sequence[Mapping[str, Any]]
    if data[:2] == b"PK":
        archive = read_archive(data)
        if archive.responses is None:
            return []
        rows, header = _lsr(archive.responses)
    elif data.lstrip(b"\xef\xbb\xbf \r\n\t").startswith(b"<"):
        rows, header = _lsr(parse_document(data))
    else:
        header, rows = read_lime_csv(path)
    options = options or infer_lime_options(header, survey)
    # an unfinished response has no submit date (NULL, left out of .lsr rows)
    dated = "submitdate" in header
    out = []
    for r in rows:
        embedded = {}
        if r.get("startlanguage"):
            embedded["LANG"] = str(r["startlanguage"])
        if r.get("token"):
            embedded["token"] = str(r["token"])
        seed = r.get("seed")
        out.append(
            Recording(
                orders={},
                answers=row_answers(survey, r, options),
                embedded=embedded,
                seed=int(seed) if isinstance(seed, str) and seed.isdigit() else None,
                id=str(r["id"]) if r.get("id") not in (None, "") else None,
                finished=bool(r.get("submitdate")) if dated else None,
            )
        )
    return out
