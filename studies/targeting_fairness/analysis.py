"""The original analysis, ported minimally (stdlib only), plus the oriented contrast.

The mega-study's script (``mega_study_evaluation/targeting_fairness/
mega_study_evaluation.py``) reads two Qualtrics CSVs (``skiprows=[1, 2]``), merges
them on ``TWIN_ID``, and for the outcome ``fair1`` reports Cohen's d between the
two ``Segment`` levels (``common/stats_analysis.calculate_effect_sizes``):

    levels = pair[cond].unique()           # order of first appearance
    d = (mean(levels[0]) - mean(levels[1])) / pooled_sd

so the *sign* of its d depends on which arm happens to come first in the file.
``original_d`` reproduces that exactly. ``contrast`` reports the same quantity
oriented by what was displayed (broad minus targeted), with a Welch confidence
interval for the mean difference and a Hedges-Olkin interval for d. Both use the
normal critical value 1.96 (n > 150 per arm here). Estimators live here, in the
study, never in surveyir.

If the mega-study checkout and its virtualenv (pandas, scipy) are present,
``run_original_script`` also runs the unmodified original script on the same files.
"""

from __future__ import annotations

import csv
import math
import os
import statistics as st
import subprocess
import tempfile
from pathlib import Path

from study import BY_SEGMENT, CONTROL, MEGA_STUDY, OUTCOME_COLUMN, TREATED

Z = 1.959963984540054


def read_qualtrics(path: Path) -> list[dict[str, str]]:
    """Rows of a Qualtrics export with three header rows (as ``skiprows=[1, 2]``)."""
    with open(path, newline="", encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    return rows[2:]


def _num(v: str | None) -> float | None:
    try:
        return float(v) if v not in (None, "") else None
    except ValueError:
        return None


def by_arm(rows: list[dict[str, str]]) -> dict[str, list[float]]:
    """Outcome values per arm key, arms identified from the recorded ``Segment``."""
    out: dict[str, list[float]] = {TREATED: [], CONTROL: []}
    for r in rows:
        arm, y = BY_SEGMENT.get(r.get("Segment", "")), _num(r.get(OUTCOME_COLUMN))
        if arm is not None and y is not None:
            out[arm.key].append(y)
    return out


def _pooled_sd(a: list[float], b: list[float]) -> float:
    n1, n2 = len(a), len(b)
    return math.sqrt(((n1 - 1) * st.variance(a) + (n2 - 1) * st.variance(b)) / (n1 + n2 - 2))


def contrast(rows: list[dict[str, str]]) -> dict:
    """mean(fair1 | broad shown) - mean(fair1 | targeted shown), with 95% CIs."""
    g = by_arm(rows)
    a, b = g[TREATED], g[CONTROL]
    if len(a) < 2 or len(b) < 2:
        return {"n_broad": len(a), "n_targeted": len(b)}
    diff = st.mean(a) - st.mean(b)
    se = math.sqrt(st.variance(a) / len(a) + st.variance(b) / len(b))
    sp = _pooled_sd(a, b)
    d = diff / sp if sp > 0 else float("nan")
    n1, n2 = len(a), len(b)
    se_d = math.sqrt((n1 + n2) / (n1 * n2) + d * d / (2 * (n1 + n2)))
    return {
        "n_broad": n1, "n_targeted": n2,
        "mean_broad": st.mean(a), "mean_targeted": st.mean(b),
        "sd_broad": st.stdev(a), "sd_targeted": st.stdev(b),
        "diff": diff, "diff_lo": diff - Z * se, "diff_hi": diff + Z * se,
        "d": d, "d_lo": d - Z * se_d, "d_hi": d + Z * se_d,
    }


def marginal(rows: list[dict[str, str]]) -> dict:
    ys = [y for r in rows if (y := _num(r.get(OUTCOME_COLUMN))) is not None]
    counts = {str(k): sum(1 for y in ys if int(y) == k) for k in range(1, 10)}
    return {"n": len(ys), "mean": st.mean(ys) if ys else float("nan"),
            "sd": st.stdev(ys) if len(ys) > 1 else float("nan"), "counts": counts}


def original_d(rows: list[dict[str, str]], order: list[str] | None = None) -> float:
    """Cohen's d exactly as the original script computes it: levels in order of first
    appearance (after ordering rows by ``order``, the human file's TWIN_ID order, as
    its ``pd.merge`` does), first level minus second, pooled SD."""
    if order is not None:
        pos = {t: k for k, t in enumerate(order)}
        rows = sorted((r for r in rows if r.get("TWIN_ID") in pos), key=lambda r: pos[r["TWIN_ID"]])
    pairs = [(r["Segment"], y) for r in rows
             if r.get("Segment") and (y := _num(r.get(OUTCOME_COLUMN))) is not None]
    levels = list(dict.fromkeys(s for s, _ in pairs))
    if len(levels) != 2:
        return float("nan")
    g1 = [y for s, y in pairs if s == levels[0]]
    g2 = [y for s, y in pairs if s == levels[1]]
    sp = _pooled_sd(g1, g2)
    return (st.mean(g1) - st.mean(g2)) / sp if sp > 0 else float("nan")


def run_original_script(human: Path, simulated: Path) -> dict | None:
    """Run the unmodified mega-study script with its own virtualenv, if present.

    Returns its ``effect size based on human/twin`` and ``mean_human/twin`` for fair1,
    or None when the checkout or its virtualenv is missing (the port above is then
    the only estimate; it is checked against the published human value either way).
    The script's own output (which includes individual-level human responses) goes to
    a temporary directory and is discarded, so no human data is written into studies/.
    """
    folder = MEGA_STUDY / "mega_study_evaluation" / "targeting_fairness"
    script = folder / "mega_study_evaluation.py"
    python = MEGA_STUDY / ".venv" / "bin" / "python"
    if not (script.exists() and python.exists()):
        return None
    with tempfile.TemporaryDirectory() as tmp:
        proc = subprocess.run(
            [str(python), str(script), "--human-data", str(human), "--twin-data",
             str(simulated), "--output-dir", tmp],
            capture_output=True, text=True, timeout=300,
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},  # leave the checkout untouched
        )
        if proc.returncode != 0:
            return {"error": proc.stderr[-2000:]}
        with open(Path(tmp) / "meta analysis.csv", newline="") as f:
            row = next(r for r in csv.DictReader(f) if r["variable name"] == OUTCOME_COLUMN)
    return {
        "effect_size_human": float(row["effect size based on human"]),
        "effect_size_twin": float(row["effect size based on twin"]),
        "mean_human": float(row["mean_human"]),
        "mean_twin": float(row["mean_twin"]),
        "sample_size": int(float(row["sample size"])),
    }
