"""Real responses as replay recordings: what each respondent was shown and answered.

``read_responses(path, survey)`` reads a response export into one
``runtime.Recording`` per response: the randomization the respondent got (as
display orders), their answers, and the embedded data supplied from outside the
flow (panel, recipient and URL fields). Replaying a recording (``Replayer``)
walks the survey again with those decisions and answers, so display logic,
branches, skips and embedded data are evaluated as the respondent went.

A Qualtrics CSV export (three header rows, or the legacy two) is read here; a
LimeSurvey export (``.lsa``, ``.lsr`` or CSV) by
``columns_limesurvey.read_responses``.

Reading a Qualtrics export inverts ``runtime.rows`` (the simulated export):

* columns are matched by their ImportId object, so repeated export tags stay
  apart; ``infer_options`` recovers the export settings (split or joined
  multi-value fields, split or single display-order columns);
* display orders come from the ``_DO`` columns: ``FL_<id>_DO`` (which arms of a
  flow randomizer were shown), ``BL_<id>_DO`` (the questions of a randomized
  block, by export tag) and ``<tag>_DO`` (a question's choices, a matrix's rows
  by position), split (one column per item, holding its position) or single
  (``|``-joined);
* answers take the shapes ``state.Answer`` documents: recodes become choice ids,
  slider, constant-sum and rank values become numbers.

What an export does not record is reconstructed or reported, never guessed:

* a flow randomizer's arms, without ``FL_<id>_DO`` columns (or in a LimeSurvey
  export): recovered by searching replays for the decisions the answers and the
  exported embedded fields pin down (``runtime.reconstruct``; ``reconstruct=``);
* a randomized block's questions without its ``_DO`` column, and a randomized
  loop's iterations (Qualtrics never exports loop order): the ones with answers.
  These hold what was shown, not in which order;
* all of these are listed in ``Recording.inferred``; decisions the response
  cannot tell apart are left out and noted in ``Recording.unrecoverable``;
* random numbers the flow draws (``${rand://...}``) are exported as drawn: they
  are kept in ``Recording.drawn`` and a replay takes them (``drawn=``);
* the scale flip is read off a flip-scale question's display order; matrix column
  order is never exported: replay these with ``fallback_kinds=("columns", "flip")``
  (``REPLAY_FALLBACK``);
* heat-map clicks, uploaded files, signatures and pick-group-rank placements are
  not replayed, and are listed in ``Recording.unrecoverable``.

Qualtrics exports no random seed, so ``Recording.seed`` is None.
"""

from __future__ import annotations

import csv
import io
import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from .columns import Column, ColumnOptions, infer_options, response_columns
from .model import (
    ChoiceQuestion,
    ConstantSumQuestion,
    DrillDownQuestion,
    EmbeddedDataNode,
    FileUploadQuestion,
    GraphicSliderQuestion,
    HeatMapQuestion,
    HighlightQuestion,
    HotSpotQuestion,
    MatrixQuestion,
    PickGroupRankQuestion,
    Question,
    RandomizerNode,
    RankOrderQuestion,
    SideBySideQuestion,
    SignatureQuestion,
    SliderQuestion,
    Survey,
    TextEntryQuestion,
    WebServiceNode,
)
from .runtime.replay import Recording, RecordKey
from .runtime.state import Answer
from .runtime.walker import FLIP_OPTIONS

__all__ = [
    "REPLAY_FALLBACK",
    "flow_set_fields",
    "read_qualtrics_responses",
    "read_responses",
]

#: order kinds a Qualtrics export never holds: replay them with these as
#: ``fallback_kinds`` (``Replayer(survey, fallback_kinds=REPLAY_FALLBACK)``)
REPLAY_FALLBACK = ("columns", "flip")

_UNREPLAYED = (FileUploadQuestion, SignatureQuestion, HeatMapQuestion, PickGroupRankQuestion)


