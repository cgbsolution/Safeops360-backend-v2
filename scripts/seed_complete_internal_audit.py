"""Seed ONE Page Industries internal audit, complete end to end.

The existing `seed_page_demo_audits.py` builds four audits across four
lifecycle states, and its closed one is deliberately thin: evidence is an inline
SVG data URI with no `storagePath`, so `auditorEvidenceIds` stays empty and the
report's checkpoint register renders no photographs at all. Half the record is
there and half is not, which is exactly what a demo must not look like.

This script builds a single INTERNAL audit and walks every step the product
actually supports, leaving nothing blank:

  * opening meeting recorded (attendees, scope confirmed)
  * auditor competence frozen from the Skill Matrix at assignment
  * all 120 checkpoints graded, EVERY one carrying an observation and at least
    one real evidence photograph uploaded to Supabase storage
  * submitted -> findings routed to the responsible auditees, criticals
    auto-spawn CAPA
  * INTERIM report issued (so the FINAL carries a revision history)
  * auditee responses WITH their own uploaded evidence, iterated through every
    branch of the state machine: accept, request-more-info (round 2), escalate
    to plant manager, and raise-CAPA
  * closing meeting recorded and acknowledged
  * lead auditor, auditee owner, plant manager and per-discipline auditor
    sign-offs
  * closed, then a FINAL report with the recorded sign-offs frozen into it

Evidence is a GENERATED placeholder image, uploaded as a real object under the
same `audit-compliance/...` prefix the browser upload flow uses. It is a real
attachment in every mechanical sense (storage path, signed URL, mime type) and
is captioned so nobody mistakes it for a site photograph.

Idempotent: the audit is keyed by title and rebuilt on every run, along with its
meetings, competence snapshots and auto-spawned CAPAs.

Needs Supabase storage configured (SUPABASE_URL + SUPABASE_SERVICE_ROLE_KEY) and
Pillow installed (`python -m pip install Pillow`) — it draws its own evidence
frames rather than shipping stock photographs.

Run from the backend root:
    PYTHONPATH=. python scripts/seed_complete_internal_audit.py
    (PowerShell:  $env:PYTHONPATH="."; python scripts/seed_complete_internal_audit.py)
"""

from __future__ import annotations

import asyncio
import io
import textwrap
import threading
from datetime import datetime, timedelta, timezone

try:
    from PIL import Image, ImageDraw, ImageFont
except ModuleNotFoundError as _e:  # pragma: no cover - setup guidance
    raise SystemExit(
        "This seeder draws its evidence frames with Pillow, which is not "
        "declared in the backend's dependencies because nothing in the running "
        "product needs it. Install it first:  python -m pip install Pillow"
    ) from _e

from sqlalchemy import select
from supabase import create_client

from app.core.config import get_settings
from app.core.db import AsyncSessionLocal
from app.models.assurance import EngagementCompetenceSnapshot, EngagementMeeting
from app.models.audit_compliance import AuditCheckpointResponse, ComplianceAudit
from app.models.capa import Capa
from app.models.competency_matrix import Competency, CompetencyRecord
from app.models.plant import Plant
from app.models.user import User
from app.services import assurance
from app.services import audit_compliance as svc
from app.services import page_grading as pg
from app.services import signoff
from app.services import storage

INDUSTRY_CODE = "PAGE_INDUSTRIES"
PLANT_CODE = "NW"
TITLE = "Internal Audit - HR, EHS & Production (Complete Walkthrough)"
DISCIPLINES = ["HR", "EHS", "PRODUCTION"]

# Ten years. A demo that quietly rots into broken thumbnails after a week is
# worse than one that never had photographs, because the failure looks like a
# product bug rather than an expired link.
EVIDENCE_URL_TTL_SEC = 10 * 365 * 86400

# Concurrent Supabase PUTs. The client is a shared sync httpx session; six is
# comfortably inside its pool and turns ~180 sequential uploads into ~30s.
UPLOAD_CONCURRENCY = 6

DISCIPLINE_TINT = {
    "HR": (109, 40, 217),          # violet
    "EHS": (185, 28, 28),          # red
    "PRODUCTION": (180, 83, 9),    # amber
}
AUDITEE_TINT = (4, 120, 87)        # emerald - remediation evidence reads green


# ── Observation copy ─────────────────────────────────────────────────────
#
# Written per grade rather than one string for everything: a register in which
# 120 rows carry the same sentence is padding, not a record.

EFFECTIVE_NOTES = [
    "Verified on the floor and against records. Documentation was current, "
    "signed and retrievable within the sampling window.",
    "Sampled six months of records with no gaps. The responsible owner "
    "demonstrated the process end to end during the walkthrough.",
    "Control is operating as designed. Physical verification agreed with the "
    "register and with the department head's account.",
    "Evidence produced on request without delay. Last review is within "
    "validity and the next is calendared.",
    "Checked against the statutory register and the display copy on site. "
    "Both agree and both are current.",
]

NA_NOTES = [
    "Not applicable to this unit - the activity is not carried out at this "
    "site. Confirmed with the department head and excluded from the score.",
    "Not applicable in the audited period. The associated process was not "
    "operated, so there is nothing to assess against this requirement.",
]

