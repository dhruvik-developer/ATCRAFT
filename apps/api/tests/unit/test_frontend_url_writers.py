"""A screen writes its URL filters through one writer, never a hand-built updater.

``setSearchParams`` is not a React state setter: every call in one handler
starts from the URL captured at render, so two calls keep only the second. The
fix - compute every key in one updater, and drop the page the old narrowing
was on - had been written out by hand 40 times in 28 files: two byte-identical
exported ``clearSearchKeys``, a masters-only ``useFilterWriter``, a dispatch
``clearKeys``, and Clear buttons, tab switches and multi-key writers that
differed only in whether, and which, page they reset. Each copy had to know
which key its list keeps its page under, and one that reset ``page`` for a
list that keeps it under ``followups_page`` left that list on a page the
narrower result did not have, drawn as "No records".

``hooks/use-list-query.ts`` holds that updater once, and writes every URL
change through it: a list's own ``setParam``/``setParams``, and
``useFilterWriter()``, which writes several keys in one navigation and resets
the page key it is given (a namespaced list's ``pageParam``, or ``null`` for a
write that narrows nothing); ``cleared(keys)`` is what a Clear button writes.

So outside that module this guard refuses, on comment-blanked source:

* a ``.delete(...)`` or ``.set(...)`` of a page key - a quoted key ending in
  ``page`` (``'page'``, a namespaced list's ``'statement_page'``), a template
  ending in ``page``, or a ``pageParam``/``pageKey`` variable;
* a call of the URL setter whose updater loops ``Object.entries`` - a
  hand-written multi-key writer;
* a call of the URL setter whose updater names a page key some other way - the
  tab switches deleted ``for (const key of ['page', 'sort', 'dir'])``.

The URL setter is ``setSearchParams``, or whatever name the file gave it in
``const [params, setParams] = useSearchParams()``. The dealer account page kept
a hand-built statement-page updater past an earlier version of this guard
through both gaps: it renamed the setter, and quoted its namespaced page key.

A single-key updater that is not about the page - dropping a one-shot
``create_from_sales_order_id``, or starting a tab with fresh parameters - is
left alone.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from tests.frontend_scan_support import GENERATED, WEB_SRC, _line, blank_comments

pytestmark = pytest.mark.unit

WRITER = WEB_SRC / "hooks" / "use-list-query.ts"

#: Where the one-navigation updater lives.
OWNERS = frozenset({"hooks/use-list-query.ts"})

#: A URL page key: a quoted key or a template ending in ``page`` - ``'page'`` or a
#: namespaced list's ``'statement_page'`` - or a variable holding one.
_PAGE_KEY = (
    r"(?:'[^'\n]*page'|\"[^\"\n]*page\"|`[^`\n]*page`"
    r"|(?<![\w$])(?:[A-Za-z_$][\w$]*\.)*[pP]age(?:Param|Key)(?![\w$]))"
)
_PAGE_KEY_WRITE = re.compile(r"\.(?:delete|set)\(\s*" + _PAGE_KEY + r"\s*[,)]")
_PAGE_KEY_NAMED = re.compile(_PAGE_KEY)
#: ``const [params, setParams] = useSearchParams()`` - the setter, by its local name.
_URL_SETTER_NAMED = re.compile(
    r"\b(?:const|let)\s*\[\s*(?:[A-Za-z_$][\w$]*)?\s*,\s*([A-Za-z_$][\w$]*)\s*\]"
    r"\s*=\s*useSearchParams\b"
)


def _url_setter_calls(code: str) -> re.Pattern[str]:
    """A bare call of ``setSearchParams``, or of the name this file gave that setter.

    Bare, so a list's own ``list.setParams(...)`` is not read as the URL setter in
    a file that also calls its URL setter ``setParams``.
    """
    names = sorted({"setSearchParams", *_URL_SETTER_NAMED.findall(code)})
    return re.compile(r"(?<![\w$.])(?:" + "|".join(map(re.escape, names)) + r")\(")


def _call_end(code: str, open_paren: int) -> int:
    """The offset of the ``)`` matching the ``(`` at ``open_paren``."""
    depth = 0
    for index in range(open_paren, len(code)):
        if code[index] == "(":
            depth += 1
        elif code[index] == ")":
            depth -= 1
            if depth == 0:
                return index
    return len(code)


def hand_written_writers(source: str) -> list[str]:
    """``line N: what`` for each page-key write or multi-key loop in a URL updater."""
    code = blank_comments(source)
    writes = [
        (match.start(), f"line {_line(code, match.start())}: {match.group(0).strip()}")
        for match in _PAGE_KEY_WRITE.finditer(code)
    ]
    found = list(writes)
    for match in _url_setter_calls(code).finditer(code):
        start = match.end() - 1
        end = _call_end(code, start)
        body = code[start + 1 : end]
        line = _line(code, match.start())
        if "Object.entries(" in body:
            found.append((match.start(), f"line {line}: a multi-key updater"))
        elif _PAGE_KEY_NAMED.search(body) and not any(start < at < end for at, _ in writes):
            found.append((match.start(), f"line {line}: a page key in an updater"))
    return [entry for _, entry in sorted(found)]


def _sources() -> list[Path]:
    return sorted(
        path
        for path in WEB_SRC.rglob("*.ts*")
        if path.suffix in {".ts", ".tsx"}
        and "node_modules" not in path.parts
        and ".test." not in path.name
        and path.relative_to(WEB_SRC).parts[0] != "test"
        and path != GENERATED
    )


def test_the_shared_writer_exists() -> None:
    source = WRITER.read_text(encoding="utf-8")
    assert "export function useFilterWriter(" in source
    assert "export function cleared(" in source
    assert len(_sources()) > 300, "source discovery found too few files"


def test_no_screen_writes_its_url_filters_by_hand() -> None:
    found = {
        name: hits
        for path in _sources()
        if (name := path.relative_to(WEB_SRC).as_posix()) not in OWNERS
        and (hits := hand_written_writers(path.read_text(encoding="utf-8")))
    }
    assert not found, (
        "These write URL filters through a hand-built updater:\n"
        f"{found}\n\nUse useFilterWriter() from hooks/use-list-query.ts - pass a "
        "namespaced list's pageParam, or null for a write that narrows nothing - and "
        "cleared(keys) for a Clear button; or a list's own setParam/setParams."
    )


# -- the rule rejects what it exists to catch -----------------------------------


def test_the_rule_finds_the_shapes_that_shipped() -> None:
    source = "\n".join(
        [
            "  const clearFilters = useCallback(() => {",
            "    setSearchParams(",
            "      (current) => {",
            "        const next = new URLSearchParams(current)",
            "        for (const key of FILTER_KEYS) next.delete(key)",
            "        next.delete('page')",
            "        return next",
            "      },",
            "      { replace: true },",
            "    )",
            "  }, [setSearchParams])",
            "        next.delete(pageParam)",
            "        updated.set('page', '1')",
            "        next.delete(`${prefix}page`)",
            "  const setParams = (changes: Record<string, string | undefined>) =>",
            "    setSearchParams((current) => {",
            "      const next = new URLSearchParams(current)",
            "      for (const [key, value] of Object.entries(changes)) next.set(key, value)",
            "      return next",
            "    })",
            "  const changeTab = (next: string) =>",
            "    setSearchParams(",
            "      (current) => {",
            "        const params = new URLSearchParams(current)",
            "        params.set('tab', next)",
            "        for (const key of ['page', 'sort', 'dir']) params.delete(key)",
            "        return params",
            "      },",
            "      { replace: true },",
            "    )",
            "        next.delete(followUps.pageParam)",
            # The dealer account page: a renamed setter, and a namespaced page key.
            "  const [params, setParams] = useSearchParams()",
            "  const setField = (key: string, value: string) =>",
            "    setParams((current) => {",
            "      const next = new URLSearchParams(current)",
            "      if (value) next.set(key, value)",
            "      else next.delete(key)",
            "      if (key !== 'statement_page') next.delete('statement_page')",
            "      return next",
            "    })",
            "    setSearchParams((current) => {",
            "      const next = new URLSearchParams(current)",
            "      next.set('overdue', 'true')",
            "      next.delete('followups_page')",
            "      return next",
            "    })",
            "  const writeAll = (changes: Record<string, string>) =>",
            "    setParams((current) => {",
            "      const next = new URLSearchParams(current)",
            "      for (const [key, value] of Object.entries(changes)) next.set(key, value)",
            "      return next",
            "    })",
            "  const changeTab = (tab: string) =>",
            "    setParams((current) => {",
            "      const next = new URLSearchParams(current)",
            "      next.set('tab', tab)",
            "      for (const key of ['statement_page', 'sort']) next.delete(key)",
            "      return next",
            "    })",
        ]
    )
    assert hand_written_writers(source) == [
        "line 6: .delete('page')",
        "line 12: .delete(pageParam)",
        "line 13: .set('page',",
        "line 14: .delete(`${prefix}page`)",
        "line 16: a multi-key updater",
        "line 22: a page key in an updater",
        "line 31: .delete(followUps.pageParam)",
        "line 38: .delete('statement_page')",
        "line 44: .delete('followups_page')",
        "line 48: a multi-key updater",
        "line 54: a page key in an updater",
    ]


def test_the_rule_leaves_other_updaters_alone() -> None:
    source = "\n".join(
        [
            "            setSearchParams(",
            "              (current) => {",
            "                const next = new URLSearchParams(current)",
            "                next.delete('create_from_sales_order_id')",
            "                return next",
            "              },",
            "              { replace: true },",
            "            )",
            "    setSearchParams(() => new URLSearchParams({ tab: value }), { replace: true })",
            "  // next.delete('page') once lived here",
            "  const pages = Object.entries(byPage)",
            "  writeFilters(cleared(FILTER_KEYS))",
            "  list.setParams({ status: undefined })",
            "  turnPage({ page: next })",
            "  const page = Number(searchParams.get('page') ?? 1)",
            "    queryFn: ({ pageParam, signal }) => fetchPage({ cursor: pageParam, signal }),",
            "  const homepageKey = `${slug}-homepage`",
            "  url.searchParams.set('pages', '2')",
            # A renamed setter starting a tab fresh; a list's own writer is not it.
            "  const [params, setParams] = useSearchParams()",
            "          setParams({ tab })",
            "  orders.setParams(Object.fromEntries(Object.entries(draft).filter(([, v]) => v)))",
        ]
    )
    assert hand_written_writers(source) == []
