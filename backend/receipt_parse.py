"""Fill receipt fields from an uploaded PDF or photo.

Digital PDFs keep selectable text; those are read with pypdf. Photos are read
with RapidOCR when that package is installed, otherwise only EXIF dates are
used. The result is always a suggestion: the user confirms before saving.
"""

from __future__ import annotations

import io
import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

logger = logging.getLogger("kasflow.receipts")

_SKIP_VENDOR = re.compile(
    r"^(bon|kassabon|kassa|receipt|invoice|factuur|btw|vat|datum|date|totaal|"
    r"te betalen|subtotal|sub-totaal|bedankt|welkom|www\.|http|pin|contant|"
    r"terminal|merchant|kvk|iban|nl\d{2}|€|\d)",
    re.IGNORECASE,
)

_DATE = re.compile(
    r"\b(\d{1,2})[-/. ](\d{1,2})[-/. ](\d{2,4})\b"
)
_ISO_DATE = re.compile(r"\b(20\d{2})-(\d{2})-(\d{2})\b")
_MONEY = re.compile(
    r"(?<![\d.,])(\d{1,3}(?:\.\d{3})*,\d{2}|\d+,\d{2}|\d+\.\d{2})(?![\d])"
)
_TOTAL_HINT = re.compile(
    r"(totaal|te betalen|total|amount due|eindbedrag|pin|visa|mastercard|te voldoen)",
    re.IGNORECASE,
)
_VAT_RATE = re.compile(r"\b(21|9|0)\s*%")
_VAT_LABEL = re.compile(r"\b(btw|vat|ob)\b", re.IGNORECASE)

VENDOR_CATEGORIES: tuple[tuple[tuple[str, ...], str], ...] = (
    (("jumbo", "albert heijn", "ah to go", "plus ", "lidl", "aldi", "dirk", "koopavond"), "overig"),
    (("ns ", "ov-chip", "arriva", "keolis", "uber", "ns.nl", "q-park", "parkeren"), "reis"),
    (("shell", "bp ", "esso", "tinq", "tango", "anwb"), "auto"),
    (("kpn", "vodafone", "odido", "ziggo", "t-mobile"), "telefoon"),
    (("microsoft", "adobe", "google", "github", "notion", "slack", "dropbox"), "software"),
    (("coolblue", "bol.com", "amazon", "office centre", "staples", "hema"), "kantoor"),
    (("camping", "hotel", "restaurant", "cafe", "café", "brunch"), "horeca"),
)


@dataclass
class ReceiptSuggestion:
    vendor: str | None = None
    receipt_date: date | None = None
    amount_cents: int | None = None
    vat_cents: int | None = None
    vat_rate: int | None = None
    vat_origin: str = "nl"
    category: str | None = None
    confidence: float = 0.0
    notes: list[str] = field(default_factory=list)
    text_preview: str = ""

    def as_dict(self) -> dict:
        return {
            "vendor": self.vendor,
            "receipt_date": self.receipt_date.isoformat() if self.receipt_date else None,
            "amount": (self.amount_cents / 100) if self.amount_cents is not None else None,
            "vat_amount": (self.vat_cents / 100) if self.vat_cents is not None else None,
            "vat_rate": self.vat_rate,
            "vat_origin": self.vat_origin,
            "category": self.category,
            "confidence": round(self.confidence, 2),
            "notes": self.notes,
            "text_preview": self.text_preview,
        }


def parse_receipt_file(data: bytes, content_type: str, filename: str = "") -> ReceiptSuggestion:
    text, notes = extract_text(data, content_type)
    suggestion = parse_receipt_text(text)
    suggestion.notes = notes + suggestion.notes
    if not suggestion.receipt_date:
        exif_day = _exif_date(data, content_type)
        if exif_day:
            suggestion.receipt_date = exif_day
            suggestion.notes.append("Datum uit de foto-gegevens (EXIF) gehaald.")
            suggestion.confidence = max(suggestion.confidence, 0.35)
    if not suggestion.vendor and filename:
        stem = re.sub(r"[_-]+", " ", filename.rsplit(".", 1)[0]).strip()
        if stem and not _SKIP_VENDOR.match(stem):
            suggestion.vendor = stem[:160]
    return suggestion


