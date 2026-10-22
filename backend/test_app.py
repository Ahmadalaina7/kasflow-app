import io
import json
import re
import sqlite3
import zipfile
from datetime import date, datetime, timedelta, timezone

from fastapi.testclient import TestClient

from app import create_app, hash_password
from mailer import MailSettings, Mailer


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
    complete_profile(client, company_name)
    return response.json()


def complete_profile(client, company_name="Studio Veer"):
    response = client.patch(
        "/api/auth/me",
        json={
            "company_name": company_name,
            "kvk_number": "12345678",
            "vat_number": "NL123456789B01",
            "street": "Prinsengracht 1",
            "postal_code": "1015 DV",
            "city": "Amsterdam",
        },
    )
    assert response.status_code == 200, response.text


ADDRESS = {"street": "Dam 2", "postal_code": "1012 JS", "city": "Amsterdam"}


def create_customer(client, name="Studio Veer"):
    response = client.post(
        "/api/customers",
        json={"name": name, "email": "sanne@example.nl", **ADDRESS},
    )
    assert response.status_code == 201
    return response.json()


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


def client_for(tmp_path):
    database = tmp_path / "test.sqlite3"
    return TestClient(create_app(f"sqlite:///{database.as_posix()}"))


def test_customer_and_invoice_are_persisted_and_amounts_are_rounded(tmp_path):
    client = client_for(tmp_path)
    register(client)

    customer_response = client.post(
        "/api/customers",
        json={"name": "  Studio Veer  ", "email": "sanne@example.nl", **ADDRESS},
    )
    assert customer_response.status_code == 201
    customer = customer_response.json()
    assert customer["name"] == "Studio Veer"

    invoice_response = client.post(
        "/api/invoices",
        json={
            "customer_id": customer["id"],
            "number": "2026-001",
            "invoice_date": date(2026, 8, 5).isoformat(),
            "due_date": date(2026, 8, 19).isoformat(),
            "lines": [
                {
                    "description": "Werk",
                    "quantity": "1",
                    "unit_price": "0.05",
                    "vat_rate": "0.21",
                }
            ],
        },
    )
    assert invoice_response.status_code == 201
    assert invoice_response.json()["subtotal_cents"] == 5
    assert invoice_response.json()["vat_cents"] == 1
    assert invoice_response.json()["total_cents"] == 6
    assert invoice_response.json()["lines"][0]["description"] == "Werk"
    assert invoice_response.json()["lines"][0]["unit_price_cents"] == 5

    assert client.get("/api/customers").json()[0]["id"] == customer["id"]
    assert client.get("/api/invoices").json()[0]["number"] == "2026-001"


def test_invoice_rejects_unknown_customer_and_invalid_dates(tmp_path):
    client = client_for(tmp_path)
    register(client)
    payload = {
        "customer_id": 999,
        "number": "2026-002",
        "invoice_date": "2026-08-19",
        "due_date": "2026-08-05",
        "lines": [
            {
                "description": "Werk",
                "quantity": "1",
                "unit_price": "100.00",
                "vat_rate": "0.21",
            }
        ],
    }

    assert client.post("/api/invoices", json=payload).status_code == 422

    payload["due_date"] = "2026-08-25"
    assert client.post("/api/invoices", json=payload).status_code == 404


def test_server_serves_app_pages_but_not_backend_source(tmp_path):
    client = client_for(tmp_path)

    assert client.get("/facturen.html", follow_redirects=False).status_code == 303
    register(client)
    assert client.get("/facturen.html").status_code == 200
    assert client.get("/app-styles.css").status_code == 200
    assert client.get("/app-shell.js").status_code == 200
    assert client.get("/backend/app.py").status_code == 404
    assert client.get("/login.html", follow_redirects=False).status_code == 303


