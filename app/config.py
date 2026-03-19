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

    model_config = {"env_prefix": "MEDIDESK_"}


settings = Settings()
