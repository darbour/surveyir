"""Check ``response_columns`` against real Qualtrics export headers.

Each export header (column names + ImportIds) is paired with the .qsf from the
same repository that best explains it. Exports often come from a different
revision of the survey than the .qsf committed next to them, so a question is
only scored when the export's column names start with the question's current
export tag. Exports re-saved by R (``First.Click``) are compared after the same
name mangling, and the export's own settings (split multi-value fields,
single-column display order) are detected and passed as ``ColumnOptions``.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from collections.abc import Callable
from pathlib import Path

from surveyir import Survey, infer_options, load_qsf, response_columns
from surveyir.columns import METADATA, unpredictable_from_qsf

QID = re.compile(r"^(?:\d+_)?(QID\d+)(?:#\d+)?")
R_MANGLED = re.compile(r"_(First|Last|Page|Click)\.(Click|Submit|Count)$|Operating\.System$")


def _r_names(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._]", ".", name)


def _key(name: str, obj: dict) -> tuple[str, str]:
    obj = {k: (str(v) if k == "choiceId" else v) for k, v in obj.items() if k != "timeZone"}
    return name, json.dumps(obj, sort_keys=True)


METADATA_IDS = {imp for _, imp, _ in METADATA}


def _category(obj: dict, survey: Survey) -> str | None:
    imp = str(obj.get("ImportId", ""))
    if m := QID.match(imp):
        return survey.questions[m.group(1)].kind if m.group(1) in survey.questions else None
    if re.fullmatch(r"FL_\d+_DO", imp):  # only randomizers present in this revision
        flow_ids = {n.id for n in survey.walk_flow()}
        return "flow_display_order" if imp[:-3] in flow_ids else None
    if re.fullmatch(r"BL_\w+_DO", imp):
        return "block_display_order" if imp[:-3] in survey.blocks else None
    if imp in METADATA_IDS:
        return "metadata"
    if imp.startswith("SC_"):
        return "score"
    return None


def score_export(
    export: dict, candidates: list[Survey]
) -> tuple[Survey, Counter[tuple[str, bool]]] | None:
    """Pick the survey that best explains ``export``; return per-category hit/miss counts.

    Columns are compared on the name and the whole third-row object (``ImportId``
    plus ``choiceId``/``point``/``coord``), not the ImportId alone.
    """
    header = [
        (c["column"], c["import_id"])
        for c in export["columns"]
        if isinstance(c["import_id"], dict) and "ImportId" in c["import_id"]
    ]
    header = [(n, o) for n, o in header if not unpredictable_from_qsf(n, o)]
    qcols = [(n, o) for n, o in header if QID.match(str(o["ImportId"]))]
    if not qcols:
        return None
    norm: Callable[[str], str] = (
        _r_names if any(R_MANGLED.search(n) for n, _ in header) else (lambda x: x)
    )
    base = infer_options(header).model_copy(update={"include_metadata": True})
    variants = [base, base.model_copy(update={"slider_naming": "position"})]
    best = None
    for survey in candidates:
        in_survey = [(n, o) for n, o in header if _category(o, survey)]
        q_in = [c for c in in_survey if QID.match(str(c[1]["ImportId"]))]
        if len(q_in) < 0.9 * len(qcols):
            continue
        for options in variants:  # the export's settings are not recorded in the file
            predicted = {
                _key(norm(c.name), c.import_object)
                for c in response_columns(survey, options=options)
            }
            score = sum(_key(n, o) in predicted for n, o in q_in) / len(q_in)
            if best is None or score > best[0]:
                best = (score, survey, predicted, in_survey)
    if best is None:
        return None
    _, survey, predicted, in_survey = best
    current: dict[str, bool] = {}
    for n, o in in_survey:
        if m := QID.match(str(o["ImportId"])):
            tag = norm(survey.questions[m.group(1)].export_tag)
            ok = re.sub(r"^\d+_", "", n).startswith(tag)
            current[m.group(1)] = current.get(m.group(1), False) or ok
    counts: Counter[tuple[str, bool]] = Counter()
    for n, o in in_survey:
        m = QID.match(str(o["ImportId"]))
        if m and not current[m.group(1)]:
            continue  # the export comes from a different revision of this question
        counts[(_category(o, survey) or "?", _key(n, o) in predicted)] += 1
    return survey, counts


def evaluate(root: Path) -> tuple[int, Counter[tuple[str, bool]]]:
    """Score every downloaded export header; returns (paired exports, counts by kind)."""
    manifest = json.loads((root / "corpus" / "manifest.json").read_text())
    by_repo: dict[str, list[str]] = {}
    for f in manifest["files"]:
        by_repo.setdefault(f["repo"], []).append(f["sha"])
    cache: dict[str, Survey | None] = {}

    def survey(sha: str) -> Survey | None:
        if sha not in cache:
            path = root / "corpus" / "external" / f"{sha}.qsf"
            cache[sha] = load_qsf(path) if path.exists() else None
        return cache[sha]

    paired = 0
    totals: Counter[tuple[str, bool]] = Counter()
    for path in sorted((root / "corpus" / "external_columns").glob("*.json")):
        export = json.loads(path.read_text())
        candidates = [s for sha in by_repo.get(export["repo"], []) if (s := survey(sha))]
        result = score_export(export, candidates)
        if result is not None:
            paired += 1
            totals.update(result[1])
    return paired, totals
