from __future__ import annotations

import hashlib
import hmac
import io
import json
import logging
import os
import re
import secrets
import threading
import time
import uuid
import zipfile
from contextlib import asynccontextmanager
from dataclasses import replace
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
    update,
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

from access import (
    ROLES,
    has_permission,
    normalize_role,
    permissions_for,
    role_label,
    write_permission_for,
)
from billing import (
    DEFAULT_PLAN,
    GRACE_DAYS,
    PLANS,
    TRIAL_DAYS,
    BillingError,
    MollieClient,
    add_month,
    checkout_link,
    plan_of,
    state_for,
)
from bank_import import StatementError, parse_statement
from config import Settings
from invoice_pdf import InvoiceLineData, InvoicePdfData, Party, build_invoice_pdf
from kvk import KvkClient, KvkError, KvkInactive
from mailer import Mailer
from migrations import run_migrations
from receipt_parse import parse_receipt_file
from tax_rules import (
    CATEGORY_BY_KEY,
    EXPENSE_CATEGORIES,
    LEGACY_CATEGORIES,
    PURCHASE_VAT_ORIGINS,
    REVERSE_CHARGE_RUBRICS,
    depreciation_between,
    estimate_income_tax,
    expense_category,
    kia_amount,
    month_index,
    tax_year,
)

logger = logging.getLogger("kasflow.app")

ALLOWED_RECEIPT_TYPES = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "application/pdf": ".pdf",
}

VAT_RATES = {Decimal("0.21"), Decimal("0.09"), Decimal("0")}

# Mandatory wording on the invoice for every treatment other than standard VAT.
VAT_TREATMENT_NOTICES = {
    "reverse_charge": "Btw verlegd.",
    "intra_eu": "Btw verlegd: intracommunautaire prestatie. De afnemer voldoet de btw.",
    "export": "Uitvoer buiten de EU: 0% btw.",
    "kor": "Vrijgesteld van omzetbelasting op grond van de kleineondernemersregeling (KOR).",
}

VAT_TREATMENT_LABELS = {
    "reverse_charge": "btw verlegd",
    "intra_eu": "een intracommunautaire prestatie",
    "export": "uitvoer buiten de EU",
    "kor": "de kleineondernemersregeling",
}

# Rubrics of the Dutch VAT return (aangifte omzetbelasting).
VAT_RUBRICS = {
    "1a": "Leveringen/diensten belast met hoog tarief (21%)",
    "1b": "Leveringen/diensten belast met laag tarief (9%)",
    "1c": "Leveringen/diensten belast met overige tarieven",
    "1e": "Leveringen/diensten belast met 0% of niet bij u belast (o.a. btw verlegd)",
    "2a": "Leveringen/diensten waarbij de btw naar u is verlegd",
    "3a": "Leveringen naar landen buiten de EU (uitvoer)",
    "3b": "Leveringen naar of diensten in landen binnen de EU",
    "4a": "Leveringen/diensten uit landen buiten de EU",
    "4b": "Leveringen/diensten uit landen binnen de EU",
}
# Rubrics where only the base amount is filled in, without VAT.
VAT_BASE_ONLY_RUBRICS = {"1e", "3a", "3b"}

BANK_STATUSES = {"open", "invoice", "receipt", "private", "ignored"}

DOMESTIC_COUNTRIES = {"nederland", "the netherlands", "netherlands", "nl"}

PROTECTED_PAGES = {
    "dashboard.html",
    "facturen.html",
    "factuur.html",
    "bonnetjes.html",
    "btw.html",
    "bank.html",
    "uren.html",
    "jaaroverzicht.html",
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
    vat_scheme: Mapped[str] = mapped_column(String(20), default="standard", nullable=False)
    ib_starter: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    ib_hours_criterion: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    email_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    verification_hash: Mapped[str | None] = mapped_column(String(64))
    verification_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    terms_accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    session_version: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    notify_email: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    notify_sms: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    notify_backup: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class Company(Base):
    __tablename__ = "companies"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
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
    vat_scheme: Mapped[str] = mapped_column(String(20), default="standard", nullable=False)
    ib_starter: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    ib_hours_criterion: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class Subscription(Base):
    """Proefperiode of betaald abonnement van één bedrijf.

    De status is een letterlijke tekst (trialing, active, past_due, canceled) en
    niet een afgeleide van datums, zodat een mislukte incasso zichtbaar blijft
    tot iemand hem oplost.
    """

    __tablename__ = "subscriptions"

    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(
        ForeignKey("companies.id"), nullable=False, unique=True
    )
    plan: Mapped[str] = mapped_column(String(20), default=DEFAULT_PLAN, nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="trialing", nullable=False)
    trial_ends_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    current_period_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    mollie_customer_id: Mapped[str | None] = mapped_column(String(50))
    mollie_subscription_id: Mapped[str | None] = mapped_column(String(50))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )


