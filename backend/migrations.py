"""Idempotent schema migrations for SQLite and PostgreSQL.

Kasflow started as a single-file SQLite app, so existing installations must keep
working. ``run_migrations`` adds any column that exists in the ORM metadata but
not yet in the database, and rebuilds the invoices table of very old SQLite
databases where the invoice number was globally unique.

This is deliberately small and runs at start-up. Every step is additive and
idempotent, so it is safe to run on each boot and on multiple workers. For a
team-sized deployment this can be replaced by Alembic, but the SQLAlchemy
metadata stays the single source of truth here.
"""

from __future__ import annotations

import logging

from sqlalchemy import MetaData, inspect, text
from sqlalchemy.engine import Engine

logger = logging.getLogger("kasflow.migrations")

# Columns added later that are NOT NULL and need a value for existing rows.
# column -> (SQL default literal, Python value used for the backfill UPDATE)
BACKFILL_DEFAULTS: dict[str, tuple[str, object]] = {
    "payment_term_days": ("30", 30),
    "session_version": ("0", 0),
    "notify_email": ("1", 1),
    "notify_sms": ("0", 0),
    "notify_backup": ("1", 1),
    "country": ("'Nederland'", "Nederland"),
    "vat_scheme": ("'standard'", "standard"),
    "kind": ("'invoice'", "invoice"),
    "vat_treatment": ("'standard'", "standard"),
    "vat_cents": ("0", 0),
    "input_vat_cents": ("0", 0),
}

INVOICE_COLUMNS = [
    "id",
    "owner_id",
    "customer_id",
    "number",
    "invoice_date",
    "due_date",
    "delivery_date",
    "note",
    "status",
    "subtotal_cents",
    "vat_cents",
    "total_cents",
    "paid_at",
    "sent_at",
    "created_at",
]


def add_missing_columns(engine: Engine, metadata: MetaData) -> list[tuple[str, str]]:
    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())
    added: list[tuple[str, str]] = []

    with engine.begin() as connection:
        for table in metadata.sorted_tables:
            if table.name not in existing_tables:
                continue
            present = {column["name"] for column in inspector.get_columns(table.name)}
            for column in table.columns:
                if column.name in present:
                    continue
                column_type = column.type.compile(dialect=engine.dialect)
                default = BACKFILL_DEFAULTS.get(column.name)
                ddl = f'ALTER TABLE {table.name} ADD COLUMN "{column.name}" {column_type}'
                if default is not None and not column.nullable:
                    ddl += f" NOT NULL DEFAULT {default[0]}"
                connection.execute(text(ddl))
                if default is not None:
                    connection.execute(
                        text(
                            f'UPDATE {table.name} SET "{column.name}" = :value '
                            f'WHERE "{column.name}" IS NULL'
                        ),
                        {"value": default[1]},
                    )
                added.append((table.name, column.name))

        # Existing accounts predate e-mail verification; do not lock them out.
        if ("users", "email_verified_at") in added:
            connection.execute(
                text("UPDATE users SET email_verified_at = created_at")
            )
            logger.info("Bestaande accounts gemarkeerd als geverifieerd")

    return added


def rebuild_legacy_invoices(engine: Engine) -> bool:
    """Old SQLite databases had a globally unique invoice number.

    Invoice numbers only need to be unique per administration, so the table is
    rebuilt with a composite unique constraint (owner_id, number).
    """
    if engine.dialect.name != "sqlite":
        return False

    with engine.connect() as connection:
        indexes = connection.exec_driver_sql("PRAGMA index_list('invoices')").fetchall()
        legacy = False
        for index in indexes:
            if not index[2]:
                continue
            columns = [
                row[2]
                for row in connection.exec_driver_sql(f"PRAGMA index_info('{index[1]}')").fetchall()
            ]
            if columns == ["number"]:
                legacy = True
                break

        if not legacy:
            return False

        connection.commit()
        connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
        connection.rollback()
        transaction = connection.begin()
        try:
            connection.exec_driver_sql("DROP TABLE IF EXISTS invoices_migrated")
            connection.exec_driver_sql(
                """
                CREATE TABLE invoices_migrated (
                    id INTEGER NOT NULL PRIMARY KEY,
                    owner_id INTEGER NOT NULL REFERENCES users(id),
                    customer_id INTEGER NOT NULL REFERENCES customers(id),
                    number VARCHAR(50) NOT NULL,
                    invoice_date DATE NOT NULL,
                    due_date DATE NOT NULL,
                    delivery_date DATE,
                    note TEXT,
                    status VARCHAR(20) NOT NULL,
                    subtotal_cents INTEGER NOT NULL,
                    vat_cents INTEGER NOT NULL,
                    total_cents INTEGER NOT NULL,
                    paid_at DATETIME,
                    sent_at DATETIME,
                    created_at DATETIME NOT NULL,
                    CONSTRAINT uq_invoices_owner_number UNIQUE (owner_id, number)
                )
                """
            )
            present = {
                row[1]
                for row in connection.exec_driver_sql("PRAGMA table_info('invoices')").fetchall()
            }
            columns = [column for column in INVOICE_COLUMNS if column in present]
            column_list = ", ".join(f'"{column}"' for column in columns)
            connection.exec_driver_sql(
                f"INSERT INTO invoices_migrated ({column_list}) "
                f"SELECT {column_list} FROM invoices"
            )
            connection.exec_driver_sql("DROP TABLE invoices")
            connection.exec_driver_sql("ALTER TABLE invoices_migrated RENAME TO invoices")
            transaction.commit()
        except Exception:
            transaction.rollback()
            raise
        finally:
            connection.exec_driver_sql("PRAGMA foreign_keys=ON")
        connection.commit()
        logger.info("Factuurnummers zijn nu uniek per account")
        return True


def run_migrations(engine: Engine, metadata: MetaData) -> None:
    added = add_missing_columns(engine, metadata)
    # The rebuild only knows the legacy invoice columns, so add the newer ones again.
    if rebuild_legacy_invoices(engine):
        added += add_missing_columns(engine, metadata)
    if added:
        logger.info("Kolommen toegevoegd: %s", ", ".join(f"{t}.{c}" for t, c in added))
