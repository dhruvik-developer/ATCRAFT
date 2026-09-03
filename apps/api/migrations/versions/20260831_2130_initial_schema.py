"""initial schema

Revision ID: b7cc02046b2d
Revises:
Created: 2026-08-31 21:30:36.651151+00:00

The whole ERP schema in one revision: 103 tables across 25 modules, the three
extensions they depend on, and the triggers that make the append-only tables
actually append-only. Before this there was no way to build the schema outside
a test process.

Review checklist (master rules section 67)
------------------------------------------

* Locking impact:      nothing to plan around. Every statement takes ACCESS
                       EXCLUSIVE on a table it created moments earlier in the
                       same transaction, so no other session can hold or want
                       that lock. Nothing here should be built CONCURRENTLY:
                       CREATE INDEX CONCURRENTLY cannot run inside a
                       transaction, and on a zero-row table it would buy
                       nothing.

* Data migration:      none. There are no existing rows - this revision creates
                       the tables that will hold them.

* Rollback:            downgrade() drops all 103 tables, the three triggers and
                       the trigger function, leaving the database empty. It is
                       verified by upgrade / downgrade / upgrade from a freshly
                       created database.

                       Two things it deliberately does not restore:

                       - The extensions. DROP EXTENSION is database-wide, not
                         migration-scoped: the container init script
                         (infrastructure/docker/postgres/init/01-extensions.sql)
                         creates the same three on cluster init, and an operator
                         may have installed them for something outside this
                         schema. Leaving them is idempotent and harmless;
                         removing them is neither.
                       - Data. Against a populated database this downgrade
                         destroys every row in the system. That is inherent in
                         reversing an initial schema rather than an oversight,
                         but it means the only safe place to run it is a
                         database you are willing to lose.

* Production size:     seconds on any hardware. 103 empty CREATE TABLEs, 643
                       empty CREATE INDEXes, 556 CHECK constraints and 590
                       foreign keys. This schema's cost is paid later, on write
                       volume, not here.

* Index creation:      not CONCURRENTLY, deliberately - see locking impact.

Hand-written additions autogenerate could not produce
-----------------------------------------------------

1. CREATE EXTENSION for pgcrypto, pg_trgm and btree_gist, ahead of any table.
   pg_trgm is not optional: ix_items_code_trgm, ix_items_name_trgm and
   ix_item_variants_code_trgm are GIN indexes using gin_trgm_ops, and CREATE
   INDEX fails outright when the operator class is absent.

2. The append-only triggers (see the SQL further down). Alembic models neither
   triggers nor functions, so a schema built from autogenerate alone silently
   lacks a guarantee that docs/security.md section 9, docs/deployment.md's
   go-live checklist, src/modules/audit/models.py, .../repository.py and
   .../router.py all state is in place.

3. Foreign-key ordering. companies, branches, plants, departments, vendors and
   users form a reference cycle: users points at all four organisation tables,
   and all four carry created_by_id / updated_by_id back to users. SQLAlchemy
   cannot topologically sort that; it warned "Foreign key constraints involving
   these tables will not be considered" and autogenerate then emitted the six
   alphabetically with every FK inline - branches first, referencing a users
   table that does not exist yet. Fixed by creating the six in dependency order
   and adding the ten users-facing constraints afterwards with
   op.create_foreign_key; downgrade() drops those ten before the tables.

4. Column types. Autogenerate rendered three column types as the application's
   own TypeDecorator subclasses:
     - src.modules.inventory.models.StatusEnum(length=N), on 32 status columns.
       This one is not merely undesirable coupling, it does not run:
       StatusEnum.__init__ is (enum_class, length=24), so the rendered call
       raises TypeError for the missing positional argument. Every migration
       here would have failed on import.
     - src.core.types.CIText(length=N)
     - src.core.types.GUID()
   All three are VARCHAR or uuid in DDL. They are spelled as plain SQLAlchemy
   types so this revision describes the schema it built instead of importing
   model code that will keep moving. (Autogenerate also emitted no import for
   them, so the file would not have imported either way.)

Verified rather than rewritten - autogenerate got these right
-------------------------------------------------------------

inventory_balances.available_quantity survived as GENERATED ALWAYS AS
(on_hand_quantity - reserved_quantity - qc_hold_quantity - damaged_quantity)
STORED, as did stock_reservations.remaining_quantity. So did the COALESCE-keyed
unique indexes uq_inventory_balances_grain, uq_physical_count_lines_position
and uq_item_variants_attribute_combo; all 60 partial indexes, including
uq_boms_one_default_approved_per_item, uq_item_vendor_links_one_preferred and
uq_customer_addresses_one_default with their postgresql_where clauses; the
GIN indexes with postgresql_using / postgresql_ops; the three table comments;
and every CHECK constraint, including the inventory correctness floor
(on_hand_quantity >= 0, reserved_quantity <= on_hand_quantity,
allocated_quantity <= reserved_quantity).

Not added here, on purpose: the btree_gist EXCLUDE constraint on
financial_years that FinancialYearRepository.overlapping_active describes as the
correct fix for its check-then-write race. The extension it needs is installed
below, but the constraint is not declared on the model, so adding it would put
the database permanently out of step with the metadata and make every future
autogenerate try to drop it. It belongs in the revision that adds it to
src/modules/organization/models.py.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "b7cc02046b2d"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# --- extensions --------------------------------------------------------------
# IF NOT EXISTS because the Docker image's init script already creates these on
# a fresh cluster, while a managed-PostgreSQL deployment has no such hook and
# needs the migration to do it.
#
#   pgcrypto     gen_random_uuid / digest
#   pg_trgm      the GIN trigram indexes on items.code, items.name and
#                item_variants.code - how section 62 replaces Elasticsearch
#   btree_gist   exclusion constraints. No table uses one yet; it is installed
#                here because it is what FinancialYearRepository documents as
#                the fix for its overlap race, and a schema-level facility
#                should not be something a later, narrower migration has to
#                stop and arrange.
EXTENSIONS = ("pgcrypto", "pg_trgm", "btree_gist")


# --- append-only enforcement -------------------------------------------------
# Master rules section 41: "Ordinary users must not be able to modify audit
# records." A repository that refuses to write is a convention; a trigger is a
# guarantee, and it holds against psql, a migration, an ORM bug and a
# compromised service account alike.
#
# One function, three triggers, because the three tables do not want the same
# rule:
#
#   audit_logs              UPDATE and DELETE blocked. The model docstring, the
#                           table comment, docs/security.md section 9 and the
#                           deployment checklist all already claimed this
#                           trigger existed. It did not.
#
#   inventory_transactions  UPDATE and DELETE blocked, same treatment. It is an
#                           ImmutableLedgerEntity for the same reason and its
#                           own table comment reads "Corrections are offsetting
#                           rows, never updates". It also stores balance_after,
#                           the anchor every stock reconciliation is checked
#                           against: one UPDATE to a historic quantity
#                           invalidates every reconciliation after it and leaves
#                           nothing behind to say it happened. Stock value feeds
#                           finance, so this is a financial record in all but
#                           name.
#
#   login_attempts          UPDATE blocked, DELETE ALLOWED - deliberately not
#                           the same treatment. It is an ImmutableLedgerEntity
#                           too and no row should ever be edited, so UPDATE is
#                           barred. But it is security telemetry, not an audit
#                           record of a business action: it stores an e-mail
#                           address and an IP for every failed sign-in,
#                           including addresses that belong to no account.
#                           Retention pruning and subject-erasure requests are
#                           legitimate, expected DELETEs against exactly that
#                           data, and no sweep exists today only because none
#                           has been written yet - workers/tasks/maintenance.py
#                           prunes user_sessions and says in as many words that
#                           it leaves login_attempts alone. Blocking DELETE here
#                           would protect no audit trail while forcing a future
#                           retention job to ship a migration, or run
#                           ALTER TABLE ... DISABLE TRIGGER in production, to do
#                           something routine and correct.
#
# Worth knowing before someone debugs it at 2am: all three tables carry an FK to
# users with ON DELETE SET NULL, and PostgreSQL implements that cascade as an
# UPDATE of the child row. A hard DELETE of a users row therefore now fails on
# audit_logs and inventory_transactions. That is the intended outcome - an audit
# row must not be able to lose its actor - and it costs nothing in practice,
# because users are soft-deleted: UserService.delete_user sets deleted_at and
# the row is never removed (SoftDeleteMixin, master rules section 28).

REJECT_MUTATION_FUNCTION = """
CREATE OR REPLACE FUNCTION atcraft_reject_row_mutation() RETURNS trigger
LANGUAGE plpgsql AS $func$
BEGIN
    RAISE EXCEPTION USING
        ERRCODE = '42501',
        MESSAGE = TG_OP || ' on ' || TG_TABLE_NAME || ' is not permitted',
        DETAIL  = 'This table is append-only. Rows are written once and never '
                  'changed; a correction is a new, offsetting row.',
        HINT    = 'Master rules section 41; docs/database.md sections 4.2 and 4.3.';
    -- Unreachable. Kept so that if the RAISE above were ever weakened the
    -- trigger still fails closed: returning NULL from a BEFORE ... FOR EACH ROW
    -- trigger cancels the statement rather than letting it through.
    RETURN NULL;
END;
$func$;
"""

IMMUTABILITY_TRIGGERS = (
    ("trg_audit_logs_immutable", "audit_logs", "BEFORE UPDATE OR DELETE"),
    ("trg_inventory_transactions_immutable", "inventory_transactions", "BEFORE UPDATE OR DELETE"),
    ("trg_login_attempts_no_update", "login_attempts", "BEFORE UPDATE"),
)


def upgrade() -> None:
    # --- extensions, ahead of anything that depends on them ------------------
    for extension in EXTENSIONS:
        op.execute(f'CREATE EXTENSION IF NOT EXISTS "{extension}"')

    # --- tables, in foreign-key dependency order -----------------------------
    op.create_table(
        "companies",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("code", sa.String(length=32), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("legal_name", sa.String(length=255), nullable=True),
        sa.Column("gstin", sa.String(length=15), nullable=True),
        sa.Column("pan", sa.String(length=10), nullable=True),
        sa.Column("cin", sa.String(length=21), nullable=True),
        sa.Column("email", sa.String(length=255), nullable=True),
        sa.Column("phone", sa.String(length=32), nullable=True),
        sa.Column("website", sa.String(length=255), nullable=True),
        sa.Column("address_line1", sa.String(length=255), nullable=True),
        sa.Column("address_line2", sa.String(length=255), nullable=True),
        sa.Column("city", sa.String(length=120), nullable=True),
        sa.Column("state", sa.String(length=120), nullable=True),
        sa.Column("state_code", sa.String(length=4), nullable=True),
        sa.Column("postal_code", sa.String(length=16), nullable=True),
        sa.Column("country", sa.String(length=80), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("financial_year_start_month", sa.Integer(), nullable=False),
        sa.Column("timezone", sa.String(length=64), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("logo_document_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_companies")),
        sa.UniqueConstraint("code", name=op.f("uq_companies_code")),
    )
    op.create_index("ix_companies_active", "companies", ["is_active"], unique=False)
    op.create_index(op.f("ix_companies_created_at"), "companies", ["created_at"], unique=False)
    op.create_index(op.f("ix_companies_deleted_at"), "companies", ["deleted_at"], unique=False)
    op.create_table(
        "branches",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("company_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("code", sa.String(length=32), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("gstin", sa.String(length=15), nullable=True),
        sa.Column("address_line1", sa.String(length=255), nullable=True),
        sa.Column("city", sa.String(length=120), nullable=True),
        sa.Column("state", sa.String(length=120), nullable=True),
        sa.Column("state_code", sa.String(length=4), nullable=True),
        sa.Column("postal_code", sa.String(length=16), nullable=True),
        sa.Column("phone", sa.String(length=32), nullable=True),
        sa.Column("email", sa.String(length=255), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("is_head_office", sa.Boolean(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("updated_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["company_id"],
            ["companies.id"],
            name=op.f("fk_branches_company_id_companies"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_branches")),
        sa.UniqueConstraint("company_id", "code", name="uq_branches_company_id_code"),
    )
    op.create_index(
        "ix_branches_company_active", "branches", ["company_id", "is_active"], unique=False
    )
    op.create_index(op.f("ix_branches_company_id"), "branches", ["company_id"], unique=False)
    op.create_index(op.f("ix_branches_created_at"), "branches", ["created_at"], unique=False)
    op.create_index(op.f("ix_branches_deleted_at"), "branches", ["deleted_at"], unique=False)
    op.create_table(
        "plants",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("company_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("branch_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("code", sa.String(length=32), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("address_line1", sa.String(length=255), nullable=True),
        sa.Column("city", sa.String(length=120), nullable=True),
        sa.Column("state", sa.String(length=120), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("updated_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["branch_id"],
            ["branches.id"],
            name=op.f("fk_plants_branch_id_branches"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["company_id"],
            ["companies.id"],
            name=op.f("fk_plants_company_id_companies"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_plants")),
        sa.UniqueConstraint("company_id", "code", name="uq_plants_company_id_code"),
    )
    op.create_index(op.f("ix_plants_branch_id"), "plants", ["branch_id"], unique=False)
    op.create_index("ix_plants_company_active", "plants", ["company_id", "is_active"], unique=False)
    op.create_index(op.f("ix_plants_company_id"), "plants", ["company_id"], unique=False)
    op.create_index(op.f("ix_plants_created_at"), "plants", ["created_at"], unique=False)
    op.create_index(op.f("ix_plants_deleted_at"), "plants", ["deleted_at"], unique=False)
    op.create_table(
        "departments",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("company_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("plant_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("parent_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("code", sa.String(length=32), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("head_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("updated_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["company_id"],
            ["companies.id"],
            name=op.f("fk_departments_company_id_companies"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["parent_id"],
            ["departments.id"],
            name=op.f("fk_departments_parent_id_departments"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["plant_id"],
            ["plants.id"],
            name=op.f("fk_departments_plant_id_plants"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_departments")),
        sa.UniqueConstraint("company_id", "code", name="uq_departments_company_id_code"),
    )
    op.create_index(op.f("ix_departments_company_id"), "departments", ["company_id"], unique=False)
    op.create_index(op.f("ix_departments_created_at"), "departments", ["created_at"], unique=False)
    op.create_index(op.f("ix_departments_deleted_at"), "departments", ["deleted_at"], unique=False)
    op.create_index(op.f("ix_departments_parent_id"), "departments", ["parent_id"], unique=False)
    op.create_index(op.f("ix_departments_plant_id"), "departments", ["plant_id"], unique=False)
    op.create_table(
        "vendors",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("company_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("code", sa.String(length=32), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("legal_name", sa.String(length=255), nullable=True),
        sa.Column(
            "vendor_types",
            postgresql.ARRAY(sa.String(length=32)),
            server_default=sa.text("'{}'::varchar[]"),
            nullable=False,
        ),
        sa.Column("gstin", sa.String(length=15), nullable=True),
        sa.Column("pan", sa.String(length=10), nullable=True),
        sa.Column("msme_number", sa.String(length=32), nullable=True),
        sa.Column("payment_terms_days", sa.Integer(), server_default="0", nullable=False),
        sa.Column("credit_limit", sa.Numeric(precision=18, scale=4), nullable=True),
        sa.Column("lead_time_days", sa.Integer(), server_default="0", nullable=False),
        sa.Column("currency", sa.String(length=3), server_default="INR", nullable=False),
        sa.Column("rating", sa.Numeric(precision=3, scale=2), nullable=True),
        sa.Column("rating_updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("rating_sample_size", sa.Integer(), server_default="0", nullable=False),
        sa.Column("is_approved", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("approved_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("bank_name", sa.String(length=120), nullable=True),
        sa.Column("bank_branch", sa.String(length=120), nullable=True),
        sa.Column("bank_account_name", sa.String(length=255), nullable=True),
        sa.Column("bank_account_number", sa.String(length=34), nullable=True),
        sa.Column("bank_ifsc", sa.String(length=11), nullable=True),
        sa.Column("bank_swift", sa.String(length=11), nullable=True),
        sa.Column("address_line1", sa.String(length=255), nullable=True),
        sa.Column("address_line2", sa.String(length=255), nullable=True),
        sa.Column("city", sa.String(length=120), nullable=True),
        sa.Column("state", sa.String(length=120), nullable=True),
        sa.Column("state_code", sa.String(length=4), nullable=True),
        sa.Column("postal_code", sa.String(length=16), nullable=True),
        sa.Column("country", sa.String(length=80), server_default="India", nullable=False),
        sa.Column("contact_person", sa.String(length=160), nullable=True),
        sa.Column("email", sa.String(length=255), nullable=True),
        sa.Column("phone", sa.String(length=32), nullable=True),
        sa.Column("mobile", sa.String(length=32), nullable=True),
        sa.Column("website", sa.String(length=255), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("updated_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.CheckConstraint(
            "vendor_types <@ ARRAY['RAW_MATERIAL_SUPPLIER','COMPONENT_SUPPLIER','PACKING_MATERIAL_SUPPLIER','JOB_WORK','TRANSPORT','SERVICE']::varchar[]",
            name=op.f("ck_vendors_vendor_types_known"),
        ),
        sa.CheckConstraint(
            "cardinality(vendor_types) >= 1", name=op.f("ck_vendors_vendor_types_not_empty")
        ),
        sa.CheckConstraint(
            "credit_limit IS NULL OR credit_limit >= 0",
            name=op.f("ck_vendors_credit_limit_positive"),
        ),
        sa.CheckConstraint(
            "is_approved = false OR approved_at IS NOT NULL",
            name=op.f("ck_vendors_approval_has_timestamp"),
        ),
        sa.CheckConstraint("lead_time_days >= 0", name=op.f("ck_vendors_lead_time_non_negative")),
        sa.CheckConstraint(
            "payment_terms_days >= 0", name=op.f("ck_vendors_payment_terms_non_negative")
        ),
        sa.CheckConstraint(
            "rating IS NULL OR (rating >= 0 AND rating <= 5)",
            name=op.f("ck_vendors_rating_in_range"),
        ),
        sa.CheckConstraint(
            "rating_sample_size >= 0", name=op.f("ck_vendors_rating_sample_non_negative")
        ),
        sa.ForeignKeyConstraint(
            ["company_id"],
            ["companies.id"],
            name=op.f("fk_vendors_company_id_companies"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_vendors")),
        sa.UniqueConstraint("company_id", "code", name="uq_vendors_company_id_code"),
    )
    op.create_index(
        "ix_vendors_company_active", "vendors", ["company_id", "is_active"], unique=False
    )
    op.create_index(
        "ix_vendors_company_approved",
        "vendors",
        ["company_id", "is_approved", "is_active"],
        unique=False,
    )
    op.create_index(op.f("ix_vendors_company_id"), "vendors", ["company_id"], unique=False)
    op.create_index(op.f("ix_vendors_created_at"), "vendors", ["created_at"], unique=False)
    op.create_index(op.f("ix_vendors_deleted_at"), "vendors", ["deleted_at"], unique=False)
    op.create_index(
        "ix_vendors_types", "vendors", ["vendor_types"], unique=False, postgresql_using="gin"
    )
    op.create_index(
        "uq_vendors_company_gstin",
        "vendors",
        ["company_id", "gstin"],
        unique=True,
        postgresql_where=sa.text("gstin IS NOT NULL AND deleted_at IS NULL"),
    )
    op.create_table(
        "users",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("company_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("email", sa.String(length=255), nullable=False),
        sa.Column("full_name", sa.String(length=255), nullable=False),
        sa.Column("phone", sa.String(length=32), nullable=True),
        sa.Column("employee_code", sa.String(length=32), nullable=True),
        sa.Column("password_hash", sa.String(length=255), nullable=False),
        sa.Column("password_changed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("must_change_password", sa.Boolean(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("is_superuser", sa.Boolean(), nullable=False),
        sa.Column("mfa_enabled", sa.Boolean(), nullable=False),
        sa.Column("mfa_secret", sa.String(length=64), nullable=True),
        sa.Column("failed_login_attempts", sa.Integer(), nullable=False),
        sa.Column("locked_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_login_ip", postgresql.INET(), nullable=True),
        sa.Column("branch_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("plant_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("department_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("vendor_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "allowed_category_ids", postgresql.ARRAY(postgresql.UUID(as_uuid=True)), nullable=True
        ),
        sa.Column(
            "allowed_warehouse_ids", postgresql.ARRAY(postgresql.UUID(as_uuid=True)), nullable=True
        ),
        sa.Column("extra_permissions", postgresql.ARRAY(sa.String()), nullable=True),
        sa.Column("denied_permissions", postgresql.ARRAY(sa.String()), nullable=True),
        sa.Column("preferences", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("updated_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["branch_id"],
            ["branches.id"],
            name=op.f("fk_users_branch_id_branches"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["company_id"],
            ["companies.id"],
            name=op.f("fk_users_company_id_companies"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_id"],
            ["users.id"],
            name=op.f("fk_users_created_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["department_id"],
            ["departments.id"],
            name=op.f("fk_users_department_id_departments"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["plant_id"], ["plants.id"], name=op.f("fk_users_plant_id_plants"), ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["updated_by_id"],
            ["users.id"],
            name=op.f("fk_users_updated_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["vendor_id"],
            ["vendors.id"],
            name=op.f("fk_users_vendor_id_vendors"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_users")),
        sa.UniqueConstraint("company_id", "email", name="uq_users_company_id_email"),
    )
    op.create_index(op.f("ix_users_branch_id"), "users", ["branch_id"], unique=False)
    op.create_index("ix_users_company_active", "users", ["company_id", "is_active"], unique=False)
    op.create_index(op.f("ix_users_company_id"), "users", ["company_id"], unique=False)
    op.create_index(op.f("ix_users_created_at"), "users", ["created_at"], unique=False)
    op.create_index(op.f("ix_users_deleted_at"), "users", ["deleted_at"], unique=False)
    op.create_index(op.f("ix_users_department_id"), "users", ["department_id"], unique=False)
    op.create_index("ix_users_email_lookup", "users", ["email"], unique=False)
    op.create_index(op.f("ix_users_plant_id"), "users", ["plant_id"], unique=False)
    op.create_index("ix_users_vendor", "users", ["company_id", "vendor_id"], unique=False)
    op.create_index(op.f("ix_users_vendor_id"), "users", ["vendor_id"], unique=False)
    op.create_table(
        "approval_requests",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("company_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("entity_type", sa.String(length=64), nullable=False),
        sa.Column("entity_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("entity_code", sa.String(length=64), nullable=True),
        sa.Column("requested_action", sa.String(length=64), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "status", sa.String(length=24), server_default=sa.text("'PENDING'"), nullable=False
        ),
        sa.Column("decided_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("decision_note", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("created_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["company_id"],
            ["companies.id"],
            name=op.f("fk_approval_requests_company_id_companies"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_id"],
            ["users.id"],
            name=op.f("fk_approval_requests_created_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["decided_by_id"],
            ["users.id"],
            name=op.f("fk_approval_requests_decided_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_approval_requests")),
    )
    op.create_index(
        op.f("ix_approval_requests_company_id"), "approval_requests", ["company_id"], unique=False
    )
    op.create_index(
        "ix_approval_requests_entity",
        "approval_requests",
        ["company_id", "entity_type", "entity_id"],
        unique=False,
    )
    op.create_index(
        "ix_approval_requests_pending",
        "approval_requests",
        ["company_id", "status", "created_at"],
        unique=False,
    )
    op.create_table(
        "audit_logs",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("company_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("action", sa.String(length=48), nullable=False),
        sa.Column("entity_type", sa.String(length=64), nullable=False),
        sa.Column("entity_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("entity_code", sa.String(length=64), nullable=True),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("old_values", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("new_values", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("changed_fields", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("context", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("request_id", sa.String(length=64), nullable=True),
        sa.Column("ip_address", postgresql.INET(), nullable=True),
        sa.Column("user_agent", sa.String(length=512), nullable=True),
        sa.Column("actor_label", sa.String(length=255), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("created_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["company_id"],
            ["companies.id"],
            name=op.f("fk_audit_logs_company_id_companies"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_id"],
            ["users.id"],
            name=op.f("fk_audit_logs_created_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_audit_logs")),
        comment="Append-only. UPDATE and DELETE are blocked by a database trigger.",
    )
    op.create_index(
        "ix_audit_logs_action", "audit_logs", ["company_id", "action", "created_at"], unique=False
    )
    op.create_index(
        "ix_audit_logs_actor",
        "audit_logs",
        ["company_id", "created_by_id", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_audit_logs_company_created",
        "audit_logs",
        ["company_id", "created_at", "id"],
        unique=False,
    )
    op.create_index(op.f("ix_audit_logs_company_id"), "audit_logs", ["company_id"], unique=False)
    op.create_index(
        "ix_audit_logs_entity",
        "audit_logs",
        ["company_id", "entity_type", "entity_id", "created_at"],
        unique=False,
    )
    op.create_index("ix_audit_logs_request", "audit_logs", ["request_id"], unique=False)
    op.create_table(
        "customers",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("company_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("code", sa.String(length=32), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("legal_name", sa.String(length=255), nullable=True),
        sa.Column("customer_type", sa.String(length=16), nullable=False),
        sa.Column("gstin", sa.String(length=15), nullable=True),
        sa.Column("pan", sa.String(length=10), nullable=True),
        sa.Column("price_list_type", sa.String(length=16), server_default="SALE", nullable=False),
        sa.Column("credit_limit", sa.Numeric(precision=18, scale=4), nullable=True),
        sa.Column("credit_days", sa.Integer(), server_default="0", nullable=False),
        sa.Column(
            "outstanding_amount",
            sa.Numeric(precision=18, scale=4),
            server_default="0",
            nullable=False,
        ),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("is_blocked", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("block_reason", sa.Text(), nullable=True),
        sa.Column("blocked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("blocked_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("assigned_sales_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("default_transporter_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("updated_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.CheckConstraint(
            "customer_type = ANY(ARRAY['CUSTOMER','DEALER','DISTRIBUTOR','OEM']::varchar[])",
            name=op.f("ck_customers_customer_type_known"),
        ),
        sa.CheckConstraint(
            "price_list_type = ANY(ARRAY['SALE','DEALER','DISTRIBUTOR']::varchar[])",
            name=op.f("ck_customers_price_list_type_known"),
        ),
        sa.CheckConstraint("credit_days >= 0", name=op.f("ck_customers_credit_days_non_negative")),
        sa.CheckConstraint(
            "credit_limit IS NULL OR credit_limit >= 0",
            name=op.f("ck_customers_credit_limit_non_negative"),
        ),
        sa.CheckConstraint(
            "is_blocked = false OR block_reason IS NOT NULL",
            name=op.f("ck_customers_block_has_reason"),
        ),
        sa.CheckConstraint(
            "is_blocked = false OR blocked_at IS NOT NULL",
            name=op.f("ck_customers_block_has_timestamp"),
        ),
        sa.ForeignKeyConstraint(
            ["assigned_sales_user_id"],
            ["users.id"],
            name=op.f("fk_customers_assigned_sales_user_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["blocked_by_id"],
            ["users.id"],
            name=op.f("fk_customers_blocked_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["company_id"],
            ["companies.id"],
            name=op.f("fk_customers_company_id_companies"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_id"],
            ["users.id"],
            name=op.f("fk_customers_created_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["updated_by_id"],
            ["users.id"],
            name=op.f("fk_customers_updated_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_customers")),
        sa.UniqueConstraint("company_id", "code", name="uq_customers_company_id_code"),
    )
    op.create_index(
        "ix_customers_blocked",
        "customers",
        ["company_id"],
        unique=False,
        postgresql_where=sa.text("is_blocked AND deleted_at IS NULL"),
    )
    op.create_index(
        "ix_customers_company_active", "customers", ["company_id", "is_active"], unique=False
    )
    op.create_index(op.f("ix_customers_company_id"), "customers", ["company_id"], unique=False)
    op.create_index(
        "ix_customers_company_type",
        "customers",
        ["company_id", "customer_type", "is_active"],
        unique=False,
    )
    op.create_index(op.f("ix_customers_created_at"), "customers", ["created_at"], unique=False)
    op.create_index(op.f("ix_customers_deleted_at"), "customers", ["deleted_at"], unique=False)
    op.create_index(
        "ix_customers_sales_user",
        "customers",
        ["company_id", "assigned_sales_user_id"],
        unique=False,
        postgresql_where=sa.text("assigned_sales_user_id IS NOT NULL AND deleted_at IS NULL"),
    )
    op.create_index(
        "uq_customers_company_gstin",
        "customers",
        ["company_id", "gstin"],
        unique=True,
        postgresql_where=sa.text("gstin IS NOT NULL AND deleted_at IS NULL"),
    )
    op.create_table(
        "document_sequences",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("company_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("doc_type", sa.String(length=32), nullable=False),
        sa.Column("period_key", sa.String(length=16), server_default=sa.text("''"), nullable=False),
        sa.Column("prefix", sa.String(length=16), nullable=False),
        sa.Column("last_number", sa.Integer(), nullable=False),
        sa.Column("padding", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("created_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("updated_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["company_id"],
            ["companies.id"],
            name=op.f("fk_document_sequences_company_id_companies"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_id"],
            ["users.id"],
            name=op.f("fk_document_sequences_created_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["updated_by_id"],
            ["users.id"],
            name=op.f("fk_document_sequences_updated_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_document_sequences")),
        sa.UniqueConstraint(
            "company_id",
            "doc_type",
            "period_key",
            name="uq_document_sequences_company_id_doc_type_period_key",
        ),
    )
    op.create_index(
        op.f("ix_document_sequences_company_id"), "document_sequences", ["company_id"], unique=False
    )
    op.create_index(
        op.f("ix_document_sequences_created_at"), "document_sequences", ["created_at"], unique=False
    )
    op.create_table(
        "documents",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("company_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("file_name", sa.String(length=255), nullable=False),
        sa.Column("storage_key", sa.String(length=512), nullable=False),
        sa.Column("mime_type", sa.String(length=128), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("checksum_sha256", sa.String(length=64), nullable=False),
        sa.Column("category", sa.String(length=16), nullable=False),
        sa.Column("entity_type", sa.String(length=48), nullable=True),
        sa.Column("entity_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("uploaded_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("is_public", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("created_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("updated_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.CheckConstraint(
            "checksum_sha256 ~ '^[0-9a-f]{64}$'", name=op.f("ck_documents_checksum_is_sha256_hex")
        ),
        sa.CheckConstraint(
            "storage_key <> '' AND storage_key NOT LIKE '/%' AND position('..' in storage_key) = 0 AND position(chr(92) in storage_key) = 0",
            name=op.f("ck_documents_storage_key_is_relative"),
        ),
        sa.CheckConstraint(
            "(entity_type IS NULL) = (entity_id IS NULL)",
            name=op.f("ck_documents_entity_reference_complete"),
        ),
        sa.CheckConstraint("size_bytes > 0", name=op.f("ck_documents_size_positive")),
        sa.ForeignKeyConstraint(
            ["company_id"],
            ["companies.id"],
            name=op.f("fk_documents_company_id_companies"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_id"],
            ["users.id"],
            name=op.f("fk_documents_created_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["updated_by_id"],
            ["users.id"],
            name=op.f("fk_documents_updated_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["uploaded_by_id"],
            ["users.id"],
            name=op.f("fk_documents_uploaded_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_documents")),
        sa.UniqueConstraint(
            "company_id", "storage_key", name="uq_documents_company_id_storage_key"
        ),
    )
    op.create_index(
        "ix_documents_company_category",
        "documents",
        ["company_id", "category", sa.literal_column("created_at DESC")],
        unique=False,
    )
    op.create_index(
        "ix_documents_company_entity",
        "documents",
        ["company_id", "entity_type", "entity_id", sa.literal_column("created_at DESC")],
        unique=False,
    )
    op.create_index(op.f("ix_documents_company_id"), "documents", ["company_id"], unique=False)
    op.create_index(
        "ix_documents_company_uploaded_by",
        "documents",
        ["company_id", "uploaded_by_id"],
        unique=False,
    )
    op.create_index(op.f("ix_documents_created_at"), "documents", ["created_at"], unique=False)
    op.create_index(
        "ix_documents_expiring",
        "documents",
        ["expires_at"],
        unique=False,
        postgresql_where=sa.text("expires_at IS NOT NULL"),
    )
    op.create_table(
        "financial_years",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("company_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("code", sa.String(length=16), nullable=False),
        sa.Column("start_date", sa.Date(), nullable=False),
        sa.Column("end_date", sa.Date(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("is_closed", sa.Boolean(), nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("updated_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["company_id"],
            ["companies.id"],
            name=op.f("fk_financial_years_company_id_companies"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_id"],
            ["users.id"],
            name=op.f("fk_financial_years_created_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["updated_by_id"],
            ["users.id"],
            name=op.f("fk_financial_years_updated_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_financial_years")),
        sa.UniqueConstraint("company_id", "code", name="uq_financial_years_company_id_code"),
    )
    op.create_index(
        op.f("ix_financial_years_company_id"), "financial_years", ["company_id"], unique=False
    )
    op.create_index(
        op.f("ix_financial_years_created_at"), "financial_years", ["created_at"], unique=False
    )
    op.create_index(
        op.f("ix_financial_years_deleted_at"), "financial_years", ["deleted_at"], unique=False
    )
    op.create_index(
        "ix_financial_years_range",
        "financial_years",
        ["company_id", "start_date", "end_date"],
        unique=False,
    )
    op.create_table(
        "item_categories",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("company_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("code", sa.String(length=32), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("parent_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("path", sa.String(length=512), nullable=False),
        sa.Column("depth", sa.Integer(), server_default="0", nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("updated_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.CheckConstraint(
            "depth >= 0 AND depth < 8", name=op.f("ck_item_categories_depth_bounded")
        ),
        sa.CheckConstraint(
            "parent_id IS NULL OR parent_id <> id", name=op.f("ck_item_categories_parent_not_self")
        ),
        sa.ForeignKeyConstraint(
            ["company_id"],
            ["companies.id"],
            name=op.f("fk_item_categories_company_id_companies"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_id"],
            ["users.id"],
            name=op.f("fk_item_categories_created_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["parent_id"],
            ["item_categories.id"],
            name=op.f("fk_item_categories_parent_id_item_categories"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["updated_by_id"],
            ["users.id"],
            name=op.f("fk_item_categories_updated_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_item_categories")),
        sa.UniqueConstraint("company_id", "code", name="uq_item_categories_company_id_code"),
    )
    op.create_index(
        op.f("ix_item_categories_company_id"), "item_categories", ["company_id"], unique=False
    )
    op.create_index(
        op.f("ix_item_categories_created_at"), "item_categories", ["created_at"], unique=False
    )
    op.create_index(
        op.f("ix_item_categories_deleted_at"), "item_categories", ["deleted_at"], unique=False
    )
    op.create_index(
        "ix_item_categories_parent", "item_categories", ["company_id", "parent_id"], unique=False
    )
    op.create_index(
        op.f("ix_item_categories_parent_id"), "item_categories", ["parent_id"], unique=False
    )
    op.create_index(
        "ix_item_categories_path",
        "item_categories",
        ["company_id", "path"],
        unique=False,
        postgresql_ops={"path": "varchar_pattern_ops"},
    )
    op.create_table(
        "ledger_accounts",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("company_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("code", sa.String(length=32), nullable=False),
        sa.Column("name", sa.String(length=160), nullable=False),
        sa.Column("account_type", sa.String(length=16), nullable=False),
        sa.Column("parent_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("is_system", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("system_code", sa.String(length=32), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("updated_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.CheckConstraint(
            "account_type IN ('ASSET', 'LIABILITY', 'INCOME', 'EXPENSE', 'EQUITY')",
            name=op.f("ck_ledger_accounts_account_type_known"),
        ),
        sa.CheckConstraint(
            "NOT is_system OR system_code IS NOT NULL",
            name=op.f("ck_ledger_accounts_system_accounts_carry_a_system_code"),
        ),
        sa.CheckConstraint(
            "parent_id IS NULL OR parent_id <> id",
            name=op.f("ck_ledger_accounts_parent_is_not_self"),
        ),
        sa.ForeignKeyConstraint(
            ["company_id"],
            ["companies.id"],
            name=op.f("fk_ledger_accounts_company_id_companies"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_id"],
            ["users.id"],
            name=op.f("fk_ledger_accounts_created_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["parent_id"],
            ["ledger_accounts.id"],
            name=op.f("fk_ledger_accounts_parent_id_ledger_accounts"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["updated_by_id"],
            ["users.id"],
            name=op.f("fk_ledger_accounts_updated_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_ledger_accounts")),
        sa.UniqueConstraint("company_id", "code", name="uq_ledger_accounts_company_id_code"),
    )
    op.create_index(
        op.f("ix_ledger_accounts_company_id"), "ledger_accounts", ["company_id"], unique=False
    )
    op.create_index(
        op.f("ix_ledger_accounts_created_at"), "ledger_accounts", ["created_at"], unique=False
    )
    op.create_index(
        op.f("ix_ledger_accounts_deleted_at"), "ledger_accounts", ["deleted_at"], unique=False
    )
    op.create_index(
        op.f("ix_ledger_accounts_parent_id"), "ledger_accounts", ["parent_id"], unique=False
    )
    op.create_index(
        "ix_ledger_accounts_type",
        "ledger_accounts",
        ["company_id", "account_type", "is_active"],
        unique=False,
    )
    op.create_index(
        "uq_ledger_accounts_system_code",
        "ledger_accounts",
        ["company_id", "system_code"],
        unique=True,
        postgresql_where=sa.text("system_code IS NOT NULL AND deleted_at IS NULL"),
    )
    op.create_table(
        "login_attempts",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("company_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("email", sa.String(length=255), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("succeeded", sa.Boolean(), nullable=False),
        sa.Column("failure_reason", sa.String(length=64), nullable=True),
        sa.Column("ip_address", postgresql.INET(), nullable=True),
        sa.Column("user_agent", sa.String(length=512), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("created_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["company_id"],
            ["companies.id"],
            name=op.f("fk_login_attempts_company_id_companies"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_id"],
            ["users.id"],
            name=op.f("fk_login_attempts_created_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_login_attempts_user_id_users"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_login_attempts")),
    )
    op.create_index(
        op.f("ix_login_attempts_company_id"), "login_attempts", ["company_id"], unique=False
    )
    op.create_index(
        "ix_login_attempts_email_time", "login_attempts", ["email", "created_at"], unique=False
    )
    op.create_index(
        "ix_login_attempts_ip_time", "login_attempts", ["ip_address", "created_at"], unique=False
    )
    op.create_table(
        "mrp_runs",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("company_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("code", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=24), server_default="QUEUED", nullable=False),
        sa.Column("mode", sa.String(length=16), server_default="SYNC", nullable=False),
        sa.Column("horizon_days", sa.Integer(), nullable=False),
        sa.Column("warehouse_ids", postgresql.ARRAY(postgresql.UUID(as_uuid=True)), nullable=True),
        sa.Column("celery_task_id", sa.String(length=64), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("demand_line_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("suggestion_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("parameters", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("result_summary", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("created_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("updated_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.CheckConstraint("mode IN ('SYNC', 'ASYNC')", name=op.f("ck_mrp_runs_mode_known")),
        sa.CheckConstraint(
            "status <> 'FAILED' OR error_message IS NOT NULL",
            name=op.f("ck_mrp_runs_failed_runs_carry_a_reason"),
        ),
        sa.CheckConstraint(
            "status IN ('QUEUED', 'RUNNING', 'COMPLETED', 'FAILED', 'CANCELLED')",
            name=op.f("ck_mrp_runs_status_known"),
        ),
        sa.CheckConstraint(
            "completed_at IS NULL OR started_at IS NOT NULL",
            name=op.f("ck_mrp_runs_completion_implies_start"),
        ),
        sa.CheckConstraint(
            "completed_at IS NULL OR started_at IS NULL OR completed_at >= started_at",
            name=op.f("ck_mrp_runs_completed_after_started"),
        ),
        sa.CheckConstraint(
            "demand_line_count >= 0", name=op.f("ck_mrp_runs_demand_line_count_non_negative")
        ),
        sa.CheckConstraint(
            "horizon_days BETWEEN 1 AND 1095", name=op.f("ck_mrp_runs_horizon_in_range")
        ),
        sa.CheckConstraint(
            "suggestion_count >= 0", name=op.f("ck_mrp_runs_suggestion_count_non_negative")
        ),
        sa.ForeignKeyConstraint(
            ["company_id"],
            ["companies.id"],
            name=op.f("fk_mrp_runs_company_id_companies"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_id"],
            ["users.id"],
            name=op.f("fk_mrp_runs_created_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["updated_by_id"],
            ["users.id"],
            name=op.f("fk_mrp_runs_updated_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_mrp_runs")),
        sa.UniqueConstraint("company_id", "code", name="uq_mrp_runs_company_id_code"),
    )
    op.create_index(
        "ix_mrp_runs_active",
        "mrp_runs",
        ["company_id", "created_at"],
        unique=False,
        postgresql_where=sa.text("status IN ('QUEUED', 'RUNNING')"),
    )
    op.create_index(
        "ix_mrp_runs_company_created", "mrp_runs", ["company_id", "created_at"], unique=False
    )
    op.create_index(op.f("ix_mrp_runs_company_id"), "mrp_runs", ["company_id"], unique=False)
    op.create_index(op.f("ix_mrp_runs_created_at"), "mrp_runs", ["created_at"], unique=False)
    op.create_table(
        "notification_preferences",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("company_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("notification_type", sa.String(length=48), nullable=False),
        sa.Column("in_app_enabled", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("email_enabled", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("created_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("updated_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["company_id"],
            ["companies.id"],
            name=op.f("fk_notification_preferences_company_id_companies"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_id"],
            ["users.id"],
            name=op.f("fk_notification_preferences_created_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["updated_by_id"],
            ["users.id"],
            name=op.f("fk_notification_preferences_updated_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_notification_preferences_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_notification_preferences")),
        sa.UniqueConstraint(
            "company_id", "user_id", "notification_type", name="uq_notification_preferences_key"
        ),
    )
    op.create_index(
        op.f("ix_notification_preferences_company_id"),
        "notification_preferences",
        ["company_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_notification_preferences_created_at"),
        "notification_preferences",
        ["created_at"],
        unique=False,
    )
    op.create_table(
        "notifications",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("company_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("notification_type", sa.String(length=48), nullable=False),
        sa.Column("severity", sa.String(length=16), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("entity_type", sa.String(length=48), nullable=True),
        sa.Column("entity_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("action_url", sa.String(length=512), nullable=True),
        sa.Column("is_read", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("read_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "channels",
            postgresql.ARRAY(sa.String(length=16)),
            server_default=sa.text("'{IN_APP}'::varchar[]"),
            nullable=False,
        ),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("group_key", sa.String(length=120), nullable=True),
        sa.Column("occurrence_count", sa.Integer(), server_default="1", nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("created_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("updated_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.CheckConstraint(
            "action_url IS NULL OR (action_url LIKE '/%' AND action_url NOT LIKE '//%')",
            name=op.f("ck_notifications_action_url_is_relative"),
        ),
        sa.CheckConstraint(
            "(entity_type IS NULL) = (entity_id IS NULL)",
            name=op.f("ck_notifications_entity_reference_complete"),
        ),
        sa.CheckConstraint(
            "(read_at IS NOT NULL) = is_read", name=op.f("ck_notifications_read_state_consistent")
        ),
        sa.CheckConstraint(
            "cardinality(channels) > 0", name=op.f("ck_notifications_channels_not_empty")
        ),
        sa.CheckConstraint(
            "length(btrim(title)) > 0", name=op.f("ck_notifications_title_not_blank")
        ),
        sa.CheckConstraint(
            "occurrence_count >= 1", name=op.f("ck_notifications_occurrence_count_positive")
        ),
        sa.ForeignKeyConstraint(
            ["company_id"],
            ["companies.id"],
            name=op.f("fk_notifications_company_id_companies"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_id"],
            ["users.id"],
            name=op.f("fk_notifications_created_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["updated_by_id"],
            ["users.id"],
            name=op.f("fk_notifications_updated_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_notifications_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_notifications")),
    )
    op.create_index(
        op.f("ix_notifications_company_id"), "notifications", ["company_id"], unique=False
    )
    op.create_index(
        op.f("ix_notifications_created_at"), "notifications", ["created_at"], unique=False
    )
    op.create_index(
        "ix_notifications_entity",
        "notifications",
        ["company_id", "entity_type", "entity_id"],
        unique=False,
        postgresql_where=sa.text("entity_id IS NOT NULL"),
    )
    op.create_index(
        "ix_notifications_feed",
        "notifications",
        [
            "company_id",
            "user_id",
            "is_read",
            sa.literal_column("created_at DESC"),
            sa.literal_column("id DESC"),
        ],
        unique=False,
    )
    op.create_index(
        "ix_notifications_unread",
        "notifications",
        ["company_id", "user_id"],
        unique=False,
        postgresql_where=sa.text("is_read = false"),
    )
    op.create_index(
        "uq_notifications_open_group",
        "notifications",
        ["company_id", "user_id", "notification_type", "group_key"],
        unique=True,
        postgresql_where=sa.text("group_key IS NOT NULL AND is_read = false"),
    )
    op.create_table(
        "password_reset_tokens",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("company_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("token_hash", sa.String(length=128), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("requested_ip", postgresql.INET(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("created_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("updated_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["company_id"],
            ["companies.id"],
            name=op.f("fk_password_reset_tokens_company_id_companies"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_id"],
            ["users.id"],
            name=op.f("fk_password_reset_tokens_created_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["updated_by_id"],
            ["users.id"],
            name=op.f("fk_password_reset_tokens_updated_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_password_reset_tokens_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_password_reset_tokens")),
        sa.UniqueConstraint("token_hash", name=op.f("uq_password_reset_tokens_token_hash")),
    )
    op.create_index(
        "ix_password_reset_active",
        "password_reset_tokens",
        ["user_id", "used_at", "expires_at"],
        unique=False,
    )
    op.create_index(
        op.f("ix_password_reset_tokens_company_id"),
        "password_reset_tokens",
        ["company_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_password_reset_tokens_created_at"),
        "password_reset_tokens",
        ["created_at"],
        unique=False,
    )
    op.create_index(
        op.f("ix_password_reset_tokens_user_id"), "password_reset_tokens", ["user_id"], unique=False
    )
    op.create_table(
        "payments",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("company_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("code", sa.String(length=64), nullable=False),
        sa.Column("payment_type", sa.String(length=16), nullable=False),
        sa.Column("party_type", sa.String(length=16), nullable=False),
        sa.Column("party_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("payment_date", sa.Date(), nullable=False),
        sa.Column("amount", sa.Numeric(precision=18, scale=4), nullable=False),
        sa.Column(
            "allocated_amount",
            sa.Numeric(precision=18, scale=4),
            server_default="0",
            nullable=False,
        ),
        sa.Column("currency", sa.String(length=3), server_default="INR", nullable=False),
        sa.Column("payment_method", sa.String(length=24), nullable=False),
        sa.Column("reference_number", sa.String(length=64), nullable=True),
        sa.Column("bank_name", sa.String(length=120), nullable=True),
        sa.Column("instrument_date", sa.Date(), nullable=True),
        sa.Column("status", sa.String(length=24), server_default="DRAFT", nullable=False),
        sa.Column("approved_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancellation_reason", sa.Text(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("created_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("updated_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.CheckConstraint(
            "party_type IN ('CUSTOMER', 'VENDOR')",
            name=op.f("ck_payments_party_type_is_a_real_party"),
        ),
        sa.CheckConstraint(
            "payment_method IN ('CASH', 'BANK_TRANSFER', 'CHEQUE', 'UPI', 'CARD', 'ADJUSTMENT')",
            name=op.f("ck_payments_payment_method_known"),
        ),
        sa.CheckConstraint(
            "payment_type IN ('PAYMENT', 'RECEIPT')", name=op.f("ck_payments_payment_type_known")
        ),
        sa.CheckConstraint(
            "status <> 'CANCELLED' OR cancellation_reason IS NOT NULL",
            name=op.f("ck_payments_cancelled_payments_carry_a_reason"),
        ),
        sa.CheckConstraint(
            "status IN ('DRAFT', 'APPROVED', 'PARTIALLY_ALLOCATED', 'ALLOCATED', 'CANCELLED')",
            name=op.f("ck_payments_status_known"),
        ),
        sa.CheckConstraint(
            "(approved_by_id IS NULL) = (approved_at IS NULL)",
            name=op.f("ck_payments_approval_evidence_complete"),
        ),
        sa.CheckConstraint(
            "allocated_amount <= amount", name=op.f("ck_payments_allocated_within_amount")
        ),
        sa.CheckConstraint(
            "allocated_amount >= 0", name=op.f("ck_payments_allocated_non_negative")
        ),
        sa.CheckConstraint("amount > 0", name=op.f("ck_payments_amount_positive")),
        sa.CheckConstraint("length(currency) = 3", name=op.f("ck_payments_currency_is_iso_code")),
        sa.ForeignKeyConstraint(
            ["approved_by_id"],
            ["users.id"],
            name=op.f("fk_payments_approved_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["company_id"],
            ["companies.id"],
            name=op.f("fk_payments_company_id_companies"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_id"],
            ["users.id"],
            name=op.f("fk_payments_created_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["updated_by_id"],
            ["users.id"],
            name=op.f("fk_payments_updated_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_payments")),
        sa.UniqueConstraint("company_id", "code", name="uq_payments_company_id_code"),
    )
    op.create_index(op.f("ix_payments_company_id"), "payments", ["company_id"], unique=False)
    op.create_index(op.f("ix_payments_created_at"), "payments", ["created_at"], unique=False)
    op.create_index(
        "ix_payments_party",
        "payments",
        ["company_id", "party_type", "party_id", "payment_date"],
        unique=False,
    )
    op.create_index(
        "ix_payments_reference_number", "payments", ["company_id", "reference_number"], unique=False
    )
    op.create_index(
        "ix_payments_status_date",
        "payments",
        ["company_id", "status", "payment_date"],
        unique=False,
    )
    op.create_table(
        "roles",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("company_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("code", sa.String(length=48), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column(
            "permissions",
            postgresql.ARRAY(sa.String()),
            server_default=sa.text("'{}'::varchar[]"),
            nullable=False,
        ),
        sa.Column("is_system", sa.Boolean(), nullable=False),
        sa.Column("is_vendor_role", sa.Boolean(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("updated_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["company_id"],
            ["companies.id"],
            name=op.f("fk_roles_company_id_companies"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_id"],
            ["users.id"],
            name=op.f("fk_roles_created_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["updated_by_id"],
            ["users.id"],
            name=op.f("fk_roles_updated_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_roles")),
        sa.UniqueConstraint("company_id", "code", name="uq_roles_company_id_code"),
    )
    op.create_index("ix_roles_company_active", "roles", ["company_id", "is_active"], unique=False)
    op.create_index(op.f("ix_roles_company_id"), "roles", ["company_id"], unique=False)
    op.create_index(op.f("ix_roles_created_at"), "roles", ["created_at"], unique=False)
    op.create_index(op.f("ix_roles_deleted_at"), "roles", ["deleted_at"], unique=False)
    op.create_table(
        "transporters",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("company_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("code", sa.String(length=32), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("transporter_type", sa.String(length=16), nullable=False),
        sa.Column("gstin", sa.String(length=15), nullable=True),
        sa.Column("pan", sa.String(length=10), nullable=True),
        sa.Column("contact_person", sa.String(length=160), nullable=True),
        sa.Column("email", sa.String(length=255), nullable=True),
        sa.Column("phone", sa.String(length=32), nullable=True),
        sa.Column("mobile", sa.String(length=32), nullable=True),
        sa.Column("website", sa.String(length=255), nullable=True),
        sa.Column("address_line1", sa.String(length=255), nullable=True),
        sa.Column("city", sa.String(length=120), nullable=True),
        sa.Column("state", sa.String(length=120), nullable=True),
        sa.Column("state_code", sa.String(length=4), nullable=True),
        sa.Column("postal_code", sa.String(length=16), nullable=True),
        sa.Column("country", sa.String(length=80), server_default="India", nullable=False),
        sa.Column("tracking_url_template", sa.String(length=500), nullable=True),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("updated_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.CheckConstraint(
            "tracking_url_template IS NULL OR strpos(tracking_url_template, '{tracking_id}') > 0",
            name=op.f("ck_transporters_tracking_template_has_placeholder"),
        ),
        sa.CheckConstraint(
            "transporter_type = ANY(ARRAY['TRANSPORT','COURIER']::varchar[])",
            name=op.f("ck_transporters_transporter_type_known"),
        ),
        sa.ForeignKeyConstraint(
            ["company_id"],
            ["companies.id"],
            name=op.f("fk_transporters_company_id_companies"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_id"],
            ["users.id"],
            name=op.f("fk_transporters_created_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["updated_by_id"],
            ["users.id"],
            name=op.f("fk_transporters_updated_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_transporters")),
        sa.UniqueConstraint("company_id", "code", name="uq_transporters_company_id_code"),
    )
    op.create_index(
        "ix_transporters_company_active", "transporters", ["company_id", "is_active"], unique=False
    )
    op.create_index(
        op.f("ix_transporters_company_id"), "transporters", ["company_id"], unique=False
    )
    op.create_index(
        "ix_transporters_company_type",
        "transporters",
        ["company_id", "transporter_type"],
        unique=False,
    )
    op.create_index(
        op.f("ix_transporters_created_at"), "transporters", ["created_at"], unique=False
    )
    op.create_index(
        op.f("ix_transporters_deleted_at"), "transporters", ["deleted_at"], unique=False
    )
    op.create_table(
        "units_of_measure",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("company_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("code", sa.String(length=16), nullable=False),
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("uom_category", sa.String(length=16), nullable=False),
        sa.Column("is_base", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("decimal_places", sa.Integer(), server_default="4", nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("updated_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.CheckConstraint(
            "decimal_places >= 0 AND decimal_places <= 6",
            name=op.f("ck_units_of_measure_decimal_places_bounded"),
        ),
        sa.ForeignKeyConstraint(
            ["company_id"],
            ["companies.id"],
            name=op.f("fk_units_of_measure_company_id_companies"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_id"],
            ["users.id"],
            name=op.f("fk_units_of_measure_created_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["updated_by_id"],
            ["users.id"],
            name=op.f("fk_units_of_measure_updated_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_units_of_measure")),
        sa.UniqueConstraint("company_id", "code", name="uq_units_of_measure_company_id_code"),
    )
    op.create_index(
        op.f("ix_units_of_measure_company_id"), "units_of_measure", ["company_id"], unique=False
    )
    op.create_index(
        op.f("ix_units_of_measure_created_at"), "units_of_measure", ["created_at"], unique=False
    )
    op.create_index(
        op.f("ix_units_of_measure_deleted_at"), "units_of_measure", ["deleted_at"], unique=False
    )
    op.create_index(
        "uq_units_of_measure_one_base_per_category",
        "units_of_measure",
        ["company_id", "uom_category"],
        unique=True,
        postgresql_where=sa.text("is_base AND deleted_at IS NULL"),
    )
    op.create_table(
        "user_sessions",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("company_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("session_id", sa.String(length=64), nullable=False),
        sa.Column("refresh_token_hash", sa.String(length=128), nullable=False),
        sa.Column("refresh_jti", sa.String(length=64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_reason", sa.String(length=64), nullable=True),
        sa.Column("replaced_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("ip_address", postgresql.INET(), nullable=True),
        sa.Column("user_agent", sa.String(length=512), nullable=True),
        sa.Column("device_label", sa.String(length=120), nullable=True),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("created_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("updated_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["company_id"],
            ["companies.id"],
            name=op.f("fk_user_sessions_company_id_companies"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_id"],
            ["users.id"],
            name=op.f("fk_user_sessions_created_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["replaced_by_id"],
            ["user_sessions.id"],
            name=op.f("fk_user_sessions_replaced_by_id_user_sessions"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["updated_by_id"],
            ["users.id"],
            name=op.f("fk_user_sessions_updated_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_user_sessions_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_user_sessions")),
        sa.UniqueConstraint("session_id", name=op.f("uq_user_sessions_session_id")),
    )
    op.create_index(
        "ix_user_sessions_active",
        "user_sessions",
        ["user_id", "revoked_at", "expires_at"],
        unique=False,
    )
    op.create_index(
        op.f("ix_user_sessions_company_id"), "user_sessions", ["company_id"], unique=False
    )
    op.create_index(
        op.f("ix_user_sessions_created_at"), "user_sessions", ["created_at"], unique=False
    )
    op.create_index(
        op.f("ix_user_sessions_refresh_jti"), "user_sessions", ["refresh_jti"], unique=False
    )
    op.create_index(op.f("ix_user_sessions_user_id"), "user_sessions", ["user_id"], unique=False)
    op.create_table(
        "vendor_contacts",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("company_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("vendor_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.String(length=160), nullable=False),
        sa.Column("designation", sa.String(length=120), nullable=True),
        sa.Column("contact_role", sa.String(length=16), nullable=False),
        sa.Column("email", sa.String(length=255), nullable=True),
        sa.Column("phone", sa.String(length=32), nullable=True),
        sa.Column("mobile", sa.String(length=32), nullable=True),
        sa.Column("is_primary", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("updated_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.CheckConstraint(
            "contact_role = ANY(ARRAY['OWNER','MANAGER','DISPATCH','QC','ACCOUNTS']::varchar[])",
            name=op.f("ck_vendor_contacts_contact_role_known"),
        ),
        sa.CheckConstraint(
            "email IS NOT NULL OR phone IS NOT NULL OR mobile IS NOT NULL",
            name=op.f("ck_vendor_contacts_contact_is_reachable"),
        ),
        sa.ForeignKeyConstraint(
            ["company_id"],
            ["companies.id"],
            name=op.f("fk_vendor_contacts_company_id_companies"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_id"],
            ["users.id"],
            name=op.f("fk_vendor_contacts_created_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["updated_by_id"],
            ["users.id"],
            name=op.f("fk_vendor_contacts_updated_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["vendor_id"],
            ["vendors.id"],
            name=op.f("fk_vendor_contacts_vendor_id_vendors"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_vendor_contacts")),
    )
    op.create_index(
        op.f("ix_vendor_contacts_company_id"), "vendor_contacts", ["company_id"], unique=False
    )
    op.create_index(
        op.f("ix_vendor_contacts_created_at"), "vendor_contacts", ["created_at"], unique=False
    )
    op.create_index(
        op.f("ix_vendor_contacts_deleted_at"), "vendor_contacts", ["deleted_at"], unique=False
    )
    op.create_index(
        "ix_vendor_contacts_role",
        "vendor_contacts",
        ["company_id", "vendor_id", "contact_role"],
        unique=False,
    )
    op.create_index(
        "ix_vendor_contacts_vendor_active",
        "vendor_contacts",
        ["company_id", "vendor_id", "is_active"],
        unique=False,
    )
    op.create_index(
        op.f("ix_vendor_contacts_vendor_id"), "vendor_contacts", ["vendor_id"], unique=False
    )
    op.create_index(
        "uq_vendor_contacts_one_primary",
        "vendor_contacts",
        ["company_id", "vendor_id"],
        unique=True,
        postgresql_where=sa.text("is_primary AND deleted_at IS NULL"),
    )
    op.create_table(
        "warehouses",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("company_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("code", sa.String(length=32), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("warehouse_type", sa.String(length=24), nullable=False),
        sa.Column("branch_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("plant_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("parent_warehouse_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("manager_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("address_line1", sa.String(length=255), nullable=True),
        sa.Column("address_line2", sa.String(length=255), nullable=True),
        sa.Column("city", sa.String(length=120), nullable=True),
        sa.Column("state", sa.String(length=120), nullable=True),
        sa.Column("state_code", sa.String(length=4), nullable=True),
        sa.Column("postal_code", sa.String(length=16), nullable=True),
        sa.Column("country", sa.String(length=80), nullable=False),
        sa.Column("phone", sa.String(length=32), nullable=True),
        sa.Column("email", sa.String(length=255), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("is_default", sa.Boolean(), nullable=False),
        sa.Column(
            "allow_negative_stock", sa.Boolean(), server_default=sa.text("false"), nullable=False
        ),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("updated_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.CheckConstraint(
            "parent_warehouse_id IS NULL OR parent_warehouse_id <> id",
            name=op.f("ck_warehouses_parent_not_self"),
        ),
        sa.ForeignKeyConstraint(
            ["branch_id"],
            ["branches.id"],
            name=op.f("fk_warehouses_branch_id_branches"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["company_id"],
            ["companies.id"],
            name=op.f("fk_warehouses_company_id_companies"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_id"],
            ["users.id"],
            name=op.f("fk_warehouses_created_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["manager_user_id"],
            ["users.id"],
            name=op.f("fk_warehouses_manager_user_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["parent_warehouse_id"],
            ["warehouses.id"],
            name=op.f("fk_warehouses_parent_warehouse_id_warehouses"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["plant_id"],
            ["plants.id"],
            name=op.f("fk_warehouses_plant_id_plants"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["updated_by_id"],
            ["users.id"],
            name=op.f("fk_warehouses_updated_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_warehouses")),
        sa.UniqueConstraint("company_id", "code", name="uq_warehouses_company_id_code"),
    )
    op.create_index(op.f("ix_warehouses_branch_id"), "warehouses", ["branch_id"], unique=False)
    op.create_index(
        "ix_warehouses_company_active", "warehouses", ["company_id", "is_active"], unique=False
    )
    op.create_index(op.f("ix_warehouses_company_id"), "warehouses", ["company_id"], unique=False)
    op.create_index(
        "ix_warehouses_company_type", "warehouses", ["company_id", "warehouse_type"], unique=False
    )
    op.create_index(op.f("ix_warehouses_created_at"), "warehouses", ["created_at"], unique=False)
    op.create_index(op.f("ix_warehouses_deleted_at"), "warehouses", ["deleted_at"], unique=False)
    op.create_index(
        op.f("ix_warehouses_parent_warehouse_id"),
        "warehouses",
        ["parent_warehouse_id"],
        unique=False,
    )
    op.create_index(op.f("ix_warehouses_plant_id"), "warehouses", ["plant_id"], unique=False)
    op.create_index(
        "uq_warehouses_company_default",
        "warehouses",
        ["company_id"],
        unique=True,
        postgresql_where=sa.text("is_default AND deleted_at IS NULL"),
    )
    op.create_table(
        "work_centers",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("company_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("code", sa.String(length=32), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("plant_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("department_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "capacity_per_hour",
            sa.Numeric(precision=18, scale=4),
            server_default="0",
            nullable=False,
        ),
        sa.Column(
            "cost_per_hour", sa.Numeric(precision=18, scale=4), server_default="0", nullable=False
        ),
        sa.Column(
            "setup_cost", sa.Numeric(precision=18, scale=4), server_default="0", nullable=False
        ),
        sa.Column(
            "working_hours_per_day",
            sa.Numeric(precision=18, scale=4),
            server_default="8",
            nullable=False,
        ),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("updated_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.CheckConstraint(
            "capacity_per_hour >= 0", name=op.f("ck_work_centers_capacity_non_negative")
        ),
        sa.CheckConstraint(
            "cost_per_hour >= 0", name=op.f("ck_work_centers_cost_per_hour_non_negative")
        ),
        sa.CheckConstraint("setup_cost >= 0", name=op.f("ck_work_centers_setup_cost_non_negative")),
        sa.CheckConstraint(
            "working_hours_per_day > 0 AND working_hours_per_day <= 24",
            name=op.f("ck_work_centers_working_hours_per_day_bounded"),
        ),
        sa.ForeignKeyConstraint(
            ["company_id"],
            ["companies.id"],
            name=op.f("fk_work_centers_company_id_companies"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_id"],
            ["users.id"],
            name=op.f("fk_work_centers_created_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["department_id"],
            ["departments.id"],
            name=op.f("fk_work_centers_department_id_departments"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["plant_id"],
            ["plants.id"],
            name=op.f("fk_work_centers_plant_id_plants"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["updated_by_id"],
            ["users.id"],
            name=op.f("fk_work_centers_updated_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_work_centers")),
        sa.UniqueConstraint("company_id", "code", name="uq_work_centers_company_id_code"),
    )
    op.create_index(
        "ix_work_centers_company_active", "work_centers", ["company_id", "is_active"], unique=False
    )
    op.create_index(
        op.f("ix_work_centers_company_id"), "work_centers", ["company_id"], unique=False
    )
    op.create_index(
        op.f("ix_work_centers_created_at"), "work_centers", ["created_at"], unique=False
    )
    op.create_index(
        op.f("ix_work_centers_deleted_at"), "work_centers", ["deleted_at"], unique=False
    )
    op.create_index(
        op.f("ix_work_centers_department_id"), "work_centers", ["department_id"], unique=False
    )
    op.create_index(op.f("ix_work_centers_plant_id"), "work_centers", ["plant_id"], unique=False)
    op.create_table(
        "customer_addresses",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("company_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("customer_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("address_type", sa.String(length=16), nullable=False),
        sa.Column("label", sa.String(length=120), nullable=True),
        sa.Column("address_line1", sa.String(length=255), nullable=False),
        sa.Column("address_line2", sa.String(length=255), nullable=True),
        sa.Column("landmark", sa.String(length=255), nullable=True),
        sa.Column("city", sa.String(length=120), nullable=False),
        sa.Column("state", sa.String(length=120), nullable=True),
        sa.Column("state_code", sa.String(length=4), nullable=True),
        sa.Column("postal_code", sa.String(length=16), nullable=True),
        sa.Column("country", sa.String(length=80), server_default="India", nullable=False),
        sa.Column("gstin", sa.String(length=15), nullable=True),
        sa.Column("contact_person", sa.String(length=160), nullable=True),
        sa.Column("phone", sa.String(length=32), nullable=True),
        sa.Column("email", sa.String(length=255), nullable=True),
        sa.Column("is_default", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("updated_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.CheckConstraint(
            "address_type = ANY(ARRAY['BILLING','SHIPPING']::varchar[])",
            name=op.f("ck_customer_addresses_address_type_known"),
        ),
        sa.CheckConstraint(
            "country <> 'India' OR state_code IS NOT NULL",
            name=op.f("ck_customer_addresses_domestic_address_has_state_code"),
        ),
        sa.CheckConstraint(
            "state_code IS NULL OR state_code ~ '^[0-9]{2}$'",
            name=op.f("ck_customer_addresses_state_code_is_two_digits"),
        ),
        sa.ForeignKeyConstraint(
            ["company_id"],
            ["companies.id"],
            name=op.f("fk_customer_addresses_company_id_companies"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_id"],
            ["users.id"],
            name=op.f("fk_customer_addresses_created_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["customer_id"],
            ["customers.id"],
            name=op.f("fk_customer_addresses_customer_id_customers"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["updated_by_id"],
            ["users.id"],
            name=op.f("fk_customer_addresses_updated_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_customer_addresses")),
    )
    op.create_index(
        op.f("ix_customer_addresses_company_id"), "customer_addresses", ["company_id"], unique=False
    )
    op.create_index(
        op.f("ix_customer_addresses_created_at"), "customer_addresses", ["created_at"], unique=False
    )
    op.create_index(
        op.f("ix_customer_addresses_customer_id"),
        "customer_addresses",
        ["customer_id"],
        unique=False,
    )
    op.create_index(
        "ix_customer_addresses_customer_type",
        "customer_addresses",
        ["company_id", "customer_id", "address_type", "is_active"],
        unique=False,
    )
    op.create_index(
        op.f("ix_customer_addresses_deleted_at"), "customer_addresses", ["deleted_at"], unique=False
    )
    op.create_index(
        "uq_customer_addresses_one_default",
        "customer_addresses",
        ["company_id", "customer_id", "address_type"],
        unique=True,
        postgresql_where=sa.text("is_default AND deleted_at IS NULL"),
    )
    op.create_table(
        "customer_contacts",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("company_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("customer_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.String(length=160), nullable=False),
        sa.Column("designation", sa.String(length=120), nullable=True),
        sa.Column("email", sa.String(length=255), nullable=True),
        sa.Column("phone", sa.String(length=32), nullable=True),
        sa.Column("is_primary", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("updated_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.CheckConstraint(
            "email IS NOT NULL OR phone IS NOT NULL",
            name=op.f("ck_customer_contacts_contact_is_reachable"),
        ),
        sa.ForeignKeyConstraint(
            ["company_id"],
            ["companies.id"],
            name=op.f("fk_customer_contacts_company_id_companies"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_id"],
            ["users.id"],
            name=op.f("fk_customer_contacts_created_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["customer_id"],
            ["customers.id"],
            name=op.f("fk_customer_contacts_customer_id_customers"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["updated_by_id"],
            ["users.id"],
            name=op.f("fk_customer_contacts_updated_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_customer_contacts")),
    )
    op.create_index(
        op.f("ix_customer_contacts_company_id"), "customer_contacts", ["company_id"], unique=False
    )
    op.create_index(
        op.f("ix_customer_contacts_created_at"), "customer_contacts", ["created_at"], unique=False
    )
    op.create_index(
        "ix_customer_contacts_customer",
        "customer_contacts",
        ["company_id", "customer_id", "is_active"],
        unique=False,
    )
    op.create_index(
        op.f("ix_customer_contacts_customer_id"), "customer_contacts", ["customer_id"], unique=False
    )
    op.create_index(
        op.f("ix_customer_contacts_deleted_at"), "customer_contacts", ["deleted_at"], unique=False
    )
    op.create_index(
        "uq_customer_contacts_one_primary",
        "customer_contacts",
        ["company_id", "customer_id"],
        unique=True,
        postgresql_where=sa.text("is_primary AND deleted_at IS NULL"),
    )
    op.create_table(
        "items",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("company_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("code", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("item_group", sa.String(length=24), nullable=False),
        sa.Column("category_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("base_uom_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("purchase_uom_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("sale_uom_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("hsn_code", sa.String(length=16), nullable=True),
        sa.Column("gst_rate", sa.Numeric(precision=9, scale=4), server_default="0", nullable=False),
        sa.Column(
            "standard_cost", sa.Numeric(precision=18, scale=4), server_default="0", nullable=False
        ),
        sa.Column(
            "sale_price", sa.Numeric(precision=18, scale=6), server_default="0", nullable=False
        ),
        sa.Column(
            "dealer_price", sa.Numeric(precision=18, scale=6), server_default="0", nullable=False
        ),
        sa.Column("finish", sa.String(length=64), nullable=True),
        sa.Column("size", sa.String(length=64), nullable=True),
        sa.Column("colour", sa.String(length=64), nullable=True),
        sa.Column("can_purchase", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("can_manufacture", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("can_sell", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column(
            "can_be_bom_component", sa.Boolean(), server_default=sa.text("false"), nullable=False
        ),
        sa.Column(
            "can_be_assembly_component",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
        sa.Column("can_be_spare", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("can_job_work", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("can_return", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("can_scrap", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("production_type", sa.String(length=16), server_default="BUY", nullable=False),
        sa.Column("lead_time_days", sa.Integer(), server_default="0", nullable=False),
        sa.Column(
            "reorder_level", sa.Numeric(precision=18, scale=4), server_default="0", nullable=False
        ),
        sa.Column(
            "reorder_quantity",
            sa.Numeric(precision=18, scale=4),
            server_default="0",
            nullable=False,
        ),
        sa.Column(
            "min_order_quantity",
            sa.Numeric(precision=18, scale=4),
            server_default="0",
            nullable=False,
        ),
        sa.Column(
            "safety_stock", sa.Numeric(precision=18, scale=4), server_default="0", nullable=False
        ),
        sa.Column("shelf_life_days", sa.Integer(), nullable=True),
        sa.Column(
            "is_batch_tracked", sa.Boolean(), server_default=sa.text("false"), nullable=False
        ),
        sa.Column(
            "is_serial_tracked", sa.Boolean(), server_default=sa.text("false"), nullable=False
        ),
        sa.Column("default_warehouse_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "valuation_method",
            sa.String(length=24),
            server_default="WEIGHTED_AVERAGE",
            nullable=False,
        ),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("updated_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.CheckConstraint("dealer_price >= 0", name=op.f("ck_items_dealer_price_non_negative")),
        sa.CheckConstraint(
            "gst_rate >= 0 AND gst_rate <= 100", name=op.f("ck_items_gst_rate_percentage")
        ),
        sa.CheckConstraint("lead_time_days >= 0", name=op.f("ck_items_lead_time_non_negative")),
        sa.CheckConstraint(
            "min_order_quantity >= 0", name=op.f("ck_items_min_order_quantity_non_negative")
        ),
        sa.CheckConstraint("reorder_level >= 0", name=op.f("ck_items_reorder_level_non_negative")),
        sa.CheckConstraint(
            "reorder_quantity >= 0", name=op.f("ck_items_reorder_quantity_non_negative")
        ),
        sa.CheckConstraint("safety_stock >= 0", name=op.f("ck_items_safety_stock_non_negative")),
        sa.CheckConstraint("sale_price >= 0", name=op.f("ck_items_sale_price_non_negative")),
        sa.CheckConstraint(
            "shelf_life_days IS NULL OR is_batch_tracked",
            name=op.f("ck_items_shelf_life_requires_batch"),
        ),
        sa.CheckConstraint(
            "shelf_life_days IS NULL OR shelf_life_days > 0",
            name=op.f("ck_items_shelf_life_positive"),
        ),
        sa.CheckConstraint("standard_cost >= 0", name=op.f("ck_items_standard_cost_non_negative")),
        sa.ForeignKeyConstraint(
            ["base_uom_id"],
            ["units_of_measure.id"],
            name=op.f("fk_items_base_uom_id_units_of_measure"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["category_id"],
            ["item_categories.id"],
            name=op.f("fk_items_category_id_item_categories"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["company_id"],
            ["companies.id"],
            name=op.f("fk_items_company_id_companies"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_id"],
            ["users.id"],
            name=op.f("fk_items_created_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["default_warehouse_id"],
            ["warehouses.id"],
            name=op.f("fk_items_default_warehouse_id_warehouses"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["purchase_uom_id"],
            ["units_of_measure.id"],
            name=op.f("fk_items_purchase_uom_id_units_of_measure"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["sale_uom_id"],
            ["units_of_measure.id"],
            name=op.f("fk_items_sale_uom_id_units_of_measure"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["updated_by_id"],
            ["users.id"],
            name=op.f("fk_items_updated_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_items")),
        sa.UniqueConstraint("company_id", "code", name="uq_items_company_id_code"),
    )
    op.create_index(op.f("ix_items_base_uom_id"), "items", ["base_uom_id"], unique=False)
    op.create_index(
        "ix_items_bom_components",
        "items",
        ["company_id", "code"],
        unique=False,
        postgresql_where=sa.text("can_be_bom_component AND is_active AND deleted_at IS NULL"),
    )
    op.create_index(op.f("ix_items_category_id"), "items", ["category_id"], unique=False)
    op.create_index(
        "ix_items_code_trgm",
        "items",
        ["code"],
        unique=False,
        postgresql_using="gin",
        postgresql_ops={"code": "gin_trgm_ops"},
    )
    op.create_index(
        "ix_items_company_category_active",
        "items",
        ["company_id", "category_id", "is_active"],
        unique=False,
        postgresql_where=sa.text("deleted_at IS NULL"),
    )
    op.create_index(op.f("ix_items_company_id"), "items", ["company_id"], unique=False)
    op.create_index(op.f("ix_items_created_at"), "items", ["created_at"], unique=False)
    op.create_index(op.f("ix_items_deleted_at"), "items", ["deleted_at"], unique=False)
    op.create_index(
        "ix_items_name_trgm",
        "items",
        ["name"],
        unique=False,
        postgresql_using="gin",
        postgresql_ops={"name": "gin_trgm_ops"},
    )
    op.create_index(
        "ix_items_purchasable",
        "items",
        ["company_id", "code"],
        unique=False,
        postgresql_where=sa.text("can_purchase AND is_active AND deleted_at IS NULL"),
    )
    op.create_index(
        "ix_items_reorder",
        "items",
        ["company_id", "reorder_level"],
        unique=False,
        postgresql_where=sa.text("reorder_level > 0 AND is_active AND deleted_at IS NULL"),
    )
    op.create_index(
        "ix_items_sellable",
        "items",
        ["company_id", "code"],
        unique=False,
        postgresql_where=sa.text("can_sell AND is_active AND deleted_at IS NULL"),
    )
    op.create_table(
        "job_work_orders",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("company_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("code", sa.String(length=64), nullable=False),
        sa.Column("vendor_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("process_type", sa.String(length=24), nullable=False),
        sa.Column("process_description", sa.Text(), nullable=True),
        sa.Column("expected_return_date", sa.Date(), nullable=False),
        sa.Column("warehouse_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("return_warehouse_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "rate_per_unit", sa.Numeric(precision=18, scale=6), server_default="0", nullable=False
        ),
        sa.Column("rate_basis", sa.String(length=16), server_default="PER_PIECE", nullable=False),
        sa.Column(
            "estimated_charges",
            sa.Numeric(precision=18, scale=4),
            server_default="0",
            nullable=False,
        ),
        sa.Column(
            "total_charges", sa.Numeric(precision=18, scale=4), server_default="0", nullable=False
        ),
        sa.Column(
            "vendor_debit_amount",
            sa.Numeric(precision=18, scale=4),
            server_default="0",
            nullable=False,
        ),
        sa.Column("status", sa.String(length=24), server_default="DRAFT", nullable=False),
        sa.Column("approved_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("issued_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("receipt_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("requires_qc", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("source_type", sa.String(length=24), server_default="MANUAL", nullable=False),
        sa.Column("source_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("cancellation_reason", sa.Text(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("created_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("updated_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.CheckConstraint(
            "process_type IN ('POLISHING', 'COATING', 'PVD', 'LASER_MARKING', 'MACHINING', 'OTHER')",
            name=op.f("ck_job_work_orders_process_type_known"),
        ),
        sa.CheckConstraint(
            "rate_basis IN ('PER_PIECE', 'PER_KG', 'LOT')",
            name=op.f("ck_job_work_orders_rate_basis_known"),
        ),
        sa.CheckConstraint(
            "source_type IN ('MANUAL', 'PRODUCTION_ORDER', 'SALES_ORDER', 'ASSEMBLY_ORDER', 'MRP_SUGGESTION', 'REWORK')",
            name=op.f("ck_job_work_orders_source_type_known"),
        ),
        sa.CheckConstraint(
            "status <> 'CANCELLED' OR cancellation_reason IS NOT NULL",
            name=op.f("ck_job_work_orders_cancelled_orders_carry_a_reason"),
        ),
        sa.CheckConstraint(
            "status IN ('DRAFT', 'APPROVED', 'ISSUED', 'PARTIALLY_RECEIVED', 'RECEIVED', 'QC_PENDING', 'COMPLETED', 'CLOSED', 'CANCELLED')",
            name=op.f("ck_job_work_orders_status_known"),
        ),
        sa.CheckConstraint(
            "(approved_by_id IS NULL) = (approved_at IS NULL)",
            name=op.f("ck_job_work_orders_approval_evidence_complete"),
        ),
        sa.CheckConstraint(
            "estimated_charges >= 0", name=op.f("ck_job_work_orders_estimated_charges_non_negative")
        ),
        sa.CheckConstraint("rate_per_unit >= 0", name=op.f("ck_job_work_orders_rate_non_negative")),
        sa.CheckConstraint(
            "receipt_count >= 0", name=op.f("ck_job_work_orders_receipt_count_non_negative")
        ),
        sa.CheckConstraint(
            "total_charges >= 0", name=op.f("ck_job_work_orders_total_charges_non_negative")
        ),
        sa.CheckConstraint(
            "vendor_debit_amount >= 0", name=op.f("ck_job_work_orders_vendor_debit_non_negative")
        ),
        sa.ForeignKeyConstraint(
            ["approved_by_id"],
            ["users.id"],
            name=op.f("fk_job_work_orders_approved_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["company_id"],
            ["companies.id"],
            name=op.f("fk_job_work_orders_company_id_companies"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_id"],
            ["users.id"],
            name=op.f("fk_job_work_orders_created_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["return_warehouse_id"],
            ["warehouses.id"],
            name=op.f("fk_job_work_orders_return_warehouse_id_warehouses"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["updated_by_id"],
            ["users.id"],
            name=op.f("fk_job_work_orders_updated_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["vendor_id"],
            ["vendors.id"],
            name=op.f("fk_job_work_orders_vendor_id_vendors"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["warehouse_id"],
            ["warehouses.id"],
            name=op.f("fk_job_work_orders_warehouse_id_warehouses"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_job_work_orders")),
        sa.UniqueConstraint("company_id", "code", name="uq_job_work_orders_company_id_code"),
    )
    op.create_index(
        op.f("ix_job_work_orders_company_id"), "job_work_orders", ["company_id"], unique=False
    )
    op.create_index(
        "ix_job_work_orders_company_status_return",
        "job_work_orders",
        ["company_id", "status", "expected_return_date"],
        unique=False,
    )
    op.create_index(
        op.f("ix_job_work_orders_created_at"), "job_work_orders", ["created_at"], unique=False
    )
    op.create_index(
        "ix_job_work_orders_material_at_vendor",
        "job_work_orders",
        ["company_id", "vendor_id", "expected_return_date"],
        unique=False,
        postgresql_where=sa.text("status IN ('ISSUED', 'PARTIALLY_RECEIVED')"),
    )
    op.create_index(
        op.f("ix_job_work_orders_return_warehouse_id"),
        "job_work_orders",
        ["return_warehouse_id"],
        unique=False,
    )
    op.create_index(
        "ix_job_work_orders_source",
        "job_work_orders",
        ["company_id", "source_type", "source_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_job_work_orders_vendor_id"), "job_work_orders", ["vendor_id"], unique=False
    )
    op.create_index(
        "ix_job_work_orders_vendor_status",
        "job_work_orders",
        ["company_id", "vendor_id", "status"],
        unique=False,
    )
    op.create_index(
        op.f("ix_job_work_orders_warehouse_id"), "job_work_orders", ["warehouse_id"], unique=False
    )
    op.create_table(
        "ledger_entries",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("company_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("journal_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("line_number", sa.Integer(), nullable=False),
        sa.Column("account_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("party_type", sa.String(length=16), server_default="NONE", nullable=False),
        sa.Column("party_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("entry_date", sa.Date(), nullable=False),
        sa.Column(
            "debit_amount", sa.Numeric(precision=18, scale=4), server_default="0", nullable=False
        ),
        sa.Column(
            "credit_amount", sa.Numeric(precision=18, scale=4), server_default="0", nullable=False
        ),
        sa.Column("currency", sa.String(length=3), server_default="INR", nullable=False),
        sa.Column("reference_type", sa.String(length=48), nullable=False),
        sa.Column("reference_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("reference_code", sa.String(length=64), nullable=True),
        sa.Column("narration", sa.String(length=500), nullable=True),
        sa.Column("financial_year_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("is_reversal", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("reverses_entry_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("created_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.CheckConstraint(
            "(party_type = 'NONE') = (party_id IS NULL)",
            name=op.f("ck_ledger_entries_party_type_matches_party_id"),
        ),
        sa.CheckConstraint(
            "party_type IN ('CUSTOMER', 'VENDOR', 'NONE')",
            name=op.f("ck_ledger_entries_party_type_known"),
        ),
        sa.CheckConstraint(
            "(debit_amount > 0 AND credit_amount = 0) OR (credit_amount > 0 AND debit_amount = 0)",
            name=op.f("ck_ledger_entries_exactly_one_side_is_non_zero"),
        ),
        sa.CheckConstraint(
            "credit_amount >= 0", name=op.f("ck_ledger_entries_credit_non_negative")
        ),
        sa.CheckConstraint("debit_amount >= 0", name=op.f("ck_ledger_entries_debit_non_negative")),
        sa.CheckConstraint(
            "is_reversal = (reverses_entry_id IS NOT NULL)",
            name=op.f("ck_ledger_entries_reversal_evidence_complete"),
        ),
        sa.CheckConstraint(
            "length(currency) = 3", name=op.f("ck_ledger_entries_currency_is_iso_code")
        ),
        sa.CheckConstraint("line_number > 0", name=op.f("ck_ledger_entries_line_number_positive")),
        sa.ForeignKeyConstraint(
            ["account_id"],
            ["ledger_accounts.id"],
            name=op.f("fk_ledger_entries_account_id_ledger_accounts"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["company_id"],
            ["companies.id"],
            name=op.f("fk_ledger_entries_company_id_companies"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_id"],
            ["users.id"],
            name=op.f("fk_ledger_entries_created_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["financial_year_id"],
            ["financial_years.id"],
            name=op.f("fk_ledger_entries_financial_year_id_financial_years"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["reverses_entry_id"],
            ["ledger_entries.id"],
            name=op.f("fk_ledger_entries_reverses_entry_id_ledger_entries"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_ledger_entries")),
        sa.UniqueConstraint("journal_id", "line_number", name="uq_ledger_entries_journal_line"),
    )
    op.create_index(
        "ix_ledger_entries_account_date",
        "ledger_entries",
        ["company_id", "account_id", "entry_date"],
        unique=False,
    )
    op.create_index(
        op.f("ix_ledger_entries_account_id"), "ledger_entries", ["account_id"], unique=False
    )
    op.create_index(
        "ix_ledger_entries_company_date",
        "ledger_entries",
        ["company_id", "entry_date", "id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_ledger_entries_company_id"), "ledger_entries", ["company_id"], unique=False
    )
    op.create_index(
        op.f("ix_ledger_entries_financial_year_id"),
        "ledger_entries",
        ["financial_year_id"],
        unique=False,
    )
    op.create_index(
        "ix_ledger_entries_journal", "ledger_entries", ["company_id", "journal_id"], unique=False
    )
    op.create_index(
        "ix_ledger_entries_party",
        "ledger_entries",
        ["company_id", "party_type", "party_id", "entry_date", "id"],
        unique=False,
    )
    op.create_index(
        "ix_ledger_entries_reference",
        "ledger_entries",
        ["company_id", "reference_type", "reference_id"],
        unique=False,
    )
    op.create_table(
        "machines",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("company_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("code", sa.String(length=32), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("work_center_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("machine_type", sa.String(length=64), nullable=True),
        sa.Column(
            "capacity_per_hour",
            sa.Numeric(precision=18, scale=4),
            server_default="0",
            nullable=False,
        ),
        sa.Column(
            "cost_per_hour", sa.Numeric(precision=18, scale=4), server_default="0", nullable=False
        ),
        sa.Column("status", sa.String(length=24), server_default="AVAILABLE", nullable=False),
        sa.Column("last_maintenance_date", sa.Date(), nullable=True),
        sa.Column("next_maintenance_date", sa.Date(), nullable=True),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("updated_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.CheckConstraint(
            "capacity_per_hour >= 0", name=op.f("ck_machines_capacity_non_negative")
        ),
        sa.CheckConstraint(
            "cost_per_hour >= 0", name=op.f("ck_machines_cost_per_hour_non_negative")
        ),
        sa.CheckConstraint(
            "next_maintenance_date IS NULL OR last_maintenance_date IS NULL OR next_maintenance_date >= last_maintenance_date",
            name=op.f("ck_machines_maintenance_dates_ordered"),
        ),
        sa.ForeignKeyConstraint(
            ["company_id"],
            ["companies.id"],
            name=op.f("fk_machines_company_id_companies"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_id"],
            ["users.id"],
            name=op.f("fk_machines_created_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["updated_by_id"],
            ["users.id"],
            name=op.f("fk_machines_updated_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["work_center_id"],
            ["work_centers.id"],
            name=op.f("fk_machines_work_center_id_work_centers"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_machines")),
        sa.UniqueConstraint("company_id", "code", name="uq_machines_company_id_code"),
        sa.UniqueConstraint("id", "work_center_id", name="uq_machines_id_work_center_id"),
    )
    op.create_index(op.f("ix_machines_company_id"), "machines", ["company_id"], unique=False)
    op.create_index(
        "ix_machines_company_work_center_status",
        "machines",
        ["company_id", "work_center_id", "status"],
        unique=False,
    )
    op.create_index(op.f("ix_machines_created_at"), "machines", ["created_at"], unique=False)
    op.create_index(op.f("ix_machines_deleted_at"), "machines", ["deleted_at"], unique=False)
    op.create_index(
        "ix_machines_next_maintenance_due",
        "machines",
        ["company_id", "next_maintenance_date"],
        unique=False,
        postgresql_where=sa.text("next_maintenance_date IS NOT NULL"),
    )
    op.create_index(
        op.f("ix_machines_work_center_id"), "machines", ["work_center_id"], unique=False
    )
    op.create_table(
        "purchase_orders",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("company_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("code", sa.String(length=64), nullable=False),
        sa.Column("vendor_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("po_date", sa.Date(), nullable=False),
        sa.Column("expected_delivery_date", sa.Date(), nullable=True),
        sa.Column("warehouse_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("branch_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("status", sa.String(length=24), server_default="DRAFT", nullable=False),
        sa.Column("priority", sa.Integer(), server_default="5", nullable=False),
        sa.Column("currency", sa.String(length=3), server_default="INR", nullable=False),
        sa.Column(
            "subtotal", sa.Numeric(precision=18, scale=4), server_default="0", nullable=False
        ),
        sa.Column(
            "discount", sa.Numeric(precision=18, scale=4), server_default="0", nullable=False
        ),
        sa.Column(
            "tax_amount", sa.Numeric(precision=18, scale=4), server_default="0", nullable=False
        ),
        sa.Column(
            "freight_amount", sa.Numeric(precision=18, scale=4), server_default="0", nullable=False
        ),
        sa.Column(
            "total_amount", sa.Numeric(precision=18, scale=4), server_default="0", nullable=False
        ),
        sa.Column("payment_terms_days", sa.Integer(), server_default="0", nullable=False),
        sa.Column("delivery_address", sa.Text(), nullable=True),
        sa.Column("terms", sa.Text(), nullable=True),
        sa.Column("approved_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("acknowledged_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("rejection_reason", sa.Text(), nullable=True),
        sa.Column("cancellation_reason", sa.Text(), nullable=True),
        sa.Column(
            "duplicate_acknowledged", sa.Boolean(), server_default=sa.text("false"), nullable=False
        ),
        sa.Column("duplicate_acknowledgement_reason", sa.Text(), nullable=True),
        sa.Column("source_type", sa.String(length=32), server_default="MANUAL", nullable=False),
        sa.Column("source_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("assigned_vendor_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("courier_name", sa.String(length=120), nullable=True),
        sa.Column("tracking_reference", sa.String(length=120), nullable=True),
        sa.Column("shipped_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("vendor_reference", sa.String(length=64), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("created_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("updated_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.CheckConstraint(
            "source_type IN ('MANUAL', 'PURCHASE_REQUISITION', 'VENDOR_QUOTATION', 'MRP_SUGGESTION', 'MRP_RUN', 'SALES_ORDER', 'BOM_DEMAND', 'REORDER')",
            name=op.f("ck_purchase_orders_source_type_known"),
        ),
        sa.CheckConstraint(
            "status <> 'CANCELLED' OR cancellation_reason IS NOT NULL",
            name=op.f("ck_purchase_orders_cancelled_orders_carry_a_reason"),
        ),
        sa.CheckConstraint(
            "status <> 'REJECTED' OR rejection_reason IS NOT NULL",
            name=op.f("ck_purchase_orders_rejected_orders_carry_a_reason"),
        ),
        sa.CheckConstraint(
            "status IN ('DRAFT', 'PENDING_APPROVAL', 'APPROVED', 'REJECTED', 'SENT', 'ACKNOWLEDGED', 'PARTIALLY_RECEIVED', 'RECEIVED', 'QC_PENDING', 'QC_COMPLETED', 'GRN_COMPLETED', 'INVOICED', 'PAID', 'CLOSED', 'CANCELLED')",
            name=op.f("ck_purchase_orders_status_known"),
        ),
        sa.CheckConstraint(
            "(approved_by_id IS NULL) = (approved_at IS NULL)",
            name=op.f("ck_purchase_orders_approval_evidence_complete"),
        ),
        sa.CheckConstraint(
            "NOT duplicate_acknowledged OR duplicate_acknowledgement_reason IS NOT NULL",
            name=op.f("ck_purchase_orders_duplicate_acknowledgement_carries_a_reason"),
        ),
        sa.CheckConstraint("discount >= 0", name=op.f("ck_purchase_orders_discount_non_negative")),
        sa.CheckConstraint(
            "expected_delivery_date IS NULL OR expected_delivery_date >= po_date",
            name=op.f("ck_purchase_orders_expected_delivery_not_before_po_date"),
        ),
        sa.CheckConstraint(
            "freight_amount >= 0", name=op.f("ck_purchase_orders_freight_non_negative")
        ),
        sa.CheckConstraint(
            "length(currency) = 3", name=op.f("ck_purchase_orders_currency_is_iso_code")
        ),
        sa.CheckConstraint(
            "payment_terms_days >= 0", name=op.f("ck_purchase_orders_payment_terms_non_negative")
        ),
        sa.CheckConstraint(
            "priority BETWEEN 1 AND 9", name=op.f("ck_purchase_orders_priority_in_range")
        ),
        sa.CheckConstraint("subtotal >= 0", name=op.f("ck_purchase_orders_subtotal_non_negative")),
        sa.CheckConstraint("tax_amount >= 0", name=op.f("ck_purchase_orders_tax_non_negative")),
        sa.CheckConstraint("total_amount >= 0", name=op.f("ck_purchase_orders_total_non_negative")),
        sa.ForeignKeyConstraint(
            ["approved_by_id"],
            ["users.id"],
            name=op.f("fk_purchase_orders_approved_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["assigned_vendor_user_id"],
            ["users.id"],
            name=op.f("fk_purchase_orders_assigned_vendor_user_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["branch_id"],
            ["branches.id"],
            name=op.f("fk_purchase_orders_branch_id_branches"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["company_id"],
            ["companies.id"],
            name=op.f("fk_purchase_orders_company_id_companies"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_id"],
            ["users.id"],
            name=op.f("fk_purchase_orders_created_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["updated_by_id"],
            ["users.id"],
            name=op.f("fk_purchase_orders_updated_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["vendor_id"],
            ["vendors.id"],
            name=op.f("fk_purchase_orders_vendor_id_vendors"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["warehouse_id"],
            ["warehouses.id"],
            name=op.f("fk_purchase_orders_warehouse_id_warehouses"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_purchase_orders")),
        sa.UniqueConstraint("company_id", "code", name="uq_purchase_orders_company_id_code"),
    )
    op.create_index(
        "ix_purchase_orders_assigned_vendor_user",
        "purchase_orders",
        ["company_id", "assigned_vendor_user_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_purchase_orders_assigned_vendor_user_id"),
        "purchase_orders",
        ["assigned_vendor_user_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_purchase_orders_branch_id"), "purchase_orders", ["branch_id"], unique=False
    )
    op.create_index(
        op.f("ix_purchase_orders_company_id"), "purchase_orders", ["company_id"], unique=False
    )
    op.create_index(
        op.f("ix_purchase_orders_created_at"), "purchase_orders", ["created_at"], unique=False
    )
    op.create_index(
        op.f("ix_purchase_orders_expected_delivery_date"),
        "purchase_orders",
        ["expected_delivery_date"],
        unique=False,
    )
    op.create_index(
        "ix_purchase_orders_open_vendor",
        "purchase_orders",
        ["company_id", "vendor_id", "warehouse_id", "expected_delivery_date"],
        unique=False,
        postgresql_where=sa.text(
            "status IN ('DRAFT', 'PENDING_APPROVAL', 'APPROVED', 'SENT', 'ACKNOWLEDGED', 'PARTIALLY_RECEIVED')"
        ),
    )
    op.create_index(
        "ix_purchase_orders_source",
        "purchase_orders",
        ["company_id", "source_type", "source_id"],
        unique=False,
    )
    op.create_index(
        "ix_purchase_orders_status_expected",
        "purchase_orders",
        ["company_id", "status", "expected_delivery_date"],
        unique=False,
    )
    op.create_index(
        "ix_purchase_orders_vendor",
        "purchase_orders",
        ["company_id", "vendor_id", "po_date"],
        unique=False,
    )
    op.create_index(
        op.f("ix_purchase_orders_vendor_id"), "purchase_orders", ["vendor_id"], unique=False
    )
    op.create_index(
        op.f("ix_purchase_orders_warehouse_id"), "purchase_orders", ["warehouse_id"], unique=False
    )
    op.create_index(
        "ix_purchase_orders_warehouse_status",
        "purchase_orders",
        ["company_id", "warehouse_id", "status"],
        unique=False,
    )
    op.create_table(
        "purchase_requisitions",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("company_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("code", sa.String(length=64), nullable=False),
        sa.Column("requisition_date", sa.Date(), nullable=False),
        sa.Column("required_date", sa.Date(), nullable=False),
        sa.Column("warehouse_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("department_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("branch_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("requested_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("status", sa.String(length=24), server_default="DRAFT", nullable=False),
        sa.Column("priority", sa.Integer(), server_default="5", nullable=False),
        sa.Column("approved_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("rejection_reason", sa.Text(), nullable=True),
        sa.Column("cancellation_reason", sa.Text(), nullable=True),
        sa.Column("source_type", sa.String(length=32), server_default="MANUAL", nullable=False),
        sa.Column("source_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("purpose", sa.String(length=255), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("created_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("updated_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.CheckConstraint(
            "source_type IN ('MANUAL', 'PURCHASE_REQUISITION', 'VENDOR_QUOTATION', 'MRP_SUGGESTION', 'MRP_RUN', 'SALES_ORDER', 'BOM_DEMAND', 'REORDER')",
            name=op.f("ck_purchase_requisitions_source_type_known"),
        ),
        sa.CheckConstraint(
            "status <> 'CANCELLED' OR cancellation_reason IS NOT NULL",
            name=op.f("ck_purchase_requisitions_cancelled_requisitions_carry_a_reason"),
        ),
        sa.CheckConstraint(
            "status <> 'REJECTED' OR rejection_reason IS NOT NULL",
            name=op.f("ck_purchase_requisitions_rejected_requisitions_carry_a_reason"),
        ),
        sa.CheckConstraint(
            "status IN ('DRAFT', 'PENDING_APPROVAL', 'APPROVED', 'REJECTED', 'CONVERTED', 'CLOSED', 'CANCELLED')",
            name=op.f("ck_purchase_requisitions_status_known"),
        ),
        sa.CheckConstraint(
            "(approved_by_id IS NULL) = (approved_at IS NULL)",
            name=op.f("ck_purchase_requisitions_approval_evidence_complete"),
        ),
        sa.CheckConstraint(
            "priority BETWEEN 1 AND 9", name=op.f("ck_purchase_requisitions_priority_in_range")
        ),
        sa.CheckConstraint(
            "required_date >= requisition_date",
            name=op.f("ck_purchase_requisitions_required_date_not_before_requisition"),
        ),
        sa.ForeignKeyConstraint(
            ["approved_by_id"],
            ["users.id"],
            name=op.f("fk_purchase_requisitions_approved_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["branch_id"],
            ["branches.id"],
            name=op.f("fk_purchase_requisitions_branch_id_branches"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["company_id"],
            ["companies.id"],
            name=op.f("fk_purchase_requisitions_company_id_companies"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_id"],
            ["users.id"],
            name=op.f("fk_purchase_requisitions_created_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["department_id"],
            ["departments.id"],
            name=op.f("fk_purchase_requisitions_department_id_departments"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["requested_by_id"],
            ["users.id"],
            name=op.f("fk_purchase_requisitions_requested_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["updated_by_id"],
            ["users.id"],
            name=op.f("fk_purchase_requisitions_updated_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["warehouse_id"],
            ["warehouses.id"],
            name=op.f("fk_purchase_requisitions_warehouse_id_warehouses"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_purchase_requisitions")),
        sa.UniqueConstraint("company_id", "code", name="uq_purchase_requisitions_company_id_code"),
    )
    op.create_index(
        op.f("ix_purchase_requisitions_branch_id"),
        "purchase_requisitions",
        ["branch_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_purchase_requisitions_company_id"),
        "purchase_requisitions",
        ["company_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_purchase_requisitions_created_at"),
        "purchase_requisitions",
        ["created_at"],
        unique=False,
    )
    op.create_index(
        op.f("ix_purchase_requisitions_department_id"),
        "purchase_requisitions",
        ["department_id"],
        unique=False,
    )
    op.create_index(
        "ix_purchase_requisitions_source",
        "purchase_requisitions",
        ["company_id", "source_type", "source_id"],
        unique=False,
    )
    op.create_index(
        "ix_purchase_requisitions_status_required",
        "purchase_requisitions",
        ["company_id", "status", "required_date"],
        unique=False,
    )
    op.create_index(
        "ix_purchase_requisitions_warehouse",
        "purchase_requisitions",
        ["company_id", "warehouse_id", "status"],
        unique=False,
    )
    op.create_index(
        op.f("ix_purchase_requisitions_warehouse_id"),
        "purchase_requisitions",
        ["warehouse_id"],
        unique=False,
    )
    op.create_table(
        "report_snapshots",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("company_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("report_key", sa.String(length=48), nullable=False),
        sa.Column("parameters_hash", sa.String(length=64), nullable=False),
        sa.Column("parameters", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("status", sa.String(length=24), server_default="QUEUED", nullable=False),
        sa.Column("generated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("row_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("document_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("celery_task_id", sa.String(length=64), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("created_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("updated_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.CheckConstraint(
            "(status = 'COMPLETED') = (generated_at IS NOT NULL)",
            name=op.f("ck_report_snapshots_completed_has_generated_at"),
        ),
        sa.CheckConstraint(
            "report_key IN ('SALES_ORDER_STATUS', 'STOCK_AVAILABILITY', 'MRP_SHORTAGE', 'PURCHASE_PENDING', 'PRODUCTION_PENDING', 'JOB_WORK_PENDING', 'QC_REJECTION', 'VENDOR_PERFORMANCE', 'PRODUCT_PROFITABILITY', 'FAST_MOVING', 'SLOW_MOVING', 'DEAD_STOCK', 'ORDER_DELAY', 'PRODUCTION_EFFICIENCY', 'MACHINE_UTILISATION', 'INVENTORY_VALUATION', 'DISPATCH', 'COURIER_TRACKING', 'VENDOR_DISPATCH_HISTORY')",
            name=op.f("ck_report_snapshots_report_key_is_known"),
        ),
        sa.CheckConstraint(
            "status = 'FAILED' OR error_message IS NULL",
            name=op.f("ck_report_snapshots_only_a_failure_carries_an_error"),
        ),
        sa.CheckConstraint(
            "status IN ('QUEUED', 'RUNNING', 'COMPLETED', 'FAILED')",
            name=op.f("ck_report_snapshots_status_is_known"),
        ),
        sa.CheckConstraint(
            "payload IS NULL OR document_id IS NULL",
            name=op.f("ck_report_snapshots_result_is_inline_or_a_document"),
        ),
        sa.CheckConstraint(
            "row_count >= 0", name=op.f("ck_report_snapshots_row_count_is_not_negative")
        ),
        sa.ForeignKeyConstraint(
            ["company_id"],
            ["companies.id"],
            name=op.f("fk_report_snapshots_company_id_companies"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_id"],
            ["users.id"],
            name=op.f("fk_report_snapshots_created_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["document_id"],
            ["documents.id"],
            name=op.f("fk_report_snapshots_document_id_documents"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["updated_by_id"],
            ["users.id"],
            name=op.f("fk_report_snapshots_updated_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_report_snapshots")),
    )
    op.create_index(
        op.f("ix_report_snapshots_company_id"), "report_snapshots", ["company_id"], unique=False
    )
    op.create_index(
        op.f("ix_report_snapshots_created_at"), "report_snapshots", ["created_at"], unique=False
    )
    op.create_index(
        op.f("ix_report_snapshots_expires_at"), "report_snapshots", ["expires_at"], unique=False
    )
    op.create_index(
        "ix_report_snapshots_lookup",
        "report_snapshots",
        ["company_id", "report_key", "parameters_hash", sa.literal_column("created_at DESC")],
        unique=False,
    )
    op.create_index(
        op.f("ix_report_snapshots_status"), "report_snapshots", ["status"], unique=False
    )
    op.create_table(
        "stock_adjustments",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("company_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("code", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=24), server_default="DRAFT", nullable=False),
        sa.Column("warehouse_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("adjustment_date", sa.Date(), nullable=False),
        sa.Column("reason_code", sa.String(length=24), nullable=False),
        sa.Column("reason", sa.String(length=255), nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column(
            "total_value", sa.Numeric(precision=18, scale=4), server_default="0", nullable=False
        ),
        sa.Column(
            "requires_approval", sa.Boolean(), server_default=sa.text("false"), nullable=False
        ),
        sa.Column("approved_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("posted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("decision_note", sa.String(length=255), nullable=True),
        sa.Column("line_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("created_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("updated_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.CheckConstraint(
            "total_value >= 0", name=op.f("ck_stock_adjustments_total_value_non_negative")
        ),
        sa.ForeignKeyConstraint(
            ["approved_by_id"],
            ["users.id"],
            name=op.f("fk_stock_adjustments_approved_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["company_id"],
            ["companies.id"],
            name=op.f("fk_stock_adjustments_company_id_companies"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_id"],
            ["users.id"],
            name=op.f("fk_stock_adjustments_created_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["updated_by_id"],
            ["users.id"],
            name=op.f("fk_stock_adjustments_updated_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["warehouse_id"],
            ["warehouses.id"],
            name=op.f("fk_stock_adjustments_warehouse_id_warehouses"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_stock_adjustments")),
        sa.UniqueConstraint("company_id", "code", name="uq_stock_adjustments_company_id_code"),
    )
    op.create_index(
        op.f("ix_stock_adjustments_company_id"), "stock_adjustments", ["company_id"], unique=False
    )
    op.create_index(
        op.f("ix_stock_adjustments_created_at"), "stock_adjustments", ["created_at"], unique=False
    )
    op.create_index(
        "ix_stock_adjustments_pending",
        "stock_adjustments",
        ["company_id", "adjustment_date"],
        unique=False,
        postgresql_where=sa.text("status = 'PENDING_APPROVAL'"),
    )
    op.create_index(
        "ix_stock_adjustments_status",
        "stock_adjustments",
        ["company_id", "status", "adjustment_date"],
        unique=False,
    )
    op.create_index(
        "ix_stock_adjustments_warehouse",
        "stock_adjustments",
        ["company_id", "warehouse_id", "adjustment_date"],
        unique=False,
    )
    op.create_table(
        "stock_transfers",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("company_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("code", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=24), server_default="DRAFT", nullable=False),
        sa.Column("from_warehouse_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("to_warehouse_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("transfer_date", sa.Date(), nullable=False),
        sa.Column("expected_date", sa.Date(), nullable=True),
        sa.Column("dispatched_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("approved_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reason", sa.String(length=255), nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("line_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column(
            "total_quantity", sa.Numeric(precision=18, scale=4), server_default="0", nullable=False
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("created_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("updated_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.CheckConstraint(
            "from_warehouse_id <> to_warehouse_id",
            name=op.f("ck_stock_transfers_source_differs_from_destination"),
        ),
        sa.CheckConstraint(
            "total_quantity >= 0", name=op.f("ck_stock_transfers_total_quantity_non_negative")
        ),
        sa.ForeignKeyConstraint(
            ["approved_by_id"],
            ["users.id"],
            name=op.f("fk_stock_transfers_approved_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["company_id"],
            ["companies.id"],
            name=op.f("fk_stock_transfers_company_id_companies"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_id"],
            ["users.id"],
            name=op.f("fk_stock_transfers_created_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["from_warehouse_id"],
            ["warehouses.id"],
            name=op.f("fk_stock_transfers_from_warehouse_id_warehouses"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["to_warehouse_id"],
            ["warehouses.id"],
            name=op.f("fk_stock_transfers_to_warehouse_id_warehouses"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["updated_by_id"],
            ["users.id"],
            name=op.f("fk_stock_transfers_updated_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_stock_transfers")),
        sa.UniqueConstraint("company_id", "code", name="uq_stock_transfers_company_id_code"),
    )
    op.create_index(
        op.f("ix_stock_transfers_company_id"), "stock_transfers", ["company_id"], unique=False
    )
    op.create_index(
        op.f("ix_stock_transfers_created_at"), "stock_transfers", ["created_at"], unique=False
    )
    op.create_index(
        "ix_stock_transfers_from",
        "stock_transfers",
        ["company_id", "from_warehouse_id", "status"],
        unique=False,
    )
    op.create_index(
        "ix_stock_transfers_status",
        "stock_transfers",
        ["company_id", "status", "transfer_date"],
        unique=False,
    )
    op.create_index(
        "ix_stock_transfers_to",
        "stock_transfers",
        ["company_id", "to_warehouse_id", "status"],
        unique=False,
    )
    op.create_table(
        "storage_locations",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("company_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("warehouse_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("code", sa.String(length=32), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("location_type", sa.String(length=16), nullable=False),
        sa.Column("parent_location_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("capacity_quantity", sa.Numeric(precision=18, scale=4), nullable=True),
        sa.Column("capacity_uom", sa.String(length=16), nullable=True),
        sa.Column("max_weight_kg", sa.Numeric(precision=18, scale=4), nullable=True),
        sa.Column("max_volume_m3", sa.Numeric(precision=18, scale=4), nullable=True),
        sa.Column("barcode", sa.String(length=64), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("updated_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.CheckConstraint(
            "capacity_quantity IS NULL OR capacity_quantity >= 0",
            name=op.f("ck_storage_locations_capacity_non_negative"),
        ),
        sa.CheckConstraint(
            "max_volume_m3 IS NULL OR max_volume_m3 >= 0",
            name=op.f("ck_storage_locations_max_volume_non_negative"),
        ),
        sa.CheckConstraint(
            "max_weight_kg IS NULL OR max_weight_kg >= 0",
            name=op.f("ck_storage_locations_max_weight_non_negative"),
        ),
        sa.CheckConstraint(
            "parent_location_id IS NULL OR parent_location_id <> id",
            name=op.f("ck_storage_locations_parent_not_self"),
        ),
        sa.ForeignKeyConstraint(
            ["company_id"],
            ["companies.id"],
            name=op.f("fk_storage_locations_company_id_companies"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_id"],
            ["users.id"],
            name=op.f("fk_storage_locations_created_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["parent_location_id"],
            ["storage_locations.id"],
            name=op.f("fk_storage_locations_parent_location_id_storage_locations"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["updated_by_id"],
            ["users.id"],
            name=op.f("fk_storage_locations_updated_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["warehouse_id"],
            ["warehouses.id"],
            name=op.f("fk_storage_locations_warehouse_id_warehouses"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_storage_locations")),
        sa.UniqueConstraint(
            "company_id",
            "warehouse_id",
            "code",
            name="uq_storage_locations_company_id_warehouse_id_code",
        ),
    )
    op.create_index(
        "ix_storage_locations_barcode", "storage_locations", ["company_id", "barcode"], unique=False
    )
    op.create_index(
        op.f("ix_storage_locations_company_id"), "storage_locations", ["company_id"], unique=False
    )
    op.create_index(
        op.f("ix_storage_locations_created_at"), "storage_locations", ["created_at"], unique=False
    )
    op.create_index(
        op.f("ix_storage_locations_deleted_at"), "storage_locations", ["deleted_at"], unique=False
    )
    op.create_index(
        op.f("ix_storage_locations_parent_location_id"),
        "storage_locations",
        ["parent_location_id"],
        unique=False,
    )
    op.create_index(
        "ix_storage_locations_warehouse_active",
        "storage_locations",
        ["company_id", "warehouse_id", "is_active"],
        unique=False,
    )
    op.create_index(
        op.f("ix_storage_locations_warehouse_id"),
        "storage_locations",
        ["warehouse_id"],
        unique=False,
    )
    op.create_table(
        "uom_conversions",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("company_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("from_uom_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("to_uom_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("factor", sa.Numeric(precision=18, scale=8), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("updated_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.CheckConstraint("factor > 0", name=op.f("ck_uom_conversions_factor_positive")),
        sa.CheckConstraint(
            "from_uom_id <> to_uom_id", name=op.f("ck_uom_conversions_from_differs_from_to")
        ),
        sa.ForeignKeyConstraint(
            ["company_id"],
            ["companies.id"],
            name=op.f("fk_uom_conversions_company_id_companies"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_id"],
            ["users.id"],
            name=op.f("fk_uom_conversions_created_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["from_uom_id"],
            ["units_of_measure.id"],
            name=op.f("fk_uom_conversions_from_uom_id_units_of_measure"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["to_uom_id"],
            ["units_of_measure.id"],
            name=op.f("fk_uom_conversions_to_uom_id_units_of_measure"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["updated_by_id"],
            ["users.id"],
            name=op.f("fk_uom_conversions_updated_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_uom_conversions")),
        sa.UniqueConstraint(
            "company_id", "from_uom_id", "to_uom_id", name="uq_uom_conversions_company_id_from_to"
        ),
    )
    op.create_index(
        op.f("ix_uom_conversions_company_id"), "uom_conversions", ["company_id"], unique=False
    )
    op.create_index(
        op.f("ix_uom_conversions_created_at"), "uom_conversions", ["created_at"], unique=False
    )
    op.create_index(
        op.f("ix_uom_conversions_deleted_at"), "uom_conversions", ["deleted_at"], unique=False
    )
    op.create_index(
        op.f("ix_uom_conversions_from_uom_id"), "uom_conversions", ["from_uom_id"], unique=False
    )
    op.create_index(
        op.f("ix_uom_conversions_to_uom_id"), "uom_conversions", ["to_uom_id"], unique=False
    )
    op.create_table(
        "user_roles",
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("role_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "assigned_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("assigned_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["assigned_by_id"],
            ["users.id"],
            name=op.f("fk_user_roles_assigned_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["role_id"], ["roles.id"], name=op.f("fk_user_roles_role_id_roles"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name=op.f("fk_user_roles_user_id_users"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("user_id", "role_id", name=op.f("pk_user_roles")),
    )
    op.create_index("ix_user_roles_role", "user_roles", ["role_id"], unique=False)
    op.create_table(
        "vendor_categories",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("company_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("vendor_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("item_category_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("is_preferred", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("created_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("updated_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["company_id"],
            ["companies.id"],
            name=op.f("fk_vendor_categories_company_id_companies"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_id"],
            ["users.id"],
            name=op.f("fk_vendor_categories_created_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["item_category_id"],
            ["item_categories.id"],
            name=op.f("fk_vendor_categories_item_category_id_item_categories"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["updated_by_id"],
            ["users.id"],
            name=op.f("fk_vendor_categories_updated_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["vendor_id"],
            ["vendors.id"],
            name=op.f("fk_vendor_categories_vendor_id_vendors"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_vendor_categories")),
        sa.UniqueConstraint(
            "company_id",
            "vendor_id",
            "item_category_id",
            name="uq_vendor_categories_company_id_vendor_id_item_category_id",
        ),
    )
    op.create_index(
        "ix_vendor_categories_category",
        "vendor_categories",
        ["company_id", "item_category_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_vendor_categories_company_id"), "vendor_categories", ["company_id"], unique=False
    )
    op.create_index(
        op.f("ix_vendor_categories_created_at"), "vendor_categories", ["created_at"], unique=False
    )
    op.create_index(
        op.f("ix_vendor_categories_item_category_id"),
        "vendor_categories",
        ["item_category_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_vendor_categories_vendor_id"), "vendor_categories", ["vendor_id"], unique=False
    )
    op.create_index(
        "uq_vendor_categories_one_preferred",
        "vendor_categories",
        ["company_id", "item_category_id"],
        unique=True,
        postgresql_where=sa.text("is_preferred"),
    )
    op.create_table(
        "boms",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("company_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("code", sa.String(length=64), nullable=False),
        sa.Column("item_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("revision", sa.String(length=16), nullable=True),
        sa.Column("status", sa.String(length=24), server_default="DRAFT", nullable=False),
        sa.Column("bom_type", sa.String(length=16), server_default="PRODUCTION", nullable=False),
        sa.Column("is_default", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column(
            "output_quantity", sa.Numeric(precision=18, scale=4), server_default="1", nullable=False
        ),
        sa.Column("base_uom_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("effective_from", sa.Date(), nullable=False),
        sa.Column("effective_to", sa.Date(), nullable=True),
        sa.Column("parent_bom_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("approved_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "total_material_cost",
            sa.Numeric(precision=18, scale=4),
            server_default="0",
            nullable=False,
        ),
        sa.Column(
            "total_operation_cost",
            sa.Numeric(precision=18, scale=4),
            server_default="0",
            nullable=False,
        ),
        sa.Column("cost_rolled_up_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("row_version", sa.Integer(), server_default="1", nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("created_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("updated_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.CheckConstraint(
            "(status <> 'APPROVED') OR (approved_by_id IS NOT NULL AND approved_at IS NOT NULL)",
            name=op.f("ck_boms_approved_rows_carry_approval_evidence"),
        ),
        sa.CheckConstraint(
            "effective_to IS NULL OR effective_to >= effective_from",
            name=op.f("ck_boms_effectivity_window_ordered"),
        ),
        sa.CheckConstraint("output_quantity > 0", name=op.f("ck_boms_output_quantity_positive")),
        sa.CheckConstraint(
            "parent_bom_id IS NULL OR parent_bom_id <> id", name=op.f("ck_boms_parent_bom_not_self")
        ),
        sa.CheckConstraint(
            "total_material_cost >= 0", name=op.f("ck_boms_material_cost_non_negative")
        ),
        sa.CheckConstraint(
            "total_operation_cost >= 0", name=op.f("ck_boms_operation_cost_non_negative")
        ),
        sa.CheckConstraint("version > 0", name=op.f("ck_boms_version_positive")),
        sa.ForeignKeyConstraint(
            ["approved_by_id"],
            ["users.id"],
            name=op.f("fk_boms_approved_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["base_uom_id"],
            ["units_of_measure.id"],
            name=op.f("fk_boms_base_uom_id_units_of_measure"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["company_id"],
            ["companies.id"],
            name=op.f("fk_boms_company_id_companies"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_id"],
            ["users.id"],
            name=op.f("fk_boms_created_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["item_id"], ["items.id"], name=op.f("fk_boms_item_id_items"), ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["parent_bom_id"],
            ["boms.id"],
            name=op.f("fk_boms_parent_bom_id_boms"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["updated_by_id"],
            ["users.id"],
            name=op.f("fk_boms_updated_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_boms")),
        sa.UniqueConstraint("company_id", "code", name="uq_boms_company_id_code"),
        sa.UniqueConstraint(
            "company_id", "item_id", "version", name="uq_boms_company_id_item_version"
        ),
        sa.UniqueConstraint("id", "item_id", name="uq_boms_id_item_id"),
    )
    op.create_index(op.f("ix_boms_company_id"), "boms", ["company_id"], unique=False)
    op.create_index(
        "ix_boms_company_status", "boms", ["company_id", "status", "created_at"], unique=False
    )
    op.create_index(op.f("ix_boms_created_at"), "boms", ["created_at"], unique=False)
    op.create_index(
        "ix_boms_effective_lookup",
        "boms",
        ["company_id", "item_id", "effective_from", "effective_to"],
        unique=False,
        postgresql_where=sa.text("status = 'APPROVED'"),
    )
    op.create_index(op.f("ix_boms_item_id"), "boms", ["item_id"], unique=False)
    op.create_index(op.f("ix_boms_parent_bom_id"), "boms", ["parent_bom_id"], unique=False)
    op.create_index(
        "uq_boms_one_default_approved_per_item",
        "boms",
        ["company_id", "item_id"],
        unique=True,
        postgresql_where=sa.text("is_default AND status = 'APPROVED'"),
    )
    op.create_table(
        "goods_receipts",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("company_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("code", sa.String(length=64), nullable=False),
        sa.Column("purchase_order_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("vendor_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("warehouse_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("receipt_date", sa.Date(), nullable=False),
        sa.Column("status", sa.String(length=24), server_default="DRAFT", nullable=False),
        sa.Column("vendor_delivery_note", sa.String(length=64), nullable=True),
        sa.Column("vendor_invoice_number", sa.String(length=64), nullable=True),
        sa.Column("gate_entry_number", sa.String(length=64), nullable=True),
        sa.Column("vehicle_number", sa.String(length=32), nullable=True),
        sa.Column("transporter_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("posted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("posted_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("cancellation_reason", sa.Text(), nullable=True),
        sa.Column(
            "total_received_quantity",
            sa.Numeric(precision=18, scale=4),
            server_default="0",
            nullable=False,
        ),
        sa.Column(
            "total_accepted_quantity",
            sa.Numeric(precision=18, scale=4),
            server_default="0",
            nullable=False,
        ),
        sa.Column(
            "total_rejected_quantity",
            sa.Numeric(precision=18, scale=4),
            server_default="0",
            nullable=False,
        ),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("created_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("updated_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.CheckConstraint(
            "status <> 'CANCELLED' OR cancellation_reason IS NOT NULL",
            name=op.f("ck_goods_receipts_cancelled_receipts_carry_a_reason"),
        ),
        sa.CheckConstraint(
            "status <> 'POSTED' OR (posted_at IS NOT NULL AND posted_by_id IS NOT NULL)",
            name=op.f("ck_goods_receipts_posted_receipts_carry_evidence"),
        ),
        sa.CheckConstraint(
            "status IN ('DRAFT', 'POSTED', 'CANCELLED')",
            name=op.f("ck_goods_receipts_status_known"),
        ),
        sa.CheckConstraint(
            "total_accepted_quantity + total_rejected_quantity = total_received_quantity",
            name=op.f("ck_goods_receipts_accepted_plus_rejected_equals_received"),
        ),
        sa.CheckConstraint(
            "total_accepted_quantity >= 0", name=op.f("ck_goods_receipts_accepted_non_negative")
        ),
        sa.CheckConstraint(
            "total_received_quantity >= 0", name=op.f("ck_goods_receipts_received_non_negative")
        ),
        sa.CheckConstraint(
            "total_rejected_quantity >= 0", name=op.f("ck_goods_receipts_rejected_non_negative")
        ),
        sa.ForeignKeyConstraint(
            ["company_id"],
            ["companies.id"],
            name=op.f("fk_goods_receipts_company_id_companies"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_id"],
            ["users.id"],
            name=op.f("fk_goods_receipts_created_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["posted_by_id"],
            ["users.id"],
            name=op.f("fk_goods_receipts_posted_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["purchase_order_id"],
            ["purchase_orders.id"],
            name=op.f("fk_goods_receipts_purchase_order_id_purchase_orders"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["updated_by_id"],
            ["users.id"],
            name=op.f("fk_goods_receipts_updated_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["vendor_id"],
            ["vendors.id"],
            name=op.f("fk_goods_receipts_vendor_id_vendors"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["warehouse_id"],
            ["warehouses.id"],
            name=op.f("fk_goods_receipts_warehouse_id_warehouses"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_goods_receipts")),
        sa.UniqueConstraint("company_id", "code", name="uq_goods_receipts_company_id_code"),
    )
    op.create_index(
        op.f("ix_goods_receipts_company_id"), "goods_receipts", ["company_id"], unique=False
    )
    op.create_index(
        op.f("ix_goods_receipts_created_at"), "goods_receipts", ["created_at"], unique=False
    )
    op.create_index(
        "ix_goods_receipts_order",
        "goods_receipts",
        ["company_id", "purchase_order_id", "receipt_date"],
        unique=False,
    )
    op.create_index(
        op.f("ix_goods_receipts_purchase_order_id"),
        "goods_receipts",
        ["purchase_order_id"],
        unique=False,
    )
    op.create_index(
        "ix_goods_receipts_status",
        "goods_receipts",
        ["company_id", "status", "receipt_date"],
        unique=False,
    )
    op.create_index(
        "ix_goods_receipts_vendor_date",
        "goods_receipts",
        ["company_id", "vendor_id", "receipt_date"],
        unique=False,
    )
    op.create_index(
        op.f("ix_goods_receipts_vendor_id"), "goods_receipts", ["vendor_id"], unique=False
    )
    op.create_index(
        op.f("ix_goods_receipts_warehouse_id"), "goods_receipts", ["warehouse_id"], unique=False
    )
    op.create_table(
        "inventory_summary",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("company_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("item_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("warehouse_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("on_hand", sa.Numeric(precision=18, scale=4), server_default="0", nullable=False),
        sa.Column(
            "reserved", sa.Numeric(precision=18, scale=4), server_default="0", nullable=False
        ),
        sa.Column(
            "available", sa.Numeric(precision=18, scale=4), server_default="0", nullable=False
        ),
        sa.Column(
            "valuation_amount",
            sa.Numeric(precision=18, scale=4),
            server_default="0",
            nullable=False,
        ),
        sa.Column("last_movement_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("refreshed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("created_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("updated_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.CheckConstraint(
            "on_hand >= 0", name=op.f("ck_inventory_summary_on_hand_is_not_negative")
        ),
        sa.CheckConstraint(
            "reserved >= 0", name=op.f("ck_inventory_summary_reserved_is_not_negative")
        ),
        sa.ForeignKeyConstraint(
            ["company_id"],
            ["companies.id"],
            name=op.f("fk_inventory_summary_company_id_companies"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_id"],
            ["users.id"],
            name=op.f("fk_inventory_summary_created_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["item_id"],
            ["items.id"],
            name=op.f("fk_inventory_summary_item_id_items"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["updated_by_id"],
            ["users.id"],
            name=op.f("fk_inventory_summary_updated_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["warehouse_id"],
            ["warehouses.id"],
            name=op.f("fk_inventory_summary_warehouse_id_warehouses"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_inventory_summary")),
        sa.UniqueConstraint(
            "company_id",
            "item_id",
            "warehouse_id",
            name="uq_inventory_summary_company_id_item_id_warehouse_id",
        ),
    )
    op.create_index(
        "ix_inventory_summary_ageing",
        "inventory_summary",
        ["company_id", sa.literal_column("last_movement_at ASC NULLS FIRST")],
        unique=False,
    )
    op.create_index(
        op.f("ix_inventory_summary_company_id"), "inventory_summary", ["company_id"], unique=False
    )
    op.create_index(
        op.f("ix_inventory_summary_created_at"), "inventory_summary", ["created_at"], unique=False
    )
    op.create_index(
        "ix_inventory_summary_value",
        "inventory_summary",
        ["company_id", sa.literal_column("valuation_amount DESC")],
        unique=False,
    )
    op.create_table(
        "item_variants",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("company_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("item_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("code", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("finish", sa.String(length=64), nullable=True),
        sa.Column("size", sa.String(length=64), nullable=True),
        sa.Column("colour", sa.String(length=64), nullable=True),
        sa.Column(
            "price_delta", sa.Numeric(precision=18, scale=6), server_default="0", nullable=False
        ),
        sa.Column(
            "cost_delta", sa.Numeric(precision=18, scale=4), server_default="0", nullable=False
        ),
        sa.Column("barcode", sa.String(length=64), nullable=True),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("updated_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["company_id"],
            ["companies.id"],
            name=op.f("fk_item_variants_company_id_companies"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_id"],
            ["users.id"],
            name=op.f("fk_item_variants_created_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["item_id"],
            ["items.id"],
            name=op.f("fk_item_variants_item_id_items"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["updated_by_id"],
            ["users.id"],
            name=op.f("fk_item_variants_updated_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_item_variants")),
        sa.UniqueConstraint("company_id", "code", name="uq_item_variants_company_id_code"),
    )
    op.create_index(
        "ix_item_variants_code_trgm",
        "item_variants",
        ["code"],
        unique=False,
        postgresql_using="gin",
        postgresql_ops={"code": "gin_trgm_ops"},
    )
    op.create_index(
        op.f("ix_item_variants_company_id"), "item_variants", ["company_id"], unique=False
    )
    op.create_index(
        op.f("ix_item_variants_created_at"), "item_variants", ["created_at"], unique=False
    )
    op.create_index(
        op.f("ix_item_variants_deleted_at"), "item_variants", ["deleted_at"], unique=False
    )
    op.create_index(
        "ix_item_variants_item_active",
        "item_variants",
        ["company_id", "item_id"],
        unique=False,
        postgresql_where=sa.text("is_active AND deleted_at IS NULL"),
    )
    op.create_index(op.f("ix_item_variants_item_id"), "item_variants", ["item_id"], unique=False)
    op.create_index(
        "uq_item_variants_attribute_combo",
        "item_variants",
        [
            "item_id",
            sa.literal_column("coalesce(finish, '')"),
            sa.literal_column("coalesce(size, '')"),
            sa.literal_column("coalesce(colour, '')"),
        ],
        unique=True,
        postgresql_where=sa.text("deleted_at IS NULL"),
    )
    op.create_table(
        "item_vendor_links",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("company_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("item_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("vendor_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("vendor_item_code", sa.String(length=64), nullable=True),
        sa.Column("vendor_item_name", sa.String(length=255), nullable=True),
        sa.Column("is_preferred", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("lead_time_days", sa.Integer(), server_default="0", nullable=False),
        sa.Column(
            "min_order_quantity",
            sa.Numeric(precision=18, scale=4),
            server_default="0",
            nullable=False,
        ),
        sa.Column(
            "order_multiple", sa.Numeric(precision=18, scale=4), server_default="0", nullable=False
        ),
        sa.Column("last_purchase_price", sa.Numeric(precision=18, scale=6), nullable=True),
        sa.Column("last_purchase_date", sa.Date(), nullable=True),
        sa.Column("currency", sa.String(length=3), server_default="INR", nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("updated_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.CheckConstraint(
            "last_purchase_price IS NULL OR last_purchase_price >= 0",
            name=op.f("ck_item_vendor_links_last_purchase_price_non_negative"),
        ),
        sa.CheckConstraint(
            "lead_time_days >= 0", name=op.f("ck_item_vendor_links_lead_time_non_negative")
        ),
        sa.CheckConstraint(
            "min_order_quantity >= 0",
            name=op.f("ck_item_vendor_links_min_order_quantity_non_negative"),
        ),
        sa.CheckConstraint(
            "order_multiple >= 0", name=op.f("ck_item_vendor_links_order_multiple_non_negative")
        ),
        sa.ForeignKeyConstraint(
            ["company_id"],
            ["companies.id"],
            name=op.f("fk_item_vendor_links_company_id_companies"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_id"],
            ["users.id"],
            name=op.f("fk_item_vendor_links_created_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["item_id"],
            ["items.id"],
            name=op.f("fk_item_vendor_links_item_id_items"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["updated_by_id"],
            ["users.id"],
            name=op.f("fk_item_vendor_links_updated_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["vendor_id"],
            ["vendors.id"],
            name=op.f("fk_item_vendor_links_vendor_id_vendors"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_item_vendor_links")),
        sa.UniqueConstraint(
            "company_id", "item_id", "vendor_id", name="uq_item_vendor_links_company_id_item_vendor"
        ),
    )
    op.create_index(
        op.f("ix_item_vendor_links_company_id"), "item_vendor_links", ["company_id"], unique=False
    )
    op.create_index(
        op.f("ix_item_vendor_links_created_at"), "item_vendor_links", ["created_at"], unique=False
    )
    op.create_index(
        op.f("ix_item_vendor_links_deleted_at"), "item_vendor_links", ["deleted_at"], unique=False
    )
    op.create_index(
        op.f("ix_item_vendor_links_item_id"), "item_vendor_links", ["item_id"], unique=False
    )
    op.create_index(
        "ix_item_vendor_links_vendor",
        "item_vendor_links",
        ["company_id", "vendor_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_item_vendor_links_vendor_id"), "item_vendor_links", ["vendor_id"], unique=False
    )
    op.create_index(
        "uq_item_vendor_links_one_preferred",
        "item_vendor_links",
        ["company_id", "item_id"],
        unique=True,
        postgresql_where=sa.text("is_preferred AND deleted_at IS NULL"),
    )
    op.create_table(
        "job_work_challans",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("company_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("code", sa.String(length=64), nullable=False),
        sa.Column("job_work_order_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("vendor_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("challan_date", sa.Date(), nullable=False),
        sa.Column("expected_return_date", sa.Date(), nullable=False),
        sa.Column("vehicle_number", sa.String(length=24), nullable=True),
        sa.Column("transporter_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "total_quantity", sa.Numeric(precision=18, scale=4), server_default="0", nullable=False
        ),
        sa.Column(
            "total_value", sa.Numeric(precision=18, scale=4), server_default="0", nullable=False
        ),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("created_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("updated_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.CheckConstraint(
            "expected_return_date >= challan_date",
            name=op.f("ck_job_work_challans_return_not_before_despatch"),
        ),
        sa.CheckConstraint(
            "total_quantity > 0", name=op.f("ck_job_work_challans_total_quantity_positive")
        ),
        sa.CheckConstraint(
            "total_value >= 0", name=op.f("ck_job_work_challans_total_value_non_negative")
        ),
        sa.ForeignKeyConstraint(
            ["company_id"],
            ["companies.id"],
            name=op.f("fk_job_work_challans_company_id_companies"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_id"],
            ["users.id"],
            name=op.f("fk_job_work_challans_created_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["job_work_order_id"],
            ["job_work_orders.id"],
            name=op.f("fk_job_work_challans_job_work_order_id_job_work_orders"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["transporter_id"],
            ["transporters.id"],
            name=op.f("fk_job_work_challans_transporter_id_transporters"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["updated_by_id"],
            ["users.id"],
            name=op.f("fk_job_work_challans_updated_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["vendor_id"],
            ["vendors.id"],
            name=op.f("fk_job_work_challans_vendor_id_vendors"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_job_work_challans")),
        sa.UniqueConstraint("company_id", "code", name="uq_job_work_challans_company_id_code"),
    )
    op.create_index(
        "ix_job_work_challans_company_date",
        "job_work_challans",
        ["company_id", "challan_date"],
        unique=False,
    )
    op.create_index(
        op.f("ix_job_work_challans_company_id"), "job_work_challans", ["company_id"], unique=False
    )
    op.create_index(
        op.f("ix_job_work_challans_created_at"), "job_work_challans", ["created_at"], unique=False
    )
    op.create_index(
        op.f("ix_job_work_challans_job_work_order_id"),
        "job_work_challans",
        ["job_work_order_id"],
        unique=False,
    )
    op.create_index(
        "ix_job_work_challans_order",
        "job_work_challans",
        ["company_id", "job_work_order_id"],
        unique=False,
    )
    op.create_index(
        "ix_job_work_challans_vendor_date",
        "job_work_challans",
        ["company_id", "vendor_id", "challan_date"],
        unique=False,
    )
    op.create_index(
        op.f("ix_job_work_challans_vendor_id"), "job_work_challans", ["vendor_id"], unique=False
    )
    op.create_table(
        "job_work_order_lines",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("company_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("job_work_order_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("line_number", sa.Integer(), nullable=False),
        sa.Column("item_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("uom_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("quantity_to_issue", sa.Numeric(precision=18, scale=4), nullable=False),
        sa.Column(
            "issued_quantity", sa.Numeric(precision=18, scale=4), server_default="0", nullable=False
        ),
        sa.Column(
            "received_quantity",
            sa.Numeric(precision=18, scale=4),
            server_default="0",
            nullable=False,
        ),
        sa.Column("expected_output_item_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("expected_output_quantity", sa.Numeric(precision=18, scale=4), nullable=False),
        sa.Column(
            "received_output_quantity",
            sa.Numeric(precision=18, scale=4),
            server_default="0",
            nullable=False,
        ),
        sa.Column(
            "rejected_quantity",
            sa.Numeric(precision=18, scale=4),
            server_default="0",
            nullable=False,
        ),
        sa.Column(
            "scrap_quantity", sa.Numeric(precision=18, scale=4), server_default="0", nullable=False
        ),
        sa.Column(
            "expected_loss_percentage",
            sa.Numeric(precision=9, scale=4),
            server_default="0",
            nullable=False,
        ),
        sa.Column("reservation_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("created_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("updated_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.CheckConstraint(
            "expected_loss_percentage >= 0 AND expected_loss_percentage <= 100",
            name=op.f("ck_job_work_order_lines_expected_loss_is_a_percentage"),
        ),
        sa.CheckConstraint(
            "expected_output_quantity > 0",
            name=op.f("ck_job_work_order_lines_expected_output_positive"),
        ),
        sa.CheckConstraint(
            "issued_quantity <= quantity_to_issue",
            name=op.f("ck_job_work_order_lines_issued_within_ordered"),
        ),
        sa.CheckConstraint(
            "issued_quantity >= 0", name=op.f("ck_job_work_order_lines_issued_non_negative")
        ),
        sa.CheckConstraint(
            "line_number > 0", name=op.f("ck_job_work_order_lines_line_number_positive")
        ),
        sa.CheckConstraint(
            "quantity_to_issue > 0", name=op.f("ck_job_work_order_lines_quantity_to_issue_positive")
        ),
        sa.CheckConstraint(
            "received_output_quantity >= 0",
            name=op.f("ck_job_work_order_lines_received_output_non_negative"),
        ),
        sa.CheckConstraint(
            "received_quantity <= issued_quantity",
            name=op.f("ck_job_work_order_lines_received_within_issued"),
        ),
        sa.CheckConstraint(
            "received_quantity >= 0", name=op.f("ck_job_work_order_lines_received_non_negative")
        ),
        sa.CheckConstraint(
            "rejected_quantity >= 0", name=op.f("ck_job_work_order_lines_rejected_non_negative")
        ),
        sa.CheckConstraint(
            "scrap_quantity <= received_quantity",
            name=op.f("ck_job_work_order_lines_scrap_within_received"),
        ),
        sa.CheckConstraint(
            "scrap_quantity >= 0", name=op.f("ck_job_work_order_lines_scrap_non_negative")
        ),
        sa.ForeignKeyConstraint(
            ["company_id"],
            ["companies.id"],
            name=op.f("fk_job_work_order_lines_company_id_companies"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_id"],
            ["users.id"],
            name=op.f("fk_job_work_order_lines_created_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["expected_output_item_id"],
            ["items.id"],
            name=op.f("fk_job_work_order_lines_expected_output_item_id_items"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["item_id"],
            ["items.id"],
            name=op.f("fk_job_work_order_lines_item_id_items"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["job_work_order_id"],
            ["job_work_orders.id"],
            name=op.f("fk_job_work_order_lines_job_work_order_id_job_work_orders"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["uom_id"],
            ["units_of_measure.id"],
            name=op.f("fk_job_work_order_lines_uom_id_units_of_measure"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["updated_by_id"],
            ["users.id"],
            name=op.f("fk_job_work_order_lines_updated_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_job_work_order_lines")),
        sa.UniqueConstraint(
            "job_work_order_id", "line_number", name="uq_job_work_order_lines_order_line_number"
        ),
    )
    op.create_index(
        op.f("ix_job_work_order_lines_company_id"),
        "job_work_order_lines",
        ["company_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_job_work_order_lines_created_at"),
        "job_work_order_lines",
        ["created_at"],
        unique=False,
    )
    op.create_index(
        op.f("ix_job_work_order_lines_expected_output_item_id"),
        "job_work_order_lines",
        ["expected_output_item_id"],
        unique=False,
    )
    op.create_index(
        "ix_job_work_order_lines_item",
        "job_work_order_lines",
        ["company_id", "item_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_job_work_order_lines_item_id"), "job_work_order_lines", ["item_id"], unique=False
    )
    op.create_index(
        op.f("ix_job_work_order_lines_job_work_order_id"),
        "job_work_order_lines",
        ["job_work_order_id"],
        unique=False,
    )
    op.create_index(
        "ix_job_work_order_lines_order",
        "job_work_order_lines",
        ["company_id", "job_work_order_id", "line_number"],
        unique=False,
    )
    op.create_index(
        "ix_job_work_order_lines_output_item",
        "job_work_order_lines",
        ["company_id", "expected_output_item_id"],
        unique=False,
    )
    op.create_table(
        "job_work_receipts",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("company_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("code", sa.String(length=80), nullable=False),
        sa.Column("receipt_number", sa.Integer(), nullable=False),
        sa.Column("job_work_order_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("vendor_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("receipt_date", sa.Date(), nullable=False),
        sa.Column("vendor_challan_number", sa.String(length=64), nullable=True),
        sa.Column("vendor_challan_date", sa.Date(), nullable=True),
        sa.Column(
            "total_output_quantity",
            sa.Numeric(precision=18, scale=4),
            server_default="0",
            nullable=False,
        ),
        sa.Column(
            "total_rejected_quantity",
            sa.Numeric(precision=18, scale=4),
            server_default="0",
            nullable=False,
        ),
        sa.Column(
            "total_returned_unprocessed",
            sa.Numeric(precision=18, scale=4),
            server_default="0",
            nullable=False,
        ),
        sa.Column(
            "total_scrap_quantity",
            sa.Numeric(precision=18, scale=4),
            server_default="0",
            nullable=False,
        ),
        sa.Column(
            "total_input_consumed",
            sa.Numeric(precision=18, scale=4),
            server_default="0",
            nullable=False,
        ),
        sa.Column(
            "total_charges", sa.Numeric(precision=18, scale=4), server_default="0", nullable=False
        ),
        sa.Column(
            "vendor_debit_amount",
            sa.Numeric(precision=18, scale=4),
            server_default="0",
            nullable=False,
        ),
        sa.Column("has_excess_loss", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("excess_loss_approved_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("excess_loss_approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("qc_held_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("qc_released_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("qc_inspection_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("reason", sa.String(length=255), nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("created_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("updated_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.CheckConstraint(
            "(excess_loss_approved_by_id IS NULL) = (excess_loss_approved_at IS NULL)",
            name=op.f("ck_job_work_receipts_excess_loss_approval_evidence_complete"),
        ),
        sa.CheckConstraint(
            "NOT has_excess_loss OR excess_loss_approved_by_id IS NOT NULL",
            name=op.f("ck_job_work_receipts_excess_loss_requires_approval"),
        ),
        sa.CheckConstraint(
            "qc_released_at IS NULL OR qc_held_at IS NOT NULL",
            name=op.f("ck_job_work_receipts_release_requires_a_hold"),
        ),
        sa.CheckConstraint(
            "receipt_number > 0", name=op.f("ck_job_work_receipts_receipt_number_positive")
        ),
        sa.CheckConstraint(
            "total_charges >= 0", name=op.f("ck_job_work_receipts_charges_non_negative")
        ),
        sa.CheckConstraint(
            "total_input_consumed > 0", name=op.f("ck_job_work_receipts_input_consumed_positive")
        ),
        sa.CheckConstraint(
            "total_output_quantity >= 0", name=op.f("ck_job_work_receipts_output_non_negative")
        ),
        sa.CheckConstraint(
            "total_rejected_quantity >= 0", name=op.f("ck_job_work_receipts_rejected_non_negative")
        ),
        sa.CheckConstraint(
            "total_returned_unprocessed >= 0",
            name=op.f("ck_job_work_receipts_returned_non_negative"),
        ),
        sa.CheckConstraint(
            "total_scrap_quantity >= 0", name=op.f("ck_job_work_receipts_scrap_non_negative")
        ),
        sa.CheckConstraint(
            "vendor_challan_date IS NULL OR vendor_challan_date <= receipt_date",
            name=op.f("ck_job_work_receipts_vendor_challan_not_after_receipt"),
        ),
        sa.CheckConstraint(
            "vendor_debit_amount >= 0", name=op.f("ck_job_work_receipts_vendor_debit_non_negative")
        ),
        sa.ForeignKeyConstraint(
            ["company_id"],
            ["companies.id"],
            name=op.f("fk_job_work_receipts_company_id_companies"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_id"],
            ["users.id"],
            name=op.f("fk_job_work_receipts_created_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["excess_loss_approved_by_id"],
            ["users.id"],
            name=op.f("fk_job_work_receipts_excess_loss_approved_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["job_work_order_id"],
            ["job_work_orders.id"],
            name=op.f("fk_job_work_receipts_job_work_order_id_job_work_orders"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["updated_by_id"],
            ["users.id"],
            name=op.f("fk_job_work_receipts_updated_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["vendor_id"],
            ["vendors.id"],
            name=op.f("fk_job_work_receipts_vendor_id_vendors"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_job_work_receipts")),
        sa.UniqueConstraint("company_id", "code", name="uq_job_work_receipts_company_id_code"),
        sa.UniqueConstraint(
            "job_work_order_id", "receipt_number", name="uq_job_work_receipts_order_receipt_number"
        ),
    )
    op.create_index(
        "ix_job_work_receipts_awaiting_inspection",
        "job_work_receipts",
        ["company_id", "qc_held_at"],
        unique=False,
        postgresql_where=sa.text("qc_held_at IS NOT NULL AND qc_released_at IS NULL"),
    )
    op.create_index(
        op.f("ix_job_work_receipts_company_id"), "job_work_receipts", ["company_id"], unique=False
    )
    op.create_index(
        op.f("ix_job_work_receipts_created_at"), "job_work_receipts", ["created_at"], unique=False
    )
    op.create_index(
        op.f("ix_job_work_receipts_job_work_order_id"),
        "job_work_receipts",
        ["job_work_order_id"],
        unique=False,
    )
    op.create_index(
        "ix_job_work_receipts_order",
        "job_work_receipts",
        ["company_id", "job_work_order_id", "receipt_date"],
        unique=False,
    )
    op.create_index(
        "ix_job_work_receipts_vendor_date",
        "job_work_receipts",
        ["company_id", "vendor_id", "receipt_date"],
        unique=False,
    )
    op.create_index(
        op.f("ix_job_work_receipts_vendor_id"), "job_work_receipts", ["vendor_id"], unique=False
    )
    op.create_table(
        "mrp_demands",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("company_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("demand_source", sa.String(length=24), nullable=False),
        sa.Column("source_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("source_line_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("source_code", sa.String(length=64), nullable=True),
        sa.Column("item_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("warehouse_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("quantity", sa.Numeric(precision=18, scale=4), nullable=False),
        sa.Column("required_date", sa.Date(), nullable=False),
        sa.Column("priority", sa.Integer(), server_default="5", nullable=False),
        sa.Column("notes", sa.String(length=255), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("created_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("updated_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.CheckConstraint(
            "(demand_source = 'MANUAL') = (source_id IS NULL)",
            name=op.f("ck_mrp_demands_source_id_matches_demand_source"),
        ),
        sa.CheckConstraint(
            "demand_source IN ('SALES_ORDER', 'MANUAL')",
            name=op.f("ck_mrp_demands_demand_source_known"),
        ),
        sa.CheckConstraint("priority >= 0", name=op.f("ck_mrp_demands_priority_non_negative")),
        sa.CheckConstraint("quantity > 0", name=op.f("ck_mrp_demands_quantity_positive")),
        sa.ForeignKeyConstraint(
            ["company_id"],
            ["companies.id"],
            name=op.f("fk_mrp_demands_company_id_companies"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_id"],
            ["users.id"],
            name=op.f("fk_mrp_demands_created_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["item_id"],
            ["items.id"],
            name=op.f("fk_mrp_demands_item_id_items"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["mrp_runs.id"],
            name=op.f("fk_mrp_demands_run_id_mrp_runs"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["updated_by_id"],
            ["users.id"],
            name=op.f("fk_mrp_demands_updated_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["warehouse_id"],
            ["warehouses.id"],
            name=op.f("fk_mrp_demands_warehouse_id_warehouses"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_mrp_demands")),
    )
    op.create_index(op.f("ix_mrp_demands_company_id"), "mrp_demands", ["company_id"], unique=False)
    op.create_index(op.f("ix_mrp_demands_created_at"), "mrp_demands", ["created_at"], unique=False)
    op.create_index(op.f("ix_mrp_demands_run_id"), "mrp_demands", ["run_id"], unique=False)
    op.create_index(
        "ix_mrp_demands_run_item", "mrp_demands", ["company_id", "run_id", "item_id"], unique=False
    )
    op.create_index(
        "ix_mrp_demands_source", "mrp_demands", ["company_id", "source_id"], unique=False
    )
    op.create_table(
        "mrp_requirements",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("company_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("item_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("item_code", sa.String(length=64), nullable=False),
        sa.Column("level", sa.Integer(), server_default="0", nullable=False),
        sa.Column(
            "direct_demand_quantity",
            sa.Numeric(precision=18, scale=4),
            server_default="0",
            nullable=False,
        ),
        sa.Column(
            "dependent_demand_quantity",
            sa.Numeric(precision=18, scale=4),
            server_default="0",
            nullable=False,
        ),
        sa.Column(
            "gross_requirement",
            sa.Numeric(precision=18, scale=4),
            server_default="0",
            nullable=False,
        ),
        sa.Column(
            "on_hand_quantity",
            sa.Numeric(precision=18, scale=4),
            server_default="0",
            nullable=False,
        ),
        sa.Column(
            "reserved_quantity",
            sa.Numeric(precision=18, scale=4),
            server_default="0",
            nullable=False,
        ),
        sa.Column(
            "available_quantity",
            sa.Numeric(precision=18, scale=4),
            server_default="0",
            nullable=False,
        ),
        sa.Column(
            "incoming_quantity",
            sa.Numeric(precision=18, scale=4),
            server_default="0",
            nullable=False,
        ),
        sa.Column(
            "in_production_quantity",
            sa.Numeric(precision=18, scale=4),
            server_default="0",
            nullable=False,
        ),
        sa.Column(
            "net_requirement", sa.Numeric(precision=18, scale=4), server_default="0", nullable=False
        ),
        sa.Column(
            "shortage_quantity",
            sa.Numeric(precision=18, scale=4),
            server_default="0",
            nullable=False,
        ),
        sa.Column("earliest_required_date", sa.Date(), nullable=False),
        sa.Column("source_demand_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("source_demands", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("created_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("updated_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.CheckConstraint(
            "available_quantity >= 0", name=op.f("ck_mrp_requirements_available_non_negative")
        ),
        sa.CheckConstraint(
            "dependent_demand_quantity >= 0",
            name=op.f("ck_mrp_requirements_dependent_demand_non_negative"),
        ),
        sa.CheckConstraint(
            "direct_demand_quantity >= 0",
            name=op.f("ck_mrp_requirements_direct_demand_non_negative"),
        ),
        sa.CheckConstraint(
            "gross_requirement = direct_demand_quantity + dependent_demand_quantity",
            name=op.f("ck_mrp_requirements_gross_is_direct_plus_dependent"),
        ),
        sa.CheckConstraint(
            "gross_requirement >= 0",
            name=op.f("ck_mrp_requirements_gross_requirement_non_negative"),
        ),
        sa.CheckConstraint(
            "in_production_quantity >= 0",
            name=op.f("ck_mrp_requirements_in_production_non_negative"),
        ),
        sa.CheckConstraint(
            "incoming_quantity >= 0", name=op.f("ck_mrp_requirements_incoming_non_negative")
        ),
        sa.CheckConstraint("level >= 0", name=op.f("ck_mrp_requirements_level_non_negative")),
        sa.CheckConstraint(
            "net_requirement <= gross_requirement",
            name=op.f("ck_mrp_requirements_net_within_gross"),
        ),
        sa.CheckConstraint(
            "net_requirement >= 0", name=op.f("ck_mrp_requirements_net_requirement_non_negative")
        ),
        sa.CheckConstraint(
            "on_hand_quantity >= 0", name=op.f("ck_mrp_requirements_on_hand_non_negative")
        ),
        sa.CheckConstraint(
            "reserved_quantity >= 0", name=op.f("ck_mrp_requirements_reserved_non_negative")
        ),
        sa.CheckConstraint(
            "shortage_quantity <= net_requirement",
            name=op.f("ck_mrp_requirements_shortage_within_net"),
        ),
        sa.CheckConstraint(
            "shortage_quantity >= 0", name=op.f("ck_mrp_requirements_shortage_non_negative")
        ),
        sa.CheckConstraint(
            "source_demand_count >= 0",
            name=op.f("ck_mrp_requirements_source_demand_count_non_negative"),
        ),
        sa.ForeignKeyConstraint(
            ["company_id"],
            ["companies.id"],
            name=op.f("fk_mrp_requirements_company_id_companies"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_id"],
            ["users.id"],
            name=op.f("fk_mrp_requirements_created_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["item_id"],
            ["items.id"],
            name=op.f("fk_mrp_requirements_item_id_items"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["mrp_runs.id"],
            name=op.f("fk_mrp_requirements_run_id_mrp_runs"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["updated_by_id"],
            ["users.id"],
            name=op.f("fk_mrp_requirements_updated_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_mrp_requirements")),
        sa.UniqueConstraint("run_id", "item_id", name="uq_mrp_requirements_run_id_item_id"),
    )
    op.create_index(
        op.f("ix_mrp_requirements_company_id"), "mrp_requirements", ["company_id"], unique=False
    )
    op.create_index(
        op.f("ix_mrp_requirements_created_at"), "mrp_requirements", ["created_at"], unique=False
    )
    op.create_index(
        "ix_mrp_requirements_item",
        "mrp_requirements",
        ["company_id", "item_id", "created_at"],
        unique=False,
    )
    op.create_index(
        op.f("ix_mrp_requirements_run_id"), "mrp_requirements", ["run_id"], unique=False
    )
    op.create_index(
        "ix_mrp_requirements_shortage",
        "mrp_requirements",
        ["company_id", "run_id", "earliest_required_date"],
        unique=False,
        postgresql_where=sa.text("shortage_quantity > 0"),
    )
    op.create_table(
        "mrp_suggestions",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("company_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("item_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("item_code", sa.String(length=64), nullable=False),
        sa.Column("suggestion_type", sa.String(length=24), nullable=False),
        sa.Column("required_quantity", sa.Numeric(precision=18, scale=4), nullable=False),
        sa.Column("suggested_quantity", sa.Numeric(precision=18, scale=4), nullable=False),
        sa.Column("shortage_quantity", sa.Numeric(precision=18, scale=4), nullable=False),
        sa.Column(
            "available_quantity",
            sa.Numeric(precision=18, scale=4),
            server_default="0",
            nullable=False,
        ),
        sa.Column(
            "incoming_quantity",
            sa.Numeric(precision=18, scale=4),
            server_default="0",
            nullable=False,
        ),
        sa.Column("required_date", sa.Date(), nullable=False),
        sa.Column("suggested_order_date", sa.Date(), nullable=False),
        sa.Column("is_late", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("lead_time_days", sa.Integer(), server_default="0", nullable=False),
        sa.Column("source_demands", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("status", sa.String(length=24), server_default="OPEN", nullable=False),
        sa.Column("converted_to_type", sa.String(length=48), nullable=True),
        sa.Column("converted_to_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("converted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ignored_reason", sa.String(length=255), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("created_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("updated_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.CheckConstraint(
            "converted_at IS NULL OR status = 'CONVERTED'",
            name=op.f("ck_mrp_suggestions_conversion_timestamp_implies_converted"),
        ),
        sa.CheckConstraint(
            "status <> 'CONVERTED' OR converted_to_type IS NOT NULL",
            name=op.f("ck_mrp_suggestions_converted_names_a_target"),
        ),
        sa.CheckConstraint(
            "status <> 'IGNORED' OR ignored_reason IS NOT NULL",
            name=op.f("ck_mrp_suggestions_ignored_carries_a_reason"),
        ),
        sa.CheckConstraint(
            "status IN ('OPEN', 'CONVERTED', 'IGNORED')",
            name=op.f("ck_mrp_suggestions_status_known"),
        ),
        sa.CheckConstraint(
            "suggestion_type IN ('MANUFACTURE', 'PURCHASE', 'JOB_WORK', 'TRANSFER')",
            name=op.f("ck_mrp_suggestions_suggestion_type_known"),
        ),
        sa.CheckConstraint(
            "available_quantity >= 0", name=op.f("ck_mrp_suggestions_available_non_negative")
        ),
        sa.CheckConstraint(
            "incoming_quantity >= 0", name=op.f("ck_mrp_suggestions_incoming_non_negative")
        ),
        sa.CheckConstraint(
            "lead_time_days >= 0", name=op.f("ck_mrp_suggestions_lead_time_non_negative")
        ),
        sa.CheckConstraint(
            "required_quantity > 0", name=op.f("ck_mrp_suggestions_required_quantity_positive")
        ),
        sa.CheckConstraint(
            "shortage_quantity > 0", name=op.f("ck_mrp_suggestions_shortage_quantity_positive")
        ),
        sa.CheckConstraint(
            "suggested_order_date <= required_date",
            name=op.f("ck_mrp_suggestions_order_date_not_after_required"),
        ),
        sa.CheckConstraint(
            "suggested_quantity > 0", name=op.f("ck_mrp_suggestions_suggested_quantity_positive")
        ),
        sa.CheckConstraint(
            "suggested_quantity >= required_quantity",
            name=op.f("ck_mrp_suggestions_suggested_covers_required"),
        ),
        sa.ForeignKeyConstraint(
            ["company_id"],
            ["companies.id"],
            name=op.f("fk_mrp_suggestions_company_id_companies"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_id"],
            ["users.id"],
            name=op.f("fk_mrp_suggestions_created_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["item_id"],
            ["items.id"],
            name=op.f("fk_mrp_suggestions_item_id_items"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["mrp_runs.id"],
            name=op.f("fk_mrp_suggestions_run_id_mrp_runs"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["updated_by_id"],
            ["users.id"],
            name=op.f("fk_mrp_suggestions_updated_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_mrp_suggestions")),
        sa.UniqueConstraint("run_id", "item_id", name="uq_mrp_suggestions_run_id_item_id"),
    )
    op.create_index(
        op.f("ix_mrp_suggestions_company_id"), "mrp_suggestions", ["company_id"], unique=False
    )
    op.create_index(
        op.f("ix_mrp_suggestions_created_at"), "mrp_suggestions", ["created_at"], unique=False
    )
    op.create_index(
        "ix_mrp_suggestions_open",
        "mrp_suggestions",
        ["company_id", "suggestion_type", "suggested_order_date"],
        unique=False,
        postgresql_where=sa.text("status = 'OPEN'"),
    )
    op.create_index(op.f("ix_mrp_suggestions_run_id"), "mrp_suggestions", ["run_id"], unique=False)
    op.create_index(
        "ix_mrp_suggestions_run_status",
        "mrp_suggestions",
        ["company_id", "run_id", "status", "suggested_order_date"],
        unique=False,
    )
    op.create_table(
        "physical_counts",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("company_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("code", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=24), server_default="DRAFT", nullable=False),
        sa.Column("warehouse_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("location_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("count_date", sa.Date(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("counted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("confirmed_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("line_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("variance_line_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column(
            "variance_value", sa.Numeric(precision=18, scale=4), server_default="0", nullable=False
        ),
        sa.Column("reason", sa.String(length=255), nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("created_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("updated_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.CheckConstraint(
            "line_count >= 0", name=op.f("ck_physical_counts_line_count_non_negative")
        ),
        sa.CheckConstraint(
            "variance_line_count >= 0",
            name=op.f("ck_physical_counts_variance_line_count_non_negative"),
        ),
        sa.ForeignKeyConstraint(
            ["company_id"],
            ["companies.id"],
            name=op.f("fk_physical_counts_company_id_companies"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["confirmed_by_id"],
            ["users.id"],
            name=op.f("fk_physical_counts_confirmed_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_id"],
            ["users.id"],
            name=op.f("fk_physical_counts_created_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["location_id"],
            ["storage_locations.id"],
            name=op.f("fk_physical_counts_location_id_storage_locations"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["updated_by_id"],
            ["users.id"],
            name=op.f("fk_physical_counts_updated_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["warehouse_id"],
            ["warehouses.id"],
            name=op.f("fk_physical_counts_warehouse_id_warehouses"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_physical_counts")),
        sa.UniqueConstraint("company_id", "code", name="uq_physical_counts_company_id_code"),
    )
    op.create_index(
        op.f("ix_physical_counts_company_id"), "physical_counts", ["company_id"], unique=False
    )
    op.create_index(
        op.f("ix_physical_counts_created_at"), "physical_counts", ["created_at"], unique=False
    )
    op.create_index(
        "ix_physical_counts_status",
        "physical_counts",
        ["company_id", "status", "count_date"],
        unique=False,
    )
    op.create_index(
        "ix_physical_counts_warehouse",
        "physical_counts",
        ["company_id", "warehouse_id", "count_date"],
        unique=False,
    )
    op.create_table(
        "purchase_requisition_lines",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("company_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("requisition_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("line_number", sa.Integer(), nullable=False),
        sa.Column("item_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("description", sa.String(length=500), nullable=True),
        sa.Column("quantity", sa.Numeric(precision=18, scale=4), nullable=False),
        sa.Column("uom_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("required_date", sa.Date(), nullable=True),
        sa.Column("estimated_unit_price", sa.Numeric(precision=18, scale=6), nullable=True),
        sa.Column("suggested_vendor_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "ordered_quantity",
            sa.Numeric(precision=18, scale=4),
            server_default="0",
            nullable=False,
        ),
        sa.Column("status", sa.String(length=24), server_default="PENDING", nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("created_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("updated_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('PENDING', 'PARTIALLY_ORDERED', 'ORDERED', 'CANCELLED')",
            name=op.f("ck_purchase_requisition_lines_status_known"),
        ),
        sa.CheckConstraint(
            "estimated_unit_price IS NULL OR estimated_unit_price >= 0",
            name=op.f("ck_purchase_requisition_lines_estimated_unit_price_non_negative"),
        ),
        sa.CheckConstraint(
            "line_number > 0", name=op.f("ck_purchase_requisition_lines_line_number_positive")
        ),
        sa.CheckConstraint(
            "ordered_quantity >= 0",
            name=op.f("ck_purchase_requisition_lines_ordered_quantity_non_negative"),
        ),
        sa.CheckConstraint(
            "quantity > 0", name=op.f("ck_purchase_requisition_lines_quantity_positive")
        ),
        sa.ForeignKeyConstraint(
            ["company_id"],
            ["companies.id"],
            name=op.f("fk_purchase_requisition_lines_company_id_companies"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_id"],
            ["users.id"],
            name=op.f("fk_purchase_requisition_lines_created_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["item_id"],
            ["items.id"],
            name=op.f("fk_purchase_requisition_lines_item_id_items"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["requisition_id"],
            ["purchase_requisitions.id"],
            name=op.f("fk_purchase_requisition_lines_requisition_id_purchase_requisitions"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["suggested_vendor_id"],
            ["vendors.id"],
            name=op.f("fk_purchase_requisition_lines_suggested_vendor_id_vendors"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["uom_id"],
            ["units_of_measure.id"],
            name=op.f("fk_purchase_requisition_lines_uom_id_units_of_measure"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["updated_by_id"],
            ["users.id"],
            name=op.f("fk_purchase_requisition_lines_updated_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_purchase_requisition_lines")),
        sa.UniqueConstraint(
            "requisition_id", "line_number", name="uq_purchase_requisition_lines_line_number"
        ),
    )
    op.create_index(
        op.f("ix_purchase_requisition_lines_company_id"),
        "purchase_requisition_lines",
        ["company_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_purchase_requisition_lines_created_at"),
        "purchase_requisition_lines",
        ["created_at"],
        unique=False,
    )
    op.create_index(
        "ix_purchase_requisition_lines_item",
        "purchase_requisition_lines",
        ["company_id", "item_id", "status"],
        unique=False,
    )
    op.create_index(
        op.f("ix_purchase_requisition_lines_item_id"),
        "purchase_requisition_lines",
        ["item_id"],
        unique=False,
    )
    op.create_index(
        "ix_purchase_requisition_lines_requisition",
        "purchase_requisition_lines",
        ["company_id", "requisition_id", "line_number"],
        unique=False,
    )
    op.create_index(
        op.f("ix_purchase_requisition_lines_requisition_id"),
        "purchase_requisition_lines",
        ["requisition_id"],
        unique=False,
    )
    op.create_table(
        "qc_templates",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("company_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("code", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("item_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("item_category_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("qc_stage", sa.String(length=24), nullable=False),
        sa.Column("sampling_plan", sa.String(length=16), server_default="ALL", nullable=False),
        sa.Column("sample_percentage", sa.Numeric(precision=9, scale=4), nullable=True),
        sa.Column("sample_size", sa.Integer(), nullable=True),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("updated_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.CheckConstraint(
            "qc_stage IN ('INCOMING', 'JOB_WORK', 'IN_PROCESS', 'ASSEMBLY', 'FINAL', 'RETURN')",
            name=op.f("ck_qc_templates_qc_stage_vocabulary"),
        ),
        sa.CheckConstraint(
            "sampling_plan <> 'FIXED_SAMPLE' OR (sample_size IS NOT NULL AND sample_size >= 1)",
            name=op.f("ck_qc_templates_fixed_plan_needs_sample_size"),
        ),
        sa.CheckConstraint(
            "sampling_plan <> 'PERCENTAGE' OR (sample_percentage IS NOT NULL AND sample_percentage > 0     AND sample_percentage <= 100)",
            name=op.f("ck_qc_templates_percentage_plan_needs_percentage"),
        ),
        sa.CheckConstraint(
            "sampling_plan IN ('ALL', 'PERCENTAGE', 'FIXED_SAMPLE', 'AQL')",
            name=op.f("ck_qc_templates_sampling_plan_vocabulary"),
        ),
        sa.CheckConstraint(
            "item_id IS NULL OR item_category_id IS NULL",
            name=op.f("ck_qc_templates_scope_is_item_or_category"),
        ),
        sa.CheckConstraint("version >= 1", name=op.f("ck_qc_templates_version_positive")),
        sa.ForeignKeyConstraint(
            ["company_id"],
            ["companies.id"],
            name=op.f("fk_qc_templates_company_id_companies"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_id"],
            ["users.id"],
            name=op.f("fk_qc_templates_created_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["item_category_id"],
            ["item_categories.id"],
            name=op.f("fk_qc_templates_item_category_id_item_categories"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["item_id"],
            ["items.id"],
            name=op.f("fk_qc_templates_item_id_items"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["updated_by_id"],
            ["users.id"],
            name=op.f("fk_qc_templates_updated_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_qc_templates")),
        sa.UniqueConstraint(
            "company_id", "code", "version", name="uq_qc_templates_company_id_code_version"
        ),
    )
    op.create_index(
        op.f("ix_qc_templates_company_id"), "qc_templates", ["company_id"], unique=False
    )
    op.create_index(
        op.f("ix_qc_templates_created_at"), "qc_templates", ["created_at"], unique=False
    )
    op.create_index(
        op.f("ix_qc_templates_deleted_at"), "qc_templates", ["deleted_at"], unique=False
    )
    op.create_index(
        "ix_qc_templates_stage",
        "qc_templates",
        ["company_id", "qc_stage", "is_active"],
        unique=False,
    )
    op.create_index(
        "uq_qc_templates_active_category_stage",
        "qc_templates",
        ["company_id", "item_category_id", "qc_stage"],
        unique=True,
        postgresql_where=sa.text(
            "is_active AND deleted_at IS NULL AND item_category_id IS NOT NULL"
        ),
    )
    op.create_index(
        "uq_qc_templates_active_item_stage",
        "qc_templates",
        ["company_id", "item_id", "qc_stage"],
        unique=True,
        postgresql_where=sa.text("is_active AND deleted_at IS NULL AND item_id IS NOT NULL"),
    )
    op.create_index(
        "uq_qc_templates_active_stage_default",
        "qc_templates",
        ["company_id", "qc_stage"],
        unique=True,
        postgresql_where=sa.text(
            "is_active AND deleted_at IS NULL AND item_id IS NULL AND item_category_id IS NULL"
        ),
    )
    op.create_table(
        "rfqs",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("company_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("code", sa.String(length=64), nullable=False),
        sa.Column("rfq_date", sa.Date(), nullable=False),
        sa.Column("response_due_date", sa.Date(), nullable=True),
        sa.Column("required_date", sa.Date(), nullable=True),
        sa.Column("requisition_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("warehouse_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "vendor_ids",
            postgresql.ARRAY(postgresql.UUID(as_uuid=True)),
            server_default=sa.text("'{}'::uuid[]"),
            nullable=False,
        ),
        sa.Column("status", sa.String(length=24), server_default="DRAFT", nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancellation_reason", sa.Text(), nullable=True),
        sa.Column("currency", sa.String(length=3), server_default="INR", nullable=False),
        sa.Column("terms", sa.Text(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("created_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("updated_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.CheckConstraint(
            "status <> 'CANCELLED' OR cancellation_reason IS NOT NULL",
            name=op.f("ck_rfqs_cancelled_rfqs_carry_a_reason"),
        ),
        sa.CheckConstraint(
            "status <> 'SENT' OR array_length(vendor_ids, 1) >= 1",
            name=op.f("ck_rfqs_sent_rfqs_have_vendors"),
        ),
        sa.CheckConstraint(
            "status IN ('DRAFT', 'SENT', 'CLOSED', 'CANCELLED')", name=op.f("ck_rfqs_status_known")
        ),
        sa.CheckConstraint("length(currency) = 3", name=op.f("ck_rfqs_currency_is_iso_code")),
        sa.CheckConstraint(
            "response_due_date IS NULL OR response_due_date >= rfq_date",
            name=op.f("ck_rfqs_response_due_not_before_rfq_date"),
        ),
        sa.ForeignKeyConstraint(
            ["company_id"],
            ["companies.id"],
            name=op.f("fk_rfqs_company_id_companies"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_id"],
            ["users.id"],
            name=op.f("fk_rfqs_created_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["requisition_id"],
            ["purchase_requisitions.id"],
            name=op.f("fk_rfqs_requisition_id_purchase_requisitions"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["updated_by_id"],
            ["users.id"],
            name=op.f("fk_rfqs_updated_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["warehouse_id"],
            ["warehouses.id"],
            name=op.f("fk_rfqs_warehouse_id_warehouses"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_rfqs")),
        sa.UniqueConstraint("company_id", "code", name="uq_rfqs_company_id_code"),
    )
    op.create_index(op.f("ix_rfqs_company_id"), "rfqs", ["company_id"], unique=False)
    op.create_index(op.f("ix_rfqs_created_at"), "rfqs", ["created_at"], unique=False)
    op.create_index("ix_rfqs_requisition", "rfqs", ["company_id", "requisition_id"], unique=False)
    op.create_index(op.f("ix_rfqs_requisition_id"), "rfqs", ["requisition_id"], unique=False)
    op.create_index(
        "ix_rfqs_status_due", "rfqs", ["company_id", "status", "response_due_date"], unique=False
    )
    op.create_index(op.f("ix_rfqs_warehouse_id"), "rfqs", ["warehouse_id"], unique=False)
    op.create_table(
        "routings",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("company_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("code", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=True),
        sa.Column("item_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("status", sa.String(length=24), server_default="DRAFT", nullable=False),
        sa.Column("is_default", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("effective_from", sa.Date(), nullable=False),
        sa.Column("effective_to", sa.Date(), nullable=True),
        sa.Column("approved_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("row_version", sa.Integer(), server_default="1", nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("created_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("updated_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.CheckConstraint(
            "(status <> 'APPROVED') OR (approved_by_id IS NOT NULL AND approved_at IS NOT NULL)",
            name=op.f("ck_routings_approved_rows_carry_approval_evidence"),
        ),
        sa.CheckConstraint(
            "effective_to IS NULL OR effective_to >= effective_from",
            name=op.f("ck_routings_effectivity_window_ordered"),
        ),
        sa.CheckConstraint("version > 0", name=op.f("ck_routings_version_positive")),
        sa.ForeignKeyConstraint(
            ["approved_by_id"],
            ["users.id"],
            name=op.f("fk_routings_approved_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["company_id"],
            ["companies.id"],
            name=op.f("fk_routings_company_id_companies"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_id"],
            ["users.id"],
            name=op.f("fk_routings_created_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["item_id"], ["items.id"], name=op.f("fk_routings_item_id_items"), ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["updated_by_id"],
            ["users.id"],
            name=op.f("fk_routings_updated_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_routings")),
        sa.UniqueConstraint("company_id", "code", name="uq_routings_company_id_code"),
        sa.UniqueConstraint(
            "company_id", "item_id", "version", name="uq_routings_company_id_item_version"
        ),
    )
    op.create_index(op.f("ix_routings_company_id"), "routings", ["company_id"], unique=False)
    op.create_index(
        "ix_routings_company_status",
        "routings",
        ["company_id", "status", "created_at"],
        unique=False,
    )
    op.create_index(op.f("ix_routings_created_at"), "routings", ["created_at"], unique=False)
    op.create_index(
        "ix_routings_effective_lookup",
        "routings",
        ["company_id", "item_id", "effective_from", "effective_to"],
        unique=False,
        postgresql_where=sa.text("status = 'APPROVED'"),
    )
    op.create_index(op.f("ix_routings_item_id"), "routings", ["item_id"], unique=False)
    op.create_index(
        "uq_routings_one_default_approved_per_item",
        "routings",
        ["company_id", "item_id"],
        unique=True,
        postgresql_where=sa.text("is_default AND status = 'APPROVED'"),
    )
    op.create_table(
        "sales_orders",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("company_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("code", sa.String(length=64), nullable=False),
        sa.Column("customer_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("customer_po_number", sa.String(length=64), nullable=True),
        sa.Column("order_date", sa.Date(), nullable=False),
        sa.Column("required_date", sa.Date(), nullable=False),
        sa.Column("promised_date", sa.Date(), nullable=True),
        sa.Column("branch_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("warehouse_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("status", sa.String(length=24), server_default="DRAFT", nullable=False),
        sa.Column("priority", sa.Integer(), server_default="5", nullable=False),
        sa.Column("is_urgent", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("currency", sa.String(length=3), server_default="INR", nullable=False),
        sa.Column(
            "subtotal", sa.Numeric(precision=18, scale=4), server_default="0", nullable=False
        ),
        sa.Column(
            "discount_amount", sa.Numeric(precision=18, scale=4), server_default="0", nullable=False
        ),
        sa.Column(
            "tax_amount", sa.Numeric(precision=18, scale=4), server_default="0", nullable=False
        ),
        sa.Column(
            "total_amount", sa.Numeric(precision=18, scale=4), server_default="0", nullable=False
        ),
        sa.Column("place_of_supply_state_code", sa.String(length=4), nullable=True),
        sa.Column("is_interstate", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("billing_address_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("shipping_address_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("transporter_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "requires_approval", sa.Boolean(), server_default=sa.text("false"), nullable=False
        ),
        sa.Column("approved_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancellation_reason", sa.Text(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("created_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("updated_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.CheckConstraint(
            "status <> 'CANCELLED' OR cancellation_reason IS NOT NULL",
            name=op.f("ck_sales_orders_cancelled_orders_carry_a_reason"),
        ),
        sa.CheckConstraint(
            "status IN ('DRAFT', 'CONFIRMED', 'ALLOCATED', 'PARTIALLY_DISPATCHED', 'DISPATCHED', 'INVOICED', 'CLOSED', 'CANCELLED')",
            name=op.f("ck_sales_orders_status_known"),
        ),
        sa.CheckConstraint(
            "(approved_by_id IS NULL) = (approved_at IS NULL)",
            name=op.f("ck_sales_orders_approval_evidence_complete"),
        ),
        sa.CheckConstraint(
            "discount_amount >= 0", name=op.f("ck_sales_orders_discount_non_negative")
        ),
        sa.CheckConstraint(
            "length(currency) = 3", name=op.f("ck_sales_orders_currency_is_iso_code")
        ),
        sa.CheckConstraint(
            "priority BETWEEN 1 AND 9", name=op.f("ck_sales_orders_priority_in_range")
        ),
        sa.CheckConstraint(
            "promised_date IS NULL OR promised_date >= order_date",
            name=op.f("ck_sales_orders_promised_date_not_before_order"),
        ),
        sa.CheckConstraint(
            "required_date >= order_date",
            name=op.f("ck_sales_orders_required_date_not_before_order"),
        ),
        sa.CheckConstraint("subtotal >= 0", name=op.f("ck_sales_orders_subtotal_non_negative")),
        sa.CheckConstraint("tax_amount >= 0", name=op.f("ck_sales_orders_tax_non_negative")),
        sa.CheckConstraint("total_amount >= 0", name=op.f("ck_sales_orders_total_non_negative")),
        sa.ForeignKeyConstraint(
            ["approved_by_id"],
            ["users.id"],
            name=op.f("fk_sales_orders_approved_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["billing_address_id"],
            ["customer_addresses.id"],
            name=op.f("fk_sales_orders_billing_address_id_customer_addresses"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["branch_id"],
            ["branches.id"],
            name=op.f("fk_sales_orders_branch_id_branches"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["company_id"],
            ["companies.id"],
            name=op.f("fk_sales_orders_company_id_companies"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_id"],
            ["users.id"],
            name=op.f("fk_sales_orders_created_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["customer_id"],
            ["customers.id"],
            name=op.f("fk_sales_orders_customer_id_customers"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["shipping_address_id"],
            ["customer_addresses.id"],
            name=op.f("fk_sales_orders_shipping_address_id_customer_addresses"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["updated_by_id"],
            ["users.id"],
            name=op.f("fk_sales_orders_updated_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["warehouse_id"],
            ["warehouses.id"],
            name=op.f("fk_sales_orders_warehouse_id_warehouses"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_sales_orders")),
        sa.UniqueConstraint("company_id", "code", name="uq_sales_orders_company_id_code"),
    )
    op.create_index(op.f("ix_sales_orders_branch_id"), "sales_orders", ["branch_id"], unique=False)
    op.create_index(
        op.f("ix_sales_orders_company_id"), "sales_orders", ["company_id"], unique=False
    )
    op.create_index(
        "ix_sales_orders_company_status_required",
        "sales_orders",
        ["company_id", "status", "required_date"],
        unique=False,
    )
    op.create_index(
        op.f("ix_sales_orders_created_at"), "sales_orders", ["created_at"], unique=False
    )
    op.create_index(
        "ix_sales_orders_customer",
        "sales_orders",
        ["company_id", "customer_id", "order_date"],
        unique=False,
    )
    op.create_index(
        op.f("ix_sales_orders_customer_id"), "sales_orders", ["customer_id"], unique=False
    )
    op.create_index(
        "ix_sales_orders_open_demand",
        "sales_orders",
        ["company_id", "required_date", "warehouse_id"],
        unique=False,
        postgresql_where=sa.text("status IN ('CONFIRMED', 'ALLOCATED', 'PARTIALLY_DISPATCHED')"),
    )
    op.create_index(
        op.f("ix_sales_orders_warehouse_id"), "sales_orders", ["warehouse_id"], unique=False
    )
    op.create_index(
        "ix_sales_orders_warehouse_status",
        "sales_orders",
        ["company_id", "warehouse_id", "status"],
        unique=False,
    )
    op.create_table(
        "stock_batches",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("company_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("item_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("batch_number", sa.String(length=64), nullable=False),
        sa.Column("supplier_batch_number", sa.String(length=64), nullable=True),
        sa.Column("vendor_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("manufactured_on", sa.Date(), nullable=True),
        sa.Column("expires_on", sa.Date(), nullable=True),
        sa.Column("received_on", sa.Date(), nullable=False),
        sa.Column(
            "initial_quantity",
            sa.Numeric(precision=18, scale=4),
            server_default="0",
            nullable=False,
        ),
        sa.Column(
            "unit_cost", sa.Numeric(precision=18, scale=6), server_default="0", nullable=False
        ),
        sa.Column("status", sa.String(length=24), server_default="ACTIVE", nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("created_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("updated_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.CheckConstraint(
            "expires_on IS NULL OR manufactured_on IS NULL OR expires_on >= manufactured_on",
            name=op.f("ck_stock_batches_expiry_after_manufacture"),
        ),
        sa.CheckConstraint(
            "initial_quantity >= 0", name=op.f("ck_stock_batches_initial_quantity_non_negative")
        ),
        sa.CheckConstraint("unit_cost >= 0", name=op.f("ck_stock_batches_unit_cost_non_negative")),
        sa.ForeignKeyConstraint(
            ["company_id"],
            ["companies.id"],
            name=op.f("fk_stock_batches_company_id_companies"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_id"],
            ["users.id"],
            name=op.f("fk_stock_batches_created_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["item_id"],
            ["items.id"],
            name=op.f("fk_stock_batches_item_id_items"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["updated_by_id"],
            ["users.id"],
            name=op.f("fk_stock_batches_updated_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["vendor_id"],
            ["vendors.id"],
            name=op.f("fk_stock_batches_vendor_id_vendors"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_stock_batches")),
        sa.UniqueConstraint(
            "company_id", "item_id", "batch_number", name="uq_stock_batches_company_item_number"
        ),
    )
    op.create_index(
        op.f("ix_stock_batches_company_id"), "stock_batches", ["company_id"], unique=False
    )
    op.create_index(
        op.f("ix_stock_batches_created_at"), "stock_batches", ["created_at"], unique=False
    )
    op.create_index(
        "ix_stock_batches_expiry",
        "stock_batches",
        ["company_id", "item_id", "expires_on"],
        unique=False,
        postgresql_where=sa.text("status = 'ACTIVE'"),
    )
    op.create_index(
        "ix_stock_batches_number", "stock_batches", ["company_id", "batch_number"], unique=False
    )
    op.create_table(
        "assembly_orders",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("company_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("code", sa.String(length=64), nullable=False),
        sa.Column("item_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("bom_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("planned_quantity", sa.Numeric(precision=18, scale=4), nullable=False),
        sa.Column(
            "assembled_quantity",
            sa.Numeric(precision=18, scale=4),
            server_default="0",
            nullable=False,
        ),
        sa.Column(
            "rejected_quantity",
            sa.Numeric(precision=18, scale=4),
            server_default="0",
            nullable=False,
        ),
        sa.Column("warehouse_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("output_warehouse_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("assembly_line", sa.String(length=64), nullable=True),
        sa.Column("planned_start", sa.DateTime(timezone=True), nullable=True),
        sa.Column("planned_end", sa.DateTime(timezone=True), nullable=True),
        sa.Column("actual_start", sa.DateTime(timezone=True), nullable=True),
        sa.Column("actual_end", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(length=24), server_default="DRAFT", nullable=False),
        sa.Column("approved_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("requires_qc", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("qc_held_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("qc_inspection_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("packing_order_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("packed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("source_type", sa.String(length=24), server_default="MANUAL", nullable=False),
        sa.Column("source_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("cancellation_reason", sa.Text(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("created_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("updated_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.CheckConstraint(
            "source_type IN ('MANUAL', 'SALES_ORDER', 'MRP_SUGGESTION', 'MRP_RUN', 'PRODUCTION_ORDER', 'STOCK_REPLENISHMENT')",
            name=op.f("ck_assembly_orders_source_type_known"),
        ),
        sa.CheckConstraint(
            "status <> 'CANCELLED' OR cancellation_reason IS NOT NULL",
            name=op.f("ck_assembly_orders_cancelled_orders_carry_a_reason"),
        ),
        sa.CheckConstraint(
            "status IN ('DRAFT', 'APPROVED', 'RELEASED', 'COMPONENTS_ISSUED', 'IN_PROGRESS', 'QC_PENDING', 'COMPLETED', 'CLOSED', 'CANCELLED')",
            name=op.f("ck_assembly_orders_status_known"),
        ),
        sa.CheckConstraint(
            "(approved_by_id IS NULL) = (approved_at IS NULL)",
            name=op.f("ck_assembly_orders_approval_evidence_complete"),
        ),
        sa.CheckConstraint(
            "(packing_order_id IS NULL) = (packed_at IS NULL)",
            name=op.f("ck_assembly_orders_packing_handover_complete"),
        ),
        sa.CheckConstraint(
            "actual_end IS NULL OR actual_start IS NULL OR actual_end >= actual_start",
            name=op.f("ck_assembly_orders_actual_end_not_before_start"),
        ),
        sa.CheckConstraint(
            "assembled_quantity + rejected_quantity <= planned_quantity",
            name=op.f("ck_assembly_orders_output_within_planned_quantity"),
        ),
        sa.CheckConstraint(
            "assembled_quantity >= 0", name=op.f("ck_assembly_orders_assembled_non_negative")
        ),
        sa.CheckConstraint(
            "planned_end IS NULL OR planned_start IS NULL OR planned_end >= planned_start",
            name=op.f("ck_assembly_orders_planned_end_not_before_start"),
        ),
        sa.CheckConstraint(
            "planned_quantity > 0", name=op.f("ck_assembly_orders_planned_quantity_positive")
        ),
        sa.CheckConstraint(
            "rejected_quantity >= 0", name=op.f("ck_assembly_orders_rejected_non_negative")
        ),
        sa.CheckConstraint(
            "requires_qc OR (qc_held_at IS NULL AND qc_inspection_id IS NULL)",
            name=op.f("ck_assembly_orders_qc_evidence_requires_qc_flag"),
        ),
        sa.ForeignKeyConstraint(
            ["approved_by_id"],
            ["users.id"],
            name=op.f("fk_assembly_orders_approved_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["bom_id"],
            ["boms.id"],
            name=op.f("fk_assembly_orders_bom_id_boms"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["company_id"],
            ["companies.id"],
            name=op.f("fk_assembly_orders_company_id_companies"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_id"],
            ["users.id"],
            name=op.f("fk_assembly_orders_created_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["item_id"],
            ["items.id"],
            name=op.f("fk_assembly_orders_item_id_items"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["output_warehouse_id"],
            ["warehouses.id"],
            name=op.f("fk_assembly_orders_output_warehouse_id_warehouses"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["updated_by_id"],
            ["users.id"],
            name=op.f("fk_assembly_orders_updated_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["warehouse_id"],
            ["warehouses.id"],
            name=op.f("fk_assembly_orders_warehouse_id_warehouses"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_assembly_orders")),
        sa.UniqueConstraint("company_id", "code", name="uq_assembly_orders_company_id_code"),
    )
    op.create_index(
        "ix_assembly_orders_awaiting_inspection",
        "assembly_orders",
        ["company_id", "qc_held_at"],
        unique=False,
        postgresql_where=sa.text("qc_held_at IS NOT NULL AND qc_inspection_id IS NULL"),
    )
    op.create_index(op.f("ix_assembly_orders_bom_id"), "assembly_orders", ["bom_id"], unique=False)
    op.create_index(
        op.f("ix_assembly_orders_company_id"), "assembly_orders", ["company_id"], unique=False
    )
    op.create_index(
        "ix_assembly_orders_company_status_planned",
        "assembly_orders",
        ["company_id", "status", "planned_start"],
        unique=False,
    )
    op.create_index(
        op.f("ix_assembly_orders_created_at"), "assembly_orders", ["created_at"], unique=False
    )
    op.create_index(
        op.f("ix_assembly_orders_item_id"), "assembly_orders", ["item_id"], unique=False
    )
    op.create_index(
        "ix_assembly_orders_item_status",
        "assembly_orders",
        ["company_id", "item_id", "status"],
        unique=False,
    )
    op.create_index(
        "ix_assembly_orders_open_supply",
        "assembly_orders",
        ["company_id", "item_id", "planned_end"],
        unique=False,
        postgresql_where=sa.text(
            "status IN ('APPROVED', 'RELEASED', 'COMPONENTS_ISSUED', 'IN_PROGRESS', 'QC_PENDING')"
        ),
    )
    op.create_index(
        op.f("ix_assembly_orders_output_warehouse_id"),
        "assembly_orders",
        ["output_warehouse_id"],
        unique=False,
    )
    op.create_index(
        "ix_assembly_orders_ready_for_packing",
        "assembly_orders",
        ["company_id", "output_warehouse_id", "item_id"],
        unique=False,
        postgresql_where=sa.text("status = 'COMPLETED' AND packing_order_id IS NULL"),
    )
    op.create_index(
        "ix_assembly_orders_source",
        "assembly_orders",
        ["company_id", "source_type", "source_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_assembly_orders_warehouse_id"), "assembly_orders", ["warehouse_id"], unique=False
    )
    op.create_index(
        "ix_assembly_orders_warehouse_status",
        "assembly_orders",
        ["company_id", "warehouse_id", "status"],
        unique=False,
    )
    op.create_table(
        "bom_lines",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("company_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("bom_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("parent_item_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("line_number", sa.Integer(), nullable=False),
        sa.Column("component_item_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("quantity_per", sa.Numeric(precision=18, scale=4), nullable=False),
        sa.Column("uom_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "scrap_percentage", sa.Numeric(precision=9, scale=4), server_default="0", nullable=False
        ),
        sa.Column("is_optional", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("substitute_for_line_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("operation_sequence", sa.Integer(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("created_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("updated_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.CheckConstraint(
            "component_item_id <> parent_item_id",
            name=op.f("ck_bom_lines_component_is_not_the_parent_item"),
        ),
        sa.CheckConstraint("line_number > 0", name=op.f("ck_bom_lines_line_number_positive")),
        sa.CheckConstraint(
            "operation_sequence IS NULL OR operation_sequence > 0",
            name=op.f("ck_bom_lines_operation_sequence_positive"),
        ),
        sa.CheckConstraint("quantity_per > 0", name=op.f("ck_bom_lines_quantity_per_positive")),
        sa.CheckConstraint(
            "scrap_percentage >= 0 AND scrap_percentage < 100",
            name=op.f("ck_bom_lines_scrap_percentage_bounded"),
        ),
        sa.CheckConstraint(
            "substitute_for_line_id IS NULL OR substitute_for_line_id <> id",
            name=op.f("ck_bom_lines_substitute_not_self"),
        ),
        sa.ForeignKeyConstraint(
            ["bom_id", "parent_item_id"],
            ["boms.id", "boms.item_id"],
            name="fk_bom_lines_bom_id_parent_item_id_boms",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["company_id"],
            ["companies.id"],
            name=op.f("fk_bom_lines_company_id_companies"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["component_item_id"],
            ["items.id"],
            name=op.f("fk_bom_lines_component_item_id_items"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_id"],
            ["users.id"],
            name=op.f("fk_bom_lines_created_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["substitute_for_line_id"],
            ["bom_lines.id"],
            name=op.f("fk_bom_lines_substitute_for_line_id_bom_lines"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["uom_id"],
            ["units_of_measure.id"],
            name=op.f("fk_bom_lines_uom_id_units_of_measure"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["updated_by_id"],
            ["users.id"],
            name=op.f("fk_bom_lines_updated_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_bom_lines")),
        sa.UniqueConstraint("bom_id", "line_number", name="uq_bom_lines_bom_id_line_number"),
    )
    op.create_index(
        "ix_bom_lines_bom", "bom_lines", ["company_id", "bom_id", "line_number"], unique=False
    )
    op.create_index(op.f("ix_bom_lines_company_id"), "bom_lines", ["company_id"], unique=False)
    op.create_index(
        "ix_bom_lines_component", "bom_lines", ["company_id", "component_item_id"], unique=False
    )
    op.create_index(
        op.f("ix_bom_lines_component_item_id"), "bom_lines", ["component_item_id"], unique=False
    )
    op.create_index(op.f("ix_bom_lines_created_at"), "bom_lines", ["created_at"], unique=False)
    op.create_table(
        "bom_operations",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("company_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("bom_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("operation_sequence", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("routing_operation_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("work_center_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "setup_time_minutes",
            sa.Numeric(precision=18, scale=4),
            server_default="0",
            nullable=False,
        ),
        sa.Column(
            "run_time_minutes_per_unit",
            sa.Numeric(precision=18, scale=4),
            server_default="0",
            nullable=False,
        ),
        sa.Column(
            "cost_per_hour", sa.Numeric(precision=18, scale=4), server_default="0", nullable=False
        ),
        sa.Column(
            "fixed_cost", sa.Numeric(precision=18, scale=4), server_default="0", nullable=False
        ),
        sa.Column("is_outsourced", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("created_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("updated_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.CheckConstraint(
            "cost_per_hour >= 0", name=op.f("ck_bom_operations_cost_per_hour_non_negative")
        ),
        sa.CheckConstraint(
            "fixed_cost >= 0", name=op.f("ck_bom_operations_fixed_cost_non_negative")
        ),
        sa.CheckConstraint(
            "operation_sequence > 0", name=op.f("ck_bom_operations_operation_sequence_positive")
        ),
        sa.CheckConstraint(
            "run_time_minutes_per_unit >= 0", name=op.f("ck_bom_operations_run_time_non_negative")
        ),
        sa.CheckConstraint(
            "setup_time_minutes >= 0", name=op.f("ck_bom_operations_setup_time_non_negative")
        ),
        sa.ForeignKeyConstraint(
            ["bom_id"], ["boms.id"], name=op.f("fk_bom_operations_bom_id_boms"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["company_id"],
            ["companies.id"],
            name=op.f("fk_bom_operations_company_id_companies"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_id"],
            ["users.id"],
            name=op.f("fk_bom_operations_created_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["updated_by_id"],
            ["users.id"],
            name=op.f("fk_bom_operations_updated_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_bom_operations")),
        sa.UniqueConstraint(
            "bom_id", "operation_sequence", name="uq_bom_operations_bom_id_operation_sequence"
        ),
    )
    op.create_index(
        "ix_bom_operations_bom",
        "bom_operations",
        ["company_id", "bom_id", "operation_sequence"],
        unique=False,
    )
    op.create_index(op.f("ix_bom_operations_bom_id"), "bom_operations", ["bom_id"], unique=False)
    op.create_index(
        op.f("ix_bom_operations_company_id"), "bom_operations", ["company_id"], unique=False
    )
    op.create_index(
        op.f("ix_bom_operations_created_at"), "bom_operations", ["created_at"], unique=False
    )
    op.create_table(
        "inventory_balances",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("company_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("item_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("warehouse_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("location_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("batch_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "on_hand_quantity",
            sa.Numeric(precision=18, scale=4),
            server_default="0",
            nullable=False,
        ),
        sa.Column(
            "reserved_quantity",
            sa.Numeric(precision=18, scale=4),
            server_default="0",
            nullable=False,
        ),
        sa.Column(
            "allocated_quantity",
            sa.Numeric(precision=18, scale=4),
            server_default="0",
            nullable=False,
        ),
        sa.Column(
            "in_transit_quantity",
            sa.Numeric(precision=18, scale=4),
            server_default="0",
            nullable=False,
        ),
        sa.Column(
            "qc_hold_quantity",
            sa.Numeric(precision=18, scale=4),
            server_default="0",
            nullable=False,
        ),
        sa.Column(
            "damaged_quantity",
            sa.Numeric(precision=18, scale=4),
            server_default="0",
            nullable=False,
        ),
        sa.Column(
            "job_work_quantity",
            sa.Numeric(precision=18, scale=4),
            server_default="0",
            nullable=False,
        ),
        sa.Column(
            "scrap_quantity", sa.Numeric(precision=18, scale=4), server_default="0", nullable=False
        ),
        sa.Column(
            "available_quantity",
            sa.Numeric(precision=18, scale=4),
            sa.Computed(
                "on_hand_quantity - reserved_quantity - qc_hold_quantity - damaged_quantity",
                persisted=True,
            ),
            nullable=False,
        ),
        sa.Column(
            "average_cost", sa.Numeric(precision=18, scale=6), server_default="0", nullable=False
        ),
        sa.Column(
            "stock_value", sa.Numeric(precision=18, scale=4), server_default="0", nullable=False
        ),
        sa.Column("last_movement_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("created_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("updated_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.CheckConstraint(
            "allocated_quantity <= reserved_quantity",
            name=op.f("ck_inventory_balances_allocated_within_reserved"),
        ),
        sa.CheckConstraint(
            "allocated_quantity >= 0", name=op.f("ck_inventory_balances_allocated_non_negative")
        ),
        sa.CheckConstraint(
            "average_cost >= 0", name=op.f("ck_inventory_balances_average_cost_non_negative")
        ),
        sa.CheckConstraint(
            "damaged_quantity >= 0", name=op.f("ck_inventory_balances_damaged_non_negative")
        ),
        sa.CheckConstraint(
            "in_transit_quantity >= 0", name=op.f("ck_inventory_balances_in_transit_non_negative")
        ),
        sa.CheckConstraint(
            "job_work_quantity >= 0", name=op.f("ck_inventory_balances_job_work_non_negative")
        ),
        sa.CheckConstraint(
            "on_hand_quantity >= 0", name=op.f("ck_inventory_balances_on_hand_non_negative")
        ),
        sa.CheckConstraint(
            "qc_hold_quantity >= 0", name=op.f("ck_inventory_balances_qc_hold_non_negative")
        ),
        sa.CheckConstraint(
            "reserved_quantity <= on_hand_quantity",
            name=op.f("ck_inventory_balances_reserved_within_on_hand"),
        ),
        sa.CheckConstraint(
            "reserved_quantity >= 0", name=op.f("ck_inventory_balances_reserved_non_negative")
        ),
        sa.CheckConstraint(
            "scrap_quantity >= 0", name=op.f("ck_inventory_balances_scrap_non_negative")
        ),
        sa.CheckConstraint(
            "stock_value >= 0", name=op.f("ck_inventory_balances_stock_value_non_negative")
        ),
        sa.ForeignKeyConstraint(
            ["batch_id"],
            ["stock_batches.id"],
            name=op.f("fk_inventory_balances_batch_id_stock_batches"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["company_id"],
            ["companies.id"],
            name=op.f("fk_inventory_balances_company_id_companies"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_id"],
            ["users.id"],
            name=op.f("fk_inventory_balances_created_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["item_id"],
            ["items.id"],
            name=op.f("fk_inventory_balances_item_id_items"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["location_id"],
            ["storage_locations.id"],
            name=op.f("fk_inventory_balances_location_id_storage_locations"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["updated_by_id"],
            ["users.id"],
            name=op.f("fk_inventory_balances_updated_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["warehouse_id"],
            ["warehouses.id"],
            name=op.f("fk_inventory_balances_warehouse_id_warehouses"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_inventory_balances")),
    )
    op.create_index(
        "ix_inventory_balances_available",
        "inventory_balances",
        ["company_id", "item_id", "warehouse_id"],
        unique=False,
        postgresql_where=sa.text("available_quantity > 0"),
    )
    op.create_index(
        "ix_inventory_balances_batch",
        "inventory_balances",
        ["company_id", "batch_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_inventory_balances_company_id"), "inventory_balances", ["company_id"], unique=False
    )
    op.create_index(
        op.f("ix_inventory_balances_created_at"), "inventory_balances", ["created_at"], unique=False
    )
    op.create_index(
        "ix_inventory_balances_item", "inventory_balances", ["company_id", "item_id"], unique=False
    )
    op.create_index(
        "ix_inventory_balances_warehouse",
        "inventory_balances",
        ["company_id", "warehouse_id", "item_id"],
        unique=False,
    )
    op.create_index(
        "uq_inventory_balances_grain",
        "inventory_balances",
        [
            "company_id",
            "item_id",
            "warehouse_id",
            sa.literal_column(
                "coalesce(location_id, '00000000-0000-0000-0000-000000000000'::uuid)"
            ),
            sa.literal_column("coalesce(batch_id, '00000000-0000-0000-0000-000000000000'::uuid)"),
        ],
        unique=True,
    )
    op.create_table(
        "item_prices",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("company_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("item_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("variant_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("price_type", sa.String(length=16), nullable=False),
        sa.Column("currency", sa.String(length=3), server_default="INR", nullable=False),
        sa.Column("price", sa.Numeric(precision=18, scale=6), nullable=False),
        sa.Column(
            "min_quantity", sa.Numeric(precision=18, scale=4), server_default="0", nullable=False
        ),
        sa.Column("valid_from", sa.Date(), nullable=False),
        sa.Column("valid_to", sa.Date(), nullable=True),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("updated_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.CheckConstraint(
            "min_quantity >= 0", name=op.f("ck_item_prices_min_quantity_non_negative")
        ),
        sa.CheckConstraint("price >= 0", name=op.f("ck_item_prices_price_non_negative")),
        sa.CheckConstraint(
            "valid_to IS NULL OR valid_to >= valid_from",
            name=op.f("ck_item_prices_validity_window_ordered"),
        ),
        sa.ForeignKeyConstraint(
            ["company_id"],
            ["companies.id"],
            name=op.f("fk_item_prices_company_id_companies"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_id"],
            ["users.id"],
            name=op.f("fk_item_prices_created_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["item_id"], ["items.id"], name=op.f("fk_item_prices_item_id_items"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["updated_by_id"],
            ["users.id"],
            name=op.f("fk_item_prices_updated_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["variant_id"],
            ["item_variants.id"],
            name=op.f("fk_item_prices_variant_id_item_variants"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_item_prices")),
    )
    op.create_index(op.f("ix_item_prices_company_id"), "item_prices", ["company_id"], unique=False)
    op.create_index(op.f("ix_item_prices_created_at"), "item_prices", ["created_at"], unique=False)
    op.create_index(op.f("ix_item_prices_deleted_at"), "item_prices", ["deleted_at"], unique=False)
    op.create_index(op.f("ix_item_prices_item_id"), "item_prices", ["item_id"], unique=False)
    op.create_index(
        "ix_item_prices_lookup",
        "item_prices",
        ["company_id", "item_id", "price_type", "valid_from"],
        unique=False,
        postgresql_where=sa.text("is_active AND deleted_at IS NULL"),
    )
    op.create_index(op.f("ix_item_prices_variant_id"), "item_prices", ["variant_id"], unique=False)
    op.create_table(
        "job_work_challan_lines",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("company_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("challan_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("job_work_order_line_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("line_number", sa.Integer(), nullable=False),
        sa.Column("item_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("uom_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("quantity", sa.Numeric(precision=18, scale=4), nullable=False),
        sa.Column(
            "unit_value", sa.Numeric(precision=18, scale=6), server_default="0", nullable=False
        ),
        sa.Column(
            "total_value", sa.Numeric(precision=18, scale=4), server_default="0", nullable=False
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("created_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("updated_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.CheckConstraint(
            "line_number > 0", name=op.f("ck_job_work_challan_lines_line_number_positive")
        ),
        sa.CheckConstraint(
            "quantity > 0", name=op.f("ck_job_work_challan_lines_quantity_positive")
        ),
        sa.CheckConstraint(
            "total_value >= 0", name=op.f("ck_job_work_challan_lines_total_value_non_negative")
        ),
        sa.CheckConstraint(
            "unit_value >= 0", name=op.f("ck_job_work_challan_lines_unit_value_non_negative")
        ),
        sa.ForeignKeyConstraint(
            ["challan_id"],
            ["job_work_challans.id"],
            name=op.f("fk_job_work_challan_lines_challan_id_job_work_challans"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["company_id"],
            ["companies.id"],
            name=op.f("fk_job_work_challan_lines_company_id_companies"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_id"],
            ["users.id"],
            name=op.f("fk_job_work_challan_lines_created_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["item_id"],
            ["items.id"],
            name=op.f("fk_job_work_challan_lines_item_id_items"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["job_work_order_line_id"],
            ["job_work_order_lines.id"],
            name=op.f("fk_job_work_challan_lines_job_work_order_line_id_job_work_order_lines"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["uom_id"],
            ["units_of_measure.id"],
            name=op.f("fk_job_work_challan_lines_uom_id_units_of_measure"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["updated_by_id"],
            ["users.id"],
            name=op.f("fk_job_work_challan_lines_updated_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_job_work_challan_lines")),
        sa.UniqueConstraint(
            "challan_id", "line_number", name="uq_job_work_challan_lines_challan_line_number"
        ),
    )
    op.create_index(
        "ix_job_work_challan_lines_challan",
        "job_work_challan_lines",
        ["company_id", "challan_id", "line_number"],
        unique=False,
    )
    op.create_index(
        op.f("ix_job_work_challan_lines_challan_id"),
        "job_work_challan_lines",
        ["challan_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_job_work_challan_lines_company_id"),
        "job_work_challan_lines",
        ["company_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_job_work_challan_lines_created_at"),
        "job_work_challan_lines",
        ["created_at"],
        unique=False,
    )
    op.create_index(
        op.f("ix_job_work_challan_lines_job_work_order_line_id"),
        "job_work_challan_lines",
        ["job_work_order_line_id"],
        unique=False,
    )
    op.create_table(
        "job_work_receipt_lines",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("company_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("receipt_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("job_work_order_line_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("line_number", sa.Integer(), nullable=False),
        sa.Column("output_item_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "output_quantity", sa.Numeric(precision=18, scale=4), server_default="0", nullable=False
        ),
        sa.Column(
            "rejected_quantity",
            sa.Numeric(precision=18, scale=4),
            server_default="0",
            nullable=False,
        ),
        sa.Column(
            "rejection_action",
            sa.String(length=24),
            server_default="RETURN_TO_STOCK",
            nullable=False,
        ),
        sa.Column(
            "returned_unprocessed_quantity",
            sa.Numeric(precision=18, scale=4),
            server_default="0",
            nullable=False,
        ),
        sa.Column(
            "scrap_quantity", sa.Numeric(precision=18, scale=4), server_default="0", nullable=False
        ),
        sa.Column(
            "excess_loss_quantity",
            sa.Numeric(precision=18, scale=4),
            server_default="0",
            nullable=False,
        ),
        sa.Column("input_consumed_quantity", sa.Numeric(precision=18, scale=4), nullable=False),
        sa.Column("charges", sa.Numeric(precision=18, scale=4), server_default="0", nullable=False),
        sa.Column(
            "vendor_debit_amount",
            sa.Numeric(precision=18, scale=4),
            server_default="0",
            nullable=False,
        ),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("created_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("updated_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.CheckConstraint(
            "rejection_action IN ('RETURN_TO_STOCK', 'REWORK_AT_VENDOR', 'SCRAP_AT_VENDOR')",
            name=op.f("ck_job_work_receipt_lines_rejection_action_known"),
        ),
        sa.CheckConstraint(
            "charges >= 0", name=op.f("ck_job_work_receipt_lines_charges_non_negative")
        ),
        sa.CheckConstraint(
            "excess_loss_quantity <= scrap_quantity",
            name=op.f("ck_job_work_receipt_lines_excess_loss_within_scrap"),
        ),
        sa.CheckConstraint(
            "excess_loss_quantity >= 0",
            name=op.f("ck_job_work_receipt_lines_excess_loss_non_negative"),
        ),
        sa.CheckConstraint(
            "input_consumed_quantity > 0",
            name=op.f("ck_job_work_receipt_lines_input_consumed_positive"),
        ),
        sa.CheckConstraint(
            "line_number > 0", name=op.f("ck_job_work_receipt_lines_line_number_positive")
        ),
        sa.CheckConstraint(
            "output_quantity >= 0", name=op.f("ck_job_work_receipt_lines_output_non_negative")
        ),
        sa.CheckConstraint(
            "rejected_quantity >= 0", name=op.f("ck_job_work_receipt_lines_rejected_non_negative")
        ),
        sa.CheckConstraint(
            "returned_unprocessed_quantity >= 0",
            name=op.f("ck_job_work_receipt_lines_returned_non_negative"),
        ),
        sa.CheckConstraint(
            "scrap_quantity + returned_unprocessed_quantity <= input_consumed_quantity",
            name=op.f("ck_job_work_receipt_lines_drawdown_components_within_consumed"),
        ),
        sa.CheckConstraint(
            "scrap_quantity >= 0", name=op.f("ck_job_work_receipt_lines_scrap_non_negative")
        ),
        sa.CheckConstraint(
            "vendor_debit_amount >= 0",
            name=op.f("ck_job_work_receipt_lines_vendor_debit_non_negative"),
        ),
        sa.ForeignKeyConstraint(
            ["company_id"],
            ["companies.id"],
            name=op.f("fk_job_work_receipt_lines_company_id_companies"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_id"],
            ["users.id"],
            name=op.f("fk_job_work_receipt_lines_created_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["job_work_order_line_id"],
            ["job_work_order_lines.id"],
            name=op.f("fk_job_work_receipt_lines_job_work_order_line_id_job_work_order_lines"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["output_item_id"],
            ["items.id"],
            name=op.f("fk_job_work_receipt_lines_output_item_id_items"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["receipt_id"],
            ["job_work_receipts.id"],
            name=op.f("fk_job_work_receipt_lines_receipt_id_job_work_receipts"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["updated_by_id"],
            ["users.id"],
            name=op.f("fk_job_work_receipt_lines_updated_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_job_work_receipt_lines")),
        sa.UniqueConstraint(
            "receipt_id", "line_number", name="uq_job_work_receipt_lines_receipt_line_number"
        ),
    )
    op.create_index(
        op.f("ix_job_work_receipt_lines_company_id"),
        "job_work_receipt_lines",
        ["company_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_job_work_receipt_lines_created_at"),
        "job_work_receipt_lines",
        ["created_at"],
        unique=False,
    )
    op.create_index(
        op.f("ix_job_work_receipt_lines_job_work_order_line_id"),
        "job_work_receipt_lines",
        ["job_work_order_line_id"],
        unique=False,
    )
    op.create_index(
        "ix_job_work_receipt_lines_order_line",
        "job_work_receipt_lines",
        ["company_id", "job_work_order_line_id"],
        unique=False,
    )
    op.create_index(
        "ix_job_work_receipt_lines_receipt",
        "job_work_receipt_lines",
        ["company_id", "receipt_id", "line_number"],
        unique=False,
    )
    op.create_index(
        op.f("ix_job_work_receipt_lines_receipt_id"),
        "job_work_receipt_lines",
        ["receipt_id"],
        unique=False,
    )
    op.create_table(
        "packing_orders",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("company_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("code", sa.String(length=64), nullable=False),
        sa.Column("sales_order_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("customer_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("warehouse_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("status", sa.String(length=24), server_default="DRAFT", nullable=False),
        sa.Column("planned_date", sa.Date(), nullable=False),
        sa.Column("packed_date", sa.DateTime(timezone=True), nullable=True),
        sa.Column("packed_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("total_cartons", sa.Integer(), server_default="0", nullable=False),
        sa.Column(
            "total_gross_weight",
            sa.Numeric(precision=18, scale=4),
            server_default="0",
            nullable=False,
        ),
        sa.Column(
            "total_net_weight",
            sa.Numeric(precision=18, scale=4),
            server_default="0",
            nullable=False,
        ),
        sa.Column("dispatch_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("handed_over_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("created_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("updated_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.CheckConstraint(
            "status IN ('DRAFT', 'IN_PROGRESS', 'PACKED', 'HANDED_OVER', 'CLOSED', 'CANCELLED')",
            name=op.f("ck_packing_orders_status_is_known"),
        ),
        sa.CheckConstraint(
            "(dispatch_id IS NULL) = (handed_over_at IS NULL)",
            name=op.f("ck_packing_orders_handover_is_complete"),
        ),
        sa.CheckConstraint(
            "total_cartons >= 0", name=op.f("ck_packing_orders_total_cartons_non_negative")
        ),
        sa.CheckConstraint(
            "total_gross_weight >= 0",
            name=op.f("ck_packing_orders_total_gross_weight_non_negative"),
        ),
        sa.CheckConstraint(
            "total_gross_weight >= total_net_weight",
            name=op.f("ck_packing_orders_total_gross_covers_net"),
        ),
        sa.CheckConstraint(
            "total_net_weight >= 0", name=op.f("ck_packing_orders_total_net_weight_non_negative")
        ),
        sa.ForeignKeyConstraint(
            ["company_id"],
            ["companies.id"],
            name=op.f("fk_packing_orders_company_id_companies"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_id"],
            ["users.id"],
            name=op.f("fk_packing_orders_created_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["customer_id"],
            ["customers.id"],
            name=op.f("fk_packing_orders_customer_id_customers"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["packed_by_id"],
            ["users.id"],
            name=op.f("fk_packing_orders_packed_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["sales_order_id"],
            ["sales_orders.id"],
            name=op.f("fk_packing_orders_sales_order_id_sales_orders"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["updated_by_id"],
            ["users.id"],
            name=op.f("fk_packing_orders_updated_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["warehouse_id"],
            ["warehouses.id"],
            name=op.f("fk_packing_orders_warehouse_id_warehouses"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_packing_orders")),
        sa.UniqueConstraint("company_id", "code", name="uq_packing_orders_company_id_code"),
    )
    op.create_index(
        "ix_packing_orders_company_customer",
        "packing_orders",
        ["company_id", "customer_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_packing_orders_company_id"), "packing_orders", ["company_id"], unique=False
    )
    op.create_index(
        "ix_packing_orders_company_status_planned",
        "packing_orders",
        ["company_id", "status", "planned_date"],
        unique=False,
    )
    op.create_index(
        "ix_packing_orders_company_warehouse_status",
        "packing_orders",
        ["company_id", "warehouse_id", "status"],
        unique=False,
    )
    op.create_index(
        op.f("ix_packing_orders_created_at"), "packing_orders", ["created_at"], unique=False
    )
    op.create_index(
        op.f("ix_packing_orders_customer_id"), "packing_orders", ["customer_id"], unique=False
    )
    op.create_index(
        "ix_packing_orders_ready_for_dispatch",
        "packing_orders",
        ["company_id", "packed_date"],
        unique=False,
        postgresql_where=sa.text("status = 'PACKED' AND dispatch_id IS NULL"),
    )
    op.create_index(
        op.f("ix_packing_orders_sales_order_id"), "packing_orders", ["sales_order_id"], unique=False
    )
    op.create_index(
        op.f("ix_packing_orders_warehouse_id"), "packing_orders", ["warehouse_id"], unique=False
    )
    op.create_table(
        "physical_count_lines",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("company_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("count_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("line_number", sa.Integer(), nullable=False),
        sa.Column("item_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("location_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("batch_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("system_quantity", sa.Numeric(precision=18, scale=4), nullable=False),
        sa.Column("counted_quantity", sa.Numeric(precision=18, scale=4), nullable=True),
        sa.Column("variance_quantity", sa.Numeric(precision=18, scale=4), nullable=True),
        sa.Column(
            "unit_cost", sa.Numeric(precision=18, scale=6), server_default="0", nullable=False
        ),
        sa.Column("variance_value", sa.Numeric(precision=18, scale=4), nullable=True),
        sa.Column("counted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("counted_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("created_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("updated_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.CheckConstraint(
            "(counted_quantity IS NULL AND variance_quantity IS NULL) OR (counted_quantity IS NOT NULL AND  variance_quantity = counted_quantity - system_quantity)",
            name=op.f("ck_physical_count_lines_variance_matches_count"),
        ),
        sa.CheckConstraint(
            "counted_quantity IS NULL OR counted_quantity >= 0",
            name=op.f("ck_physical_count_lines_counted_non_negative"),
        ),
        sa.CheckConstraint(
            "system_quantity >= 0",
            name=op.f("ck_physical_count_lines_system_quantity_non_negative"),
        ),
        sa.ForeignKeyConstraint(
            ["batch_id"],
            ["stock_batches.id"],
            name=op.f("fk_physical_count_lines_batch_id_stock_batches"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["company_id"],
            ["companies.id"],
            name=op.f("fk_physical_count_lines_company_id_companies"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["count_id"],
            ["physical_counts.id"],
            name=op.f("fk_physical_count_lines_count_id_physical_counts"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["counted_by_id"],
            ["users.id"],
            name=op.f("fk_physical_count_lines_counted_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_id"],
            ["users.id"],
            name=op.f("fk_physical_count_lines_created_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["item_id"],
            ["items.id"],
            name=op.f("fk_physical_count_lines_item_id_items"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["location_id"],
            ["storage_locations.id"],
            name=op.f("fk_physical_count_lines_location_id_storage_locations"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["updated_by_id"],
            ["users.id"],
            name=op.f("fk_physical_count_lines_updated_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_physical_count_lines")),
        sa.UniqueConstraint(
            "count_id", "line_number", name="uq_physical_count_lines_count_id_line_number"
        ),
    )
    op.create_index(
        op.f("ix_physical_count_lines_company_id"),
        "physical_count_lines",
        ["company_id"],
        unique=False,
    )
    op.create_index(
        "ix_physical_count_lines_count", "physical_count_lines", ["count_id"], unique=False
    )
    op.create_index(
        op.f("ix_physical_count_lines_created_at"),
        "physical_count_lines",
        ["created_at"],
        unique=False,
    )
    op.create_index(
        "ix_physical_count_lines_item",
        "physical_count_lines",
        ["company_id", "item_id"],
        unique=False,
    )
    op.create_index(
        "uq_physical_count_lines_position",
        "physical_count_lines",
        [
            "count_id",
            "item_id",
            sa.literal_column(
                "coalesce(location_id, '00000000-0000-0000-0000-000000000000'::uuid)"
            ),
            sa.literal_column("coalesce(batch_id, '00000000-0000-0000-0000-000000000000'::uuid)"),
        ],
        unique=True,
    )
    op.create_table(
        "production_orders",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("company_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("code", sa.String(length=32), nullable=False),
        sa.Column("item_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("bom_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("routing_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("planned_quantity", sa.Numeric(precision=18, scale=4), nullable=False),
        sa.Column(
            "produced_quantity",
            sa.Numeric(precision=18, scale=4),
            server_default="0",
            nullable=False,
        ),
        sa.Column(
            "rejected_quantity",
            sa.Numeric(precision=18, scale=4),
            server_default="0",
            nullable=False,
        ),
        sa.Column(
            "scrap_quantity", sa.Numeric(precision=18, scale=4), server_default="0", nullable=False
        ),
        sa.Column("warehouse_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("plant_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("planned_start_date", sa.Date(), nullable=False),
        sa.Column("planned_end_date", sa.Date(), nullable=False),
        sa.Column("actual_start_date", sa.Date(), nullable=True),
        sa.Column("actual_end_date", sa.Date(), nullable=True),
        sa.Column("priority", sa.Integer(), server_default="100", nullable=False),
        sa.Column("status", sa.String(length=24), server_default="DRAFT", nullable=False),
        sa.Column("source_type", sa.String(length=24), server_default="MANUAL", nullable=False),
        sa.Column("source_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("approved_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("closure_note", sa.Text(), nullable=True),
        sa.Column(
            "total_material_cost",
            sa.Numeric(precision=18, scale=4),
            server_default="0",
            nullable=False,
        ),
        sa.Column(
            "total_operation_cost",
            sa.Numeric(precision=18, scale=4),
            server_default="0",
            nullable=False,
        ),
        sa.Column(
            "total_cost", sa.Numeric(precision=18, scale=4), server_default="0", nullable=False
        ),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("created_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("updated_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.CheckConstraint(
            "status NOT IN ('APPROVED', 'CLOSED', 'COMPLETED', 'IN_PROCESS', 'MATERIAL_ISSUED', 'RELEASED') OR approved_by_id IS NOT NULL",
            name=op.f("ck_production_orders_approved_orders_carry_approval_evidence"),
        ),
        sa.CheckConstraint(
            "(approved_by_id IS NULL) = (approved_at IS NULL)",
            name=op.f("ck_production_orders_approval_evidence_paired"),
        ),
        sa.CheckConstraint(
            "actual_end_date IS NULL OR actual_start_date IS NULL OR actual_end_date >= actual_start_date",
            name=op.f("ck_production_orders_actual_window_ordered"),
        ),
        sa.CheckConstraint(
            "planned_end_date >= planned_start_date",
            name=op.f("ck_production_orders_planned_window_ordered"),
        ),
        sa.CheckConstraint(
            "planned_quantity > 0", name=op.f("ck_production_orders_planned_quantity_positive")
        ),
        sa.CheckConstraint(
            "priority >= 0", name=op.f("ck_production_orders_priority_non_negative")
        ),
        sa.CheckConstraint(
            "produced_quantity >= 0",
            name=op.f("ck_production_orders_produced_quantity_non_negative"),
        ),
        sa.CheckConstraint(
            "rejected_quantity >= 0",
            name=op.f("ck_production_orders_rejected_quantity_non_negative"),
        ),
        sa.CheckConstraint(
            "scrap_quantity >= 0", name=op.f("ck_production_orders_scrap_quantity_non_negative")
        ),
        sa.CheckConstraint(
            "total_cost >= 0", name=op.f("ck_production_orders_total_cost_non_negative")
        ),
        sa.CheckConstraint(
            "total_material_cost >= 0", name=op.f("ck_production_orders_material_cost_non_negative")
        ),
        sa.CheckConstraint(
            "total_operation_cost >= 0",
            name=op.f("ck_production_orders_operation_cost_non_negative"),
        ),
        sa.ForeignKeyConstraint(
            ["approved_by_id"],
            ["users.id"],
            name=op.f("fk_production_orders_approved_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["bom_id"],
            ["boms.id"],
            name=op.f("fk_production_orders_bom_id_boms"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["company_id"],
            ["companies.id"],
            name=op.f("fk_production_orders_company_id_companies"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_id"],
            ["users.id"],
            name=op.f("fk_production_orders_created_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["item_id"],
            ["items.id"],
            name=op.f("fk_production_orders_item_id_items"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["plant_id"],
            ["plants.id"],
            name=op.f("fk_production_orders_plant_id_plants"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["routing_id"],
            ["routings.id"],
            name=op.f("fk_production_orders_routing_id_routings"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["updated_by_id"],
            ["users.id"],
            name=op.f("fk_production_orders_updated_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["warehouse_id"],
            ["warehouses.id"],
            name=op.f("fk_production_orders_warehouse_id_warehouses"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_production_orders")),
        sa.UniqueConstraint("company_id", "code", name="uq_production_orders_company_id_code"),
        comment="Manufacturing orders. Costs are written once, at closure.",
    )
    op.create_index(
        op.f("ix_production_orders_company_id"), "production_orders", ["company_id"], unique=False
    )
    op.create_index(
        op.f("ix_production_orders_created_at"), "production_orders", ["created_at"], unique=False
    )
    op.create_index(
        "ix_production_orders_item",
        "production_orders",
        ["company_id", "item_id", "status"],
        unique=False,
    )
    op.create_index(
        "ix_production_orders_open",
        "production_orders",
        ["company_id", "priority", "planned_end_date"],
        unique=False,
        postgresql_where=sa.text("status IN ('IN_PROCESS', 'MATERIAL_ISSUED', 'RELEASED')"),
    )
    op.create_index(
        "ix_production_orders_plant",
        "production_orders",
        ["company_id", "plant_id", "status"],
        unique=False,
    )
    op.create_index(
        "ix_production_orders_source",
        "production_orders",
        ["company_id", "source_type", "source_id"],
        unique=False,
    )
    op.create_index(
        "ix_production_orders_status",
        "production_orders",
        ["company_id", "status", "planned_start_date"],
        unique=False,
    )
    op.create_index(
        "ix_production_orders_warehouse",
        "production_orders",
        ["company_id", "warehouse_id", "status"],
        unique=False,
    )
    op.create_table(
        "purchase_returns",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("company_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("code", sa.String(length=64), nullable=False),
        sa.Column("vendor_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("goods_receipt_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("purchase_order_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("warehouse_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("return_date", sa.Date(), nullable=False),
        sa.Column("status", sa.String(length=24), server_default="DRAFT", nullable=False),
        sa.Column("reason", sa.String(length=255), nullable=False),
        sa.Column("debit_note_number", sa.String(length=64), nullable=True),
        sa.Column("currency", sa.String(length=3), server_default="INR", nullable=False),
        sa.Column(
            "subtotal", sa.Numeric(precision=18, scale=4), server_default="0", nullable=False
        ),
        sa.Column(
            "tax_amount", sa.Numeric(precision=18, scale=4), server_default="0", nullable=False
        ),
        sa.Column(
            "total_amount", sa.Numeric(precision=18, scale=4), server_default="0", nullable=False
        ),
        sa.Column("posted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("posted_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("cancellation_reason", sa.Text(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("created_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("updated_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.CheckConstraint(
            "status <> 'CANCELLED' OR cancellation_reason IS NOT NULL",
            name=op.f("ck_purchase_returns_cancelled_returns_carry_a_reason"),
        ),
        sa.CheckConstraint(
            "status <> 'POSTED' OR (posted_at IS NOT NULL AND posted_by_id IS NOT NULL)",
            name=op.f("ck_purchase_returns_posted_returns_carry_evidence"),
        ),
        sa.CheckConstraint(
            "status IN ('DRAFT', 'POSTED', 'CANCELLED')",
            name=op.f("ck_purchase_returns_status_known"),
        ),
        sa.CheckConstraint(
            "length(currency) = 3", name=op.f("ck_purchase_returns_currency_is_iso_code")
        ),
        sa.CheckConstraint("subtotal >= 0", name=op.f("ck_purchase_returns_subtotal_non_negative")),
        sa.CheckConstraint("tax_amount >= 0", name=op.f("ck_purchase_returns_tax_non_negative")),
        sa.CheckConstraint(
            "total_amount >= 0", name=op.f("ck_purchase_returns_total_non_negative")
        ),
        sa.ForeignKeyConstraint(
            ["company_id"],
            ["companies.id"],
            name=op.f("fk_purchase_returns_company_id_companies"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_id"],
            ["users.id"],
            name=op.f("fk_purchase_returns_created_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["goods_receipt_id"],
            ["goods_receipts.id"],
            name=op.f("fk_purchase_returns_goods_receipt_id_goods_receipts"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["posted_by_id"],
            ["users.id"],
            name=op.f("fk_purchase_returns_posted_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["purchase_order_id"],
            ["purchase_orders.id"],
            name=op.f("fk_purchase_returns_purchase_order_id_purchase_orders"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["updated_by_id"],
            ["users.id"],
            name=op.f("fk_purchase_returns_updated_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["vendor_id"],
            ["vendors.id"],
            name=op.f("fk_purchase_returns_vendor_id_vendors"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["warehouse_id"],
            ["warehouses.id"],
            name=op.f("fk_purchase_returns_warehouse_id_warehouses"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_purchase_returns")),
        sa.UniqueConstraint("company_id", "code", name="uq_purchase_returns_company_id_code"),
    )
    op.create_index(
        op.f("ix_purchase_returns_company_id"), "purchase_returns", ["company_id"], unique=False
    )
    op.create_index(
        op.f("ix_purchase_returns_created_at"), "purchase_returns", ["created_at"], unique=False
    )
    op.create_index(
        op.f("ix_purchase_returns_goods_receipt_id"),
        "purchase_returns",
        ["goods_receipt_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_purchase_returns_purchase_order_id"),
        "purchase_returns",
        ["purchase_order_id"],
        unique=False,
    )
    op.create_index(
        "ix_purchase_returns_receipt",
        "purchase_returns",
        ["company_id", "goods_receipt_id"],
        unique=False,
    )
    op.create_index(
        "ix_purchase_returns_status",
        "purchase_returns",
        ["company_id", "status", "return_date"],
        unique=False,
    )
    op.create_index(
        "ix_purchase_returns_vendor_date",
        "purchase_returns",
        ["company_id", "vendor_id", "return_date"],
        unique=False,
    )
    op.create_index(
        op.f("ix_purchase_returns_vendor_id"), "purchase_returns", ["vendor_id"], unique=False
    )
    op.create_index(
        op.f("ix_purchase_returns_warehouse_id"), "purchase_returns", ["warehouse_id"], unique=False
    )
    op.create_table(
        "qc_inspections",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("company_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("code", sa.String(length=64), nullable=False),
        sa.Column("qc_stage", sa.String(length=24), nullable=False),
        sa.Column("item_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("warehouse_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("batch_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("template_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("template_version", sa.Integer(), nullable=True),
        sa.Column("vendor_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("reference_type", sa.String(length=48), nullable=False),
        sa.Column("reference_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("reference_code", sa.String(length=64), nullable=True),
        sa.Column("inspected_quantity", sa.Numeric(precision=18, scale=4), nullable=False),
        sa.Column("sample_size", sa.Numeric(precision=18, scale=4), nullable=False),
        sa.Column(
            "accepted_quantity",
            sa.Numeric(precision=18, scale=4),
            server_default="0",
            nullable=False,
        ),
        sa.Column(
            "rejected_quantity",
            sa.Numeric(precision=18, scale=4),
            server_default="0",
            nullable=False,
        ),
        sa.Column(
            "rework_quantity", sa.Numeric(precision=18, scale=4), server_default="0", nullable=False
        ),
        sa.Column(
            "hold_quantity", sa.Numeric(precision=18, scale=4), server_default="0", nullable=False
        ),
        sa.Column(
            "scrap_quantity", sa.Numeric(precision=18, scale=4), server_default="0", nullable=False
        ),
        sa.Column("result", sa.String(length=16), server_default="PENDING", nullable=False),
        sa.Column("status", sa.String(length=24), server_default="PENDING", nullable=False),
        sa.Column("rejection_reason", sa.Text(), nullable=True),
        sa.Column("remarks", sa.Text(), nullable=True),
        sa.Column("inspector_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("inspected_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("return_required", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("created_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("updated_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.CheckConstraint(
            "(status IN ('PENDING', 'IN_PROGRESS') AND result = 'PENDING') OR (status NOT IN ('PENDING', 'IN_PROGRESS') AND result <> 'PENDING')",
            name=op.f("ck_qc_inspections_status_and_result_agree"),
        ),
        sa.CheckConstraint(
            "qc_stage IN ('INCOMING', 'JOB_WORK', 'IN_PROCESS', 'ASSEMBLY', 'FINAL', 'RETURN')",
            name=op.f("ck_qc_inspections_qc_stage_vocabulary"),
        ),
        sa.CheckConstraint(
            "result IN ('PENDING', 'ACCEPTED', 'REWORK', 'REJECTED', 'HOLD', 'SCRAP')",
            name=op.f("ck_qc_inspections_result_vocabulary"),
        ),
        sa.CheckConstraint(
            "status <> 'CLOSED' OR accepted_quantity + rejected_quantity + rework_quantity    + hold_quantity + scrap_quantity = inspected_quantity",
            name=op.f("ck_qc_inspections_closed_disposition_balances"),
        ),
        sa.CheckConstraint(
            "status IN ('PENDING', 'IN_PROGRESS') OR (inspector_user_id IS NOT NULL AND inspected_at IS NOT NULL)",
            name=op.f("ck_qc_inspections_decision_is_attributed"),
        ),
        sa.CheckConstraint(
            "status IN ('PENDING', 'IN_PROGRESS', 'ACCEPTED', 'REWORK', 'REJECTED', 'HOLD', 'SCRAP', 'CLOSED')",
            name=op.f("ck_qc_inspections_status_vocabulary"),
        ),
        sa.CheckConstraint(
            "accepted_quantity + rejected_quantity + rework_quantity + hold_quantity + scrap_quantity <= inspected_quantity",
            name=op.f("ck_qc_inspections_disposition_within_lot"),
        ),
        sa.CheckConstraint(
            "accepted_quantity >= 0", name=op.f("ck_qc_inspections_accepted_non_negative")
        ),
        sa.CheckConstraint("hold_quantity >= 0", name=op.f("ck_qc_inspections_hold_non_negative")),
        sa.CheckConstraint(
            "inspected_quantity > 0", name=op.f("ck_qc_inspections_inspected_quantity_positive")
        ),
        sa.CheckConstraint(
            "rejected_quantity + scrap_quantity = 0 OR rejection_reason IS NOT NULL",
            name=op.f("ck_qc_inspections_refusal_states_a_reason"),
        ),
        sa.CheckConstraint(
            "rejected_quantity >= 0", name=op.f("ck_qc_inspections_rejected_non_negative")
        ),
        sa.CheckConstraint(
            "rework_quantity >= 0", name=op.f("ck_qc_inspections_rework_non_negative")
        ),
        sa.CheckConstraint(
            "sample_size > 0 AND sample_size <= inspected_quantity",
            name=op.f("ck_qc_inspections_sample_within_lot"),
        ),
        sa.CheckConstraint(
            "scrap_quantity >= 0", name=op.f("ck_qc_inspections_scrap_non_negative")
        ),
        sa.CheckConstraint(
            "template_id IS NOT NULL OR template_version IS NULL",
            name=op.f("ck_qc_inspections_version_needs_template"),
        ),
        sa.ForeignKeyConstraint(
            ["batch_id"],
            ["stock_batches.id"],
            name=op.f("fk_qc_inspections_batch_id_stock_batches"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["company_id"],
            ["companies.id"],
            name=op.f("fk_qc_inspections_company_id_companies"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_id"],
            ["users.id"],
            name=op.f("fk_qc_inspections_created_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["inspector_user_id"],
            ["users.id"],
            name=op.f("fk_qc_inspections_inspector_user_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["item_id"],
            ["items.id"],
            name=op.f("fk_qc_inspections_item_id_items"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["template_id"],
            ["qc_templates.id"],
            name=op.f("fk_qc_inspections_template_id_qc_templates"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["updated_by_id"],
            ["users.id"],
            name=op.f("fk_qc_inspections_updated_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["vendor_id"],
            ["vendors.id"],
            name=op.f("fk_qc_inspections_vendor_id_vendors"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["warehouse_id"],
            ["warehouses.id"],
            name=op.f("fk_qc_inspections_warehouse_id_warehouses"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_qc_inspections")),
        sa.UniqueConstraint("company_id", "code", name="uq_qc_inspections_company_id_code"),
    )
    op.create_index(
        op.f("ix_qc_inspections_company_id"), "qc_inspections", ["company_id"], unique=False
    )
    op.create_index(
        op.f("ix_qc_inspections_created_at"), "qc_inspections", ["created_at"], unique=False
    )
    op.create_index(
        "ix_qc_inspections_item_history",
        "qc_inspections",
        ["company_id", "item_id", "inspected_at"],
        unique=False,
    )
    op.create_index(
        "ix_qc_inspections_open_queue",
        "qc_inspections",
        ["company_id", "qc_stage", "created_at"],
        unique=False,
        postgresql_where=sa.text("status IN ('PENDING', 'IN_PROGRESS')"),
    )
    op.create_index(
        "ix_qc_inspections_reference",
        "qc_inspections",
        ["company_id", "reference_type", "reference_id"],
        unique=False,
    )
    op.create_index(
        "ix_qc_inspections_result",
        "qc_inspections",
        ["company_id", "result", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_qc_inspections_rework",
        "qc_inspections",
        ["company_id", "warehouse_id", "item_id"],
        unique=False,
        postgresql_where=sa.text("rework_quantity > 0 AND status <> 'CLOSED'"),
    )
    op.create_index(
        "ix_qc_inspections_vendor_history",
        "qc_inspections",
        ["company_id", "vendor_id", "inspected_at"],
        unique=False,
        postgresql_where=sa.text("vendor_id IS NOT NULL"),
    )
    op.create_table(
        "qc_template_parameters",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("company_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("template_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("parameter_name", sa.String(length=120), nullable=False),
        sa.Column("parameter_type", sa.String(length=16), nullable=False),
        sa.Column("unit", sa.String(length=24), nullable=True),
        sa.Column("min_value", sa.Numeric(precision=18, scale=6), nullable=True),
        sa.Column("max_value", sa.Numeric(precision=18, scale=6), nullable=True),
        sa.Column("target_value", sa.Numeric(precision=18, scale=6), nullable=True),
        sa.Column("allowed_values", postgresql.ARRAY(sa.String(length=64)), nullable=True),
        sa.Column("is_critical", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("is_mandatory", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("created_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("updated_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.CheckConstraint(
            "parameter_type <> 'SELECT' OR (allowed_values IS NOT NULL AND cardinality(allowed_values) > 0)",
            name=op.f("ck_qc_template_parameters_select_needs_allowed_values"),
        ),
        sa.CheckConstraint(
            "parameter_type = 'NUMERIC' OR (min_value IS NULL AND max_value IS NULL AND target_value IS NULL)",
            name=op.f("ck_qc_template_parameters_bounds_are_numeric_only"),
        ),
        sa.CheckConstraint(
            "parameter_type IN ('NUMERIC', 'BOOLEAN', 'TEXT', 'SELECT')",
            name=op.f("ck_qc_template_parameters_parameter_type_vocabulary"),
        ),
        sa.CheckConstraint(
            "parameter_type IN ('SELECT', 'TEXT') OR allowed_values IS NULL",
            name=op.f("ck_qc_template_parameters_allowed_values_are_textual_only"),
        ),
        sa.CheckConstraint(
            "min_value IS NULL OR max_value IS NULL OR min_value <= max_value",
            name=op.f("ck_qc_template_parameters_min_not_above_max"),
        ),
        sa.CheckConstraint(
            "sequence >= 1", name=op.f("ck_qc_template_parameters_sequence_positive")
        ),
        sa.ForeignKeyConstraint(
            ["company_id"],
            ["companies.id"],
            name=op.f("fk_qc_template_parameters_company_id_companies"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_id"],
            ["users.id"],
            name=op.f("fk_qc_template_parameters_created_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["template_id"],
            ["qc_templates.id"],
            name=op.f("fk_qc_template_parameters_template_id_qc_templates"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["updated_by_id"],
            ["users.id"],
            name=op.f("fk_qc_template_parameters_updated_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_qc_template_parameters")),
        sa.UniqueConstraint(
            "template_id", "sequence", name="uq_qc_template_parameters_template_id_sequence"
        ),
    )
    op.create_index(
        op.f("ix_qc_template_parameters_company_id"),
        "qc_template_parameters",
        ["company_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_qc_template_parameters_created_at"),
        "qc_template_parameters",
        ["created_at"],
        unique=False,
    )
    op.create_index(
        "ix_qc_template_parameters_order",
        "qc_template_parameters",
        ["company_id", "template_id", "sequence"],
        unique=False,
    )
    op.create_index(
        op.f("ix_qc_template_parameters_template_id"),
        "qc_template_parameters",
        ["template_id"],
        unique=False,
    )
    op.create_table(
        "rfq_lines",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("company_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("rfq_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("line_number", sa.Integer(), nullable=False),
        sa.Column("requisition_line_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("item_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("description", sa.String(length=500), nullable=True),
        sa.Column("quantity", sa.Numeric(precision=18, scale=4), nullable=False),
        sa.Column("uom_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("required_date", sa.Date(), nullable=True),
        sa.Column("specification", sa.Text(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("created_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("updated_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.CheckConstraint("line_number > 0", name=op.f("ck_rfq_lines_line_number_positive")),
        sa.CheckConstraint("quantity > 0", name=op.f("ck_rfq_lines_quantity_positive")),
        sa.ForeignKeyConstraint(
            ["company_id"],
            ["companies.id"],
            name=op.f("fk_rfq_lines_company_id_companies"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_id"],
            ["users.id"],
            name=op.f("fk_rfq_lines_created_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["item_id"], ["items.id"], name=op.f("fk_rfq_lines_item_id_items"), ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["requisition_line_id"],
            ["purchase_requisition_lines.id"],
            name=op.f("fk_rfq_lines_requisition_line_id_purchase_requisition_lines"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["rfq_id"], ["rfqs.id"], name=op.f("fk_rfq_lines_rfq_id_rfqs"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["uom_id"],
            ["units_of_measure.id"],
            name=op.f("fk_rfq_lines_uom_id_units_of_measure"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["updated_by_id"],
            ["users.id"],
            name=op.f("fk_rfq_lines_updated_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_rfq_lines")),
        sa.UniqueConstraint("rfq_id", "line_number", name="uq_rfq_lines_line_number"),
    )
    op.create_index(op.f("ix_rfq_lines_company_id"), "rfq_lines", ["company_id"], unique=False)
    op.create_index(op.f("ix_rfq_lines_created_at"), "rfq_lines", ["created_at"], unique=False)
    op.create_index("ix_rfq_lines_item", "rfq_lines", ["company_id", "item_id"], unique=False)
    op.create_index(op.f("ix_rfq_lines_item_id"), "rfq_lines", ["item_id"], unique=False)
    op.create_index(
        "ix_rfq_lines_rfq", "rfq_lines", ["company_id", "rfq_id", "line_number"], unique=False
    )
    op.create_index(op.f("ix_rfq_lines_rfq_id"), "rfq_lines", ["rfq_id"], unique=False)
    op.create_table(
        "routing_operations",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("company_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("routing_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("operation_code", sa.String(length=32), nullable=False),
        sa.Column("operation_name", sa.String(length=255), nullable=False),
        sa.Column("work_center_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("machine_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "setup_time_minutes",
            sa.Numeric(precision=18, scale=4),
            server_default="0",
            nullable=False,
        ),
        sa.Column(
            "run_time_per_unit_minutes",
            sa.Numeric(precision=18, scale=4),
            server_default="0",
            nullable=False,
        ),
        sa.Column(
            "queue_time_minutes",
            sa.Numeric(precision=18, scale=4),
            server_default="0",
            nullable=False,
        ),
        sa.Column(
            "move_time_minutes",
            sa.Numeric(precision=18, scale=4),
            server_default="0",
            nullable=False,
        ),
        sa.Column(
            "process_cost", sa.Numeric(precision=18, scale=4), server_default="0", nullable=False
        ),
        sa.Column("is_outsourced", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("job_work_vendor_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("qc_required", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("created_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("updated_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.CheckConstraint(
            "job_work_vendor_id IS NULL OR is_outsourced",
            name=op.f("ck_routing_operations_job_work_vendor_only_when_outsourced"),
        ),
        sa.CheckConstraint(
            "move_time_minutes >= 0", name=op.f("ck_routing_operations_move_time_non_negative")
        ),
        sa.CheckConstraint(
            "process_cost >= 0", name=op.f("ck_routing_operations_process_cost_non_negative")
        ),
        sa.CheckConstraint(
            "queue_time_minutes >= 0", name=op.f("ck_routing_operations_queue_time_non_negative")
        ),
        sa.CheckConstraint(
            "run_time_per_unit_minutes >= 0",
            name=op.f("ck_routing_operations_run_time_non_negative"),
        ),
        sa.CheckConstraint("sequence > 0", name=op.f("ck_routing_operations_sequence_positive")),
        sa.CheckConstraint(
            "setup_time_minutes >= 0", name=op.f("ck_routing_operations_setup_time_non_negative")
        ),
        sa.ForeignKeyConstraint(
            ["company_id"],
            ["companies.id"],
            name=op.f("fk_routing_operations_company_id_companies"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_id"],
            ["users.id"],
            name=op.f("fk_routing_operations_created_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["machine_id", "work_center_id"],
            ["machines.id", "machines.work_center_id"],
            name="fk_routing_operations_machine_id_work_center_id_machines",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["routing_id"],
            ["routings.id"],
            name=op.f("fk_routing_operations_routing_id_routings"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["updated_by_id"],
            ["users.id"],
            name=op.f("fk_routing_operations_updated_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["work_center_id"],
            ["work_centers.id"],
            name=op.f("fk_routing_operations_work_center_id_work_centers"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_routing_operations")),
        sa.UniqueConstraint(
            "routing_id", "sequence", name="uq_routing_operations_routing_sequence"
        ),
    )
    op.create_index(
        op.f("ix_routing_operations_company_id"), "routing_operations", ["company_id"], unique=False
    )
    op.create_index(
        op.f("ix_routing_operations_created_at"), "routing_operations", ["created_at"], unique=False
    )
    op.create_index(
        "ix_routing_operations_routing",
        "routing_operations",
        ["company_id", "routing_id", "sequence"],
        unique=False,
    )
    op.create_index(
        op.f("ix_routing_operations_routing_id"), "routing_operations", ["routing_id"], unique=False
    )
    op.create_index(
        "ix_routing_operations_work_center",
        "routing_operations",
        ["company_id", "work_center_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_routing_operations_work_center_id"),
        "routing_operations",
        ["work_center_id"],
        unique=False,
    )
    op.create_table(
        "sales_order_lines",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("company_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("sales_order_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("line_number", sa.Integer(), nullable=False),
        sa.Column("item_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("item_variant_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("description", sa.String(length=500), nullable=True),
        sa.Column("quantity", sa.Numeric(precision=18, scale=4), nullable=False),
        sa.Column("uom_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("unit_price", sa.Numeric(precision=18, scale=6), nullable=False),
        sa.Column(
            "discount_percentage",
            sa.Numeric(precision=9, scale=4),
            server_default="0",
            nullable=False,
        ),
        sa.Column(
            "discount_amount", sa.Numeric(precision=18, scale=4), server_default="0", nullable=False
        ),
        sa.Column("tax_rate", sa.Numeric(precision=9, scale=4), server_default="0", nullable=False),
        sa.Column(
            "cgst_amount", sa.Numeric(precision=18, scale=4), server_default="0", nullable=False
        ),
        sa.Column(
            "sgst_amount", sa.Numeric(precision=18, scale=4), server_default="0", nullable=False
        ),
        sa.Column(
            "igst_amount", sa.Numeric(precision=18, scale=4), server_default="0", nullable=False
        ),
        sa.Column(
            "tax_amount", sa.Numeric(precision=18, scale=4), server_default="0", nullable=False
        ),
        sa.Column(
            "line_total", sa.Numeric(precision=18, scale=4), server_default="0", nullable=False
        ),
        sa.Column("required_date", sa.Date(), nullable=True),
        sa.Column(
            "allocated_quantity",
            sa.Numeric(precision=18, scale=4),
            server_default="0",
            nullable=False,
        ),
        sa.Column(
            "dispatched_quantity",
            sa.Numeric(precision=18, scale=4),
            server_default="0",
            nullable=False,
        ),
        sa.Column(
            "invoiced_quantity",
            sa.Numeric(precision=18, scale=4),
            server_default="0",
            nullable=False,
        ),
        sa.Column("status", sa.String(length=24), server_default="PENDING", nullable=False),
        sa.Column("reservation_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("created_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("updated_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('PENDING', 'PARTIALLY_ALLOCATED', 'ALLOCATED', 'PARTIALLY_DISPATCHED', 'DISPATCHED', 'CANCELLED')",
            name=op.f("ck_sales_order_lines_status_known"),
        ),
        sa.CheckConstraint(
            "allocated_quantity <= quantity",
            name=op.f("ck_sales_order_lines_allocated_within_ordered"),
        ),
        sa.CheckConstraint(
            "allocated_quantity >= 0", name=op.f("ck_sales_order_lines_allocated_non_negative")
        ),
        sa.CheckConstraint("cgst_amount >= 0", name=op.f("ck_sales_order_lines_cgst_non_negative")),
        sa.CheckConstraint(
            "discount_amount >= 0", name=op.f("ck_sales_order_lines_discount_non_negative")
        ),
        sa.CheckConstraint(
            "discount_percentage >= 0 AND discount_percentage <= 100",
            name=op.f("ck_sales_order_lines_discount_percentage_bounded"),
        ),
        sa.CheckConstraint(
            "dispatched_quantity <= quantity",
            name=op.f("ck_sales_order_lines_dispatched_within_ordered"),
        ),
        sa.CheckConstraint(
            "dispatched_quantity >= 0", name=op.f("ck_sales_order_lines_dispatched_non_negative")
        ),
        sa.CheckConstraint(
            "igst_amount = 0 OR (cgst_amount = 0 AND sgst_amount = 0)",
            name=op.f("ck_sales_order_lines_gst_treatment_is_exclusive"),
        ),
        sa.CheckConstraint("igst_amount >= 0", name=op.f("ck_sales_order_lines_igst_non_negative")),
        sa.CheckConstraint(
            "invoiced_quantity <= quantity",
            name=op.f("ck_sales_order_lines_invoiced_within_ordered"),
        ),
        sa.CheckConstraint(
            "invoiced_quantity >= 0", name=op.f("ck_sales_order_lines_invoiced_non_negative")
        ),
        sa.CheckConstraint(
            "line_number > 0", name=op.f("ck_sales_order_lines_line_number_positive")
        ),
        sa.CheckConstraint(
            "line_total >= 0", name=op.f("ck_sales_order_lines_line_total_non_negative")
        ),
        sa.CheckConstraint("quantity > 0", name=op.f("ck_sales_order_lines_quantity_positive")),
        sa.CheckConstraint("sgst_amount >= 0", name=op.f("ck_sales_order_lines_sgst_non_negative")),
        sa.CheckConstraint(
            "tax_amount = cgst_amount + sgst_amount + igst_amount",
            name=op.f("ck_sales_order_lines_tax_split_sums_to_total"),
        ),
        sa.CheckConstraint("tax_amount >= 0", name=op.f("ck_sales_order_lines_tax_non_negative")),
        sa.CheckConstraint(
            "tax_rate >= 0 AND tax_rate <= 100", name=op.f("ck_sales_order_lines_tax_rate_bounded")
        ),
        sa.CheckConstraint(
            "unit_price >= 0", name=op.f("ck_sales_order_lines_unit_price_non_negative")
        ),
        sa.ForeignKeyConstraint(
            ["company_id"],
            ["companies.id"],
            name=op.f("fk_sales_order_lines_company_id_companies"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_id"],
            ["users.id"],
            name=op.f("fk_sales_order_lines_created_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["item_id"],
            ["items.id"],
            name=op.f("fk_sales_order_lines_item_id_items"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["item_variant_id"],
            ["item_variants.id"],
            name=op.f("fk_sales_order_lines_item_variant_id_item_variants"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["sales_order_id"],
            ["sales_orders.id"],
            name=op.f("fk_sales_order_lines_sales_order_id_sales_orders"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["uom_id"],
            ["units_of_measure.id"],
            name=op.f("fk_sales_order_lines_uom_id_units_of_measure"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["updated_by_id"],
            ["users.id"],
            name=op.f("fk_sales_order_lines_updated_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_sales_order_lines")),
        sa.UniqueConstraint(
            "sales_order_id", "line_number", name="uq_sales_order_lines_order_line_number"
        ),
    )
    op.create_index(
        op.f("ix_sales_order_lines_company_id"), "sales_order_lines", ["company_id"], unique=False
    )
    op.create_index(
        op.f("ix_sales_order_lines_created_at"), "sales_order_lines", ["created_at"], unique=False
    )
    op.create_index(
        op.f("ix_sales_order_lines_item_id"), "sales_order_lines", ["item_id"], unique=False
    )
    op.create_index(
        "ix_sales_order_lines_open_item",
        "sales_order_lines",
        ["company_id", "item_id"],
        unique=False,
        postgresql_where=sa.text("status <> 'CANCELLED' AND dispatched_quantity < quantity"),
    )
    op.create_index(
        "ix_sales_order_lines_order",
        "sales_order_lines",
        ["company_id", "sales_order_id", "line_number"],
        unique=False,
    )
    op.create_index(
        op.f("ix_sales_order_lines_sales_order_id"),
        "sales_order_lines",
        ["sales_order_id"],
        unique=False,
    )
    op.create_table(
        "stock_adjustment_lines",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("company_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("adjustment_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("line_number", sa.Integer(), nullable=False),
        sa.Column("item_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("location_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("batch_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("quantity", sa.Numeric(precision=18, scale=4), nullable=False),
        sa.Column(
            "unit_cost", sa.Numeric(precision=18, scale=6), server_default="0", nullable=False
        ),
        sa.Column(
            "total_cost", sa.Numeric(precision=18, scale=4), server_default="0", nullable=False
        ),
        sa.Column("system_quantity", sa.Numeric(precision=18, scale=4), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("created_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("updated_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.CheckConstraint(
            "quantity <> 0", name=op.f("ck_stock_adjustment_lines_quantity_non_zero")
        ),
        sa.CheckConstraint(
            "unit_cost >= 0", name=op.f("ck_stock_adjustment_lines_unit_cost_non_negative")
        ),
        sa.ForeignKeyConstraint(
            ["adjustment_id"],
            ["stock_adjustments.id"],
            name=op.f("fk_stock_adjustment_lines_adjustment_id_stock_adjustments"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["batch_id"],
            ["stock_batches.id"],
            name=op.f("fk_stock_adjustment_lines_batch_id_stock_batches"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["company_id"],
            ["companies.id"],
            name=op.f("fk_stock_adjustment_lines_company_id_companies"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_id"],
            ["users.id"],
            name=op.f("fk_stock_adjustment_lines_created_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["item_id"],
            ["items.id"],
            name=op.f("fk_stock_adjustment_lines_item_id_items"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["location_id"],
            ["storage_locations.id"],
            name=op.f("fk_stock_adjustment_lines_location_id_storage_locations"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["updated_by_id"],
            ["users.id"],
            name=op.f("fk_stock_adjustment_lines_updated_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_stock_adjustment_lines")),
        sa.UniqueConstraint(
            "adjustment_id",
            "line_number",
            name="uq_stock_adjustment_lines_adjustment_id_line_number",
        ),
    )
    op.create_index(
        "ix_stock_adjustment_lines_adjustment",
        "stock_adjustment_lines",
        ["adjustment_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_stock_adjustment_lines_company_id"),
        "stock_adjustment_lines",
        ["company_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_stock_adjustment_lines_created_at"),
        "stock_adjustment_lines",
        ["created_at"],
        unique=False,
    )
    op.create_index(
        "ix_stock_adjustment_lines_item",
        "stock_adjustment_lines",
        ["company_id", "item_id"],
        unique=False,
    )
    op.create_table(
        "stock_reservations",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("company_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("item_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("warehouse_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("location_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("batch_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("demand_type", sa.String(length=24), nullable=False),
        sa.Column("demand_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("demand_line_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("demand_code", sa.String(length=64), nullable=True),
        sa.Column("reservation_type", sa.String(length=24), nullable=False),
        sa.Column("quantity", sa.Numeric(precision=18, scale=4), nullable=False),
        sa.Column(
            "consumed_quantity",
            sa.Numeric(precision=18, scale=4),
            server_default="0",
            nullable=False,
        ),
        sa.Column(
            "released_quantity",
            sa.Numeric(precision=18, scale=4),
            server_default="0",
            nullable=False,
        ),
        sa.Column(
            "remaining_quantity",
            sa.Numeric(precision=18, scale=4),
            sa.Computed("quantity - consumed_quantity - released_quantity", persisted=True),
            nullable=False,
        ),
        sa.Column("status", sa.String(length=24), server_default="ACTIVE", nullable=False),
        sa.Column("priority", sa.Integer(), server_default="100", nullable=False),
        sa.Column("required_date", sa.Date(), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reason", sa.String(length=255), nullable=False),
        sa.Column("released_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("released_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("release_reason", sa.String(length=255), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("created_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("updated_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.CheckConstraint(
            "consumed_quantity + released_quantity <= quantity",
            name=op.f("ck_stock_reservations_consumed_and_released_within_quantity"),
        ),
        sa.CheckConstraint(
            "consumed_quantity >= 0", name=op.f("ck_stock_reservations_consumed_non_negative")
        ),
        sa.CheckConstraint(
            "priority >= 0", name=op.f("ck_stock_reservations_priority_non_negative")
        ),
        sa.CheckConstraint("quantity > 0", name=op.f("ck_stock_reservations_quantity_positive")),
        sa.CheckConstraint(
            "released_quantity >= 0", name=op.f("ck_stock_reservations_released_non_negative")
        ),
        sa.ForeignKeyConstraint(
            ["batch_id"],
            ["stock_batches.id"],
            name=op.f("fk_stock_reservations_batch_id_stock_batches"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["company_id"],
            ["companies.id"],
            name=op.f("fk_stock_reservations_company_id_companies"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_id"],
            ["users.id"],
            name=op.f("fk_stock_reservations_created_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["item_id"],
            ["items.id"],
            name=op.f("fk_stock_reservations_item_id_items"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["location_id"],
            ["storage_locations.id"],
            name=op.f("fk_stock_reservations_location_id_storage_locations"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["released_by_id"],
            ["users.id"],
            name=op.f("fk_stock_reservations_released_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["updated_by_id"],
            ["users.id"],
            name=op.f("fk_stock_reservations_updated_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["warehouse_id"],
            ["warehouses.id"],
            name=op.f("fk_stock_reservations_warehouse_id_warehouses"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_stock_reservations")),
    )
    op.create_index(
        "ix_stock_reservations_candidates",
        "stock_reservations",
        ["company_id", "item_id", "warehouse_id", "priority", "required_date"],
        unique=False,
        postgresql_where=sa.text("status IN ('ACTIVE', 'PARTIALLY_CONSUMED')"),
    )
    op.create_index(
        op.f("ix_stock_reservations_company_id"), "stock_reservations", ["company_id"], unique=False
    )
    op.create_index(
        op.f("ix_stock_reservations_created_at"), "stock_reservations", ["created_at"], unique=False
    )
    op.create_index(
        "ix_stock_reservations_demand",
        "stock_reservations",
        ["company_id", "demand_type", "demand_id"],
        unique=False,
    )
    op.create_index(
        "ix_stock_reservations_expiring",
        "stock_reservations",
        ["company_id", "expires_at"],
        unique=False,
        postgresql_where=sa.text(
            "status IN ('ACTIVE', 'PARTIALLY_CONSUMED') AND expires_at IS NOT NULL"
        ),
    )
    op.create_index(
        "ix_stock_reservations_item",
        "stock_reservations",
        ["company_id", "item_id", "warehouse_id", "status"],
        unique=False,
    )
    op.create_table(
        "stock_serials",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("company_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("item_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("serial_number", sa.String(length=64), nullable=False),
        sa.Column("batch_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("warehouse_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("location_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("status", sa.String(length=24), server_default="IN_STOCK", nullable=False),
        sa.Column("received_on", sa.Date(), nullable=True),
        sa.Column(
            "unit_cost", sa.Numeric(precision=18, scale=6), server_default="0", nullable=False
        ),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("created_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("updated_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.CheckConstraint("unit_cost >= 0", name=op.f("ck_stock_serials_unit_cost_non_negative")),
        sa.ForeignKeyConstraint(
            ["batch_id"],
            ["stock_batches.id"],
            name=op.f("fk_stock_serials_batch_id_stock_batches"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["company_id"],
            ["companies.id"],
            name=op.f("fk_stock_serials_company_id_companies"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_id"],
            ["users.id"],
            name=op.f("fk_stock_serials_created_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["item_id"],
            ["items.id"],
            name=op.f("fk_stock_serials_item_id_items"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["location_id"],
            ["storage_locations.id"],
            name=op.f("fk_stock_serials_location_id_storage_locations"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["updated_by_id"],
            ["users.id"],
            name=op.f("fk_stock_serials_updated_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["warehouse_id"],
            ["warehouses.id"],
            name=op.f("fk_stock_serials_warehouse_id_warehouses"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_stock_serials")),
        sa.UniqueConstraint(
            "company_id", "item_id", "serial_number", name="uq_stock_serials_company_item_serial"
        ),
    )
    op.create_index(
        op.f("ix_stock_serials_company_id"), "stock_serials", ["company_id"], unique=False
    )
    op.create_index(
        op.f("ix_stock_serials_created_at"), "stock_serials", ["created_at"], unique=False
    )
    op.create_index(
        "ix_stock_serials_location",
        "stock_serials",
        ["company_id", "warehouse_id", "status"],
        unique=False,
    )
    op.create_index(
        "ix_stock_serials_number", "stock_serials", ["company_id", "serial_number"], unique=False
    )
    op.create_table(
        "stock_transfer_lines",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("company_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("transfer_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("line_number", sa.Integer(), nullable=False),
        sa.Column("item_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("batch_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("from_location_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("to_location_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("quantity", sa.Numeric(precision=18, scale=4), nullable=False),
        sa.Column(
            "dispatched_quantity",
            sa.Numeric(precision=18, scale=4),
            server_default="0",
            nullable=False,
        ),
        sa.Column(
            "received_quantity",
            sa.Numeric(precision=18, scale=4),
            server_default="0",
            nullable=False,
        ),
        sa.Column(
            "unit_cost", sa.Numeric(precision=18, scale=6), server_default="0", nullable=False
        ),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("created_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("updated_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.CheckConstraint(
            "dispatched_quantity <= quantity",
            name=op.f("ck_stock_transfer_lines_dispatched_within_ordered"),
        ),
        sa.CheckConstraint(
            "dispatched_quantity >= 0", name=op.f("ck_stock_transfer_lines_dispatched_non_negative")
        ),
        sa.CheckConstraint("quantity > 0", name=op.f("ck_stock_transfer_lines_quantity_positive")),
        sa.CheckConstraint(
            "received_quantity <= dispatched_quantity",
            name=op.f("ck_stock_transfer_lines_received_within_dispatched"),
        ),
        sa.CheckConstraint(
            "received_quantity >= 0", name=op.f("ck_stock_transfer_lines_received_non_negative")
        ),
        sa.ForeignKeyConstraint(
            ["batch_id"],
            ["stock_batches.id"],
            name=op.f("fk_stock_transfer_lines_batch_id_stock_batches"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["company_id"],
            ["companies.id"],
            name=op.f("fk_stock_transfer_lines_company_id_companies"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_id"],
            ["users.id"],
            name=op.f("fk_stock_transfer_lines_created_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["from_location_id"],
            ["storage_locations.id"],
            name=op.f("fk_stock_transfer_lines_from_location_id_storage_locations"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["item_id"],
            ["items.id"],
            name=op.f("fk_stock_transfer_lines_item_id_items"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["to_location_id"],
            ["storage_locations.id"],
            name=op.f("fk_stock_transfer_lines_to_location_id_storage_locations"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["transfer_id"],
            ["stock_transfers.id"],
            name=op.f("fk_stock_transfer_lines_transfer_id_stock_transfers"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["updated_by_id"],
            ["users.id"],
            name=op.f("fk_stock_transfer_lines_updated_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_stock_transfer_lines")),
        sa.UniqueConstraint(
            "transfer_id", "line_number", name="uq_stock_transfer_lines_transfer_id_line_number"
        ),
    )
    op.create_index(
        op.f("ix_stock_transfer_lines_company_id"),
        "stock_transfer_lines",
        ["company_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_stock_transfer_lines_created_at"),
        "stock_transfer_lines",
        ["created_at"],
        unique=False,
    )
    op.create_index(
        "ix_stock_transfer_lines_item",
        "stock_transfer_lines",
        ["company_id", "item_id"],
        unique=False,
    )
    op.create_index(
        "ix_stock_transfer_lines_transfer", "stock_transfer_lines", ["transfer_id"], unique=False
    )
    op.create_table(
        "vendor_invoices",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("company_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("code", sa.String(length=64), nullable=False),
        sa.Column("vendor_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("vendor_invoice_number", sa.String(length=64), nullable=False),
        sa.Column("invoice_date", sa.Date(), nullable=False),
        sa.Column("due_date", sa.Date(), nullable=False),
        sa.Column("purchase_order_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("goods_receipt_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("currency", sa.String(length=3), server_default="INR", nullable=False),
        sa.Column(
            "subtotal", sa.Numeric(precision=18, scale=4), server_default="0", nullable=False
        ),
        sa.Column(
            "tax_amount", sa.Numeric(precision=18, scale=4), server_default="0", nullable=False
        ),
        sa.Column(
            "round_off_amount",
            sa.Numeric(precision=18, scale=4),
            server_default="0",
            nullable=False,
        ),
        sa.Column(
            "total_amount", sa.Numeric(precision=18, scale=4), server_default="0", nullable=False
        ),
        sa.Column(
            "paid_amount", sa.Numeric(precision=18, scale=4), server_default="0", nullable=False
        ),
        sa.Column("status", sa.String(length=24), server_default="DRAFT", nullable=False),
        sa.Column(
            "match_status", sa.String(length=16), server_default="NOT_MATCHED", nullable=False
        ),
        sa.Column("matched_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "match_variance_amount",
            sa.Numeric(precision=18, scale=4),
            server_default="0",
            nullable=False,
        ),
        sa.Column("variance_override_reason", sa.Text(), nullable=True),
        sa.Column("approved_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("dispute_reason", sa.Text(), nullable=True),
        sa.Column("cancellation_reason", sa.Text(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("created_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("updated_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.CheckConstraint(
            "(matched_at IS NULL) = (match_status = 'NOT_MATCHED')",
            name=op.f("ck_vendor_invoices_match_evidence_complete"),
        ),
        sa.CheckConstraint(
            "match_status <> 'OVERRIDDEN' OR variance_override_reason IS NOT NULL",
            name=op.f("ck_vendor_invoices_overrides_carry_a_reason"),
        ),
        sa.CheckConstraint(
            "match_status IN ('NOT_MATCHED', 'MATCHED', 'VARIANCE', 'OVERRIDDEN')",
            name=op.f("ck_vendor_invoices_match_status_known"),
        ),
        sa.CheckConstraint(
            "status <> 'CANCELLED' OR cancellation_reason IS NOT NULL",
            name=op.f("ck_vendor_invoices_cancelled_invoices_carry_a_reason"),
        ),
        sa.CheckConstraint(
            "status <> 'DISPUTED' OR dispute_reason IS NOT NULL",
            name=op.f("ck_vendor_invoices_disputed_invoices_carry_a_reason"),
        ),
        sa.CheckConstraint(
            "status IN ('DRAFT', 'PENDING_APPROVAL', 'APPROVED', 'PARTIALLY_PAID', 'PAID', 'DISPUTED', 'CANCELLED')",
            name=op.f("ck_vendor_invoices_status_known"),
        ),
        sa.CheckConstraint(
            "(approved_by_id IS NULL) = (approved_at IS NULL)",
            name=op.f("ck_vendor_invoices_approval_evidence_complete"),
        ),
        sa.CheckConstraint(
            "due_date >= invoice_date", name=op.f("ck_vendor_invoices_due_not_before_invoice")
        ),
        sa.CheckConstraint(
            "length(currency) = 3", name=op.f("ck_vendor_invoices_currency_is_iso_code")
        ),
        sa.CheckConstraint(
            "paid_amount <= total_amount", name=op.f("ck_vendor_invoices_paid_within_total")
        ),
        sa.CheckConstraint("paid_amount >= 0", name=op.f("ck_vendor_invoices_paid_non_negative")),
        sa.CheckConstraint("subtotal >= 0", name=op.f("ck_vendor_invoices_subtotal_non_negative")),
        sa.CheckConstraint("tax_amount >= 0", name=op.f("ck_vendor_invoices_tax_non_negative")),
        sa.CheckConstraint(
            "total_amount = subtotal + tax_amount + round_off_amount",
            name=op.f("ck_vendor_invoices_total_is_subtotal_plus_tax_plus_round_off"),
        ),
        sa.CheckConstraint("total_amount >= 0", name=op.f("ck_vendor_invoices_total_non_negative")),
        sa.ForeignKeyConstraint(
            ["approved_by_id"],
            ["users.id"],
            name=op.f("fk_vendor_invoices_approved_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["company_id"],
            ["companies.id"],
            name=op.f("fk_vendor_invoices_company_id_companies"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_id"],
            ["users.id"],
            name=op.f("fk_vendor_invoices_created_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["goods_receipt_id"],
            ["goods_receipts.id"],
            name=op.f("fk_vendor_invoices_goods_receipt_id_goods_receipts"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["purchase_order_id"],
            ["purchase_orders.id"],
            name=op.f("fk_vendor_invoices_purchase_order_id_purchase_orders"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["updated_by_id"],
            ["users.id"],
            name=op.f("fk_vendor_invoices_updated_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["vendor_id"],
            ["vendors.id"],
            name=op.f("fk_vendor_invoices_vendor_id_vendors"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_vendor_invoices")),
        sa.UniqueConstraint("company_id", "code", name="uq_vendor_invoices_company_id_code"),
        sa.UniqueConstraint(
            "company_id",
            "vendor_id",
            "vendor_invoice_number",
            name="uq_vendor_invoices_vendor_invoice_number",
        ),
    )
    op.create_index(
        op.f("ix_vendor_invoices_company_id"), "vendor_invoices", ["company_id"], unique=False
    )
    op.create_index(
        op.f("ix_vendor_invoices_created_at"), "vendor_invoices", ["created_at"], unique=False
    )
    op.create_index(
        op.f("ix_vendor_invoices_due_date"), "vendor_invoices", ["due_date"], unique=False
    )
    op.create_index(
        op.f("ix_vendor_invoices_goods_receipt_id"),
        "vendor_invoices",
        ["goods_receipt_id"],
        unique=False,
    )
    op.create_index(
        "ix_vendor_invoices_open",
        "vendor_invoices",
        ["company_id", "vendor_id", "due_date"],
        unique=False,
        postgresql_where=sa.text(
            "status IN ('PENDING_APPROVAL', 'APPROVED', 'PARTIALLY_PAID', 'DISPUTED')"
        ),
    )
    op.create_index(
        "ix_vendor_invoices_purchase_order",
        "vendor_invoices",
        ["company_id", "purchase_order_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_vendor_invoices_purchase_order_id"),
        "vendor_invoices",
        ["purchase_order_id"],
        unique=False,
    )
    op.create_index(
        "ix_vendor_invoices_status_due",
        "vendor_invoices",
        ["company_id", "status", "due_date"],
        unique=False,
    )
    op.create_index(
        "ix_vendor_invoices_vendor_date",
        "vendor_invoices",
        ["company_id", "vendor_id", "invoice_date"],
        unique=False,
    )
    op.create_index(
        op.f("ix_vendor_invoices_vendor_id"), "vendor_invoices", ["vendor_id"], unique=False
    )
    op.create_table(
        "vendor_quotations",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("company_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("rfq_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("vendor_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("vendor_reference", sa.String(length=64), nullable=False),
        sa.Column("quotation_date", sa.Date(), nullable=False),
        sa.Column("valid_until", sa.Date(), nullable=True),
        sa.Column("currency", sa.String(length=3), server_default="INR", nullable=False),
        sa.Column("payment_terms_days", sa.Integer(), server_default="0", nullable=False),
        sa.Column("lead_time_days", sa.Integer(), server_default="0", nullable=False),
        sa.Column(
            "subtotal", sa.Numeric(precision=18, scale=4), server_default="0", nullable=False
        ),
        sa.Column(
            "discount", sa.Numeric(precision=18, scale=4), server_default="0", nullable=False
        ),
        sa.Column(
            "tax_amount", sa.Numeric(precision=18, scale=4), server_default="0", nullable=False
        ),
        sa.Column(
            "freight_amount", sa.Numeric(precision=18, scale=4), server_default="0", nullable=False
        ),
        sa.Column(
            "total_amount", sa.Numeric(precision=18, scale=4), server_default="0", nullable=False
        ),
        sa.Column("status", sa.String(length=24), server_default="RECEIVED", nullable=False),
        sa.Column("selected_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("selected_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("decision_reason", sa.Text(), nullable=True),
        sa.Column("converted_purchase_order_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("terms", sa.Text(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("created_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("updated_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('RECEIVED', 'UNDER_REVIEW', 'SELECTED', 'REJECTED', 'EXPIRED')",
            name=op.f("ck_vendor_quotations_status_known"),
        ),
        sa.CheckConstraint(
            "(selected_by_id IS NULL) = (selected_at IS NULL)",
            name=op.f("ck_vendor_quotations_selection_evidence_complete"),
        ),
        sa.CheckConstraint(
            "discount >= 0", name=op.f("ck_vendor_quotations_discount_non_negative")
        ),
        sa.CheckConstraint(
            "freight_amount >= 0", name=op.f("ck_vendor_quotations_freight_non_negative")
        ),
        sa.CheckConstraint(
            "lead_time_days >= 0", name=op.f("ck_vendor_quotations_lead_time_non_negative")
        ),
        sa.CheckConstraint(
            "length(currency) = 3", name=op.f("ck_vendor_quotations_currency_is_iso_code")
        ),
        sa.CheckConstraint(
            "payment_terms_days >= 0", name=op.f("ck_vendor_quotations_payment_terms_non_negative")
        ),
        sa.CheckConstraint(
            "subtotal >= 0", name=op.f("ck_vendor_quotations_subtotal_non_negative")
        ),
        sa.CheckConstraint("tax_amount >= 0", name=op.f("ck_vendor_quotations_tax_non_negative")),
        sa.CheckConstraint(
            "total_amount >= 0", name=op.f("ck_vendor_quotations_total_non_negative")
        ),
        sa.CheckConstraint(
            "valid_until IS NULL OR valid_until >= quotation_date",
            name=op.f("ck_vendor_quotations_valid_until_not_before_quotation_date"),
        ),
        sa.ForeignKeyConstraint(
            ["company_id"],
            ["companies.id"],
            name=op.f("fk_vendor_quotations_company_id_companies"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["converted_purchase_order_id"],
            ["purchase_orders.id"],
            name=op.f("fk_vendor_quotations_converted_purchase_order_id_purchase_orders"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_id"],
            ["users.id"],
            name=op.f("fk_vendor_quotations_created_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["rfq_id"],
            ["rfqs.id"],
            name=op.f("fk_vendor_quotations_rfq_id_rfqs"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["selected_by_id"],
            ["users.id"],
            name=op.f("fk_vendor_quotations_selected_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["updated_by_id"],
            ["users.id"],
            name=op.f("fk_vendor_quotations_updated_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["vendor_id"],
            ["vendors.id"],
            name=op.f("fk_vendor_quotations_vendor_id_vendors"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_vendor_quotations")),
        sa.UniqueConstraint(
            "company_id", "rfq_id", "vendor_id", name="uq_vendor_quotations_rfq_vendor"
        ),
    )
    op.create_index(
        op.f("ix_vendor_quotations_company_id"), "vendor_quotations", ["company_id"], unique=False
    )
    op.create_index(
        op.f("ix_vendor_quotations_created_at"), "vendor_quotations", ["created_at"], unique=False
    )
    op.create_index(
        op.f("ix_vendor_quotations_rfq_id"), "vendor_quotations", ["rfq_id"], unique=False
    )
    op.create_index(
        "ix_vendor_quotations_status",
        "vendor_quotations",
        ["company_id", "status", "valid_until"],
        unique=False,
    )
    op.create_index(
        "ix_vendor_quotations_vendor",
        "vendor_quotations",
        ["company_id", "vendor_id", "quotation_date"],
        unique=False,
    )
    op.create_index(
        op.f("ix_vendor_quotations_vendor_id"), "vendor_quotations", ["vendor_id"], unique=False
    )
    op.create_table(
        "assembly_order_components",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("company_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("assembly_order_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("line_number", sa.Integer(), nullable=False),
        sa.Column("item_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("uom_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("required_quantity", sa.Numeric(precision=18, scale=4), nullable=False),
        sa.Column(
            "issued_quantity", sa.Numeric(precision=18, scale=4), server_default="0", nullable=False
        ),
        sa.Column(
            "consumed_quantity",
            sa.Numeric(precision=18, scale=4),
            server_default="0",
            nullable=False,
        ),
        sa.Column(
            "is_serial_tracked_component",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
        sa.Column(
            "consumed_serial_ids",
            postgresql.ARRAY(postgresql.UUID(as_uuid=True)),
            server_default=sa.text("'{}'::uuid[]"),
            nullable=False,
        ),
        sa.Column("reservation_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("created_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("updated_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.CheckConstraint(
            "NOT is_serial_tracked_component OR cardinality(consumed_serial_ids) = consumed_quantity",
            name=op.f("ck_assembly_order_components_serial_count_matches_consumed"),
        ),
        sa.CheckConstraint(
            "consumed_quantity <= issued_quantity",
            name=op.f("ck_assembly_order_components_consumed_within_issued"),
        ),
        sa.CheckConstraint(
            "consumed_quantity >= 0",
            name=op.f("ck_assembly_order_components_consumed_non_negative"),
        ),
        sa.CheckConstraint(
            "is_serial_tracked_component OR cardinality(consumed_serial_ids) = 0",
            name=op.f("ck_assembly_order_components_serials_only_when_serialised"),
        ),
        sa.CheckConstraint(
            "issued_quantity >= 0", name=op.f("ck_assembly_order_components_issued_non_negative")
        ),
        sa.CheckConstraint(
            "line_number > 0", name=op.f("ck_assembly_order_components_line_number_positive")
        ),
        sa.CheckConstraint(
            "required_quantity > 0",
            name=op.f("ck_assembly_order_components_required_quantity_positive"),
        ),
        sa.ForeignKeyConstraint(
            ["assembly_order_id"],
            ["assembly_orders.id"],
            name=op.f("fk_assembly_order_components_assembly_order_id_assembly_orders"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["company_id"],
            ["companies.id"],
            name=op.f("fk_assembly_order_components_company_id_companies"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_id"],
            ["users.id"],
            name=op.f("fk_assembly_order_components_created_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["item_id"],
            ["items.id"],
            name=op.f("fk_assembly_order_components_item_id_items"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["uom_id"],
            ["units_of_measure.id"],
            name=op.f("fk_assembly_order_components_uom_id_units_of_measure"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["updated_by_id"],
            ["users.id"],
            name=op.f("fk_assembly_order_components_updated_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_assembly_order_components")),
        sa.UniqueConstraint(
            "assembly_order_id",
            "line_number",
            name="uq_assembly_order_components_order_line_number",
        ),
    )
    op.create_index(
        op.f("ix_assembly_order_components_assembly_order_id"),
        "assembly_order_components",
        ["assembly_order_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_assembly_order_components_company_id"),
        "assembly_order_components",
        ["company_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_assembly_order_components_created_at"),
        "assembly_order_components",
        ["created_at"],
        unique=False,
    )
    op.create_index(
        "ix_assembly_order_components_item",
        "assembly_order_components",
        ["company_id", "item_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_assembly_order_components_item_id"),
        "assembly_order_components",
        ["item_id"],
        unique=False,
    )
    op.create_index(
        "ix_assembly_order_components_order",
        "assembly_order_components",
        ["company_id", "assembly_order_id", "line_number"],
        unique=False,
    )
    op.create_index(
        "ix_assembly_order_components_serials",
        "assembly_order_components",
        ["consumed_serial_ids"],
        unique=False,
        postgresql_using="gin",
    )
    op.create_table(
        "cartons",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("company_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("packing_order_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("carton_number", sa.Integer(), nullable=False),
        sa.Column("carton_type", sa.String(length=24), server_default="BOX", nullable=False),
        sa.Column("barcode", sa.String(length=64), nullable=False),
        sa.Column("length_cm", sa.Numeric(precision=18, scale=4), nullable=True),
        sa.Column("width_cm", sa.Numeric(precision=18, scale=4), nullable=True),
        sa.Column("height_cm", sa.Numeric(precision=18, scale=4), nullable=True),
        sa.Column(
            "tare_weight_kg", sa.Numeric(precision=18, scale=4), server_default="0", nullable=False
        ),
        sa.Column(
            "net_weight_kg", sa.Numeric(precision=18, scale=4), server_default="0", nullable=False
        ),
        sa.Column(
            "gross_weight_kg", sa.Numeric(precision=18, scale=4), server_default="0", nullable=False
        ),
        sa.Column("packing_material_item_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "packing_material_quantity",
            sa.Numeric(precision=18, scale=4),
            server_default="1",
            nullable=False,
        ),
        sa.Column("material_transaction_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("is_sealed", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("sealed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("created_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("updated_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.CheckConstraint(
            "barcode ~ '^[0-9A-Z-]{8,64}$'", name=op.f("ck_cartons_barcode_is_scannable")
        ),
        sa.CheckConstraint(
            "carton_type IN ('BOX', 'CARTON', 'CRATE', 'PALLET', 'BAG', 'DRUM', 'SHRINK_WRAP')",
            name=op.f("ck_cartons_carton_type_is_known"),
        ),
        sa.CheckConstraint(
            "(length_cm IS NULL) = (width_cm IS NULL) AND (width_cm IS NULL) = (height_cm IS NULL)",
            name=op.f("ck_cartons_dimensions_are_complete"),
        ),
        sa.CheckConstraint(
            "NOT is_sealed OR material_transaction_id IS NOT NULL",
            name=op.f("ck_cartons_sealed_carton_posted_its_material"),
        ),
        sa.CheckConstraint("carton_number > 0", name=op.f("ck_cartons_carton_number_positive")),
        sa.CheckConstraint(
            "gross_weight_kg >= 0", name=op.f("ck_cartons_gross_weight_non_negative")
        ),
        sa.CheckConstraint(
            "gross_weight_kg >= net_weight_kg", name=op.f("ck_cartons_gross_covers_net")
        ),
        sa.CheckConstraint(
            "is_sealed = (sealed_at IS NOT NULL)", name=op.f("ck_cartons_seal_is_timestamped")
        ),
        sa.CheckConstraint(
            "length_cm IS NULL OR (length_cm > 0 AND width_cm > 0 AND height_cm > 0)",
            name=op.f("ck_cartons_dimensions_positive"),
        ),
        sa.CheckConstraint("net_weight_kg >= 0", name=op.f("ck_cartons_net_weight_non_negative")),
        sa.CheckConstraint(
            "packing_material_quantity > 0",
            name=op.f("ck_cartons_packing_material_quantity_positive"),
        ),
        sa.CheckConstraint("tare_weight_kg >= 0", name=op.f("ck_cartons_tare_weight_non_negative")),
        sa.ForeignKeyConstraint(
            ["company_id"],
            ["companies.id"],
            name=op.f("fk_cartons_company_id_companies"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_id"],
            ["users.id"],
            name=op.f("fk_cartons_created_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["packing_material_item_id"],
            ["items.id"],
            name=op.f("fk_cartons_packing_material_item_id_items"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["packing_order_id"],
            ["packing_orders.id"],
            name=op.f("fk_cartons_packing_order_id_packing_orders"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["updated_by_id"],
            ["users.id"],
            name=op.f("fk_cartons_updated_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_cartons")),
        sa.UniqueConstraint("company_id", "barcode", name="uq_cartons_company_id_barcode"),
        sa.UniqueConstraint(
            "packing_order_id", "carton_number", name="uq_cartons_order_carton_number"
        ),
    )
    op.create_index(op.f("ix_cartons_company_id"), "cartons", ["company_id"], unique=False)
    op.create_index(op.f("ix_cartons_created_at"), "cartons", ["created_at"], unique=False)
    op.create_index(
        "ix_cartons_open",
        "cartons",
        ["company_id", "packing_order_id"],
        unique=False,
        postgresql_where=sa.text("is_sealed = false"),
    )
    op.create_index(
        "ix_cartons_order",
        "cartons",
        ["company_id", "packing_order_id", "carton_number"],
        unique=False,
    )
    op.create_index(
        op.f("ix_cartons_packing_material_item_id"),
        "cartons",
        ["packing_material_item_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_cartons_packing_order_id"), "cartons", ["packing_order_id"], unique=False
    )
    op.create_table(
        "dispatches",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("company_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("code", sa.String(length=64), nullable=False),
        sa.Column("dispatch_type", sa.String(length=24), server_default="SALES", nullable=False),
        sa.Column("sales_order_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("packing_order_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("customer_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("vendor_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("warehouse_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("status", sa.String(length=24), server_default="NOT_DISPATCHED", nullable=False),
        sa.Column("dispatch_date", sa.Date(), nullable=False),
        sa.Column("dispatched_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expected_delivery_date", sa.Date(), nullable=True),
        sa.Column("actual_delivery_date", sa.Date(), nullable=True),
        sa.Column("transporter_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("courier_name", sa.String(length=120), nullable=True),
        sa.Column("transport_name", sa.String(length=160), nullable=True),
        sa.Column("tracking_id", sa.String(length=120), nullable=True),
        sa.Column("lr_number", sa.String(length=64), nullable=True),
        sa.Column("vehicle_number", sa.String(length=32), nullable=True),
        sa.Column("driver_name", sa.String(length=120), nullable=True),
        sa.Column("driver_mobile", sa.String(length=20), nullable=True),
        sa.Column("total_boxes", sa.Integer(), server_default="0", nullable=False),
        sa.Column(
            "total_weight_kg", sa.Numeric(precision=18, scale=4), server_default="0", nullable=False
        ),
        sa.Column(
            "freight_amount", sa.Numeric(precision=18, scale=4), server_default="0", nullable=False
        ),
        sa.Column("freight_paid_by", sa.String(length=16), nullable=True),
        sa.Column("eway_bill_number", sa.String(length=24), nullable=True),
        sa.Column("invoice_document_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("delivery_confirmed_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("delivery_note", sa.Text(), nullable=True),
        sa.Column("delay_reason", sa.Text(), nullable=True),
        sa.Column("delay_notified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("created_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("updated_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.CheckConstraint(
            "dispatch_type <> 'SALES' OR customer_id IS NOT NULL",
            name=op.f("ck_dispatches_sales_dispatch_names_a_customer"),
        ),
        sa.CheckConstraint(
            "dispatch_type IN ('SALES', 'PURCHASE_RETURN', 'JOB_WORK', 'TRANSFER')",
            name=op.f("ck_dispatches_dispatch_type_is_known"),
        ),
        sa.CheckConstraint(
            "dispatch_type NOT IN ('PURCHASE_RETURN', 'JOB_WORK') OR vendor_id IS NOT NULL",
            name=op.f("ck_dispatches_vendor_dispatch_names_a_vendor"),
        ),
        sa.CheckConstraint(
            "freight_paid_by IS NULL OR freight_paid_by IN ('CONSIGNOR', 'CONSIGNEE')",
            name=op.f("ck_dispatches_freight_paid_by_is_known"),
        ),
        sa.CheckConstraint(
            "sales_order_id IS NULL OR dispatch_type = 'SALES'",
            name=op.f("ck_dispatches_sales_order_only_on_sales_dispatch"),
        ),
        sa.CheckConstraint(
            "status IN ('NOT_DISPATCHED', 'READY_FOR_DISPATCH', 'DISPATCHED', 'IN_TRANSIT', 'DELIVERED', 'QC_PENDING', 'RECEIVED', 'QC_COMPLETED', 'GRN_COMPLETED', 'CANCELLED')",
            name=op.f("ck_dispatches_status_is_known"),
        ),
        sa.CheckConstraint(
            "status NOT IN ('DELIVERED', 'QC_PENDING', 'RECEIVED', 'QC_COMPLETED', 'GRN_COMPLETED') OR actual_delivery_date IS NOT NULL",
            name=op.f("ck_dispatches_delivered_dispatch_has_arrival_date"),
        ),
        sa.CheckConstraint(
            "actual_delivery_date IS NULL OR actual_delivery_date >= dispatch_date",
            name=op.f("ck_dispatches_actual_delivery_not_before_dispatch"),
        ),
        sa.CheckConstraint(
            "expected_delivery_date IS NULL OR expected_delivery_date >= dispatch_date",
            name=op.f("ck_dispatches_expected_delivery_not_before_dispatch"),
        ),
        sa.CheckConstraint(
            "freight_amount >= 0", name=op.f("ck_dispatches_freight_amount_non_negative")
        ),
        sa.CheckConstraint("total_boxes >= 0", name=op.f("ck_dispatches_total_boxes_non_negative")),
        sa.CheckConstraint(
            "total_weight_kg >= 0", name=op.f("ck_dispatches_total_weight_non_negative")
        ),
        sa.ForeignKeyConstraint(
            ["company_id"],
            ["companies.id"],
            name=op.f("fk_dispatches_company_id_companies"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_id"],
            ["users.id"],
            name=op.f("fk_dispatches_created_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["customer_id"],
            ["customers.id"],
            name=op.f("fk_dispatches_customer_id_customers"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["delivery_confirmed_by"],
            ["users.id"],
            name=op.f("fk_dispatches_delivery_confirmed_by_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["invoice_document_id"],
            ["documents.id"],
            name=op.f("fk_dispatches_invoice_document_id_documents"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["packing_order_id"],
            ["packing_orders.id"],
            name=op.f("fk_dispatches_packing_order_id_packing_orders"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["sales_order_id"],
            ["sales_orders.id"],
            name=op.f("fk_dispatches_sales_order_id_sales_orders"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["transporter_id"],
            ["transporters.id"],
            name=op.f("fk_dispatches_transporter_id_transporters"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["updated_by_id"],
            ["users.id"],
            name=op.f("fk_dispatches_updated_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["vendor_id"],
            ["vendors.id"],
            name=op.f("fk_dispatches_vendor_id_vendors"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["warehouse_id"],
            ["warehouses.id"],
            name=op.f("fk_dispatches_warehouse_id_warehouses"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_dispatches")),
        sa.UniqueConstraint("company_id", "code", name="uq_dispatches_company_id_code"),
    )
    op.create_index(
        "ix_dispatches_company_customer",
        "dispatches",
        ["company_id", "customer_id", "dispatch_date"],
        unique=False,
    )
    op.create_index(op.f("ix_dispatches_company_id"), "dispatches", ["company_id"], unique=False)
    op.create_index(
        "ix_dispatches_company_status_date",
        "dispatches",
        ["company_id", "status", "dispatch_date"],
        unique=False,
    )
    op.create_index(
        "ix_dispatches_company_vendor",
        "dispatches",
        ["company_id", "vendor_id", "dispatch_date"],
        unique=False,
    )
    op.create_index(
        "ix_dispatches_company_warehouse_status",
        "dispatches",
        ["company_id", "warehouse_id", "status"],
        unique=False,
    )
    op.create_index(op.f("ix_dispatches_created_at"), "dispatches", ["created_at"], unique=False)
    op.create_index(op.f("ix_dispatches_customer_id"), "dispatches", ["customer_id"], unique=False)
    op.create_index(
        "ix_dispatches_in_flight_expected",
        "dispatches",
        ["company_id", "expected_delivery_date"],
        unique=False,
        postgresql_where=sa.text(
            "status IN ('DISPATCHED', 'IN_TRANSIT') AND expected_delivery_date IS NOT NULL"
        ),
    )
    op.create_index(
        op.f("ix_dispatches_packing_order_id"), "dispatches", ["packing_order_id"], unique=False
    )
    op.create_index(
        op.f("ix_dispatches_sales_order_id"), "dispatches", ["sales_order_id"], unique=False
    )
    op.create_index(
        "ix_dispatches_tracking_id",
        "dispatches",
        ["company_id", "tracking_id"],
        unique=False,
        postgresql_where=sa.text("tracking_id IS NOT NULL"),
    )
    op.create_index(
        op.f("ix_dispatches_transporter_id"), "dispatches", ["transporter_id"], unique=False
    )
    op.create_index(op.f("ix_dispatches_vendor_id"), "dispatches", ["vendor_id"], unique=False)
    op.create_index(
        op.f("ix_dispatches_warehouse_id"), "dispatches", ["warehouse_id"], unique=False
    )
    op.create_table(
        "inventory_transactions",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("company_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("transaction_type", sa.String(length=32), nullable=False),
        sa.Column("item_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("warehouse_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("location_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("batch_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("serial_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("quantity", sa.Numeric(precision=18, scale=4), nullable=False),
        sa.Column(
            "unit_cost", sa.Numeric(precision=18, scale=6), server_default="0", nullable=False
        ),
        sa.Column(
            "total_cost", sa.Numeric(precision=18, scale=4), server_default="0", nullable=False
        ),
        sa.Column("balance_after", sa.Numeric(precision=18, scale=4), nullable=False),
        sa.Column("stock_state", sa.String(length=24), server_default="AVAILABLE", nullable=False),
        sa.Column("source_state", sa.String(length=24), nullable=True),
        sa.Column("reference_type", sa.String(length=48), nullable=False),
        sa.Column("reference_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("reference_code", sa.String(length=64), nullable=True),
        sa.Column("reason", sa.String(length=255), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("created_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.CheckConstraint(
            "quantity <> 0", name=op.f("ck_inventory_transactions_quantity_non_zero")
        ),
        sa.CheckConstraint(
            "unit_cost >= 0", name=op.f("ck_inventory_transactions_unit_cost_non_negative")
        ),
        sa.ForeignKeyConstraint(
            ["batch_id"],
            ["stock_batches.id"],
            name=op.f("fk_inventory_transactions_batch_id_stock_batches"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["company_id"],
            ["companies.id"],
            name=op.f("fk_inventory_transactions_company_id_companies"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_id"],
            ["users.id"],
            name=op.f("fk_inventory_transactions_created_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["item_id"],
            ["items.id"],
            name=op.f("fk_inventory_transactions_item_id_items"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["location_id"],
            ["storage_locations.id"],
            name=op.f("fk_inventory_transactions_location_id_storage_locations"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["serial_id"],
            ["stock_serials.id"],
            name=op.f("fk_inventory_transactions_serial_id_stock_serials"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["warehouse_id"],
            ["warehouses.id"],
            name=op.f("fk_inventory_transactions_warehouse_id_warehouses"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_inventory_transactions")),
        comment="Append-only stock ledger. Corrections are offsetting rows, never updates.",
    )
    op.create_index(
        "ix_inventory_transactions_batch",
        "inventory_transactions",
        ["company_id", "batch_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_inventory_transactions_company_id"),
        "inventory_transactions",
        ["company_id"],
        unique=False,
    )
    op.create_index(
        "ix_inventory_transactions_item_history",
        "inventory_transactions",
        ["company_id", "item_id", "warehouse_id", sa.literal_column("occurred_at DESC")],
        unique=False,
    )
    op.create_index(
        "ix_inventory_transactions_keyset",
        "inventory_transactions",
        ["company_id", sa.literal_column("created_at DESC"), sa.literal_column("id DESC")],
        unique=False,
    )
    op.create_index(
        "ix_inventory_transactions_occurred",
        "inventory_transactions",
        ["company_id", sa.literal_column("occurred_at DESC"), sa.literal_column("id DESC")],
        unique=False,
    )
    op.create_index(
        "ix_inventory_transactions_reference",
        "inventory_transactions",
        ["company_id", "reference_type", "reference_id"],
        unique=False,
    )
    op.create_index(
        "ix_inventory_transactions_serial",
        "inventory_transactions",
        ["company_id", "serial_id"],
        unique=False,
    )
    op.create_index(
        "ix_inventory_transactions_type",
        "inventory_transactions",
        ["company_id", "transaction_type", sa.literal_column("occurred_at DESC")],
        unique=False,
    )
    op.create_table(
        "material_issues",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("company_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("code", sa.String(length=32), nullable=False),
        sa.Column("production_order_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("issue_type", sa.String(length=24), server_default="ISSUE", nullable=False),
        sa.Column("status", sa.String(length=24), server_default="DRAFT", nullable=False),
        sa.Column("issue_date", sa.Date(), nullable=False),
        sa.Column("warehouse_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("posted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("posted_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("reason", sa.String(length=255), nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("line_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column(
            "total_quantity", sa.Numeric(precision=18, scale=4), server_default="0", nullable=False
        ),
        sa.Column(
            "total_cost", sa.Numeric(precision=18, scale=4), server_default="0", nullable=False
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("created_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("updated_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.CheckConstraint(
            "(status = 'POSTED') = (posted_at IS NOT NULL)",
            name=op.f("ck_material_issues_posted_rows_carry_posting_time"),
        ),
        sa.CheckConstraint(
            "(posted_at IS NULL) = (posted_by_id IS NULL)",
            name=op.f("ck_material_issues_posting_evidence_paired"),
        ),
        sa.CheckConstraint(
            "line_count >= 0", name=op.f("ck_material_issues_line_count_non_negative")
        ),
        sa.CheckConstraint(
            "total_cost >= 0", name=op.f("ck_material_issues_total_cost_non_negative")
        ),
        sa.CheckConstraint(
            "total_quantity >= 0", name=op.f("ck_material_issues_total_quantity_non_negative")
        ),
        sa.ForeignKeyConstraint(
            ["company_id"],
            ["companies.id"],
            name=op.f("fk_material_issues_company_id_companies"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_id"],
            ["users.id"],
            name=op.f("fk_material_issues_created_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["posted_by_id"],
            ["users.id"],
            name=op.f("fk_material_issues_posted_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["production_order_id"],
            ["production_orders.id"],
            name=op.f("fk_material_issues_production_order_id_production_orders"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["updated_by_id"],
            ["users.id"],
            name=op.f("fk_material_issues_updated_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["warehouse_id"],
            ["warehouses.id"],
            name=op.f("fk_material_issues_warehouse_id_warehouses"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_material_issues")),
        sa.UniqueConstraint("company_id", "code", name="uq_material_issues_company_id_code"),
    )
    op.create_index(
        op.f("ix_material_issues_company_id"), "material_issues", ["company_id"], unique=False
    )
    op.create_index(
        op.f("ix_material_issues_created_at"), "material_issues", ["created_at"], unique=False
    )
    op.create_index(
        "ix_material_issues_order",
        "material_issues",
        ["company_id", "production_order_id", "issue_date"],
        unique=False,
    )
    op.create_index(
        "ix_material_issues_status",
        "material_issues",
        ["company_id", "status", "issue_date"],
        unique=False,
    )
    op.create_index(
        "ix_material_issues_warehouse",
        "material_issues",
        ["company_id", "warehouse_id", "status"],
        unique=False,
    )
    op.create_table(
        "packing_order_lines",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("company_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("packing_order_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("line_number", sa.Integer(), nullable=False),
        sa.Column("sales_order_line_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("item_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("quantity_to_pack", sa.Numeric(precision=18, scale=4), nullable=False),
        sa.Column(
            "packed_quantity", sa.Numeric(precision=18, scale=4), server_default="0", nullable=False
        ),
        sa.Column("uom_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("created_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("updated_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.CheckConstraint(
            "line_number > 0", name=op.f("ck_packing_order_lines_line_number_positive")
        ),
        sa.CheckConstraint(
            "packed_quantity <= quantity_to_pack",
            name=op.f("ck_packing_order_lines_packed_within_requested"),
        ),
        sa.CheckConstraint(
            "packed_quantity >= 0", name=op.f("ck_packing_order_lines_packed_quantity_non_negative")
        ),
        sa.CheckConstraint(
            "quantity_to_pack > 0", name=op.f("ck_packing_order_lines_quantity_to_pack_positive")
        ),
        sa.ForeignKeyConstraint(
            ["company_id"],
            ["companies.id"],
            name=op.f("fk_packing_order_lines_company_id_companies"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_id"],
            ["users.id"],
            name=op.f("fk_packing_order_lines_created_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["item_id"],
            ["items.id"],
            name=op.f("fk_packing_order_lines_item_id_items"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["packing_order_id"],
            ["packing_orders.id"],
            name=op.f("fk_packing_order_lines_packing_order_id_packing_orders"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["sales_order_line_id"],
            ["sales_order_lines.id"],
            name=op.f("fk_packing_order_lines_sales_order_line_id_sales_order_lines"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["uom_id"],
            ["units_of_measure.id"],
            name=op.f("fk_packing_order_lines_uom_id_units_of_measure"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["updated_by_id"],
            ["users.id"],
            name=op.f("fk_packing_order_lines_updated_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_packing_order_lines")),
        sa.UniqueConstraint(
            "packing_order_id", "line_number", name="uq_packing_order_lines_order_line_number"
        ),
    )
    op.create_index(
        op.f("ix_packing_order_lines_company_id"),
        "packing_order_lines",
        ["company_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_packing_order_lines_created_at"),
        "packing_order_lines",
        ["created_at"],
        unique=False,
    )
    op.create_index(
        "ix_packing_order_lines_item",
        "packing_order_lines",
        ["company_id", "item_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_packing_order_lines_item_id"), "packing_order_lines", ["item_id"], unique=False
    )
    op.create_index(
        "ix_packing_order_lines_order",
        "packing_order_lines",
        ["company_id", "packing_order_id", "line_number"],
        unique=False,
    )
    op.create_index(
        op.f("ix_packing_order_lines_packing_order_id"),
        "packing_order_lines",
        ["packing_order_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_packing_order_lines_sales_order_line_id"),
        "packing_order_lines",
        ["sales_order_line_id"],
        unique=False,
    )
    op.create_table(
        "payment_allocations",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("company_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("payment_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("vendor_invoice_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("amount", sa.Numeric(precision=18, scale=4), nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("created_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("updated_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.CheckConstraint("amount > 0", name=op.f("ck_payment_allocations_amount_positive")),
        sa.ForeignKeyConstraint(
            ["company_id"],
            ["companies.id"],
            name=op.f("fk_payment_allocations_company_id_companies"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_id"],
            ["users.id"],
            name=op.f("fk_payment_allocations_created_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["payment_id"],
            ["payments.id"],
            name=op.f("fk_payment_allocations_payment_id_payments"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["updated_by_id"],
            ["users.id"],
            name=op.f("fk_payment_allocations_updated_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["vendor_invoice_id"],
            ["vendor_invoices.id"],
            name=op.f("fk_payment_allocations_vendor_invoice_id_vendor_invoices"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_payment_allocations")),
        sa.UniqueConstraint(
            "payment_id", "vendor_invoice_id", name="uq_payment_allocations_payment_invoice"
        ),
    )
    op.create_index(
        op.f("ix_payment_allocations_company_id"),
        "payment_allocations",
        ["company_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_payment_allocations_created_at"),
        "payment_allocations",
        ["created_at"],
        unique=False,
    )
    op.create_index(
        "ix_payment_allocations_invoice",
        "payment_allocations",
        ["company_id", "vendor_invoice_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_payment_allocations_payment_id"),
        "payment_allocations",
        ["payment_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_payment_allocations_vendor_invoice_id"),
        "payment_allocations",
        ["vendor_invoice_id"],
        unique=False,
    )
    op.create_table(
        "production_order_components",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("company_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("production_order_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("line_number", sa.Integer(), nullable=False),
        sa.Column("item_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("item_code", sa.String(length=64), nullable=False),
        sa.Column("uom_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("bom_level", sa.Integer(), server_default="1", nullable=False),
        sa.Column("required_quantity", sa.Numeric(precision=18, scale=4), nullable=False),
        sa.Column(
            "issued_quantity", sa.Numeric(precision=18, scale=4), server_default="0", nullable=False
        ),
        sa.Column(
            "returned_quantity",
            sa.Numeric(precision=18, scale=4),
            server_default="0",
            nullable=False,
        ),
        sa.Column(
            "consumed_quantity",
            sa.Numeric(precision=18, scale=4),
            server_default="0",
            nullable=False,
        ),
        sa.Column("warehouse_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("is_backflushed", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("created_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("updated_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.CheckConstraint(
            "bom_level >= 1", name=op.f("ck_production_order_components_bom_level_positive")
        ),
        sa.CheckConstraint(
            "consumed_quantity <= issued_quantity - returned_quantity",
            name=op.f("ck_production_order_components_consumed_within_net_issued"),
        ),
        sa.CheckConstraint(
            "consumed_quantity >= 0",
            name=op.f("ck_production_order_components_consumed_quantity_non_negative"),
        ),
        sa.CheckConstraint(
            "issued_quantity >= 0",
            name=op.f("ck_production_order_components_issued_quantity_non_negative"),
        ),
        sa.CheckConstraint(
            "required_quantity > 0",
            name=op.f("ck_production_order_components_required_quantity_positive"),
        ),
        sa.CheckConstraint(
            "returned_quantity <= issued_quantity",
            name=op.f("ck_production_order_components_returned_within_issued"),
        ),
        sa.CheckConstraint(
            "returned_quantity >= 0",
            name=op.f("ck_production_order_components_returned_quantity_non_negative"),
        ),
        sa.ForeignKeyConstraint(
            ["company_id"],
            ["companies.id"],
            name=op.f("fk_production_order_components_company_id_companies"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_id"],
            ["users.id"],
            name=op.f("fk_production_order_components_created_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["item_id"],
            ["items.id"],
            name=op.f("fk_production_order_components_item_id_items"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["production_order_id"],
            ["production_orders.id"],
            name=op.f("fk_production_order_components_production_order_id_production_orders"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["uom_id"],
            ["units_of_measure.id"],
            name=op.f("fk_production_order_components_uom_id_units_of_measure"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["updated_by_id"],
            ["users.id"],
            name=op.f("fk_production_order_components_updated_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["warehouse_id"],
            ["warehouses.id"],
            name=op.f("fk_production_order_components_warehouse_id_warehouses"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_production_order_components")),
        sa.UniqueConstraint(
            "production_order_id", "item_id", name="uq_production_order_components_order_item"
        ),
        sa.UniqueConstraint(
            "production_order_id", "line_number", name="uq_production_order_components_order_line"
        ),
    )
    op.create_index(
        op.f("ix_production_order_components_company_id"),
        "production_order_components",
        ["company_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_production_order_components_created_at"),
        "production_order_components",
        ["created_at"],
        unique=False,
    )
    op.create_index(
        "ix_production_order_components_item",
        "production_order_components",
        ["company_id", "item_id"],
        unique=False,
    )
    op.create_index(
        "ix_production_order_components_order",
        "production_order_components",
        ["production_order_id", "line_number"],
        unique=False,
    )
    op.create_table(
        "qc_inspection_results",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("company_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("inspection_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("parameter_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("measured_value_numeric", sa.Numeric(precision=18, scale=6), nullable=True),
        sa.Column("measured_value_text", sa.String(length=255), nullable=True),
        sa.Column("measured_value_bool", sa.Boolean(), nullable=True),
        sa.Column("is_within_spec", sa.Boolean(), nullable=False),
        sa.Column("remark", sa.Text(), nullable=True),
        sa.Column("photo_document_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("created_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("updated_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.CheckConstraint(
            "num_nonnulls(measured_value_numeric, measured_value_text, measured_value_bool) = 1",
            name=op.f("ck_qc_inspection_results_exactly_one_measured_value"),
        ),
        sa.ForeignKeyConstraint(
            ["company_id"],
            ["companies.id"],
            name=op.f("fk_qc_inspection_results_company_id_companies"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_id"],
            ["users.id"],
            name=op.f("fk_qc_inspection_results_created_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["inspection_id"],
            ["qc_inspections.id"],
            name=op.f("fk_qc_inspection_results_inspection_id_qc_inspections"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["parameter_id"],
            ["qc_template_parameters.id"],
            name=op.f("fk_qc_inspection_results_parameter_id_qc_template_parameters"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["updated_by_id"],
            ["users.id"],
            name=op.f("fk_qc_inspection_results_updated_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_qc_inspection_results")),
        sa.UniqueConstraint(
            "inspection_id",
            "parameter_id",
            name="uq_qc_inspection_results_inspection_id_parameter_id",
        ),
    )
    op.create_index(
        op.f("ix_qc_inspection_results_company_id"),
        "qc_inspection_results",
        ["company_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_qc_inspection_results_created_at"),
        "qc_inspection_results",
        ["created_at"],
        unique=False,
    )
    op.create_index(
        op.f("ix_qc_inspection_results_inspection_id"),
        "qc_inspection_results",
        ["inspection_id"],
        unique=False,
    )
    op.create_index(
        "ix_qc_inspection_results_parameter",
        "qc_inspection_results",
        ["company_id", "parameter_id", "is_within_spec"],
        unique=False,
    )
    op.create_index(
        "ix_qc_inspection_results_sheet",
        "qc_inspection_results",
        ["company_id", "inspection_id"],
        unique=False,
    )
    op.create_table(
        "reallocation_requests",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("company_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("from_reservation_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("to_demand_type", sa.String(length=24), nullable=False),
        sa.Column("to_demand_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("to_demand_code", sa.String(length=64), nullable=True),
        sa.Column("item_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("warehouse_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("quantity", sa.Numeric(precision=18, scale=4), nullable=False),
        sa.Column("reason", sa.String(length=255), nullable=False),
        sa.Column("status", sa.String(length=24), server_default="PENDING", nullable=False),
        sa.Column("requested_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("approved_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("decision_note", sa.Text(), nullable=True),
        sa.Column("impact_summary", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("created_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("updated_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.CheckConstraint("quantity > 0", name=op.f("ck_reallocation_requests_quantity_positive")),
        sa.ForeignKeyConstraint(
            ["approved_by_id"],
            ["users.id"],
            name=op.f("fk_reallocation_requests_approved_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["company_id"],
            ["companies.id"],
            name=op.f("fk_reallocation_requests_company_id_companies"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_id"],
            ["users.id"],
            name=op.f("fk_reallocation_requests_created_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["from_reservation_id"],
            ["stock_reservations.id"],
            name=op.f("fk_reallocation_requests_from_reservation_id_stock_reservations"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["item_id"],
            ["items.id"],
            name=op.f("fk_reallocation_requests_item_id_items"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["requested_by_id"],
            ["users.id"],
            name=op.f("fk_reallocation_requests_requested_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["updated_by_id"],
            ["users.id"],
            name=op.f("fk_reallocation_requests_updated_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["warehouse_id"],
            ["warehouses.id"],
            name=op.f("fk_reallocation_requests_warehouse_id_warehouses"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_reallocation_requests")),
    )
    op.create_index(
        op.f("ix_reallocation_requests_company_id"),
        "reallocation_requests",
        ["company_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_reallocation_requests_created_at"),
        "reallocation_requests",
        ["created_at"],
        unique=False,
    )
    op.create_index(
        "ix_reallocation_requests_donor",
        "reallocation_requests",
        ["company_id", "from_reservation_id", "status"],
        unique=False,
    )
    op.create_index(
        "ix_reallocation_requests_item",
        "reallocation_requests",
        ["company_id", "item_id", "warehouse_id"],
        unique=False,
    )
    op.create_index(
        "ix_reallocation_requests_pending",
        "reallocation_requests",
        ["company_id", "created_at"],
        unique=False,
        postgresql_where=sa.text("status = 'PENDING'"),
    )
    op.create_index(
        "ix_reallocation_requests_status",
        "reallocation_requests",
        ["company_id", "status", "created_at"],
        unique=False,
    )
    op.create_table(
        "vendor_quotation_lines",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("company_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("quotation_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("line_number", sa.Integer(), nullable=False),
        sa.Column("rfq_line_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("item_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("description", sa.String(length=500), nullable=True),
        sa.Column("quantity", sa.Numeric(precision=18, scale=4), nullable=False),
        sa.Column("uom_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("unit_price", sa.Numeric(precision=18, scale=6), nullable=False),
        sa.Column(
            "discount_percentage",
            sa.Numeric(precision=9, scale=4),
            server_default="0",
            nullable=False,
        ),
        sa.Column(
            "discount", sa.Numeric(precision=18, scale=4), server_default="0", nullable=False
        ),
        sa.Column("tax_rate", sa.Numeric(precision=9, scale=4), server_default="0", nullable=False),
        sa.Column(
            "tax_amount", sa.Numeric(precision=18, scale=4), server_default="0", nullable=False
        ),
        sa.Column(
            "line_total", sa.Numeric(precision=18, scale=4), server_default="0", nullable=False
        ),
        sa.Column("lead_time_days", sa.Integer(), nullable=True),
        sa.Column(
            "min_order_quantity",
            sa.Numeric(precision=18, scale=4),
            server_default="0",
            nullable=False,
        ),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("created_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("updated_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.CheckConstraint(
            "discount >= 0", name=op.f("ck_vendor_quotation_lines_discount_non_negative")
        ),
        sa.CheckConstraint(
            "discount_percentage >= 0 AND discount_percentage <= 100",
            name=op.f("ck_vendor_quotation_lines_discount_percentage_bounded"),
        ),
        sa.CheckConstraint(
            "lead_time_days IS NULL OR lead_time_days >= 0",
            name=op.f("ck_vendor_quotation_lines_lead_time_non_negative"),
        ),
        sa.CheckConstraint(
            "line_number > 0", name=op.f("ck_vendor_quotation_lines_line_number_positive")
        ),
        sa.CheckConstraint(
            "line_total >= 0", name=op.f("ck_vendor_quotation_lines_line_total_non_negative")
        ),
        sa.CheckConstraint(
            "min_order_quantity >= 0",
            name=op.f("ck_vendor_quotation_lines_min_order_quantity_non_negative"),
        ),
        sa.CheckConstraint(
            "quantity > 0", name=op.f("ck_vendor_quotation_lines_quantity_positive")
        ),
        sa.CheckConstraint(
            "tax_amount >= 0", name=op.f("ck_vendor_quotation_lines_tax_non_negative")
        ),
        sa.CheckConstraint(
            "tax_rate >= 0 AND tax_rate <= 100",
            name=op.f("ck_vendor_quotation_lines_tax_rate_bounded"),
        ),
        sa.CheckConstraint(
            "unit_price >= 0", name=op.f("ck_vendor_quotation_lines_unit_price_non_negative")
        ),
        sa.ForeignKeyConstraint(
            ["company_id"],
            ["companies.id"],
            name=op.f("fk_vendor_quotation_lines_company_id_companies"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_id"],
            ["users.id"],
            name=op.f("fk_vendor_quotation_lines_created_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["item_id"],
            ["items.id"],
            name=op.f("fk_vendor_quotation_lines_item_id_items"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["quotation_id"],
            ["vendor_quotations.id"],
            name=op.f("fk_vendor_quotation_lines_quotation_id_vendor_quotations"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["rfq_line_id"],
            ["rfq_lines.id"],
            name=op.f("fk_vendor_quotation_lines_rfq_line_id_rfq_lines"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["uom_id"],
            ["units_of_measure.id"],
            name=op.f("fk_vendor_quotation_lines_uom_id_units_of_measure"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["updated_by_id"],
            ["users.id"],
            name=op.f("fk_vendor_quotation_lines_updated_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_vendor_quotation_lines")),
        sa.UniqueConstraint(
            "quotation_id", "line_number", name="uq_vendor_quotation_lines_line_number"
        ),
    )
    op.create_index(
        op.f("ix_vendor_quotation_lines_company_id"),
        "vendor_quotation_lines",
        ["company_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_vendor_quotation_lines_created_at"),
        "vendor_quotation_lines",
        ["created_at"],
        unique=False,
    )
    op.create_index(
        "ix_vendor_quotation_lines_item",
        "vendor_quotation_lines",
        ["company_id", "item_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_vendor_quotation_lines_item_id"),
        "vendor_quotation_lines",
        ["item_id"],
        unique=False,
    )
    op.create_index(
        "ix_vendor_quotation_lines_quotation",
        "vendor_quotation_lines",
        ["company_id", "quotation_id", "line_number"],
        unique=False,
    )
    op.create_index(
        op.f("ix_vendor_quotation_lines_quotation_id"),
        "vendor_quotation_lines",
        ["quotation_id"],
        unique=False,
    )
    op.create_index(
        "ix_vendor_quotation_lines_rfq_line",
        "vendor_quotation_lines",
        ["company_id", "rfq_line_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_vendor_quotation_lines_rfq_line_id"),
        "vendor_quotation_lines",
        ["rfq_line_id"],
        unique=False,
    )
    op.create_table(
        "work_orders",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("company_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("code", sa.String(length=32), nullable=False),
        sa.Column("production_order_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("routing_operation_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("operation_code", sa.String(length=32), nullable=False),
        sa.Column("operation_name", sa.String(length=255), nullable=False),
        sa.Column("work_center_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("machine_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("operator_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("shift", sa.String(length=16), nullable=True),
        sa.Column("planned_start_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("planned_end_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("actual_start_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("actual_end_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("planned_quantity", sa.Numeric(precision=18, scale=4), nullable=False),
        sa.Column(
            "completed_quantity",
            sa.Numeric(precision=18, scale=4),
            server_default="0",
            nullable=False,
        ),
        sa.Column(
            "rejected_quantity",
            sa.Numeric(precision=18, scale=4),
            server_default="0",
            nullable=False,
        ),
        sa.Column(
            "planned_setup_minutes",
            sa.Numeric(precision=18, scale=4),
            server_default="0",
            nullable=False,
        ),
        sa.Column(
            "planned_run_minutes",
            sa.Numeric(precision=18, scale=4),
            server_default="0",
            nullable=False,
        ),
        sa.Column(
            "process_cost", sa.Numeric(precision=18, scale=4), server_default="0", nullable=False
        ),
        sa.Column(
            "work_center_cost_per_hour",
            sa.Numeric(precision=18, scale=4),
            server_default="0",
            nullable=False,
        ),
        sa.Column(
            "work_center_setup_cost",
            sa.Numeric(precision=18, scale=4),
            server_default="0",
            nullable=False,
        ),
        sa.Column("status", sa.String(length=24), server_default="PENDING", nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("created_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("updated_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.CheckConstraint(
            "status <> 'COMPLETED' OR actual_start_at IS NOT NULL",
            name=op.f("ck_work_orders_completed_operations_were_started"),
        ),
        sa.CheckConstraint(
            "actual_end_at IS NULL OR actual_start_at IS NULL OR actual_end_at >= actual_start_at",
            name=op.f("ck_work_orders_actual_window_ordered"),
        ),
        sa.CheckConstraint(
            "completed_quantity >= 0", name=op.f("ck_work_orders_completed_quantity_non_negative")
        ),
        sa.CheckConstraint(
            "planned_end_at IS NULL OR planned_start_at IS NULL OR planned_end_at >= planned_start_at",
            name=op.f("ck_work_orders_planned_window_ordered"),
        ),
        sa.CheckConstraint(
            "planned_quantity > 0", name=op.f("ck_work_orders_planned_quantity_positive")
        ),
        sa.CheckConstraint(
            "planned_run_minutes >= 0", name=op.f("ck_work_orders_run_minutes_non_negative")
        ),
        sa.CheckConstraint(
            "planned_setup_minutes >= 0", name=op.f("ck_work_orders_setup_minutes_non_negative")
        ),
        sa.CheckConstraint(
            "process_cost >= 0", name=op.f("ck_work_orders_process_cost_non_negative")
        ),
        sa.CheckConstraint(
            "rejected_quantity >= 0", name=op.f("ck_work_orders_rejected_quantity_non_negative")
        ),
        sa.CheckConstraint("sequence > 0", name=op.f("ck_work_orders_sequence_positive")),
        sa.CheckConstraint(
            "work_center_cost_per_hour >= 0", name=op.f("ck_work_orders_cost_per_hour_non_negative")
        ),
        sa.CheckConstraint(
            "work_center_setup_cost >= 0", name=op.f("ck_work_orders_setup_cost_non_negative")
        ),
        sa.ForeignKeyConstraint(
            ["company_id"],
            ["companies.id"],
            name=op.f("fk_work_orders_company_id_companies"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_id"],
            ["users.id"],
            name=op.f("fk_work_orders_created_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["machine_id", "work_center_id"],
            ["machines.id", "machines.work_center_id"],
            name="fk_work_orders_machine_id_work_center_id_machines",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["operator_user_id"],
            ["users.id"],
            name=op.f("fk_work_orders_operator_user_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["production_order_id"],
            ["production_orders.id"],
            name=op.f("fk_work_orders_production_order_id_production_orders"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["updated_by_id"],
            ["users.id"],
            name=op.f("fk_work_orders_updated_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["work_center_id"],
            ["work_centers.id"],
            name=op.f("fk_work_orders_work_center_id_work_centers"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_work_orders")),
        sa.UniqueConstraint("company_id", "code", name="uq_work_orders_company_id_code"),
        sa.UniqueConstraint(
            "production_order_id", "sequence", name="uq_work_orders_order_sequence"
        ),
    )
    op.create_index(op.f("ix_work_orders_company_id"), "work_orders", ["company_id"], unique=False)
    op.create_index(op.f("ix_work_orders_created_at"), "work_orders", ["created_at"], unique=False)
    op.create_index(
        "ix_work_orders_machine",
        "work_orders",
        ["company_id", "machine_id", "status"],
        unique=False,
    )
    op.create_index(
        "ix_work_orders_operator",
        "work_orders",
        ["company_id", "operator_user_id", "status"],
        unique=False,
    )
    op.create_index(
        "ix_work_orders_order", "work_orders", ["production_order_id", "sequence"], unique=False
    )
    op.create_index(
        "ix_work_orders_work_center",
        "work_orders",
        ["company_id", "work_center_id", "status"],
        unique=False,
    )
    op.create_table(
        "carton_contents",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("company_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("carton_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("packing_order_line_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("item_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("quantity", sa.Numeric(precision=18, scale=4), nullable=False),
        sa.Column(
            "net_weight_kg", sa.Numeric(precision=18, scale=4), server_default="0", nullable=False
        ),
        sa.Column("batch_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "is_serial_tracked_item", sa.Boolean(), server_default=sa.text("false"), nullable=False
        ),
        sa.Column(
            "serial_ids",
            postgresql.ARRAY(postgresql.UUID(as_uuid=True)),
            server_default=sa.text("'{}'::uuid[]"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("created_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("updated_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.CheckConstraint(
            "NOT is_serial_tracked_item OR cardinality(serial_ids) = quantity",
            name=op.f("ck_carton_contents_serial_count_matches_quantity"),
        ),
        sa.CheckConstraint(
            "is_serial_tracked_item OR cardinality(serial_ids) = 0",
            name=op.f("ck_carton_contents_serials_only_on_serialised_items"),
        ),
        sa.CheckConstraint(
            "net_weight_kg >= 0", name=op.f("ck_carton_contents_net_weight_non_negative")
        ),
        sa.CheckConstraint("quantity > 0", name=op.f("ck_carton_contents_quantity_positive")),
        sa.ForeignKeyConstraint(
            ["batch_id"],
            ["stock_batches.id"],
            name=op.f("fk_carton_contents_batch_id_stock_batches"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["carton_id"],
            ["cartons.id"],
            name=op.f("fk_carton_contents_carton_id_cartons"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["company_id"],
            ["companies.id"],
            name=op.f("fk_carton_contents_company_id_companies"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_id"],
            ["users.id"],
            name=op.f("fk_carton_contents_created_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["item_id"],
            ["items.id"],
            name=op.f("fk_carton_contents_item_id_items"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["packing_order_line_id"],
            ["packing_order_lines.id"],
            name=op.f("fk_carton_contents_packing_order_line_id_packing_order_lines"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["updated_by_id"],
            ["users.id"],
            name=op.f("fk_carton_contents_updated_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_carton_contents")),
    )
    op.create_index(
        op.f("ix_carton_contents_batch_id"), "carton_contents", ["batch_id"], unique=False
    )
    op.create_index(
        "ix_carton_contents_carton", "carton_contents", ["company_id", "carton_id"], unique=False
    )
    op.create_index(
        op.f("ix_carton_contents_carton_id"), "carton_contents", ["carton_id"], unique=False
    )
    op.create_index(
        op.f("ix_carton_contents_company_id"), "carton_contents", ["company_id"], unique=False
    )
    op.create_index(
        op.f("ix_carton_contents_created_at"), "carton_contents", ["created_at"], unique=False
    )
    op.create_index(
        "ix_carton_contents_item", "carton_contents", ["company_id", "item_id"], unique=False
    )
    op.create_index(
        op.f("ix_carton_contents_item_id"), "carton_contents", ["item_id"], unique=False
    )
    op.create_index(
        "ix_carton_contents_line",
        "carton_contents",
        ["company_id", "packing_order_line_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_carton_contents_packing_order_line_id"),
        "carton_contents",
        ["packing_order_line_id"],
        unique=False,
    )
    op.create_index(
        "ix_carton_contents_serials",
        "carton_contents",
        ["serial_ids"],
        unique=False,
        postgresql_using="gin",
    )
    op.create_table(
        "dispatch_lines",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("company_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("dispatch_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("line_number", sa.Integer(), nullable=False),
        sa.Column("sales_order_line_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("item_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("quantity", sa.Numeric(precision=18, scale=4), nullable=False),
        sa.Column("batch_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "serial_ids",
            postgresql.ARRAY(postgresql.UUID(as_uuid=True)),
            server_default=sa.text("'{}'::uuid[]"),
            nullable=False,
        ),
        sa.Column(
            "carton_ids",
            postgresql.ARRAY(postgresql.UUID(as_uuid=True)),
            server_default=sa.text("'{}'::uuid[]"),
            nullable=False,
        ),
        sa.Column("reservation_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("inventory_transaction_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("created_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("updated_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.CheckConstraint(
            "cardinality(carton_ids) <= 5000", name=op.f("ck_dispatch_lines_carton_count_bounded")
        ),
        sa.CheckConstraint(
            "cardinality(serial_ids) <= 10000", name=op.f("ck_dispatch_lines_serial_count_bounded")
        ),
        sa.CheckConstraint(
            "cardinality(serial_ids) = 0 OR cardinality(serial_ids) = quantity",
            name=op.f("ck_dispatch_lines_serial_count_matches_quantity"),
        ),
        sa.CheckConstraint("line_number > 0", name=op.f("ck_dispatch_lines_line_number_positive")),
        sa.CheckConstraint("quantity > 0", name=op.f("ck_dispatch_lines_quantity_positive")),
        sa.ForeignKeyConstraint(
            ["batch_id"],
            ["stock_batches.id"],
            name=op.f("fk_dispatch_lines_batch_id_stock_batches"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["company_id"],
            ["companies.id"],
            name=op.f("fk_dispatch_lines_company_id_companies"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_id"],
            ["users.id"],
            name=op.f("fk_dispatch_lines_created_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["dispatch_id"],
            ["dispatches.id"],
            name=op.f("fk_dispatch_lines_dispatch_id_dispatches"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["item_id"],
            ["items.id"],
            name=op.f("fk_dispatch_lines_item_id_items"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["sales_order_line_id"],
            ["sales_order_lines.id"],
            name=op.f("fk_dispatch_lines_sales_order_line_id_sales_order_lines"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["updated_by_id"],
            ["users.id"],
            name=op.f("fk_dispatch_lines_updated_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_dispatch_lines")),
        sa.UniqueConstraint(
            "dispatch_id", "line_number", name="uq_dispatch_lines_dispatch_line_number"
        ),
    )
    op.create_index(
        op.f("ix_dispatch_lines_batch_id"), "dispatch_lines", ["batch_id"], unique=False
    )
    op.create_index(
        "ix_dispatch_lines_cartons",
        "dispatch_lines",
        ["carton_ids"],
        unique=False,
        postgresql_using="gin",
    )
    op.create_index(
        op.f("ix_dispatch_lines_company_id"), "dispatch_lines", ["company_id"], unique=False
    )
    op.create_index(
        op.f("ix_dispatch_lines_created_at"), "dispatch_lines", ["created_at"], unique=False
    )
    op.create_index(
        "ix_dispatch_lines_dispatch",
        "dispatch_lines",
        ["company_id", "dispatch_id", "line_number"],
        unique=False,
    )
    op.create_index(
        op.f("ix_dispatch_lines_dispatch_id"), "dispatch_lines", ["dispatch_id"], unique=False
    )
    op.create_index(
        "ix_dispatch_lines_item", "dispatch_lines", ["company_id", "item_id"], unique=False
    )
    op.create_index(op.f("ix_dispatch_lines_item_id"), "dispatch_lines", ["item_id"], unique=False)
    op.create_index(
        "ix_dispatch_lines_sales_order_line",
        "dispatch_lines",
        ["company_id", "sales_order_line_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_dispatch_lines_sales_order_line_id"),
        "dispatch_lines",
        ["sales_order_line_id"],
        unique=False,
    )
    op.create_index(
        "ix_dispatch_lines_serials",
        "dispatch_lines",
        ["serial_ids"],
        unique=False,
        postgresql_using="gin",
    )
    op.create_table(
        "dispatch_tracking_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("company_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("dispatch_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("event_status", sa.String(length=24), nullable=False),
        sa.Column("event_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("location", sa.String(length=255), nullable=True),
        sa.Column("remark", sa.String(length=500), nullable=True),
        sa.Column("recorded_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("source", sa.String(length=16), server_default="MANUAL", nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("created_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.CheckConstraint(
            "event_status IN ('NOT_DISPATCHED', 'READY_FOR_DISPATCH', 'DISPATCHED', 'IN_TRANSIT', 'DELIVERED', 'QC_PENDING', 'RECEIVED', 'QC_COMPLETED', 'GRN_COMPLETED', 'CANCELLED')",
            name=op.f("ck_dispatch_tracking_events_event_status_is_known"),
        ),
        sa.CheckConstraint(
            "source IN ('MANUAL', 'COURIER_API', 'VENDOR_PORTAL')",
            name=op.f("ck_dispatch_tracking_events_source_is_known"),
        ),
        sa.ForeignKeyConstraint(
            ["company_id"],
            ["companies.id"],
            name=op.f("fk_dispatch_tracking_events_company_id_companies"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_id"],
            ["users.id"],
            name=op.f("fk_dispatch_tracking_events_created_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["dispatch_id"],
            ["dispatches.id"],
            name=op.f("fk_dispatch_tracking_events_dispatch_id_dispatches"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["recorded_by_id"],
            ["users.id"],
            name=op.f("fk_dispatch_tracking_events_recorded_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_dispatch_tracking_events")),
    )
    op.create_index(
        op.f("ix_dispatch_tracking_events_company_id"),
        "dispatch_tracking_events",
        ["company_id"],
        unique=False,
    )
    op.create_index(
        "ix_dispatch_tracking_events_dispatch",
        "dispatch_tracking_events",
        ["company_id", "dispatch_id", "event_time"],
        unique=False,
    )
    op.create_index(
        op.f("ix_dispatch_tracking_events_dispatch_id"),
        "dispatch_tracking_events",
        ["dispatch_id"],
        unique=False,
    )
    op.create_index(
        "ix_dispatch_tracking_events_keyset",
        "dispatch_tracking_events",
        ["company_id", "created_at", "id"],
        unique=False,
    )
    op.create_table(
        "material_issue_lines",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("company_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("material_issue_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("line_number", sa.Integer(), nullable=False),
        sa.Column("component_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("item_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("warehouse_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("location_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("batch_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("quantity", sa.Numeric(precision=18, scale=4), nullable=False),
        sa.Column(
            "unit_cost", sa.Numeric(precision=18, scale=6), server_default="0", nullable=False
        ),
        sa.Column(
            "total_cost", sa.Numeric(precision=18, scale=4), server_default="0", nullable=False
        ),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("created_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("updated_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.CheckConstraint("quantity > 0", name=op.f("ck_material_issue_lines_quantity_positive")),
        sa.CheckConstraint(
            "total_cost >= 0", name=op.f("ck_material_issue_lines_total_cost_non_negative")
        ),
        sa.CheckConstraint(
            "unit_cost >= 0", name=op.f("ck_material_issue_lines_unit_cost_non_negative")
        ),
        sa.ForeignKeyConstraint(
            ["batch_id"],
            ["stock_batches.id"],
            name=op.f("fk_material_issue_lines_batch_id_stock_batches"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["company_id"],
            ["companies.id"],
            name=op.f("fk_material_issue_lines_company_id_companies"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["component_id"],
            ["production_order_components.id"],
            name=op.f("fk_material_issue_lines_component_id_production_order_components"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_id"],
            ["users.id"],
            name=op.f("fk_material_issue_lines_created_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["item_id"],
            ["items.id"],
            name=op.f("fk_material_issue_lines_item_id_items"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["location_id"],
            ["storage_locations.id"],
            name=op.f("fk_material_issue_lines_location_id_storage_locations"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["material_issue_id"],
            ["material_issues.id"],
            name=op.f("fk_material_issue_lines_material_issue_id_material_issues"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["updated_by_id"],
            ["users.id"],
            name=op.f("fk_material_issue_lines_updated_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["warehouse_id"],
            ["warehouses.id"],
            name=op.f("fk_material_issue_lines_warehouse_id_warehouses"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_material_issue_lines")),
        sa.UniqueConstraint(
            "material_issue_id", "line_number", name="uq_material_issue_lines_issue_line"
        ),
    )
    op.create_index(
        op.f("ix_material_issue_lines_company_id"),
        "material_issue_lines",
        ["company_id"],
        unique=False,
    )
    op.create_index(
        "ix_material_issue_lines_component",
        "material_issue_lines",
        ["company_id", "component_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_material_issue_lines_created_at"),
        "material_issue_lines",
        ["created_at"],
        unique=False,
    )
    op.create_index(
        "ix_material_issue_lines_issue",
        "material_issue_lines",
        ["material_issue_id", "line_number"],
        unique=False,
    )
    op.create_index(
        "ix_material_issue_lines_item",
        "material_issue_lines",
        ["company_id", "item_id"],
        unique=False,
    )
    op.create_table(
        "production_entries",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("company_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("production_order_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("work_order_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("entry_date", sa.Date(), nullable=False),
        sa.Column("shift", sa.String(length=16), nullable=True),
        sa.Column("operator_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("machine_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "produced_quantity",
            sa.Numeric(precision=18, scale=4),
            server_default="0",
            nullable=False,
        ),
        sa.Column(
            "rejected_quantity",
            sa.Numeric(precision=18, scale=4),
            server_default="0",
            nullable=False,
        ),
        sa.Column("rejection_reason", sa.String(length=255), nullable=True),
        sa.Column(
            "downtime_minutes",
            sa.Numeric(precision=18, scale=4),
            server_default="0",
            nullable=False,
        ),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("output_transaction_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("created_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("updated_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.CheckConstraint(
            "downtime_minutes >= 0", name=op.f("ck_production_entries_downtime_non_negative")
        ),
        sa.CheckConstraint(
            "produced_quantity > 0 OR rejected_quantity > 0 OR downtime_minutes > 0",
            name=op.f("ck_production_entries_entry_records_something"),
        ),
        sa.CheckConstraint(
            "produced_quantity >= 0",
            name=op.f("ck_production_entries_produced_quantity_non_negative"),
        ),
        sa.CheckConstraint(
            "rejected_quantity = 0 OR rejection_reason IS NOT NULL",
            name=op.f("ck_production_entries_rejections_carry_a_reason"),
        ),
        sa.CheckConstraint(
            "rejected_quantity >= 0",
            name=op.f("ck_production_entries_rejected_quantity_non_negative"),
        ),
        sa.ForeignKeyConstraint(
            ["company_id"],
            ["companies.id"],
            name=op.f("fk_production_entries_company_id_companies"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_id"],
            ["users.id"],
            name=op.f("fk_production_entries_created_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["machine_id"],
            ["machines.id"],
            name=op.f("fk_production_entries_machine_id_machines"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["operator_user_id"],
            ["users.id"],
            name=op.f("fk_production_entries_operator_user_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["production_order_id"],
            ["production_orders.id"],
            name=op.f("fk_production_entries_production_order_id_production_orders"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["updated_by_id"],
            ["users.id"],
            name=op.f("fk_production_entries_updated_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["work_order_id"],
            ["work_orders.id"],
            name=op.f("fk_production_entries_work_order_id_work_orders"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_production_entries")),
    )
    op.create_index(
        op.f("ix_production_entries_company_id"), "production_entries", ["company_id"], unique=False
    )
    op.create_index(
        op.f("ix_production_entries_created_at"), "production_entries", ["created_at"], unique=False
    )
    op.create_index(
        "ix_production_entries_date",
        "production_entries",
        ["company_id", "entry_date", "shift"],
        unique=False,
    )
    op.create_index(
        "ix_production_entries_operator",
        "production_entries",
        ["company_id", "operator_user_id", "entry_date"],
        unique=False,
    )
    op.create_index(
        "ix_production_entries_order",
        "production_entries",
        ["company_id", "production_order_id", "entry_date"],
        unique=False,
    )
    op.create_index(
        "ix_production_entries_work_order",
        "production_entries",
        ["company_id", "work_order_id", "entry_date"],
        unique=False,
    )
    op.create_table(
        "purchase_order_lines",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("company_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("purchase_order_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("line_number", sa.Integer(), nullable=False),
        sa.Column("item_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("description", sa.String(length=500), nullable=True),
        sa.Column("quantity", sa.Numeric(precision=18, scale=4), nullable=False),
        sa.Column("uom_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("unit_price", sa.Numeric(precision=18, scale=6), nullable=False),
        sa.Column(
            "discount_percentage",
            sa.Numeric(precision=9, scale=4),
            server_default="0",
            nullable=False,
        ),
        sa.Column(
            "discount", sa.Numeric(precision=18, scale=4), server_default="0", nullable=False
        ),
        sa.Column("tax_rate", sa.Numeric(precision=9, scale=4), server_default="0", nullable=False),
        sa.Column(
            "tax_amount", sa.Numeric(precision=18, scale=4), server_default="0", nullable=False
        ),
        sa.Column(
            "line_total", sa.Numeric(precision=18, scale=4), server_default="0", nullable=False
        ),
        sa.Column("required_date", sa.Date(), nullable=True),
        sa.Column(
            "received_quantity",
            sa.Numeric(precision=18, scale=4),
            server_default="0",
            nullable=False,
        ),
        sa.Column(
            "accepted_quantity",
            sa.Numeric(precision=18, scale=4),
            server_default="0",
            nullable=False,
        ),
        sa.Column(
            "rejected_quantity",
            sa.Numeric(precision=18, scale=4),
            server_default="0",
            nullable=False,
        ),
        sa.Column(
            "returned_quantity",
            sa.Numeric(precision=18, scale=4),
            server_default="0",
            nullable=False,
        ),
        sa.Column("requires_qc", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("status", sa.String(length=24), server_default="PENDING", nullable=False),
        sa.Column("requisition_line_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("quotation_line_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("created_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("updated_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('PENDING', 'PARTIALLY_RECEIVED', 'RECEIVED', 'CLOSED', 'CANCELLED')",
            name=op.f("ck_purchase_order_lines_status_known"),
        ),
        sa.CheckConstraint(
            "accepted_quantity + rejected_quantity = received_quantity",
            name=op.f("ck_purchase_order_lines_accepted_plus_rejected_equals_received"),
        ),
        sa.CheckConstraint(
            "accepted_quantity >= 0", name=op.f("ck_purchase_order_lines_accepted_non_negative")
        ),
        sa.CheckConstraint(
            "discount >= 0", name=op.f("ck_purchase_order_lines_discount_non_negative")
        ),
        sa.CheckConstraint(
            "discount_percentage >= 0 AND discount_percentage <= 100",
            name=op.f("ck_purchase_order_lines_discount_percentage_bounded"),
        ),
        sa.CheckConstraint(
            "line_number > 0", name=op.f("ck_purchase_order_lines_line_number_positive")
        ),
        sa.CheckConstraint(
            "line_total >= 0", name=op.f("ck_purchase_order_lines_line_total_non_negative")
        ),
        sa.CheckConstraint("quantity > 0", name=op.f("ck_purchase_order_lines_quantity_positive")),
        sa.CheckConstraint(
            "received_quantity <= quantity * 1.05",
            name=op.f("ck_purchase_order_lines_received_within_over_receipt_tolerance"),
        ),
        sa.CheckConstraint(
            "received_quantity >= 0", name=op.f("ck_purchase_order_lines_received_non_negative")
        ),
        sa.CheckConstraint(
            "rejected_quantity >= 0", name=op.f("ck_purchase_order_lines_rejected_non_negative")
        ),
        sa.CheckConstraint(
            "returned_quantity <= accepted_quantity",
            name=op.f("ck_purchase_order_lines_returned_within_accepted"),
        ),
        sa.CheckConstraint(
            "returned_quantity >= 0", name=op.f("ck_purchase_order_lines_returned_non_negative")
        ),
        sa.CheckConstraint(
            "tax_amount >= 0", name=op.f("ck_purchase_order_lines_tax_non_negative")
        ),
        sa.CheckConstraint(
            "tax_rate >= 0 AND tax_rate <= 100",
            name=op.f("ck_purchase_order_lines_tax_rate_bounded"),
        ),
        sa.CheckConstraint(
            "unit_price >= 0", name=op.f("ck_purchase_order_lines_unit_price_non_negative")
        ),
        sa.ForeignKeyConstraint(
            ["company_id"],
            ["companies.id"],
            name=op.f("fk_purchase_order_lines_company_id_companies"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_id"],
            ["users.id"],
            name=op.f("fk_purchase_order_lines_created_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["item_id"],
            ["items.id"],
            name=op.f("fk_purchase_order_lines_item_id_items"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["purchase_order_id"],
            ["purchase_orders.id"],
            name=op.f("fk_purchase_order_lines_purchase_order_id_purchase_orders"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["quotation_line_id"],
            ["vendor_quotation_lines.id"],
            name=op.f("fk_purchase_order_lines_quotation_line_id_vendor_quotation_lines"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["requisition_line_id"],
            ["purchase_requisition_lines.id"],
            name=op.f("fk_purchase_order_lines_requisition_line_id_purchase_requisition_lines"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["uom_id"],
            ["units_of_measure.id"],
            name=op.f("fk_purchase_order_lines_uom_id_units_of_measure"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["updated_by_id"],
            ["users.id"],
            name=op.f("fk_purchase_order_lines_updated_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_purchase_order_lines")),
        sa.UniqueConstraint(
            "purchase_order_id", "line_number", name="uq_purchase_order_lines_line_number"
        ),
    )
    op.create_index(
        op.f("ix_purchase_order_lines_company_id"),
        "purchase_order_lines",
        ["company_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_purchase_order_lines_created_at"),
        "purchase_order_lines",
        ["created_at"],
        unique=False,
    )
    op.create_index(
        op.f("ix_purchase_order_lines_item_id"), "purchase_order_lines", ["item_id"], unique=False
    )
    op.create_index(
        "ix_purchase_order_lines_open_item",
        "purchase_order_lines",
        ["company_id", "item_id", "required_date"],
        unique=False,
        postgresql_where=sa.text("status IN ('PENDING', 'PARTIALLY_RECEIVED')"),
    )
    op.create_index(
        "ix_purchase_order_lines_order",
        "purchase_order_lines",
        ["company_id", "purchase_order_id", "line_number"],
        unique=False,
    )
    op.create_index(
        op.f("ix_purchase_order_lines_purchase_order_id"),
        "purchase_order_lines",
        ["purchase_order_id"],
        unique=False,
    )
    op.create_index(
        "ix_purchase_order_lines_requisition_line",
        "purchase_order_lines",
        ["company_id", "requisition_line_id"],
        unique=False,
    )
    op.create_table(
        "goods_receipt_lines",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("company_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("goods_receipt_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("line_number", sa.Integer(), nullable=False),
        sa.Column("purchase_order_line_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("item_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("description", sa.String(length=500), nullable=True),
        sa.Column("received_quantity", sa.Numeric(precision=18, scale=4), nullable=False),
        sa.Column(
            "accepted_quantity",
            sa.Numeric(precision=18, scale=4),
            server_default="0",
            nullable=False,
        ),
        sa.Column(
            "rejected_quantity",
            sa.Numeric(precision=18, scale=4),
            server_default="0",
            nullable=False,
        ),
        sa.Column(
            "returned_quantity",
            sa.Numeric(precision=18, scale=4),
            server_default="0",
            nullable=False,
        ),
        sa.Column("uom_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "unit_cost", sa.Numeric(precision=18, scale=6), server_default="0", nullable=False
        ),
        sa.Column("batch_number", sa.String(length=64), nullable=True),
        sa.Column("batch_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("manufactured_on", sa.Date(), nullable=True),
        sa.Column("expires_on", sa.Date(), nullable=True),
        sa.Column("location_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("requires_qc", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("qc_status", sa.String(length=24), server_default="NOT_REQUIRED", nullable=False),
        sa.Column("qc_inspection_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("posted_stock_state", sa.String(length=24), nullable=True),
        sa.Column("rejection_reason", sa.Text(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("created_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("updated_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.CheckConstraint(
            "NOT requires_qc OR qc_status <> 'NOT_REQUIRED'",
            name=op.f("ck_goods_receipt_lines_qc_required_lines_have_a_qc_status"),
        ),
        sa.CheckConstraint(
            "qc_status IN ('NOT_REQUIRED', 'PENDING', 'PASSED', 'FAILED')",
            name=op.f("ck_goods_receipt_lines_qc_status_known"),
        ),
        sa.CheckConstraint(
            "accepted_quantity + rejected_quantity = received_quantity",
            name=op.f("ck_goods_receipt_lines_accepted_plus_rejected_equals_received"),
        ),
        sa.CheckConstraint(
            "accepted_quantity >= 0", name=op.f("ck_goods_receipt_lines_accepted_non_negative")
        ),
        sa.CheckConstraint(
            "expires_on IS NULL OR manufactured_on IS NULL OR expires_on >= manufactured_on",
            name=op.f("ck_goods_receipt_lines_expiry_not_before_manufacture"),
        ),
        sa.CheckConstraint(
            "line_number > 0", name=op.f("ck_goods_receipt_lines_line_number_positive")
        ),
        sa.CheckConstraint(
            "received_quantity > 0", name=op.f("ck_goods_receipt_lines_received_positive")
        ),
        sa.CheckConstraint(
            "rejected_quantity = 0 OR rejection_reason IS NOT NULL",
            name=op.f("ck_goods_receipt_lines_rejections_carry_a_reason"),
        ),
        sa.CheckConstraint(
            "rejected_quantity >= 0", name=op.f("ck_goods_receipt_lines_rejected_non_negative")
        ),
        sa.CheckConstraint(
            "returned_quantity <= accepted_quantity",
            name=op.f("ck_goods_receipt_lines_returned_within_accepted"),
        ),
        sa.CheckConstraint(
            "returned_quantity >= 0", name=op.f("ck_goods_receipt_lines_returned_non_negative")
        ),
        sa.CheckConstraint(
            "unit_cost >= 0", name=op.f("ck_goods_receipt_lines_unit_cost_non_negative")
        ),
        sa.ForeignKeyConstraint(
            ["company_id"],
            ["companies.id"],
            name=op.f("fk_goods_receipt_lines_company_id_companies"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_id"],
            ["users.id"],
            name=op.f("fk_goods_receipt_lines_created_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["goods_receipt_id"],
            ["goods_receipts.id"],
            name=op.f("fk_goods_receipt_lines_goods_receipt_id_goods_receipts"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["item_id"],
            ["items.id"],
            name=op.f("fk_goods_receipt_lines_item_id_items"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["location_id"],
            ["storage_locations.id"],
            name=op.f("fk_goods_receipt_lines_location_id_storage_locations"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["purchase_order_line_id"],
            ["purchase_order_lines.id"],
            name=op.f("fk_goods_receipt_lines_purchase_order_line_id_purchase_order_lines"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["uom_id"],
            ["units_of_measure.id"],
            name=op.f("fk_goods_receipt_lines_uom_id_units_of_measure"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["updated_by_id"],
            ["users.id"],
            name=op.f("fk_goods_receipt_lines_updated_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_goods_receipt_lines")),
        sa.UniqueConstraint(
            "goods_receipt_id", "line_number", name="uq_goods_receipt_lines_line_number"
        ),
    )
    op.create_index(
        op.f("ix_goods_receipt_lines_company_id"),
        "goods_receipt_lines",
        ["company_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_goods_receipt_lines_created_at"),
        "goods_receipt_lines",
        ["created_at"],
        unique=False,
    )
    op.create_index(
        op.f("ix_goods_receipt_lines_goods_receipt_id"),
        "goods_receipt_lines",
        ["goods_receipt_id"],
        unique=False,
    )
    op.create_index(
        "ix_goods_receipt_lines_item",
        "goods_receipt_lines",
        ["company_id", "item_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_goods_receipt_lines_item_id"), "goods_receipt_lines", ["item_id"], unique=False
    )
    op.create_index(
        "ix_goods_receipt_lines_po_line",
        "goods_receipt_lines",
        ["company_id", "purchase_order_line_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_goods_receipt_lines_purchase_order_line_id"),
        "goods_receipt_lines",
        ["purchase_order_line_id"],
        unique=False,
    )
    op.create_index(
        "ix_goods_receipt_lines_qc_pending",
        "goods_receipt_lines",
        ["company_id", "item_id"],
        unique=False,
        postgresql_where=sa.text("qc_status = 'PENDING'"),
    )
    op.create_index(
        "ix_goods_receipt_lines_receipt",
        "goods_receipt_lines",
        ["company_id", "goods_receipt_id", "line_number"],
        unique=False,
    )
    op.create_table(
        "purchase_return_lines",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("company_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("purchase_return_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("line_number", sa.Integer(), nullable=False),
        sa.Column("goods_receipt_line_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("item_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("description", sa.String(length=500), nullable=True),
        sa.Column("quantity", sa.Numeric(precision=18, scale=4), nullable=False),
        sa.Column("uom_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("unit_price", sa.Numeric(precision=18, scale=6), nullable=False),
        sa.Column("tax_rate", sa.Numeric(precision=9, scale=4), server_default="0", nullable=False),
        sa.Column(
            "tax_amount", sa.Numeric(precision=18, scale=4), server_default="0", nullable=False
        ),
        sa.Column(
            "line_total", sa.Numeric(precision=18, scale=4), server_default="0", nullable=False
        ),
        sa.Column("batch_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("location_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("reason", sa.String(length=255), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("created_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("updated_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.CheckConstraint(
            "line_number > 0", name=op.f("ck_purchase_return_lines_line_number_positive")
        ),
        sa.CheckConstraint(
            "line_total >= 0", name=op.f("ck_purchase_return_lines_line_total_non_negative")
        ),
        sa.CheckConstraint("quantity > 0", name=op.f("ck_purchase_return_lines_quantity_positive")),
        sa.CheckConstraint(
            "tax_amount >= 0", name=op.f("ck_purchase_return_lines_tax_non_negative")
        ),
        sa.CheckConstraint(
            "tax_rate >= 0 AND tax_rate <= 100",
            name=op.f("ck_purchase_return_lines_tax_rate_bounded"),
        ),
        sa.CheckConstraint(
            "unit_price >= 0", name=op.f("ck_purchase_return_lines_unit_price_non_negative")
        ),
        sa.ForeignKeyConstraint(
            ["company_id"],
            ["companies.id"],
            name=op.f("fk_purchase_return_lines_company_id_companies"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_id"],
            ["users.id"],
            name=op.f("fk_purchase_return_lines_created_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["goods_receipt_line_id"],
            ["goods_receipt_lines.id"],
            name=op.f("fk_purchase_return_lines_goods_receipt_line_id_goods_receipt_lines"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["item_id"],
            ["items.id"],
            name=op.f("fk_purchase_return_lines_item_id_items"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["location_id"],
            ["storage_locations.id"],
            name=op.f("fk_purchase_return_lines_location_id_storage_locations"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["purchase_return_id"],
            ["purchase_returns.id"],
            name=op.f("fk_purchase_return_lines_purchase_return_id_purchase_returns"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["uom_id"],
            ["units_of_measure.id"],
            name=op.f("fk_purchase_return_lines_uom_id_units_of_measure"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["updated_by_id"],
            ["users.id"],
            name=op.f("fk_purchase_return_lines_updated_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_purchase_return_lines")),
        sa.UniqueConstraint(
            "purchase_return_id", "line_number", name="uq_purchase_return_lines_line_number"
        ),
    )
    op.create_index(
        op.f("ix_purchase_return_lines_company_id"),
        "purchase_return_lines",
        ["company_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_purchase_return_lines_created_at"),
        "purchase_return_lines",
        ["created_at"],
        unique=False,
    )
    op.create_index(
        op.f("ix_purchase_return_lines_goods_receipt_line_id"),
        "purchase_return_lines",
        ["goods_receipt_line_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_purchase_return_lines_item_id"), "purchase_return_lines", ["item_id"], unique=False
    )
    op.create_index(
        op.f("ix_purchase_return_lines_purchase_return_id"),
        "purchase_return_lines",
        ["purchase_return_id"],
        unique=False,
    )
    op.create_index(
        "ix_purchase_return_lines_receipt_line",
        "purchase_return_lines",
        ["company_id", "goods_receipt_line_id"],
        unique=False,
    )
    op.create_index(
        "ix_purchase_return_lines_return",
        "purchase_return_lines",
        ["company_id", "purchase_return_id", "line_number"],
        unique=False,
    )
    op.create_table(
        "vendor_invoice_lines",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("company_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("vendor_invoice_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("line_number", sa.Integer(), nullable=False),
        sa.Column("purchase_order_line_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("goods_receipt_line_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("item_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("description", sa.String(length=500), nullable=True),
        sa.Column("hsn_code", sa.String(length=16), nullable=True),
        sa.Column("quantity", sa.Numeric(precision=18, scale=4), nullable=False),
        sa.Column("uom_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("unit_price", sa.Numeric(precision=18, scale=6), nullable=False),
        sa.Column(
            "discount_amount", sa.Numeric(precision=18, scale=4), server_default="0", nullable=False
        ),
        sa.Column(
            "taxable_amount", sa.Numeric(precision=18, scale=4), server_default="0", nullable=False
        ),
        sa.Column("tax_rate", sa.Numeric(precision=9, scale=4), server_default="0", nullable=False),
        sa.Column(
            "cgst_amount", sa.Numeric(precision=18, scale=4), server_default="0", nullable=False
        ),
        sa.Column(
            "sgst_amount", sa.Numeric(precision=18, scale=4), server_default="0", nullable=False
        ),
        sa.Column(
            "igst_amount", sa.Numeric(precision=18, scale=4), server_default="0", nullable=False
        ),
        sa.Column(
            "tax_amount", sa.Numeric(precision=18, scale=4), server_default="0", nullable=False
        ),
        sa.Column(
            "line_total", sa.Numeric(precision=18, scale=4), server_default="0", nullable=False
        ),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("created_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("updated_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.CheckConstraint(
            "cgst_amount >= 0", name=op.f("ck_vendor_invoice_lines_cgst_non_negative")
        ),
        sa.CheckConstraint(
            "discount_amount >= 0", name=op.f("ck_vendor_invoice_lines_discount_non_negative")
        ),
        sa.CheckConstraint(
            "igst_amount = 0 OR (cgst_amount = 0 AND sgst_amount = 0)",
            name=op.f("ck_vendor_invoice_lines_igst_excludes_cgst_and_sgst"),
        ),
        sa.CheckConstraint(
            "igst_amount >= 0", name=op.f("ck_vendor_invoice_lines_igst_non_negative")
        ),
        sa.CheckConstraint(
            "line_number > 0", name=op.f("ck_vendor_invoice_lines_line_number_positive")
        ),
        sa.CheckConstraint(
            "line_total = taxable_amount + tax_amount",
            name=op.f("ck_vendor_invoice_lines_line_total_is_taxable_plus_tax"),
        ),
        sa.CheckConstraint("quantity > 0", name=op.f("ck_vendor_invoice_lines_quantity_positive")),
        sa.CheckConstraint(
            "sgst_amount >= 0", name=op.f("ck_vendor_invoice_lines_sgst_non_negative")
        ),
        sa.CheckConstraint(
            "tax_amount = cgst_amount + sgst_amount + igst_amount",
            name=op.f("ck_vendor_invoice_lines_tax_is_the_sum_of_splits"),
        ),
        sa.CheckConstraint(
            "tax_rate >= 0 AND tax_rate <= 100",
            name=op.f("ck_vendor_invoice_lines_tax_rate_bounded"),
        ),
        sa.CheckConstraint(
            "taxable_amount >= 0", name=op.f("ck_vendor_invoice_lines_taxable_non_negative")
        ),
        sa.CheckConstraint(
            "unit_price >= 0", name=op.f("ck_vendor_invoice_lines_unit_price_non_negative")
        ),
        sa.ForeignKeyConstraint(
            ["company_id"],
            ["companies.id"],
            name=op.f("fk_vendor_invoice_lines_company_id_companies"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_id"],
            ["users.id"],
            name=op.f("fk_vendor_invoice_lines_created_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["goods_receipt_line_id"],
            ["goods_receipt_lines.id"],
            name=op.f("fk_vendor_invoice_lines_goods_receipt_line_id_goods_receipt_lines"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["item_id"],
            ["items.id"],
            name=op.f("fk_vendor_invoice_lines_item_id_items"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["purchase_order_line_id"],
            ["purchase_order_lines.id"],
            name=op.f("fk_vendor_invoice_lines_purchase_order_line_id_purchase_order_lines"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["uom_id"],
            ["units_of_measure.id"],
            name=op.f("fk_vendor_invoice_lines_uom_id_units_of_measure"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["updated_by_id"],
            ["users.id"],
            name=op.f("fk_vendor_invoice_lines_updated_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["vendor_invoice_id"],
            ["vendor_invoices.id"],
            name=op.f("fk_vendor_invoice_lines_vendor_invoice_id_vendor_invoices"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_vendor_invoice_lines")),
        sa.UniqueConstraint(
            "vendor_invoice_id", "line_number", name="uq_vendor_invoice_lines_line_number"
        ),
    )
    op.create_index(
        op.f("ix_vendor_invoice_lines_company_id"),
        "vendor_invoice_lines",
        ["company_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_vendor_invoice_lines_created_at"),
        "vendor_invoice_lines",
        ["created_at"],
        unique=False,
    )
    op.create_index(
        op.f("ix_vendor_invoice_lines_goods_receipt_line_id"),
        "vendor_invoice_lines",
        ["goods_receipt_line_id"],
        unique=False,
    )
    op.create_index(
        "ix_vendor_invoice_lines_hsn",
        "vendor_invoice_lines",
        ["company_id", "hsn_code", "tax_rate"],
        unique=False,
    )
    op.create_index(
        "ix_vendor_invoice_lines_invoice",
        "vendor_invoice_lines",
        ["company_id", "vendor_invoice_id", "line_number"],
        unique=False,
    )
    op.create_index(
        op.f("ix_vendor_invoice_lines_item_id"), "vendor_invoice_lines", ["item_id"], unique=False
    )
    op.create_index(
        "ix_vendor_invoice_lines_po_line",
        "vendor_invoice_lines",
        ["company_id", "purchase_order_line_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_vendor_invoice_lines_purchase_order_line_id"),
        "vendor_invoice_lines",
        ["purchase_order_line_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_vendor_invoice_lines_vendor_invoice_id"),
        "vendor_invoice_lines",
        ["vendor_invoice_id"],
        unique=False,
    )

    # --- the ten deferred foreign keys, now that users exists ---------------
    # Removed from the CREATE TABLEs above to break the organisation/users
    # cycle. Same names, same ON DELETE behaviour, so the finished schema is
    # identical to the one the models describe.
    op.create_foreign_key(
        "fk_branches_created_by_id_users",
        "branches",
        "users",
        ["created_by_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_foreign_key(
        "fk_branches_updated_by_id_users",
        "branches",
        "users",
        ["updated_by_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_foreign_key(
        "fk_plants_created_by_id_users",
        "plants",
        "users",
        ["created_by_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_foreign_key(
        "fk_plants_updated_by_id_users",
        "plants",
        "users",
        ["updated_by_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_foreign_key(
        "fk_departments_created_by_id_users",
        "departments",
        "users",
        ["created_by_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_foreign_key(
        "fk_departments_head_user_id_users",
        "departments",
        "users",
        ["head_user_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_foreign_key(
        "fk_departments_updated_by_id_users",
        "departments",
        "users",
        ["updated_by_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_foreign_key(
        "fk_vendors_approved_by_id_users",
        "vendors",
        "users",
        ["approved_by_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_foreign_key(
        "fk_vendors_created_by_id_users",
        "vendors",
        "users",
        ["created_by_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_foreign_key(
        "fk_vendors_updated_by_id_users",
        "vendors",
        "users",
        ["updated_by_id"],
        ["id"],
        ondelete="SET NULL",
    )

    # --- append-only enforcement --------------------------------------------
    op.execute(REJECT_MUTATION_FUNCTION)
    for trigger_name, table_name, timing in IMMUTABILITY_TRIGGERS:
        op.execute(
            f"CREATE TRIGGER {trigger_name} {timing} ON {table_name} "
            f"FOR EACH ROW EXECUTE FUNCTION atcraft_reject_row_mutation()"
        )


def downgrade() -> None:
    # --- append-only enforcement --------------------------------------------
    # DROP TABLE would take the triggers with it but not the function, and
    # being explicit keeps the two halves of this revision symmetrical.
    for trigger_name, table_name, _timing in IMMUTABILITY_TRIGGERS:
        op.execute(f"DROP TRIGGER IF EXISTS {trigger_name} ON {table_name}")
    op.execute("DROP FUNCTION IF EXISTS atcraft_reject_row_mutation()")

    # --- the deferred foreign keys, before the tables they tie together ------
    op.drop_constraint("fk_branches_created_by_id_users", "branches", type_="foreignkey")
    op.drop_constraint("fk_branches_updated_by_id_users", "branches", type_="foreignkey")
    op.drop_constraint("fk_plants_created_by_id_users", "plants", type_="foreignkey")
    op.drop_constraint("fk_plants_updated_by_id_users", "plants", type_="foreignkey")
    op.drop_constraint("fk_departments_created_by_id_users", "departments", type_="foreignkey")
    op.drop_constraint("fk_departments_head_user_id_users", "departments", type_="foreignkey")
    op.drop_constraint("fk_departments_updated_by_id_users", "departments", type_="foreignkey")
    op.drop_constraint("fk_vendors_approved_by_id_users", "vendors", type_="foreignkey")
    op.drop_constraint("fk_vendors_created_by_id_users", "vendors", type_="foreignkey")
    op.drop_constraint("fk_vendors_updated_by_id_users", "vendors", type_="foreignkey")

    # --- tables, the exact reverse of the creation order --------------------
    op.drop_index(
        op.f("ix_vendor_invoice_lines_vendor_invoice_id"), table_name="vendor_invoice_lines"
    )
    op.drop_index(
        op.f("ix_vendor_invoice_lines_purchase_order_line_id"), table_name="vendor_invoice_lines"
    )
    op.drop_index("ix_vendor_invoice_lines_po_line", table_name="vendor_invoice_lines")
    op.drop_index(op.f("ix_vendor_invoice_lines_item_id"), table_name="vendor_invoice_lines")
    op.drop_index("ix_vendor_invoice_lines_invoice", table_name="vendor_invoice_lines")
    op.drop_index("ix_vendor_invoice_lines_hsn", table_name="vendor_invoice_lines")
    op.drop_index(
        op.f("ix_vendor_invoice_lines_goods_receipt_line_id"), table_name="vendor_invoice_lines"
    )
    op.drop_index(op.f("ix_vendor_invoice_lines_created_at"), table_name="vendor_invoice_lines")
    op.drop_index(op.f("ix_vendor_invoice_lines_company_id"), table_name="vendor_invoice_lines")
    op.drop_table("vendor_invoice_lines")
    op.drop_index("ix_purchase_return_lines_return", table_name="purchase_return_lines")
    op.drop_index("ix_purchase_return_lines_receipt_line", table_name="purchase_return_lines")
    op.drop_index(
        op.f("ix_purchase_return_lines_purchase_return_id"), table_name="purchase_return_lines"
    )
    op.drop_index(op.f("ix_purchase_return_lines_item_id"), table_name="purchase_return_lines")
    op.drop_index(
        op.f("ix_purchase_return_lines_goods_receipt_line_id"), table_name="purchase_return_lines"
    )
    op.drop_index(op.f("ix_purchase_return_lines_created_at"), table_name="purchase_return_lines")
    op.drop_index(op.f("ix_purchase_return_lines_company_id"), table_name="purchase_return_lines")
    op.drop_table("purchase_return_lines")
    op.drop_index("ix_goods_receipt_lines_receipt", table_name="goods_receipt_lines")
    op.drop_index(
        "ix_goods_receipt_lines_qc_pending",
        table_name="goods_receipt_lines",
        postgresql_where=sa.text("qc_status = 'PENDING'"),
    )
    op.drop_index(
        op.f("ix_goods_receipt_lines_purchase_order_line_id"), table_name="goods_receipt_lines"
    )
    op.drop_index("ix_goods_receipt_lines_po_line", table_name="goods_receipt_lines")
    op.drop_index(op.f("ix_goods_receipt_lines_item_id"), table_name="goods_receipt_lines")
    op.drop_index("ix_goods_receipt_lines_item", table_name="goods_receipt_lines")
    op.drop_index(op.f("ix_goods_receipt_lines_goods_receipt_id"), table_name="goods_receipt_lines")
    op.drop_index(op.f("ix_goods_receipt_lines_created_at"), table_name="goods_receipt_lines")
    op.drop_index(op.f("ix_goods_receipt_lines_company_id"), table_name="goods_receipt_lines")
    op.drop_table("goods_receipt_lines")
    op.drop_index("ix_purchase_order_lines_requisition_line", table_name="purchase_order_lines")
    op.drop_index(
        op.f("ix_purchase_order_lines_purchase_order_id"), table_name="purchase_order_lines"
    )
    op.drop_index("ix_purchase_order_lines_order", table_name="purchase_order_lines")
    op.drop_index(
        "ix_purchase_order_lines_open_item",
        table_name="purchase_order_lines",
        postgresql_where=sa.text("status IN ('PENDING', 'PARTIALLY_RECEIVED')"),
    )
    op.drop_index(op.f("ix_purchase_order_lines_item_id"), table_name="purchase_order_lines")
    op.drop_index(op.f("ix_purchase_order_lines_created_at"), table_name="purchase_order_lines")
    op.drop_index(op.f("ix_purchase_order_lines_company_id"), table_name="purchase_order_lines")
    op.drop_table("purchase_order_lines")
    op.drop_index("ix_production_entries_work_order", table_name="production_entries")
    op.drop_index("ix_production_entries_order", table_name="production_entries")
    op.drop_index("ix_production_entries_operator", table_name="production_entries")
    op.drop_index("ix_production_entries_date", table_name="production_entries")
    op.drop_index(op.f("ix_production_entries_created_at"), table_name="production_entries")
    op.drop_index(op.f("ix_production_entries_company_id"), table_name="production_entries")
    op.drop_table("production_entries")
    op.drop_index("ix_material_issue_lines_item", table_name="material_issue_lines")
    op.drop_index("ix_material_issue_lines_issue", table_name="material_issue_lines")
    op.drop_index(op.f("ix_material_issue_lines_created_at"), table_name="material_issue_lines")
    op.drop_index("ix_material_issue_lines_component", table_name="material_issue_lines")
    op.drop_index(op.f("ix_material_issue_lines_company_id"), table_name="material_issue_lines")
    op.drop_table("material_issue_lines")
    op.drop_index("ix_dispatch_tracking_events_keyset", table_name="dispatch_tracking_events")
    op.drop_index(
        op.f("ix_dispatch_tracking_events_dispatch_id"), table_name="dispatch_tracking_events"
    )
    op.drop_index("ix_dispatch_tracking_events_dispatch", table_name="dispatch_tracking_events")
    op.drop_index(
        op.f("ix_dispatch_tracking_events_company_id"), table_name="dispatch_tracking_events"
    )
    op.drop_table("dispatch_tracking_events")
    op.drop_index("ix_dispatch_lines_serials", table_name="dispatch_lines", postgresql_using="gin")
    op.drop_index(op.f("ix_dispatch_lines_sales_order_line_id"), table_name="dispatch_lines")
    op.drop_index("ix_dispatch_lines_sales_order_line", table_name="dispatch_lines")
    op.drop_index(op.f("ix_dispatch_lines_item_id"), table_name="dispatch_lines")
    op.drop_index("ix_dispatch_lines_item", table_name="dispatch_lines")
    op.drop_index(op.f("ix_dispatch_lines_dispatch_id"), table_name="dispatch_lines")
    op.drop_index("ix_dispatch_lines_dispatch", table_name="dispatch_lines")
    op.drop_index(op.f("ix_dispatch_lines_created_at"), table_name="dispatch_lines")
    op.drop_index(op.f("ix_dispatch_lines_company_id"), table_name="dispatch_lines")
    op.drop_index("ix_dispatch_lines_cartons", table_name="dispatch_lines", postgresql_using="gin")
    op.drop_index(op.f("ix_dispatch_lines_batch_id"), table_name="dispatch_lines")
    op.drop_table("dispatch_lines")
    op.drop_index(
        "ix_carton_contents_serials", table_name="carton_contents", postgresql_using="gin"
    )
    op.drop_index(op.f("ix_carton_contents_packing_order_line_id"), table_name="carton_contents")
    op.drop_index("ix_carton_contents_line", table_name="carton_contents")
    op.drop_index(op.f("ix_carton_contents_item_id"), table_name="carton_contents")
    op.drop_index("ix_carton_contents_item", table_name="carton_contents")
    op.drop_index(op.f("ix_carton_contents_created_at"), table_name="carton_contents")
    op.drop_index(op.f("ix_carton_contents_company_id"), table_name="carton_contents")
    op.drop_index(op.f("ix_carton_contents_carton_id"), table_name="carton_contents")
    op.drop_index("ix_carton_contents_carton", table_name="carton_contents")
    op.drop_index(op.f("ix_carton_contents_batch_id"), table_name="carton_contents")
    op.drop_table("carton_contents")
    op.drop_index("ix_work_orders_work_center", table_name="work_orders")
    op.drop_index("ix_work_orders_order", table_name="work_orders")
    op.drop_index("ix_work_orders_operator", table_name="work_orders")
    op.drop_index("ix_work_orders_machine", table_name="work_orders")
    op.drop_index(op.f("ix_work_orders_created_at"), table_name="work_orders")
    op.drop_index(op.f("ix_work_orders_company_id"), table_name="work_orders")
    op.drop_table("work_orders")
    op.drop_index(
        op.f("ix_vendor_quotation_lines_rfq_line_id"), table_name="vendor_quotation_lines"
    )
    op.drop_index("ix_vendor_quotation_lines_rfq_line", table_name="vendor_quotation_lines")
    op.drop_index(
        op.f("ix_vendor_quotation_lines_quotation_id"), table_name="vendor_quotation_lines"
    )
    op.drop_index("ix_vendor_quotation_lines_quotation", table_name="vendor_quotation_lines")
    op.drop_index(op.f("ix_vendor_quotation_lines_item_id"), table_name="vendor_quotation_lines")
    op.drop_index("ix_vendor_quotation_lines_item", table_name="vendor_quotation_lines")
    op.drop_index(op.f("ix_vendor_quotation_lines_created_at"), table_name="vendor_quotation_lines")
    op.drop_index(op.f("ix_vendor_quotation_lines_company_id"), table_name="vendor_quotation_lines")
    op.drop_table("vendor_quotation_lines")
    op.drop_index("ix_reallocation_requests_status", table_name="reallocation_requests")
    op.drop_index(
        "ix_reallocation_requests_pending",
        table_name="reallocation_requests",
        postgresql_where=sa.text("status = 'PENDING'"),
    )
    op.drop_index("ix_reallocation_requests_item", table_name="reallocation_requests")
    op.drop_index("ix_reallocation_requests_donor", table_name="reallocation_requests")
    op.drop_index(op.f("ix_reallocation_requests_created_at"), table_name="reallocation_requests")
    op.drop_index(op.f("ix_reallocation_requests_company_id"), table_name="reallocation_requests")
    op.drop_table("reallocation_requests")
    op.drop_index("ix_qc_inspection_results_sheet", table_name="qc_inspection_results")
    op.drop_index("ix_qc_inspection_results_parameter", table_name="qc_inspection_results")
    op.drop_index(
        op.f("ix_qc_inspection_results_inspection_id"), table_name="qc_inspection_results"
    )
    op.drop_index(op.f("ix_qc_inspection_results_created_at"), table_name="qc_inspection_results")
    op.drop_index(op.f("ix_qc_inspection_results_company_id"), table_name="qc_inspection_results")
    op.drop_table("qc_inspection_results")
    op.drop_index("ix_production_order_components_order", table_name="production_order_components")
    op.drop_index("ix_production_order_components_item", table_name="production_order_components")
    op.drop_index(
        op.f("ix_production_order_components_created_at"), table_name="production_order_components"
    )
    op.drop_index(
        op.f("ix_production_order_components_company_id"), table_name="production_order_components"
    )
    op.drop_table("production_order_components")
    op.drop_index(
        op.f("ix_payment_allocations_vendor_invoice_id"), table_name="payment_allocations"
    )
    op.drop_index(op.f("ix_payment_allocations_payment_id"), table_name="payment_allocations")
    op.drop_index("ix_payment_allocations_invoice", table_name="payment_allocations")
    op.drop_index(op.f("ix_payment_allocations_created_at"), table_name="payment_allocations")
    op.drop_index(op.f("ix_payment_allocations_company_id"), table_name="payment_allocations")
    op.drop_table("payment_allocations")
    op.drop_index(
        op.f("ix_packing_order_lines_sales_order_line_id"), table_name="packing_order_lines"
    )
    op.drop_index(op.f("ix_packing_order_lines_packing_order_id"), table_name="packing_order_lines")
    op.drop_index("ix_packing_order_lines_order", table_name="packing_order_lines")
    op.drop_index(op.f("ix_packing_order_lines_item_id"), table_name="packing_order_lines")
    op.drop_index("ix_packing_order_lines_item", table_name="packing_order_lines")
    op.drop_index(op.f("ix_packing_order_lines_created_at"), table_name="packing_order_lines")
    op.drop_index(op.f("ix_packing_order_lines_company_id"), table_name="packing_order_lines")
    op.drop_table("packing_order_lines")
    op.drop_index("ix_material_issues_warehouse", table_name="material_issues")
    op.drop_index("ix_material_issues_status", table_name="material_issues")
    op.drop_index("ix_material_issues_order", table_name="material_issues")
    op.drop_index(op.f("ix_material_issues_created_at"), table_name="material_issues")
    op.drop_index(op.f("ix_material_issues_company_id"), table_name="material_issues")
    op.drop_table("material_issues")
    op.drop_index("ix_inventory_transactions_type", table_name="inventory_transactions")
    op.drop_index("ix_inventory_transactions_serial", table_name="inventory_transactions")
    op.drop_index("ix_inventory_transactions_reference", table_name="inventory_transactions")
    op.drop_index("ix_inventory_transactions_occurred", table_name="inventory_transactions")
    op.drop_index("ix_inventory_transactions_keyset", table_name="inventory_transactions")
    op.drop_index("ix_inventory_transactions_item_history", table_name="inventory_transactions")
    op.drop_index(op.f("ix_inventory_transactions_company_id"), table_name="inventory_transactions")
    op.drop_index("ix_inventory_transactions_batch", table_name="inventory_transactions")
    op.drop_table("inventory_transactions")
    op.drop_index(op.f("ix_dispatches_warehouse_id"), table_name="dispatches")
    op.drop_index(op.f("ix_dispatches_vendor_id"), table_name="dispatches")
    op.drop_index(op.f("ix_dispatches_transporter_id"), table_name="dispatches")
    op.drop_index(
        "ix_dispatches_tracking_id",
        table_name="dispatches",
        postgresql_where=sa.text("tracking_id IS NOT NULL"),
    )
    op.drop_index(op.f("ix_dispatches_sales_order_id"), table_name="dispatches")
    op.drop_index(op.f("ix_dispatches_packing_order_id"), table_name="dispatches")
    op.drop_index(
        "ix_dispatches_in_flight_expected",
        table_name="dispatches",
        postgresql_where=sa.text(
            "status IN ('DISPATCHED', 'IN_TRANSIT') AND expected_delivery_date IS NOT NULL"
        ),
    )
    op.drop_index(op.f("ix_dispatches_customer_id"), table_name="dispatches")
    op.drop_index(op.f("ix_dispatches_created_at"), table_name="dispatches")
    op.drop_index("ix_dispatches_company_warehouse_status", table_name="dispatches")
    op.drop_index("ix_dispatches_company_vendor", table_name="dispatches")
    op.drop_index("ix_dispatches_company_status_date", table_name="dispatches")
    op.drop_index(op.f("ix_dispatches_company_id"), table_name="dispatches")
    op.drop_index("ix_dispatches_company_customer", table_name="dispatches")
    op.drop_table("dispatches")
    op.drop_index(op.f("ix_cartons_packing_order_id"), table_name="cartons")
    op.drop_index(op.f("ix_cartons_packing_material_item_id"), table_name="cartons")
    op.drop_index("ix_cartons_order", table_name="cartons")
    op.drop_index(
        "ix_cartons_open", table_name="cartons", postgresql_where=sa.text("is_sealed = false")
    )
    op.drop_index(op.f("ix_cartons_created_at"), table_name="cartons")
    op.drop_index(op.f("ix_cartons_company_id"), table_name="cartons")
    op.drop_table("cartons")
    op.drop_index(
        "ix_assembly_order_components_serials",
        table_name="assembly_order_components",
        postgresql_using="gin",
    )
    op.drop_index("ix_assembly_order_components_order", table_name="assembly_order_components")
    op.drop_index(
        op.f("ix_assembly_order_components_item_id"), table_name="assembly_order_components"
    )
    op.drop_index("ix_assembly_order_components_item", table_name="assembly_order_components")
    op.drop_index(
        op.f("ix_assembly_order_components_created_at"), table_name="assembly_order_components"
    )
    op.drop_index(
        op.f("ix_assembly_order_components_company_id"), table_name="assembly_order_components"
    )
    op.drop_index(
        op.f("ix_assembly_order_components_assembly_order_id"),
        table_name="assembly_order_components",
    )
    op.drop_table("assembly_order_components")
    op.drop_index(op.f("ix_vendor_quotations_vendor_id"), table_name="vendor_quotations")
    op.drop_index("ix_vendor_quotations_vendor", table_name="vendor_quotations")
    op.drop_index("ix_vendor_quotations_status", table_name="vendor_quotations")
    op.drop_index(op.f("ix_vendor_quotations_rfq_id"), table_name="vendor_quotations")
    op.drop_index(op.f("ix_vendor_quotations_created_at"), table_name="vendor_quotations")
    op.drop_index(op.f("ix_vendor_quotations_company_id"), table_name="vendor_quotations")
    op.drop_table("vendor_quotations")
    op.drop_index(op.f("ix_vendor_invoices_vendor_id"), table_name="vendor_invoices")
    op.drop_index("ix_vendor_invoices_vendor_date", table_name="vendor_invoices")
    op.drop_index("ix_vendor_invoices_status_due", table_name="vendor_invoices")
    op.drop_index(op.f("ix_vendor_invoices_purchase_order_id"), table_name="vendor_invoices")
    op.drop_index("ix_vendor_invoices_purchase_order", table_name="vendor_invoices")
    op.drop_index(
        "ix_vendor_invoices_open",
        table_name="vendor_invoices",
        postgresql_where=sa.text(
            "status IN ('PENDING_APPROVAL', 'APPROVED', 'PARTIALLY_PAID', 'DISPUTED')"
        ),
    )
    op.drop_index(op.f("ix_vendor_invoices_goods_receipt_id"), table_name="vendor_invoices")
    op.drop_index(op.f("ix_vendor_invoices_due_date"), table_name="vendor_invoices")
    op.drop_index(op.f("ix_vendor_invoices_created_at"), table_name="vendor_invoices")
    op.drop_index(op.f("ix_vendor_invoices_company_id"), table_name="vendor_invoices")
    op.drop_table("vendor_invoices")
    op.drop_index("ix_stock_transfer_lines_transfer", table_name="stock_transfer_lines")
    op.drop_index("ix_stock_transfer_lines_item", table_name="stock_transfer_lines")
    op.drop_index(op.f("ix_stock_transfer_lines_created_at"), table_name="stock_transfer_lines")
    op.drop_index(op.f("ix_stock_transfer_lines_company_id"), table_name="stock_transfer_lines")
    op.drop_table("stock_transfer_lines")
    op.drop_index("ix_stock_serials_number", table_name="stock_serials")
    op.drop_index("ix_stock_serials_location", table_name="stock_serials")
    op.drop_index(op.f("ix_stock_serials_created_at"), table_name="stock_serials")
    op.drop_index(op.f("ix_stock_serials_company_id"), table_name="stock_serials")
    op.drop_table("stock_serials")
    op.drop_index("ix_stock_reservations_item", table_name="stock_reservations")
    op.drop_index(
        "ix_stock_reservations_expiring",
        table_name="stock_reservations",
        postgresql_where=sa.text(
            "status IN ('ACTIVE', 'PARTIALLY_CONSUMED') AND expires_at IS NOT NULL"
        ),
    )
    op.drop_index("ix_stock_reservations_demand", table_name="stock_reservations")
    op.drop_index(op.f("ix_stock_reservations_created_at"), table_name="stock_reservations")
    op.drop_index(op.f("ix_stock_reservations_company_id"), table_name="stock_reservations")
    op.drop_index(
        "ix_stock_reservations_candidates",
        table_name="stock_reservations",
        postgresql_where=sa.text("status IN ('ACTIVE', 'PARTIALLY_CONSUMED')"),
    )
    op.drop_table("stock_reservations")
    op.drop_index("ix_stock_adjustment_lines_item", table_name="stock_adjustment_lines")
    op.drop_index(op.f("ix_stock_adjustment_lines_created_at"), table_name="stock_adjustment_lines")
    op.drop_index(op.f("ix_stock_adjustment_lines_company_id"), table_name="stock_adjustment_lines")
    op.drop_index("ix_stock_adjustment_lines_adjustment", table_name="stock_adjustment_lines")
    op.drop_table("stock_adjustment_lines")
    op.drop_index(op.f("ix_sales_order_lines_sales_order_id"), table_name="sales_order_lines")
    op.drop_index("ix_sales_order_lines_order", table_name="sales_order_lines")
    op.drop_index(
        "ix_sales_order_lines_open_item",
        table_name="sales_order_lines",
        postgresql_where=sa.text("status <> 'CANCELLED' AND dispatched_quantity < quantity"),
    )
    op.drop_index(op.f("ix_sales_order_lines_item_id"), table_name="sales_order_lines")
    op.drop_index(op.f("ix_sales_order_lines_created_at"), table_name="sales_order_lines")
    op.drop_index(op.f("ix_sales_order_lines_company_id"), table_name="sales_order_lines")
    op.drop_table("sales_order_lines")
    op.drop_index(op.f("ix_routing_operations_work_center_id"), table_name="routing_operations")
    op.drop_index("ix_routing_operations_work_center", table_name="routing_operations")
    op.drop_index(op.f("ix_routing_operations_routing_id"), table_name="routing_operations")
    op.drop_index("ix_routing_operations_routing", table_name="routing_operations")
    op.drop_index(op.f("ix_routing_operations_created_at"), table_name="routing_operations")
    op.drop_index(op.f("ix_routing_operations_company_id"), table_name="routing_operations")
    op.drop_table("routing_operations")
    op.drop_index(op.f("ix_rfq_lines_rfq_id"), table_name="rfq_lines")
    op.drop_index("ix_rfq_lines_rfq", table_name="rfq_lines")
    op.drop_index(op.f("ix_rfq_lines_item_id"), table_name="rfq_lines")
    op.drop_index("ix_rfq_lines_item", table_name="rfq_lines")
    op.drop_index(op.f("ix_rfq_lines_created_at"), table_name="rfq_lines")
    op.drop_index(op.f("ix_rfq_lines_company_id"), table_name="rfq_lines")
    op.drop_table("rfq_lines")
    op.drop_index(
        op.f("ix_qc_template_parameters_template_id"), table_name="qc_template_parameters"
    )
    op.drop_index("ix_qc_template_parameters_order", table_name="qc_template_parameters")
    op.drop_index(op.f("ix_qc_template_parameters_created_at"), table_name="qc_template_parameters")
    op.drop_index(op.f("ix_qc_template_parameters_company_id"), table_name="qc_template_parameters")
    op.drop_table("qc_template_parameters")
    op.drop_index(
        "ix_qc_inspections_vendor_history",
        table_name="qc_inspections",
        postgresql_where=sa.text("vendor_id IS NOT NULL"),
    )
    op.drop_index(
        "ix_qc_inspections_rework",
        table_name="qc_inspections",
        postgresql_where=sa.text("rework_quantity > 0 AND status <> 'CLOSED'"),
    )
    op.drop_index("ix_qc_inspections_result", table_name="qc_inspections")
    op.drop_index("ix_qc_inspections_reference", table_name="qc_inspections")
    op.drop_index(
        "ix_qc_inspections_open_queue",
        table_name="qc_inspections",
        postgresql_where=sa.text("status IN ('PENDING', 'IN_PROGRESS')"),
    )
    op.drop_index("ix_qc_inspections_item_history", table_name="qc_inspections")
    op.drop_index(op.f("ix_qc_inspections_created_at"), table_name="qc_inspections")
    op.drop_index(op.f("ix_qc_inspections_company_id"), table_name="qc_inspections")
    op.drop_table("qc_inspections")
    op.drop_index(op.f("ix_purchase_returns_warehouse_id"), table_name="purchase_returns")
    op.drop_index(op.f("ix_purchase_returns_vendor_id"), table_name="purchase_returns")
    op.drop_index("ix_purchase_returns_vendor_date", table_name="purchase_returns")
    op.drop_index("ix_purchase_returns_status", table_name="purchase_returns")
    op.drop_index("ix_purchase_returns_receipt", table_name="purchase_returns")
    op.drop_index(op.f("ix_purchase_returns_purchase_order_id"), table_name="purchase_returns")
    op.drop_index(op.f("ix_purchase_returns_goods_receipt_id"), table_name="purchase_returns")
    op.drop_index(op.f("ix_purchase_returns_created_at"), table_name="purchase_returns")
    op.drop_index(op.f("ix_purchase_returns_company_id"), table_name="purchase_returns")
    op.drop_table("purchase_returns")
    op.drop_index("ix_production_orders_warehouse", table_name="production_orders")
    op.drop_index("ix_production_orders_status", table_name="production_orders")
    op.drop_index("ix_production_orders_source", table_name="production_orders")
    op.drop_index("ix_production_orders_plant", table_name="production_orders")
    op.drop_index(
        "ix_production_orders_open",
        table_name="production_orders",
        postgresql_where=sa.text("status IN ('IN_PROCESS', 'MATERIAL_ISSUED', 'RELEASED')"),
    )
    op.drop_index("ix_production_orders_item", table_name="production_orders")
    op.drop_index(op.f("ix_production_orders_created_at"), table_name="production_orders")
    op.drop_index(op.f("ix_production_orders_company_id"), table_name="production_orders")
    op.drop_table("production_orders")
    op.drop_index("uq_physical_count_lines_position", table_name="physical_count_lines")
    op.drop_index("ix_physical_count_lines_item", table_name="physical_count_lines")
    op.drop_index(op.f("ix_physical_count_lines_created_at"), table_name="physical_count_lines")
    op.drop_index("ix_physical_count_lines_count", table_name="physical_count_lines")
    op.drop_index(op.f("ix_physical_count_lines_company_id"), table_name="physical_count_lines")
    op.drop_table("physical_count_lines")
    op.drop_index(op.f("ix_packing_orders_warehouse_id"), table_name="packing_orders")
    op.drop_index(op.f("ix_packing_orders_sales_order_id"), table_name="packing_orders")
    op.drop_index(
        "ix_packing_orders_ready_for_dispatch",
        table_name="packing_orders",
        postgresql_where=sa.text("status = 'PACKED' AND dispatch_id IS NULL"),
    )
    op.drop_index(op.f("ix_packing_orders_customer_id"), table_name="packing_orders")
    op.drop_index(op.f("ix_packing_orders_created_at"), table_name="packing_orders")
    op.drop_index("ix_packing_orders_company_warehouse_status", table_name="packing_orders")
    op.drop_index("ix_packing_orders_company_status_planned", table_name="packing_orders")
    op.drop_index(op.f("ix_packing_orders_company_id"), table_name="packing_orders")
    op.drop_index("ix_packing_orders_company_customer", table_name="packing_orders")
    op.drop_table("packing_orders")
    op.drop_index(op.f("ix_job_work_receipt_lines_receipt_id"), table_name="job_work_receipt_lines")
    op.drop_index("ix_job_work_receipt_lines_receipt", table_name="job_work_receipt_lines")
    op.drop_index("ix_job_work_receipt_lines_order_line", table_name="job_work_receipt_lines")
    op.drop_index(
        op.f("ix_job_work_receipt_lines_job_work_order_line_id"),
        table_name="job_work_receipt_lines",
    )
    op.drop_index(op.f("ix_job_work_receipt_lines_created_at"), table_name="job_work_receipt_lines")
    op.drop_index(op.f("ix_job_work_receipt_lines_company_id"), table_name="job_work_receipt_lines")
    op.drop_table("job_work_receipt_lines")
    op.drop_index(
        op.f("ix_job_work_challan_lines_job_work_order_line_id"),
        table_name="job_work_challan_lines",
    )
    op.drop_index(op.f("ix_job_work_challan_lines_created_at"), table_name="job_work_challan_lines")
    op.drop_index(op.f("ix_job_work_challan_lines_company_id"), table_name="job_work_challan_lines")
    op.drop_index(op.f("ix_job_work_challan_lines_challan_id"), table_name="job_work_challan_lines")
    op.drop_index("ix_job_work_challan_lines_challan", table_name="job_work_challan_lines")
    op.drop_table("job_work_challan_lines")
    op.drop_index(op.f("ix_item_prices_variant_id"), table_name="item_prices")
    op.drop_index(
        "ix_item_prices_lookup",
        table_name="item_prices",
        postgresql_where=sa.text("is_active AND deleted_at IS NULL"),
    )
    op.drop_index(op.f("ix_item_prices_item_id"), table_name="item_prices")
    op.drop_index(op.f("ix_item_prices_deleted_at"), table_name="item_prices")
    op.drop_index(op.f("ix_item_prices_created_at"), table_name="item_prices")
    op.drop_index(op.f("ix_item_prices_company_id"), table_name="item_prices")
    op.drop_table("item_prices")
    op.drop_index("uq_inventory_balances_grain", table_name="inventory_balances")
    op.drop_index("ix_inventory_balances_warehouse", table_name="inventory_balances")
    op.drop_index("ix_inventory_balances_item", table_name="inventory_balances")
    op.drop_index(op.f("ix_inventory_balances_created_at"), table_name="inventory_balances")
    op.drop_index(op.f("ix_inventory_balances_company_id"), table_name="inventory_balances")
    op.drop_index("ix_inventory_balances_batch", table_name="inventory_balances")
    op.drop_index(
        "ix_inventory_balances_available",
        table_name="inventory_balances",
        postgresql_where=sa.text("available_quantity > 0"),
    )
    op.drop_table("inventory_balances")
    op.drop_index(op.f("ix_bom_operations_created_at"), table_name="bom_operations")
    op.drop_index(op.f("ix_bom_operations_company_id"), table_name="bom_operations")
    op.drop_index(op.f("ix_bom_operations_bom_id"), table_name="bom_operations")
    op.drop_index("ix_bom_operations_bom", table_name="bom_operations")
    op.drop_table("bom_operations")
    op.drop_index(op.f("ix_bom_lines_created_at"), table_name="bom_lines")
    op.drop_index(op.f("ix_bom_lines_component_item_id"), table_name="bom_lines")
    op.drop_index("ix_bom_lines_component", table_name="bom_lines")
    op.drop_index(op.f("ix_bom_lines_company_id"), table_name="bom_lines")
    op.drop_index("ix_bom_lines_bom", table_name="bom_lines")
    op.drop_table("bom_lines")
    op.drop_index("ix_assembly_orders_warehouse_status", table_name="assembly_orders")
    op.drop_index(op.f("ix_assembly_orders_warehouse_id"), table_name="assembly_orders")
    op.drop_index("ix_assembly_orders_source", table_name="assembly_orders")
    op.drop_index(
        "ix_assembly_orders_ready_for_packing",
        table_name="assembly_orders",
        postgresql_where=sa.text("status = 'COMPLETED' AND packing_order_id IS NULL"),
    )
    op.drop_index(op.f("ix_assembly_orders_output_warehouse_id"), table_name="assembly_orders")
    op.drop_index(
        "ix_assembly_orders_open_supply",
        table_name="assembly_orders",
        postgresql_where=sa.text(
            "status IN ('APPROVED', 'RELEASED', 'COMPONENTS_ISSUED', 'IN_PROGRESS', 'QC_PENDING')"
        ),
    )
    op.drop_index("ix_assembly_orders_item_status", table_name="assembly_orders")
    op.drop_index(op.f("ix_assembly_orders_item_id"), table_name="assembly_orders")
    op.drop_index(op.f("ix_assembly_orders_created_at"), table_name="assembly_orders")
    op.drop_index("ix_assembly_orders_company_status_planned", table_name="assembly_orders")
    op.drop_index(op.f("ix_assembly_orders_company_id"), table_name="assembly_orders")
    op.drop_index(op.f("ix_assembly_orders_bom_id"), table_name="assembly_orders")
    op.drop_index(
        "ix_assembly_orders_awaiting_inspection",
        table_name="assembly_orders",
        postgresql_where=sa.text("qc_held_at IS NOT NULL AND qc_inspection_id IS NULL"),
    )
    op.drop_table("assembly_orders")
    op.drop_index("ix_stock_batches_number", table_name="stock_batches")
    op.drop_index(
        "ix_stock_batches_expiry",
        table_name="stock_batches",
        postgresql_where=sa.text("status = 'ACTIVE'"),
    )
    op.drop_index(op.f("ix_stock_batches_created_at"), table_name="stock_batches")
    op.drop_index(op.f("ix_stock_batches_company_id"), table_name="stock_batches")
    op.drop_table("stock_batches")
    op.drop_index("ix_sales_orders_warehouse_status", table_name="sales_orders")
    op.drop_index(op.f("ix_sales_orders_warehouse_id"), table_name="sales_orders")
    op.drop_index(
        "ix_sales_orders_open_demand",
        table_name="sales_orders",
        postgresql_where=sa.text("status IN ('CONFIRMED', 'ALLOCATED', 'PARTIALLY_DISPATCHED')"),
    )
    op.drop_index(op.f("ix_sales_orders_customer_id"), table_name="sales_orders")
    op.drop_index("ix_sales_orders_customer", table_name="sales_orders")
    op.drop_index(op.f("ix_sales_orders_created_at"), table_name="sales_orders")
    op.drop_index("ix_sales_orders_company_status_required", table_name="sales_orders")
    op.drop_index(op.f("ix_sales_orders_company_id"), table_name="sales_orders")
    op.drop_index(op.f("ix_sales_orders_branch_id"), table_name="sales_orders")
    op.drop_table("sales_orders")
    op.drop_index(
        "uq_routings_one_default_approved_per_item",
        table_name="routings",
        postgresql_where=sa.text("is_default AND status = 'APPROVED'"),
    )
    op.drop_index(op.f("ix_routings_item_id"), table_name="routings")
    op.drop_index(
        "ix_routings_effective_lookup",
        table_name="routings",
        postgresql_where=sa.text("status = 'APPROVED'"),
    )
    op.drop_index(op.f("ix_routings_created_at"), table_name="routings")
    op.drop_index("ix_routings_company_status", table_name="routings")
    op.drop_index(op.f("ix_routings_company_id"), table_name="routings")
    op.drop_table("routings")
    op.drop_index(op.f("ix_rfqs_warehouse_id"), table_name="rfqs")
    op.drop_index("ix_rfqs_status_due", table_name="rfqs")
    op.drop_index(op.f("ix_rfqs_requisition_id"), table_name="rfqs")
    op.drop_index("ix_rfqs_requisition", table_name="rfqs")
    op.drop_index(op.f("ix_rfqs_created_at"), table_name="rfqs")
    op.drop_index(op.f("ix_rfqs_company_id"), table_name="rfqs")
    op.drop_table("rfqs")
    op.drop_index(
        "uq_qc_templates_active_stage_default",
        table_name="qc_templates",
        postgresql_where=sa.text(
            "is_active AND deleted_at IS NULL AND item_id IS NULL AND item_category_id IS NULL"
        ),
    )
    op.drop_index(
        "uq_qc_templates_active_item_stage",
        table_name="qc_templates",
        postgresql_where=sa.text("is_active AND deleted_at IS NULL AND item_id IS NOT NULL"),
    )
    op.drop_index(
        "uq_qc_templates_active_category_stage",
        table_name="qc_templates",
        postgresql_where=sa.text(
            "is_active AND deleted_at IS NULL AND item_category_id IS NOT NULL"
        ),
    )
    op.drop_index("ix_qc_templates_stage", table_name="qc_templates")
    op.drop_index(op.f("ix_qc_templates_deleted_at"), table_name="qc_templates")
    op.drop_index(op.f("ix_qc_templates_created_at"), table_name="qc_templates")
    op.drop_index(op.f("ix_qc_templates_company_id"), table_name="qc_templates")
    op.drop_table("qc_templates")
    op.drop_index(
        op.f("ix_purchase_requisition_lines_requisition_id"),
        table_name="purchase_requisition_lines",
    )
    op.drop_index(
        "ix_purchase_requisition_lines_requisition", table_name="purchase_requisition_lines"
    )
    op.drop_index(
        op.f("ix_purchase_requisition_lines_item_id"), table_name="purchase_requisition_lines"
    )
    op.drop_index("ix_purchase_requisition_lines_item", table_name="purchase_requisition_lines")
    op.drop_index(
        op.f("ix_purchase_requisition_lines_created_at"), table_name="purchase_requisition_lines"
    )
    op.drop_index(
        op.f("ix_purchase_requisition_lines_company_id"), table_name="purchase_requisition_lines"
    )
    op.drop_table("purchase_requisition_lines")
    op.drop_index("ix_physical_counts_warehouse", table_name="physical_counts")
    op.drop_index("ix_physical_counts_status", table_name="physical_counts")
    op.drop_index(op.f("ix_physical_counts_created_at"), table_name="physical_counts")
    op.drop_index(op.f("ix_physical_counts_company_id"), table_name="physical_counts")
    op.drop_table("physical_counts")
    op.drop_index("ix_mrp_suggestions_run_status", table_name="mrp_suggestions")
    op.drop_index(op.f("ix_mrp_suggestions_run_id"), table_name="mrp_suggestions")
    op.drop_index(
        "ix_mrp_suggestions_open",
        table_name="mrp_suggestions",
        postgresql_where=sa.text("status = 'OPEN'"),
    )
    op.drop_index(op.f("ix_mrp_suggestions_created_at"), table_name="mrp_suggestions")
    op.drop_index(op.f("ix_mrp_suggestions_company_id"), table_name="mrp_suggestions")
    op.drop_table("mrp_suggestions")
    op.drop_index(
        "ix_mrp_requirements_shortage",
        table_name="mrp_requirements",
        postgresql_where=sa.text("shortage_quantity > 0"),
    )
    op.drop_index(op.f("ix_mrp_requirements_run_id"), table_name="mrp_requirements")
    op.drop_index("ix_mrp_requirements_item", table_name="mrp_requirements")
    op.drop_index(op.f("ix_mrp_requirements_created_at"), table_name="mrp_requirements")
    op.drop_index(op.f("ix_mrp_requirements_company_id"), table_name="mrp_requirements")
    op.drop_table("mrp_requirements")
    op.drop_index("ix_mrp_demands_source", table_name="mrp_demands")
    op.drop_index("ix_mrp_demands_run_item", table_name="mrp_demands")
    op.drop_index(op.f("ix_mrp_demands_run_id"), table_name="mrp_demands")
    op.drop_index(op.f("ix_mrp_demands_created_at"), table_name="mrp_demands")
    op.drop_index(op.f("ix_mrp_demands_company_id"), table_name="mrp_demands")
    op.drop_table("mrp_demands")
    op.drop_index(op.f("ix_job_work_receipts_vendor_id"), table_name="job_work_receipts")
    op.drop_index("ix_job_work_receipts_vendor_date", table_name="job_work_receipts")
    op.drop_index("ix_job_work_receipts_order", table_name="job_work_receipts")
    op.drop_index(op.f("ix_job_work_receipts_job_work_order_id"), table_name="job_work_receipts")
    op.drop_index(op.f("ix_job_work_receipts_created_at"), table_name="job_work_receipts")
    op.drop_index(op.f("ix_job_work_receipts_company_id"), table_name="job_work_receipts")
    op.drop_index(
        "ix_job_work_receipts_awaiting_inspection",
        table_name="job_work_receipts",
        postgresql_where=sa.text("qc_held_at IS NOT NULL AND qc_released_at IS NULL"),
    )
    op.drop_table("job_work_receipts")
    op.drop_index("ix_job_work_order_lines_output_item", table_name="job_work_order_lines")
    op.drop_index("ix_job_work_order_lines_order", table_name="job_work_order_lines")
    op.drop_index(
        op.f("ix_job_work_order_lines_job_work_order_id"), table_name="job_work_order_lines"
    )
    op.drop_index(op.f("ix_job_work_order_lines_item_id"), table_name="job_work_order_lines")
    op.drop_index("ix_job_work_order_lines_item", table_name="job_work_order_lines")
    op.drop_index(
        op.f("ix_job_work_order_lines_expected_output_item_id"), table_name="job_work_order_lines"
    )
    op.drop_index(op.f("ix_job_work_order_lines_created_at"), table_name="job_work_order_lines")
    op.drop_index(op.f("ix_job_work_order_lines_company_id"), table_name="job_work_order_lines")
    op.drop_table("job_work_order_lines")
    op.drop_index(op.f("ix_job_work_challans_vendor_id"), table_name="job_work_challans")
    op.drop_index("ix_job_work_challans_vendor_date", table_name="job_work_challans")
    op.drop_index("ix_job_work_challans_order", table_name="job_work_challans")
    op.drop_index(op.f("ix_job_work_challans_job_work_order_id"), table_name="job_work_challans")
    op.drop_index(op.f("ix_job_work_challans_created_at"), table_name="job_work_challans")
    op.drop_index(op.f("ix_job_work_challans_company_id"), table_name="job_work_challans")
    op.drop_index("ix_job_work_challans_company_date", table_name="job_work_challans")
    op.drop_table("job_work_challans")
    op.drop_index(
        "uq_item_vendor_links_one_preferred",
        table_name="item_vendor_links",
        postgresql_where=sa.text("is_preferred AND deleted_at IS NULL"),
    )
    op.drop_index(op.f("ix_item_vendor_links_vendor_id"), table_name="item_vendor_links")
    op.drop_index("ix_item_vendor_links_vendor", table_name="item_vendor_links")
    op.drop_index(op.f("ix_item_vendor_links_item_id"), table_name="item_vendor_links")
    op.drop_index(op.f("ix_item_vendor_links_deleted_at"), table_name="item_vendor_links")
    op.drop_index(op.f("ix_item_vendor_links_created_at"), table_name="item_vendor_links")
    op.drop_index(op.f("ix_item_vendor_links_company_id"), table_name="item_vendor_links")
    op.drop_table("item_vendor_links")
    op.drop_index(
        "uq_item_variants_attribute_combo",
        table_name="item_variants",
        postgresql_where=sa.text("deleted_at IS NULL"),
    )
    op.drop_index(op.f("ix_item_variants_item_id"), table_name="item_variants")
    op.drop_index(
        "ix_item_variants_item_active",
        table_name="item_variants",
        postgresql_where=sa.text("is_active AND deleted_at IS NULL"),
    )
    op.drop_index(op.f("ix_item_variants_deleted_at"), table_name="item_variants")
    op.drop_index(op.f("ix_item_variants_created_at"), table_name="item_variants")
    op.drop_index(op.f("ix_item_variants_company_id"), table_name="item_variants")
    op.drop_index(
        "ix_item_variants_code_trgm",
        table_name="item_variants",
        postgresql_using="gin",
        postgresql_ops={"code": "gin_trgm_ops"},
    )
    op.drop_table("item_variants")
    op.drop_index("ix_inventory_summary_value", table_name="inventory_summary")
    op.drop_index(op.f("ix_inventory_summary_created_at"), table_name="inventory_summary")
    op.drop_index(op.f("ix_inventory_summary_company_id"), table_name="inventory_summary")
    op.drop_index("ix_inventory_summary_ageing", table_name="inventory_summary")
    op.drop_table("inventory_summary")
    op.drop_index(op.f("ix_goods_receipts_warehouse_id"), table_name="goods_receipts")
    op.drop_index(op.f("ix_goods_receipts_vendor_id"), table_name="goods_receipts")
    op.drop_index("ix_goods_receipts_vendor_date", table_name="goods_receipts")
    op.drop_index("ix_goods_receipts_status", table_name="goods_receipts")
    op.drop_index(op.f("ix_goods_receipts_purchase_order_id"), table_name="goods_receipts")
    op.drop_index("ix_goods_receipts_order", table_name="goods_receipts")
    op.drop_index(op.f("ix_goods_receipts_created_at"), table_name="goods_receipts")
    op.drop_index(op.f("ix_goods_receipts_company_id"), table_name="goods_receipts")
    op.drop_table("goods_receipts")
    op.drop_index(
        "uq_boms_one_default_approved_per_item",
        table_name="boms",
        postgresql_where=sa.text("is_default AND status = 'APPROVED'"),
    )
    op.drop_index(op.f("ix_boms_parent_bom_id"), table_name="boms")
    op.drop_index(op.f("ix_boms_item_id"), table_name="boms")
    op.drop_index(
        "ix_boms_effective_lookup",
        table_name="boms",
        postgresql_where=sa.text("status = 'APPROVED'"),
    )
    op.drop_index(op.f("ix_boms_created_at"), table_name="boms")
    op.drop_index("ix_boms_company_status", table_name="boms")
    op.drop_index(op.f("ix_boms_company_id"), table_name="boms")
    op.drop_table("boms")
    op.drop_index(
        "uq_vendor_categories_one_preferred",
        table_name="vendor_categories",
        postgresql_where=sa.text("is_preferred"),
    )
    op.drop_index(op.f("ix_vendor_categories_vendor_id"), table_name="vendor_categories")
    op.drop_index(op.f("ix_vendor_categories_item_category_id"), table_name="vendor_categories")
    op.drop_index(op.f("ix_vendor_categories_created_at"), table_name="vendor_categories")
    op.drop_index(op.f("ix_vendor_categories_company_id"), table_name="vendor_categories")
    op.drop_index("ix_vendor_categories_category", table_name="vendor_categories")
    op.drop_table("vendor_categories")
    op.drop_index("ix_user_roles_role", table_name="user_roles")
    op.drop_table("user_roles")
    op.drop_index(op.f("ix_uom_conversions_to_uom_id"), table_name="uom_conversions")
    op.drop_index(op.f("ix_uom_conversions_from_uom_id"), table_name="uom_conversions")
    op.drop_index(op.f("ix_uom_conversions_deleted_at"), table_name="uom_conversions")
    op.drop_index(op.f("ix_uom_conversions_created_at"), table_name="uom_conversions")
    op.drop_index(op.f("ix_uom_conversions_company_id"), table_name="uom_conversions")
    op.drop_table("uom_conversions")
    op.drop_index(op.f("ix_storage_locations_warehouse_id"), table_name="storage_locations")
    op.drop_index("ix_storage_locations_warehouse_active", table_name="storage_locations")
    op.drop_index(op.f("ix_storage_locations_parent_location_id"), table_name="storage_locations")
    op.drop_index(op.f("ix_storage_locations_deleted_at"), table_name="storage_locations")
    op.drop_index(op.f("ix_storage_locations_created_at"), table_name="storage_locations")
    op.drop_index(op.f("ix_storage_locations_company_id"), table_name="storage_locations")
    op.drop_index("ix_storage_locations_barcode", table_name="storage_locations")
    op.drop_table("storage_locations")
    op.drop_index("ix_stock_transfers_to", table_name="stock_transfers")
    op.drop_index("ix_stock_transfers_status", table_name="stock_transfers")
    op.drop_index("ix_stock_transfers_from", table_name="stock_transfers")
    op.drop_index(op.f("ix_stock_transfers_created_at"), table_name="stock_transfers")
    op.drop_index(op.f("ix_stock_transfers_company_id"), table_name="stock_transfers")
    op.drop_table("stock_transfers")
    op.drop_index("ix_stock_adjustments_warehouse", table_name="stock_adjustments")
    op.drop_index("ix_stock_adjustments_status", table_name="stock_adjustments")
    op.drop_index(
        "ix_stock_adjustments_pending",
        table_name="stock_adjustments",
        postgresql_where=sa.text("status = 'PENDING_APPROVAL'"),
    )
    op.drop_index(op.f("ix_stock_adjustments_created_at"), table_name="stock_adjustments")
    op.drop_index(op.f("ix_stock_adjustments_company_id"), table_name="stock_adjustments")
    op.drop_table("stock_adjustments")
    op.drop_index(op.f("ix_report_snapshots_status"), table_name="report_snapshots")
    op.drop_index("ix_report_snapshots_lookup", table_name="report_snapshots")
    op.drop_index(op.f("ix_report_snapshots_expires_at"), table_name="report_snapshots")
    op.drop_index(op.f("ix_report_snapshots_created_at"), table_name="report_snapshots")
    op.drop_index(op.f("ix_report_snapshots_company_id"), table_name="report_snapshots")
    op.drop_table("report_snapshots")
    op.drop_index(op.f("ix_purchase_requisitions_warehouse_id"), table_name="purchase_requisitions")
    op.drop_index("ix_purchase_requisitions_warehouse", table_name="purchase_requisitions")
    op.drop_index("ix_purchase_requisitions_status_required", table_name="purchase_requisitions")
    op.drop_index("ix_purchase_requisitions_source", table_name="purchase_requisitions")
    op.drop_index(
        op.f("ix_purchase_requisitions_department_id"), table_name="purchase_requisitions"
    )
    op.drop_index(op.f("ix_purchase_requisitions_created_at"), table_name="purchase_requisitions")
    op.drop_index(op.f("ix_purchase_requisitions_company_id"), table_name="purchase_requisitions")
    op.drop_index(op.f("ix_purchase_requisitions_branch_id"), table_name="purchase_requisitions")
    op.drop_table("purchase_requisitions")
    op.drop_index("ix_purchase_orders_warehouse_status", table_name="purchase_orders")
    op.drop_index(op.f("ix_purchase_orders_warehouse_id"), table_name="purchase_orders")
    op.drop_index(op.f("ix_purchase_orders_vendor_id"), table_name="purchase_orders")
    op.drop_index("ix_purchase_orders_vendor", table_name="purchase_orders")
    op.drop_index("ix_purchase_orders_status_expected", table_name="purchase_orders")
    op.drop_index("ix_purchase_orders_source", table_name="purchase_orders")
    op.drop_index(
        "ix_purchase_orders_open_vendor",
        table_name="purchase_orders",
        postgresql_where=sa.text(
            "status IN ('DRAFT', 'PENDING_APPROVAL', 'APPROVED', 'SENT', 'ACKNOWLEDGED', 'PARTIALLY_RECEIVED')"
        ),
    )
    op.drop_index(op.f("ix_purchase_orders_expected_delivery_date"), table_name="purchase_orders")
    op.drop_index(op.f("ix_purchase_orders_created_at"), table_name="purchase_orders")
    op.drop_index(op.f("ix_purchase_orders_company_id"), table_name="purchase_orders")
    op.drop_index(op.f("ix_purchase_orders_branch_id"), table_name="purchase_orders")
    op.drop_index(op.f("ix_purchase_orders_assigned_vendor_user_id"), table_name="purchase_orders")
    op.drop_index("ix_purchase_orders_assigned_vendor_user", table_name="purchase_orders")
    op.drop_table("purchase_orders")
    op.drop_index(op.f("ix_machines_work_center_id"), table_name="machines")
    op.drop_index(
        "ix_machines_next_maintenance_due",
        table_name="machines",
        postgresql_where=sa.text("next_maintenance_date IS NOT NULL"),
    )
    op.drop_index(op.f("ix_machines_deleted_at"), table_name="machines")
    op.drop_index(op.f("ix_machines_created_at"), table_name="machines")
    op.drop_index("ix_machines_company_work_center_status", table_name="machines")
    op.drop_index(op.f("ix_machines_company_id"), table_name="machines")
    op.drop_table("machines")
    op.drop_index("ix_ledger_entries_reference", table_name="ledger_entries")
    op.drop_index("ix_ledger_entries_party", table_name="ledger_entries")
    op.drop_index("ix_ledger_entries_journal", table_name="ledger_entries")
    op.drop_index(op.f("ix_ledger_entries_financial_year_id"), table_name="ledger_entries")
    op.drop_index(op.f("ix_ledger_entries_company_id"), table_name="ledger_entries")
    op.drop_index("ix_ledger_entries_company_date", table_name="ledger_entries")
    op.drop_index(op.f("ix_ledger_entries_account_id"), table_name="ledger_entries")
    op.drop_index("ix_ledger_entries_account_date", table_name="ledger_entries")
    op.drop_table("ledger_entries")
    op.drop_index(op.f("ix_job_work_orders_warehouse_id"), table_name="job_work_orders")
    op.drop_index("ix_job_work_orders_vendor_status", table_name="job_work_orders")
    op.drop_index(op.f("ix_job_work_orders_vendor_id"), table_name="job_work_orders")
    op.drop_index("ix_job_work_orders_source", table_name="job_work_orders")
    op.drop_index(op.f("ix_job_work_orders_return_warehouse_id"), table_name="job_work_orders")
    op.drop_index(
        "ix_job_work_orders_material_at_vendor",
        table_name="job_work_orders",
        postgresql_where=sa.text("status IN ('ISSUED', 'PARTIALLY_RECEIVED')"),
    )
    op.drop_index(op.f("ix_job_work_orders_created_at"), table_name="job_work_orders")
    op.drop_index("ix_job_work_orders_company_status_return", table_name="job_work_orders")
    op.drop_index(op.f("ix_job_work_orders_company_id"), table_name="job_work_orders")
    op.drop_table("job_work_orders")
    op.drop_index(
        "ix_items_sellable",
        table_name="items",
        postgresql_where=sa.text("can_sell AND is_active AND deleted_at IS NULL"),
    )
    op.drop_index(
        "ix_items_reorder",
        table_name="items",
        postgresql_where=sa.text("reorder_level > 0 AND is_active AND deleted_at IS NULL"),
    )
    op.drop_index(
        "ix_items_purchasable",
        table_name="items",
        postgresql_where=sa.text("can_purchase AND is_active AND deleted_at IS NULL"),
    )
    op.drop_index(
        "ix_items_name_trgm",
        table_name="items",
        postgresql_using="gin",
        postgresql_ops={"name": "gin_trgm_ops"},
    )
    op.drop_index(op.f("ix_items_deleted_at"), table_name="items")
    op.drop_index(op.f("ix_items_created_at"), table_name="items")
    op.drop_index(op.f("ix_items_company_id"), table_name="items")
    op.drop_index(
        "ix_items_company_category_active",
        table_name="items",
        postgresql_where=sa.text("deleted_at IS NULL"),
    )
    op.drop_index(
        "ix_items_code_trgm",
        table_name="items",
        postgresql_using="gin",
        postgresql_ops={"code": "gin_trgm_ops"},
    )
    op.drop_index(op.f("ix_items_category_id"), table_name="items")
    op.drop_index(
        "ix_items_bom_components",
        table_name="items",
        postgresql_where=sa.text("can_be_bom_component AND is_active AND deleted_at IS NULL"),
    )
    op.drop_index(op.f("ix_items_base_uom_id"), table_name="items")
    op.drop_table("items")
    op.drop_index(
        "uq_customer_contacts_one_primary",
        table_name="customer_contacts",
        postgresql_where=sa.text("is_primary AND deleted_at IS NULL"),
    )
    op.drop_index(op.f("ix_customer_contacts_deleted_at"), table_name="customer_contacts")
    op.drop_index(op.f("ix_customer_contacts_customer_id"), table_name="customer_contacts")
    op.drop_index("ix_customer_contacts_customer", table_name="customer_contacts")
    op.drop_index(op.f("ix_customer_contacts_created_at"), table_name="customer_contacts")
    op.drop_index(op.f("ix_customer_contacts_company_id"), table_name="customer_contacts")
    op.drop_table("customer_contacts")
    op.drop_index(
        "uq_customer_addresses_one_default",
        table_name="customer_addresses",
        postgresql_where=sa.text("is_default AND deleted_at IS NULL"),
    )
    op.drop_index(op.f("ix_customer_addresses_deleted_at"), table_name="customer_addresses")
    op.drop_index("ix_customer_addresses_customer_type", table_name="customer_addresses")
    op.drop_index(op.f("ix_customer_addresses_customer_id"), table_name="customer_addresses")
    op.drop_index(op.f("ix_customer_addresses_created_at"), table_name="customer_addresses")
    op.drop_index(op.f("ix_customer_addresses_company_id"), table_name="customer_addresses")
    op.drop_table("customer_addresses")
    op.drop_index(op.f("ix_work_centers_plant_id"), table_name="work_centers")
    op.drop_index(op.f("ix_work_centers_department_id"), table_name="work_centers")
    op.drop_index(op.f("ix_work_centers_deleted_at"), table_name="work_centers")
    op.drop_index(op.f("ix_work_centers_created_at"), table_name="work_centers")
    op.drop_index(op.f("ix_work_centers_company_id"), table_name="work_centers")
    op.drop_index("ix_work_centers_company_active", table_name="work_centers")
    op.drop_table("work_centers")
    op.drop_index(
        "uq_warehouses_company_default",
        table_name="warehouses",
        postgresql_where=sa.text("is_default AND deleted_at IS NULL"),
    )
    op.drop_index(op.f("ix_warehouses_plant_id"), table_name="warehouses")
    op.drop_index(op.f("ix_warehouses_parent_warehouse_id"), table_name="warehouses")
    op.drop_index(op.f("ix_warehouses_deleted_at"), table_name="warehouses")
    op.drop_index(op.f("ix_warehouses_created_at"), table_name="warehouses")
    op.drop_index("ix_warehouses_company_type", table_name="warehouses")
    op.drop_index(op.f("ix_warehouses_company_id"), table_name="warehouses")
    op.drop_index("ix_warehouses_company_active", table_name="warehouses")
    op.drop_index(op.f("ix_warehouses_branch_id"), table_name="warehouses")
    op.drop_table("warehouses")
    op.drop_index(
        "uq_vendor_contacts_one_primary",
        table_name="vendor_contacts",
        postgresql_where=sa.text("is_primary AND deleted_at IS NULL"),
    )
    op.drop_index(op.f("ix_vendor_contacts_vendor_id"), table_name="vendor_contacts")
    op.drop_index("ix_vendor_contacts_vendor_active", table_name="vendor_contacts")
    op.drop_index("ix_vendor_contacts_role", table_name="vendor_contacts")
    op.drop_index(op.f("ix_vendor_contacts_deleted_at"), table_name="vendor_contacts")
    op.drop_index(op.f("ix_vendor_contacts_created_at"), table_name="vendor_contacts")
    op.drop_index(op.f("ix_vendor_contacts_company_id"), table_name="vendor_contacts")
    op.drop_table("vendor_contacts")
    op.drop_index(op.f("ix_user_sessions_user_id"), table_name="user_sessions")
    op.drop_index(op.f("ix_user_sessions_refresh_jti"), table_name="user_sessions")
    op.drop_index(op.f("ix_user_sessions_created_at"), table_name="user_sessions")
    op.drop_index(op.f("ix_user_sessions_company_id"), table_name="user_sessions")
    op.drop_index("ix_user_sessions_active", table_name="user_sessions")
    op.drop_table("user_sessions")
    op.drop_index(
        "uq_units_of_measure_one_base_per_category",
        table_name="units_of_measure",
        postgresql_where=sa.text("is_base AND deleted_at IS NULL"),
    )
    op.drop_index(op.f("ix_units_of_measure_deleted_at"), table_name="units_of_measure")
    op.drop_index(op.f("ix_units_of_measure_created_at"), table_name="units_of_measure")
    op.drop_index(op.f("ix_units_of_measure_company_id"), table_name="units_of_measure")
    op.drop_table("units_of_measure")
    op.drop_index(op.f("ix_transporters_deleted_at"), table_name="transporters")
    op.drop_index(op.f("ix_transporters_created_at"), table_name="transporters")
    op.drop_index("ix_transporters_company_type", table_name="transporters")
    op.drop_index(op.f("ix_transporters_company_id"), table_name="transporters")
    op.drop_index("ix_transporters_company_active", table_name="transporters")
    op.drop_table("transporters")
    op.drop_index(op.f("ix_roles_deleted_at"), table_name="roles")
    op.drop_index(op.f("ix_roles_created_at"), table_name="roles")
    op.drop_index(op.f("ix_roles_company_id"), table_name="roles")
    op.drop_index("ix_roles_company_active", table_name="roles")
    op.drop_table("roles")
    op.drop_index("ix_payments_status_date", table_name="payments")
    op.drop_index("ix_payments_reference_number", table_name="payments")
    op.drop_index("ix_payments_party", table_name="payments")
    op.drop_index(op.f("ix_payments_created_at"), table_name="payments")
    op.drop_index(op.f("ix_payments_company_id"), table_name="payments")
    op.drop_table("payments")
    op.drop_index(op.f("ix_password_reset_tokens_user_id"), table_name="password_reset_tokens")
    op.drop_index(op.f("ix_password_reset_tokens_created_at"), table_name="password_reset_tokens")
    op.drop_index(op.f("ix_password_reset_tokens_company_id"), table_name="password_reset_tokens")
    op.drop_index("ix_password_reset_active", table_name="password_reset_tokens")
    op.drop_table("password_reset_tokens")
    op.drop_index(
        "uq_notifications_open_group",
        table_name="notifications",
        postgresql_where=sa.text("group_key IS NOT NULL AND is_read = false"),
    )
    op.drop_index(
        "ix_notifications_unread",
        table_name="notifications",
        postgresql_where=sa.text("is_read = false"),
    )
    op.drop_index("ix_notifications_feed", table_name="notifications")
    op.drop_index(
        "ix_notifications_entity",
        table_name="notifications",
        postgresql_where=sa.text("entity_id IS NOT NULL"),
    )
    op.drop_index(op.f("ix_notifications_created_at"), table_name="notifications")
    op.drop_index(op.f("ix_notifications_company_id"), table_name="notifications")
    op.drop_table("notifications")
    op.drop_index(
        op.f("ix_notification_preferences_created_at"), table_name="notification_preferences"
    )
    op.drop_index(
        op.f("ix_notification_preferences_company_id"), table_name="notification_preferences"
    )
    op.drop_table("notification_preferences")
    op.drop_index(op.f("ix_mrp_runs_created_at"), table_name="mrp_runs")
    op.drop_index(op.f("ix_mrp_runs_company_id"), table_name="mrp_runs")
    op.drop_index("ix_mrp_runs_company_created", table_name="mrp_runs")
    op.drop_index(
        "ix_mrp_runs_active",
        table_name="mrp_runs",
        postgresql_where=sa.text("status IN ('QUEUED', 'RUNNING')"),
    )
    op.drop_table("mrp_runs")
    op.drop_index("ix_login_attempts_ip_time", table_name="login_attempts")
    op.drop_index("ix_login_attempts_email_time", table_name="login_attempts")
    op.drop_index(op.f("ix_login_attempts_company_id"), table_name="login_attempts")
    op.drop_table("login_attempts")
    op.drop_index(
        "uq_ledger_accounts_system_code",
        table_name="ledger_accounts",
        postgresql_where=sa.text("system_code IS NOT NULL AND deleted_at IS NULL"),
    )
    op.drop_index("ix_ledger_accounts_type", table_name="ledger_accounts")
    op.drop_index(op.f("ix_ledger_accounts_parent_id"), table_name="ledger_accounts")
    op.drop_index(op.f("ix_ledger_accounts_deleted_at"), table_name="ledger_accounts")
    op.drop_index(op.f("ix_ledger_accounts_created_at"), table_name="ledger_accounts")
    op.drop_index(op.f("ix_ledger_accounts_company_id"), table_name="ledger_accounts")
    op.drop_table("ledger_accounts")
    op.drop_index(
        "ix_item_categories_path",
        table_name="item_categories",
        postgresql_ops={"path": "varchar_pattern_ops"},
    )
    op.drop_index(op.f("ix_item_categories_parent_id"), table_name="item_categories")
    op.drop_index("ix_item_categories_parent", table_name="item_categories")
    op.drop_index(op.f("ix_item_categories_deleted_at"), table_name="item_categories")
    op.drop_index(op.f("ix_item_categories_created_at"), table_name="item_categories")
    op.drop_index(op.f("ix_item_categories_company_id"), table_name="item_categories")
    op.drop_table("item_categories")
    op.drop_index("ix_financial_years_range", table_name="financial_years")
    op.drop_index(op.f("ix_financial_years_deleted_at"), table_name="financial_years")
    op.drop_index(op.f("ix_financial_years_created_at"), table_name="financial_years")
    op.drop_index(op.f("ix_financial_years_company_id"), table_name="financial_years")
    op.drop_table("financial_years")
    op.drop_index(
        "ix_documents_expiring",
        table_name="documents",
        postgresql_where=sa.text("expires_at IS NOT NULL"),
    )
    op.drop_index(op.f("ix_documents_created_at"), table_name="documents")
    op.drop_index("ix_documents_company_uploaded_by", table_name="documents")
    op.drop_index(op.f("ix_documents_company_id"), table_name="documents")
    op.drop_index("ix_documents_company_entity", table_name="documents")
    op.drop_index("ix_documents_company_category", table_name="documents")
    op.drop_table("documents")
    op.drop_index(op.f("ix_document_sequences_created_at"), table_name="document_sequences")
    op.drop_index(op.f("ix_document_sequences_company_id"), table_name="document_sequences")
    op.drop_table("document_sequences")
    op.drop_index(
        "uq_customers_company_gstin",
        table_name="customers",
        postgresql_where=sa.text("gstin IS NOT NULL AND deleted_at IS NULL"),
    )
    op.drop_index(
        "ix_customers_sales_user",
        table_name="customers",
        postgresql_where=sa.text("assigned_sales_user_id IS NOT NULL AND deleted_at IS NULL"),
    )
    op.drop_index(op.f("ix_customers_deleted_at"), table_name="customers")
    op.drop_index(op.f("ix_customers_created_at"), table_name="customers")
    op.drop_index("ix_customers_company_type", table_name="customers")
    op.drop_index(op.f("ix_customers_company_id"), table_name="customers")
    op.drop_index("ix_customers_company_active", table_name="customers")
    op.drop_index(
        "ix_customers_blocked",
        table_name="customers",
        postgresql_where=sa.text("is_blocked AND deleted_at IS NULL"),
    )
    op.drop_table("customers")
    op.drop_index("ix_audit_logs_request", table_name="audit_logs")
    op.drop_index("ix_audit_logs_entity", table_name="audit_logs")
    op.drop_index(op.f("ix_audit_logs_company_id"), table_name="audit_logs")
    op.drop_index("ix_audit_logs_company_created", table_name="audit_logs")
    op.drop_index("ix_audit_logs_actor", table_name="audit_logs")
    op.drop_index("ix_audit_logs_action", table_name="audit_logs")
    op.drop_table("audit_logs")
    op.drop_index("ix_approval_requests_pending", table_name="approval_requests")
    op.drop_index("ix_approval_requests_entity", table_name="approval_requests")
    op.drop_index(op.f("ix_approval_requests_company_id"), table_name="approval_requests")
    op.drop_table("approval_requests")
    op.drop_index(op.f("ix_users_vendor_id"), table_name="users")
    op.drop_index("ix_users_vendor", table_name="users")
    op.drop_index(op.f("ix_users_plant_id"), table_name="users")
    op.drop_index("ix_users_email_lookup", table_name="users")
    op.drop_index(op.f("ix_users_department_id"), table_name="users")
    op.drop_index(op.f("ix_users_deleted_at"), table_name="users")
    op.drop_index(op.f("ix_users_created_at"), table_name="users")
    op.drop_index(op.f("ix_users_company_id"), table_name="users")
    op.drop_index("ix_users_company_active", table_name="users")
    op.drop_index(op.f("ix_users_branch_id"), table_name="users")
    op.drop_table("users")
    op.drop_index(
        "uq_vendors_company_gstin",
        table_name="vendors",
        postgresql_where=sa.text("gstin IS NOT NULL AND deleted_at IS NULL"),
    )
    op.drop_index("ix_vendors_types", table_name="vendors", postgresql_using="gin")
    op.drop_index(op.f("ix_vendors_deleted_at"), table_name="vendors")
    op.drop_index(op.f("ix_vendors_created_at"), table_name="vendors")
    op.drop_index(op.f("ix_vendors_company_id"), table_name="vendors")
    op.drop_index("ix_vendors_company_approved", table_name="vendors")
    op.drop_index("ix_vendors_company_active", table_name="vendors")
    op.drop_table("vendors")
    op.drop_index(op.f("ix_departments_plant_id"), table_name="departments")
    op.drop_index(op.f("ix_departments_parent_id"), table_name="departments")
    op.drop_index(op.f("ix_departments_deleted_at"), table_name="departments")
    op.drop_index(op.f("ix_departments_created_at"), table_name="departments")
    op.drop_index(op.f("ix_departments_company_id"), table_name="departments")
    op.drop_table("departments")
    op.drop_index(op.f("ix_plants_deleted_at"), table_name="plants")
    op.drop_index(op.f("ix_plants_created_at"), table_name="plants")
    op.drop_index(op.f("ix_plants_company_id"), table_name="plants")
    op.drop_index("ix_plants_company_active", table_name="plants")
    op.drop_index(op.f("ix_plants_branch_id"), table_name="plants")
    op.drop_table("plants")
    op.drop_index(op.f("ix_branches_deleted_at"), table_name="branches")
    op.drop_index(op.f("ix_branches_created_at"), table_name="branches")
    op.drop_index(op.f("ix_branches_company_id"), table_name="branches")
    op.drop_index("ix_branches_company_active", table_name="branches")
    op.drop_table("branches")
    op.drop_index(op.f("ix_companies_deleted_at"), table_name="companies")
    op.drop_index(op.f("ix_companies_created_at"), table_name="companies")
    op.drop_index("ix_companies_active", table_name="companies")
    op.drop_table("companies")

    # The three extensions are intentionally left installed - see the module
    # docstring. Nothing else this revision created survives.
