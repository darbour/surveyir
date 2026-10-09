# Data columns

`response_columns(survey)` lists every column a Qualtrics CSV export of the
survey would contain, with the column name (row 1 of the export) and the
ImportId object (row 3). Use it to:

- give simulated data the same layout as real data, so one analysis script works
  on both;
- build a codebook;
- match the columns of a real export to the questions they came from.

How well these rules hold up is measured against real exports in
[parity.md](../parity.md).

```python
import surveyir

survey = surveyir.load_qsf("tests/fixtures/qualtrics/obedient_twins.qsf")
columns = surveyir.response_columns(survey)
for c in columns[:8]:
    print(f"{c.name:<22} {c.part:<10} {c.import_object}")
```

```text
consent non_twins      response   {'ImportId': 'QID35'}
consent twins          response   {'ImportId': 'QID34'}
Q57                    response   {'ImportId': 'QID31_TEXT'}
Q4_First Click         timing     {'ImportId': 'QID4_FIRST_CLICK'}
Q4_Last Click          timing     {'ImportId': 'QID4_LAST_CLICK'}
Q4_Page Submit         timing     {'ImportId': 'QID4_PAGE_SUBMIT'}
Q4_Click Count         timing     {'ImportId': 'QID4_CLICK_COUNT'}
attitude_1             response   {'ImportId': 'QID3_1'}
```

Each `Column` also records where it came from (`question_id`, `choice_id`,
`row_id`, `answer_id`, `loop`) and its `part`: `response`, `text`,
`display_order`, `timing`, `meta`, `file`, `coordinate`, `region`, `score`,
`metadata` or `embedded_data`.

## Match columns on the ImportId object

Column names are not unique. Questions can share an export tag, and a heat map
gives several columns the same `ImportId`. Match on the whole `import_object`:

```python
heat = next(c for c in surveyir.response_columns(
    surveyir.load_qsf({"SurveyElements": [
        {"Element": "BL", "Payload": [{"Type": "Default", "ID": "BL_1",
            "BlockElements": [{"Type": "Question", "QuestionID": "QID7"}]}]},
        {"Element": "SQ", "Payload": {"QuestionID": "QID7", "QuestionType": "HeatMap",
            "Selector": "Image", "DataExportTag": "Q1", "QuestionText": "Click", "Clicks": 1}},
    ]})) if c.part == "coordinate")
print(heat.name, heat.import_object, heat.evidence, heat.verified)
```

```text
Q1_1_x {'ImportId': 'QID7', 'point': 1, 'coord': 'x'} vendor_docs True
```

