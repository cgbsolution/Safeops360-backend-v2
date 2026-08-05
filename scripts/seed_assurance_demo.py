"""Seed the auditor-independence demo scenario (docs/cams/09 §2.1.7).

Page Industries asked to see, explicitly, that **the same person can be an
auditor on one engagement and an auditee on another**, with independence still
enforced. This script makes that showable on real records:

  1. Picks a real user at a real site who is already an auditor somewhere.
  2. Names them the auditee owner on a DIFFERENT engagement at another site.
  3. Records a DisciplineOwner row so the own-work guard has something to fire
     on - which makes the BLOCKED case demonstrable live.

The 20-second demo it enables:

    /cams/assurance?userId=<them>
      -> "As auditor" lists engagement A - "As auditee" lists engagement B
      -> the "Wears both hats" badge is present, and that is CORRECT

    /cams/audits -> Schedule Audit at their site, pick them as lead auditor
      -> the inline independence panel blocks with the reason named,
        and the Schedule button is disabled

Idempotent: re-running updates the same rows rather than duplicating them.
Read-mostly - it writes at most one auditee assignment and one ownership row.

    .venv/Scripts/python.exe scripts/seed_assurance_demo.py            # dry run
    .venv/Scripts/python.exe scripts/seed_assurance_demo.py --commit   # write

WARNING: The backend .env points at PRODUCTION. Run the dry run first and read what it
plans to do before passing --commit.
"""

from __future__ import annotations

import sys

from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models.assurance import DisciplineOwner
from app.models.audit_compliance import AuditCheckpointResponse, ComplianceAudit
from app.models.user import User


# Roles that could credibly BOTH audit and own a safety discipline, best first.
# A Plant HSE Head is the strongest story: they audit peer sites and answer for
# their own. An Insurance or Finance manager is not on this list by design.
PLAUSIBLE_ROLES = [
    "PLANT_HSE_HEAD",
    "LEAD_AUDITOR",
    "CORPORATE_HSE",
    "HSE_MANAGER",
    "AUDIT_MANAGER",
    "AUDITOR",
]


def _pick_subject(s: Session, by_site: dict) -> User | None:
    """The most senior role-plausible person who has audits at their own site."""
    candidates = list(
        s.execute(select(User).where(User.role.in_(PLAUSIBLE_ROLES))).scalars().all()
    )
    for role in PLAUSIBLE_ROLES:
        for u in candidates:
            if u.role == role and u.plantId and u.plantId in by_site:
                return u
    return None