FINDING_NOTES = {
    pg.GRADE_SOME_IMPROVEMENT: [
        "Largely in place, but records were incomplete for two of the six "
        "months sampled. Raised with the department head during the "
        "walkthrough and acknowledged on the spot.",
        "The control operates, but it is not documented and depends on one "
        "individual being present. No deputy has been briefed.",
        "Evidence was produced for the current period only; earlier periods "
        "could not be retrieved within the audit window.",
    ],
    pg.GRADE_MAJOR_IMPROVEMENT: [
        "Requirement only partially met. Evidence exists for the current "
        "month but the review cycle has slipped twice and no corrective "
        "action was recorded either time.",
        "The procedure is defined but is not being followed in practice. Two "
        "of four workstations sampled deviated from the written method.",
        "Records are maintained but were not reviewed or signed by the "
        "responsible manager for the last quarter.",
    ],
    pg.GRADE_UNSATISFACTORY: [
        "Not met. No evidence could be produced during the audit and the "
        "responsible owner confirmed the control is not currently operating. "
        "Raised at the closing meeting as a critical non-conformity.",
        "Not met, and previously raised. The corrective action agreed at the "
        "last audit was not implemented, so this is recorded as a repeated "
        "non-compliance.",
    ],
}

AUDITEE_RESPONSES = [
    "Accepted. The gap has been closed - the register was reconstructed from "
    "the source records and countersigned by the department head. Evidence "
    "attached.",
    "Accepted. The written procedure has been issued, a deputy has been "
    "briefed and both have signed the acknowledgement sheet. Copy attached.",
    "Accepted. The review cycle has been moved into the monthly compliance "
    "calendar with a named owner. First completed cycle attached as evidence.",
]

AUDITEE_ROUND2 = (
    "Additional evidence supplied as requested: the full six-month record set "
    "with the manager's review signatures, plus the revised checklist now in "
    "use at the workstation."
)


# ── Evidence image generation ────────────────────────────────────────────

_FONT_PATHS_BOLD = [
    r"C:\Windows\Fonts\segoeuib.ttf",
    r"C:\Windows\Fonts\arialbd.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
]
_FONT_PATHS_REG = [
    r"C:\Windows\Fonts\segoeui.ttf",
    r"C:\Windows\Fonts\arial.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
]
_font_cache: dict[tuple[int, bool], ImageFont.FreeTypeFont] = {}


def _font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    key = (size, bold)
    if key not in _font_cache:
        for path in _FONT_PATHS_BOLD if bold else _FONT_PATHS_REG:
            try:
                _font_cache[key] = ImageFont.truetype(path, size)
                break
            except OSError:
                continue
        else:
            _font_cache[key] = ImageFont.load_default()
    return _font_cache[key]


def make_evidence_image(
    *, kind: str, code: str, discipline: str, question: str,
    site: str, captured: datetime, frame: int,
) -> bytes:
    """A labelled evidence frame for one checkpoint.

    Deliberately self-describing. A grey rectangle would be indistinguishable
    from a failed upload, and a stock photograph would be a lie about what was
    observed on site - so the frame states what it is, what it belongs to and
    when it was captured.
    """
    is_auditee = kind == "AUDITEE"
    tint = AUDITEE_TINT if is_auditee else DISCIPLINE_TINT.get(discipline, (51, 65, 85))
    w, h = 800, 600
    img = Image.new("RGB", (w, h), (238, 242, 247))
    d = ImageDraw.Draw(img)

    # Header band
    d.rectangle([0, 0, w, 92], fill=tint)
    d.text((32, 20), f"{'AUDITEE' if is_auditee else 'AUDITOR'} EVIDENCE",
           font=_font(22, True), fill=(255, 255, 255))
    d.text((32, 54), f"{code}   -   frame {frame}",
           font=_font(17), fill=(255, 255, 255))
    d.text((w - 32, 34), discipline, font=_font(20, True),
           fill=(255, 255, 255), anchor="ra")

    # Body card
    d.rounded_rectangle([36, 124, w - 36, 470], radius=14,
                        fill=(255, 255, 255), outline=(203, 213, 225), width=2)
    y = 152
    d.text((60, y), "CHECKPOINT", font=_font(13, True), fill=(148, 163, 184))
    y += 24
    for line in textwrap.wrap(question, width=58)[:4]:
        d.text((60, y), line, font=_font(19), fill=(30, 41, 59))
        y += 28
    y += 12
    d.line([60, y, w - 60, y], fill=(226, 232, 240), width=2)
    y += 20
    for label, value in (
        ("Site", site),
        ("Captured", captured.strftime("%d %b %Y, %H:%M")),
        ("Reference", f"{code} / {kind.lower()} / {frame:02d}"),
    ):
        d.text((60, y), f"{label}:", font=_font(15, True), fill=(100, 116, 139))
        d.text((190, y), value, font=_font(15), fill=(51, 65, 85))
        y += 26

    # Footer strip - the honesty line
    d.rectangle([0, h - 76, w, h], fill=(30, 41, 59))
    d.text((32, h - 60), "DEMO EVIDENCE", font=_font(19, True), fill=(255, 255, 255))
    d.text((32, h - 34), "Generated placeholder - not a site photograph.",
           font=_font(14), fill=(148, 163, 184))

    # Faint diagonal watermark
    mark = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    # Low in the card's empty half, so it never sits over the text above it.
    ImageDraw.Draw(mark).text((w // 2, 424), "SAFEOPS360", font=_font(64, True),
                              fill=(15, 23, 42, 18), anchor="mm")
    img = Image.alpha_composite(img.convert("RGBA"), mark).convert("RGB")

    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=82, optimize=True)
    return buf.getvalue()


_thread_local = threading.local()


def _bucket():
    """A storage handle owned by the CALLING THREAD.

    `app.services.storage` memoises one process-wide Supabase client, and its
    underlying httpx connection pool is not safe to drive from several threads
    at once — on Windows the concurrent uploads died with `WinError 10035` on a
    shared non-blocking socket. One client per worker thread costs four idle
    httpx sessions and removes the whole class of problem.
    """
    b = getattr(_thread_local, "bucket", None)
    if b is None:
        settings = get_settings()
        client = create_client(settings.supabase_url, settings.supabase_service_role_key)
        b = client.storage.from_(settings.supabase_incident_bucket)
        _thread_local.bucket = b
    return b


