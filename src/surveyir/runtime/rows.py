"""Turn a simulated respondent into one row of a Qualtrics-style export.

Values follow Qualtrics' numeric export: recode values for choices and scale
points, 1/blank for split multi-select columns, display positions (1-based,
blank if not shown) in split display-order columns, pipe-joined ids in single
display-order columns. Questions the respondent never saw are blank.
"""

from __future__ import annotations

import csv
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..columns import Column, ColumnOptions, key_columns, response_columns, response_header_rows
from ..model import (
    ChoiceQuestion,
    ConstantSumQuestion,
    DrillDownQuestion,
    GraphicSliderQuestion,
    HighlightQuestion,
    HotSpotQuestion,
    MatrixQuestion,
    PickGroupRankQuestion,
    Question,
    RankOrderQuestion,
    SideBySideQuestion,
    SliderQuestion,
    Survey,
    TextEntryQuestion,
)
from .state import Answer

if TYPE_CHECKING:
    from .walker import RespondentRun


def _numeric(value: Any) -> Any:
    """Qualtrics exports a choice's id as its value when it has no recode value."""
    return int(value) if isinstance(value, str) and value.isdigit() else value


def _code(q: Question, choice_id: str) -> Any:
    for attr in ("choices", "items", "points"):
        for c in getattr(q, attr, None) or []:
            if c.id == choice_id:
                return c.recode if c.recode is not None else _numeric(c.id)
    return _numeric(choice_id)


def _scale_code(q: MatrixQuestion, row_id: str, answer_id: str) -> Any:
    for a in q.row_columns.get(row_id) or q.columns:
        if a.id == answer_id:
            return a.recode if a.recode is not None else _numeric(a.id)
    return _numeric(answer_id)


def _position(order: list[str], key: str) -> Any:
    return order.index(key) + 1 if key in order else ""


def _question_value(q: Question, col: Column, answer: Answer | None, order: list[str]) -> Any:
    if col.part == "display_order":
        if col.choice_id is None:
            return "|".join(order)
        if isinstance(q, MatrixQuestion):  # keys are row positions in definition order
            rows = [r.id for r in q.rows]
            pos = int(col.choice_id) - 1
            return _position(order, rows[pos]) if 0 <= pos < len(rows) else ""
        return _position(order, col.choice_id)
    if answer is None:
        return ""
    value = answer.value
    if col.part == "text":
        key = col.choice_id or col.row_id or ""
        return answer.text.get(key, "")
    if isinstance(q, ChoiceQuestion):
        chosen = [str(v) for v in (value if isinstance(value, list) else [value]) if v is not None]
        if col.choice_id is not None:  # split multi-select
            return 1 if col.choice_id in chosen else ""
        codes = [_code(q, c) for c in chosen]
        return codes[0] if len(codes) == 1 else ",".join(str(c) for c in codes)
    if isinstance(q, MatrixQuestion) and isinstance(value, dict):
        cell = value.get(col.row_id)
        if q.mode == "text":
            return (cell or {}).get(col.answer_id, "") if isinstance(cell, dict) else ""
        if q.mode == "multiple":
            return 1 if col.answer_id in (cell or []) else ""
        return _scale_code(q, col.row_id or "", cell) if cell is not None else ""
    if isinstance(q, TextEntryQuestion):
        if isinstance(value, dict):
            return value.get(col.choice_id, "")
        return value
    if isinstance(q, (SliderQuestion, ConstantSumQuestion, RankOrderQuestion)) and isinstance(
        value, dict
    ):
        return value.get(col.choice_id, "")
    if isinstance(q, GraphicSliderQuestion):
        return _code(q, str(value))
    if isinstance(q, DrillDownQuestion) and isinstance(value, dict):
        return value.get(col.choice_id, "")
    if isinstance(q, HotSpotQuestion) and isinstance(value, dict):
        return value.get(col.choice_id, "")
    if isinstance(q, HighlightQuestion) and isinstance(value, dict):
        words = value.get(col.answer_id, [])
        if col.choice_id is not None:
            return col.choice_id if col.choice_id in words else ""
        return ",".join(words)
    if isinstance(q, PickGroupRankQuestion) and isinstance(value, dict):
        return ""  # placement columns: not generated yet
    return ""


