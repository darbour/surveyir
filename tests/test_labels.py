"""Export header labels (second header row), key columns and the CSV writer.

The label rules were read off real Qualtrics exports. The committed fixtures hold
only column names and ImportIds, so the label check against real exports runs
only when ``SURVEYIR_TWIN_DAT`` points at the Twin-2K-500 mega-study ``.dat``
directory; it reads the three header rows of each ``response.csv`` and nothing
else, and stores nothing.
"""

from __future__ import annotations

import csv
import json
import os
from collections import Counter
from pathlib import Path

import pytest

from surveyir import (
    ColumnOptions,
    column_label,
    infer_options,
    load_qsf,
    read_header,
    response_columns,
    response_header_rows,
    write_responses_csv,
)
from surveyir.columns import _label_text
from surveyir.runtime import Simulator
from tests.conftest import FIXTURES

TF = FIXTURES / "targeting_fairness.qsf"


@pytest.mark.parametrize(
    ("source", "label"),
    [
        # <br> is a newline; source newlines and spaces are kept; other tags vanish
        (
            "For the next questions.\n\n<br><br>Increased use",
            "For the next questions.\n\n\n\nIncreased use",
        ),
        ("<div><b>Two wardrobes.&nbsp;</b></div><div>&nbsp;</div>", "Two wardrobes."),
        ("A&nbsp;<b>bold</b> &rsquo;claim&rsquo;", "A bold ’claim’"),
        # Loop & Merge pipes are written as [Field-n]
        (
            "Idea #${lm://CurrentLoopNumber}:<br>\n${lm://Field/3}",
            "Idea #[CurrentLoopNumber]:\n\n[Field-3]",
        ),
        ("&nbsp;", ""),
    ],
)
def test_label_text_follows_qualtrics(source, label):
    assert _label_text(source) == label


def test_header_rows_for_targeting_fairness():
    survey = load_qsf(TF)
    opts = ColumnOptions(include_metadata=True)
    names, labels, imports = response_header_rows(survey, options=opts, key=["TWIN_ID"])
    by_name = dict(zip(names, labels, strict=True))
    assert by_name["StartDate"] == "Start Date" and by_name["RecordedDate"] == "Recorded Date"
    assert by_name["fair1"] == "How fair is this advertising plan?"
    assert by_name["Q41_First Click"] == "Timing - First Click"
    assert by_name["FL_491_DO_FL_493"] == "FL_491 - Block Randomizer - Display Order - FL_493"
    assert by_name["Segment"] == "Segment"
    assert names[-1] == by_name["TWIN_ID"] == "TWIN_ID"
    # ImportIds as Qualtrics writes them: compact JSON
    assert imports[-1] == '{"ImportId":"TWIN_ID"}'
    assert '{"ImportId":"FL_491_DO","choiceId":"FL_493"}' in imports
    # names and ImportIds as in the committed real export (made with its own settings)
    real = json.loads((FIXTURES / "targeting_fairness.columns.json").read_text())
    real_header = [(c["column"], c["import_id"]) for c in real]
    names, _, imports = response_header_rows(survey, options=infer_options(real_header))
    ours = {n: json.loads(i) for n, i in zip(names, imports, strict=True)}
    # it omits some metadata, and also has a single-column FL_491_DO and the TWIN_ID the
    # mega-study added
    assert {n for n, obj in real_header if ours.get(n) != obj} == {"FL_491_DO", "TWIN_ID"}


def test_choice_and_matrix_sub_labels():
    from tests.conftest import mc, minimal_qsf

    multi = mc(
        "QID1",
        QuestionText="<b>Which apply?</b>",
        Selector="MAVR",
        Choices={
            "1": {"Display": "Red <i>wine</i>"},
            "2": {"Display": "Other", "TextEntry": "true"},
        },
        ChoiceOrder=[1, 2],
        Randomization={"Type": "All"},
    )
    matrix = {
        "QuestionID": "QID2",
        "QuestionType": "Matrix",
        "Selector": "Likert",
        "SubSelector": "SingleAnswer",
        "DataExportTag": "Q2",
        "QuestionText": "Rate",
        "Choices": {"1": {"Display": "Row <b>one</b>"}},
        "ChoiceOrder": [1],
        "Answers": {"1": {"Display": "Low"}, "2": {"Display": "High"}},
        "AnswerOrder": [1, 2],
        "Randomization": {"Type": "All"},
    }
    survey = load_qsf(minimal_qsf([multi, matrix]))
    labels = {c.name: column_label(survey, c) for c in response_columns(survey)}
    # choice text follows as written in the source (markup kept), "Selected Choice"
    # when the question has text-entry choices
    assert labels["Q1_1"] == "Which apply? - Selected Choice - Red <i>wine</i>"
    assert labels["Q1_2_TEXT"] == "Which apply? - Other - Text"
    assert labels["Q1_DO_1"] == "Which apply? - Display Order - Red <i>wine</i>"
    # matrix rows of response columns are flattened; of display-order columns not
    assert labels["Q2_1"] == "Rate - Row one"
    assert labels["Q2_DO_1"] == "Rate - Display Order - Row <b>one</b>"