def _upload_evidence(spec: dict) -> dict:
    """Blocking: generate, PUT to Supabase, sign. Run in a worker thread."""
    data = make_evidence_image(
        kind=spec["kind"], code=spec["code"], discipline=spec["discipline"],
        question=spec["question"], site=spec["site"], captured=spec["captured"],
        frame=spec["frame"],
    )
    file_name = f"{spec['code']}-{spec['kind'].lower()}-{spec['frame']:02d}.jpg"
    path = svc.attachment_storage_path(spec["auditId"], spec["code"], file_name)
    bucket = _bucket()
    bucket.upload(path, data, {"content-type": "image/jpeg", "upsert": "true"})
    res = bucket.create_signed_url(path, EVIDENCE_URL_TTL_SEC)
    url = (res or {}).get("signed_url") or (res or {}).get("signedURL")
    if not url:
        raise RuntimeError(f"Could not sign {path}: {res}")
    return {
        "url": url,
        "storagePath": path,
        "caption": spec["caption"],
        "mimeType": "image/jpeg",
        "fileName": file_name,
    }


async def upload_all(specs: list[dict]) -> list[dict]:
    """Upload every frame concurrently, preserving input order."""
    sem = asyncio.Semaphore(UPLOAD_CONCURRENCY)
    done = 0
    total = len(specs)

    async def one(spec: dict) -> dict:
        nonlocal done
        async with sem:
            out = await asyncio.to_thread(_upload_evidence, spec)
        done += 1
        if done % 25 == 0 or done == total:
            print(f"    uploaded {done}/{total} evidence frames")
        return out

    return list(await asyncio.gather(*(one(s) for s in specs)))


# ── Grading plan ─────────────────────────────────────────────────────────


def build_plan(rows: list[AuditCheckpointResponse]) -> dict[str, dict]:
    """Decide the grade, status, risk and observation for every checkpoint.

    Deterministic and criticality-aware rather than a blind round robin: which
    checkpoint fails decides whether a CAPA spawns and whether the audit reads
    CRITICAL_NC, and those should be chosen, not left to modular arithmetic
    landing wherever the library happens to order its rows.
    """
    by_disc: dict[str, list[AuditCheckpointResponse]] = {}
    for r in sorted(rows, key=lambda x: x.sequence):
        by_disc.setdefault(r.categoryId, []).append(r)

    plan: dict[str, dict] = {}
    counters = {"eff": 0, "na": 0}
    finding_seq = {g: 0 for g in FINDING_NOTES}

    def note(bucket: list[str], key: str) -> str:
        counters[key] = counters.get(key, 0) + 1
        return bucket[(counters[key] - 1) % len(bucket)]

    def finding(grade: str) -> str:
        finding_seq[grade] += 1
        bucket = FINDING_NOTES[grade]
        return bucket[(finding_seq[grade] - 1) % len(bucket)]

    # Exactly two critical failures, one each in EHS and Production, on
    # checkpoints configured to auto-spawn a CAPA. Two is enough to exercise the
    # auto-CAPA path and the critical-failure gate without turning the demo into
    # a factory that fails everything.
    critical_fails: set[str] = set()
    for disc in ("EHS", "PRODUCTION"):
        for r in by_disc.get(disc, []):
            if r.criticality == "critical" and r.autoTriggerCapaOnFail:
                critical_fails.add(r.checkpointCode)
                break

    for disc, items in by_disc.items():
        non_crit = [r for r in items if r.criticality != "critical"]
        minors = [r for r in items if r.criticality == "minor"]
        majors_nc = {r.checkpointCode for r in non_crit[2::12]}
        partials = {r.checkpointCode for r in non_crit[5::8]}
        nas = {r.checkpointCode for r in minors[1::5]}
        # Precedence, worst first, so a checkpoint never lands in two buckets.
        partials -= majors_nc
        nas -= majors_nc | partials

        for i, r in enumerate(items):
            code = r.checkpointCode
            if code in critical_fails:
                plan[code] = {
                    "grade": pg.GRADE_UNSATISFACTORY,
                    "status": pg.STATUS_REPEATED_NON_COMPLIANCE,
                    "risk": pg.RISK_HIGH,
                    "note": finding(pg.GRADE_UNSATISFACTORY),
                }
            elif code in majors_nc:
                plan[code] = {
                    "grade": pg.GRADE_MAJOR_IMPROVEMENT,
                    "status": pg.STATUS_NON_COMPLIANCE,
                    "risk": pg.RISK_MEDIUM if i % 2 else pg.RISK_HIGH,
                    "note": finding(pg.GRADE_MAJOR_IMPROVEMENT),
                }
            elif code in partials:
                repeat = i % 3 == 0
                plan[code] = {
                    "grade": pg.GRADE_SOME_IMPROVEMENT,
                    "status": (pg.STATUS_REPEATED_OBSERVATION if repeat
                               else pg.STATUS_NEW_OBSERVATION),
                    "risk": pg.RISK_MEDIUM if repeat else pg.RISK_LOW,
                    "note": finding(pg.GRADE_SOME_IMPROVEMENT),
                }
            elif code in nas:
                plan[code] = {
                    "grade": pg.GRADE_NA, "status": pg.STATUS_NA, "risk": None,
                    "note": note(NA_NOTES, "na"),
                }
            else:
                plan[code] = {
                    "grade": pg.GRADE_EFFECTIVE, "status": pg.STATUS_COMPLIED,
                    "risk": None, "note": note(EFFECTIVE_NOTES, "eff"),
                }
    return plan


# ── Cast ─────────────────────────────────────────────────────────────────


