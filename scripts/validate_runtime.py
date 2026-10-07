"""Validate the runtime (design, logic, simulator) against real Qualtrics responses.

    uv run python scripts/validate_runtime.py ~/path/to/Twin-2K-500-Mega-Study/.dat

Reads each fixture study's response.csv (rows stay local) and writes only
aggregates to tests/fixtures/validation/twin.json:

* per flow randomizer: how often each arm was shown, among respondents who
  reached it (from the FL_<id>_DO_<arm> columns);
* per block with randomized question order: how many questions each respondent saw;
* branch / display-logic replay: each finished respondent's recorded embedded data
  and answers are fed to the logic evaluator, which predicts which questions they
  could see. A question predicted hidden but answered is a violation.
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
    MatrixQuestion,
    RandomizerNode,
    Survey,
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


def _unknowable(condition, exported_fields: set[str]) -> bool:
    """True if the condition reads embedded data the export does not contain."""
    from surveyir.model import iter_comparisons

    return any(
        c.left.kind == "embedded_data" and c.left.name not in exported_fields
        for c in iter_comparisons(condition)
    )


def replay(survey: Survey, header: list[str], rows: list[dict]) -> dict:
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
    result = Counter()
    finished = [r for r in rows if r.get(_canon({"ImportId": "finished"})) == "1"]
    for row in finished:
        state = respondent_state(survey, row, columns)
        for q in logic_qs:
            if _unknowable(q.display_logic, exported_fields):
                result["display_unverifiable"] += 1
                continue
            answered = any(row.get(c) for c in has_data[q.id])
            shown = evaluate(q.display_logic, state, survey)
            result["display_checks"] += 1
            if answered and not shown:
                result["display_violations"] += 1
        for node, qids in branch_blocks:
            if _unknowable(node.condition, exported_fields):
                result["branch_unverifiable"] += 1
                continue
            answered = any(row.get(c) for q in qids for c in has_data[q])
            taken = evaluate(node.condition, state, survey)
            result["branch_checks"] += 1
            if answered and not taken:
                result["branch_violations"] += 1
            if taken and not answered:
                result["branch_taken_unanswered"] += 1
    return {"finished": len(finished), **result}


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
            "replay": replay(survey, header, rows),
        }
        r = report[qsf.stem]["replay"]
        print(
            f"{qsf.stem:28s} display {r.get('display_violations', 0)}/{r.get('display_checks', 0)}"
            f"  branch {r.get('branch_violations', 0)}/{r.get('branch_checks', 0)}"
            f" (taken but unanswered {r.get('branch_taken_unanswered', 0)},"
            f" unverifiable {r.get('branch_unverifiable', 0) + r.get('display_unverifiable', 0)})"
        )
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(report, indent=1, sort_keys=True))
    print(f"wrote {OUT}")


if __name__ == "__main__":
    main(Path(sys.argv[1]).expanduser())
