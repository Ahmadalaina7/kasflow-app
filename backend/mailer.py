"""Transactional email for Kasflow.

Configure a real SMTP server (for example a Cloud86 mailbox) with:

    KASFLOW_SMTP_HOST      mail.example.nl      SMTP server hostname
    KASFLOW_SMTP_PORT      587                  587 (TLS/STARTTLS) or 465 (SSL)
    KASFLOW_SMTP_SECURITY  starttls             starttls | ssl | none
    KASFLOW_SMTP_USER      noreply@example.nl   usually the full mailbox address
    KASFLOW_SMTP_PASSWORD  ********             mailbox password
    KASFLOW_MAIL_FROM      noreply@example.nl   envelope sender
    KASFLOW_MAIL_FROM_NAME Kasflow
    KASFLOW_PUBLIC_URL     https://app.example.nl

Without configuration the mailer runs in *log mode*: messages are logged and kept
in memory (``Mailer.outbox``) so local development and tests work without
credentials. Nothing is silently discarded.
"""

from __future__ import annotations

import logging
import os
import smtplib
import ssl
import time
from dataclasses import dataclass, field
from email.message import EmailMessage
from email.utils import formataddr, formatdate
from typing import Iterable

logger = logging.getLogger("kasflow.mailer")

DEFAULT_PUBLIC_URL = "http://127.0.0.1:8010"


@dataclass
class MailSettings:
    host: str = ""
    port: int = 587
    security: str = "starttls"
    user: str = ""
    password: str = ""
    sender: str = ""
    sender_name: str = "Kasflow"
    public_url: str = DEFAULT_PUBLIC_URL

    @property
    def configured(self) -> bool:
        return bool(self.host and self.sender)

    @classmethod
    def from_environment(cls) -> "MailSettings":
        return cls(
            host=os.getenv("KASFLOW_SMTP_HOST", "").strip(),
            port=int(os.getenv("KASFLOW_SMTP_PORT", "587") or "587"),
            security=os.getenv("KASFLOW_SMTP_SECURITY", "starttls").strip().lower(),
            user=os.getenv("KASFLOW_SMTP_USER", "").strip(),
            password=os.getenv("KASFLOW_SMTP_PASSWORD", ""),
            sender=os.getenv("KASFLOW_MAIL_FROM", "").strip(),
            sender_name=os.getenv("KASFLOW_MAIL_FROM_NAME", "Kasflow").strip() or "Kasflow",
            public_url=(os.getenv("KASFLOW_PUBLIC_URL", DEFAULT_PUBLIC_URL) or DEFAULT_PUBLIC_URL).rstrip("/"),
        )


def link(public_url: str, path: str) -> str:
    return f"{public_url.rstrip('/')}/{path.lstrip('/')}"


def money(cents: int) -> str:
    """Format cents as Dutch currency: 123456 -> € 1.234,56."""
    sign = "-" if cents < 0 else ""
    whole, rest = divmod(abs(int(cents)), 100)
    groups = f"{whole:,}".replace(",", ".")
    return f"€ {sign}{groups},{rest:02d}"


def _text_body(paragraphs: Iterable[str], signature: list[str]) -> str:
    blocks = [paragraph.strip() for paragraph in paragraphs if paragraph and paragraph.strip()]
    blocks.extend(signature)
    return "\n\n".join(blocks) + "\n"


