"""What a failed statement's text carries into the logs and the error tracker."""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError

from src.core import database
from src.core.config import AppEnv, settings

pytestmark = pytest.mark.unit


def test_the_applications_engine_keeps_bound_values_out_of_error_text() -> None:
    assert database.engine.sync_engine.hide_parameters is True


@pytest.mark.parametrize("environment", [AppEnv.test, AppEnv.production])
def test_every_engine_the_application_builds_hides_them(
    monkeypatch: pytest.MonkeyPatch, environment: AppEnv
) -> None:
    """Both branches: the test engine and the pooled one every deployment uses."""
    monkeypatch.setattr(settings, "app_env", environment)

    engine = database._create_engine()

    try:
        assert engine.sync_engine.hide_parameters is True
    finally:
        engine.sync_engine.dispose()


@pytest.mark.parametrize("hidden", [False, True])
def test_hidden_parameters_leave_the_sql_and_drop_the_values(hidden: bool) -> None:
    """The self-test: the flag is what decides whether the value is in the text."""
    engine = create_engine("sqlite://", hide_parameters=hidden)
    with engine.connect() as connection:
        connection.execute(text("CREATE TABLE people (email TEXT UNIQUE)"))
        statement = text("INSERT INTO people VALUES (:email)")
        connection.execute(statement, {"email": "priya@example.com"})
        with pytest.raises(IntegrityError) as raised:
            connection.execute(statement, {"email": "priya@example.com"})

    message = str(raised.value)
    assert "INSERT INTO people" in message
    assert ("priya@example.com" in message) is not hidden