async def resolve_cast(db, plant_id: str) -> dict:
    """Distinct, INDEPENDENT people for every seat, from the same scope-filtered
    lists the scheduling modal offers.

    Two constraints, both real rather than cosmetic. `create_audit` refuses to
    seat one person as both auditor and auditee, and it runs the ISO 19011
    §7.2.3 independence guard over the audit team — on this tenant five of the
    eighteen eligible auditors are already auditee owners on another engagement
    covering a discipline in scope here, and picking alphabetically lands on one
    of them. So the guard is consulted BEFORE the cast is chosen, using the same
    scope `create_audit` will build, rather than discovered as a failure after.
    """
    from app.services import audit_assignment as assignment
    from app.services import independence

    slots = (await assignment.assignable_users(db, plant_id=plant_id))["assignable"]

    # Auditees and the plant manager first: they are part of the scope the
    # auditors are then judged against.
    auditees: list[dict] = []
    for u in slots["auditee"]:
        auditees.append(u)
        if len(auditees) == len(DISCIPLINES):
            break
    if len(auditees) < len(DISCIPLINES):
        raise SystemExit("Not enough distinct assignable users to seat the cast.")
    taken = {u["id"] for u in auditees}
    pm = next(u for u in slots["plantManager"] if u["id"] not in taken)
    taken.add(pm["id"])

    scope = independence.EngagementScope(
        kind="AUDIT", id=None, siteId=plant_id,
        disciplineCodes=[],                    # full library, as create_audit sees it
        areaIds=[], departments=[],
        leadAuditorId=None, teamAuditorIds=[],
        auditeeUserIds=sorted(taken),
    )
    pool = [u for u in slots["leadAuditor"] if u["id"] not in taken]
    verdicts = await independence.check_many(
        db, user_ids=[u["id"] for u in pool], scope=scope, assigning_as="AUDITOR",
    )
    clean = [u for u in pool if not verdicts[u["id"]].blocking]
    if not clean:
        raise SystemExit(
            "Every eligible auditor at this site has a blocking independence "
            "conflict against this scope. Resolve one, or narrow the scope."
        )
    lead = clean[0]
    co = clean[1] if len(clean) > 1 else None
    return {"lead": lead, "co": co, "pm": pm, "auditees": auditees}


# ── Competence ───────────────────────────────────────────────────────────

AUDITOR_COMPETENCIES = ("ISO-45001-INTERNAL-AUDITOR", "ISO-9001-INTERNAL-AUDITOR")


async def freeze_competence(db, *, audit: ComplianceAudit, plant_id: str,
                            user_ids: list[str], captured_by: str) -> int:
    """Back the report's competence section with real Skill Matrix records.

    `assurance.capture_competence_snapshot` no-ops unless the audit type
    declares required competencies, and no `CamsAuditType` row exists for
    `internal_audit`. Rather than mutate global audit-type configuration from a
    seeder, the records and the engagement snapshot are written here - the
    snapshot then agrees with what the Skill Matrix says, which is the whole
    point of freezing it.
    """
    comps = (await db.execute(
        select(Competency).where(Competency.code.in_(AUDITOR_COMPETENCIES))
    )).scalars().all()
    if not comps:
        return 0

    now = datetime.now(timezone.utc)
    valid_from = now - timedelta(days=420)
    valid_until = now + timedelta(days=580)
    written = 0

    for uid in user_ids:
        for comp in comps:
            rec = (await db.execute(
                select(CompetencyRecord).where(
                    CompetencyRecord.personUserId == uid,
                    CompetencyRecord.competencyId == comp.id,
                )
            )).scalars().first()
            if rec is None:
                rec = CompetencyRecord(
                    plantId=plant_id, personUserId=uid, competencyId=comp.id,
                    createdByUserId=captured_by,
                )
                db.add(rec)
            rec.state = "validated"
            rec.currentValidatedAt = valid_from
            rec.currentValidatedByUserId = captured_by
            rec.currentValidationMethod = "external_certificate"
            rec.externalCertificateReference = f"{comp.code}/{uid[-6:].upper()}"
            rec.externalCertificateAuthority = "Accredited certification body"
            rec.validFrom = valid_from
            rec.validUntil = valid_until
            rec.nextRevalidationDue = valid_until
            rec.updatedByUserId = captured_by
            await db.flush()

            db.add(EngagementCompetenceSnapshot(
                engagementKind="AUDIT", engagementId=audit.id, userId=uid,
                competencyId=comp.id, competencyCode=comp.code,
                competencyName=comp.name, state=rec.state,
                validUntil=rec.validUntil,
                externalCertificateReference=rec.externalCertificateReference,
                held=True, waivedGap=False, capturedByUserId=captured_by,
            ))
            written += 1
    await db.flush()
    return written


# ── Teardown ─────────────────────────────────────────────────────────────


async def wipe_previous(db) -> None:
    """Remove any earlier run of this seeder, including what it created
    outside the audit's own cascade."""
    old = (await db.execute(
        select(ComplianceAudit).where(ComplianceAudit.title == TITLE)
    )).scalars().all()
    if not old:
        return
    for a in old:
        # Drop the storage objects too. The paths are read off the rows rather
        # than listed from the bucket — exact, and one call instead of ~120.
        paths: list[str] = []
        for r in (await db.execute(
            select(AuditCheckpointResponse)
            .where(AuditCheckpointResponse.auditId == a.id)
        )).scalars().all():
            paths += list(r.auditorEvidenceIds or []) + list(r.auditeeEvidenceIds or [])
        if paths:
            try:
                await asyncio.to_thread(_bucket().remove, paths)
                print(f"  removed {len(paths)} orphaned evidence object(s)")
            except Exception as e:  # noqa: BLE001 - storage tidy-up is not the point
                print(f"  ! could not remove {len(paths)} old evidence object(s): {e}")
        capas = (await db.execute(
            select(Capa).where(Capa.sourceReferenceUrl == f"/cams/audits/{a.id}")
        )).scalars().all()
        for c in capas:
            await db.delete(c)
        for model in (EngagementMeeting, EngagementCompetenceSnapshot):
            for row in (await db.execute(
                select(model).where(model.engagementKind == "AUDIT",
                                    model.engagementId == a.id)
            )).scalars().all():
                await db.delete(row)
        await db.delete(a)
    await db.flush()
    print(f"  removed {len(old)} previous run(s) of this audit")


