"""Baseline reference data.

Creates what an empty database needs before anyone can sign in: a company, the
role catalogue from :mod:`src.core.permissions`, an administrator, standard units
of measure, and a default warehouse per type.

Idempotent by design — running it twice must not duplicate anything, because it
runs on every fresh environment and people will run it again "just in case".

    python -m src.core.seed
    python -m src.core.seed --company "Atcraft Hardware" --admin-email ops@atcraft.example
"""

from __future__ import annotations

import argparse
import asyncio
import secrets
import sys
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.clock import utcnow
from src.core.database import session_scope
from src.core.ids import new_id
from src.core.logging import configure_logging, get_logger
from src.core.permissions import ROLE_PRESETS
from src.core.registry import import_all_models
from src.core.security import hash_password
from src.modules.organization.models import Branch, Company
from src.modules.users.models import Role, User

log = get_logger("seed")

#: The administrator created when nobody names one.
#:
#: NOT a .local / .test address, however natural those look for a seeded
#: environment: Pydantic's ``EmailStr`` refuses special-use TLDs, so an account
#: seeded at ``admin@atcraft.local`` is created successfully and can then never
#: log in - ``/auth/login`` rejects its own seeded identity at validation. The
#: ``.example`` TLD is RFC 2606 reserved, so it passes validation and still
#: cannot collide with a real domain or leak mail to a real recipient.
#: ``tests/integration/test_core_seed.py`` holds this to the same validator the
#: login route uses.
DEFAULT_ADMIN_EMAIL = "admin@atcraft.example"

# --- Units of measure ---------------------------------------------------------
# Architectural hardware is counted, weighed and measured, so all three families
# are present. `is_base` marks the unit conversions resolve through.
UNITS: tuple[dict[str, Any], ...] = (
    {
        "code": "NOS",
        "name": "Numbers",
        "uom_category": "COUNT",
        "is_base": True,
        "decimal_places": 0,
    },
    {
        "code": "PCS",
        "name": "Pieces",
        "uom_category": "COUNT",
        "is_base": False,
        "decimal_places": 0,
    },
    {"code": "SET", "name": "Set", "uom_category": "COUNT", "is_base": False, "decimal_places": 0},
    {
        "code": "PAIR",
        "name": "Pair",
        "uom_category": "COUNT",
        "is_base": False,
        "decimal_places": 0,
    },
    {"code": "BOX", "name": "Box", "uom_category": "COUNT", "is_base": False, "decimal_places": 0},
    {
        "code": "KG",
        "name": "Kilogram",
        "uom_category": "WEIGHT",
        "is_base": True,
        "decimal_places": 3,
    },
    {"code": "GM", "name": "Gram", "uom_category": "WEIGHT", "is_base": False, "decimal_places": 3},
    {
        "code": "TON",
        "name": "Metric Tonne",
        "uom_category": "WEIGHT",
        "is_base": False,
        "decimal_places": 4,
    },
    {
        "code": "MTR",
        "name": "Metre",
        "uom_category": "LENGTH",
        "is_base": True,
        "decimal_places": 3,
    },
    {
        "code": "MM",
        "name": "Millimetre",
        "uom_category": "LENGTH",
        "is_base": False,
        "decimal_places": 2,
    },
    {"code": "FT", "name": "Feet", "uom_category": "LENGTH", "is_base": False, "decimal_places": 3},
    {
        "code": "SQM",
        "name": "Square Metre",
        "uom_category": "AREA",
        "is_base": True,
        "decimal_places": 4,
    },
    {
        "code": "LTR",
        "name": "Litre",
        "uom_category": "VOLUME",
        "is_base": True,
        "decimal_places": 3,
    },
    {"code": "HR", "name": "Hour", "uom_category": "TIME", "is_base": True, "decimal_places": 2},
)

# Conversions to the base unit of the same family.
UOM_CONVERSIONS: tuple[tuple[str, str, str], ...] = (
    ("PCS", "NOS", "1"),
    ("PAIR", "NOS", "2"),
    ("GM", "KG", "0.001"),
    ("TON", "KG", "1000"),
    ("MM", "MTR", "0.001"),
    ("FT", "MTR", "0.3048"),
)

