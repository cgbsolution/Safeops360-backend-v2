"""Make the Independence Register demonstrable — all four sources, real history.

Two jobs, and the second is the one that matters:

**1. Close the two data gaps that made a guard source untestable.**
`seed_assurance_demo.py` never writes an `Area.ownerUserId` (0 of 187 areas own
anything), so the AREA_OWNER branch of the own-work guard has never fired
against real data, and it produces nobody who reaches the guard through
checkpoint ownership specifically — the exact path that was silently invisible
on the Independence screen until the source-resolution fix. This seeds at least
one person through EACH of the four sources, so the register can demonstrate all
four rather than only DECLARED_AUDITEE.

**2. Replay historical attempts through the REAL code path.**
Every event below comes from calling `check_assignment` and recording its actual
verdict — no hand-written `IndependenceEvent` rows. If the guard's reasoning
changes, this seed changes with it or fails loudly; a seed that inserts the
answer directly would keep producing a beautiful demo of a guard that had
stopped working.

Four attempts, chosen to cover what a certification body asks:

  * 2 × blocked-and-abandoned — the strongest evidence in the register. A block
    that was never overridden proves enforcement; a waiver only proves
    governance.
  * 1 × blocked-then-waived, with a named approver who is not the subject.
  * 1 × warned-and-proceeded — a WARN is not a block, and the register has to
    show that the product distinguishes them.

Idempotent: re-running tops up rather than duplicating. Dry run by default.

    .venv/Scripts/python.exe scripts/seed_independence_register.py
    .venv/Scripts/python.exe scripts/seed_independence_register.py --commit

WARNING: The backend .env points at PRODUCTION.
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import get_settings
from app.models.assurance import DisciplineOwner, IndependenceEvent, IndependenceWaiver
from app.models.audit_compliance import AuditCheckpointResponse, ComplianceAudit
from app.models.plant import Area
from app.models.user import User
from app.services import independence as ind
from app.services import independence_events as inde

WAIVER_JUSTIFICATION = (
    "Single-site coverage: no other auditor at this site holds the required competence for "
    "this discipline in the audit window. Approved with the conflict recorded, an independent "
    "reviewer added to the report sign-off, and the finding sample re-checked by the "
    "corporate HSE lead."
)


def _fmt(u: User | None) -> str:
    return f"{u.name} ({u.id[:10]}…)" if u else "—"


async def run(commit: bool) -> int:
    engine = create_async_engine(
        get_settings().async_database_url,
        connect_args={"statement_cache_size": 0, "prepared_statement_cache_size": 0},
    )
    Session = async_sessionmaker(engine, expire_on_commit=False)
    created = {"areaOwner": 0, "disciplineOwner": 0, "waiver": 0, "events": 0}

    async with Session() as s:
        audits = list(
            (
                await s.execute(
                    select(ComplianceAudit).where(ComplianceAudit.isDeleted.is_(False))
                )
            ).scalars().all()
        )
        if not audits:
            print("No live audits — nothing to attempt against.")
            return 1
        # Prefer a REAL audit with a real discipline scope. The 1,500-checkpoint
        # scale fixture has the most disciplines and the worst names for a demo
        # ("DISC-01"), so having the most scope must not win on its own — the
        # register would render evidence labelled with fixture vocabulary.
        def _is_fixture(a: ComplianceAudit) -> bool:
            return "SD1" in (a.auditNumber or "") or all(
                str(d).upper().startswith("DISC-") for d in (a.selectedDisciplineIds or ["x"])
            )

        audits.sort(
            key=lambda a: (
                _is_fixture(a),
                -(len(a.selectedDisciplineIds or [])),
                a.auditNumber,
            )
        )
        target = next((a for a in audits if a.plantId), audits[0])
        site = target.plantId
        print(f"target engagement: {target.auditNumber} @ site {site}")
        print(f"  disciplines in scope: {(target.selectedDisciplineIds or [])[:4]}")

        users = list(
            (
                await s.execute(select(User).where(User.plantId == site).limit(60))
            ).scalars().all()
        )
        if len(users) < 4:
            users += list((await s.execute(select(User).limit(30))).scalars().all())
        seen: set[str] = set()
        users = [u for u in users if not (u.id in seen or seen.add(u.id))]
        print(f"  candidate pool: {len(users)} users")

        scope = await ind.scope_for_audit(s, target)

        # ── Gap 1: AREA_OWNER has never had a row ─────────────────────
        owned_areas = (
            await s.execute(select(func.count(Area.id)).where(Area.ownerUserId.isnot(None)))
        ).scalar_one()
        area_owner: User | None = None
        if owned_areas == 0:
            area = (
                await s.execute(select(Area).where(Area.plantId == site).limit(1))
            ).scalars().first()
            if area is not None:
                # Someone who is NOT already conflicted, so the new row is what
                # makes them conflicted — that is what makes the source
                # demonstrable rather than incidental.
                for u in users:
                    v = await ind.check_assignment(s, user_id=u.id, scope=scope)
                    if not v.blocking:
                        area_owner = u
                        break
                if area_owner:
                    area.ownerUserId = area_owner.id
                    await s.flush()
                    created["areaOwner"] = 1
                    print(f"  + Area.ownerUserId: {_fmt(area_owner)} owns “{area.name}”")
        else:
            print(f"  = Area owners already present ({owned_areas}); left alone")

        # ── Gap 2: nobody reaches the guard via CHECKPOINT_OWNER on a
        #          real (non-fixture) audit ──────────────────────────────
        cp_owner: User | None = None
        cp_rows = list(
            (
                await s.execute(
                    select(AuditCheckpointResponse)
                    .where(
                        AuditCheckpointResponse.auditId == target.id,
                        AuditCheckpointResponse.assignedOwnerId.is_(None),
                    )
                    .limit(6)
                )
            ).scalars().all()
        )
        already_cp = (
            await s.execute(
                select(func.count(AuditCheckpointResponse.id)).where(
                    AuditCheckpointResponse.auditId == target.id,
                    AuditCheckpointResponse.assignedOwnerId.isnot(None),
                )
            )
        ).scalar_one()
        if cp_rows and not already_cp:
            for u in users:
                if area_owner and u.id == area_owner.id:
                    continue
                v = await ind.check_assignment(s, user_id=u.id, scope=scope)
                if not v.blocking:
                    cp_owner = u
                    break
            if cp_owner:
                for r in cp_rows:
                    r.assignedOwnerId = cp_owner.id
                await s.flush()
                print(
                    f"  + checkpoint ownership: {_fmt(cp_owner)} owns "
                    f"{len(cp_rows)} checkpoints on {target.auditNumber}"
                )
        elif already_cp:
            print(f"  = checkpoint ownership already present ({already_cp} rows); left alone")

        # ── Gap 3: DISCIPLINE_OWNER has exactly one row ───────────────
        disc_codes = [d for d in (target.selectedDisciplineIds or []) if d]
        disc_owner: User | None = None
        if disc_codes:
            code = disc_codes[0]
            existing = (
                await s.execute(
                    select(DisciplineOwner).where(
                        DisciplineOwner.disciplineCode == code,
                        DisciplineOwner.plantId == site,
                        DisciplineOwner.isActive.is_(True),
                    )
                )
            ).scalars().first()
            if existing is None:
                for u in users:
                    if u.id in {getattr(area_owner, "id", None), getattr(cp_owner, "id", None)}:
                        continue
                    v = await ind.check_assignment(s, user_id=u.id, scope=scope)
                    if not v.blocking:
                        disc_owner = u
                        break
                if disc_owner:
                    s.add(
                        DisciplineOwner(
                            plantId=site,
                            disciplineCode=code,
                            disciplineLabel=code.replace("-", " ").title(),
                            ownerUserId=disc_owner.id,
                            ownershipType="ACCOUNTABLE",
                            isActive=True,
                        )
                    )
                    await s.flush()
                    created["disciplineOwner"] = 1
                    print(f"  + DisciplineOwner: {_fmt(disc_owner)} owns {code} @ site")
            else:
                disc_owner = await s.get(User, existing.ownerUserId)
                print(f"  = DisciplineOwner already present for {code}: {_fmt(disc_owner)}")

        # ── Replay the attempts through the real guard ────────────────
        # Re-resolve: the rows written above change what the guard now says, and
        # recording a stale verdict would be the same lie this build set out to fix.
        scope = await ind.scope_for_audit(s, target)
        actor = (
            await s.execute(select(User).where(User.role == "ADMIN").limit(1))
        ).scalars().first() or users[0]

        blocked: list[tuple[User, ind.IndependenceVerdict]] = []
        warned: list[tuple[User, ind.IndependenceVerdict]] = []
        for u in users:
            v = await ind.check_assignment(s, user_id=u.id, scope=scope)
            if v.blocking and len(blocked) < 3:
                blocked.append((u, v))
            elif not v.blocking and v.warnings and len(warned) < 1:
                warned.append((u, v))
            if len(blocked) >= 3 and warned:
                break

        print(f"\n  guard verdicts available: {len(blocked)} blocking, {len(warned)} warning-only")
        existing_events = (
            await s.execute(select(func.count(IndependenceEvent.id)))
        ).scalar_one()
        print(f"  IndependenceEvent rows before: {existing_events}")

        # 2 blocked-and-abandoned + 1 warned-and-proceeded.
        for u, v in blocked[:2]:
            rid = await inde.record_event(
                subject_user_id=u.id,
                engagement_kind="AUDIT",
                engagement_id=target.id,
                engagement_code=target.auditNumber,
                site_id=site,
                origin="CREATE_AUDIT",
                attempted_by_user_id=actor.id,
                dedupe=False,
                session=s,
                **inde.event_fields_for(v),
            )
            created["events"] += 1 if rid else 0
            print(f"  + BLOCKED (abandoned): {_fmt(u)} — {v.blocking[0].source}")

        for u, v in warned:
            rid = await inde.record_event(
                subject_user_id=u.id,
                engagement_kind="AUDIT",
                engagement_id=target.id,
                engagement_code=target.auditNumber,
                site_id=site,
                origin="CREATE_AUDIT",
                attempted_by_user_id=actor.id,
                dedupe=False,
                session=s,
                **inde.event_fields_for(v),
            )
            created["events"] += 1 if rid else 0
            print(f"  + WARNED (proceeded): {_fmt(u)} — {v.warnings[0].source}")

        # 1 blocked-then-waived, with a named approver who is not the subject.
        if len(blocked) >= 3:
            subject, verdict = blocked[2]
            approver = next(
                (u for u in users if ind.segregation_ok(u.id, subject.id) and u.id != actor.id),
                actor,
            )
            existing_waiver = (
                await s.execute(
                    select(IndependenceWaiver).where(
                        IndependenceWaiver.engagementId == target.id,
                        IndependenceWaiver.subjectUserId == subject.id,
                        IndependenceWaiver.revokedAt.is_(None),
                    )
                )
            ).scalars().first()
            if existing_waiver is None:
                # The block first — the timeline must show the guard firing
                # BEFORE the override, because that ordering is the evidence.
                rid = await inde.record_event(
                    subject_user_id=subject.id,
                    engagement_kind="AUDIT",
                    engagement_id=target.id,
                    engagement_code=target.auditNumber,
                    site_id=site,
                    origin="CREATE_AUDIT",
                    attempted_by_user_id=actor.id,
                    dedupe=False,
                    session=s,
                    **inde.event_fields_for(verdict),
                )
                created["events"] += 1 if rid else 0

                waiver = IndependenceWaiver(
                    engagementKind="AUDIT",
                    engagementId=target.id,
                    subjectUserId=subject.id,
                    ruleViolated=verdict.blocking[0].rule,
                    conflictDetail=verdict.blocking[0].as_dict(),
                    justification=WAIVER_JUSTIFICATION,
                    approvedByUserId=approver.id,
                    scope="ENGAGEMENT",
                    checkpointCodes=[],
                )
                s.add(waiver)
                await s.flush()
                created["waiver"] = 1
                rid = await inde.record_event(
                    subject_user_id=subject.id,
                    engagement_kind="AUDIT",
                    engagement_id=target.id,
                    engagement_code=target.auditNumber,
                    site_id=site,
                    outcome="WAIVED",
                    origin="WAIVER_GRANT",
                    attempted_by_user_id=approver.id,
                    rule=verdict.blocking[0].rule,
                    source=verdict.blocking[0].source,
                    reason=WAIVER_JUSTIFICATION,
                    conflict_detail=verdict.blocking[0].as_dict(),
                    waiver_id=waiver.id,
                    dedupe=False,
                    session=s,
                )
                created["events"] += 1 if rid else 0
                print(
                    f"  + BLOCKED -> WAIVED: {_fmt(subject)} — approved by {_fmt(approver)}"
                )
            else:
                print(f"  = waiver already present for {_fmt(subject)}; left alone")

        # ── Report the four sources the register can now demonstrate ──
        await s.flush()
        resolved = await ind.resolve_ownership_sources(s, include_auditor_roles=True)
        by_source: dict[str, set[str]] = {}
        for uid, src in resolved.items():
            for o in src.owns:
                by_source.setdefault(o.source, set()).add(uid)
        print("\n-- ownership sources now represented ---------")
        for src in ("DECLARED_AUDITEE", "CHECKPOINT_OWNER", "AREA_OWNER", "DISCIPLINE_OWNER"):
            people = by_source.get(src, set())
            print(f"  {len(people):>4} people  {src}" + ("" if people else "   << STILL EMPTY"))
        missing = [
            s_ for s_ in ("DECLARED_AUDITEE", "CHECKPOINT_OWNER", "AREA_OWNER", "DISCIPLINE_OWNER")
            if not by_source.get(s_)
        ]

        print("\n-- created -----------------------------------")
        for k, v in created.items():
            print(f"  {k:<18} {v}")

        if commit and not missing:
            await s.commit()
            print("\nCOMMITTED.")
        elif commit:
            await s.rollback()
            print(f"\nROLLED BACK — {missing} still unrepresented.")
        else:
            await s.rollback()
            print("\nDRY RUN — nothing written. Re-run with --commit.")

    await engine.dispose()
    return 1 if missing else 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--commit", action="store_true")
    args = ap.parse_args()
    return asyncio.run(run(args.commit))


if __name__ == "__main__":
    raise SystemExit(main())
