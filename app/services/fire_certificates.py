"""Expiry tiers for AMC contracts and fire certificates — spec §4.4 and §5.6.

Two things share one engine here because they are the same problem: a dated
document, a ladder of reminders before it dies, and an escalation when nobody
acted. Writing that twice guarantees the two ladders drift.

**Where site-level certificates live.** Fire NOC and PESO licence are NOT stored
by this module. `factory_ext.RegulatoryRegistration` already holds statutory
site registrations — including a `FIRE_LICENSE` type — with expiry dates,
renewal tracking and the canonical `legalObligationId` that makes the Statutory
Register the single source of truth. Spec §6 says certificates "sync into the
existing register rather than being a second source of truth", so this module
*reads and tiers* those rows rather than copying them. What it owns outright is
`FireAssetCertificate`: per-cylinder hydrostatic tests, which cannot live in a
FactoryProfile-scoped table at all.

**Idempotency.** The nightly job runs every night; a document sitting 45 days
from expiry is inside the 90 and 60 tiers on all of those nights. Firing on
"inside a tier" would send the 90-day reminder 30 nights running until people
filter the alerts. `lastReminderTierSent` records the tightest tier already sent,
so each tier fires exactly once and re-entry is impossible without a reset.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.factory_ext import RegulatoryRegistration
from app.models.fire_safety import FireAmcContract, FireAssetCertificate, FireEquipment

# Spec §4.4 / §5.6 default ladder. Overridable per contract, per certificate and
# per registration — hence stored as data on each row, with this only as the
# fallback when a row declares none.
DEFAULT_TIERS = [90, 60, 30, 7]

# Statutory registration types this module tiers. Others in the register belong
# to their own modules and are left alone.
FIRE_REGISTRATION_TYPES = ("FIRE_LICENSE", "FIRE_NOC", "PESO_LICENSE", "BUILDING_CERT")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(d: datetime | None) -> datetime | None:
    if d is None:
        return None
    return d.replace(tzinfo=timezone.utc) if d.tzinfo is None else d


def tiers_for(configured: list | None) -> list[int]:
    """Normalise a configured ladder: ints only, descending, deduped.

    Descending order is load-bearing — `due_tier` walks it and returns the first
    tier the document has entered, which is only the *tightest* applicable tier
    if the list is sorted. An unsorted config row would silently under-escalate.
    """
    vals = {int(t) for t in (configured or []) if isinstance(t, (int, float)) and int(t) > 0}
    return sorted(vals, reverse=True) if vals else list(DEFAULT_TIERS)


def days_remaining(expiry: datetime | None, now: datetime | None = None) -> int | None:
    if expiry is None:
        return None
    return (_aware(expiry) - (now or _now())).days


def due_tier(expiry: datetime | None, tiers: list[int], last_sent: int | None, now: datetime | None = None) -> int | None:
    """The tier that should fire tonight, or None.

    Returns the tightest tier the document has entered but not yet been notified
    about. Skipping matters: a contract created 20 days before expiry has blown
    through 90 and 60 without a reminder ever being appropriate, and should get
    the 30-day notice, not three notices at once.
    """
    remaining = days_remaining(expiry, now)
    if remaining is None:
        return None
    entered = [t for t in tiers if remaining <= t]
    if not entered:
        return None
    tightest = min(entered)
    if last_sent is not None and tightest >= last_sent:
        return None  # already notified at this tier or a tighter one
    return tightest


def status_for(expiry: datetime | None, tiers: list[int], now: datetime | None = None) -> str:
    """VALID / EXPIRING_SOON / EXPIRED. Computed, never entered (spec §5.5's
    principle applied to documents: a status a human types is a status that goes
    stale the day after they type it)."""
    remaining = days_remaining(expiry, now)
    if remaining is None:
        return "VALID"  # no expiry recorded — perpetual, not expired
    if remaining < 0:
        return "EXPIRED"
    if remaining <= max(tiers or DEFAULT_TIERS):
        return "EXPIRING_SOON"
    return "VALID"


@dataclass
class ExpiryEvent:
    """One reminder the nightly job decided to raise."""

    kind: str  # AMC | ASSET_CERT | STATUTORY_REGISTRATION
    recordId: str
    plantId: str | None
    label: str
    expiryDate: datetime | None
    daysRemaining: int | None
    tier: int
    escalate: bool
    status: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "recordId": self.recordId,
            "plantId": self.plantId,
            "label": self.label,
            "expiryDate": self.expiryDate.isoformat() if self.expiryDate else None,
            "daysRemaining": self.daysRemaining,
            "tier": self.tier,
            "escalate": self.escalate,
            "status": self.status,
        }


# ── AMC contracts ────────────────────────────────────────────────────────────
async def sweep_amc_contracts(
    db: AsyncSession, plant_id: str | None = None, now: datetime | None = None
) -> dict[str, Any]:
    """Recompute AMC status, fire due reminders, escalate inside the final tier.

    Spec §4.4's last clause — "contract lapse flips linked assets' AMC-coverage
    flag (informational, does not block asset compliance status)" — is honoured by
    what this function does NOT do: it never touches `FireEquipment.status`.
    Coverage is derived on read from the contract's status, so there is no
    denormalised flag to fall out of sync.
    """
    now = now or _now()
    stmt = select(FireAmcContract).where(FireAmcContract.isDeleted.is_(False))
    if plant_id:
        stmt = stmt.where(FireAmcContract.plantId == plant_id)
    contracts = (await db.execute(stmt)).scalars().all()

    events: list[ExpiryEvent] = []
    lapsed = 0
    for c in contracts:
        if c.status in ("RENEWED", "CANCELLED"):
            continue
        tiers = tiers_for(c.renewalReminderDays)
        new_status = status_for(c.endDate, tiers, now)
        # A contract past its end date is LAPSED, not merely EXPIRED — the word
        # the AMC screens and the vendor portal already use.
        if new_status == "EXPIRED":
            new_status = "LAPSED"
            lapsed += 1
        c.status = new_status

        tier = due_tier(c.endDate, tiers, c.lastReminderTierSent, now)
        if tier is not None:
            c.lastReminderTierSent = tier
            # Escalation to the Facility Manager happens inside the final tier —
            # the point at which reminders have demonstrably not worked.
            escalate = tier <= min(tiers)
            if escalate and c.escalatedAt is None:
                c.escalatedAt = now
            events.append(
                ExpiryEvent(
                    kind="AMC",
                    recordId=c.id,
                    plantId=c.plantId,
                    label=f"{c.contractCode} — {c.vendorName}",
                    expiryDate=c.endDate,
                    daysRemaining=days_remaining(c.endDate, now),
                    tier=tier,
                    escalate=escalate,
                    status=c.status,
                )
            )
    await db.flush()
    return {
        "evaluated": len(contracts),
        "lapsed": lapsed,
        "reminders": [e.as_dict() for e in events],
    }


async def amc_coverage(db: AsyncSession, asset_ids: list[str]) -> dict[str, dict[str, Any]]:
    """AMC coverage per asset, derived on read.

    Informational only — nothing here feeds compliance status. Returned for every
    requested asset, including those with no contract, so callers do not have to
    distinguish "no coverage" from "asset not in the result".
    """
    if not asset_ids:
        return {}
    assets = (
        await db.execute(select(FireEquipment).where(FireEquipment.id.in_(asset_ids)))
    ).scalars().all()
    contract_ids = {a.amcContractId for a in assets if a.amcContractId}
    contracts = (
        {
            c.id: c
            for c in (
                await db.execute(select(FireAmcContract).where(FireAmcContract.id.in_(contract_ids)))
            ).scalars().all()
        }
        if contract_ids
        else {}
    )
    out: dict[str, dict[str, Any]] = {}
    for a in assets:
        c = contracts.get(a.amcContractId) if a.amcContractId else None
        out[a.id] = {
            "covered": bool(c and c.status in ("ACTIVE", "EXPIRING_SOON")),
            "contractId": c.id if c else None,
            "contractCode": c.contractCode if c else None,
            "vendorName": c.vendorName if c else None,
            "status": c.status if c else "NO_CONTRACT",
            "endDate": c.endDate.isoformat() if c and c.endDate else None,
            # Stated explicitly so no consumer has to guess whether to gate on it.
            "affectsComplianceStatus": False,
        }
    return out


# ── Asset-level certificates ─────────────────────────────────────────────────
async def sweep_asset_certificates(
    db: AsyncSession, plant_id: str | None = None, now: datetime | None = None
) -> dict[str, Any]:
    now = now or _now()
    stmt = select(FireAssetCertificate).where(FireAssetCertificate.isDeleted.is_(False))
    if plant_id:
        stmt = stmt.where(FireAssetCertificate.plantId == plant_id)
    certs = (await db.execute(stmt)).scalars().all()

    events: list[ExpiryEvent] = []
    expired = 0
    for cert in certs:
        tiers = tiers_for(cert.escalationTierDays)
        cert.status = status_for(cert.expiryDate, tiers, now)
        if cert.status == "EXPIRED":
            expired += 1
        tier = due_tier(cert.expiryDate, tiers, cert.lastReminderTierSent, now)
        if tier is not None:
            cert.lastReminderTierSent = tier
            events.append(
                ExpiryEvent(
                    kind="ASSET_CERT",
                    recordId=cert.id,
                    plantId=cert.plantId,
                    label=f"{cert.certificateType} — {cert.certificateNo or cert.id[:8]}",
                    expiryDate=cert.expiryDate,
                    daysRemaining=days_remaining(cert.expiryDate, now),
                    tier=tier,
                    escalate=tier <= min(tiers),
                    status=cert.status,
                )
            )
    await db.flush()
    return {"evaluated": len(certs), "expired": expired, "reminders": [e.as_dict() for e in events]}


# ── Site-level statutory registrations (read the existing register) ──────────
async def sweep_statutory_registrations(
    db: AsyncSession, plant_id: str | None = None, now: datetime | None = None
) -> dict[str, Any]:
    """Tier the fire-relevant rows of the existing Statutory Register.

    This does not own the data and does not copy it. It sets the tier columns
    added by `apply-firelifesafety-ddl.ts` and returns the reminders due — the
    register itself remains the source of truth, and its own status computation
    in `services/factory_ext.py` is left alone.
    """
    now = now or _now()
    stmt = (
        select(RegulatoryRegistration)
        .where(RegulatoryRegistration.isDeleted.is_(False))
        .where(RegulatoryRegistration.registrationType.in_(FIRE_REGISTRATION_TYPES))
    )
    if plant_id:
        stmt = stmt.where(RegulatoryRegistration.siteId == plant_id)
    regs = (await db.execute(stmt)).scalars().all()

    events: list[ExpiryEvent] = []
    for r in regs:
        # `alertThresholdDays` is the register's own single-tier setting; honour
        # it as the widest tier when no explicit ladder is configured, so this
        # module never quietly narrows a warning window someone already chose.
        configured = list(getattr(r, "escalationTierDays", None) or [])
        if not configured and r.alertThresholdDays:
            configured = sorted({r.alertThresholdDays, *DEFAULT_TIERS}, reverse=True)
        tiers = tiers_for(configured)
        tier = due_tier(r.expiryDate, tiers, getattr(r, "lastReminderTierSent", None), now)
        if tier is not None:
            r.lastReminderTierSent = tier
            events.append(
                ExpiryEvent(
                    kind="STATUTORY_REGISTRATION",
                    recordId=r.id,
                    plantId=r.siteId,
                    label=f"{r.registrationType} — {r.registrationName}",
                    expiryDate=r.expiryDate,
                    daysRemaining=days_remaining(r.expiryDate, now),
                    tier=tier,
                    escalate=tier <= min(tiers),
                    status=status_for(r.expiryDate, tiers, now),
                )
            )
    await db.flush()
    return {"evaluated": len(regs), "reminders": [e.as_dict() for e in events]}


async def sweep_all(
    db: AsyncSession, plant_id: str | None = None, now: datetime | None = None
) -> dict[str, Any]:
    """The whole expiry sweep — one call for the nightly job. Caller commits."""
    amc = await sweep_amc_contracts(db, plant_id, now)
    certs = await sweep_asset_certificates(db, plant_id, now)
    regs = await sweep_statutory_registrations(db, plant_id, now)
    return {
        "amc": amc,
        "assetCertificates": certs,
        "statutoryRegistrations": regs,
        "totalReminders": len(amc["reminders"]) + len(certs["reminders"]) + len(regs["reminders"]),
    }


__all__ = [
    "DEFAULT_TIERS",
    "FIRE_REGISTRATION_TYPES",
    "ExpiryEvent",
    "tiers_for",
    "days_remaining",
    "due_tier",
    "status_for",
    "sweep_amc_contracts",
    "amc_coverage",
    "sweep_asset_certificates",
    "sweep_statutory_registrations",
    "sweep_all",
]
