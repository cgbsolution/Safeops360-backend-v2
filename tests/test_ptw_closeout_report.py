"""PTW close-out PDF rendering — offline unit tests.

`render_ptw_closeout_pdf` is a pure function over the dict the router builds,
so it needs no DB — the house test style.

Regression under test: every `kv()` / `multi_cell()` block in the report is a
full-width call (`w=0`). fpdf2 leaves the cursor on the RIGHT edge of such a
cell (new_x=XPos.RIGHT is its default), so the *second* full-width write had
zero usable width left and fpdf2 raised

    FPDFException: Not enough horizontal space to render a single character

That turned GET /api/ptw/{id}/report into a 500 for every permit, empty or
not. The renderer now defaults multi_cell to new_x=LMARGIN.
"""

from __future__ import annotations

import base64
from datetime import datetime, timezone

from app.services.ptw_report import render_ptw_closeout_pdf

D = datetime(2025, 6, 29, 9, 46, tzinfo=timezone.utc)

# Smallest valid PNG — stands in for a drawn signature / onsite photo.
_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


def _bare_permit_data() -> dict:
    """A legacy/imported permit: closed, but no crew, evidence or approvals."""
    return {
        "latestAuditHash": None,
        "permit": {
            "number": "PTW-SW-HIST-12-09",
            "type": "GENERAL_COLD",
            "status": "CLOSED",
            "outcome": None,
            "plantName": "Meridian South Works",
            "location": "Utilities",
            "specificLocation": None,
            "scopeOfWork": "General maintenance task in non-hazardous area.",
            "validFrom": D,
            "validTo": D,
            "originatorName": "Praveen Roy",
            "issuerName": "Sumit Das",
            "receiverName": "—",
            "contractorName": None,
            "flraRequired": False,
            "gpsLatitude": None,
            "gpsLongitude": None,
            "workCompletedAt": None,
            "workCompletedByName": "—",
            "returnNotes": None,
            "siteVerifiedAt": None,
            "siteVerifiedByName": "—",
            "siteVerificationChecklist": None,
            "closedAt": D,
            "closedByName": "—",
            "closingRemark": "Work completed. Area restored and permit returned.",
            "cancelledAt": None,
            "cancelledByName": "—",
            "cancellationReason": None,
        },
        "crew": [],
        "evidence": [],
        "approvals": [],
        "gasReadings": [],
        "isolations": [],
        "suspensions": [],
        "extensions": [],
    }


def _full_permit_data() -> dict:
    """Every optional section populated, including long wrapping text, a valid
    signature blob, photos, and one deliberately corrupt signature."""
    data = _bare_permit_data()
    p = data["permit"]
    p.update(
        number="PTW-SW-0001",
        type="HOT_WORK",
        status="OPEN",  # not CLOSED -> PROVISIONAL watermark path
        outcome="COMPLETED",
        specificLocation="Deaerator platform, +12 m, east side of the LP steam header run",
        scopeOfWork="Replace corroded steam trap on the LP header. " * 8,
        receiverName="Anil Kumar",
        contractorName="Zenith Mechanical Services Pvt Ltd",
        flraRequired=True,
        gpsLatitude=12.9716,
        gpsLongitude=77.5946,
        workCompletedAt=D,
        workCompletedByName="Anil Kumar",
        returnNotes="Trap replaced, insulation reinstated, area washed down.",
        siteVerifiedAt=D,
        siteVerifiedByName="Sumit Das",
        siteVerificationChecklist={"Area clean": True, "Isolations restored": False},
        cancelledAt=D,
        cancelledByName="Sumit Das",
        cancellationReason="Superseded by PTW-SW-0002",
    )
    data["crew"] = [
        {"name": "Anil Kumar", "role": "FITTER", "trainingValid": True, "ppeValid": False, "removedAt": None}
    ]
    data["evidence"] = [
        {
            "action": "ISSUE",
            "actorName": "Sumit Das",
            "capturedAt": D,
            "gpsLatitude": 12.97,
            "gpsLongitude": 77.59,
            "gpsAccuracyMeters": 8.0,
            "declarationText": "I confirm the isolations listed are in place and verified. " * 4,
            "comments": "Second gas test taken before entry. " * 4,
            "signatureImageBase64": "data:image/png;base64," + base64.b64encode(_PNG).decode(),
            "photoBytes": [_PNG, _PNG],
        },
        {
            # No GPS, no declaration, and an unreadable signature blob — the
            # renderer must degrade instead of raising.
            "action": "CLOSE",
            "actorName": "Sumit Das",
            "capturedAt": None,
            "gpsLatitude": None,
            "gpsLongitude": None,
            "gpsAccuracyMeters": None,
            "declarationText": None,
            "comments": None,
            "signatureImageBase64": "not-valid-base64!!",
            "photoBytes": [],
        },
    ]
    data["approvals"] = [
        {"step": "ISSUER", "approverName": "Sumit Das", "decision": "APPROVED", "decidedAt": D, "comments": None}
    ]
    data["gasReadings"] = [
        {
            "recordedAt": D,
            "byName": "Anil Kumar",
            "readings": [{"parameter": "O2", "value": 20.8}],
            "isExceedance": False,
            "isPreEntry": True,
        }
    ]
    data["isolations"] = [
        {
            "tag": "V-101",
            "type": "MECHANICAL",
            "verifiedAt": D,
            "verifiedByName": "Sumit Das",
            "restoredAt": None,
            "restoredByName": None,
            "lotoTag": "LT-9",
        }
    ]
    data["suspensions"] = [
        {
            "suspendedAt": D,
            "reason": "WEATHER",
            "reasonDetail": None,
            "resumedAt": D,
            "reFlraRequired": True,
            "byName": "Sumit Das",
        }
    ]
    data["extensions"] = [
        {"requestedAt": D, "newValidTo": D, "status": "APPROVED", "approverName": None, "reason": "Scope grew"}
    ]
    return data


def test_bare_permit_renders():
    out = render_ptw_closeout_pdf(_bare_permit_data())
    assert out.startswith(b"%PDF")
    assert len(out) > 1000


def test_full_permit_renders():
    out = render_ptw_closeout_pdf(_full_permit_data())
    assert out.startswith(b"%PDF")


def test_consecutive_full_width_blocks_do_not_exhaust_the_line():
    """The exact shape of the 500: two full-width writes in a row."""
    from app.services.ptw_report import _PtwReport

    pdf = _PtwReport("PTW-1", provisional=False)
    pdf.add_page()
    for i in range(20):
        pdf.kv(f"Field {i}", "a value long enough to wrap onto a second line " * 2)
    assert pdf.get_x() == pdf.l_margin