def _html_body(title: str, paragraphs: Iterable[str], button: tuple[str, str] | None, signature: list[str]) -> str:
    body = []
    for paragraph in paragraphs:
        if not paragraph or not paragraph.strip():
            continue
        body.append(
            '<p style="margin:0 0 16px; font-size:15px; line-height:1.6; color:#33415C;">'
            + paragraph.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            + "</p>"
        )
    if button:
        label, href = button
        body.append(
            '<p style="margin:24px 0;"><a href="' + href + '" style="display:inline-block; '
            'padding:13px 22px; border-radius:999px; background:#0E9F6E; color:#0A1830; '
            'font-weight:700; text-decoration:none;">' + label + "</a></p>"
        )
        body.append(
            '<p style="margin:0 0 16px; font-size:13px; line-height:1.6; color:#64748B;">'
            "Werkt de knop niet? Kopieer dan deze link:<br>" + href + "</p>"
        )
    footer = "".join(
        '<p style="margin:0 0 4px; font-size:12px; color:#64748B;">' + line + "</p>" for line in signature
    )
    return (
        '<!DOCTYPE html><html lang="nl"><body style="margin:0; padding:24px; background:#F6F7F9; '
        'font-family:Inter, Arial, sans-serif;">'
        '<div style="max-width:560px; margin:0 auto; background:#FFFFFF; border-radius:20px; padding:32px;">'
        '<div style="font-family:Georgia, serif; font-size:20px; font-weight:600; color:#0F2244; margin-bottom:24px;">Kasflow</div>'
        '<h1 style="margin:0 0 20px; font-family:Georgia, serif; font-size:22px; font-weight:500; color:#0F2244;">' + title + "</h1>"
        + "".join(body)
        + '<div style="margin-top:28px; padding-top:20px; border-top:1px solid #EEF0F4;">' + footer + "</div>"
        "</div></body></html>"
    )


