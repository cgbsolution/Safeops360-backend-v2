"""Fire defect lifecycle — the three rules the Fire & Life Safety spec makes hard.

A fire "Defect" is a `CamsFinding` raised against a FIRE engagement. There is no
Defect table: the spec's own §6 says extend the CAMS engine rather than fork it,
and a finding already carries severity, owner, CAPA link, due date, closure
audit and the evidence attachments. What CAMS did not carry were these three
guarantees, which is what this module is.

**§5.4 — a CRITICAL defect must have a CAPA, enforced by the database.**
The spec explicitly calls out the HIRA failure it is trying not to repeat: "the
exact 'column exists but nothing enforces it' gap found in HIRA's consequence
field". So the enforcement is layered, and the DB layer is the one that counts:

  1. `raise_defect` spawns the CAPA in the SAME transaction as the finding, so
     the normal path can never produce an unlinked CRITICAL defect.
  2. The finding is written with `requiresCapa = true`.
  3. A DEFERRABLE INITIALLY DEFERRED constraint trigger (see
     `prisma/apply-firelifesafety-ddl.ts`) re-checks at COMMIT. Any *other* code
     path — a script, a future endpoint, a bulk import — that leaves a required
     CAPA unlinked aborts the transaction. Deferred rather than immediate because
     the legal ordering is insert-finding → spawn-CAPA → link, and an immediate
     CHECK would outlaw it.

**§5.3 — the inspector who failed it cannot close it.**
Reuses `services/independence.check_assignment`; this module writes no new
conflict logic. It adds one rule the generic guard cannot know: self-closure of
your own failed inspection. That is a *specific* fact about who executed the
engagement, and it is checked directly rather than by teaching the shared guard
a fire-shaped special case.

**§4.3 — OPEN cannot reach CLOSED without a verification inspection.**
`verificationEngagementId` must point at a COMPLETED FIRE engagement against the
same asset, raised after the defect. A note is not evidence that someone went and
looked; a re-inspection record is. Mirrors the re-approval-on-edit lock decided
for HIRA.

Blockers are returned all-at-once, never short-circuited, so the UI renders every
reason in one panel — the same convention as `ptw_activation_gate`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.cams import CamsEngagement, CamsFinding, CamsResponse
from app.models.fire_safety import FireEquipment
from app.services import independence as indep
from app.services.capa_spawn import existing_capas_for, spawn_capa

# The spec's MINOR / MAJOR / CRITICAL, expressed in the CAMS finding vocabulary.
# Mapping rather than a second enum: a fire defect and an audit NC land in the
# same register and must sort together on severity.
SEVERITY_MAP = {
    "MINOR": "MINOR_NC",
    "MAJOR": "MAJOR_NC",
    "CRITICAL": "CRITICAL_NC",
}

# Severities that hard-require a CAPA. Only CRITICAL today, but expressed as a
# set because a tenant tightening MAJOR is a config decision, not a rewrite.
CAPA_REQUIRED_SEVERITIES = {"CRITICAL_NC"}

# Closure SLA per severity, aligned with the existing CAMS finding SLAs in
# services/audit_findings.py rather than inventing a parallel ladder.
_DUE_DAYS = {"CRITICAL_NC": 7, "MAJOR_NC": 30, "MINOR_NC": 60, "OBSERVATION": 90}

# CAPA severity for each defect severity, matching capa_spawn's own map.
_CAPA_SEVERITY = {"CRITICAL_NC": "CRITICAL", "MAJOR_NC": "HIGH", "MINOR_NC": "MODERATE"}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(d: datetime | None) -> datetime | None:
    if d is None:
        return None
    return d.replace(tzinfo=timezone.utc) if d.tzinfo is None else d


def normalise_severity(severity: str | None) -> str:
    """Accept either vocabulary. Defaults to MAJOR_NC, not MINOR_NC: an
    unclassified fire defect is more likely under-triaged than over-triaged, and
    the failure that matters is a CRITICAL one silently filed as minor."""
    s = (severity or "").strip().upper()
    if s in SEVERITY_MAP:
        return SEVERITY_MAP[s]
    if s in _DUE_DAYS:
        return s
    return "MAJOR_NC"


# ── Raise ────────────────────────────────────────────────────────────────────
async def _next_finding_code(db: AsyncSession) -> str:
    n = (
        await db.execute(
            select(func.count()).select_from(CamsFinding).where(CamsFinding.findingCode.like("FIRE-DEF-%"))
        )
    ).scalar() or 0
    return f"FIRE-DEF-{_now().year}-{n + 1:05d}"


async def raise_defect(
    db: AsyncSession,
    *,
    engagement: CamsEngagement,
    asset: FireEquipment,
    title: str,
    description: str = "",
    severity: str = "MAJOR",
    owner_id: str | None = None,
    actor_id: str | None = None,
    source_question_id: str | None = None,
    evidence_attachment_ids: list[str] | None = None,
) -> dict[str, Any]:
    """Raise one defect against an asset, spawning its CAPA when required.

    Caller commits. The CAPA is created *inside* the caller's transaction on
    purpose — committing the finding first and the CAPA second would leave a
    window (and, on any failure, a permanent state) where a CRITICAL defect
    exists with no corrective action, which is the exact gap §5.4 forbids.
    """
    sev = normalise_severity(severity)
    requires_capa = sev in CAPA_REQUIRED_SEVERITIES

    finding = CamsFinding(
        findingCode=await _next_finding_code(db),
        engagementId=engagement.id,
        sourceQuestionId=source_question_id,
        title=title[:200],
        description=description,
        severity=sev,
        siteId=asset.plantId,
        # The asset this defect is against. The fire defect board and the hot-work
        # zone guard both read this column.
        areaOrAssetRef=asset.id,
        # FireEquipment carries no owner column; the defect owner is whoever the
        # caller assigns, falling back to the engagement's declared auditee owner
        # rather than to nobody — an unowned CRITICAL defect has no escalation path.
        ownerId=owner_id or engagement.auditeeOwnerId,
        status="OPEN",
        dueDate=_now() + timedelta(days=_DUE_DAYS.get(sev, 60)),
        evidenceAttachmentIds=evidence_attachment_ids or [],
        requiresCapa=requires_capa,
        createdBy=actor_id,
    )
    db.add(finding)
    # Flush so the finding has an id to hand the CAPA as its source reference.
    # The deferred constraint trigger tolerates this intermediate state; an
    # immediate CHECK would not.
    await db.flush()

    capa_id: str | None = None
    capa_number: str | None = None
    if requires_capa:
        existing = await existing_capas_for(db, "INSPECTION_FINDING", finding.id)
        if existing:
            capa_id, capa_number = existing[0].id, existing[0].capaNumber
        else:
            capa = await spawn_capa(
                db,
                source_code="INSPECTION_FINDING",
                plant_id=asset.plantId,
                title=f"Fire defect: {title}"[:200],
                problem=(
                    f"{finding.findingCode} — CRITICAL fire defect on asset "
                    f"{asset.equipmentCode} ({asset.type}) at {asset.location}. "
                    f"{description}"
                )[:2000],
                ref_id=finding.id,
                ref_url=f"/fire-safety/defects/{finding.id}",
                ref_summary=f"{finding.findingCode} — {title}",
                metadata={
                    "findingCode": finding.findingCode,
                    "engagementId": engagement.id,
                    "assetId": asset.id,
                    "assetCode": asset.equipmentCode,
                    "assetType": asset.type,
                    "zoneId": asset.zoneId,
                    "findingSeverity": sev,
                    "sourceModule": "FIRE",
                },
                severity=_CAPA_SEVERITY.get(sev, "HIGH"),
                detected_method="FIRE_INSPECTION",
                owner_id=owner_id,
                actor_id=actor_id,
                due_days=_DUE_DAYS.get(sev, 60),
            )
            await db.flush()
            capa_id, capa_number = capa.id, capa.capaNumber
        # Satisfying the deferred trigger. Without this the COMMIT aborts.
        finding.capaId = capa_id

    return {
        "findingId": finding.id,
        "findingCode": finding.findingCode,
        "severity": sev,
        "requiresCapa": requires_capa,
        "capaId": capa_id,
        "capaNumber": capa_number,
    }


# ── Closure gate ─────────────────────────────────────────────────────────────
@dataclass
class DefectBlocker:
    code: str
    message: str
    severity: str = "ERROR"  # ERROR | WARN

    def as_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, "severity": self.severity}


async def _executors_of(db: AsyncSession, engagement: CamsEngagement) -> set[str]:
    """Everyone who executed the inspection: lead auditor, team, and whoever
    actually submitted the checklist response.

    `completedBy` matters and is easy to miss — on a delegated round the lead
    auditor is the person of record but a different inspector filled the form,
    and letting *that* person close their own failure is the same conflict.
    """
    ids = {engagement.leadAuditorId} | {t for t in (engagement.auditTeamIds or []) if t}
    resp = (
        await db.execute(select(CamsResponse).where(CamsResponse.engagementId == engagement.id))
    ).scalars().first()
    if resp and resp.completedBy:
        ids.add(resp.completedBy)
    return {i for i in ids if i}


async def closure_blockers(
    db: AsyncSession, finding: CamsFinding, *, actor_id: str | None
) -> list[DefectBlocker]:
    """Every reason this defect cannot move to CLOSED. All-at-once, never short-circuited."""
    blockers: list[DefectBlocker] = []

    if finding.status in ("CLOSED", "VERIFIED"):
        blockers.append(
            DefectBlocker("ALREADY_CLOSED", f"Defect is already {finding.status}.")
        )

    # §5.4 — the CAPA must exist and be linked.
    if finding.requiresCapa and not finding.capaId:
        blockers.append(
            DefectBlocker(
                "CAPA_MISSING",
                f"{finding.findingCode} is a CRITICAL defect with no linked CAPA. "
                "A CAPA is mandatory before closure.",
            )
        )

    origin = await db.get(CamsEngagement, finding.engagementId)

    # §5.3 — no self-closure of your own failed inspection.
    if origin and actor_id:
        executors = await _executors_of(db, origin)
        if actor_id in executors:
            blockers.append(
                DefectBlocker(
                    "SELF_CLOSURE",
                    f"You executed inspection {origin.engagementCode}, which raised this defect. "
                    "The inspector who records a failure cannot close it (ISO 19011 independence).",
                )
            )

    # §4.3 — a verification inspection is required, and it must be real.
    if not finding.verificationEngagementId:
        blockers.append(
            DefectBlocker(
                "VERIFICATION_MISSING",
                "Closure requires a linked verification inspection. Re-inspect the asset and "
                "attach that engagement before closing.",
            )
        )
    else:
        ver = await db.get(CamsEngagement, finding.verificationEngagementId)
        if ver is None or ver.isDeleted:
            blockers.append(
                DefectBlocker("VERIFICATION_NOT_FOUND", "The linked verification inspection no longer exists.")
            )
        else:
            if (ver.status or "").upper() not in ("COMPLETED", "CLOSED"):
                blockers.append(
                    DefectBlocker(
                        "VERIFICATION_INCOMPLETE",
                        f"Verification inspection {ver.engagementCode} is {ver.status}, not completed.",
                    )
                )
            # It must have re-inspected THIS asset, not merely be a fire engagement.
            if finding.areaOrAssetRef and ver.sourceEntityId != finding.areaOrAssetRef:
                blockers.append(
                    DefectBlocker(
                        "VERIFICATION_WRONG_ASSET",
                        f"Verification inspection {ver.engagementCode} targets a different asset.",
                    )
                )
            # And it must post-date the defect, or it is the very inspection that
            # found it — the loop that closes itself.
            ver_date = _aware(ver.conductedDate or ver.plannedDate)
            if ver_date and _aware(finding.createdAt) and ver_date < _aware(finding.createdAt):
                blockers.append(
                    DefectBlocker(
                        "VERIFICATION_PREDATES_DEFECT",
                        f"Verification inspection {ver.engagementCode} was conducted before the defect "
                        "was raised, so it cannot evidence the remediation.",
                    )
                )
            # And whoever verified must be independent of the asset. This is the
            # shared guard, not a fire-specific rule.
            if actor_id:
                verdict = await indep.check_assignment(
                    db,
                    user_id=actor_id,
                    scope=indep.scope_for_engagement(ver),
                    assigning_as="AUDITOR",
                )
                for c in verdict.blocking:
                    blockers.append(DefectBlocker("INDEPENDENCE", c.reason))
                for c in verdict.warnings:
                    blockers.append(DefectBlocker("INDEPENDENCE_WARN", c.reason, severity="WARN"))

    return blockers


async def close_defect(
    db: AsyncSession,
    finding: CamsFinding,
    *,
    actor_id: str | None,
    verification_engagement_id: str | None = None,
    note: str | None = None,
) -> dict[str, Any]:
    """Move a defect to CLOSED, or refuse with every reason at once.

    Returns `{ok: False, blockers: [...]}` rather than raising, so the caller
    decides between an inline panel and an HTTP 400 — the same contract as
    `ptw_activation_gate`.
    """
    if verification_engagement_id:
        finding.verificationEngagementId = verification_engagement_id

    blockers = await closure_blockers(db, finding, actor_id=actor_id)
    hard = [b for b in blockers if b.severity == "ERROR"]
    if hard:
        return {"ok": False, "blockers": [b.as_dict() for b in blockers]}

    finding.status = "CLOSED"
    finding.closedBy = actor_id
    finding.closedAt = _now()
    finding.verificationNote = note
    finding.updatedBy = actor_id
    return {
        "ok": True,
        "findingId": finding.id,
        "status": finding.status,
        "warnings": [b.as_dict() for b in blockers if b.severity == "WARN"],
    }


async def verify_defect(
    db: AsyncSession, finding: CamsFinding, *, actor_id: str | None, note: str | None = None
) -> dict[str, Any]:
    """CLOSED → VERIFIED. A second pair of eyes on the closure itself.

    The verifier may not be the closer, for the same reason the closer may not be
    the original inspector: a two-step workflow where one person performs both
    steps is a one-step workflow with extra clicks.
    """
    if finding.status != "CLOSED":
        return {
            "ok": False,
            "blockers": [
                DefectBlocker(
                    "NOT_CLOSED", f"Only a CLOSED defect can be verified; this one is {finding.status}."
                ).as_dict()
            ],
        }
    if actor_id and finding.closedBy and actor_id == finding.closedBy:
        return {
            "ok": False,
            "blockers": [
                DefectBlocker(
                    "SELF_VERIFICATION", "You closed this defect; verification requires a different person."
                ).as_dict()
            ],
        }
    finding.status = "VERIFIED"
    finding.verificationNote = note or finding.verificationNote
    finding.updatedBy = actor_id
    return {"ok": True, "findingId": finding.id, "status": finding.status}


# ── Board / queries ──────────────────────────────────────────────────────────
async def defects_for_asset(db: AsyncSession, asset_id: str) -> list[CamsFinding]:
    return (
        await db.execute(
            select(CamsFinding)
            .where(CamsFinding.areaOrAssetRef == asset_id)
            .where(CamsFinding.isDeleted.is_(False))
            .order_by(CamsFinding.createdAt.desc())
        )
    ).scalars().all()


async def open_critical_defect_asset_ids(db: AsyncSession, plant_id: str | None = None) -> set[str]:
    """Assets with an unresolved CRITICAL defect.

    Read by the nightly status recompute (spec §5.2, which lists open CRITICAL
    defects as a status input) and by the hot-work PTW guard. Returned as a set
    of ids so both callers do one query instead of one per asset.
    """
    stmt = (
        select(CamsFinding.areaOrAssetRef)
        .where(CamsFinding.severity == "CRITICAL_NC")
        .where(CamsFinding.status.in_(("OPEN", "IN_PROGRESS")))
        .where(CamsFinding.isDeleted.is_(False))
        .where(CamsFinding.areaOrAssetRef.is_not(None))
    )
    if plant_id:
        stmt = stmt.where(CamsFinding.siteId == plant_id)
    return {r for r in (await db.execute(stmt)).scalars().all() if r}


async def unlinked_required_capa_findings(db: AsyncSession) -> list[CamsFinding]:
    """Findings flagged `requiresCapa` with no CAPA.

    Should always be empty — the deferred constraint trigger makes committing one
    impossible. It is queried by the nightly job anyway, because a guarantee
    nobody checks is a guarantee nobody notices losing (for instance if the
    trigger is dropped by a future `prisma db push`).
    """
    return (
        await db.execute(
            select(CamsFinding)
            .where(CamsFinding.requiresCapa.is_(True))
            .where(CamsFinding.capaId.is_(None))
            .where(CamsFinding.isDeleted.is_(False))
        )
    ).scalars().all()


__all__ = [
    "SEVERITY_MAP",
    "CAPA_REQUIRED_SEVERITIES",
    "DefectBlocker",
    "normalise_severity",
    "raise_defect",
    "closure_blockers",
    "close_defect",
    "verify_defect",
    "defects_for_asset",
    "open_critical_defect_asset_ids",
    "unlinked_required_capa_findings",
]
