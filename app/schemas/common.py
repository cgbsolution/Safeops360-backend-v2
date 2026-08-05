"""Shared, cross-module schemas.

`UserRefOut` is a compact, display-ready reference to a user. Use it to keep
raw user IDs out of API responses: instead of returning an opaque cuid the UI
would have to render verbatim, attach a directory of these refs (name + plant
+ role) so any user id the payload carries can be shown as a person.

Resolve a set of IDs to a `{id: UserRefOut}` map with
`app.services.user_directory.resolve_user_directory`.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class UserRefOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    # `role` is the denormalised primary role (User.role) — the same value the
    # role badge reads. Operational overlays (UserRole assignments) are not
    # collapsed in here; this is the at-a-glance "who is this person" role.
    role: str | None = None
    designation: str | None = None
    department: str | None = None
    plantId: str | None = None
    plantName: str | None = None
    plantCode: str | None = None
