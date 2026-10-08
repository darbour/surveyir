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

* ``replay``: each finished respondent is re-administered by the simulator
  (``surveyir.runtime.replay``) with their recorded randomization (the
  ``FL_<id>_DO`` flow-randomizer columns, the block and choice ``_DO`` columns),
  their recorded answers, and their panel fields and other embedded data the flow
  does not set as inputs. Display logic, branches and skips are then evaluated
  with the state at each point, as the respondent went. Counted:

  - ``violations``: answered, but the replay did not display it;
  - ``unknown``: displayed by the replay, but unanswered;
  - ``not_in_display_order``: displayed by the replay, in a randomized block
    whose recorded display order does not list it (also counted in ``unknown``);
  - ``in_display_order_not_displayed``: listed in a randomized block's recorded
    display order (so Qualtrics showed it), but not displayed by the replay;
  - ``embedded``: embedded fields the flow sets, the replay's final value
    against the exported one (``agree`` of ``compared``). Fields whose value is
    piped from a field the flow does not declare and the export lacks (set by
    question JavaScript) are counted as ``unexported_source`` instead;
  - ``unverifiable``: respondents the replay could not administer, by reason
    (a flow randomizer the response has no order for, or another gap the
    strict replay stops at). Question JavaScript is allowed and counted: the
    fields it sets are inputs read from the export, and the checks here do not
    depend on what else it does. Answers Qualtrics' validation would reject
    (``answer.invalid``) are allowed and counted too: they are the recorded
    answers, reconstructed from the export.

The ``consistency`` checks are against observed responses. They do not validate
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
from surveyir.model.question import NON_RESPONSE_KINDS  # noqa: E402
from surveyir.runtime import (  # noqa: E402
    Answer,
    ExecutionError,
    Replayer,
    ReplayGap,
    RespondentState,
    evaluate,
)
from surveyir.runtime.trace import Allowed, BranchEval, Display, Hidden  # noqa: E402

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


# --------------------------------------------------------------------------- replay

#: orders the export does not record, or that do not change which questions are
#: shown: drawn by the fallback chooser, and counted
REPLAY_FALLBACK = ("choices", "columns", "loop")
#: gaps a replay may pass (and counts): see the module docstring
REPLAY_ALLOW = {"javascript", "answer.invalid"}


def _positions(row: dict, cols: list[tuple[str, str]]) -> tuple[str, ...]:
    """Options with a display position in ``row``, in display order."""
    shown = []
    for option, key in cols:
        value = row.get(key, "")
        if value.strip():
            try:
                shown.append((float(value), option))
            except ValueError:
                continue
    return tuple(o for _, o in sorted(shown))


def order_columns(survey: Survey, columns) -> dict[tuple, list[tuple[str, str]]]:
    """Replay keys -> [(option, column key)] for every display-order column present."""
    out: dict[tuple, list[tuple[str, str]]] = {}
    for col in columns:
        if col.part != "display_order" or col.choice_id is None:
            continue
        if col.question_id is None and col.import_id.startswith("FL_"):
            key, option = ("flow", col.import_id[:-3], None), col.choice_id
        elif col.question_id is None:
            block = survey.blocks.get(col.import_id[:-3])
            if block is None:
                continue
            tags = {survey.questions[q].export_tag: q for q in block.question_ids
                    if q in survey.questions}
            key, option = ("block", block.id, None), tags.get(col.choice_id, col.choice_id)
        else:
            q = survey.questions.get(col.question_id)
            if isinstance(q, MatrixQuestion):  # keyed by row position
                pos = int(col.choice_id) - 1
                if not 0 <= pos < len(q.rows):
                    continue
                option = q.rows[pos].id
            else:
                option = col.choice_id
            key = ("choices", col.question_id, col.loop)
        out.setdefault(key, []).append((option, col_key(col)))
    return out


