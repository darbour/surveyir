"""Every question code seen in a corpus file must be one Qualtrics documents.

tests/reference/qualtrics_types.json vendors the type/selector enums of the
Qualtrics survey-definitions API and the render keys of its survey-taking
runtime. A code outside both is either a new Qualtrics feature or a malformed
file; either way it needs a look, so it fails here until added to
``corpus_allowlist``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from surveyir.loaders.qualtrics.questions import KNOWN_UNMODELED

ROOT = Path(__file__).resolve().parents[1]
REF = json.loads((Path(__file__).parent / "reference" / "qualtrics_types.json").read_text())
FILES = sorted((ROOT / "tests" / "fixtures" / "qualtrics").glob("*.qsf"))
if (ROOT / "corpus" / "external").exists():
    FILES += sorted((ROOT / "corpus" / "external").glob("*.qsf"))

TYPES = set(REF["api_question_types"]) | {r.split("/")[0] for r in REF["runtime"]}
SELECTORS = set(REF["api_selectors"]) | {r.split("/")[1] for r in REF["runtime"]}


def _first(v):
    return v[0] if isinstance(v, list) and v else v


def test_reference_covers_codes_the_loader_special_cases():
    for qtype, selector, _ in KNOWN_UNMODELED:
        assert qtype in TYPES
        assert selector is None or any(s.startswith(selector) for s in SELECTORS)


@pytest.mark.parametrize("path", FILES, ids=[p.stem[:12] for p in FILES])
def test_question_codes_are_documented(path):
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    unknown = set()
    for el in data.get("SurveyElements") or []:
        payload = el.get("Payload") if isinstance(el, dict) else None
        if not (isinstance(payload, dict) and el.get("Element") == "SQ"):
            continue
        qtype, selector = _first(payload.get("QuestionType")), _first(payload.get("Selector"))
        if not qtype or not selector:
            continue  # reported by the loader as a source defect
        key = f"{qtype}/{selector}"
        if (qtype not in TYPES or selector not in SELECTORS) and key not in REF["corpus_allowlist"]:
            unknown.add(key)
    assert unknown == set(), "undocumented Qualtrics codes; model them or allowlist them"
