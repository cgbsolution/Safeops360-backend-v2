from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.db import get_db
from app.core.deps import get_current_user, require_permission_with_context
from app.models.training import (
    TrainingProgram,
    TrainingProgramMaterial,
    TrainingProgramQuestion,
    TrainingRecord,
)
from app.models.plant import Plant
from app.models.user import User
from app.services.register_view import status_counts
from app.schemas.training import (
    ProgramApprovalDecision,
    ProgramRetire,
    ProgramSubmitForReview,
    TrainingCreate,
    TrainingProgramCreate,
    TrainingProgramMaterialInput,
    TrainingProgramMaterialOut,
    TrainingProgramOut,
    TrainingProgramQuestionInput,
    TrainingProgramQuestionOut,
    TrainingProgramUpdate,
    TrainingRecordOut,
)
from app.services.permissions import PermissionContext, can, get_user_role_codes

router = APIRouter(prefix="/api/training", tags=["training"])


# ═══════════════════════════════════════════════════════════════════════
#  TRAINING PROGRAM MASTER (production-depth)
# ═══════════════════════════════════════════════════════════════════════


@router.get("/analytics")
async def get_training_analytics(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """The competency dashboard, in one call.

    Read-only aggregate over training data the caller can already reach through
    the module; gated by TRAINING.READ rather than a narrower scope because
    every figure here is a rollup, not a record.
    """
    check = await can(db, user.id, "TRAINING.READ", PermissionContext())
    if not check.allowed:
        raise HTTPException(status.HTTP_403_FORBIDDEN, check.reason or "Access denied")
    from app.services.training_analytics import training_analytics

    return await training_analytics(db, datetime.now(timezone.utc))


@router.get("/programs")
async def list_programs(
    category: str | None = None,
    is_statutory: bool | None = None,
    plant_id: str | None = None,
    approval_status: str | None = None,
    active_only: bool = True,
    q: str | None = None,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """List training programs with filters. Default returns only ACTIVE
    + APPROVED programs (the workable set); pass active_only=false to
    see drafts and retired."""
    stmt = select(TrainingProgram)
    if active_only:
        stmt = stmt.where(TrainingProgram.isActive == True)  # noqa: E712
    if category:
        stmt = stmt.where(TrainingProgram.category == category)
    if is_statutory is not None:
        stmt = stmt.where(TrainingProgram.isStatutory == is_statutory)
    if plant_id:
        stmt = stmt.where(
            or_(TrainingProgram.plantId == plant_id, TrainingProgram.plantId.is_(None))
        )
    if approval_status:
        stmt = stmt.where(TrainingProgram.approvalStatus == approval_status)
    if q:
        like = f"%{q.lower()}%"
        stmt = stmt.where(
            or_(
                TrainingProgram.name.ilike(like),
                TrainingProgram.programName.ilike(like),
                TrainingProgram.code.ilike(like),
                TrainingProgram.programCode.ilike(like),
            )
        )

    # Approval-state tallies for the register chips, plus the statutory-coverage
    # figure the page headlines. Both describe the same filtered set the rows
    # come from, so a chip can never disagree with the list under it.
    counts = await status_counts(db, stmt, TrainingProgram.approvalStatus)
    statutory_approved = int(
        (
            await db.execute(
                select(func.count()).select_from(
                    stmt.where(TrainingProgram.isStatutory.is_(True))
                    .where(TrainingProgram.approvalStatus == "APPROVED")
                    .subquery()
                )
            )
        ).scalar_one()
    )

    rows = (await db.execute(stmt.order_by(TrainingProgram.name))).scalars().all()
    return {
        "items": [TrainingProgramOut.model_validate(r) for r in rows],
        "total": len(rows),
        "approvalCounts": counts,
        "statutoryApproved": statutory_approved,
    }


@router.get("/programs/{program_id}")
async def get_program(
    program_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """A programme with the relations its detail page renders: owning plant,
    owner, approver, and the ordered question + material lists.

    The questions and materials are also reachable on their own sub-routes for
    callers that want them alone; nesting them here keeps the detail page to a
    single request instead of three.
    """
    program = await db.get(TrainingProgram, program_id)
    if program is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Program not found")

    out = TrainingProgramOut.model_validate(program).model_dump()

    plant = await db.get(Plant, program.plantId) if program.plantId else None
    out["plant"] = {"id": plant.id, "name": plant.name} if plant else None

    people_ids = {
        pid for pid in (program.ownerId, program.approvedById) if pid
    }
    people = dict(
        (
            await db.execute(select(User.id, User.name).where(User.id.in_(people_ids)))
        ).all()
    ) if people_ids else {}
    out["owner"] = (
        {"id": program.ownerId, "name": people.get(program.ownerId)}
        if program.ownerId
        else None
    )
    out["approvedBy"] = (
        {"id": program.approvedById, "name": people.get(program.approvedById)}
        if program.approvedById
        else None
    )

    questions = (
        await db.execute(
            select(TrainingProgramQuestion)
            .where(TrainingProgramQuestion.programId == program_id)
            .order_by(TrainingProgramQuestion.sequence)
        )
    ).scalars().all()
    materials = (
        await db.execute(
            select(TrainingProgramMaterial)
            .where(TrainingProgramMaterial.programId == program_id)
            .order_by(TrainingProgramMaterial.sequence)
        )
    ).scalars().all()
    out["questions"] = [
        TrainingProgramQuestionOut.model_validate(q).model_dump() for q in questions
    ]
    out["materials"] = [
        TrainingProgramMaterialOut.model_validate(m).model_dump() for m in materials
    ]
    return out


@router.get("/programs/{program_id}/questions")
async def list_program_questions(
    program_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    rows = (
        await db.execute(
            select(TrainingProgramQuestion)
            .where(TrainingProgramQuestion.programId == program_id)
            .order_by(TrainingProgramQuestion.sequence)
        )
    ).scalars().all()
    return {"items": [TrainingProgramQuestionOut.model_validate(r) for r in rows]}


@router.get("/programs/{program_id}/materials")
async def list_program_materials(
    program_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    rows = (
        await db.execute(
            select(TrainingProgramMaterial)
            .where(TrainingProgramMaterial.programId == program_id)
            .order_by(TrainingProgramMaterial.sequence)
        )
    ).scalars().all()
    return {"items": [TrainingProgramMaterialOut.model_validate(r) for r in rows]}


# ─── Permission helper for program-master mutations ────────────────────


async def _program_mutation_check(
    db: AsyncSession, user: User, action: str = "TRAINING.UPDATE"
) -> None:
    """Programs are LD_MANAGER / HSE_MANAGER / ADMIN owned. We let the
    permission service enforce; falling back to a role-list check would
    duplicate the matrix. plantId on the context lets HSE_MANAGER do
    their own plant only."""
    result = await can(db, user.id, action, PermissionContext())
    if not result.allowed:
        raise HTTPException(status.HTTP_403_FORBIDDEN, result.reason or "Access denied")


# ─── Create / Update / Lifecycle ──────────────────────────────────────


@router.post(
    "/programs",
    response_model=TrainingProgramOut,
    status_code=status.HTTP_201_CREATED,
)
async def create_program(
    payload: TrainingProgramCreate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> TrainingProgramOut:
    await _program_mutation_check(db, user, "TRAINING.CREATE")

    # Reject duplicate programCode
    existing = (
        await db.execute(
            select(TrainingProgram).where(TrainingProgram.programCode == payload.programCode)
        )
    ).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"Program code '{payload.programCode}' already exists.",
        )

    # Pre-compute legacy fields so existing PTW/FLRA crew validation
    # keeps reading them.
    legacy_validity = (
        payload.certificateValidityMonths if payload.certificateValidityMonths is not None else 12
    )
    legacy_passing = payload.passingScorePercent if payload.passingScorePercent is not None else 60
    is_mandatory_legacy = (
        payload.isStatutory
        or len(payload.isMandatoryForRoles) > 0
        or len(payload.isMandatoryForPermitTypes) > 0
    )

    program = TrainingProgram(
        # Legacy + canonical paired
        code=payload.programCode,
        programCode=payload.programCode,
        name=payload.programName,
        programName=payload.programName,
        description=payload.description,
        # Tab 1
        category=payload.category,
        type=payload.type,
        ownerId=payload.ownerId or user.id,
        plantId=payload.plantId,
        # Tab 2
        isStatutory=payload.isStatutory,
        statutoryReference=payload.statutoryReference,
        isMandatoryForRoles=payload.isMandatoryForRoles,
        isMandatoryForActivities=payload.isMandatoryForActivities,
        isMandatoryForPermitTypes=payload.isMandatoryForPermitTypes,
        # Tab 3
        durationHours=payload.durationHours,
        durationSessions=payload.durationSessions,
        maxParticipantsPerBatch=payload.maxParticipantsPerBatch,
        language=payload.language,
        # Tab 4
        prerequisitePrograms=payload.prerequisitePrograms,
        prerequisiteRoles=payload.prerequisiteRoles,
        minimumExperienceMonths=payload.minimumExperienceMonths,
        medicalFitnessRequired=payload.medicalFitnessRequired,
        # Tab 5
        hasAssessment=payload.hasAssessment,
        assessmentType=payload.assessmentType,
        passingScore=legacy_passing,
        passingScorePercent=payload.passingScorePercent,
        practicalAssessmentRubric=payload.practicalAssessmentRubric,
        attemptsAllowed=payload.attemptsAllowed,
        # Tab 6
        issuesCertificate=payload.issuesCertificate,
        certificateTemplateUrl=payload.certificateTemplateUrl,
        validityMonths=legacy_validity,
        certificateValidityMonths=payload.certificateValidityMonths,
        certificateExpiryGracePeriodDays=payload.certificateExpiryGracePeriodDays,
        refresherProgramCode=payload.refresherProgramCode,
        # Tab 7
        contentOutline=payload.contentOutline,
        learningObjectives=payload.learningObjectives,
        # Tab 8
        approvedTrainerIds=payload.approvedTrainerIds,
        externalTrainerAllowed=payload.externalTrainerAllowed,
        trainerQualifications=payload.trainerQualifications,
        # Tab 9
        evaluatesEffectiveness=payload.evaluatesEffectiveness,
        effectivenessReviewMonths=payload.effectivenessReviewMonths,
        feedbackQuestionnaireId=payload.feedbackQuestionnaireId,
        # Tab 10
        blocksPtwIfMissing=payload.blocksPtwIfMissing,
        blocksRoleAssignmentIfMissing=payload.blocksRoleAssignmentIfMissing,
        blocksContractorOnboardingIfMissing=payload.blocksContractorOnboardingIfMissing,
        # Status
        mandatory=is_mandatory_legacy,
        isActive=True,
        approvalStatus="DRAFT",
    )
    db.add(program)
    await db.flush()

    # Sub-resources
    for q in payload.questions:
        db.add(
            TrainingProgramQuestion(
                programId=program.id,
                sequence=q.sequence,
                questionText=q.questionText,
                questionType=q.questionType,
                options=q.options,
                correctAnswer=q.correctAnswer,
                marks=q.marks,
                isCritical=q.isCritical,
                explanation=q.explanation,
            )
        )
    for m in payload.materials:
        db.add(
            TrainingProgramMaterial(
                programId=program.id,
                title=m.title,
                type=m.type,
                fileUrl=m.fileUrl,
                externalUrl=m.externalUrl,
                fileSize=m.fileSize,
                duration=m.duration,
                language=m.language,
                isMandatory=m.isMandatory,
                sequence=m.sequence,
            )
        )
    await db.flush()
    await db.refresh(program)
    return TrainingProgramOut.model_validate(program)


@router.patch("/programs/{program_id}", response_model=TrainingProgramOut)
async def update_program(
    program_id: str,
    payload: TrainingProgramUpdate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> TrainingProgramOut:
    await _program_mutation_check(db, user, "TRAINING.UPDATE")
    program = await db.get(TrainingProgram, program_id)
    if program is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Program not found")
    if program.approvalStatus == "RETIRED":
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Retired programs cannot be edited. Create a new program instead.",
        )

    # Safety-sensitive fields edited on an APPROVED program flip it
    # back to UNDER_REVIEW so HSE Manager re-approves.
    safety_fields = {
        "passingScorePercent",
        "certificateValidityMonths",
        "certificateExpiryGracePeriodDays",
        "isMandatoryForRoles",
        "isMandatoryForActivities",
        "isMandatoryForPermitTypes",
        "prerequisitePrograms",
        "prerequisiteRoles",
        "blocksPtwIfMissing",
        "blocksRoleAssignmentIfMissing",
        "blocksContractorOnboardingIfMissing",
    }
    payload_dict = payload.model_dump(exclude_unset=True)
    safety_change = any(k in safety_fields for k in payload_dict)
    if safety_change and program.approvalStatus == "APPROVED":
        program.approvalStatus = "UNDER_REVIEW"
        program.approvedById = None
        program.approvedAt = None

    for k, v in payload_dict.items():
        setattr(program, k, v)
        # Mirror legacy fields where applicable
        if k == "programName":
            program.name = v
        if k == "passingScorePercent" and v is not None:
            program.passingScore = v
        if k == "certificateValidityMonths" and v is not None:
            program.validityMonths = v

    await db.flush()
    await db.refresh(program)
    return TrainingProgramOut.model_validate(program)


@router.delete("/programs/{program_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_program(
    program_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    """Hard-delete only allowed for DRAFT programs. APPROVED/RETIRED
    programs preserve audit trail; use /retire instead."""
    await _program_mutation_check(db, user, "TRAINING.DELETE")
    program = await db.get(TrainingProgram, program_id)
    if program is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Program not found")
    if program.approvalStatus != "DRAFT":
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"Cannot hard-delete a {program.approvalStatus} program. Use /retire instead.",
        )
    await db.delete(program)
    await db.flush()


# ─── Lifecycle transitions ────────────────────────────────────────────


@router.post("/programs/{program_id}/submit")
async def submit_program_for_review(
    program_id: str,
    payload: ProgramSubmitForReview,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """DRAFT → UNDER_REVIEW. Owner / LD Manager triggers."""
    await _program_mutation_check(db, user, "TRAINING.UPDATE")
    program = await db.get(TrainingProgram, program_id)
    if program is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Program not found")
    if program.approvalStatus != "DRAFT":
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"Cannot submit a {program.approvalStatus} program for review.",
        )
    program.approvalStatus = "UNDER_REVIEW"
    await db.flush()
    return {"ok": True, "approvalStatus": "UNDER_REVIEW"}


@router.post("/programs/{program_id}/decide")
async def decide_program_approval(
    program_id: str,
    payload: ProgramApprovalDecision,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """UNDER_REVIEW → APPROVED or back to DRAFT (rejected). HSE_MANAGER
    or ADMIN role required."""
    role_codes = await get_user_role_codes(db, user.id)
    if not any(r in {"HSE_MANAGER", "ADMIN"} for r in role_codes):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "Only HSE Manager / Admin can approve or reject training programs.",
        )
    program = await db.get(TrainingProgram, program_id)
    if program is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Program not found")
    if program.approvalStatus != "UNDER_REVIEW":
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"Cannot decide on a {program.approvalStatus} program.",
        )
    if payload.decision == "APPROVED":
        program.approvalStatus = "APPROVED"
        program.approvedById = user.id
        program.approvedAt = datetime.now(timezone.utc)
    else:
        program.approvalStatus = "DRAFT"
    await db.flush()
    return {"ok": True, "approvalStatus": program.approvalStatus}


@router.post("/programs/{program_id}/retire")
async def retire_program(
    program_id: str,
    payload: ProgramRetire,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """APPROVED → RETIRED. New schedules cannot use this program but
    existing certificates stay valid until expiry. HSE_MANAGER required."""
    role_codes = await get_user_role_codes(db, user.id)
    if not any(r in {"HSE_MANAGER", "ADMIN", "LD_MANAGER"} for r in role_codes):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "Only HSE Manager / LD Manager / Admin can retire training programs.",
        )
    program = await db.get(TrainingProgram, program_id)
    if program is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Program not found")
    if program.approvalStatus != "APPROVED":
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"Cannot retire a {program.approvalStatus} program.",
        )
    program.approvalStatus = "RETIRED"
    program.isActive = False
    await db.flush()
    return {"ok": True, "approvalStatus": "RETIRED", "reason": payload.reason}


# ─── Sub-resources: questions ─────────────────────────────────────────


@router.post(
    "/programs/{program_id}/questions",
    response_model=TrainingProgramQuestionOut,
    status_code=status.HTTP_201_CREATED,
)
async def add_program_question(
    program_id: str,
    payload: TrainingProgramQuestionInput,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> TrainingProgramQuestionOut:
    await _program_mutation_check(db, user, "TRAINING.UPDATE")
    program = await db.get(TrainingProgram, program_id)
    if program is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Program not found")
    q = TrainingProgramQuestion(
        programId=program_id,
        sequence=payload.sequence,
        questionText=payload.questionText,
        questionType=payload.questionType,
        options=payload.options,
        correctAnswer=payload.correctAnswer,
        marks=payload.marks,
        isCritical=payload.isCritical,
        explanation=payload.explanation,
    )
    db.add(q)
    await db.flush()
    await db.refresh(q)
    return TrainingProgramQuestionOut.model_validate(q)


@router.delete(
    "/programs/{program_id}/questions/{question_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_program_question(
    program_id: str,
    question_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    await _program_mutation_check(db, user, "TRAINING.UPDATE")
    q = await db.get(TrainingProgramQuestion, question_id)
    if q is None or q.programId != program_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Question not found")
    await db.delete(q)
    await db.flush()


# ─── Sub-resources: materials ─────────────────────────────────────────


@router.post(
    "/programs/{program_id}/materials",
    response_model=TrainingProgramMaterialOut,
    status_code=status.HTTP_201_CREATED,
)
async def add_program_material(
    program_id: str,
    payload: TrainingProgramMaterialInput,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> TrainingProgramMaterialOut:
    await _program_mutation_check(db, user, "TRAINING.UPDATE")
    program = await db.get(TrainingProgram, program_id)
    if program is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Program not found")
    m = TrainingProgramMaterial(
        programId=program_id,
        title=payload.title,
        type=payload.type,
        fileUrl=payload.fileUrl,
        externalUrl=payload.externalUrl,
        fileSize=payload.fileSize,
        duration=payload.duration,
        language=payload.language,
        isMandatory=payload.isMandatory,
        sequence=payload.sequence,
    )
    db.add(m)
    await db.flush()
    await db.refresh(m)
    return TrainingProgramMaterialOut.model_validate(m)


@router.delete(
    "/programs/{program_id}/materials/{material_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_program_material(
    program_id: str,
    material_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    await _program_mutation_check(db, user, "TRAINING.UPDATE")
    m = await db.get(TrainingProgramMaterial, material_id)
    if m is None or m.programId != program_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Material not found")
    await db.delete(m)
    await db.flush()


# ═══════════════════════════════════════════════════════════════════════
#  TRAINING SCHEDULE + DELIVERY (production-depth)
# ═══════════════════════════════════════════════════════════════════════


from app.models.training import (  # noqa: E402
    TrainingAssessment,
    TrainingAssessmentResponse,
    TrainingAttendance,
    TrainingRegistration,
    TrainingSchedule,
    TrainingSession,
)
from app.schemas.training import (  # noqa: E402
    ScheduleCancel,
    ScheduleStateAction,
    TrainingAssessmentOut,
    TrainingAssessmentSubmit,
    TrainingAttendanceBulk,
    TrainingAttendanceInput,
    TrainingAttendanceOut,
    TrainingRegistrationCreate,
    TrainingRegistrationDecision,
    TrainingRegistrationOut,
    TrainingRegistrationWithdraw,
    TrainingScheduleCreate,
    TrainingScheduleOut,
    TrainingScheduleUpdate,
    TrainingSessionInput,
    TrainingSessionOut,
)
from sqlalchemy import func  # noqa: E402


# ─── Schedule list / get ──────────────────────────────────────────────


@router.get("/schedules")
async def list_schedules(
    plant_id: str | None = None,
    program_id: str | None = None,
    status_filter: str | None = None,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    stmt = select(TrainingSchedule)
    if plant_id:
        stmt = stmt.where(TrainingSchedule.plantId == plant_id)
    if program_id:
        stmt = stmt.where(TrainingSchedule.programId == program_id)
    if status_filter:
        stmt = stmt.where(TrainingSchedule.status == status_filter)
    # Scope-only SELECT — the tab counts describe the whole register, not the
    # 200 rows this page returns.
    counts = await status_counts(db, stmt, TrainingSchedule.status)

    rows = (
        # Newest-created first — platform-wide register convention.
        await db.execute(
            stmt.order_by(TrainingSchedule.createdAt.desc(), TrainingSchedule.id.desc()).limit(200)
        )
    ).scalars().all()

    # The register table shows programme + plant names, which live on other
    # tables; joining them here keeps the page to one call.
    prog_rows = (
        await db.execute(
            select(
                TrainingProgram.id,
                TrainingProgram.name,
                TrainingProgram.programName,
                TrainingProgram.isStatutory,
                TrainingProgram.category,
            ).where(TrainingProgram.id.in_({r.programId for r in rows}))
        )
    ).all() if rows else []
    programs_by_id = {
        pid: {"name": pname or name, "isStatutory": stat, "category": cat}
        for pid, name, pname, stat, cat in prog_rows
    }
    plant_names = dict(
        (
            await db.execute(
                select(Plant.id, Plant.name).where(
                    Plant.id.in_({r.plantId for r in rows if r.plantId})
                )
            )
        ).all()
    ) if rows else {}

    trainer_names = dict(
        (
            await db.execute(
                select(User.id, User.name).where(
                    User.id.in_({r.trainerId for r in rows if r.trainerId})
                )
            )
        ).all()
    ) if rows else {}

    # Registration / session tallies per schedule — the "12 / 20 enrolled" and
    # session-count columns. Two grouped counts rather than a query per row.
    schedule_ids = [r.id for r in rows]
    reg_counts = dict(
        (
            await db.execute(
                select(TrainingRegistration.scheduleId, func.count())
                .where(TrainingRegistration.scheduleId.in_(schedule_ids))
                .group_by(TrainingRegistration.scheduleId)
            )
        ).all()
    ) if schedule_ids else {}
    session_counts = dict(
        (
            await db.execute(
                select(TrainingSession.scheduleId, func.count())
                .where(TrainingSession.scheduleId.in_(schedule_ids))
                .group_by(TrainingSession.scheduleId)
            )
        ).all()
    ) if schedule_ids else {}

    items = []
    for r in rows:
        item = TrainingScheduleOut.model_validate(r).model_dump()
        prog = programs_by_id.get(r.programId) or {}
        item["programName"] = prog.get("name")
        item["programIsStatutory"] = prog.get("isStatutory", False)
        item["programCategory"] = prog.get("category")
        item["plantName"] = plant_names.get(r.plantId) if r.plantId else None
        item["trainerName"] = trainer_names.get(r.trainerId) if r.trainerId else None
        item["registrationCount"] = int(reg_counts.get(r.id, 0))
        item["sessionCount"] = int(session_counts.get(r.id, 0))
        items.append(item)
    return {"items": items, "total": len(items), "statusCounts": counts}


@router.get("/schedules/{schedule_id}")
async def get_schedule(
    schedule_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """A schedule with everything its delivery page runs on: the programme,
    plant, trainer and creator, the ordered sessions, and the nominee roster.

    Sessions and registrations also have their own sub-routes; nesting them
    here is what lets the page render in one request rather than four.
    """
    schedule = await db.get(TrainingSchedule, schedule_id)
    if schedule is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Schedule not found")

    out = TrainingScheduleOut.model_validate(schedule).model_dump()

    program = await db.get(TrainingProgram, schedule.programId)
    out["program"] = (
        TrainingProgramOut.model_validate(program).model_dump() if program else None
    )
    plant = await db.get(Plant, schedule.plantId) if schedule.plantId else None
    out["plant"] = {"id": plant.id, "name": plant.name} if plant else None

    people_ids = {pid for pid in (schedule.trainerId, schedule.createdById) if pid}
    people = dict(
        (await db.execute(select(User.id, User.name).where(User.id.in_(people_ids)))).all()
    ) if people_ids else {}
    out["trainer"] = (
        {"id": schedule.trainerId, "name": people.get(schedule.trainerId)}
        if schedule.trainerId
        else None
    )
    out["createdBy"] = (
        {"id": schedule.createdById, "name": people.get(schedule.createdById)}
        if schedule.createdById
        else None
    )

    sessions = (
        await db.execute(
            select(TrainingSession)
            .where(TrainingSession.scheduleId == schedule_id)
            .order_by(TrainingSession.sequence)
        )
    ).scalars().all()
    out["sessions"] = [TrainingSessionOut.model_validate(x).model_dump() for x in sessions]

    reg_rows = (
        await db.execute(
            select(TrainingRegistration, User.name, User.designation)
            .outerjoin(User, User.id == TrainingRegistration.userId)
            .where(TrainingRegistration.scheduleId == schedule_id)
            .order_by(TrainingRegistration.registeredAt)
        )
    ).all()
    out["registrations"] = [
        {
            **TrainingRegistrationOut.model_validate(r).model_dump(),
            "user": {"id": r.userId, "name": name, "designation": desig},
        }
        for r, name, desig in reg_rows
    ]
    return out


@router.get("/registrations/{registration_id}")
async def get_registration(
    registration_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """One registration with the learner, the schedule's programme (including
    its ordered question bank), and every assessment attempt — what the
    assessment screen needs to render or mark a paper.

    Readable by the learner themselves or a privileged assessor; anyone else
    gets a 403 rather than a filtered view, because there is nothing on this
    screen a third party has a reason to see.
    """
    reg = await db.get(TrainingRegistration, registration_id)
    if reg is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Registration not found")

    role_codes = await get_user_role_codes(db, user.id)
    is_assessor = any(
        r in {"TRAINER", "LD_MANAGER", "HSE_MANAGER", "ADMIN", "CORPORATE_HSE"}
        for r in role_codes
    )
    if reg.userId != user.id and not is_assessor:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Access denied")

    out = TrainingRegistrationOut.model_validate(reg).model_dump()

    learner = await db.get(User, reg.userId)
    out["user"] = {"id": learner.id, "name": learner.name} if learner else None

    schedule = await db.get(TrainingSchedule, reg.scheduleId)
    schedule_out = (
        TrainingScheduleOut.model_validate(schedule).model_dump() if schedule else None
    )
    if schedule_out is not None:
        program = await db.get(TrainingProgram, schedule.programId)
        program_out = (
            TrainingProgramOut.model_validate(program).model_dump() if program else None
        )
        if program_out is not None:
            questions = (
                await db.execute(
                    select(TrainingProgramQuestion)
                    .where(TrainingProgramQuestion.programId == program.id)
                    .order_by(TrainingProgramQuestion.sequence)
                )
            ).scalars().all()
            program_out["questions"] = [
                TrainingProgramQuestionOut.model_validate(q).model_dump() for q in questions
            ]
        schedule_out["program"] = program_out
    out["schedule"] = schedule_out

    attempts = (
        await db.execute(
            select(TrainingAssessment)
            .where(TrainingAssessment.registrationId == registration_id)
            .order_by(TrainingAssessment.attemptNumber)
        )
    ).scalars().all()
    out["assessments"] = [
        TrainingAssessmentOut.model_validate(a).model_dump() for a in attempts
    ]
    return out


@router.get("/schedules/{schedule_id}/sessions")
async def list_schedule_sessions(
    schedule_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    rows = (
        await db.execute(
            select(TrainingSession)
            .where(TrainingSession.scheduleId == schedule_id)
            .order_by(TrainingSession.sequence)
        )
    ).scalars().all()
    return {"items": [TrainingSessionOut.model_validate(r) for r in rows]}


@router.get("/schedules/{schedule_id}/registrations")
async def list_schedule_registrations(
    schedule_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    rows = (
        await db.execute(
            select(TrainingRegistration)
            .where(TrainingRegistration.scheduleId == schedule_id)
            .order_by(TrainingRegistration.registeredAt)
        )
    ).scalars().all()
    return {"items": [TrainingRegistrationOut.model_validate(r) for r in rows]}


@router.get("/sessions/{session_id}/attendance")
async def list_session_attendance(
    session_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    rows = (
        await db.execute(
            select(TrainingAttendance).where(TrainingAttendance.sessionId == session_id)
        )
    ).scalars().all()
    return {"items": [TrainingAttendanceOut.model_validate(r) for r in rows]}


# ─── Schedule create + update + lifecycle ─────────────────────────────


async def _next_schedule_number(db: AsyncSession, plant: Plant) -> str:
    year = datetime.now(timezone.utc).year
    count = (
        await db.execute(
            select(func.count())
            .select_from(TrainingSchedule)
            .where(TrainingSchedule.plantId == plant.id)
        )
    ).scalar_one()
    return f"TS-{year}-{plant.code}-{count + 1:04d}"


@router.post(
    "/schedules", response_model=TrainingScheduleOut, status_code=status.HTTP_201_CREATED
)
async def create_schedule(
    payload: TrainingScheduleCreate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> TrainingScheduleOut:
    # Permission: program scheduling is LD_MANAGER / HSE_MANAGER / ADMIN
    role_codes = await get_user_role_codes(db, user.id)
    if not any(
        r in {"LD_MANAGER", "HSE_MANAGER", "ADMIN"} for r in role_codes
    ):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "Only LD Manager / HSE Manager / Admin can schedule trainings.",
        )

    program = await db.get(TrainingProgram, payload.programId)
    if program is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid program")
    if program.approvalStatus != "APPROVED" or not program.isActive:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"Program is {program.approvalStatus} — only APPROVED active programs can be scheduled.",
        )

    plant = await db.get(Plant, payload.plantId)
    if plant is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid plant")

    if payload.endDate < payload.startDate:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "End date must be on or after start date."
        )

    if payload.trainerId:
        trainer = await db.get(User, payload.trainerId)
        if trainer is None:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid trainer")

    schedule_number = await _next_schedule_number(db, plant)

    schedule = TrainingSchedule(
        scheduleNumber=schedule_number,
        programId=payload.programId,
        plantId=payload.plantId,
        startDate=payload.startDate,
        endDate=payload.endDate,
        venue=payload.venue,
        language=payload.language,
        trainerId=payload.trainerId,
        isExternalTrainer=payload.isExternalTrainer,
        externalTrainerName=payload.externalTrainerName,
        externalTrainerOrg=payload.externalTrainerOrg,
        externalTrainerCert=payload.externalTrainerCert,
        maxParticipants=payload.maxParticipants,
        status="DRAFT",
        createdById=user.id,
    )
    db.add(schedule)
    await db.flush()

    # Sessions
    sessions = payload.sessions
    if not sessions:
        # Auto-generate one session covering the whole window
        sessions = [
            TrainingSessionInput(
                sequence=1,
                title=program.programName or program.name,
                startTime=payload.startDate,
                endTime=payload.endDate,
                trainerId=payload.trainerId,
            )
        ]
    for s in sessions:
        db.add(
            TrainingSession(
                scheduleId=schedule.id,
                sequence=s.sequence,
                title=s.title,
                startTime=s.startTime,
                endTime=s.endTime,
                trainerId=s.trainerId or payload.trainerId,
                topicsCovered=s.topicsCovered,
            )
        )

    # Initial nominees
    for uid in payload.initialNomineeUserIds:
        u = await db.get(User, uid)
        if u is None:
            continue
        db.add(
            TrainingRegistration(
                scheduleId=schedule.id,
                userId=uid,
                registrationType="MANAGER_NOMINATED",
                nominatedById=user.id,
                approvalStatus="APPROVED",
                status="REGISTERED",
                prerequisitesMet=True,
            )
        )

    await db.flush()
    await db.refresh(schedule)
    return TrainingScheduleOut.model_validate(schedule)


@router.patch("/schedules/{schedule_id}", response_model=TrainingScheduleOut)
async def update_schedule(
    schedule_id: str,
    payload: TrainingScheduleUpdate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> TrainingScheduleOut:
    role_codes = await get_user_role_codes(db, user.id)
    if not any(
        r in {"LD_MANAGER", "HSE_MANAGER", "ADMIN"} for r in role_codes
    ):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Only schedulers can edit.")
    schedule = await db.get(TrainingSchedule, schedule_id)
    if schedule is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Schedule not found")
    if schedule.status in {"IN_PROGRESS", "COMPLETED", "CANCELLED"}:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"Cannot edit a {schedule.status} schedule.",
        )
    for k, v in payload.model_dump(exclude_unset=True).items():
        setattr(schedule, k, v)
    await db.flush()
    await db.refresh(schedule)
    return TrainingScheduleOut.model_validate(schedule)


def _allowed_state_transition(current: str, target: str) -> bool:
    transitions = {
        "DRAFT": {"PUBLISHED", "CANCELLED"},
        "PUBLISHED": {"NOMINATIONS_OPEN", "CANCELLED"},
        "NOMINATIONS_OPEN": {"IN_PROGRESS", "CANCELLED"},
        "IN_PROGRESS": {"COMPLETED", "CANCELLED"},
        "COMPLETED": set(),
        "CANCELLED": set(),
        "POSTPONED": {"DRAFT"},
    }
    return target in transitions.get(current, set())


@router.post("/schedules/{schedule_id}/publish")
async def publish_schedule(
    schedule_id: str,
    payload: ScheduleStateAction,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    schedule = await db.get(TrainingSchedule, schedule_id)
    if schedule is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Schedule not found")
    if not _allowed_state_transition(schedule.status, "PUBLISHED"):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"Cannot publish from {schedule.status}.",
        )
    schedule.status = "PUBLISHED"
    schedule.publishedAt = datetime.now(timezone.utc)
    await db.flush()
    return {"ok": True, "status": "PUBLISHED"}


@router.post("/schedules/{schedule_id}/open-nominations")
async def open_nominations(
    schedule_id: str,
    payload: ScheduleStateAction,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    schedule = await db.get(TrainingSchedule, schedule_id)
    if schedule is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Schedule not found")
    if not _allowed_state_transition(schedule.status, "NOMINATIONS_OPEN"):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"Cannot open nominations from {schedule.status}.",
        )
    schedule.status = "NOMINATIONS_OPEN"
    await db.flush()
    return {"ok": True, "status": "NOMINATIONS_OPEN"}


@router.post("/schedules/{schedule_id}/start")
async def start_schedule(
    schedule_id: str,
    payload: ScheduleStateAction,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    schedule = await db.get(TrainingSchedule, schedule_id)
    if schedule is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Schedule not found")
    if not _allowed_state_transition(schedule.status, "IN_PROGRESS"):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"Cannot start from {schedule.status}.",
        )
    schedule.status = "IN_PROGRESS"
    await db.flush()
    return {"ok": True, "status": "IN_PROGRESS"}


@router.post("/schedules/{schedule_id}/complete")
async def complete_schedule(
    schedule_id: str,
    payload: ScheduleStateAction,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Mark schedule complete + roll up immediate-pass-rate stats.
    Per-registration certificate issuance happens in Commit 4."""
    schedule = await db.get(TrainingSchedule, schedule_id)
    if schedule is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Schedule not found")
    if not _allowed_state_transition(schedule.status, "COMPLETED"):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"Cannot complete from {schedule.status}.",
        )

    # Compute pass rate
    registrations = (
        await db.execute(
            select(TrainingRegistration).where(
                TrainingRegistration.scheduleId == schedule_id
            )
        )
    ).scalars().all()
    if registrations:
        attempted = sum(1 for r in registrations if r.passed is not None)
        passed = sum(1 for r in registrations if r.passed is True)
        if attempted:
            schedule.immediateAssessmentPassRate = round(passed / attempted * 100, 1)

    schedule.status = "COMPLETED"
    await db.flush()

    # Auto-issue certificates for every PASSED registration. Idempotent
    # so re-running complete (e.g. after data fix) doesn't create dupes.
    # Best-effort: a failure here does NOT roll back the completion —
    # an admin can re-run via /certificates/admin/refresh-states + the
    # per-registration issue endpoint.
    try:
        from app.services.training_certificates import issue_certificates_for_schedule

        issued = await issue_certificates_for_schedule(
            db, schedule_id=schedule_id, issuer_user_id=user.id
        )
        issued_count = len(issued)
    except Exception as e:  # noqa: BLE001
        import sys

        print(f"[training] cert auto-issue failed for {schedule_id}: {e}", file=sys.stderr)
        issued_count = 0

    return {
        "ok": True,
        "status": "COMPLETED",
        "passRate": schedule.immediateAssessmentPassRate,
        "certificatesIssued": issued_count,
    }


@router.post("/schedules/{schedule_id}/cancel")
async def cancel_schedule(
    schedule_id: str,
    payload: ScheduleCancel,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    schedule = await db.get(TrainingSchedule, schedule_id)
    if schedule is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Schedule not found")
    if not _allowed_state_transition(schedule.status, "CANCELLED"):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"Cannot cancel from {schedule.status}.",
        )
    schedule.status = "CANCELLED"
    schedule.cancelledAt = datetime.now(timezone.utc)
    schedule.cancellationReason = payload.reason
    # Cancel all pending registrations
    regs = (
        await db.execute(
            select(TrainingRegistration).where(
                TrainingRegistration.scheduleId == schedule_id
            )
        )
    ).scalars().all()
    for r in regs:
        if r.status in {"REGISTERED", "CONFIRMED"}:
            r.status = "CANCELLED"
    await db.flush()
    return {"ok": True, "status": "CANCELLED"}


# ─── Registrations ────────────────────────────────────────────────────


@router.post(
    "/registrations",
    response_model=TrainingRegistrationOut,
    status_code=status.HTTP_201_CREATED,
)
async def create_registration(
    payload: TrainingRegistrationCreate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> TrainingRegistrationOut:
    schedule = await db.get(TrainingSchedule, payload.scheduleId)
    if schedule is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid schedule")
    if schedule.status not in {"PUBLISHED", "NOMINATIONS_OPEN"}:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"Cannot register on a {schedule.status} schedule.",
        )

    # Self-nominations require schedule to be in NOMINATIONS_OPEN
    if (
        payload.registrationType == "SELF_NOMINATED"
        and schedule.status != "NOMINATIONS_OPEN"
    ):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Self-nominations require nominations to be open.",
        )

    target = await db.get(User, payload.userId)
    if target is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid user")

    # Self-nominate must match the auth user
    if payload.registrationType == "SELF_NOMINATED" and payload.userId != user.id:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Self-nomination must register the authenticated user.",
        )

    # Capacity check
    existing_count = (
        await db.execute(
            select(func.count())
            .select_from(TrainingRegistration)
            .where(TrainingRegistration.scheduleId == payload.scheduleId)
            .where(TrainingRegistration.status.in_(["REGISTERED", "CONFIRMED", "ATTENDED"]))
        )
    ).scalar_one()
    if existing_count >= schedule.maxParticipants:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"Schedule is full ({existing_count}/{schedule.maxParticipants}).",
        )

    # Already-registered check
    dupe = (
        await db.execute(
            select(TrainingRegistration)
            .where(TrainingRegistration.scheduleId == payload.scheduleId)
            .where(TrainingRegistration.userId == payload.userId)
        )
    ).scalar_one_or_none()
    if dupe is not None and dupe.status not in {"CANCELLED", "WITHDREW"}:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "User is already registered on this schedule."
        )

    # Self-nominations need approval; manager nominations auto-approve.
    needs_approval = payload.registrationType == "SELF_NOMINATED"

    reg = TrainingRegistration(
        scheduleId=payload.scheduleId,
        userId=payload.userId,
        registrationType=payload.registrationType,
        nominatedById=user.id if payload.registrationType != "SELF_NOMINATED" else None,
        triggerReason=payload.triggerReason,
        triggerSourceId=payload.triggerSourceId,
        prerequisitesMet=True,  # Commit 5 wires the actual check
        approvalStatus="PENDING" if needs_approval else "APPROVED",
        approvedById=None if needs_approval else user.id,
        approvedAt=None if needs_approval else datetime.now(timezone.utc),
        status="REGISTERED" if not needs_approval else "REGISTERED",
    )
    db.add(reg)
    await db.flush()
    await db.refresh(reg)
    return TrainingRegistrationOut.model_validate(reg)


@router.post("/registrations/{registration_id}/decide")
async def decide_registration(
    registration_id: str,
    payload: TrainingRegistrationDecision,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Manager approves / rejects a self-nomination."""
    role_codes = await get_user_role_codes(db, user.id)
    if not any(
        r in {"SUPERVISOR", "DEPARTMENT_HEAD", "LD_MANAGER", "HSE_MANAGER", "ADMIN"}
        for r in role_codes
    ):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "Only Supervisor / Dept Head / LD / HSE / Admin can decide registrations.",
        )
    reg = await db.get(TrainingRegistration, registration_id)
    if reg is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Registration not found")
    if reg.approvalStatus != "PENDING":
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"Registration is already {reg.approvalStatus}.",
        )
    if payload.decision == "APPROVED":
        reg.approvalStatus = "APPROVED"
        reg.approvedById = user.id
        reg.approvedAt = datetime.now(timezone.utc)
    else:
        reg.approvalStatus = "REJECTED"
        reg.status = "CANCELLED"
    await db.flush()
    return {"ok": True, "approvalStatus": reg.approvalStatus}


@router.post("/registrations/{registration_id}/withdraw")
async def withdraw_registration(
    registration_id: str,
    payload: TrainingRegistrationWithdraw,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    reg = await db.get(TrainingRegistration, registration_id)
    if reg is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Registration not found")
    # Only the registered user OR a scheduler can withdraw
    role_codes = await get_user_role_codes(db, user.id)
    is_priv = any(
        r in {"LD_MANAGER", "HSE_MANAGER", "ADMIN"} for r in role_codes
    )
    if reg.userId != user.id and not is_priv:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, "Only the registered user or LD/HSE/Admin can withdraw."
        )
    if reg.status in {"COMPLETED", "FAILED", "WITHDREW", "CANCELLED"}:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, f"Cannot withdraw a {reg.status} registration."
        )
    reg.status = "WITHDREW"
    await db.flush()
    return {"ok": True}


# ─── Attendance ───────────────────────────────────────────────────────


@router.post("/attendance/bulk")
async def capture_attendance_bulk(
    payload: TrainingAttendanceBulk,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Trainer captures the whole roster's attendance for one session
    in a single round-trip. Idempotent — upserts existing rows."""
    session_obj = await db.get(TrainingSession, payload.sessionId)
    if session_obj is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Session not found")

    # Permission: the assigned trainer OR LD/HSE/Admin
    role_codes = await get_user_role_codes(db, user.id)
    is_priv = any(
        r in {"LD_MANAGER", "HSE_MANAGER", "ADMIN", "TRAINER"}
        for r in role_codes
    )
    if session_obj.trainerId != user.id and not is_priv:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "Only the session trainer or LD/HSE/Admin can capture attendance.",
        )

    saved = 0
    for row in payload.rows:
        existing = (
            await db.execute(
                select(TrainingAttendance)
                .where(TrainingAttendance.sessionId == payload.sessionId)
                .where(TrainingAttendance.registrationId == row.registrationId)
            )
        ).scalar_one_or_none()
        if existing is None:
            db.add(
                TrainingAttendance(
                    sessionId=payload.sessionId,
                    registrationId=row.registrationId,
                    status=row.status,
                    arrivalTime=row.arrivalTime,
                    departureTime=row.departureTime,
                    durationMinutes=(
                        int((row.departureTime - row.arrivalTime).total_seconds() / 60)
                        if row.arrivalTime and row.departureTime
                        else None
                    ),
                    signatureCaptured=row.signatureCaptured,
                    signatureUrl=row.signatureUrl,
                    qrScanned=row.qrScanned,
                    qrScannedAt=datetime.now(timezone.utc) if row.qrScanned else None,
                    geoLocation=row.geoLocation,
                    attendancePhotos=row.attendancePhotos,
                    notes=row.notes,
                    capturedById=user.id,
                )
            )
        else:
            existing.status = row.status
            existing.arrivalTime = row.arrivalTime
            existing.departureTime = row.departureTime
            if row.arrivalTime and row.departureTime:
                existing.durationMinutes = int(
                    (row.departureTime - row.arrivalTime).total_seconds() / 60
                )
            existing.signatureCaptured = row.signatureCaptured
            existing.signatureUrl = row.signatureUrl
            existing.qrScanned = row.qrScanned
            existing.qrScannedAt = datetime.now(timezone.utc) if row.qrScanned else None
            existing.geoLocation = row.geoLocation
            existing.attendancePhotos = row.attendancePhotos
            existing.notes = row.notes
            existing.capturedById = user.id
        saved += 1

    # Mark session conducted
    session_obj.conductedAt = datetime.now(timezone.utc)
    if session_obj.startTime and session_obj.endTime:
        session_obj.durationMinutesActual = int(
            (session_obj.endTime - session_obj.startTime).total_seconds() / 60
        )

    # Recompute attendance percent on each registration
    for row in payload.rows:
        reg = await db.get(TrainingRegistration, row.registrationId)
        if reg is None:
            continue
        all_sessions = (
            await db.execute(
                select(TrainingSession).where(
                    TrainingSession.scheduleId == reg.scheduleId
                )
            )
        ).scalars().all()
        all_attendance = (
            await db.execute(
                select(TrainingAttendance).where(
                    TrainingAttendance.registrationId == reg.id
                )
            )
        ).scalars().all()
        present_count = sum(
            1 for a in all_attendance if a.status in {"PRESENT", "LATE"}
        )
        if all_sessions:
            reg.attendancePercent = round(present_count / len(all_sessions) * 100, 1)
            # Mark ATTENDED if present at any session
            if present_count > 0 and reg.status == "REGISTERED":
                reg.status = "ATTENDED"

    await db.flush()
    return {"ok": True, "saved": saved}


# ─── Assessment ───────────────────────────────────────────────────────


@router.post(
    "/assessments/start",
    response_model=TrainingAssessmentOut,
    status_code=status.HTTP_201_CREATED,
)
async def start_assessment(
    registration_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> TrainingAssessmentOut:
    """Open an attempt. Online MCQ: learner starts their own. Practical /
    oral: assessor starts on behalf."""
    reg = await db.get(TrainingRegistration, registration_id)
    if reg is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Registration not found")

    program = await db.get(TrainingProgram, (await db.get(TrainingSchedule, reg.scheduleId)).programId)  # type: ignore
    if program is None or not program.hasAssessment:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Program does not have an assessment configured.",
        )

    # Self-assessment by learner OR assessor proxy
    if reg.userId != user.id:
        role_codes = await get_user_role_codes(db, user.id)
        if not any(
            r in {"TRAINER", "LD_MANAGER", "HSE_MANAGER", "ADMIN"}
            for r in role_codes
        ):
            raise HTTPException(
                status.HTTP_403_FORBIDDEN, "Only the learner or trainer/assessor can start."
            )

    if reg.assessmentAttempts >= program.attemptsAllowed:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"All {program.attemptsAllowed} attempts already used.",
        )

    # Compute total marks from question bank
    questions = (
        await db.execute(
            select(TrainingProgramQuestion).where(
                TrainingProgramQuestion.programId == program.id
            )
        )
    ).scalars().all()
    total_marks = sum(q.marks for q in questions) if questions else 100

    attempt_number = reg.assessmentAttempts + 1
    assessment = TrainingAssessment(
        registrationId=registration_id,
        attemptNumber=attempt_number,
        startedAt=datetime.now(timezone.utc),
        totalMarks=total_marks,
        passed=False,
        assessedById=user.id,
        retakeAllowed=attempt_number < program.attemptsAllowed,
    )
    db.add(assessment)
    await db.flush()
    await db.refresh(assessment)
    return TrainingAssessmentOut.model_validate(assessment)


@router.post("/assessments/{assessment_id}/submit", response_model=TrainingAssessmentOut)
async def submit_assessment(
    assessment_id: str,
    payload: TrainingAssessmentSubmit,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> TrainingAssessmentOut:
    """Submit an MCQ attempt — server grades against question bank.
    For practical / oral, the assessor passes practicalScores +
    assessorNarrative; we trust their grading."""
    assessment = await db.get(TrainingAssessment, assessment_id)
    if assessment is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Assessment not found")
    if assessment.submittedAt is not None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Already submitted.")

    reg = await db.get(TrainingRegistration, assessment.registrationId)
    if reg is None:
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, "Registration missing")
    schedule = await db.get(TrainingSchedule, reg.scheduleId)
    if schedule is None:
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, "Schedule missing")
    program = await db.get(TrainingProgram, schedule.programId)
    if program is None:
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, "Program missing")

    now = datetime.now(timezone.utc)
    duration_min = int((now - assessment.startedAt).total_seconds() / 60)

    failure_reasons: list[str] = []

    if payload.responses:
        # MCQ grading
        questions_map = {
            q.id: q
            for q in (
                await db.execute(
                    select(TrainingProgramQuestion).where(
                        TrainingProgramQuestion.programId == program.id
                    )
                )
            )
            .scalars()
            .all()
        }

        total_score = 0.0
        critical_wrong = False
        for r in payload.responses:
            q = questions_map.get(r.questionId)
            if q is None:
                continue
            is_correct = False
            if q.questionType in ("MCQ_SINGLE", "MCQ_MULTI"):
                opts = q.options or []
                correct_indices = {
                    i for i, o in enumerate(opts) if isinstance(o, dict) and o.get("isCorrect")
                }
                selected = set(r.selectedOptions or [])
                is_correct = selected == correct_indices
            elif q.questionType == "TRUE_FALSE":
                is_correct = (r.textAnswer or "").strip().lower() == (
                    q.correctAnswer or ""
                ).strip().lower()
            elif q.questionType == "SHORT_ANSWER":
                is_correct = (r.textAnswer or "").strip().lower() == (
                    q.correctAnswer or ""
                ).strip().lower()
            elif q.questionType == "NUMERIC":
                try:
                    is_correct = float(r.numericAnswer or 0) == float(q.correctAnswer or 0)
                except (TypeError, ValueError):
                    is_correct = False

            marks = q.marks if is_correct else 0
            if is_correct:
                total_score += marks
            elif q.isCritical:
                critical_wrong = True

            db.add(
                TrainingAssessmentResponse(
                    assessmentId=assessment.id,
                    questionId=q.id,
                    selectedOptions=r.selectedOptions,
                    textAnswer=r.textAnswer,
                    numericAnswer=r.numericAnswer,
                    isCorrect=is_correct,
                    marksAwarded=marks,
                )
            )

        assessment.totalScore = total_score
        if assessment.totalMarks > 0:
            assessment.scorePercent = round(total_score / assessment.totalMarks * 100, 1)

        passing_pct = program.passingScorePercent or program.passingScore
        score_ok = (assessment.scorePercent or 0) >= passing_pct
        if critical_wrong:
            failure_reasons.append("critical_question_wrong")
        if not score_ok:
            failure_reasons.append("below_threshold")
        assessment.passed = score_ok and not critical_wrong
    else:
        # Practical / oral / observation — trust assessor scoring
        assessment.practicalScores = payload.practicalScores
        assessment.practicalNotes = payload.practicalNotes
        assessment.assessorNarrative = payload.assessorNarrative
        if payload.practicalScores:
            total = sum(float(v) for v in payload.practicalScores.values())
            assessment.totalScore = total
            assessment.scorePercent = round(total / assessment.totalMarks * 100, 1)
            passing_pct = program.passingScorePercent or program.passingScore
            assessment.passed = (assessment.scorePercent or 0) >= passing_pct
            if not assessment.passed:
                failure_reasons.append("below_threshold")

    assessment.submittedAt = now
    assessment.durationMinutes = duration_min
    assessment.failureReasons = failure_reasons or None

    # Update registration outcome
    reg.assessmentAttempts = assessment.attemptNumber
    reg.assessmentScore = assessment.scorePercent
    reg.passed = assessment.passed
    if assessment.passed:
        reg.status = "COMPLETED"
    elif assessment.attemptNumber >= program.attemptsAllowed:
        reg.status = "FAILED"
        assessment.retakeAllowed = False
    else:
        # Allow retake
        assessment.retakeAllowed = True
        assessment.retakeAfterDate = now

    await db.flush()

    # Auto-issue certificate if learner passed. Idempotent — issuance
    # checks for an existing cert on the registration first. Best-effort
    # so a flaky cert path doesn't roll back the assessment outcome.
    if assessment.passed:
        try:
            from app.services.training_certificates import issue_certificate_if_eligible

            await issue_certificate_if_eligible(
                db, registration_id=reg.id, issuer_user_id=user.id
            )
        except Exception as e:  # noqa: BLE001
            import sys

            print(
                f"[training] cert auto-issue failed for reg {reg.id}: {e}",
                file=sys.stderr,
            )

    await db.refresh(assessment)
    return TrainingAssessmentOut.model_validate(assessment)


@router.get("/assessments/{assessment_id}", response_model=TrainingAssessmentOut)
async def get_assessment(
    assessment_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> TrainingAssessmentOut:
    a = await db.get(TrainingAssessment, assessment_id)
    if a is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Assessment not found")
    return TrainingAssessmentOut.model_validate(a)


# ═══════════════════════════════════════════════════════════════════════
#  TRAINING CERTIFICATE LIFECYCLE
# ═══════════════════════════════════════════════════════════════════════


from app.models.training import TrainingCertificate  # noqa: E402
from app.schemas.training import (  # noqa: E402
    CertificatePublicVerifyOut,
    CertificateRevokeRequest,
    EffectivenessReviewRequest,
    TrainingCertificateOut,
)
from app.services.training_certificates import (  # noqa: E402
    issue_certificate_if_eligible,
    issue_certificates_for_schedule,
    record_effectiveness_review,
    refresh_certificate_states,
    revoke_certificate,
)


# ─── Certificate list / get ──────────────────────────────────────────


@router.get("/certificates")
async def list_certificates(
    user_id: str | None = None,
    program_id: str | None = None,
    status_filter: str | None = None,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """List certificates. Workers see only their own; HSE/LD/Admin see
    plant-wide. Accepts user_id / program_id / status_filter query args."""
    role_codes = await get_user_role_codes(db, user.id)
    is_priv = any(
        r in {"HSE_MANAGER", "LD_MANAGER", "ADMIN", "CORPORATE_HSE", "PLANT_HEAD"}
        for r in role_codes
    )
    stmt = select(TrainingCertificate)
    if not is_priv:
        # Force OWN scope
        stmt = stmt.where(TrainingCertificate.userId == user.id)
    elif user_id:
        stmt = stmt.where(TrainingCertificate.userId == user_id)
    if program_id:
        stmt = stmt.where(TrainingCertificate.programId == program_id)
    if status_filter:
        stmt = stmt.where(TrainingCertificate.status == status_filter)

    counts = await status_counts(db, stmt, TrainingCertificate.status)

    rows = (
        # Newest-created first — platform-wide register convention.
        await db.execute(
            stmt.order_by(TrainingCertificate.createdAt.desc(), TrainingCertificate.id.desc()).limit(500)
        )
    ).scalars().all()

    prog_rows = (
        await db.execute(
            select(
                TrainingProgram.id,
                TrainingProgram.name,
                TrainingProgram.programName,
                TrainingProgram.code,
                TrainingProgram.programCode,
                TrainingProgram.isStatutory,
                TrainingProgram.statutoryReference,
            ).where(TrainingProgram.id.in_({c.programId for c in rows}))
        )
    ).all() if rows else []
    programs_by_id = {
        pid: {
            "name": pname or name,
            "code": pcode or code,
            "isStatutory": stat,
            "statutoryReference": ref,
        }
        for pid, name, pname, code, pcode, stat, ref in prog_rows
    }
    holder_rows = (
        await db.execute(
            select(User.id, User.name, User.designation).where(
                User.id.in_({c.userId for c in rows})
            )
        )
    ).all() if rows else []
    holders_by_id = {uid: {"name": n, "designation": d} for uid, n, d in holder_rows}

    items = []
    for c in rows:
        item = TrainingCertificateOut.model_validate(c).model_dump()
        prog = programs_by_id.get(c.programId) or {}
        holder = holders_by_id.get(c.userId) or {}
        item["programName"] = prog.get("name")
        item["programCode"] = prog.get("code")
        item["programIsStatutory"] = prog.get("isStatutory", False)
        item["statutoryReference"] = prog.get("statutoryReference")
        item["holderName"] = holder.get("name")
        item["holderDesignation"] = holder.get("designation")
        items.append(item)
    return {"items": items, "total": len(items), "statusCounts": counts}


@router.get("/certificates/me")
async def list_my_certificates(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Worker self-service — own certificates only.

    Ordered by status then expiry so anything lapsed or close to expiring
    surfaces first; that is the whole point of the screen.
    """
    rows = (
        await db.execute(
            select(TrainingCertificate)
            .where(TrainingCertificate.userId == user.id)
            .order_by(TrainingCertificate.status.asc(), TrainingCertificate.validTo.asc())
        )
    ).scalars().all()

    prog_rows = (
        await db.execute(
            select(
                TrainingProgram.id,
                TrainingProgram.name,
                TrainingProgram.programName,
                TrainingProgram.code,
                TrainingProgram.programCode,
                TrainingProgram.isStatutory,
                TrainingProgram.statutoryReference,
            ).where(TrainingProgram.id.in_({c.programId for c in rows}))
        )
    ).all() if rows else []
    programs_by_id = {
        pid: {
            "name": pname or name,
            "code": pcode or code,
            "isStatutory": stat,
            "statutoryReference": ref,
        }
        for pid, name, pname, code, pcode, stat, ref in prog_rows
    }

    items = []
    for c in rows:
        item = TrainingCertificateOut.model_validate(c).model_dump()
        prog = programs_by_id.get(c.programId) or {}
        item["programName"] = prog.get("name")
        item["programCode"] = prog.get("code")
        item["programIsStatutory"] = prog.get("isStatutory", False)
        item["statutoryReference"] = prog.get("statutoryReference")
        items.append(item)
    return {"items": items}


@router.get("/certificates/{certificate_id}")
async def get_certificate(
    certificate_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    cert = await db.get(TrainingCertificate, certificate_id)
    if cert is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Certificate not found")
    # Workers can only read their own
    role_codes = await get_user_role_codes(db, user.id)
    is_priv = any(
        r in {"HSE_MANAGER", "LD_MANAGER", "ADMIN", "CORPORATE_HSE", "PLANT_HEAD", "SAFETY_OFFICER", "SUPERVISOR"}
        for r in role_codes
    )
    if cert.userId != user.id and not is_priv:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Access denied")

    # Nest what the certificate page prints: the programme, the holder, and the
    # people behind each state change (issued / revoked / effectiveness review).
    out = TrainingCertificateOut.model_validate(cert).model_dump()

    program = await db.get(TrainingProgram, cert.programId)
    out["program"] = (
        TrainingProgramOut.model_validate(program).model_dump() if program else None
    )

    people_ids = {
        pid
        for pid in (
            cert.userId,
            cert.issuedById,
            cert.revokedById,
            cert.effectivenessReviewedById,
        )
        if pid
    }
    people = {
        uid: {"id": uid, "name": name, "designation": desig, "plantId": plant_id}
        for uid, name, desig, plant_id in (
            await db.execute(
                select(User.id, User.name, User.designation, User.plantId).where(
                    User.id.in_(people_ids)
                )
            )
        ).all()
    }
    out["user"] = people.get(cert.userId)
    out["issuedBy"] = people.get(cert.issuedById) if cert.issuedById else None
    out["revokedBy"] = people.get(cert.revokedById) if cert.revokedById else None
    out["effectivenessReviewedBy"] = (
        people.get(cert.effectivenessReviewedById)
        if cert.effectivenessReviewedById
        else None
    )

    # The originating schedule, when the certificate came out of a delivery
    # rather than being recorded directly.
    out["registration"] = None
    if cert.registrationId:
        reg = await db.get(TrainingRegistration, cert.registrationId)
        if reg is not None:
            schedule = await db.get(TrainingSchedule, reg.scheduleId)
            out["registration"] = {
                "id": reg.id,
                "schedule": (
                    {"id": schedule.id, "scheduleNumber": schedule.scheduleNumber}
                    if schedule
                    else None
                ),
            }
    return out


# ─── Public verification — NO AUTH ───────────────────────────────────
#
# This endpoint is intentionally outside the auth middleware. It only
# exposes safe-to-share fields so an external auditor / inspector can
# scan the QR and verify the certificate is real + valid.


@router.get(
    "/certificates/verify/{certificate_number}",
    response_model=CertificatePublicVerifyOut,
)
async def verify_certificate_public(
    certificate_number: str,
    db: AsyncSession = Depends(get_db),
) -> CertificatePublicVerifyOut:
    """PUBLIC — accessed by anyone scanning the QR or entering the cert
    number. Returns ONLY the fields safe to expose externally; never
    returns scores, internal IDs, or revocation details."""
    cert = (
        await db.execute(
            select(TrainingCertificate).where(
                TrainingCertificate.certificateNumber == certificate_number
            )
        )
    ).scalar_one_or_none()
    if cert is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Certificate not found")

    program = await db.get(TrainingProgram, cert.programId)
    holder = await db.get(User, cert.userId)
    plant_name: str | None = None
    if holder and getattr(holder, "plantId", None):
        plant = await db.get(Plant, holder.plantId)
        plant_name = plant.name if plant else None

    return CertificatePublicVerifyOut(
        certificateNumber=cert.certificateNumber,
        programName=(program.programName or program.name) if program else "—",
        holderName=holder.name if holder else "—",
        plantName=plant_name,
        issuedAt=cert.issuedAt,
        validFrom=cert.validFrom,
        validTo=cert.validTo,
        status=cert.status,
        isStatutory=program.isStatutory if program else False,
        statutoryReference=program.statutoryReference if program else None,
        revoked=cert.status == "REVOKED",
        revocationReason=cert.revocationReason if cert.status == "REVOKED" else None,
    )


# ─── Admin actions ───────────────────────────────────────────────────


@router.post("/certificates/{certificate_id}/revoke", response_model=TrainingCertificateOut)
async def revoke_certificate_endpoint(
    certificate_id: str,
    payload: CertificateRevokeRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> TrainingCertificateOut:
    """HSE Manager / LD Manager / Admin — revoke an active certificate."""
    role_codes = await get_user_role_codes(db, user.id)
    if not any(
        r in {"HSE_MANAGER", "LD_MANAGER", "ADMIN", "PLANT_HEAD"}
        for r in role_codes
    ):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "Only HSE Manager / LD Manager / Plant Head / Admin can revoke certificates.",
        )
    try:
        cert = await revoke_certificate(
            db,
            certificate_id=certificate_id,
            revoker_user_id=user.id,
            reason=payload.reason,
            details=payload.details,
        )
    except ValueError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e)) from e
    return TrainingCertificateOut.model_validate(cert)


@router.post(
    "/certificates/{certificate_id}/effectiveness-review",
    response_model=TrainingCertificateOut,
)
async def review_effectiveness_endpoint(
    certificate_id: str,
    payload: EffectivenessReviewRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> TrainingCertificateOut:
    role_codes = await get_user_role_codes(db, user.id)
    if not any(
        r in {"HSE_MANAGER", "LD_MANAGER", "ADMIN", "SUPERVISOR"}
        for r in role_codes
    ):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "Only HSE / LD / Supervisor / Admin can record effectiveness reviews.",
        )
    try:
        cert = await record_effectiveness_review(
            db,
            certificate_id=certificate_id,
            reviewer_user_id=user.id,
            rating=payload.rating,
            notes=payload.notes,
        )
    except ValueError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e)) from e
    return TrainingCertificateOut.model_validate(cert)


# ─── State refresh (admin job) ───────────────────────────────────────


@router.post("/certificates/admin/refresh-states")
async def refresh_states_endpoint(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Admin-triggered scan of all certificates to update state machine.
    In production this should run as a nightly cron; for now an admin
    button on the certificate dashboard fires it on demand."""
    role_codes = await get_user_role_codes(db, user.id)
    if not any(
        r in {"HSE_MANAGER", "LD_MANAGER", "ADMIN", "CORPORATE_HSE"}
        for r in role_codes
    ):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Admin / HSE / LD only.")
    transitions = await refresh_certificate_states(db)
    return {"ok": True, "transitions": transitions}


# ═══════════════════════════════════════════════════════════════════════
#  COMPETENCY CHECK (cross-module pre-flight)
# ═══════════════════════════════════════════════════════════════════════
#
# Endpoint surface for the canonical competency.py service. Called by:
#   • PTW frontend — pre-check a candidate crew member's competency
#                   before submitting the form
#   • FLRA frontend — show "you are eligible / not eligible" banner
#                    on the sign-off page
#   • Future role-assignment UI — gate role grants per the matrix
#   • Future contractor-onboarding UI — gate gate-pass issuance
#
# The actual server-side enforcement happens inside the PTW
# create_permit and FLRA sign endpoints — this endpoint is for UI
# pre-flight only, not the enforcement boundary.


@router.get("/competency/check")
async def check_competency(
    user_id: str,
    permit_type: str | None = None,
    role_code: str | None = None,
    contractor_onboarding: bool = False,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Returns the canonical competency check for a user against EITHER
    a permit type OR a role OR contractor onboarding. Exactly one of
    those three filters should be supplied; if none, returns 400."""
    from app.services.competency import (
        check_competency_for_contractor_onboarding,
        check_competency_for_permit_type,
        check_competency_for_role,
    )

    if sum(bool(x) for x in [permit_type, role_code, contractor_onboarding]) != 1:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Pass exactly one of permit_type, role_code, or contractor_onboarding=true.",
        )

    target = await db.get(User, user_id)
    if target is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "User not found")

    if permit_type:
        result = await check_competency_for_permit_type(db, user_id, permit_type)
        context = {"permitType": permit_type}
    elif role_code:
        result = await check_competency_for_role(db, user_id, role_code)
        context = {"roleCode": role_code}
    else:
        result = await check_competency_for_contractor_onboarding(db, user_id)
        context = {"contractorOnboarding": True}

    return {
        "ok": result.ok,
        "userId": user_id,
        "userName": target.name,
        "context": context,
        "blockers": [
            {"programCode": b.programCode, "programName": b.programName, "code": b.code, "message": b.message}
            for b in result.blockers
        ],
        "warnings": [
            {
                "programCode": w.programCode,
                "programName": w.programName,
                "code": w.code,
                "message": w.message,
                "daysUntilExpiry": w.daysUntilExpiry,
            }
            for w in result.warnings
        ],
        "satisfied": result.satisfied,
    }


# ═══════════════════════════════════════════════════════════════════════
#  TRAINING RECORD (legacy — kept for back-compat with PTW/FLRA gates)
# ═══════════════════════════════════════════════════════════════════════


@router.get("")
async def list_records(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Training records, scoped to what the caller may read.

    TrainingRecord has no plantId of its own — it hangs off the employee — so
    the plant scope is applied through the employee's plant. This endpoint
    previously returned every record to any authenticated caller, with no
    permission check and no scope at all.
    """
    check = await can(db, user.id, "TRAINING.READ", PermissionContext())
    if not check.allowed:
        raise HTTPException(status.HTTP_403_FORBIDDEN, check.reason or "Access denied")

    stmt = select(TrainingRecord)
    plants = await get_accessible_plants(db, user.id)
    if plants is not None:
        if not plants:
            return {"items": [], "total": 0}
        stmt = stmt.where(
            TrainingRecord.employeeId.in_(
                select(User.id).where(User.plantId.in_(plants))
            )
        )
    if check.matched_scope == "OWN_RECORDS":
        # A worker sees the training they took, and anything they delivered.
        stmt = stmt.where(
            or_(TrainingRecord.employeeId == user.id, TrainingRecord.trainerId == user.id)
        )

    # 2000, not 200: the register derives compliance from the LATEST record per
    # (employee, programme) pair, so truncating the set would silently change
    # the percentage rather than just shortening the list.
    rows = (
        # Newest-created first — platform-wide register convention.
        await db.execute(
            stmt.order_by(TrainingRecord.createdAt.desc(), TrainingRecord.id.desc()).limit(2000)
        )
    ).scalars().all()

    employees = {
        uid: {"id": uid, "name": name, "department": dept}
        for uid, name, dept in (
            await db.execute(
                select(User.id, User.name, User.department).where(
                    User.id.in_({r.employeeId for r in rows})
                )
            )
        ).all()
    } if rows else {}
    programs = {
        p.id: TrainingProgramOut.model_validate(p).model_dump()
        for p in (
            await db.execute(
                select(TrainingProgram).where(
                    TrainingProgram.id.in_({r.programId for r in rows})
                )
            )
        ).scalars().all()
    } if rows else {}

    items = []
    for r in rows:
        item = TrainingRecordOut.model_validate(r).model_dump()
        item["employee"] = employees.get(r.employeeId)
        item["program"] = programs.get(r.programId)
        items.append(item)
    return {"items": items, "total": len(items)}


@router.post("", response_model=TrainingRecordOut, status_code=status.HTTP_201_CREATED)
async def create_record(
    payload: TrainingCreate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> TrainingRecordOut:
    employee = await db.get(User, payload.employeeId)
    if employee is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid employee")
    await require_permission_with_context("TRAINING.CREATE", user, db, plant_id=employee.plantId)

    program = await db.get(TrainingProgram, payload.programId)
    if program is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid program")

    if not (payload.trainerId or payload.trainerName):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Trainer is required.")
    if payload.trainerId:
        trainer = await db.get(User, payload.trainerId)
        if trainer is None:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid trainer")

    if payload.date.timestamp() > datetime.now(timezone.utc).timestamp() + 300:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Training date cannot be in the future.")
    if payload.score is not None and not (0 <= payload.score <= 100):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Score must be 0-100.")

    valid_until = payload.date + timedelta(days=program.validityMonths * 30)

    record = TrainingRecord(
        employeeId=payload.employeeId,
        programId=payload.programId,
        trainerId=payload.trainerId,
        trainerName=payload.trainerName,
        date=payload.date,
        durationHours=payload.durationHours,
        score=payload.score,
        passed=payload.passed,
        validUntil=valid_until,
        certificateUrl=payload.certificateUrl,
        remarks=payload.remarks,
    )
    db.add(record)
    await db.flush()
    await db.refresh(record)
    return TrainingRecordOut.model_validate(record)


@router.delete("/{record_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_training_record(
    record_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    """Hard-delete a training record. Per the RBAC matrix:
    - LD_MANAGER can delete OWN_RECORDS (own draft records)
    - HSE_MANAGER can delete OWN_PLANT
    - ADMIN can delete ALL_PLANTS
    The permission service enforces the scope. TrainingRecord has no
    children to cascade — clean delete."""
    record = await db.get(TrainingRecord, record_id)
    if record is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Training record not found")
    record_dict = {"employeeId": record.employeeId, "trainerId": record.trainerId}
    result = await can(
        db,
        user.id,
        "TRAINING.DELETE",
        PermissionContext(record_id=record.id, plant_id=None, record=record_dict),
    )
    if not result.allowed:
        raise HTTPException(status.HTTP_403_FORBIDDEN, result.reason or "Access denied")
    await db.delete(record)
    await db.flush()


# Declared LAST so every specific GET above (/programs, /schedules,
# /certificates, /analytics, /competency/check) matches first — FastAPI
# resolves in declaration order, and this path would otherwise swallow them.
@router.get("/{record_id}")
async def get_record(
    record_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """One training record with the employee, programme and trainer nested —
    what the record's detail page prints."""
    record = await db.get(TrainingRecord, record_id)
    if record is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Training record not found")

    employee = await db.get(User, record.employeeId)
    check = await can(
        db,
        user.id,
        "TRAINING.READ",
        PermissionContext(
            record_id=record.id,
            plant_id=employee.plantId if employee else None,
            record={"employeeId": record.employeeId, "trainerId": record.trainerId},
        ),
    )
    if not check.allowed:
        raise HTTPException(status.HTTP_403_FORBIDDEN, check.reason or "Access denied")

    out = TrainingRecordOut.model_validate(record).model_dump()
    out["employee"] = (
        {
            "id": employee.id,
            "name": employee.name,
            "designation": employee.designation,
            "department": employee.department,
        }
        if employee
        else None
    )
    program = await db.get(TrainingProgram, record.programId)
    out["program"] = (
        TrainingProgramOut.model_validate(program).model_dump() if program else None
    )
    trainer = await db.get(User, record.trainerId) if record.trainerId else None
    out["trainer"] = (
        {"id": trainer.id, "name": trainer.name, "designation": trainer.designation}
        if trainer
        else None
    )
    return out
