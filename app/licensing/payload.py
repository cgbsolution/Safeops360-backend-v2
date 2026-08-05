"""Licence payload (signed claims) + runtime state types.

`LicencePayload` is the schema of the JWS payload — what the Authority signs
and the validator parses after the signature checks out. Claim keys match the
build prompt §3.3 exactly (camelCase custom claims + standard JWT claims) so
the signed JSON is stable and human-readable.

`RuntimeLicenceState` is the in-app, post-validation view used by the
enforcement layer and the admin status API.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

LicenceType = Literal["POC", "SUBSCRIPTION", "PERPETUAL"]
DeploymentMode = Literal["ON_PREM", "CLOUD"]
BindingMode = Literal["SOFT", "STRICT"]

LicenceStatus = Literal[
    "ACTIVE",          # normal operation
    "EXPIRING_SOON",   # within the warn window before exp
    "GRACE",           # past exp, within gracePeriodDays
    "EXPIRED_LOCKED",  # past grace — restricted to licence/export/upload
    "INVALID",         # signature / structure / binding(strict) failure
    "MISSING",         # no .lic file present
]


class LicenceLimits(BaseModel):
    """Hard caps enforced at create paths. Absent (None) = unlimited."""
    model_config = ConfigDict(populate_by_name=True, extra="allow")

    max_sites: int | None = Field(default=None, alias="maxSites")
    max_users: int | None = Field(default=None, alias="maxUsers")
    max_factories: int | None = Field(default=None, alias="maxFactories")


class LicencePayload(BaseModel):
    """Parsed, signature-verified licence claims."""
    model_config = ConfigDict(populate_by_name=True, extra="allow")

    # standard JWT claims
    iss: str
    sub: str
    jti: str
    iat: int
    nbf: int
    exp: int
    # entitlements
    customer_name: str = Field(alias="customerName")
    edition: str
    enabled_modules: list[str] = Field(alias="enabledModules")
    limits: LicenceLimits = Field(default_factory=LicenceLimits)
    licence_type: LicenceType = Field(alias="licenceType")
    grace_period_days: int = Field(default=0, alias="gracePeriodDays")
    installation_binding: str | None = Field(default=None, alias="installationBinding")
    binding_mode: BindingMode = Field(default="SOFT", alias="bindingMode")
    feature_flags: dict[str, bool] = Field(default_factory=dict, alias="featureFlags")
    deployment_mode: DeploymentMode = Field(default="ON_PREM", alias="deploymentMode")

    @property
    def valid_from(self) -> datetime:
        return datetime.fromtimestamp(self.nbf, tz=timezone.utc)

    @property
    def valid_until(self) -> datetime:
        return datetime.fromtimestamp(self.exp, tz=timezone.utc)

    @property
    def issued_at(self) -> datetime:
        return datetime.fromtimestamp(self.iat, tz=timezone.utc)


@dataclass
class RuntimeLicenceState:
    """In-app, post-validation snapshot. Built by the validator, read by the
    enforcement layer and the admin status API. NEVER fabricated to grant
    access — the default/empty state is a locked one."""

    status: LicenceStatus
    last_validated_at: datetime
    payload: LicencePayload | None = None
    days_to_expiry: int | None = None
    enabled_module_set: frozenset[str] = field(default_factory=frozenset)
    validation_error: str | None = None  # admin-only diagnostic
    effective_clock: datetime | None = None
    clock_tamper_warning: bool = False
    binding_warning: bool = False

    # The set of statuses under which operational modules remain reachable.
    # Anything else is the restricted lock (licence/export/upload only).
    _OPERATIONAL = frozenset({"ACTIVE", "EXPIRING_SOON", "GRACE"})

    @property
    def is_operational(self) -> bool:
        """True when modules can run. False locks the app to the licence
        screen — but core modules stay reachable regardless (see
        enforcement.is_module_enabled)."""
        return self.status in self._OPERATIONAL

    @property
    def is_locked(self) -> bool:
        return not self.is_operational

    def has_module(self, code: str) -> bool:
        return code in self.enabled_module_set

    @classmethod
    def locked(cls, status: LicenceStatus, *, now: datetime,
               error: str | None = None) -> "RuntimeLicenceState":
        """A fail-closed state with NO enabled product modules. Core modules
        are layered on by the enforcement helpers, not here, so even a totally
        invalid licence leaves identity/RBAC/licensing reachable (TL-14)."""
        return cls(
            status=status,
            last_validated_at=now,
            enabled_module_set=frozenset(),
            validation_error=error,
        )
