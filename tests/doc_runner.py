"""Execute the code examples in the Markdown docs and check their printed output.

Conventions (rendered normally by GitHub):

* A ```python block is executed. All blocks in one file share a namespace and
  run with the repository root as the working directory.
* A ```text block directly after a ```python block is that block's expected
  output (what it prints).
* A ```console block holds ``$ surveyir ...`` commands, each followed by its
  expected output. ``| head -n N`` keeps the first N lines.
* ``<!-- no-run -->`` on the line before a fence skips that block (used for
  examples that need network access or an API key).

``scripts/update_docs.py`` rewrites the expected outputs from a real run;
``tests/test_docs.py`` fails if any output has drifted.
"""

from __future__ import annotations

import contextlib
import io
import os
import re
import shlex
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FENCE = re.compile(r"^```(\w*)\s*$")


@dataclass
class Block:
    lang: str
    start: int  # line index of the opening fence
    end: int  # line index of the closing fence
    body: list[str]
    skip: bool


def blocks(lines: list[str]) -> list[Block]:
    out, i = [], 0
    while i < len(lines):
        m = FENCE.match(lines[i])
        if m and m.group(1):
            j = i + 1
            while j < len(lines) and lines[j].rstrip() != "```":
                j += 1
            skip = i > 0 and lines[i - 1].strip() == "<!-- no-run -->"
            out.append(Block(m.group(1), i, j, lines[i + 1 : j], skip))
            i = j + 1
        else:
            i += 1
    return out


def _run_cli(command: str) -> str:
    from surveyir.cli import main

    head = None
    if "| head -n" in command:
        command, _, n = command.partition("| head -n")
        head = int(n.strip())
    argv = shlex.split(command)
    assert argv[0] == "surveyir", f"only surveyir commands can run: {command}"
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        main(argv[1:])
    lines = buf.getvalue().rstrip("\n").split("\n")
    return "\n".join(lines[:head] if head else lines)


def _normalize(text: str) -> str:
    return "\n".join(line.rstrip() for line in text.split("\n")).strip("\n")


def run_file(path: Path, update: bool = False) -> list[str]:
    """Run every example in ``path``; return mismatch descriptions (rewrite if ``update``)."""
    lines = path.read_text(encoding="utf-8").split("\n")
    found = blocks(lines)
    namespace: dict = {"__name__": "__docs__"}
    replacements: list[tuple[int, int, list[str]]] = []
    problems = []
    cwd = os.getcwd()
    os.chdir(ROOT)
    try:
        for k, b in enumerate(found):
            if b.skip:
                continue
            if b.lang == "python":
                buf = io.StringIO()
                with contextlib.redirect_stdout(buf):
                    exec(
                        compile("\n".join(b.body), f"{path.name}:{b.start + 1}", "exec"), namespace
                    )
                nxt = found[k + 1] if k + 1 < len(found) else None
                between = lines[b.end + 1 : nxt.start] if nxt else []
                if nxt and nxt.lang == "text" and all(not s.strip() for s in between):
                    actual = _normalize(buf.getvalue())
                    if actual != _normalize("\n".join(nxt.body)):
                        problems.append(f"{path.name}:{b.start + 1}: output differs")
                    replacements.append((nxt.start + 1, nxt.end, actual.split("\n")))
            elif b.lang == "console":
                new: list[str] = []
                for line in b.body:
                    if line.startswith("$ "):
                        new.append(line)
                        new.extend(_run_cli(line[2:]).split("\n"))
                if _normalize("\n".join(new)) != _normalize("\n".join(b.body)):
                    problems.append(f"{path.name}:{b.start + 1}: console output differs")
                replacements.append((b.start + 1, b.end, new))
    finally:
        os.chdir(cwd)
    if update:
        for start, end, new in sorted(replacements, reverse=True):
            lines[start:end] = new
        path.write_text("\n".join(lines), encoding="utf-8")
    return problems


def doc_files() -> list[Path]:
    return sorted((ROOT / "docs").rglob("*.md"))
