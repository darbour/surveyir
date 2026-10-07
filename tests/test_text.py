from __future__ import annotations

from surveyir.text import html_to_text, make_text


def test_paragraphs_breaks_and_entities():
    html = "<p>Hello&nbsp;<b>world</b></p><p>Line one<br>Line two &amp; more</p>"
    assert html_to_text(html) == "Hello world\n\nLine one\nLine two & more"


def test_lists_and_tables():
    html = "<ul><li>a</li><li>b</li></ul><table><tr><td>x</td><td>y</td></tr></table>"
    assert html_to_text(html) == "- a\n- b\n\nx | y"


def test_scripts_are_dropped():
    assert html_to_text("<script>var a = 1;</script>Visible") == "Visible"


def test_plain_text_passes_through():
    assert html_to_text("  just text  ") == "just text"


def test_pipes_are_parsed_and_deduplicated():
    t = make_text(
        "Hi ${e://Field/name}! You chose ${q://QID5/ChoiceGroup/SelectedChoices} "
        "and wrote ${q://2_QID7/ChoiceTextEntryValue/4}. ${e://Field/name} "
        "Loop ${lm://Field/1} of ${lm://TotalLoops}"
    )
    kinds = [
        (p.kind, p.question_id, p.loop_iteration, p.selector, p.choice_id, p.name) for p in t.pipes
    ]
    assert kinds == [
        ("embedded_data", None, None, None, None, "name"),
        ("question", "QID5", None, "ChoiceGroup/SelectedChoices", None, None),
        ("question", "QID7", 2, "ChoiceTextEntryValue", "4", None),
        ("loop_merge", None, None, None, None, "1"),
        ("loop_merge", None, None, None, None, "TotalLoops"),
    ]


def test_render_substitutes_known_pipes_only():
    t = make_text("<p>Hello ${e://Field/name}, ${e://Field/unknown}</p>")
    out = t.render(lambda p: "Ada" if p.name == "name" else None)
    assert out == "Hello Ada, ${e://Field/unknown}"
