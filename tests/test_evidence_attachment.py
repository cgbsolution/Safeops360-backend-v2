"""Shared Evidence Attachment layer — offline unit tests (pure bits).

The two-phase upload + versioning is DB-bound and exercised through the live
flow; here we cover the pure pieces: the storage-path builder's sanitisation,
the entity registry, and the document-category contract.
"""

from __future__ import annotations

from app.schemas.attachment import DOCUMENT_CATEGORIES
from app.services import evidence_registry as reg
from app.services.storage import build_evidence_storage_path


def test_storage_path_shape_and_sanitisation():
    path = build_evidence_storage_path(
        entity_type="cams_finding",
        entity_id="fnd_123",
        category="FINDING_EVIDENCE",
        file_name="Weird / name*.pdf",
    )
    assert path.startswith("evidence/cams_finding/fnd_123/finding_evidence/")
    # unsafe chars are replaced; extension preserved
    assert path.endswith(".pdf")
    assert " " not in path and "*" not in path and "/name" not in path.split("/")[-1]


def test_storage_path_unique_per_call():
    a = build_evidence_storage_path(entity_type="x", entity_id="1", category="c", file_name="f.pdf")
    b = build_evidence_storage_path(entity_type="x", entity_id="1", category="c", file_name="f.pdf")
    assert a != b  # short random prefix guarantees uniqueness


def test_registry_has_cams_finding_with_correct_gate():
    spec = reg.get_spec("cams_finding")
    assert spec is not None
    assert spec.plant_attr == "siteId"
    assert spec.read_perm == "CAMS.READ"
    assert spec.write_perm == "CAMS.FINDING_MANAGE"
    assert "FINDING_EVIDENCE" in spec.categories
    assert "cams_finding" in reg.supported_entities()


def test_unknown_entity_returns_none():
    assert reg.get_spec("not_registered") is None


def test_document_categories_cover_ai_keys():
    # The classes §6 auto-extraction keys off must exist.
    for k in ("SDS", "certificate", "license"):
        assert k in DOCUMENT_CATEGORIES