@dataclass
class Mailer:
    settings: MailSettings = field(default_factory=MailSettings.from_environment)
    outbox: list[dict] = field(default_factory=list)

    @property
    def configured(self) -> bool:
        return self.settings.configured

    def build_message(
        self,
        to: str,
        subject: str,
        text: str,
        html: str | None = None,
        attachments: list[tuple[str, str, bytes]] | None = None,
    ) -> EmailMessage:
        message = EmailMessage()
        message["Subject"] = subject
        message["From"] = formataddr((self.settings.sender_name, self.settings.sender or "no-reply@localhost"))
        message["To"] = to
        message["Date"] = formatdate(localtime=True)
        message["Auto-Submitted"] = "auto-generated"
        message.set_content(text)
        if html:
            message.add_alternative(html, subtype="html")
        for filename, mime_type, payload in attachments or []:
            maintype, _, subtype = mime_type.partition("/")
            message.add_attachment(payload, maintype=maintype, subtype=subtype, filename=filename)
        return message

    def send(
        self,
        to: str,
        subject: str,
        text: str,
        html: str | None = None,
        attachments: list[tuple[str, str, bytes]] | None = None,
    ) -> bool:
        message = self.build_message(to, subject, text, html, attachments)
        record = {
            "to": to,
            "subject": subject,
            "text": text,
            "attachments": [name for name, _, _ in attachments or []],
            "sent_at": time.time(),
            "delivered": False,
        }
        if not self.configured:
            record["mode"] = "log"
            self.outbox.append(record)
            logger.info("Mailer in logmodus: '%s' aan %s (geen SMTP geconfigureerd)", subject, to)
            return True

        errors: list[str] = []
        for attempt in range(2):
            try:
                self._deliver(message)
                record["delivered"] = True
                self.outbox.append(record)
                return True
            except (smtplib.SMTPException, OSError, ssl.SSLError) as error:
                errors.append(f"{type(error).__name__}: {error}")
                if attempt == 0:
                    time.sleep(1.0)
        record["mode"] = "error"
        record["error"] = "; ".join(errors)
        self.outbox.append(record)
        logger.error("Verzenden van '%s' aan %s mislukt: %s", subject, to, record["error"])
        return False

    def _deliver(self, message: EmailMessage) -> None:
        settings = self.settings
        timeout = 15
        if settings.security == "ssl":
            context = ssl.create_default_context()
            with smtplib.SMTP_SSL(settings.host, settings.port, timeout=timeout, context=context) as server:
                if settings.user:
                    server.login(settings.user, settings.password)
                server.send_message(message)
            return
        with smtplib.SMTP(settings.host, settings.port, timeout=timeout) as server:
            server.ehlo()
            if settings.security == "starttls":
                server.starttls(context=ssl.create_default_context())
                server.ehlo()
            if settings.user:
                server.login(settings.user, settings.password)
            server.send_message(message)

    # ---- Message builders -------------------------------------------------

    def send_email_verification(self, user) -> bool:
        token_link = link(self.settings.public_url, f"verify.html?token={user['token']}")
        paragraphs = [
            f"Hallo {user['company_name']},",
            "Welkom bij Kasflow. Bevestig je e-mailadres zodat we je facturen kunnen versturen en je account kunnen herstellen.",
            "Deze link is 48 uur geldig.",
        ]
        signature = [
            "Je ontvangt deze e-mail omdat er een Kasflow-account met dit adres is aangemaakt.",
            f"Kasflow · {self.settings.public_url}",
        ]
        return self.send(
            to=user["email"],
            subject="Bevestig je e-mailadres voor Kasflow",
            text=_text_body(paragraphs, [f"Bevestig je adres: {token_link}"] + signature),
            html=_html_body("Bevestig je e-mailadres", paragraphs, ("E-mailadres bevestigen", token_link), signature),
        )

    def send_password_reset(self, user) -> bool:
        reset_link = link(self.settings.public_url, f"reset.html?token={user['token']}")
        paragraphs = [
            f"Hallo {user['company_name']},",
            "Je vroeg een nieuw wachtwoord aan voor Kasflow. Met de knop hieronder stel je het opnieuw in.",
            "Deze link is 60 minuten geldig en kan één keer worden gebruikt. Heb je dit niet aangevraagd? Dan hoef je niets te doen; je huidige wachtwoord blijft werken.",
        ]
        signature = ["Kasflow · " + self.settings.public_url]
        return self.send(
            to=user["email"],
            subject="Nieuw wachtwoord instellen voor Kasflow",
            text=_text_body(paragraphs, [f"Nieuw wachtwoord instellen: {reset_link}"] + signature),
            html=_html_body("Nieuw wachtwoord instellen", paragraphs, ("Wachtwoord instellen", reset_link), signature),
        )

    def send_invoice(
        self,
        *,
        sender_name: str,
        customer_name: str,
        customer_email: str,
        invoice_number: str,
        total_cents: int,
        due_date: str,
        note: str,
        pdf_bytes: bytes,
        pdf_filename: str,
        credit: bool = False,
    ) -> bool:
        document = "Creditfactuur" if credit else "Factuur"
        if credit:
            paragraphs = [
                f"Hallo {customer_name},",
                f"In de bijlage vind je creditfactuur {invoice_number} van {sender_name} voor {money(-total_cents)} inclusief btw.",
                "Dit bedrag verrekenen we met je openstaande factuur of storten we terug als je die al hebt betaald.",
            ]
        else:
            paragraphs = [
                f"Hallo {customer_name},",
                f"In de bijlage vind je factuur {invoice_number} van {sender_name} voor {money(total_cents)} inclusief btw.",
                f"Je kunt het bedrag voor {due_date} overmaken onder vermelding van factuurnummer {invoice_number}.",
            ]
        if note:
            paragraphs.insert(2, note)
        signature = [
            f"Deze e-mail is verstuurd via Kasflow namens {sender_name}.",
            "Vragen over deze factuur? Antwoord dan direct op deze e-mail.",
        ]
        return self.send(
            to=customer_email,
            subject=f"{document} {invoice_number} van {sender_name}",
            text=_text_body(paragraphs, signature),
            html=_html_body(f"{document} {invoice_number}", paragraphs, None, signature),
            attachments=[(pdf_filename, "application/pdf", pdf_bytes)],
        )

    def send_reminder(
        self,
        *,
        sender_name: str,
        customer_name: str,
        customer_email: str,
        invoice_number: str,
        total_cents: int,
        due_date: str,
        pdf_bytes: bytes,
        pdf_filename: str,
    ) -> bool:
        paragraphs = [
            f"Hallo {customer_name},",
            f"Een vriendelijke herinnering: factuur {invoice_number} van {sender_name} "
            f"({money(total_cents)} inclusief btw) was te betalen vóór {due_date}.",
            "In de bijlage staat de factuur nog een keer. Is de betaling onderweg? Dan kun je dit bericht negeren.",
        ]
        signature = [
            f"Deze herinnering is verstuurd via Kasflow namens {sender_name}.",
            "Vragen over deze factuur? Antwoord dan direct op deze e-mail.",
        ]
        return self.send(
            to=customer_email,
            subject=f"Herinnering: factuur {invoice_number} van {sender_name}",
            text=_text_body(paragraphs, signature),
            html=_html_body(f"Herinnering factuur {invoice_number}", paragraphs, None, signature),
            attachments=[(pdf_filename, "application/pdf", pdf_bytes)],
        )
