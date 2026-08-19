"""Adding, revising, retiring and deleting fire checklists.

The eleven Page Industries sheets arrived as seed data, but a client hands over a
new EHS checklist every few months and a revised one more often than that. So the
library has to be editable from the product, not only from `seed_fire_checklists.py`
— otherwise "the client sent us CL/029" is a developer ticket.

WHAT A CHECKLIST IS HERE
------------------------
Still a `CamsTemplate`. This module adds no tables and no second template store;
it is the write side of the same rows the seeder creates, with the rules a
controlled document needs on top.

THE THREE RULES THAT MATTER
---------------------------
1. **A published sheet with recorded inspections is frozen.** Editing its items
   would retroactively change what a signed record was answering. The engine
   already snapshots `templateVersionUsed` per run, but a snapshot of a *version
   number* is no use if the version's text was edited underneath it. So an edit
   to a template that has runs is refused with a pointer to `clone_revision`.

2. **Publishing retires the previous revision of the same sheet.** "The same
   sheet" is (assetType, frequency, siteVariant) — the tuple that decides which
   template a screen offers for a given asset. Two APPROVED templates on that
   tuple is an ambiguity the UI resolves arbitrarily, which is how a plant ends up
   half-inspecting against R1 and half against R2.

3. **Retire is not delete.** A retired template stops being offered but keeps
   serving the runs already recorded against it — a signed inspection has to
   remain readable, and its questions live on the template. Hard delete is
   available only while nothing has ever been recorded.

Statuses are CAMS's own: DRAFT -> IN_REVIEW -> APPROVED -> RETIRED. Reusing them
means the CAMS template screens, the approval gate in `transition_engagement` and
these routes all agree on what "APPROVED" means.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

from app.models.cams import (
    CamsEngagement, CamsResponse, CamsTemplate, CamsTemplateQuestion, CamsTemplateSection,
)
from app.services.fire_checklists import ChecklistError, SOURCE_MODULE
from app.services.fire_checklist_templates import (
    ALL_TEMPLATES, BEAM_DETECTOR, FIRE_ALARM_PANEL, FIRE_EXTINGUISHER, HYDRANT_SYSTEM,
    LAYOUT_DAY_GRID, LAYOUT_FORM, LAYOUT_MONTH_GRID, LAYOUT_QUARTER_GRID,
)

# The closed vocabularies a fire checklist may declare. Validated server-side
# because a template is configuration that outlives the screen that created it:
# a typo'd assetType produces a sheet that silently matches no asset, and the
# only symptom is an empty tab.
ASSET_TYPES = (FIRE_ALARM_PANEL, BEAM_DETECTOR, HYDRANT_SYSTEM, FIRE_EXTINGUISHER)
FREQUENCIES = ("DAILY", "MONTHLY", "QUARTERLY", "ANNUAL")
LAYOUTS = (LAYOUT_DAY_GRID, LAYOUT_MONTH_GRID, LAYOUT_QUARTER_GRID, LAYOUT_FORM)
ITEM_TYPES = ("YES_NO_NA", "NUMERIC", "TEXT")

# Which layouts a cadence can legally be drawn with. A DAILY sheet paged as a
# 12-month grid, or a QUARTERLY one paged by day, produces columns whose period
# labels the run resolver then rejects — a mismatch worth catching at authoring
# time rather than as a 400 in front of an inspector.
_LAYOUTS_FOR_FREQUENCY: dict[str, tuple[str, ...]] = {
    "DAILY": (LAYOUT_DAY_GRID, LAYOUT_FORM),
    "MONTHLY": (LAYOUT_MONTH_GRID, LAYOUT_FORM),
    "QUARTERLY": (LAYOUT_QUARTER_GRID, LAYOUT_FORM),
    "ANNUAL": (LAYOUT_FORM,),
}

_SEEDED_CODES = frozenset(t.code for t in ALL_TEMPLATES)
_KEY_RE = re.compile(r"^[a-z0-9][a-z0-9_]{1,62}$")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def is_fire_template(tpl: CamsTemplate) -> bool:
    """A fire checklist is a CamsTemplate whose documentMeta declares a fire asset.

    Discriminating on the metadata rather than on a list of known template codes
    is what lets a checklist added through the product show up alongside the
    eleven seeded ones. The earlier code-list filter would have hidden every
    template a user created.
    """
    meta = tpl.documentMeta or {}
    return meta.get("assetType") in ASSET_TYPES and bool(meta.get("documentNo"))


def is_seeded(tpl: CamsTemplate) -> bool:
    """True for the eleven transcribed client sheets.

    Not a permission — an authorised user may revise a seeded sheet, and should,
    when the client issues a new revision. It drives one warning in the UI: a
    direct edit will be overwritten the next time `seed_fire_checklists.py` runs,
    whereas a cloned revision will not.
    """
    return tpl.templateCode in _SEEDED_CODES


async def load(db, template_id: str) -> CamsTemplate:
    tpl = (
        await db.execute(
            select(CamsTemplate)
            .options(selectinload(CamsTemplate.sections).selectinload(CamsTemplateSection.questions))
            .where(CamsTemplate.id == template_id)
        )
    ).scalars().first()
    if tpl is None or tpl.isDeleted:
        raise ChecklistError("Checklist template not found.", 404)
    if not is_fire_template(tpl):
        raise ChecklistError("That template is not a fire checklist.", 409)
    return tpl


async def run_count(db, template_id: str) -> int:
    """How many inspections have been recorded against this template."""
    return (
        await db.execute(
            select(func.count())
            .select_from(CamsEngagement)
            .where(CamsEngagement.templateId == template_id)
            .where(CamsEngagement.sourceModule == SOURCE_MODULE)
            .where(CamsEngagement.isDeleted.is_(False))
        )
    ).scalar() or 0


async def list_templates(db, *, include_retired: bool = False) -> list[CamsTemplate]:
    stmt = (
        select(CamsTemplate)
        .options(selectinload(CamsTemplate.sections).selectinload(CamsTemplateSection.questions))
        .where(CamsTemplate.isDeleted.is_(False))
    )
    if not include_retired:
        stmt = stmt.where(CamsTemplate.status != "RETIRED")
    rows = [t for t in (await db.execute(stmt)).scalars().all() if is_fire_template(t)]
    # Seeded sheets first, in workbook-tab order, then anything added later. An
    # inspector's muscle memory is the tab order of the sheets they know.
    order = {t.code: i for i, t in enumerate(ALL_TEMPLATES)}
    rows.sort(key=lambda t: (order.get(t.templateCode, 10_000), t.name))
    return rows


# ═══════════════════════════════════════════════════════════════════════════
# Validation
# ═══════════════════════════════════════════════════════════════════════════
def validate_definition(body: dict[str, Any], *, existing_keys: set[str] | None = None) -> None:
    """Reject a definition that would produce an unusable sheet."""
    asset_type = body.get("assetType")
    if asset_type not in ASSET_TYPES:
        raise ChecklistError(f"assetType must be one of {', '.join(ASSET_TYPES)}.")
    frequency = body.get("frequency")
    if frequency not in FREQUENCIES:
        raise ChecklistError(f"frequency must be one of {', '.join(FREQUENCIES)}.")
    layout = body.get("layout")
    if layout not in LAYOUTS:
        raise ChecklistError(f"layout must be one of {', '.join(LAYOUTS)}.")
    if layout not in _LAYOUTS_FOR_FREQUENCY[frequency]:
        raise ChecklistError(
            f"A {frequency} checklist cannot use the {layout} layout "
            f"(allowed: {', '.join(_LAYOUTS_FOR_FREQUENCY[frequency])}). "
            "The grid columns would carry period labels this cadence cannot resolve."
        )
    if not (body.get("documentNo") or "").strip():
        raise ChecklistError("documentNo is required — a controlled document without its number is not controlled.")

    sections = body.get("sections") or []
    if not sections:
        raise ChecklistError("Add at least one section.")
    seen: set[str] = set()
    total = 0
    for s_i, sec in enumerate(sections, start=1):
        if not (sec.get("title") or "").strip():
            raise ChecklistError(f"Section {s_i} has no heading.")
        items = sec.get("items") or []
        if not items:
            raise ChecklistError(f"Section '{sec.get('title')}' has no items.")
        for item in items:
            key = (item.get("key") or "").strip().lower()
            if not _KEY_RE.match(key):
                raise ChecklistError(
                    f"Item key '{item.get('key')}' is invalid — use lowercase letters, digits "
                    "and underscores (2-63 chars). The key is the record identity for every "
                    "answer already stored against this item, so it has to be stable and typeable."
                )
            if key in seen:
                raise ChecklistError(f"Duplicate item key '{key}'. Keys identify answers; a duplicate loses one item.")
            seen.add(key)
            if not (item.get("text") or "").strip():
                raise ChecklistError(f"Item '{key}' has no wording.")
            itype = item.get("type") or "YES_NO_NA"
            if itype not in ITEM_TYPES:
                raise ChecklistError(f"Item '{key}' has an unknown type. Use one of {', '.join(ITEM_TYPES)}.")
            # A pass/fail check that raises no CAPA has to say why. Silently
            # exempting one is how "every No raises a CAPA" quietly stops being
            # true, and the reason is what a reviewer needs in order to disagree.
            if itype == "YES_NO_NA" and item.get("triggersFinding") is False                     and not (item.get("noFindingReason") or "").strip():
                raise ChecklistError(
                    f"Item '{key}' is a Yes/No/NA check with CAPA raising switched off. "
                    "Give a reason (noFindingReason) — e.g. the question is inverted and "
                    '"No" is the healthy answer.'
                )
            total += 1
    if total > 400:
        raise ChecklistError("A single sheet is capped at 400 items.")

    # Dropping a key that answers are already stored against orphans those
    # answers: they stay in the response JSON keyed to a question that no longer
    # renders, so the sheet silently loses recorded history.
    if existing_keys:
        removed = existing_keys - seen
        if removed:
            head = ", ".join(sorted(removed)[:5])
            raise ChecklistError(
                f"{len(removed)} item(s) would be removed ({head}). Answers are stored against "
                "item keys, so removing one orphans its recorded history. Publish a new revision "
                "instead — the old one keeps serving the inspections already filed against it."
            )


def _document_meta(body: dict[str, Any], *, audit_type_id: str | None) -> dict[str, Any]:
    return {
        "documentNo": (body.get("documentNo") or "").strip(),
        "supersedesNo": (body.get("supersedesNo") or "").strip() or None,
        "revision": (body.get("revision") or "R1").strip(),
        "effectiveDate": body.get("effectiveDate"),
        "reviewDate": body.get("reviewDate"),
        "department": (body.get("department") or "EHS").strip(),
        "pageLabel": body.get("pageLabel") or "1 of 1",
        "frequency": body["frequency"],
        "assetType": body["assetType"],
        "layout": body["layout"],
        "siteVariant": (body.get("siteVariant") or "").strip() or None,
        "sourceSheet": body.get("sourceSheet") or "",
        "signOffRoles": body.get("signOffRoles") or [
            "Prepared by: Person In-charge",
            "Reviewed by: Intermediatory Head",
            "Approved by: HOD",
        ],
        "footnotes": body.get("footnotes") or [],
        "sectionNotes": {
            (s.get("title") or ""): s["note"]
            for s in (body.get("sections") or []) if s.get("note")
        },
        "auditTypeId": audit_type_id,
    }


async def _next_code(db, body: dict[str, Any]) -> str:
    """A stable, human-recognisable templateCode derived from the document number.

    Derived rather than random so the code an admin sees in a URL or an export
    filename still says which sheet it is.
    """
    base = re.sub(r"[^A-Z0-9]+", "-", (body["documentNo"] or "").upper()).strip("-") or "FIRE-CHECKLIST"
    variant = (body.get("siteVariant") or "").strip().upper().replace(" ", "_")
    candidate = f"{base}-{variant}" if variant else base
    candidate = candidate[:80]
    taken = set(
        (await db.execute(select(CamsTemplate.templateCode).where(CamsTemplate.templateCode.like(f"{candidate}%"))))
        .scalars().all()
    )
    if candidate not in taken:
        return candidate
    for n in range(2, 100):
        alt = f"{candidate}-{n}"
        if alt not in taken:
            return alt
    raise ChecklistError("Could not derive a unique template code; set one explicitly.")


async def _write_sections(
    db, tpl: CamsTemplate, body: dict[str, Any], *, loaded: list[CamsTemplateSection] | None = None,
) -> None:
    """Rebuild sections/questions, reusing question rows by item key.

    Reuse is the whole point: question ids are what `CamsResponse.answers` is
    keyed by, so recreating a question that already has answers would orphan them.
    Matching on the key across ALL sections (not per section) also means moving an
    item under a different heading in a revision keeps its history.

    `loaded` is passed explicitly by the create path. Reading `tpl.sections` on a
    freshly-added instance makes SQLAlchemy lazy-load the collection, which under
    asyncio raises MissingGreenlet rather than issuing a query — the standard
    async-ORM trap, and the reason this is a parameter and not a lookup.
    """
    loaded = list(tpl.sections) if loaded is None else loaded
    existing_sections = {s.title: s for s in loaded}
    existing_questions = {
        q.standardClauseRef: q for s in loaded for q in s.questions if q.standardClauseRef
    }
    kept_questions: set[str] = set()

    for s_i, sec_def in enumerate(body["sections"]):
        title = (sec_def["title"] or "").strip()
        sec = existing_sections.get(title)
        if sec is None:
            sec = CamsTemplateSection(templateId=tpl.id, title=title, orderIndex=s_i)
            db.add(sec)
            await db.flush()
        sec.orderIndex = s_i

        for q_i, item in enumerate(sec_def["items"]):
            key = (item["key"] or "").strip().lower()
            q = existing_questions.get(key)
            if q is None:
                q = CamsTemplateQuestion(sectionId=sec.id, standardClauseRef=key)
                db.add(q)
            else:
                q.sectionId = sec.id
            q.orderIndex = q_i
            q.text = (item["text"] or "").strip()
            q.questionType = item.get("type") or "YES_NO_NA"
            q.isMandatory = bool(item.get("mandatory", True))
            q.guidance = (item.get("guidance") or "").strip() or None
            # A reading can never be non-conforming, so a flag saying it raises a
            # finding would read as enabled and be inert. Forced off here for the
            # same reason the transcribed templates force it off.
            is_judgement = (item.get("type") or "YES_NO_NA") == "YES_NO_NA"
            q.ncTriggersFinding = bool(item.get("triggersFinding", True)) and is_judgement
            q.evidenceRequiredOnNc = False
            q.weight = None
            sev = item.get("ncSeverity") or "MINOR_NC"
            if sev not in ("OBSERVATION", "MINOR_NC", "MAJOR_NC", "CRITICAL_NC"):
                sev = "MINOR_NC"
            cfg: dict[str, Any] = {"ncSeverity": sev}
            if item.get("noFindingReason"):
                cfg["noFindingReason"] = str(item["noFindingReason"])[:400]
            q.options = cfg
            kept_questions.add(key)
    await db.flush()

    # Sections the revision no longer names. Only removable when empty — validate
    # already refused any change that drops a key, so a section still holding
    # questions here means those questions moved elsewhere.
    for sec in loaded:
        if sec.title not in {(s["title"] or "").strip() for s in body["sections"]}:
            still = [q for q in sec.questions if (q.standardClauseRef or "") in kept_questions]
            if not still:
                await db.delete(sec)
    await db.flush()


# ═══════════════════════════════════════════════════════════════════════════
# Create / revise / publish / retire / delete
# ═══════════════════════════════════════════════════════════════════════════
async def create(db, body: dict[str, Any], *, actor_id: str, audit_type_id: str | None) -> CamsTemplate:
    """A new checklist, as DRAFT. Publishing it is a separate authority."""
    validate_definition(body)
    code = (body.get("templateCode") or "").strip() or await _next_code(db, body)
    if (await db.execute(select(CamsTemplate.id).where(CamsTemplate.templateCode == code))).scalars().first():
        raise ChecklistError(f"Template code '{code}' already exists.", 409)

    tpl = CamsTemplate(
        templateCode=code,
        name=(body.get("name") or body["documentNo"]).strip(),
        description=f"{body['documentNo']} ({body.get('revision') or 'R1'})",
        applicableEngagementTypes=["INSPECTION"],
        standardRefs=[body["documentNo"], body.get("supersedesNo")],
        documentMeta=_document_meta(body, audit_type_id=audit_type_id),
        # PASS_FAIL, matching the seeded sheets: a statutory equipment check has
        # no pass mark, and "82 % of the fire alarm works" is not a compliance
        # statement. One NO fails the sheet.
        scoringConfig={"mode": "PASS_FAIL"},
        ownerId=actor_id,
        isGlobal=True,
        status="DRAFT",
        version=1,
        createdBy=actor_id,
        updatedBy=actor_id,
    )
    db.add(tpl)
    await db.flush()
    # A brand-new template has no sections; say so explicitly rather than reading
    # (and so lazy-loading) the relationship. See _write_sections' docstring.
    await _write_sections(db, tpl, body, loaded=[])
    return tpl


async def update(db, tpl: CamsTemplate, body: dict[str, Any], *, actor_id: str) -> CamsTemplate:
    """Edit a checklist in place. Refused once inspections exist against it."""
    runs = await run_count(db, tpl.id)
    if runs:
        raise ChecklistError(
            f"{runs} inspection(s) are already recorded against this sheet, so its items are frozen — "
            "editing them would change what a signed record was answering. "
            "Clone it as a new revision instead; the old one keeps serving those inspections.",
            409,
        )
    if tpl.status == "RETIRED":
        raise ChecklistError("A retired checklist cannot be edited. Clone it to revive it.", 409)

    existing_keys = {q.standardClauseRef for s in tpl.sections for q in s.questions if q.standardClauseRef}
    validate_definition(body, existing_keys=existing_keys if tpl.status == "APPROVED" else None)

    tpl.name = (body.get("name") or body["documentNo"]).strip()
    tpl.description = f"{body['documentNo']} ({body.get('revision') or 'R1'})"
    tpl.standardRefs = [body["documentNo"], body.get("supersedesNo")]
    tpl.documentMeta = _document_meta(body, audit_type_id=(tpl.documentMeta or {}).get("auditTypeId"))
    tpl.updatedBy = actor_id
    await _write_sections(db, tpl, body)
    return tpl


def to_definition(tpl: CamsTemplate) -> dict[str, Any]:
    """The editable shape, for the authoring screen and for cloning."""
    meta = dict(tpl.documentMeta or {})
    notes = meta.get("sectionNotes") or {}
    return {
        "name": tpl.name,
        "documentNo": meta.get("documentNo"),
        "supersedesNo": meta.get("supersedesNo"),
        "revision": meta.get("revision"),
        "effectiveDate": meta.get("effectiveDate"),
        "reviewDate": meta.get("reviewDate"),
        "department": meta.get("department"),
        "assetType": meta.get("assetType"),
        "frequency": meta.get("frequency"),
        "layout": meta.get("layout"),
        "siteVariant": meta.get("siteVariant"),
        "sourceSheet": meta.get("sourceSheet"),
        "signOffRoles": meta.get("signOffRoles") or [],
        "footnotes": meta.get("footnotes") or [],
        "sections": [
            {
                "title": sec.title,
                "note": notes.get(sec.title),
                "items": [
                    {
                        "key": q.standardClauseRef,
                        "text": q.text,
                        "type": q.questionType,
                        "guidance": q.guidance,
                        "mandatory": q.isMandatory,
                        "triggersFinding": q.ncTriggersFinding,
                        "ncSeverity": (q.options or {}).get("ncSeverity", "MINOR_NC")
                        if isinstance(q.options, dict) else "MINOR_NC",
                        "noFindingReason": (q.options or {}).get("noFindingReason")
                        if isinstance(q.options, dict) else None,
                    }
                    for q in sorted(sec.questions, key=lambda x: x.orderIndex)
                ],
            }
            for sec in sorted(tpl.sections, key=lambda s: s.orderIndex)
        ],
    }


def _bump_revision(revision: str | None) -> str:
    """R1 -> R2. Falls through to appending when the client's scheme isn't Rn."""
    m = re.match(r"^R(\d+)$", (revision or "").strip(), re.I)
    return f"R{int(m.group(1)) + 1}" if m else f"{(revision or 'R1').strip()}-rev"


async def clone_revision(db, tpl: CamsTemplate, *, actor_id: str, revision: str | None = None) -> CamsTemplate:
    """A new DRAFT revision of an existing sheet.

    The route out of rule 1: when a client issues a revised sheet, or a frozen
    one needs a wording fix, this is how. `supersedesNo` is set to the parent's
    document number so the lineage is on the record, which is what the source
    sheets themselves do ("Supersedes No.: PIL/EHS/CL/002-R0").
    """
    body = to_definition(tpl)
    parent_doc = body.get("documentNo")
    body["revision"] = (revision or "").strip() or _bump_revision(body.get("revision"))
    body["supersedesNo"] = parent_doc
    body["name"] = f"{tpl.name} ({body['revision']})"
    child = await create(
        db, body, actor_id=actor_id, audit_type_id=(tpl.documentMeta or {}).get("auditTypeId"),
    )
    child.parentTemplateId = tpl.id
    child.version = (tpl.version or 1) + 1
    await db.flush()
    return child


async def publish(db, tpl: CamsTemplate, *, actor_id: str) -> tuple[CamsTemplate, list[str]]:
    """DRAFT/IN_REVIEW -> APPROVED, retiring the previous revision of the same sheet.

    Returns (template, retired template codes). "The same sheet" is
    (assetType, frequency, siteVariant): the tuple a screen uses to pick a
    template for an asset. Leaving two APPROVED on that tuple means the picker
    chooses arbitrarily, and a plant ends up half-inspected against each.
    """
    if tpl.status == "APPROVED":
        raise ChecklistError("This checklist is already published.", 409)
    if tpl.status == "RETIRED":
        raise ChecklistError("A retired checklist cannot be published. Clone it instead.", 409)
    if not tpl.sections or not any(s.questions for s in tpl.sections):
        raise ChecklistError("Add at least one section with an item before publishing.")

    meta = tpl.documentMeta or {}
    others = [
        t for t in await list_templates(db)
        if t.id != tpl.id
        and t.status == "APPROVED"
        and (t.documentMeta or {}).get("assetType") == meta.get("assetType")
        and (t.documentMeta or {}).get("frequency") == meta.get("frequency")
        and (t.documentMeta or {}).get("siteVariant") == meta.get("siteVariant")
    ]
    retired: list[str] = []
    for other in others:
        other.status = "RETIRED"
        other.updatedBy = actor_id
        retired.append(other.templateCode)

    tpl.status = "APPROVED"
    tpl.approvedBy = actor_id
    tpl.approvedAt = _now()
    tpl.updatedBy = actor_id
    await db.flush()
    return tpl, retired


async def retire(db, tpl: CamsTemplate, *, actor_id: str) -> CamsTemplate:
    """Stop offering this sheet. Inspections already filed against it stay readable."""
    if tpl.status == "RETIRED":
        return tpl
    tpl.status = "RETIRED"
    tpl.updatedBy = actor_id
    await db.flush()
    return tpl


async def delete(db, tpl: CamsTemplate, *, actor_id: str) -> dict[str, Any]:
    """Hard-delete a checklist — only while nothing has been recorded against it.

    A template with runs is retired instead, and the caller is told so rather
    than being given a silent no-op. Deleting it would take the questions with it
    and leave every stored answer pointing at nothing, which turns a signed
    inspection into an unreadable blob.
    """
    runs = await run_count(db, tpl.id)
    if runs:
        await retire(db, tpl, actor_id=actor_id)
        return {
            "deleted": False, "retired": True, "runCount": runs,
            "reason": (
                f"{runs} inspection(s) are recorded against this sheet, so it was retired rather "
                "than deleted — deleting it would leave those signed records unreadable. It will "
                "no longer be offered for new inspections."
            ),
        }
    tpl.isDeleted = True
    tpl.status = "RETIRED"
    tpl.updatedBy = actor_id
    await db.flush()
    return {"deleted": True, "retired": False, "runCount": 0,
            "reason": "No inspections were recorded against this sheet, so it was removed."}


def out(tpl: CamsTemplate, *, run_count_value: int | None = None, with_definition: bool = False) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "id": tpl.id,
        "templateCode": tpl.templateCode,
        "name": tpl.name,
        "status": tpl.status,
        "version": tpl.version,
        "document": dict(tpl.documentMeta or {}),
        "itemCount": sum(len(s.questions) for s in tpl.sections),
        "sectionCount": len(tpl.sections),
        "seeded": is_seeded(tpl),
        "parentTemplateId": tpl.parentTemplateId,
        "approvedAt": tpl.approvedAt.isoformat() if tpl.approvedAt else None,
    }
    if run_count_value is not None:
        payload["runCount"] = run_count_value
        # Surfaced so the screen can explain WHY edit is unavailable, rather than
        # just greying the button out.
        payload["frozen"] = run_count_value > 0
    if with_definition:
        payload["definition"] = to_definition(tpl)
    return payload


__all__ = [
    "ASSET_TYPES", "FREQUENCIES", "LAYOUTS", "ITEM_TYPES",
    "is_fire_template", "is_seeded", "load", "run_count", "list_templates",
    "validate_definition", "create", "update", "to_definition", "clone_revision",
    "publish", "retire", "delete", "out",
]
