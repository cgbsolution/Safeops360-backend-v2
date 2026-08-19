"""Register of Fire Extinguishers — PIL/EHSD/CL/028-R1.

The client's register is a sixteen-column controlled sheet. It is NOT a checklist
and gets no checklist machinery: it is the extinguisher slice of the existing
`FireEquipment` asset master, projected into the sheet's own column order and
vocabulary.

WHERE THE FOUR CERTIFICATE COLUMNS COME FROM
--------------------------------------------
The sheet prints "HP tested on", "HP Test due date", "Refilled on" and "Due for
refilling" as four flat columns. They are stored as two `FireAssetCertificate`
rows — a HYDROSTATIC_TEST and a REFILL — because each pair is the issue and
expiry of a certificate, and that table already models exactly that: expiry
status, escalation tiers, the attached document, and a nightly sweep that raises
the reminders. Copying the dates onto `FireEquipment` as well would give the
register two answers to "is this cylinder due", which is precisely the duplicate
source of truth the certificate table exists to prevent.

So this module projects them out on read and upserts them on write. The screen
and the PDF see the sheet's four columns; the platform sees certificates.

BADGES
------
Due-date badges use `fire_certificates.status_for` — the platform's existing
computed VALID / EXPIRING_SOON / EXPIRED convention, the same one the AMC and
statutory-registration sweeps already use. There is no separate "Statutory
Registers" badge component in the frontend to reuse (searched; it does not
exist), so reusing the *computed status* rather than inventing a fourth colour
rule is the meaningful form of "don't reinvent the badge".

The register renders three independent badges per row — cylinder life, HP test,
refill — because they expire independently and a single roll-up would hide which
one is the problem. `worstBadge` is provided for sorting and for the register
header count, so the screen can lead with "6 cylinders need attention" without
recomputing the rule.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select

from app.models.fire_safety import FireAssetCertificate, FireEquipment
from app.services import fire_certificates as certsvc
from app.services.fire_checklist_templates import FE_REGISTER_DOC

# The asset type the register covers. The fire register holds hydrants, panels
# and detectors too; PIL/EHSD/CL/028 is about cylinders only.
EXTINGUISHER = "FIRE_EXTINGUISHER"

CERT_HYDROSTATIC = "HYDROSTATIC_TEST"
CERT_REFILL = "REFILL"

# Badge ladder. Deliberately NOT the certificate module's four-tier escalation
# ladder (90/60/30/7): that ladder decides who gets emailed tonight, this one
# decides what colour a cell is. The build spec fixes the visual rule at
# red / amber-within-30-days / green, and a register whose amber threshold moved
# with a per-certificate notification config would be unreadable at a glance.
BADGE_AMBER_DAYS = 30

BADGE_OVERDUE = "OVERDUE"
BADGE_DUE_SOON = "DUE_SOON"
BADGE_OK = "OK"
BADGE_NONE = "NOT_RECORDED"

_BADGE_RANK = {BADGE_OVERDUE: 3, BADGE_DUE_SOON: 2, BADGE_OK: 1, BADGE_NONE: 0}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(d: datetime | None) -> datetime | None:
    if d is None:
        return None
    return d.replace(tzinfo=timezone.utc) if d.tzinfo is None else d


def badge_for(due: datetime | None, now: datetime | None = None) -> dict[str, Any]:
    """{status, daysRemaining} for one due date.

    A missing due date is NOT_RECORDED, not OK. An extinguisher with no refill
    date on file is a gap in the register, and colouring it green would report
    the gap as compliance — the one failure mode that makes a due-date register
    worse than no register.
    """
    if due is None:
        return {"status": BADGE_NONE, "daysRemaining": None, "dueDate": None}
    remaining = certsvc.days_remaining(due, now or _now())
    if remaining is None:
        return {"status": BADGE_NONE, "daysRemaining": None, "dueDate": None}
    status = BADGE_OVERDUE if remaining < 0 else (BADGE_DUE_SOON if remaining <= BADGE_AMBER_DAYS else BADGE_OK)
    return {"status": status, "daysRemaining": remaining, "dueDate": _aware(due).isoformat()}


async def latest_certificates(db, asset_ids: list[str]) -> dict[str, dict[str, FireAssetCertificate]]:
    """{assetId: {certificateType: newest certificate}}.

    "Newest" is by `issueDate`, falling back to `createdAt` for rows imported
    without one — a cylinder re-tested every five years accumulates certificates,
    and the register shows the current one, not the first.
    """
    out: dict[str, dict[str, FireAssetCertificate]] = {}
    if not asset_ids:
        return out
    rows = (
        await db.execute(
            select(FireAssetCertificate)
            .where(FireAssetCertificate.assetId.in_(asset_ids))
            .where(FireAssetCertificate.isDeleted.is_(False))
            .where(FireAssetCertificate.certificateType.in_([CERT_HYDROSTATIC, CERT_REFILL]))
        )
    ).scalars().all()
    for c in rows:
        slot = out.setdefault(c.assetId, {})
        prev = slot.get(c.certificateType)
        key_new = _aware(c.issueDate) or _aware(c.createdAt)
        key_old = (_aware(prev.issueDate) or _aware(prev.createdAt)) if prev else None
        if prev is None or (key_new and key_old and key_new > key_old):
            slot[c.certificateType] = c
    return out


def register_row(
    e: FireEquipment,
    certs: dict[str, FireAssetCertificate],
    *,
    sl_no: int | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """One sheet row: the sheet's columns, plus the badges the paper cannot have."""
    now = now or _now()
    hp = certs.get(CERT_HYDROSTATIC)
    refill = certs.get(CERT_REFILL)

    hp_due = _aware(hp.expiryDate) if hp else None
    refill_due = _aware(refill.expiryDate) if refill else None
    expiry = _aware(e.expiryDate)

    badges = {
        "cylinderLife": badge_for(expiry, now),
        "hpTest": badge_for(hp_due, now),
        "refill": badge_for(refill_due, now),
    }
    worst = max(badges.values(), key=lambda b: _BADGE_RANK[b["status"]])

    return {
        "id": e.id,
        "slNo": sl_no,
        "equipmentCode": e.equipmentCode,
        # ── the sheet's sixteen columns, in the sheet's own order ────────────
        "serialNo": e.serialNo,
        "type": e.assetSubtype or e.capacitySpec or "",   # CO2 / ABC / DCP / Foam
        "capacity": e.capacitySpec,
        "yearOfManufacture": e.yearOfManufacture,
        "expiryDate": expiry.isoformat() if expiry else None,
        "make": e.make,
        "allottedSerialNo": e.allottedSerialNo,
        "location": e.location,
        "hpTestedOn": (_aware(hp.issueDate).isoformat() if hp and hp.issueDate else None),
        "hpTestDueDate": hp_due.isoformat() if hp_due else None,
        "dateOfDischarge": _aware(e.dateOfDischarge).isoformat() if e.dateOfDischarge else None,
        "refilledOn": (_aware(refill.issueDate).isoformat() if refill and refill.issueDate else None),
        "dueForRefilling": refill_due.isoformat() if refill_due else None,
        "weightKg": e.weightKg,
        "remarks": e.registerRemarks,
        # ── platform additions ──────────────────────────────────────────────
        "plantId": e.plantId,
        "status": e.status,
        "nextInspectionDueDate": (
            _aware(e.nextInspectionDueDate).isoformat() if e.nextInspectionDueDate else None
        ),
        "badges": badges,
        "worstBadge": worst["status"],
        "hpCertificateId": hp.id if hp else None,
        "refillCertificateId": refill.id if refill else None,
    }


