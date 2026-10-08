"""Exact-trace benchmark: what each simulated respondent saw, checked against a hand-written spec.

Each case is a small synthetic instrument built here, and a JSON file in
``tests/fixtures/traces/`` holding the respondent-visible trace the instrument's
specification implies. The JSON files were written from the instrument (its
``spec`` field says what it is), not generated from simulator output: a failure
means the runtime and the specification disagree, so read the spec before
touching the file.

Format of ``tests/fixtures/traces/<case>.json``::

    {
      "case": "<file stem>",
      "spec": "<the instrument in words>",
      "runs": [{
        "name": "<label>",
        "when": {"<randomizer flow id>": ["<arm key>", ...]},   # optional
        "simulator": "allow" | "implement",                       # optional (case e)
        "answers": {"<qid>" | "<qid>@<loop id>": <value>},
        "trace": [<observation>, ...],
        "not_displayed": ["<qid>" | "<qid>#<choice id>", ...],
        "history_at_response": {"<key>": ["<key of each Display in ctx.history>", ...]},
        # optional:
        "page_at_response": {"<key>": ["<qid of each Display in ctx.page>", ...]},
        "branches": [["<flow id>", <result>], ...],               # every BranchEval, in order
        "embedded_sets": [["<field>", "<value>", "<source>"], ...],  # every EmbeddedSet
        "skips": [["<qid>", "<destination>"], ...],                # every SkipTaken
        "approximations": [["<code>", "<location>", "<affects>"], ...],  # default: none
        "allowed": [["<code>", "<location>"], ...],                # default: none
        "not_in_transcript": ["<string>", ...],
        "not_in_context": ["<string>", ...],    # absent from repr() of every ResponseContext
        "rendered_once": ["<string>", ...]      # appears exactly once in the transcript
      }]
    }

Observations, normalized (keys at their default are omitted):

* ``{"type": "page_start", "page", "block", "loop", "loop_number", "loop_total"}``
* ``{"type": "display", "qid", "loop", "kind", "text", "choices": [[id, text]],
  "columns": [[id, text]], "revealed": true}``
* ``{"type": "response", "qid", "loop", "value", "text": {...}}``
* ``{"type": "submit", "page"}``
* ``{"type": "end", "reason", "finished"}``

``when`` selects a randomizer outcome: respondents are drawn from one seeded
``Simulator`` until the audit's ``RandomizerDecision`` for each named flow
randomizer shows exactly those arms. ``answers`` scripts the respondent: every
question it is asked must be scripted, and every scripted answer must be asked
for. Besides the expected file, every run is checked for structural invariants:
display seq numbering, pages, and each ``ResponseContext.history`` being the
respondent's view of a prefix of the final trace (the trace with block ids blanked:
they are researcher-side identifiers that can name the arm).
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path
from typing import Any

import pytest

from surveyir import load_qsf
from surveyir.model import Survey
from surveyir.runtime import ExecutionError, Simulator, transcript
from surveyir.runtime.trace import (
    Allowed,
    Approximation,
    BranchEval,
    Display,
    EmbeddedSet,
    End,
    PageStart,
    PageSubmit,
    RandomizerDecision,
    Response,
    SkipTaken,
)
from tests.conftest import mc
from tests.test_runtime import ed, logic, qx

TRACES = Path(__file__).parent / "fixtures" / "traces"


# ------------------------------------------------------------------ instruments


def qsf(
    questions: list[dict],
    blocks: dict[str, list],
    flow: list[dict],
    options: dict[str, dict] | None = None,
) -> Survey:
    """A QSF with several blocks. ``blocks`` maps block id -> elements: a qid, ``"|"``
    for a page break, or a raw block element dict (for skip logic)."""

    def element(e: Any) -> dict:
        if isinstance(e, dict):
            return e
        return {"Type": "Page Break"} if e == "|" else {"Type": "Question", "QuestionID": e}

    payload = [
        {
            "Type": "Default" if n == 0 else "Standard",
            "ID": bid,
            "Description": bid,
            "BlockElements": [element(e) for e in elements],
            "Options": (options or {}).get(bid, {}),
        }
        for n, (bid, elements) in enumerate(blocks.items())
    ]
    return load_qsf(
        {
            "SurveyEntry": {"SurveyID": "SV_trace", "SurveyName": "Trace", "SurveyLanguage": "EN"},
            "SurveyElements": [
                {"Element": "BL", "Payload": payload},
                {"Element": "FL", "Payload": {"Type": "Root", "FlowID": "FL_1", "Flow": flow}},
                *[{"Element": "SQ", "Payload": q} for q in questions],
            ],
        }
    )


def text_screen(qid: str, text: str) -> dict:
    return {
        "QuestionID": qid,
        "QuestionType": "DB",
        "Selector": "TB",
        "DataExportTag": qid.replace("QID", "D"),
        "QuestionText": f"<p>{text}</p>",
    }


def ask(qid: str, text: str, choices: tuple[str, str] = ("Yes", "No"), **extra: Any) -> dict:
    return mc(
        qid,
        QuestionText=f"<p>{text}</p>",
        Choices={"1": {"Display": choices[0]}, "2": {"Display": choices[1]}},
        **extra,
    )


def block(fid: str, bid: str) -> dict:
    return {"Type": "Block", "ID": bid, "FlowID": fid}


def embedded(fid: str, **fields: str) -> dict:
    return {
        "Type": "EmbeddedData",
        "FlowID": fid,
        "EmbeddedData": [{"Field": k, "Type": "Custom", "Value": v} for k, v in fields.items()],
    }


def branch(fid: str, condition: dict, *children: dict) -> dict:
    return {
        "Type": "Branch",
        "FlowID": fid,
        "BranchLogic": logic(condition),
        "Flow": list(children),
    }


def randomizer(fid: str, *arms: dict) -> dict:
    return {
        "Type": "BlockRandomizer",
        "FlowID": fid,
        "SubSet": 1,
        "EvenPresentation": True,
        "Flow": list(arms),
    }


def group(fid: str, *children: dict) -> dict:
    return {"Type": "Group", "FlowID": fid, "Description": fid, "Flow": list(children)}


def a_treatment_screens() -> Survey:
    return qsf(
        [
            ask("QID1", "How much do you trust people around you?"),
            text_screen(
                "QID10", "A neighbour returns your lost wallet with every banknote inside."
            ),
            text_screen("QID11", "A stranger keeps the wallet you dropped on the train."),
        ],
        {"BL_1": ["QID1"], "BL_alpha": ["QID10"], "BL_beta": ["QID11"]},
        [
            randomizer(
                "FL_2",
                group("FL_3", block("FL_31", "BL_alpha")),
                group("FL_4", block("FL_41", "BL_beta")),
            ),
            block("FL_5", "BL_1"),
        ],
    )


def b_branch_sides() -> Survey:
    return qsf(
        [
            ask("QID1", "Do you own a car?"),
            ask("QID2", "How often do you drive?", ("Daily", "Weekly")),
            ask("QID3", "How do you commute?", ("Bus", "Bicycle")),
        ],
        {"BL_1": ["QID1"], "BL_X": ["QID2"], "BL_Y": ["QID3"]},
        [
            block("FL_2", "BL_1"),
            branch("FL_3", qx("q://QID1/SelectableChoice/1"), block("FL_4", "BL_X")),
            branch("FL_5", qx("q://QID1/SelectableChoice/2"), block("FL_6", "BL_Y")),
        ],
    )


def c_field_changed() -> Survey:
    return qsf(
        [
            ask("QID2", "Second question"),
            ask("QID3", "Third question"),
            ask("QID4", "Fourth question"),
        ],
        {"BL_2": ["QID2"], "BL_3": ["QID3"], "BL_4": ["QID4"]},
        [
            embedded("FL_2", x="1"),
            branch("FL_3", ed("x", "EqualTo", "1"), block("FL_31", "BL_2")),
            embedded("FL_4", x="2"),
            branch("FL_5", ed("x", "EqualTo", "1"), block("FL_51", "BL_3")),
            branch("FL_6", ed("x", "EqualTo", "2"), block("FL_61", "BL_4")),
        ],
    )


def d_loop() -> Survey:
    loop = {
        "Looping": "Static",
        "LoopingOptions": {
            "Static": {"1": {"1": "apples"}, "2": {"1": "bananas"}, "3": {"1": "cherries"}}
        },
    }
    return qsf(
        [ask("QID1", "How much do you like ${lm://Field/1}?", ("A lot", "A little"))],
        {"BL_1": ["QID1"]},
        [block("FL_2", "BL_1")],
        options={"BL_1": loop},
    )


def e_web_service() -> Survey:
    return qsf(
        [ask("QID1", "Any comments?"), ask("QID2", "Would you like the premium offer?")],
        {"BL_1": ["QID1"], "BL_2": ["QID2"]},
        [
            {
                "Type": "WebService",
                "FlowID": "FL_2",
                "URL": "https://example.org/tier",
                "Method": "GET",
                "ResponseMap": [{"key": "tier", "value": "segment"}],
            },
            branch("FL_3", ed("segment", "EqualTo", "premium"), block("FL_31", "BL_2")),
            block("FL_4", "BL_1"),
        ],
    )


def f_in_page() -> Survey:
    skip = {
        "Type": "Question",
        "QuestionID": "QID2",
        "SkipLogic": [
            {
                "ChoiceLocator": "q://QID2/SelectableChoice/1",
                "Condition": "Selected",
                "QuestionID": "QID2",
                "SkipToDestination": "ENDOFBLOCK",
            }
        ],
    }
    return qsf(
        [
            ask("QID1", "Do you exercise?"),
            ask(
                "QID2",
                "Would you like tips on starting?",
                InPageDisplayLogic=logic(qx("q://QID1/SelectableChoice/2")),
            ),
            ask("QID3", "Do you sleep well?"),
            ask("QID4", "How many days a week?"),
            ask("QID5", "Any last thoughts?"),
        ],
        {"BL_1": ["QID1", skip, "QID3", "|", "QID4"], "BL_2": ["QID5"]},
        [block("FL_2", "BL_1"), block("FL_3", "BL_2")],
    )


def g_hidden_fields() -> Survey:
    return qsf(
        [ask("QID1", "Hello ${e://Field/shown_name}, are you ready?")],
        {"BL_1": ["QID1"]},
        [
            randomizer(
                "FL_2", embedded("FL_3", cond_secret="ZK9"), embedded("FL_4", cond_secret="QX7")
            ),
            embedded("FL_5", shown_name="Avery"),
            block("FL_6", "BL_1"),
        ],
    )


INSTRUMENTS = {
    f.__name__: f
    for f in (
        a_treatment_screens,
        b_branch_sides,
        c_field_changed,
        d_loop,
        e_web_service,
        f_in_page,
        g_hidden_fields,
    )
}


def tier_service(node: Any, state: Any) -> dict[str, str]:
    return {"segment": "premium"}


SIMULATORS: dict[str | None, dict[str, Any]] = {
    None: {},
    "allow": {"allow": {"web_service"}},
    "implement": {"implementations": {"web_service": tier_service}},
}


# ------------------------------------------------------------------ respondent & normal form


class Scripted:
    """Answers from a dict keyed by qid or ``qid@loop``; records every context it gets."""

    def __init__(self, answers: dict[str, Any]) -> None:
        self.answers, self.contexts, self.unscripted = answers, {}, []

    def answer(self, ctx: Any) -> Any:
        k = key(ctx.view)
        self.contexts[k] = ctx
        if k not in self.answers:
            self.unscripted.append(k)
            return None
        return self.answers[k]


def key(ob: Display | Response) -> str:
    return ob.qid if ob.loop_id is None else f"{ob.qid}@{ob.loop_id}"


def normal(ob: Any) -> dict[str, Any]:
    if isinstance(ob, PageStart):
        out: dict[str, Any] = {"type": "page_start", "page": ob.page, "block": ob.block_id}
        if ob.loop_id is not None:
            out |= {"loop": ob.loop_id, "loop_number": ob.loop_number, "loop_total": ob.loop_total}
        return out
    if isinstance(ob, Display):
        out = {"type": "display", "qid": ob.qid}
        if ob.loop_id is not None:
            out["loop"] = ob.loop_id
        out |= {"kind": ob.kind, "text": ob.text}
        if ob.choices:
            out["choices"] = [[c.id, c.text] for c in ob.choices]
        if ob.columns:
            out["columns"] = [[c.id, c.text] for c in ob.columns]
        if ob.revealed_in_page:
            out["revealed"] = True
        return out
    if isinstance(ob, Response):
        out = {"type": "response", "qid": ob.qid}
        if ob.loop_id is not None:
            out["loop"] = ob.loop_id
        out["value"] = ob.value
        if ob.text:
            out["text"] = dict(ob.text)
        return out
    if isinstance(ob, PageSubmit):
        return {"type": "submit", "page": ob.page}
    assert isinstance(ob, End)
    return {"type": "end", "reason": ob.reason, "finished": ob.finished}


def public(ob: Any) -> Any:
    """An observation as the respondent model gets it: block ids are researcher-side
    identifiers (they can name the arm), so the context blanks them."""
    if isinstance(ob, (Display, PageStart)):
        return dataclasses.replace(ob, block_id="")
    return ob


def flow_decisions(run: Any) -> dict[str, list[str]]:
    return {
        e.node_id: list(e.shown)
        for e in run.audit
        if isinstance(e, RandomizerDecision) and e.kind == "flow"
    }


def administer(survey: Survey, spec: dict) -> tuple[Any, Scripted]:
    """Run the scripted respondent; with ``when``, the first respondent from one seeded
    simulator whose randomizer decisions match."""
    sim = Simulator(survey, seed=0, **SIMULATORS[spec.get("simulator")])
    for _ in range(50):
        who = Scripted(spec["answers"])
        run = sim.respondent(who)
        decided = flow_decisions(run)
        if all(decided.get(n) == arms for n, arms in spec.get("when", {}).items()):
            return run, who
    raise AssertionError(f"no respondent in 50 matched {spec['when']}")


def check_invariants(run: Any, who: Scripted) -> None:
    current_page, displays = None, {}
    for ob in run.trace:
        if isinstance(ob, PageStart):
            current_page = ob.page
        elif isinstance(ob, Display):
            assert ob.page == current_page
            assert ob.seq == len(displays) + 1
            displays[key(ob)] = ob
        elif isinstance(ob, Response):
            assert displays[key(ob)].seq == ob.seq and ob.page == current_page
        elif isinstance(ob, PageSubmit):
            assert ob.page == current_page
            current_page = None
    assert isinstance(run.trace[-1], End)
    for k, ctx in who.contexts.items():
        n = len(ctx.history)
        assert ctx.history == tuple(public(o) for o in run.trace[:n]), (
            f"history of {k} is not the respondent's view of a prefix of the trace"
        )
        assert ctx.view == public(displays[k]) and ctx.view in ctx.history
        assert ctx.view in ctx.page and ctx.respondent_index == run.index
    assert not run.privileged


def cases() -> list:
    params = []
    for path in sorted(TRACES.glob("*.json")):
        doc = json.loads(path.read_text(encoding="utf-8"))
        for n, run in enumerate(doc["runs"]):
            params.append(pytest.param(path.stem, run, id=f"{path.stem}[{n}]"))
    return params


def test_every_case_has_an_instrument_and_expected_file():
    assert sorted(INSTRUMENTS) == sorted(p.stem for p in TRACES.glob("*.json"))


@pytest.mark.parametrize("case,spec", cases())
def test_exact_trace(case: str, spec: dict) -> None:
    run, who = administer(INSTRUMENTS[case](), spec)
    assert not who.unscripted, f"asked unscripted questions {who.unscripted}"
    assert sorted(who.contexts) == sorted(spec["answers"]), "scripted answers never asked for"
    check_invariants(run, who)

    assert [normal(o) for o in run.trace] == spec["trace"]

    shown = [o for o in run.trace if isinstance(o, Display)]
    for token in spec["not_displayed"]:
        qid, _, choice = token.partition("#")
        if choice:
            assert not [d for d in shown if d.qid == qid and choice in {c.id for c in d.choices}]
        else:
            assert qid not in {d.qid for d in shown}
            assert qid not in {q for q, _ in run.displayed} | {q for q, _ in run.answers}

    for k, expected in spec["history_at_response"].items():
        history = who.contexts[k].history
        assert [key(o) for o in history if isinstance(o, Display)] == expected, k
    for k, expected in spec.get("page_at_response", {}).items():
        assert [d.qid for d in who.contexts[k].page] == expected, k

    audit = run.audit
    if "branches" in spec:
        assert [[e.node_id, e.result] for e in audit if isinstance(e, BranchEval)] == spec[
            "branches"
        ]
        assert not [e for e in audit if isinstance(e, BranchEval) and e.unknown]
    if "embedded_sets" in spec:
        assert [[e.name, e.value, e.source] for e in audit if isinstance(e, EmbeddedSet)] == spec[
            "embedded_sets"
        ]
    if "skips" in spec:
        assert [[e.qid, e.destination] for e in audit if isinstance(e, SkipTaken)] == spec["skips"]
    assert [
        [e.code, e.location, e.affects] for e in audit if isinstance(e, Approximation)
    ] == spec.get("approximations", [])
    assert [[e.code, e.location] for e in audit if isinstance(e, Allowed)] == spec.get(
        "allowed", []
    )

    text = transcript(run.trace)
    for s in spec.get("not_in_transcript", []):
        assert s not in text and s not in transcript(run.trace, include_ids=False), s
    for s in spec.get("not_in_context", []):
        assert not [k for k, ctx in who.contexts.items() if s in repr(ctx)], s
    for s in spec.get("rendered_once", []):
        assert text.count(s) == 1, s


def test_both_arms_are_benchmarked():
    """Cases with a randomizer cover every arm, so neither side goes unchecked."""
    for case in ("a_treatment_screens", "g_hidden_fields"):
        doc = json.loads((TRACES / f"{case}.json").read_text(encoding="utf-8"))
        assert sorted(r["when"]["FL_2"][0] for r in doc["runs"]) == ["FL_3", "FL_4"]


def test_unsupported_web_service_stops_a_strict_run():
    sim = Simulator(e_web_service(), seed=0)  # strict by default
    who = Scripted({"QID1": "1"})
    with pytest.raises(ExecutionError) as stopped:
        sim.respondent(who)
    a = stopped.value.approximation
    assert (a.code, a.location, a.affects) == ("web_service", "FL_2", "assignment")
    assert who.contexts == {}  # stopped before anything was asked
