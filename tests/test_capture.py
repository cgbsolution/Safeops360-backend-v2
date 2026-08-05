"""Guided Field Capture — offline unit tests (no DB), house style of test_rca.py.

Covers the triage 5x5 banding, conversion maps, anonymous masking/ownership,
and the synthesised-description contract (>=10 chars for module conversion).
"""

from __future__ import annotations

from types import SimpleNamespace

from app.services import capture as svc


# ── 5x5 banding ───────────────────────────────────────────────────────────────
def test_risk_level_banding_matches_std_5x5():
    assert svc.risk_level_for(1) == "LOW"
    assert svc.risk_level_for(4) == "LOW"
    assert svc.risk_level_for(5) == "MODERATE"
    assert svc.risk_level_for(9) == "MODERATE"
    assert svc.risk_level_for(10) == "HIGH"
    assert svc.risk_level_for(16) == "HIGH"
    assert svc.risk_level_for(17) == "CRITICAL"
    assert svc.risk_level_for(25) == "CRITICAL"


def test_severity_maps_are_total():
    # every self-severity and every risk band must map to a module severity
    for s in ("low", "medium", "high"):
        assert svc.SELF_SEVERITY_TO_MODULE[s] in ("LOW", "MEDIUM", "HIGH")
    for band in ("LOW", "MODERATE", "HIGH", "CRITICAL"):
        assert svc.RISK_LEVEL_TO_MODULE[band] in ("LOW", "MEDIUM", "HIGH", "CRITICAL")


def test_hazard_map_targets_are_valid_observation_categories():
    from app.models.observation import ObservationCategory

    valid = {c.value for c in ObservationCategory}
    for target in svc.HAZARD_TO_OBS_CATEGORY.values():
        assert target in valid, f"{target} is not an ObservationCategory"


# ── anonymity ─────────────────────────────────────────────────────────────────
def test_anon_hash_is_deterministic_and_user_specific():
    a1 = svc.anon_hash("user-a")
    assert a1 == svc.anon_hash("user-a")
    assert a1 != svc.anon_hash("user-b")
    assert len(a1) == 64  # sha256 hex


