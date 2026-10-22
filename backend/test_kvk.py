from fastapi.testclient import TestClient

from app import create_app
from config import Settings
from kvk import KvkCompany, MemoryKvk, names_match


def test_company_names_match_trade_name_and_legal_form():
    assert names_match("Test BV Donald", ["Test BV Donald"])
    assert names_match("test bv donald", ["Test B.V. Donald"])
    assert not names_match("Ander Bedrijf", ["Test BV Donald"])


def kvk_app(tmp_path, records, **settings_extra):
    database = tmp_path / "kvk.sqlite3"
    settings = Settings(
        env="development",
        secret_key="s" * 48,
        database_url=f"sqlite:///{database.as_posix()}",
        public_url=settings_extra.get("public_url", "http://127.0.0.1:8011"),
        allowed_hosts=settings_extra.get("allowed_hosts", ()),
        upload_dir=tmp_path / "uploads",
    )
    kvk = MemoryKvk(records)
    return TestClient(create_app(settings=settings, kvk=kvk, upload_dir=tmp_path / "uploads"))


DONALD = KvkCompany(
    kvk_number="68750110",
    name="Test BV Donald",
    names=["Test BV Donald", "Test BV Donald Nevenvestiging"],
    city="Lollum",
    street="Hizzaarderlaan",
)


def payload(**overrides):
    data = {
        "email": "donald@example.nl",
        "password": "correct-horse-battery-staple",
        "company_name": "Test BV Donald",
        "kvk_number": "68750110",
        "terms": True,
    }
    data.update(overrides)
    return data


def test_register_rejects_unknown_kvk_number(tmp_path):
    client = kvk_app(tmp_path, {"68750110": DONALD})
    response = client.post("/api/auth/register", json=payload(kvk_number="11111111"))
    assert response.status_code == 422
    assert "Handelsregister" in response.json()["detail"]


def test_register_rejects_name_that_does_not_belong_to_kvk(tmp_path):
    client = kvk_app(tmp_path, {"68750110": DONALD})
    response = client.post("/api/auth/register", json=payload(company_name="Iets Anders BV"))
    assert response.status_code == 422
    assert "Test BV Donald" in response.json()["detail"]


def test_register_accepts_a_company_that_exists_at_kvk(tmp_path):
    client = kvk_app(tmp_path, {"68750110": DONALD})
    response = client.post("/api/auth/register", json=payload())
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["company_name"] == "Test BV Donald"
    assert body["city"] == "Lollum"


def test_lookup_returns_the_official_name(tmp_path):
    client = kvk_app(tmp_path, {"68750110": DONALD})
    response = client.get("/api/kvk/lookup", params={"kvk_number": "6875.0110"})
    assert response.status_code == 200
    assert response.json()["name"] == "Test BV Donald"
    assert response.json()["city"] == "Lollum"


def test_signup_from_the_public_host_is_not_blocked_by_a_localhost_proxy(tmp_path):
    client = kvk_app(
        tmp_path,
        {"68750110": DONALD},
        public_url="https://app.example.nl",
    )
    response = client.post(
        "/api/auth/register",
        json=payload(),
        headers={"Origin": "https://app.example.nl", "Host": "127.0.0.1:8000"},
    )
    assert response.status_code == 201, response.text


def test_signup_from_a_foreign_origin_is_rejected(tmp_path):
    client = kvk_app(tmp_path, {"68750110": DONALD})
    response = client.post(
        "/api/auth/register",
        json=payload(),
        headers={"Origin": "https://evil.example", "Host": "testserver"},
    )
    assert response.status_code == 403
    assert response.json()["detail"] == "Ongeldige aanvraag"
