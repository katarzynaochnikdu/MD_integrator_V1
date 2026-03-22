from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    medidesk_api_base: str = "https://app.medidesk.io/api/forms"
    default_site_domain: str = "twoja-domena.pl"
    default_site_url: str = "/kontakt"
    http_timeout: float = 15.0

    demo_page_enabled: bool = False
    debug_upstream: bool = False
    cors_origins: str = ""

    # Facebook OAuth & Graph API
    fb_app_id: str = ""
    fb_app_secret: str = ""
    fb_redirect_uri: str = "http://localhost:8000/auth/facebook/callback"
    fb_graph_version: str = "v25.0"
    fb_webhook_verify_token: str = "medidesk_integrator_verify_2026"

    # Integration storage
    integrations_file: str = "integrations.json"
    lead_log_file: str = "lead_log.json"

    # Session
    fb_session_secret: str = "medidesk-session-secret-change-me-2026"
    admin_password: str = "medidesk-admin-2026"

    # Encryption (Fernet) — generate with: python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
    encryption_key: str = ""

    model_config = {"env_prefix": "MEDIDESK_"}

    @property
    def cors_origins_list(self) -> list[str]:
        if not self.cors_origins.strip():
            return []
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


settings = Settings()
