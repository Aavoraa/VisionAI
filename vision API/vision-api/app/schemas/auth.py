"""Dashboard account schemas (Pydantic V2)."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.core.security import PASSWORD_MAX_LENGTH, PASSWORD_MIN_LENGTH, normalize_email, validate_password
from app.schemas.common import UTCDateTime

_EXAMPLE_USER = {
    "id": "5f1d2c3b-4a59-4e6f-8a7b-9c0d1e2f3a4b",
    "email": "ada@example.com",
    "role": "user",
    "is_active": True,
    "created_at": "2026-09-24T09:30:00Z",
    "last_login_at": "2026-09-24T09:30:00Z",
}


class _Credentials(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: str = Field(max_length=320, description="Account email (case-insensitive).", examples=["ada@example.com"])
    password: str = Field(
        min_length=PASSWORD_MIN_LENGTH,
        max_length=PASSWORD_MAX_LENGTH,
        description=f"{PASSWORD_MIN_LENGTH}-{PASSWORD_MAX_LENGTH} characters. Passphrases are encouraged.",
        examples=["correct horse battery staple"],
    )

    @field_validator("email")
    @classmethod
    def _normalize_email(cls, value: str) -> str:
        return normalize_email(value)


class SignupRequest(_Credentials):
    @field_validator("password")
    @classmethod
    def _check_password(cls, value: str) -> str:
        return validate_password(value)


class LoginRequest(_Credentials):
    # No strength check on login: only the stored hash decides.
    password: str = Field(min_length=1, max_length=PASSWORD_MAX_LENGTH, examples=["correct horse battery staple"])


class PasswordChangeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    current_password: str = Field(min_length=1, max_length=PASSWORD_MAX_LENGTH, description="The password in use now.")
    new_password: str = Field(
        min_length=PASSWORD_MIN_LENGTH,
        max_length=PASSWORD_MAX_LENGTH,
        description=f"The new password, {PASSWORD_MIN_LENGTH}-{PASSWORD_MAX_LENGTH} characters.",
    )

    @field_validator("new_password")
    @classmethod
    def _check_password(cls, value: str) -> str:
        return validate_password(value)


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True, json_schema_extra={"examples": [_EXAMPLE_USER]})

    id: str = Field(description="User id (UUID).")
    email: str = Field(description="Account email.")
    role: Literal["user", "admin"] = Field(description="`admin` can manage every account; `user` only their own.")
    is_active: bool = Field(description="Inactive accounts cannot sign in or use their API keys.")
    created_at: UTCDateTime = Field(description="Account creation time (UTC).")
    last_login_at: UTCDateTime | None = Field(default=None, description="Most recent successful sign-in (UTC).")


class AuthResponse(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={"examples": [{"user": _EXAMPLE_USER, "csrf_token": "q3VtY2hhbmdlLXRoaXMtdG9rZW4tZXhhbXBsZQ"}]}
    )

    user: UserOut
    csrf_token: str = Field(
        description=(
            "Send as the `X-CSRF-Token` header on state-changing dashboard requests. "
            "Also set as a readable cookie so browser code can pick it up after a reload."
        )
    )
