"""Assurance integrity — auditor independence, competence, meetings, errata.

Designed in [docs/cams/09-module-completion.md](../../../docs/cams/09-module-completion.md) Part 2.

The premise of this module is that CAMS already models auditor and auditee
correctly — both are **engagement-scoped** (`ComplianceAudit.leadAuditorUserId` /
`coAuditors` / `auditees` and the per-checkpoint `assignedAuditorId` /
`assignedOwnerId`), and there is no global `AUDITOR` role in RBAC. What was
missing was every *guard* over that model, and every surface that made a user's
two hats visible. These tables are those guards' records — not a new role model.

Nothing here is a Prisma mirror: these are NEW tables owned SQLAlchemy-side and
applied by `scripts/add_assurance_integrity.py`, with a matching Prisma block so
the two schemas do not drift (the mechanism behind the module's worst defect).
camelCase column names match the platform-wide Prisma convention.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Index,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models._base import Base, IdMixin


# ─── Ownership (Q17) ──────────────────────────────────────────────────────


class DisciplineOwner(Base, IdMixin):
    """Who is responsible for a discipline at a site.

    The platform had no discipline↔owner mapping of any kind, which is why
    [09 §2.1.4](../../../docs/cams/09-module-completion.md) reported the
    own-work guard as underivable at discipline level. This is that mapping,
    kept deliberately thin: a site, a discipline code (the audit library's
    `categoryId`), and a user.

    `plantId` NULL means estate-wide ownership — a group HSE lead who owns Fire
    Safety across every factory. That person should not audit Fire Safety
    anywhere, and the guard reads it that way.
    """

    __tablename__ = "DisciplineOwner"

    plantId: Mapped[str | None] = mapped_column(String, index=True)
    disciplineCode: Mapped[str] = mapped_column(String, nullable=False, index=True)
    disciplineLabel: Mapped[str] = mapped_column(String, nullable=False, default="")
    ownerUserId: Mapped[str] = mapped_column(String, nullable=False, index=True)
    # ACCOUNTABLE (owns the outcome) | RESPONSIBLE (runs it day to day).
    # Both block; the distinction is rendered in the block reason so the message
    # says something true rather than something generic.
    ownershipType: Mapped[str] = mapped_column(String, nullable=False, default="ACCOUNTABLE")
    isActive: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    createdAt: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    createdBy: Mapped[str | None] = mapped_column(String)

    __table_args__ = (
        UniqueConstraint(
            "plantId", "disciplineCode", "ownerUserId", name="uq_DisciplineOwner_scope"
        ),
        Index("ix_DisciplineOwner_lookup", "plantId", "disciplineCode", "isActive"),
    )


# ─── Independence (§2.1) ──────────────────────────────────────────────────


class IndependenceWaiver(Base, IdMixin):
    """A governed, visible exception to an independence rule.

    ISO 19011 acknowledges proportionality — a single-site organisation may not
    be able to staff full independence. What matters is that the exception is
    visible, not that it is impossible. So this row is required to carry a
    justification and a named approver, and it is rendered in the engagement's
    report (never an appendix footnote).

    `approvedByUserId` must not be `subjectUserId` — enforced in the service via
    the shared `segregation_ok`, the same helper ERM Internal Controls uses.
    """

    __tablename__ = "IndependenceWaiver"

    engagementKind: Mapped[str] = mapped_column(String, nullable=False)  # AUDIT | INSPECTION
    engagementId: Mapped[str] = mapped_column(String, nullable=False, index=True)
    subjectUserId: Mapped[str] = mapped_column(String, nullable=False, index=True)
    # OWN_WORK | SAME_ENGAGEMENT_DUAL_ROLE
    ruleViolated: Mapped[str] = mapped_column(String, nullable=False)
    # Machine-readable copy of what the guard said at waiver time, so the report
    # can state the original conflict even after the underlying data changes.
    conflictDetail: Mapped[dict | None] = mapped_column(JSON)
    justification: Mapped[str] = mapped_column(Text, nullable=False)
    approvedByUserId: Mapped[str] = mapped_column(String, nullable=False)
    approvedAt: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    # ENGAGEMENT | CHECKPOINT_SET
    scope: Mapped[str] = mapped_column(String, nullable=False, default="ENGAGEMENT")
    checkpointCodes: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    revokedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revokedByUserId: Mapped[str | None] = mapped_column(String)

    createdAt: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        Index("ix_IndependenceWaiver_engagement", "engagementKind", "engagementId"),
        Index("ix_IndependenceWaiver_subject", "subjectUserId", "revokedAt"),
    )


class IndependenceEvent(Base, IdMixin):
    """Append-only record of every time the independence guard reached a verdict.

    **The gap this closes.** Before it, a blocked assignment left nothing behind:
    `create_audit` raised `ValueError` → HTTP 400 and the transaction rolled
    back; preflight returned a verdict and wrote nothing. So the product enforced
    independence perfectly and could not prove it had ever done so. The only
    evidence of the guard existing was the waiver table — which is evidence of
    the guard being *overridden*, and it had zero rows.

    For a certification-facing demo this is the more valuable half: **a blocked
    attempt that was never overridden is stronger evidence of enforcement than a
    waiver is.** A waiver says "we noticed and allowed it"; a block says "we
    noticed and stopped".

    Append-only by discipline — nothing updates a row here, and the register is
    `IndependenceEvent LEFT JOIN IndependenceWaiver`. `engagementId` is nullable
    because the attempt legitimately precedes engagement creation (the schedule
    wizard pre-flights a team before the audit row exists), and that is exactly
    the attempt worth recording: it is the one that never became an engagement.

    Written from a session of its own, so an event survives the rollback of the
    business transaction that produced it.
    """

    __tablename__ = "IndependenceEvent"

    occurredAt: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True
    )
    attemptedByUserId: Mapped[str | None] = mapped_column(String, index=True)
    subjectUserId: Mapped[str] = mapped_column(String, nullable=False, index=True)
    engagementKind: Mapped[str] = mapped_column(String, nullable=False)  # AUDIT | INSPECTION
    engagementId: Mapped[str | None] = mapped_column(String, index=True)
    engagementCode: Mapped[str | None] = mapped_column(String)
    siteId: Mapped[str | None] = mapped_column(String, index=True)
    # BLOCKED  — the guard refused the assignment
    # WARNED   — non-blocking conflict shown; the caller could proceed
    # WAIVED   — a block was overridden by a governed waiver
    # CLEARED  — a previously blocked/warned subject came back clean
    outcome: Mapped[str] = mapped_column(String, nullable=False, index=True)
    # Which check_assignment rule fired, and which of the four ownership sources
    # produced it — the two columns that make the register answer "why".
    rule: Mapped[str | None] = mapped_column(String)
    source: Mapped[str | None] = mapped_column(String)
    # The message shown to the user AT THE TIME. Frozen, because the underlying
    # ownership can change and the record must still say what was decided.
    reason: Mapped[str] = mapped_column(Text, nullable=False, default="")
    conflictDetail: Mapped[dict | None] = mapped_column(JSON)
    waiverId: Mapped[str | None] = mapped_column(String, index=True)
    # PREFLIGHT | CREATE_AUDIT | WAIVER_GRANT | WAIVER_REVOKE | SEED
    origin: Mapped[str] = mapped_column(String, nullable=False, default="PREFLIGHT")

    createdAt: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        Index("ix_IndependenceEvent_subject_time", "subjectUserId", "occurredAt"),
        Index("ix_IndependenceEvent_outcome_time", "outcome", "occurredAt"),
        Index("ix_IndependenceEvent_engagement", "engagementKind", "engagementId"),
    )


# ─── Competence (§2.2) ────────────────────────────────────────────────────


class EngagementCompetenceSnapshot(Base, IdMixin):
    """What the Skill Matrix said about this auditor at assignment time.

    Same principle as the template snapshot, and for the same reason: a
    certification body asks "was this person qualified when the audit was
    conducted?", and a live `CompetencyRecord` read cannot answer that after a
    revalidation, an expiry or a suspension.

    `state` / `validUntil` are copied from `CompetencyRecord`; `held` is the
    resolved verdict at capture time so the report does not have to re-derive it.
    """

    __tablename__ = "EngagementCompetenceSnapshot"

    engagementKind: Mapped[str] = mapped_column(String, nullable=False)
    engagementId: Mapped[str] = mapped_column(String, nullable=False, index=True)
    userId: Mapped[str] = mapped_column(String, nullable=False, index=True)
    competencyId: Mapped[str] = mapped_column(String, nullable=False)
    competencyCode: Mapped[str] = mapped_column(String, nullable=False, default="")
    competencyName: Mapped[str] = mapped_column(String, nullable=False, default="")
    state: Mapped[str | None] = mapped_column(String)
    validUntil: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    externalCertificateReference: Mapped[str | None] = mapped_column(String)
    held: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # Set when the assignment proceeded despite a gap (warn mode).
    waivedGap: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    capturedAt: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    capturedByUserId: Mapped[str | None] = mapped_column(String)

    __table_args__ = (
        Index("ix_EngCompSnapshot_eng_user", "engagementKind", "engagementId", "userId"),
    )


# ─── Meetings (§2.3) ──────────────────────────────────────────────────────


class EngagementMeeting(Base, IdMixin):
    """Opening / closing meeting record — ISO 19011 §6.4.2 and §6.4.9.

    `ComplianceAudit.openingRemarks` / `closingRemarks` are free-text columns
    with no attendees, no time and no acknowledgement, so the report could only
    ever assert a meeting the product had no evidence of. This is the evidence.

    `attendees` is a JSON list of either `{userId}` or
    `{name, organisation, role}` — buyer auditors, certification-body assessors
    and contractor representatives attend these meetings and are not users.
    """

    __tablename__ = "EngagementMeeting"

    engagementKind: Mapped[str] = mapped_column(String, nullable=False)
    engagementId: Mapped[str] = mapped_column(String, nullable=False, index=True)
    meetingType: Mapped[str] = mapped_column(String, nullable=False)  # OPENING | CLOSING
    heldAt: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    attendees: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    # Opening only.
    scopeConfirmed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # Closing only.
    findingsSummaryPresented: Mapped[str | None] = mapped_column(Text)
    auditeeAcknowledgedByUserId: Mapped[str | None] = mapped_column(String)
    auditeeAcknowledgedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    notes: Mapped[str | None] = mapped_column(Text)

    recordedByUserId: Mapped[str] = mapped_column(String, nullable=False)
    createdAt: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updatedAt: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=func.now(), onupdate=func.now(), nullable=False
    )

    __table_args__ = (
        UniqueConstraint(
            "engagementKind", "engagementId", "meetingType", name="uq_EngagementMeeting_type"
        ),
        Index("ix_EngagementMeeting_engagement", "engagementKind", "engagementId"),
    )


# ─── Report integrity (§2.5) ──────────────────────────────────────────────


class ReportErratum(Base, IdMixin):
    """An append-only correction to an issued report.

    The alternative — editing the snapshot — destroys the integrity hash's
    meaning. An erratum leaves the original snapshot and its hash untouched and
    renders as a dated block at the head of the report, which is exactly how a
    certification body expects a correction to a issued document to behave.
    """

    __tablename__ = "ReportErratum"

    reportId: Mapped[str] = mapped_column(String, nullable=False, index=True)
    auditId: Mapped[str] = mapped_column(String, nullable=False, index=True)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    raisedByUserId: Mapped[str] = mapped_column(String, nullable=False)
    approvedByUserId: Mapped[str] = mapped_column(String, nullable=False)
    createdAt: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        UniqueConstraint("reportId", "sequence", name="uq_ReportErratum_seq"),
    )


__all__ = [
    "DisciplineOwner",
    "IndependenceEvent",
    "IndependenceWaiver",
    "EngagementCompetenceSnapshot",
    "EngagementMeeting",
    "ReportErratum",
]
