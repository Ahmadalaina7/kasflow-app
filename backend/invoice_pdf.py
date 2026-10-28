"""Invoice PDF generation.

The layout carries every field the Dutch tax administration requires on an
invoice: full legal names and addresses of both parties, the supplier's VAT
identification number and KVK number, invoice date, a unique invoice number,
description and quantity of the services, the delivery date, amounts excluding
VAT per rate and the applicable VAT rate with its amount.
"""

from __future__ import annotations

from dataclasses import dataclass, field, fields, replace
from datetime import date
from decimal import Decimal
from io import BytesIO
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.enums import TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (
    KeepTogether,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

NAVY = colors.HexColor("#0F2244")
INK = colors.HexColor("#33415C")
MUTED = colors.HexColor("#64748B")
LINE = colors.HexColor("#E8EBF0")
EMERALD = colors.HexColor("#0E9F6E")

MONTHS = [
    "januari", "februari", "maart", "april", "mei", "juni",
    "juli", "augustus", "september", "oktober", "november", "december",
]


def dutch_date(value: date | None) -> str:
    if value is None:
        return "—"
    return f"{value.day} {MONTHS[value.month - 1]} {value.year}"


def money(cents: int) -> str:
    sign = "-" if cents < 0 else ""
    whole, rest = divmod(abs(int(cents)), 100)
    groups = f"{whole:,}".replace(",", ".")
    return f"€ {sign}{groups},{rest:02d}"


def quantity(value: Decimal | float | int) -> str:
    number = Decimal(str(value)).normalize()
    text = format(number, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text.replace(".", ",") or "0"


def percentage(bps: int) -> str:
    value = Decimal(bps) / 100
    text = format(value.normalize(), "f").rstrip("0").rstrip(".") if value else "0"
    return (text or "0").replace(".", ",") + "%"


@dataclass
class Party:
    name: str
    street: str = ""
    postal_code: str = ""
    city: str = ""
    country: str = "Nederland"
    contact: str = ""
    email: str = ""
    phone: str = ""
    vat_number: str = ""
    kvk_number: str = ""
    iban: str = ""

    def address_lines(self) -> list[str]:
        lines: list[str] = []
        if self.street:
            lines.append(self.street)
        postal_city = " ".join(part for part in [self.postal_code, self.city] if part)
        if postal_city:
            lines.append(postal_city)
        if self.country and self.country.lower() not in {"nederland", "the netherlands", "nl"}:
            lines.append(self.country)
        return lines


@dataclass
class InvoiceLineData:
    description: str
    quantity: Decimal
    unit_price_cents: int
    vat_rate_bps: int
    net_cents: int
    vat_cents: int


@dataclass
class InvoicePdfData:
    invoice_number: str
    invoice_date: date
    due_date: date
    delivery_date: date | None
    payment_term_days: int
    supplier: Party
    customer: Party
    lines: list[InvoiceLineData] = field(default_factory=list)
    subtotal_cents: int = 0
    vat_cents: int = 0
    total_cents: int = 0
    note: str = ""
    paid: bool = False
    missing_fields: list[str] = field(default_factory=list)
    credit: bool = False
    credited_number: str = ""
    credited_date: date | None = None
    vat_notice: str = ""
    # Een offerte gebruikt hetzelfde papier als een factuur, maar zonder
    # betaalopdracht: er staat een geldigheidsdatum en een akkoordverzoek in.
    quote: bool = False
    valid_until: date | None = None


def _styles() -> dict[str, ParagraphStyle]:
    base = getSampleStyleSheet()
    return {
        "title": ParagraphStyle(
            "title", parent=base["Normal"], fontName="Helvetica-Bold", fontSize=24,
            textColor=NAVY, leading=28, alignment=TA_RIGHT,
        ),
        "label": ParagraphStyle(
            "label", parent=base["Normal"], fontName="Helvetica-Bold", fontSize=8,
            textColor=MUTED, leading=11, spaceAfter=2,
        ),
        "body": ParagraphStyle("body", parent=base["Normal"], fontSize=9.5, textColor=INK, leading=14),
        "small": ParagraphStyle("small", parent=base["Normal"], fontSize=8.5, textColor=MUTED, leading=12),
        "party": ParagraphStyle("party", parent=base["Normal"], fontSize=10, textColor=NAVY, leading=14),
        "cell": ParagraphStyle("cell", parent=base["Normal"], fontSize=9, textColor=INK, leading=12),
        "cellRight": ParagraphStyle(
            "cellRight", parent=base["Normal"], fontSize=9, textColor=INK, leading=12, alignment=TA_RIGHT
        ),
    }


def _escape_party(party: Party) -> Party:
    return replace(party, **{item.name: escape(getattr(party, item.name)) for item in fields(party)})


def build_invoice_pdf(data: InvoicePdfData) -> bytes:
    # reportlab Paragraphs parse markup, so every user-supplied text is escaped.
    data = replace(
        data,
        invoice_number=escape(data.invoice_number),
        note=escape(data.note),
        credited_number=escape(data.credited_number),
        supplier=_escape_party(data.supplier),
        customer=_escape_party(data.customer),
        lines=[replace(line, description=escape(line.description)) for line in data.lines],
        missing_fields=[escape(item) for item in data.missing_fields],
    )
    label = "Offerte" if data.quote else ("Creditfactuur" if data.credit else "Factuur")
    buffer = BytesIO()
    document = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        leftMargin=20 * mm,
        rightMargin=20 * mm,
        topMargin=18 * mm,
        bottomMargin=18 * mm,
        title=f"{label} {data.invoice_number}",
        author=data.supplier.name,
        subject=f"{label} {data.invoice_number}",
        # Keep the text streams readable: invoice PDFs stay small and the
        # contents can be checked without a separate text-extraction step.
        pageCompression=0,
    )
    styles = _styles()
    story: list = []

    if data.missing_fields:
        story.append(
            Paragraph(
                "<b>CONCEPT — nog geen geldige factuur.</b> Ontbrekende verplichte gegevens: "
                + ", ".join(data.missing_fields) + ".",
                ParagraphStyle("draft", parent=styles["body"], textColor=colors.HexColor("#C2483D")),
            )
        )
        story.append(Spacer(1, 6 * mm))

    supplier_lines = [
        f"<b>{data.supplier.name}</b>",
        *data.supplier.address_lines(),
        f"KVK {data.supplier.kvk_number}" if data.supplier.kvk_number else "",
        f"Btw-id {data.supplier.vat_number}" if data.supplier.vat_number else "",
        f"IBAN {data.supplier.iban}" if data.supplier.iban else "",
        data.supplier.email,
        data.supplier.phone,
    ]
    supplier_block = Paragraph(
        "<br/>".join(line for line in supplier_lines if line), styles["small"]
    )

    if data.quote:
        meta_rows = [
            [Paragraph("OFFERTENUMMER", styles["label"]), Paragraph(data.invoice_number, styles["body"])],
            [Paragraph("OFFERTEDATUM", styles["label"]), Paragraph(dutch_date(data.invoice_date), styles["body"])],
            [
                Paragraph("GELDIG TOT", styles["label"]),
                Paragraph(dutch_date(data.valid_until), styles["body"]),
            ],
        ]
    else:
        meta_rows = [
            [Paragraph("FACTUURNUMMER", styles["label"]), Paragraph(data.invoice_number, styles["body"])],
            [Paragraph("FACTUURDATUM", styles["label"]), Paragraph(dutch_date(data.invoice_date), styles["body"])],
            [
                Paragraph("LEVERDATUM", styles["label"]),
                Paragraph(dutch_date(data.delivery_date or data.invoice_date), styles["body"]),
            ],
            [Paragraph("VERVALDATUM", styles["label"]), Paragraph(dutch_date(data.due_date), styles["body"])],
        ]
    meta_table = Table(meta_rows, colWidths=[32 * mm, 30 * mm])
    meta_table.setStyle(
        TableStyle(
            [
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
                ("TOPPADDING", (0, 0), (-1, -1), 0),
            ]
        )
    )

    header = Table(
        [[supplier_block, Table([[Paragraph(label.upper(), styles["title"])]], style=TableStyle([
            ("ALIGN", (0, 0), (-1, -1), "RIGHT"),
            ("LEFTPADDING", (0, 0), (-1, -1), 0),
            ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ]))]],
        colWidths=[95 * mm, 75 * mm],
    )
    header.setStyle(
        TableStyle(
            [
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                ("RIGHTPADDING", (0, 0), (-1, -1), 0),
            ]
        )
    )
    story.append(header)
    story.append(Spacer(1, 10 * mm))

    customer_lines = [
        "<b>Offerte voor</b>" if data.quote else "<b>Factuuradres</b>",
        data.customer.name,
    ]
    if data.customer.contact:
        customer_lines.append(f"T.a.v. {data.customer.contact}")
    customer_lines.extend(data.customer.address_lines())
    if data.customer.vat_number:
        customer_lines.append(f"Btw-id afnemer: {data.customer.vat_number}")

    address_table = Table(
        [
            [
                Paragraph("<br/>".join(customer_lines), styles["party"]),
                meta_table,
            ]
        ],
        colWidths=[105 * mm, 65 * mm],
    )
    address_table.setStyle(
        TableStyle(
            [
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                ("RIGHTPADDING", (0, 0), (-1, -1), 0),
            ]
        )
    )
    story.append(address_table)
    story.append(Spacer(1, 10 * mm))

    line_rows = [
        [
            Paragraph("<b>Omschrijving</b>", styles["cell"]),
            Paragraph("<b>Aantal</b>", styles["cellRight"]),
            Paragraph("<b>Prijs excl. btw</b>", styles["cellRight"]),
            Paragraph("<b>Btw</b>", styles["cellRight"]),
            Paragraph("<b>Bedrag excl. btw</b>", styles["cellRight"]),
        ]
    ]
    for line in data.lines:
        line_rows.append(
            [
                Paragraph(line.description, styles["cell"]),
                Paragraph(quantity(line.quantity), styles["cellRight"]),
                Paragraph(money(line.unit_price_cents), styles["cellRight"]),
                Paragraph(percentage(line.vat_rate_bps), styles["cellRight"]),
                Paragraph(money(line.net_cents), styles["cellRight"]),
            ]
        )
    lines_table = Table(line_rows, colWidths=[72 * mm, 18 * mm, 28 * mm, 16 * mm, 36 * mm], repeatRows=1)
    lines_table.setStyle(
        TableStyle(
            [
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LINEBELOW", (0, 0), (-1, 0), 0.8, NAVY),
                ("LINEBELOW", (0, 1), (-1, -1), 0.4, LINE),
                ("TOPPADDING", (0, 0), (-1, -1), 7),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                ("RIGHTPADDING", (0, 0), (-1, -1), 0),
            ]
        )
    )
    story.append(lines_table)
    story.append(Spacer(1, 6 * mm))

    rate_totals: dict[int, dict[str, int]] = {}
    for line in data.lines:
        bucket = rate_totals.setdefault(line.vat_rate_bps, {"net": 0, "vat": 0})
        bucket["net"] += line.net_cents
        bucket["vat"] += line.vat_cents

    totals_rows = [
        [Paragraph("Totaal excl. btw", styles["cellRight"]), Paragraph(money(data.subtotal_cents), styles["cellRight"])]
    ]
    for rate_bps, bucket in sorted(rate_totals.items(), reverse=True):
        totals_rows.append(
            [
                Paragraph(
                    f"Btw {percentage(rate_bps)} over {money(bucket['net'])}", styles["cellRight"]
                ),
                Paragraph(money(bucket["vat"]), styles["cellRight"]),
            ]
        )
    totals_rows.append(
        [
            Paragraph(
                "<b>Totaal</b>" if data.quote else "<b>Totaal te betalen</b>", styles["cellRight"]
            ),
            Paragraph(f"<b>{money(data.total_cents)}</b>", styles["cellRight"]),
        ]
    )
    totals_table = Table(totals_rows, colWidths=[70 * mm, 42 * mm], hAlign="RIGHT")
    totals_table.setStyle(
        TableStyle(
            [
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("TOPPADDING", (0, 0), (-1, -1), 5),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                ("LINEABOVE", (0, -1), (-1, -1), 0.8, NAVY),
                ("TEXTCOLOR", (0, -1), (-1, -1), NAVY),
            ]
        )
    )
    story.append(totals_table)
    story.append(Spacer(1, 8 * mm))

    if data.quote:
        payment_lines = [
            f"<b>Deze offerte is geldig tot {dutch_date(data.valid_until)}.</b> "
            "Akkoord geven kan met de knop in de e-mail die je van ons kreeg. "
            "Daarna nemen we contact op om het werk in te plannen en volgt de factuur.",
        ]
    elif data.credit:
        payment_lines = [
            f"<b>Creditering van factuur {data.credited_number} van {dutch_date(data.credited_date)}.</b> "
            f"Het bedrag van {money(-data.total_cents)} verrekenen we met die factuur, "
            "of storten we terug als je die al hebt betaald.",
        ]
    elif data.paid:
        payment_lines = [
            "<b>Deze factuur is betaald.</b> Met dank voor je betaling.",
        ]
    else:
        payment_lines = [
            f"<b>Betaal {money(data.total_cents)} voor {dutch_date(data.due_date)}</b> "
            f"(betalingstermijn {data.payment_term_days} dagen)"
            + (f" op IBAN {data.supplier.iban}" if data.supplier.iban else "")
            + f", onder vermelding van factuurnummer {data.invoice_number}.",
            "Vermeld bij je betaling altijd het factuurnummer, dan kunnen we je betaling direct verwerken.",
        ]
    if data.vat_notice:
        payment_lines.insert(0, f"<b>{data.vat_notice}</b>")
    if data.note:
        payment_lines.append(data.note)
    payment_block = Paragraph("<br/><br/>".join(payment_lines), styles["body"])

    legal_lines = [
        data.supplier.name,
        " · ".join(
            part
            for part in [
                f"KVK {data.supplier.kvk_number}" if data.supplier.kvk_number else "",
                f"Btw-id {data.supplier.vat_number}" if data.supplier.vat_number else "",
                "IBAN " + data.supplier.iban if data.supplier.iban else "",
            ]
            if part
        ),
        data.supplier.email,
    ]
    story.append(
        KeepTogether(
            [
                payment_block,
                Spacer(1, 10 * mm),
                Paragraph(
                    "<br/>".join(line for line in legal_lines if line), styles["small"]
                ),
                Paragraph(
                    "Deze offerte is opgemaakt met Kasflow."
                    if data.quote
                    else (
                        "Deze factuur is opgemaakt met Kasflow."
                        if data.missing_fields
                        else "Deze factuur is opgemaakt met Kasflow en voldoet aan de factuureisen van de Belastingdienst."
                    ),
                    styles["small"],
                ),
            ]
        )
    )

    document.build(story)
    return buffer.getvalue()
