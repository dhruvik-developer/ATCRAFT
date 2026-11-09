"""An icon-only button is at least 24px square to press, whatever the icon's size.

WCAG 2.2 SC 2.5.8 sets 24 x 24 CSS px as the minimum pointer target. The shared
``Button`` sizes meet it (``icon-sm`` is 32px); the raw ``<button>``s written
around a bare lucide icon did not. The Close control of every dialog was its
16px icon, every list's clear-search X was a 16px target inside the input it
clears - so a tap that missed it landed in the text - the toast dismiss was
20px and six filter-chip removals were 12px. None of that is visible to the
axe sweep, which runs the WCAG 2.0/2.1 tags at desktop width with dialogs
closed.

The rule finds each raw ``<button>`` and ``<DialogPrimitive.Close>`` whose only
content is a lucide icon imported by that file (an ``sr-only`` label beside it
is fine) and measures the box its unprefixed classes give: an explicit
``h-``/``size-``/``min-h-`` (and the matching width), or else the icon's own
size plus its padding. A responsive class does not count, because the phone is
the screen that gets the unprefixed one. A box the rule cannot measure is
reported too, so it is stated rather than assumed.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

WEB_SRC = Path(__file__).resolve().parents[3] / "web" / "src"

#: WCAG 2.2 SC 2.5.8 (Target Size, Minimum), in CSS pixels.
MIN_TARGET_PX = 24

#: Tailwind's default spacing step; ``tailwind.config.ts`` does not override it.
SPACING_STEP_PX = 4

_OPENING = re.compile(r"<(button|DialogPrimitive\.Close)(?=[\s>])")
_LUCIDE_IMPORT = re.compile(r"import\s*\{([^}]*)\}\s*from\s*'lucide-react'")
_JSX_COMMENT = re.compile(r"\{/\*.*?\*/\}", re.S)
_SR_ONLY = re.compile(r"<span\s+className=\"sr-only\">.*?</span>", re.S)
_SINGLE_ICON = re.compile(r"<([A-Z][\w]*)\b([^<>]*)/>", re.S)
_TWO_ICONS = re.compile(
    r"\{[^{}<]*\?\s*<([A-Z]\w*)\b([^<>]*)/>\s*:\s*<([A-Z]\w*)\b([^<>]*)/>\s*\}", re.S
)
_STRING = re.compile(r"'([^']*)'|\"([^\"]*)\"|`([^`]*)`")


def _tag_end(text: str, start: int) -> int:
    """Index of the ``>`` closing the JSX opening tag that begins at ``start``."""
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


def _class_tokens(tag: str) -> list[str]:
    """The unprefixed utility classes a tag's ``className`` names."""
    match = re.search(r"(?<![\w-])className=(?:\"([^\"]*)\"|\{)", tag)
    if not match:
        return []
    if match.group(1) is not None:
        text = match.group(1)
    else:
        depth = 1
        index = match.end()
        while index < len(tag) and depth:
            depth += {"{": 1, "}": -1}.get(tag[index], 0)
            index += 1
        expression = tag[match.end() : index - 1]
        text = " ".join(
            next(p for p in found if p) for found in _STRING.findall(expression) if any(found)
        )
    return [token for token in text.split() if ":" not in token]


def _spacing(tokens: list[str], prefixes: tuple[str, ...]) -> float | None:
    """The largest ``prefix-N`` among ``tokens``, in px; ``None`` when there is none."""
    values = [
        float(match.group(1)) * SPACING_STEP_PX
        for token in tokens
        for prefix in prefixes
        if (match := re.fullmatch(rf"{re.escape(prefix)}-(\d+(?:\.\d+)?)", token))
    ]
    return max(values) if values else None


def _extent(button: list[str], icons: list[list[str]], axis: str) -> float | None:
    """The target's height (``axis="y"``) or width (``"x"``) in px."""
    side = "h" if axis == "y" else "w"
    explicit = _spacing(button, (side, "size", f"min-{side}"))
    if explicit is not None:
        return explicit
    sizes = [_spacing(icon, (side, "size")) for icon in icons]
    if any(size is None for size in sizes):
        return None
    padding = _spacing(button, ("p", f"p{axis}")) or 0
    return min(size for size in sizes if size is not None) + 2 * padding


def undersized_icon_buttons(source: str) -> list[str]:
    """``line N: <tag> HxW`` for each icon-only button smaller than the minimum."""
    icons = {
        name.strip().split(" as ")[-1].strip()
        for block in _LUCIDE_IMPORT.findall(source)
        for name in block.split(",")
        if name.strip() and not name.strip().startswith("type ")
    }
    found: list[str] = []
    for match in _OPENING.finditer(source):
        name = match.group(1)
        end = _tag_end(source, match.end())
        tag = source[match.start() : end + 1]
        if tag.endswith("/>") or re.search(r"(?<![\w-])asChild(?![\w-])", tag):
            continue
        close = source.find(f"</{name}>", end)
        body = _SR_ONLY.sub(" ", _JSX_COMMENT.sub(" ", source[end + 1 : close])).strip()
        if single := _SINGLE_ICON.fullmatch(body):
            children = [(single.group(1), single.group(2))]
        elif pair := _TWO_ICONS.fullmatch(body):
            children = [(pair.group(1), pair.group(2)), (pair.group(3), pair.group(4))]
        else:
            continue
        if not all(child in icons for child, _ in children):
            continue
        button = _class_tokens(tag)
        icon_tokens = [_class_tokens(f"<{child} {attributes}>") for child, attributes in children]
        height, width = _extent(button, icon_tokens, "y"), _extent(button, icon_tokens, "x")
        line = source.count("\n", 0, match.start()) + 1
        if height is None or width is None:
            found.append(f"line {line}: <{name}> (size not measurable)")
        elif height < MIN_TARGET_PX or width < MIN_TARGET_PX:
            found.append(f"line {line}: <{name}> {height:g}x{width:g}px")
    return found


