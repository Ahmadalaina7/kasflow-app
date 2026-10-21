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

from sqlalchemy import Boolean, MetaData, inspect, text
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
    "ib_starter": ("0", 0),
    "ib_hours_criterion": ("0", 0),
    "reminder_count": ("0", 0),
    "vat_origin": ("'nl'", "nl"),
    "company_id": ("0", 0),
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
                if default is not None and isinstance(column.type, Boolean):
                    # PostgreSQL rejects 0/1 as a boolean default.
                    flag = bool(default[1])
                    default = ("TRUE" if flag else "FALSE", flag)
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


TENANT_TABLES = (
    "customers",
    "invoices",
    "receipts",
    "bank_transactions",
    "time_entries",
    "trips",
    "vat_declarations",
)


def ensure_tenancy(engine: Engine) -> None:
    """Give every existing account a company and stamp bookkeeping rows with it.

    Older databases stored the administration on the user. New features (team
    roles, extra companies) need a company row, so this copies the profile into
    ``companies`` with the same id as the user and fills ``company_id``.
    """
    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    if "users" not in tables or "companies" not in tables:
        return

    dialect = engine.dialect.name
    with engine.begin() as connection:
        for table in TENANT_TABLES:
            if table not in tables:
                continue
            columns = {column["name"] for column in inspector.get_columns(table)}
            if "company_id" not in columns:
                connection.execute(text(f'ALTER TABLE {table} ADD COLUMN "company_id" INTEGER'))
                logger.info("Kolom %s.company_id toegevoegd", table)
                inspector = inspect(engine)

        connection.execute(
            text(
                """
                INSERT INTO companies (
                    id, name, kvk_number, vat_number, phone, street, postal_code,
                    city, country, iban, payment_term_days, invoice_prefix, vat_scheme,
                    ib_starter, ib_hours_criterion, created_by_id, created_at
                )
                SELECT
                    u.id, u.company_name, u.kvk_number, u.vat_number, u.phone, u.street,
                    u.postal_code, u.city, COALESCE(u.country, 'Nederland'), u.iban,
                    COALESCE(u.payment_term_days, 30), u.invoice_prefix,
                    COALESCE(u.vat_scheme, 'standard'), COALESCE(u.ib_starter, 0),
                    COALESCE(u.ib_hours_criterion, 0), u.id, u.created_at
                FROM users u
                WHERE NOT EXISTS (SELECT 1 FROM companies c WHERE c.id = u.id)
                """
            )
        )
        if "memberships" in tables:
            connection.execute(
                text(
                    """
                    INSERT INTO memberships (user_id, company_id, role, created_at)
                    SELECT u.id, u.id, 'owner', u.created_at
                    FROM users u
                    WHERE NOT EXISTS (
                        SELECT 1 FROM memberships m WHERE m.user_id = u.id AND m.company_id = u.id
                    )
                    """
                )
            )

        for table in TENANT_TABLES:
            if table not in tables:
                continue
            connection.execute(
                text(
                    f'UPDATE {table} SET company_id = owner_id '
                    f'WHERE company_id IS NULL OR company_id = 0'
                )
            )

        if dialect == "postgresql":
            connection.execute(
                text(
                    """
                    SELECT setval(
                        pg_get_serial_sequence('companies', 'id'),
                        GREATEST((SELECT COALESCE(MAX(id), 1) FROM companies), 1)
                    )
                    """
                )
            )
            constraint_drops = (
                'ALTER TABLE invoices DROP CONSTRAINT IF EXISTS uq_invoices_owner_number',
                'ALTER TABLE bank_transactions DROP CONSTRAINT IF EXISTS uq_bank_owner_fingerprint',
                'ALTER TABLE vat_declarations DROP CONSTRAINT IF EXISTS uq_vat_owner_period',
            )
            for statement in constraint_drops:
                connection.execute(text(statement))
        else:
            connection.execute(text("DROP INDEX IF EXISTS uq_invoices_owner_number"))
            connection.execute(text("DROP INDEX IF EXISTS uq_bank_owner_fingerprint"))
            connection.execute(text("DROP INDEX IF EXISTS uq_vat_owner_period"))

        connection.execute(
            text(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_invoices_company_number "
                "ON invoices (company_id, number)"
            )
        )
        if "bank_transactions" in tables:
            connection.execute(
                text(
                    "CREATE UNIQUE INDEX IF NOT EXISTS uq_bank_company_fingerprint "
                    "ON bank_transactions (company_id, fingerprint)"
                )
            )
        if "vat_declarations" in tables:
            connection.execute(
                text(
                    "CREATE UNIQUE INDEX IF NOT EXISTS uq_vat_company_period "
                    "ON vat_declarations (company_id, year, quarter)"
                )
            )


def ensure_trials(engine: Engine) -> None:
    """Give every existing company a proefperiode that starts at its creation.

    Accounts that predate the subscription layer should not be locked out on
    the first deploy; their trial starts when the company was created, which is
    the same rule new accounts get.
    """
    from datetime import datetime, timedelta, timezone

    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    if "subscriptions" not in tables or "companies" not in tables:
        return
    now = datetime.now(timezone.utc)
    with engine.begin() as connection:
        rows = connection.execute(
            text(
                """
                SELECT c.id, c.created_at FROM companies c
                WHERE NOT EXISTS (SELECT 1 FROM subscriptions s WHERE s.company_id = c.id)
                """
            )
        ).fetchall()
        def as_datetime(value) -> datetime:
            if isinstance(value, datetime):
                parsed = value
            elif isinstance(value, str):
                try:
                    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
                except ValueError:
                    return now
            else:
                return now
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)

        for company_id, created_at in rows:
            start = as_datetime(created_at)
            # Bestaande bedrijven krijgen een volle proefperiode vanaf nu; ze
            # mogen niet op de dag van de upgrade op slot gaan.
            trial_end = max(start, now) + timedelta(days=14)
            connection.execute(
                text(
                    "INSERT INTO subscriptions "
                    "(company_id, plan, status, trial_ends_at, created_at, updated_at) "
                    "VALUES (:company_id, 'starter', 'trialing', :trial_end, :now, :now)"
                ),
                {"company_id": company_id, "trial_end": trial_end, "now": now},
            )
        if rows:
            logger.info("Proefperiode gestart voor %s bestaande bedrijven", len(rows))


def run_migrations(engine: Engine, metadata: MetaData) -> None:
    added = add_missing_columns(engine, metadata)
    # The rebuild only knows the legacy invoice columns, so add the newer ones again.
    if rebuild_legacy_invoices(engine):
        added += add_missing_columns(engine, metadata)
    ensure_tenancy(engine)
    ensure_trials(engine)
    if added:
        logger.info("Kolommen toegevoegd: %s", ", ".join(f"{t}.{c}" for t, c in added))
