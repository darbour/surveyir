"""End-to-end study harness: Targeting Fairness (Twin-2K-500 mega-study replication).

    uv run python studies/targeting_fairness/run.py --model mock
    uv run python studies/targeting_fairness/run.py --model isolated-mock
    uv run python studies/targeting_fairness/run.py            # both mock conditions

Steps: (1) preflight (``surveyir check --strict`` and the design summary);
(2) simulate one respondent per human respondent with the chosen respondent model,
strict execution; (3) the exposure gate on every trace; (4) a Qualtrics-shaped CSV;
(5) the original analysis on the human and simulated data; (6) ``report.md`` from
every condition found in ``outputs/``.

``--model claude`` / ``isolated-claude`` only print a cost estimate unless
``--i-approve-spend`` is also given. No real model is called by default.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any

import analysis
import gate
import report
from respondents import IsolatedBaseline, LLMRespondent, MockRespondent, PromptMeter
from study import ARMS, OUT, OUTCOME, PANEL_FIELDS, QSF, human_csv

import surveyir
from surveyir.runtime import ExecutionError, ExecutionPolicy, Simulator, executability, transcript
from surveyir.runtime.design import exposures

MODELS = ("mock", "isolated-mock", "claude", "isolated-claude")
LLM_MODEL = "claude-opus-5-5"  # as in examples/llm_respondents.py

#: ASSUMED prices in USD per million tokens. NOT looked up: replace with the current
#: published prices (or pass --price-in/--price-out) before approving a run.
ASSUMED_PRICES = {"claude-opus-5-5": {"input": 5.00, "output": 25.00}}
CHARS_PER_TOKEN = 4.0  # ASSUMED average for English text and JSON
OUTPUT_TOKENS_PER_REQUEST = 20  # ASSUMED: answers are short JSON objects


def preflight(survey: surveyir.Survey) -> dict:
    """Save the strict executability report and the design summary."""
    OUT.mkdir(parents=True, exist_ok=True)
    report_ = executability(survey)
    blocking = report_.blocking(ExecutionPolicy())
    lines = [report_.summary(), "", f"Blocking under strict execution: {len(blocking)}"]
    lines += [f"  {f.resolution()}" for f in blocking]
    (OUT / "check.txt").write_text("\n".join(lines) + "\n")
    d = surveyir.design(survey)
    (OUT / "design.txt").write_text(d.summary() + "\n")
    (OUT / "design.json").write_text(d.model_dump_json(indent=2) + "\n")
    factor = next(f for f in d.factors if f.id == gate.FACTOR)
    return {"blocking": [f.resolution() for f in blocking],
            "factor_contrast": factor.contrast, "summary": d.summary()}


def deep(obj: Any, name: str) -> Any:
    """An attribute of a respondent or of the respondent it wraps."""
    while obj is not None and not hasattr(obj, name):
        obj = getattr(obj, "inner", None)
    return getattr(obj, name, {}) if obj is not None else {}


def build(model: str, seed: int, args: argparse.Namespace) -> tuple[Any, frozenset[str]]:
    isolated = model.startswith("isolated-")
    if model.endswith("mock"):
        base: Any = MockRespondent(seed=seed, effect=args.effect)
        allow: frozenset[str] = frozenset()
    else:
        import anthropic  # only reached after --i-approve-spend

        base = LLMRespondent(anthropic.Anthropic(), LLM_MODEL)
        # a model can break a question's validation; accept and record it
        allow = frozenset({"answer.invalid"})
    inner = IsolatedBaseline(base) if isolated else base
    return PromptMeter(inner, isolated=isolated), allow


def simulate(survey, respondent, twin_ids: list[str], seed: int, allow: frozenset[str]):
    sim = Simulator(survey, seed=seed, allow=set(allow))  # strict by default
    runs, ids, errors = [], [], []
    for i, tid in enumerate(twin_ids):
        panel = {f: f"sim-{i}" for f in PANEL_FIELDS}
        try:
            runs.append(sim.respondent(respondent, embedded=panel))
            ids.append(tid)
        except ExecutionError as e:  # an execution failure: reported, never hidden
            errors.append(f"respondent {i}: {e}")
    return runs, ids, errors


def write_csv(survey, runs, twin_ids: list[str], path: Path) -> None:
    """Qualtrics-shaped: names, labels, ImportIds; plus the TWIN_ID key the original
    script merges on (simulated respondent i stands in for human respondent i)."""
    columns = list(surveyir.response_columns(survey))
    def label(c):
        q = survey.questions.get(c.question_id) if c.question_id else None
        return q.text.plain if q is not None else c.name
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow([c.name for c in columns] + ["TWIN_ID"])
        w.writerow([label(c) for c in columns] + ["TWIN_ID"])
        w.writerow([json.dumps(c.import_object) for c in columns] + ['{"ImportId":"TWIN_ID"}'])
        for run, tid in zip(runs, twin_ids, strict=True):
            w.writerow([v for _, v in run.cells(survey)] + [tid])


def save_examples(survey, runs, respondent, out: Path) -> None:
    """One respondent per arm: the visible trace, the exposure record, and exactly what
    the respondent model was given when it rated the outcome."""
    trace_dir = out / "traces"
    trace_dir.mkdir(parents=True, exist_ok=True)
    contexts = deep(respondent, "context")
    for arm in ARMS.values():
        i, run = next((i, r) for i, r in enumerate(runs) if gate.arm_of(r) == arm.key)
        stem = f"{arm.label}_{arm.key}"
        (trace_dir / f"{stem}.transcript.txt").write_text(
            f"# respondent {i}, arm {arm.key} ({arm.label}); trace as displayed\n\n"
            + transcript(run.trace) + "\n")
        record = asdict(exposures(run, survey))
        record["audit_assignment"] = [asdict(e) for e in run.audit
                                      if type(e).__name__ == "RandomizerDecision"]
        record["recorded_fields"] = {k: run.embedded.get(k) for k in ("Segment", "target")}
        (trace_dir / f"{stem}.exposures.json").write_text(
            json.dumps(record, indent=2, default=str) + "\n")
        if (i, OUTCOME) in contexts:
            (trace_dir / f"{stem}.outcome_context.txt").write_text(
                f"# what the respondent model could read when answering {OUTCOME}\n\n"
                + contexts[i, OUTCOME] + "\n")


def cost(requests: list[int], n_runs: int, n_target: int, args: argparse.Namespace) -> dict:
    price = dict(ASSUMED_PRICES[LLM_MODEL])
    if args.price_in is not None:
        price["input"] = args.price_in
    if args.price_out is not None:
        price["output"] = args.price_out
    scale = n_target / max(n_runs, 1)
    tokens_in = sum(requests) / CHARS_PER_TOKEN * scale
    tokens_out = len(requests) * OUTPUT_TOKENS_PER_REQUEST * scale
    usd = tokens_in / 1e6 * price["input"] + tokens_out / 1e6 * price["output"]
    return {"model": LLM_MODEL, "respondents": n_target,
            "requests": round(len(requests) * scale), "input_tokens": round(tokens_in),
            "output_tokens": round(tokens_out), "usd": round(usd, 2),
            "assumptions": {"price_usd_per_mtok": price, "chars_per_token": CHARS_PER_TOKEN,
                            "output_tokens_per_request": OUTPUT_TOKENS_PER_REQUEST,
                            "prompt_caching": "none assumed"}}


def condition(model: str, survey, human_rows, twin_ids, args) -> dict:
    out = OUT / model
    out.mkdir(parents=True, exist_ok=True)
    respondent, allow = build(model, args.seed, args)
    runs, run_ids, errors = simulate(survey, respondent, twin_ids, args.seed, allow)
    result = gate.verify(runs, errors, allow)
    info = gate.information_state(deep(respondent, "seen"), runs)
    if runs:
        save_examples(survey, runs, respondent, out)
    csv_path = out / "responses.csv"
    write_csv(survey, runs, run_ids, csv_path)
    rows = analysis.read_qualtrics(csv_path)
    original = analysis.run_original_script(human_csv(), csv_path)
    res = {
        "model": model, "seed": args.seed, "gate": result.as_dict(), "information_state": info,
        "contrast": analysis.contrast(rows), "marginal": analysis.marginal(rows),
        "original_d": analysis.original_d(rows, order=twin_ids),
        "original_script": original,
        "llm_cost_if_run": cost(respondent.requests, len(runs), len(twin_ids), args),
        "planted_effect": args.effect if model.endswith("mock") else None,
    }
    (out / "results.json").write_text(json.dumps(res, indent=2, default=str) + "\n")
    status = "PASSED" if result.passed else "FAILED"
    c = res["contrast"]
    print(f"[{model}] execution gate {status} (n={result.n}, arms={dict(result.arms)}); "
          f"own vignette in context: {info['own_vignette_in_context']:.0%}; "
          f"broad - targeted = {c.get('diff', float('nan')):+.2f} "
          f"[{c.get('diff_lo', float('nan')):+.2f}, {c.get('diff_hi', float('nan')):+.2f}]")
    return res


def negative_controls(survey, runs: list) -> dict:
    """The gate must be able to fail. Two controls on this instrument:
    a wrong design statement (vignettes swapped between arms) checked against the real
    runs, and a legacy answerer that reads hidden state, run on a few respondents."""
    swapped = gate.verify(runs, arms=gate.swapped_arms())
    def legacy(view, state):  # the old (view, state) form: can see embedded `Segment`
        q = view.question
        if q.kind == "text_entry":
            return "THIRTY"
        return q.choices[0].id if getattr(q, "choices", None) else None
    leaky, _, errors = simulate(survey, legacy, ["x"] * 5, 1, frozenset())
    privileged = gate.verify(leaky, errors)
    return {
        "swapped_design": {"n": swapped.n, "passed": swapped.passed,
                           "failed_checks": {k: len(v) for k, v in swapped.failures.items() if v}},
        "legacy_answerer": {"n": privileged.n, "passed": privileged.passed,
                            "failed_checks": {k: len(v) for k, v in privileged.failures.items()
                                              if v}},
    }


def estimate_llm(model: str, survey, twin_ids, args) -> dict:
    """Meter the prompts on a mock run of the same instrument (no API), then price them."""
    isolated = model.startswith("isolated-")
    inner: Any = MockRespondent(seed=args.seed)
    meter = PromptMeter(IsolatedBaseline(inner) if isolated else inner, isolated=isolated)
    runs, _, _ = simulate(survey, meter, twin_ids[: args.meter_n], args.seed, frozenset())
    return cost(meter.requests, len(runs), args.n or len(twin_ids), args)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    p.add_argument("--model", action="append", choices=MODELS,
                   help="respondent model (repeatable); default: mock and isolated-mock")
    p.add_argument("--seed", type=int, default=20250408)
    p.add_argument("--effect", type=float, default=2.0, help="mock's planted effect (points)")
    p.add_argument("--n", type=int, help="respondents (default: one per human respondent)")
    p.add_argument("--meter-n", type=int, default=50, help="mock respondents used to meter prompts")
    p.add_argument("--i-approve-spend", action="store_true",
                   help="actually call the Claude API for --model claude/isolated-claude")
    p.add_argument("--price-in", type=float,
                   help="USD per million input tokens (overrides the assumed price)")
    p.add_argument("--price-out", type=float, help="USD per million output tokens")
    args = p.parse_args(argv)
    models = args.model or ["mock", "isolated-mock"]

    survey = surveyir.load_qsf(QSF)
    human_rows = analysis.read_qualtrics(human_csv())
    twin_ids = [r["TWIN_ID"] for r in human_rows][: args.n or None]

    for model in models:
        if model.endswith("claude"):
            est = estimate_llm(model, survey, twin_ids, args)
            print(f"[{model}] COST ESTIMATE (assumed prices; see ASSUMED_PRICES in run.py):")
            print(json.dumps(est, indent=2))
            if not args.i_approve_spend:
                print(f"[{model}] not run: pass --i-approve-spend to call the API.")
                return 2
            if not os.environ.get("ANTHROPIC_API_KEY"):
                print(f"[{model}] not run: ANTHROPIC_API_KEY is not set.")
                return 2

    pre = preflight(survey)
    if "mock" in models:
        runs, _, _ = simulate(survey, MockRespondent(seed=args.seed), twin_ids, args.seed,
                              frozenset())
        pre["negative_controls"] = negative_controls(survey, runs)
        (OUT / "negative_controls.json").write_text(
            json.dumps(pre["negative_controls"], indent=2) + "\n")
    elif (OUT / "negative_controls.json").exists():
        pre["negative_controls"] = json.loads((OUT / "negative_controls.json").read_text())
    if pre["blocking"]:
        print("preflight: blocking gaps under strict execution:", *pre["blocking"], sep="\n  ")
    for model in models:
        condition(model, survey, human_rows, twin_ids, args)

    human = {
        "contrast": analysis.contrast(human_rows), "marginal": analysis.marginal(human_rows),
        "original_d": analysis.original_d(human_rows),
        "consistency": report.human_consistency(human_rows),
        "first_segment": human_rows[0]["Segment"] if human_rows else "",
    }
    path = report.write(pre, human)
    print(f"report: {path.relative_to(Path.cwd()) if path.is_relative_to(Path.cwd()) else path}")
    failed = [m for m in models
              if not json.loads((OUT / m / "results.json").read_text())["gate"]["passed"]]
    nc = pre.get("negative_controls", {})
    vacuous = [k for k, v in nc.items() if v["passed"]]  # a control the gate failed to catch
    if vacuous:
        print("negative controls not caught by the gate:", ", ".join(vacuous))
    return 1 if failed or vacuous else 0


if __name__ == "__main__":
    sys.exit(main())
