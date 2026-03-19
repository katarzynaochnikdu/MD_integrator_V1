from app.mapper import build_medidesk_payload
from app.schemas import ContactRequest, TopicOption


def _make_request(**overrides) -> ContactRequest:
    defaults = {
        "captchaToken": "test-token",
        "fullName": "Jan Kowalski",
        "phone": "+48500600700",
        "email": "jan@kowalski.pl",
        "topic": "Inna",
        "message": "Proszę o kontakt",
        "consent": True,
    }
    defaults.update(overrides)
    return ContactRequest.model_validate(defaults)


class TestBuildMedideskPayload:
    def test_basic_mapping(self):
        req = _make_request()
        payload = build_medidesk_payload(req)

        fv = payload["fieldsValues"]
        assert fv["Imię-i-Nazwisko"] == "Jan Kowalski"
        assert fv["Telefon"] == "+48500600700"
        assert fv["E-mail"] == "jan@kowalski.pl"
        assert fv["W-czym-możemy-pomóc-"] == "Inna"
        assert fv["Dodatkowa-informacja"] == "Proszę o kontakt"
        assert fv["Wyrażam-zgodę-na-kontakt-zwrotny-telefonicznie-lub-mailowo-"] == "true"

    def test_consent_false(self):
        req = _make_request(consent=False)
        payload = build_medidesk_payload(req)
        assert payload["fieldsValues"]["Wyrażam-zgodę-na-kontakt-zwrotny-telefonicznie-lub-mailowo-"] == "false"

    def test_email_none_becomes_empty_string(self):
        req = _make_request(email=None)
        payload = build_medidesk_payload(req)
        assert payload["fieldsValues"]["E-mail"] == ""

    def test_topic_default_is_inna(self):
        req = _make_request()
        assert req.topic == TopicOption.INNA

    def test_topic_umowienie_wizyty(self):
        req = _make_request(topic="Umówienie wizyty")
        payload = build_medidesk_payload(req)
        assert payload["fieldsValues"]["W-czym-możemy-pomóc-"] == "Umówienie wizyty"

    def test_site_domain_defaults(self):
        req = _make_request()
        payload = build_medidesk_payload(req)
        assert payload["siteDomain"] == "twoja-domena.pl"
        assert payload["siteUrl"] == "/kontakt"

    def test_site_domain_override(self):
        req = _make_request(siteDomain="moja-klinika.pl", siteUrl="/formularz")
        payload = build_medidesk_payload(req)
        assert payload["siteDomain"] == "moja-klinika.pl"
        assert payload["siteUrl"] == "/formularz"

    def test_attachments_added_when_provided(self):
        req = _make_request()
        payload = build_medidesk_payload(req, attachment_ids=["uuid-123"])
        assert payload["attachments"] == {"Dodaj-zdjęcie": ["uuid-123"]}

    def test_no_attachments_key_when_none(self):
        req = _make_request()
        payload = build_medidesk_payload(req)
        assert "attachments" not in payload