def read_responses(
    source: str | Path,
    survey: Survey,
    *,
    options: Any = None,
    finished_only: bool = False,
    reconstruct: bool | Mapping[str, Any] = True,
) -> list[Recording]:
    """Each response of an export of ``survey``, as a ``Recording`` (see the module
    docstring). Dispatches on the survey's format: a Qualtrics CSV export, or a
    LimeSurvey ``.lsa``/``.lsr``/CSV export. ``options`` overrides the export
    settings read off the file (``ColumnOptions`` or ``LimeColumnOptions``).
    ``finished_only`` drops responses the export marks unfinished.

    ``reconstruct`` (default True): flow randomizer decisions the export does not
    record are recovered by searching replays (``runtime.reconstruct``); a mapping
    is passed to it as keyword arguments (``max_replays``, ``implementations``)."""
    if survey.source.format.startswith("limesurvey"):
        from .columns_limesurvey import read_responses as read_lime

        recordings = read_lime(source, survey, options)
    else:
        recordings = read_qualtrics_responses(source, survey, options=options)
    if finished_only:
        recordings = [r for r in recordings if r.finished is not False]
    if reconstruct and _subset_randomizers(survey):
        from .runtime.reconstruct import reconstruct as search

        recordings = [
            search(survey, r, **(reconstruct if isinstance(reconstruct, Mapping) else {}))
            if _subset_randomizers(survey) - {k[1] for k in r.orders if k[0] == "flow"}
            else r
            for r in recordings
        ]
    return recordings


def _subset_randomizers(survey: Survey) -> set[str]:
    """Flow randomizers that present some of their arms: decisions an export must say."""
    return {
        n.id
        for n in survey.walk_flow()
        if isinstance(n, RandomizerNode)
        and n.id
        and n.subset_size
        and n.subset_size < len(n.children)
    }


# --------------------------------------------------------------------------- header


def _canon(obj: Mapping[str, Any]) -> str:
    """A column's ImportId object as a key: time zones are an export setting."""
    return json.dumps({k: str(v) for k, v in obj.items() if k != "timeZone"}, sort_keys=True)


def _read_csv(source: str | Path) -> tuple[list[list[str]], list[list[str]]]:
    """(header rows, data rows) of a Qualtrics CSV export: three header rows when
    the third holds ImportIds, else the legacy two."""
    text = Path(source).read_text(encoding="utf-8-sig")
    rows = list(csv.reader(io.StringIO(text, newline="")))
    n = 3 if len(rows) >= 3 and any("ImportId" in c for c in rows[2]) else 2
    return rows[:n], rows[n:]


def _columns(
    survey: Survey, header: list[list[str]], options: ColumnOptions | None
) -> tuple[list[tuple[int, Column]], list[str]]:
    """The survey's columns present in the export, with their position; and notes.

    Columns are matched on the ImportId object, or on the name in a legacy export
    (repeated names: the first column of that name)."""
    notes: list[str] = []
    imported = _import_objects(header) if len(header) == 3 else []
    if options is None:
        options = infer_options(imported) if imported else ColumnOptions()
    options = options.model_copy(update={"include_metadata": True})
    predicted = response_columns(survey, options=options)
    if imported:
        position = {}
        for i, (_, obj) in enumerate(imported):
            position.setdefault(_canon(obj), i)
        matched = [
            (position[_canon(c.import_object)], c)
            for c in predicted
            if _canon(c.import_object) in position
        ]
    else:
        notes.append("legacy export (no ImportId row): columns matched by name")
        position = {}
        for i, name in enumerate(header[0]):
            position.setdefault(name, i)
        matched = [(position[c.name], c) for c in predicted if c.name in position]
    return matched, notes


def _import_objects(header: list[list[str]]) -> list[tuple[str, dict[str, Any]]]:
    """(name, ImportId object) per column, as ``columns.read_header`` reads them."""
    out = []
    for name, cell in zip(header[0], header[2], strict=False):
        try:
            obj = json.loads(cell)
        except json.JSONDecodeError:
            obj = {"ImportId": cell}
        out.append((name, obj if isinstance(obj, dict) else {"ImportId": str(obj)}))
    return out


