"""Find conjoint experiments in a Qualtrics survey.

Two kinds are recognized:

* The legacy built-in self-explicated conjoint (``CJ`` survey elements). Its
  ``Features`` key names are unverified, so features are read best-effort and
  the raw payload is kept in ``extras``.
* DIY conjoints, where question JavaScript draws attribute levels at run time
  and stores them in embedded data named ``<P>-<task>-<attribute>`` (attribute
  name) and ``<P>-<task>-<profile>-<attribute>`` (level). Strezhnev's Conjoint
  Survey Design Tool and cjoint use ``P = F``; projoint uses ``K``. The tool's
  JavaScript declares ``var featurearray = {attribute: [levels]}`` and the
  task/profile counts ``var K`` and ``var N``.
"""

from __future__ import annotations

import json
import re
from typing import Any

from ...model import ConjointDesign, ConjointFeature, EmbeddedDataNode, Survey
from ._util import Diagnostics, as_mapping, truthy

FIELD = re.compile(r"^([A-Z])-(\d+)-(\d+)(?:-(\d+))?$")
FEATURE_ARRAY = re.compile(r"var\s+featurearray\s*=\s*(\{.*?\})\s*;", re.S)
RESTRICTIONS = re.compile(r"var\s+restrictionarray\s*=\s*(\[.*?\])\s*;", re.S)


def _int_var(js: str, name: str) -> int | None:
    m = re.search(rf"var\s+{name}\s*=\s*(\d+)\s*;", js)
    return int(m.group(1)) if m else None


def _parse_literal(text: str) -> Any:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        try:  # single-quoted JavaScript literals
            return json.loads(re.sub(r"'", '"', text))
        except json.JSONDecodeError:
            return None


def legacy(elements: list[dict[str, Any]], flow_text: str, diags: Diagnostics) -> list:
    designs = []
    for el in elements:
        cj = as_mapping(el.get("Payload"))
        cid = str(cj.get("ID") or "")
        features_raw = cj.get("Features") or []
        in_flow = bool(cid) and cid in flow_text
        if not features_raw and not in_flow:
            diags.info("unused-conjoint", "Empty, unreferenced conjoint stub", cid or None)
            continue
        features = []
        for f in features_raw if isinstance(features_raw, list) else []:
            f = as_mapping(f)
            levels = f.get("Levels") or f.get("levels") or []
            features.append(
                ConjointFeature(
                    name=str(f.get("Name") or f.get("Description") or f.get("Display") or ""),
                    levels=[
                        str(as_mapping(lv).get("Display", lv)) if isinstance(lv, dict) else str(lv)
                        for lv in (levels.values() if isinstance(levels, dict) else levels)
                    ],
                    allow_elimination=None
                    if "AllowElimination" not in f
                    else truthy(f.get("AllowElimination")),
                )
            )
        diags.warn(
            "legacy-conjoint",
            "Legacy self-explicated conjoint: feature keys are unverified, raw payload kept",
            cid or None,
        )
        designs.append(
            ConjointDesign(
                kind="self_explicated_legacy",
                id=cid or None,
                features=features,
                confidence="low",
                source=f"CJ element {cid}".strip(),
                extras=cj,
            )
        )
    return designs


def diy(survey: Survey) -> list[ConjointDesign]:
    """DIY JavaScript conjoints, grouped by embedded-data prefix."""
    fields: dict[str, set[tuple[int, int | None, int]]] = {}
    users: dict[str, list[str]] = {}

    def note(name: str, qid: str | None = None) -> None:
        m = FIELD.match(name)
        if not m:
            return
        prefix, task, a, b = m.group(1), int(m.group(2)), int(m.group(3)), m.group(4)
        profile, attr = (a, int(b)) if b else (None, a)
        fields.setdefault(prefix, set()).add((task, profile, attr))
        if qid and qid not in users.setdefault(prefix, []):
            users[prefix].append(qid)

    for node in survey.walk_flow():
        if isinstance(node, EmbeddedDataNode):
            for f in node.fields:
                note(f.name)
    scripts: list[tuple[str, str]] = []
    for q in survey.questions.values():
        for pipe in q.text.pipes:
            if pipe.kind == "embedded_data" and pipe.name:
                note(pipe.name, q.id)
        if q.javascript and "featurearray" in q.javascript:
            scripts.append((q.id, q.javascript))

    designs = []
    for qid, js in scripts:
        m = FEATURE_ARRAY.search(js)
        parsed = _parse_literal(m.group(1)) if m else None
        features = (
            [
                ConjointFeature(name=str(k), levels=[str(v) for v in vs])
                for k, vs in parsed.items()
                if isinstance(vs, list)
            ]
            if isinstance(parsed, dict)
            else []
        )
        restrictions = RESTRICTIONS.search(js)
        prefix = next(iter(sorted(fields)), None) if fields else None
        designs.append(
            ConjointDesign(
                kind="diy_js",
                features=features,
                tasks=_int_var(js, "K"),
                profiles=_int_var(js, "N"),
                field_prefix=prefix,
                question_ids=[qid] + [u for u in users.get(prefix or "", []) if u != qid],
                confidence="high" if features else "low",
                source=f"featurearray in JavaScript of {qid}",
                extras={"restrictions": _parse_literal(restrictions.group(1))}
                if restrictions
                else {},
            )
        )
    merged: list[ConjointDesign] = []
    for d in designs:  # the same design is often pasted into several questions
        same = next((m for m in merged if m.features == d.features and m.tasks == d.tasks), None)
        if same is None:
            merged.append(d)
        else:
            same.question_ids += [q for q in d.question_ids if q not in same.question_ids]
    designs = merged
    if not designs:
        for prefix, keys in sorted(fields.items()):
            if not any(profile is not None for _, profile, _ in keys):
                continue  # need level fields (task-profile-attribute) to call it a conjoint
            designs.append(
                ConjointDesign(
                    kind="diy_js",
                    tasks=max(t for t, _, _ in keys),
                    profiles=max(p for _, p, _ in keys if p is not None),
                    features=[
                        ConjointFeature(name=f"attribute {a}")
                        for a in sorted({a for _, _, a in keys})
                    ],
                    field_prefix=prefix,
                    question_ids=users.get(prefix, []),
                    confidence="medium",
                    source=f"embedded-data fields {prefix}-<task>-<profile>-<attribute>",
                )
            )
    return designs