def test_accounts_cannot_access_each_others_customers_or_invoices(tmp_path):
    client = client_for(tmp_path)
    register(client)
    customer = create_customer(client, "Privé")
    client.post("/api/auth/logout")
    register(client, "ander@example.nl", "Ander Bedrijf")

    assert client.get("/api/customers").json() == []
    response = client.post(
        "/api/invoices",
        json={
            "customer_id": customer["id"],
            "number": "2026-001",
            "invoice_date": "2026-08-05",
            "due_date": "2026-08-19",
            "lines": [
                {
                    "description": "Werk",
                    "quantity": "1",
                    "unit_price": "100.00",
                    "vat_rate": "0.21",
                }
            ],
        },
    )
    assert response.status_code == 404


def test_two_accounts_can_use_the_same_invoice_number(tmp_path):
    client = client_for(tmp_path)
    register(client)
    first_customer = create_customer(client, "Eerste klant")
    create_invoice(client, first_customer["id"], "2026-001")

    client.post("/api/auth/logout")
    register(client, "tweede@example.nl", "Tweede Bedrijf")
    second_customer = create_customer(client, "Tweede klant")
    invoice = create_invoice(client, second_customer["id"], "2026-001")
    assert invoice["number"] == "2026-001"

    duplicate = client.post(
        "/api/invoices",
        json={
            "customer_id": second_customer["id"],
            "number": "2026-001",
            "invoice_date": date.today().isoformat(),
            "due_date": date.today().isoformat(),
            "lines": [
                {
                    "description": "Dubbel",
                    "quantity": "1",
                    "unit_price": "10.00",
                    "vat_rate": "0.21",
                }
            ],
        },
    )
    assert duplicate.status_code == 409


def test_profile_password_and_logout_flow(tmp_path):
    client = client_for(tmp_path)
    register(client)

    response = client.patch(
        "/api/auth/me",
        json={
            "company_name": "Nieuwe Naam BV",
            "kvk_number": "87654321",
            "vat_number": "NL123456789B01",
            "phone": "020 123 4567",
            "notify_email": False,
            "notify_sms": True,
            "notify_backup": False,
        },
    )
    assert response.status_code == 200
    profile = response.json()
    assert profile["company_name"] == "Nieuwe Naam BV"
    assert profile["notify_sms"] is True
    assert profile["notify_email"] is False
    assert client.get("/api/auth/me").json()["phone"] == "020 123 4567"

    wrong = client.post(
        "/api/auth/password",
        json={
            "current_password": "verkeerd-wachtwoord",
            "new_password": "een-heel-nieuw-wachtwoord",
        },
    )
    assert wrong.status_code == 400

    changed = client.post(
        "/api/auth/password",
        json={
            "current_password": "correct-horse-battery-staple",
            "new_password": "een-heel-nieuw-wachtwoord",
        },
    )
    assert changed.status_code == 204

    client.post("/api/auth/logout")
    assert client.get("/api/auth/me").status_code == 401
    login = client.post(
        "/api/auth/login",
        json={"email": "sanne@example.nl", "password": "een-heel-nieuw-wachtwoord"},
    )
    assert login.status_code == 200


def test_customers_can_be_edited_and_deleted_until_they_have_invoices(tmp_path):
    client = client_for(tmp_path)
    register(client)
    customer = create_customer(client)

    updated = client.patch(
        f"/api/customers/{customer['id']}",
        json={
            "name": "Studio Veer Holding",
            "contact": "Sanne",
            "email": "administratie@studioveer.nl",
            "phone": "020 000 0000",
        },
    )
    assert updated.status_code == 200
    assert updated.json()["name"] == "Studio Veer Holding"

    assert client.delete(f"/api/customers/{customer['id']}").status_code == 204
    assert client.get("/api/customers").json() == []

    second = create_customer(client, "Met factuur")
    create_invoice(client, second["id"], "2026-003")
    blocked = client.delete(f"/api/customers/{second['id']}")
    assert blocked.status_code == 409


