from datetime import datetime, timedelta, timezone
from typing import Any

import bcrypt
from jose import JWTError, jwt

from app.core.config import get_settings

settings = get_settings()

# Direct bcrypt usage — passlib 1.7.4 is incompatible with bcrypt 5.x
# (silently returns False from verify), so we skip the wrapper and call
# the bcrypt library directly. Hash format is the standard $2a$/$2b$
# bcrypt format, which is byte-identical to what Node's bcryptjs writes,
# so all existing passwords in the User table continue to verify.


def _truncate_72(plain: str) -> bytes:
    """bcrypt has a 72-byte input limit. bcryptjs (Node) silently truncates;
    Python's bcrypt 4.1+ raises. We pre-truncate to stay byte-compatible
    with hashes written by bcryptjs. Returns UTF-8 bytes ready for bcrypt."""
    raw = plain.encode("utf-8")
    return raw[:72] if len(raw) > 72 else raw


def hash_password(plain: str) -> str:
    """Hash a password with bcrypt cost factor 10 — same default bcryptjs
    uses, so hashes are interchangeable across Node and Python writers."""
    return bcrypt.hashpw(_truncate_72(plain), bcrypt.gensalt(rounds=10)).decode("ascii")


def verify_password(plain: str, hashed: str) -> bool:
    """Verify a plaintext password against a stored bcrypt hash. Returns
    False on any error (malformed hash, wrong format, etc.) so a bad row
    can never crash login — the caller treats it as a wrong password."""
    if not hashed:
        return False
    try:
        return bcrypt.checkpw(_truncate_72(plain), hashed.encode("ascii"))
    except (ValueError, TypeError):
        return False


def create_access_token(subject: str, extra_claims: dict[str, Any] | None = None) -> str:
    """Mint an access token. `subject` is the user id."""
    now = datetime.now(timezone.utc)
    payload: dict[str, Any] = {
        "sub": subject,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=settings.access_token_ttl_minutes)).timestamp()),
    }
    if extra_claims:
        payload.update(extra_claims)
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def decode_token(token: str) -> dict[str, Any]:
    """Decode + validate signature/expiry. Raises JWTError on failure."""
    return jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])


class InvalidTokenError(Exception):
    """Raised by deps.get_current_user when the token can't be trusted."""


def safe_decode(token: str) -> dict[str, Any]:
    try:
        return decode_token(token)
    except JWTError as e:
        raise InvalidTokenError(str(e)) from e