def to_row(
    run: RespondentRun,
    survey: Survey,
    options: ColumnOptions | None = None,
    *,
    key: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Values keyed by column name.

    Surveys can reuse an export tag, which makes two columns share a name (Qualtrics
    then writes both under the same header). A dict cannot hold both, so use
    ``to_cells`` when the exact export layout matters.
    """
    return {col.name: value for col, value in to_cells(run, survey, options, key=key)}


def to_cells(
    run: RespondentRun,
    survey: Survey,
    options: ColumnOptions | None = None,
    *,
    key: Mapping[str, Any] | None = None,
) -> list[tuple[Column, Any]]:
    """(column, value) for every ``response_columns(survey, options)`` column, in order.

    ``key`` appends external key columns after them, in order: ``{"TWIN_ID": "T17"}``
    adds a ``TWIN_ID`` column (``part == "key"``) holding ``"T17"``, so simulated
    rows can be merged with a human export on it."""
    options = options or ColumnOptions()
    row: list[tuple[Column, Any]] = []
    questions = dict(survey.questions)
    for q in list(questions.values()):
        if isinstance(q, SideBySideQuestion):
            for sub in q.questions:
                questions[sub.id] = sub
    for col in response_columns(survey, options=options):
        value: Any = ""
        if col.part == "metadata":
            value = {
                "Progress": 100 if run.finished else "",
                "Finished": 1 if run.finished else 0,
                "ResponseId": f"R_sim{run.index:06d}",
                "DistributionChannel": "simulation",
            }.get(col.name, "")
        elif col.part == "embedded_data":
            value = run.embedded.get(col.name, "")
        elif col.part == "display_order" and col.question_id is None:
            if col.import_id.startswith("FL_"):
                order = run.flow_order.get(col.import_id[:-3], [])
            else:
                block_id = col.import_id[:-3]
                order = [
                    questions[q].export_tag
                    for q in run.block_order.get((block_id, None), [])
                    if q in questions
                ]
            value = "|".join(order) if col.choice_id is None else _position(order, col.choice_id)
        elif col.question_id is not None:
            base_qid = col.question_id.split("#")[0]
            q = questions.get(col.question_id) or questions.get(base_qid)
            if q is not None:
                where = (base_qid, col.loop)
                if where in run.state.displayed:
                    answer = run.answers.get(where)
                    if q.id != base_qid and answer is not None and isinstance(answer.value, dict):
                        answer = Answer(value=answer.value.get(q.id))  # side-by-side column
                    order = run.choice_order.get(where, [])
                    value = _question_value(q, col, answer, order)
        row.append((col, value))
    if key:
        row.extend(zip(key_columns(key), key.values(), strict=True))
    return row


def write_responses_csv(
    survey: Survey,
    runs: Iterable[RespondentRun],
    path: str | Path,
    *,
    options: ColumnOptions | None = None,
    keys: Mapping[str, Sequence[Any]] | None = None,
) -> Path:
    """Write ``runs`` as a Qualtrics-shaped CSV export: the three header rows of
    ``response_header_rows`` (names, labels, ImportIds), then one row per run.

    ``keys`` maps extra column names to one value per run (in the order of
    ``runs``), appended after the response columns: e.g. ``{"TWIN_ID": ids}`` to
    merge simulated respondent ``i`` with human respondent ``ids[i]``. Returns the path.
    """
    runs = list(runs)
    keys = dict(keys or {})
    for name, values in keys.items():
        if len(values) != len(runs):
            raise ValueError(f"keys[{name!r}] has {len(values)} values for {len(runs)} runs")
    columns = response_columns(survey, options=options)
    path = Path(path)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerows(response_header_rows(survey, columns, key=list(keys)))
        for i, run in enumerate(runs):
            key = {name: values[i] for name, values in keys.items()}
            w.writerow([v for _, v in to_cells(run, survey, options, key=key)])
    return path
