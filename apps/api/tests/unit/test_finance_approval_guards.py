"""Overriding a three-way-match variance is refused the way every denial is.

The service-side check used to be a ``BusinessRuleError`` dressed as a 403,
with the permission hand-typed and no ``required_permission`` detail - the one
denial in the API a client could not act on, and the one the demo seeder's
``except BusinessRuleError`` quietly swallowed. It is now an
``AuthorizationError`` spelled from the ``Permission`` enum, so the shape
matches the route gate and the other service-level guards.
"""

from __future__ import annotations

import uuid

import pytest

from src.core.deps import Principal
from src.core.errors import AuthorizationError, BusinessRuleError, ErrorCode
from src.core.permissions import Permission
from src.modules.finance.service import assert_may_override_variance

pytestmark = pytest.mark.unit

INVOICE_CODE = "VI/2026-27/00012"


def _principal(*permissions: Permission, superuser: bool = False) -> Principal:
    return Principal(
        user_id=uuid.uuid4(),
        company_id=uuid.uuid4(),
        email="accounts@test.local",
        full_name="Accounts",
        is_superuser=superuser,
        permissions=frozenset(str(p) for p in permissions),
    )


class TestOverrideVariance:
    def test_without_the_approval_right_the_denial_names_it_and_the_invoice(self) -> None:
        with pytest.raises(AuthorizationError) as exc:
            assert_may_override_variance(
                _principal(Permission.VENDOR_INVOICE_CREATE), invoice_code=INVOICE_CODE
            )

        assert exc.value.status_code == 403
        assert exc.value.code is ErrorCode.PERMISSION_DENIED
        assert exc.value.details["required_permission"] == str(Permission.VENDOR_INVOICE_APPROVE)
        assert str(Permission.VENDOR_INVOICE_APPROVE) in exc.value.message
        assert INVOICE_CODE in exc.value.message
        # Not a business-rule failure: the seeder's ``except BusinessRuleError``
        # around a walked-through approval must not swallow a denial.
        assert not isinstance(exc.value, BusinessRuleError)

    def test_the_approval_right_passes(self) -> None:
        assert_may_override_variance(
            _principal(Permission.VENDOR_INVOICE_APPROVE), invoice_code=INVOICE_CODE
        )

    def test_a_superuser_passes(self) -> None:
        assert_may_override_variance(_principal(superuser=True), invoice_code=INVOICE_CODE)