# --------------------------------------------------------------------------- values


def _number(value: str) -> Any:
    """An exported number as int or float; text that is not a number is kept."""
    try:
        f = float(value)
    except ValueError:
        return value
    return int(f) if f.is_integer() and "." not in value and "e" not in value.lower() else f


def _decoder(items: Sequence[Any]) -> dict[str, str]:
    """Exported code (recode value, else id) -> id."""
    return {str(c.recode if c.recode is not None else c.id): c.id for c in items}


def _items(survey: Survey, q: Question, attr: str) -> list[Any]:
    """A question's items, with the ones carried forward (``x``-prefixed ids)."""
    from .columns import _carried

    items = list(getattr(q, attr, None) or [])
    ids = {c.id for c in items}
    return items + [c for c in _carried(survey, q) if c.id not in ids]


def _answer(survey: Survey, q: Question, cells: list[tuple[Column, str]]) -> Answer | None:
    """The answer to ``q`` from its non-blank response and text cells."""
    text = {c.choice_id or c.row_id or "": v for c, v in cells if c.part == "text"}
    values = [(c, v) for c, v in cells if c.part == "response"]
    if not values:
        return Answer(value=None, text=text) if text else None
    value: Any
    if isinstance(q, ChoiceQuestion):
        decode = _decoder(_items(survey, q, "choices"))
        if any(c.choice_id is not None for c, _ in values):  # split multi-select
            value = [c.choice_id for c, _ in values if c.choice_id is not None]
        else:
            raw = values[0][1]
            codes = raw.split(",") if q.multiple else [raw]
            ids = [decode.get(x.strip(), x.strip()) for x in codes if x.strip()]
            value = ids if q.multiple else ids[0]
    elif isinstance(q, MatrixQuestion):
        rows: dict[str, Any] = {}
        for c, v in values:
            if not c.row_id:
                continue
            if q.mode == "multiple":
                rows.setdefault(c.row_id, []).append(c.answer_id)
            elif q.mode == "text":
                rows.setdefault(c.row_id, {})[c.answer_id] = v
            else:
                rows[c.row_id] = _decoder(q.row_columns.get(c.row_id) or q.columns).get(v, v)
        value = rows
    elif isinstance(q, TextEntryQuestion):
        if q.mode == "form":
            value = {c.choice_id: v for c, v in values if c.choice_id is not None}
        else:
            value = values[0][1]
    elif isinstance(q, (SliderQuestion, ConstantSumQuestion, RankOrderQuestion)):
        value = {c.choice_id: _number(v) for c, v in values if c.choice_id is not None}
    elif isinstance(q, (DrillDownQuestion, HotSpotQuestion)):
        value = {c.choice_id: v for c, v in values if c.choice_id is not None}
    elif isinstance(q, HighlightQuestion):
        value = {}
        for c, v in values:
            if c.answer_id is None:
                continue
            words = value.setdefault(c.answer_id, [])
            if c.choice_id is not None:  # split: one column per word
                words.append(c.choice_id)
            else:
                words.extend(w for w in v.split(",") if w)
    elif isinstance(q, GraphicSliderQuestion):
        value = _decoder(_items(survey, q, "points")).get(values[0][1], values[0][1])
    else:
        value = values[0][1]
    return Answer(value=value, text=text)


# --------------------------------------------------------------------------- orders


def _order_option(survey: Survey, col: Column, item: str) -> tuple[RecordKey, str] | None:
    """The replay key of a display-order column, and ``item`` as a replay option."""
    if col.question_id is None and col.import_id.startswith("FL_"):
        return ("flow", col.import_id[:-3], None), item
    if col.question_id is None:
        block = survey.blocks.get(col.import_id[:-3])
        if block is None:
            return None
        tags = {
            survey.questions[q].export_tag: q for q in block.question_ids if q in survey.questions
        }
        return ("block", block.id, None), tags.get(item, item)
    q = survey.questions.get(col.question_id)
    if isinstance(q, MatrixQuestion) and col.choice_id is not None:  # split: row positions
        pos = int(item) - 1 if item.isdigit() else -1
        if not 0 <= pos < len(q.rows):
            return None
        item = q.rows[pos].id
    return ("choices", col.question_id, col.loop), item