# --- Warehouses ---------------------------------------------------------------
# One per functional stage, matching the warehouse types in master rules 9. QC
# hold, scrap and job-work stores exist as real warehouses so that stock in those
# states has somewhere to live and still appears in valuation.
WAREHOUSES: tuple[dict[str, str], ...] = (
    {"code": "WH-RM", "name": "Raw Material Store", "warehouse_type": "RAW_MATERIAL"},
    {"code": "WH-CMP", "name": "Component Store", "warehouse_type": "COMPONENT"},
    {"code": "WH-WIP", "name": "Work in Progress", "warehouse_type": "WIP"},
    {"code": "WH-ASM", "name": "Assembly Store", "warehouse_type": "ASSEMBLY"},
    {"code": "WH-FG", "name": "Finished Goods", "warehouse_type": "FINISHED_GOODS"},
    {"code": "WH-PKG", "name": "Packing Material Store", "warehouse_type": "PACKING"},
    {"code": "WH-QC", "name": "QC Hold", "warehouse_type": "QC_HOLD"},
    {"code": "WH-JW", "name": "Job Work (at vendor)", "warehouse_type": "JOB_WORK"},
    {"code": "WH-SCR", "name": "Scrap Yard", "warehouse_type": "SCRAP"},
    {"code": "WH-RET", "name": "Sales Return Store", "warehouse_type": "RETURN"},
    {"code": "WH-CNS", "name": "Consumables Store", "warehouse_type": "CONSUMABLES"},
)

ITEM_CATEGORIES: tuple[tuple[str, str, str | None], ...] = (
    ("LOCKS", "Locks", None),
    ("LOCK-MORT", "Mortise Locks", "LOCKS"),
    ("LOCK-CYL", "Cylinders", "LOCKS"),
    ("HANDLES", "Handles", None),
    ("HANDLE-ROSE", "Rose Handles", "HANDLES"),
    ("HANDLE-LEVER", "Lever Handles", "HANDLES"),
    ("HINGES", "Hinges", None),
    ("ACCESSORIES", "Accessories", None),
    ("RAW", "Raw Materials", None),
    ("RAW-BRASS", "Brass", "RAW"),
    ("RAW-SS", "Stainless Steel", "RAW"),
    ("RAW-ZINC", "Zinc Alloy", "RAW"),
    ("FASTENERS", "Fasteners", None),
    ("PACKING", "Packing Materials", None),
    ("CONSUMABLE", "Consumables", None),
)


async def _get_or_create_company(session: AsyncSession, name: str, code: str) -> Company:
    existing = (
        await session.execute(select(Company).where(Company.code == code))
    ).scalar_one_or_none()
    if existing:
        log.info("seed.company_exists", code=code)
        return existing

    company = Company(
        code=code,
        name=name,
        legal_name=name,
        country="India",
        currency="INR",
        financial_year_start_month=4,
        timezone="Asia/Kolkata",
        is_active=True,
    )
    session.add(company)
    await session.flush()
    log.info("seed.company_created", code=code, id=str(company.id))
    return company


async def _seed_roles(session: AsyncSession, company: Company) -> dict[str, Role]:
    """Create the role catalogue from the presets.

    Existing roles are left alone: an administrator may legitimately have edited
    a role's permission set, and re-seeding must not undo that (master rules 81 —
    prefer configurable behaviour).
    """
    existing = {
        role.code: role
        for role in (
            await session.execute(select(Role).where(Role.company_id == company.id))
        ).scalars()
    }

    created = 0
    refreshed = 0
    for code, spec in ROLE_PRESETS.items():
        permission_values = spec["permissions"]
        if not isinstance(permission_values, (list, tuple, set, frozenset)):
            raise TypeError(f"Role preset {code!r} has an invalid permissions collection")
        wanted = sorted(str(permission) for permission in permission_values)

        if code in existing:
            # A SYSTEM role's permission list is ours, not the client's, and the
            # stored copy is a snapshot from the day the company was seeded. So
            # a permission added in a later release reached the enum, the
            # presets and the API and never reached the role - the screen it
            # gates stayed invisible to every existing account, including the
            # superuser, and the only cure was editing roles by hand on each
            # deployment. Refreshing here closes that for every future release.
            #
            # Only `is_system` roles. A role the client built is theirs, and
            # rewriting its permissions from a preset would silently undo
            # whatever they had decided.
            role = existing[code]
            if role.is_system and role.permissions != wanted:
                role.permissions = wanted
                refreshed += 1
            continue
        role = Role(
            company_id=company.id,
            code=code,
            name=str(spec["name"]),
            description=str(spec["description"]),
            permissions=wanted,
            is_system=bool(spec.get("is_system")),
            is_vendor_role=bool(spec.get("is_vendor_role")),
            is_dealer_role=bool(spec.get("is_dealer_role")),
            is_active=True,
        )
        session.add(role)
        existing[code] = role
        created += 1

    await session.flush()
    log.info("seed.roles", created=created, refreshed=refreshed, total=len(existing))
    return existing


