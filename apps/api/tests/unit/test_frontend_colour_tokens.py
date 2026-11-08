"""Colour in the web app is a token that both themes define - never a literal.

CLAUDE.md: "Colour - never a literal - a token from ``styles/globals.css``,
which ``:root`` and ``.dark`` both define." Hex, rgb and palette classes were
already gone; the white and black utilities were the last literals, kept on the
grounds that the sidebar is dark and a scrim is black in both themes. A token
defined identically in both sets says exactly that, and puts the decision in the
one file a theme or brand change edits - the two scrims had already drifted into
two spellings.

Two rules:

* no colour utility names ``white``, ``black``, a Tailwind palette colour
  (``red-500``) or an arbitrary colour (``bg-[#fff]``) - in a class string or an
  ``@apply``, under any variant. ``transparent`` and ``current`` are not colours
  and stay allowed;
* every ``hsl(var(--x))`` colour in ``tailwind.config.ts`` is defined in both
  the ``:root`` and the ``.dark`` set of ``globals.css``, so no token silently
  keeps its light value in dark mode.

Comment lines are skipped, so the history can still be written beside the code.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

WEB = Path(__file__).resolve().parents[3] / "web"
WEB_SRC = WEB / "src"
GLOBALS_CSS = WEB_SRC / "styles" / "globals.css"
TAILWIND_CONFIG = WEB / "tailwind.config.ts"

_UTILITY = (
    r"(?:bg|text|border|ring|from|via|to|fill|stroke|shadow|divide|outline|placeholder"
    r"|decoration|caret|accent)"
)
_PALETTE = (
    r"(?:slate|gray|zinc|neutral|stone|red|orange|amber|yellow|lime|green|emerald|teal|cyan"
    r"|sky|blue|indigo|violet|purple|fuchsia|pink|rose)-\d{2,3}"
)
_LITERAL = re.compile(
    rf"(?:^|(?<=[\s'\"`:]))(?:{_UTILITY})-(?:white|black|{_PALETTE}|\[(?:#|rgb|hsl)[^\]]*\])"
    r"(?:/\d+)?(?![\w-])"
)
_COMMENT_LINE = re.compile(r"^\s*(?://|/\*|\*|\{/\*)")
_COLOUR_VAR = re.compile(r"hsl\(var\(--([\w-]+)\)\)")
_DEFINITION = re.compile(r"--([\w-]+)\s*:")


def colour_literals(source: str) -> list[str]:
    """``line N: utility`` for each colour utility that names a literal colour."""
    return [
        f"line {number}: {match.group(0)}"
        for number, line in enumerate(source.splitlines(), start=1)
        if not _COMMENT_LINE.match(line)
        for match in _LITERAL.finditer(line)
    ]


def _block(css: str, selector: str) -> str:
    """The declarations of the first ``selector { ... }`` block."""
    start = css.find(f"{selector} {{")
    assert start != -1, f"{selector} block not found in globals.css"
    depth = 0
    for index in range(css.index("{", start), len(css)):
        depth += {"{": 1, "}": -1}.get(css[index], 0)
        if depth == 0:
            return css[start : index + 1]
    return css[start:]


def undefined_theme_colours(config: str, css: str) -> list[str]:
    """``--x missing from :root/.dark`` for each config colour a theme set lacks."""
    themes = {
        selector: set(_DEFINITION.findall(_block(css, selector))) for selector in (":root", ".dark")
    }
    return [
        f"--{name} missing from {selector}"
        for name in sorted(set(_COLOUR_VAR.findall(config)))
        for selector, defined in themes.items()
        if name not in defined
    ]


def _sources() -> list[Path]:
    return sorted(
        path
        for pattern in ("*.tsx", "*.ts", "*.css")
        for path in WEB_SRC.rglob(pattern)
        if "node_modules" not in path.parts and ".test." not in path.name
    )


def test_the_scan_reaches_the_colour_utilities_it_polices() -> None:
    """A move of the sources would otherwise leave this passing on nothing."""
    tokens = sum(
        len(re.findall(r"\btext-muted-foreground\b", p.read_text("utf-8"))) for p in _sources()
    )
    assert tokens > 200, f"found only {tokens} token colour classes; is the scan elsewhere?"


def test_no_colour_is_written_as_a_literal() -> None:
    offenders = {
        path.relative_to(WEB_SRC).as_posix(): found
        for path in _sources()
        if (found := colour_literals(path.read_text(encoding="utf-8")))
    }
    assert not offenders, (
        "These colours are literals rather than theme tokens:\n  "
        + "\n  ".join(f"{name}: {found}" for name, found in offenders.items())
        + "\n\nDefine a token in both :root and .dark of styles/globals.css, map it in "
        "tailwind.config.ts and use that (CLAUDE.md, 'Colour')."
    )


def test_every_configured_colour_is_defined_in_both_themes() -> None:
    missing = undefined_theme_colours(
        TAILWIND_CONFIG.read_text(encoding="utf-8"), GLOBALS_CSS.read_text(encoding="utf-8")
    )
    assert not missing, (
        "tailwind.config.ts maps colours that a theme set does not define:\n  "
        + "\n  ".join(missing)
        + "\n\nDefine each in both :root and .dark - the same value in both when the colour "
        "is meant not to change with the theme."
    )


# -- the rules reject what they exist to catch --------------------------------


def test_the_rule_rejects_white_black_palette_and_arbitrary_colours() -> None:
    source = (
        '<p className="truncate text-sm font-semibold text-white">\n'
        "'text-sidebar-foreground/80 hover:bg-white/5 hover:text-white'\n"
        "const overlayClass = 'fixed inset-0 z-50 bg-black/50 backdrop-blur-[1px]'\n"
        "  @apply bg-gradient-to-r from-transparent via-white/25 to-transparent;\n"
        '<span className="bg-red-500 text-[#fff]">\n'
    )
    assert colour_literals(source) == [
        "line 1: text-white",
        "line 2: bg-white/5",
        "line 2: text-white",
        "line 3: bg-black/50",
        "line 4: via-white/25",
        "line 5: bg-red-500",
        "line 5: text-[#fff]",
    ]


def test_the_rule_allows_tokens_and_non_colours() -> None:
    source = (
        '<div className="bg-overlay text-sidebar-emphasis hover:bg-sidebar-hover">\n'
        '<span className="bg-transparent text-current whitespace-nowrap border-white-ish">\n'
        "// The sidebar used text-white before it had a token.\n"
        "{/* bg-black/50 was the scrim's old spelling. */}\n"
    )
    assert colour_literals(source) == []


def test_the_theme_rule_rejects_a_colour_one_set_lacks() -> None:
    # --radius is not a colour, so its absence from .dark is not a theme gap.
    config = "accent: 'hsl(var(--accent))', overlay: 'hsl(var(--overlay))', lg: 'var(--radius)'"
    css = (
        ":root {\n  --accent: 214 30% 94%;\n  --overlay: 0 0% 0% / 0.5;\n  --radius: 0.5rem;\n}\n"
        ".dark {\n  --accent: 218 20% 22%;\n}\n"
    )
    assert undefined_theme_colours(config, css) == ["--overlay missing from .dark"]