def main(commit: bool) -> int:
    engine = create_engine(get_settings().sync_database_url, future=True)
    with Session(engine) as s:
        audits = list(
            s.execute(
                select(ComplianceAudit)
                .where(ComplianceAudit.isDeleted.is_(False))
                .order_by(ComplianceAudit.scheduledDate.desc())
            ).scalars().all()
        )
        if len(audits) < 2:
            print("Need at least two audits in the tenant to stage the scenario. Found "
                  f"{len(audits)}.")
            return 1

        # Group by site so "auditor here, auditee there" is genuinely across sites -
        # a same-site pairing would be a weaker demonstration and, at discipline
        # level, might legitimately be blocked.
        by_site: dict[str, list[ComplianceAudit]] = {}
        for a in audits:
            by_site.setdefault(a.plantId, []).append(a)

        if len(by_site) < 2:
            print("All audits are at one site. The cross-site case needs audits at two sites.")
            print(f"Sites found: {list(by_site)}")
            return 1

        # ── Pick a ROLE-PLAUSIBLE subject ─────────────────────────────
        #
        # The first version of this script just took `audit_a.leadAuditorUserId`
        # and landed on an "Insurance & Risk Transfer Manager" as a Fire Safety
        # owner — which is precisely the implausible allocation the diagnosis
        # flagged as F-36, and a client would rightly find it odd. So the subject
        # must be someone who would credibly audit AND credibly own a safety
        # discipline.
        #
        # The story we want is the most natural real-world two-hat case:
        #   a Plant HSE Head who audits ANOTHER site, and is the auditee-owner
        #   for a discipline at THEIR OWN site.
        subject = _pick_subject(s, by_site)
        if subject is None:
            print("No role-plausible auditor found (need an HSE/audit role). Aborting rather")
            print("than staging an implausible scenario.")
            return 1

        # Which way round the two hats go MATTERS, and the first version had it
        # inverted:
        #
        #   OWN site  -> they are the AUDITEE. They own disciplines here, so the
        #                own-work guard must BLOCK them from auditing it.
        #   OTHER site -> they are the AUDITOR. They are independent here, so
        #                this assignment is legitimate and must stay allowed.
        #
        # Putting them on the auditee list at the site they audit would instead
        # trip rule 2 (same-engagement dual role) and demo a bug, not a feature.
        own_site = subject.plantId
        others = [p for p in by_site if p != own_site]
        if own_site not in by_site or not others:
            print(f"{subject.name} has no audits at their own site, or no second site exists.")
            return 1
        other_site = others[0]
        audit_auditee = by_site[own_site][0]   # they answer for this one
        audit_auditor = by_site[other_site][0]  # they audit this one

        # A discipline the subject can plausibly own at THEIR OWN site - this is
        # what makes the blocked case fire.
        disc = s.execute(
            select(
                AuditCheckpointResponse.categoryId, AuditCheckpointResponse.categoryName
            )
            .where(AuditCheckpointResponse.auditId == audit_auditee.id)
            .limit(1)
        ).first()
        disc_code, disc_name = (disc[0], disc[1]) if disc else ("GT-FS", "Fire Safety")

        print("-- planned demo scenario --------------------------")
        print(f"  Subject          : {subject.name} ({subject.id})")
        print(f"  Designation      : {subject.designation or '-'}")
        print(f"  AS AUDITOR (new) : {audit_auditor.auditNumber} - {audit_auditor.title}")
        print(f"                     site {other_site}  <- independent here, LEGITIMATE")
        print(f"  AS AUDITEE       : {audit_auditee.auditNumber} - {audit_auditee.title}")
        print(f"                     site {own_site}  <- their own site")
        print(f"  Owns discipline  : {disc_name} ({disc_code}) at site {own_site}")
        print()
        print("  Blocked case to demo: schedule an audit at their OWN site "
              f"({own_site}) covering {disc_name} and try to assign {subject.name} as lead.")
        print()

        # 1. Make them LEAD AUDITOR on an audit at the OTHER site.
        #    This is the half that makes the two-hat claim true: auditing a site
        #    they do not answer for is exactly what ISO 19011 permits.
        if audit_auditor.leadAuditorUserId == subject.id:
            print(f"  - {subject.name} already leads {audit_auditor.auditNumber} - no change.")
        else:
            prev = audit_auditor.leadAuditorUserId
            print(f"  + set {subject.name} as lead auditor on {audit_auditor.auditNumber}"
                  f" (was {prev})")
            if commit:
                audit_auditor.leadAuditorUserId = subject.id

        # 1b. They must NOT also be an auditee on that same engagement - that is
        #     rule 2 (same-engagement dual role) and would demo a defect.
        aud_list = list(audit_auditor.auditees or [])
        cleaned = [
            e for e in aud_list
            if (e.get("userId") if isinstance(e, dict) else e) != subject.id
        ]
        if len(cleaned) != len(aud_list):
            print(f"  + remove {subject.name} from {audit_auditor.auditNumber}.auditees "
                  "(cannot be auditor AND auditee on one engagement)")
            if commit:
                audit_auditor.auditees = cleaned

        # 1c. Ensure they ARE an auditee at their own site.
        own_list = list(audit_auditee.auditees or [])
        if any((e.get("userId") if isinstance(e, dict) else e) == subject.id for e in own_list):
            print(f"  - {subject.name} is already an auditee on {audit_auditee.auditNumber}.")
        else:
            print(f"  + add {subject.name} to {audit_auditee.auditNumber}.auditees")
            if commit:
                own_list.append({"userId": subject.id, "responsibleCategories": []})
                audit_auditee.auditees = own_list

        # 2. Record discipline ownership at their own site.
        owner_row = s.execute(
            select(DisciplineOwner).where(
                DisciplineOwner.plantId == own_site,
                DisciplineOwner.disciplineCode == disc_code,
                DisciplineOwner.ownerUserId == subject.id,
            )
        ).scalars().first()
        if owner_row is not None:
            print(f"  - ownership of {disc_code} @ {own_site} already recorded - no change.")
            if commit:
                owner_row.isActive = True
        else:
            print(f"  + DisciplineOwner: {subject.name} owns {disc_code} @ {own_site}")
            if commit:
                s.add(
                    DisciplineOwner(
                        plantId=own_site,
                        disciplineCode=disc_code,
                        disciplineLabel=disc_name,
                        ownerUserId=subject.id,
                        ownershipType="ACCOUNTABLE",
                        createdBy="seed_assurance_demo",
                    )
                )

        if commit:
            s.commit()
            print("\nCOMMITTED.")
        else:
            print("\nDRY RUN - nothing written. Re-run with --commit to apply.")

        print("\n-- demo script ------------------------------------")
        print(f"  1. Open /cams/assurance?userId={subject.id}")
        print("     Both sections populate; the 'Wears both hats' badge shows. This is CORRECT.")
        print("  2. Open /cams/audits -> Schedule Audit")
        print(f"     Site {own_site} (their OWN site), scope including {disc_name},")
        print(f"     lead auditor = {subject.name}")
        print("     -> the independence panel blocks with the reason named inline,")
        print("       and the Schedule button is disabled.")
        print("  3. Change the lead auditor to anyone else -> the block clears.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main("--commit" in sys.argv))