def extract_text(data: bytes, content_type: str) -> tuple[str, list[str]]:
    notes: list[str] = []
    if content_type == "application/pdf" or data[:5] == b"%PDF-":
        text = _pdf_text(data)
        if text.strip():
            return text, notes
        notes.append("Deze PDF bevat geen selecteerbare tekst (waarschijnlijk een scan).")
        rendered = _ocr_image_bytes(_pdf_first_page_png(data), "image/png")
        if rendered:
            return rendered, notes
        notes.append("Geen tekst herkend in de PDF. Vul de gegevens zelf in.")
        return "", notes

    text = _ocr_image_bytes(data, content_type)
    if text.strip():
        return text, notes
    notes.append("Geen tekst herkend op de foto. Vul de gegevens zelf in, of upload een PDF met tekst.")
    return "", notes


def parse_receipt_text(text: str) -> ReceiptSuggestion:
    suggestion = ReceiptSuggestion()
    cleaned = _normalize(text)
    suggestion.text_preview = cleaned[:400]
    if not cleaned.strip():
        return suggestion

    lines = [line.strip() for line in cleaned.splitlines() if line.strip()]
    suggestion.vendor = _vendor(lines)
    suggestion.receipt_date = _date(cleaned)
    suggestion.amount_cents, suggestion.vat_cents, suggestion.vat_rate = _amounts(cleaned, lines)
    suggestion.category = _category(suggestion.vendor, cleaned)
    if suggestion.vat_cents == 0 and suggestion.vat_rate == 0:
        suggestion.vat_origin = "none"

    score = 0.0
    if suggestion.vendor:
        score += 0.25
    if suggestion.receipt_date:
        score += 0.25
    if suggestion.amount_cents:
        score += 0.35
    if suggestion.vat_cents is not None:
        score += 0.15
    suggestion.confidence = score
    if suggestion.amount_cents:
        suggestion.notes.append("Controleer bedrag en btw met de bon voordat je opslaat.")
    return suggestion


def _normalize(text: str) -> str:
    text = text.replace("\u00a0", " ").replace("€", " EUR ")
    text = re.sub(r"[ \t]+", " ", text)
    return text


def _pdf_text(data: bytes) -> str:
    try:
        from pypdf import PdfReader
    except ImportError:
        logger.warning("pypdf ontbreekt; PDF-tekst kan niet worden gelezen")
        return ""
    try:
        reader = PdfReader(io.BytesIO(data))
        parts = []
        for page in reader.pages[:4]:
            parts.append(page.extract_text() or "")
        return "\n".join(parts)
    except Exception:
        logger.exception("PDF-tekst lezen mislukt")
        return ""


def _pdf_first_page_png(data: bytes) -> bytes:
    """Best-effort render; returns empty bytes when pypdf cannot rasterize."""
    return b""


def _ocr_image_bytes(data: bytes, content_type: str) -> str:
    if not data:
        return ""
    try:
        from rapidocr_onnxruntime import RapidOCR
    except ImportError:
        return ""
    try:
        engine = _rapid_engine()
        result, _ = engine(data)
        if not result:
            return ""
        lines = []
        for item in result:
            # RapidOCR: [box, text, confidence]
            if isinstance(item, (list, tuple)) and len(item) >= 2:
                lines.append(str(item[1]))
        return "\n".join(lines)
    except Exception:
        logger.exception("OCR op afbeelding mislukt")
        return ""


_ENGINE = None


def _rapid_engine():
    global _ENGINE
    if _ENGINE is None:
        from rapidocr_onnxruntime import RapidOCR

        _ENGINE = RapidOCR()
    return _ENGINE


