"""WP-45 - supplier / vendor audits.

docs/cams/09 §3.5.

**The brief asks for "a `Supplier` auditable entity distinct from own
facilities". The platform already has one.** `VendorProfile` (ERM Tier 3)
carries vendorCode, legalName, category, criticality, tier, siteScope and a
relationship owner, and ERM's dual-lens vendor scoring depends on it. Creating a
second supplier table would fork the master data and guarantee drift - the exact
failure the two-engine split already demonstrates. So `SupplierAuditLink` is a
LINK, and the supplier stays where it lives.

**The Supplier Audit chip was decorative.** `SUPPLIER_AUDIT` is a valid
`engagementType` on `CamsAuditType` and appears on the calendar, but nothing
connected an engagement to a supplier - so a "supplier audit" was an ordinary
site audit with a different label. This module is that connection.

**Staged, and the staging is stated.** Stage 1 (here): the entity link, the
scheduling snapshot, coverage of suppliers as programme scope units, and
findings that carry a supplier contact. Stage 2: a supplier-facing response
channel. A vendor factory manager will never hold a platform seat, so that
channel is a token-scoped portal over a single audit — see
`app/services/supplier_portal.py`. The `responseChannel` reported below is
derived from whether a portal token has actually been issued, not assumed.

**Vendor data is reached only through `app/services/vendors.py`.** This module
used to `from app.models.erm_t3 import VendorProfile`, which is the cross-module
model import the CAMS diagnosis flagged as F-48: it couples the audit engine to
the vendor schema so a change on one side breaks the other silently. Every
vendor read here now goes through the boundary DTO.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.audit_compliance import ComplianceAudit
from app.models.cams import CamsEngagement, CamsFinding
from app.models.cams_completion import AuditFinding, SupplierAuditLink
from app.services import vendors as vendor_svc

# Vendor criticality -> how often the programme should cover them. The gradient
# is the policy: a single-source critical vendor audited annually is not a
# control.
CRITICALITY_FREQUENCY: dict[str, int] = {
    "CRITICAL": 2,
    "HIGH": 1,
    "MEDIUM": 1,
    "LOW": 0,  # covered by exception only
}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


async def link_supplier(
    db: AsyncSession,
    *,
    engagement_kind: str,
    engagement_id: str,
    vendor_profile_id: str,
    vendor_site_ref: str | None = None,
    contact_name: str | None = None,
    contact_email: str | None = None,
    actor_id: str | None = None,
) -> SupplierAuditLink:
    """Attach a supplier to an engagement, snapshotting its risk posture.

    The criticality/tier snapshot matters: a vendor re-tiered next quarter must
    not silently rewrite *why* this audit was scheduled.
    """
    vendor = await vendor_svc.get_vendor(db, vendor_profile_id)
    if vendor is None:
        raise ValueError("Vendor not found")

    existing = (
        await db.execute(
            select(SupplierAuditLink).where(
                SupplierAuditLink.engagementKind == engagement_kind.upper(),
                SupplierAuditLink.engagementId == engagement_id,
                SupplierAuditLink.vendorProfileId == vendor_profile_id,
            )
        )
    ).scalars().first()
    if existing is not None:
        existing.vendorSiteRef = vendor_site_ref or existing.vendorSiteRef
        existing.supplierContactName = contact_name or existing.supplierContactName
        existing.supplierContactEmail = contact_email or existing.supplierContactEmail
        await db.flush()
        return existing

    row = SupplierAuditLink(
        engagementKind=engagement_kind.upper(),
        engagementId=engagement_id,
        vendorProfileId=vendor_profile_id,
        vendorSiteRef=vendor_site_ref,
        supplierContactName=contact_name,
        supplierContactEmail=contact_email,
        criticalityAtScheduling=vendor.criticality,
        tierAtScheduling=vendor.tier,
        createdById=actor_id,
    )
    db.add(row)
    await db.flush()
    return row


async def supplier_for_engagement(
    db: AsyncSession, *, engagement_kind: str, engagement_id: str
) -> dict[str, Any] | None:
    """The supplier block an engagement screen renders. None = own facility."""
    link = (
        await db.execute(
            select(SupplierAuditLink).where(
                SupplierAuditLink.engagementKind == engagement_kind.upper(),
                SupplierAuditLink.engagementId == engagement_id,
            )
        )
    ).scalars().first()
    if link is None:
        return None

    vendor = await vendor_svc.get_vendor(db, link.vendorProfileId)
    # Stage 2: whether the supplier can actually respond depends on a portal
    # token having been ISSUED for this audit, so it is read rather than
    # assumed. Imported here to keep the portal an optional layer over the
    # link rather than a hard dependency of it.
    from app.services import supplier_portal

    portal = await supplier_portal.channel_for_engagement(
        db, engagement_kind=engagement_kind, engagement_id=engagement_id
    )
    return {
        "linkId": link.id,
        "vendorProfileId": link.vendorProfileId,
        "vendorCode": vendor.vendorCode if vendor else None,
        "legalName": vendor.legalName if vendor else "Unknown vendor",
        "category": vendor.category if vendor else None,
        # Current vs at-scheduling, side by side — a re-tier between scheduling
        # and conduct is exactly the kind of drift an auditor should see.
        "criticality": vendor.criticality if vendor else None,
        "tier": vendor.tier if vendor else None,
        "criticalityAtScheduling": link.criticalityAtScheduling,
        "tierAtScheduling": link.tierAtScheduling,
        "riskPostureChanged": bool(
            vendor and vendor.criticality != link.criticalityAtScheduling
        ),
        "vendorSiteRef": link.vendorSiteRef,
        "supplierContactName": link.supplierContactName,
        "supplierContactEmail": link.supplierContactEmail,
        "isSingleSource": vendor.isSingleSource if vendor else False,
        "relationshipOwnerId": vendor.relationshipOwnerId if vendor else None,
        # Derived from the portal state, never assumed: PORTAL once a live
        # token exists, OUT_OF_BAND until then. Reporting "portal" for an audit
        # whose token was never issued (or has expired) would tell an internal
        # user the supplier can respond when they cannot.
        **portal,
    }


async def supplier_audit_history(
    db: AsyncSession, *, vendor_profile_id: str
) -> dict[str, Any]:
    """Every engagement against this vendor, with its finding load.

    This is what makes a supplier audit worth modelling: "have we audited them,
    when, and what is still open" is unanswerable while the chip is decorative.
    """
    links = (
        await db.execute(
            select(SupplierAuditLink)
            .where(SupplierAuditLink.vendorProfileId == vendor_profile_id)
            .order_by(SupplierAuditLink.createdAt.desc())
        )
    ).scalars().all()

    engagements: list[dict[str, Any]] = []
    open_findings = 0
    for ln in links:
        if ln.engagementKind == "AUDIT":
            a = await db.get(ComplianceAudit, ln.engagementId)
            if a is None or a.isDeleted:
                continue
            n = (
                await db.execute(
                    select(func.count(AuditFinding.id)).where(
                        AuditFinding.auditId == a.id,
                        AuditFinding.status == "OPEN",
                        AuditFinding.isDeleted.is_(False),
                    )
                )
            ).scalar_one()
            open_findings += n
            engagements.append({
                "engagementKind": "AUDIT", "engagementId": a.id, "code": a.auditNumber,
                "title": a.title, "status": a.status,
                "date": a.scheduledDate.isoformat() if a.scheduledDate else None,
                "openFindings": n, "scorePct": a.overallCompliancePct,
            })
        else:
            e = await db.get(CamsEngagement, ln.engagementId)
            if e is None or e.isDeleted:
                continue
            n = (
                await db.execute(
                    select(func.count(CamsFinding.id)).where(
                        CamsFinding.engagementId == e.id,
                        CamsFinding.status == "OPEN",
                        CamsFinding.isDeleted.is_(False),
                    )
                )
            ).scalar_one()
            open_findings += n
            engagements.append({
                "engagementKind": "INSPECTION", "engagementId": e.id,
                "code": e.engagementCode, "title": e.title, "status": e.status,
                "date": e.plannedDate.isoformat() if e.plannedDate else None,
                "openFindings": n, "scorePct": e.scorePercent,
            })

    vendor = await vendor_svc.get_vendor(db, vendor_profile_id)
    recommended = CRITICALITY_FREQUENCY.get((vendor.criticality or "").upper(), 1) if vendor else 1
    return {
        "vendorProfileId": vendor_profile_id,
        "vendorCode": vendor.vendorCode if vendor else None,
        "legalName": vendor.legalName if vendor else None,
        "criticality": vendor.criticality if vendor else None,
        "engagements": engagements,
        "engagementCount": len(engagements),
        "openFindingCount": open_findings,
        "recommendedAuditsPerYear": recommended,
        "neverAudited": not engagements,
    }


async def unaudited_suppliers(
    db: AsyncSession, *, criticality: str | None = None
) -> list[dict[str, Any]]:
    """Critical vendors with no engagement on record - the coverage gap.

    The programme's supplier scope units are built from this: a critical,
    single-source vendor that has never been audited is the highest-value row a
    coverage matrix can surface.
    """
    vendors = await vendor_svc.list_vendors(db, criticality=criticality)
    linked = {
        r[0]
        for r in (await db.execute(select(SupplierAuditLink.vendorProfileId))).all()
    }
    out = []
    for v in vendors:
        if v.id in linked:
            continue
        out.append({
            "vendorProfileId": v.id,
            "vendorCode": v.vendorCode,
            "legalName": v.legalName,
            "category": v.category,
            "criticality": v.criticality,
            "tier": v.tier,
            "isSingleSource": v.isSingleSource,
            "recommendedAuditsPerYear": CRITICALITY_FREQUENCY.get(
                (v.criticality or "").upper(), 1
            ),
        })
    # Highest risk first: critical single-source vendors head the list.
    order = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3}
    out.sort(key=lambda r: (order.get((r["criticality"] or "").upper(), 4),
                            not r["isSingleSource"], r["legalName"] or ""))
    return out


__all__ = [
    "CRITICALITY_FREQUENCY",
    "link_supplier",
    "supplier_for_engagement",
    "supplier_audit_history",
    "unaudited_suppliers",
]
