"""Download the external corpus listed in corpus/manifest.json.

    GITHUB_TOKEN=... uv run python scripts/fetch_corpus.py [--jobs 8]

* ``files``: Qualtrics .qsf files, saved to corpus/external/<sha>.qsf.
* ``exports``: Qualtrics CSV exports. Only the column-name row and the ImportId
  row are kept (as corpus/external_columns/<sha>.json); response rows are
  discarded without being written to disk.

Everything is fetched by git blob SHA from the GitHub API, so it is exactly the
version that was tested. The files belong to their authors and are not
redistributed by this project. A GITHUB_TOKEN avoids the anonymous rate limit.
"""

from __future__ import annotations

import argparse
import base64
import csv
import io
import json
import os
import sys
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "corpus" / "manifest.json"
QSF_DIR = ROOT / "corpus" / "external"
COLUMNS_DIR = ROOT / "corpus" / "external_columns"


def _blob(entry: dict, token: str | None) -> bytes | None:
    url = f"https://api.github.com/repos/{entry['repo']}/git/blobs/{entry['sha']}"
    req = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json"})
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            return base64.b64decode(json.load(resp)["content"])
    except (urllib.error.HTTPError, urllib.error.URLError, KeyError):
        return None


def export_header(data: bytes) -> list[dict] | None:
    """Column names paired with their ImportId cells; None if not a Qualtrics export."""
    text = data.decode("utf-8-sig", "replace")  # headers can be very wide
    rows: list[list[str]] = []
    try:
        for row in csv.reader(io.StringIO(text, newline="")):
            rows.append(row)
            if len(rows) >= 5:
                break
    except csv.Error:
        return None
    imp = next((i for i, r in enumerate(rows[1:], 1) if any("ImportId" in c for c in r)), None)
    if imp is None:
        return None
    columns = []
    for name, cell in zip(rows[0], rows[imp], strict=False):
        try:
            import_id = json.loads(cell)
        except json.JSONDecodeError:
            import_id = {"raw": cell}
        columns.append({"column": name, "import_id": import_id})
    return columns


def fetch_qsf(entry: dict, token: str | None) -> str:
    dest = QSF_DIR / f"{entry['sha']}.qsf"
    if dest.exists() and dest.stat().st_size:
        return "cached"
    data = _blob(entry, token)
    if data is None:
        return "failed"
    dest.write_bytes(data)
    return "downloaded"


def fetch_export(entry: dict, token: str | None) -> str:
    dest = COLUMNS_DIR / f"{entry['sha']}.json"
    if dest.exists() and dest.stat().st_size:
        return "cached"
    data = _blob(entry, token)
    columns = export_header(data) if data is not None else None
    if columns is None:
        return "failed"
    dest.write_text(json.dumps({**entry, "columns": columns}))
    return "downloaded"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--jobs", type=int, default=8)
    parser.add_argument("--no-exports", action="store_true", help="Only fetch .qsf files.")
    args = parser.parse_args()
    manifest = json.loads(MANIFEST.read_text())
    QSF_DIR.mkdir(parents=True, exist_ok=True)
    COLUMNS_DIR.mkdir(parents=True, exist_ok=True)
    token = os.environ.get("GITHUB_TOKEN")
    jobs = [(fetch_qsf, e) for e in manifest["files"]]
    if not args.no_exports:
        jobs += [(fetch_export, e) for e in manifest.get("exports", [])]
    with ThreadPoolExecutor(args.jobs) as pool:
        results = list(pool.map(lambda job: job[0](job[1], token), jobs))
    summary: dict[str, int] = {}
    for r in results:
        summary[r] = summary.get(r, 0) + 1
    print(", ".join(f"{n} {k}" for k, n in sorted(summary.items())))
    # Export headers are a recall oracle; only missing .qsf files are fatal.
    qsf_failed = any(r == "failed" for r in results[: len(manifest["files"])])
    return 1 if qsf_failed else 0


if __name__ == "__main__":
    sys.exit(main())
