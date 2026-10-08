"""Consistency checks of the runtime (design, logic) against observed Qualtrics responses.

    uv run python scripts/validate_runtime.py ~/path/to/Twin-2K-500-Mega-Study/.dat

Reads each fixture study's response.csv (rows stay local) and writes only
aggregates to tests/fixtures/validation/twin.json:

* per flow randomizer: how often each arm was shown, among respondents who
  reached it (from the FL_<id>_DO_<arm> columns);
* per block with randomized question order: how many questions each respondent saw;
* ``consistency``: each finished respondent's recorded embedded data and answers
  are fed to the logic evaluator, which predicts which questions they could see
  (display logic) and which branches they took. Both directions are counted:

  - ``violations``: answered, but predicted hidden (or the branch predicted not
    taken). The recorded response contradicts the evaluator.
  - ``unknown``: predicted shown (or taken), but unanswered. A blank field cannot
    tell a hidden question from a skipped one, so this is not a violation.
  - ``unverifiable``: the condition reads a field the export lacks, or a
    comparison the evaluator cannot decide (it records an approximation).

These are consistency checks against observed responses. They do not validate
what a respondent experienced, and they are not a replay. In particular, the
state is rebuilt from *final* embedded values and completed responses, not
replayed at each decision point: a field changed later in the flow, or an
answer given after the condition was evaluated, is visible to every check.
Checks whose condition reads an embedded field set inside the flow (by an
embedded-data element or a web service) are marked ``final_state_dependent``
and counted separately as well as in the totals; the others read only answers
and fields supplied from outside the survey (URL parameters, panels).
"""

from __future__ import annotations

import csv
import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from surveyir import load_qsf, response_columns  # noqa: E402
from surveyir.model import (  # noqa: E402
    BlockNode,
    BranchNode,
    ChoiceQuestion,
    EmbeddedDataNode,
    MatrixQuestion,
    RandomizerNode,
    Survey,
    WebServiceNode,
    iter_comparisons,
    walk,
)
from surveyir.runtime import Answer, RespondentState, evaluate  # noqa: E402

FIXTURES = ROOT / "tests" / "fixtures" / "qualtrics"
OUT = ROOT / "tests" / "fixtures" / "validation" / "twin.json"


def _canon(obj: dict) -> str:
    return json.dumps({k: str(v) for k, v in obj.items() if k != "timeZone"}, sort_keys=True)


