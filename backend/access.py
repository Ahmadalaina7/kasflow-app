"""Roles and permissions for a company (administratie).

The account (login) and the company (boekhouding) are separate: one person can
own several companies and can be invited into someone else's with a limited role.
"""

from __future__ import annotations

from typing import Iterable

ROLES = {
    "owner": "Eigenaar",
    "admin": "Beheerder",
    "boekhouder": "Boekhouder",
    "facturatie": "Facturatie",
    "kijker": "Alleen kijken",
}

PERMISSIONS = (
    "dashboard.read",
    "invoices.read",
    "invoices.write",
    "invoices.send",
    "receipts.read",
    "receipts.write",
    "customers.read",
    "customers.write",
    "bank.read",
    "bank.write",
    "vat.read",
    "vat.approve",
    "hours.read",
    "hours.write",
    "year.read",
    "settings.read",
    "settings.write",
    "team.read",
    "team.write",
    "company.delete",
)

_ALL = frozenset(PERMISSIONS)
_READ_BOOKKEEPING = frozenset(
    {
        "dashboard.read",
        "invoices.read",
        "receipts.read",
        "customers.read",
        "bank.read",
        "vat.read",
        "hours.read",
        "year.read",
        "settings.read",
    }
)

ROLE_PERMISSIONS: dict[str, frozenset[str]] = {
    "owner": _ALL,
    "admin": _ALL - {"company.delete"},
    "boekhouder": _READ_BOOKKEEPING
    | {
        "invoices.write",
        "invoices.send",
        "receipts.write",
        "customers.write",
        "bank.write",
        "vat.approve",
        "hours.write",
    },
    "facturatie": frozenset(
        {
            "dashboard.read",
            "invoices.read",
            "invoices.write",
            "invoices.send",
            "customers.read",
            "customers.write",
            "settings.read",
        }
    ),
    "kijker": _READ_BOOKKEEPING,
}


def normalize_role(role: str | None) -> str:
    key = (role or "kijker").strip().lower()
    return key if key in ROLE_PERMISSIONS else "kijker"


def permissions_for(role: str) -> frozenset[str]:
    return ROLE_PERMISSIONS[normalize_role(role)]


def has_permission(role: str, permission: str) -> bool:
    return permission in permissions_for(role)


def role_label(role: str) -> str:
    return ROLES.get(normalize_role(role), ROLES["kijker"])


def write_permission_for(path: str, method: str) -> str | None:
    """Map an API path to the permission that a mutating call needs.

    GETs are checked per-route; this is the extra gate for POST/PATCH/DELETE.
    """
    if method in {"GET", "HEAD", "OPTIONS"}:
        return None
    if path.startswith("/api/companies/") and method == "DELETE":
        return "company.delete"
    if path in {"/api/companies", "/api/companies/select"}:
        return None
    rules: Iterable[tuple[str, str]] = (
        ("/api/team", "team.write"),
        ("/api/invoices", "invoices.write"),
        ("/api/receipts", "receipts.write"),
        ("/api/customers", "customers.write"),
        ("/api/bank", "bank.write"),
        ("/api/vat", "vat.approve"),
        ("/api/hours", "hours.write"),
        ("/api/trips", "hours.write"),
        ("/api/auth/me", "settings.write"),
    )
    for prefix, permission in rules:
        if path.startswith(prefix):
            if prefix == "/api/invoices" and (
                path.endswith("/send") or path.endswith("/remind")
            ):
                return "invoices.send"
            return permission
    return None
