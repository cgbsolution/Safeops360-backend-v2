"""Vendor boundary — the ONLY crossing point between CAMS and ERM Tier 3.

**Why this file exists.** A supplier audit's subject and a vendor-risk subject
are the same legal entity, so CAMS has to reach vendor master data. It must not
reach it by importing `app.models.erm_t3` — that is the tight coupling the CAMS
diagnosis already flagged (F-48), and it is what lets a change to the vendor
model break the audit engine silently. Everything CAMS needs is exposed here as
plain dataclasses/dicts, so no ORM object crosses the module line and the two
sides can be changed independently.

The rule, stated so it survives review: **`app/services/cams_suppliers.py`, the
audit engine and `app/services/independence.py` import THIS module and never
`app.models.erm_t3`.** A grep for `erm_t3` under CAMS should return nothing.

Three capabilities:

  1. **Read** — resolve vendors for pickers, registers and the supplier link.
  2. **Responsibility** — who owns the commercial relationship, which is the
     fact auditor independence needs (procurement auditing its own supplier).
  3. **Write-back** — an on-site audit result becomes a `VendorAssessment`, so
     the audit score actually moves the vendor's risk band instead of being
     filed somewhere the risk model never reads.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.erm_t3 import VendorAssessment, VendorProfile, VendorScoringConfig

# The domain an on-site compliance audit actually evidences. Scoring any other
# domain from an audit would be inventing data the audit never collected — the
# same failure mode as auto-filling clause citations.
AUDIT_EVIDENCED_DOMAIN = "compliance_legal"

# Vendor criticality -> how long an audit-derived assessment stays current.
# A critical vendor's posture goes stale faster, so its assessment expires
# sooner and `nextReviewDate` pulls forward.
_VALIDITY_MONTHS: dict[str, int] = {
    "CRITICAL": 6,
    "STRATEGIC": 6,
    "HIGH": 12,
    "MEDIUM": 12,
    "LOW": 24,
}
_DEFAULT_VALIDITY_MONTHS = 12


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _aware(d: datetime | None) -> datetime | None:
    if d is None:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


# ─────────────────────────────────────────────────────────────────────
# The DTO — what CAMS is allowed to know about a vendor
# ─────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class VendorRef:
    """A vendor, flattened. Deliberately NOT the ORM row.

    Everything on here is either identity, risk posture or responsibility —
    the three things an audit needs. Commercial fields (spend, onboarding
    state, linked processes) are not exposed, because an audit has no business
    reading them and exposing them invites the coupling this module prevents.
    """

    id: str
    vendorCode: str
    legalName: str
    category: str | None
    criticality: str | None
    tier: str | None
    siteScope: list[str] = field(default_factory=list)
    relationshipOwnerId: str | None = None
    isSingleSource: bool = False
    isActive: bool = True
    currentRiskScore: float | None = None
    currentRiskBand: str | None = None
    currentEsgScore: float | None = None
    currentEsgBand: str | None = None
    nextReviewDate: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "vendorProfileId": self.id,
            "vendorCode": self.vendorCode,
            "legalName": self.legalName,
            "category": self.category,
            "criticality": self.criticality,
            "tier": self.tier,
            "siteScope": self.siteScope,
            "relationshipOwnerId": self.relationshipOwnerId,
            "isSingleSource": self.isSingleSource,
            "isActive": self.isActive,
            "currentRiskScore": self.currentRiskScore,
            "currentRiskBand": self.currentRiskBand,
            "currentEsgScore": self.currentEsgScore,
            "currentEsgBand": self.currentEsgBand,
            "nextReviewDate": self.nextReviewDate,
        }


def _to_ref(v: VendorProfile) -> VendorRef:
    return VendorRef(
        id=v.id,
        vendorCode=v.vendorCode,
        legalName=v.legalName,
        category=v.category,
        criticality=v.criticality,
        tier=v.tier,
        siteScope=list(v.siteScope or []),
        relationshipOwnerId=v.relationshipOwnerId,
        isSingleSource=bool(v.isSingleSource),
        isActive=bool(v.isActive),
        currentRiskScore=v.currentRiskScore,
        currentRiskBand=v.currentRiskBand,
        currentEsgScore=v.currentEsgScore,
        currentEsgBand=v.currentEsgBand,
        nextReviewDate=_aware(v.nextReviewDate).isoformat() if v.nextReviewDate else None,
    )


# ─────────────────────────────────────────────────────────────────────
# 1. Read
# ─────────────────────────────────────────────────────────────────────


async def get_vendor(db: AsyncSession, vendor_id: str | None) -> VendorRef | None:
    if not vendor_id:
        return None
    v = await db.get(VendorProfile, vendor_id)
    if v is None or v.isDeleted:
        return None
    return _to_ref(v)


async def get_vendors(
    db: AsyncSession, vendor_ids: Iterable[str | None]
) -> dict[str, VendorRef]:
    """Batch resolve. The register renders one row per audit and must not issue
    one query per row."""
    ids = sorted({v for v in vendor_ids if v})
    if not ids:
        return {}
    rows = (
        await db.execute(
            select(VendorProfile).where(
                VendorProfile.id.in_(ids), VendorProfile.isDeleted.is_(False)
            )
        )
    ).scalars().all()
    return {v.id: _to_ref(v) for v in rows}


async def list_vendors(
    db: AsyncSession, *, active_only: bool = True, criticality: str | None = None
) -> list[VendorRef]:
    """The picker's source. Highest-criticality first so the vendors that most
    need auditing are the ones at the top of the list."""
    q = select(VendorProfile).where(VendorProfile.isDeleted.is_(False))
    if active_only:
        q = q.where(VendorProfile.isActive.is_(True))
    if criticality:
        q = q.where(VendorProfile.criticality == criticality.upper())
    rows = (await db.execute(q)).scalars().all()
    order = {"CRITICAL": 0, "STRATEGIC": 1, "HIGH": 2, "MEDIUM": 3, "LOW": 4}
    refs = [_to_ref(v) for v in rows]
    refs.sort(
        key=lambda r: (
            order.get((r.criticality or "").upper(), 9),
            not r.isSingleSource,
            r.legalName or "",
        )
    )
    return refs


# ─────────────────────────────────────────────────────────────────────
# 2. Responsibility — the fact independence needs
# ─────────────────────────────────────────────────────────────────────


async def relationship_owners(
    db: AsyncSession, *, vendor_ids: Iterable[str | None] | None = None
) -> dict[str, dict[str, Any]]:
    """vendorProfileId -> {ownerUserId, legalName, vendorCode, criticality}.

    Independence asks exactly one question of the vendor module — "who owns this
    commercial relationship?" — so that is the only shape crossing the boundary.
    Passing `vendor_ids=None` returns the whole estate, which the Independence
    Register needs; the scheduling guard passes one id and pays an indexed cost.
    """
    q = select(
        VendorProfile.id,
        VendorProfile.relationshipOwnerId,
        VendorProfile.legalName,
        VendorProfile.vendorCode,
        VendorProfile.criticality,
    ).where(
        VendorProfile.isDeleted.is_(False),
        VendorProfile.relationshipOwnerId.isnot(None),
    )
    if vendor_ids is not None:
        ids = sorted({v for v in vendor_ids if v})
        if not ids:
            return {}
        q = q.where(VendorProfile.id.in_(ids))
    return {
        vid: {
            "ownerUserId": owner,
            "legalName": name,
            "vendorCode": code,
            "criticality": crit,
        }
        for vid, owner, name, code, crit in (await db.execute(q)).all()
    }


# ─────────────────────────────────────────────────────────────────────
# 3. Write-back — the audit result becomes vendor risk evidence
# ─────────────────────────────────────────────────────────────────────


def audit_pct_to_raw_score(
    compliance_pct: float,
    *,
    critical_failures: int = 0,
    audit_passed: bool | None = None,
) -> float:
    """Compliance % -> the RISK lens' 1–5 raw score. Pure, so it is testable.

    The RISK lens runs **higher = riskier**, so the mapping inverts: 100%
    compliant is raw 1, 0% is raw 5.

    Two floors, and they exist because a straight linear map produces a
    dangerous answer. A supplier at 96% with three critical non-conformances
    computes to raw 1.2 — "LOW risk" — which is precisely the read a critical
    NC is supposed to prevent. The floors are the same reduction-veto shape the
    programme's frequency engine already uses: they can only ever RAISE the
    assessed risk, never lower it.
    """
    pct = max(0.0, min(100.0, float(compliance_pct)))
    raw = 1.0 + 4.0 * (1.0 - pct / 100.0)
    if critical_failures > 0:
        raw = max(raw, 3.5)  # -> 70/100, at least HIGH
    if audit_passed is False:
        raw = max(raw, 3.0)  # -> 60/100, at least HIGH
    return round(max(1.0, min(5.0, raw)), 2)


def _validity_until(criticality: str | None, now: datetime) -> datetime:
    months = _VALIDITY_MONTHS.get((criticality or "").upper(), _DEFAULT_VALIDITY_MONTHS)
    return now + timedelta(days=months * 30)


def build_domain_scores(
    config_domains: list[dict[str, Any]],
    prior_domain_scores: list[dict[str, Any]] | None,
    *,
    raw_score: float,
    evidence_note: str,
) -> list[dict[str, Any]]:
    """Assemble the assessment's domain scores. Pure.

    **The honesty problem this solves.** An on-site audit evidences compliance
    standing. It says nothing about the vendor's solvency, its cyber posture or
    how dependent we are on it — yet `compute_weighted_score` sums every domain,
    so omitting the others would score them zero and report a strategically
    risky vendor as LOW risk.

    So: the audited domain takes the audit's score, and every other domain is
    **carried forward from the previous current assessment**, each labelled with
    where its number came from. Nothing is invented, and the composite stays
    comparable with the assessment it supersedes.

    When there is no prior assessment there is nothing to carry, so the audited
    domain is renormalised to the full weight and the assessment honestly
    represents one domain measured at 100% weight rather than six domains of
    which five are fiction.
    """
    prior_by_key = {
        d.get("domainKey"): d for d in (prior_domain_scores or []) if d.get("domainKey")
    }
    out: list[dict[str, Any]] = []
    for d in config_domains or []:
        key = d.get("domainKey")
        if not key:
            continue
        weight = float(d.get("weightPct", 0))
        if key == AUDIT_EVIDENCED_DOMAIN:
            out.append({
                "domainKey": key,
                "rawScore": raw_score,
                "weightPct": weight,
                "evidenceNotes": evidence_note,
            })
        elif key in prior_by_key:
            p = prior_by_key[key]
            out.append({
                "domainKey": key,
                "rawScore": p.get("rawScore", 0),
                "weightPct": weight,
                "evidenceNotes": (
                    "Carried forward — not evidenced by this audit. "
                    f"{p.get('evidenceNotes') or 'previous assessment'}"
                ),
                "carriedForward": True,
            })
        # A domain with neither audit evidence nor a prior score is OMITTED
        # rather than scored zero: on the RISK lens zero reads as "no risk",
        # which is the opposite of "we have not looked".

    scored_keys = {d["domainKey"] for d in out}
    if scored_keys == {AUDIT_EVIDENCED_DOMAIN}:
        # Nothing to carry — renormalise so the one measured domain expresses
        # the whole composite instead of being diluted by absent ones.
        out[0]["weightPct"] = 100.0
        out[0]["evidenceNotes"] += " (sole domain assessed; weight renormalised to 100%)"
    return out


@dataclass
class AssessmentResult:
    written: bool
    reason: str = ""
    assessmentId: str | None = None
    weightedScore: float | None = None
    band: str | None = None
    vendorProfileId: str | None = None
    validUntil: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "written": self.written,
            "reason": self.reason,
            "assessmentId": self.assessmentId,
            "weightedScore": self.weightedScore,
            "band": self.band,
            "vendorProfileId": self.vendorProfileId,
            "validUntil": self.validUntil,
        }


async def record_audit_assessment(
    db: AsyncSession,
    *,
    vendor_id: str,
    audit_code: str,
    audit_id: str,
    compliance_pct: float | None,
    critical_failures: int = 0,
    audit_passed: bool | None = None,
    assessor_id: str | None = None,
    assessment_date: datetime | None = None,
) -> AssessmentResult:
    """Turn a closed supplier audit into a `VendorAssessment(ONSITE_AUDIT)`.

    This is the whole point of linking the two modules: without it a supplier
    audit is a PDF, and the vendor's risk band still reflects a desk review
    nobody has revisited.

    **It declines to write when the audit cannot support a score.** An audit
    with no assessable checkpoints has `compliance_pct = None` — the same
    condition under which the report suppresses its grade. Writing a risk score
    off a suppressed grade would put a number on the vendor profile that the
    audit report itself refuses to state.

    Best-effort by contract: returns a result rather than raising, so a vendor
    write-back problem can never block an audit from closing.
    """
    now = assessment_date or _utcnow()

    v = await db.get(VendorProfile, vendor_id)
    if v is None or v.isDeleted:
        return AssessmentResult(False, "Vendor not found", vendorProfileId=vendor_id)

    if compliance_pct is None:
        return AssessmentResult(
            False,
            "Audit has no assessable checkpoints — no score to carry into the vendor "
            "risk model (the report suppresses its grade for the same reason).",
            vendorProfileId=vendor_id,
        )

    raw = audit_pct_to_raw_score(
        compliance_pct, critical_failures=critical_failures, audit_passed=audit_passed
    )

    cfg = (
        await db.execute(select(VendorScoringConfig).where(VendorScoringConfig.lens == "RISK"))
    ).scalar_one_or_none()
    domains = list((cfg.domains if cfg else None) or [])
    if not domains:
        # No configured RISK model: score the one domain we evidenced at full
        # weight rather than refusing outright.
        domains = [{"domainKey": AUDIT_EVIDENCED_DOMAIN, "weightPct": 100.0}]

    prior = (
        await db.execute(
            select(VendorAssessment)
            .where(
                VendorAssessment.vendorId == vendor_id,
                VendorAssessment.lens == "RISK",
                VendorAssessment.isCurrent.is_(True),
                VendorAssessment.isDeleted.is_(False),
            )
            .order_by(VendorAssessment.assessmentDate.desc())
        )
    ).scalars().first()

    note = (
        f"On-site audit {audit_code}: {round(float(compliance_pct), 1)}% compliance, "
        f"{critical_failures} critical failure(s)."
    )
    domain_scores = build_domain_scores(
        domains,
        list(prior.domainScores or []) if prior else None,
        raw_score=raw,
        evidence_note=note,
    )

    # Imported here rather than at module scope: `services.erm_t3` imports
    # `services.independence`, which imports this module for the vendor
    # relationship-owner rule. A module-level import would close that cycle.
    from app.services.erm_t3 import band_for, compute_weighted_score, recompute_vendor_scores

    score = compute_weighted_score(domain_scores)
    band = band_for(list((cfg.bandThresholds if cfg else None) or []), score)
    valid_until = _validity_until(v.criticality, now)

    # Supersede the prior current RISK assessment — "current" means latest
    # evidence, and this audit is newer than whatever it replaces.
    for p in (
        await db.execute(
            select(VendorAssessment).where(
                VendorAssessment.vendorId == vendor_id,
                VendorAssessment.lens == "RISK",
                VendorAssessment.isCurrent.is_(True),
            )
        )
    ).scalars().all():
        p.isCurrent = False

    carried = sum(1 for d in domain_scores if d.get("carriedForward"))
    a = VendorAssessment(
        vendorId=vendor_id,
        lens="RISK",
        assessmentDate=now,
        assessorId=assessor_id or "system",
        method="ONSITE_AUDIT",
        domainScores=domain_scores,
        weightedScore=score,
        band=band,
        summaryNotes=(
            f"Derived from closed supplier audit {audit_code}. "
            f"Compliance {round(float(compliance_pct), 1)}%, "
            f"{critical_failures} critical failure(s). "
            f"Scored domain: {AUDIT_EVIDENCED_DOMAIN}"
            + (f"; {carried} domain(s) carried forward from the prior assessment." if carried else ".")
        ),
        validUntil=valid_until,
        isCurrent=True,
        # Audit findings live on the audit and become CAPAs there. Duplicating
        # them into the assessment would create a second, diverging copy.
        findings=[],
        createdBy=assessor_id,
    )
    db.add(a)
    await db.flush()

    await recompute_vendor_scores(db, v)
    await db.flush()

    return AssessmentResult(
        True,
        f"Vendor risk reassessed from audit {audit_code}",
        assessmentId=a.id,
        weightedScore=score,
        band=band,
        vendorProfileId=vendor_id,
        validUntil=valid_until.isoformat(),
    )


__all__ = [
    "AUDIT_EVIDENCED_DOMAIN",
    "AssessmentResult",
    "VendorRef",
    "audit_pct_to_raw_score",
    "build_domain_scores",
    "get_vendor",
    "get_vendors",
    "list_vendors",
    "record_audit_assessment",
    "relationship_owners",
]