def test_invoice_status_and_vat_overview(tmp_path):
    client = client_for(tmp_path)
    register(client)
    customer = create_customer(client)
    invoice = create_invoice(client, customer["id"], "2026-004", "0.21")

    paid = client.patch(f"/api/invoices/{invoice['id']}", json={"status": "paid"})
    assert paid.status_code == 200
    assert paid.json()["status"] == "paid"
    assert paid.json()["paid_at"] is not None

    overview = client.get("/api/vat")
    assert overview.status_code == 200
    current = overview.json()["current"]
    assert current["net_cents"] == 20000
    assert current["vat_cents"] == 4200
    assert current["payable_cents"] == 4200
    assert current["rates"][0]["rate_bps"] == 2100
    assert {r["code"]: r["vat_cents"] for r in current["rubrics"]}["1a"] == 4200
    assert current["approved_at"] is None
    assert current["closed"] is False

    running = client.post(
        "/api/vat/approve",
        json={"year": current["year"], "quarter": current["quarter"]},
    )
    assert running.status_code == 422

    previous = overview.json()["quarters"][1]
    period = {"year": previous["year"], "quarter": previous["quarter"]}
    approved = client.post("/api/vat/approve", json=period)
    assert approved.status_code == 200
    assert approved.json()["approved_at"] is not None
    assert client.get("/api/vat").json()["quarters"][1]["approved_at"] is not None

    revoked = client.delete("/api/vat/approval", params=period)
    assert revoked.status_code == 204
    assert client.get("/api/vat").json()["quarters"][1]["approved_at"] is None


def test_receipts_can_be_uploaded_downloaded_and_deleted(tmp_path):
    client = client_for(tmp_path)
    register(client)
    png_bytes = (
        b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
        b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01"
        b"\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
    )

    response = client.post(
        "/api/receipts",
        data={
            "vendor": "Jumbo",
            "receipt_date": date.today().isoformat(),
            "amount": "18.50",
            "category": "Kantoor",
        },
        files={"file": ("bon.png", png_bytes, "image/png")},
    )
    assert response.status_code == 201, response.text
    receipt = response.json()
    assert receipt["amount_cents"] == 1850
    assert receipt["vendor"] == "Jumbo"

    listing = client.get("/api/receipts").json()
    assert len(listing) == 1
    file_response = client.get(f"/api/receipts/{receipt['id']}/file")
    assert file_response.status_code == 200
    assert file_response.headers["content-type"] == "image/png"
    assert file_response.content == png_bytes

    assert client.delete(f"/api/receipts/{receipt['id']}").status_code == 204
    assert client.get("/api/receipts").json() == []
    assert client.get(f"/api/receipts/{receipt['id']}/file").status_code == 404


def test_receipts_reject_unsupported_files(tmp_path):
    client = client_for(tmp_path)
    register(client)
    response = client.post(
        "/api/receipts",
        data={
            "vendor": "Jumbo",
            "receipt_date": date.today().isoformat(),
            "amount": "18.50",
            "category": "Kantoor",
        },
        files={"file": ("bon.txt", b"geen afbeelding", "text/plain")},
    )
    assert response.status_code == 415


def test_mutable_requests_from_foreign_origins_are_rejected(tmp_path):
    client = client_for(tmp_path)
    register(client)
    response = client.post(
        "/api/customers",
        json={"name": "CSRF"},
        headers={"Origin": "https://kwaad.example"},
    )
    assert response.status_code == 403
    assert client.get("/api/customers").json() == []


