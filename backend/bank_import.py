"""Read bank statements: CAMT.053 (XML) and the CSV exports of Dutch banks.

Every Dutch bank offers CAMT.053, so that is the reliable route. CSV layouts
differ per bank; columns are recognised by their (Dutch or English) header
names, which covers ING, Rabobank, bunq, Knab, Triodos and ABN AMRO's CSV.
"""

from __future__ import annotations

import csv
import hashlib
import io
import re
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from xml.etree import ElementTree


class StatementError(ValueError):
    pass


@dataclass(frozen=True)
class StatementLine:
    booking_date: date
    amount_cents: int
    counterparty: str
    counter_iban: str
    description: str
    reference: str

    def fingerprint(self, occurrence: int) -> str:
        """Stable id so importing the same statement twice adds nothing.

        ``occurrence`` separates genuinely identical lines on the same day.
        """
        parts = [
            self.booking_date.isoformat(),
            str(self.amount_cents),
            self.counter_iban,
            self.description,
            self.reference,
            str(occurrence),
        ]
        return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()


MAX_LINES = 5_000


def parse_statement(data: bytes) -> list[StatementLine]:
    text = decode(data)
    stripped = text.lstrip()
    if stripped.startswith("<"):
        lines = parse_camt(stripped)
    else:
        lines = parse_csv(text)
    if not lines:
        raise StatementError("Er staan geen transacties in dit bestand.")
    if len(lines) > MAX_LINES:
        raise StatementError(f"Importeer maximaal {MAX_LINES} transacties per keer.")
    return lines


