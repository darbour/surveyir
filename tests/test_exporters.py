from __future__ import annotations

import csv
import io
import json

import pytest

from surveyir import Exporter, Survey, export, get_exporter, load_qsf, response_columns
from surveyir.cli import main
from surveyir.model import TextEntryQuestion
from tests.conftest import FIXTURES, mc, minimal_qsf


@pytest.fixture
def survey() -> Survey:
    return load_qsf(
        minimal_qsf([mc("QID1", RecodeValues={"1": "1", "2": "0"}), mc("QID2", selector="MAVR")])
    )


def test_json_is_lossless_and_compact_drops_extras(survey):
    full = json.loads(export(survey, "json"))
    assert Survey.model_validate(full) == survey
    compact = export(survey, "json", compact=True)
    assert '"extras"' not in compact and '"html"' not in compact


def test_markdown(survey):
    md = export(survey, "markdown")
    assert md.startswith("# Test\n")
    assert "#### `Q1` · single choice, vertical" in md
    assert "1. Yes `[1]`" in md


def test_codebook(survey):
    rows = list(csv.DictReader(io.StringIO(export(survey, "codebook"))))
    assert [r["column"] for r in rows] == ["Q1", "Q2_1", "Q2_2"]
    assert rows[0]["value_labels"] == "1=Yes | 0=No"


def test_unknown_option_and_format_fail_loudly(survey):
    with pytest.raises(TypeError):
        get_exporter("codebook", nonsense=True)
    with pytest.raises(ValueError):
        get_exporter("nope")


def test_custom_exporter_subclass(survey):
    class TagList(Exporter):
        name = "tags"

        def export(self, survey):
            return ",".join(q.export_tag for q in survey.iter_questions())

    assert TagList().export(survey) == "Q1,Q2"


def test_text_entry_and_loop_columns():
    te = {
        "QuestionID": "QID3",
        "QuestionType": "TE",
        "Selector": "SL",
        "DataExportTag": "why",
        "QuestionText": "Why?",
    }
    doc = minimal_qsf(
        [te],
        block_options={
            "Looping": "Static",
            "LoopingOptions": {
                "Static": {"1": {"1": "a"}, "2": {"1": "b"}},
                "Randomization": "None",
            },
        },
    )
    survey = load_qsf(doc)
    assert isinstance(survey.questions["QID3"], TextEntryQuestion)
    assert [(c.name, c.import_id) for c in response_columns(survey)] == [
        ("1_why", "1_QID3_TEXT"),
        ("2_why", "2_QID3_TEXT"),
    ]


def test_cli_convert_and_inspect(tmp_path, capsys):
    qsf = FIXTURES / "junk_fees.qsf"
    out = tmp_path / "out.md"
    assert main(["convert", str(qsf), "-t", "markdown", "-o", str(out)]) == 0
    assert out.read_text().startswith("# ")
    assert main(["convert", str(qsf), "-t", "json", "--opt", "compact=true"]) == 0
    assert '"extras"' not in capsys.readouterr().out
    assert main(["inspect", str(qsf), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["questions"] == 133


def test_fetch_script_reads_wide_export_headers():
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "fetch_corpus", FIXTURES.parents[2] / "scripts" / "fetch_corpus.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    wide = 3000
    names = ",".join(f"Q{i}" for i in range(wide))
    labels = ",".join(f'"{"long question text " * 10}{i}"' for i in range(wide))
    ids = ",".join(f'"{{""ImportId"":""QID{i}""}}"' for i in range(wide))
    data = f"{names}\n{labels}\n{ids}\n".encode()
    assert len(data) > 262144
    header = module.export_header(data)
    assert len(header) == wide and header[-1]["import_id"] == {"ImportId": f"QID{wide - 1}"}


def test_codebook_kind_for_non_question_columns():
    flow = [
        {
            "Type": "BlockRandomizer",
            "FlowID": "FL_9",
            "SubSet": 1,
            "Flow": [{"Type": "Block", "ID": "BL_1", "FlowID": "FL_10"}],
        }
    ]
    survey = load_qsf(
        minimal_qsf([mc("QID1")], flow=flow, block_options={"RandomizeQuestions": "RandomizeAll"})
    )
    rows = {r["column"]: r["kind"] for r in csv.DictReader(io.StringIO(export(survey, "codebook")))}
    assert rows["FL_9_DO_Main"] == "display_order" and rows["Main_DO_Q1"] == "display_order"
