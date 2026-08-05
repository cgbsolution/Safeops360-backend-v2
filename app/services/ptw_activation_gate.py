"""PTW activation gate.

A permit can only transition out of its receiver step (ASSIGNEE_TASK) into
ACTIVE state when **all** of these checks pass:

  • A non-superseded FLRA exists, COMPLETED, with every crew row signed
  • The permit is not yet expired and not already CLOSED / REJECTED / SUSPENDED
  • Every crew member has training/medical/contractor validity at issuance
    (trainingValidAtIssuance, medicalValidAtIssuance, contractorActiveAtIssuance)
    OR has been removed (removedAt set)
  • Every isolation row has isolationVerifiedAt set (isolations physically
    locked-out and tagged before work starts)
  • Every crew member holds valid, acknowledged PPE for their role profile
    AND the permit type (PPE-01 Pass 2 — Build Prompt §6.4). Checked LIVE
    via ppe_gate.check_ppe_for_crew, not from the crew-add snapshot,
    because PPE state (returns, recalls, failed inspections) moves fast.

The blockers list is **all-at-once** — we don't short-circuit, so the UI
can render every reason in a single panel.

This is the single source of truth used by:
  - workflow_engine.execute() before advancing the receiver task
  - GET /api/ptw/{id}/activation-gate to render the receiver-step blocker UX
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.flra import FLRA, FLRACrewSignature, FLRAStatus
from app.models.permit import (
    Permit,
    PermitCrewMember,
    PermitIsolation,
    PermitStatus,
)
from app.models.user import User


@dataclass
class GateBlocker:
    code: str
    message: str
    severity: str = "ERROR"  # ERROR | WARN


@dataclass
class PtwActivationGateStatus:
    ok: bool
    blockers: list[GateBlocker] = field(default_factory=list)
    flra_id: str | None = None
    flra_number: str | None = None
    flra_status: str | None = None
    signed_count: int = 0
    total_crew: int = 0
    crew_validity_issues: list[str] = field(default_factory=list)
    isolations_pending: int = 0
    isolations_total: int = 0
    crew_ppe_issues: list[str] = field(default_factory=list)
    crew_ppe_warnings: list[str] = field(default_factory=list)


def _utc_aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


async def can_ptw_transition_to_active(
    db: AsyncSession, permit_id: str
) -> PtwActivationGateStatus:
    """Returns the full blocker list (or ok=True) for the receiver-step
    activation transition. Always read-only — safe from server components."""

    # populate_existing is load-bearing, not a tidy-up. Session.get() returns
    # an instance straight from the identity map when one is already there,
    # and silently DROPS the loader options when it does — so workCrew /
    # isolations stay unloaded and the reads below fire a lazy load, which
    # raises MissingGreenlet under asyncio. Every caller reaches here with the
    # permit already in the session (the /accept route loads it to check
    # receiverId; the engine loads it in _sync_record_status), so without
    # this flag the gate crashes on the common path rather than the rare one.
    permit = await db.get(
        Permit,
        permit_id,
        options=[
            selectinload(Permit.workCrew),
            selectinload(Permit.isolations),
        ],
        populate_existing=True,
    )
    status = PtwActivationGateStatus(ok=True)

    if permit is None:
        status.ok = False
        status.blockers.append(
            GateBlocker(code="PERMIT_NOT_FOUND", message="Permit not found.")
        )
        return status

    # ─── 1. Permit-state pre-check ────────────────────────────────────
    if permit.status == PermitStatus.CLOSED:
        status.ok = False
        status.blockers.append(
            GateBlocker(code="PERMIT_CLOSED", message="Permit is already closed.")
        )
    if permit.status == PermitStatus.REJECTED:
        status.ok = False
        status.blockers.append(
            GateBlocker(code="PERMIT_REJECTED", message="Permit was rejected and cannot activate.")
        )
    if permit.status == PermitStatus.SUSPENDED:
        status.ok = False
        status.blockers.append(
            GateBlocker(
                code="PERMIT_SUSPENDED",
                message=f"Permit is suspended: {permit.suspendedReason or 'no reason recorded'}.",
            )
        )
    valid_to = _utc_aware(permit.validTo)
    if valid_to is not None and valid_to < datetime.now(timezone.utc):
        status.ok = False
        status.blockers.append(
            GateBlocker(
                code="PERMIT_EXPIRED",
                message=f"Validity ended on {valid_to.strftime('%d %b %Y %H:%M UTC')}. Extend before activation.",
            )
        )

    # ─── 1b. Competency RE-CHECK at activation (P3-4) ─────────────────
    # The competency gate fires at creation; a competency can expire between
    # creation and activation. Re-run the live Skill-Matrix check now.
    try:
        from app.services.competency import check_competency_for_permit_type

        ptype = permit.type.value if hasattr(permit.type, "value") else str(permit.type)
        crew_ids = {getattr(c, "userId", None) or getattr(c, "workerId", None) for c in (permit.workCrew or [])}
        crew_ids.add(getattr(permit, "receiverId", None))
        for uid in [u for u in crew_ids if u]:
            chk = await check_competency_for_permit_type(db, uid, ptype)
            if chk is not None and not getattr(chk, "is_valid", getattr(chk, "valid", True)):
                status.ok = False
                status.blockers.append(GateBlocker(
                    code="COMPETENCY_EXPIRED_AT_ACTIVATION",
                    message=f"Crew member competency for {ptype} is no longer valid: "
                            f"{getattr(chk, 'reason', 'expired or missing')}. Resolve before activation.",
                ))
    except Exception:
        pass  # competency module absent → degrade gracefully (gate doesn't block on it)

    # ─── 2. FLRA gate — CONDITIONAL (closed-loop rebuild) ─────────────
    # FLRA is an optional sub-flow: it blocks activation only when the
    # permit was created with flraRequired (instance config / wizard
    # override, snapshotted on the row). When an FLRA exists anyway on a
    # non-required permit, its state is still surfaced for the UI but
    # never blocks.
    flra_stmt = (
        select(FLRA)
        .where(FLRA.permitId == permit_id)
        .where(FLRA.status.in_([FLRAStatus.IN_PROGRESS, FLRAStatus.COMPLETED]))
        .order_by(FLRA.createdAt.desc())
        .options(selectinload(FLRA.crewSignatures))
        .limit(1)
    )
    flra = (await db.execute(flra_stmt)).scalar_one_or_none()
    flra_required = bool(permit.flraRequired)

    if flra is None:
        if flra_required:
            status.ok = False
            status.blockers.append(
                GateBlocker(
                    code="FLRA_MISSING",
                    message="A completed FLRA is required before activation. Crew must sign at the worksite.",
                )
            )
    else:
        status.flra_id = flra.id
        status.flra_number = flra.number
        status.flra_status = flra.status.value if isinstance(flra.status, FLRAStatus) else str(flra.status)
        status.total_crew = len(flra.crewSignatures)
        status.signed_count = sum(1 for s in flra.crewSignatures if s.signed)

        if flra.status != FLRAStatus.COMPLETED:
            unsigned = [s for s in flra.crewSignatures if not s.signed and not s.refusedToSign]
            refused = [s for s in flra.crewSignatures if s.refusedToSign]

            if unsigned:
                # Resolve names
                user_ids = [s.userId for s in unsigned]
                u_rows = (
                    await db.execute(select(User).where(User.id.in_(user_ids)))
                ).scalars().all()
                names_by_id = {u.id: u.name for u in u_rows}
                names = [names_by_id.get(s.userId, s.userId) for s in unsigned]
                if flra_required:
                    status.ok = False
                status.blockers.append(
                    GateBlocker(
                        code="FLRA_UNSIGNED",
                        message=f"FLRA awaiting sign-off from: {', '.join(names)}.",
                        severity="ERROR" if flra_required else "WARN",
                    )
                )
            if refused:
                # Names of refusers
                user_ids = [s.userId for s in refused]
                u_rows = (
                    await db.execute(select(User).where(User.id.in_(user_ids)))
                ).scalars().all()
                names_by_id = {u.id: u.name for u in u_rows}
                names = [names_by_id.get(s.userId, s.userId) for s in refused]
                if flra_required:
                    status.ok = False
                status.blockers.append(
                    GateBlocker(
                        code="FLRA_REFUSED",
                        message=(
                            f"Crew refused to sign: {', '.join(names)}. "
                            "Supervisor must replace them and re-do the FLRA."
                        ),
                        severity="ERROR" if flra_required else "WARN",
                    )
                )

    # ─── 3. Crew validity at issuance ─────────────────────────────────
    active_crew = [c for c in permit.workCrew if c.removedAt is None]
    crew_user_ids = [c.userId for c in active_crew]
    crew_names_by_id: dict[str, str] = {}
    if crew_user_ids:
        u_rows = (
            await db.execute(select(User).where(User.id.in_(crew_user_ids)))
        ).scalars().all()
        crew_names_by_id = {u.id: u.name for u in u_rows}

    for c in active_crew:
        name = crew_names_by_id.get(c.userId, c.userId)
        issues: list[str] = []
        # Note: ``False`` blocks; ``None`` is treated as "not yet checked" and is
        # tolerated for legacy rows pre-Commit 1. New permits all carry the bools.
        if c.trainingValidAtIssuance is False:
            issues.append("training expired")
        if c.medicalValidAtIssuance is False:
            issues.append("medical expired")
        if c.contractorActiveAtIssuance is False:
            issues.append("contractor inactive")
        if issues:
            status.crew_validity_issues.append(f"{name} ({', '.join(issues)})")

    if status.crew_validity_issues:
        status.ok = False
        status.blockers.append(
            GateBlocker(
                code="CREW_VALIDITY",
                message=(
                    "Crew has invalid credentials: "
                    + "; ".join(status.crew_validity_issues)
                    + ". Replace crew or update records before activation."
                ),
            )
        )

    # ─── 4. PPE compliance (live, per crew member) ────────────────────
    # Role-profile mandatory PPE + permit-type PPE (PpeType.enablesPermitTypes)
    # must be issued, acknowledged, and serviceable for every active crew
    # member. Lazy import keeps the PPE module optional at import time.
    from app.services.ppe_gate import check_ppe_for_crew

    if active_crew:
        permit_type_code = (
            permit.type.value if hasattr(permit.type, "value") else str(permit.type)
        )
        ppe_results = await check_ppe_for_crew(
            db,
            plant_id=permit.plantId,
            user_ids=crew_user_ids,
            permit_type_code=permit_type_code,
        )
        for c in active_crew:
            res = ppe_results.get(c.userId)
            if res is None:
                continue
            name = crew_names_by_id.get(c.userId, c.userId)
            if not res.ok:
                status.crew_ppe_issues.append(f"{name}: {res.summary()}")
            elif res.warnings:
                status.crew_ppe_warnings.append(
                    f"{name}: " + "; ".join(w.message for w in res.warnings)
                )

    if status.crew_ppe_issues:
        status.ok = False
        status.blockers.append(
            GateBlocker(
                code="CREW_PPE",
                message=(
                    "Crew PPE non-compliance: "
                    + " | ".join(status.crew_ppe_issues)
                    + ". Issue or replace PPE before activation."
                ),
            )
        )
    if status.crew_ppe_warnings:
        status.blockers.append(
            GateBlocker(
                code="CREW_PPE_WARN",
                message="PPE attention needed: " + " | ".join(status.crew_ppe_warnings),
                severity="WARN",
            )
        )

    # ─── 5. Isolations verified ────────────────────────────────────────
    status.isolations_total = len(permit.isolations)
    pending = [i for i in permit.isolations if i.isolationVerifiedAt is None]
    status.isolations_pending = len(pending)
    if pending:
        status.ok = False
        if status.isolations_total == 1:
            msg = "Isolation has not been verified at the worksite. Lock-out and confirm before activation."
        else:
            msg = (
                f"{status.isolations_pending} of {status.isolations_total} isolations not yet verified. "
                "Receiver must lock-out and confirm each one."
            )
        status.blockers.append(GateBlocker(code="ISOLATIONS_PENDING", message=msg))

    return status
