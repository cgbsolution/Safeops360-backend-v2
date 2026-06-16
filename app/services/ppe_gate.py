"""PTW PPE compliance gate (PPE-01 Pass 2 — Build Prompt §6.4 / §9.1).

Single source of truth for "does person X hold valid PPE for situation Y".
Mirrors the shape of the canonical competency check (`competency.py::
CompetencyCheckResult`) so the PTW gates can treat both identically:
blockers fail the gate, warnings render a chip but allow.

What counts as "required PPE" for a crew member on a permit:

  1. Their role-based requirement profile (`PpeRequirementProfile`,
     scopeType="role", their role + the *ALL* base set) — MANDATORY items
     block, recommended items only warn. Same rule as People Compliance.
  2. Permit-type PPE: every active `PpeType` whose `enablesPermitTypes`
     contains the permit type code (e.g. HARNESS-FULLBODY-EN361 enables
     WORK_AT_HEIGHT). Always mandatory for that permit — but types sharing
     a (category, subcategory) are interchangeable VARIANTS (e.g. the two
     safety-helmet models, shock-absorbing vs twin-tail lanyard), so each
     subcategory group is satisfied by ANY ONE of its members.

What counts as "holding valid PPE":

  • An ACTIVE `PpeIssuance` of that type at the same plant, AND
  • the recipient has acknowledged receipt (Build Prompt §6.2 — an
    issued-but-unacknowledged item is a paper issuance, not protection), AND
  • the underlying item passes `ppe_inventory.item_validity` (not retired /
    recalled / service-life exceeded / inspection overdue / unserviceable).

This check is LIVE — the activation gate calls it at transition time rather
than trusting the `ppeValidAtIssuance` snapshot on `PermitCrewMember`,
because PPE state (returns, recalls, inspection failures) moves faster than
training records. The snapshot is still written at crew-add for the audit
trail, exactly like `trainingValidAtIssuance`.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.ppe import PpeIssuance, PpeItem, PpeRequirementProfile, PpeType
from app.models.user import User
from app.services.ppe_inventory import ALL_ROLES, _dedup_requirements, item_validity

# ─── Result shapes (parallel to CompetencyBlocker / CompetencyCheckResult) ─


@dataclass
class PpeGateGap:
    """One reason (or near-miss) for the PPE gate on one person."""

    ppeTypeCode: str
    ppeTypeName: str
    code: str  # NOT_ISSUED | NOT_ACKNOWLEDGED | ITEM_INVALID | RECOMMENDED_MISSING | ITEM_WARNING
    message: str


@dataclass
class PpeCheckResult:
    ok: bool = True
    blockers: list[PpeGateGap] = field(default_factory=list)
    warnings: list[PpeGateGap] = field(default_factory=list)
    # PPE type codes the user holds valid (for UI green-tick rendering)
    satisfied: list[str] = field(default_factory=list)

    def summary(self) -> str:
        """Compact one-liner for gate blocker messages."""
        return "; ".join(b.message for b in self.blockers)


# ─── Requirement lookup ───────────────────────────────────────────────────


# PermitType enum (UPPERCASE) → catalog `enablesPermitTypes` token (lowercase).
# The catalog speaks lowercase and splits electrical into LV/HT; LOTO work is
# the LV case.
_PERMIT_TYPE_ALIASES = {"electrical_loto": "electrical"}


def _catalog_permit_token(permit_type_code: str) -> str:
    token = permit_type_code.lower()
    return _PERMIT_TYPE_ALIASES.get(token, token)


async def get_permit_type_ppe(db: AsyncSession, permit_type_code: str) -> list[PpeType]:
    """Active PPE types whose `enablesPermitTypes` JSONB array contains the
    permit type code. Catalog is small (~40 rows) so filter in Python rather
    than relying on dialect-specific JSONB operators."""
    token = _catalog_permit_token(permit_type_code)
    rows = (
        await db.execute(select(PpeType).where(PpeType.isActive.is_(True)))
    ).scalars().all()
    return [
        t
        for t in rows
        if token in {str(x).lower() for x in (t.enablesPermitTypes or [])}
    ]


async def _role_requirements(
    db: AsyncSession, plant_id: str
) -> dict[str, list[dict]]:
    """role → requirement dicts ({ppe_type_code, ppe_type_name,
    requirement_level}), with the *ALL* base set under its own key."""
    profiles = (
        await db.execute(
            select(PpeRequirementProfile)
            .where(PpeRequirementProfile.plantId == plant_id)
            .where(PpeRequirementProfile.isActive.is_(True))
            .where(PpeRequirementProfile.scopeType == "role")
        )
    ).scalars().all()
    role_reqs: dict[str, list[dict]] = {}
    for p in profiles:
        role_reqs.setdefault(p.scopeId, []).extend(p.requiredPpe or [])
    return role_reqs


# ─── Core check ───────────────────────────────────────────────────────────


async def check_ppe_for_crew(
    db: AsyncSession,
    *,
    plant_id: str,
    user_ids: list[str],
    permit_type_code: str | None = None,
) -> dict[str, PpeCheckResult]:
    """Batch PPE check for a permit crew. Returns {user_id: PpeCheckResult}.
    Users with no requirements at all come back ok=True with empty lists."""
    results: dict[str, PpeCheckResult] = {u: PpeCheckResult() for u in user_ids}
    if not user_ids:
        return results

    users = (
        await db.execute(select(User).where(User.id.in_(user_ids)))
    ).scalars().all()
    users_by_id = {u.id: u for u in users}

    role_reqs = await _role_requirements(db, plant_id)
    base_reqs = role_reqs.get(ALL_ROLES, [])

    # Permit-type requirements as variant groups: any ONE member of a
    # (category, subcategory) group satisfies it.
    permit_groups: list[dict] = []
    if permit_type_code:
        grouped: dict[tuple[str, str], list[PpeType]] = {}
        for t in await get_permit_type_ppe(db, permit_type_code):
            grouped.setdefault((t.category, t.subcategory), []).append(t)
        permit_groups = [
            {"codes": [t.code for t in g], "names": [t.name for t in g]}
            for g in grouped.values()
        ]

    # Active issuances at this plant for the crew → holdings per user.
    issuances = (
        await db.execute(
            select(PpeIssuance)
            .where(PpeIssuance.plantId == plant_id)
            .where(PpeIssuance.status == "active")
            .where(PpeIssuance.issuedToUserId.in_(user_ids))
        )
    ).scalars().all()
    item_ids = {i.ppeItemId for i in issuances}
    items_by_id: dict[str, PpeItem] = {}
    if item_ids:
        rows = (
            await db.execute(select(PpeItem).where(PpeItem.id.in_(item_ids)))
        ).scalars().all()
        items_by_id = {it.id: it for it in rows}
    held_by_user: dict[str, list[PpeIssuance]] = {}
    for iss in issuances:
        held_by_user.setdefault(iss.issuedToUserId, []).append(iss)

    for user_id in user_ids:
        result = results[user_id]
        user = users_by_id.get(user_id)
        if user is None:
            result.ok = False
            result.blockers.append(
                PpeGateGap(
                    ppeTypeCode="—",
                    ppeTypeName="—",
                    code="USER_NOT_FOUND",
                    message="user record not found",
                )
            )
            continue

        reqs = _dedup_requirements(base_reqs + role_reqs.get(user.role or "", []))
        holdings = held_by_user.get(user_id, [])

        def evaluate(type_code: str) -> tuple[str, str]:
            """Does this user hold a valid, acknowledged item of this type?
            Returns (state, detail) — state ∈ missing | unacknowledged |
            invalid | warn | pass. A user can hold SEVERAL items of one type
            (e.g. an expired pair not yet returned plus its replacement) —
            the BEST holding wins, never the first one found."""
            rank = {"pass": 0, "warn": 1, "invalid": 2, "unacknowledged": 3, "missing": 4}
            best: tuple[str, str] = ("missing", "not issued")
            for iss in holdings:
                if iss.ppeTypeCode != type_code:
                    continue
                if not iss.recipientAcknowledged:
                    # Paper issuance — §6.2: doesn't count until acknowledged.
                    outcome = ("unacknowledged", "issued but receipt not acknowledged")
                else:
                    item = items_by_id.get(iss.ppeItemId)
                    v = (
                        item_validity(item)
                        if item is not None
                        else {"level": "block", "blockers": ["item record missing"], "warnings": []}
                    )
                    if v["level"] == "block":
                        outcome = ("invalid", "; ".join(v["blockers"]))
                    elif v["level"] == "warn":
                        outcome = ("warn", "; ".join(v["warnings"]))
                    else:
                        return "pass", ""
                if rank[outcome[0]] < rank[best[0]]:
                    best = outcome
            return best

        # 1. Role-profile requirements — one explicit type code each.
        for req in reqs:
            type_code = req.get("ppe_type_code")
            type_name = req.get("ppe_type_name", type_code)
            is_mandatory = req.get("requirement_level", "mandatory") == "mandatory"
            state, detail = evaluate(type_code)
            if state == "missing":
                if is_mandatory:
                    result.ok = False
                    result.blockers.append(
                        PpeGateGap(type_code, type_name, "NOT_ISSUED", f"{type_name} not issued")
                    )
                else:
                    result.warnings.append(
                        PpeGateGap(
                            type_code, type_name, "RECOMMENDED_MISSING",
                            f"{type_name} (recommended) not issued",
                        )
                    )
            elif state == "unacknowledged":
                if is_mandatory:
                    result.ok = False
                    result.blockers.append(
                        PpeGateGap(type_code, type_name, "NOT_ACKNOWLEDGED", f"{type_name} {detail}")
                    )
            elif state == "invalid":
                if is_mandatory:
                    result.ok = False
                    result.blockers.append(
                        PpeGateGap(
                            type_code, type_name, "ITEM_INVALID",
                            f"{type_name} unserviceable ({detail})",
                        )
                    )
            elif state == "warn":
                result.satisfied.append(type_code)
                result.warnings.append(
                    PpeGateGap(type_code, type_name, "ITEM_WARNING", f"{type_name}: {detail}")
                )
            else:
                result.satisfied.append(type_code)

        # 2. Permit-type variant groups — ANY one member satisfies.
        profile_codes = {r.get("ppe_type_code") for r in reqs}
        for grp in permit_groups:
            if profile_codes & set(grp["codes"]):
                continue  # already governed by an explicit profile requirement
            outcomes = [(code, *evaluate(code)) for code in grp["codes"]]
            best = next((o for o in outcomes if o[1] == "pass"), None) or next(
                (o for o in outcomes if o[1] == "warn"), None
            )
            label = " / ".join(grp["names"])
            if best is not None:
                code, state, detail = best
                result.satisfied.append(code)
                if state == "warn":
                    result.warnings.append(
                        PpeGateGap(code, label, "ITEM_WARNING", f"{label}: {detail}")
                    )
                continue
            result.ok = False
            held = next((o for o in outcomes if o[1] != "missing"), None)
            if held is None:
                result.blockers.append(
                    PpeGateGap(grp["codes"][0], label, "NOT_ISSUED", f"{label} not issued")
                )
            else:
                code, state, detail = held
                gap_code = "NOT_ACKNOWLEDGED" if state == "unacknowledged" else "ITEM_INVALID"
                result.blockers.append(
                    PpeGateGap(code, label, gap_code, f"{label}: {detail}")
                )

    return results


async def check_ppe_for_user(
    db: AsyncSession,
    *,
    user_id: str,
    plant_id: str,
    permit_type_code: str | None = None,
) -> PpeCheckResult:
    """Single-user convenience wrapper (crew-add snapshot, ad-hoc checks)."""
    results = await check_ppe_for_crew(
        db, plant_id=plant_id, user_ids=[user_id], permit_type_code=permit_type_code
    )
    return results[user_id]