def _sources() -> list[Path]:
    return sorted(
        path
        for path in WEB_SRC.rglob("*.tsx")
        if "node_modules" not in path.parts and ".test." not in path.name
    )


def test_the_scan_reaches_the_raw_buttons_it_polices() -> None:
    """A change in how buttons are written would otherwise leave this passing on nothing.

    The floor only has to tell some from none. It sat at 50 while there were 61;
    folding thirteen pickers' option buttons into one in
    ``components/ui/search-picker.tsx`` left 49, which is the point of sharing.
    """
    raw = sum(len(_OPENING.findall(path.read_text("utf-8"))) for path in _sources())
    assert raw > 30, f"found only {raw} raw buttons; is the scan looking elsewhere?"


def test_every_icon_only_button_is_big_enough_to_press() -> None:
    offenders = {
        path.relative_to(WEB_SRC).as_posix(): found
        for path in _sources()
        if (found := undersized_icon_buttons(path.read_text(encoding="utf-8")))
    }
    assert not offenders, (
        f"These icon-only buttons are smaller than {MIN_TARGET_PX}px to press:\n  "
        + "\n  ".join(f"{name}: {found}" for name, found in offenders.items())
        + "\n\nGrow the hit area, not the icon: <Button size='icon-sm'>, BadgeDismiss for "
        "a chip, or 'grid h-8 w-8 place-items-center' with a negative margin that keeps "
        "the icon where it was."
    )


# -- the rule rejects what it exists to catch ---------------------------------

_IMPORTS = "import { Eye, EyeOff, X } from 'lucide-react'\n"


def test_the_rule_rejects_a_bare_icon_and_a_bare_dialog_close() -> None:
    source = (
        _IMPORTS
        + '<button type="button" aria-label="Remove" className="rounded-sm">\n'
        + '  <X className="h-3 w-3" aria-hidden />\n'
        + "</button>\n"
        + '<DialogPrimitive.Close className="absolute right-4 top-4 rounded-sm">\n'
        + '  <X className="h-4 w-4" />\n'
        + '  <span className="sr-only">Close</span>\n'
        + "</DialogPrimitive.Close>\n"
    )
    assert undersized_icon_buttons(source) == [
        "line 2: <button> 12x12px",
        "line 5: <DialogPrimitive.Close> 16x16px",
    ]


def test_the_rule_measures_padding_around_the_icon() -> None:
    short = _IMPORTS + '<button className="p-0.5"><X className="h-4 w-4" /></button>\n'
    assert undersized_icon_buttons(short) == ["line 2: <button> 20x20px"]

    enough = _IMPORTS + '<button className="-m-1 p-1.5"><X className="h-4 w-4" /></button>\n'
    assert undersized_icon_buttons(enough) == []

    toggle = (
        _IMPORTS + '<button className="absolute right-2 p-1">\n'
        '  {shown ? <EyeOff className="h-4 w-4" /> : <Eye className="h-4 w-4" />}\n'
        "</button>\n"
    )
    assert undersized_icon_buttons(toggle) == []


def test_the_rule_accepts_an_explicit_box_and_ignores_a_responsive_one() -> None:
    boxed = _IMPORTS + (
        '<button className="-my-1 grid h-6 w-6 place-items-center">'
        '<X className="h-3 w-3" /></button>\n'
    )
    assert undersized_icon_buttons(boxed) == []

    desktop_only = _IMPORTS + (
        '<button className="sm:h-8 sm:w-8"><X className="h-3 w-3" /></button>\n'
    )
    assert undersized_icon_buttons(desktop_only) == ["line 2: <button> 12x12px"]


def test_the_rule_reports_a_box_it_cannot_measure() -> None:
    source = _IMPORTS + '<button className="rounded-sm"><X /></button>\n'
    assert undersized_icon_buttons(source) == ["line 2: <button> (size not measurable)"]


def test_the_rule_leaves_text_buttons_and_other_components_alone() -> None:
    source = _IMPORTS + (
        '<button className="text-xs"><X className="h-3 w-3" /> Remove</button>\n'
        '<button className="text-left"><RatingStars rating={4} /></button>\n'
        '<DialogPrimitive.Close asChild><Button size="icon"><X /></Button>'
        "</DialogPrimitive.Close>\n"
    )
    assert undersized_icon_buttons(source) == []