class Membership(Base):
    __tablename__ = "memberships"
    __table_args__ = (UniqueConstraint("user_id", "company_id", name="uq_memberships_user_company"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"), nullable=False, index=True)
    role: Mapped[str] = mapped_column(String(20), default="kijker", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class TeamInvite(Base):
    __tablename__ = "team_invites"

    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"), nullable=False, index=True)
    email: Mapped[str] = mapped_column(String(320), nullable=False)
    role: Mapped[str] = mapped_column(String(20), default="boekhouder", nullable=False)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    invited_by_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class Customer(Base):
    __tablename__ = "customers"

    id: Mapped[int] = mapped_column(primary_key=True)
    owner_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"), nullable=False, index=True)
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
    __table_args__ = (UniqueConstraint("company_id", "number", name="uq_invoices_company_number"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    owner_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"), nullable=False, index=True)
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
    kind: Mapped[str] = mapped_column(String(10), default="invoice", nullable=False)
    credited_invoice_id: Mapped[int | None] = mapped_column(ForeignKey("invoices.id"))
    vat_treatment: Mapped[str] = mapped_column(String(20), default="standard", nullable=False)
    # Party details as they were on the day the invoice was issued; an issued
    # invoice must not change when the profile or customer is edited later.
    supplier_snapshot: Mapped[str | None] = mapped_column(Text)
    customer_snapshot: Mapped[str | None] = mapped_column(Text)
    reminder_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_reminder_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
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
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"), nullable=False, index=True)
    vendor: Mapped[str] = mapped_column(String(160), nullable=False)
    receipt_date: Mapped[date] = mapped_column(Date, nullable=False)
    amount_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    vat_cents: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    vat_origin: Mapped[str] = mapped_column(String(12), default="nl", nullable=False)
    category: Mapped[str] = mapped_column(String(60), nullable=False)
    # Only for investments: depreciated over this many years.
    life_years: Mapped[int | None] = mapped_column(Integer)
    filename: Mapped[str] = mapped_column(String(200), nullable=False)
    content_type: Mapped[str] = mapped_column(String(100), nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class BankTransaction(Base):
    __tablename__ = "bank_transactions"
    __table_args__ = (UniqueConstraint("company_id", "fingerprint", name="uq_bank_company_fingerprint"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    owner_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"), nullable=False, index=True)
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    booking_date: Mapped[date] = mapped_column(Date, nullable=False)
    amount_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    counterparty: Mapped[str] = mapped_column(String(200), default="", nullable=False)
    counter_iban: Mapped[str] = mapped_column(String(42), default="", nullable=False)
    description: Mapped[str] = mapped_column(String(500), default="", nullable=False)
    status: Mapped[str] = mapped_column(String(12), default="open", nullable=False)
    invoice_id: Mapped[int | None] = mapped_column(ForeignKey("invoices.id"))
    receipt_id: Mapped[int | None] = mapped_column(ForeignKey("receipts.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class TimeEntry(Base):
    __tablename__ = "time_entries"

    id: Mapped[int] = mapped_column(primary_key=True)
    owner_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"), nullable=False, index=True)
    entry_date: Mapped[date] = mapped_column(Date, nullable=False)
    minutes: Mapped[int] = mapped_column(Integer, nullable=False)
    description: Mapped[str] = mapped_column(String(300), nullable=False)
    customer_id: Mapped[int | None] = mapped_column(ForeignKey("customers.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class Trip(Base):
    __tablename__ = "trips"

    id: Mapped[int] = mapped_column(primary_key=True)
    owner_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"), nullable=False, index=True)
    trip_date: Mapped[date] = mapped_column(Date, nullable=False)
    kilometers: Mapped[Decimal] = mapped_column(Numeric(8, 1), nullable=False)
    origin: Mapped[str] = mapped_column(String(160), nullable=False)
    destination: Mapped[str] = mapped_column(String(160), nullable=False)
    purpose: Mapped[str] = mapped_column(String(300), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class VatDeclaration(Base):
    __tablename__ = "vat_declarations"
    __table_args__ = (UniqueConstraint("company_id", "year", "quarter", name="uq_vat_company_period"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    owner_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"), nullable=False, index=True)
    year: Mapped[int] = mapped_column(Integer, nullable=False)
    quarter: Mapped[int] = mapped_column(Integer, nullable=False)
    net_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    vat_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    input_vat_cents: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
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


def digits_kvk(value: str) -> str:
    digits = re.sub(r"\D", "", value or "")
    if len(digits) != 8:
        raise ValueError("Vul een KVK-nummer van 8 cijfers in")
    return digits


class RegisterInput(BaseModel):
    email: Annotated[str, Field(min_length=3, max_length=320)]
    password: Annotated[str, Field(min_length=12, max_length=1024)]
    company_name: Annotated[str, Field(min_length=1, max_length=200)]
    kvk_number: Annotated[str, Field(min_length=8, max_length=16)]
    vat_number: Annotated[str | None, Field(max_length=20)] = None
    terms: bool

    @field_validator("email")
    @classmethod
    def normalize_email(cls, value: str) -> str:
        normalized = value.strip().lower()
        if "@" not in normalized or "." not in normalized.split("@")[-1]:
            raise ValueError("Vul een geldig e-mailadres in")
        return normalized

    @field_validator("password")
    @classmethod
    def strong_password(cls, value: str) -> str:
        if len(value) < 12:
            raise ValueError("Kies een wachtwoord van minimaal 12 tekens")
        return value

    @field_validator("kvk_number")
    @classmethod
    def normalize_kvk(cls, value: str) -> str:
        return digits_kvk(value)

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


class CompanySummary(BaseModel):
    id: int
    name: str
    kvk_number: str
    role: str
    role_label: str


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
    vat_scheme: str
    ib_starter: bool
    ib_hours_criterion: bool
    email_verified: bool
    notify_email: bool
    notify_sms: bool
    notify_backup: bool
    company_id: int = 0
    role: str = "owner"
    role_label: str = "Eigenaar"
    permissions: list[str] = []
    companies: list[CompanySummary] = []

    model_config = ConfigDict(from_attributes=True)


class CompanyCreateInput(BaseModel):
    name: Annotated[str, Field(min_length=1, max_length=200)]
    kvk_number: Annotated[str, Field(min_length=8, max_length=16)]
    vat_number: Annotated[str | None, Field(max_length=20)] = None

    @field_validator("kvk_number")
    @classmethod
    def normalize_company_kvk(cls, value: str) -> str:
        return digits_kvk(value)

    @field_validator("name", "vat_number")
    @classmethod
    def strip_company_text(cls, value: str | None) -> str | None:
        return clean_optional(value)


class CompanySelectInput(BaseModel):
    company_id: int


class CheckoutInput(BaseModel):
    plan: str = DEFAULT_PLAN

    @field_validator("plan")
    @classmethod
    def known_plan(cls, value: str) -> str:
        key = (value or "").strip().lower()
        if key not in PLANS:
            raise ValueError("Kies een bestaand abonnement")
        return key


class BillingPlanOutput(BaseModel):
    key: str
    label: str
    cents: int
    description: str


class BillingOutput(BaseModel):
    plan: str
    plan_label: str
    status: str
    trial_ends_at: datetime | None
    current_period_end: datetime | None
    can_write: bool
    days_left: int
    provider_ready: bool
    message: str
    mode: str = "off"
    plans: list[BillingPlanOutput] = []


class InviteInput(BaseModel):
    email: Annotated[str, Field(min_length=3, max_length=320)]
    role: Annotated[str, Field(max_length=20)] = "boekhouder"

    @field_validator("email")
    @classmethod
    def normalize_invite_email(cls, value: str) -> str:
        return value.strip().lower()

    @field_validator("role")
    @classmethod
    def known_role(cls, value: str) -> str:
        role = normalize_role(value)
        if role == "owner":
            raise ValueError("Een eigenaar nodig je niet uit; maak een extra bedrijf")
        return role


class InviteAcceptInput(BaseModel):
    token: Annotated[str, Field(min_length=8, max_length=200)]
    password: Annotated[str | None, Field(min_length=12, max_length=1024)] = None


class MembershipRoleInput(BaseModel):
    role: Annotated[str, Field(max_length=20)]

    @field_validator("role")
    @classmethod
    def known_role(cls, value: str) -> str:
        return normalize_role(value)


class RegisterOutput(UserOutput):
    verification_required: bool = False


class ProfileInput(BaseModel):
    company_name: Annotated[str, Field(min_length=1, max_length=200)]
    kvk_number: Annotated[str, Field(min_length=8, max_length=16)]
    vat_number: Annotated[str | None, Field(max_length=20)] = None
    phone: Annotated[str | None, Field(max_length=50)] = None
    street: Annotated[str | None, Field(max_length=200)] = None
    postal_code: Annotated[str | None, Field(max_length=20)] = None
    city: Annotated[str | None, Field(max_length=120)] = None
    country: Annotated[str, Field(max_length=120)] = "Nederland"
    iban: Annotated[str | None, Field(max_length=42)] = None
    payment_term_days: Annotated[int, Field(ge=0, le=365)] = 30
    invoice_prefix: Annotated[str | None, Field(max_length=12)] = None
    vat_scheme: Literal["standard", "kor"] = "standard"
    ib_starter: bool = False
    ib_hours_criterion: bool = False
    notify_email: bool = True
    notify_sms: bool = False
    notify_backup: bool = True

    @field_validator("kvk_number")
    @classmethod
    def normalize_profile_kvk(cls, value: str) -> str:
        return digits_kvk(value)

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
    vat_treatment: Literal["standard", "reverse_charge", "intra_eu", "export"] = "standard"
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


class CreditInput(BaseModel):
    number: Annotated[str | None, Field(max_length=50)] = None
    invoice_date: date | None = None
    reason: Annotated[str | None, Field(max_length=1000)] = None

    @field_validator("number", "reason")
    @classmethod
    def strip_text(cls, value: str | None) -> str | None:
        return clean_optional(value)


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
    kind: str
    credited_invoice_id: int | None
    vat_treatment: str
    reminder_count: int
    last_reminder_at: datetime | None
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


class VatRubricOutput(BaseModel):
    code: str
    label: str
    net_cents: int
    vat_cents: int


class VatFormLine(BaseModel):
    """One line of the VAT return as it is typed in, in whole euros."""

    code: str
    label: str
    base_euros: int | None
    vat_euros: int | None


class IcpLine(BaseModel):
    vat_number: str
    name: str
    country: str
    net_cents: int


class VatCheck(BaseModel):
    ok: bool
    label: str
    href: str | None = None


class VatQuarterOutput(BaseModel):
    year: int
    quarter: int
    label: str
    start_date: date
    end_date: date
    deadline: date
    closed: bool
    invoice_count: int
    receipt_count: int
    net_cents: int
    vat_cents: int
    reverse_charge_vat_cents: int
    input_vat_cents: int
    payable_cents: int
    kor: bool
    rates: list[VatRateOutput]
    rubrics: list[VatRubricOutput]
    form: list[VatFormLine]
    form_total_euros: int
    icp: list[IcpLine]
    checks: list[VatCheck]
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
    vat_cents: int
    vat_origin: str
    category: str
    category_label: str
    deductible_vat_cents: int
    life_years: int | None
    content_type: str
    size_bytes: int
    created_at: datetime


class BankTransactionOutput(BaseModel):
    id: int
    booking_date: date
    amount_cents: int
    counterparty: str
    counter_iban: str
    description: str
    status: str
    invoice_id: int | None
    receipt_id: int | None
    suggestion: dict | None = None

    model_config = ConfigDict(from_attributes=True)


class BankImportOutput(BaseModel):
    imported: int
    duplicates: int
    matched: int


class BankActionInput(BaseModel):
    action: Literal["invoice", "receipt", "private", "ignored", "open"]
    invoice_id: int | None = None
    receipt_id: int | None = None


class TimeEntryInput(BaseModel):
    entry_date: date
    hours: Annotated[Decimal, Field(gt=0, le=24, max_digits=4, decimal_places=2)]
    description: Annotated[str, Field(min_length=1, max_length=300)]
    customer_id: int | None = None

    @field_validator("description")
    @classmethod
    def strip_description(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("Beschrijf kort waar je aan hebt gewerkt")
        return stripped


class TimeEntryOutput(BaseModel):
    id: int
    entry_date: date
    minutes: int
    hours: Decimal
    description: str
    customer_id: int | None


class TripInput(BaseModel):
    trip_date: date
    kilometers: Annotated[Decimal, Field(gt=0, le=5000, max_digits=6, decimal_places=1)]
    origin: Annotated[str, Field(min_length=1, max_length=160)]
    destination: Annotated[str, Field(min_length=1, max_length=160)]
    purpose: Annotated[str, Field(min_length=1, max_length=300)]

    @field_validator("origin", "destination", "purpose")
    @classmethod
    def strip_text(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("Dit veld is verplicht")
        return stripped


class TripOutput(BaseModel):
    id: int
    trip_date: date
    kilometers: Decimal
    origin: str
    destination: str
    purpose: str

    model_config = ConfigDict(from_attributes=True)


class YearLine(BaseModel):
    key: str
    label: str
    cents: int


class YearAsset(BaseModel):
    vendor: str
    receipt_date: date
    cost_cents: int
    life_years: int
    depreciation_cents: int


class YearOverviewOutput(BaseModel):
    year: int
    rules_year: int
    rules_exact: bool
    revenue_cents: int
    costs_cents: int
    depreciation_cents: int
    mileage_cents: int
    kia_cents: int
    profit_cents: int
    hours_minutes: int
    hours_criterion: int
    hours_met: bool
    kilometers: Decimal
    starter: bool
    self_employed_deduction: int
    starter_deduction: int
    mkb_exemption: int
    taxable_profit: int
    income_tax_cents: int
    zvw_cents: int
    total_tax_cents: int
    monthly_reserve_cents: int
    categories: list[YearLine]
    assets: list[YearAsset]
    note: str


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


def apply_ib_share(cents: int, share: Decimal) -> int:
    if share == 1:
        return cents
    return int((Decimal(cents) * share).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def year_category_label(key: str) -> str:
    if key == "afschrijving":
        return "Afschrijving bedrijfsmiddelen"
    if key == "kilometers":
        return "Zakelijke kilometers (forfait)"
    return CATEGORY_BY_KEY.get(key, CATEGORY_BY_KEY["overig"]).label


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


def invoice_file_prefix(invoice) -> str:
    return "creditfactuur" if invoice.kind == "credit" else "factuur"


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
    billing: MollieClient | None = None,
    kvk=None,
) -> FastAPI:
    settings = settings or Settings.from_environment()
    if database_url:
        settings = replace(
            settings,
            database_url=database_url,
            upload_dir=upload_dir or settings.upload_dir,
        )
    if upload_dir is not None:
        settings = replace(settings, upload_dir=upload_dir)
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
    billing_client = billing or MollieClient(settings.mollie_api_key)
    kvk_client = kvk if kvk is not None else KvkClient.from_settings(settings)
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
    app.state.billing = billing_client
    app.state.kvk = kvk_client
    app.state.limiter = limiter
    app.state.web_root = web_root
    app.state.upload_root = upload_root

    def origin_allowed(origin: str, request: Request) -> bool:
        """Allow the public hostname even when nginx talks to gunicorn as localhost."""
        hostname = (urlparse(origin).hostname or "").lower()
        if not hostname:
            return False
        request_host = (request.headers.get("host") or "").split(":")[0].lower()
        allowed = {request_host, (settings.host or "").lower()}
        allowed.update(host.split(":")[0].lower() for host in settings.allowed_hosts)
        allowed.discard("")
        return hostname in allowed

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
            if origin and not origin_allowed(origin, request):
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

    def raise_kvk(error: KvkError) -> None:
        raise HTTPException(status_code=error.status_code, detail=error.message) from error

    def checked_company(kvk_number: str, company_name: str):
        try:
            return kvk_client.require(kvk_number, company_name)
        except KvkError as error:
            raise_kvk(error)

    def enforce(request: Request, scope: str, limit: int, window: int, extra: str = "") -> None:
        key = f"{scope}:{client_ip(request)}:{extra}"
        allowed, retry_after = limiter.hit(key, limit, window)
        if not allowed:
            raise HTTPException(
                status_code=429,
                detail="Te veel pogingen. Probeer het over een paar minuten opnieuw.",
                headers={"Retry-After": str(retry_after)},
            )

    def company_from_user(user: User) -> Company:
        return Company(
            name=user.company_name,
            kvk_number=user.kvk_number,
            vat_number=user.vat_number,
            phone=user.phone,
            street=user.street,
            postal_code=user.postal_code,
            city=user.city,
            country=user.country or "Nederland",
            iban=user.iban,
            payment_term_days=user.payment_term_days,
            invoice_prefix=user.invoice_prefix,
            vat_scheme=user.vat_scheme,
            ib_starter=user.ib_starter,
            ib_hours_criterion=user.ib_hours_criterion,
            created_by_id=user.id,
        )

    def memberships_for(session: Session, user: User) -> list[Membership]:
        return list(
            session.scalars(
                select(Membership)
                .where(Membership.user_id == user.id)
                .order_by(Membership.id)
            ).all()
        )

    def bind_company(user: User, company: Company, role: str) -> None:
        user._tenant_id = company.id
        user._company = company
        user._role = normalize_role(role)

    def tenant_id(user: User) -> int:
        value = getattr(user, "_tenant_id", None)
        if not value:
            raise HTTPException(status_code=409, detail="Kies eerst een bedrijf")
        return int(value)

    def subscription_for(session: Session, company_id: int) -> Subscription:
        sub = session.scalar(
            select(Subscription).where(Subscription.company_id == company_id)
        )
        if sub is None:
            sub = Subscription(
                company_id=company_id,
                plan=DEFAULT_PLAN,
                status="trialing",
                trial_ends_at=utcnow() + timedelta(days=TRIAL_DAYS),
            )
            session.add(sub)
            session.commit()
            session.refresh(sub)
        return sub

    def billing_status(session: Session, company_id: int):
        sub = subscription_for(session, company_id)
        state = state_for(
            plan=sub.plan,
            status=sub.status,
            trial_ends_at=sub.trial_ends_at,
            current_period_end=sub.current_period_end,
            provider_ready=billing_client.configured,
        )
        return state, sub

    def require_perm(user: User, permission: str) -> None:
        role = getattr(user, "_role", "kijker")
        if not has_permission(role, permission):
            raise HTTPException(
                status_code=403,
                detail="Je rol in dit bedrijf mag deze actie niet uitvoeren",
            )

    def activate_company(session: Session, request: Request, user: User, company_id: int | None) -> Company:
        rows = memberships_for(session, user)
        if not rows:
            company = company_from_user(user)
            session.add(company)
            session.flush()
            session.add(Membership(user_id=user.id, company_id=company.id, role="owner"))
            session.commit()
            session.refresh(user)
            rows = memberships_for(session, user)
        if not rows:
            raise HTTPException(
                status_code=409,
                detail="Dit account heeft geen bedrijf. Log uit en weer in, of maak een bedrijf aan onder Instellingen.",
            )
        chosen = None
        if company_id:
            chosen = next((row for row in rows if row.company_id == company_id), None)
        if chosen is None:
            chosen = next((row for row in rows if row.role == "owner"), rows[0])
        company = session.get(Company, chosen.company_id)
        if company is None:
            raise HTTPException(status_code=404, detail="Bedrijf niet gevonden")
        bind_company(user, company, chosen.role)
        request.session["company_id"] = company.id
        return company

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
        activate_company(session, request, user, request.session.get("company_id"))
        _gate_write(request, user, session)
        return user

    def _gate_write(request: Request, user: User, session: Session) -> None:
        if request.method in {"GET", "HEAD", "OPTIONS"}:
            return
        path = request.url.path
        if path.startswith("/api/auth/") and path != "/api/auth/me":
            return
        if path in {"/api/account", "/api/team/accept", "/api/companies", "/api/companies/select"}:
            return
        if path.startswith("/api/receipts") and path.endswith("/parse"):
            return
        permission = write_permission_for(path, request.method)
        if permission:
            require_perm(user, permission)
        if path.startswith("/api/billing") or path == "/api/auth/me":
            # Een abonnement afsluiten en je eigen gegevens bijwerken moeten altijd
            # kunnen, ook als de proefperiode voorbij is.
            return
        # Na de proefperiode blijft lezen en exporteren mogelijk; boeken niet.
        state, _ = billing_status(session, tenant_id(user))
        if not state.can_write:
            raise HTTPException(
                status_code=402,
                detail=(
                    "Je proefperiode is afgelopen. Kies een abonnement onder Instellingen "
                    "om verder te boeken. Je gegevens blijven staan en exporteren kan altijd."
                ),
            )

    def start_session(request: Request, user: User, session: Session | None = None) -> None:
        request.session.clear()
        request.session["user_id"] = user.id
        request.session["ver"] = user.session_version
        if session is not None:
            activate_company(session, request, user, None)

    def get_owned_invoice(session: Session, user: User, invoice_id: int) -> Invoice:
        invoice = session.scalar(
            select(Invoice)
            .options(selectinload(Invoice.lines))
            .where(Invoice.id == invoice_id, Invoice.company_id == tenant_id(user))
        )
        if invoice is None:
            raise HTTPException(status_code=404, detail="Factuur niet gevonden")
        return invoice

    def user_output(user: User, session: Session | None = None) -> dict:
        company = getattr(user, "_company", None)
        role = getattr(user, "_role", "owner")
        companies = []
        if session is not None:
            rows = memberships_for(session, user)
            for row in rows:
                item = session.get(Company, row.company_id)
                if item is None:
                    continue
                companies.append(
                    {
                        "id": item.id,
                        "name": item.name,
                        "kvk_number": item.kvk_number,
                        "role": row.role,
                        "role_label": role_label(row.role),
                    }
                )
        return {
            "id": user.id,
            "email": user.email,
            "company_name": company.name if company else user.company_name,
            "kvk_number": company.kvk_number if company else user.kvk_number,
            "vat_number": company.vat_number if company else user.vat_number,
            "phone": company.phone if company else user.phone,
            "street": company.street if company else user.street,
            "postal_code": company.postal_code if company else user.postal_code,
            "city": company.city if company else user.city,
            "country": (company.country if company else user.country) or "Nederland",
            "iban": company.iban if company else user.iban,
            "payment_term_days": company.payment_term_days if company else user.payment_term_days,
            "invoice_prefix": company.invoice_prefix if company else user.invoice_prefix,
            "vat_scheme": company.vat_scheme if company else user.vat_scheme,
            "ib_starter": company.ib_starter if company else user.ib_starter,
            "ib_hours_criterion": company.ib_hours_criterion if company else user.ib_hours_criterion,
            "email_verified": user.email_verified_at is not None,
            "notify_email": user.notify_email,
            "notify_sms": user.notify_sms,
            "notify_backup": user.notify_backup,
            "company_id": getattr(user, "_tenant_id", 0) or 0,
            "role": role,
            "role_label": role_label(role),
            "permissions": sorted(permissions_for(role)),
            "companies": companies,
        }

    def supplier_details(user: User) -> dict[str, str]:
        company = getattr(user, "_company", None)
        return {
            "name": (company.name if company else user.company_name),
            "street": (company.street if company else user.street) or "",
            "postal_code": (company.postal_code if company else user.postal_code) or "",
            "city": (company.city if company else user.city) or "",
            "country": (company.country if company else user.country) or "Nederland",
            "email": user.email,
            "phone": (company.phone if company else user.phone) or "",
            "vat_number": (company.vat_number if company else user.vat_number) or "",
            "kvk_number": company.kvk_number if company else user.kvk_number,
            "iban": (company.iban if company else user.iban) or "",
        }

    def customer_details(customer: Customer) -> dict[str, str]:
        return {
            "name": customer.name,
            "street": customer.street or "",
            "postal_code": customer.postal_code or "",
            "city": customer.city or "",
            "country": customer.country or "Nederland",
            "contact": customer.contact or "",
            "email": customer.email or "",
            "phone": customer.phone or "",
            "vat_number": customer.vat_number or "",
        }

    def invoice_parties(invoice: Invoice, user: User, customer: Customer) -> tuple[dict, dict]:
        supplier = (
            json.loads(invoice.supplier_snapshot) if invoice.supplier_snapshot else supplier_details(user)
        )
        recipient = (
            json.loads(invoice.customer_snapshot)
            if invoice.customer_snapshot
            else customer_details(customer)
        )
        return supplier, recipient

    def missing_invoice_fields(supplier: dict, customer: dict, treatment: str) -> list[str]:
        missing = []
        if not (supplier["street"] and supplier["postal_code"] and supplier["city"]):
            missing.append("je bedrijfsadres")
        if treatment != "kor" and not supplier["vat_number"]:
            missing.append("je btw-id")
        if not (customer["street"] and customer["postal_code"] and customer["city"]):
            missing.append(f"het adres van {customer['name']}")
        if treatment in {"reverse_charge", "intra_eu"} and not customer["vat_number"]:
            missing.append(f"het btw-nummer van {customer['name']}")
        return missing

    def ensure_quarter_open(session: Session, user: User, day: date) -> None:
        quarter = (day.month - 1) // 3 + 1
        approved = session.scalar(
            select(VatDeclaration.id).where(
                VatDeclaration.company_id == tenant_id(user),
                VatDeclaration.year == day.year,
                VatDeclaration.quarter == quarter,
            )
        )
        if approved is not None:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"Q{quarter} {day.year} is al afgevinkt voor de btw. "
                    "Kies een datum in een open kwartaal of trek de afvinking eerst in."
                ),
            )

    def invoice_pdf_bytes(
        session: Session, user: User, customer: Customer, invoice: Invoice
    ) -> bytes:
        supplier_data, customer_data = invoice_parties(invoice, user, customer)
        supplier = Party(**supplier_data)
        recipient = Party(**customer_data)
        credited = (
            session.get(Invoice, invoice.credited_invoice_id) if invoice.credited_invoice_id else None
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
                missing_fields=missing_invoice_fields(
                    supplier_data, customer_data, invoice.vat_treatment
                ),
                credit=invoice.kind == "credit",
                credited_number=credited.number if credited else "",
                credited_date=credited.invoice_date if credited else None,
                vat_notice=VAT_TREATMENT_NOTICES.get(invoice.vat_treatment, ""),
            )
        )

    def next_invoice_number(session: Session, user: User) -> str:
        year = date.today().year
        company = getattr(user, "_company", None)
        prefix = (company.invoice_prefix if company is not None else user.invoice_prefix) or ""
        base = f"{prefix}{year}-"
        numbers = session.scalars(
            select(Invoice.number).where(
                Invoice.company_id == tenant_id(user), Invoice.number.like(f"{base}%")
            )
        ).all()
        highest = 0
        for number in numbers:
            suffix = number[len(base) :]
            if suffix.isdigit():
                highest = max(highest, int(suffix))
        return f"{base}{highest + 1:03d}"

    def deductible_vat(receipt: Receipt, user: User) -> int:
        """VAT on a purchase that may be deducted as voorbelasting (rubric 5b)."""
        company = getattr(user, "_company", None)
        scheme = company.vat_scheme if company is not None else user.vat_scheme
        if scheme == "kor" or receipt.vat_origin == "none":
            return 0
        if not expense_category(receipt.category).vat_deductible:
            return 0
        return receipt.vat_cents

    def receipt_cost(receipt: Receipt, user: User) -> int:
        """What the purchase costs the business, excluding VAT it gets back."""
        if receipt.vat_origin in REVERSE_CHARGE_RUBRICS:
            # The amount is without VAT; the VAT declared on it is only a cost
            # when it cannot be deducted again.
            return receipt.amount_cents + receipt.vat_cents - deductible_vat(receipt, user)
        return receipt.amount_cents - deductible_vat(receipt, user)

    def receipt_output(receipt: Receipt, user: User) -> ReceiptOutput:
        category = expense_category(receipt.category)
        return ReceiptOutput(
            id=receipt.id,
            vendor=receipt.vendor,
            receipt_date=receipt.receipt_date,
            amount_cents=receipt.amount_cents,
            vat_cents=receipt.vat_cents,
            vat_origin=receipt.vat_origin,
            category=category.key,
            category_label=category.label,
            deductible_vat_cents=deductible_vat(receipt, user),
            life_years=receipt.life_years,
            content_type=receipt.content_type,
            size_bytes=receipt.size_bytes,
            created_at=receipt.created_at,
        )

    def time_output(entry: TimeEntry) -> TimeEntryOutput:
        hours = (Decimal(entry.minutes) / Decimal(60)).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )
        return TimeEntryOutput(
            id=entry.id,
            entry_date=entry.entry_date,
            minutes=entry.minutes,
            hours=hours,
            description=entry.description,
            customer_id=entry.customer_id,
        )

    def build_year_overview(session: Session, user: User, year: int) -> YearOverviewOutput:
        rules, exact = tax_year(year)
        start, end = date(year, 1, 1), date(year, 12, 31)
        invoices = session.scalars(
            select(Invoice).where(
                Invoice.company_id == tenant_id(user),
                Invoice.invoice_date >= start,
                Invoice.invoice_date <= end,
            )
        ).all()
        receipts = session.scalars(
            select(Receipt).where(
                Receipt.company_id == tenant_id(user),
                Receipt.receipt_date >= start,
                Receipt.receipt_date <= end,
            )
        ).all()
        # Depreciation also runs for assets bought in earlier years.
        assets = session.scalars(
            select(Receipt).where(
                Receipt.company_id == tenant_id(user),
                Receipt.life_years.is_not(None),
                Receipt.receipt_date <= end,
            )
        ).all()
        entries = session.scalars(
            select(TimeEntry).where(
                TimeEntry.company_id == tenant_id(user),
                TimeEntry.entry_date >= start,
                TimeEntry.entry_date <= end,
            )
        ).all()
        trips = session.scalars(
            select(Trip).where(
                Trip.company_id == tenant_id(user),
                Trip.trip_date >= start,
                Trip.trip_date <= end,
            )
        ).all()

        revenue_cents = sum(invoice.subtotal_cents for invoice in invoices)
        category_totals: dict[str, int] = {}
        costs_cents = 0
        for receipt in receipts:
            category = expense_category(receipt.category)
            if category.asset:
                continue
            cost = apply_ib_share(receipt_cost(receipt, user), category.ib_share)
            costs_cents += cost
            category_totals[category.key] = category_totals.get(category.key, 0) + cost

        first_month = month_index(start)
        last_month = month_index(date(year, 12, 1))
        depreciation_cents = 0
        invested = 0
        asset_rows: list[YearAsset] = []
        for receipt in assets:
            category = expense_category(receipt.category)
            if not category.asset or not receipt.life_years:
                continue
            cost = receipt_cost(receipt, user)
            amount = depreciation_between(
                cost, 0, receipt.life_years, receipt.receipt_date, first_month, last_month
            )
            depreciation_cents += amount
            if start <= receipt.receipt_date <= end and cost >= rules.kia_item_minimum:
                invested += cost
            if amount or start <= receipt.receipt_date <= end:
                asset_rows.append(
                    YearAsset(
                        vendor=receipt.vendor,
                        receipt_date=receipt.receipt_date,
                        cost_cents=cost,
                        life_years=receipt.life_years,
                        depreciation_cents=amount,
                    )
                )
        if depreciation_cents:
            category_totals["afschrijving"] = depreciation_cents

        kilometers = sum((trip.kilometers for trip in trips), Decimal("0"))
        mileage_cents = int(
            (kilometers * Decimal(rules.km_rate_cents)).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
        )
        if mileage_cents:
            category_totals["kilometers"] = mileage_cents
        kia_cents = kia_amount(rules, invested)
        profit_cents = revenue_cents - costs_cents - depreciation_cents - mileage_cents - kia_cents
        hours_minutes = sum(entry.minutes for entry in entries)
        hours_met = hours_minutes >= rules.hours_criterion * 60
        tax = estimate_income_tax(
            rules, profit_cents, hours_criterion_met=hours_met, starter=(getattr(user, "_company", None) or user).ib_starter
        )
        note = (
            f"Tarieven {rules.year} (nog niet gepubliceerd voor {year})."
            if not exact
            else (
                "Schatting voor een eenmanszaak zonder ander box 1-inkomen, eigen woning of fiscale partner. "
                "Neem de winst over in je aangifte inkomstenbelasting; Kasflow dient niet zelf in."
            )
        )
        return YearOverviewOutput(
            year=year,
            rules_year=rules.year,
            rules_exact=exact,
            revenue_cents=revenue_cents,
            costs_cents=costs_cents,
            depreciation_cents=depreciation_cents,
            mileage_cents=mileage_cents,
            kia_cents=kia_cents,
            profit_cents=profit_cents,
            hours_minutes=hours_minutes,
            hours_criterion=rules.hours_criterion,
            hours_met=hours_met,
            kilometers=kilometers.quantize(Decimal("0.1")),
            starter=(getattr(user, "_company", None) or user).ib_starter,
            self_employed_deduction=tax.self_employed_deduction,
            starter_deduction=tax.starter_deduction,
            mkb_exemption=tax.mkb_exemption,
            taxable_profit=tax.taxable_profit,
            income_tax_cents=tax.income_tax,
            zvw_cents=tax.zvw,
            total_tax_cents=tax.total,
            monthly_reserve_cents=(tax.total + 11) // 12,
            categories=[
                YearLine(key=key, label=year_category_label(key), cents=cents)
                for key, cents in sorted(category_totals.items(), key=lambda item: -item[1])
            ],
            assets=asset_rows,
            note=note,
        )

    def build_vat_quarter(session: Session, user: User, year: int, quarter: int) -> VatQuarterOutput:
        start, end = quarter_bounds(year, quarter)
        invoices = session.scalars(
            select(Invoice)
            .options(selectinload(Invoice.lines))
            .where(
                Invoice.company_id == tenant_id(user),
                Invoice.invoice_date >= start,
                Invoice.invoice_date <= end,
            )
        ).all()

        rate_totals: dict[int, dict[str, int]] = {}
        rubric_totals: dict[str, dict[str, int]] = {
            code: {"net": 0, "vat": 0}
            for code in ("1a", "1b", "1e", "2a", "3a", "3b", "4a", "4b")
        }
        net_cents = 0
        vat_cents = 0
        kor_net_cents = 0
        icp: dict[str, IcpLine] = {}
        for invoice in invoices:
            if invoice.vat_treatment == "intra_eu":
                customer = (
                    json.loads(invoice.customer_snapshot)
                    if invoice.customer_snapshot
                    else customer_details(session.get(Customer, invoice.customer_id))
                )
                key = customer["vat_number"].replace(" ", "").upper()
                entry = icp.setdefault(
                    key,
                    IcpLine(vat_number=key, name=customer["name"], country=customer["country"], net_cents=0),
                )
                entry.net_cents += invoice.subtotal_cents
            for line in invoice.lines:
                bucket = rate_totals.setdefault(line.vat_rate_bps, {"net": 0, "vat": 0})
                bucket["net"] += line.net_cents
                bucket["vat"] += line.vat_cents
                net_cents += line.net_cents
                vat_cents += line.vat_cents
                if invoice.vat_treatment == "kor":
                    kor_net_cents += line.net_cents
                    continue
                if invoice.vat_treatment == "export":
                    code = "3a"
                elif invoice.vat_treatment == "intra_eu":
                    code = "3b"
                elif invoice.vat_treatment == "reverse_charge" or line.vat_rate_bps == 0:
                    code = "1e"
                elif line.vat_rate_bps == 2100:
                    code = "1a"
                elif line.vat_rate_bps == 900:
                    code = "1b"
                else:
                    code = "1c"
                rubric = rubric_totals.setdefault(code, {"net": 0, "vat": 0})
                rubric["net"] += line.net_cents
                rubric["vat"] += line.vat_cents

        company = getattr(user, "_company", None)
        kor = (company.vat_scheme if company is not None else user.vat_scheme) == "kor"
        receipts = session.scalars(
            select(Receipt).where(
                Receipt.company_id == tenant_id(user),
                Receipt.receipt_date >= start,
                Receipt.receipt_date <= end,
            )
        ).all()
        reverse_charge_vat_cents = 0
        if not kor:
            for receipt in receipts:
                code = REVERSE_CHARGE_RUBRICS.get(receipt.vat_origin)
                if code is None:
                    continue
                rubric = rubric_totals.setdefault(code, {"net": 0, "vat": 0})
                rubric["net"] += receipt.amount_cents
                rubric["vat"] += receipt.vat_cents
                reverse_charge_vat_cents += receipt.vat_cents
        # Under the KOR no VAT return is filed, so none of this is deducted either.
        input_vat_cents = 0 if kor else sum(deductible_vat(receipt, user) for receipt in receipts)
        rubrics = [
            VatRubricOutput(
                code=code, label=VAT_RUBRICS[code], net_cents=totals["net"], vat_cents=totals["vat"]
            )
            for code, totals in sorted(rubric_totals.items())
        ]

        # The return is filed in whole euros, rounded in the filer's favour:
        # amounts owed down, deductible VAT up.
        form = []
        owed_euros = 0
        for code in ("1a", "1b", "1c", "1e", "2a", "3a", "3b", "4a", "4b"):
            totals = rubric_totals.get(code)
            if totals is None and code == "1c":
                continue
            totals = totals or {"net": 0, "vat": 0}
            base_only = code in VAT_BASE_ONLY_RUBRICS
            line_vat = None if base_only else totals["vat"] // 100
            if line_vat is not None:
                owed_euros += line_vat
            form.append(
                VatFormLine(
                    code=code,
                    label=VAT_RUBRICS[code],
                    base_euros=totals["net"] // 100,
                    vat_euros=line_vat,
                )
            )
        input_euros = -(-input_vat_cents // 100)
        form.append(VatFormLine(code="5a", label="Verschuldigde omzetbelasting", base_euros=None, vat_euros=owed_euros))
        form.append(VatFormLine(code="5b", label="Voorbelasting", base_euros=None, vat_euros=input_euros))
        form.append(
            VatFormLine(code="5c", label="Subtotaal", base_euros=None, vat_euros=owed_euros - input_euros)
        )
        if kor_net_cents:
            rubrics.append(
                VatRubricOutput(
                    code="KOR",
                    label="Omzet onder de kleineondernemersregeling (niet aangeven)",
                    net_cents=kor_net_cents,
                    vat_cents=0,
                )
            )

        declaration = session.scalar(
            select(VatDeclaration).where(
                VatDeclaration.company_id == tenant_id(user),
                VatDeclaration.year == year,
                VatDeclaration.quarter == quarter,
            )
        )
        bank_rows = session.scalars(
            select(BankTransaction).where(
                BankTransaction.company_id == tenant_id(user),
                BankTransaction.booking_date >= start,
                BankTransaction.booking_date <= end,
            )
        ).all()
        open_out = sum(1 for item in bank_rows if item.status == "open" and item.amount_cents < 0)
        open_in = sum(1 for item in bank_rows if item.status == "open" and item.amount_cents > 0)
        checks = [
            VatCheck(
                ok=bool(bank_rows),
                label="Bankafschrift voor dit kwartaal geïmporteerd" if bank_rows else "Nog geen bankafschrift in dit kwartaal",
                href="bank.html",
            ),
            VatCheck(
                ok=open_out == 0,
                label="Alle afschrijvingen zijn gekoppeld" if open_out == 0 else f"{open_out} afschrijvingen nog zonder bon",
                href="bank.html",
            ),
            VatCheck(
                ok=open_in == 0,
                label="Alle bijschrijvingen zijn gekoppeld" if open_in == 0 else f"{open_in} bijschrijvingen nog niet gekoppeld aan een factuur",
                href="bank.html",
            ),
        ]

        return VatQuarterOutput(
            year=year,
            quarter=quarter,
            label=f"Q{quarter} {year}",
            start_date=start,
            end_date=end,
            deadline=vat_deadline(year, quarter),
            closed=end < date.today(),
            invoice_count=len(invoices),
            receipt_count=len(receipts),
            net_cents=net_cents,
            vat_cents=vat_cents,
            reverse_charge_vat_cents=reverse_charge_vat_cents,
            input_vat_cents=input_vat_cents,
            payable_cents=(
                0 if kor else vat_cents + reverse_charge_vat_cents - input_vat_cents
            ),
            kor=kor,
            rubrics=rubrics,
            form=form,
            form_total_euros=owed_euros - input_euros,
            icp=sorted(icp.values(), key=lambda line: line.vat_number),
            checks=checks,
            rates=[
                VatRateOutput(rate_bps=rate, net_cents=totals["net"], vat_cents=totals["vat"])
                for rate, totals in sorted(rate_totals.items(), reverse=True)
            ],
            approved_at=declaration.approved_at if declaration else None,
            approved_on=declaration.approved_at.date() if declaration else None,
        )

    # -- Auth -------------------------------------------------------------

    @app.get("/api/kvk/lookup")
    def lookup_kvk(kvk_number: str, request: Request):
        enforce(request, "kvk-lookup", 20, 3600)
        try:
            company = kvk_client.lookup(kvk_number)
        except KvkInactive as error:
            raise_kvk(error)
        except KvkError as error:
            if getattr(kvk_client, "strict", True):
                raise_kvk(error)
            digits = re.sub(r"\D", "", kvk_number or "")
            return {
                "kvk_number": digits,
                "name": "",
                "city": "",
                "street": "",
                "active": True,
                "verified": False,
            }
        return company.as_public()

    @app.post("/api/auth/register", response_model=RegisterOutput, status_code=201)
    def register(
        payload: RegisterInput, request: Request, session: Session = Depends(get_session)
    ):
        enforce(request, "register", 5, 3600)
        profile = checked_company(payload.kvk_number, payload.company_name)
        verified_at = None if mailer.configured else utcnow()
        user = User(
            email=payload.email,
            password_hash=hash_password(payload.password),
            company_name=profile.name or payload.company_name,
            kvk_number=profile.kvk_number,
            vat_number=payload.vat_number,
            street=profile.street or None,
            city=profile.city or None,
            email_verified_at=verified_at,
            terms_accepted_at=utcnow(),
        )
        session.add(user)
        try:
            session.flush()
            company = company_from_user(user)
            session.add(company)
            session.flush()
            session.add(Membership(user_id=user.id, company_id=company.id, role="owner"))
            session.add(
                Subscription(
                    company_id=company.id,
                    plan=DEFAULT_PLAN,
                    status="trialing",
                    trial_ends_at=utcnow() + timedelta(days=TRIAL_DAYS),
                )
            )
            audit(session, user.id, "account.register", None, client_ip(request))
            session.commit()
        except IntegrityError as error:
            session.rollback()
            raise HTTPException(
                status_code=409, detail="Dit e-mailadres heeft al een account"
            ) from error
        session.refresh(user)

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
            start_session(request, user, session)
        return {**user_output(user, session), "verification_required": verification_required}

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
        start_session(request, user, session)
        audit(session, user.id, "account.login", None, client_ip(request))
        session.commit()
        return user_output(user, session)

    @app.get("/api/auth/me", response_model=UserOutput)
    def current_user(
        session: Session = Depends(get_session), user: User = Depends(get_current_user)
    ):
        return user_output(user, session)

    @app.patch("/api/auth/me", response_model=UserOutput)
    def update_profile(
        payload: ProfileInput,
        request: Request,
        session: Session = Depends(get_session),
        user: User = Depends(get_current_user),
    ):
        data = payload.model_dump()
        company = getattr(user, "_company", None)
        user.notify_email = data["notify_email"]
        user.notify_sms = data["notify_sms"]
        user.notify_backup = data["notify_backup"]
        if company is not None:
            company.name = data["company_name"]
            company.kvk_number = data["kvk_number"]
            company.vat_number = data["vat_number"]
            company.phone = data["phone"]
            company.street = data["street"]
            company.postal_code = data["postal_code"]
            company.city = data["city"]
            company.country = data["country"]
            company.iban = data["iban"]
            company.payment_term_days = data["payment_term_days"]
            company.invoice_prefix = data["invoice_prefix"]
            company.vat_scheme = data["vat_scheme"]
            company.ib_starter = data["ib_starter"]
            company.ib_hours_criterion = data["ib_hours_criterion"]
        user.company_name = data["company_name"]
        user.kvk_number = data["kvk_number"]
        user.vat_number = data["vat_number"]
        user.phone = data["phone"]
        user.street = data["street"]
        user.postal_code = data["postal_code"]
        user.city = data["city"]
        user.country = data["country"]
        user.iban = data["iban"]
        user.payment_term_days = data["payment_term_days"]
        user.invoice_prefix = data["invoice_prefix"]
        user.vat_scheme = data["vat_scheme"]
        user.ib_starter = data["ib_starter"]
        user.ib_hours_criterion = data["ib_hours_criterion"]
        audit(session, user.id, "account.profile", None, client_ip(request))
        session.commit()
        session.refresh(user)
        if company is not None:
            session.refresh(company)
            bind_company(user, company, getattr(user, "_role", "owner"))
        return user_output(user, session)

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
        start_session(request, user, session)

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
        start_session(request, user, session)
        return user_output(user, session)

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
        start_session(request, user, session)
        return user_output(user, session)

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
            select(Customer).where(Customer.company_id == tenant_id(user)).order_by(Customer.id)
        ).all()
        invoices = session.scalars(
            select(Invoice)
            .options(selectinload(Invoice.lines))
            .where(Invoice.company_id == tenant_id(user))
            .order_by(Invoice.id)
        ).all()
        receipts = session.scalars(
            select(Receipt).where(Receipt.company_id == tenant_id(user)).order_by(Receipt.id)
        ).all()
        declarations = session.scalars(
            select(VatDeclaration).where(VatDeclaration.company_id == tenant_id(user))
        ).all()
        bank = session.scalars(
            select(BankTransaction)
            .where(BankTransaction.company_id == tenant_id(user))
            .order_by(BankTransaction.booking_date, BankTransaction.id)
        ).all()
        hours = session.scalars(
            select(TimeEntry).where(TimeEntry.company_id == tenant_id(user)).order_by(TimeEntry.id)
        ).all()
        trips = session.scalars(
            select(Trip).where(Trip.company_id == tenant_id(user)).order_by(Trip.id)
        ).all()
        events = session.scalars(
            select(AuditEvent).where(AuditEvent.owner_id == user.id).order_by(AuditEvent.id)
        ).all()

        payload = {
            "exported_at": utcnow().isoformat(),
            "account": user_output(user, session),
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
                    "kind": invoice.kind,
                    "credited_invoice_id": invoice.credited_invoice_id,
                    "vat_treatment": invoice.vat_treatment,
                    "supplier": json.loads(invoice.supplier_snapshot) if invoice.supplier_snapshot else None,
                    "customer": json.loads(invoice.customer_snapshot) if invoice.customer_snapshot else None,
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
                    "vat_cents": receipt.vat_cents,
                    "vat_origin": receipt.vat_origin,
                    "category": receipt.category,
                    "content_type": receipt.content_type,
                    "size_bytes": receipt.size_bytes,
                    "file": f"bonnetjes/{receipt.id}{Path(receipt.filename).suffix}",
                }
                for receipt in receipts
            ],
            "bank_transactions": [
                {
                    "id": item.id,
                    "booking_date": item.booking_date.isoformat(),
                    "amount_cents": item.amount_cents,
                    "counterparty": item.counterparty,
                    "counter_iban": item.counter_iban,
                    "description": item.description,
                    "status": item.status,
                    "invoice_id": item.invoice_id,
                    "receipt_id": item.receipt_id,
                }
                for item in bank
            ],
            "vat_declarations": [
                {
                    "year": declaration.year,
                    "quarter": declaration.quarter,
                    "net_cents": declaration.net_cents,
                    "vat_cents": declaration.vat_cents,
                    "input_vat_cents": declaration.input_vat_cents,
                    "approved_at": declaration.approved_at.isoformat(),
                }
                for declaration in declarations
            ],
            "time_entries": [
                {
                    "id": entry.id,
                    "entry_date": entry.entry_date.isoformat(),
                    "minutes": entry.minutes,
                    "description": entry.description,
                    "customer_id": entry.customer_id,
                }
                for entry in hours
            ],
            "trips": [
                {
                    "id": trip.id,
                    "trip_date": trip.trip_date.isoformat(),
                    "kilometers": str(trip.kilometers),
                    "origin": trip.origin,
                    "destination": trip.destination,
                    "purpose": trip.purpose,
                }
                for trip in trips
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
        owned = session.scalars(
            select(Membership.company_id).where(
                Membership.user_id == user_id, Membership.role == "owner"
            )
        ).all()
        for company_id in owned:
            for receipt in session.scalars(select(Receipt).where(Receipt.company_id == company_id)).all():
                path = (upload_root / receipt.filename).resolve()
                if upload_root.resolve() in path.parents:
                    path.unlink(missing_ok=True)
            directory = upload_root / str(company_id)
            if directory.is_dir():
                for child in directory.iterdir():
                    child.unlink(missing_ok=True)
                directory.rmdir()
            invoice_ids = select(Invoice.id).where(Invoice.company_id == company_id)
            session.execute(delete(BankTransaction).where(BankTransaction.company_id == company_id))
            session.execute(delete(TimeEntry).where(TimeEntry.company_id == company_id))
            session.execute(delete(Trip).where(Trip.company_id == company_id))
            session.execute(
                update(Invoice).where(Invoice.company_id == company_id).values(credited_invoice_id=None)
            )
            session.execute(delete(InvoiceLine).where(InvoiceLine.invoice_id.in_(invoice_ids)))
            session.execute(delete(Invoice).where(Invoice.company_id == company_id))
            session.execute(delete(Receipt).where(Receipt.company_id == company_id))
            session.execute(delete(VatDeclaration).where(VatDeclaration.company_id == company_id))
            session.execute(delete(Customer).where(Customer.company_id == company_id))
            session.execute(delete(TeamInvite).where(TeamInvite.company_id == company_id))
            session.execute(delete(Membership).where(Membership.company_id == company_id))
            session.execute(delete(Subscription).where(Subscription.company_id == company_id))
            company = session.get(Company, company_id)
            if company is not None:
                session.delete(company)
        session.execute(delete(Membership).where(Membership.user_id == user_id))
        session.execute(delete(PasswordReset).where(PasswordReset.user_id == user_id))
        session.execute(delete(AuditEvent).where(AuditEvent.owner_id == user_id))
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
            select(Customer).where(Customer.company_id == tenant_id(user)).order_by(Customer.name)
        ).all()

    @app.post(
        "/api/customers", response_model=CustomerOutput, status_code=status.HTTP_201_CREATED
    )
    def create_customer(
        payload: CustomerInput,
        session: Session = Depends(get_session),
        user: User = Depends(get_current_user),
    ):
        customer = Customer(**payload.model_dump(), owner_id=user.id, company_id=tenant_id(user))
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
        if customer is None or customer.company_id != tenant_id(user):
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
        if customer is None or customer.company_id != tenant_id(user):
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
            .where(Invoice.company_id == tenant_id(user))
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
        if customer is None or customer.company_id != tenant_id(user):
            raise HTTPException(status_code=404, detail="Klant niet gevonden")

        duplicate = session.scalar(
            select(Invoice.id).where(Invoice.company_id == tenant_id(user), Invoice.number == payload.number)
        )
        if duplicate is not None:
            raise HTTPException(status_code=409, detail="Factuurnummer bestaat al")

        company = getattr(user, "_company", None)
        scheme = company.vat_scheme if company is not None else user.vat_scheme
        treatment = "kor" if scheme == "kor" else payload.vat_treatment
        if treatment != "standard" and any(line.vat_rate != 0 for line in payload.lines):
            raise HTTPException(
                status_code=422,
                detail=f"Bij {VAT_TREATMENT_LABELS[treatment]} is het btw-tarief op elke regel 0%.",
            )
        supplier = supplier_details(user)
        recipient = customer_details(customer)
        missing = missing_invoice_fields(supplier, recipient, treatment)
        if missing:
            raise HTTPException(
                status_code=422,
                detail=(
                    "Een factuur moet deze gegevens bevatten: " + ", ".join(missing) + ". "
                    "Vul ze aan bij Instellingen of Klanten en sla de factuur opnieuw op."
                ),
            )
        abroad = recipient["country"].strip().lower() not in DOMESTIC_COUNTRIES
        if treatment in {"intra_eu", "export"} and not abroad:
            raise HTTPException(
                status_code=422,
                detail=f"Bij {VAT_TREATMENT_LABELS[treatment]} moet de klant in het buitenland gevestigd zijn. Vul het land in bij de klant.",
            )
        ensure_quarter_open(session, user, payload.invoice_date)

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
            owner_id=user.id, company_id=tenant_id(user),
            customer_id=payload.customer_id,
            number=payload.number,
            invoice_date=payload.invoice_date,
            due_date=payload.due_date,
            delivery_date=payload.delivery_date,
            note=payload.note,
            subtotal_cents=subtotal_cents,
            vat_cents=vat_cents,
            total_cents=subtotal_cents + vat_cents,
            vat_treatment=treatment,
            supplier_snapshot=json.dumps(supplier, ensure_ascii=False),
            customer_snapshot=json.dumps(recipient, ensure_ascii=False),
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

    @app.post(
        "/api/invoices/{invoice_id}/credit",
        response_model=InvoiceOutput,
        status_code=status.HTTP_201_CREATED,
    )
    def credit_invoice(
        invoice_id: int,
        payload: CreditInput,
        request: Request,
        session: Session = Depends(get_session),
        user: User = Depends(get_current_user),
    ):
        original = get_owned_invoice(session, user, invoice_id)
        if original.kind == "credit":
            raise HTTPException(status_code=409, detail="Een creditfactuur kun je niet crediteren")
        if original.status == "credited":
            raise HTTPException(status_code=409, detail="Deze factuur is al gecrediteerd")
        customer = session.get(Customer, original.customer_id)
        if customer is None:
            raise HTTPException(status_code=404, detail="Klant niet gevonden")

        credit_date = payload.invoice_date or date.today()
        if credit_date < original.invoice_date:
            raise HTTPException(
                status_code=422,
                detail="De creditfactuur kan niet eerder gedateerd zijn dan de oorspronkelijke factuur",
            )
        number = payload.number or next_invoice_number(session, user)
        duplicate = session.scalar(
            select(Invoice.id).where(Invoice.company_id == tenant_id(user), Invoice.number == number)
        )
        if duplicate is not None:
            raise HTTPException(status_code=409, detail="Factuurnummer bestaat al")
        ensure_quarter_open(session, user, credit_date)

        supplier, recipient = invoice_parties(original, user, customer)
        credit = Invoice(
            owner_id=user.id, company_id=tenant_id(user),
            customer_id=original.customer_id,
            number=number,
            invoice_date=credit_date,
            due_date=credit_date,
            delivery_date=original.delivery_date or original.invoice_date,
            note=payload.reason,
            status="credited",
            kind="credit",
            credited_invoice_id=original.id,
            vat_treatment=original.vat_treatment,
            subtotal_cents=-original.subtotal_cents,
            vat_cents=-original.vat_cents,
            total_cents=-original.total_cents,
            supplier_snapshot=json.dumps(supplier, ensure_ascii=False),
            customer_snapshot=json.dumps(recipient, ensure_ascii=False),
            lines=[
                InvoiceLine(
                    description=line.description,
                    quantity=line.quantity,
                    unit_price_cents=-line.unit_price_cents,
                    vat_rate_bps=line.vat_rate_bps,
                    net_cents=-line.net_cents,
                    vat_cents=-line.vat_cents,
                )
                for line in original.lines
            ],
        )
        original.status = "credited"
        session.add(credit)
        try:
            session.commit()
        except IntegrityError as error:
            session.rollback()
            raise HTTPException(status_code=409, detail="Factuurnummer bestaat al") from error
        audit(
            session,
            user.id,
            "invoice.credit",
            f"{credit.number} crediteert {original.number}",
            client_ip(request),
        )
        session.commit()
        return get_owned_invoice(session, user, credit.id)

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
        pdf = invoice_pdf_bytes(session, user, customer, invoice)
        audit(session, user.id, "invoice.pdf", invoice.number, client_ip(request))
        session.commit()
        filename = f"{invoice_file_prefix(invoice)}-{safe_filename(invoice.number)}.pdf"
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
        supplier_data, customer_data = invoice_parties(invoice, user, customer)
        missing = missing_invoice_fields(supplier_data, customer_data, invoice.vat_treatment)
        if missing:
            raise HTTPException(
                status_code=422,
                detail=(
                    "Deze factuur mist verplichte gegevens: " + ", ".join(missing) + ". "
                    "Maak een creditfactuur en een nieuwe factuur met de juiste gegevens."
                ),
            )
        if payload.to:
            customer.email = recipient

        pdf = invoice_pdf_bytes(session, user, customer, invoice)
        sent = mailer.send_invoice(
            sender_name=(getattr(user, "_company", None).name if getattr(user, "_company", None) else user.company_name),
            customer_name=customer_data["name"],
            customer_email=recipient,
            invoice_number=invoice.number,
            total_cents=invoice.total_cents,
            due_date=invoice.due_date.strftime("%d-%m-%Y"),
            note=payload.message or invoice.note or "",
            pdf_bytes=pdf,
            pdf_filename=f"{invoice_file_prefix(invoice)}-{safe_filename(invoice.number)}.pdf",
            credit=invoice.kind == "credit",
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

    @app.post("/api/invoices/{invoice_id}/remind", response_model=SendInvoiceOutput)
    def remind_invoice(
        invoice_id: int,
        request: Request,
        session: Session = Depends(get_session),
        user: User = Depends(get_current_user),
    ):
        enforce(request, "invoice-remind", 20, 3600, str(user.id))
        invoice = get_owned_invoice(session, user, invoice_id)
        if invoice.kind == "credit" or invoice.status in {"paid", "credited"}:
            raise HTTPException(
                status_code=409, detail="Alleen een openstaande factuur krijgt een herinnering"
            )
        customer = session.get(Customer, invoice.customer_id)
        if customer is None:
            raise HTTPException(status_code=404, detail="Klant niet gevonden")
        _, customer_data = invoice_parties(invoice, user, customer)
        recipient = (customer.email or customer_data.get("email") or "").strip()
        if "@" not in recipient:
            raise HTTPException(
                status_code=422,
                detail="Deze klant heeft geen e-mailadres. Zet dat eerst op de klant.",
            )
        pdf = invoice_pdf_bytes(session, user, customer, invoice)
        sent = mailer.send_reminder(
            sender_name=(getattr(user, "_company", None).name if getattr(user, "_company", None) else user.company_name),
            customer_name=customer_data["name"],
            customer_email=recipient,
            invoice_number=invoice.number,
            total_cents=invoice.total_cents,
            due_date=invoice.due_date.strftime("%d-%m-%Y"),
            pdf_bytes=pdf,
            pdf_filename=f"{invoice_file_prefix(invoice)}-{safe_filename(invoice.number)}.pdf",
        )
        if not sent:
            raise HTTPException(
                status_code=502,
                detail="De e-mail kon niet worden verstuurd. Controleer de SMTP-instellingen en probeer het opnieuw.",
            )
        invoice.reminder_count += 1
        invoice.last_reminder_at = utcnow()
        audit(
            session,
            user.id,
            "invoice.remind",
            f"{invoice.number} → {recipient}",
            client_ip(request),
        )
        session.commit()
        return SendInvoiceOutput(
            sent=True,
            mode="smtp" if mailer.configured else "log",
            to=recipient,
            sent_at=invoice.last_reminder_at,
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
        if invoice.status == "credited":
            raise HTTPException(
                status_code=409,
                detail="Een gecrediteerde factuur of creditfactuur heeft geen betaalstatus meer",
            )
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
        if not quarter.closed:
            raise HTTPException(
                status_code=422,
                detail=f"{quarter.label} loopt nog. Je kunt een kwartaal pas afvinken als het voorbij is.",
            )
        declaration = session.scalar(
            select(VatDeclaration).where(
                VatDeclaration.company_id == tenant_id(user),
                VatDeclaration.year == payload.year,
                VatDeclaration.quarter == payload.quarter,
            )
        )
        if declaration is None:
            declaration = VatDeclaration(
                owner_id=user.id, company_id=tenant_id(user),
                year=payload.year,
                quarter=payload.quarter,
                net_cents=quarter.net_cents,
                vat_cents=quarter.vat_cents,
                input_vat_cents=quarter.input_vat_cents,
            )
            session.add(declaration)
        else:
            declaration.net_cents = quarter.net_cents
            declaration.vat_cents = quarter.vat_cents
            declaration.input_vat_cents = quarter.input_vat_cents
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
                VatDeclaration.company_id == tenant_id(user),
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
        receipts = session.scalars(
            select(Receipt)
            .where(Receipt.company_id == tenant_id(user))
            .order_by(Receipt.receipt_date.desc(), Receipt.id.desc())
        ).all()
        return [receipt_output(receipt, user) for receipt in receipts]

    @app.get("/api/meta/bookkeeping")
    def bookkeeping_meta(_: User = Depends(get_current_user)):
        return {
            "categories": [
                {
                    "key": category.key,
                    "label": category.label,
                    "vat_deductible": category.vat_deductible,
                    "ib_share": float(category.ib_share),
                    "asset": category.asset,
                    "hint": category.hint,
                }
                for category in EXPENSE_CATEGORIES
            ],
            "vat_origins": [{"key": key, "label": label} for key, label in PURCHASE_VAT_ORIGINS.items()],
            "roles": [{"key": key, "label": label} for key, label in ROLES.items() if key != "owner"],
        }

    @app.post("/api/receipts/parse")
    async def parse_receipt(
        request: Request,
        file: Annotated[UploadFile, File()],
        user: User = Depends(get_current_user),
    ):
        enforce(request, "receipt-parse", 40, 3600, str(user.id))
        require_perm(user, "receipts.write")
        content_type = file.content_type or ""
        if content_type not in ALLOWED_RECEIPT_TYPES:
            raise HTTPException(
                status_code=415,
                detail="Alleen JPG, PNG, WEBP of PDF-bestanden zijn toegestaan",
            )
        max_bytes = settings.max_upload_bytes
        data = await file.read(max_bytes + 1)
        if len(data) > max_bytes:
            raise HTTPException(
                status_code=413,
                detail=f"Het bestand is groter dan {max_bytes // (1024 * 1024)} MB",
            )
        if not data:
            raise HTTPException(status_code=422, detail="Het bestand is leeg")
        suggestion = parse_receipt_file(data, content_type, file.filename or "")
        return suggestion.as_dict()

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
        vat_amount: Annotated[Decimal, Form(ge=0, max_digits=12, decimal_places=2)] = Decimal("0"),
        vat_origin: Annotated[str, Form()] = "nl",
        life_years: Annotated[int | None, Form(ge=5, le=30)] = None,
        session: Session = Depends(get_session),
        user: User = Depends(get_current_user),
    ):
        enforce(request, "receipt-upload", 60, 3600, str(user.id))
        category_key = LEGACY_CATEGORIES.get(category.strip(), category.strip())
        if category_key not in CATEGORY_BY_KEY:
            raise HTTPException(status_code=422, detail="Kies een categorie uit de lijst")
        if vat_origin not in PURCHASE_VAT_ORIGINS:
            raise HTTPException(status_code=422, detail="Kies hoe de btw op deze bon is berekend")
        if vat_origin == "none" and vat_amount:
            raise HTTPException(status_code=422, detail="Een bon zonder btw heeft geen btw-bedrag")
        if vat_origin in REVERSE_CHARGE_RUBRICS:
            # The amount is without Dutch VAT; you declare 21% yourself unless you fill in another amount.
            if vat_amount == 0:
                vat_amount = (amount * Decimal("0.21")).quantize(
                    Decimal("0.01"), rounding=ROUND_HALF_UP
                )
            if vat_amount > amount * Decimal("0.21") + Decimal("0.01"):
                raise HTTPException(
                    status_code=422,
                    detail="De verlegde btw is hoger dan 21% van het bedrag. Controleer het bedrag.",
                )
        elif vat_amount > amount:
            raise HTTPException(
                status_code=422, detail="De btw kan niet hoger zijn dan het totaalbedrag van de bon"
            )
        category_rules = CATEGORY_BY_KEY[category_key]
        if category_rules.asset:
            if life_years is None:
                raise HTTPException(
                    status_code=422,
                    detail="Vul in over hoeveel jaar je deze investering afschrijft (minimaal 5)",
                )
            net = amount - (vat_amount if vat_origin == "nl" else Decimal("0"))
            if net < Decimal("450"):
                raise HTTPException(
                    status_code=422,
                    detail="Onder € 450 exclusief btw boek je dit als gewone kosten, niet als investering",
                )
        else:
            life_years = None
        ensure_quarter_open(session, user, receipt_date)
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

        directory = upload_root / str(tenant_id(user))
        directory.mkdir(parents=True, exist_ok=True)
        filename = f"{uuid.uuid4().hex}{suffix}"
        (directory / filename).write_bytes(data)

        receipt = Receipt(
            owner_id=user.id, company_id=tenant_id(user),
            vendor=vendor.strip(),
            receipt_date=receipt_date,
            amount_cents=to_cents(amount),
            vat_cents=to_cents(vat_amount),
            vat_origin=vat_origin,
            category=category_key,
            life_years=life_years,
            filename=f"{tenant_id(user)}/{filename}",
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
        return receipt_output(receipt, user)

    @app.get("/api/receipts/{receipt_id}/file")
    def receipt_file(
        receipt_id: int,
        session: Session = Depends(get_session),
        user: User = Depends(get_current_user),
    ):
        receipt = session.get(Receipt, receipt_id)
        if receipt is None or receipt.company_id != tenant_id(user):
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
        if receipt is None or receipt.company_id != tenant_id(user):
            raise HTTPException(status_code=404, detail="Bon niet gevonden")
        ensure_quarter_open(session, user, receipt.receipt_date)
        file_path = (upload_root / receipt.filename).resolve()
        session.execute(
            update(BankTransaction)
            .where(BankTransaction.receipt_id == receipt.id)
            .values(receipt_id=None, status="open")
        )
        session.delete(receipt)
        session.commit()
        if upload_root.resolve() in file_path.parents:
            file_path.unlink(missing_ok=True)

    def bank_suggestion(
        item: BankTransaction,
        invoices: list[Invoice],
        receipts: list[Receipt],
        taken_invoices: set[int],
        taken_receipts: set[int],
    ) -> dict | None:
        if item.status != "open":
            return None
        haystack = f"{item.counterparty} {item.description}".lower()
        if item.amount_cents > 0:
            candidates = [
                invoice
                for invoice in invoices
                if invoice.kind == "invoice"
                and invoice.status != "credited"
                and invoice.total_cents == item.amount_cents
                and invoice.id not in taken_invoices
            ]
            numbered = [invoice for invoice in candidates if invoice.number.lower() in haystack]
            pool = numbered or [invoice for invoice in candidates if invoice.status != "paid"] or candidates
            if len(pool) == 1:
                invoice = pool[0]
                return {"kind": "invoice", "id": invoice.id, "label": invoice.number}
            return None
        amount = -item.amount_cents
        candidates = [
            receipt
            for receipt in receipts
            if receipt.amount_cents == amount and receipt.id not in taken_receipts
        ]
        close = [
            receipt
            for receipt in candidates
            if abs((receipt.receipt_date - item.booking_date).days) <= 14
        ]
        pool = close or candidates
        if len(pool) == 1:
            receipt = pool[0]
            return {"kind": "receipt", "id": receipt.id, "label": receipt.vendor}
        return None

    def get_owned_bank(session: Session, user: User, transaction_id: int) -> BankTransaction:
        item = session.get(BankTransaction, transaction_id)
        if item is None or item.company_id != tenant_id(user):
            raise HTTPException(status_code=404, detail="Transactie niet gevonden")
        return item

    @app.post("/api/bank/import", response_model=BankImportOutput)
    async def import_bank(
        request: Request,
        file: Annotated[UploadFile, File()],
        session: Session = Depends(get_session),
        user: User = Depends(get_current_user),
    ):
        enforce(request, "bank-import", 20, 3600, str(user.id))
        max_bytes = settings.max_upload_bytes
        data = await file.read(max_bytes + 1)
        if len(data) > max_bytes:
            raise HTTPException(
                status_code=413,
                detail=f"Het bestand is groter dan {max_bytes // (1024 * 1024)} MB",
            )
        if not data:
            raise HTTPException(status_code=422, detail="Het bestand is leeg")
        try:
            lines = parse_statement(data)
        except StatementError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error

        invoices = session.scalars(select(Invoice).where(Invoice.company_id == tenant_id(user))).all()
        receipts = session.scalars(select(Receipt).where(Receipt.company_id == tenant_id(user))).all()
        existing = {
            row[0]
            for row in session.execute(
                select(BankTransaction.fingerprint).where(BankTransaction.company_id == tenant_id(user))
            )
        }
        taken_invoices = {invoice.id for invoice in invoices if invoice.status == "paid"}
        taken_receipts = set()
        for linked in session.scalars(
            select(BankTransaction).where(
                BankTransaction.company_id == tenant_id(user),
                BankTransaction.receipt_id.is_not(None),
            )
        ):
            taken_receipts.add(linked.receipt_id)

        imported = 0
        duplicates = 0
        matched = 0
        seen: dict[str, int] = {}
        for line in lines:
            key = "|".join(
                [
                    line.booking_date.isoformat(),
                    str(line.amount_cents),
                    line.counter_iban,
                    line.description,
                    line.reference,
                ]
            )
            seen[key] = seen.get(key, 0) + 1
            fingerprint = line.fingerprint(seen[key])
            if fingerprint in existing:
                duplicates += 1
                continue
            item = BankTransaction(
                owner_id=user.id, company_id=tenant_id(user),
                fingerprint=fingerprint,
                booking_date=line.booking_date,
                amount_cents=line.amount_cents,
                counterparty=line.counterparty,
                counter_iban=line.counter_iban,
                description=line.description,
            )
            haystack = f"{item.counterparty} {item.description}".lower()
            unique_open = [
                invoice
                for invoice in invoices
                if invoice.kind == "invoice"
                and invoice.status not in {"paid", "credited"}
                and invoice.total_cents == item.amount_cents
                and invoice.id not in taken_invoices
            ]
            numbered = [invoice for invoice in unique_open if invoice.number.lower() in haystack]
            match = (numbered[0] if len(numbered) == 1 else None) or (
                unique_open[0] if len(unique_open) == 1 else None
            )
            if match is not None:
                item.status = "invoice"
                item.invoice_id = match.id
                match.status = "paid"
                match.paid_at = utcnow()
                taken_invoices.add(match.id)
                matched += 1
            session.add(item)
            existing.add(fingerprint)
            imported += 1
        audit(session, user.id, "bank.import", f"{imported} nieuw, {duplicates} al bekend", client_ip(request))
        session.commit()
        return BankImportOutput(imported=imported, duplicates=duplicates, matched=matched)

    @app.get("/api/bank/transactions", response_model=list[BankTransactionOutput])
    def list_bank_transactions(
        session: Session = Depends(get_session),
        user: User = Depends(get_current_user),
    ):
        items = session.scalars(
            select(BankTransaction)
            .where(BankTransaction.company_id == tenant_id(user))
            .order_by(BankTransaction.booking_date.desc(), BankTransaction.id.desc())
        ).all()
        invoices = session.scalars(select(Invoice).where(Invoice.company_id == tenant_id(user))).all()
        receipts = session.scalars(select(Receipt).where(Receipt.company_id == tenant_id(user))).all()
        taken_invoices = {item.invoice_id for item in items if item.invoice_id}
        taken_receipts = {item.receipt_id for item in items if item.receipt_id}
        return [
            BankTransactionOutput(
                id=item.id,
                booking_date=item.booking_date,
                amount_cents=item.amount_cents,
                counterparty=item.counterparty,
                counter_iban=item.counter_iban,
                description=item.description,
                status=item.status,
                invoice_id=item.invoice_id,
                receipt_id=item.receipt_id,
                suggestion=bank_suggestion(item, invoices, receipts, taken_invoices, taken_receipts),
            )
            for item in items
        ]

    @app.post("/api/bank/transactions/{transaction_id}", response_model=BankTransactionOutput)
    def classify_bank_transaction(
        transaction_id: int,
        payload: BankActionInput,
        session: Session = Depends(get_session),
        user: User = Depends(get_current_user),
    ):
        item = get_owned_bank(session, user, transaction_id)
        if payload.action == "invoice":
            if payload.invoice_id is None:
                raise HTTPException(status_code=422, detail="Kies de factuur die bij deze betaling hoort")
            invoice = get_owned_invoice(session, user, payload.invoice_id)
            if invoice.kind != "invoice" or invoice.status == "credited":
                raise HTTPException(status_code=409, detail="Deze factuur kan niet aan een betaling worden gekoppeld")
            item.status = "invoice"
            item.invoice_id = invoice.id
            item.receipt_id = None
            invoice.status = "paid"
            invoice.paid_at = utcnow()
        elif payload.action == "receipt":
            if payload.receipt_id is None:
                raise HTTPException(status_code=422, detail="Kies de bon die bij deze afschrijving hoort")
            receipt = session.get(Receipt, payload.receipt_id)
            if receipt is None or receipt.company_id != tenant_id(user):
                raise HTTPException(status_code=404, detail="Bon niet gevonden")
            item.status = "receipt"
            item.receipt_id = receipt.id
            item.invoice_id = None
        elif payload.action in {"private", "ignored", "open"}:
            item.status = payload.action
            item.invoice_id = None
            item.receipt_id = None
        else:
            raise HTTPException(status_code=422, detail="Onbekende actie")
        session.commit()
        return BankTransactionOutput(
            id=item.id,
            booking_date=item.booking_date,
            amount_cents=item.amount_cents,
            counterparty=item.counterparty,
            counter_iban=item.counter_iban,
            description=item.description,
            status=item.status,
            invoice_id=item.invoice_id,
            receipt_id=item.receipt_id,
            suggestion=None,
        )

    def owned_customer_id(session: Session, user: User, customer_id: int | None) -> int | None:
        if customer_id is None:
            return None
        customer = session.get(Customer, customer_id)
        if customer is None or customer.company_id != tenant_id(user):
            raise HTTPException(status_code=404, detail="Klant niet gevonden")
        return customer.id

    @app.get("/api/hours", response_model=list[TimeEntryOutput])
    def list_hours(
        year: int | None = None,
        session: Session = Depends(get_session),
        user: User = Depends(get_current_user),
    ):
        query = select(TimeEntry).where(TimeEntry.company_id == tenant_id(user))
        if year:
            query = query.where(TimeEntry.entry_date >= date(year, 1, 1), TimeEntry.entry_date <= date(year, 12, 31))
        entries = session.scalars(query.order_by(TimeEntry.entry_date.desc(), TimeEntry.id.desc())).all()
        return [time_output(entry) for entry in entries]

    @app.post("/api/hours", response_model=TimeEntryOutput, status_code=201)
    def create_hours(
        payload: TimeEntryInput,
        session: Session = Depends(get_session),
        user: User = Depends(get_current_user),
    ):
        if payload.entry_date > date.today():
            raise HTTPException(status_code=422, detail="Je kunt geen uren in de toekomst bijhouden")
        minutes = int((payload.hours * 60).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
        day_total = sum(
            session.scalars(
                select(TimeEntry.minutes).where(
                    TimeEntry.company_id == tenant_id(user), TimeEntry.entry_date == payload.entry_date
                )
            ).all()
        )
        if day_total + minutes > 24 * 60:
            raise HTTPException(status_code=422, detail="Op één dag kun je maximaal 24 uur registreren")
        entry = TimeEntry(
            owner_id=user.id, company_id=tenant_id(user),
            entry_date=payload.entry_date,
            minutes=minutes,
            description=payload.description,
            customer_id=owned_customer_id(session, user, payload.customer_id),
        )
        session.add(entry)
        session.commit()
        session.refresh(entry)
        return time_output(entry)

    @app.delete("/api/hours/{entry_id}", status_code=204)
    def delete_hours(
        entry_id: int,
        session: Session = Depends(get_session),
        user: User = Depends(get_current_user),
    ):
        entry = session.get(TimeEntry, entry_id)
        if entry is None or entry.company_id != tenant_id(user):
            raise HTTPException(status_code=404, detail="Regel niet gevonden")
        session.delete(entry)
        session.commit()

    @app.get("/api/trips", response_model=list[TripOutput])
    def list_trips(
        year: int | None = None,
        session: Session = Depends(get_session),
        user: User = Depends(get_current_user),
    ):
        query = select(Trip).where(Trip.company_id == tenant_id(user))
        if year:
            query = query.where(Trip.trip_date >= date(year, 1, 1), Trip.trip_date <= date(year, 12, 31))
        return session.scalars(query.order_by(Trip.trip_date.desc(), Trip.id.desc())).all()

    @app.post("/api/trips", response_model=TripOutput, status_code=201)
    def create_trip(
        payload: TripInput,
        session: Session = Depends(get_session),
        user: User = Depends(get_current_user),
    ):
        if payload.trip_date > date.today():
            raise HTTPException(status_code=422, detail="Je kunt geen rit in de toekomst bijhouden")
        trip = Trip(owner_id=user.id, company_id=tenant_id(user), **payload.model_dump())
        session.add(trip)
        session.commit()
        session.refresh(trip)
        return trip

    @app.delete("/api/trips/{trip_id}", status_code=204)
    def delete_trip(
        trip_id: int,
        session: Session = Depends(get_session),
        user: User = Depends(get_current_user),
    ):
        trip = session.get(Trip, trip_id)
        if trip is None or trip.company_id != tenant_id(user):
            raise HTTPException(status_code=404, detail="Rit niet gevonden")
        session.delete(trip)
        session.commit()

    @app.get("/api/year", response_model=YearOverviewOutput)
    def year_overview(
        year: int | None = None,
        session: Session = Depends(get_session),
        user: User = Depends(get_current_user),
    ):
        chosen = year or date.today().year
        if chosen < 2000 or chosen > 2100:
            raise HTTPException(status_code=422, detail="Ongeldig jaar")
        return build_year_overview(session, user, chosen)

    # -- Companies and team ----------------------------------------------

    @app.get("/api/companies")
    def list_companies(
        session: Session = Depends(get_session), user: User = Depends(get_current_user)
    ):
        return user_output(user, session)["companies"]

    @app.post("/api/companies", status_code=201)
    def create_company(
        payload: CompanyCreateInput,
        request: Request,
        session: Session = Depends(get_session),
        user: User = Depends(get_current_user),
    ):
        profile = checked_company(payload.kvk_number, payload.name)
        company = Company(
            name=profile.name or payload.name,
            kvk_number=profile.kvk_number,
            vat_number=payload.vat_number,
            street=profile.street or None,
            city=profile.city or None,
            created_by_id=user.id,
        )
        session.add(company)
        session.flush()
        session.add(Membership(user_id=user.id, company_id=company.id, role="owner"))
        session.add(
            Subscription(
                company_id=company.id,
                plan=DEFAULT_PLAN,
                status="trialing",
                trial_ends_at=utcnow() + timedelta(days=TRIAL_DAYS),
            )
        )
        audit(session, user.id, "company.create", company.name, client_ip(request))
        session.commit()
        activate_company(session, request, user, company.id)
        return user_output(user, session)

    @app.post("/api/companies/select")
    def select_company(
        payload: CompanySelectInput,
        request: Request,
        session: Session = Depends(get_session),
        user: User = Depends(get_current_user),
    ):
        activate_company(session, request, user, payload.company_id)
        if tenant_id(user) != payload.company_id:
            raise HTTPException(status_code=404, detail="Dit bedrijf hoort niet bij jouw account")
        return user_output(user, session)

    @app.get("/api/team")
    def list_team(
        session: Session = Depends(get_session), user: User = Depends(get_current_user)
    ):
        require_perm(user, "team.read")
        company_id = tenant_id(user)
        members = []
        for row in session.scalars(
            select(Membership).where(Membership.company_id == company_id).order_by(Membership.id)
        ).all():
            member = session.get(User, row.user_id)
            if member is None:
                continue
            members.append(
                {
                    "id": row.id,
                    "user_id": member.id,
                    "email": member.email,
                    "role": row.role,
                    "role_label": role_label(row.role),
                    "self": member.id == user.id,
                }
            )
        invites = []
        for invite in session.scalars(
            select(TeamInvite)
            .where(TeamInvite.company_id == company_id, TeamInvite.accepted_at.is_(None))
            .order_by(TeamInvite.id.desc())
        ).all():
            invites.append(
                {
                    "id": invite.id,
                    "email": invite.email,
                    "role": invite.role,
                    "role_label": role_label(invite.role),
                    "expires_at": invite.expires_at,
                }
            )
        return {"members": members, "invites": invites, "roles": [
            {"key": key, "label": label} for key, label in ROLES.items() if key != "owner"
        ]}

    @app.post("/api/team/invites", status_code=201)
    def invite_member(
        payload: InviteInput,
        request: Request,
        session: Session = Depends(get_session),
        user: User = Depends(get_current_user),
    ):
        require_perm(user, "team.write")
        company = getattr(user, "_company")
        if payload.email == user.email:
            raise HTTPException(status_code=422, detail="Je kunt jezelf niet uitnodigen")
        existing_user = session.scalar(select(User).where(User.email == payload.email))
        if existing_user is not None:
            already = session.scalar(
                select(Membership).where(
                    Membership.user_id == existing_user.id,
                    Membership.company_id == company.id,
                )
            )
            if already is not None:
                raise HTTPException(status_code=409, detail="Deze persoon zit al in dit bedrijf")
        token, hashed = new_token()
        invite = TeamInvite(
            company_id=company.id,
            email=payload.email,
            role=payload.role,
            token_hash=hashed,
            invited_by_id=user.id,
            expires_at=utcnow() + timedelta(days=7),
        )
        session.add(invite)
        session.flush()
        mailer.send_team_invite(
            {
                "email": payload.email,
                "company_name": company.name,
                "role_label": role_label(payload.role),
                "inviter": user.email,
                "token": token,
            }
        )
        audit(session, user.id, "team.invite", payload.email, client_ip(request))
        session.commit()
        return {"id": invite.id, "email": invite.email, "role": invite.role}

    @app.get("/api/team/invites/preview")
    def preview_invite(token: str, session: Session = Depends(get_session)):
        invite = session.scalar(select(TeamInvite).where(TeamInvite.token_hash == token_hash(token)))
        if invite is None or invite.accepted_at is not None:
            raise HTTPException(status_code=400, detail="Deze uitnodiging is ongeldig of al gebruikt")
        expires_at = invite.expires_at
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        if expires_at < utcnow():
            raise HTTPException(status_code=400, detail="Deze uitnodiging is verlopen")
        company = session.get(Company, invite.company_id)
        return {
            "email": invite.email,
            "company_name": company.name if company else "",
            "role": invite.role,
            "role_label": role_label(invite.role),
            "has_account": session.scalar(select(User.id).where(User.email == invite.email)) is not None,
        }

    @app.post("/api/team/accept")
    def accept_invite(
        payload: InviteAcceptInput,
        request: Request,
        session: Session = Depends(get_session),
    ):
        invite = session.scalar(
            select(TeamInvite).where(TeamInvite.token_hash == token_hash(payload.token))
        )
        if invite is None or invite.accepted_at is not None:
            raise HTTPException(status_code=400, detail="Deze uitnodiging is ongeldig of al gebruikt")
        expires_at = invite.expires_at
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        if expires_at < utcnow():
            raise HTTPException(status_code=400, detail="Deze uitnodiging is verlopen")
        company = session.get(Company, invite.company_id)
        if company is None:
            raise HTTPException(status_code=400, detail="Dit bedrijf bestaat niet meer")
        user_id = request.session.get("user_id")
        user = session.get(User, user_id) if user_id else None
        if user is None:
            existing = session.scalar(select(User).where(User.email == invite.email))
            if existing is not None:
                raise HTTPException(
                    status_code=401,
                    detail="Log in met het uitgenodigde e-mailadres om de uitnodiging te accepteren",
                )
            if not payload.password:
                raise HTTPException(
                    status_code=422,
                    detail="Kies een wachtwoord van minimaal 12 tekens om je account aan te maken",
                )
            user = User(
                email=invite.email,
                password_hash=hash_password(payload.password),
                company_name=company.name,
                kvk_number=company.kvk_number,
                vat_number=company.vat_number,
                email_verified_at=utcnow(),
                terms_accepted_at=utcnow(),
            )
            session.add(user)
            session.flush()
        elif user.email != invite.email:
            raise HTTPException(
                status_code=403,
                detail="Deze uitnodiging is voor een ander e-mailadres. Log uit en gebruik het juiste account.",
            )
        already = session.scalar(
            select(Membership).where(
                Membership.user_id == user.id, Membership.company_id == company.id
            )
        )
        if already is None:
            session.add(Membership(user_id=user.id, company_id=company.id, role=invite.role))
        invite.accepted_at = utcnow()
        audit(session, user.id, "team.accept", company.name, client_ip(request))
        session.commit()
        start_session(request, user, session)
        request.session["company_id"] = company.id
        activate_company(session, request, user, company.id)
        return user_output(user, session)

    @app.patch("/api/team/{membership_id}")
    def update_member_role(
        membership_id: int,
        payload: MembershipRoleInput,
        request: Request,
        session: Session = Depends(get_session),
        user: User = Depends(get_current_user),
    ):
        require_perm(user, "team.write")
        row = session.get(Membership, membership_id)
        if row is None or row.company_id != tenant_id(user):
            raise HTTPException(status_code=404, detail="Teamlid niet gevonden")
        if row.role == "owner":
            raise HTTPException(status_code=409, detail="De eigenaar van het bedrijf kun je niet wijzigen")
        if payload.role == "owner":
            raise HTTPException(status_code=422, detail="Eigenaarschap kun je niet overdragen via deze weg")
        row.role = payload.role
        audit(session, user.id, "team.role", f"{row.user_id}:{payload.role}", client_ip(request))
        session.commit()
        return {"id": row.id, "role": row.role, "role_label": role_label(row.role)}

    @app.delete("/api/team/{membership_id}", status_code=204)
    def remove_member(
        membership_id: int,
        request: Request,
        session: Session = Depends(get_session),
        user: User = Depends(get_current_user),
    ):
        row = session.get(Membership, membership_id)
        if row is None or row.company_id != tenant_id(user):
            raise HTTPException(status_code=404, detail="Teamlid niet gevonden")
        if row.user_id == user.id:
            owners = session.scalars(
                select(Membership).where(
                    Membership.company_id == row.company_id, Membership.role == "owner"
                )
            ).all()
            if row.role == "owner" and len(owners) <= 1:
                raise HTTPException(
                    status_code=409,
                    detail="Je kunt het bedrijf niet verlaten als enige eigenaar. Verwijder het bedrijf of het account.",
                )
            session.delete(row)
            session.commit()
            return
        require_perm(user, "team.write")
        if row.role == "owner":
            raise HTTPException(status_code=409, detail="De eigenaar kun je niet verwijderen")
        session.delete(row)
        audit(session, user.id, "team.remove", str(row.user_id), client_ip(request))
        session.commit()

    @app.delete("/api/team/invites/{invite_id}", status_code=204)
    def revoke_invite(
        invite_id: int,
        session: Session = Depends(get_session),
        user: User = Depends(get_current_user),
    ):
        require_perm(user, "team.write")
        invite = session.get(TeamInvite, invite_id)
        if invite is None or invite.company_id != tenant_id(user):
            raise HTTPException(status_code=404, detail="Uitnodiging niet gevonden")
        session.delete(invite)
        session.commit()

    # -- Abonnement -------------------------------------------------------

    def billing_output(session: Session, company_id: int) -> BillingOutput:
        state, _ = billing_status(session, company_id)
        mode = "off"
        if billing_client.configured:
            mode = "test" if billing_client.test_mode else "live"
        return BillingOutput(
            **state.as_dict(),
            mode=mode,
            plans=[
                BillingPlanOutput(
                    key=plan.key, label=plan.label, cents=plan.cents, description=plan.description
                )
                for plan in PLANS.values()
            ],
        )

    def apply_payment(session: Session, sub: Subscription, payment: dict) -> None:
        """Update the subscription after a payment was fetched from Mollie.

        Mollie ruft de webhook zonder handtekening aan, dus de status komt altijd
        uit deze opgehaalde betaalresource en nooit uit de melding zelf.
        """
        status_value = (payment.get("status") or "").lower()
        meta = payment.get("metadata") or {}
        plan = plan_of(meta.get("plan") or sub.plan)
        if status_value == "paid":
            if (payment.get("sequenceType") or "").lower() == "first":
                customer_id = payment.get("customerId") or sub.mollie_customer_id
                if not customer_id:
                    raise BillingError("Deze betaling hoort niet bij een klant.")
                mandates = billing_client.list_mandates(customer_id)
                if not any(item.get("status") in {"valid", "pending"} for item in mandates):
                    raise BillingError(
                        "De bankmachtiging is nog niet actief. Probeer het over een minuut opnieuw."
                    )
                previous = sub.mollie_subscription_id
                next_charge = add_month(date.today())
                started = billing_client.create_subscription(
                    customer_id=customer_id,
                    plan=plan,
                    start_date=next_charge,
                    webhook_url=f"{settings.public_url}/api/billing/webhook",
                )
                sub.mollie_customer_id = customer_id
                sub.mollie_subscription_id = started.get("id")
                sub.plan = plan.key
                sub.status = "active"
                sub.current_period_end = datetime.combine(
                    next_charge, datetime.min.time(), tzinfo=timezone.utc
                )
                if previous and previous != sub.mollie_subscription_id:
                    try:
                        billing_client.cancel_subscription(customer_id, previous)
                    except BillingError:
                        logger.warning("Oude abonnement %s kon niet worden opgezegd", previous)
                logger.info(
                    "Abonnement %s actief voor bedrijf %s", plan.key, sub.company_id
                )
            else:
                current_end = sub.current_period_end
                if current_end is not None and current_end.tzinfo is None:
                    current_end = current_end.replace(tzinfo=timezone.utc)
                start = max(date.today(), current_end.date() if current_end else date.today())
                sub.current_period_end = datetime.combine(
                    add_month(start), datetime.min.time(), tzinfo=timezone.utc
                )
                sub.status = "active"
        elif status_value in {"failed", "expired", "canceled"}:
            sub.status = "past_due"
            logger.info("Betaling %s voor bedrijf %s is %s", payment.get("id"), sub.company_id, status_value)

    @app.get("/api/billing", response_model=BillingOutput)
    def billing_overview(
        session: Session = Depends(get_session), user: User = Depends(get_current_user)
    ):
        require_perm(user, "settings.read")
        return billing_output(session, tenant_id(user))

    @app.post("/api/billing/checkout")
    def billing_checkout(
        payload: CheckoutInput,
        request: Request,
        session: Session = Depends(get_session),
        user: User = Depends(get_current_user),
    ):
        require_perm(user, "settings.write")
        if not billing_client.configured:
            raise HTTPException(
                status_code=503,
                detail=(
                    "Betalen is op deze server nog niet ingesteld. Je proefperiode blijft gewoon "
                    "lopen; de beheerder moet de Mollie-sleutel toevoegen."
                ),
            )
        enforce(request, "billing-checkout", 10, 3600, str(user.id))
        company = getattr(user, "_company", None)
        if company is None:
            raise HTTPException(status_code=409, detail="Kies eerst een bedrijf")
        sub = subscription_for(session, company.id)
        plan = plan_of(payload.plan)
        if sub.status == "active" and sub.plan == plan.key and sub.mollie_subscription_id:
            raise HTTPException(status_code=409, detail="Dit abonnement loopt al.")
        customer_id = sub.mollie_customer_id
        try:
            if not customer_id:
                customer_id = billing_client.create_customer(
                    name=company.name, email=user.email, company_id=company.id
                )
                sub.mollie_customer_id = customer_id
                session.commit()
            payment = billing_client.create_first_payment(
                customer_id=customer_id,
                plan=plan,
                company_id=company.id,
                redirect_url=f"{settings.public_url}/instellingen.html?billing=return",
                webhook_url=f"{settings.public_url}/api/billing/webhook",
            )
        except BillingError as error:
            session.rollback()
            raise HTTPException(status_code=502, detail=str(error)) from error
        audit(session, user.id, "billing.checkout", plan.key, client_ip(request))
        session.commit()
        return {"checkout_url": checkout_link(payment), "payment_id": payment.get("id")}

    @app.get("/api/billing/confirm", response_model=BillingOutput)
    def billing_confirm(
        payment_id: str = "",
        session: Session = Depends(get_session),
        user: User = Depends(get_current_user),
    ):
        require_perm(user, "settings.read")
        company_id = tenant_id(user)
        sub = subscription_for(session, company_id)
        if payment_id and billing_client.configured:
            try:
                payment = billing_client.get_payment(payment_id)
                if str((payment.get("metadata") or {}).get("company_id")) == str(company_id):
                    apply_payment(session, sub, payment)
                    session.commit()
            except BillingError as error:
                raise HTTPException(status_code=502, detail=str(error)) from error
        return billing_output(session, company_id)

    @app.post("/api/billing/webhook", include_in_schema=False)
    async def billing_webhook(request: Request, session: Session = Depends(get_session)):
        form = await request.form()
        payment_id = str(form.get("id") or "").strip()
        if not payment_id:
            raise HTTPException(status_code=400, detail="Geen payment-id")
        try:
            payment = billing_client.get_payment(payment_id)
        except BillingError as error:
            logger.warning("Webhook kon betaling %s niet ophalen: %s", payment_id, error)
            return JSONResponse({"detail": "probeer later opnieuw"}, status_code=503)
        meta_company = (payment.get("metadata") or {}).get("company_id")
        sub = None
        if str(meta_company or "").isdigit():
            sub = subscription_for(session, int(str(meta_company)))
        elif payment.get("subscriptionId"):
            sub = session.scalar(
                select(Subscription).where(
                    Subscription.mollie_subscription_id == payment.get("subscriptionId")
                )
            )
        if sub is None:
            return {"ok": True, "ignored": True}
        try:
            apply_payment(session, sub, payment)
        except BillingError as error:
            logger.warning("Webhook %s kon abonnement niet bijwerken: %s", payment_id, error)
            return JSONResponse({"detail": "probeer later opnieuw"}, status_code=503)
        session.commit()
        return {"ok": True}

    @app.post("/api/billing/cancel", status_code=204)
    def billing_cancel(
        request: Request,
        session: Session = Depends(get_session),
        user: User = Depends(get_current_user),
    ):
        require_perm(user, "settings.write")
        sub = subscription_for(session, tenant_id(user))
        if sub.mollie_customer_id and sub.mollie_subscription_id:
            try:
                billing_client.cancel_subscription(
                    sub.mollie_customer_id, sub.mollie_subscription_id
                )
            except BillingError as error:
                raise HTTPException(status_code=502, detail=str(error)) from error
        sub.status = "canceled"
        sub.mollie_subscription_id = None
        audit(session, user.id, "billing.cancel", None, client_ip(request))
        session.commit()

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

    @app.get("/favicon.ico", include_in_schema=False)
    def favicon():
        """Browsers vragen deze naam op, ook als de pagina een SVG-icoon kiest."""
        return FileResponse(
            web_root / "icon.svg",
            media_type="image/svg+xml",
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