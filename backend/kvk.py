"""Controleer KVK-nummer en bedrijfsnaam tegen het Handelsregister.

Zonder deze check kan iedereen een account openen met een verzonnen nummer.
In productie gaat de aanvraag naar de echte KVK-API (sleutel verplicht). Lokaal
zonder sleutel gebruiken we de testdataset van KVK, zodat het pad hetzelfde is.
"""

from __future__ import annotations

import logging
import os
import re
import unicodedata
from dataclasses import dataclass, field

import httpx

logger = logging.getLogger("kasflow.kvk")

PRODUCTION_BASE = "https://api.kvk.nl"
TEST_BASE = "https://api.kvk.nl/test"
# Publieke testsleutel uit de KVK-documentatie, alleen geldig op de test-API.
TEST_API_KEY = "l7xx1f2691f2520d487b902f4e0b57a0b197"
LEGAL_FORM = re.compile(
    r"\b(b\.?\s*v\.?|n\.?\s*v\.?|v\.?\s*o\.?\s*f\.?|c\.?\s*v\.?|eenmanszaak|"
    r"stichting|cooperatie|coöperatie|maatschap|vof)\b",
    re.IGNORECASE,
)
NON_ALNUM = re.compile(r"[^a-z0-9]+")


class KvkError(Exception):
    status_code = 422
    message = "Dit bedrijf konden we niet controleren bij KVK."


class KvkNotFound(KvkError):
    message = "Dit KVK-nummer staat niet in het Handelsregister."


class KvkInactive(KvkError):
    message = "Dit bedrijf is uitgeschreven bij KVK. Je kunt er geen account mee openen."


class KvkNameMismatch(KvkError):
    def __init__(self, company: "KvkCompany"):
        self.company = company
        self.message = (
            f"De bedrijfsnaam hoort niet bij KVK-nummer {company.kvk_number}. "
            f"Volgens KVK heet dit bedrijf {company.name}."
        )
        super().__init__(self.message)


class KvkUnavailable(KvkError):
    status_code = 503
    message = "Het Handelsregister is tijdelijk niet bereikbaar. Probeer het zo opnieuw."


class KvkNotConfigured(KvkError):
    status_code = 503
    message = (
        "Bedrijfscontrole bij KVK is niet ingesteld. Zet KASFLOW_KVK_API_KEY in de serverconfiguratie."
    )


@dataclass(frozen=True)
class KvkCompany:
    kvk_number: str
    name: str
    names: list[str] = field(default_factory=list)
    city: str = ""
    street: str = ""
    active: bool = True
    match_any_name: bool = False

    def as_public(self) -> dict:
        return {
            "kvk_number": self.kvk_number,
            "name": self.name,
            "city": self.city,
            "street": self.street,
            "active": self.active,
        }


def normalize_name(value: str) -> str:
    folded = unicodedata.normalize("NFKD", value or "")
    folded = folded.encode("ascii", "ignore").decode("ascii").lower()
    folded = LEGAL_FORM.sub(" ", folded)
    return NON_ALNUM.sub("", folded)


def names_match(given: str, official: list[str]) -> bool:
    needle = normalize_name(given)
    if len(needle) < 2:
        return False
    for name in official:
        hay = normalize_name(name)
        if hay and (needle == hay or needle in hay or hay in needle):
            return True
    return False


def _text(value: object) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _address(entry: dict) -> tuple[str, str]:
    adres = entry.get("adres") or {}
    binnenlands = adres.get("binnenlandsAdres") if isinstance(adres, dict) else {}
    if not isinstance(binnenlands, dict):
        binnenlands = adres if isinstance(adres, dict) else {}
    return _text(binnenlands.get("straatnaam")), _text(binnenlands.get("plaats"))


def company_from_results(kvk_number: str, results: list[dict]) -> KvkCompany:
    active_rows = [row for row in results if _text(row.get("actief")).lower() != "nee"]
    if not active_rows:
        raise KvkInactive()
    preferred = next(
        (row for row in active_rows if row.get("type") == "hoofdvestiging"),
        None,
    ) or next(
        (row for row in active_rows if row.get("type") == "rechtspersoon"),
        active_rows[0],
    )
    names: list[str] = []
    for row in active_rows:
        naam = _text(row.get("naam"))
        if naam and naam not in names:
            names.append(naam)
    street, city = _address(preferred)
    if not city:
        for row in active_rows:
            _, place = _address(row)
            if place:
                city = place
                if not street:
                    street, _ = _address(row)
                break
    return KvkCompany(
        kvk_number=kvk_number,
        name=names[0] if names else kvk_number,
        names=names,
        city=city,
        street=street,
        active=True,
    )


