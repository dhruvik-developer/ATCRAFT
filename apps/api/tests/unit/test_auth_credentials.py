"""Credential-resolution helpers - no database.

These are the pure pieces of the sign-in path: which account an address refers
to, the constant hash that keeps an unknown address costing the same as a known
one, and reading a session id out of a refresh token without trusting it.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

import pytest

from src.core.security import (
    create_access_token,
    create_refresh_token,
    hash_secret_token,
    verify_password,
)
from src.modules.auth.service import (
    _TIMING_EQUALISER_HASH,
    FAILURE_AMBIGUOUS_EMAIL,
    FAILURE_UNKNOWN_EMAIL,
    _email_domain,
    _resolve_account,
    _session_id_from_refresh,
)

pytestmark = pytest.mark.unit


@dataclass
class FakeAccount:
    company_id: uuid.UUID
    id: uuid.UUID


def _account() -> FakeAccount:
    return FakeAccount(company_id=uuid.uuid4(), id=uuid.uuid4())


# -----------------------------------------------------------------------------
# Account resolution
# -----------------------------------------------------------------------------


def test_no_match_reports_an_unknown_address() -> None:
    account, reason = _resolve_account([])
    assert account is None
    assert reason == FAILURE_UNKNOWN_EMAIL


def test_single_match_resolves() -> None:
    only = _account()
    account, reason = _resolve_account([only])
    assert account is only
    assert reason is None


def test_two_tenants_sharing_an_address_are_refused_not_guessed() -> None:
    """``users`` is unique on (company_id, email), so one address can exist in
    two companies. Picking one would sign the person into the wrong company's
    data; the service requires an explicit company_code instead."""
    account, reason = _resolve_account([_account(), _account()])
    assert account is None
    assert reason == FAILURE_AMBIGUOUS_EMAIL


# -----------------------------------------------------------------------------
# Timing equaliser
# -----------------------------------------------------------------------------


def test_dummy_hash_is_argon2id_so_the_verify_costs_what_a_real_one_costs() -> None:
    assert _TIMING_EQUALISER_HASH.startswith("$argon2id$")


@pytest.mark.parametrize("attempt", ["", "password", "S3cure-Passphrase!2026"])
def test_nothing_verifies_against_the_dummy_hash(attempt: str) -> None:
    """It is built from a per-process random secret, so it can never become a
    universal password the way a checked-in hash could."""
    assert verify_password(attempt, _TIMING_EQUALISER_HASH) is False


# -----------------------------------------------------------------------------
# Reading a refresh token defensively
# -----------------------------------------------------------------------------


def test_session_id_is_read_from_a_valid_refresh_token() -> None:
    token, _ = create_refresh_token(uuid.uuid4(), uuid.uuid4(), session_id="sess-abc")
    assert _session_id_from_refresh(token) == "sess-abc"


def test_an_access_token_is_not_accepted_as_a_refresh_token() -> None:
    """Type confusion between the two would let a 15-minute credential act as a
    7-day one."""
    token, _ = create_access_token(uuid.uuid4(), uuid.uuid4(), session_id="sess-abc")
    assert _session_id_from_refresh(token) is None


@pytest.mark.parametrize("value", [None, "", "not.a.token", "a" * 40])
def test_unusable_tokens_yield_no_session_rather_than_raising(value: str | None) -> None:
    """Signing out must not fail because the client handed over rubbish - the
    session simply cannot be identified."""
    assert _session_id_from_refresh(value) is None


def test_refresh_tokens_are_stored_only_as_digests() -> None:
    token, _ = create_refresh_token(uuid.uuid4(), uuid.uuid4(), session_id="sess-abc")
    digest = hash_secret_token(token)
    assert digest != token
    assert len(digest) == 64
    assert hash_secret_token(token) == digest


# -----------------------------------------------------------------------------
# Logging helper
# -----------------------------------------------------------------------------


def test_only_the_domain_of_a_failed_address_is_logged() -> None:
    """A failed sign-in must not write an unverified address into the log; the
    domain is enough to spot a targeted run."""
    assert _email_domain("someone@atcraft.co.in") == "atcraft.co.in"
    assert _email_domain("garbage") == "unknown"
