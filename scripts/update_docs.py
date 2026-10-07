"""Run every example in the docs and write its real output into the Markdown.

    uv run python scripts/update_docs.py

See tests/doc_runner.py for the conventions. tests/test_docs.py checks that the
outputs in the committed docs are still what the examples print.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tests.doc_runner import doc_files, run_file  # noqa: E402

if __name__ == "__main__":
    for path in doc_files():
        changed = run_file(path, update=True)
        print(f"{path.relative_to(ROOT)}: {len(changed)} output(s) updated")
