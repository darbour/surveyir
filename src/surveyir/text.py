"""Text utilities shared by loaders: HTML to plain text and piped-text parsing."""

from __future__ import annotations

import re
from html.parser import HTMLParser

from .model.text import PIPE_PATTERN, Pipe, Text

_BLOCK_TAGS = {
    "p",
    "div",
    "br",
    "tr",
    "table",
    "ul",
    "ol",
    "li",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "blockquote",
    "section",
    "article",
    "header",
    "footer",
    "hr",
}
_CELL_TAGS = {"td", "th"}
_SKIP_TAGS = {"script", "style", "head", "title"}


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0
        self._cell_in_row = 0

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag in _SKIP_TAGS:
            self._skip += 1
        elif tag == "tr":
            self.parts.append("\n")
            self._cell_in_row = 0
        elif tag in _CELL_TAGS:
            if self._cell_in_row:
                self.parts.append(" | ")
            self._cell_in_row += 1
        elif tag == "li":
            self.parts.append("\n- ")
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")
        elif tag == "img":
            alt = dict(attrs).get("alt")
            if alt:
                self.parts.append(f"[image: {alt}]")

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP_TAGS:
            self._skip = max(0, self._skip - 1)
        elif tag in _BLOCK_TAGS and tag not in ("br", "li", "tr"):
            self.parts.append("\n")

    def handle_startendtag(self, tag: str, attrs) -> None:
        self.handle_starttag(tag, attrs)

    def handle_data(self, data: str) -> None:
        if not self._skip:
            self.parts.append(data)


def html_to_text(html: str) -> str:
    """Convert survey-editor HTML into readable plain text.

    Paragraphs and line breaks become newlines, list items become ``- `` lines,
    table cells are joined with `` | `` per row, and scripts/styles are dropped.
    """
    if not html:
        return ""
    if "<" not in html and "&" not in html:
        text = html
    else:
        parser = _TextExtractor()
        parser.feed(html)
        parser.close()
        text = "".join(parser.parts)
    text = text.replace("\xa0", " ").replace("\r\n", "\n").replace("\r", "\n")
    lines = [re.sub(r"[ \t\f\v]+", " ", line).strip() for line in text.split("\n")]
    text = "\n".join(lines)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def parse_pipe(raw: str, scheme: str, path: str) -> Pipe:
    """Parse one piped-text reference (``scheme://path``) into a ``Pipe``."""
    parts = path.split("/")
    fields: dict = {"raw": raw, "scheme": scheme, "path": path}
    s = scheme.lower()
    if s == "q":
        head = parts[0]
        m = re.fullmatch(r"(?:(\d+)_)?(QID\d+.*)", head)
        if m:
            fields["loop_iteration"] = int(m.group(1)) if m.group(1) else None
            fields["question_id"] = m.group(2)
        else:
            fields["question_id"] = head
        rest = parts[1:]
        if len(rest) >= 2 and rest[-1].isdigit():
            fields["choice_id"] = rest[-1]
            rest = rest[:-1]
        fields["selector"] = "/".join(rest) or None
        return Pipe(kind="question", **fields)
    if s == "e":
        name = "/".join(parts[1:]) if parts[0] == "Field" else path
        return Pipe(kind="embedded_data", name=name, **fields)
    if s == "lm":
        name = "/".join(parts[1:]) if parts[0] == "Field" else parts[0]
        return Pipe(kind="loop_merge", name=name, **fields)
    if s == "gr":
        return Pipe(kind="scoring", name=parts[0], selector="/".join(parts[1:]) or None, **fields)
    if s == "rand":
        return Pipe(kind="random", selector=path, **fields)
    if s == "date":
        return Pipe(kind="date", selector=path, **fields)
    return Pipe(kind="other", **fields)


def make_text(html: str | None) -> Text:
    """Build a ``Text`` from source markup, extracting every piped reference."""
    html = html or ""
    if not isinstance(html, str):
        html = str(html)
    plain = html_to_text(html)
    pipes: list[Pipe] = []
    seen: set[str] = set()
    for m in PIPE_PATTERN.finditer(html):
        if m.group(0) in seen:
            continue
        seen.add(m.group(0))
        pipes.append(parse_pipe(m.group(0), m.group("scheme"), m.group("path")))
    return Text(html=html, plain=plain, pipes=pipes)
