"""Child tables for NearMiss — persons involved/affected, witnesses,
CAPAs, attachments, comments. Kept in a separate module to avoid
inflating near_miss.py."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, JSON, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models._base import Base, IdMixin
from app.models.user import User


class NearMissPersonInvolved(Base, IdMixin):
    __tablename__ = "NearMissPersonInvolved"
    __table_args__ = (UniqueConstraint("nearMissId", "userId", name="NMPersonInvolved_uniq"),)

    nearMissId: Mapped[str] = mapped_column(
        ForeignKey("NearMiss.id", ondelete="CASCADE"), nullable=False, index=True
    )
    userId: Mapped[str] = mapped_column(ForeignKey("User.id"), nullable=False)
    role: Mapped[str | None] = mapped_column(String)

    user: Mapped[User] = relationship(foreign_keys=[userId])


class NearMissPersonAffected(Base, IdMixin):
    __tablename__ = "NearMissPersonAffected"
    __table_args__ = (UniqueConstraint("nearMissId", "userId", name="NMPersonAffected_uniq"),)

    nearMissId: Mapped[str] = mapped_column(
        ForeignKey("NearMiss.id", ondelete="CASCADE"), nullable=False, index=True
    )
    userId: Mapped[str] = mapped_column(ForeignKey("User.id"), nullable=False)
    proximityToHazard: Mapped[str | None] = mapped_column(String)

    user: Mapped[User] = relationship(foreign_keys=[userId])


class NearMissWitness(Base, IdMixin):
    __tablename__ = "NearMissWitness"
    __table_args__ = (UniqueConstraint("nearMissId", "witnessId", name="NMWitness_uniq"),)

    nearMissId: Mapped[str] = mapped_column(
        ForeignKey("NearMiss.id", ondelete="CASCADE"), nullable=False, index=True
    )
    witnessId: Mapped[str] = mapped_column(ForeignKey("User.id"), nullable=False)
    statementCaptured: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    witness: Mapped[User] = relationship(foreign_keys=[witnessId])


class NearMissCapa(Base, IdMixin):
    __tablename__ = "NearMissCapa"

    nearMissId: Mapped[str] = mapped_column(
        ForeignKey("NearMiss.id", ondelete="CASCADE"), nullable=False, index=True
    )
    description: Mapped[str] = mapped_column(Text, nullable=False)
    type: Mapped[str] = mapped_column(String, nullable=False)
    ownerId: Mapped[str] = mapped_column(ForeignKey("User.id"), nullable=False)
    targetDate: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False, default="PENDING")
    evidenceUrl: Mapped[str | None] = mapped_column(String)
    evidenceDescription: Mapped[str | None] = mapped_column(Text)
    completionNotes: Mapped[str | None] = mapped_column(Text)
    completedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    verifiedById: Mapped[str | None] = mapped_column(ForeignKey("User.id"))
    verifiedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    rejectionReason: Mapped[str | None] = mapped_column(Text)
    reworkRound: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    workflowTaskId: Mapped[str | None] = mapped_column(String)
    createdAt: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updatedAt: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=func.now(), onupdate=func.now()
    )


class NearMissAttachment(Base, IdMixin):
    __tablename__ = "NearMissAttachment"

    nearMissId: Mapped[str] = mapped_column(
        ForeignKey("NearMiss.id", ondelete="CASCADE"), nullable=False, index=True
    )
    category: Mapped[str] = mapped_column(String, nullable=False, index=True)
    fileName: Mapped[str] = mapped_column(String, nullable=False)
    storagePath: Mapped[str] = mapped_column(String, nullable=False)
    fileSize: Mapped[int] = mapped_column(Integer, nullable=False)
    mimeType: Mapped[str] = mapped_column(String, nullable=False)
    caption: Mapped[str | None] = mapped_column(Text)
    exifData: Mapped[dict | None] = mapped_column(JSON)
    uploadedById: Mapped[str] = mapped_column(ForeignKey("User.id"), nullable=False, index=True)
    uploadedAt: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    deletedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)

    uploadedBy: Mapped[User] = relationship(foreign_keys=[uploadedById], lazy="joined")


class NearMissComment(Base, IdMixin):
    __tablename__ = "NearMissComment"

    nearMissId: Mapped[str] = mapped_column(
        ForeignKey("NearMiss.id", ondelete="CASCADE"), nullable=False, index=True
    )
    authorId: Mapped[str] = mapped_column(ForeignKey("User.id"), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    createdAt: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    author: Mapped[User] = relationship(foreign_keys=[authorId])