`evidence` says how a naming rule is known: `corpus` (checked against real
exports), `vendor_docs` (from export screenshots on Qualtrics' support pages) or
`inferred`. `verified` is true for the first two.

## Export settings

The same survey exports differently depending on settings chosen at download
time. None of them are recorded in the .qsf. `ColumnOptions` takes them, under
their Qualtrics UI names or the v3 API names:

| `ColumnOptions` field | API name | effect |
|---|---|---|
| `split_multi_value` | `breakoutSets` | one column per choice for multi-select (default) or one comma-joined column |
| `display_order` | `includeDisplayOrder` | `"auto"` (follows the split setting), `"split"`, `"single"` or `"none"` |
| `slider_naming` | — | slider columns named by item id (UI exports) or by position (some API exports) |
| `include_metadata` | — | add `StartDate`, `ResponseId`, ... at the front |
| `question_ids` | `questionIds` | only these questions |
| `embedded_data_ids` | `embeddedDataIds` | only these embedded-data fields |

```python
opts = surveyir.ColumnOptions(breakoutSets=False, include_metadata=True)
names = [c.name for c in surveyir.response_columns(survey, options=opts)]
print(names[:4], "...", [n for n in names if n.startswith("Imagine1")])
```

```text
['StartDate', 'EndDate', 'Status', 'IPAddress'] ... ['Imagine1_DO']
```

## Reading a real export

`read_header` parses the three header rows of a Qualtrics CSV into
`(name, import object)` pairs, and `infer_options` recovers the settings the
export was made with. A minimal header looks like this:

```python
header_text = (
    "StartDate,Q57,Imagine1_DO_Q8\n"
    "Start Date,Please type...,Display Order\n"
    '"{""ImportId"":""startDate"",""timeZone"":""America/New_York""}",'
    '"{""ImportId"":""QID31_TEXT""}",'
    '"{""ImportId"":""BL_cMZntkAHYaNDzEy_DO"",""choiceId"":""Q8""}"\n'
)
print(surveyir.read_header(header_text))
```

```text
[('StartDate', {'ImportId': 'startDate', 'timeZone': 'America/New_York'}), ('Q57', {'ImportId': 'QID31_TEXT'}), ('Imagine1_DO_Q8', {'ImportId': 'BL_cMZntkAHYaNDzEy_DO', 'choiceId': 'Q8'})]
```

The repository keeps the header of the real export of this study (63 columns,
no response data) in `tests/fixtures/qualtrics/obedient_twins.columns.json`.
Inferring its settings and matching every column back to the survey:

```python
import json

real = [(c["column"], c["import_id"])
        for c in json.load(open("tests/fixtures/qualtrics/obedient_twins.columns.json"))]
opts = surveyir.infer_options(real)
print(opts.split_multi_value, opts.do_layout, opts.include_metadata, opts.time_zone)

def key(name, obj):  # compare whole import objects; time zones are an export setting
    return name, json.dumps({k: v for k, v in obj.items() if k != "timeZone"}, sort_keys=True)

predicted = {key(c.name, c.import_object)
             for c in surveyir.response_columns(survey, options=opts)}
unmatched = [name for name, obj in real if key(name, obj) not in predicted]
print(f"{len(real) - len(unmatched)} of {len(real)} real columns predicted exactly")
print("not predicted:", unmatched)
```

```text
True split True America/New_York
61 of 63 real columns predicted exactly
not predicted: ['TWIN_ID', '']
```

The two unpredicted columns were added by the dataset's publishers after export
(`TWIN_ID`, and one with an empty name). Neither has an ImportId.

## Labels

The second header row of a Qualtrics export holds a label per column.
`column_label(survey, column)` produces it, and `response_header_rows(survey)`
gives all three header rows (names, labels, ImportIds as compact JSON), with
`key=["TWIN_ID"]` appending key columns:

```python
for name, label, import_id in zip(*surveyir.response_header_rows(survey, key=["TWIN_ID"])):
    if name.startswith(("Q4_First", "FL_50_DO", "attitude", "TWIN")):
        print(f"{name:<16} {label[:60]!r:<64} {import_id}")
```

```text
Q4_First Click   'Timing - First Click'                                           {"ImportId":"QID4_FIRST_CLICK"}
attitude_1       'To what extent do you agree with the proposition, "America s'   {"ImportId":"QID3_1"}
FL_50_DO_FL_51   'FL_50 - Block Randomizer - Display Order - FL_51'               {"ImportId":"FL_50_DO","choiceId":"FL_51"}
FL_50_DO_FL_52   'FL_50 - Block Randomizer - Display Order - FL_52'               {"ImportId":"FL_50_DO","choiceId":"FL_52"}
TWIN_ID          'TWIN_ID'                                                        {"ImportId":"TWIN_ID"}
```

The rules, read off real exports: question text with its markup stripped
(`<br>` is a newline, source spacing is kept, `${lm://Field/3}` becomes
`[Field-3]`; the export tag when the text is empty), then `` - `` and the part:
the choice, row, item or form field (as written in the source, markup and all,
except matrix rows of answer columns, which are stripped), `Selected Choice`
for choice questions with text-entry choices (their text columns are
`Question - Choice - Text`), `Display Order - <item>`, or the timing, meta-info
or file part. Flow randomizer display order reads
`FL_5 - Block Randomizer - Display Order - <arm>`, block display order
`<block description> - Display Order - <tag>`; embedded data and key columns are
their name; score columns the scoring category's name.

Checked against the 19 human exports of the Twin-2K-500 mega-study
(`tests/test_labels.py`, which runs when `SURVEYIR_TWIN_DAT` is set and reads
only the header rows): of 2,831 columns paired by ImportId, 2,806 labels are
reproduced exactly; the 25 others are the columns of one question whose text
contains `<div><br></div>`. Verified: the metadata columns Start Date, End Date,
Progress, Duration (in seconds), Finished and Recorded Date; single-answer and
split multi-select choice questions (Selected Choice and text columns too);
single-answer matrix rows; single-line and form text entry; slider; constant
sum; timing; meta info; choice, matrix, block and flow display order (split
layout); embedded data; score. Not verified: the other metadata labels
(`METADATA_LABELS`), multi-answer and text matrix cells, side by side, rank
order, drill down, file upload, signature, hot spot, heat map, highlight,
pick-group-rank, graphic slider, single-column display order, and piped text
other than `lm://`. Loop & Merge labels lead with the loop's first field value
(`TikTok - Question`), but Qualtrics varies this by column kind; the patterns
in the mega-study exports are reproduced, others are not checked.
