from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, ForeignKey, String, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models._base import Base, IdMixin

if TYPE_CHECKING:
    from app.models.user import User


# Plant + Area only have createdAt in the Prisma schema (no updatedAt) —
# don't use TimestampMixin which would also add an updatedAt column.
# Mixing one in here would cause queries to reference a non-existent column.
class Plant(Base, IdMixin):
    __tablename__ = "Plant"

    code: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String, nullable=False)
    location: Mapped[str] = mapped_column(String, nullable=False)
    state: Mapped[str] = mapped_column(String, nullable=False)
    unitType: Mapped[str] = mapped_column(String, nullable=False)

    # Exchange resource mailbox booked for this site's audit opening/closing
    # meetings. A site-level default rather than a per-audit choice because the
    # client's requirement is that scheduling an audit books everything at once
    # — asking a scheduler to pick a room every time would mean the room is the
    # one thing that is usually forgotten. Overridable per booking.
    #
    # DEFERRED, and that is not a performance decision. `Plant` is on the login
    # path (`/api/auth/login` resolves the user's plant), so if these columns
    # were in the default SELECT, deploying this model before running
    # `scripts/add_calendar_bookings.py` would make EVERY login fail with
    # "column does not exist". Deferring them keeps them out of every ordinary
    # Plant query; only the calendar's room lookup touches them, and that call
    # is already wrapped to degrade to "no default room".
    #
    # A meeting-room convenience must never be able to lock people out of the
    # product.
    defaultMeetingRoomEmail: Mapped[str | None] = mapped_column(String, deferred=True)
    defaultMeetingRoomName: Mapped[str | None] = mapped_column(String, deferred=True)

    createdAt: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    users: Mapped[list[User]] = relationship(back_populates="plant")
    areas: Mapped[list[Area]] = relationship(back_populates="plant", cascade="all, delete-orphan")


class Area(Base, IdMixin):
    __tablename__ = "Area"

    name: Mapped[str] = mapped_column(String, nullable=False)
    plantId: Mapped[str] = mapped_column(ForeignKey("Plant.id"), nullable=False)
    # Responsible owner for this area (docs/cams/09 §2.1.4, open question Q17).
    # Before this column the platform had NO area-level ownership at all, which
    # meant the audit own-work independence guard could only reason at site and
    # department granularity. Nullable: most areas are unowned, and an absent
    # owner must degrade the guard to "no signal", never to "no conflict".
    ownerUserId: Mapped[str | None] = mapped_column(String, index=True)
    createdAt: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    plant: Mapped[Plant] = relationship(back_populates="areas")
