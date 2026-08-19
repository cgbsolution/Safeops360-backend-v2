"""Fire checklist + extinguisher register API (PIL/EHS/CL 025-028).

Mounted on the same `/api/fire` prefix as `routers/fire_safety.py` — one module,
one URL namespace. Splitting it into its own file rather than growing
fire_safety.py past 1,500 lines is the only reason it is separate.

RBAC is per action, not a read/write pair: `FIRE.EXECUTE` fills a sheet,
`FIRE.VERIFY` reviews it, `FIRE.APPROVE` locks it, and those go to different
roles because the sign-off block printed on the client's own sheets says they
should. See services/fire_permissions.py.

Everything here is a thin HTTP shell over two services:

  • services/fire_checklists.py — period identity, run resolution, the
    Prepared/Reviewed/Approved chain, and the grid pivot.
  • services/fire_register.py  — the sixteen-column register projection and its
    due-date badges.

The routes deliberately hold no business rules of their own: the same "can this
be approved yet" answer has to hold whether it is asked from this router, the
seeder or a future scheduled job.
"""

from __future__ import annotations

from datetime import date, datetime, time, timezone
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.core.deps import get_current_user
from app.models.cams import CamsEngagement, CamsTemplate
from app.models.fire_safety import FireEquipment, PlantNonWorkingDay
from app.models.user import User
from app.services import fire_capa
from app.services import fire_checklist_admin as admin
from app.services import fire_checklist_pdf as pdfsvc
from app.services import fire_checklists as svc
from app.services import fire_register as regsvc
from app.services import fire_signoff
from app.services import fire_permissions as perm
from app.services.access_scope import build_query_scope

router = APIRouter(prefix="/api/fire", tags=["fire-safety"])

# Permission codes are named per action rather than collapsed into a read/write
# pair, because the sheets themselves separate the three sign-off authorities and
# a router that cannot tell EXECUTE from APPROVE cannot enforce that separation.
# services/fire_permissions.py holds the codes and the migration guard for
# deployments whose RBAC has not been reseeded yet.
_require = perm.require


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _domain(exc: svc.ChecklistError) -> HTTPException:
    return HTTPException(exc.status, exc.message)


async def _names(db: AsyncSession, ids: list[str | None]) -> dict[str, str]:
    """userId -> display name, for the sign-off block.

    The block prints a person, not a cuid: "Sign. & Date" on the paper original
    carries a human's name, and an export showing `clx8k2...` next to a date is
    not the document the auditor was handed.
    """
    wanted = sorted({i for i in ids if i})
    if not wanted:
        return {}
    rows = (await db.execute(select(User).where(User.id.in_(wanted)))).scalars().all()
    return {u.id: (getattr(u, "name", None) or getattr(u, "email", None) or u.id) for u in rows}


def _with_names(payload: dict[str, Any], names: dict[str, str]) -> dict[str, Any]:
    so = payload.get("signOff") or {}
    for role in ("prepared", "reviewed", "approved"):
        uid = so.get(f"{role}By")
        so[f"{role}ByName"] = names.get(uid) if uid else None
    return payload