# ── Main ─────────────────────────────────────────────────────────────────
#
# Split into phases, each in its OWN committed session, with the evidence
# uploads BETWEEN them rather than inside them.
#
# Not cosmetic. A single session covering the whole run holds one pooled
# connection open across ~3 minutes of Supabase PUTs during which it issues no
# SQL at all, and the pooler closes it underneath — the first run to get this
# far died on `connection was closed in the middle of operation` while writing
# the sign-offs, three minutes of work thrown away. Committing per phase also
# means a failure late in the script leaves a real audit behind that the next
# run's `wipe_previous` cleans up, instead of a silent full rollback.


async def phase_schedule() -> dict:
    """Create the audit, record the opening meeting, freeze competence."""
    async with AsyncSessionLocal() as db:
        plant = (await db.execute(
            select(Plant).where(Plant.code == PLANT_CODE)
        )).scalars().first()
        if plant is None:
            plant = (await db.execute(
                select(Plant).order_by(Plant.createdAt).limit(1)
            )).scalars().first()
        if plant is None:
            raise SystemExit("No plant in this database - seed a plant first.")

        await wipe_previous(db)

        cast = await resolve_cast(db, plant.id)
        lead = await db.get(User, cast["lead"]["id"])
        pm_user = await db.get(User, cast["pm"]["id"])
        co_user = await db.get(User, cast["co"]["id"]) if cast["co"] else None
        auditee_users = [await db.get(User, u["id"]) for u in cast["auditees"]]
        auditee_by_disc = dict(zip(DISCIPLINES, auditee_users))

        print(f"Site          : {plant.code} - {plant.name}")
        print(f"Lead auditor  : {lead.name}")
        if co_user:
            print(f"Co-auditor    : {co_user.name} (EHS)")
        print(f"Plant manager : {pm_user.name}")
        for disc, u in auditee_by_disc.items():
            print(f"Auditee {disc:<11}: {u.name}")
        print()

        now = datetime.now(timezone.utc)
        scheduled = now - timedelta(days=20)

        def audit_data(co: User | None) -> dict:
            return {
                "plantId": plant.id,
                "title": TITLE,
                "industryCode": INDUSTRY_CODE,
                "auditType": "internal_audit",
                "selectedDisciplineIds": [],       # full library: HR + EHS + Production
                "scheduledDate": scheduled,
                "scheduledStartTime": "09:00",
                "estimatedDurationHours": 8,
                "leadAuditorUserId": lead.id,
                # A co-auditor conducting EHS makes the per-discipline auditor
                # assignment real rather than every row falling back to the lead.
                "coAuditors": ([{"userId": co.id, "disciplineIds": ["EHS"]}]
                               if co else []),
                "plantManagerUserId": pm_user.id,
                "auditees": [{"userId": u.id, "responsibleCategories": [d]}
                             for d, u in auditee_by_disc.items()],
                "scopeDepartments": ["Human Resources", "EHS", "Cutting", "Sewing",
                                     "Finishing", "Stores"],
                "scopeAreas": ["Production Floor 1", "Production Floor 2",
                               "Warehouse", "Utility Block", "Canteen & Welfare"],
                "scopeDescription": (
                    "Annual internal audit programme - Page Industries checklist. "
                    "Human Resources, Environment/Health/Safety and Production "
                    "assessed against the full 120-checkpoint internal standard, "
                    "covering both shifts."
                ),
                "openingRemarks": (
                    "Opening meeting held with plant management. Scope, criteria, "
                    "sampling basis and the closing-meeting time were confirmed "
                    "with the auditees before fieldwork began."
                ),
            }

        # The independence guard can legitimately refuse a co-auditor. Drop the
        # second auditor rather than seed around the guard - a seeded engagement
        # that bypassed it would demonstrate the exact segregation-of-duties
        # breach the guard exists to prevent.
        try:
            audit = await svc.create_audit(db, user=lead, data=audit_data(co_user))
        except ValueError as e:
            if co_user is None or "independence" not in str(e).lower():
                raise
            print(f"  !  co-auditor {co_user.name} refused by the independence "
                  f"guard; conducting single-auditor.\n     {e}")
            co_user = None
            audit = await svc.create_audit(db, user=lead, data=audit_data(None))
        await db.flush()
        print(f"  1. scheduled            {audit.auditNumber} "
              f"({audit.totalCheckpoints} checkpoints)")

        attendees = (
            [{"userId": lead.id, "role": "Lead auditor"}]
            + ([{"userId": co_user.id, "role": "Auditor"}] if co_user else [])
            + [{"userId": pm_user.id, "role": "Plant manager"}]
            + [{"userId": u.id, "role": f"Auditee - {d}"}
               for d, u in auditee_by_disc.items()]
        )
        await assurance.upsert_meeting(
            db, engagement_kind="AUDIT", engagement_id=audit.id,
            meeting_type="OPENING", user=lead,
            payload={
                "heldAt": scheduled.replace(hour=9, minute=0).isoformat(),
                "attendees": attendees,
                "scopeConfirmed": True,
                "notes": (
                    "Scope, audit criteria and the sampling basis were presented "
                    "and accepted. Escort arrangements, PPE requirements and the "
                    "closing-meeting time were agreed."
                ),
            },
        )
        print(f"  2. opening meeting      {len(attendees)} attendees, scope confirmed")

        team = [lead.id] + ([co_user.id] if co_user else [])
        n_comp = await freeze_competence(
            db, audit=audit, plant_id=plant.id, user_ids=team, captured_by=lead.id,
        )
        print(f"  3. competence frozen    {n_comp} snapshot(s) for {len(team)} auditor(s)")

        rows = (await db.execute(
            select(AuditCheckpointResponse)
            .where(AuditCheckpointResponse.auditId == audit.id)
            .order_by(AuditCheckpointResponse.sequence)
        )).scalars().all()
        plan = build_plan(rows)
        checkpoints = [
            {"code": r.checkpointCode, "discipline": r.categoryId,
             "question": r.checkpointQuestion}
            for r in rows
        ]

        ctx = {
            "auditId": audit.id, "auditNumber": audit.auditNumber,
            "plantName": plant.name, "leadId": lead.id, "pmId": pm_user.id,
            "auditeeIds": [u.id for u in auditee_users],
            "attendees": attendees, "scheduled": scheduled, "now": now,
            "plan": plan, "checkpoints": checkpoints,
        }
        await db.commit()
        return ctx