def _orders(survey: Survey, cells: list[tuple[Column, str]]) -> dict[RecordKey, tuple[str, ...]]:
    """Recorded orders from display-order cells, split or single layout."""
    split: dict[RecordKey, list[tuple[float, str]]] = {}
    seen: set[RecordKey] = set()
    out: dict[RecordKey, tuple[str, ...]] = {}
    for col, value in cells:
        if col.part != "display_order":
            continue
        if col.choice_id is None:  # single: "a|b|c"
            first = _order_option(survey, col, "")
            if first is None:
                continue
            key = first[0]
            items = [x for x in value.split("|") if x] if value else []
            opts = [_order_option(survey, col, x) for x in items]
            out[key] = tuple(o[1] for o in opts if o is not None)
            continue
        found = _order_option(survey, col, col.choice_id)
        if found is None:
            continue
        key, option = found
        seen.add(key)
        if value.strip():
            try:
                split.setdefault(key, []).append((float(value), option))
            except ValueError:
                continue
    for key in seen:
        out[key] = tuple(o for _, o in sorted(split.get(key, [])))
    # an unreached flow randomizer has no decision to replay
    return {k: v for k, v in out.items() if v or k[0] != "flow"}


def _flip(survey: Survey, orders: Mapping[RecordKey, tuple[str, ...]]) -> str | None:
    """The respondent's scale flip, read off a flip-scale question whose choices are
    not otherwise randomized: its display order is the source order, or reversed."""
    for (kind, qid, _), shown in orders.items():
        q = survey.questions.get(qid)
        r = getattr(q, "randomization", None)
        if kind != "choices" or len(shown) < 2 or q is None or isinstance(q, MatrixQuestion):
            continue  # a matrix's flip reverses its columns, which are not exported
        if r is None or not r.flip_scale or r.mode != "none":
            continue
        items = _items(survey, q, "choices") or _items(survey, q, "items")
        source = [c.id for c in items if c.id in shown]
        if list(shown) == source:
            return FLIP_OPTIONS[0]
        if list(shown) == source[::-1]:
            return FLIP_OPTIONS[1]
    return None


# --------------------------------------------------------------------------- embedded


def drawn_fields(survey: Survey) -> set[str]:
    """Embedded fields the flow sets from a random draw (``${rand://...}``)."""
    return {
        f.name
        for node in survey.walk_flow()
        if isinstance(node, EmbeddedDataNode)
        for f in node.fields
        if f.source == "custom"
        and f.value is not None
        and any(p.kind == "random" for p in f.value.pipes)
    }


def flow_set_fields(survey: Survey) -> set[str]:
    """Embedded fields the flow itself sets (to a value, or a random number) or a web
    service returns: their exported value is the final one, which a replay recomputes,
    so they are not supplied as inputs."""
    out: set[str] = set()
    for node in survey.walk_flow():
        if isinstance(node, EmbeddedDataNode):
            out.update(
                f.name
                for f in node.fields
                if f.source in ("custom", "random") and f.value is not None
            )
        elif isinstance(node, WebServiceNode):
            out.update(node.sets_fields)
    return out


# --------------------------------------------------------------------------- reader


