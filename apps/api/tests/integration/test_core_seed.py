"""The seed is a shipped command and had no test at all.

``make seed`` is documented in CLAUDE.md as the way to make an empty database
usable, and three separate defects shipped in it because nothing ever ran it
against a real schema:

* ``UNITS`` passed ``uom_type=`` to a model whose column is ``uom_category`` -
  a ``TypeError`` on the very first unit;
* item categories were built without ``path``, which is ``NOT NULL`` - the seed
  died on a ``NotNullViolationError`` after the units had already been written;
* the default administrator was ``admin@atcraft.local``, and Pydantic's
  ``EmailStr`` refuses special-use TLDs - so the account was created and could
  then never log in, because ``/auth/login`` rejected its own seeded identity.

None of the 3 400 tests caught any of them, because every test built its own
fixtures instead. These tests run the real seed functions against the real
metadata, so the next such mistake fails here rather than on someone's first
five minutes with the product.
"""

from __future__ import annotations

import uuid

import pytest
from pydantic import BaseModel, EmailStr, ValidationError
from sqlalchemy import inspect as sa_inspect
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core import seed as seed_module
from src.core.deps import TenantContext
from src.modules.products.models import ItemCategory, UnitOfMeasure
from src.modules.warehouses.models import Warehouse

pytestmark = pytest.mark.integration


def _column_names(model: type) -> set[str]:
    return {attr.key for attr in sa_inspect(model).mapper.column_attrs}


class TestSeedSpecsMatchTheirModels:
    """Every literal spec must name columns that actually exist.

    A dict splatted into a model constructor fails at runtime, not at import,
    so a wrong key here is invisible until somebody seeds a database.
    """

    def test_every_unit_spec_key_is_a_real_column(self) -> None:
        columns = _column_names(UnitOfMeasure)
        for spec in seed_module.UNITS:
            unknown = set(spec) - columns
            assert not unknown, (
                f"UNITS entry {spec.get('code')!r} passes {sorted(unknown)}, which "
                f"{UnitOfMeasure.__name__} does not define. Known columns: {sorted(columns)}"
            )

    def test_every_warehouse_spec_key_is_a_real_column(self) -> None:
        columns = _column_names(Warehouse)
        for spec in seed_module.WAREHOUSES:
            unknown = set(spec) - columns
            assert not unknown, (
                f"WAREHOUSES entry {spec.get('code')!r} passes {sorted(unknown)}, which "
                f"{Warehouse.__name__} does not define."
            )

    def test_unit_categories_are_real_enum_members(self) -> None:
        from src.modules.products.models import UomCategory

        valid = {str(member) for member in UomCategory}
        for spec in seed_module.UNITS:
            assert spec["uom_category"] in valid, (
                f"{spec['code']} declares category {spec['uom_category']!r}, "
                f"which is not one of {sorted(valid)}"
            )

    def test_every_unit_family_has_exactly_one_base(self) -> None:
        """Conversions resolve through a base unit; two bases is ambiguous, none
        is unresolvable."""
        families: dict[str, list[str]] = {}
        for spec in seed_module.UNITS:
            if spec["is_base"]:
                families.setdefault(spec["uom_category"], []).append(spec["code"])
        for spec in seed_module.UNITS:
            family = spec["uom_category"]
            assert families.get(family), f"family {family} has no base unit"
            assert len(families[family]) == 1, (
                f"family {family} has several base units: {families[family]}"
            )