def recorded_answers(survey: Survey, row: dict, columns) -> dict[tuple, Answer]:
    """Answers keyed (question id, loop id), as ``Answer`` values (see ``state.Answer``)."""
    parts: dict[tuple, dict] = {}
    for col in columns:
        value = row.get(col_key(col), "")
        if not value or col.question_id is None or col.part not in ("response", "text"):
            continue
        qid = col.question_id
        q = survey.questions.get(qid) or survey.questions.get(qid.split("#")[0])
        a = parts.setdefault((qid, col.loop), {})
        if col.part == "text":
            a.setdefault("text", {})[col.choice_id or col.row_id or ""] = value
        elif isinstance(q, ChoiceQuestion):
            if col.choice_id is not None:
                a.setdefault("multi", []).append(col.choice_id)
            else:
                code = {str(c.recode if c.recode is not None else c.id): c.id for c in q.choices}
                a["single"] = code.get(value, value)
        elif isinstance(q, MatrixQuestion) and col.row_id:
            if col.answer_id is not None and q.mode == "multiple":
                a.setdefault("rows", {}).setdefault(col.row_id, []).append(col.answer_id)
            elif col.answer_id is not None:
                a.setdefault("rows", {}).setdefault(col.row_id, {})[col.answer_id] = value
            else:
                scale = q.row_columns.get(col.row_id) or q.columns
                code = {str(x.recode if x.recode is not None else x.id): x.id for x in scale}
                a.setdefault("rows", {})[col.row_id] = code.get(value, value)
        elif col.choice_id is not None:
            a.setdefault("by_choice", {})[col.choice_id] = value
        else:
            a["plain"] = value
    out: dict[tuple, Answer] = {}
    for (qid, loop), a in parts.items():
        value = a.get("multi", a.get("single", a.get("rows", a.get("by_choice", a.get("plain")))))
        base = qid.split("#")[0]
        if base != qid:  # a side-by-side sub-question: one part of the parent's answer
            parent = out.setdefault((base, loop), Answer(value={}))
            parent.value[qid] = value
            continue
        out[(qid, loop)] = Answer(value=value, text=a.get("text", {}))
    return out


def undeclared_reads(survey: Survey) -> tuple[set[str], dict[str, set[str]]]:
    """Embedded fields logic reads that the flow neither declares nor sets (they can
    only come from outside, e.g. a URL, and are not exported), and where:
    ({flow ids}, {field: question ids whose display logic reads it})."""
    declared = {f.name for n in survey.walk_flow() if isinstance(n, EmbeddedDataNode)
                for f in n.fields}
    declared |= {f for n in survey.walk_flow() if isinstance(n, WebServiceNode)
                 for f in n.sets_fields}
    nodes = {n.id for n in survey.walk_flow() if isinstance(n, BranchNode)
             and n.condition is not None and _embedded_read(n.condition) - declared}
    questions: dict[str, set[str]] = {}
    for q in survey.questions.values():
        for cond in (q.display_logic, q.in_page_display_logic):
            if cond is not None and _embedded_read(cond) - declared:
                questions.setdefault(q.id, set()).update(_embedded_read(cond) - declared)
    return nodes, questions


def _replay_tally() -> dict:
    return {
        "replayed": 0,
        "answered": 0,
        "violations": 0,
        "unknown": 0,
        "not_in_display_order": 0,
        "in_display_order_not_displayed": 0,
        "ended_early": 0,
        "embedded": {"compared": 0, "agree": 0, "unexported_source": 0},
        "allowed": {},
        "order_fallbacks": {},
        "inferred_orders": {},
        "unverifiable": {},
    }