def test_legacy_database_is_migrated_without_losing_data(tmp_path):
    database = tmp_path / "legacy.sqlite3"
    connection = sqlite3.connect(database)
    connection.executescript(
        """
        CREATE TABLE users (
            id INTEGER NOT NULL PRIMARY KEY,
            email VARCHAR(320) NOT NULL UNIQUE,
            password_hash VARCHAR(200) NOT NULL,
            company_name VARCHAR(200) NOT NULL,
            kvk_number VARCHAR(8) NOT NULL,
            vat_number VARCHAR(20),
            created_at DATETIME NOT NULL
        );
        CREATE TABLE customers (
            id INTEGER NOT NULL PRIMARY KEY,
            owner_id INTEGER NOT NULL REFERENCES users(id),
            name VARCHAR(200) NOT NULL,
            contact VARCHAR(200),
            email VARCHAR(320),
            phone VARCHAR(50),
            created_at DATETIME NOT NULL
        );
        CREATE TABLE invoices (
            id INTEGER NOT NULL PRIMARY KEY,
            owner_id INTEGER NOT NULL REFERENCES users(id),
            customer_id INTEGER NOT NULL REFERENCES customers(id),
            number VARCHAR(50) NOT NULL UNIQUE,
            invoice_date DATE NOT NULL,
            due_date DATE NOT NULL,
            status VARCHAR(20) NOT NULL,
            subtotal_cents INTEGER NOT NULL,
            vat_cents INTEGER NOT NULL,
            total_cents INTEGER NOT NULL,
            created_at DATETIME NOT NULL
        );
        """
    )
    password_hash = hash_password("correct-horse-battery-staple")
    connection.execute(
        "INSERT INTO users VALUES (1, 'oud@example.nl', ?, 'Oud Bedrijf', '12345678', NULL, '2026-01-01 00:00:00')",
        (password_hash,),
    )
    connection.execute(
        "INSERT INTO customers VALUES (1, 1, 'Bestaande klant', NULL, NULL, NULL, '2026-01-01 00:00:00')"
    )
    connection.execute(
        "INSERT INTO invoices VALUES (1, 1, 1, '2026-001', '2026-01-02', '2026-01-16', 'paid', 10000, 2100, 12100, '2026-01-02 00:00:00')"
    )
    connection.commit()
    connection.close()

    client = TestClient(create_app(f"sqlite:///{database.as_posix()}"))
    login = client.post(
        "/api/auth/login",
        json={"email": "oud@example.nl", "password": "correct-horse-battery-staple"},
    )
    assert login.status_code == 200
    assert login.json()["company_name"] == "Oud Bedrijf"
    assert login.json()["notify_email"] is True
    assert login.json()["email_verified"] is True

    invoices = client.get("/api/invoices").json()
    assert len(invoices) == 1
    assert invoices[0]["number"] == "2026-001"
    assert invoices[0]["status"] == "paid"

    # The old database allowed only one global "2026-001"; after migration a
    # second account can use the same number.
    client.post("/api/auth/logout")
    register(client, "nieuw@example.nl", "Nieuw Bedrijf")
    new_customer = create_customer(client, "Nieuwe klant")
    create_invoice(client, new_customer["id"], "2026-001")

    connection = sqlite3.connect(database)
    index_columns = [
        row[2]
        for row in connection.execute("PRAGMA index_info('sqlite_autoindex_invoices_1')")
    ]
    assert index_columns == ["owner_id", "number"]

    # De upgrade naar abonnementen mag een bestaand bedrijf niet op slot zetten:
    # elk bedrijf dat de migratie tegenkomt, begint met een volle proefperiode.
    trials = connection.execute(
        "SELECT status, trial_ends_at FROM subscriptions ORDER BY company_id"
    ).fetchall()
    assert len(trials) == 2
    assert {status for status, _ in trials} == {"trialing"}
    expected = datetime.now(timezone.utc) + timedelta(days=14)
    for _, ends_at in trials:
        moment = datetime.fromisoformat(str(ends_at))
        moment = moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)
        assert abs((moment - expected).total_seconds()) < 300
    connection.close()


def test_login_provisions_company_when_membership_is_missing(tmp_path):
    database = tmp_path / "test.sqlite3"
    client = TestClient(create_app(f"sqlite:///{database.as_posix()}"))
    register(client)
    assert client.post("/api/auth/logout").status_code == 204

    connection = sqlite3.connect(database)
    connection.execute("DELETE FROM memberships")
    connection.commit()
    connection.close()

    login = client.post(
        "/api/auth/login",
        json={"email": "sanne@example.nl", "password": "correct-horse-battery-staple"},
    )
    assert login.status_code == 200, login.text
    assert login.json()["company_id"]
    me = client.get("/api/auth/me")
    assert me.status_code == 200
    assert me.json()["company_id"] == login.json()["company_id"]