async def phase_conduct(ctx: dict, auditor_photos: dict[str, list[dict]]) -> dict:
    """Grade every checkpoint, submit, issue the interim report."""
    async with AsyncSessionLocal() as db:
        lead = await db.get(User, ctx["leadId"])
        plan = ctx["plan"]
        for cp in ctx["checkpoints"]:
            p = plan[cp["code"]]
            payload: dict = {
                "checkpointCode": cp["code"],
                "gradeAwarded": p["grade"],
                "complianceStatus": p["status"],
                "auditFindings": p["note"],
                "photos": auditor_photos[cp["code"]],
            }
            if p["risk"]:
                payload["riskGrade"] = p["risk"]
            await svc.save_response(db, user=lead, audit_id=ctx["auditId"], payload=payload)
        await db.flush()
        graded = {g: sum(1 for p in plan.values() if p["grade"] == g)
                  for g in (pg.GRADE_EFFECTIVE, pg.GRADE_SOME_IMPROVEMENT,
                            pg.GRADE_MAJOR_IMPROVEMENT, pg.GRADE_UNSATISFACTORY,
                            pg.GRADE_NA)}
        print("  5. graded               " + ", ".join(
            f"{pg.GRADE_LABEL.get(g, g)}={n}" for g, n in graded.items()))

        res = await svc.submit_audit(db, user=lead, audit_id=ctx["auditId"])
        print(f"  6. submitted            {res['capasSpawned']} CAPA auto-spawned, "
              f"score {res['score']['score_obtained']}/{res['score']['score_allotted']} "
              f"= {res['score']['overall_score_pct']}%")

        interim = await svc.generate_report(
            db, user=lead, audit_id=ctx["auditId"], report_type="INTERIM",
        )
        print(f"  7. interim report       {interim['reportCode']}")

        routed = (await db.execute(
            select(AuditCheckpointResponse).where(
                AuditCheckpointResponse.auditId == ctx["auditId"],
                AuditCheckpointResponse.workflowState == "AWAITING_AUDITEE",
            ).order_by(AuditCheckpointResponse.sequence)
        )).scalars().all()
        findings = [
            {"id": r.id, "code": r.checkpointCode, "discipline": r.categoryId,
             "question": r.checkpointQuestion,
             "ownerId": r.assignedOwnerId or r.routedToUserId}
            for r in routed
        ]
        await db.commit()
        return {"interimCode": interim["reportCode"], "findings": findings}


async def phase_resolve(ctx: dict, findings: list[dict], branches: list[str],
                        auditee_photos: dict[str, list[dict]]) -> None:
    """Walk every routed finding through the iteration state machine."""
    now = ctx["now"]
    async with AsyncSessionLocal() as db:
        lead = await db.get(User, ctx["leadId"])
        pm_user = await db.get(User, ctx["pmId"])
        tally = {"ACCEPT": 0, "MORE_INFO": 0, "ESCALATE": 0, "CAPA": 0}

        for i, f in enumerate(findings):
            branch = branches[i]
            owner = (await db.get(User, f["ownerId"])) if f["ownerId"] else None
            owner = owner or lead
            photos = auditee_photos.get(f["code"], [])
            reply = AUDITEE_RESPONSES[i % len(AUDITEE_RESPONSES)]

            await svc.transition_checkpoint(
                db, user=owner, audit_id=ctx["auditId"], checkpoint_id=f["id"],
                action="AUDITEE_RESPOND",
                payload={
                    "comment": reply, "actionTaken": reply,
                    "actionDate": (now - timedelta(days=6)).isoformat(),
                    "estimatedClosureDate": (now + timedelta(days=14)).isoformat(),
                    "photos": photos[:1],
                    "evidenceIds": [p["storagePath"] for p in photos[:1]],
                },
            )

            if branch == "MORE_INFO":
                await svc.transition_checkpoint(
                    db, user=lead, audit_id=ctx["auditId"], checkpoint_id=f["id"],
                    action="REQUEST_MORE_INFO",
                    payload={"comment": (
                        "The action described is right, but the evidence covers "
                        "one month only. Supply the full sampled period with the "
                        "manager's review signatures."
                    )},
                )
                await svc.transition_checkpoint(
                    db, user=owner, audit_id=ctx["auditId"], checkpoint_id=f["id"],
                    action="AUDITEE_RESPOND",
                    payload={
                        "comment": AUDITEE_ROUND2, "actionTaken": AUDITEE_ROUND2,
                        "actionDate": (now - timedelta(days=3)).isoformat(),
                        "photos": photos[1:2],
                        "evidenceIds": [p["storagePath"] for p in photos[1:2]],
                    },
                )
                await svc.transition_checkpoint(
                    db, user=lead, audit_id=ctx["auditId"], checkpoint_id=f["id"],
                    action="ACCEPT",
                    payload={"comment": (
                        "Full period now evidenced and countersigned. Accepted "
                        "at the closing meeting."
                    )},
                )
            elif branch == "ESCALATE":
                await svc.transition_checkpoint(
                    db, user=lead, audit_id=ctx["auditId"], checkpoint_id=f["id"],
                    action="ESCALATE",
                    payload={"comment": (
                        "Remediation needs resource outside the department's "
                        "control. Referred to the plant manager for a decision."
                    )},
                )
                await svc.transition_checkpoint(
                    db, user=pm_user, audit_id=ctx["auditId"], checkpoint_id=f["id"],
                    action="PM_ACCEPT",
                    payload={"comment": (
                        "Budget approved and the work is scheduled into the "
                        "shutdown window. Accepted."
                    )},
                )
            elif branch == "CAPA":
                await svc.transition_checkpoint(
                    db, user=lead, audit_id=ctx["auditId"], checkpoint_id=f["id"],
                    action="RAISE_CAPA",
                    payload={"comment": (
                        "Immediate correction accepted, but the underlying cause "
                        "is systemic. Raising a CAPA to carry the root-cause work."
                    )},
                )
            else:
                await svc.transition_checkpoint(
                    db, user=lead, audit_id=ctx["auditId"], checkpoint_id=f["id"],
                    action="ACCEPT",
                    payload={"comment": (
                        "Evidence reviewed against the finding and accepted at "
                        "the closing meeting."
                    )},
                )
            tally[branch] += 1
        await db.commit()
        print("  9. iterations resolved  " + ", ".join(f"{k.lower()}={v}"
                                                       for k, v in tally.items()))


