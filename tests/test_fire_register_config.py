"""Config-driven fire registers — the layer that makes FireRegisterViewConfig real.

The table was seeded with all three registers and read by nothing, so its stated
purpose — "adding the next register is a seed entry, not a screen" — was false.
What is worth testing is therefore the contract that makes it true:

  * the document-control block is built from the config row, not from a constant;
  * a column key the row builder cannot produce is REPORTED, not silently blank,
    because a blank column on a statutory register reads as "nothing recorded";
  * the extinguisher keeps its certificate-projected columns, since no generic
    field projection can produce them.

Offline — no DB.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from app.services import fire_register_config as regcfg


def _cfg(**kw):
    base = dict(
        tenantId=None, assetType="FIRE_ALARM_PANEL",
        brandName="Register of Fire Alarm Panels", routeSlug="alarm-panel-register",
        documentNo="SO360/FIRE/REG/FAS-01", supersedesNo=None, revision="R1",
        effectiveDate=datetime(2026, 1, 1, tzinfo=timezone.utc),
        reviewDate=datetime(2029, 1, 1, tzinfo=timezone.utc),
        department="EHS", pdfTemplateKey="GENERIC_REGISTER", isActive=True,
        columns=[("slNo", "Sl. No"), ("equipmentCode", "Panel Code"), ("location", "Location")],
    )
    base.update(kw)
    return SimpleNamespace(**base)


def _asset(**kw):
    base = dict(
        id="a1", equipmentCode="FAS-A", assetSubtype="ZONE", make="Ravel", model="RE-4",
        serialNo="SN-1", capacitySpec=None, location="Admin Block", zoneId=None,
        installationDate=None, lastInspectionDate=None,
        nextInspectionDueDate=datetime.now(timezone.utc) + timedelta(days=10),
        status="ACTIVE", registerRemarks=None, plantId="p1",
    )
    base.update(kw)
    return SimpleNamespace(**base)


# ── the document comes from config ───────────────────────────────────────────
def test_document_is_built_from_the_config_row():
    doc = regcfg.document_from_config(_cfg())
    assert doc["documentNo"] == "SO360/FIRE/REG/FAS-01"
    assert doc["revision"] == "R1"
    assert doc["department"] == "EHS"
    assert doc["title"] == "REGISTER OF FIRE ALARM PANELS"
    assert doc["columns"] == [["slNo", "Sl. No"], ["equipmentCode", "Panel Code"],
                              ["location", "Location"]]
    assert doc["routeSlug"] == "alarm-panel-register"


def test_columns_survive_both_stored_shapes():
    # JSONB round-trips a seeded tuple as a list; a hand-edited row may be dicts.
    as_dicts = _cfg(columns=[{"key": "equipmentCode", "label": "Panel Code"}])
    assert regcfg.document_from_config(as_dicts)["columns"] == [["equipmentCode", "Panel Code"]]


def test_an_unknown_pdf_template_degrades_to_generic():
    # `pdfTemplateKey` is a key selecting a layout, not a filename, precisely so
    # a bad value is caught here instead of 500-ing halfway through a render.
    doc = regcfg.document_from_config(_cfg(pdfTemplateKey="NOT_A_REAL_LAYOUT"))
    assert doc["pdfTemplateKey"] == regcfg.TEMPLATE_GENERIC


def test_the_extinguisher_keeps_its_own_layout_key():
    doc = regcfg.document_from_config(_cfg(assetType=regcfg.EXTINGUISHER, pdfTemplateKey="FE_REGISTER"))
    assert doc["pdfTemplateKey"] == regcfg.TEMPLATE_FE


# ── rows ─────────────────────────────────────────────────────────────────────
def test_generic_row_projects_what_the_asset_actually_holds():
    row = regcfg.generic_row(_asset(), sl_no=1)
    assert row["slNo"] == 1
    assert row["equipmentCode"] == "FAS-A"
    assert row["assetSubtype"] == "ZONE"
    assert row["location"] == "Admin Block"
    assert row["status"] == "ACTIVE"


def test_generic_row_carries_the_same_due_badge_ladder_as_the_extinguisher():
    # One visual rule across all three registers, not three.
    overdue = regcfg.generic_row(_asset(nextInspectionDueDate=datetime.now(timezone.utc) - timedelta(days=3)))
    soon = regcfg.generic_row(_asset(nextInspectionDueDate=datetime.now(timezone.utc) + timedelta(days=10)))
    never = regcfg.generic_row(_asset(nextInspectionDueDate=None))
    assert overdue["worstBadge"] == "OVERDUE"
    assert soon["worstBadge"] == "DUE_SOON"
    assert never["worstBadge"] == "NOT_RECORDED"


# ── the misconfiguration guard ───────────────────────────────────────────────
def test_a_column_with_no_backing_field_is_reported():
    # THE point of this check. `zoneCount` is in the seeded alarm-panel config
    # and FireEquipment has no such field — it holds `zoneId`, a FireZone
    # reference. Left unreported, that column renders blank and an auditor reads
    # it as "no zones recorded" rather than "never wired up".
    cfg = _cfg(columns=[("equipmentCode", "Panel Code"), ("zoneCount", "Zones / Loops")])
    assert regcfg.unmapped_columns(cfg, regcfg.generic_row(_asset())) == ["zoneCount"]


def test_a_fully_mapped_config_reports_nothing():
    cfg = _cfg(columns=[("equipmentCode", "Code"), ("location", "Location"), ("status", "Status")])
    assert regcfg.unmapped_columns(cfg, regcfg.generic_row(_asset())) == []


def test_unmapped_check_tolerates_an_empty_column_list():
    assert regcfg.unmapped_columns(_cfg(columns=[]), regcfg.generic_row(_asset())) == []
    assert regcfg.unmapped_columns(_cfg(columns=None), regcfg.generic_row(_asset())) == []


# ── the exporters both render from doc["columns"] ────────────────────────────
def test_generic_pdf_and_xlsx_render_from_the_config_columns():
    from app.services.fire_checklist_pdf import render_generic_register
    from app.services.fire_checklist_xlsx import render_generic_register as xlsx

    payload = {
        "document": regcfg.document_from_config(_cfg()),
        "rows": [regcfg.generic_row(_asset(), sl_no=1)],
        "summary": {"total": 1, "overdue": 0, "dueSoon": 1, "notRecorded": 0},
    }
    assert render_generic_register(payload).startswith(b"%PDF")
    assert xlsx(payload).startswith(b"PK")


def test_generic_exporters_survive_a_config_with_no_columns():
    # A register is a statutory document: it must still print if its config is
    # half-finished, rather than 500-ing when someone opens it.
    from app.services.fire_checklist_pdf import render_generic_register

    payload = {"document": regcfg.document_from_config(_cfg(columns=[])),
               "rows": [regcfg.generic_row(_asset())], "summary": {"total": 1}}
    assert render_generic_register(payload).startswith(b"%PDF")
