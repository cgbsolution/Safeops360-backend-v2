from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, JSON, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models._base import Base, IdMixin, TimestampMixin

if TYPE_CHECKING:
    from app.models.plant import Plant


class User(Base, IdMixin):
    __tablename__ = "User"

    email: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String, nullable=False)
    passwordHash: Mapped[str] = mapped_column(String, nullable=False)
    # Denormalised "primary role" — kept for back-compat with code paths still
    # reading session.user.role. New code asks the permission service via UserRole.
    role: Mapped[str] = mapped_column(String, nullable=False, default="WORKER")
    plantId: Mapped[str | None] = mapped_column(ForeignKey("Plant.id"))
    designation: Mapped[str | None] = mapped_column(String)
    department: Mapped[str | None] = mapped_column(String)
    createdAt: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    plant: Mapped[Plant | None] = relationship(back_populates="users")
    user_roles: Mapped[list[UserRole]] = relationship(
        back_populates="user",
        foreign_keys="UserRole.userId",
        cascade="all, delete-orphan",
    )


class Role(Base, IdMixin, TimestampMixin):
    __tablename__ = "Role"

    code: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String, nullable=False)
    description: Mapped[str | None] = mapped_column(String)
    isSystem: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    isActive: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    sortOrder: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    defaultLanding: Mapped[str | None] = mapped_column(String)

    permissions: Mapped[list[RolePermission]] = relationship(
        back_populates="role", cascade="all, delete-orphan"
    )
    users: Mapped[list[UserRole]] = relationship(back_populates="role", cascade="all, delete-orphan")


class UserRole(Base, IdMixin):
    __tablename__ = "UserRole"
    __table_args__ = (
        UniqueConstraint("userId", "roleId", "scopeType", "scopeValue", name="uq_user_role_scope"),
    )

    userId: Mapped[str] = mapped_column(ForeignKey("User.id", ondelete="CASCADE"), nullable=False, index=True)
    roleId: Mapped[str] = mapped_column(ForeignKey("Role.id", ondelete="CASCADE"), nullable=False, index=True)
    scopeType: Mapped[str | None] = mapped_column(String)  # PLANT | DEPARTMENT | None
    scopeValue: Mapped[str | None] = mapped_column(String)
    validFrom: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    validTo: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    assignedAt: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    assignedById: Mapped[str | None] = mapped_column(ForeignKey("User.id"))

    user: Mapped[User] = relationship(back_populates="user_roles", foreign_keys=[userId])
    role: Mapped[Role] = relationship(back_populates="users")


class Permission(Base, IdMixin):
    __tablename__ = "Permission"

    code: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    module: Mapped[str] = mapped_column(String, nullable=False)
    action: Mapped[str] = mapped_column(String, nullable=False)
    description: Mapped[str | None] = mapped_column(String)
    createdAt: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    role_permissions: Mapped[list[RolePermission]] = relationship(
        back_populates="permission", cascade="all, delete-orphan"
    )


class RolePermission(Base, IdMixin):
    __tablename__ = "RolePermission"
    __table_args__ = (
        UniqueConstraint("roleId", "permissionId", name="uq_role_permission"),
    )

    roleId: Mapped[str] = mapped_column(ForeignKey("Role.id", ondelete="CASCADE"), nullable=False, index=True)
    permissionId: Mapped[str] = mapped_column(
        ForeignKey("Permission.id", ondelete="CASCADE"), nullable=False, index=True
    )
    scope: Mapped[str] = mapped_column(String, nullable=False, default="OWN_PLANT")
    conditions: Mapped[dict | None] = mapped_column(JSON)

    role: Mapped[Role] = relationship(back_populates="permissions")
    permission: Mapped[Permission] = relationship(back_populates="role_permissions")