async def _seed_admin(
    session: AsyncSession,
    company: Company,
    roles: dict[str, Role],
    email: str,
    password: str | None,
) -> tuple[User, str | None]:
    existing = (
        await session.execute(
            select(User).where(User.company_id == company.id, User.email == email.lower())
        )
    ).scalar_one_or_none()
    if existing:
        log.info("seed.admin_exists", email=email)
        return existing, None

    # A generated password is printed once and never stored in plaintext. The
    # account is forced to change it at first sign-in.
    generated = password or f"Atc-{secrets.token_urlsafe(12)}!7"

    admin = User(
        company_id=company.id,
        email=email.lower(),
        full_name="System Administrator",
        password_hash=hash_password(generated),
        password_changed_at=utcnow(),
        must_change_password=True,
        is_active=True,
        is_superuser=True,
    )
    super_admin = roles.get("SUPER_ADMIN")
    if super_admin:
        admin.roles.append(super_admin)

    session.add(admin)
    await session.flush()
    log.info("seed.admin_created", email=email, id=str(admin.id))
    return admin, generated


async def _seed_branch(session: AsyncSession, company: Company, user_id: Any) -> Branch:
    existing = (
        await session.execute(
            select(Branch).where(Branch.company_id == company.id, Branch.code == "HO")
        )
    ).scalar_one_or_none()
    if existing:
        return existing

    branch = Branch(
        company_id=company.id,
        code="HO",
        name="Head Office",
        is_head_office=True,
        is_active=True,
        created_by_id=user_id,
    )
    session.add(branch)
    await session.flush()
    return branch