def read_rows(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    """Header names and rows keyed by a column key that is unique even when export
    tags repeat: the canonical ImportId object (falls back to the name)."""
    with path.open(encoding="utf-8-sig", newline="") as f:
        reader = csv.reader(f)
        names = next(reader)
        next(reader)
        ids = next(reader)
        keys = []
        for name, cell in zip(names, ids, strict=False):
            try:
                keys.append(_canon(json.loads(cell)))
            except json.JSONDecodeError:
                keys.append(name)
        return keys, [dict(zip(keys, row, strict=False)) for row in reader]


def _import_id(key: str) -> str | None:
    try:
        return json.loads(key).get("ImportId")
    except (json.JSONDecodeError, AttributeError):
        return None


def col_key(col) -> str:
    return _canon(col.import_object)


def randomizer_counts(survey: Survey, header: list[str], rows: list[dict]) -> dict:
    out = {}
    for node in survey.walk_flow():
        if not isinstance(node, RandomizerNode):
            continue
        cols = [c for c in header if _import_id(c) == f"{node.id}_DO" and "choiceId" in c]
        if not cols:
            continue
        reached = [r for r in rows if any(r.get(c) for c in cols)]
        out[node.id] = {
            "reached": len(reached),
            "arms": {
                json.loads(c)["choiceId"]: sum(1 for r in reached if r.get(c))
                for c in cols
                if "choiceId" in json.loads(c)
            },
            "shown_per_respondent": dict(
                Counter(sum(1 for c in cols if r.get(c)) for r in reached)
            ),
        }
    return out


def block_counts(survey: Survey, header: list[str], rows: list[dict]) -> dict:
    out = {}
    for block in survey.blocks.values():
        if block.randomization is None or block.randomization.mode == "none":
            continue
        cols = [c for c in header if _import_id(c) == f"{block.id}_DO" and "choiceId" in c]
        if not cols:
            continue
        reached = [r for r in rows if any(r.get(c) for c in cols)]
        out[block.id] = {
            "reached": len(reached),
            "questions": len(cols),
            "shown_per_respondent": dict(
                Counter(sum(1 for c in cols if r.get(c)) for r in reached)
            ),
        }
    return out


def respondent_state(survey: Survey, row: dict[str, str], columns) -> RespondentState:
    """Rebuild what logic can see: embedded data and (choice/matrix) answers."""
    state = RespondentState()
    for col in columns:
        if col.part == "embedded_data" and row.get(col_key(col)):
            state.embedded[col.name] = row[col_key(col)]
    answers: dict[str, dict] = {}
    for col in columns:
        value = row.get(col_key(col), "")
        if not value or col.question_id is None or col.part != "response" or col.loop:
            continue
        q = survey.questions.get(col.question_id)
        a = answers.setdefault(col.question_id, {})
        if isinstance(q, ChoiceQuestion):
            if col.choice_id is not None:
                a.setdefault("multi", []).append(col.choice_id)
            else:
                code = {str(c.recode if c.recode is not None else c.id): c.id for c in q.choices}
                a["single"] = code.get(value, value)
        elif isinstance(q, MatrixQuestion) and col.row_id:
            code = {str(x.recode if x.recode is not None else x.id): x.id for x in q.columns}
            a.setdefault("rows", {})[col.row_id] = code.get(value, value)
        else:
            a["text"] = value
            a.setdefault("by_choice", {})[col.choice_id or ""] = value
    for qid, a in answers.items():
        if "multi" in a:
            value = a["multi"]
        elif "single" in a:
            value = a["single"]
        elif "rows" in a:
            value = a["rows"]
        elif len(a.get("by_choice", {})) > 1 or "" not in a.get("by_choice", {""}):
            value = a["by_choice"]
        else:
            value = a.get("text")
        state.answers[(qid, None)] = Answer(value=value)
        state.displayed.add((qid, None))
    return state


def _embedded_read(condition) -> set[str]:
    return {
        c.left.name
        for c in iter_comparisons(condition)
        if c.left.kind == "embedded_data" and c.left.name
    }


def _unknowable(condition, exported_fields: set[str]) -> bool:
    """True if the condition reads embedded data the export does not contain."""
    return bool(_embedded_read(condition) - exported_fields)


def flow_set_fields(survey: Survey) -> set[str]:
    """Embedded fields the flow itself sets: their exported value is the final one, which
    may differ from the value at the point a condition was evaluated."""
    out: set[str] = set()
    for node in survey.walk_flow():
        if isinstance(node, EmbeddedDataNode):
            out.update(
                f.name
                for f in node.fields
                if f.source in ("custom", "random") and f.value is not None
            )
        elif isinstance(node, WebServiceNode):
            out.update(node.sets_fields)
    return out


def _predict(condition, state: RespondentState, survey: Survey) -> bool | None:
    """The evaluator's verdict, or None if it hit a comparison it cannot evaluate
    (recorded as an approximation; the runtime treats it as false)."""
    hit: list = []
    state.on_approximation = hit.append
    try:
        result = evaluate(condition, state, survey)
    finally:
        state.on_approximation = None
    return None if hit else bool(result)


def _tally() -> dict:
    return {
        "checks": 0,
        "violations": 0,
        "unknown": 0,
        "unverifiable": 0,
        "final_state_dependent": {"checks": 0, "violations": 0, "unknown": 0},
    }


def _record(tally: dict, *, predicted: bool, answered: bool, final_state: bool) -> None:
    """One check. ``predicted``: the evaluator says shown (display) or taken (branch)."""
    outcome = None
    if answered and not predicted:
        outcome = "violations"
    elif predicted and not answered:
        outcome = "unknown"
    for t in (tally, tally["final_state_dependent"]) if final_state else (tally,):
        t["checks"] += 1
        if outcome:
            t[outcome] += 1


def consistency(survey: Survey, header: list[str], rows: list[dict]) -> dict:
    """Compare the evaluator's predictions with what each finished respondent answered.

    Uses final embedded values and completed responses, not the state at each
    decision point (see the module docstring)."""
    present = set(header)
    columns = [c for c in response_columns(survey) if col_key(c) in present]
    has_data: dict[str, list[str]] = {}
    for col in columns:
        if col.question_id and col.part in ("response", "text"):
            has_data.setdefault(col.question_id, []).append(col_key(col))
    logic_qs = [
        q for q in survey.questions.values() if q.display_logic is not None and q.id in has_data
    ]
    uses = Counter(n.block_id for n in survey.walk_flow() if isinstance(n, BlockNode))
    branch_blocks = []
    for node in survey.walk_flow():
        if isinstance(node, BranchNode) and node.condition is not None:
            # only blocks reachable solely through this branch prove it was taken
            qids = [
                q
                for n in walk(node.children)
                if isinstance(n, BlockNode)
                and n.block_id in survey.blocks
                and uses[n.block_id] == 1
                for q in survey.blocks[n.block_id].question_ids
                if q in has_data
            ]
            if qids:
                branch_blocks.append((node, qids))
    exported_fields = {c.name for c in columns if c.part == "embedded_data"}
    in_flow = flow_set_fields(survey)
    display, branch = _tally(), _tally()
    finished = [r for r in rows if r.get(_canon({"ImportId": "finished"})) == "1"]
    for row in finished:
        state = respondent_state(survey, row, columns)
        for q in logic_qs:
            shown = None
            if not _unknowable(q.display_logic, exported_fields):
                shown = _predict(q.display_logic, state, survey)
            if shown is None:
                display["unverifiable"] += 1
                continue
            _record(
                display,
                predicted=shown,
                answered=any(row.get(c) for c in has_data[q.id]),
                final_state=bool(_embedded_read(q.display_logic) & in_flow),
            )
        for node, qids in branch_blocks:
            taken = None
            if not _unknowable(node.condition, exported_fields):
                taken = _predict(node.condition, state, survey)
            if taken is None:
                branch["unverifiable"] += 1
                continue
            _record(
                branch,
                predicted=taken,
                answered=any(row.get(c) for q in qids for c in has_data[q]),
                final_state=bool(_embedded_read(node.condition) & in_flow),
            )
    return {"finished": len(finished), "display": display, "branch": branch}


def _line(kind: str, t: dict) -> str:
    if not t["checks"] and not t["unverifiable"]:
        return ""
    fs = t["final_state_dependent"]
    return (
        f"{kind}: {t['violations']} answered-but-predicted-hidden of {t['checks']} checks,"
        f" {t['unknown']} unknown, {t['unverifiable']} unverifiable"
        f" (final-state-dependent: {fs['violations']} of {fs['checks']}, {fs['unknown']} unknown)"
    )


def main(dat: Path) -> None:
    report = {}
    for qsf in sorted(FIXTURES.glob("*.qsf")):
        csv_path = dat / qsf.stem / "raw_data" / "response.csv"
        if not csv_path.exists():
            continue
        survey = load_qsf(qsf)
        header, rows = read_rows(csv_path)
        report[qsf.stem] = {
            "respondents": len(rows),
            "randomizers": randomizer_counts(survey, header, rows),
            "blocks": block_counts(survey, header, rows),
            "consistency": consistency(survey, header, rows),
        }
        c = report[qsf.stem]["consistency"]
        lines = [x for x in (_line(k, c[k]) for k in ("display", "branch")) if x]
        print(f"{qsf.stem:28s} " + ("; ".join(lines) or "no logic checks"))
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(report, indent=1, sort_keys=True))
    print(f"wrote {OUT}")


if __name__ == "__main__":
    main(Path(sys.argv[1]).expanduser())