class TestTheSeededAdministratorCanActuallySignIn:
    """The seeded identity must satisfy the same validation the login route uses.

    This is the whole of the third defect: the account existed and was correct
    in the database, and the API refused the email before ever looking at it.
    """

    def test_the_default_admin_email_passes_the_api_validator(self) -> None:
        class LoginLike(BaseModel):
            email: EmailStr

        try:
            LoginLike(email=seed_module.DEFAULT_ADMIN_EMAIL)
        except ValidationError as exc:
            pytest.fail(
                f"The seeded administrator email {seed_module.DEFAULT_ADMIN_EMAIL!r} is "
                "refused by the same EmailStr validation /auth/login applies, so the "
                f"account this command creates could never sign in. {exc}"
            )

    def test_the_parser_really_uses_that_constant(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Guards against the constant and the shipped CLI default drifting apart.

        Asserting on the constant alone would be vacuous if the parser stopped
        using it, so this reads the default out of ``--help`` as a user sees it.
        """
        import contextlib
        import io as _io
        import sys

        monkeypatch.setattr(sys, "argv", ["seed", "--help"])
        captured = _io.StringIO()
        with contextlib.redirect_stdout(captured), contextlib.suppress(SystemExit):
            seed_module.main()
        helptext = captured.getvalue()
        assert helptext, "--help produced nothing; the parser did not run"
        assert seed_module.DEFAULT_ADMIN_EMAIL in helptext, (
            "--admin-email no longer defaults to DEFAULT_ADMIN_EMAIL, so the "
            "validated constant and the shipped default have diverged. "
            f"--help said: {helptext[:400]}"
        )

    @pytest.mark.parametrize("rejected", ["admin@atcraft.local", "admin@atcraft.test"])
    def test_the_validator_really_does_refuse_special_use_domains(self, rejected: str) -> None:
        """Proves the test above is not vacuous."""

        class LoginLike(BaseModel):
            email: EmailStr

        with pytest.raises(ValidationError):
            LoginLike(email=rejected)


class TestSeedingAnEmptyDatabase:
    """Run the real seed helpers against the real schema."""

    async def test_optional_masters_seed_and_are_idempotent(
        self, session: AsyncSession, tenant: TenantContext
    ) -> None:
        company = await seed_module._get_or_create_company(
            session, "Seed Test Co", f"ST{uuid.uuid4().hex[:6].upper()}"
        )
        roles = await seed_module._seed_roles(session, company)
        assert roles, "the role catalogue is empty"

        admin, _ = await seed_module._seed_admin(
            session, company, roles, f"seed-{uuid.uuid4().hex[:8]}@atcraft.example", "Sd-Test!2026x"
        )
        branch = await seed_module._seed_branch(session, company, admin.id)

        await seed_module._seed_optional(session, company, branch, admin.id)
        await session.flush()

        units = (
            (
                await session.execute(
                    select(UnitOfMeasure).where(UnitOfMeasure.company_id == company.id)
                )
            )
            .scalars()
            .all()
        )
        assert len(units) == len(seed_module.UNITS)

        categories = (
            (
                await session.execute(
                    select(ItemCategory).where(ItemCategory.company_id == company.id)
                )
            )
            .scalars()
            .all()
        )
        assert len(categories) == len(seed_module.ITEM_CATEGORIES)

        # The second defect: path is NOT NULL and is what every subtree query
        # matches on, so a blank one is not merely untidy - it is unqueryable.
        for category in categories:
            assert category.path, f"category {category.code} has no materialised path"
            assert category.path.startswith("/") and category.path.endswith("/"), (
                f"category {category.code} path {category.path!r} is not /a/b/ shaped"
            )
            assert str(category.id) in category.path, (
                f"category {category.code} path {category.path!r} does not contain its own id, "
                "so a LIKE prefix search cannot find its descendants"
            )

        by_code = {c.code: c for c in categories}
        for code, _name, parent_code in seed_module.ITEM_CATEGORIES:
            node = by_code[code]
            if parent_code is None:
                assert node.depth == 0, f"root {code} has depth {node.depth}"
            else:
                parent = by_code[parent_code]
                assert node.path.startswith(parent.path), (
                    f"{code} path {node.path!r} is not under its parent {parent.path!r}"
                )
                assert node.depth == parent.depth + 1

        # Idempotence: the command is documented as safe to re-run.
        await seed_module._seed_optional(session, company, branch, admin.id)
        await session.flush()

        again = (
            (
                await session.execute(
                    select(UnitOfMeasure).where(UnitOfMeasure.company_id == company.id)
                )
            )
            .scalars()
            .all()
        )
        assert len(again) == len(seed_module.UNITS), "re-seeding duplicated the units"

        cats_again = (
            (
                await session.execute(
                    select(ItemCategory).where(ItemCategory.company_id == company.id)
                )
            )
            .scalars()
            .all()
        )
        assert len(cats_again) == len(seed_module.ITEM_CATEGORIES), (
            "re-seeding duplicated the item categories"
        )
