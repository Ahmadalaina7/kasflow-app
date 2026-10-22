"""Controleer KVK-nummer en bedrijfsnaam tegen het Handelsregister.

Met een officiële KVK-API-sleutel is de check hard: nummer en naam moeten kloppen.
Zonder die sleutel gebruiken we de gratis KVK-check van Bedrijfsdata.nl (geen
eigen api_key nodig). Daarmee vullen we naam en plaats in, ook voor een
eenmanszaak. Alleen als die bron het nummer niet kent, mag de ondernemer de naam
zelf invullen.
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
OPENDATA_URL = "https://opendata.kvk.nl/api/v1/hvds/basisbedrijfsgegevens/kvknummer"
BEDRIJFSDATA_URL = "https://free.bedrijfsdata.nl/v1.1/kvk"
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


class KvkBusy(KvkError):
    status_code = 429
    message = "KVK laat even geen extra controles toe. Wacht een minuut en probeer het opnieuw."


class KvkNotConfigured(KvkError):
    status_code = 503
    message = (
        "Bedrijfscontrole bij KVK is niet ingesteld. Zet KASFLOW_KVK_API_KEY in de serverconfiguratie."
    )


class KvkNameRequired(KvkError):
    message = "Vul de bedrijfsnaam in zoals die bij KVK staat."


@dataclass(frozen=True)
class KvkCompany:
    kvk_number: str
    name: str
    names: list[str] = field(default_factory=list)
    city: str = ""
    street: str = ""
    active: bool = True
    match_any_name: bool = False
    verified: bool = True

    def as_public(self) -> dict:
        return {
            "kvk_number": self.kvk_number,
            "name": self.name,
            "city": self.city,
            "street": self.street,
            "active": self.active,
            "verified": self.verified,
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


def company_from_bedrijfsdata(kvk_number: str, payload: dict) -> KvkCompany:
    rows = payload.get("kvk") if isinstance(payload.get("kvk"), list) else []
    if not rows:
        raise KvkNotFound()
    active_rows = [
        row
        for row in rows
        if isinstance(row, dict) and row.get("active") not in {0, "0", False, "false", "False"}
    ]
    if not active_rows:
        raise KvkInactive()
    preferred = next(
        (row for row in active_rows if _text(row.get("type")).lower() == "hoofdvestiging"),
        active_rows[0],
    )
    names: list[str] = []
    for row in [preferred, *active_rows]:
        naam = _text(row.get("name"))
        if naam and naam not in names:
            names.append(naam)
    return KvkCompany(
        kvk_number=kvk_number,
        name=names[0] if names else kvk_number,
        names=names,
        city=_text(preferred.get("city")),
        active=True,
        verified=True,
    )


def company_from_opendata(kvk_number: str, payload: dict) -> KvkCompany:
    if _text(payload.get("actief")).upper() == "N":
        raise KvkInactive()
    return KvkCompany(
        kvk_number=kvk_number,
        name="",
        names=[],
        match_any_name=True,
        active=True,
        verified=True,
    )


def declared_company(kvk_number: str, company_name: str) -> KvkCompany:
    number = re.sub(r"\D", "", kvk_number or "")
    if len(number) != 8:
        raise KvkNotFound()
    name = (company_name or "").strip()
    if not name:
        raise KvkNameRequired()
    return KvkCompany(
        kvk_number=number,
        name=name,
        names=[name],
        match_any_name=True,
        verified=False,
    )


class KvkClient:
    def __init__(self, *, api_key: str, base_url: str, timeout: float = 8.0, strict: bool = False):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.strict = strict
        self._hits: dict[str, KvkCompany] = {}
        self._misses: set[str] = set()

    @classmethod
    def from_settings(cls, settings) -> "KvkClient | PermissiveKvk":
        if os.getenv("PYTEST_CURRENT_TEST"):
            return PermissiveKvk()
        key = getattr(settings, "kvk_api_key", "") or ""
        override = getattr(settings, "kvk_base_url", "") or ""
        if key:
            return cls(api_key=key, base_url=override or PRODUCTION_BASE, strict=True)
        return cls(api_key=TEST_API_KEY, base_url=override or TEST_BASE, strict=False)

    def lookup(self, kvk_number: str) -> KvkCompany:
        number = re.sub(r"\D", "", kvk_number or "")
        if len(number) != 8:
            raise KvkNotFound()
        if number in self._hits:
            return self._hits[number]
        if number in self._misses:
            raise KvkNotFound()
        last: KvkError | None = None
        if self.strict:
            try:
                company = self._zoeken(self.base_url, number)
                self._hits[number] = company
                return company
            except KvkInactive:
                raise
            except (KvkNotFound, KvkUnavailable, KvkBusy, KvkNotConfigured) as error:
                last = error
        try:
            company = self._lookup_public(number)
        except KvkError:
            if last is not None:
                raise last
            raise
        self._hits[number] = company
        return company

    def _lookup_public(self, number: str) -> KvkCompany:
        last: KvkError | None = None
        sources = [self._bedrijfsdata]
        if not self.strict:
            sources.append(self._zoeken_current)
        sources.append(self._opendata)
        for source in sources:
            try:
                return source(number)
            except KvkInactive:
                raise
            except KvkNotFound as error:
                last = error
            except (KvkUnavailable, KvkBusy, KvkNotConfigured) as error:
                last = error
        self._misses.add(number)
        if isinstance(last, (KvkBusy, KvkUnavailable)):
            raise last
        raise KvkNotFound()

    def _zoeken_current(self, number: str) -> KvkCompany:
        return self._zoeken(self.base_url, number)

    def _headers(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        headers = {
            "Accept": "application/json",
            "User-Agent": "Kasflow/2.0 (boekhoudapp)",
        }
        if extra:
            headers.update(extra)
        return headers

    def _zoeken(self, base_url: str, number: str) -> KvkCompany:
        url = f"{base_url.rstrip('/')}/api/v2/zoeken"
        try:
            response = httpx.get(
                url,
                params={"kvkNummer": number, "resultatenPerPagina": 10},
                headers=self._headers({"apikey": self.api_key}),
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

    def _bedrijfsdata(self, number: str) -> KvkCompany:
        try:
            response = httpx.get(
                BEDRIJFSDATA_URL,
                params={"kvk": number},
                headers=self._headers(),
                timeout=self.timeout,
            )
        except httpx.HTTPError as error:
            logger.warning("Bedrijfsdata lookup mislukt: %s", error)
            raise KvkUnavailable() from error
        if response.status_code == 404:
            raise KvkNotFound()
        if response.status_code == 429:
            raise KvkBusy()
        if response.status_code >= 400:
            logger.warning("Bedrijfsdata gaf %s", response.status_code)
            raise KvkUnavailable()
        return company_from_bedrijfsdata(number, response.json())

    def _opendata(self, number: str) -> KvkCompany:
        url = f"{OPENDATA_URL}/{number}"
        try:
            response = httpx.get(
                url,
                headers=self._headers(),
                timeout=self.timeout,
            )
        except httpx.HTTPError as error:
            logger.warning("KVK open data mislukt: %s", error)
            raise KvkUnavailable() from error
        if response.status_code == 404:
            self._misses.add(number)
            raise KvkNotFound()
        if response.status_code == 429:
            raise KvkBusy()
        if response.status_code >= 400:
            logger.warning("KVK open data gaf %s", response.status_code)
            raise KvkUnavailable()
        return company_from_opendata(number, response.json())

    def require(self, kvk_number: str, company_name: str) -> KvkCompany:
        try:
            company = self.lookup(kvk_number)
        except (KvkNotFound, KvkUnavailable, KvkBusy, KvkNotConfigured):
            if self.strict:
                raise
            return declared_company(kvk_number, company_name)
        if company.match_any_name:
            return KvkCompany(
                kvk_number=company.kvk_number,
                name=company_name or company.name,
                names=list(company.names) + ([company_name] if company_name else []),
                city=company.city,
                street=company.street,
                match_any_name=True,
                verified=company.verified,
            )
        if not names_match(company_name, company.names or [company.name]):
            raise KvkNameMismatch(company)
        return company


class UnconfiguredKvk:
    strict = False

    def lookup(self, kvk_number: str) -> KvkCompany:
        raise KvkNotConfigured()

    def require(self, kvk_number: str, company_name: str) -> KvkCompany:
        return declared_company(kvk_number, company_name)


class PermissiveKvk:
    """Laat testdata door zonder het live Handelsregister aan te roepen."""

    strict = False

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
    strict = True

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