def replay_check(survey: Survey, header: list[str], rows: list[dict]) -> dict:
    """Re-administer each finished respondent with their recorded orders, answers and
    inputs, and compare what the replay displayed with what they answered (see the
    module docstring)."""
    present = set(header)
    columns = [c for c in response_columns(survey) if col_key(c) in present]
    answer_cols: dict[tuple, list[str]] = {}
    for col in columns:
        if col.question_id and col.part in ("response", "text"):
            answer_cols.setdefault((col.question_id.split("#")[0], col.loop), []).append(
                col_key(col))
    orders = order_columns(survey, columns)
    exported = {c.name: col_key(c) for c in columns if c.part == "embedded_data"}
    set_in_flow = flow_set_fields(survey) & set(exported)
    inputs = {n: k for n, k in exported.items() if n not in set_in_flow}
    # fields whose flow value pipes a field nothing declares and the export lacks
    # (one question JavaScript sets): no replay can know that value
    declared = {f.name for nd in survey.walk_flow() if isinstance(nd, EmbeddedDataNode)
                for f in nd.fields}
    unexported_source = {
        f.name for nd in survey.walk_flow() if isinstance(nd, EmbeddedDataNode)
        for f in nd.fields if f.name in set_in_flow and f.value is not None
        and any(pp.kind == "embedded_data" and pp.name not in declared | set(exported)
                for pp in f.value.pipes)
    }
    in_randomized_block = {
        q: b.id for b in survey.blocks.values()
        if b.randomization is not None and b.randomization.mode != "none"
        for q in b.question_ids
    }
    # randomized blocks and loops without display-order columns: the questions or
    # iterations shown are inferred from those with answers (their order is not known)
    inferred: dict[tuple, dict[str, list[tuple]]] = {}
    for b in survey.blocks.values():
        if b.randomization is not None and b.randomization.mode != "none" \
                and ("block", b.id, None) not in orders:
            inferred[("block", b.id, None)] = {
                q: [k for k in answer_cols if k[0] == q] for q in b.question_ids}
        if b.loop is not None and b.loop.randomization is not None \
                and b.loop.randomization.mode != "none":
            iterations: dict[str, list[tuple]] = {}
            for k in answer_cols:
                if k[1] is not None and k[0] in b.question_ids:
                    iterations.setdefault(k[1], []).append(k)
            inferred[("loop", b.id, None)] = iterations
    undeclared_nodes, undeclared_questions = undeclared_reads(survey)
    replayer = Replayer(survey, fallback_kinds=REPLAY_FALLBACK, allow=REPLAY_ALLOW)
    t = _replay_tally()
    finished = [r for r in rows if r.get(_canon({"ImportId": "finished"})) == "1"]
    for n, row in enumerate(finished):
        answered = {k for k, cols in answer_cols.items() if any(row.get(c) for c in cols)}
        recorded = {}
        for key, cols in orders.items():
            shown = _positions(row, cols)
            if shown or key[0] != "flow":  # an unreached randomizer is left unrecorded
                recorded[key] = shown
        for key, options in inferred.items():
            recorded[key] = tuple(o for o, ks in options.items() if set(ks) & answered)
            t["inferred_orders"][key[0]] = t["inferred_orders"].get(key[0], 0) + 1
        try:
            run = replayer.respondent(
                recorded, recorded_answers(survey, row, columns),
                embedded={name: row.get(k, "") for name, k in inputs.items()}, seed=n)
        except ReplayGap as e:
            reason = f"replay.gap:{e.request.kind}"
            t["unverifiable"][reason] = t["unverifiable"].get(reason, 0) + 1
            continue
        except ExecutionError as e:
            reason = e.approximation.code
            t["unverifiable"][reason] = t["unverifiable"].get(reason, 0) + 1
            continue
        loaded = {e.qid for e in run.audit if isinstance(e, Hidden)} | {
            d.qid for d in run.trace if isinstance(d, Display)}
        reads = sorted(
            {f for q in loaded & set(undeclared_questions) for f in undeclared_questions[q]}
            | ({"(branch)"} if any(isinstance(e, BranchEval) and e.node_id in undeclared_nodes
                                   for e in run.audit) else set()))
        if reads:  # decided by a field nobody supplied and the export lacks
            reason = "undeclared_field"
            t["unverifiable"][reason] = t["unverifiable"].get(reason, 0) + 1
            continue
        t["replayed"] += 1
        for code in sorted({e.code for e in run.audit if isinstance(e, Allowed)}):
            t["allowed"][code] = t["allowed"].get(code, 0) + 1
        for kind in sorted({r.kind for r in replayer.chooser.fallbacks}):
            t["order_fallbacks"][kind] = t["order_fallbacks"].get(kind, 0) + 1
        if not run.finished:
            t["ended_early"] += 1
        displayed = {(d.qid, d.loop_id) for d in run.trace
                     if isinstance(d, Display) and d.kind not in NON_RESPONSE_KINDS}
        t["answered"] += len(answered)
        t["violations"] += len(answered - displayed)
        for qid, _loop in (displayed & set(answer_cols)) - answered:
            t["unknown"] += 1
            block = in_randomized_block.get(qid)
            shown = recorded.get(("block", block, None)) if block else None
            if shown and qid not in shown:
                t["not_in_display_order"] += 1
        screens = {(d.qid, d.loop_id) for d in run.trace if isinstance(d, Display)}
        for key, shown in recorded.items():  # Qualtrics showed these; did the replay?
            if key[0] == "block" and key not in inferred:
                t["in_display_order_not_displayed"] += sum(
                    1 for q in shown if (q, None) not in screens)
        for name in set_in_flow:
            if name in unexported_source:
                t["embedded"]["unexported_source"] += 1
                continue
            t["embedded"]["compared"] += 1
            t["embedded"]["agree"] += run.embedded.get(name, "") == row.get(exported[name], "")
    return {"finished": len(finished), **t}


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
            "replay": replay_check(survey, header, rows),
        }
        c = report[qsf.stem]["consistency"]
        lines = [x for x in (_line(k, c[k]) for k in ("display", "branch")) if x]
        print(f"{qsf.stem:28s} " + ("; ".join(lines) or "no logic checks"))
        r = report[qsf.stem]["replay"]
        e = r["embedded"]
        print(f"{'':28s} replay: {r['replayed']} of {r['finished']} replayed,"
              f" {r['violations']} answered-not-displayed of {r['answered']} answered,"
              f" {r['unknown']} unknown ({r['not_in_display_order']} not in display order),"
              f" embedded {e['agree']}/{e['compared']}, ended early {r['ended_early']},"
              f" unverifiable {r['unverifiable']}, allowed {r['allowed']}")
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(report, indent=1, sort_keys=True))
    print(f"wrote {OUT}")


if __name__ == "__main__":
    main(Path(sys.argv[1]).expanduser())