def _exif_date(data: bytes, content_type: str) -> date | None:
    if not content_type.startswith("image/"):
        return None
    try:
        from PIL import Image, ExifTags
    except ImportError:
        return None
    try:
        image = Image.open(io.BytesIO(data))
        exif = image.getexif()
        if not exif:
            return None
        tags = {ExifTags.TAGS.get(key, key): value for key, value in exif.items()}
        raw = tags.get("DateTimeOriginal") or tags.get("DateTime")
        if not raw:
            return None
        parsed = datetime.strptime(str(raw)[:19], "%Y:%m:%d %H:%M:%S")
        return parsed.date()
    except Exception:
        return None


def _vendor(lines: list[str]) -> str | None:
    for line in lines[:12]:
        compact = re.sub(r"\s+", " ", line).strip(" -:|")
        if len(compact) < 3 or len(compact) > 80:
            continue
        if _SKIP_VENDOR.match(compact):
            continue
        if _MONEY.search(compact) and not re.search(r"[A-Za-z]{3,}", compact):
            continue
        return compact[:160]
    return None


def _date(text: str) -> date | None:
    today = date.today()
    found: list[date] = []
    for match in _ISO_DATE.finditer(text):
        found.append(date(int(match.group(1)), int(match.group(2)), int(match.group(3))))
    for match in _DATE.finditer(text):
        day, month, year = (int(match.group(1)), int(match.group(2)), int(match.group(3)))
        if year < 100:
            year += 2000
        try:
            found.append(date(year, month, day))
        except ValueError:
            try:
                found.append(date(year, day, month))
            except ValueError:
                continue
    usable = [item for item in found if date(2000, 1, 1) <= item <= today]
    return usable[-1] if usable else None


def _parse_money(value: str) -> int | None:
    raw = value.strip()
    if re.match(r"^\d{1,3}(?:\.\d{3})*,\d{2}$", raw) or re.match(r"^\d+,\d{2}$", raw):
        raw = raw.replace(".", "").replace(",", ".")
    elif re.match(r"^\d+\.\d{2}$", raw):
        pass
    else:
        return None
    try:
        cents = int((Decimal(raw) * 100).quantize(Decimal("1")))
    except (InvalidOperation, ValueError):
        return None
    if cents <= 0 or cents > 10_000_000:
        return None
    return cents


def _amounts(text: str, lines: list[str]) -> tuple[int | None, int | None, int | None]:
    totals: list[int] = []
    vat_amounts: list[int] = []
    for line in lines:
        amounts = [_parse_money(match.group(1)) for match in _MONEY.finditer(line)]
        amounts = [item for item in amounts if item is not None]
        if not amounts:
            continue
        if _TOTAL_HINT.search(line):
            totals.extend(amounts)
        if _VAT_LABEL.search(line):
            vat_amounts.extend(amounts)
    if not totals:
        all_amounts = [_parse_money(match.group(1)) for match in _MONEY.finditer(text)]
        totals = [item for item in all_amounts if item is not None]

    amount = max(totals) if totals else None
    rate = None
    rates = [int(match.group(1)) for match in _VAT_RATE.finditer(text)]
    if 21 in rates:
        rate = 21
    elif 9 in rates:
        rate = 9
    elif 0 in rates:
        rate = 0

    vat = None
    if vat_amounts and amount:
        vat = min(vat_amounts, key=lambda value: abs(value - amount))
        if vat >= amount:
            vat = None
    if vat is None and amount and rate in {9, 21}:
        vat = round(amount * rate / (100 + rate))
    if vat is None and amount and rate == 0:
        vat = 0
    return amount, vat, rate


def _category(vendor: str | None, text: str) -> str | None:
    haystack = f"{vendor or ''} {text}".lower()
    for needles, key in VENDOR_CATEGORIES:
        if any(needle in haystack for needle in needles):
            return key
    if "btw verlegd" in haystack or "reverse charge" in haystack:
        return "software"
    return None