def test_key_columns_and_csv_writer(tmp_path):
    survey = load_qsf(TF)
    runs = Simulator(survey, seed=1, strict=False).run(3)
    cells = runs[0].cells(survey, key={"TWIN_ID": "T0"})
    col, value = cells[-1]
    assert (col.name, col.part, col.import_object, value) == (
        "TWIN_ID",
        "key",
        {"ImportId": "TWIN_ID"},
        "T0",
    )
    assert [c for c, _ in cells[:-1]] == response_columns(survey)
    assert runs[0].row(survey, key={"TWIN_ID": "T0"})["TWIN_ID"] == "T0"

    path = write_responses_csv(
        survey, runs, tmp_path / "sim.csv", keys={"TWIN_ID": ["a", "b", "c"]}
    )
    with open(path, newline="", encoding="utf-8") as f:
        rows = list(csv.reader(f))
    assert rows[:3] == response_header_rows(survey, key=["TWIN_ID"])
    assert [r[-1] for r in rows[3:]] == ["a", "b", "c"]
    assert rows[3][:-1] == [str(v) for _, v in runs[0].cells(survey)]
    header = read_header(path.read_text(encoding="utf-8"))
    assert [n for n, _ in header] == rows[0]
    with pytest.raises(ValueError, match="2 values for 3 runs"):
        write_responses_csv(survey, runs, tmp_path / "x.csv", keys={"TWIN_ID": ["a", "b"]})


# ------------------------------------------------------------------ real exports (env-gated)

DAT = os.environ.get("SURVEYIR_TWIN_DAT")
#: columns whose label is known not to match: idea_generation's QID34 text has
#: ``<div><br></div>``, which Qualtrics writes with whitespace not reproduced here
KNOWN_MISSES = {("idea_generation", "QID34")}


def _key(obj: dict) -> str:
    return json.dumps(
        {k: str(v) if k == "choiceId" else v for k, v in obj.items() if k != "timeZone"},
        sort_keys=True,
    )


@pytest.mark.skipif(not DAT, reason="SURVEYIR_TWIN_DAT not set (human exports not available)")
def test_labels_match_real_exports():
    """Every label paired by ImportId with a real export's second header row is
    reproduced exactly, except KNOWN_MISSES."""
    assert DAT is not None
    studies = sorted(p for p in Path(DAT).expanduser().iterdir() if (p / "raw_data").is_dir())
    counts: Counter[tuple[str, bool]] = Counter()
    misses = []
    for study in studies:
        response = study / "raw_data" / "response.csv"
        if not response.exists():
            continue
        with open(response, encoding="utf-8-sig", newline="") as f:
            reader = csv.reader(f)
            head = [next(reader), next(reader), next(reader)]  # header rows only
        header = [(name, _import_object(cell)) for name, cell in zip(head[0], head[2], strict=True)]
        labels = {_key(obj): label for (_, obj), label in zip(header, head[1], strict=True)}
        survey = load_qsf(study / "raw_data" / "survey.qsf")
        for col in response_columns(survey, options=infer_options(header)):
            want = labels.get(_key(col.import_object))
            if want is None:
                continue
            q = (col.question_id or "").split("#")[0]
            ok = column_label(survey, col) == want
            counts[(col.part, ok)] += 1
            if not ok and (study.name, q) not in KNOWN_MISSES:
                misses.append((study.name, col.name))
    assert sum(counts.values()) > 2500
    assert misses == []


def _import_object(cell: str) -> dict:
    try:
        obj = json.loads(cell)
    except json.JSONDecodeError:
        return {"ImportId": cell}
    return obj if isinstance(obj, dict) else {"ImportId": str(obj)}
