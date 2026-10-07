"""A data codebook: one row per response column, with value labels."""

from __future__ import annotations

import csv
import io

from ..columns import Column, response_columns
from ..model import ChoiceQuestion, MatrixQuestion, Question, Survey
from .base import Exporter


def _value_labels(q: Question | None, col: Column) -> str:
    if q is None or col.part != "response":
        return ""
    pairs: list[tuple[str, str]] = []
    if isinstance(q, ChoiceQuestion):
        if q.multiple:
            pairs = [("1", "selected")]
        else:
            pairs = [
                (str(c.recode if c.recode is not None else c.id), c.text.plain) for c in q.choices
            ]
    elif isinstance(q, MatrixQuestion) and q.mode == "max_diff" and len(q.columns) == 2:
        # Qualtrics codes the left answer column 1 and the right one 0 (Matrix Table docs).
        pairs = [("1", q.columns[0].text.plain), ("0", q.columns[1].text.plain)]
    elif isinstance(q, MatrixQuestion) and q.mode in ("single", "bipolar", "dropdown"):
        pairs = [(str(a.recode if a.recode is not None else a.id), a.text.plain) for a in q.columns]
    return " | ".join(f"{v}={' '.join(label.split())}" for v, label in pairs)


def _label(q: Question | None, col: Column) -> str:
    if q is None:
        return ""
    for attr in ("choices", "items", "fields", "rows"):
        for item in getattr(q, attr, None) or []:
            if item.id in (col.choice_id, col.row_id):
                return " ".join(item.text.plain.split())
    return ""


class CodebookExporter(Exporter):
    """CSV codebook matching Qualtrics export column names.

    Columns: column, import_id, question_id, export_tag, block, kind, part,
    loop, question_text, item_label, value_labels.
    """

    name = "codebook"
    extension = ".csv"
    media_type = "text/csv"
    summary = "CSV codebook: every response column with labels (Qualtrics naming)."

    def export(self, survey: Survey) -> str:
        block_of = {qid: bid for bid, b in survey.blocks.items() for qid in b.question_ids}
        buf = io.StringIO()
        writer = csv.writer(buf, lineterminator="\n")
        writer.writerow(
            [
                "column",
                "import_id",
                "question_id",
                "export_tag",
                "block",
                "kind",
                "part",
                "loop",
                "question_text",
                "item_label",
                "value_labels",
            ]
        )
        for col in response_columns(survey):
            q = survey.questions.get(col.question_id) if col.question_id else None
            block = survey.blocks.get(block_of.get(col.question_id or "", ""))
            writer.writerow(
                [
                    col.name,
                    col.import_id,
                    col.question_id or "",
                    q.export_tag if q else "",
                    block.description if block else "",
                    q.kind if q else col.part,
                    col.part,
                    col.loop or "",
                    " ".join(q.text.plain.split()) if q else "",
                    _label(q, col),
                    _value_labels(q, col),
                ]
            )
        return buf.getvalue()