def _sub(**overrides):
    base = dict(
        reporterId=None,
        isAnonymous=True,
        anonHash=svc.anon_hash("tech-1"),
        number="FLD-2026-NW-0001",
        categorySnapshot={"l1": {"code": "machine_guarding", "labels": {"en": "Machine guarding"}}},
        description=None,
        transcriptEnglish=None,
        transcriptOriginal=None,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def test_owner_of_anonymous_report_is_recognised_via_hash():
    sub = _sub()
    assert svc.is_owner(sub, "tech-1") is True
    assert svc.is_owner(sub, "tech-2") is False


def test_named_report_ownership_uses_reporter_id():
    sub = _sub(isAnonymous=False, anonHash=None, reporterId="tech-9")
    assert svc.is_owner(sub, "tech-9") is True
    assert svc.is_owner(sub, "tech-1") is False


def test_submission_out_masks_anonymous_reporter():
    reporter = SimpleNamespace(id="tech-1", name="Ramesh Kumar", designation="Operator")
    sub = SimpleNamespace(
        id="s1", number="FLD-2026-NW-0001", clientSubmissionId="c1", type="observation",
        status="submitted", isAnonymous=True, reporter=reporter, reporterId=None,
        plantId="p1", areaId=None, mapPinX=None, mapPinY=None, equipmentId=None,
        qrScanned=False, categoryL1Id=None, categoryL2Id=None, categorySnapshot=None,
        aiSuggested=False, aiConfidence=None, severitySelfReported="high",
        description=None, voiceLangCode=None, transcriptOriginal=None,
        transcriptEnglish=None, transcriptionStatus="none", triagedById=None,
        triagedAt=None, hiraLikelihood=None, hiraSeverity=None, riskScore=None,
        riskLevel=None, triageNote=None, convertedEntityType=None,
        convertedEntityId=None, convertedAt=None, linkedRcaIds=[], linkedCapaIds=[],
        linkedPtwIds=[], tapCount=6, durationMs=42000, wasOffline=False,
        appVersion=None, deviceLang="hi", createdAtClient=None, createdAt=None,
    )
    masked = svc.submission_out(sub)
    assert masked["reporter"] is None  # non-owner never sees identity
    owner_view = svc.submission_out(sub, viewer_is_owner=True)
    assert owner_view["reporter"]["name"] == "Ramesh Kumar"
    unmasked = svc.submission_out(sub, unmasked=True)
    assert unmasked["reporter"]["id"] == "tech-1"
    # tap-count instrumentation must survive serialisation (spec 1.1.7)
    assert masked["capture"]["tapCount"] == 6


# ── conversion narrative ──────────────────────────────────────────────────────
def test_synth_description_is_long_enough_for_module_schemas():
    text = svc.synth_description(_sub())
    assert len(text) >= 10
    assert "Machine guarding" in text
    assert "FLD-2026-NW-0001" in text


def test_synth_description_prefers_english_transcript():
    sub = _sub(transcriptEnglish="Guard missing on cutting machine 3", transcriptOriginal="कटिंग मशीन 3 पर गार्ड नहीं है")
    text = svc.synth_description(sub)
    assert "Guard missing on cutting machine 3" in text
    assert "कटिंग" not in text  # English preferred when present


def test_synth_description_falls_back_to_original_transcript():
    sub = _sub(transcriptOriginal="कटिंग मशीन 3 पर गार्ड नहीं है")
    assert "कटिंग मशीन 3" in svc.synth_description(sub)


# ── Mobile Field Capture layer: 5 flows routed through triage (spec §2/§8.2) ──
def test_submission_create_accepts_all_five_flow_types():
    from app.schemas.capture import SubmissionCreate

    for flow in ("observation", "near_miss", "unsafe_condition", "incident", "ptw", "flra"):
        m = SubmissionCreate(clientSubmissionId="abcd1234", type=flow)
        assert m.type == flow


def test_submission_create_rejects_unknown_type():
    import pytest
    from pydantic import ValidationError

    from app.schemas.capture import SubmissionCreate

    with pytest.raises(ValidationError):
        SubmissionCreate(clientSubmissionId="abcd1234", type="banana")


def test_convert_body_carries_ptw_authorisation_fields():
    from datetime import datetime, timezone

    from app.schemas.capture import ConvertBody

    b = ConvertBody(
        target="ptw",
        permitType="HOT_WORK",
        validFrom=datetime(2026, 7, 11, 10, tzinfo=timezone.utc),
        validTo=datetime(2026, 7, 11, 14, tzinfo=timezone.utc),
        issuerId="u1",
        receiverId="u2",
    )
    assert b.target == "ptw" and b.issuerId == "u1" and b.receiverId == "u2"


def test_convert_body_carries_flra_crew_and_toolbox():
    from app.schemas.capture import ConvertBody

    b = ConvertBody(target="flra", teamMemberIds=["u1", "u2"], toolboxTalkById="u3")
    assert b.target == "flra" and b.teamMemberIds == ["u1", "u2"] and b.toolboxTalkById == "u3"


def test_convert_body_defaults_and_rejects_unknown_target():
    import pytest
    from pydantic import ValidationError

    from app.schemas.capture import ConvertBody

    base = ConvertBody(target="observation")
    assert base.teamMemberIds == [] and base.permitType is None and base.toolboxTalkById is None
    with pytest.raises(ValidationError):
        ConvertBody(target="permit")  # not one of the five targets


def test_ai_request_bodies_validate_length():
    import pytest
    from pydantic import ValidationError

    from app.schemas.capture import CleanupTextBody, SuggestCategoryBody

    assert CleanupTextBody(text="guard missing").lang == "hi"  # default lang
    with pytest.raises(ValidationError):
        CleanupTextBody(text="")
    with pytest.raises(ValidationError):
        SuggestCategoryBody(text="x" * 4001)


# ── AI text assist: fail-soft + fact-preserving guards (spec §7) ──────────────
async def test_cleanup_text_guards_reject_short_and_long_input():
    from app.services.ai.capture_providers import cleanup_text

    assert await cleanup_text("ab", "hi") is None      # too short — no API call
    assert await cleanup_text("x" * 4001, "hi") is None  # too long — no API call


async def test_cleanup_text_fails_soft_when_ai_unavailable(monkeypatch):
    import app.services.ai.anthropic_client as ac

    async def _none(**_kwargs):
        return None

    monkeypatch.setattr(ac, "complete_json", _none)
    from app.services.ai.capture_providers import cleanup_text

    assert await cleanup_text("guard missing on press 3 for two days", "en") is None


async def test_cleanup_text_returns_cleaned_when_available(monkeypatch):
    import app.services.ai.anthropic_client as ac

    async def _clean(**_kwargs):
        return {"cleaned": "The guard is missing on press 3."}

    monkeypatch.setattr(ac, "complete_json", _clean)
    from app.services.ai.capture_providers import cleanup_text

    assert await cleanup_text("guard missing press 3", "en") == "The guard is missing on press 3."


async def test_suggest_category_from_text_guards_and_maps(monkeypatch):
    from app.services.ai.capture_providers import suggest_category_from_text

    assert await suggest_category_from_text("ab", [], "hi") is None  # too short — no API call

    import app.services.ai.anthropic_client as ac

    async def _cat(**_kwargs):
        return {"l1Code": "electrical", "l2Code": None, "confidence": 0.9}

    monkeypatch.setattr(ac, "complete_json", _cat)
    out = await suggest_category_from_text(
        "sparks from the panel", [{"code": "electrical", "labels": {"en": "Electrical"}}], "en"
    )
    assert out is not None and out["l1Code"] == "electrical" and out["confidence"] == 0.9


# ── STOP act/condition axis ───────────────────────────────────────────────────
# Before this, `obsKind` never left the wizard — it was appended to the
# description as a sentence and nothing else — so conversion fell back to
# `sub.type in ("unsafe_condition", "near_miss")`. Since the observation flow
# always sends type="observation", EVERY captured observation converted to
# UNSAFE_ACT regardless of what the reporter tapped.

def test_observation_axis_follows_the_kind_answer_not_the_flow():
    assert svc.capture_axis("observation", "unsafe_act") == "ACT"
    assert svc.capture_axis("observation", "unsafe_condition") == "CONDITION"


def test_unsafe_condition_flow_is_a_condition_without_asking():
    # Its own top-level report type — it has no Kind step to answer.
    assert svc.capture_axis("unsafe_condition", None) == "CONDITION"
    assert svc.capture_axis("unsafe_condition", "unsafe_act") == "CONDITION"


def test_near_miss_keeps_its_existing_condition_mapping():
    assert svc.capture_axis("near_miss", None) == "CONDITION"


def test_skipped_kind_defaults_to_act_matching_historical_behaviour():
    assert svc.capture_axis("observation", None) == "ACT"


def test_non_observation_flows_have_no_stop_axis():
    for t in ("incident", "ptw", "flra"):
        assert svc.capture_axis(t, None) is None


def test_axis_from_snapshot_prefers_what_was_recorded():
    sub = SimpleNamespace(
        type="observation",
        categorySnapshot={"stop": {"axis": "CONDITION"}},
    )
    assert svc.axis_from_snapshot(sub) == "CONDITION"


def test_axis_from_snapshot_falls_back_for_pre_change_submissions():
    # Reports filed before the Kind step was persisted have no `stop` key.
    sub = SimpleNamespace(type="observation", categorySnapshot={"l1": {"code": "ppe"}})
    assert svc.axis_from_snapshot(sub) == "ACT"
    sub_uc = SimpleNamespace(type="unsafe_condition", categorySnapshot=None)
    assert svc.axis_from_snapshot(sub_uc) == "CONDITION"


def test_stop_pair_from_snapshot_reads_the_reporters_choice():
    sub = SimpleNamespace(
        categorySnapshot={
            "stop": {"axis": "ACT", "categoryCode": "PPE", "subCategoryCode": "PPE_NOT_WORN"}
        }
    )
    assert svc.stop_pair_from_snapshot(sub) == ("PPE", "PPE_NOT_WORN")


def test_stop_pair_from_snapshot_is_empty_when_category_was_skipped():
    sub = SimpleNamespace(categorySnapshot={"stop": {"axis": "ACT"}})
    assert svc.stop_pair_from_snapshot(sub) == (None, None)
    assert svc.stop_pair_from_snapshot(SimpleNamespace(categorySnapshot=None)) == (None, None)
