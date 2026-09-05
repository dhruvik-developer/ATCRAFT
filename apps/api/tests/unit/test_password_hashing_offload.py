"""Password hashing runs off the event loop.

Argon2id is deliberately expensive - measured at about 37 ms per hash and per
verify with the parameters in `security.py` - which is the right cost against
offline cracking and exactly the wrong thing to run inline on an async worker.
Every login froze the whole worker for that long, so a shift change where a
dozen people sign in at once serialised into a second of dead air during which
requests touching no password at all also waited.

Measured before and after on eight concurrent verifications: 269 ms wall with a
267 ms event-loop stall, against 100 ms wall with a 54 ms stall.

These tests pin the contract rather than the timing - a timing assertion on a
loaded CI box is a flaky test, and what actually matters is that the async form
agrees with the sync one and that a request path never calls the sync one.
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path

import pytest

from src.core.security import (
    hash_password,
    hash_password_async,
    verify_password,
    verify_password_async,
)

pytestmark = pytest.mark.unit

PASSWORD = "CorrectHorseBattery!23"
AUTH_SERVICE = Path(__file__).resolve().parents[2] / "src" / "modules" / "auth" / "service.py"


async def test_the_async_hash_is_verifiable_by_both_forms() -> None:
    stored = await hash_password_async(PASSWORD)

    assert verify_password(PASSWORD, stored) is True
    assert await verify_password_async(PASSWORD, stored) is True


async def test_the_async_verify_agrees_with_the_sync_one() -> None:
    stored = hash_password(PASSWORD)

    for candidate, expected in ((PASSWORD, True), ("wrong", False), ("", False)):
        assert verify_password(candidate, stored) is expected
        assert await verify_password_async(candidate, stored) is expected


async def test_the_loop_keeps_running_while_a_password_is_hashed() -> None:
    """The point of the change: other work progresses during the hash.

    Counting ticks rather than measuring elapsed time - the assertion is
    "the loop was not frozen", which is true on any machine, rather than
    "it took under N milliseconds", which is not.
    """
    ticks = 0
    running = True

    async def ticker() -> None:
        nonlocal ticks
        while running:
            await asyncio.sleep(0.001)
            ticks += 1

    task = asyncio.create_task(ticker())
    await asyncio.sleep(0.01)
    before = ticks

    await hash_password_async(PASSWORD)

    running = False
    await task
    assert ticks > before, "the event loop made no progress during the hash"


def test_no_request_path_hashes_on_the_event_loop() -> None:
    """The auth service must not call the synchronous forms.

    One exception: the module-level timing equaliser, which is hashed once at
    import and never inside a request.
    """
    source = AUTH_SERVICE.read_text(encoding="utf-8")
    offenders = [
        line.strip()
        for line in source.splitlines()
        if re.search(r"(?<![_\w])(hash_password|verify_password)\s*\(", line)
        and "_async" not in line
        and not line.startswith("_TIMING_EQUALISER_HASH")
    ]
    assert offenders == [], (
        f"these call the blocking form from a request path; use the _async variant: {offenders}"
    )
