"""A control's accessible name contains the words its visible label shows.

WCAG 2.1 SC 2.5.3 (Label in Name, level A). An ``aria-label`` replaces the name a
``<Label htmlFor>`` would give, so when the two say different things the screen
says one and the control answers to the other: a speech-input user saying
"click Held for" reaches nothing, and a screen reader announces "Demand type"
under a heading that reads "Held for". Five controls shipped like that - the
reservation's "Held for", the tracking event's "What happened", the RFQ's
"Raised against" and both durations on every alert rule.

The rule pairs each ``<Label htmlFor=X>`` whose text is static with the element
carrying ``id=X`` in the same file. When that element also has a readable
``aria-label``, the label's words must appear in it (case aside). Containment is
the criterion; "BOM type" for a label reading "Type" passes. A trailing
parenthetical such as "(optional)" is a hint rather than part of the name, so it
is not required.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

WEB_SRC = Path(__file__).resolve().parents[3] / "web" / "src"

_OPENING = re.compile(r"<([A-Za-z][\w.]*)(?=[\s>/])")
_HINT = re.compile(r"\s*\([^()]*\)$")
_PLACEHOLDER = re.compile(r"\$\{[^}]*\}")


def _tag_end(text: str, start: int) -> int:
    """Index of the ``>`` closing the JSX opening tag that begins at ``start``.

    Strings and comments are skipped: a handler's comment saying "the vendor's
    equipment" would otherwise open a quote that runs into the next tag.
    """
    depth = 0
    quote: str | None = None
    index = start
    while index < len(text):
        char = text[index]
        if quote:
            if char == "\\":
                index += 2
                continue
            if char == quote:
                quote = None
        elif text.startswith("//", index):
            newline = text.find("\n", index)
            index = len(text) if newline == -1 else newline
            continue
        elif text.startswith("/*", index):
            close = text.find("*/", index + 2)
            index = len(text) if close == -1 else close + 2
            continue
        elif char in "\"'`":
            quote = char
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
        elif char == ">" and depth == 0:
            return index
        index += 1
    return index


def _attributes(tag: str) -> dict[str, str | None]:
    """The tag's own attributes: the static or template text, or ``None`` for an expression.

    Only the top level is read. A prop such as ``toolbar={<>...</>}`` holds whole
    elements, and searching the tag's text would pair one nested element's ``id``
    with another's ``aria-label``.
    """
    found: dict[str, str | None] = {}
    name = _OPENING.match(tag)
    index = name.end() if name else 0
    while index < len(tag):
        if tag[index].isspace():
            index += 1
            continue
        if tag.startswith("//", index):
            newline = tag.find("\n", index)
            index = len(tag) if newline == -1 else newline
            continue
        if tag.startswith("/*", index):
            close = tag.find("*/", index + 2)
            index = len(tag) if close == -1 else close + 2
            continue
        if tag[index] == "{":
            # A spread such as {...field} names no attribute this rule reads.
            index = _closing_brace(tag, index)
            continue
        attribute = re.match(r"[\w:-]+", tag[index:])
        if not attribute:
            break
        key = attribute.group(0)
        index += len(key)
        if not tag.startswith("=", index):
            found[key] = None
            continue
        index += 1
        if tag[index : index + 1] in ("'", '"'):
            close = tag.find(tag[index], index + 1)
            if close == -1:
                break
            found[key] = tag[index + 1 : close]
            index = close + 1
        elif tag.startswith("{", index):
            end = _closing_brace(tag, index)
            literal = re.fullmatch(
                r"\s*(?:`([^`]*)`|'([^']*)'|\"([^\"]*)\")\s*", tag[index + 1 : end - 1]
            )
            found[key] = (
                next(group for group in literal.groups() if group is not None) if literal else None
            )
            index = end
        else:
            break
    return found


def _closing_brace(text: str, opening: int) -> int:
    """Index just past the ``}`` matching the ``{`` at ``opening``, skipping strings."""
    depth = 0
    quote: str | None = None
    index = opening
    while index < len(text):
        char = text[index]
        if quote:
            if char == "\\":
                index += 2
                continue
            if char == quote:
                quote = None
        elif text.startswith("//", index):
            newline = text.find("\n", index)
            index = len(text) if newline == -1 else newline
            continue
        elif text.startswith("/*", index):
            close = text.find("*/", index + 2)
            index = len(text) if close == -1 else close + 2
            continue
        elif char in "\"'`":
            quote = char
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return index + 1
        index += 1
    return index


def _tags(source: str) -> list[tuple[int, str, str, int]]:
    """``(start, element, tag text, end)`` for every JSX opening tag."""
    found = []
    for match in _OPENING.finditer(source):
        end = _tag_end(source, match.end())
        found.append((match.start(), match.group(1), source[match.start() : end + 1], end))
    return found


def _line(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def label_name_mismatches(source: str) -> list[str]:
    """``line N: label 'X' but aria-label 'Y'`` for each control that drops its label."""
    tags = _tags(source)
    labels: dict[str, str] = {}
    for _, element, tag, end in tags:
        if element not in ("Label", "label") or tag.endswith("/>"):
            continue
        target = _attributes(tag).get("htmlFor")
        close = source.find(f"</{element}>", end)
        body = source[end + 1 : close] if close != -1 else ""
        if target is None or "{" in body or "<" in body:
            continue
        text = _HINT.sub("", " ".join(body.split())).rstrip("*:").strip()
        if text:
            labels[target] = text
    found: list[str] = []
    for start, element, tag, _ in tags:
        if element in ("Label", "label"):
            continue
        attributes = _attributes(tag)
        control, name = attributes.get("id"), attributes.get("aria-label")
        if control is None or name is None or control not in labels:
            continue
        words = labels[control]
        if words.lower() not in _PLACEHOLDER.sub("\0", name).lower():
            found.append(f"line {_line(source, start)}: label {words!r} but aria-label {name!r}")
    return found


def _sources() -> list[Path]:
    return sorted(
        path
        for path in WEB_SRC.rglob("*.tsx")
        if "node_modules" not in path.parts and ".test." not in path.name
    )


def test_the_scan_reaches_the_labels_it_polices() -> None:
    """A rename of the label component would otherwise leave this passing on nothing."""
    labelled = sum(
        len(re.findall(r"<Label[^>]*htmlFor=", p.read_text("utf-8"))) for p in _sources()
    )
    assert labelled > 100, f"found only {labelled} labelled controls; is the scan elsewhere?"


def test_every_aria_label_keeps_the_words_its_label_shows() -> None:
    offenders = {
        path.relative_to(WEB_SRC).as_posix(): found
        for path in _sources()
        if (found := label_name_mismatches(path.read_text(encoding="utf-8")))
    }
    assert not offenders, (
        "These controls are named differently from the label on screen (WCAG 2.5.3):\n  "
        + "\n  ".join(f"{name}: {found}" for name, found in offenders.items())
        + "\n\nDrop the aria-label where the <Label htmlFor> already names the control, or "
        "lead it with the visible words and add what tells repeated controls apart: "
        "aria-label={`Alert after, ${rule.name}`}."
    )


# -- the rule rejects what it exists to catch ---------------------------------


def test_the_rule_rejects_an_aria_label_that_replaces_the_label() -> None:
    source = (
        '<Label htmlFor="reservation-demand-type" required>\n'
        "  Held for\n"
        "</Label>\n"
        '<SelectTrigger id="reservation-demand-type" aria-label="Demand type">\n'
    )
    assert label_name_mismatches(source) == [
        "line 4: label 'Held for' but aria-label 'Demand type'"
    ]


def test_the_rule_reads_template_ids_and_names() -> None:
    label = "<Label htmlFor={`alert-threshold-${rule.key}`}>Alert after</Label>\n"
    renamed = label + (
        "<Input id={`alert-threshold-${rule.key}`} aria-label={`${rule.name} alert threshold`} />\n"
    )
    assert label_name_mismatches(renamed) == [
        "line 2: label 'Alert after' but aria-label '${rule.name} alert threshold'"
    ]
    led = label + (
        "<Input id={`alert-threshold-${rule.key}`} aria-label={`Alert after, ${rule.name}`} />\n"
    )
    assert label_name_mismatches(led) == []


def test_the_rule_wants_containment_and_ignores_a_trailing_hint() -> None:
    source = (
        '<Label htmlFor="bom-type">Type</Label>\n'
        '<SelectTrigger id="bom-type" aria-label="BOM type">\n'
        '<Label htmlFor="rework-wc">Work centre (optional)</Label>\n'
        '<SelectTrigger id="rework-wc" aria-label="Work centre">\n'
    )
    assert label_name_mismatches(source) == []


def test_a_comment_inside_a_tag_does_not_carry_the_scan_into_the_next_one() -> None:
    source = (
        "<Checkbox\n"
        "  id={`op-outsourced-${index}`}\n"
        "  onCheckedChange={(checked) => {\n"
        "    // An outsourced step runs on the vendor's equipment.\n"
        "    flag.onChange(checked)\n"
        "  }}\n"
        "/>\n"
        "<Label htmlFor={`op-outsourced-${index}`}>Job work</Label>\n"
        '<SelectTrigger id="op-wc" aria-label="Work centre">\n'
    )
    assert label_name_mismatches(source) == []


def test_a_prop_holding_elements_does_not_pair_their_attributes() -> None:
    # DataTable's toolbar prop holds a filter's trigger and another control; the
    # id of one and the aria-label of the other belong to neither the table nor
    # each other.
    source = (
        '<Label htmlFor="gst-state">GST state code</Label>\n'
        "<DataTable\n"
        "  toolbar={\n"
        "    <>\n"
        '      <SelectTrigger aria-label="Filter by channel" />\n'
        '      <Input id="gst-state" {...register("gst")} />\n'
        "    </>\n"
        "  }\n"
        "/>\n"
    )
    assert label_name_mismatches(source) == []


def test_the_rule_leaves_what_it_cannot_read_alone() -> None:
    source = (
        '<Label htmlFor="x">{label}</Label>\n'
        '<Input id="x" aria-label="Something else" />\n'
        '<Label htmlFor="y">Quantity</Label>\n'
        '<Input id="y" aria-label={name} />\n'
    )
    assert label_name_mismatches(source) == []
