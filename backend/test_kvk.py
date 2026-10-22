from fastapi.testclient import TestClient

from app import create_app
from config import Settings
from kvk import (
    KvkClient,
    KvkCompany,
    KvkInactive,
    KvkNotFound,
    MemoryKvk,
    company_from_bedrijfsdata,
    company_from_opendata,
    names_match,
)


def test_bedrijfsdata_picks_the_hoofdvestiging_and_city():
    company = company_from_bedrijfsdata(
        "89395808",
        {
            "status": "ok",
            "found": 2,
            "kvk": [
                {
                    "coc": "89395808",
                    "name": "Nevenvestiging",
                    "city": "Utrecht",
                    "type": "Nevenvestiging",
                    "active": 1,
                },
                {
                    "coc": "89395808",
                    "name": "Bedrijfsdata.nl B.V.",
                    "city": "Amsterdam",
                    "type": "Hoofdvestiging",
                    "active": 1,
                },
            ],
        },
    )
    assert company.name == "Bedrijfsdata.nl B.V."
    assert company.city == "Amsterdam"
    assert company.verified is True
    assert "Nevenvestiging" in company.names


def test_bedrijfsdata_rejects_inactive_and_unknown_numbers():
    try:
        company_from_bedrijfsdata(
            "11111111",
            {"status": "ok", "found": 0, "kvk": []},
        )
    except KvkNotFound:
        pass
    else:
        raise AssertionError("unknown number should be rejected")
    try:
        company_from_bedrijfsdata(
            "11111111",
            {"kvk": [{"name": "Was", "city": "X", "type": "Hoofdvestiging", "active": 0}]},
        )
    except KvkInactive:
        return
    raise AssertionError("inactive company should be rejected")


def test_opendata_accepts_an_active_bv_without_a_public_name():
    company = company_from_opendata(
        "24330087",
        {"actief": "J", "rechtsvormCode": "BV", "postcodeRegio": 30},
    )
    assert company.match_any_name is True
    assert company.kvk_number == "24330087"


def test_opendata_rejects_an_inactive_registration():
    try:
        company_from_opendata("24330087", {"actief": "N"})
    except KvkInactive:
        return
    raise AssertionError("inactive company should be rejected")


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


class _NoDirectory(KvkClient):
    def lookup(self, kvk_number):
        raise KvkNotFound()


def test_without_kvk_api_key_an_unknown_number_can_still_register(tmp_path):
    database = tmp_path / "soft.sqlite3"
    settings = Settings(
        env="development",
        secret_key="s" * 48,
        database_url=f"sqlite:///{database.as_posix()}",
        upload_dir=tmp_path / "uploads",
    )
    kvk = _NoDirectory(api_key="x", base_url="https://example.invalid", strict=False)
    client = TestClient(create_app(settings=settings, kvk=kvk, upload_dir=tmp_path / "uploads"))
    response = client.post(
        "/api/auth/register",
        json={
            "email": "zzp@example.nl",
            "password": "correct-horse-battery-staple",
            "company_name": "Ahmad Klussen",
            "kvk_number": "87654321",
            "terms": True,
        },
    )
    assert response.status_code == 201, response.text
    assert response.json()["company_name"] == "Ahmad Klussen"
    assert response.json()["kvk_number"] == "87654321"


def test_lookup_without_api_key_lets_unknown_numbers_continue(tmp_path):
    database = tmp_path / "soft-lookup.sqlite3"
    settings = Settings(
        env="development",
        secret_key="s" * 48,
        database_url=f"sqlite:///{database.as_posix()}",
        upload_dir=tmp_path / "uploads",
    )
    kvk = _NoDirectory(api_key="x", base_url="https://example.invalid", strict=False)
    client = TestClient(create_app(settings=settings, kvk=kvk, upload_dir=tmp_path / "uploads"))
    response = client.get("/api/kvk/lookup", params={"kvk_number": "87654321"})
    assert response.status_code == 200
    assert response.json()["verified"] is False
    assert response.json()["kvk_number"] == "87654321"
