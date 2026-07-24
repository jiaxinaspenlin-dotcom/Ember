"""Authentication request/response schemas."""

from __future__ import annotations

from pydantic import BaseModel, EmailStr, Field

from app.schemas.common import CurrentUserOut


class SignupRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=10, max_length=200)
    display_name: str = Field(min_length=2, max_length=120)


class LoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=200)


class ChangePasswordRequest(BaseModel):
    current_password: str = Field(min_length=1, max_length=200)
    new_password: str = Field(min_length=10, max_length=200)


class SetPasswordRequest(BaseModel):
    new_password: str = Field(min_length=10, max_length=200)


class ForgotPasswordRequest(BaseModel):
    email: EmailStr


class ResetPasswordRequest(BaseModel):
    token: str = Field(min_length=8, max_length=256)
    new_password: str = Field(min_length=10, max_length=200)


class VerifyEmailRequest(BaseModel):
    token: str = Field(min_length=8, max_length=256)


class NeutralResponse(BaseModel):
    """A response that is identical whether or not the address exists.

    ``delivered`` reports whether mail actually left the machine, so a developer
    running the console backend is never misled -- it says nothing about whether
    the address is registered.
    """

    ok: bool = True
    message: str
    delivered: bool = False


class SignupResponse(BaseModel):
    """Signup result.

    With verification required there is no session and no user payload: the
    response is deliberately identical for a new and an existing address.
    """

    user: CurrentUserOut | None = None
    authenticated: bool = False
    verification_required: bool = False
    message: str


class SessionResponse(BaseModel):
    """Returned after a successful sign-in.

    Contains no session token: the session lives in an HTTP-only cookie.
    """

    user: CurrentUserOut
    authenticated: bool = True


class AuthStatusResponse(BaseModel):
    authenticated: bool
    user: CurrentUserOut | None = None
    github_enabled: bool = False
