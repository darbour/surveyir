"""Invariants checked against every real QSF in tests/fixtures/qualtrics."""

from __future__ import annotations

import json
import re

import pytest

from surveyir import Survey, export, load_qsf, response_columns
from surveyir.exporters import available_exporters
from surveyir.model import BranchNode
from tests.conftest import raw_blocks, raw_flow, raw_questions

# Warnings that reflect real problems in the source files, not loader gaps.
EXPECTED_WARNINGS = {"missing-loop-source"}


@pytest.fixture
def survey(qsf_path) -> Survey:
    return load_qsf(qsf_path)


def test_loads_without_unexpected_diagnostics(survey):
    problems = [
        d
        for d in survey.diagnostics
        if d.level == "error" or (d.level == "warning" and d.code not in EXPECTED_WARNINGS)
    ]
    assert problems == []


def test_every_live_question_is_loaded(survey, raw):
    live = {
        e["QuestionID"]
        for b in raw_blocks(raw)
        for e in b.get("BlockElements", [])
        if e.get("Type") == "Question" and e["QuestionID"] in raw_questions(raw)
    }
    assert set(survey.questions) == live


def test_block_order_and_page_breaks_match_source(survey, raw):
    for b in raw_blocks(raw):
        expected = [
            ("page_break" if e["Type"] == "Page Break" else e["QuestionID"])
            for e in b.get("BlockElements", [])
        ]
        got = [
            ("page_break" if e.type == "page_break" else e.question_id)
            for e in survey.blocks[b["ID"]].elements
        ]
        assert got == expected


def test_choice_ids_are_strings_in_source_order(survey, raw):
    raws = raw_questions(raw)
    for q in survey.questions.values():
        items = getattr(q, "choices", None) or getattr(q, "items", None) or getattr(q, "rows", None)
        if not items:
            continue
        order = [str(x) for x in raws[q.id].get("ChoiceOrder") or []]
        ids = [c.id for c in items]
        assert all(isinstance(i, str) for i in ids)
        if order:
            assert ids[: len(order)] == [i for i in order if i in ids]


def test_branches_have_conditions(survey, raw):
    def count(flow):
        return sum(
            (1 if f.get("Type") == "Branch" and f.get("BranchLogic") else 0)
            + count(f.get("Flow", []))
            for f in flow
        )

    branches = [n for n in survey.walk_flow() if isinstance(n, BranchNode)]
    assert len(branches) == count(raw_flow(raw))
    assert all(b.condition is not None for b in branches)


def test_randomization_is_detected(survey, raw):
    for b in raw_blocks(raw):
        mode = (b.get("Options") or {}).get("RandomizeQuestions", "false")
        assert (survey.blocks[b["ID"]].randomization is not None) == (mode not in ("false", None))
    raws = raw_questions(raw)
    for q in survey.questions.values():
        rtype = (
            (raws[q.id].get("Randomization") or {}).get("Type", "None")
            if isinstance(raws[q.id].get("Randomization"), dict)
            else "None"
        )
        if rtype != "None" and hasattr(q, "randomization"):
            assert q.randomization is not None, q.id


def test_piped_text_is_parsed(survey, raw):
    raws = raw_questions(raw)
    for q in survey.questions.values():
        source = raws[q.id].get("QuestionText") or ""
        assert len(q.text.pipes) == len(set(re.findall(r"\$\{[a-z]+://[^}]*\}", source)))


def test_json_round_trip(survey):
    again = Survey.from_json(survey.to_json())
    assert again == survey


@pytest.mark.parametrize("fmt", sorted(available_exporters()))
def test_exporters_run(survey, fmt):
    assert export(survey, fmt)


def test_response_columns_match_real_export(qsf_path, survey):
    """Every question column in the real Qualtrics export is predicted exactly."""
    oracle_path = qsf_path.with_suffix(".columns.json")
    if not oracle_path.exists():
        pytest.skip("no export header for this survey")
    oracle = json.loads(oracle_path.read_text(encoding="utf-8"))
    predicted = {(c.name, c.import_id) for c in response_columns(survey)}
    missing = []
    for col in oracle:
        import_id = col["import_id"].get("ImportId", "")
        m = re.match(r"^(?:\d+_)?(QID\d+)", import_id)
        if m and m.group(1) in survey.questions and (col["column"], import_id) not in predicted:
            missing.append((col["column"], import_id))
    assert missing == []
