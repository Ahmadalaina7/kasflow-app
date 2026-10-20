"""Tests for the production features: PDF invoices, e-mail, AVG, security."""

import io
import json
import re
import zipfile
from datetime import date
from pathlib import Path

from fastapi.testclient import TestClient

from app import create_app
from mailer import Mailer, MailSettings


class RecordingMailer(Mailer):
    """SMTP-configured mailer that records messages instead of sending them."""

    def __init__(self):
        super().__init__(
            settings=MailSettings(
                host="smtp.example.nl",
                user="noreply@example.nl",
                password="geheim",
                sender="noreply@example.nl",
                public_url="https://app.example.nl",
            )
        )
        self.delivered = []

    def _deliver(self, message):
        self.delivered.append(message)


def token_from(mailer, path):
    for record in mailer.outbox:
        match = re.search(path + r"\?token=([A-Za-z0-9_\-]+)", record["text"])
        if match:
            return match.group(1)
    raise AssertionError("geen token in de uitgaande mail gevonden")


def client_for(tmp_path, mailer=None):
    database = tmp_path / "product.sqlite3"
    return TestClient(
        create_app(
            f"sqlite:///{database.as_posix()}",
            mailer=mailer,
            upload_dir=tmp_path / "uploads",
        )
    )


def register(client, email="sanne@example.nl", company_name="Studio Veer"):
    response = client.post(
        "/api/auth/register",
        json={
            "email": email,
            "password": "correct-horse-battery-staple",
            "company_name": company_name,
            "kvk_number": "12345678",
            "terms": True,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def verified_client(tmp_path):
    mailer = RecordingMailer()
    client = client_for(tmp_path, mailer)
    register(client)
    assert client.post(
        "/api/auth/verify-email", json={"token": token_from(mailer, "verify.html")}
    ).status_code == 200
    return client, mailer


def create_customer(client, name="Studio Veer", email="sanne@example.nl"):
    response = client.post(
        "/api/customers",
        json={
            "name": name,
            "email": email,
            "street": "Dam 2",
            "postal_code": "1012 JS",
            "city": "Amsterdam",
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def complete_profile(client):
    response = client.patch(
        "/api/auth/me",
        json={
            "company_name": "Studio Veer",
            "kvk_number": "12345678",
            "vat_number": "NL123456789B01",
            "street": "Prinsengracht 1",
            "postal_code": "1015 DV",
            "city": "Amsterdam",
        },
    )
    assert response.status_code == 200, response.text


def create_invoice(client, customer_id, number="2026-001", vat_rate="0.21"):
    response = client.post(
        "/api/invoices",
        json={
            "customer_id": customer_id,
            "number": number,
            "invoice_date": date.today().isoformat(),
            "due_date": date.today().replace(day=28).isoformat(),
            "lines": [
                {
                    "description": "Werk",
                    "quantity": "2",
                    "unit_price": "100.00",
                    "vat_rate": vat_rate,
                }
            ],
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_invoice_pdf_contains_all_legal_invoice_fields(tmp_path):
    client = client_for(tmp_path)
    register(client)
    profile = client.patch(
        "/api/auth/me",
        json={
            "company_name": "Studio Veer",
            "kvk_number": "12345678",
            "vat_number": "NL123456789B01",
            "street": "Prinsengracht 1",
            "postal_code": "1015 DV",
            "city": "Amsterdam",
            "iban": "NL91ABNA0417164300",
            "payment_term_days": 14,
        },
    )
    assert profile.status_code == 200
    customer = create_customer(client)
    client.patch(
        f"/api/customers/{customer['id']}",
        json={
            "name": "Klant BV",
            "contact": "Sanne de Vries",
            "email": "klant@example.nl",
            "street": "Dam 2",
            "postal_code": "1012 JS",
            "city": "Amsterdam",
            "vat_number": "NL000000000B01",
        },
    )
    invoice = create_invoice(client, customer["id"], "2026-042")

    response = client.get(f"/api/invoices/{invoice['id']}/pdf")
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/pdf"
    assert "attachment" in response.headers["content-disposition"]
    assert "factuur-2026-042.pdf" in response.headers["content-disposition"]

    pdf = response.content
    assert pdf.startswith(b"%PDF")
    for field in [
        b"Studio Veer",
        b"Prinsengracht 1",
        b"1015 DV",
        b"KVK 12345678",
        b"NL123456789B01",
        b"NL91ABNA0417164300",
        b"Klant BV",
        b"Dam 2",
        b"1012 JS",
        b"2026-042",
        b"Totaal te betalen",
        b"Btw 21%",
    ]:
        assert field in pdf, field


def test_invoice_can_be_emailed_with_pdf_attachment(tmp_path):
    client, mailer = verified_client(tmp_path)
    complete_profile(client)
    customer = create_customer(client)
    invoice = create_invoice(client, customer["id"], "2026-100")

    sent = client.post(f"/api/invoices/{invoice['id']}/send", json={})
    assert sent.status_code == 200, sent.text
    body = sent.json()
    assert body["sent"] is True
    assert body["to"] == "sanne@example.nl"
    assert body["mode"] == "smtp"
    assert body["sent_at"] is not None

    assert len(mailer.outbox) == 2  # verification + invoice
    record = mailer.outbox[-1]
    assert "Factuur 2026-100" in record["subject"]
    assert record["attachments"] == ["factuur-2026-100.pdf"]
    assert record["delivered"] is True

    refreshed = client.get(f"/api/invoices/{invoice['id']}").json()
    assert refreshed["sent_at"] is not None

    without_email = create_customer(client, "Zonder mail", email=None)
    draft = create_invoice(client, without_email["id"], "2026-101")
    assert client.post(f"/api/invoices/{draft['id']}/send", json={}).status_code == 422
    override = client.post(
        f"/api/invoices/{draft['id']}/send", json={"to": "inkoop@example.nl"}
    )
    assert override.status_code == 200
    assert override.json()["to"] == "inkoop@example.nl"


def test_email_verification_is_required_when_smtp_is_configured(tmp_path):
    mailer = RecordingMailer()
    client = client_for(tmp_path, mailer)
    registered = register(client)
    assert registered["verification_required"] is True
    assert registered["email_verified"] is False
    assert client.get("/api/auth/me").status_code == 401

    blocked = client.post(
        "/api/auth/login",
        json={"email": "sanne@example.nl", "password": "correct-horse-battery-staple"},
    )
    assert blocked.status_code == 403
    assert "Bevestig" in blocked.json()["detail"]
    assert len(mailer.outbox) == 1

    token = token_from(mailer, "verify.html")
    verified = client.post("/api/auth/verify-email", json={"token": token})
    assert verified.status_code == 200, verified.text
    assert verified.json()["email_verified"] is True
    assert client.get("/api/auth/me").status_code == 200

    assert client.post("/api/auth/verify-email", json={"token": token}).status_code == 400


def test_password_reset_flow_invalidates_old_password(tmp_path):
    client, mailer = verified_client(tmp_path)
    client.post("/api/auth/logout")

    accepted = client.post("/api/auth/forgot-password", json={"email": "sanne@example.nl"})
    assert accepted.status_code == 202
    unknown = client.post("/api/auth/forgot-password", json={"email": "niemand@example.nl"})
    assert unknown.status_code == 202, "een onbekend adres mag niets verraden"

    reset_token = token_from(mailer, "reset.html")
    reset = client.post(
        "/api/auth/reset-password",
        json={"token": reset_token, "password": "een-nieuw-wachtwoord-2026"},
    )
    assert reset.status_code == 200, reset.text

    again = client.post(
        "/api/auth/reset-password",
        json={"token": reset_token, "password": "nog-een-wachtwoord-2026"},
    )
    assert again.status_code == 400

    client.post("/api/auth/logout")
    old = client.post(
        "/api/auth/login",
        json={"email": "sanne@example.nl", "password": "correct-horse-battery-staple"},
    )
    assert old.status_code == 401
    new = client.post(
        "/api/auth/login",
        json={"email": "sanne@example.nl", "password": "een-nieuw-wachtwoord-2026"},
    )
    assert new.status_code == 200


def test_account_export_and_deletion_remove_all_data(tmp_path):
    upload_root = tmp_path / "uploads"
    client = client_for(tmp_path)
    register(client)
    customer = create_customer(client)
    create_invoice(client, customer["id"], "2026-200")
    uploaded = client.post(
        "/api/receipts",
        data={
            "vendor": "Jumbo",
            "receipt_date": date.today().isoformat(),
            "amount": "12.50",
            "category": "Kantoor",
        },
        files={"file": ("bon.png", b"\x89PNG\r\n\x1a\nrest", "image/png")},
    )
    assert uploaded.status_code == 201

    export = client.get("/api/account/export")
    assert export.status_code == 200
    with zipfile.ZipFile(io.BytesIO(export.content)) as archive:
        names = archive.namelist()
        payload = json.loads(archive.read("kasflow-export.json"))
    assert payload["account"]["email"] == "sanne@example.nl"
    assert payload["invoices"][0]["number"] == "2026-200"
    assert payload["customers"][0]["name"] == "Studio Veer"
    assert any(name.startswith("bonnetjes/") for name in names)
    assert "account.export" in [event["action"] for event in payload["activity"]]

    refused = client.request(
        "DELETE",
        "/api/account",
        json={"password": "correct-horse-battery-staple", "confirm": "ja"},
    )
    assert refused.status_code == 422
    wrong_password = client.request(
        "DELETE",
        "/api/account",
        json={"password": "verkeerd-wachtwoord", "confirm": "VERWIJDER"},
    )
    assert wrong_password.status_code == 400

    deleted = client.request(
        "DELETE",
        "/api/account",
        json={"password": "correct-horse-battery-staple", "confirm": "VERWIJDER"},
    )
    assert deleted.status_code == 204
    assert client.get("/api/auth/me").status_code == 401
    assert not list(upload_root.glob("*/*")), "geuploade bonnen moeten zijn verwijderd"
    login = client.post(
        "/api/auth/login",
        json={"email": "sanne@example.nl", "password": "correct-horse-battery-staple"},
    )
    assert login.status_code == 401


def test_next_invoice_number_uses_prefix_and_sequence(tmp_path):
    client = client_for(tmp_path)
    register(client)
    customer = create_customer(client)

    first = client.get("/api/invoices/next-number").json()["number"]
    assert first.endswith("-001")
    create_invoice(client, customer["id"], first)
    second = client.get("/api/invoices/next-number").json()["number"]
    assert second.endswith("-002")

    client.patch(
        "/api/auth/me",
        json={"company_name": "Studio Veer", "kvk_number": "12345678", "invoice_prefix": "kv"},
    )
    prefixed = client.get("/api/invoices/next-number").json()["number"]
    assert prefixed.startswith("KV") and prefixed.endswith("-001")


def test_login_is_rate_limited(tmp_path):
    client = client_for(tmp_path)
    register(client)
    client.post("/api/auth/logout")
    for attempt_number in range(10):
        attempt = client.post(
            "/api/auth/login",
            json={"email": "sanne@example.nl", "password": "fout-wachtwoord"},
            headers={"X-Forwarded-For": f"10.0.0.{attempt_number}"},
        )
        assert attempt.status_code == 401
    blocked = client.post(
        "/api/auth/login",
        json={"email": "sanne@example.nl", "password": "fout-wachtwoord"},
        headers={"X-Forwarded-For": "10.0.0.99"},
    )
    assert blocked.status_code == 429
    assert int(blocked.headers["retry-after"]) > 0


def test_invoices_only_accept_dutch_vat_rates(tmp_path):
    client = client_for(tmp_path)
    register(client)
    customer = create_customer(client)
    for rate in ["0.21", "0.09", "0"]:
        create_invoice(client, customer["id"], f"2026-{rate}", vat_rate=rate)
    rejected = client.post(
        "/api/invoices",
        json={
            "customer_id": customer["id"],
            "number": "2026-017",
            "invoice_date": date.today().isoformat(),
            "due_date": date.today().isoformat(),
            "lines": [{"description": "Werk", "quantity": "1", "unit_price": "10", "vat_rate": "0.17"}],
        },
    )
    assert rejected.status_code == 422


def test_approved_vat_quarter_is_locked_for_new_invoices(tmp_path):
    client = client_for(tmp_path)
    register(client)
    customer = create_customer(client)
    create_invoice(client, customer["id"], "2026-001")
    today = date.today()
    period = {"year": today.year, "quarter": (today.month - 1) // 3 + 1}
    approved = client.post("/api/vat/approve", json=period).json()

    late = client.post(
        "/api/invoices",
        json={
            "customer_id": customer["id"],
            "number": "2026-002",
            "invoice_date": today.isoformat(),
            "due_date": today.isoformat(),
            "lines": [{"description": "Te laat", "quantity": "1", "unit_price": "10", "vat_rate": "0.21"}],
        },
    )
    assert late.status_code == 409
    assert client.get("/api/vat").json()["current"]["vat_cents"] == approved["vat_cents"]

    client.delete("/api/vat/approval", params=period)
    create_invoice(client, customer["id"], "2026-002")


def test_incomplete_invoice_is_a_marked_draft_and_cannot_be_sent(tmp_path):
    client, _ = verified_client(tmp_path)
    customer = client.post(
        "/api/customers", json={"name": "Zonder adres", "email": "klant@example.nl"}
    ).json()
    invoice = create_invoice(client, customer["id"], "2026-200")

    pdf = client.get(f"/api/invoices/{invoice['id']}/pdf").content
    assert b"CONCEPT" in pdf
    assert b"voldoet aan de factuureisen" not in pdf

    blocked = client.post(f"/api/invoices/{invoice['id']}/send", json={})
    assert blocked.status_code == 422
    detail = blocked.json()["detail"]
    assert "je bedrijfsadres" in detail and "je btw-id" in detail and "Zonder adres" in detail


def test_registration_requires_terms_and_valid_email(tmp_path):
    client = client_for(tmp_path)
    payload = {
        "email": "zonder@example.nl",
        "password": "correct-horse-battery-staple",
        "company_name": "Zonder Voorwaarden",
        "kvk_number": "12345678",
        "terms": False,
    }
    assert client.post("/api/auth/register", json=payload).status_code == 422
    payload["terms"] = True
    payload["email"] = "geen-adres"
    assert client.post("/api/auth/register", json=payload).status_code == 422
    payload["email"] = "wel@example.nl"
    assert client.post("/api/auth/register", json=payload).status_code == 201


def test_health_security_headers_and_public_pages(tmp_path):
    client = client_for(tmp_path)
    health = client.get("/api/health")
    assert health.status_code == 200
    assert health.json()["database"] == "ok"
    assert health.json()["mail"] == "log"

    page = client.get("/privacy.html")
    assert page.status_code == 200
    assert "Content-Security-Policy" in page.headers
    assert page.headers["X-Content-Type-Options"] == "nosniff"
    assert client.get("/offline.html").status_code == 200
    assert client.get("/sw.js").status_code == 200
    assert client.get("/sw.js").headers["service-worker-allowed"] == "/"
    assert client.get("/backup.sqlite3").status_code == 404


WEB_ROOT = Path(__file__).resolve().parents[1]


def test_fonts_are_self_hosted_and_served_with_the_right_type(tmp_path):
    client = client_for(tmp_path)

    css = client.get("/fonts.css")
    assert css.status_code == 200
    assert "fonts.googleapis" not in css.text

    fonts = re.findall(r"url\('([^']+\.woff2)'\)", css.text)
    assert fonts, "fonts.css verwijst naar geen enkel lettertypebestand"
    for name in fonts:
        response = client.get("/" + name)
        assert response.status_code == 200, name
        assert response.headers["content-type"] == "font/woff2"
        assert response.content[:4] == b"wOF2"
        assert "max-age" in response.headers["cache-control"]

    assert client.get("/fonts/bestaat-niet.woff2").status_code == 404
    assert client.get("/fonts/OFL-Inter.txt").status_code == 404

    styles = client.get("/app-styles.css").text
    assert "--display:'Plus Jakarta Sans'" in styles
    assert "--sans:'Inter'" in styles


def test_every_page_uses_the_local_fonts():
    pages = sorted(WEB_ROOT.glob("*.html"))
    assert pages
    for page in pages:
        html = page.read_text(encoding="utf-8")
        assert "fonts.googleapis" not in html, page.name
        assert "fonts.gstatic" not in html, page.name
        assert 'href="fonts.css"' in html, page.name
        for href in re.findall(r'href="(fonts/[^"]+\.woff2)"', html):
            assert (WEB_ROOT / href).is_file(), f"{page.name} verwijst naar {href}"

    # De service worker moet de lettertypen voor offline gebruik klaarzetten.
    service_worker = (WEB_ROOT / "sw.js").read_text(encoding="utf-8")
    assert "fonts/jakarta-normal-latin.woff2" in service_worker
    assert "fonts/inter-normal-latin.woff2" in service_worker
