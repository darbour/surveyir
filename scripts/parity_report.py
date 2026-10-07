"""Regenerate docs/parity.md.

    uv run python scripts/parity_report.py

Uses the committed fixtures and, if downloaded (scripts/fetch_corpus.py), the
external corpus of public QSFs and export headers. The checklist of Qualtrics
question types is maintained by hand; every count is computed.
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from surveyir import load_qsf  # noqa: E402
from surveyir.cli import extras_keys  # noqa: E402
from tests.column_oracle import evaluate  # noqa: E402

FIXTURES = ROOT / "tests" / "fixtures" / "qualtrics"
EXTERNAL = ROOT / "corpus" / "external"

#: Qualtrics' documented question types (support page "Question Types Overview"),
#: with the source QuestionType/Selector that identifies them in a .qsf.
CHECKLIST = [
    ("Multiple choice", "MC", None, "choice"),
    ("Net Promoter Score", "MC", "NPS", "choice (layout nps)"),
    ("Text entry", "TE", None, "text_entry"),
    ("Form field", "TE", "FORM", "text_entry (mode form)"),
    ("Text / graphic", "DB", None, "descriptive"),
    ("Matrix table", "Matrix", None, "matrix"),
    ("Slider", "Slider", "HSLIDER", "slider"),
    ("Number scale", "Slider", "NumberScale", "slider (mode number_scale)"),
    ("Rank order", "RO", None, "rank_order"),
    ("Side by side", "SBS", None, "side_by_side"),
    ("Constant sum", "CS", None, "constant_sum"),
    ("Pick, group and rank", "PGR", None, "pick_group_rank"),
    ("Drill down", "DD", None, "drill_down"),
    ("Graphic slider", "SS", None, "graphic_slider"),
    ("Heat map", "HeatMap", None, "heat_map"),
    ("Hot spot", "HotSpot", None, "hot_spot"),
    ("Highlight", "HL", None, "highlight"),
    ("File upload", "FileUpload", None, "file_upload"),
    ("Signature", "Draw", None, "signature"),
    ("Timing", "Timing", None, "timing"),
    ("Meta info", "Meta", None, "meta_info"),
    ("Captcha verification", "Captcha", None, "captcha"),
]

#: Types with no public payload. Codes come from the Qualtrics API spec and the
#: survey-taking runtime (tests/reference/qualtrics_types.json).
UNMODELED = [
    ("Calendar", "`TE/Calendar*`", "runtime render key"),
    ("Screen capture", "`FileUpload/ScreenCapture`", "API enum + runtime"),
    ("Video response", "`FileUpload/VideoCapture/VideoToText`", "runtime render key"),
    ("Org hierarchy", "`TreeSelect/*`", "API enum + runtime"),
    ("Location selector", "`TE/AUTO/SDS` (probable)", "runtime render key; weak"),
    ("ArcGIS map", "unknown; probably a plugin", "absent from API and runtime"),
    ("Tree testing", "unknown; probably a plugin", "absent from API and runtime"),
    ("Solicit reviews", "unknown; probably a plugin", "absent from API and runtime"),
    ("Unmoderated user testing", "unknown; probably a plugin", "absent from API and runtime"),
]

OTHER_FEATURES = """\
| Area | Qualtrics feature | IR | Status |
|---|---|---|---|
| Choices | Recode values, variable naming, per-choice export tags | `Choice.recode` / `variable_name` / `export_tag` | typed |
| | "Other, specify" text entry (+ required, validation), exclusive answers | `Choice.text_entry*`, `exclusive`, `ScalePoint.exclusive` | typed |
| | Choice display logic, choice groups | `Choice.display_logic`, `ChoiceQuestion.groups` | typed |
| | Carry forward (question choice groups, reference lists) | `CarryForward` | typed |
| | Randomization: all, subset, advanced, scale reversal | `Randomization` | typed |
| | Scoring (grading data, categories) | `Score`, `ScoringCategory` | typed |
| Validation | Force / request response; content type; number range; char min/max/range; choice min/max/range; totals; file type | `Validation` | typed |
| | Custom validation logic + message | `Validation.custom` | typed |
| | Consecutive-number and AI text-analysis checks | `Validation.extras` | preserved |
| Logic | Display, in-page display, branch, skip (incl. value comparisons), custom validation, quota | `Condition` (`And`/`Or`/`Comparison`) | typed; AND binds tighter than OR |
| | Operands: question (choice, cell, answer, text, number, displayed, counts), embedded data, loop field, device, GeoIP, quota, constant | `Operand` | typed |
| | Loop-aware evaluation (`LoopAndMergeLoops`, `evaluateAllInArrayFunction`) | `Comparison.extras` | preserved |
| | Hidden / incomplete ("Invalid Logic") expressions | dropped / kept with diagnostic | |
| Blocks | Question order, page breaks, skip logic | `Block.elements` | typed |
| | Question randomization (all, per page, subset, advanced) | `Block.randomization` | typed |
| | Loop & Merge (static table, question-driven, numeric response) | `LoopAndMerge` | typed |
| | Trash | dropped | |
| Flow | Block, embedded data (custom / recipient / panel), branch, randomizer (current and legacy), group, end of survey (redirect, message, screen-out flags) | typed nodes | typed |
| | Web service (URL, method, request params, response → field map) | `WebServiceNode` | typed |
| | Library blocks (`ReferenceSurvey`) | `LibraryBlockNode` | typed; contents live in the library, not the file |
| | Quota check, authenticator, table of contents | typed nodes | typed (details in `extras`) |
| Survey | Quotas (limit, condition, action) | `Survey.quotas` | typed |
| | Translations | `Question.translations` | typed |
| | Options, header/footer, notes, triggers, conjoint definitions | `Survey.options` / `extras` | preserved |
| Text | HTML → plain text; piped text (`q://`, `e://`, `lm://`, `gr://`, `rand://`, `date://`, loop prefixes) | `Text.plain`, `Text.pipes` | typed |
| Robustness | IDs as numbers or strings; id maps written as JSON arrays; one-block-per-element exports; untagged elements; missing flow/blocks | normalized | diagnostics where data is reconstructed |
"""


def _table(counter: Counter, head: str, limit: int | None = None) -> str:
    rows = "\n".join(f"| `{k}` | {v} |" for k, v in counter.most_common(limit))
    return f"| {head} | count |\n|---|---|\n{rows}\n"


def _raw_types(paths: list[Path]) -> Counter[tuple[str, str]]:
    """(QuestionType, Selector) of every SQ element, including trashed questions."""
    counts: Counter[tuple[str, str]] = Counter()
    for path in paths:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        for el in data.get("SurveyElements") or []:
            payload = el.get("Payload") if isinstance(el, dict) else None
            if isinstance(payload, dict) and el.get("Element") == "SQ":
                counts[(str(payload.get("QuestionType")), str(payload.get("Selector")))] += 1
    return counts


def _scan(paths: list[Path]):
    origins: Counter[tuple[str, str]] = Counter()
    kinds: Counter[str] = Counter()
    flow: Counter[str] = Counter()
    diags: Counter[str] = Counter()
    extras: Counter[str] = Counter()
    unsupported: Counter[str] = Counter()
    n_q = 0
    for path in paths:
        survey = load_qsf(path)
        n_q += len(survey.questions)
        for q in survey.questions.values():
            kinds[q.kind] += 1
            origins[(q.origin.type, q.origin.selector or "")] += 1
            if q.kind == "unsupported":
                unsupported[f"{q.origin.type}/{q.origin.selector}"] += 1
        for node in survey.walk_flow():
            flow[node.type] += 1
        for d in survey.diagnostics:
            diags[f"{d.level}: {d.code}"] += 1
        survey.extras = {}
        extras.update(extras_keys(survey))
    return n_q, origins, kinds, flow, diags, extras, unsupported


def main() -> None:
    fixtures = sorted(FIXTURES.glob("*.qsf"))
    external = sorted(EXTERNAL.glob("*.qsf")) if EXTERNAL.exists() else []
    paths = fixtures + external
    n_q, origins, kinds, flow, diags, extras, unsupported = _scan(paths)

    raw = _raw_types(paths)

    def examples(counter: Counter, qtype: str | None, selector: str | None) -> int:
        if qtype is None:
            return 0
        return sum(v for (t, s), v in counter.items() if t == qtype and (selector in (None, s)))

    checklist = [
        "| Qualtrics type | Source type | IR kind | Live questions | All examples (incl. trash) |",
        "|---|---|---|---|---|",
    ]
    for name, qtype, selector, kind in CHECKLIST:
        src = f"`{qtype}{'/' + selector if selector else ''}`" if qtype else "—"
        live, every = examples(origins, qtype, selector), examples(raw, qtype, selector)
        checklist.append(f"| {name} | {src} | `{kind}` | {live:,} | {every:,} |")
    unmodeled = [
        "| Qualtrics type | Code | Evidence for the code |",
        "|---|---|---|",
        *[f"| {n} | {code} | {ev} |" for n, code, ev in UNMODELED],
    ]

    out = [
        "# Qualtrics parity",
        "",
        "Generated by `scripts/parity_report.py`; do not edit by hand.",
        "",
        "**What parity means here:** every Qualtrics question type and flow element that "
        "appears in a real export has a typed model. Editor and display settings stay in "
        "`extras` by design, so nothing is lost. A type with no public example loads "
        "as `unsupported` with its payload intact and a warning. It is never coerced into "
        "another type. `tests/known_gaps.json` lists the gaps CI tolerates (currently none).",
        "",
        f"**Corpus:** {len(fixtures)} committed fixtures + {len(external)} public QSFs from "
        f"GitHub (`corpus/manifest.json`), {n_q:,} live questions in total.",
        "",
    ]
    if external:
        paired, counts = evaluate(ROOT)
        hit = sum(v for (_, ok), v in counts.items() if ok)
        total = sum(counts.values())
        out += [
            f"**Data columns:** {hit:,} of {total:,} columns ({100 * hit / total:.2f}%) "
            f"in {paired:,} real Qualtrics CSV exports (questions, metadata, scores and "
            "display order) are reproduced exactly by `response_columns`. Misses are survey "
            "revisions the pairing cannot detect.",
            "",
            "| IR kind | columns verified | recall |",
            "|---|---|---|",
            *[
                f"| `{k}` | {counts[(k, True)]:,} / {counts[(k, True)] + counts[(k, False)]:,} "
                f"| {100 * counts[(k, True)] / (counts[(k, True)] + counts[(k, False)]):.1f}% |"
                for k in sorted({k for k, _ in counts})
            ],
            "",
            "Columns are compared on the name and the whole third header row object "
            "(`ImportId` plus `choiceId`, `point`, `coord`). Text iQ analytics and label "
            "columns cannot be predicted from a .qsf and are excluded.",
            "",
            "Rules confirmed only by export screenshots on Qualtrics' support pages "
            '(`Column.evidence == "vendor_docs"`): signature, heat map, highlight, hot spot, '
            "graphic slider. Constant sum and rank order are verified only where item position "
            "equals item id. Slider columns are named by item id in UI exports and by position "
            "in some API exports; the .qsf cannot tell which (`ColumnOptions.slider_naming`).",
            "",
        ]
    out += [
        "## Question types",
        "",
        "\n".join(checklist),
        "",
        "### Types with no public example",
        "",
        "These load as `unsupported`, payload verbatim, with a warning that names the "
        "probable type. A question carrying `QuestionTypePluginProperties` (Qualtrics' "
        "question-type plugin mechanism) also loads as `unsupported` (`plugin-question`). "
        "One exported example of each would be enough to model it.",
        "",
        "\n".join(unmodeled),
        "",
        "## Other features",
        "",
        OTHER_FEATURES,
        "## Corpus statistics",
        "",
        "### IR question kinds",
        "",
        _table(kinds, "kind"),
        "### Flow nodes",
        "",
        _table(flow, "node"),
        "### Unsupported question types",
        "",
        _table(unsupported, "type/selector") if unsupported else "None.\n",
        "### Load diagnostics",
        "",
        "Warnings other than `unsupported-*` describe defects in the source files "
        "(dangling references, incomplete logic, hand-edited exports).",
        "",
        _table(diags, "diagnostic"),
        "## Source keys kept only in `extras` (top 40)",
        "",
        "These load losslessly but have no typed field. Most are editor or display settings. "
        "Promote a key when a consumer needs it.",
        "",
        _table(extras, "IR node . source key", 40),
    ]
    (ROOT / "docs" / "parity.md").write_text("\n".join(out), encoding="utf-8")
    print("wrote docs/parity.md")


if __name__ == "__main__":
    main()
