"""LimeSurvey response columns, CSV and .lsr output, and replay of real responses."""

from __future__ import annotations

import csv
from pathlib import Path

import pytest

import surveyir
from surveyir import RandomAnswerer, Simulator, write_responses_csv
from surveyir.columns_limesurvey import (
    LimeColumnOptions,
    infer_lime_options,
    lime_columns,
    read_responses,
    write_lsr,
)
from surveyir.loaders.limesurvey import load_lss
from surveyir.loaders.limesurvey.xmlfile import parse_document, read_archive
from surveyir.runtime.replay import Replayer

FIX = Path(__file__).parent / "fixtures" / "limesurvey"
ORDER_KINDS = ("block", "choices", "columns", "flow")


@pytest.fixture(scope="module")
def types():
    return load_lss(FIX / "types_v6.lss", strict=True)


CODE_HEADINGS = [
    "id",
    "submitdate",
    "lastpage",
    "startlanguage",
    "seed",
    "colour",
    "colour[other]",
    "country",
    "brand",
    "brand[comment]",
    "five",
    "gender",
    "agree",
    "pets[SQ001]",
    "pets[SQ002]",
    "pets[SQ003]",
    "pets[SQ004]",
    "pets[other]",
    "media[SQ001]",
    "media[SQ001comment]",
    "media[SQ002]",
    "media[SQ002comment]",
    "rate[SQ001]",
    "rate[SQ002]",
    "bycol[SQ001]",
    "bycol[SQ002]",
    "a5[SQ001]",
    "b10[SQ001]",
    "ync[SQ001]",
    "isd[SQ001]",
    "dual[SQ001][1]",
    "dual[SQ001][2]",
    "dual[SQ002][1]",
    "dual[SQ002][2]",
    "nums[R1_C1]",
    "nums[R1_C2]",
    "nums[R2_C1]",
    "nums[R2_C2]",
    "texts[R1_C1]",
    "alloc[SQ001]",
    "alloc[SQ002]",
    "names[SQ001]",
    "names[SQ002]",
    "short",
    "long",
    "huge",
    "age",
    "when",
    "rank[1]",
    "rank[2]",
    "rank[3]",
    "info",
    "score",
    "upload",
    "upload[filecount]",
]


def test_code_headings(types):
    assert [c.name for c in lime_columns(types)] == CODE_HEADINGS


def test_database_names(types):
    sgqa = [c.name for c in lime_columns(types, LimeColumnOptions(heading="field"))]
    assert sgqa[5:8] == ["111111X1X101", "111111X1X101other", "111111X1X102"]
    assert "111111X2X207SQ001#1" in sgqa and "111111X3X3082" in sgqa
    assert "111111X3X311_filecount" in sgqa
    v7 = [c.name for c in lime_columns(types, LimeColumnOptions(heading="field", naming="qid"))]
    assert "quota_exit" in v7
    assert "Q101_Cother" in v7 and "Q107_S1071" in v7 and "Q108_S1081_Ccomment" in v7
    assert "Q207_S2071#1" in v7 and "Q208_S2081_S2083" in v7 and "Q311_Cfilecount" in v7
    em = [c.name for c in lime_columns(types, LimeColumnOptions(heading="em"))]
    assert "pets_SQ001" in em and "dual_SQ001_1" in em


def test_version_seven_names_by_default():
    v7 = load_lss(FIX / "types_v7.lss")
    names = [c.name for c in lime_columns(v7, LimeColumnOptions(heading="field"))]
    assert "Q308_S3081" in names  # ranking slots are named by subquestion


def test_metadata_follows_survey_settings():
    logic = load_lss(FIX / "logic.lss")
    names = [c.name for c in lime_columns(logic)]
    assert names[:6] == ["id", "submitdate", "lastpage", "startlanguage", "seed", "token"]


def test_simulated_csv_and_lsr(types, tmp_path):
    runs = Simulator(types, seed=2, strict=False).run(4, RandomAnswerer(seed=5))
    path = write_responses_csv(types, runs, tmp_path / "out.csv")
    rows = list(csv.reader(path.open()))
    assert rows[0] == CODE_HEADINGS and len(rows) == 5
    assert rows[1][0] == "1" and rows[1][1] == "1980-01-01 00:00:00"
    lsr = write_lsr(types, runs, tmp_path / "out.lsr")
    doc = parse_document(lsr.read_bytes())
    assert doc.doctype == "Responses" and len(doc.rows("responses")) == 4
    assert doc.fields["responses"][:2] == ["id", "submitdate"]


