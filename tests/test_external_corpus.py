"""Run the loader over the external corpus (see corpus/manifest.json).

Skipped unless the files have been downloaded with scripts/fetch_corpus.py.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from surveyir import Survey, export, load_qsf
from surveyir.loaders.qualtrics import SOURCE_DEFECTS
from surveyir.model import UnsupportedNode, UnsupportedQuestion

ROOT = Path(__file__).resolve().parents[1]
EXTERNAL = ROOT / "corpus" / "external"
FILES = sorted(EXTERNAL.glob("*.qsf")) if EXTERNAL.exists() else []
GAPS = json.loads((Path(__file__).parent / "known_gaps.json").read_text())

pytestmark = pytest.mark.skipif(not FILES, reason="external corpus not downloaded")


@pytest.mark.parametrize("path", FILES, ids=[p.stem[:12] for p in FILES])
def test_external_file(path):
    survey = load_qsf(path)

    assert [d for d in survey.diagnostics if d.level == "error"] == []
    unexpected = {
        d.code
        for d in survey.diagnostics
        if d.level == "warning"
        and d.code not in SOURCE_DEFECTS
        and d.code not in ("unsupported-question", "unsupported-flow")
    }
    assert unexpected == set()

    for q in survey.questions.values():
        if isinstance(q, UnsupportedQuestion) and q.origin.type:
            key = f"{q.origin.type}/{q.origin.selector}"
            assert key in GAPS["question_types"], (
                f"new gap {key!r}: add a typed model or list it in tests/known_gaps.json"
            )
    for node in survey.walk_flow():
        if isinstance(node, UnsupportedNode):
            assert node.source_type in GAPS["flow_types"], (
                f"new gap {node.source_type!r}: add a typed node or list it in known_gaps.json"
            )

    assert Survey.from_json(survey.to_json()) == survey
    for fmt in ("markdown", "codebook"):
        assert export(survey, fmt)
