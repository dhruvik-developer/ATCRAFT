"""Demo tools must distinguish identical administrator emails in two tenants."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.demo_data import DemoDataError, _resolve_principal
from src.modules.organization.models import Company
from src.modules.users.models import User

pytestmark = pytest.mark.integration


async def test_demo_principal_requires_company_when_email_is_ambiguous(
    session: AsyncSession, company: Company, user: User
) -> None:
    other_company = Company(code=f"SEED{uuid.uuid4().hex[:8].upper()}", name="Other seed tenant")
    session.add(other_company)
    await session.flush()
    other_user = User(
        company_id=other_company.id,
        email=user.email,
        full_name="Other seed administrator",
        password_hash=user.password_hash,
        is_active=True,
        is_superuser=True,
    )
    session.add(other_user)
    await session.flush()

    with pytest.raises(DemoDataError, match="specify --company-code"):
        await _resolve_principal(session, user.email)

    selected = await _resolve_principal(session, user.email, company_code=other_company.code)
    assert selected.company_id == other_company.id
    assert selected.user_id == other_user.id
    original = await _resolve_principal(session, user.email, company_code=company.code)
    assert original.company_id == company.id
    assert original.user_id == user.id

    with pytest.raises(DemoDataError, match="no user"):
        await _resolve_principal(session, user.email, company_code="UNKNOWN-SEED-COMPANY")
