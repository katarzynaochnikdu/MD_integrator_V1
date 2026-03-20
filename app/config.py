from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    medidesk_api_base: str = "https://app.medidesk.io/api/forms"
    default_site_domain: str = "twoja-domena.pl"
    default_site_url: str = "/kontakt"
    http_timeout: float = 15.0

    demo_page_enabled: bool = False
    debug_upstream: bool = False
    cors_origins: str = ""

    model_config = {"env_prefix": "MEDIDESK_"}

    @property
    def cors_origins_list(self) -> list[str]:
        if not self.cors_origins.strip():
            return []
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


settings = Settings()
