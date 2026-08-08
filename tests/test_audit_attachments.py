"""Checkpoint evidence uploads — photographs AND documents.

Half of an audit's evidence is paperwork: a factory licence, a calibration
certificate, a test report, a wage register extract. Before this the upload
endpoint accepted images and PDF only, so the evidence for exactly the statutory
checkpoints that need it hardest had nowhere to go.

Pure assertions over the SERVICE's allowlist and path builder — no HTTP client
or storage harness, matching `test_supplier_audits.py`'s style. Which file types
count as evidence is domain policy, so it lives in the service and the router
only enforces it; that is also what makes it importable here, since the router
pulls in a Supabase client.

The signing itself is Supabase's and is not re-tested. What IS worth pinning is
which types get through the door, and that a filename cannot escape its prefix.
"""

from __future__ import annotations

import pytest

from app.services.audit_compliance import (
    ALLOWED_DOCUMENT_MIME,
    ALLOWED_IMAGE_MIME,
    ALLOWED_UPLOAD_MIME,
    attachment_storage_path,
)

DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


# ── What may be attached ──────────────────────────────────────────────────


@pytest.mark.parametrize(
    "mime",
    ["image/jpeg", "image/png", "image/webp", "image/heic", "image/gif"],
)
def test_photographs_are_still_accepted(mime: str):
    """The regression that would matter most: widening the allowlist must not
    drop a camera format an auditor's phone actually produces."""
    assert mime in ALLOWED_UPLOAD_MIME


@pytest.mark.parametrize(
    "mime",
    [
        "application/pdf",
        DOCX,
        XLSX,
        "application/msword",
        "application/vnd.ms-excel",
        "text/csv",
        "text/plain",
    ],
)
def test_documents_are_accepted(mime: str):
    assert mime in ALLOWED_UPLOAD_MIME


@pytest.mark.parametrize(
    "mime",
    [
        "application/zip",
        "application/x-msdownload",
        "application/octet-stream",
        "text/html",
        "image/svg+xml",  # scriptable — an image type that is not a photograph
        "video/mp4",
    ],
)
def test_executables_archives_and_active_content_stay_out(mime: str):
    """An allowlist, not a widening to anything. SVG is the sharp one: it looks
    like an image and can carry script, so it must not ride in on `image/`."""
    assert mime not in ALLOWED_UPLOAD_MIME


def test_the_two_halves_are_disjoint_and_cover_the_whole():
    """The UI builds its two file pickers from these sets. Overlap would put the
    same type behind both buttons; a gap would make something acceptable to the
    server that no picker offers."""
    assert not (ALLOWED_IMAGE_MIME & ALLOWED_DOCUMENT_MIME)
    assert ALLOWED_IMAGE_MIME | ALLOWED_DOCUMENT_MIME == ALLOWED_UPLOAD_MIME


def test_every_image_type_is_an_image_and_no_document_is():
    """Guards the split the client relies on to decide whether it may render an
    `<img>`: a document mistakenly filed as an image renders as a broken
    thumbnail where a reviewer expects evidence."""
    assert all(m.startswith("image/") for m in ALLOWED_IMAGE_MIME)
    assert not any(m.startswith("image/") for m in ALLOWED_DOCUMENT_MIME)


# ── Where it is stored ────────────────────────────────────────────────────


def test_path_is_scoped_to_the_audit_and_checkpoint():
    p = attachment_storage_path("aud-1", "PI-QMS-001", "licence.pdf")
    assert p.startswith("audit-compliance/aud-1/pi-qms-001/")
    assert p.endswith("-licence.pdf")


def test_path_keeps_the_extension_so_the_type_stays_recoverable():
    """Attachments stored before `mimeType` was recorded are classified from
    their extension. Stripping it would make an old PDF indistinguishable from a
    photograph, which is precisely the broken-thumbnail case."""
    for name, ext in [("a.pdf", ".pdf"), ("b.XLSX", ".XLSX"), ("c.docx", ".docx")]:
        assert attachment_storage_path("a", "c", name).endswith(ext)


def test_traversal_and_separators_cannot_escape_the_prefix():
    """The file name comes from the browser, and a storage path is built from it.

    The property that matters is that the name stays ONE path segment: dots are
    harmless once the separators are gone — `.._.._etc_passwd` is a silly
    filename, not a traversal — so what is asserted is the segment count and
    that no separator or traversal component survives, rather than the absence
    of the characters themselves.
    """
    for evil in ["../../etc/passwd", "..\\..\\win.ini", "a/b/c.pdf", "/absolute.pdf", "....//x"]:
        p = attachment_storage_path("aud-1", "cp", evil)
        assert p.startswith("audit-compliance/aud-1/cp/")
        # Exactly four segments: prefix / audit / checkpoint / file.
        segments = p.split("/")
        assert len(segments) == 4, p
        leaf = segments[-1]
        assert "\\" not in leaf, p
        # A leaf that IS a traversal component would be the real defect.
        assert leaf.split("-", 1)[-1] not in ("..", "."), p


def test_two_uploads_of_the_same_name_do_not_collide():
    """Same checkpoint, same filename, twice — a random prefix is what stops the
    second upload silently overwriting the first auditor's evidence."""
    a = attachment_storage_path("aud-1", "cp", "licence.pdf")
    b = attachment_storage_path("aud-1", "cp", "licence.pdf")
    assert a != b


def test_a_nameless_upload_still_produces_a_usable_path():
    p = attachment_storage_path(None, None, "")
    assert p.startswith("audit-compliance/unassigned/general/")
    assert p.rsplit("/", 1)[-1].endswith("-file")
