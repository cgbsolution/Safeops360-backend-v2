"""Minimal supplier portal — token-scoped, single-audit external access.

A vendor factory manager will never hold a platform seat, so a supplier
corrective-action response has to arrive through something other than a login.
This is that surface, and it is deliberately the SMALLEST thing that closes the
loop: one opaque token, one audit, read + two writes, time-limited.

**Why external submissions get their own table.** The obvious move is to write
supplier comments into `CapaComment` and supplier files into `Attachment`. Both
have `authorUserId` / `uploadedById` as **NOT NULL foreign keys to `User`** — so
recording a supplier there requires either inventing a User row for a vendor
contact (a fake identity in the RBAC system, which is worse than the problem) or
widening two core platform tables every module depends on. Neither is
justifiable for a first portal.

So external input is its own append-only record. The internal CAPA view reads
both and renders them side by side, which also satisfies the requirement that a
supplier response be *visually distinguishable* from an internal user updating
on the supplier's behalf — here that distinction is structural, not a flag
someone can forget to set.

**Nothing here grants access to anything but its own audit.** The token carries
the audit id; every read and write re-derives scope from the token row rather
than from anything the caller sends.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models._base import Base, IdMixin


class SupplierPortalToken(Base, IdMixin):
    """One opaque credential, scoped to exactly one audit.

    **The raw token is never stored.** Only a SHA-256 hash is persisted, so a
    database read cannot be replayed as portal access — the same reason password
    hashes exist. The plaintext is returned once, at issue time, to be emailed;
    re-sharing means re-issuing, which revokes the previous token by design.
    """

    __tablename__ = "SupplierPortalToken"

    engagementKind: Mapped[str] = mapped_column(String, nullable=False, default="AUDIT")
    auditId: Mapped[str] = mapped_column(String, nullable=False, index=True)
    vendorProfileId: Mapped[str | None] = mapped_column(String, index=True)

    tokenHash: Mapped[str] = mapped_column(String, nullable=False, unique=True, index=True)
    # First few characters, kept in clear ONLY so support and the access log can
    # refer to a token without holding one that works.
    tokenPrefix: Mapped[str] = mapped_column(String, nullable=False)

    supplierContactEmail: Mapped[str] = mapped_column(String, nullable=False)
    supplierContactName: Mapped[str | None] = mapped_column(String)

    expiresAt: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    createdAt: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    createdById: Mapped[str | None] = mapped_column(String)

    revokedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revokedById: Mapped[str | None] = mapped_column(String)
    revokedReason: Mapped[str | None] = mapped_column(Text)

    lastAccessedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    accessCount: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    emailSentAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        Index("ix_SupplierPortalToken_audit_live", "auditId", "revokedAt", "expiresAt"),
    )


class SupplierPortalSubmission(Base, IdMixin):
    """Something the SUPPLIER sent us. Append-only.

    `kind`:
      COMMENT  — free text against a finding.
      EVIDENCE — a file the supplier uploaded against a finding or CAPA.

    The actor is recorded as the token's contact identity, never as a user id.
    That is what makes "supplier said this" and "our engineer typed this on
    their behalf" impossible to confuse downstream.
    """

    __tablename__ = "SupplierPortalSubmission"

    tokenId: Mapped[str] = mapped_column(
        ForeignKey("SupplierPortalToken.id", ondelete="CASCADE"), nullable=False, index=True
    )
    auditId: Mapped[str] = mapped_column(String, nullable=False, index=True)

    kind: Mapped[str] = mapped_column(String, nullable=False)  # COMMENT | EVIDENCE

    # What it is about. A submission always names a finding; `capaId` is set
    # when that finding has already produced a CAPA.
    checkpointResponseId: Mapped[str | None] = mapped_column(String, index=True)
    capaId: Mapped[str | None] = mapped_column(String, index=True)

    body: Mapped[str] = mapped_column(Text, nullable=False, default="")

    # EVIDENCE only.
    fileName: Mapped[str | None] = mapped_column(String)
    storagePath: Mapped[str | None] = mapped_column(String)
    fileSize: Mapped[int | None] = mapped_column(Integer)
    mimeType: Mapped[str | None] = mapped_column(String)

    # The external actor, denormalised at submission time so it survives the
    # token being revoked or the vendor contact changing.
    submittedByEmail: Mapped[str] = mapped_column(String, nullable=False)
    submittedByName: Mapped[str | None] = mapped_column(String)
    submittedAt: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    # Internal triage of external input. A supplier cannot close their own
    # finding — an internal owner still has to accept the evidence.
    acknowledgedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    acknowledgedById: Mapped[str | None] = mapped_column(String)

    __table_args__ = (
        Index("ix_SupplierPortalSubmission_audit_kind", "auditId", "kind"),
    )


class SupplierPortalAccessLog(Base, IdMixin):
    """Every attempt against a portal token, successful or not.

    An external surface that handles corrective-action evidence needs to be able
    to answer "who tried what, when" after the fact. Failures are logged
    *especially* — a run of NOT_FOUND against sequential tokens is the signal
    that matters, and it is invisible if only successes are recorded.

    `tokenId` is null when the token did not resolve, which is exactly the case
    worth keeping.
    """

    __tablename__ = "SupplierPortalAccessLog"

    tokenId: Mapped[str | None] = mapped_column(String, index=True)
    tokenPrefix: Mapped[str | None] = mapped_column(String, index=True)
    auditId: Mapped[str | None] = mapped_column(String, index=True)

    # OK | EXPIRED | REVOKED | NOT_FOUND | RATE_LIMITED | AUDIT_MISSING
    outcome: Mapped[str] = mapped_column(String, nullable=False, index=True)
    action: Mapped[str] = mapped_column(String, nullable=False)  # VIEW | COMMENT | EVIDENCE | UPLOAD_URL

    ipAddress: Mapped[str | None] = mapped_column(String)
    userAgent: Mapped[str | None] = mapped_column(String)

    at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True
    )
    isWrite: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)


__all__ = [
    "SupplierPortalToken",
    "SupplierPortalSubmission",
    "SupplierPortalAccessLog",
]
