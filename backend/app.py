from __future__ import annotations

import hashlib
import hmac
import io
import json
import logging
import os
import secrets
import threading
import time
import uuid
import zipfile
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import urlparse

from fastapi import (
    Depends,
    FastAPI,
    File,
    Form,
    HTTPException,
    Request,
    UploadFile,
    status,
)
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    create_engine,
    delete,
    event,
    select,
    text,
)
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    Session,
    mapped_column,
    relationship,
    selectinload,
    sessionmaker,
)
from starlette.middleware.sessions import SessionMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware

from config import Settings
from invoice_pdf import InvoiceLineData, InvoicePdfData, Party, build_invoice_pdf
from mailer import Mailer
from migrations import run_migrations

logger = logging.getLogger("kasflow.app")

ALLOWED_RECEIPT_TYPES = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "application/pdf": ".pdf",
}

VAT_RATES = {Decimal("0.21"), Decimal("0.09"), Decimal("0")}

PROTECTED_PAGES = {
    "dashboard.html",
    "facturen.html",
    "factuur.html",
    "bonnetjes.html",
    "btw.html",
    "bank.html",
    "klanten.html",
    "instellingen.html",
}

STATIC_SUFFIXES = {
    ".html",
    ".css",
    ".js",
    ".svg",
    ".webmanifest",
    ".png",
    ".ico",
    ".txt",
    ".webp",
}