async def _seed_optional(
    session: AsyncSession, company: Company, branch: Branch, user_id: Any
) -> None:
    """Seed data owned by modules that may not be present yet.

    The masters modules are built independently, so this runs against whichever
    of them exist. A missing module is skipped rather than failing the whole
    seed — a partially-built checkout must still produce a usable login.
    """
    # --- units of measure ---------------------------------------------------
    try:
        from src.modules.products.models import UnitOfMeasure, UomConversion

        present = {
            row.code
            for row in (
                await session.execute(
                    select(UnitOfMeasure).where(UnitOfMeasure.company_id == company.id)
                )
            ).scalars()
        }
        for spec in UNITS:
            if spec["code"] in present:
                continue
            session.add(UnitOfMeasure(company_id=company.id, created_by_id=user_id, **spec))
        await session.flush()

        by_code = {
            row.code: row
            for row in (
                await session.execute(
                    select(UnitOfMeasure).where(UnitOfMeasure.company_id == company.id)
                )
            ).scalars()
        }
        for from_code, to_code, factor in UOM_CONVERSIONS:
            source, target = by_code.get(from_code), by_code.get(to_code)
            if not source or not target:
                continue
            exists = (
                await session.execute(
                    select(UomConversion).where(
                        UomConversion.company_id == company.id,
                        UomConversion.from_uom_id == source.id,
                        UomConversion.to_uom_id == target.id,
                    )
                )
            ).scalar_one_or_none()
            if exists:
                continue
            session.add(
                UomConversion(
                    company_id=company.id,
                    from_uom_id=source.id,
                    to_uom_id=target.id,
                    factor=factor,
                    created_by_id=user_id,
                )
            )
        await session.flush()
        log.info("seed.uom", count=len(UNITS))
    except (ImportError, AttributeError) as exc:
        log.warning("seed.uom_skipped", reason=str(exc))

    # --- item categories ----------------------------------------------------
    try:
        from src.modules.products.models import ItemCategory
        from src.modules.products.service import build_category_path, category_depth

        present_cat = {
            row.code: row
            for row in (
                await session.execute(
                    select(ItemCategory).where(ItemCategory.company_id == company.id)
                )
            ).scalars()
        }
        # Two passes so a child never looks for a parent that has not been
        # flushed yet.
        # ``path`` is NOT NULL and is the materialised ancestor chain, which
        # includes the node's OWN id - so the id has to exist before the row is
        # built rather than being left to the column default. Both it and
        # ``depth`` are derived with the same helpers the products service uses
        # (``build_category_path`` / ``category_depth``); a second path format
        # invented here would sort and prefix-match differently from every
        # category the application creates, and the LIKE-based subtree queries
        # would quietly miss the seeded ones.
        for code, name, parent in ITEM_CATEGORIES:
            if parent is None and code not in present_cat:
                category_id = new_id()
                path = build_category_path(None, category_id)
                category = ItemCategory(
                    id=category_id,
                    company_id=company.id,
                    code=code,
                    name=name,
                    path=path,
                    depth=category_depth(path),
                    created_by_id=user_id,
                )
                session.add(category)
                present_cat[code] = category
        await session.flush()

        for code, name, parent in ITEM_CATEGORIES:
            if parent is not None and code not in present_cat:
                parent_row = present_cat.get(parent)
                category_id = new_id()
                path = build_category_path(parent_row.path if parent_row else None, category_id)
                category = ItemCategory(
                    id=category_id,
                    company_id=company.id,
                    code=code,
                    name=name,
                    parent_id=parent_row.id if parent_row else None,
                    path=path,
                    depth=category_depth(path),
                    created_by_id=user_id,
                )
                session.add(category)
                present_cat[code] = category
        await session.flush()
        log.info("seed.categories", count=len(ITEM_CATEGORIES))
    except (ImportError, AttributeError) as exc:
        log.warning("seed.categories_skipped", reason=str(exc))

    # --- warehouses ---------------------------------------------------------
    try:
        from src.modules.warehouses.models import Warehouse

        present_wh = {
            row.code
            for row in (
                await session.execute(select(Warehouse).where(Warehouse.company_id == company.id))
            ).scalars()
        }
        for index, spec in enumerate(WAREHOUSES):
            if spec["code"] in present_wh:
                continue
            session.add(
                Warehouse(
                    company_id=company.id,
                    branch_id=branch.id,
                    is_active=True,
                    is_default=(index == 0),
                    created_by_id=user_id,
                    **spec,
                )
            )
        await session.flush()
        log.info("seed.warehouses", count=len(WAREHOUSES))
    except (ImportError, AttributeError, TypeError) as exc:
        log.warning("seed.warehouses_skipped", reason=str(exc))


async def seed(
    company_name: str, company_code: str, admin_email: str, admin_password: str | None
) -> None:
    import_all_models()

    async with session_scope() as session:
        company = await _get_or_create_company(session, company_name, company_code)
        roles = await _seed_roles(session, company)
        admin, generated = await _seed_admin(session, company, roles, admin_email, admin_password)
        branch = await _seed_branch(session, company, admin.id)
        await _seed_optional(session, company, branch, admin.id)

    print("\n" + "=" * 66)
    print("  Seed complete")
    print("=" * 66)
    print(f"  Company    {company_name} ({company_code})")
    print(f"  Sign in    {admin_email}")
    if generated:
        print(f"  Password   {generated}")
        print("\n  This password is shown ONCE and is not stored anywhere in")
        print("  plaintext. The account must change it at first sign-in.")
    else:
        print("  Password   unchanged (the account already existed)")
    print("=" * 66 + "\n")


def main() -> int:
    parser = argparse.ArgumentParser(description="Seed baseline reference data")
    parser.add_argument("--company", default="Atcraft", help="Company display name")
    parser.add_argument("--company-code", default="ATCRAFT", help="Company code")
    parser.add_argument(
        "--admin-email",
        default=DEFAULT_ADMIN_EMAIL,
        help="Administrator email (default: %(default)s)",
    )
    parser.add_argument(
        "--admin-password",
        default=None,
        help="Administrator password. Omit to have a strong one generated and printed once.",
    )
    args = parser.parse_args()

    configure_logging()
    try:
        asyncio.run(seed(args.company, args.company_code, args.admin_email, args.admin_password))
    except Exception as exc:
        log.error("seed.failed", error=str(exc), exc_info=exc)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
