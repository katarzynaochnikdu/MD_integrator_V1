"""Tests for the Facebook webhook handler."""
from unittest.mock import AsyncMock, patch, MagicMock

from fastapi.testclient import TestClient

from app.config import settings
from app.main import app
from app.fb_client import FBLead
from app.integrations_store import FieldMapping, Integration

client = TestClient(app)


class TestWebhookVerify:
    def test_verify_success(self):
        resp = client.get("/webhook/facebook", params={
            "hub.mode": "subscribe",
            "hub.challenge": "test_challenge_123",
            "hub.verify_token": settings.fb_webhook_verify_token,
        })
        assert resp.status_code == 200
        assert resp.text == "test_challenge_123"

    def test_verify_wrong_token(self):
        resp = client.get("/webhook/facebook", params={
            "hub.mode": "subscribe",
            "hub.challenge": "test",
            "hub.verify_token": "wrong_token",
        })
        assert resp.status_code == 403

    def test_verify_missing_params(self):
        resp = client.get("/webhook/facebook")
        assert resp.status_code == 403


class TestWebhookHandler:
    LEADGEN_PAYLOAD = {
        "object": "page",
        "entry": [{
            "id": "111222333",
            "changes": [{
                "field": "leadgen",
                "value": {
                    "leadgen_id": "lead_123",
                    "form_id": "form_456",
                    "page_id": "111222333",
                }
            }]
        }]
    }

    @patch("app.webhook.submit_form_urlencoded", new_callable=AsyncMock)
    @patch("app.webhook.get_lead_data", new_callable=AsyncMock)
    @patch("app.webhook.find_by_fb_page_and_form")
    def test_processes_lead(self, mock_find, mock_lead, mock_submit):
        mock_find.return_value = Integration(
            id="int-1",
            fb_page_id="111222333",
            fb_page_name="Test Page",
            fb_page_token="token123",
            fb_form_id="form_456",
            fb_form_name="Test Form",
            fb_form_questions=[],
            medidesk_form_id="md-form-1",
            medidesk_form_name="Formularz",
            medidesk_fields=[],
            field_mappings=[
                FieldMapping(fb_field="email", medidesk_field="E-mail", confidence=0.95),
                FieldMapping(fb_field="full_name", medidesk_field="Imie-i-nazwisko", confidence=0.98),
            ],
            active=True,
        )
        mock_lead.return_value = FBLead(
            lead_id="lead_123",
            created_time="2026-03-21T10:00:00Z",
            field_data={"email": "test@klinika.pl", "full_name": "Jan Kowalski"},
        )
        from app.medidesk_client import MedideskResult
        mock_submit.return_value = MedideskResult(success=True, status_code=200)

        resp = client.post("/webhook/facebook", json=self.LEADGEN_PAYLOAD)
        assert resp.status_code == 200
        assert resp.json()["processed"] == 1

        # Verify the Medidesk submit was called with mapped fields
        mock_submit.assert_called_once()
        call_args = mock_submit.call_args
        assert call_args[0][0] == "md-form-1"  # form_id
        assert call_args[0][1]["E-mail"] == "test@klinika.pl"
        assert call_args[0][1]["Imie-i-nazwisko"] == "Jan Kowalski"

    @patch("app.webhook.find_by_fb_page_and_form")
    def test_ignores_unknown_page(self, mock_find):
        mock_find.return_value = None
        resp = client.post("/webhook/facebook", json=self.LEADGEN_PAYLOAD)
        assert resp.status_code == 200
        assert resp.json()["processed"] == 0

    def test_ignores_non_page_object(self):
        resp = client.post("/webhook/facebook", json={"object": "user", "entry": []})
        assert resp.status_code == 200
        assert resp.json()["status"] == "ignored"


class TestSetupPage:
    def test_setup_page_loads(self):
        resp = client.get("/setup")
        assert resp.status_code == 200
        assert "Połącz formularze" in resp.text

    def test_setup_page_has_steps(self):
        resp = client.get("/setup")
        assert "Facebook" in resp.text
        assert "Medidesk" in resp.text
        assert "Mapowanie" in resp.text
        assert "Gotowe" in resp.text


class TestMappingSuggestEndpoint:
    def test_suggest_returns_results(self):
        resp = client.post("/api/mapping/suggest", json={
            "fb_questions": [
                {"key": "email", "label": "Email"},
                {"key": "full_name", "label": "Full Name"},
            ],
            "medidesk_fields": [
                {"fieldId": "E-mail", "name": "E-mail"},
                {"fieldId": "Imie-i-nazwisko", "name": "Imię i Nazwisko"},
            ],
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["matched"] >= 1
        assert len(data["suggestions"]) >= 1

    def test_suggest_empty_inputs(self):
        resp = client.post("/api/mapping/suggest", json={
            "fb_questions": [],
            "medidesk_fields": [],
        })
        assert resp.status_code == 400
