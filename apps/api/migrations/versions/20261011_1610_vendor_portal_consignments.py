"""vendor_portal_consignments

Revision ID: 4480835a545e
Revises: c1a1100scope
Created: 2026-10-11 16:10:42.327930+00:00

Review: nullable PO columns require brief ACCESS EXCLUSIVE metadata locks, no
rewrite or backfill. New tables are empty; their FKs/indexes are built at creation.
Status CHECKs use NOT VALID followed by VALIDATE. Rollback removes new attachments
and consignments and refuses while declined orders or PO report snapshots exist.
Expected size proportional to PO and snapshot validation scans; schedule at low load.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "4480835a545e"
down_revision: str | None = "c1a1100scope"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    _swap(
        "purchase_orders",
        "ck_purchase_orders_status_known",
        "status IN ('DRAFT','PENDING_APPROVAL','APPROVED','REJECTED','SENT','VENDOR_DECLINED','ACKNOWLEDGED','PARTIALLY_RECEIVED','RECEIVED','QC_PENDING','QC_COMPLETED','GRN_COMPLETED','INVOICED','PAID','CLOSED','CANCELLED')",
    )
    _swap(
        "report_snapshots",
        "ck_report_snapshots_report_key_is_known",
        "report_key IN ('SALES_ORDER_STATUS','STOCK_AVAILABILITY','MRP_SHORTAGE','PURCHASE_PENDING','PRODUCTION_PENDING','JOB_WORK_PENDING','QC_REJECTION','VENDOR_PERFORMANCE','PRODUCT_PROFITABILITY','FAST_MOVING','SLOW_MOVING','DEAD_STOCK','ORDER_DELAY','PRODUCTION_EFFICIENCY','MACHINE_UTILISATION','INVENTORY_VALUATION','DISPATCH','COURIER_TRACKING','VENDOR_DISPATCH_HISTORY','COST_CENTRE_EXPENSE','PURCHASE_ORDER','PACKING_LIST')",
    )
    op.create_table(
        "purchase_order_shipments",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("company_id", sa.Uuid(), nullable=False),
        sa.Column("purchase_order_id", sa.Uuid(), nullable=False),
        sa.Column("shipment_number", sa.Integer(), nullable=False),
        sa.Column("transporter_id", sa.Uuid(), nullable=True),
        sa.Column("courier_name", sa.String(length=120), nullable=True),
        sa.Column("tracking_reference", sa.String(length=120), nullable=True),
        sa.Column("lr_number", sa.String(length=64), nullable=True),
        sa.Column("vehicle_number", sa.String(length=32), nullable=True),
        sa.Column("driver_name", sa.String(length=120), nullable=True),
        sa.Column("driver_mobile", sa.String(length=32), nullable=True),
        sa.Column("shipped_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expected_delivery_date", sa.Date(), nullable=True),
        sa.Column("packages", sa.Integer(), nullable=True),
        sa.Column("weight_kg", sa.Numeric(precision=18, scale=4), nullable=True),
        sa.Column(
            "freight_amount", sa.Numeric(precision=18, scale=4), server_default="0", nullable=False
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
        sa.Column("created_by_id", sa.Uuid(), nullable=True),
        sa.Column("updated_by_id", sa.Uuid(), nullable=True),
        sa.CheckConstraint(
            "delivered_at IS NULL OR delivered_at >= shipped_at",
            name=op.f("ck_purchase_order_shipments_delivery_after_dispatch"),
        ),
        sa.CheckConstraint(
            "freight_amount >= 0", name=op.f("ck_purchase_order_shipments_freight_nonnegative")
        ),
        sa.CheckConstraint(
            "packages IS NULL OR packages >= 1",
            name=op.f("ck_purchase_order_shipments_packages_positive"),
        ),
        sa.CheckConstraint(
            "shipment_number >= 1", name=op.f("ck_purchase_order_shipments_number_positive")
        ),
        sa.CheckConstraint(
            "transporter_id IS NOT NULL OR courier_name IS NOT NULL",
            name=op.f("ck_purchase_order_shipments_carrier_required"),
        ),
        sa.CheckConstraint(
            "weight_kg IS NULL OR weight_kg >= 0",
            name=op.f("ck_purchase_order_shipments_weight_nonnegative"),
        ),
        sa.ForeignKeyConstraint(
            ["company_id"],
            ["companies.id"],
            name=op.f("fk_purchase_order_shipments_company_id_companies"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_id"],
            ["users.id"],
            name=op.f("fk_purchase_order_shipments_created_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["purchase_order_id"],
            ["purchase_orders.id"],
            name=op.f("fk_purchase_order_shipments_purchase_order_id_purchase_orders"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["transporter_id"],
            ["transporters.id"],
            name=op.f("fk_purchase_order_shipments_transporter_id_transporters"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["updated_by_id"],
            ["users.id"],
            name=op.f("fk_purchase_order_shipments_updated_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_purchase_order_shipments")),
        sa.UniqueConstraint(
            "company_id", "purchase_order_id", "shipment_number", name="uq_po_shipments_number"
        ),
    )
    op.create_index(
        "ix_po_shipments_order",
        "purchase_order_shipments",
        ["company_id", "purchase_order_id", "shipment_number"],
        unique=False,
    )
    op.create_index(
        op.f("ix_purchase_order_shipments_company_id"),
        "purchase_order_shipments",
        ["company_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_purchase_order_shipments_created_at"),
        "purchase_order_shipments",
        ["created_at"],
        unique=False,
    )
    op.create_table(
        "purchase_order_documents",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("company_id", sa.Uuid(), nullable=False),
        sa.Column("purchase_order_id", sa.Uuid(), nullable=False),
        sa.Column("document_id", sa.Uuid(), nullable=False),
        sa.Column("shipment_id", sa.Uuid(), nullable=True),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column(
            "vendor_invoice_id", sa.Uuid(), sa.ForeignKey("vendor_invoices.id", ondelete="RESTRICT")
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
        sa.Column("created_by_id", sa.Uuid(), nullable=True),
        sa.Column("updated_by_id", sa.Uuid(), nullable=True),
        sa.CheckConstraint(
            "kind IN ('INVOICE','DISPATCH_PHOTO','LR_COPY','DELIVERY_CHALLAN','OTHER')",
            name=op.f("ck_purchase_order_documents_kind_known"),
        ),
        sa.ForeignKeyConstraint(
            ["company_id"],
            ["companies.id"],
            name=op.f("fk_purchase_order_documents_company_id_companies"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_id"],
            ["users.id"],
            name=op.f("fk_purchase_order_documents_created_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["document_id"],
            ["documents.id"],
            name=op.f("fk_purchase_order_documents_document_id_documents"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["purchase_order_id"],
            ["purchase_orders.id"],
            name=op.f("fk_purchase_order_documents_purchase_order_id_purchase_orders"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["shipment_id"],
            ["purchase_order_shipments.id"],
            name=op.f("fk_po_documents_shipment"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["updated_by_id"],
            ["users.id"],
            name=op.f("fk_purchase_order_documents_updated_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_purchase_order_documents")),
        sa.UniqueConstraint("document_id", name=op.f("uq_purchase_order_documents_document_id")),
    )
    op.create_index(
        "ix_po_documents_order",
        "purchase_order_documents",
        ["company_id", "purchase_order_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_purchase_order_documents_company_id"),
        "purchase_order_documents",
        ["company_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_purchase_order_documents_created_at"),
        "purchase_order_documents",
        ["created_at"],
        unique=False,
    )
    op.add_column("purchase_orders", sa.Column("vendor_committed_date", sa.Date(), nullable=True))
    op.add_column(
        "purchase_orders", sa.Column("vendor_decline_reason", sa.String(length=255), nullable=True)
    )
    op.add_column(
        "purchase_orders",
        sa.Column("ready_for_dispatch_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column("purchase_orders", sa.Column("ready_packages", sa.Integer(), nullable=True))
    op.add_column("purchase_orders", sa.Column("ready_note", sa.Text(), nullable=True))


def downgrade() -> None:
    _swap(
        "purchase_orders",
        "ck_purchase_orders_status_known",
        "status IN ('DRAFT','PENDING_APPROVAL','APPROVED','REJECTED','SENT','ACKNOWLEDGED','PARTIALLY_RECEIVED','RECEIVED','QC_PENDING','QC_COMPLETED','GRN_COMPLETED','INVOICED','PAID','CLOSED','CANCELLED')",
    )
    _swap(
        "report_snapshots",
        "ck_report_snapshots_report_key_is_known",
        "report_key IN ('SALES_ORDER_STATUS','STOCK_AVAILABILITY','MRP_SHORTAGE','PURCHASE_PENDING','PRODUCTION_PENDING','JOB_WORK_PENDING','QC_REJECTION','VENDOR_PERFORMANCE','PRODUCT_PROFITABILITY','FAST_MOVING','SLOW_MOVING','DEAD_STOCK','ORDER_DELAY','PRODUCTION_EFFICIENCY','MACHINE_UTILISATION','INVENTORY_VALUATION','DISPATCH','COURIER_TRACKING','VENDOR_DISPATCH_HISTORY','COST_CENTRE_EXPENSE','PACKING_LIST')",
    )
    op.drop_column("purchase_orders", "ready_note")
    op.drop_column("purchase_orders", "ready_packages")
    op.drop_column("purchase_orders", "ready_for_dispatch_at")
    op.drop_column("purchase_orders", "vendor_decline_reason")
    op.drop_column("purchase_orders", "vendor_committed_date")
    op.drop_index(
        op.f("ix_purchase_order_documents_created_at"), table_name="purchase_order_documents"
    )
    op.drop_index(
        op.f("ix_purchase_order_documents_company_id"), table_name="purchase_order_documents"
    )
    op.drop_index("ix_po_documents_order", table_name="purchase_order_documents")
    op.drop_table("purchase_order_documents")
    op.drop_index(
        op.f("ix_purchase_order_shipments_created_at"), table_name="purchase_order_shipments"
    )
    op.drop_index(
        op.f("ix_purchase_order_shipments_company_id"), table_name="purchase_order_shipments"
    )
    op.drop_index("ix_po_shipments_order", table_name="purchase_order_shipments")
    op.drop_table("purchase_order_shipments")


def _swap(table: str, name: str, check: str) -> None:
    op.drop_constraint(op.f(name), table, type_="check")
    op.create_check_constraint(op.f(name), table, check, postgresql_not_valid=True)
    op.execute(sa.text(f"ALTER TABLE {table} VALIDATE CONSTRAINT {name}"))