def test_simulated_runs_replay_from_their_own_export(types, tmp_path):
    runs = Simulator(types, seed=2, strict=False).run(6, RandomAnswerer(seed=5))
    path = write_responses_csv(types, runs, tmp_path / "out.csv")
    recordings = read_responses(path, types)
    assert len(recordings) == 6
    replayer = Replayer(types, strict=False, fallback_kinds=ORDER_KINDS)
    for run, rec in zip(runs, recordings, strict=True):
        again = replayer.respondent(rec.orders, rec.answers, embedded=rec.embedded)
        for key, answer in run.answers.items():
            value = answer.value
            if isinstance(value, list) and "other" in value and not answer.text.get("other"):
                value = [v for v in value if v != "other"]  # "Other" is ticked by typing
            if _norm(value) in ("", None, [], {}):
                continue  # "No answer" is exported empty, like a question not answered
            assert _norm(again.answers[key].value) == _norm(value), key


def _norm(value):
    """What an export can hold: no empty ("No answer") cells, ticks in no order."""
    if isinstance(value, dict):
        out = {k: _norm(v) for k, v in value.items()}
        return {k: v for k, v in out.items() if v not in ("", None, {})}
    if isinstance(value, list):  # Y/N columns do not keep the order things were ticked
        return sorted(value)
    return value


EXPECTED = {
    "colour": "A2",
    "country": "FR",
    "brand": "B1",
    "pets": ["SQ001", "SQ003", "other"],
    "media": ["SQ001"],
    "rate": {"SQ001": "A3", "SQ002": "A1"},
    "dual": {"dual#0": {"SQ001": "A2"}, "dual#1": {"SQ001": "B1"}},
    "nums": {"R1": {"C1": "3"}, "R2": {"C2": "7"}},
    "alloc": {"SQ001": "40", "SQ002": "60"},
    "names": {"SQ001": "Ada", "SQ002": "Lovelace"},
    "age": "36",
    "rank": {"R3": 1, "R1": 2, "R2": 3},
}


@pytest.mark.parametrize("source", ["responses.lsa", "responses.csv"])
def test_real_responses_read(types, source):
    recs = read_responses(FIX / source, types)
    assert len(recs) == 3
    first = {k[0]: a.value for k, a in recs[0].answers.items()}
    for qid, value in EXPECTED.items():
        assert first[qid] == value, qid
    assert recs[0].answers[("pets", None)].text == {"other": "Parrot"}
    assert recs[0].answers[("brand", None)].text == {"comment": "cheap"}
    assert recs[1].answers[("colour", None)].text == {"-oth-": "Purple"}
    assert recs[1].embedded == {"LANG": "de"}
    assert "score" not in first  # equations are computed, not replayed


def test_lsa_and_csv_agree(types):
    a = read_responses(FIX / "responses.lsa", types)
    b = read_responses(FIX / "responses.csv", types)
    assert [r.answers for r in a] == [r.answers for r in b]


def test_real_responses_replay(types):
    recs = read_responses(FIX / "responses.lsa", types)
    replayer = Replayer(types, strict=False, fallback_kinds=ORDER_KINDS)
    run = replayer.respondent(recs[0].orders, recs[0].answers, embedded=recs[0].embedded)
    for key, answer in recs[0].answers.items():
        assert run.answers[key].value == answer.value
    assert run.answers[("score", None)].value == "37"  # recomputed from the replayed age
    archive = read_archive((FIX / "responses.lsa").read_bytes())
    assert archive.responses is not None
    assert archive.responses.rows("responses")[0]["111111X3X310"] == "37"  # as recorded


def test_infer_options_from_headers(types):
    assert infer_lime_options(CODE_HEADINGS).heading == "code"
    sgqa = [c.name for c in lime_columns(types, LimeColumnOptions(heading="field"))]
    opts = infer_lime_options(sgqa)
    assert (opts.heading, opts.naming) == ("field", "sgqa")


def test_response_columns_dispatch(types):
    cols = surveyir.response_columns(types)
    assert [c.name for c in cols] == CODE_HEADINGS
    assert surveyir.column_label(types, cols[5]) == "Favourite colour?"
    header = surveyir.response_header_rows(types)
    assert header == [CODE_HEADINGS]