class KvkClient:
    def __init__(self, *, api_key: str, base_url: str, timeout: float = 8.0):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    @classmethod
    def from_settings(cls, settings) -> "KvkClient | PermissiveKvk | UnconfiguredKvk":
        if os.getenv("PYTEST_CURRENT_TEST"):
            return PermissiveKvk()
        key = getattr(settings, "kvk_api_key", "") or ""
        override = getattr(settings, "kvk_base_url", "") or ""
        if key:
            return cls(api_key=key, base_url=override or PRODUCTION_BASE)
        if getattr(settings, "production", False):
            return UnconfiguredKvk()
        return cls(api_key=TEST_API_KEY, base_url=override or TEST_BASE)

    def lookup(self, kvk_number: str) -> KvkCompany:
        number = re.sub(r"\D", "", kvk_number or "")
        if len(number) != 8:
            raise KvkNotFound()
        url = f"{self.base_url}/api/v2/zoeken"
        try:
            response = httpx.get(
                url,
                params={"kvkNummer": number, "resultatenPerPagina": 10},
                headers={"apikey": self.api_key, "Accept": "application/json"},
                timeout=self.timeout,
            )
        except httpx.HTTPError as error:
            logger.warning("KVK zoeken mislukt: %s", error)
            raise KvkUnavailable() from error
        if response.status_code in {401, 403}:
            logger.error("KVK weigerde de API-sleutel (%s)", response.status_code)
            raise KvkNotConfigured()
        if response.status_code == 404:
            raise KvkNotFound()
        if response.status_code >= 500:
            raise KvkUnavailable()
        if response.status_code >= 400:
            logger.warning("KVK gaf %s: %s", response.status_code, response.text[:200])
            raise KvkUnavailable()
        payload = response.json()
        results = payload.get("resultaten") or []
        if not results:
            raise KvkNotFound()
        return company_from_results(number, results)

    def require(self, kvk_number: str, company_name: str) -> KvkCompany:
        company = self.lookup(kvk_number)
        if company.match_any_name:
            return company
        if not names_match(company_name, company.names or [company.name]):
            raise KvkNameMismatch(company)
        return company


class UnconfiguredKvk:
    def lookup(self, kvk_number: str) -> KvkCompany:
        raise KvkNotConfigured()

    def require(self, kvk_number: str, company_name: str) -> KvkCompany:
        raise KvkNotConfigured()


class PermissiveKvk:
    """Laat testdata door zonder het live Handelsregister aan te roepen."""

    def lookup(self, kvk_number: str) -> KvkCompany:
        number = re.sub(r"\D", "", kvk_number or "")
        if len(number) != 8:
            raise KvkNotFound()
        return KvkCompany(
            kvk_number=number,
            name="Testbedrijf",
            names=["Testbedrijf"],
            city="Amsterdam",
            match_any_name=True,
        )

    def require(self, kvk_number: str, company_name: str) -> KvkCompany:
        company = self.lookup(kvk_number)
        return KvkCompany(
            kvk_number=company.kvk_number,
            name=company_name or company.name,
            names=[company_name or company.name],
            city=company.city,
            match_any_name=True,
        )


class MemoryKvk:
    def __init__(self, records: dict[str, KvkCompany] | None = None):
        self.records = records or {}

    def lookup(self, kvk_number: str) -> KvkCompany:
        number = re.sub(r"\D", "", kvk_number or "")
        company = self.records.get(number)
        if company is None:
            raise KvkNotFound()
        if not company.active:
            raise KvkInactive()
        return company

    def require(self, kvk_number: str, company_name: str) -> KvkCompany:
        company = self.lookup(kvk_number)
        if not names_match(company_name, company.names or [company.name]):
            raise KvkNameMismatch(company)
        return company
