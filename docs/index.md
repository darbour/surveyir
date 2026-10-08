# surveyir documentation

surveyir reads survey instruments (currently Qualtrics `.qsf` exports) into one
typed, documented structure. It writes that structure to other formats, describes
the experimental design, and simulates respondents with the same data layout as a
real Qualtrics export.

surveyir makes three separate claims, with different evidence: the IR
preserves the information in the source file; a strict simulation administers
the instrument faithfully (checked by hand-written exact traces); and the
behavioral validity of simulated responses, which is out of scope for the core
and needs separate empirical validation. See [Design](design.md#what-surveyir-claims).

Every code example in these docs is run by the test suite (`tests/test_docs.py`).
The output shown under an example is what it actually prints.

| Page | What it covers |
|---|---|
| [Quickstart](quickstart.md) | Install, load a survey, look at questions, export, data columns |
| [Loading surveys](guide/loading.md) | The survey structure, diagnostics, strict mode, `extras`, logic |
| [Exporting](guide/exporting.md) | JSON, Markdown and codebook exporters; writing your own |
| [Data columns](guide/data-columns.md) | Qualtrics export columns, export settings, reading real exports |
| [Experiments](guide/experiments.md) | What a survey randomizes: factors, arms, crossing, cells; exposure histories |
| [Simulating respondents](guide/simulation.md) | Running answerers (rules, random, LLMs) through a survey |
| [Command line](cli.md) | `inspect`, `convert`, `design`, `simulate`, `formats`, `schema` |
| [Design](design.md) | Why the library is built the way it is, and what it claims on what evidence |
| [Positioning](positioning.md) | How surveyir compares with related tools (ExploraTwin, EDSL, DDI-Lifecycle) |
| [Qualtrics parity](parity.md) | Exactly which Qualtrics features are covered, measured on real files |

Runnable scripts are in [`examples/`](../examples):

| Script | Shows |
|---|---|
| `examples/inspect_survey.py` | load a .qsf and print its questions, design and diagnostics |
| `examples/simulate_to_csv.py` | simulate respondents and write a Qualtrics-style CSV |
| `examples/custom_exporter.py` | write and use your own exporter |
| `examples/llm_respondents.py` | simulate LLM personas with Claude (needs an API key) |
