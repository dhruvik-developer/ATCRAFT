"""What a database error may say in a log line: never the row it was writing.

``hide_parameters`` (:data:`src.core.database.HIDE_STATEMENT_PARAMETERS`) keeps a
failed statement's bound values out of SQLAlchemy's own text. The driver's text
is another matter, and SQLAlchemy embeds it verbatim:

* an integrity violation carries the server's ``DETAIL`` - ``Key (company_id,
  email)=(..., priya@example.com) already exists``, and for a NOT NULL or CHECK
  violation ``Failing row contains (...)``, every column of the row;
* a data exception names the value it could not take - ``invalid input syntax
  for type uuid: "..."``;
* asyncpg refuses a value it cannot encode before the server sees it -
  ``invalid input for query argument $1: 'priya@example.com' (...)``, its
  ``DataError``, SQLSTATE 22000 - and the encoder's own error beneath it
  repeats the value.

That text reached every traceback, the worker's ``task.failed`` line, each
``error=str(exc)`` logged beside one, Celery's own failure line and the error
tracker: free text, which the key-based redaction (:mod:`src.core.logging`)
cannot see inside. Those sinks ask :func:`database_error_redactor` for a
rewrite instead. Each such error's own text gives way to what identifies the
failure without quoting it - the SQLSTATE, and the constraint, table and column
the server reported as separate fields. SQLAlchemy's ``[SQL: ...]`` stays: it
holds no values, and it says which statement failed.

Only those two SQLSTATE classes are rewritten. The text of every other database
error - a connection refused at an address, a column that does not exist, a
lock timeout - binds no value and is the diagnosis.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterator

from sqlalchemy.exc import DataError, IntegrityError

#: SQLSTATE classes whose text can quote a value: 22, data exception - asyncpg's
#: refusal to encode an argument among them - and 23, integrity constraint
#: violation.
VALUE_BEARING_SQLSTATE_CLASSES = frozenset({"22", "23"})

#: What a withheld driver text starts with, ahead of what identifies the failure.
DRIVER_TEXT_WITHHELD = "database error text withheld"

#: Schema identifiers PostgreSQL reports beside an error, as separate fields.
SCHEMA_FIELDS = ("constraint_name", "table_name", "column_name")

#: The part of asyncpg's refusal of an argument that names no value: which one.
_ARGUMENT_POSITION = re.compile(
    r"invalid input for query argument \$\d+(?: in element #\d+ of executemany\(\) sequence)?"
)


def own_sqlstate(error: BaseException) -> str | None:
    """The SQLSTATE ``error`` itself carries, if any.

    ``sqlstate`` for asyncpg and psycopg 3, ``pgcode`` for psycopg 2;
    SQLAlchemy's adapted asyncpg error carries both.
    """
    code = getattr(error, "sqlstate", None) or getattr(error, "pgcode", None)
    return str(code) if code else None


def own_schema_name(error: BaseException, field: str) -> str | None:
    """A schema identifier the server reported with ``error`` - never a value.

    asyncpg puts the constraint, table and column on its error; psycopg puts
    them on ``diag``.
    """
    value = getattr(error, field, None) or getattr(getattr(error, "diag", None), field, None)
    return str(value) if value else None


def _driver_errors(exc: BaseException) -> Iterator[BaseException]:
    """Where a SQLAlchemy error keeps the driver's: ``orig``, and the error
    SQLAlchemy's asyncpg adapter raised it from, its ``__cause__``."""
    original = getattr(exc, "orig", None)
    for candidate in (original, getattr(original, "__cause__", None)):
        if candidate is not None:
            yield candidate


def sqlstate_of(exc: BaseException) -> str | None:
    """The five-character SQLSTATE the driver attached to ``exc``, if any."""
    for candidate in _driver_errors(exc):
        if code := own_sqlstate(candidate):
            return code
    return None


def schema_name_of(exc: BaseException, field: str) -> str | None:
    """A schema identifier the server reported with ``exc`` - never a value."""
    for candidate in _driver_errors(exc):
        if value := own_schema_name(candidate, field):
            return value
    return None


