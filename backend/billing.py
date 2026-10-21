"""Abonnementen en betalingen via Mollie.

Kasflow rekent per maand af. De eerste betaling gaat met iDEAL; Mollie maakt
daar een SEPA-machtiging (mandaat) van en int de volgende maanden automatisch.
Deze module bevat alleen het catalogus- en providerdeel: de abonnementsstatus
zelf staat in de database (``subscriptions``), zodat de app ook zonder
betaalprovider blijft werken tijdens de proefperiode.

Zonder ``KASFLOW_MOLLIE_API_KEY`` is de provider niet geconfigureerd: de app
start gewoon, maar ``checkout`` geeft een duidelijke fout in plaats van te doen
alsof er betaald kan worden.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any

import httpx

logger = logging.getLogger("kasflow.billing")

MOLLIE_API = "https://api.mollie.com/v2"
TRIAL_DAYS = 14
# Na het einde van een betaalde periode blijven lezen en exporteren mogelijk;
# schrijven stopt pas na deze respijtperiode.
GRACE_DAYS = 7


@dataclass(frozen=True)
class Plan:
    key: str
    label: str
    cents: int
    description: str

    @property
    def amount(self) -> str:
        return f"{self.cents / 100:.2f}"


PLANS: dict[str, Plan] = {
    "starter": Plan(
        key="starter",
        label="Starter",
        cents=1_900,
        description="Voor zzp'ers die net beginnen",
    ),
    "groei": Plan(
        key="groei",
        label="Groei",
        cents=3_900,
        description="Voor ondernemers met personeel of groeiplannen",
    ),
    "onderneming": Plan(
        key="onderneming",
        label="Onderneming",
        cents=7_900,
        description="Voor teams met meerdere entiteiten",
    ),
}

DEFAULT_PLAN = "starter"


class BillingError(RuntimeError):
    """The payment provider refused or is not configured."""


def plan_of(key: str | None) -> Plan:
    return PLANS.get((key or "").strip().lower(), PLANS[DEFAULT_PLAN])


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


@dataclass(frozen=True)
class BillingState:
    """Everything the interface needs to explain the current status."""

    plan: str
    plan_label: str
    status: str
    trial_ends_at: datetime | None
    current_period_end: datetime | None
    can_write: bool
    days_left: int
    provider_ready: bool
    message: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "plan": self.plan,
            "plan_label": self.plan_label,
            "status": self.status,
            "trial_ends_at": self.trial_ends_at.isoformat() if self.trial_ends_at else None,
            "current_period_end": (
                self.current_period_end.isoformat() if self.current_period_end else None
            ),
            "can_write": self.can_write,
            "days_left": self.days_left,
            "provider_ready": self.provider_ready,
            "message": self.message,
        }


def state_for(
    *,
    plan: str | None,
    status: str | None,
    trial_ends_at: datetime | None,
    current_period_end: datetime | None,
    provider_ready: bool,
    now: datetime | None = None,
) -> BillingState:
    """Turn the stored subscription into something the app can act on.

    A trial that has not ended is usable. A paid subscription is usable until
    the end of the period plus a short grace window, so a failed collection
    does not lock someone out of their administration on day one.
    """
    moment = now or utcnow()
    chosen = plan_of(plan)
    trial = as_utc(trial_ends_at)
    period = as_utc(current_period_end)
    current = (status or "trialing").lower()

    def days_until(moment_end: datetime | None) -> int:
        if moment_end is None:
            return 0
        return max(0, (moment_end - moment).days + (1 if (moment_end - moment).seconds else 0))

    if current == "trialing":
        usable = trial is not None and moment < trial
        left = days_until(trial)
        if usable:
            message = f"Proefperiode: nog {left} dag{'en' if left != 1 else ''}."
        else:
            message = "Je proefperiode is afgelopen. Kies een abonnement om verder te boeken."
        return BillingState(
            plan=chosen.key,
            plan_label=chosen.label,
            status="trialing" if usable else "trial_expired",
            trial_ends_at=trial,
            current_period_end=None,
            can_write=usable,
            days_left=left,
            provider_ready=provider_ready,
            message=message,
        )

    if current in {"active", "past_due"}:
        usable = period is not None and moment < period + timedelta(days=GRACE_DAYS)
        left = days_until(period)
        if current == "past_due":
            message = (
                "De laatste incasso is niet gelukt. Werk je betaalgegevens bij om te voorkomen "
                "dat je administratie op slot gaat."
            )
        elif usable:
            message = f"Je abonnement loopt tot {period:%d-%m-%Y}."
        else:
            message = "Je betaling is niet binnengekomen. Kies opnieuw een abonnement."
        return BillingState(
            plan=chosen.key,
            plan_label=chosen.label,
            status=current if usable else "expired",
            trial_ends_at=trial,
            current_period_end=period,
            can_write=usable,
            days_left=left,
            provider_ready=provider_ready,
            message=message,
        )

    # Opgezegd: de rest van de betaalde periode blijft bruikbaar.
    usable = period is not None and moment < period
    return BillingState(
        plan=chosen.key,
        plan_label=chosen.label,
        status="canceled" if usable else "canceled_expired",
        trial_ends_at=trial,
        current_period_end=period,
        can_write=usable,
        days_left=days_until(period),
        provider_ready=provider_ready,
        message=(
            f"Opgezegd. Je kunt tot {period:%d-%m-%Y} nog boeken."
            if usable and period
            else "Je abonnement is opgezegd."
        ),
    )


def add_month(value: date) -> date:
    """The same day next month, without rolling over into the month after."""
    year = value.year + (value.month // 12)
    month = value.month % 12 + 1
    day = min(value.day, [31, 29 if year % 4 == 0 and (year % 100 != 0 or year % 400 == 0) else 28,
                          31, 30, 31, 30, 31, 31, 30, 31, 30, 31][month - 1])
    return date(year, month, day)


class MollieClient:
    """Thin wrapper around the Mollie REST API (test and live keys)."""

    def __init__(self, api_key: str, *, timeout: float = 20.0) -> None:
        self.api_key = (api_key or "").strip()
        self.timeout = timeout

    @property
    def configured(self) -> bool:
        return bool(self.api_key)

    @property
    def test_mode(self) -> bool:
        return self.api_key.startswith("test_")

    def _request(self, method: str, path: str, payload: dict | None = None) -> dict:
        if not self.configured:
            raise BillingError("De betaalprovider is nog niet geconfigureerd.")
        try:
            response = httpx.request(
                method,
                MOLLIE_API + path,
                json=payload,
                timeout=self.timeout,
                headers={"Authorization": f"Bearer {self.api_key}"},
            )
        except httpx.HTTPError as error:  # network problem, never a rejection
            logger.warning("Mollie niet bereikbaar: %s", error)
            raise BillingError("De betaaldienst is tijdelijk niet bereikbaar.") from error
        if response.status_code >= 400:
            detail = ""
            try:
                body = response.json()
                detail = body.get("detail") or body.get("title") or ""
            except ValueError:
                detail = response.text[:200]
            logger.warning("Mollie %s %s gaf %s: %s", method, path, response.status_code, detail)
            raise BillingError(detail or "De betaaldienst weigerde de aanvraag.")
        if response.status_code == 204 or not response.content:
            return {}
        return response.json()

    def create_customer(self, *, name: str, email: str, company_id: int) -> str:
        customer = self._request(
            "POST",
            "/customers",
            {
                "name": name[:255],
                "email": email,
                "locale": "nl_NL",
                "metadata": {"company_id": company_id},
            },
        )
        customer_id = customer.get("id")
        if not customer_id:
            raise BillingError("De betaaldienst gaf geen klant-id terug.")
        return customer_id

    def create_first_payment(
        self,
        *,
        customer_id: str,
        plan: Plan,
        company_id: int,
        redirect_url: str,
        webhook_url: str,
    ) -> dict:
        return self._request(
            "POST",
            "/payments",
            {
                "amount": {"currency": "EUR", "value": plan.amount},
                "customerId": customer_id,
                "sequenceType": "first",
                "method": "ideal",
                "description": f"Kasflow {plan.label} — eerste maand",
                "redirectUrl": redirect_url,
                "webhookUrl": webhook_url,
                "locale": "nl_NL",
                "metadata": {"company_id": company_id, "plan": plan.key},
            },
        )

    def list_mandates(self, customer_id: str) -> list[dict]:
        payload = self._request("GET", f"/customers/{customer_id}/mandates?limit=20")
        return payload.get("_embedded", {}).get("mandates", [])

    def create_subscription(
        self, *, customer_id: str, plan: Plan, start_date: date, webhook_url: str
    ) -> dict:
        return self._request(
            "POST",
            f"/customers/{customer_id}/subscriptions",
            {
                "amount": {"currency": "EUR", "value": plan.amount},
                "interval": "1 month",
                "startDate": start_date.isoformat(),
                "description": f"Kasflow {plan.label}",
                "webhookUrl": webhook_url,
                "metadata": {"plan": plan.key},
            },
        )

    def cancel_subscription(self, customer_id: str, subscription_id: str) -> None:
        self._request("DELETE", f"/customers/{customer_id}/subscriptions/{subscription_id}")

    def get_payment(self, payment_id: str) -> dict:
        return self._request("GET", f"/payments/{payment_id}")


def checkout_link(payment: dict) -> str:
    links = payment.get("_links") or {}
    checkout = links.get("checkout") or {}
    url = checkout.get("href")
    if not url:
        raise BillingError("De betaaldienst gaf geen betaallink terug.")
    return url
