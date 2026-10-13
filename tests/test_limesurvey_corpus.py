"""Run the LimeSurvey loader, runtime and columns over LimeSurvey's own surveys.

The files (LimeSurvey's demo and test surveys, GPL-2.0-or-later) are listed in
corpus/manifest.json under ``limesurvey`` and are never committed. Skipped unless
they have been downloaded with ``scripts/fetch_corpus.py --only limesurvey``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from surveyir import RandomAnswerer, Simulator, Survey, export
from surveyir.columns_limesurvey import (
    METADATA,
    LimeColumnOptions,
    infer_lime_options,
    lime_columns,
    read_responses,
)
from surveyir.loaders.limesurvey import LimeSurveyLoader
from surveyir.loaders.limesurvey.loader import SOURCE_DEFECTS
from surveyir.loaders.limesurvey.xmlfile import read_archive
from surveyir.model import UnsupportedQuestion
from surveyir.runtime.executability import executability
from surveyir.runtime.replay import Replayer

ROOT = Path(__file__).resolve().parents[1]
CORPUS = ROOT / "corpus" / "external" / "limesurvey"
FILES = sorted(CORPUS.glob("*")) if CORPUS.exists() else []
NAMES = {
    e["sha"]: Path(e["path"]).name
    for e in json.loads((ROOT / "corpus" / "manifest.json").read_text()).get("limesurvey", [])
}
ARCHIVES = [p for p in FILES if p.suffix == ".lsa"]

pytestmark = pytest.mark.skipif(not FILES, reason="LimeSurvey corpus not downloaded")


def _id(p: Path) -> str:
    return NAMES.get(p.stem, p.stem)[:40]


@pytest.mark.parametrize("path", FILES, ids=_id)
def test_corpus_file(path):
    survey = LimeSurveyLoader().load(path)
    assert [d for d in survey.diagnostics if d.level == "error"] == []
    unexpected = {
        d.code
        for d in survey.diagnostics
        if d.level == "warning"
        and d.code not in SOURCE_DEFECTS
        and d.code != "limesurvey-randomization-group-split"
    }
    assert unexpected == set()
    assert not any(isinstance(q, UnsupportedQuestion) for q in survey.questions.values())
    assert Survey.from_json(survey.to_json()) == survey
    for fmt in ("markdown", "codebook"):
        assert export(survey, fmt)

    # every gap a run records was predicted by the static inventory
    predicted = {(f.code, f.location) for f in executability(survey).features if f.code}
    for run in Simulator(survey, seed=1, strict=False).run(3, RandomAnswerer(seed=2)):
        for a in run.state.approximations:
            if a.code != "answer.invalid":  # depends on the answers
                assert (a.code, a.location) in predicted, a


@pytest.mark.parametrize("path", ARCHIVES, ids=_id)
def test_archive_responses(path):
    archive = read_archive(path.read_bytes())
    if archive.responses is None or not archive.responses.fields.get("responses"):
        pytest.skip("no responses in this archive")
    survey = LimeSurveyLoader().load(path)
    fields = [f.replace("-", "#") for f in archive.responses.fields["responses"]]
    options = infer_lime_options(fields, survey).model_copy(update={"heading": "field"})
    meta = {m for m, _, _ in METADATA} | {"quota_exit"}
    ours = [c.name for c in lime_columns(survey, options)]
    assert [f for f in ours if f not in meta] == [f for f in fields if f not in meta]
    assert {f for f in ours if f in meta} == {f for f in fields if f in meta}

    replayer = Replayer(
        survey, strict=False, fallback_kinds=("block", "choices", "columns", "flow")
    )
    for rec in read_responses(path, survey):
        run = replayer.respondent(rec.orders, rec.answers, embedded=rec.embedded, seed=rec.seed)
        for key, answer in rec.answers.items():
            assert run.answers[key].value == answer.value


def test_options_are_independent_of_file():
    assert LimeColumnOptions().heading == "code"