def _quotes_values(error: BaseException) -> bool:
    """Whether ``error``'s own text can quote the values its statement carried."""
    state = own_sqlstate(error)
    return state is not None and state[:2] in VALUE_BEARING_SQLSTATE_CLASSES


def _linked(error: BaseException) -> Iterator[tuple[BaseException, bool]]:
    """Each error a traceback of ``error`` also prints, and whether ``error``
    names it as its own cause - ``orig`` or ``__cause__`` - rather than as one
    it was raised while handling."""
    original = getattr(error, "orig", None)
    if isinstance(original, BaseException):
        yield original, True
    if error.__cause__ is not None:
        yield error.__cause__, True
    if error.__context__ is not None and not error.__suppress_context__:
        yield error.__context__, False


def withheld_errors(exc: BaseException) -> list[BaseException]:
    """Every error in ``exc``'s chain whose own text may quote a value.

    One whose SQLSTATE is in a value-bearing class, and everything it names as
    its cause - the encoder's error beneath asyncpg's refusal repeats the
    value. A SQLAlchemy ``IntegrityError`` or
    ``DataError`` says the same of the driver error it wraps, which covers a
    driver that attaches no SQLSTATE. SQLAlchemy's own wrapper is not among
    them: its text embeds the driver's, which a rewrite of the driver's text
    reaches, beside the SQL, which stays.
    """
    found: list[BaseException] = []
    seen: set[int] = set()
    pending: list[tuple[BaseException, bool]] = [(exc, False)]
    while pending:
        error, inherited = pending.pop()
        if id(error) in seen:
            continue
        seen.add(id(error))
        quotes = inherited or _quotes_values(error)
        if quotes:
            found.append(error)
        passes_on = quotes or isinstance(error, IntegrityError | DataError)
        pending.extend((linked, passes_on and caused) for linked, caused in _linked(error))
    return found


def _text(error: BaseException) -> str:
    """``str(error)``, never raising - as a traceback prints it."""
    try:
        return str(error)
    except Exception:
        return ""


def database_error_summary(errors: list[BaseException]) -> str:
    """What identifies the failure: the SQLSTATE and the schema names, never a value."""
    parts: list[str] = []
    state = next((code for error in errors if (code := own_sqlstate(error))), None)
    if state:
        parts.append(f"SQLSTATE {state}")
    for field in SCHEMA_FIELDS:
        name = next((value for error in errors if (value := own_schema_name(error, field))), None)
        if name:
            parts.append(f"{field.removesuffix('_name')} {name}")
    position = next(
        (found.group(0) for error in errors if (found := _ARGUMENT_POSITION.match(_text(error)))),
        None,
    )
    if position:
        parts.append(position)
    return f"[{DRIVER_TEXT_WITHHELD}{': ' + ', '.join(parts) if parts else ''}]"


def database_error_redactor(exc: BaseException | None) -> Callable[[str], str] | None:
    """A rewrite that withholds what ``exc``'s database errors quote, or ``None``.

    ``None`` when nothing in the chain can quote a value, so the caller keeps
    its text as it is. Otherwise the rewrite replaces each such error's own
    text, wherever it appears - its own traceback line, SQLAlchemy's message
    that embeds it, an ``error=str(exc)`` - with :func:`database_error_summary`.
    The longest text goes first, so an outer error's text is replaced whole
    before an inner one it embeds could split it.
    """
    if exc is None:
        return None
    withheld = withheld_errors(exc)
    if not withheld:
        return None
    summary = database_error_summary(withheld)
    texts = sorted(
        {text for error in withheld if (text := _text(error).strip())}, key=len, reverse=True
    )

    def redact(text: str) -> str:
        for raw in texts:
            text = text.replace(raw, summary)
        return text

    return redact


__all__ = [
    "DRIVER_TEXT_WITHHELD",
    "SCHEMA_FIELDS",
    "VALUE_BEARING_SQLSTATE_CLASSES",
    "database_error_redactor",
    "database_error_summary",
    "own_schema_name",
    "own_sqlstate",
    "schema_name_of",
    "sqlstate_of",
    "withheld_errors",
]
