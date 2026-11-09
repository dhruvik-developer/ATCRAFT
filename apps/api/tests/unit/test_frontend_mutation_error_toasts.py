"""A failed mutation is announced once, by the global net.

``apps/web/src/lib/query-client.ts`` gives every mutation one ``MutationCache``
``onError``, and TanStack runs it *before* the mutation's own ``onError``. So a
mutation that also toasts in its own handler says the same refusal twice - and
error toasts never auto-dismiss, so a refused courier refresh left two red
boxes with one sentence in them. Fourteen mutations did exactly that (the
inbound panel through a local ``errorToast``, the courier screen through
``reportFailure``, three collections actions titled with the API's own
sentence, a dispatch refresh and a label download).

A mutation names its failure with ``meta: { errorTitle }`` instead, which the
net uses in place of the code's headline, or opts out with ``meta: {
suppressErrorToast: true }`` when it renders the failure itself. Its own
``onError`` may still put a 422's field detail on screen - ``applyFieldErrors``
or ``reportFieldErrors``, the one case the net leaves alone - but it never
raises an error toast of its own.

The same double has a quieter shape: a component that shows its mutation's
whole failure - ``describeError`` or ``dealerError`` of it, a ``ReasonDialog``
given ``error=``, or an ``onError`` that writes one into state - while the
mutation still lets the net toast it. Fifty-seven did, from confirm dialogs to
form alerts. Showing only ``fieldErrorSummary`` is not that: it is the 422
detail the net leaves to the screen, and the net still says everything else.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

WEB_SRC = Path(__file__).resolve().parents[3] / "web" / "src"
QUERY_CLIENT = WEB_SRC / "lib" / "query-client.ts"

_ON_ERROR = re.compile(r"(?<![\w$.])onError\s*:\s*")
_ERROR_TOAST = re.compile(r"\btoast\.(?:error|failure)\s*\(")
_IDENTIFIER = re.compile(r"[A-Za-z_$][\w$]*")
_CLOSERS = {")": "(", "]": "[", "}": "{"}


def _line(source: str, offset: int) -> int:
    return source.count("\n", 0, offset) + 1


def _skip_string(source: str, start: int) -> int:
    """The offset just past the quoted literal that opens at ``start``."""
    quote = source[start]
    index = start + 1
    while index < len(source):
        char = source[index]
        if char == "\\":
            index += 2
            continue
        if char == quote:
            return index + 1
        if quote == "`" and source.startswith("${", index):
            index = _expression_end(source, index + 2, stop_at_comma=False) + 1
            continue
        index += 1
    return index


def _indent(source: str, offset: int) -> int:
    line_start = source.rfind("\n", 0, offset) + 1
    line = source[line_start:]
    return len(line) - len(line.lstrip(" "))


def _expression_end(
    source: str, start: int, *, stop_at_comma: bool, statement_indent: int | None = None
) -> int:
    """Where the expression starting at ``start`` ends.

    That is the first ``,`` at its own depth (for a property value) or the
    bracket that closes the one it sits in - so an object property's value is
    read whole however many lines its arrow body spans. A statement outside
    any bracket (``const reportFailure = (error) =>`` and its indented body)
    ends instead at the first line indented no deeper than it, which is how
    Prettier lays out a continuation.
    """
    stack: list[str] = []
    index = start
    while index < len(source):
        if (
            statement_indent is not None
            and not stack
            and source[index] == "\n"
            and index + 1 < len(source)
            and source[index + 1 :].strip()
            and _indent(source, index + 1) <= statement_indent
        ):
            return index
        char = source[index]
        if char in "'\"`":
            index = _skip_string(source, index)
            continue
        if source.startswith("//", index):
            newline = source.find("\n", index)
            index = len(source) if newline == -1 else newline
            continue
        if source.startswith("/*", index):
            close = source.find("*/", index + 2)
            index = len(source) if close == -1 else close + 2
            continue
        if char in "([{":
            stack.append(char)
        elif char in _CLOSERS:
            if not stack:
                return index
            stack.pop()
        elif char == "," and stop_at_comma and not stack:
            return index
        index += 1
    return index


def _definition_body(source: str, name: str) -> str | None:
    """The text of a same-file ``const name = ...`` or ``function name(...) {...}``."""
    bound = re.search(rf"\b(?:const|let|var)\s+{re.escape(name)}\s*(?::[^=]+)?=\s*", source)
    if bound:
        end = _expression_end(
            source,
            bound.end(),
            stop_at_comma=False,
            statement_indent=_indent(source, bound.start()),
        )
        return source[bound.end() : end]
    declared = re.search(rf"\bfunction\s+{re.escape(name)}\s*\(", source)
    if declared:
        brace = source.find("{", declared.end())
        if brace != -1:
            return source[brace : _expression_end(source, brace + 1, stop_at_comma=False) + 1]
    return None


def toasting_handlers(source: str) -> list[str]:
    """``line N`` for every ``onError`` that raises an error toast itself."""
    found = []
    for match in _ON_ERROR.finditer(source):
        handler = source[match.end() : _expression_end(source, match.end(), stop_at_comma=True)]
        body = handler
        if _IDENTIFIER.fullmatch(handler.strip()):
            body = _definition_body(source, handler.strip()) or ""
        if _ERROR_TOAST.search(body):
            found.append(f"line {_line(source, match.start())}")
    return found


_MUTATION = re.compile(
    r"\bconst\s+(?P<name>[A-Za-z_$][\w$]*)\s*=\s*(?:useMutation|useDealerAction)\s*(?:<[^>]*>)?\("
)
#: Where a component ends: the next top-level function, or component bound to a const.
_TOP_LEVEL = re.compile(
    r"^(?:export\s+)?(?:default\s+)?(?:async\s+)?function\b|^(?:export\s+)?const\s+[A-Z][\w$]*\s*=",
    re.M,
)
#: What says a failure whole. ``fieldErrorSummary`` alone is the 422 detail.
_SAYS = r"\b(?:describeError|dealerError)\s*\("


def _component_end(source: str, offset: int) -> int:
    following = _TOP_LEVEL.search(source, offset)
    return following.start() if following else len(source)


def shown_and_toasted(source: str) -> list[str]:
    """``line N: name`` for a mutation its component shows the failure of, toast left on.

    The reads are looked for in the declaring component only, so two
    components in one file can each name a mutation ``retire``.
    """
    found = []
    for match in _MUTATION.finditer(source):
        name = match.group("name")
        call = source[match.end() : _expression_end(source, match.end(), stop_at_comma=False)]
        if "suppressErrorToast" in call:
            continue
        scope = source[match.start() : _component_end(source, match.end())]
        read = re.escape(name)
        shows = re.search(rf"{_SAYS}\s*{read}\.error\b|\berror=\{{\s*{read}\.error\s*\}}", scope)
        writes = any(
            re.search(r"\bset\w+\s*\(", handler) and re.search(_SAYS, handler)
            for handler in (
                call[on_error.end() : _expression_end(call, on_error.end(), stop_at_comma=True)]
                for on_error in _ON_ERROR.finditer(call)
            )
        )
        if shows or writes:
            found.append(f"line {_line(source, match.start())}: {name}")
    return found


def _sources() -> list[Path]:
    return sorted(
        path
        for path in WEB_SRC.rglob("*.ts*")
        if path.suffix in {".ts", ".tsx"}
        and "node_modules" not in path.parts
        and ".test." not in path.name
    )


def test_the_net_still_reads_the_meta_a_mutation_names_its_failure_with() -> None:
    """If the net stops reading these, the guard below points authors at nothing."""
    source = QUERY_CLIENT.read_text(encoding="utf-8")
    assert "mutationCache: new MutationCache(" in source
    assert "meta?.errorTitle" in source
    assert "meta?.suppressErrorToast" in source
    assert len(_sources()) > 100, "source discovery found too few files"


def test_no_mutation_toasts_its_own_failure() -> None:
    toasting = {
        path.relative_to(WEB_SRC).as_posix(): found
        for path in _sources()
        if path != QUERY_CLIENT and (found := toasting_handlers(path.read_text(encoding="utf-8")))
    }
    assert not toasting, (
        "A mutation raises its own error toast after the global net has raised one:\n  "
        + "\n  ".join(f"{name}: {', '.join(found)}" for name, found in toasting.items())
        + "\n\nName the action with `meta: { errorTitle: 'Could not ...' }` and let "
        "lib/query-client say it, or render the failure in place and declare "
        "`meta: { suppressErrorToast: true }`. A 422's field detail is the exception: "
        "applyFieldErrors or reportFieldErrors from '@/lib/form-errors'."
    )


def test_no_mutation_is_shown_in_place_and_toasted_as_well() -> None:
    doubled = {
        path.relative_to(WEB_SRC).as_posix(): found
        for path in _sources()
        if (found := shown_and_toasted(path.read_text(encoding="utf-8")))
    }
    assert not doubled, (
        "A screen shows a mutation's failure and the global net toasts it as well:\n  "
        + "\n  ".join(f"{name}: {', '.join(found)}" for name, found in doubled.items())
        + "\n\nDeclare `meta: { suppressErrorToast: true }` on the mutation, and say "
        "every failure in place: `fieldErrorSummary(error) || describeError(error)` for "
        "a plain slot, `errors.root?.message || describeError(error)` for a "
        "react-hook-form alert (a model-level 422 lands on `root`). Check the dialog "
        "stays open when the request fails, or nothing will say it."
    )


# -- the rule rejects what it exists to catch ---------------------------------


def test_the_rule_finds_the_shapes_that_shipped() -> None:
    source = (
        "const errorToast = (error: unknown) => toast.error(describeError(error))\n"
        "const adopt = useMutation({\n"
        "  mutationFn: () => api.post(`${base}/accept-vendor-date`),\n"
        "  onError: errorToast,\n"
        "})\n"
        "const change = useMutation({\n"
        "  onError: (error) => toast.error(describeError(error)),\n"
        "})\n"
        "const refresh = useMutation({\n"
        "  onError: (error) => {\n"
        "    setBusy(false)\n"
        "    toast.failure(error, `Could not refresh ${code}`)\n"
        "  },\n"
        "  onSuccess: () => toast.success('Done'),\n"
        "})\n"
        "function reportFailure(error: unknown) {\n"
        "  toast.error('Courier operation failed', describeError(error))\n"
        "}\n"
        "const create = useMutation({ onError: reportFailure, mutationFn: save })\n"
    )
    assert toasting_handlers(source) == ["line 4", "line 7", "line 10", "line 19"]


def test_the_rule_ignores_field_detail_meta_and_other_toasts() -> None:
    source = (
        "const save = useMutation({\n"
        "  meta: { errorTitle: 'Could not save the courier account' },\n"
        "  onError: (error) => reportFieldErrors(error, 'Could not save the courier account'),\n"
        "  onSuccess: () => toast.success(success.saved('Courier account')),\n"
        "})\n"
        "const edit = useMutation({ onError: (error) => applyFieldErrors(error, setError) })\n"
        "const pick = useMutation({\n"
        "  onError: (error) => setComplaint(fieldErrorSummary(error) || describeError(error)),\n"
        "  onSettled: () => toast.error('not an onError handler'),\n"
        "})\n"
        "const keep = (error: unknown) => setFailure(describeError(error))\n"
        "const other = useMutation({ onError: keep })\n"
        "try { await api.download(path) } catch (error) { toast.failure(error) }\n"
        "<img src={url} onError={() => toast.error('A JSX attribute is not a mutation')} />\n"
    )
    assert toasting_handlers(source) == []


def test_the_shown_rule_finds_the_shapes_that_shipped() -> None:
    source = (
        "function RetireDialog() {\n"
        "  const retire = useMutation({ mutationFn: drop })\n"
        "  return <Alert>{describeError(retire.error)}</Alert>\n"
        "}\n"
        "function OrderPage() {\n"
        "  const cancel = useDealerAction(act, 'Order cancelled', { onSuccess: close })\n"
        "  return <FieldError message={cancel.error ? dealerError(cancel.error) : undefined} />\n"
        "}\n"
        "export function StatusDialog() {\n"
        "  const change = useMutation({ mutationFn: post })\n"
        "  return <ReasonDialog open error={change.error} />\n"
        "}\n"
        "const PushCard = () => {\n"
        "  const test = useMutation({\n"
        "    mutationFn: send,\n"
        "    onError: (error) => {\n"
        "      setFailure(\n"
        "        describeError(error),\n"
        "      )\n"
        "    },\n"
        "  })\n"
        "}\n"
    )
    assert shown_and_toasted(source) == [
        "line 2: retire",
        "line 6: cancel",
        "line 10: change",
        "line 14: test",
    ]


def test_the_shown_rule_ignores_opt_outs_field_detail_and_a_namesake() -> None:
    source = (
        "function Form() {\n"
        "  const save = useMutation({ mutationFn: put, meta: { suppressErrorToast: true } })\n"
        "  return <Alert>{errors.root?.message || describeError(save.error)}</Alert>\n"
        "}\n"
        "function Lines() {\n"
        "  const run = useMutation({ mutationFn: post })\n"
        "  return <FieldError message={fieldErrorSummary(run.error) || undefined} />\n"
        "}\n"
        "function Quiet() {\n"
        "  const mutation = useMutation({ mutationFn: drop })\n"
        "  return <Button onClick={() => mutation.mutate()} />\n"
        "}\n"
        "export function Loud() {\n"
        "  const mutation = useMutation({ mutationFn: put, meta: { suppressErrorToast: true } })\n"
        "  return <Alert>{describeError(mutation.error)}</Alert>\n"
        "}\n"
        "function Detail() {\n"
        "  const detail = useQuery({ queryKey, queryFn })\n"
        "  const pdf = useMutation({ mutationFn: download })\n"
        "  return <LoadError error={detail.error} />\n"
        "}\n"
    )
    assert shown_and_toasted(source) == []
