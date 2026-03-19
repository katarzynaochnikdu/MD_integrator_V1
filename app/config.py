from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    medidesk_form_url: str = (
        "https://app.medidesk.io/api/forms/d908ee01-0b7d-44a0-a494-a707ab5a55ef"
    )
    medidesk_attachments_url: str = (
        "https://app.medidesk.io/api/forms/d908ee01-0b7d-44a0-a494-a707ab5a55ef/attachments"
    )
    default_site_domain: str = "twoja-domena.pl"
    default_site_url: str = "/kontakt"
    http_timeout: float = 15.0

    # reCAPTCHA v3 site key (public) – używane na stronie demo / do wklejenia na WWW placówki
    recaptcha_site_key: str = "6Lfs81ghAAAAAL1x7coNFL3OORZHAkNk7ugPcBJ_"

    # GET /demo/contact – formularz testowy bez ręcznego kopiowania tokenu (wyłącz na produkcji jeśli niepotrzebny)
    demo_page_enabled: bool = False

    # Opcjonalnie: front na innej domenie niż API (np. strona placówki) – lista originów rozdzielona przecinkami
    cors_origins: str = ""

    model_config = {"env_prefix": "MEDIDESK_"}

    @property
    def cors_origins_list(self) -> list[str]:
        if not self.cors_origins.strip():
            return []
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


settings = Settings()