async def phase_close(ctx: dict) -> dict:
    """Closing meeting, sign-offs, closure, FINAL report."""
    now = ctx["now"]
    async with AsyncSessionLocal() as db:
        lead = await db.get(User, ctx["leadId"])
        pm_user = await db.get(User, ctx["pmId"])
        auditees = [await db.get(User, uid) for uid in ctx["auditeeIds"]]

        await assurance.upsert_meeting(
            db, engagement_kind="AUDIT", engagement_id=ctx["auditId"],
            meeting_type="CLOSING", user=lead,
            payload={
                "heldAt": (now - timedelta(days=2)).isoformat(),
                "attendees": ctx["attendees"],
                "findingsSummaryPresented": (
                    "Every finding was presented with its evidence, grade, "
                    "compliance status and risk grade. Corrective actions and "
                    "target dates were agreed with each owner."
                ),
                "auditeeAcknowledged": True,
                "auditeeAcknowledgedByUserId": auditees[0].id,
                "notes": (
                    "The audit conclusion, the critical non-conformities and the "
                    "CAPA register were reviewed. No dissent was recorded."
                ),
            },
        )
        print(" 10. closing meeting      findings presented and acknowledged")

        a = await db.get(ComplianceAudit, ctx["auditId"])
        await signoff.record_signoff(
            db, audit=a, user=lead, role="LEAD_AUDITOR", signature_kind="TYPED",
            typed_name=lead.name,
            statement="I confirm this audit was conducted per the approved programme.",
        )
        await signoff.record_signoff(
            db, audit=a, user=auditees[0], role="AUDITEE_OWNER",
            signature_kind="TYPED", typed_name=auditees[0].name,
            statement="Findings received and corrective actions agreed.",
        )
        await signoff.record_signoff(
            db, audit=a, user=pm_user, role="PLANT_MANAGER", signature_kind="TYPED",
            typed_name=pm_user.name,
            statement="Audit result noted and resourcing for the agreed actions approved.",
        )
        # One per discipline, signed by whoever actually conducted it.
        auditor_by_disc = dict((await db.execute(
            select(AuditCheckpointResponse.categoryId,
                   AuditCheckpointResponse.assignedAuditorId)
            .where(AuditCheckpointResponse.auditId == ctx["auditId"]).distinct()
        )).all())
        for disc, auditor_id in auditor_by_disc.items():
            signer = (await db.get(User, auditor_id)) if auditor_id else lead
            await signoff.record_signoff(
                db, audit=a, user=signer or lead, role="DISCIPLINE_AUDITOR",
                discipline_code=disc, signature_kind="TYPED",
                typed_name=(signer or lead).name,
                statement=(f"I conducted the {disc} discipline and confirm the "
                           "findings recorded."),
            )
        status = await signoff.signoff_status(db, a)
        print(f" 11. sign-offs            {len(status['signOffs'])} recorded "
              f"({status['disciplinesSigned']}/{status['disciplinesTotal']} disciplines)")

        await svc.close_audit(
            db, user=lead, audit_id=ctx["auditId"],
            closing_remarks=(
                "All findings responded to, evidenced and accepted. The critical "
                "non-conformities carry CAPAs tracked to closure outside this "
                "audit. Audit closed."
            ),
        )
        a = await db.get(ComplianceAudit, ctx["auditId"])
        final = await svc.generate_report(
            db, user=lead, audit_id=ctx["auditId"], report_type="FINAL",
            sign_offs=list(a.signOffs or []),
        )
        print(f" 12. closed + FINAL       {final['reportCode']}")
        await db.commit()
        return final