async def build_register(
    db, equipment: list[FireEquipment], *, now: datetime | None = None,
) -> dict[str, Any]:
    """The full register payload: document header, rows, and the attention counts."""
    now = now or _now()
    ordered = sorted(equipment, key=lambda e: (e.location or "", e.allottedSerialNo or "", e.equipmentCode))
    certs = await latest_certificates(db, [e.id for e in ordered])
    rows = [
        register_row(e, certs.get(e.id, {}), sl_no=i, now=now)
        for i, e in enumerate(ordered, start=1)
    ]
    summary = {
        "total": len(rows),
        "overdue": sum(1 for r in rows if r["worstBadge"] == BADGE_OVERDUE),
        "dueSoon": sum(1 for r in rows if r["worstBadge"] == BADGE_DUE_SOON),
        "notRecorded": sum(1 for r in rows if r["worstBadge"] == BADGE_NONE),
    }
    return {"document": FE_REGISTER_DOC, "summary": summary, "rows": rows}


async def upsert_certificate(
    db,
    asset: FireEquipment,
    cert_type: str,
    *,
    issued_on: datetime | None,
    due_on: datetime | None,
    actor_id: str | None,
) -> FireAssetCertificate | None:
    """Record an HP test or a refill from the register's flat date columns.

    Updates the current certificate when the issue date is unchanged, and creates
    a new one when it moves — because a re-test is a new certificate, not an edit
    of the old one, and overwriting would erase the cylinder's test history. That
    history is what a factory inspector asks for.

    Both dates empty clears nothing: an operator blanking a cell should not
    silently delete a certificate and the document attached to it. Removal is an
    explicit act on the certificate itself.
    """
    if issued_on is None and due_on is None:
        return None

    current = (await latest_certificates(db, [asset.id])).get(asset.id, {}).get(cert_type)
    same_issue = (
        current is not None
        and _aware(current.issueDate) == _aware(issued_on)
    )
    if current is not None and same_issue:
        current.expiryDate = due_on
        current.status = certsvc.status_for(due_on, certsvc.tiers_for(current.escalationTierDays))
        current.updatedBy = actor_id
        await db.flush()
        return current

    cert = FireAssetCertificate(
        assetId=asset.id,
        plantId=asset.plantId,
        certificateType=cert_type,
        issueDate=issued_on,
        expiryDate=due_on,
        status=certsvc.status_for(due_on, certsvc.DEFAULT_TIERS),
        escalationTierDays=[],
        documentIds=[],
        createdBy=actor_id,
        updatedBy=actor_id,
    )
    db.add(cert)
    await db.flush()
    return cert


__all__ = [
    "EXTINGUISHER", "CERT_HYDROSTATIC", "CERT_REFILL",
    "BADGE_OVERDUE", "BADGE_DUE_SOON", "BADGE_OK", "BADGE_NONE", "BADGE_AMBER_DAYS",
    "badge_for", "latest_certificates", "register_row", "build_register", "upsert_certificate",
]