def read_qualtrics_responses(
    source: str | Path, survey: Survey, *, options: ColumnOptions | None = None
) -> list[Recording]:
    """Each response of a Qualtrics CSV export of ``survey`` as a ``Recording`` (see
    the module docstring). ``options`` overrides the settings read off the header."""
    header, data = _read_csv(source)
    matched, notes = _columns(
        survey, header, options if isinstance(options, ColumnOptions) else None
    )
    questions: dict[str, Question] = dict(survey.questions)
    for q in survey.questions.values():
        if isinstance(q, SideBySideQuestion):
            for sub in q.questions:
                questions[sub.id] = sub
    set_in_flow = flow_set_fields(survey)
    random_fields = drawn_fields(survey)
    by_meta = {c.import_id: i for i, c in matched if c.part == "metadata"}
    have_do = {
        ("flow", c.import_id[:-3], None)
        if c.import_id.startswith("FL_")
        else ("block", c.import_id[:-3], None)
        for _, c in matched
        if c.part == "display_order" and c.question_id is None
    }
    randomized_blocks = [
        b
        for b in survey.blocks.values()
        if b.randomization is not None and b.randomization.mode != "none"
    ]
    looped_blocks = [
        b
        for b in survey.blocks.values()
        if b.loop is not None
        and b.loop.randomization is not None
        and b.loop.randomization.mode != "none"
    ]
    out: list[Recording] = []
    for row in data:
        if not row:  # an empty line (a row of blank cells is still a response)
            continue
        cells = [(c, row[i] if i < len(row) else "") for i, c in matched]
        per_question: dict[tuple[str, str | None], list[tuple[Column, str]]] = {}
        embedded: dict[str, str] = {}
        outputs: dict[str, str] = {}
        unrecoverable: list[str] = list(notes)
        for c, v in cells:
            if c.part == "embedded_data":
                if c.name in set_in_flow:
                    outputs[c.name] = v
                elif v != "":
                    embedded[c.name] = v
            elif c.question_id is not None and v.strip():
                if c.part in ("response", "text"):
                    per_question.setdefault((c.question_id, c.loop), []).append((c, v))
                elif c.part in ("coordinate", "region", "file"):
                    note = f"{c.question_id}: {c.part} values are not replayed"
                    if note not in unrecoverable:
                        unrecoverable.append(note)
        answers: dict[tuple[str, str | None], Answer] = {}
        for (qid, loop), qcells in per_question.items():
            q = questions.get(qid)
            if q is None:
                continue
            if isinstance(q, _UNREPLAYED):
                unrecoverable.append(f"{qid}: {q.kind} answers are not replayed")
                continue
            a = _answer(survey, q, qcells)
            if a is None:
                continue
            base = qid.split("#")[0]
            if base != qid:  # a side-by-side sub-question: one part of its parent's answer
                parent = answers.setdefault((base, loop), Answer(value={}))
                parent.value[qid] = a.value
                parent.text.update(a.text)
            else:
                answers[(qid, loop)] = a
        answered = set(answers)
        orders = _orders(survey, cells)
        inferred: list[RecordKey] = []
        for b in randomized_blocks:
            key = ("block", b.id, None)
            if key not in have_do and key not in orders:
                orders[key] = tuple(q for q in b.question_ids if (q, None) in answered)
                inferred.append(key)
        for b in looped_blocks:
            loops = list(
                dict.fromkeys(
                    loop for (q, loop) in answers if loop is not None and q in b.question_ids
                )
            )
            orders[("loop", b.id, None)] = tuple(loops)
            inferred.append(("loop", b.id, None))
        flip = _flip(survey, orders)
        if flip is not None:
            orders[("flip", "scale", None)] = (flip,)
        finished = None
        if "finished" in by_meta:
            raw = row[by_meta["finished"]] if by_meta["finished"] < len(row) else ""
            finished = raw.strip() in ("1", "True", "true") if raw.strip() else None
        rid = None
        if "_recordId" in by_meta and by_meta["_recordId"] < len(row):
            rid = row[by_meta["_recordId"]] or None
        out.append(
            Recording(
                orders=orders,
                answers=answers,
                embedded=embedded,
                seed=None,
                id=rid,
                finished=finished,
                inferred=tuple(inferred),
                unrecoverable=tuple(dict.fromkeys(unrecoverable)),
                outputs=outputs,
                drawn={k: v for k, v in outputs.items() if k in random_fields and v != ""},
            )
        )
    return out
