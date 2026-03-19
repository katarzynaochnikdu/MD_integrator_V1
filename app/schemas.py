from __future__ import annotations

from enum import Enum
from typing import Optional

from pydantic import BaseModel, EmailStr, Field


class TopicOption(str, Enum):
    UMOWIENIE_WIZYTY = "Umówienie wizyty"
    UMOWIENIE_BADAN_USG = "Umówienie badań - USG"
    ODWOLANIE_WIZYTY = "Odwołanie wizyty"
    ZMIANA_TERMINU = "Zmiana terminu"
    INNA = "Inna"


# --- Request models ---


class ContactRequest(BaseModel):
    captcha_token: str = Field(..., alias="captchaToken", min_length=1)
    full_name: str = Field(..., alias="fullName", min_length=1, max_length=200)
    phone: str = Field(..., min_length=5, max_length=20)
    email: Optional[str] = Field(default=None, max_length=254)
    topic: TopicOption = Field(default=TopicOption.INNA)
    message: str = Field(..., min_length=1, max_length=5000)
    consent: bool = Field(default=True)

    site_domain: Optional[str] = Field(default=None, alias="siteDomain")
    site_url: Optional[str] = Field(default=None, alias="siteUrl")

    model_config = {"populate_by_name": True}


# --- Response models ---


class SuccessResponse(BaseModel):
    status: str = "ok"


class FieldError(BaseModel):
    code: str
    params: list = Field(default_factory=list)


class ValidationErrorResponse(BaseModel):
    status: str = "validation_error"
    global_errors: list[FieldError] = Field(default_factory=list, alias="globalErrors")
    field_errors: dict[str, list[FieldError]] = Field(
        default_factory=dict, alias="fieldErrors"
    )

    model_config = {"populate_by_name": True}


class CaptchaErrorResponse(BaseModel):
    status: str = "captcha_invalid"
    message: str = "Nieprawidłowy token reCAPTCHA"


class UpstreamErrorResponse(BaseModel):
    status: str = "upstream_error"
    message: str