async def phase_verify(ctx: dict, final: dict) -> None:
    """Read the report back and say, section by section, whether it is complete."""
    async with AsyncSessionLocal() as db:
        snap = final["snapshot"]
        reg: list[dict] = []
        cursor = None
        while True:
            page = await svc.list_report_register(
                db, report_id=final["id"], cursor=cursor, limit=200,
            )
            if not page:
                break
            reg.extend(page["register"])
            cursor = page["nextCursor"]
            if not cursor:
                break

        with_auditor_ev = sum(1 for e in reg if e["auditorEvidenceIds"])
        with_auditee_ev = sum(1 for e in reg if e["auditeeEvidenceIds"])
        with_obs = sum(1 for e in reg if (e["observation"] or "").strip())
        with_thread = sum(1 for e in reg if e["interactions"])

        checks = [
            ("cover / plant name", bool(snap.get("plantName"))),
            ("executive summary", snap.get("checkpointsAssessed") == snap.get("checkpointsTotal")),
            ("discipline compliance", len(snap.get("categoryScores") or []) == len(DISCIPLINES)),
            ("standards rollup", bool(snap.get("standardsRollup"))),
            ("findings register", bool(snap.get("findings"))),
            ("CAPA snapshot", (snap.get("capaSummary") or {}).get("total", 0) > 0),
            ("checkpoint register", len(reg) == snap.get("checkpointsTotal")),
            ("observations on every row", with_obs == len(reg)),
            ("auditor photos on every row", with_auditor_ev == len(reg)),
            ("auditee photos on findings", with_auditee_ev > 0),
            ("iteration threads", with_thread > 0),
            ("sign-offs", bool(final.get("signOffs"))),
            ("methodology + limitations", bool((snap.get("methodology") or {}).get("limitations"))),
            ("independence statement", bool((snap.get("independence") or {}).get("statement"))),
            ("opening meeting", (snap.get("meetings") or {}).get("opening", {}).get("recorded")),
            ("closing meeting", (snap.get("meetings") or {}).get("closing", {}).get("recorded")),
            ("team competence", bool(snap.get("competence"))),
            ("clause index", bool(snap.get("clauseIndex"))),
            ("distribution list", bool(snap.get("distributionList"))),
            ("revision history", bool(snap.get("revisionHistory"))),
            ("no false data-integrity flag", not snap.get("dataIntegrityFlags")),
        ]
        print("\nReport completeness check")
        for label, ok in checks:
            print(f"  [{'x' if ok else ' '}] {label}")
        missing = [label for label, ok in checks if not ok]

        print(f"\n  audit          {ctx['auditNumber']}  (closed)")
        print(f"  compliance     {snap.get('overallScorePct')}%  ({snap.get('overallResult')})")
        print(f"  checkpoints    {len(reg)}  |  findings {len(snap.get('findings') or [])}"
              f"  |  CAPAs {(snap.get('capaSummary') or {}).get('total')}")
        print(f"  evidence       {with_auditor_ev} rows with auditor photos, "
              f"{with_auditee_ev} with auditee photos")
        print(f"  report page    /cams/audits/{ctx['auditId']}/reports/{final['id']}")
        print(f"  report PDF     /api/audit-compliance/reports/{final['id']}/pdf")
        if missing:
            print("\n  INCOMPLETE: " + ", ".join(missing))
        else:
            print("\nDone - every section of the report is populated.")


async def main() -> None:
    if not storage.is_storage_configured():
        raise SystemExit(
            "Supabase storage is not configured (SUPABASE_URL + "
            "SUPABASE_SERVICE_ROLE_KEY). Evidence photographs cannot be seeded "
            "without it."
        )

    ctx = await phase_schedule()

    # ── Auditor evidence, uploaded with no transaction open ──────────────
    plan = ctx["plan"]
    specs: list[dict] = []
    for cp in ctx["checkpoints"]:
        p = plan[cp["code"]]
        adverse = p["grade"] in FINDING_NOTES
        # Every checkpoint carries at least one frame; a finding carries two,
        # because a finding is what a reader zooms into.
        for frame in range(1, 3 if adverse else 2):
            specs.append({
                "kind": "AUDITOR", "auditId": ctx["auditId"], "code": cp["code"],
                "discipline": cp["discipline"], "question": cp["question"],
                "site": ctx["plantName"],
                "captured": ctx["scheduled"] + timedelta(minutes=len(specs) * 3),
                "frame": frame,
                "caption": (f"{'Non-conformity' if adverse else 'Verification'} "
                            f"evidence - {cp['code']} frame {frame}"),
            })
    print(f"  4. evidence upload      {len(specs)} auditor frames ...")
    uploaded = await upload_all(specs)
    auditor_photos: dict[str, list[dict]] = {}
    for spec, photo in zip(specs, uploaded):
        auditor_photos.setdefault(spec["code"], []).append(photo)

    conducted = await phase_conduct(ctx, auditor_photos)
    findings = conducted["findings"]

    # Route each finding down a different branch of the state machine, so the
    # thread in the report shows more than one shape of resolution.
    branches = [
        "MORE_INFO" if i % 7 == 3 else
        "ESCALATE" if i % 11 == 5 else
        "CAPA" if i % 9 == 8 else
        "ACCEPT"
        for i in range(len(findings))
    ]

    # ── Auditee evidence, likewise outside any transaction ───────────────
    auditee_specs: list[dict] = []
    for i, f in enumerate(findings):
        for frame in range(1, (2 if branches[i] == "MORE_INFO" else 1) + 1):
            auditee_specs.append({
                "kind": "AUDITEE", "auditId": ctx["auditId"], "code": f["code"],
                "discipline": f["discipline"], "question": f["question"],
                "site": ctx["plantName"],
                "captured": (ctx["now"] - timedelta(days=6)
                             + timedelta(minutes=len(auditee_specs) * 5)),
                "frame": frame,
                "caption": f"Remediation evidence - {f['code']} round {frame}",
            })
    print(f"  8. evidence upload      {len(auditee_specs)} auditee frames ...")
    auditee_uploaded = await upload_all(auditee_specs)
    auditee_photos: dict[str, list[dict]] = {}
    for spec, photo in zip(auditee_specs, auditee_uploaded):
        auditee_photos.setdefault(spec["code"], []).append(photo)

    await phase_resolve(ctx, findings, branches, auditee_photos)
    final = await phase_close(ctx)
    await phase_verify(ctx, final)


if __name__ == "__main__":
    asyncio.run(main())
