from pydantic import BaseModel, EmailStr, Field


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class UserOut(BaseModel):
    id: str
    email: EmailStr
    name: str
    role: str
    plantId: str | None = None
    designation: str | None = None
    department: str | None = None


class LoginResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: UserOut
    refresh_token: str | None = None


class PermissionsResponse(BaseModel):
    permissions: dict[str, bool]


# --- Refresh / password-reset / device endpoints used by the mobile app. ---
# These are currently stubs: refresh re-mints from the bearer, OTPs are not
# emailed, devices aren't persisted. See BACKEND_TODO.md in the mobile app
# for the full production contract.


class RefreshRequest(BaseModel):
    refresh_token: str | None = None


class RefreshResponse(BaseModel):
    access_token: str
    refresh_token: str | None = None


class ForgotPasswordRequest(BaseModel):
    email: EmailStr


class ForgotPasswordResponse(BaseModel):
    ok: bool = True
    # In dev, surface the OTP so QA can complete the flow without an
    # SMTP/SMS gateway. In production this field is None and the OTP is
    # sent out-of-band.
    dev_otp: str | None = None


class VerifyOtpRequest(BaseModel):
    email: EmailStr
    otp: str = Field(min_length=4, max_length=8)


class VerifyOtpResponse(BaseModel):
    resetToken: str


class ResetPasswordRequest(BaseModel):
    resetToken: str
    newPassword: str = Field(min_length=8, max_length=128)


class ResetPasswordResponse(BaseModel):
    ok: bool = True


class DeviceRegisterRequest(BaseModel):
    token: str
    platform: str = Field(pattern="^(ios|android|web)$")
    app_version: str | None = None


class DeviceRegisterResponse(BaseModel):
    id: str
    ok: bool = True
