from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.main import app
from app.medidesk_client import MedideskResult

client = TestClient(app)


@pytest.fixture(autouse=True)
def disable_auto_placeholder_photo(monkeypatch):
    """Testy nie wywołują prawdziwego uploadu do Medidesk."""
    monkeypatch.setattr(settings, "auto_placeholder_photo", False)

VALID_PAYLOAD = {
    "captchaToken": "valid-token",
    "fullName": "Jan Kowalski",
    "phone": "+48500600700",
    "email": "jan@kowalski.pl",
    "topic": "Inna",
    "message": "Proszę o kontakt",
    "consent": True,
}


def test_root_returns_200():
    resp = client.get("/")
    assert resp.status_code == 200
    data = resp.json()
    assert "docs" in data
    assert data["docs"] == "/docs"


class TestSubmitContact:
    @patch("app.main.submit_form", new_callable=AsyncMock)
    def test_success(self, mock_submit):
        mock_submit.return_value = MedideskResult(success=True, status_code=200)

        resp = client.post("/api/medidesk/contact", json=VALID_PAYLOAD)

        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"
        mock_submit.assert_called_once()

    @patch("app.main.submit_form", new_callable=AsyncMock)
    def test_captcha_invalid(self, mock_submit):
        mock_submit.return_value = MedideskResult(
            success=False, status_code=401, body=None
        )

        resp = client.post("/api/medidesk/contact", json=VALID_PAYLOAD)

        assert resp.status_code == 401
        assert resp.json()["status"] == "captcha_invalid"

    @patch("app.main.submit_form", new_callable=AsyncMock)
    def test_validation_error(self, mock_submit):
        mock_submit.return_value = MedideskResult(
            success=False,
            status_code=400,
            body={
                "globalErrors": [],
                "fieldErrors": {
                    "Telefon": [{"code": "format", "params": []}],
                },
            },
        )

        resp = client.post("/api/medidesk/contact", json=VALID_PAYLOAD)

        assert resp.status_code == 400
        data = resp.json()
        assert data["status"] == "validation_error"
        assert "Telefon" in data["fieldErrors"]

    @patch("app.main.submit_form", new_callable=AsyncMock)
    def test_upstream_timeout(self, mock_submit):
        mock_submit.return_value = MedideskResult(success=False, status_code=504)

        resp = client.post("/api/medidesk/contact", json=VALID_PAYLOAD)

        assert resp.status_code == 504
        assert resp.json()["status"] == "upstream_error"

    @patch("app.main.submit_form", new_callable=AsyncMock)
    def test_upstream_generic_error(self, mock_submit):
        mock_submit.return_value = MedideskResult(
            success=False, status_code=500, body=None
        )

        resp = client.post("/api/medidesk/contact", json=VALID_PAYLOAD)

        assert resp.status_code == 502
        assert resp.json()["status"] == "upstream_error"

    def test_missing_required_field(self):
        payload = VALID_PAYLOAD.copy()
        del payload["fullName"]

        resp = client.post("/api/medidesk/contact", json=payload)
        assert resp.status_code == 422

    def test_invalid_topic(self):
        payload = {**VALID_PAYLOAD, "topic": "Nieistniejąca opcja"}

        resp = client.post("/api/medidesk/contact", json=payload)
        assert resp.status_code == 422

    @patch("app.main.submit_form", new_callable=AsyncMock)
    def test_captcha_header_passed(self, mock_submit):
        mock_submit.return_value = MedideskResult(success=True, status_code=200)

        client.post("/api/medidesk/contact", json=VALID_PAYLOAD)

        args, kwargs = mock_submit.call_args
        captcha_value = kwargs.get("captcha_token") or args[1]
        assert captcha_value == "valid-token"


class TestDemoContactPage:
    def test_demo_disabled_by_default(self):
        resp = client.get("/demo/contact")
        assert resp.status_code == 404

    def test_demo_served_when_enabled(self, monkeypatch):
        monkeypatch.setattr(settings, "demo_page_enabled", True)
        resp = client.get("/demo/contact")
        assert resp.status_code == 200
        assert "grecaptcha" in resp.text
        assert settings.recaptcha_site_key in resp.text


class TestPlaceholderAttachment:
    @patch("app.main.get_placeholder_attachment_id", new_callable=AsyncMock)
    @patch("app.main.submit_form", new_callable=AsyncMock)
    def test_payload_includes_attachment_when_placeholder_ok(
        self, mock_submit, mock_placeholder, monkeypatch
    ):
        monkeypatch.setattr(settings, "auto_placeholder_photo", True)
        mock_placeholder.return_value = "uuid-placeholder-123"
        mock_submit.return_value = MedideskResult(success=True, status_code=200)

        client.post("/api/medidesk/contact", json=VALID_PAYLOAD)

        args, _kwargs = mock_submit.call_args
        payload = args[0]
        assert payload["attachments"] == {"Dodaj-zdjęcie": ["uuid-placeholder-123"]}