class Base(DeclarativeBase):
    pass


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(320), unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(200), nullable=False)
    company_name: Mapped[str] = mapped_column(String(200), nullable=False)
    kvk_number: Mapped[str] = mapped_column(String(8), nullable=False)
    vat_number: Mapped[str | None] = mapped_column(String(20))
    phone: Mapped[str | None] = mapped_column(String(50))
    street: Mapped[str | None] = mapped_column(String(200))
    postal_code: Mapped[str | None] = mapped_column(String(20))
    city: Mapped[str | None] = mapped_column(String(120))
    country: Mapped[str] = mapped_column(String(120), default="Nederland", nullable=False)
    iban: Mapped[str | None] = mapped_column(String(42))
    payment_term_days: Mapped[int] = mapped_column(Integer, default=30, nullable=False)
    invoice_prefix: Mapped[str | None] = mapped_column(String(12))
    email_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    verification_hash: Mapped[str | None] = mapped_column(String(64))
    verification_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    terms_accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    session_version: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    notify_email: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    notify_sms: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    notify_backup: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class Customer(Base):
    __tablename__ = "customers"

    id: Mapped[int] = mapped_column(primary_key=True)
    owner_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    contact: Mapped[str | None] = mapped_column(String(200))
    email: Mapped[str | None] = mapped_column(String(320))
    phone: Mapped[str | None] = mapped_column(String(50))
    street: Mapped[str | None] = mapped_column(String(200))
    postal_code: Mapped[str | None] = mapped_column(String(20))
    city: Mapped[str | None] = mapped_column(String(120))
    country: Mapped[str] = mapped_column(String(120), default="Nederland", nullable=False)
    vat_number: Mapped[str | None] = mapped_column(String(20))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class Invoice(Base):
    __tablename__ = "invoices"
    __table_args__ = (UniqueConstraint("owner_id", "number", name="uq_invoices_owner_number"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    owner_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id"), nullable=False)
    number: Mapped[str] = mapped_column(String(50), nullable=False)
    invoice_date: Mapped[date] = mapped_column(Date, nullable=False)
    due_date: Mapped[date] = mapped_column(Date, nullable=False)
    delivery_date: Mapped[date | None] = mapped_column(Date)
    note: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), default="pending", nullable=False)
    subtotal_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    vat_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    total_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lines: Mapped[list[InvoiceLine]] = relationship(
        back_populates="invoice", cascade="all, delete-orphan"
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class InvoiceLine(Base):
    __tablename__ = "invoice_lines"

    id: Mapped[int] = mapped_column(primary_key=True)
    invoice_id: Mapped[int] = mapped_column(ForeignKey("invoices.id"), nullable=False)
    description: Mapped[str] = mapped_column(String(500), nullable=False)
    quantity: Mapped[Decimal] = mapped_column(Numeric(12, 3), nullable=False)
    unit_price_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    vat_rate_bps: Mapped[int] = mapped_column(Integer, nullable=False)
    net_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    vat_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    invoice: Mapped[Invoice] = relationship(back_populates="lines")


class Receipt(Base):
    __tablename__ = "receipts"

    id: Mapped[int] = mapped_column(primary_key=True)
    owner_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    vendor: Mapped[str] = mapped_column(String(160), nullable=False)
    receipt_date: Mapped[date] = mapped_column(Date, nullable=False)
    amount_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    category: Mapped[str] = mapped_column(String(60), nullable=False)
    filename: Mapped[str] = mapped_column(String(200), nullable=False)
    content_type: Mapped[str] = mapped_column(String(100), nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class VatDeclaration(Base):
    __tablename__ = "vat_declarations"
    __table_args__ = (UniqueConstraint("owner_id", "year", "quarter", name="uq_vat_owner_period"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    owner_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    year: Mapped[int] = mapped_column(Integer, nullable=False)
    quarter: Mapped[int] = mapped_column(Integer, nullable=False)
    net_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    vat_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    approved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class PasswordReset(Base):
    __tablename__ = "password_resets"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class AuditEvent(Base):
    __tablename__ = "audit_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    owner_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    action: Mapped[str] = mapped_column(String(60), nullable=False)
    detail: Mapped[str | None] = mapped_column(String(300))
    ip: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False, index=True
    )


# ---------------------------------------------------------------------------
# Request/response models
# ---------------------------------------------------------------------------


def clean_optional(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


class CustomerInput(BaseModel):
    name: Annotated[str, Field(min_length=1, max_length=200)]
    contact: Annotated[str | None, Field(max_length=200)] = None
    email: Annotated[str | None, Field(max_length=320)] = None
    phone: Annotated[str | None, Field(max_length=50)] = None
    street: Annotated[str | None, Field(max_length=200)] = None
    postal_code: Annotated[str | None, Field(max_length=20)] = None
    city: Annotated[str | None, Field(max_length=120)] = None
    country: Annotated[str, Field(max_length=120)] = "Nederland"
    vat_number: Annotated[str | None, Field(max_length=20)] = None

    @field_validator("name", "contact", "email", "phone", "street", "postal_code", "city", "vat_number")
    @classmethod
    def strip_text(cls, value: str | None) -> str | None:
        return clean_optional(value)

    @field_validator("country")
    @classmethod
    def default_country(cls, value: str) -> str:
        return (value or "").strip() or "Nederland"


class CustomerOutput(CustomerInput):
    model_config = ConfigDict(from_attributes=True)

    id: int


class RegisterInput(BaseModel):
    email: Annotated[str, Field(min_length=3, max_length=320)]
    password: Annotated[str, Field(min_length=12, max_length=1024)]
    company_name: Annotated[str, Field(min_length=1, max_length=200)]
    kvk_number: Annotated[str, Field(pattern=r"^[0-9]{8}$")]
    vat_number: Annotated[str | None, Field(max_length=20)] = None
    terms: bool

    @field_validator("email")
    @classmethod
    def normalize_email(cls, value: str) -> str:
        normalized = value.strip().lower()
        if "@" not in normalized or "." not in normalized.split("@")[-1]:
            raise ValueError("Vul een geldig e-mailadres in")
        return normalized

    @field_validator("company_name", "vat_number")
    @classmethod
    def strip_optional_text(cls, value: str | None) -> str | None:
        return clean_optional(value)

    @field_validator("terms")
    @classmethod
    def accept_terms(cls, value: bool) -> bool:
        if not value:
            raise ValueError("Je moet de voorwaarden accepteren")
        return value


class LoginInput(BaseModel):
    email: Annotated[str, Field(min_length=3, max_length=320)]
    password: Annotated[str, Field(min_length=1, max_length=1024)]

    @field_validator("email")
    @classmethod
    def normalize_email(cls, value: str) -> str:
        return value.strip().lower()


class UserOutput(BaseModel):
    id: int
    email: str
    company_name: str
    kvk_number: str
    vat_number: str | None
    phone: str | None
    street: str | None
    postal_code: str | None
    city: str | None
    country: str
    iban: str | None
    payment_term_days: int
    invoice_prefix: str | None
    email_verified: bool
    notify_email: bool
    notify_sms: bool
    notify_backup: bool

    model_config = ConfigDict(from_attributes=True)


class RegisterOutput(UserOutput):
    verification_required: bool = False


class ProfileInput(BaseModel):
    company_name: Annotated[str, Field(min_length=1, max_length=200)]
    kvk_number: Annotated[str, Field(pattern=r"^[0-9]{8}$")]
    vat_number: Annotated[str | None, Field(max_length=20)] = None
    phone: Annotated[str | None, Field(max_length=50)] = None
    street: Annotated[str | None, Field(max_length=200)] = None
    postal_code: Annotated[str | None, Field(max_length=20)] = None
    city: Annotated[str | None, Field(max_length=120)] = None
    country: Annotated[str, Field(max_length=120)] = "Nederland"
    iban: Annotated[str | None, Field(max_length=42)] = None
    payment_term_days: Annotated[int, Field(ge=0, le=365)] = 30
    invoice_prefix: Annotated[str | None, Field(max_length=12)] = None
    notify_email: bool = True
    notify_sms: bool = False
    notify_backup: bool = True

    @field_validator("company_name", "vat_number", "phone", "street", "postal_code", "city", "iban", "invoice_prefix")
    @classmethod
    def strip_text(cls, value: str | None) -> str | None:
        return clean_optional(value)

    @field_validator("country")
    @classmethod
    def default_country(cls, value: str) -> str:
        return (value or "").strip() or "Nederland"

    @field_validator("iban")
    @classmethod
    def normalize_iban(cls, value: str | None) -> str | None:
        cleaned = clean_optional(value)
        return cleaned.replace(" ", "").upper() if cleaned else None

    @field_validator("invoice_prefix")
    @classmethod
    def normalize_prefix(cls, value: str | None) -> str | None:
        cleaned = clean_optional(value)
        return cleaned.upper() if cleaned else None


class PasswordInput(BaseModel):
    current_password: Annotated[str, Field(min_length=1, max_length=1024)]
    new_password: Annotated[str, Field(min_length=12, max_length=1024)]


class ForgotPasswordInput(BaseModel):
    email: Annotated[str, Field(min_length=3, max_length=320)]

    @field_validator("email")
    @classmethod
    def normalize_email(cls, value: str) -> str:
        return value.strip().lower()


class ResetPasswordInput(BaseModel):
    token: Annotated[str, Field(min_length=20, max_length=200)]
    password: Annotated[str, Field(min_length=12, max_length=1024)]


class TokenInput(BaseModel):
    token: Annotated[str, Field(min_length=20, max_length=200)]


class DeleteAccountInput(BaseModel):
    password: Annotated[str, Field(min_length=1, max_length=1024)]
    confirm: Annotated[str, Field(min_length=1, max_length=40)]


class InvoiceLineInput(BaseModel):
    description: Annotated[str, Field(min_length=1, max_length=500)]
    quantity: Annotated[Decimal, Field(gt=0, max_digits=12, decimal_places=3)]
    unit_price: Annotated[Decimal, Field(ge=0, max_digits=12, decimal_places=2)]
    vat_rate: Annotated[Decimal, Field(ge=0, le=1, max_digits=4, decimal_places=3)]

    @field_validator("description")
    @classmethod
    def strip_description(cls, value: str) -> str:
        return value.strip()

    @field_validator("vat_rate")
    @classmethod
    def dutch_vat_rate(cls, value: Decimal) -> Decimal:
        if value not in VAT_RATES:
            raise ValueError("Kies een Nederlands btw-tarief: 21%, 9% of 0%")
        return value


class InvoiceInput(BaseModel):
    customer_id: int = Field(gt=0)
    number: Annotated[str, Field(min_length=1, max_length=50)]
    invoice_date: date
    due_date: date
    delivery_date: date | None = None
    note: Annotated[str | None, Field(max_length=1000)] = None
    lines: Annotated[list[InvoiceLineInput], Field(min_length=1, max_length=100)]

    @field_validator("number")
    @classmethod
    def strip_number(cls, value: str) -> str:
        return value.strip()

    @field_validator("note")
    @classmethod
    def strip_note(cls, value: str | None) -> str | None:
        return clean_optional(value)

    @field_validator("due_date")
    @classmethod
    def due_date_not_before_invoice(cls, value: date, info) -> date:
        invoice_date = info.data.get("invoice_date")
        if invoice_date and value < invoice_date:
            raise ValueError("Vervaldatum mag niet voor de factuurdatum liggen")
        return value

    @field_validator("delivery_date")
    @classmethod
    def delivery_not_before_invoice(cls, value: date | None, info) -> date | None:
        invoice_date = info.data.get("invoice_date")
        if value and invoice_date and value < invoice_date:
            raise ValueError("Leverdatum mag niet voor de factuurdatum liggen")
        return value


class InvoiceLineOutput(BaseModel):
    description: str
    quantity: Decimal
    unit_price_cents: int
    vat_rate_bps: int
    net_cents: int
    vat_cents: int

    model_config = ConfigDict(from_attributes=True)


class InvoiceOutput(BaseModel):
    id: int
    customer_id: int
    number: str
    invoice_date: date
    due_date: date
    delivery_date: date | None
    note: str | None
    status: str
    subtotal_cents: int
    vat_cents: int
    total_cents: int
    paid_at: datetime | None
    sent_at: datetime | None
    lines: list[InvoiceLineOutput]

    model_config = ConfigDict(from_attributes=True)


class InvoiceStatusInput(BaseModel):
    status: Literal["pending", "paid"]


class SendInvoiceInput(BaseModel):
    to: Annotated[str | None, Field(max_length=320)] = None
    message: Annotated[str | None, Field(max_length=1000)] = None


class SendInvoiceOutput(BaseModel):
    sent: bool
    mode: str
    to: str
    sent_at: datetime | None


class VatRateOutput(BaseModel):
    rate_bps: int
    net_cents: int
    vat_cents: int


class VatQuarterOutput(BaseModel):
    year: int
    quarter: int
    label: str
    start_date: date
    end_date: date
    deadline: date
    invoice_count: int
    net_cents: int
    vat_cents: int
    rates: list[VatRateOutput]
    approved_at: datetime | None
    approved_on: date | None


class VatOverviewOutput(BaseModel):
    current: VatQuarterOutput
    quarters: list[VatQuarterOutput]


class VatApproveInput(BaseModel):
    year: Annotated[int, Field(ge=2000, le=2100)]
    quarter: Annotated[int, Field(ge=1, le=4)]


class ReceiptOutput(BaseModel):
    id: int
    vendor: str
    receipt_date: date
    amount_cents: int
    category: str
    content_type: str
    size_bytes: int
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class AuditOutput(BaseModel):
    action: str
    detail: str | None
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def to_cents(amount: Decimal) -> int:
    return int((amount * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=2**14, r=8, p=1)
    return f"{salt.hex()}${digest.hex()}"


def verify_password(password: str, stored_hash: str) -> bool:
    try:
        salt_hex, digest_hex = stored_hash.split("$", maxsplit=1)
        expected = bytes.fromhex(digest_hex)
        actual = hashlib.scrypt(
            password.encode("utf-8"), salt=bytes.fromhex(salt_hex), n=2**14, r=8, p=1
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(actual, expected)


def new_token() -> tuple[str, str]:
    token = secrets.token_urlsafe(32)
    return token, hashlib.sha256(token.encode("utf-8")).hexdigest()


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def quarter_bounds(year: int, quarter: int) -> tuple[date, date]:
    start_month = (quarter - 1) * 3 + 1
    end_month = start_month + 2
    start = date(year, start_month, 1)
    if end_month == 12:
        end = date(year, 12, 31)
    else:
        next_month = date(year, end_month + 1, 1)
        end = next_month.fromordinal(next_month.toordinal() - 1)
    return start, end


def vat_deadline(year: int, quarter: int) -> date:
    deadline_year = year if quarter < 4 else year + 1
    deadline_month = quarter * 3 + 1
    if deadline_month > 12:
        deadline_month -= 12
    if deadline_month == 12:
        return date(deadline_year, 12, 31)
    next_month = date(deadline_year, deadline_month + 1, 1)
    return next_month.fromordinal(next_month.toordinal() - 1)


def previous_quarter(year: int, quarter: int) -> tuple[int, int]:
    return (year - 1, 4) if quarter == 1 else (year, quarter - 1)


def safe_filename(value: str) -> str:
    cleaned = "".join(character for character in value if character.isalnum() or character in "-_")
    return cleaned or "factuur"


class RateLimiter:
    """Small in-process sliding window limiter.

    One gunicorn/uvicorn worker is the normal deployment for a product this
    size. Behind multiple workers the limit is per worker, which is still far
    better than no limit; the deployment guide explains how to move it to Redis
    when that becomes necessary.
    """

    def __init__(self) -> None:
        self._events: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def hit(self, key: str, limit: int, window_seconds: int) -> tuple[bool, int]:
        now = time.monotonic()
        with self._lock:
            events = [stamp for stamp in self._events.get(key, []) if now - stamp < window_seconds]
            allowed = len(events) < limit
            if allowed:
                events.append(now)
            self._events[key] = events
            retry_after = 0
            if not allowed and events:
                retry_after = max(1, int(window_seconds - (now - events[0])) + 1)
            if len(self._events) > 10_000:
                self._events = {
                    name: stamps for name, stamps in self._events.items() if stamps and now - stamps[-1] < 3600
                }
        return allowed, retry_after

    def clear(self) -> None:
        with self._lock:
            self._events.clear()


def configure_sqlite(engine) -> None:
    if engine.dialect.name != "sqlite":
        return

    @event.listens_for(engine, "connect")
    def _on_connect(dbapi_connection, _record):  # pragma: no cover - driver hook
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.close()


def create_app(
    database_url: str | None = None,
    *,
    settings: Settings | None = None,
    mailer: Mailer | None = None,
    upload_dir: Path | None = None,
) -> FastAPI:
    settings = settings or Settings.from_environment()
    if database_url:
        settings = Settings(
            env=settings.env,
            secret_key=settings.secret_key,
            database_url=database_url,
            public_url=settings.public_url,
            upload_dir=upload_dir or settings.upload_dir,
            allowed_hosts=settings.allowed_hosts,
            session_max_age=settings.session_max_age,
            max_upload_bytes=settings.max_upload_bytes,
        )
    if upload_dir is not None:
        settings = Settings(
            env=settings.env,
            secret_key=settings.secret_key,
            database_url=settings.database_url,
            public_url=settings.public_url,
            upload_dir=upload_dir,
            allowed_hosts=settings.allowed_hosts,
            session_max_age=settings.session_max_age,
            max_upload_bytes=settings.max_upload_bytes,
        )
    settings.validate()

    if not logging.getLogger().handlers:
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s %(levelname)s %(name)s %(message)s",
        )

    connect_args = {"check_same_thread": False} if settings.database_url.startswith("sqlite") else {}
    engine = create_engine(
        settings.database_url,
        connect_args=connect_args,
        pool_pre_ping=not settings.database_url.startswith("sqlite"),
    )
    configure_sqlite(engine)
    Base.metadata.create_all(engine)
    run_migrations(engine, Base.metadata)
    session_factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

    mailer = mailer or Mailer()
    upload_root = Path(settings.upload_dir)
    upload_root.mkdir(parents=True, exist_ok=True)
    limiter = RateLimiter()

    web_root = Path(__file__).resolve().parent.parent

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        if not settings.secret_key:
            logger.warning(
                "KASFLOW_SECRET_KEY is not set; using a random key so sessions end on restart"
            )
        logger.info(
            "Kasflow gestart (env=%s, mail=%s, db=%s)",
            settings.env,
            "smtp" if mailer.configured else "log",
            settings.database_url.split("://", 1)[0],
        )
        yield
        engine.dispose()

    app = FastAPI(title="Kasflow API", version="2.0.0", lifespan=lifespan)
    app.state.settings = settings
    app.state.mailer = mailer
    app.state.limiter = limiter
    app.state.web_root = web_root
    app.state.upload_root = upload_root

    if settings.allowed_hosts:
        app.add_middleware(TrustedHostMiddleware, allowed_hosts=list(settings.allowed_hosts))
    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.secret_key or secrets.token_urlsafe(32),
        session_cookie="kasflow_session",
        max_age=settings.session_max_age,
        same_site="lax",
        https_only=settings.cookie_secure,
    )

    @app.middleware("http")
    async def security_guard(request: Request, call_next):
        if request.method in {"POST", "PUT", "PATCH", "DELETE"} and request.url.path.startswith("/api/"):
            origin = request.headers.get("origin")
            if origin:
                origin_host = urlparse(origin).netloc
                if not origin_host or origin_host != request.headers.get("host"):
                    return JSONResponse(status_code=403, content={"detail": "Ongeldige aanvraag"})
        response = await call_next(request)
        headers = response.headers
        headers.setdefault("X-Content-Type-Options", "nosniff")
        headers.setdefault("X-Frame-Options", "DENY")
        headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' data: blob:; style-src 'self' 'unsafe-inline'; "
            "script-src 'self' 'unsafe-inline'; connect-src 'self'; font-src 'self'; "
            "object-src 'none'; base-uri 'self'; form-action 'self'; frame-ancestors 'none'",
        )
        if settings.production:
            headers.setdefault(
                "Strict-Transport-Security", "max-age=31536000; includeSubDomains"
            )
        path = request.url.path
        if path.endswith(".html") or path.startswith("/api/"):
            headers.setdefault("Cache-Control", "no-store")
        else:
            # Static assets have no content hash in their name, so they must be
            # revalidated after every deploy. Starlette answers with a 304 when
            # the file is unchanged, which keeps repeat visits cheap.
            headers.setdefault("Cache-Control", "no-cache")
        return response

    def get_session():
        with session_factory() as session:
            yield session

    def client_ip(request: Request) -> str:
        # Never read X-Forwarded-For here: clients can set it themselves and
        # would get a fresh rate-limit bucket per request. Behind the proxy,
        # uvicorn --proxy-headers already puts the real address in request.client.
        return request.client.host if request.client else "unknown"

    def audit(session: Session, user_id: int | None, action: str, detail: str | None, ip: str | None) -> None:
        session.add(AuditEvent(owner_id=user_id, action=action, detail=detail, ip=ip))

    def enforce(request: Request, scope: str, limit: int, window: int, extra: str = "") -> None:
        key = f"{scope}:{client_ip(request)}:{extra}"
        allowed, retry_after = limiter.hit(key, limit, window)
        if not allowed:
            raise HTTPException(
                status_code=429,
                detail="Te veel pogingen. Probeer het over een paar minuten opnieuw.",
                headers={"Retry-After": str(retry_after)},
            )

    def get_current_user(
        request: Request, session: Session = Depends(get_session)
    ) -> User:
        user_id = request.session.get("user_id")
        user = session.get(User, user_id) if user_id else None
        if user is None:
            request.session.clear()
            raise HTTPException(status_code=401, detail="Log in om door te gaan")
        if request.session.get("ver", 0) != user.session_version:
            request.session.clear()
            raise HTTPException(status_code=401, detail="Je sessie is verlopen. Log opnieuw in.")
        return user

    def start_session(request: Request, user: User) -> None:
        request.session.clear()
        request.session["user_id"] = user.id
        request.session["ver"] = user.session_version

    def get_owned_invoice(session: Session, user: User, invoice_id: int) -> Invoice:
        invoice = session.scalar(
            select(Invoice)
            .options(selectinload(Invoice.lines))
            .where(Invoice.id == invoice_id, Invoice.owner_id == user.id)
        )
        if invoice is None:
            raise HTTPException(status_code=404, detail="Factuur niet gevonden")
        return invoice

    def user_output(user: User) -> dict:
        return {
            "id": user.id,
            "email": user.email,
            "company_name": user.company_name,
            "kvk_number": user.kvk_number,
            "vat_number": user.vat_number,
            "phone": user.phone,
            "street": user.street,
            "postal_code": user.postal_code,
            "city": user.city,
            "country": user.country,
            "iban": user.iban,
            "payment_term_days": user.payment_term_days,
            "invoice_prefix": user.invoice_prefix,
            "email_verified": user.email_verified_at is not None,
            "notify_email": user.notify_email,
            "notify_sms": user.notify_sms,
            "notify_backup": user.notify_backup,
        }

    def missing_invoice_fields(user: User, customer: Customer) -> list[str]:
        missing = []
        if not (user.street and user.postal_code and user.city):
            missing.append("je bedrijfsadres")
        if not user.vat_number:
            missing.append("je btw-id")
        if not (customer.street and customer.postal_code and customer.city):
            missing.append(f"het adres van {customer.name}")
        return missing

    def invoice_pdf_bytes(user: User, customer: Customer, invoice: Invoice) -> bytes:
        supplier = Party(
            name=user.company_name,
            street=user.street or "",
            postal_code=user.postal_code or "",
            city=user.city or "",
            country=user.country or "Nederland",
            email=user.email,
            phone=user.phone or "",
            vat_number=user.vat_number or "",
            kvk_number=user.kvk_number,
            iban=user.iban or "",
        )
        recipient = Party(
            name=customer.name,
            street=customer.street or "",
            postal_code=customer.postal_code or "",
            city=customer.city or "",
            country=customer.country or "Nederland",
            contact=customer.contact or "",
            email=customer.email or "",
            phone=customer.phone or "",
            vat_number=customer.vat_number or "",
        )
        lines = [
            InvoiceLineData(
                description=line.description,
                quantity=line.quantity,
                unit_price_cents=line.unit_price_cents,
                vat_rate_bps=line.vat_rate_bps,
                net_cents=line.net_cents,
                vat_cents=line.vat_cents,
            )
            for line in invoice.lines
        ]
        return build_invoice_pdf(
            InvoicePdfData(
                invoice_number=invoice.number,
                invoice_date=invoice.invoice_date,
                due_date=invoice.due_date,
                delivery_date=invoice.delivery_date,
                payment_term_days=max(0, (invoice.due_date - invoice.invoice_date).days),
                supplier=supplier,
                customer=recipient,
                lines=lines,
                subtotal_cents=invoice.subtotal_cents,
                vat_cents=invoice.vat_cents,
                total_cents=invoice.total_cents,
                note=invoice.note or "",
                paid=invoice.status == "paid",
                missing_fields=missing_invoice_fields(user, customer),
            )
        )

    def next_invoice_number(session: Session, user: User) -> str:
        year = date.today().year
        prefix = user.invoice_prefix or ""
        base = f"{prefix}{year}-"
        numbers = session.scalars(
            select(Invoice.number).where(
                Invoice.owner_id == user.id, Invoice.number.like(f"{base}%")
            )
        ).all()
        highest = 0
        for number in numbers:
            suffix = number[len(base) :]
            if suffix.isdigit():
                highest = max(highest, int(suffix))
        return f"{base}{highest + 1:03d}"

    def build_vat_quarter(session: Session, user: User, year: int, quarter: int) -> VatQuarterOutput:
        start, end = quarter_bounds(year, quarter)
        invoices = session.scalars(
            select(Invoice)
            .options(selectinload(Invoice.lines))
            .where(
                Invoice.owner_id == user.id,
                Invoice.invoice_date >= start,
                Invoice.invoice_date <= end,
            )
        ).all()

        rate_totals: dict[int, dict[str, int]] = {}
        net_cents = 0
        vat_cents = 0
        for invoice in invoices:
            for line in invoice.lines:
                bucket = rate_totals.setdefault(line.vat_rate_bps, {"net": 0, "vat": 0})
                bucket["net"] += line.net_cents
                bucket["vat"] += line.vat_cents
                net_cents += line.net_cents
                vat_cents += line.vat_cents

        declaration = session.scalar(
            select(VatDeclaration).where(
                VatDeclaration.owner_id == user.id,
                VatDeclaration.year == year,
                VatDeclaration.quarter == quarter,
            )
        )

        return VatQuarterOutput(
            year=year,
            quarter=quarter,
            label=f"Q{quarter} {year}",
            start_date=start,
            end_date=end,
            deadline=vat_deadline(year, quarter),
            invoice_count=len(invoices),
            net_cents=net_cents,
            vat_cents=vat_cents,
            rates=[
                VatRateOutput(rate_bps=rate, net_cents=totals["net"], vat_cents=totals["vat"])
                for rate, totals in sorted(rate_totals.items(), reverse=True)
            ],
            approved_at=declaration.approved_at if declaration else None,
            approved_on=declaration.approved_at.date() if declaration else None,
        )

    # -- Auth -------------------------------------------------------------

    @app.post("/api/auth/register", response_model=RegisterOutput, status_code=201)
    def register(
        payload: RegisterInput, request: Request, session: Session = Depends(get_session)
    ):
        enforce(request, "register", 5, 3600)
        verified_at = None if mailer.configured else utcnow()
        user = User(
            email=payload.email,
            password_hash=hash_password(payload.password),
            company_name=payload.company_name,
            kvk_number=payload.kvk_number,
            vat_number=payload.vat_number,
            email_verified_at=verified_at,
            terms_accepted_at=utcnow(),
        )
        session.add(user)
        try:
            session.commit()
        except IntegrityError as error:
            session.rollback()
            raise HTTPException(
                status_code=409, detail="Dit e-mailadres heeft al een account"
            ) from error
        session.refresh(user)
        audit(session, user.id, "account.register", None, client_ip(request))
        session.commit()

        verification_required = mailer.configured
        if verification_required:
            token, hashed = new_token()
            user.verification_hash = hashed
            user.verification_expires_at = utcnow() + timedelta(hours=48)
            session.commit()
            mailer.send_email_verification(
                {
                    "email": user.email,
                    "company_name": user.company_name,
                    "token": token,
                }
            )
        else:
            start_session(request, user)
        return {**user_output(user), "verification_required": verification_required}

    @app.post("/api/auth/login", response_model=UserOutput)
    def login(
        payload: LoginInput, request: Request, session: Session = Depends(get_session)
    ):
        enforce(request, "login", 10, 900, payload.email)
        user = session.scalar(select(User).where(User.email == payload.email))
        if user is None or not verify_password(payload.password, user.password_hash):
            raise HTTPException(
                status_code=401, detail="E-mailadres of wachtwoord is onjuist"
            )
        if mailer.configured and user.email_verified_at is None:
            raise HTTPException(
                status_code=403,
                detail="Bevestig eerst je e-mailadres. Check je inbox of vraag een nieuwe link aan.",
            )
        start_session(request, user)
        audit(session, user.id, "account.login", None, client_ip(request))
        session.commit()
        return user_output(user)

    @app.get("/api/auth/me", response_model=UserOutput)
    def current_user(user: User = Depends(get_current_user)):
        return user_output(user)

    @app.patch("/api/auth/me", response_model=UserOutput)
    def update_profile(
        payload: ProfileInput,
        request: Request,
        session: Session = Depends(get_session),
        user: User = Depends(get_current_user),
    ):
        for field, value in payload.model_dump().items():
            setattr(user, field, value)
        audit(session, user.id, "account.profile", None, client_ip(request))
        session.commit()
        session.refresh(user)
        return user_output(user)

    @app.post("/api/auth/password", status_code=204)
    def change_password(
        payload: PasswordInput,
        request: Request,
        session: Session = Depends(get_session),
        user: User = Depends(get_current_user),
    ):
        enforce(request, "change-password", 10, 3600, str(user.id))
        if not verify_password(payload.current_password, user.password_hash):
            raise HTTPException(status_code=400, detail="Je huidige wachtwoord is onjuist")
        user.password_hash = hash_password(payload.new_password)
        user.session_version += 1
        audit(session, user.id, "account.password", None, client_ip(request))
        session.commit()
        start_session(request, user)

    @app.post("/api/auth/logout", status_code=204)
    def logout(request: Request, _: User = Depends(get_current_user)):
        request.session.clear()

    @app.post("/api/auth/forgot-password", status_code=202)
    def forgot_password(
        payload: ForgotPasswordInput, request: Request, session: Session = Depends(get_session)
    ):
        enforce(request, "forgot-password", 5, 3600, payload.email)
        user = session.scalar(select(User).where(User.email == payload.email))
        if user is not None:
            token, hashed = new_token()
            session.add(
                PasswordReset(
                    user_id=user.id,
                    token_hash=hashed,
                    expires_at=utcnow() + timedelta(hours=1),
                )
            )
            audit(session, user.id, "account.reset_requested", None, client_ip(request))
            session.commit()
            mailer.send_password_reset(
                {"email": user.email, "company_name": user.company_name, "token": token}
            )
        else:
            # Same response time and answer: never reveal which addresses exist.
            logger.info("Wachtwoordreset aangevraagd voor onbekend adres uit %s", client_ip(request))
        return {"status": "accepted"}

    @app.post("/api/auth/reset-password", response_model=UserOutput)
    def reset_password(
        payload: ResetPasswordInput, request: Request, session: Session = Depends(get_session)
    ):
        enforce(request, "reset-password", 10, 3600)
        reset = session.scalar(
            select(PasswordReset).where(PasswordReset.token_hash == token_hash(payload.token))
        )
        if reset is None or reset.used_at is not None:
            raise HTTPException(status_code=400, detail="Deze link is ongeldig of al gebruikt")
        expires_at = reset.expires_at
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        if expires_at < utcnow():
            raise HTTPException(status_code=400, detail="Deze link is verlopen. Vraag een nieuwe aan.")
        user = session.get(User, reset.user_id)
        if user is None:
            raise HTTPException(status_code=400, detail="Deze link is ongeldig")
        user.password_hash = hash_password(payload.password)
        user.session_version += 1
        reset.used_at = utcnow()
        audit(session, user.id, "account.password_reset", None, client_ip(request))
        session.commit()
        start_session(request, user)
        return user_output(user)

    @app.post("/api/auth/verify-email", response_model=UserOutput)
    def verify_email(
        payload: TokenInput, request: Request, session: Session = Depends(get_session)
    ):
        enforce(request, "verify-email", 20, 3600)
        user = session.scalar(
            select(User).where(User.verification_hash == token_hash(payload.token))
        )
        if user is None:
            raise HTTPException(status_code=400, detail="Deze bevestigingslink is ongeldig")
        expires_at = user.verification_expires_at
        if expires_at and expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        if user.email_verified_at is None:
            if expires_at and expires_at < utcnow():
                raise HTTPException(
                    status_code=400, detail="Deze link is verlopen. Vraag een nieuwe link aan."
                )
            user.email_verified_at = utcnow()
        user.verification_hash = None
        user.verification_expires_at = None
        audit(session, user.id, "account.email_verified", None, client_ip(request))
        session.commit()
        start_session(request, user)
        return user_output(user)

    @app.post("/api/auth/resend-verification", status_code=202)
    def resend_verification(
        request: Request,
        session: Session = Depends(get_session),
        user: User = Depends(get_current_user),
    ):
        enforce(request, "resend-verification", 3, 3600, str(user.id))
        if user.email_verified_at is not None:
            return {"status": "already_verified"}
        if not mailer.configured:
            user.email_verified_at = utcnow()
            session.commit()
            return {"status": "verified"}
        token, hashed = new_token()
        user.verification_hash = hashed
        user.verification_expires_at = utcnow() + timedelta(hours=48)
        audit(session, user.id, "account.verification_sent", None, client_ip(request))
        session.commit()
        mailer.send_email_verification(
            {"email": user.email, "company_name": user.company_name, "token": token}
        )
        return {"status": "sent"}

    @app.get("/api/health")
    def health(session: Session = Depends(get_session)) -> dict:
        try:
            session.execute(text("SELECT 1"))
            database = "ok"
        except Exception:  # pragma: no cover - only on a broken database
            logger.exception("Healthcheck: database niet bereikbaar")
            database = "error"
        return {
            "status": "ok" if database == "ok" else "degraded",
            "version": "2.0.0",
            "database": database,
            "mail": "smtp" if mailer.configured else "log",
            "env": settings.env,
        }

    # -- Account (AVG) ----------------------------------------------------

    @app.get("/api/account/export")
    def export_account(
        request: Request,
        session: Session = Depends(get_session),
        user: User = Depends(get_current_user),
    ):
        enforce(request, "export", 3, 3600, str(user.id))
        audit(session, user.id, "account.export", None, client_ip(request))
        session.commit()
        customers = session.scalars(
            select(Customer).where(Customer.owner_id == user.id).order_by(Customer.id)
        ).all()
        invoices = session.scalars(
            select(Invoice)
            .options(selectinload(Invoice.lines))
            .where(Invoice.owner_id == user.id)
            .order_by(Invoice.id)
        ).all()
        receipts = session.scalars(
            select(Receipt).where(Receipt.owner_id == user.id).order_by(Receipt.id)
        ).all()
        declarations = session.scalars(
            select(VatDeclaration).where(VatDeclaration.owner_id == user.id)
        ).all()
        events = session.scalars(
            select(AuditEvent).where(AuditEvent.owner_id == user.id).order_by(AuditEvent.id)
        ).all()

        payload = {
            "exported_at": utcnow().isoformat(),
            "account": user_output(user),
            "customers": [
                {
                    "id": customer.id,
                    "name": customer.name,
                    "contact": customer.contact,
                    "email": customer.email,
                    "phone": customer.phone,
                    "street": customer.street,
                    "postal_code": customer.postal_code,
                    "city": customer.city,
                    "country": customer.country,
                    "vat_number": customer.vat_number,
                    "created_at": customer.created_at.isoformat(),
                }
                for customer in customers
            ],
            "invoices": [
                {
                    "id": invoice.id,
                    "number": invoice.number,
                    "customer_id": invoice.customer_id,
                    "invoice_date": invoice.invoice_date.isoformat(),
                    "due_date": invoice.due_date.isoformat(),
                    "delivery_date": invoice.delivery_date.isoformat() if invoice.delivery_date else None,
                    "note": invoice.note,
                    "status": invoice.status,
                    "subtotal_cents": invoice.subtotal_cents,
                    "vat_cents": invoice.vat_cents,
                    "total_cents": invoice.total_cents,
                    "paid_at": invoice.paid_at.isoformat() if invoice.paid_at else None,
                    "sent_at": invoice.sent_at.isoformat() if invoice.sent_at else None,
                    "lines": [
                        {
                            "description": line.description,
                            "quantity": str(line.quantity),
                            "unit_price_cents": line.unit_price_cents,
                            "vat_rate_bps": line.vat_rate_bps,
                            "net_cents": line.net_cents,
                            "vat_cents": line.vat_cents,
                        }
                        for line in invoice.lines
                    ],
                }
                for invoice in invoices
            ],
            "receipts": [
                {
                    "id": receipt.id,
                    "vendor": receipt.vendor,
                    "receipt_date": receipt.receipt_date.isoformat(),
                    "amount_cents": receipt.amount_cents,
                    "category": receipt.category,
                    "content_type": receipt.content_type,
                    "size_bytes": receipt.size_bytes,
                    "file": f"bonnetjes/{receipt.id}{Path(receipt.filename).suffix}",
                }
                for receipt in receipts
            ],
            "vat_declarations": [
                {
                    "year": declaration.year,
                    "quarter": declaration.quarter,
                    "net_cents": declaration.net_cents,
                    "vat_cents": declaration.vat_cents,
                    "approved_at": declaration.approved_at.isoformat(),
                }
                for declaration in declarations
            ],
            "activity": [
                {"action": event.action, "detail": event.detail, "at": event.created_at.isoformat()}
                for event in events
            ],
        }

        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("kasflow-export.json", json.dumps(payload, ensure_ascii=False, indent=2))
            for receipt in receipts:
                path = (upload_root / receipt.filename).resolve()
                if upload_root.resolve() in path.parents and path.is_file():
                    archive.write(path, f"bonnetjes/{receipt.id}{path.suffix}")
        filename = f"kasflow-export-{date.today().isoformat()}.zip"
        return Response(
            content=buffer.getvalue(),
            media_type="application/zip",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    @app.get("/api/account/activity", response_model=list[AuditOutput])
    def account_activity(
        session: Session = Depends(get_session), user: User = Depends(get_current_user)
    ):
        return session.scalars(
            select(AuditEvent)
            .where(AuditEvent.owner_id == user.id)
            .order_by(AuditEvent.created_at.desc(), AuditEvent.id.desc())
            .limit(25)
        ).all()

    @app.delete("/api/account", status_code=204)
    def delete_account(
        request: Request,
        payload: DeleteAccountInput,
        session: Session = Depends(get_session),
        user: User = Depends(get_current_user),
    ):
        enforce(request, "delete-account", 3, 3600, str(user.id))
        if payload.confirm.strip().upper() != "VERWIJDER":
            raise HTTPException(
                status_code=422, detail='Typ VERWIJDER om je account definitief te verwijderen'
            )
        if not verify_password(payload.password, user.password_hash):
            raise HTTPException(status_code=400, detail="Je wachtwoord is onjuist")

        user_id = user.id
        for receipt in session.scalars(select(Receipt).where(Receipt.owner_id == user_id)).all():
            path = (upload_root / receipt.filename).resolve()
            if upload_root.resolve() in path.parents:
                path.unlink(missing_ok=True)
        directory = upload_root / str(user_id)
        if directory.is_dir():
            for child in directory.iterdir():
                child.unlink(missing_ok=True)
            directory.rmdir()

        invoice_ids = select(Invoice.id).where(Invoice.owner_id == user_id)
        session.execute(delete(InvoiceLine).where(InvoiceLine.invoice_id.in_(invoice_ids)))
        session.execute(delete(Invoice).where(Invoice.owner_id == user_id))
        session.execute(delete(Receipt).where(Receipt.owner_id == user_id))
        session.execute(delete(VatDeclaration).where(VatDeclaration.owner_id == user_id))
        session.execute(delete(PasswordReset).where(PasswordReset.user_id == user_id))
        session.execute(delete(AuditEvent).where(AuditEvent.owner_id == user_id))
        session.execute(delete(Customer).where(Customer.owner_id == user_id))
        session.delete(user)
        session.commit()
        request.session.clear()
        logger.info("Account %s definitief verwijderd", user_id)

    # -- Customers --------------------------------------------------------

    @app.get("/api/customers", response_model=list[CustomerOutput])
    def list_customers(
        session: Session = Depends(get_session),
        user: User = Depends(get_current_user),
    ):
        return session.scalars(
            select(Customer).where(Customer.owner_id == user.id).order_by(Customer.name)
        ).all()

    @app.post(
        "/api/customers", response_model=CustomerOutput, status_code=status.HTTP_201_CREATED
    )
    def create_customer(
        payload: CustomerInput,
        session: Session = Depends(get_session),
        user: User = Depends(get_current_user),
    ):
        customer = Customer(**payload.model_dump(), owner_id=user.id)
        session.add(customer)
        session.commit()
        session.refresh(customer)
        return customer

    @app.patch("/api/customers/{customer_id}", response_model=CustomerOutput)
    def update_customer(
        customer_id: int,
        payload: CustomerInput,
        session: Session = Depends(get_session),
        user: User = Depends(get_current_user),
    ):
        customer = session.get(Customer, customer_id)
        if customer is None or customer.owner_id != user.id:
            raise HTTPException(status_code=404, detail="Klant niet gevonden")
        for field, value in payload.model_dump().items():
            setattr(customer, field, value)
        session.commit()
        session.refresh(customer)
        return customer

    @app.delete("/api/customers/{customer_id}", status_code=204)
    def delete_customer(
        customer_id: int,
        session: Session = Depends(get_session),
        user: User = Depends(get_current_user),
    ):
        customer = session.get(Customer, customer_id)
        if customer is None or customer.owner_id != user.id:
            raise HTTPException(status_code=404, detail="Klant niet gevonden")
        has_invoices = session.scalar(
            select(Invoice.id).where(Invoice.customer_id == customer.id).limit(1)
        )
        if has_invoices is not None:
            raise HTTPException(
                status_code=409,
                detail="Deze klant heeft facturen en kan niet worden verwijderd",
            )
        session.delete(customer)
        session.commit()

    # -- Invoices ---------------------------------------------------------

    @app.get("/api/invoices", response_model=list[InvoiceOutput])
    def list_invoices(
        session: Session = Depends(get_session),
        user: User = Depends(get_current_user),
    ):
        return session.scalars(
            select(Invoice)
            .options(selectinload(Invoice.lines))
            .where(Invoice.owner_id == user.id)
            .order_by(Invoice.invoice_date.desc(), Invoice.id.desc())
        ).all()

    @app.get("/api/invoices/next-number")
    def next_number(
        session: Session = Depends(get_session), user: User = Depends(get_current_user)
    ) -> dict[str, str]:
        return {"number": next_invoice_number(session, user)}

    @app.get("/api/invoices/{invoice_id}", response_model=InvoiceOutput)
    def get_invoice(
        invoice_id: int,
        session: Session = Depends(get_session),
        user: User = Depends(get_current_user),
    ):
        return get_owned_invoice(session, user, invoice_id)

    @app.post(
        "/api/invoices", response_model=InvoiceOutput, status_code=status.HTTP_201_CREATED
    )
    def create_invoice(
        payload: InvoiceInput,
        request: Request,
        session: Session = Depends(get_session),
        user: User = Depends(get_current_user),
    ):
        customer = session.get(Customer, payload.customer_id)
        if customer is None or customer.owner_id != user.id:
            raise HTTPException(status_code=404, detail="Klant niet gevonden")

        duplicate = session.scalar(
            select(Invoice.id).where(Invoice.owner_id == user.id, Invoice.number == payload.number)
        )
        if duplicate is not None:
            raise HTTPException(status_code=409, detail="Factuurnummer bestaat al")

        invoice_quarter = (payload.invoice_date.month - 1) // 3 + 1
        approved = session.scalar(
            select(VatDeclaration.id).where(
                VatDeclaration.owner_id == user.id,
                VatDeclaration.year == payload.invoice_date.year,
                VatDeclaration.quarter == invoice_quarter,
            )
        )
        if approved is not None:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"Q{invoice_quarter} {payload.invoice_date.year} is al afgevinkt voor de btw. "
                    "Kies een factuurdatum in een open kwartaal of trek de afvinking eerst in."
                ),
            )

        subtotal_cents = 0
        vat_cents = 0
        stored_lines = []
        for line in payload.lines:
            net = line.quantity * line.unit_price
            line_net_cents = to_cents(net)
            line_vat_cents = to_cents(net * line.vat_rate)
            subtotal_cents += line_net_cents
            vat_cents += line_vat_cents
            stored_lines.append(
                InvoiceLine(
                    description=line.description,
                    quantity=line.quantity,
                    unit_price_cents=to_cents(line.unit_price),
                    vat_rate_bps=int(line.vat_rate * 10_000),
                    net_cents=line_net_cents,
                    vat_cents=line_vat_cents,
                )
            )

        invoice = Invoice(
            owner_id=user.id,
            customer_id=payload.customer_id,
            number=payload.number,
            invoice_date=payload.invoice_date,
            due_date=payload.due_date,
            delivery_date=payload.delivery_date,
            note=payload.note,
            subtotal_cents=subtotal_cents,
            vat_cents=vat_cents,
            total_cents=subtotal_cents + vat_cents,
            lines=stored_lines,
        )
        session.add(invoice)
        try:
            session.commit()
        except IntegrityError as error:
            session.rollback()
            raise HTTPException(status_code=409, detail="Factuurnummer bestaat al") from error
        audit(session, user.id, "invoice.create", invoice.number, client_ip(request))
        session.commit()
        return get_owned_invoice(session, user, invoice.id)

    @app.get("/api/invoices/{invoice_id}/pdf")
    def invoice_pdf(
        invoice_id: int,
        request: Request,
        session: Session = Depends(get_session),
        user: User = Depends(get_current_user),
    ):
        enforce(request, "invoice-pdf", 60, 3600, str(user.id))
        invoice = get_owned_invoice(session, user, invoice_id)
        customer = session.get(Customer, invoice.customer_id)
        if customer is None:
            raise HTTPException(status_code=404, detail="Klant niet gevonden")
        pdf = invoice_pdf_bytes(user, customer, invoice)
        audit(session, user.id, "invoice.pdf", invoice.number, client_ip(request))
        session.commit()
        filename = f"factuur-{safe_filename(invoice.number)}.pdf"
        return Response(
            content=pdf,
            media_type="application/pdf",
            headers={
                "Content-Disposition": f'attachment; filename="{filename}"',
                "Cache-Control": "no-store",
            },
        )

    @app.post("/api/invoices/{invoice_id}/send", response_model=SendInvoiceOutput)
    def send_invoice(
        invoice_id: int,
        payload: SendInvoiceInput,
        request: Request,
        session: Session = Depends(get_session),
        user: User = Depends(get_current_user),
    ):
        enforce(request, "invoice-send", 30, 3600, str(user.id))
        invoice = get_owned_invoice(session, user, invoice_id)
        customer = session.get(Customer, invoice.customer_id)
        if customer is None:
            raise HTTPException(status_code=404, detail="Klant niet gevonden")
        recipient = (payload.to or customer.email or "").strip()
        if "@" not in recipient:
            raise HTTPException(
                status_code=422,
                detail="Deze klant heeft geen e-mailadres. Vul een ontvanger in of bewerk de klant.",
            )
        missing = missing_invoice_fields(user, customer)
        if missing:
            raise HTTPException(
                status_code=422,
                detail=(
                    "Deze factuur mist verplichte gegevens: " + ", ".join(missing) + ". "
                    "Vul ze aan bij Instellingen of Klanten en verstuur de factuur opnieuw."
                ),
            )
        if payload.to:
            customer.email = recipient

        pdf = invoice_pdf_bytes(user, customer, invoice)
        sent = mailer.send_invoice(
            sender_name=user.company_name,
            customer_name=customer.name,
            customer_email=recipient,
            invoice_number=invoice.number,
            total_cents=invoice.total_cents,
            due_date=invoice.due_date.strftime("%d-%m-%Y"),
            note=payload.message or invoice.note or "",
            pdf_bytes=pdf,
            pdf_filename=f"factuur-{safe_filename(invoice.number)}.pdf",
        )
        if not sent:
            raise HTTPException(
                status_code=502,
                detail="De e-mail kon niet worden verstuurd. Controleer de SMTP-instellingen en probeer het opnieuw.",
            )
        invoice.sent_at = utcnow()
        audit(session, user.id, "invoice.send", f"{invoice.number} → {recipient}", client_ip(request))
        session.commit()
        return SendInvoiceOutput(
            sent=True,
            mode="smtp" if mailer.configured else "log",
            to=recipient,
            sent_at=invoice.sent_at,
        )

    @app.patch("/api/invoices/{invoice_id}", response_model=InvoiceOutput)
    def update_invoice_status(
        invoice_id: int,
        payload: InvoiceStatusInput,
        request: Request,
        session: Session = Depends(get_session),
        user: User = Depends(get_current_user),
    ):
        invoice = get_owned_invoice(session, user, invoice_id)
        invoice.status = payload.status
        invoice.paid_at = utcnow() if payload.status == "paid" else None
        audit(
            session,
            user.id,
            "invoice.paid" if payload.status == "paid" else "invoice.reopened",
            invoice.number,
            client_ip(request),
        )
        session.commit()
        return get_owned_invoice(session, user, invoice_id)

    # -- VAT --------------------------------------------------------------

    @app.get("/api/vat", response_model=VatOverviewOutput)
    def vat_overview(
        session: Session = Depends(get_session),
        user: User = Depends(get_current_user),
    ):
        today = date.today()
        year = today.year
        quarter = (today.month - 1) // 3 + 1
        quarters = [build_vat_quarter(session, user, year, quarter)]
        for _ in range(3):
            year, quarter = previous_quarter(year, quarter)
            quarters.append(build_vat_quarter(session, user, year, quarter))
        return VatOverviewOutput(current=quarters[0], quarters=quarters)

    @app.post("/api/vat/approve", response_model=VatQuarterOutput)
    def approve_vat(
        payload: VatApproveInput,
        session: Session = Depends(get_session),
        user: User = Depends(get_current_user),
    ):
        quarter = build_vat_quarter(session, user, payload.year, payload.quarter)
        declaration = session.scalar(
            select(VatDeclaration).where(
                VatDeclaration.owner_id == user.id,
                VatDeclaration.year == payload.year,
                VatDeclaration.quarter == payload.quarter,
            )
        )
        if declaration is None:
            declaration = VatDeclaration(
                owner_id=user.id,
                year=payload.year,
                quarter=payload.quarter,
                net_cents=quarter.net_cents,
                vat_cents=quarter.vat_cents,
            )
            session.add(declaration)
        else:
            declaration.net_cents = quarter.net_cents
            declaration.vat_cents = quarter.vat_cents
            declaration.approved_at = utcnow()
        session.commit()
        return build_vat_quarter(session, user, payload.year, payload.quarter)

    @app.delete("/api/vat/approval", status_code=204)
    def revoke_vat_approval(
        year: int,
        quarter: int,
        session: Session = Depends(get_session),
        user: User = Depends(get_current_user),
    ):
        if quarter < 1 or quarter > 4:
            raise HTTPException(status_code=422, detail="Ongeldig kwartaal")
        declaration = session.scalar(
            select(VatDeclaration).where(
                VatDeclaration.owner_id == user.id,
                VatDeclaration.year == year,
                VatDeclaration.quarter == quarter,
            )
        )
        if declaration is not None:
            session.delete(declaration)
            session.commit()

    # -- Receipts ---------------------------------------------------------

    @app.get("/api/receipts", response_model=list[ReceiptOutput])
    def list_receipts(
        session: Session = Depends(get_session),
        user: User = Depends(get_current_user),
    ):
        return session.scalars(
            select(Receipt)
            .where(Receipt.owner_id == user.id)
            .order_by(Receipt.receipt_date.desc(), Receipt.id.desc())
        ).all()

    @app.post(
        "/api/receipts", response_model=ReceiptOutput, status_code=status.HTTP_201_CREATED
    )
    async def create_receipt(
        request: Request,
        vendor: Annotated[str, Form(min_length=1, max_length=160)],
        receipt_date: Annotated[date, Form()],
        amount: Annotated[Decimal, Form(ge=0, max_digits=12, decimal_places=2)],
        category: Annotated[str, Form(min_length=1, max_length=60)],
        file: Annotated[UploadFile, File()],
        session: Session = Depends(get_session),
        user: User = Depends(get_current_user),
    ):
        enforce(request, "receipt-upload", 60, 3600, str(user.id))
        content_type = file.content_type or ""
        suffix = ALLOWED_RECEIPT_TYPES.get(content_type)
        if suffix is None:
            raise HTTPException(
                status_code=415,
                detail="Alleen JPG, PNG, WEBP of PDF-bestanden zijn toegestaan",
            )
        if receipt_date > date.today():
            raise HTTPException(status_code=422, detail="De bondatum kan niet in de toekomst liggen")
        max_bytes = settings.max_upload_bytes
        data = await file.read(max_bytes + 1)
        if len(data) > max_bytes:
            raise HTTPException(
                status_code=413,
                detail=f"Het bestand is groter dan {max_bytes // (1024 * 1024)} MB",
            )
        if not data:
            raise HTTPException(status_code=422, detail="Het bestand is leeg")

        directory = upload_root / str(user.id)
        directory.mkdir(parents=True, exist_ok=True)
        filename = f"{uuid.uuid4().hex}{suffix}"
        (directory / filename).write_bytes(data)

        receipt = Receipt(
            owner_id=user.id,
            vendor=vendor.strip(),
            receipt_date=receipt_date,
            amount_cents=to_cents(amount),
            category=category.strip(),
            filename=f"{user.id}/{filename}",
            content_type=content_type,
            size_bytes=len(data),
        )
        session.add(receipt)
        try:
            session.commit()
        except Exception:
            session.rollback()
            (directory / filename).unlink(missing_ok=True)
            raise
        session.refresh(receipt)
        return receipt

    @app.get("/api/receipts/{receipt_id}/file")
    def receipt_file(
        receipt_id: int,
        session: Session = Depends(get_session),
        user: User = Depends(get_current_user),
    ):
        receipt = session.get(Receipt, receipt_id)
        if receipt is None or receipt.owner_id != user.id:
            raise HTTPException(status_code=404, detail="Bon niet gevonden")
        file_path = (upload_root / receipt.filename).resolve()
        if upload_root.resolve() not in file_path.parents or not file_path.is_file():
            raise HTTPException(status_code=404, detail="Bestand niet gevonden")
        return FileResponse(
            file_path,
            media_type=receipt.content_type,
            headers={
                "Content-Disposition": f'inline; filename="bon-{receipt.id}{file_path.suffix}"'
            },
        )

    @app.delete("/api/receipts/{receipt_id}", status_code=204)
    def delete_receipt(
        receipt_id: int,
        session: Session = Depends(get_session),
        user: User = Depends(get_current_user),
    ):
        receipt = session.get(Receipt, receipt_id)
        if receipt is None or receipt.owner_id != user.id:
            raise HTTPException(status_code=404, detail="Bon niet gevonden")
        file_path = (upload_root / receipt.filename).resolve()
        session.delete(receipt)
        session.commit()
        if upload_root.resolve() in file_path.parents:
            file_path.unlink(missing_ok=True)

    # -- Static pages -----------------------------------------------------

    @app.get("/", include_in_schema=False)
    def public_homepage():
        return FileResponse(web_root / "index.html")

    @app.get("/sw.js", include_in_schema=False)
    def service_worker():
        path = web_root / "sw.js"
        if not path.is_file():
            raise HTTPException(status_code=404, detail="Bestand niet gevonden")
        return FileResponse(
            path,
            media_type="application/javascript",
            headers={"Cache-Control": "no-cache", "Service-Worker-Allowed": "/"},
        )

    @app.get("/fonts/{filename}", include_in_schema=False)
    def font_asset(filename: str):
        """Zelf gehoste lettertypen.

        Expliciet als font/woff2 geserveerd: Python raadt het type op Windows
        niet betrouwbaar, en met een verkeerd MIME-type weigert de browser het
        bestand (CSP staat alleen font-src 'self' toe). De bestandsnaam bevat
        geen hash, dus een week cache met revalidatie is de veilige middenweg.
        """
        if Path(filename).suffix != ".woff2":
            raise HTTPException(status_code=404, detail="Bestand niet gevonden")
        font_root = (web_root / "fonts").resolve()
        asset = (font_root / filename).resolve()
        if asset.parent != font_root or not asset.is_file():
            raise HTTPException(status_code=404, detail="Bestand niet gevonden")
        return FileResponse(
            asset,
            media_type="font/woff2",
            headers={"Cache-Control": "public, max-age=604800"},
        )

    @app.get("/{filename}", include_in_schema=False)
    def public_asset(filename: str, request: Request):
        if Path(filename).suffix not in STATIC_SUFFIXES:
            raise HTTPException(status_code=404, detail="Bestand niet gevonden")
        asset = (web_root / filename).resolve()
        if asset.parent != web_root or not asset.is_file():
            raise HTTPException(status_code=404, detail="Bestand niet gevonden")
        logged_in = bool(request.session.get("user_id"))
        if filename in PROTECTED_PAGES and not logged_in:
            return RedirectResponse("/login.html", status_code=303)
        if filename in {"login.html", "signup.html"} and logged_in:
            return RedirectResponse("/dashboard.html", status_code=303)
        return FileResponse(asset)

    return app


app = create_app()