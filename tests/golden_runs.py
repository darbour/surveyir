"""Seeded simulator output for every fixture study, as plain JSON.

The golden snapshot pins what the runtime does under fixed seeds, so changes
that should not alter administration (recording traces, page handling for
surveys without same-page dependencies, strict mode) are checked against it.
Runs are permissive (``strict=False``): several fixtures have question
JavaScript or unsupplied panel fields, which a strict run stops on.
Regenerate only for a deliberate behaviour change:

    uv run python tests/golden_runs.py
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from surveyir import load
from surveyir.runtime import RandomAnswerer, Simulator

FIXTURES = Path(__file__).parent / "fixtures"
GOLDEN = FIXTURES / "golden" / "runtime.json"
SEED, ANSWER_SEED, N = 11, 3, 12


def _key(k: tuple) -> str:
    return "|".join("" if p is None else str(p) for p in k)


def _plain(value: Any) -> Any:
    return json.loads(json.dumps(value, default=list, sort_keys=True))


def snapshot(path: Path, **simulator: Any) -> list[dict[str, Any]]:
    survey = load(path)
    sim = Simulator(survey, seed=SEED, **simulator)
    out = []
    for run in sim.run(N, RandomAnswerer(seed=ANSWER_SEED)):
        out.append(
            _plain(
                {
                    "displayed": [_key(k) for k in run.displayed],
                    "flow_order": run.flow_order,
                    "choice_order": {_key(k): v for k, v in run.choice_order.items()},
                    "column_order": {_key(k): v for k, v in run.column_order.items()},
                    "block_order": {_key(k): v for k, v in run.block_order.items()},
                    "loops": run.loops,
                    "embedded": run.embedded,
                    "answers": {_key(k): [a.value, a.text] for k, a in run.answers.items()},
                    "finished": run.finished,
                    "ended_by": run.ended_by,
                }
            )
        )
    return out


def all_snapshots(**simulator: Any) -> dict[str, list[dict[str, Any]]]:
    return {
        p.stem: snapshot(p, **simulator) for p in sorted((FIXTURES / "qualtrics").glob("*.qsf"))
    }


if __name__ == "__main__":
    import warnings

    warnings.filterwarnings("ignore")
    GOLDEN.write_text(json.dumps(all_snapshots(strict=False), indent=1, sort_keys=True) + "\n")
    print(f"wrote {GOLDEN}")