def decode(data: bytes) -> str:
    for encoding in ("utf-8-sig", "cp1252"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise StatementError("Dit bestand kan niet worden gelezen.")


def parse_amount(value: str) -> int:
    cleaned = value.strip().replace("\u20ac", "").replace("EUR", "").replace(" ", "")
    if not cleaned:
        raise StatementError("Er ontbreekt een bedrag.")
    if "," in cleaned and "." in cleaned:
        # The last separator is the decimal one: 1.234,56 or 1,234.56
        if cleaned.rfind(",") > cleaned.rfind("."):
            cleaned = cleaned.replace(".", "").replace(",", ".")
        else:
            cleaned = cleaned.replace(",", "")
    elif "," in cleaned:
        cleaned = cleaned.replace(",", ".")
    try:
        amount = Decimal(cleaned)
    except InvalidOperation as error:
        raise StatementError(f"Onbekend bedrag: {value}") from error
    return int((amount * 100).to_integral_value())


DATE_FORMATS = ("%Y%m%d", "%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%d.%m.%Y", "%Y/%m/%d")


def parse_date(value: str) -> date:
    cleaned = value.strip()[:10]
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(cleaned, fmt).date()
        except ValueError:
            continue
    raise StatementError(f"Onbekende datum: {value}")


# ---------------------------------------------------------------------------
# CAMT.053
# ---------------------------------------------------------------------------


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _find(element, *path: str):
    current = element
    for name in path:
        if current is None:
            return None
        current = next((child for child in current if _local(child.tag) == name), None)
    return current


def _text(element, *path: str) -> str:
    found = _find(element, *path)
    return (found.text or "").strip() if found is not None else ""


def parse_camt(text: str) -> list[StatementLine]:
    if "<!DOCTYPE" in text or "<!ENTITY" in text:
        raise StatementError("Dit XML-bestand wordt niet geaccepteerd.")
    try:
        root = ElementTree.fromstring(text)
    except ElementTree.ParseError as error:
        raise StatementError("Dit is geen geldig CAMT.053-bestand.") from error

    lines: list[StatementLine] = []
    for entry in root.iter():
        if _local(entry.tag) != "Ntry":
            continue
        amount_text = _text(entry, "Amt")
        if not amount_text:
            continue
        cents = parse_amount(amount_text)
        if _text(entry, "CdtDbtInd") == "DBIT":
            cents = -cents
        booked = _text(entry, "BookgDt", "Dt") or _text(entry, "BookgDt", "DtTm") or _text(entry, "ValDt", "Dt")
        details = _find(entry, "NtryDtls", "TxDtls")
        party_role = "Dbtr" if cents > 0 else "Cdtr"
        counterparty = ""
        iban = ""
        remittance = ""
        reference = _text(entry, "AcctSvcrRef")
        if details is not None:
            counterparty = _text(details, "RltdPties", party_role, "Nm") or _text(
                details, "RltdPties", party_role, "Pty", "Nm"
            )
            iban = _text(details, "RltdPties", f"{party_role}Acct", "Id", "IBAN")
            remittance = " ".join(
                (node.text or "").strip()
                for node in details.iter()
                if _local(node.tag) == "Ustrd" and node.text
            )
            if not remittance:
                remittance = _text(details, "RmtInf", "Strd", "CdtrRefInf", "Ref")
            reference = reference or _text(details, "Refs", "EndToEndId")
        if not remittance:
            remittance = _text(entry, "AddtlNtryInf")
        lines.append(
            StatementLine(
                booking_date=parse_date(booked),
                amount_cents=cents,
                counterparty=counterparty[:200],
                counter_iban=iban.replace(" ", "")[:42],
                description=remittance[:500],
                reference=reference[:120],
            )
        )
    return lines


# ---------------------------------------------------------------------------
# CSV
# ---------------------------------------------------------------------------

DATE_HEADERS = ("boekingsdatum", "datum", "date", "transactiedatum", "rentedatum", "valutadatum")
AMOUNT_HEADERS = ("bedrag (eur)", "bedrag", "amount (eur)", "amount", "transactiebedrag")
SIGN_HEADERS = ("af bij", "af/bij", "debet/credit", "credit/debit", "debit/credit")
NAME_HEADERS = (
    "naam tegenpartij",
    "naam / omschrijving",
    "naam/omschrijving",
    "naam",
    "tegenpartij",
    "counterparty",
    "name",
    "omschrijving tegenpartij",
)
IBAN_HEADERS = (
    "tegenrekening iban/bban",
    "tegenrekening",
    "rekening tegenpartij",
    "counterparty iban",
    "iban tegenpartij",
    "account",
)
DESCRIPTION_HEADERS = (
    "mededelingen",
    "omschrijving",
    "omschrijving-1",
    "description",
    "betalingskenmerk",
    "omschrijving-2",
    "omschrijving-3",
)
REFERENCE_HEADERS = ("volgnr", "transactiereferentie", "referentie", "reference", "kenmerk")
DEBIT_WORDS = {"af", "debet", "debit", "d"}


def _column(headers: list[str], names: tuple[str, ...]) -> int | None:
    for name in names:
        if name in headers:
            return headers.index(name)
    return None


def parse_csv(text: str) -> list[StatementLine]:
    sample = text[:4096]
    try:
        delimiter = csv.Sniffer().sniff(sample, delimiters=",;\t").delimiter
    except csv.Error:
        delimiter = ";" if sample.count(";") > sample.count(",") else ","
    rows = [
        row
        for row in csv.reader(io.StringIO(text), delimiter=delimiter)
        if any(cell.strip() for cell in row)
    ]
    if len(rows) < 2:
        raise StatementError("Er staan geen transacties in dit bestand.")

    headers = [cell.strip().strip('"').lower() for cell in rows[0]]
    date_col = _column(headers, DATE_HEADERS)
    amount_col = _column(headers, AMOUNT_HEADERS)
    if date_col is None or amount_col is None:
        raise StatementError(
            "Dit CSV-formaat herkennen we niet. Download je afschrift als CAMT.053 of als CSV met kolomnamen."
        )
    sign_col = _column(headers, SIGN_HEADERS)
    name_col = _column(headers, NAME_HEADERS)
    iban_col = _column(headers, IBAN_HEADERS)
    reference_col = _column(headers, REFERENCE_HEADERS)
    description_cols = [index for index, name in enumerate(headers) if name in DESCRIPTION_HEADERS]
    if name_col is not None and name_col in description_cols and len(description_cols) > 1:
        description_cols.remove(name_col)

    def cell(row: list[str], index: int | None) -> str:
        if index is None or index >= len(row):
            return ""
        return row[index].strip()

    lines = []
    for row in rows[1:]:
        cents = parse_amount(cell(row, amount_col))
        sign = cell(row, sign_col).lower()
        if sign in DEBIT_WORDS and cents > 0:
            cents = -cents
        description = " ".join(cell(row, index) for index in description_cols if cell(row, index))
        description = re.sub(r"\s+", " ", description)
        lines.append(
            StatementLine(
                booking_date=parse_date(cell(row, date_col)),
                amount_cents=cents,
                counterparty=cell(row, name_col)[:200],
                counter_iban=cell(row, iban_col).replace(" ", "")[:42],
                description=description[:500],
                reference=cell(row, reference_col)[:120],
            )
        )
    return lines
