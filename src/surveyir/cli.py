"""Command-line interface: ``surveyir convert|inspect|formats|schema``."""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from . import available_exporters, available_loaders, get_exporter, load
from .model import Survey


def _parse_options(pairs: list[str]) -> dict[str, Any]:
    options: dict[str, Any] = {}
    for pair in pairs:
        key, sep, value = pair.partition("=")
        if not sep:
            raise SystemExit(f"--opt expects key=value, got {pair!r}")
        try:
            options[key] = json.loads(value)
        except json.JSONDecodeError:
            options[key] = value
    return options


def extras_keys(obj: Any, prefix: str = "") -> Counter[str]:
    """Count source keys preserved in ``extras`` anywhere in an IR tree."""
    counts: Counter[str] = Counter()
    if isinstance(obj, BaseModel):
        for k in getattr(obj, "extras", None) or {}:
            counts[f"{type(obj).__name__}.{k}"] += 1
        for name in type(obj).model_fields:
            if name != "extras":
                counts.update(extras_keys(getattr(obj, name)))
    elif isinstance(obj, (list, tuple)):
        for x in obj:
            counts.update(extras_keys(x))
    elif isinstance(obj, dict):
        for x in obj.values():
            counts.update(extras_keys(x))
    return counts


def cmd_convert(args: argparse.Namespace) -> int:
    survey = load(args.input, format=args.source_format, strict=args.strict)
    exporter = get_exporter(args.to, **_parse_options(args.opt))
    if args.output in (None, "-"):
        data = exporter.export(survey)
        if isinstance(data, bytes):
            sys.stdout.buffer.write(data)
        else:
            sys.stdout.write(data)
    else:
        exporter.write(survey, args.output)
    return 0


def _summary(survey: Survey) -> dict[str, Any]:
    kinds = Counter(q.kind for q in survey.questions.values())
    flow = Counter(n.type for n in survey.walk_flow())
    return {
        "name": survey.name,
        "questions": len(survey.questions),
        "blocks": len(survey.blocks),
        "unused_blocks": len(survey.unused_block_ids()),
        "question_kinds": dict(kinds.most_common()),
        "flow_nodes": dict(flow.most_common()),
        "languages": survey.languages,
        "diagnostics": [d.model_dump(exclude_none=True) for d in survey.diagnostics],
    }


def cmd_inspect(args: argparse.Namespace) -> int:
    reports = []
    totals: Counter[str] = Counter()
    worst = 0
    for path in args.inputs:
        survey = load(path, format=args.source_format)
        report = {"file": str(path), **_summary(survey)}
        if args.extras:
            keys = extras_keys(survey)
            totals.update(keys)
            report["extras_keys"] = dict(sorted(keys.items()))
        reports.append(report)
        if any(d.level != "info" for d in survey.diagnostics):
            worst = 1
    if args.json:
        json.dump(reports if len(reports) > 1 else reports[0], sys.stdout, indent=2)
        sys.stdout.write("\n")
        return worst
    for r in reports:
        print(f"{r['file']}: {r['name']!r}")
        print(
            f"  {r['questions']} questions, {r['blocks']} blocks ({r['unused_blocks']} not in flow)"
        )
        print("  kinds: " + ", ".join(f"{k}={v}" for k, v in r["question_kinds"].items()))
        print("  flow:  " + ", ".join(f"{k}={v}" for k, v in r["flow_nodes"].items()))
        for d in r["diagnostics"]:
            if d["level"] != "info" or args.verbose:
                print(
                    f"  {d['level']:7s} {d['code']}: {d['message']}"
                    + (f" [{d['location']}]" if d.get("location") else "")
                )
    if args.extras:
        print("\nSource keys preserved in extras (not yet typed), across all inputs:")
        for key, n in sorted(totals.items()):
            print(f"  {n:6d}  {key}")
    return worst


def cmd_formats(args: argparse.Namespace) -> int:
    print("Input formats:")
    for name, cls in available_loaders().items():
        print(f"  {name:12s} {', '.join(getattr(cls, 'extensions', ()))}")
    print("Output formats:")
    for name, cls in available_exporters().items():
        print(f"  {name:12s} {cls.summary}")
    return 0


def cmd_schema(args: argparse.Namespace) -> int:
    schema = Survey.model_json_schema()
    text = json.dumps(schema, indent=2)
    if args.output:
        Path(args.output).write_text(text + "\n", encoding="utf-8")
    else:
        print(text)
    return 0


def cmd_design(args: argparse.Namespace) -> int:
    from .runtime import design

    survey = load(args.input, format=args.source_format)
    d = design(survey)
    if args.json:
        print(d.model_dump_json(indent=2))
    else:
        print(d.summary() or "No randomization.")
    return 0


def cmd_simulate(args: argparse.Namespace) -> int:
    import csv

    from .columns import ColumnOptions, response_columns
    from .runtime import RandomAnswerer, Simulator

    survey = load(args.input, format=args.source_format)
    options = ColumnOptions(include_metadata=True)
    sim = Simulator(survey, seed=args.seed)
    answerer = RandomAnswerer(seed=args.seed)
    names = [c.name for c in response_columns(survey, options=options)]

    def write(out) -> None:  # positional, so repeated export tags keep both columns
        writer = csv.writer(out)
        writer.writerow(names)
        for _ in range(args.n):
            writer.writerow([v for _, v in sim.respondent(answerer).cells(survey, options)])

    if args.output:
        with open(args.output, "w", newline="", encoding="utf-8") as f:
            write(f)
    else:
        write(sys.stdout)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="surveyir", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("convert", help="Convert a survey file to another format.")
    p.add_argument("input")
    p.add_argument("-t", "--to", default="json", help="Output format (see `formats`).")
    p.add_argument("-o", "--output", help="Output path (default: stdout).")
    p.add_argument(
        "-f", "--from", dest="source_format", help="Input format (default: by extension)."
    )
    p.add_argument("--strict", action="store_true", help="Fail if anything can't be represented.")
    p.add_argument(
        "--opt",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="Exporter option; repeatable. Values are parsed as JSON when possible.",
    )
    p.set_defaults(func=cmd_convert)

    p = sub.add_parser("inspect", help="Summarize surveys and report load diagnostics.")
    p.add_argument("inputs", nargs="+")
    p.add_argument("-f", "--from", dest="source_format")
    p.add_argument("--extras", action="store_true", help="List source keys kept only in extras.")
    p.add_argument("--json", action="store_true")
    p.add_argument("-v", "--verbose", action="store_true", help="Include info diagnostics.")
    p.set_defaults(func=cmd_inspect)

    p = sub.add_parser("design", help="Describe the experimental design (randomization).")
    p.add_argument("input")
    p.add_argument("-f", "--from", dest="source_format")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_design)

    p = sub.add_parser(
        "simulate",
        help="Simulate respondents with random answers; writes a Qualtrics-style CSV.",
    )
    p.add_argument("input")
    p.add_argument("-n", type=int, default=100, help="Number of respondents.")
    p.add_argument("--seed", type=int, default=None)
    p.add_argument("-o", "--output", help="CSV path (default: stdout).")
    p.add_argument("-f", "--from", dest="source_format")
    p.set_defaults(func=cmd_simulate)

    p = sub.add_parser("formats", help="List input and output formats.")
    p.set_defaults(func=cmd_formats)

    p = sub.add_parser("schema", help="Print the IR JSON Schema.")
    p.add_argument("-o", "--output")
    p.set_defaults(func=cmd_schema)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
