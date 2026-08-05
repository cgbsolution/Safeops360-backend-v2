"""Auditor independence — ISO 19011 §5.4.2 / §7.2.3.

Designed in [docs/cams/09-module-completion.md](../../../docs/cams/09-module-completion.md) §2.1.

**Auditor and auditee are engagement-scoped roles, not user types.** The model
already had that right: `ComplianceAudit.leadAuditorUserId` / `coAuditors` /
`auditees` plus per-checkpoint `assignedAuditorId` / `assignedOwnerId`, and no
global `AUDITOR` role anywhere in RBAC. What was missing was every guard. This
module is those guards, and it is the ONLY place independence is decided —
CAMS and ERM Internal Controls both call it, so the product has one definition
of "independent" rather than two that drift.

Three rules, all engagement-scoped:

  1. **Own-work guard** — you may not audit something you own.
  2. **Same-engagement exclusivity** — you may never be auditor and auditee on
     the *same* engagement, including via per-checkpoint allocation.
  3. **Cross-engagement freedom** — auditing site A while being auditee at site
     B is valid, expected, and must be *visible*. Rule 3 is not a block; it is
     `two_hat_summary()`, which powers the surfaces that show it.

Every verdict carries its reason. A guard that says "denied" without naming the
conflicting engagement is a guard people route around.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable, Literal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.assurance import DisciplineOwner, IndependenceWaiver
from app.models.audit_compliance import AuditCheckpointResponse, ComplianceAudit
from app.models.cams import CamsEngagement
from app.models.cams_completion import SupplierAuditLink
from app.models.plant import Area
from app.models.user import User, UserRole

Severity = Literal["BLOCK", "WARN"]
EngagementKind = Literal["AUDIT", "INSPECTION"]


# ─────────────────────────────────────────────────────────────────────
# Shared primitive — the one ERM Internal Controls already relies on
# ─────────────────────────────────────────────────────────────────────


def segregation_ok(actor_id: str | None, owner_id: str | None) -> bool:
    """Two parties to a control/assurance activity must be different people.

    Lifted verbatim from `app.services.erm_t3.segregation_ok` so there is a
    single definition; ERM T3 now delegates here. Kept deliberately trivial —
    the value is that exactly one implementation exists.
    """
    return bool(actor_id) and actor_id != owner_id


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _aware(dt: datetime | None) -> datetime | None:
    """Postgres TIMESTAMPTZ round-trips aware; test stand-ins often are not."""
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


# ─────────────────────────────────────────────────────────────────────
# Verdict types
# ─────────────────────────────────────────────────────────────────────


@dataclass
class Conflict:
    rule: str  # OWN_WORK | SAME_ENGAGEMENT_DUAL_ROLE
    severity: Severity
    # The four OWNERSHIP sources the register groups on:
    #   DECLARED_AUDITEE | CHECKPOINT_OWNER | AREA_OWNER | DISCIPLINE_OWNER
    # plus two that are signals rather than ownership (both WARN):
    #   ROLE_SCOPE | PROFILE_AFFINITY
    # and one that is neither — rule 2 reads this engagement's own roster:
    #   SAME_ENGAGEMENT_ROSTER
    source: str
    reason: str  # human-readable, rendered inline in the UI
    detail: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "rule": self.rule,
            "severity": self.severity,
            "source": self.source,
            "reason": self.reason,
            "detail": self.detail,
        }


@dataclass
class IndependenceVerdict:
    allowed: bool
    conflicts: list[Conflict] = field(default_factory=list)
    waived: bool = False
    waiverId: str | None = None

    @property
    def blocking(self) -> list[Conflict]:
        return [c for c in self.conflicts if c.severity == "BLOCK"]

    @property
    def warnings(self) -> list[Conflict]:
        return [c for c in self.conflicts if c.severity == "WARN"]

    def as_dict(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed,
            "waived": self.waived,
            "waiverId": self.waiverId,
            "conflicts": [c.as_dict() for c in self.conflicts],
            "blockingCount": len(self.blocking),
            "warningCount": len(self.warnings),
            # The single line the UI renders when it has room for one.
            "summary": (
                self.blocking[0].reason
                if self.blocking
                else (self.warnings[0].reason if self.warnings else "")
            ),
        }


# ─────────────────────────────────────────────────────────────────────
# Engagement scope — normalised across the two engines
# ─────────────────────────────────────────────────────────────────────


@dataclass
class EngagementScope:
    """The subset of an engagement the independence rules reason about.

    Normalising here is what lets one rule set serve both engines, and it is the
    same shape the programme's `resolve_engagement` needs (docs/cams/08 §1).
    """

    kind: EngagementKind
    id: str | None
    siteId: str | None
    disciplineCodes: list[str] = field(default_factory=list)
    areaIds: list[str] = field(default_factory=list)
    departments: list[str] = field(default_factory=list)
    leadAuditorId: str | None = None
    teamAuditorIds: list[str] = field(default_factory=list)
    auditeeUserIds: list[str] = field(default_factory=list)
    # WP-45. Set when the engagement audits a SUPPLIER rather than our own
    # facility. `siteId` still names the owning plant, so without this field the
    # rules would have no way to know an external party is the subject — and the
    # most obvious conflict on a supplier audit (procurement auditing the vendor
    # it manages) would be invisible.
    vendorProfileId: str | None = None


def _coauditor_ids(co_auditors: list | None) -> list[str]:
    """Tolerates both the legacy flat shape (list[str]) and the structured shape
    (list[{userId, disciplineIds}]). Mirrors the helper in audit_compliance."""
    out: list[str] = []
    for c in co_auditors or []:
        if isinstance(c, dict):
            uid = c.get("userId")
            if uid:
                out.append(uid)
        elif c:
            out.append(c)
    return out


def _auditee_ids(auditees: list | None) -> list[str]:
    out: list[str] = []
    for a in auditees or []:
        if isinstance(a, dict):
            uid = a.get("userId")
            if uid:
                out.append(uid)
        elif a:
            out.append(a)
    return out


async def scope_for_audit(
    db: AsyncSession, audit: ComplianceAudit, *, include_allocation: bool = True
) -> EngagementScope:
    disciplines = list(audit.selectedDisciplineIds or [])
    auditees = _auditee_ids(audit.auditees)
    if audit.plantManagerUserId:
        auditees.append(audit.plantManagerUserId)

    if include_allocation and audit.id:
        rows = (
            await db.execute(
                select(
                    AuditCheckpointResponse.categoryId,
                    AuditCheckpointResponse.assignedOwnerId,
                ).where(AuditCheckpointResponse.auditId == audit.id)
            )
        ).all()
        for cat, owner in rows:
            if cat and cat not in disciplines:
                disciplines.append(cat)
            if owner:
                auditees.append(owner)

    # WP-45 — is the subject a supplier? `SupplierAuditLink` is a CAMS table, so
    # reading it here is not a module-boundary crossing; the VENDOR data behind
    # it is still reached only through `services/vendors.py`.
    vendor_profile_id = None
    if audit.id:
        vendor_profile_id = (
            await db.execute(
                select(SupplierAuditLink.vendorProfileId).where(
                    SupplierAuditLink.engagementKind == "AUDIT",
                    SupplierAuditLink.engagementId == audit.id,
                )
            )
        ).scalars().first()

    return EngagementScope(
        kind="AUDIT",
        id=audit.id,
        siteId=audit.plantId,
        disciplineCodes=[d for d in disciplines if d],
        areaIds=[a for a in (audit.scopeAreas or []) if a],
        departments=[d for d in (audit.scopeDepartments or []) if d],
        leadAuditorId=audit.leadAuditorUserId,
        teamAuditorIds=_coauditor_ids(audit.coAuditors),
        auditeeUserIds=sorted({a for a in auditees if a}),
        vendorProfileId=vendor_profile_id,
    )


def scope_for_engagement(eng: CamsEngagement) -> EngagementScope:
    auditees = [eng.auditeeOwnerId] if eng.auditeeOwnerId else []
    return EngagementScope(
        kind="INSPECTION",
        id=eng.id,
        siteId=eng.siteId,
        disciplineCodes=[s for s in (eng.standardRefs or []) if s],
        areaIds=[eng.areaOrAssetRef] if eng.areaOrAssetRef else [],
        departments=[],
        leadAuditorId=eng.leadAuditorId,
        teamAuditorIds=[t for t in (eng.auditTeamIds or []) if t],
        auditeeUserIds=auditees,
    )


# ─────────────────────────────────────────────────────────────────────
# Rule 1 — own-work guard
# ─────────────────────────────────────────────────────────────────────
#
# Responsibility is derived from four sources, in descending confidence. Two of
# them did not exist before Q17 was answered; the other two are weaker than they
# look and are deliberately not treated as proof.
#
#   1. DECLARED_AUDITEE  — the user is named as auditee/owner on an engagement
#                          at this site. Authoritative. BLOCK.
#   2. AREA_OWNER /      — Area.ownerUserId (Q17) / DisciplineOwner (Q17).
#      DISCIPLINE_OWNER    Explicit ownership statements. BLOCK.
#   3. ROLE_SCOPE        — UserRole(scopeType=PLANT|DEPARTMENT, scopeValue).
#                          BLOCK at site scope, WARN at department scope, because
#                          department scope is coarser than audit scope.
#   4. PROFILE_AFFINITY  — User.plantId + free-text User.department string match.
#                          WARN only. A string match is not evidence, and blocking
#                          on one makes the product feel arbitrary.


# ─────────────────────────────────────────────────────────────────────
# Source resolution — ONE definition of "what does this person own?"
# ─────────────────────────────────────────────────────────────────────
#
# `check_assignment` applies the independence RULE to these facts;
# `two_hat_summary` merely REPORTS them. They must never derive them
# separately.
#
# They used to. `two_hat_summary` read `ComplianceAudit.auditees` and
# `plantManagerUserId` and nothing else — 1 of the 4 sources the guard reads —
# so three people who own audit checkpoints (Rohit Kumar, Imran Solanki, Nikhil
# Desai) rendered as "0 engagements, wears both hats: no" on the Independence
# screen while the scheduling guard blocked all three. A register that says
# "clear" about someone the product refuses to schedule is worse than no
# register: it is evidence that is wrong.
#
# Cost note: pass `user_ids` for the guard path (indexed, one user, same query
# cost as before) and omit it for the register (one estate-wide pass). Same
# code either way — the filter is a WHERE clause, not a second implementation.


@dataclass
class OwnedThing:
    """One fact: this person answers for this thing.

    `engagementId` is set only for the two engagement-derived sources; the
    ownership-of-record sources (area, discipline) are standing facts that are
    not attached to any one engagement, which is precisely why they survive when
    an engagement closes.
    """

    source: str  # DECLARED_AUDITEE | CHECKPOINT_OWNER | AREA_OWNER | DISCIPLINE_OWNER
    label: str
    siteId: str | None = None
    disciplineCodes: list[str] = field(default_factory=list)
    engagementKind: str | None = None
    engagementId: str | None = None
    engagementCode: str | None = None
    detail: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "label": self.label,
            "siteId": self.siteId,
            "disciplineCodes": self.disciplineCodes,
            "engagementKind": self.engagementKind,
            "engagementId": self.engagementId,
            "engagementCode": self.engagementCode,
            "detail": self.detail,
        }


@dataclass
class AuditorRole:
    """The other hat: an engagement this person audits."""

    hat: str  # LEAD_AUDITOR | CO_AUDITOR | TEAM_AUDITOR
    engagementKind: str
    engagementId: str
    engagementCode: str
    title: str
    siteId: str | None
    status: str
    scheduledDate: str | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "hat": self.hat,
            "engagementKind": self.engagementKind,
            "engagementId": self.engagementId,
            "code": self.engagementCode,
            "title": self.title,
            "siteId": self.siteId,
            "status": self.status,
            "scheduledDate": self.scheduledDate,
        }


@dataclass
class OwnershipSources:
    """Everything the four sources say about one person."""

    userId: str
    owns: list[OwnedThing] = field(default_factory=list)
    audits: list[AuditorRole] = field(default_factory=list)

    def by_source(self, source: str) -> list[OwnedThing]:
        return [o for o in self.owns if o.source == source]

    @property
    def sourcesPresent(self) -> list[str]:
        # Stable order so the register's source chips do not shuffle between
        # requests for reasons the reader cannot see.
        order = [
            "DECLARED_AUDITEE",
            "CHECKPOINT_OWNER",
            "AREA_OWNER",
            "DISCIPLINE_OWNER",
            "VENDOR_RELATIONSHIP_OWNER",
        ]
        present = {o.source for o in self.owns}
        return [s for s in order if s in present]


async def resolve_ownership_sources(
    db: AsyncSession,
    *,
    user_ids: Iterable[str] | None = None,
    site_id: str | None = None,
    area_ids: Iterable[str] | None = None,
    include_auditor_roles: bool = False,
    include_inspections: bool = True,
) -> dict[str, OwnershipSources]:
    """THE resolver. Every independence surface reads its facts from here.

    Returns a map keyed by user id. `user_ids=None` means the whole estate,
    which is what the register needs; the guard passes exactly one id and pays
    the same indexed cost it always did.

    Nothing here decides anything — no severity, no rule, no scope overlap. Those
    belong to `check_assignment`, and keeping them out is what stops a second
    definition of "auditee" growing back.
    """
    wanted: set[str] | None = {u for u in user_ids if u} if user_ids is not None else None
    if wanted is not None and not wanted:
        return {}

    out: dict[str, OwnershipSources] = {}

    def bucket(uid: str) -> OwnershipSources:
        if uid not in out:
            out[uid] = OwnershipSources(userId=uid)
        return out[uid]

    def wants(uid: str | None) -> bool:
        return bool(uid) and (wanted is None or uid in wanted)

    # ── 1. DECLARED_AUDITEE + the auditor side, from the audit header ──
    aq = select(ComplianceAudit).where(ComplianceAudit.isDeleted.is_(False))
    if site_id:
        aq = aq.where(ComplianceAudit.plantId == site_id)
    audits = (await db.execute(aq)).scalars().all()
    audit_by_id = {a.id: a for a in audits}

    for a in audits:
        for uid in {*_auditee_ids(a.auditees), a.plantManagerUserId} - {None}:
            if not wants(uid):
                continue
            bucket(uid).owns.append(
                OwnedThing(
                    source="DECLARED_AUDITEE",
                    label=a.title or a.auditNumber,
                    siteId=a.plantId,
                    disciplineCodes=list(a.selectedDisciplineIds or []),
                    engagementKind="AUDIT",
                    engagementId=a.id,
                    engagementCode=a.auditNumber,
                    detail={"status": a.status},
                )
            )
        if include_auditor_roles:
            if wants(a.leadAuditorUserId):
                bucket(a.leadAuditorUserId).audits.append(_auditor_role(a, "LEAD_AUDITOR"))
            for uid in _coauditor_ids(a.coAuditors):
                if wants(uid) and uid != a.leadAuditorUserId:
                    bucket(uid).audits.append(_auditor_role(a, "CO_AUDITOR"))

    # ── 2. CHECKPOINT_OWNER — the source that was invisible ────────────
    cq = select(
        AuditCheckpointResponse.assignedOwnerId,
        AuditCheckpointResponse.auditId,
        AuditCheckpointResponse.plantId,
        AuditCheckpointResponse.categoryId,
        AuditCheckpointResponse.categoryName,
        func.count().label("n"),
    ).where(AuditCheckpointResponse.assignedOwnerId.isnot(None))
    if wanted is not None:
        cq = cq.where(AuditCheckpointResponse.assignedOwnerId.in_(wanted))
    if site_id:
        cq = cq.where(AuditCheckpointResponse.plantId == site_id)
    cq = cq.group_by(
        AuditCheckpointResponse.assignedOwnerId,
        AuditCheckpointResponse.auditId,
        AuditCheckpointResponse.plantId,
        AuditCheckpointResponse.categoryId,
        AuditCheckpointResponse.categoryName,
    )
    for owner, audit_id, plant_id, cat, cat_name, n in (await db.execute(cq)).all():
        if not wants(owner):
            continue
        parent = audit_by_id.get(audit_id)
        bucket(owner).owns.append(
            OwnedThing(
                source="CHECKPOINT_OWNER",
                label=cat_name or cat or "checkpoints",
                siteId=plant_id,
                disciplineCodes=[cat] if cat else [],
                engagementKind="AUDIT",
                engagementId=audit_id,
                engagementCode=parent.auditNumber if parent else None,
                detail={"checkpointCount": int(n), "discipline": cat_name or cat},
            )
        )

    # ── 3. AREA_OWNER ──────────────────────────────────────────────────
    arq = select(Area).where(Area.ownerUserId.isnot(None))
    if wanted is not None:
        arq = arq.where(Area.ownerUserId.in_(wanted))
    skip_areas = False
    if area_ids is not None:
        # The guard asks for exactly the areas an engagement declares in scope;
        # the register asks for all of them. Narrowing by area beats narrowing by
        # site here — an engagement's scope is a set of areas, not a whole plant.
        ids = [a for a in area_ids if a]
        # An empty in-scope area set can only ever match nothing. Issuing the
        # query anyway cost a full ~160ms round trip to return zero rows, on
        # every picker check, for every audit that names no areas — which is
        # most of them.
        skip_areas = not ids
        arq = arq.where(Area.id.in_(ids))
    elif site_id:
        arq = arq.where(Area.plantId == site_id)
    for ar in (() if skip_areas else (await db.execute(arq)).scalars().all()):
        if not wants(ar.ownerUserId):
            continue
        bucket(ar.ownerUserId).owns.append(
            OwnedThing(
                source="AREA_OWNER",
                label=ar.name,
                siteId=getattr(ar, "plantId", None),
                detail={"areaId": ar.id, "areaName": ar.name},
            )
        )

    # ── 4. DISCIPLINE_OWNER ────────────────────────────────────────────
    dq = select(DisciplineOwner).where(DisciplineOwner.isActive.is_(True))
    if wanted is not None:
        dq = dq.where(DisciplineOwner.ownerUserId.in_(wanted))
    if site_id:
        # NULL plantId is estate-wide ownership and conflicts everywhere, so it
        # must survive a site filter — the whole point of that sentinel.
        dq = dq.where((DisciplineOwner.plantId == site_id) | (DisciplineOwner.plantId.is_(None)))
    for d in (await db.execute(dq)).scalars().all():
        if not wants(d.ownerUserId):
            continue
        bucket(d.ownerUserId).owns.append(
            OwnedThing(
                source="DISCIPLINE_OWNER",
                label=d.disciplineLabel or d.disciplineCode,
                siteId=d.plantId,
                disciplineCodes=[d.disciplineCode],
                detail={
                    "disciplineCode": d.disciplineCode,
                    "ownershipType": d.ownershipType,
                    "estateWide": d.plantId is None,
                },
            )
        )

    # ── Inspections: auditee owner + the auditor side ──────────────────
    # The REGISTER needs these — being the auditee owner of an inspection is a
    # fact it must show. The GUARD does not: `declared_auditee_conflicts` acts
    # only on AUDIT-kind facts (a deliberate narrowing, asserted in
    # `test_independence_parity`), so fetching them for a scheduling check was a
    # ~200ms round trip whose every row was then filtered out.
    if not include_inspections:
        return out

    eq = select(CamsEngagement).where(CamsEngagement.isDeleted.is_(False))
    if site_id:
        eq = eq.where(CamsEngagement.siteId == site_id)
    for e in (await db.execute(eq)).scalars().all():
        if wants(e.auditeeOwnerId):
            bucket(e.auditeeOwnerId).owns.append(
                OwnedThing(
                    source="DECLARED_AUDITEE",
                    label=e.title or e.engagementCode,
                    siteId=e.siteId,
                    disciplineCodes=list(e.standardRefs or []),
                    engagementKind="INSPECTION",
                    engagementId=e.id,
                    engagementCode=e.engagementCode,
                    detail={"status": e.status},
                )
            )
        if include_auditor_roles:
            if wants(e.leadAuditorId):
                bucket(e.leadAuditorId).audits.append(_inspector_role(e, "LEAD_AUDITOR"))
            for uid in e.auditTeamIds or []:
                if wants(uid) and uid != e.leadAuditorId:
                    bucket(uid).audits.append(_inspector_role(e, "TEAM_AUDITOR"))

    return out


def _auditor_role(a: ComplianceAudit, hat: str) -> AuditorRole:
    return AuditorRole(
        hat=hat,
        engagementKind="AUDIT",
        engagementId=a.id,
        engagementCode=a.auditNumber,
        title=a.title,
        siteId=a.plantId,
        status=a.status,
        scheduledDate=a.scheduledDate.isoformat() if a.scheduledDate else None,
    )


def _inspector_role(e: CamsEngagement, hat: str) -> AuditorRole:
    return AuditorRole(
        hat=hat,
        engagementKind="INSPECTION",
        engagementId=e.id,
        engagementCode=e.engagementCode,
        title=e.title,
        siteId=e.siteId,
        status=e.status,
        scheduledDate=e.plannedDate.isoformat() if e.plannedDate else None,
    )


def _overlaps(a: Iterable[str] | None, b: Iterable[str] | None) -> bool:
    """Do two discipline-code sets intersect?

    An EMPTY set is the full-library sentinel (`ComplianceAudit
    .selectedDisciplineIds` — "empty list = full library"), so it overlaps
    everything. Reading empty as "nothing in scope" would silently disable the
    guard on exactly the widest audits, which is the dangerous direction to be
    wrong in.
    """
    sa, sb = set(a or []), set(b or [])
    if not sa or not sb:
        return True
    return bool(sa & sb)


def declared_auditee_conflicts(
    owned: Iterable[OwnedThing], scope: EngagementScope
) -> list[Conflict]:
    """Pure decision: which resolved facts conflict with THIS engagement's scope?

    **Scope-unit overlap, not site membership.** The first cut of this rule asked
    "auditee anywhere at this site", which blocked 59 of 59 candidates at
    Meridian North Works and left *zero* independent auditors at every site in
    the tenant. §2.1 specifies the own-work guard as an area/site/discipline
    "**for which** that user is the responsible owner" — owning Emergency
    Response checkpoints does not compromise someone auditing Worker Welfare.

    Two deliberate narrowings against the raw facts the resolver returns, both
    preserving the guard's existing behaviour rather than quietly widening it:

      * only AUDIT-kind declared-auditee facts block. Being the auditee owner of
        an *inspection* is reported on the register but has never blocked an
        audit assignment, and turning that on is a scheduling-policy change, not
        a display fix.
      * at most one declared-auditee conflict is emitted, and the checkpoint
        source is consulted only when the header source found nothing — the
        message is the same either way, and two copies of it help nobody.
    """
    in_scope = [d for d in (scope.disciplineCodes or []) if d]
    same_engagement = lambda o: scope.kind == o.engagementKind and o.engagementId == scope.id  # noqa: E731

    for o in owned:
        if o.source != "DECLARED_AUDITEE" or o.engagementKind != "AUDIT":
            continue
        if same_engagement(o):
            continue  # rule 2's territory, not rule 1's
        if scope.siteId and o.siteId != scope.siteId:
            continue
        # Being an auditee on an engagement that shares no discipline with this
        # one is not a conflict for this one.
        if not _overlaps(in_scope, o.disciplineCodes):
            continue
        return [
            Conflict(
                rule="OWN_WORK",
                severity="BLOCK",
                source="DECLARED_AUDITEE",
                reason=(
                    f"They are a declared auditee on {o.engagementCode}, which shares "
                    "disciplines with this audit's scope — ISO 19011 §7.2.3, auditors "
                    "should not audit their own work."
                ),
                detail={"engagementKind": "AUDIT", "engagementId": o.engagementId,
                        "engagementCode": o.engagementCode},
            )
        ]

    for o in owned:
        if o.source != "CHECKPOINT_OWNER" or same_engagement(o):
            continue
        if scope.siteId and o.siteId != scope.siteId:
            continue
        # An empty in-scope set is the full-library sentinel, so it matches every
        # discipline — same reading as `_overlaps`.
        if in_scope and not set(in_scope) & set(o.disciplineCodes):
            continue
        return [
            Conflict(
                rule="OWN_WORK",
                severity="BLOCK",
                source="CHECKPOINT_OWNER",
                reason=(
                    f"They hold auditee ownership of “{o.label}” checkpoints at this site "
                    "on another engagement, and that discipline is in scope here — "
                    "ISO 19011 §7.2.3."
                ),
                detail={"engagementKind": "AUDIT", "engagementId": o.engagementId,
                        "discipline": o.label},
            )
        ]
    return []


def ownership_of_record_conflicts(
    owned: Iterable[OwnedThing], scope: EngagementScope
) -> list[Conflict]:
    """Pure decision: standing ownership (area, discipline) against this scope.

    Kept scope-filtered rather than site-filtered for the same reason as above:
    the guard has always asked "do they own something IN SCOPE HERE", and
    widening it to "at this site" is what left zero eligible auditors.
    """
    out: list[Conflict] = []
    area_ids = set(scope.areaIds or [])
    in_scope = [d for d in (scope.disciplineCodes or []) if d]

    for o in owned:
        if o.source == "AREA_OWNER" and o.detail.get("areaId") in area_ids:
            out.append(
                Conflict(
                    rule="OWN_WORK",
                    severity="BLOCK",
                    source="AREA_OWNER",
                    reason=f"They are the responsible owner of area “{o.label}”, which is in scope.",
                    detail={"areaId": o.detail.get("areaId"), "areaName": o.label},
                )
            )
        elif o.source == "DISCIPLINE_OWNER" and in_scope:
            code = o.detail.get("disciplineCode")
            if code not in in_scope:
                continue
            # `plantId is None` means estate-wide ownership — a group lead who
            # owns Fire Safety everywhere should not audit it anywhere.
            if o.siteId and scope.siteId and o.siteId != scope.siteId:
                continue
            where = "across the estate" if not o.siteId else "at this site"
            out.append(
                Conflict(
                    rule="OWN_WORK",
                    severity="BLOCK",
                    source="DISCIPLINE_OWNER",
                    reason=(
                        f"They are the {(o.detail.get('ownershipType') or 'accountable').lower()} "
                        f"owner for {o.label} {where}, which is in scope."
                    ),
                    detail={
                        "disciplineCode": code,
                        "ownershipType": o.detail.get("ownershipType"),
                    },
                )
            )
    return out


# The three finders below are split into a PURE decision core and a thin async
# fetch wrapper. The cores take already-loaded rows, so they are unit-testable
# with stand-ins in the house style (the suite has no async-DB harness), and the
# guard logic — the part that must not be wrong — is covered directly.


def area_owner_conflicts(areas: Iterable[Any], user_id: str) -> list[Conflict]:
    """Row-shaped adapter over `ownership_of_record_conflicts` (Q17 `Area.ownerUserId`).

    Takes already-loaded `Area` rows and DELEGATES the decision — it does not
    re-implement it. Kept because it is the shape the existing unit tests and
    any row-holding caller already speak; deleting it would push those callers
    into writing their own version, which is the failure mode this whole build
    is about.
    """
    rows = [a for a in areas if getattr(a, "ownerUserId", None) == user_id]
    owned = [
        OwnedThing(
            source="AREA_OWNER",
            label=a.name,
            siteId=getattr(a, "plantId", None),
            detail={"areaId": a.id, "areaName": a.name},
        )
        for a in rows
    ]
    scope = EngagementScope(kind="AUDIT", id=None, siteId=None, areaIds=[a.id for a in rows])
    return ownership_of_record_conflicts(owned, scope)


def discipline_owner_conflicts(
    owners: Iterable[Any], user_id: str, site_id: str | None
) -> list[Conflict]:
    """Row-shaped adapter over `ownership_of_record_conflicts` (Q17 `DisciplineOwner`).

    `plantId is None` means estate-wide ownership — a group lead who owns Fire
    Safety everywhere should not audit Fire Safety anywhere, so it conflicts at
    every site rather than none. That rule lives in the delegate, once.
    """
    rows = [
        d for d in owners
        if getattr(d, "ownerUserId", None) == user_id and getattr(d, "isActive", True)
    ]
    owned = [
        OwnedThing(
            source="DISCIPLINE_OWNER",
            label=d.disciplineLabel or d.disciplineCode,
            siteId=d.plantId,
            disciplineCodes=[d.disciplineCode],
            detail={
                "disciplineCode": d.disciplineCode,
                "ownershipType": d.ownershipType,
                "estateWide": d.plantId is None,
            },
        )
        for d in rows
    ]
    scope = EngagementScope(
        kind="AUDIT",
        id=None,
        siteId=site_id,
        disciplineCodes=[d.disciplineCode for d in rows],
    )
    return ownership_of_record_conflicts(owned, scope)


def vendor_relationship_conflicts(
    owned: Iterable[OwnedThing], scope: EngagementScope
) -> list[Conflict]:
    """Pure: the relationship owner of the audited vendor cannot audit it.

    **Why this is a BLOCK and not a warning.** The other own-work sources say
    "you are responsible for the thing being examined". This one says the same
    with a commercial interest attached: the person who selected the supplier,
    negotiated with them and is measured on that relationship is being asked to
    judge whether they conform. ISO 19011 §7.2.3 requires auditors to be free
    from bias and conflict of interest, and a procurement owner auditing their
    own vendor is the textbook case a buyer will raise first.

    Scoped to the vendor actually under audit — owning a *different* supplier
    relationship is not a conflict here, in the same way that owning an area
    that is out of scope is not.
    """
    if not scope.vendorProfileId:
        return []
    out: list[Conflict] = []
    for o in owned:
        if o.source != "VENDOR_RELATIONSHIP_OWNER":
            continue
        if o.detail.get("vendorProfileId") != scope.vendorProfileId:
            continue
        out.append(
            Conflict(
                rule="OWN_WORK",
                severity="BLOCK",
                source="VENDOR_RELATIONSHIP_OWNER",
                reason=(
                    f"They are the relationship owner for {o.label}, the supplier under "
                    "audit — ISO 19011 §7.2.3, auditors must be free from conflict of "
                    "interest in the party they audit."
                ),
                detail={
                    "vendorProfileId": o.detail.get("vendorProfileId"),
                    "vendorCode": o.detail.get("vendorCode"),
                    "legalName": o.label,
                },
            )
        )
    return out


def role_scope_conflicts(
    user_roles: Iterable[Any], scope: EngagementScope, *, now: datetime | None = None
) -> list[Conflict]:
    """Pure: UserRole(scopeType=PLANT|DEPARTMENT). **Both WARN, neither blocks.**

    Department scope warns because a department is coarser than an audit scope
    and blocking on it would deny legitimate assignments.

    Site scope used to BLOCK, and that was wrong. `UserRole(scopeType='PLANT')`
    is how the platform grants a user *access* to a site — 161 of this tenant's
    users hold one — so it records site membership, not responsibility for the
    disciplines under audit. Blocking on it left **0 independent candidates at
    every site in the tenant**, which is a guard nobody can comply with and
    therefore a guard that gets waived into meaninglessness.

    Ownership is asserted by the sources that actually model it and that are
    already scope-unit-correct: `Area.ownerUserId`, `DisciplineOwner`, and
    declared-auditee overlap (WP-51). Those still BLOCK. This one informs.
    """
    now = now or _utcnow()
    out: list[Conflict] = []
    for ur in user_roles:
        vt, vf = _aware(getattr(ur, "validTo", None)), _aware(getattr(ur, "validFrom", None))
        if vt and vt < now:
            continue
        if vf and vf > now:
            continue
        if ur.scopeType == "PLANT" and scope.siteId and ur.scopeValue == scope.siteId:
            out.append(
                Conflict(
                    rule="OWN_WORK",
                    severity="WARN",
                    source="ROLE_SCOPE",
                    reason=(
                        "They hold a site-scoped role at the site being audited. Site access "
                        "is not ownership — confirm they are not responsible for any "
                        "discipline in scope."
                    ),
                    detail={"scopeType": "PLANT", "scopeValue": ur.scopeValue},
                )
            )
        elif (
            ur.scopeType == "DEPARTMENT"
            and ur.scopeValue
            and ur.scopeValue in (scope.departments or [])
        ):
            out.append(
                Conflict(
                    rule="OWN_WORK",
                    severity="WARN",
                    source="ROLE_SCOPE",
                    reason=(
                        f"They hold a department-scoped role for “{ur.scopeValue}”, which "
                        "is in scope. Department scope is coarser than audit scope — confirm "
                        "before proceeding."
                    ),
                    detail={"scopeType": "DEPARTMENT", "scopeValue": ur.scopeValue},
                )
            )
    return out


def profile_affinity_conflicts(user: Any, scope: EngagementScope) -> list[Conflict]:
    """Pure, WARN only: `User.plantId` + free-text `User.department` string match.

    `User.department` is not an FK to `Department`, so this is a string
    comparison. A string match is not evidence of ownership and must never
    block — see docs/cams/09 §2.1.4 for why this source is kept deliberately weak.
    """
    if user is None or not scope.siteId or not scope.departments:
        return []
    if user.plantId != scope.siteId or not user.department:
        return []
    dept = user.department.strip().lower()
    if any(dept == (d or "").strip().lower() for d in scope.departments):
        return [
            Conflict(
                rule="OWN_WORK",
                severity="WARN",
                source="PROFILE_AFFINITY",
                reason=(
                    f"Their profile places them in “{user.department}” at this site, "
                    "which is in scope. This is a profile match, not a declared ownership record."
                ),
                detail={"department": user.department},
            )
        ]
    return []


# The per-candidate fetch helpers that used to live here — `resolve_for_scope`,
# `_role_scope_conflicts`, `_profile_affinity_conflicts` — are gone on purpose.
# Every one of them issued a query for ONE user, which is how `check_many`
# quietly became a 362-query fan-out. `check_many` now fetches for the whole
# candidate list and the decisions below stay pure; leaving a per-user fetch
# helper in the module is how that regrows.


# ─────────────────────────────────────────────────────────────────────
# Rule 2 — same-engagement exclusivity
# ─────────────────────────────────────────────────────────────────────


def same_engagement_conflict(
    user_id: str, scope: EngagementScope, *, assigning_as: Literal["AUDITOR", "AUDITEE"]
) -> Conflict | None:
    """A user may never hold both hats on the same engagement.

    This is the rule the checkpoint-allocation path never had, which is how an
    insurance manager came to own 513 audit checkpoints (F-36). It must run
    inside the allocation loop, per row — not once on the engagement header.
    """
    if assigning_as == "AUDITOR":
        if user_id in scope.auditeeUserIds:
            return Conflict(
                rule="SAME_ENGAGEMENT_DUAL_ROLE",
                severity="BLOCK",
                # NOT one of the four ownership sources. Rule 2 fires off this
                # engagement's own roster, and `scope.auditeeUserIds` is built
                # from declared auditees, the plant manager AND checkpoint
                # allocation — so labelling it DECLARED_AUDITEE claimed a
                # provenance it did not have, and the register (which groups on
                # `source`) reported the wrong reason for the block.
                source="SAME_ENGAGEMENT_ROSTER",
                reason=(
                    "They are already an auditee on this engagement. The same person cannot be "
                    "auditor and auditee on one engagement."
                ),
                detail={"engagementId": scope.id},
            )
    else:
        auditors = set(scope.teamAuditorIds) | ({scope.leadAuditorId} if scope.leadAuditorId else set())
        if user_id in auditors:
            return Conflict(
                rule="SAME_ENGAGEMENT_DUAL_ROLE",
                severity="BLOCK",
                # NOT one of the four ownership sources. Rule 2 fires off this
                # engagement's own roster, and `scope.auditeeUserIds` is built
                # from declared auditees, the plant manager AND checkpoint
                # allocation — so labelling it DECLARED_AUDITEE claimed a
                # provenance it did not have, and the register (which groups on
                # `source`) reported the wrong reason for the block.
                source="SAME_ENGAGEMENT_ROSTER",
                reason=(
                    "They are already an auditor on this engagement. The same person cannot be "
                    "auditor and auditee on one engagement."
                ),
                detail={"engagementId": scope.id},
            )
    return None


# ─────────────────────────────────────────────────────────────────────
# Public entry points
# ─────────────────────────────────────────────────────────────────────


async def active_waiver(
    db: AsyncSession, *, engagement_kind: str, engagement_id: str | None, user_id: str
) -> IndependenceWaiver | None:
    if not engagement_id:
        return None
    q = select(IndependenceWaiver).where(
        IndependenceWaiver.engagementKind == engagement_kind,
        IndependenceWaiver.engagementId == engagement_id,
        IndependenceWaiver.subjectUserId == user_id,
        IndependenceWaiver.revokedAt.is_(None),
    )
    return (await db.execute(q)).scalars().first()


def verdict_for(
    user_id: str,
    scope: EngagementScope,
    *,
    assigning_as: Literal["AUDITOR", "AUDITEE"],
    owned: list[OwnedThing],
    user_roles: Iterable[Any] = (),
    user: Any = None,
    waiver: IndependenceWaiver | None = None,
) -> IndependenceVerdict:
    """THE decision, pure. Every caller reaches this and nothing else decides.

    Takes already-loaded facts so the batch path can resolve once for 150
    candidates and still run the identical logic per person. Splitting fetch
    from decision is what makes "one implementation" true rather than aspirational
    — a second query shape cannot drift into a second answer if there is only
    one place an answer is produced.
    """
    conflicts: list[Conflict] = []

    dual = same_engagement_conflict(user_id, scope, assigning_as=assigning_as)
    if dual:
        conflicts.append(dual)

    if assigning_as == "AUDITOR":
        conflicts += declared_auditee_conflicts(owned, scope)
        conflicts += ownership_of_record_conflicts(owned, scope)
        conflicts += vendor_relationship_conflicts(owned, scope)
        conflicts += role_scope_conflicts(user_roles, scope)
        conflicts += profile_affinity_conflicts(user, scope)

    blocking = [c for c in conflicts if c.severity == "BLOCK"]
    return IndependenceVerdict(
        allowed=not blocking or waiver is not None,
        conflicts=conflicts,
        waived=bool(blocking and waiver is not None),
        waiverId=waiver.id if waiver else None,
    )


async def check_many(
    db: AsyncSession,
    *,
    user_ids: Iterable[str],
    scope: EngagementScope,
    assigning_as: Literal["AUDITOR", "AUDITEE"] = "AUDITOR",
) -> dict[str, IndependenceVerdict]:
    """Verdicts for a candidate LIST, in a constant number of queries.

    **This used to be a fan-out wearing a batch's clothes.** It looped
    `check_assignment`, and each call issued ~6 queries of its own: 362 queries
    and **52.9 seconds** for this tenant's 59-candidate site pool. That is fine
    when the caller checks two people it has already chosen; it is unusable for
    a picker that wants status against every visible candidate before anyone is
    chosen.

    Every fetch below is `... IN (candidates)` — five for ownership, one for
    role scope, one for profiles, one for waivers. Eight queries whether the
    list is 1 or 150, and the per-candidate work is then pure arithmetic in
    `verdict_for`.

    The reduction is entirely in the fetching. Not one rule changed, and
    `check_assignment` is now a one-element call into this function so there is
    literally a single code path.
    """
    ids = sorted({u for u in user_ids if u})
    if not ids:
        return {}

    owned_by_user: dict[str, list[OwnedThing]] = {}
    roles_by_user: dict[str, list[Any]] = {}
    users_by_id: dict[str, Any] = {}

    if assigning_as == "AUDITOR":
        # 5 queries for the whole list — `resolve_ownership_sources` takes a
        # candidate list precisely so the register and the guard can share it.
        if scope.siteId or scope.areaIds:
            resolved = await resolve_ownership_sources(
                db,
                user_ids=ids,
                site_id=scope.siteId,
                area_ids=scope.areaIds or [],
                include_inspections=False,
            )
            owned_by_user = {uid: src.owns for uid, src in resolved.items()}

        # WP-45 — one extra query, and only when a supplier is actually the
        # subject. Reached through `services/vendors.py`: independence must not
        # import the vendor model any more than the audit engine may.
        #
        # The import is deferred because `services.vendors` reaches
        # `services.erm_t3` for the scoring helpers, and `erm_t3` imports
        # `segregation_ok` from THIS module — a module-level import would close
        # that cycle at startup.
        if scope.vendorProfileId:
            from app.services import vendors as vendor_svc

            owners = await vendor_svc.relationship_owners(
                db, vendor_ids=[scope.vendorProfileId]
            )
            for vid, info in owners.items():
                uid = info.get("ownerUserId")
                if uid not in ids:
                    continue
                owned_by_user.setdefault(uid, []).append(
                    OwnedThing(
                        source="VENDOR_RELATIONSHIP_OWNER",
                        label=info.get("legalName") or "this supplier",
                        siteId=scope.siteId,
                        detail={
                            "vendorProfileId": vid,
                            "vendorCode": info.get("vendorCode"),
                            "criticality": info.get("criticality"),
                        },
                    )
                )

        for r in (
            await db.execute(
                select(UserRole).where(
                    UserRole.userId.in_(ids), UserRole.scopeType.isnot(None)
                )
            )
        ).scalars().all():
            roles_by_user.setdefault(r.userId, []).append(r)

        # Profile affinity only reads anything when the scope names departments,
        # so the query is skipped rather than loaded and ignored.
        if scope.siteId and scope.departments:
            users_by_id = {
                u.id: u
                for u in (
                    await db.execute(select(User).where(User.id.in_(ids)))
                ).scalars().all()
            }

    waivers: dict[str, IndependenceWaiver] = {}
    if scope.id:
        for w in (
            await db.execute(
                select(IndependenceWaiver).where(
                    IndependenceWaiver.engagementKind == scope.kind,
                    IndependenceWaiver.engagementId == scope.id,
                    IndependenceWaiver.subjectUserId.in_(ids),
                    IndependenceWaiver.revokedAt.is_(None),
                )
            )
        ).scalars().all():
            waivers.setdefault(w.subjectUserId, w)

    return {
        uid: verdict_for(
            uid,
            scope,
            assigning_as=assigning_as,
            owned=owned_by_user.get(uid, []),
            user_roles=roles_by_user.get(uid, ()),
            user=users_by_id.get(uid),
            waiver=waivers.get(uid),
        )
        for uid in ids
    }


async def check_assignment(
    db: AsyncSession,
    *,
    user_id: str,
    scope: EngagementScope,
    assigning_as: Literal["AUDITOR", "AUDITEE"] = "AUDITOR",
) -> IndependenceVerdict:
    """The single question every assignment path asks.

    Returns a verdict rather than raising, so callers can render warnings inline
    and let the user proceed, or convert a block into an HTTP 400 with the
    reason attached. Nothing here mutates.

    A one-element `check_many`, deliberately: the single-candidate and
    candidate-list paths cannot disagree if one calls the other.
    """
    out = await check_many(
        db, user_ids=[user_id], scope=scope, assigning_as=assigning_as
    )
    return out.get(user_id) or IndependenceVerdict(allowed=True)


HAT_FOR_SOURCE = {
    "DECLARED_AUDITEE": "AUDITEE_OWNER",
    "CHECKPOINT_OWNER": "CHECKPOINT_OWNER",
    "AREA_OWNER": "AREA_OWNER",
    "DISCIPLINE_OWNER": "DISCIPLINE_OWNER",
    "VENDOR_RELATIONSHIP_OWNER": "VENDOR_RELATIONSHIP_OWNER",
}

# Ownership that is not tied to an engagement. It is real responsibility — the
# guard BLOCKS on both — but "wears both hats" is a claim about engagements, and
# owning a discipline you have never been audited on is not two hats. It is
# reported as ownership of record instead, which is the honest label.
STANDING_SOURCES = ("AREA_OWNER", "DISCIPLINE_OWNER", "VENDOR_RELATIONSHIP_OWNER")


def summarise_two_hats(sources: OwnershipSources) -> dict[str, Any]:
    """Pure: turn resolved facts into the two-hat view. No DB, no decisions.

    Split out so the shape the register renders is unit-testable against
    hand-built facts, and so `two_hat_summary` cannot drift from the register —
    both call this.
    """
    as_auditor = [r.as_dict() for r in sources.audits]
    as_auditee = [
        {**o.as_dict(), "hat": HAT_FOR_SOURCE.get(o.source, o.source)}
        for o in sources.owns
        if o.engagementId
    ]
    ownership = [o.as_dict() for o in sources.owns if o.source in STANDING_SOURCES]
    return {
        "userId": sources.userId,
        "asAuditor": as_auditor,
        "asAuditee": as_auditee,
        "ownershipOfRecord": ownership,
        "auditorCount": len(as_auditor),
        "auditeeCount": len(as_auditee),
        "ownershipCount": len(ownership),
        "sources": sources.sourcesPresent,
        # The claim the demo makes concrete. Engagement-scoped on both sides.
        "wearsBothHats": bool(as_auditor and as_auditee),
        # Distinct from the above: standing ownership that the guard blocks on
        # even when the person is on no engagement at all.
        "hasOwnershipOfRecord": bool(ownership),
    }


async def two_hat_summary(
    db: AsyncSession, *, user_id: str, since: datetime | None = None
) -> dict[str, Any]:
    """Rule 3 made visible: every engagement this user touches and the hat worn.

    This is the screen to open when a client asks "can the same person be an
    auditor here and an auditee there?" — the answer is a list, not an assertion.

    Reads `resolve_ownership_sources`, the same four sources `check_assignment`
    reads. It previously ran its own narrower query over `ComplianceAudit
    .auditees` alone, so "0 engagements" on this screen meant "this narrower
    query found nothing", not "the guard has nothing on them" — and for three
    live users those two answers disagreed.
    """
    resolved = await resolve_ownership_sources(
        db, user_ids=[user_id], include_auditor_roles=True
    )
    sources = resolved.get(user_id) or OwnershipSources(userId=user_id)
    out = summarise_two_hats(sources)
    if since is not None:
        cutoff = _aware(since)
        keep = lambda r: not r.get("scheduledDate") or _aware(  # noqa: E731
            datetime.fromisoformat(r["scheduledDate"])
        ) >= cutoff
        out["asAuditor"] = [r for r in out["asAuditor"] if keep(r)]
        out["auditorCount"] = len(out["asAuditor"])
        out["wearsBothHats"] = bool(out["asAuditor"] and out["asAuditee"])
    return out


__all__ = [
    "segregation_ok",
    "Conflict",
    "IndependenceVerdict",
    "EngagementScope",
    "scope_for_audit",
    "scope_for_engagement",
    "check_assignment",
    "resolve_ownership_sources",
    "verdict_for",
    "declared_auditee_conflicts",
    "ownership_of_record_conflicts",
    "summarise_two_hats",
    "OwnedThing",
    "OwnershipSources",
    "AuditorRole",
    "check_many",
    "same_engagement_conflict",
    "active_waiver",
    "two_hat_summary",
    # Pure decision cores — the unit-testable half of the own-work guard.
    "area_owner_conflicts",
    "discipline_owner_conflicts",
    "role_scope_conflicts",
    "profile_affinity_conflicts",
]
