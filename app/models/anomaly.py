from __future__ import annotations

from datetime import datetime

from sqlalchemy import ARRAY, DateTime, ForeignKey, Index, JSON, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models._base import Base, IdMixin


class Anomaly(Base, IdMixin):
    """Detected anomalies surfaced to HSE for review.

    Detector logic (the runner that creates these rows) still lives on the
    Node side — porting that ML/statistics code is a separate task. The
    Python side only handles READ + status transitions.
    """

    __tablename__ = "Anomaly"
    __table_args__ = (
        Index("ix_anomaly_status_detected", "status", "detectedAt"),
        Index("ix_anomaly_plant_status", "plantId", "status"),
        Index("ix_anomaly_detector_status", "detectorId", "status"),
    )

    detectedAt: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    # Detector identity. Known: FREQUENCY_SPIKE | SEVERITY_DRIFT |
    # HOTSPOT_CLUSTER | PERSON_OF_CONCERN | CROSS_CORRELATION
    detectorId: Mapped[str] = mapped_column(String, nullable=False)
    module: Mapped[str] = mapped_column(String, nullable=False)

    # Subject — what the anomaly is about
    plantId: Mapped[str | None] = mapped_column(ForeignKey("Plant.id"))
    category: Mapped[str | None] = mapped_column(String)
    area: Mapped[str | None] = mapped_column(String)
    personId: Mapped[str | None] = mapped_column(ForeignKey("User.id"))

    # INFO | WARNING | CRITICAL
    severity: Mapped[str] = mapped_column(String, nullable=False, default="WARNING")

    # Algorithm-specific signal payload + human description
    signalData: Mapped[dict] = mapped_column(JSON, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)

    contributingRecordIds: Mapped[list[str]] = mapped_column(
        ARRAY(String), nullable=False, default=list, server_default="{}"
    )

    # PENDING_REVIEW → ACKNOWLEDGED → CONFIRMED|DISMISSED|EXPIRED
    status: Mapped[str] = mapped_column(String, nullable=False, default="PENDING_REVIEW")
    reviewerId: Mapped[str | None] = mapped_column(ForeignKey("User.id"))
    reviewedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reviewNote: Mapped[str | None] = mapped_column(Text)

    # Detector-side de-dup. Not enforced here — the Node detector populates it.
    fingerprint: Mapped[str | None] = mapped_column(String, unique=True)
    emailNotifiedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
