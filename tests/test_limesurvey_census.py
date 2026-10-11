"""Every LimeSurvey question type has a mapping, and every type seen in a file is one
LimeSurvey defines (tests/reference/limesurvey_types.json, from Question.php)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from surveyir.loaders.limesurvey import LimeSurveyLoader
from surveyir.loaders.limesurvey.questions import TYPES

ROOT = Path(__file__).resolve().parents[1]
REF = json.loads((Path(__file__).parent / "reference" / "limesurvey_types.json").read_text())
CODES = set(REF["question_types"])
FILES = sorted((ROOT / "tests" / "fixtures" / "limesurvey").rglob("*.ls?"))
if (ROOT / "corpus" / "external" / "limesurvey").exists():
    FILES += sorted((ROOT / "corpus" / "external" / "limesurvey").glob("*"))


def test_every_defined_type_is_mapped():
    assert set(TYPES) == CODES


@pytest.mark.parametrize("path", FILES, ids=lambda p: p.name[:24])
def test_types_in_files_are_defined(path):
    survey = LimeSurveyLoader().load(path)
    seen = {
        q.origin.type
        for q in survey.questions.values()
        if not (q.extras.get("limesurvey") or {}).get("synthetic")  # welcome, group info
    }
    assert seen <= CODES