# ═══════════════════════════════════════════════════════════════════════════
# Templates + asset pickers
# ═══════════════════════════════════════════════════════════════════════════
@router.get("/checklists/capabilities")
async def read_capabilities(
    plantId: str | None = Query(None),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """What this principal may do, so a screen can hide what it cannot offer.

    A page that shows an Approve button to someone who will get a 403 teaches the
    rule by failing at it, and on a shared plant terminal the operator cannot tell
    a permission problem from a bug.
    """
    return await perm.capabilities(db, user, plantId)


@router.get("/checklists/templates")
async def list_templates(
    assetType: str | None = Query(None),
    frequency: str | None = Query(None),
    includeRetired: bool = Query(False),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Every fire checklist in the library, with its controlled-document header.

    Discriminates on `documentMeta.assetType`, NOT on the eleven seeded template
    codes. The earlier code-list filter meant a checklist added through the
    product was invisible to every screen that lists them — which made "add
    another checklist" a feature that appeared to do nothing.

    Retired revisions are excluded by default: they still serve the inspections
    already filed against them, but offering one for a new inspection is how a
    plant ends up recording against a superseded sheet.
    """
    await _require(db, user, perm.READ)
    rows = await admin.list_templates(db, include_retired=includeRetired)
    items = []
    for t in rows:
        meta = t.documentMeta or {}
        if assetType and meta.get("assetType") != assetType:
            continue
        if frequency and meta.get("frequency") != frequency:
            continue
        items.append(admin.out(t))
    if not items and not (assetType or frequency):
        raise HTTPException(404, "No fire checklist templates found. Run seed_fire_checklists.py, "
                                 "or add one from the Checklist Library screen.")
    return {"items": items, "total": len(items)}


@router.get("/checklists/templates/{template_id}")
async def read_template(
    template_id: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """One checklist with its full editable definition."""
    await _require(db, user, perm.READ)
    try:
        tpl = await admin.load(db, template_id)
        return admin.out(tpl, run_count_value=await admin.run_count(db, tpl.id), with_definition=True)
    except svc.ChecklistError as exc:
        raise _domain(exc) from exc


class ChecklistItemIn(BaseModel):
    # The stable identity of this row across revisions and re-seeds; every stored
    # answer is keyed to it. Lowercase slug, validated server-side.
    key: str
    text: str
    type: str = "YES_NO_NA"
    guidance: str | None = None
    mandatory: bool = True
    # A "No" raises a finding and a CAPA. ON by default: duplicates are prevented
    # by one-open-CAPA-per-(asset,item) in services/fire_capa.py, not by declining
    # to raise them. Switching it off on a Yes/No/NA item requires a reason.
    triggersFinding: bool = True
    ncSeverity: str = "MINOR_NC"
    noFindingReason: str | None = None


class ChecklistSectionIn(BaseModel):
    title: str
    note: str | None = None
    items: list[ChecklistItemIn] = Field(default_factory=list)


class ChecklistDefinitionIn(BaseModel):
    """A controlled checklist, as its own document plus its sections."""

    name: str | None = None
    documentNo: str
    supersedesNo: str | None = None
    revision: str = "R1"
    effectiveDate: str | None = None
    reviewDate: str | None = None
    department: str = "EHS"
    assetType: str
    frequency: str
    layout: str
    siteVariant: str | None = None
    sourceSheet: str | None = None
    signOffRoles: list[str] | None = None
    footnotes: list[str] | None = None
    templateCode: str | None = None
    sections: list[ChecklistSectionIn] = Field(default_factory=list)


async def _audit_type_id(db: AsyncSession) -> str | None:
    """The FIRE-CHECKLIST audit type the seeder creates, if it exists yet."""
    from app.models.cams import CamsAuditType

    return (
        await db.execute(select(CamsAuditType.id).where(CamsAuditType.typeCode == "FIRE-CHECKLIST"))
    ).scalars().first()


@router.post("/checklists/templates", status_code=201)
async def create_template(
    body: ChecklistDefinitionIn,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Add a checklist. Created as DRAFT — publishing is a separate authority.

    TEMPLATE_AUTHOR, not UPDATE: transcribing a client's controlled sheet and
    editing an inspection record are different jobs, and the person who does the
    first should not automatically be able to rule their own transcription fit to
    publish.
    """
    await _require(db, user, perm.TEMPLATE_AUTHOR)
    try:
        tpl = await admin.create(
            db, body.model_dump(), actor_id=user.id, audit_type_id=await _audit_type_id(db),
        )
        await db.commit()
    except svc.ChecklistError as exc:
        await db.rollback()
        raise _domain(exc) from exc
    tpl = await admin.load(db, tpl.id)
    return admin.out(tpl, run_count_value=0, with_definition=True)


@router.put("/checklists/templates/{template_id}")
async def update_template(
    template_id: str,
    body: ChecklistDefinitionIn,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Edit a checklist. Refused once inspections are recorded against it."""
    await _require(db, user, perm.TEMPLATE_AUTHOR)
    try:
        tpl = await admin.load(db, template_id)
        await admin.update(db, tpl, body.model_dump(), actor_id=user.id)
        await db.commit()
    except svc.ChecklistError as exc:
        await db.rollback()
        raise _domain(exc) from exc
    tpl = await admin.load(db, template_id)
    return admin.out(tpl, run_count_value=await admin.run_count(db, tpl.id), with_definition=True)


class CloneIn(BaseModel):
    revision: str | None = None


@router.post("/checklists/templates/{template_id}/clone", status_code=201)
async def clone_template(
    template_id: str,
    body: CloneIn | None = None,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """A new DRAFT revision of an existing sheet.

    The supported route for revising a frozen checklist: the old revision keeps
    serving the inspections already filed against it, and the new one carries
    `supersedesNo` pointing at its parent — the same lineage the paper sheets
    print.
    """
    await _require(db, user, perm.TEMPLATE_AUTHOR)
    try:
        parent = await admin.load(db, template_id)
        child = await admin.clone_revision(
            db, parent, actor_id=user.id, revision=(body.revision if body else None),
        )
        await db.commit()
    except svc.ChecklistError as exc:
        await db.rollback()
        raise _domain(exc) from exc
    child = await admin.load(db, child.id)
    return admin.out(child, run_count_value=0, with_definition=True)


@router.post("/checklists/templates/{template_id}/publish")
async def publish_template(
    template_id: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Publish a revision for use, retiring the previous one of the same sheet.

    TEMPLATE_APPROVE, deliberately not the same code as TEMPLATE_AUTHOR: this
    publishes a version of a controlled document that every future inspection on
    the site is recorded against. It is a document-control act, and the person who
    transcribed the sheet should not also be the one who rules it fit to publish.
    """
    await _require(db, user, perm.TEMPLATE_APPROVE)
    try:
        tpl = await admin.load(db, template_id)
        _tpl, retired = await admin.publish(db, tpl, actor_id=user.id)
        await db.commit()
    except svc.ChecklistError as exc:
        await db.rollback()
        raise _domain(exc) from exc
    tpl = await admin.load(db, template_id)
    payload = admin.out(tpl, run_count_value=await admin.run_count(db, tpl.id), with_definition=True)
    payload["retiredTemplates"] = retired
    return payload


@router.post("/checklists/templates/{template_id}/retire")
async def retire_template(
    template_id: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Stop offering this sheet. Inspections already filed against it stay readable."""
    await _require(db, user, perm.TEMPLATE_APPROVE)
    try:
        tpl = await admin.load(db, template_id)
        await admin.retire(db, tpl, actor_id=user.id)
        await db.commit()
    except svc.ChecklistError as exc:
        await db.rollback()
        raise _domain(exc) from exc
    tpl = await admin.load(db, template_id)
    return admin.out(tpl, run_count_value=await admin.run_count(db, tpl.id))


@router.delete("/checklists/templates/{template_id}")
async def delete_template(
    template_id: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Delete a checklist — or retire it, and say so, if inspections exist.

    Returns 200 with `{deleted, retired, reason}` rather than 204, because
    "retired instead of deleted" is an outcome the operator needs to be told
    about. A silent 204 that actually retired the sheet would be a lie.
    """
    await _require(db, user, perm.DELETE)
    try:
        tpl = await admin.load(db, template_id)
        result = await admin.delete(db, tpl, actor_id=user.id)
        await db.commit()
    except svc.ChecklistError as exc:
        await db.rollback()
        raise _domain(exc) from exc
    return {"templateId": template_id, **result}


@router.get("/checklists/assets")
async def list_checklist_assets(
    assetType: str = Query(..., description="FIRE_ALARM_PANEL | BEAM_DETECTOR | FIRE_HYDRANT_SYSTEM | FIRE_EXTINGUISHER"),
    q: str | None = Query(None, description="match on code, allotted serial or location"),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """The asset picker behind each checklist screen. Plant-scoped."""
    await _require(db, user, perm.READ)
    scope = await build_query_scope(db, user.id, perm.READ)
    stmt = scope.apply(
        select(FireEquipment)
        .where(FireEquipment.isDeleted.is_(False))
        .where(FireEquipment.isActive.is_(True))
        .where(FireEquipment.type == assetType),
        FireEquipment,
    )
    rows = (await db.execute(stmt)).scalars().all()
    if q:
        needle = q.strip().lower()
        rows = [
            e for e in rows
            if needle in (e.equipmentCode or "").lower()
            or needle in (e.allottedSerialNo or "").lower()
            or needle in (e.location or "").lower()
        ]
    rows.sort(key=lambda e: (e.location or "", e.equipmentCode))
    return {
        "items": [
            {
                "id": e.id, "equipmentCode": e.equipmentCode, "type": e.type,
                "assetSubtype": e.assetSubtype, "location": e.location, "plantId": e.plantId,
                "capacitySpec": e.capacitySpec, "allottedSerialNo": e.allottedSerialNo,
                "status": e.status,
            }
            for e in rows
        ],
        "total": len(rows),
    }


# ═══════════════════════════════════════════════════════════════════════════
# Runs
# ═══════════════════════════════════════════════════════════════════════════
async def _load_run(db: AsyncSession, run_id: str) -> tuple[CamsTemplate, CamsEngagement, FireEquipment | None]:
    run = await db.get(CamsEngagement, run_id)
    if run is None or run.isDeleted or run.sourceModule != svc.SOURCE_MODULE or not run.periodLabel:
        raise HTTPException(404, "Checklist run not found.")
    try:
        tpl = await svc.load_template(db, template_id=run.templateId)
    except svc.ChecklistError as exc:
        raise _domain(exc) from exc
    asset = await db.get(FireEquipment, run.sourceEntityId) if run.sourceEntityId else None
    return tpl, run, asset


@router.get("/checklists/run")
async def get_or_create_run(
    templateCode: str = Query(...),
    assetId: str = Query(...),
    period: str | None = Query(None, description="omit for the current period"),
    create: bool = Query(True, description="false to look up without creating"),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """The run for (template, asset, period) — created on first access.

    Auto-create is the whole point: an inspector on the floor opens today's sheet
    and starts ticking. Making them press "create" first is a step the paper
    process does not have (they pick up the clipboard) and one more thing to
    forget. `create=false` exists for the grid, which must render an empty column
    for a day nobody inspected without conjuring a record for it.
    """
    try:
        tpl = await svc.load_template(db, template_code=templateCode)
        meta = svc.template_meta(tpl)
        asset = await svc.resolve_asset(db, tpl, assetId)
        await _require(db, user, perm.READ if not create else perm.EXECUTE, plant_id=asset.plantId)
        period = period or svc.period_label(meta.get("frequency", "DAILY"), _now().date())
        svc.validate_period(meta.get("frequency", "DAILY"), period)

        run = await svc.find_run(db, tpl, asset.id, period)
        created = False
        if run is None:
            if not create:
                return {
                    "run": None, "templateCode": tpl.templateCode, "templateName": tpl.name,
                    "document": meta, "assetId": asset.id, "periodLabel": period,
                }
            run, created = await svc.get_or_create_run(db, tpl, asset, period, actor_id=user.id)
            await db.commit()
        resp = await svc.load_response(db, run)
    except svc.ChecklistError as exc:
        raise _domain(exc) from exc

    payload = svc.run_out(tpl, run, resp, asset)
    payload["created"] = created
    return _with_names(payload, await _names(db, [resp.completedBy, run.reviewedBy, run.approvedBy]))


class RunCreate(BaseModel):
    templateCode: str
    assetId: str
    period: str | None = None


@router.post("/checklists/run", status_code=201)
async def create_run(
    body: RunCreate, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Explicit create, for callers that would rather not rely on GET's side effect."""
    try:
        tpl = await svc.load_template(db, template_code=body.templateCode)
        meta = svc.template_meta(tpl)
        asset = await svc.resolve_asset(db, tpl, body.assetId)
        await _require(db, user, perm.EXECUTE, plant_id=asset.plantId)
        period = body.period or svc.period_label(meta.get("frequency", "DAILY"), _now().date())
        run, created = await svc.get_or_create_run(db, tpl, asset, period, actor_id=user.id)
        await db.commit()
        resp = await svc.load_response(db, run)
    except svc.ChecklistError as exc:
        raise _domain(exc) from exc
    payload = svc.run_out(tpl, run, resp, asset)
    payload["created"] = created
    return _with_names(payload, await _names(db, [resp.completedBy, run.reviewedBy, run.approvedBy]))


@router.get("/checklists/run/{run_id}")
async def read_run(
    run_id: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    tpl, run, asset = await _load_run(db, run_id)
    await _require(db, user, perm.READ, plant_id=run.siteId)
    resp = await svc.load_response(db, run)
    return _with_names(
        svc.run_out(tpl, run, resp, asset),
        await _names(db, [resp.completedBy, run.reviewedBy, run.approvedBy]),
    )


class AnswerIn(BaseModel):
    """One answered item. Identify it by `itemKey` (stable across reseeds) or by
    `questionId`; the key is what a grid cell knows about itself."""

    itemKey: str | None = None
    questionId: str | None = None
    value: Any = None
    note: str | None = None
    ncSeverity: str | None = None
    evidenceAttachmentIds: list[str] | None = None


class ResponsesIn(BaseModel):
    answers: list[AnswerIn] = Field(default_factory=list)


@router.put("/checklists/run/{run_id}/responses")
async def save_responses(
    run_id: str, body: ResponsesIn,
    user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Bulk-save the period's answers. Idempotent merge, not a replace.

    Merge rather than replace so a partially-connected tablet saving eight of a
    sheet's twenty items cannot blank the other twelve by omitting them.
    """
    tpl, run, asset = await _load_run(db, run_id)
    await _require(db, user, perm.EXECUTE, plant_id=run.siteId)
    try:
        resp = await svc.save_answers(
            db, tpl, run, [a.model_dump(exclude_none=True) for a in body.answers], actor_id=user.id,
        )
        await db.commit()
    except svc.ChecklistError as exc:
        await db.rollback()
        raise _domain(exc) from exc
    await db.refresh(run)
    return _with_names(
        svc.run_out(tpl, run, resp, asset),
        await _names(db, [resp.completedBy, run.reviewedBy, run.approvedBy]),
    )


# Prepared / Reviewed / Approved are three separate authorities, not one write
# grant. Collapsing them would let one principal fill a sheet, review it and
# approve it -- which is exactly what the three-stage block printed on the sheet
# exists to prevent, and stage-ORDER enforcement is no substitute: order stops
# you skipping a stage, not from being all three people.
_STAGE_PERMISSION = {
    svc.STAGE_SUBMITTED: perm.EXECUTE,
    svc.STAGE_REVIEWED: perm.VERIFY,
    svc.STAGE_APPROVED: perm.APPROVE,
}


class SignOffBody(BaseModel):
    """The signature accompanying a stage transition.

    Mirrors `assurance.SignOffBody` field-for-field — same DRAWN/TYPED vocabulary,
    same `signaturePayload` data-URI convention — because it is the same platform
    mechanism, not a fire-specific one.

    Optional on the wire so a DAILY sheet can advance without one (see
    `svc.signature_enforced` for why daily rounds are exempted); the service
    rejects a missing signature on every sheet that requires it.
    """

    signatureKind: Literal["DRAWN", "TYPED"] = "DRAWN"
    signaturePayload: str | None = None   # PNG data URI for DRAWN
    typedName: str | None = None          # required for TYPED
    designation: str | None = None


async def _advance(
    db: AsyncSession, user: User, run_id: str, stage: str, body: SignOffBody | None,
) -> dict[str, Any]:
    tpl, run, asset = await _load_run(db, run_id)
    await _require(db, user, _STAGE_PERMISSION[stage], plant_id=run.siteId)
    body = body or SignOffBody()
    try:
        _run, outcome = await svc.advance(
            db, tpl, run, stage, actor_id=user.id,
            # The signer is the authenticated user, resolved here where the row is
            # fresh. There is deliberately no way to sign in someone else's name —
            # that manufactures evidence.
            signer=fire_signoff.signer_identity(user),
            signature_kind=body.signatureKind,
            signature_payload=body.signaturePayload,
            typed_name=body.typedName,
            designation=body.designation,
        )
        await db.commit()
    except svc.ChecklistError as exc:
        await db.rollback()
        raise _domain(exc) from exc
    await db.refresh(run)
    resp = await svc.load_response(db, run)
    payload = _with_names(
        svc.run_out(tpl, run, resp, asset),
        await _names(db, [resp.completedBy, run.reviewedBy, run.approvedBy]),
    )
    # What the transition actually created. An operator who just submitted a sheet
    # with four failures has opened four CAPAs and needs telling, not a silent 200.
    payload["outcome"] = outcome
    payload["outcomeMessage"] = fire_capa.summarise(outcome)
    return payload


@router.post("/checklists/run/{run_id}/submit")
async def submit_run(
    run_id: str, body: SignOffBody | None = None,
    user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db),
):
    """Prepared by — answers every mandatory item, then raises a CAPA per failure.

    Every "No" becomes a tracked defect here, deduped to one open CAPA per
    (asset, item) so a fault left unfixed for three weeks is one CAPA with 21
    occurrences rather than 21 CAPAs. See services/fire_capa.py.
    """
    return await _advance(db, user, run_id, svc.STAGE_SUBMITTED, body)


@router.post("/checklists/run/{run_id}/review")
async def review_run(
    run_id: str, body: SignOffBody | None = None,
    user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db),
):
    """Reviewed by — rejected unless the run is SUBMITTED."""
    return await _advance(db, user, run_id, svc.STAGE_REVIEWED, body)


@router.post("/checklists/run/{run_id}/approve")
async def approve_run(
    run_id: str, body: SignOffBody | None = None,
    user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db),
):
    """Approved by — rejected unless the run is REVIEWED. Locks the record."""
    return await _advance(db, user, run_id, svc.STAGE_APPROVED, body)


@router.get("/checklists/run/{run_id}/signoff")
async def read_signoff(
    run_id: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """What is signed, what is outstanding, and whether a signature is required."""
    tpl, run, _asset = await _load_run(db, run_id)
    await _require(db, user, perm.READ, plant_id=run.siteId)
    signed = fire_signoff.existing_roles(run)
    return {
        "runId": run.id,
        "stage": svc.stage_of(run),
        "signatureRequired": svc.signature_enforced(tpl),
        "roles": [
            {
                "role": role,
                "label": fire_signoff.ROLE_LABEL[role],
                "statement": fire_signoff.STATEMENT[role],
                "signed": role in signed,
            }
            for role in ("PREPARED_BY", "REVIEWED_BY", "APPROVED_BY")
        ],
        "signatures": fire_signoff.out(run),
        "maxSignatureBytes": fire_signoff.MAX_SIGNATURE_BYTES,
    }


@router.get("/checklists/run/{run_id}/defects")
async def run_defects(
    run_id: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """The defects this record raised, with their CAPAs.

    A failed check that leads nowhere visible is a failed check nobody acts on, so
    the record links straight through to the CAPA rather than leaving the operator
    to find it in the CAPA register.
    """
    from app.models.capa import Capa
    from app.models.cams import CamsFinding

    _tpl, run, _asset = await _load_run(db, run_id)
    await _require(db, user, perm.READ, plant_id=run.siteId)
    findings = (
        await db.execute(select(CamsFinding).where(CamsFinding.engagementId == run.id))
    ).scalars().all()
    capa_ids = [f.capaId for f in findings if f.capaId]
    capas: dict[str, Any] = {}
    if capa_ids:
        for c in (await db.execute(select(Capa).where(Capa.id.in_(capa_ids)))).scalars().all():
            capas[c.id] = {"id": c.id, "capaNumber": c.capaNumber, "state": c.state}
    return {
        "runId": run.id,
        "items": [
            {
                "findingId": f.id, "findingCode": f.findingCode, "title": f.title,
                "severity": f.severity, "status": f.status,
                "isRepeatFinding": f.isRepeatFinding,
                "occurrences": (getattr(f, "sourceMetadata", None) or {}).get("occurrences", 1),
                "capa": capas.get(f.capaId) if f.capaId else None,
            }
            for f in findings
        ],
        "total": len(findings),
    }


@router.get("/checklists/run/{run_id}/history")
async def run_history(
    run_id: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """The record's audit trail, from the platform's tamper-evident chain.

    Read out of the shared AuditLog rather than a checklist-specific history
    table: a second trail would be a second thing to keep honest, and this one is
    already hash-chained.
    """
    _tpl, run, _asset = await _load_run(db, run_id)
    await _require(db, user, perm.READ, plant_id=run.siteId)

    events: list[dict[str, Any]] = []
    try:
        from app.models.audit_log import AuditLog  # local: the table is optional in slim deploys

        rows = (
            await db.execute(
                select(AuditLog)
                .where(AuditLog.entityId.in_([run.id]))
                .order_by(AuditLog.createdAt.asc())
            )
        ).scalars().all()
        events = [
            {
                "at": r.createdAt.isoformat() if r.createdAt else None,
                "actorId": getattr(r, "actorId", None),
                "action": getattr(r, "action", None),
                "changed": getattr(r, "changedFields", None),
            }
            for r in rows
        ]
    except Exception:  # noqa: BLE001 — history is informational; never 500 a record view over it
        events = []

    resp = await svc.load_response(db, run)
    stamps = [
        {"stage": svc.STAGE_SUBMITTED, "by": resp.completedBy,
         "at": resp.completedAt.isoformat() if resp.completedAt else None},
        {"stage": svc.STAGE_REVIEWED, "by": run.reviewedBy,
         "at": run.reviewedAt.isoformat() if run.reviewedAt else None},
        {"stage": svc.STAGE_APPROVED, "by": run.approvedBy,
         "at": run.approvedAt.isoformat() if run.approvedAt else None},
    ]
    names = await _names(db, [s["by"] for s in stamps] + [e.get("actorId") for e in events])
    for s in stamps:
        s["byName"] = names.get(s["by"]) if s["by"] else None
    for e in events:
        e["actorName"] = names.get(e.get("actorId")) if e.get("actorId") else None
    return {"runId": run.id, "stage": svc.stage_of(run), "signOff": stamps, "events": events}


@router.get("/checklists/run/{run_id}/export.pdf")
async def export_run_pdf(
    run_id: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db),
) -> Response:
    tpl, run, asset = await _load_run(db, run_id)
    await _require(db, user, perm.EXPORT, plant_id=run.siteId)
    resp = await svc.load_response(db, run)
    payload = _with_names(
        svc.run_out(tpl, run, resp, asset),
        await _names(db, [resp.completedBy, run.reviewedBy, run.approvedBy]),
    )
    pdf = pdfsvc.render_form(payload)
    doc_no = (payload.get("document") or {}).get("documentNo", "checklist").replace("/", "-")
    return Response(
        content=pdf, media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="{doc_no}-{run.periodLabel}.pdf"'},
    )


# ═══════════════════════════════════════════════════════════════════════════
# Grid — the printed page
# ═══════════════════════════════════════════════════════════════════════════
async def _grid(db: AsyncSession, user: User, template_code: str, asset_id: str, window: str | None):
    try:
        tpl = await svc.load_template(db, template_code=template_code)
        meta = svc.template_meta(tpl)
        # A FORM sheet has no grid. Without this it would silently render a
        # single nonsense column — `default_window` returns a year, which is not
        # a valid period for a monthly sheet — and the caller would get a page
        # that looks plausible and is wrong. A 409 says which endpoint to use.
        if meta.get("layout") == "FORM":
            raise svc.ChecklistError(
                f"'{tpl.name}' is a single-period form, not a grid. "
                "Use /api/fire/checklists/run for this sheet.", 409,
            )
        asset = await svc.resolve_asset(db, tpl, asset_id)
        await _require(db, user, perm.READ, plant_id=asset.plantId)
        window = window or svc.default_window(meta.get("layout", "DAY_GRID"))
        payload = await svc.grid_out(db, tpl, asset, window)
    except svc.ChecklistError as exc:
        raise _domain(exc) from exc
    layout = payload["layout"]
    payload["prevWindow"] = svc.shift_window(layout, window, -1)
    payload["nextWindow"] = svc.shift_window(layout, window, 1)
    return payload


@router.get("/checklists/grid")
async def read_grid(
    templateCode: str = Query(...),
    assetId: str = Query(...),
    window: str | None = Query(None, description="YYYY-MM for a daily grid, YYYY otherwise"),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """One printed page: items down, periods across."""
    return await _grid(db, user, templateCode, assetId, window)


@router.get("/checklists/grid/export.pdf")
async def export_grid_pdf(
    templateCode: str = Query(...),
    assetId: str = Query(...),
    window: str | None = Query(None),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Response:
    payload = await _grid(db, user, templateCode, assetId, window)
    await _require(db, user, perm.EXPORT, plant_id=payload.get("plantId"))
    pdf = pdfsvc.render_grid(payload)
    doc_no = (payload.get("document") or {}).get("documentNo", "checklist").replace("/", "-")
    return Response(
        content=pdf, media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="{doc_no}-{payload["window"]}.pdf"'},
    )


class GridCellIn(BaseModel):
    periodLabel: str
    itemKey: str
    value: Any = None
    note: str | None = None


class GridSaveIn(BaseModel):
    templateCode: str
    assetId: str
    cells: list[GridCellIn] = Field(default_factory=list)


@router.put("/checklists/grid")
async def save_grid(
    body: GridSaveIn, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Save cells across several periods in one call.

    A daily grid is edited a column at a time in practice, but an inspector
    catching up after a weekend fills three columns before saving. Doing that as
    three round-trips means three chances to half-save, so the whole page saves
    together — and any period whose run is already signed off is reported back
    rather than silently skipped.
    """
    try:
        tpl = await svc.load_template(db, template_code=body.templateCode)
        asset = await svc.resolve_asset(db, tpl, body.assetId)
        await _require(db, user, perm.EXECUTE, plant_id=asset.plantId)

        by_period: dict[str, list[dict[str, Any]]] = {}
        for c in body.cells:
            by_period.setdefault(c.periodLabel, []).append(
                {"itemKey": c.itemKey, "value": c.value, "note": c.note}
            )

        saved, rejected = [], []
        for period, answers in by_period.items():
            run, _created = await svc.get_or_create_run(db, tpl, asset, period, actor_id=user.id)
            if svc.is_locked(run):
                rejected.append({"periodLabel": period, "stage": svc.stage_of(run),
                                 "reason": "signed off — edit rejected"})
                continue
            await svc.save_answers(db, tpl, run, answers, actor_id=user.id)
            saved.append(period)
        await db.commit()
    except svc.ChecklistError as exc:
        await db.rollback()
        raise _domain(exc) from exc

    # Return the page that was actually edited, not today's. A caller catching up
    # on last month would otherwise get September back after saving August and
    # have no way to tell the save landed on the right page.
    layout = svc.template_meta(tpl).get("layout", "DAY_GRID")
    edited = sorted(by_period)[0] if by_period else None
    window = (
        edited[:7] if edited and layout == svc.LAYOUT_DAY_GRID
        else edited[:4] if edited
        else svc.default_window(layout)
    )
    payload = await svc.grid_out(db, tpl, asset, window)
    return {"saved": saved, "rejected": rejected, "periodsSaved": len(saved), "grid": payload}


# ═══════════════════════════════════════════════════════════════════════════
# Register of Fire Extinguishers — PIL/EHSD/CL/028-R1
# ═══════════════════════════════════════════════════════════════════════════
@router.get("/register/extinguishers")
async def read_register(
    location: str | None = Query(None),
    feType: str | None = Query(None, alias="type", description="CO2 / ABC / DCP / FOAM"),
    badge: str | None = Query(None, description="OVERDUE | DUE_SOON | OK | NOT_RECORDED"),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """The sixteen-column register, with computed due-date badges."""
    await _require(db, user, perm.READ)
    scope = await build_query_scope(db, user.id, perm.READ)
    stmt = scope.apply(
        select(FireEquipment)
        .where(FireEquipment.isDeleted.is_(False))
        .where(FireEquipment.type == regsvc.EXTINGUISHER),
        FireEquipment,
    )
    rows = (await db.execute(stmt)).scalars().all()
    if location:
        needle = location.strip().lower()
        rows = [e for e in rows if needle in (e.location or "").lower()]
    if feType:
        rows = [e for e in rows if (e.assetSubtype or "").upper() == feType.strip().upper()]

    payload = await regsvc.build_register(db, rows)
    if badge:
        wanted = badge.strip().upper()
        payload["rows"] = [r for r in payload["rows"] if r["worstBadge"] == wanted]
        payload["filtered"] = len(payload["rows"])
    return payload


class RegisterUpsert(BaseModel):
    """The register's own columns. HP-test and refill dates are accepted flat, as
    the sheet prints them, and stored as certificates — see services/fire_register."""

    plantId: str | None = None
    equipmentCode: str | None = None
    serialNo: str | None = None
    allottedSerialNo: str | None = None
    feType: str | None = Field(None, alias="type")     # CO2 / ABC / DCP / FOAM
    capacity: str | None = None
    yearOfManufacture: int | None = Field(None, ge=1950, le=2100)
    expiryDate: datetime | None = None
    make: str | None = None
    location: str | None = None
    hpTestedOn: datetime | None = None
    hpTestDueDate: datetime | None = None
    dateOfDischarge: datetime | None = None
    refilledOn: datetime | None = None
    dueForRefilling: datetime | None = None
    weightKg: float | None = Field(None, ge=0)
    remarks: str | None = None

    model_config = {"populate_by_name": True}


def _apply_register_fields(e: FireEquipment, body: RegisterUpsert, fields: set[str]) -> None:
    mapping = {
        "serialNo": "serialNo", "allottedSerialNo": "allottedSerialNo", "feType": "assetSubtype",
        "capacity": "capacitySpec", "yearOfManufacture": "yearOfManufacture", "expiryDate": "expiryDate",
        "make": "make", "location": "location", "dateOfDischarge": "dateOfDischarge",
        "weightKg": "weightKg", "remarks": "registerRemarks",
    }
    for src, dest in mapping.items():
        if src in fields:
            setattr(e, dest, getattr(body, src))


@router.post("/register/extinguishers", status_code=201)
async def create_register_row(
    body: RegisterUpsert, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    if not body.plantId:
        raise HTTPException(400, "plantId is required.")
    if not body.location:
        raise HTTPException(400, "Location is required — the register is read by location.")
    await _require(db, user, perm.CREATE, plant_id=body.plantId)

    code = body.equipmentCode
    if not code:
        # Derived from the client's own allotted tag when there is one, so the
        # platform code and the number stencilled on the cylinder agree.
        suffix = body.allottedSerialNo or body.serialNo or _now().strftime("%y%m%d%H%M%S")
        code = f"FIRE-FE-{suffix}"
    if (await db.execute(select(FireEquipment).where(FireEquipment.equipmentCode == code))).scalars().first():
        raise HTTPException(409, f"Equipment code '{code}' already exists.")

    e = FireEquipment(
        equipmentCode=code, plantId=body.plantId, type=regsvc.EXTINGUISHER,
        location=body.location, inspectionFrequencyDays=30, isActive=True,
        createdBy=user.id, updatedBy=user.id,
    )
    _apply_register_fields(e, body, set(body.model_fields_set))
    db.add(e)
    await db.flush()

    await regsvc.upsert_certificate(db, e, regsvc.CERT_HYDROSTATIC,
                                    issued_on=body.hpTestedOn, due_on=body.hpTestDueDate, actor_id=user.id)
    await regsvc.upsert_certificate(db, e, regsvc.CERT_REFILL,
                                    issued_on=body.refilledOn, due_on=body.dueForRefilling, actor_id=user.id)
    await db.commit()
    await db.refresh(e)
    certs = (await regsvc.latest_certificates(db, [e.id])).get(e.id, {})
    return regsvc.register_row(e, certs)


@router.patch("/register/extinguishers/{eid}")
async def update_register_row(
    eid: str, body: RegisterUpsert,
    user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Partial update — only the fields actually sent are applied, so a client
    editing one cell cannot blank the rest of the row by omission."""
    e = await db.get(FireEquipment, eid)
    if e is None or e.isDeleted:
        raise HTTPException(404, "Extinguisher not found.")
    if e.type != regsvc.EXTINGUISHER:
        raise HTTPException(409, f"{e.equipmentCode} is not an extinguisher.")
    await _require(db, user, perm.UPDATE, plant_id=e.plantId)

    sent = set(body.model_fields_set)
    _apply_register_fields(e, body, sent)
    e.updatedBy = user.id

    if sent & {"hpTestedOn", "hpTestDueDate"}:
        await regsvc.upsert_certificate(db, e, regsvc.CERT_HYDROSTATIC,
                                        issued_on=body.hpTestedOn, due_on=body.hpTestDueDate, actor_id=user.id)
    if sent & {"refilledOn", "dueForRefilling"}:
        await regsvc.upsert_certificate(db, e, regsvc.CERT_REFILL,
                                        issued_on=body.refilledOn, due_on=body.dueForRefilling, actor_id=user.id)
    await db.commit()
    await db.refresh(e)
    certs = (await regsvc.latest_certificates(db, [e.id])).get(e.id, {})
    return regsvc.register_row(e, certs)


@router.get("/register/extinguishers/export.pdf")
async def export_register_pdf(
    location: str | None = Query(None),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Response:
    await _require(db, user, perm.EXPORT)
    payload = await read_register(location=location, feType=None, badge=None, user=user, db=db)
    pdf = pdfsvc.render_register(payload)
    return Response(
        content=pdf, media_type="application/pdf",
        headers={"Content-Disposition": 'inline; filename="PIL-EHSD-CL-028-R1-register.pdf"'},
    )


@router.get("/register/extinguishers/{eid}/inspections")
async def register_row_inspections(
    eid: str, year: int | None = Query(None),
    user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Click-through from a register row to that cylinder's inspection year grid."""
    e = await db.get(FireEquipment, eid)
    if e is None or e.isDeleted:
        raise HTTPException(404, "Extinguisher not found.")
    await _require(db, user, perm.READ, plant_id=e.plantId)
    return await _grid(db, user, "PIL-FE-INSPECTION", eid, str(year) if year else None)


# ═══════════════════════════════════════════════════════════════════════════
# Non-working days — the holiday-calendar stopgap
# ═══════════════════════════════════════════════════════════════════════════
@router.get("/non-working-days")
async def list_non_working_days(
    plantId: str = Query(...),
    window: str = Query(..., description="YYYY-MM"),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    await _require(db, user, perm.READ, plant_id=plantId)
    try:
        days = [date.fromisoformat(p) for p, _ in svc.grid_periods("DAY_GRID", "DAILY", window)]
    except svc.ChecklistError as exc:
        raise _domain(exc) from exc
    marked = await svc.non_working_days(db, plantId, days)
    return {
        "plantId": plantId, "window": window,
        # SUNDAY entries are computed; only HOLIDAY rows are editable, and the
        # UI needs to know which is which or it offers to delete a weekday.
        "days": [{"day": d, "label": lab, "editable": lab != "SUNDAY"} for d, lab in sorted(marked.items())],
    }


class NonWorkingDayIn(BaseModel):
    plantId: str
    day: date
    label: str = "HOLIDAY"


@router.post("/non-working-days", status_code=201)
async def mark_non_working_day(
    body: NonWorkingDayIn, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    await _require(db, user, perm.CALENDAR, plant_id=body.plantId)
    day = datetime.combine(body.day, time.min, tzinfo=timezone.utc)
    existing = (
        await db.execute(
            select(PlantNonWorkingDay)
            .where(PlantNonWorkingDay.plantId == body.plantId)
            .where(PlantNonWorkingDay.day == day)
        )
    ).scalars().first()
    if existing is None:
        existing = PlantNonWorkingDay(plantId=body.plantId, day=day, createdBy=user.id)
        db.add(existing)
    existing.label = body.label or "HOLIDAY"
    await db.commit()
    return {"plantId": body.plantId, "day": body.day.isoformat(), "label": existing.label}


@router.delete("/non-working-days", status_code=204)
async def unmark_non_working_day(
    plantId: str = Query(...), day: date = Query(...),
    user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db),
) -> Response:
    await _require(db, user, perm.CALENDAR, plant_id=plantId)
    target = datetime.combine(day, time.min, tzinfo=timezone.utc)
    row = (
        await db.execute(
            select(PlantNonWorkingDay)
            .where(PlantNonWorkingDay.plantId == plantId)
            .where(PlantNonWorkingDay.day == target)
        )
    ).scalars().first()
    if row is not None:
        await db.delete(row)
        await db.commit()
    return Response(status_code=204)
