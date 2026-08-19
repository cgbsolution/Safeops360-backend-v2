"""PDF export for the Page Industries fire checklists and register.

This is the artefact that gets handed to an external auditor, so fidelity to the
paper original beats platform-standard styling — which is the build spec's own
instruction and the reason this module does not reuse `report_pdf.py`'s branded
cover-page-and-donut layout. It DOES reuse that module's typographic plumbing:
the same fpdf2 dependency, the same latin-1 sanitiser (`report_pdf._s`), the same
IST-by-default timestamp rule. Importing those rather than restating them is what
"do not build a second PDF generator" means in practice — there is one PDF
toolchain on this platform, and this is a second layout on it, not a second
toolchain.

WHAT "MATCHES THE SOURCE" MEANS HERE
------------------------------------
Every sheet in the four workbooks shares one skeleton, and it is that skeleton
these three renderers reproduce:

    [ Department: EHS ] [ TITLE ] [ Page No.: 1 of 1 ]
    [ Document No. ] [ Supersedes No. ] [ Effective Date ] [ Review Date ]
    ------------------------------------------------------------------
    the item table, in the sheet's own section order and wording
    ------------------------------------------------------------------
    footnotes / revision details, verbatim
    [ Prepared by: Person In-charge ][ Reviewed by: ... ][ Approved by: HOD ]
    [ Sign. & Date:               ][ Sign. & Date:    ][ Sign. & Date:     ]

An auditor comparing this against their copy is checking the document number, the
revision, the effective date and the sign-off block. Those are reproduced
verbatim from `documentMeta`, which the seeder transcribed from the sheet.

Three renderers because the workbooks have three shapes:
  render_grid     — items x periods (daily month page, FE year page, quarter page)
  render_form     — one period, sectioned (monthly / annual sheets)
  render_register — PIL/EHSD/CL/028-R1, sixteen columns landscape
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fpdf import FPDF

# The shared toolchain: same sanitiser, same timezone rule, same colour constants
# as every other PDF this platform emits.
from app.services.report_pdf import REPORT_TZ, REPORT_TZ_LABEL, _s

# Midnight Executive — this is a new module, so it ships in the new design
# language rather than the legacy violet. Navy rules and headers, gold accents,
# ice fills. Matches src/app/(dashboard)/fire-safety/lib.ts so the export and the
# screen that produced it read as one system.
NAVY = (11, 31, 77)
GOLD = (201, 169, 97)
ICE = (232, 238, 247)
INK = (26, 32, 44)
GREY = (110, 118, 135)
RULE = (190, 200, 216)
RED = (192, 57, 43)
AMBER = (200, 130, 20)
GREEN = (39, 139, 87)

# YES/NO/NA cell tint. Deliberately gentle: an inspector's month of ticks should
# read as a document, not a heat map, and a NO must still stand out on a page
# printed in greyscale — which is why NO is also the only bold cell.
CELL_TINT = {"YES": (238, 246, 241), "NO": (252, 235, 233), "NA": (240, 242, 246)}
CELL_INK = {"YES": GREEN, "NO": RED, "NA": GREY}


def _now_label() -> str:
    return f"{datetime.now(REPORT_TZ).strftime('%d-%m-%Y %H:%M')} {REPORT_TZ_LABEL}"


def _date(iso: str | None) -> str:
    """dd.mm.yyyy — the format the source sheets print."""
    if not iso:
        return ""
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(REPORT_TZ).strftime("%d.%m.%Y")
    except ValueError:
        return iso


class _Sheet(FPDF):
    """A controlled-document page: header band, footer with page numbers.

    The header repeats on every page because the source sheets are one page each
    and a two-page export of a filled month must still show the document number
    on the page an auditor happens to be holding.
    """

    def __init__(self, orientation: str, doc: dict[str, Any], title: str) -> None:
        super().__init__(orientation=orientation, unit="mm", format="A4")
        self.doc = doc or {}
        self.doc_title = title
        self.set_auto_page_break(auto=True, margin=16)
        self.set_margins(8, 8, 8)

    # ── header ───────────────────────────────────────────────────────────────
    def header(self) -> None:  # noqa: D102 — fpdf2 hook
        w = self.w - 16
        self.set_xy(8, 8)

        self.set_fill_color(*NAVY)
        self.set_text_color(255, 255, 255)
        self.set_font("Helvetica", "B", 11)
        self.cell(w, 7, _s("PAGE INDUSTRIES LIMITED"), border=0, align="C", fill=True, new_x="LMARGIN", new_y="NEXT")

        # Row 1: Department | title | page
        self.set_text_color(*INK)
        self.set_draw_color(*RULE)
        self.set_font("Helvetica", "", 7.5)
        c1, c3 = 42.0, 34.0
        c2 = w - c1 - c3
        self.cell(c1, 6, _s(f"Department: {self.doc.get('department', 'EHS')}"), border=1)
        self.set_font("Helvetica", "B", 8.5)
        self.cell(c2, 6, _s(self.doc_title), border=1, align="C")
        self.set_font("Helvetica", "", 7.5)
        self.cell(c3, 6, _s(f"Page No.: {self.page_no()} of {{nb}}"), border=1, align="C",
                  new_x="LMARGIN", new_y="NEXT")

        # Row 2: the four control fields, equal width, in the sheet's own order
        q = w / 4
        for label, value in (
            ("Document No.", self.doc.get("documentNo", "")),
            ("Supersedes No.", self.doc.get("supersedesNo", "")),
            ("Effective Date", _date(self.doc.get("effectiveDate"))),
            ("Review Date", _date(self.doc.get("reviewDate"))),
        ):
            self.cell(q, 6, _s(f"{label}: {value}"), border=1)
        self.ln(6)
        self.ln(1.5)

    # ── footer ───────────────────────────────────────────────────────────────
    def footer(self) -> None:  # noqa: D102 — fpdf2 hook
        self.set_y(-12)
        self.set_font("Helvetica", "I", 6.5)
        self.set_text_color(*GREY)
        self.cell(0, 4, _s(f"SafeOps360 — generated {_now_label()} — controlled document, "
                           f"{self.doc.get('documentNo', '')}"), align="C")


def _sign_off_block(pdf: _Sheet, sign: dict[str, Any] | None, roles: list[str] | None) -> None:
    """Prepared / Reviewed / Approved, printed exactly where the sheets print it.

    Prints the recorded name and timestamp where the stage is complete and leaves
    the cell blank where it is not — a blank "Sign. & Date" on the export is the
    correct rendering of an unsigned record, and pre-filling it would be the
    export asserting an approval that never happened.
    """
    roles = roles or ["Prepared by: Person In-charge", "Reviewed by: Intermediatory Head",
                      "Approved by: HOD"]
    sign = sign or {}
    w = (pdf.w - 16) / 3
    if pdf.get_y() > pdf.h - 34:
        pdf.add_page()
    pdf.ln(3)
    pdf.set_draw_color(*RULE)
    pdf.set_fill_color(*ICE)
    pdf.set_text_color(*NAVY)
    pdf.set_font("Helvetica", "B", 7.5)
    for r in roles[:3]:
        pdf.cell(w, 6, _s(r), border=1, align="C", fill=True)
    pdf.ln(6)

    pdf.set_text_color(*INK)
    pdf.set_font("Helvetica", "", 7)
    for stage in ("prepared", "reviewed", "approved"):
        name = sign.get(f"{stage}ByName") or ""
        at = sign.get(f"{stage}At")
        text = f"Sign. & Date: {name}  {_date(at)}".rstrip() if name else "Sign. & Date:"
        pdf.cell(w, 8, _s(text), border=1)
    pdf.ln(8)


def _footnotes(pdf: _Sheet, lines: list[str] | None) -> None:
    if not lines:
        return
    pdf.ln(1)
    pdf.set_font("Helvetica", "", 6.5)
    pdf.set_text_color(*GREY)
    for line in lines:
        pdf.multi_cell(pdf.w - 16, 3.4, _s(line), border=0)
    pdf.set_text_color(*INK)


def _out(pdf: FPDF) -> bytes:
    return bytes(pdf.output())


# ═══════════════════════════════════════════════════════════════════════════
# render_grid — items x periods
# ═══════════════════════════════════════════════════════════════════════════
def render_grid(payload: dict[str, Any]) -> bytes:
    """The daily month page, the FE year page and the quarter page.

    Landscape for the daily grid because 31 columns will not fit portrait at a
    legible size — the source sheet is landscape for the same reason.
    """
    doc = payload.get("document") or {}
    cols = payload.get("columns") or []
    rows = payload.get("rows") or []
    landscape = len(cols) > 12

    pdf = _Sheet("L" if landscape else "P", doc, payload.get("templateName", ""))
    pdf.alias_nb_pages()
    pdf.add_page()

    total_w = pdf.w - 16
    sl_w = 8.0
    # The item column takes whatever the period columns leave, floored so the
    # wording stays readable: a 31-column daily grid with a 40 mm item column is
    # a grid nobody can read the checks in.
    per_min, per_max = 5.0, 26.0
    per_w = max(per_min, min(per_max, (total_w - sl_w - 62.0) / max(1, len(cols))))
    item_w = total_w - sl_w - per_w * len(cols)

    # ── asset identity strip — the sheet's own header fields ────────────────
    pdf.set_font("Helvetica", "", 7.5)
    pdf.set_text_color(*INK)
    bits = [f"Asset: {payload.get('assetCode', '')}"]
    if payload.get("allottedSerialNo"):
        bits.append(f"FE No.: {payload['allottedSerialNo']}")
    if payload.get("assetSubtype"):
        bits.append(f"Type: {payload['assetSubtype']}")
    if payload.get("assetLocation"):
        bits.append(f"Location & Floor: {payload['assetLocation']}")
    bits.append(f"Period: {payload.get('window', '')}")
    pdf.cell(total_w, 5.5, _s("   |   ".join(bits)), border=1, new_x="LMARGIN", new_y="NEXT")

    def head() -> None:
        pdf.set_fill_color(*ICE)
        pdf.set_text_color(*NAVY)
        pdf.set_font("Helvetica", "B", 6.5)
        pdf.cell(sl_w, 7, _s("Sl."), border=1, align="C", fill=True)
        pdf.cell(item_w, 7, _s(payload.get("rows", [{}])[0].get("sectionTitle", "Checks to be done")
                               if rows else "Checks"), border=1, fill=True)
        for c in cols:
            # A non-working day is tinted at the header so a whole empty column
            # reads as "the plant was shut", not "nobody inspected".
            pdf.set_fill_color(*(GOLD if c.get("nonWorkingDay") else ICE))
            pdf.cell(per_w, 7, _s(str(c.get("header", ""))), border=1, align="C", fill=True)
        pdf.ln(7)
        pdf.set_fill_color(*ICE)

    head()

    current_section = rows[0].get("sectionTitle") if rows else None
    n = 0
    for r in rows:
        if pdf.get_y() > pdf.h - 40:
            pdf.add_page()
            head()
        if r.get("sectionTitle") != current_section:
            current_section = r.get("sectionTitle")
            pdf.set_font("Helvetica", "B", 7)
            pdf.set_fill_color(*ICE)
            pdf.set_text_color(*NAVY)
            pdf.cell(total_w, 5, _s(current_section or ""), border=1, fill=True, new_x="LMARGIN", new_y="NEXT")
        n += 1

        pdf.set_font("Helvetica", "", 6.3)
        pdf.set_text_color(*INK)
        text = r.get("text", "")
        if r.get("guidance"):
            text = f"{text}  (Note: {r['guidance']})"
        # Height driven by the wording — the long hydrant and alarm items are two
        # printed lines on the source sheet too.
        h = 5.0 if pdf.get_string_width(_s(text)) <= (item_w - 3) else 8.0
        y0 = pdf.get_y()
        pdf.cell(sl_w, h, str(n), border=1, align="C")
        x_item = pdf.get_x()
        pdf.multi_cell(item_w, h / 2 if h > 5.0 else h, _s(text), border=1,
                       new_x="RIGHT", new_y="TOP", max_line_height=h / 2 if h > 5.0 else h)
        pdf.set_xy(x_item + item_w, y0)

        for c in cols:
            label = str(r.get("cells", {}).get(c["periodLabel"], {}).get("value") or "")
            closed = c.get("nonWorkingDay")
            if closed and not label:
                pdf.set_fill_color(*GOLD)
                pdf.set_text_color(255, 255, 255)
                pdf.set_font("Helvetica", "B", 4.6)
                pdf.cell(per_w, h, _s(closed[:3]), border=1, align="C", fill=True)
            else:
                tint = CELL_TINT.get(label)
                pdf.set_fill_color(*(tint or (255, 255, 255)))
                pdf.set_text_color(*CELL_INK.get(label, INK))
                pdf.set_font("Helvetica", "B" if label == "NO" else "", 6.0)
                pdf.cell(per_w, h, _s(label[:8]), border=1, align="C", fill=bool(tint))
        pdf.ln(h)
        pdf.set_text_color(*INK)

    # ── per-period stage strip ──────────────────────────────────────────────
    # A grid page covers many runs, each with its own sign-off state, so the
    # single foot block cannot speak for all of them. This row says which periods
    # are approved and which are still drafts, which is the question an auditor
    # asks of a month page.
    pdf.set_font("Helvetica", "B", 5.6)
    pdf.set_fill_color(*ICE)
    pdf.set_text_color(*NAVY)
    pdf.cell(sl_w + item_w, 5, _s("Sign-off stage"), border=1, fill=True)
    pdf.set_font("Helvetica", "", 4.8)
    pdf.set_text_color(*GREY)
    for c in cols:
        stage = (c.get("stage") or "")[:4]
        pdf.cell(per_w, 5, _s(stage), border=1, align="C")
    pdf.ln(5)

    _footnotes(pdf, doc.get("footnotes"))
    # The Prepared/Reviewed/Approved block is printed blank on a grid page: the
    # paper sheet's block is signed once for the whole month, and the per-period
    # stamps are in the strip above.
    _sign_off_block(pdf, None, doc.get("signOffRoles"))
    return _out(pdf)


# ═══════════════════════════════════════════════════════════════════════════
# render_form — one period, sectioned
# ═══════════════════════════════════════════════════════════════════════════
def render_form(payload: dict[str, Any]) -> bytes:
    """The monthly / annual sheets: headings, items, answers, sign-off."""
    doc = payload.get("document") or {}
    pdf = _Sheet("P", doc, payload.get("templateName", ""))
    pdf.alias_nb_pages()
    pdf.add_page()

    total_w = pdf.w - 16
    sl_w, ans_w = 9.0, 26.0
    item_w = total_w - sl_w - ans_w

    pdf.set_font("Helvetica", "", 7.5)
    pdf.set_text_color(*INK)
    bits = [f"Asset: {payload.get('assetCode', '')}"]
    if payload.get("assetLocation"):
        bits.append(f"Location: {payload['assetLocation']}")
    bits.append(f"Period: {payload.get('periodLabel', '')}")
    bits.append(f"Status: {payload.get('stage', '')}")
    pdf.cell(total_w, 5.5, _s("   |   ".join(bits)), border=1, new_x="LMARGIN", new_y="NEXT")

    for sec in payload.get("sections") or []:
        if pdf.get_y() > pdf.h - 46:
            pdf.add_page()
        pdf.set_font("Helvetica", "B", 8)
        pdf.set_fill_color(*ICE)
        pdf.set_text_color(*NAVY)
        pdf.cell(total_w, 5.5, _s(sec.get("title", "")), border=1, fill=True, new_x="LMARGIN", new_y="NEXT")
        if sec.get("note"):
            pdf.set_font("Helvetica", "I", 6.5)
            pdf.set_text_color(*GREY)
            pdf.cell(total_w, 4.5, _s(sec["note"]), border=1, new_x="LMARGIN", new_y="NEXT")

        for i, item in enumerate(sec.get("items") or [], start=1):
            if pdf.get_y() > pdf.h - 40:
                pdf.add_page()
            text = item.get("text", "")
            if item.get("guidance"):
                text = f"{text}  (Note: {item['guidance']})"
            pdf.set_font("Helvetica", "", 7)
            pdf.set_text_color(*INK)
            h = 5.0 if pdf.get_string_width(_s(text)) <= (item_w - 3) else 9.0
            y0 = pdf.get_y()
            pdf.cell(sl_w, h, str(i), border=1, align="C")
            x_item = pdf.get_x()
            pdf.multi_cell(item_w, h / 2 if h > 5.0 else h, _s(text), border=1,
                           new_x="RIGHT", new_y="TOP", max_line_height=h / 2 if h > 5.0 else h)
            pdf.set_xy(x_item + item_w, y0)

            value = str(item.get("value") or "")
            tint = CELL_TINT.get(value)
            pdf.set_fill_color(*(tint or (255, 255, 255)))
            pdf.set_text_color(*CELL_INK.get(value, INK))
            pdf.set_font("Helvetica", "B" if value == "NO" else "", 7)
            pdf.cell(ans_w, h, _s(value[:22]), border=1, align="C", fill=bool(tint))
            pdf.ln(h)

            if item.get("note"):
                pdf.set_font("Helvetica", "I", 6.2)
                pdf.set_text_color(*GREY)
                pdf.cell(sl_w, 4, "", border=1)
                pdf.cell(item_w + ans_w, 4, _s(f"Remark: {item['note']}"), border=1,
                         new_x="LMARGIN", new_y="NEXT")
            pdf.set_text_color(*INK)

    _footnotes(pdf, doc.get("footnotes"))
    _sign_off_block(pdf, payload.get("signOff"), doc.get("signOffRoles"))
    return _out(pdf)


# ═══════════════════════════════════════════════════════════════════════════
# render_register — PIL/EHSD/CL/028-R1
# ═══════════════════════════════════════════════════════════════════════════
_REG_COLS: list[tuple[str, str, float]] = [
    ("slNo", "Sl.", 8), ("serialNo", "Mfr Serial No.", 22), ("type", "Type", 14),
    ("capacity", "Capacity", 15), ("yearOfManufacture", "Yr Mfg", 13), ("expiryDate", "Expiry", 18),
    ("make", "Make", 20), ("allottedSerialNo", "Alloted Sl. No.", 20), ("location", "Location", 44),
    ("hpTestedOn", "HP tested", 18), ("hpTestDueDate", "HP due", 18),
    ("dateOfDischarge", "Discharged", 18), ("refilledOn", "Refilled", 18),
    ("dueForRefilling", "Refill due", 18), ("weightKg", "Wt kg", 12), ("remarks", "Remarks", 25),
]

# Which badge governs which column — so the colour sits on the date it is about,
# rather than on the row, which is what makes "which of the three is overdue?"
# answerable from the printout.
_COL_BADGE = {"expiryDate": "cylinderLife", "hpTestDueDate": "hpTest", "dueForRefilling": "refill"}
_BADGE_INK = {"OVERDUE": RED, "DUE_SOON": AMBER, "OK": GREEN, "NOT_RECORDED": GREY}


def render_register(payload: dict[str, Any]) -> bytes:
    doc = payload.get("document") or {}
    rows = payload.get("rows") or []
    summary = payload.get("summary") or {}

    pdf = _Sheet("L", doc, doc.get("title", "REGISTER OF FIRE EXTINGUISHERS"))
    pdf.alias_nb_pages()
    pdf.add_page()

    total_w = pdf.w - 16
    scale = total_w / sum(c[2] for c in _REG_COLS)
    widths = [c[2] * scale for c in _REG_COLS]

    pdf.set_font("Helvetica", "", 7.5)
    pdf.set_text_color(*INK)
    pdf.cell(
        total_w, 5.5,
        _s(f"{summary.get('total', len(rows))} cylinder(s)   |   "
           f"Overdue: {summary.get('overdue', 0)}   |   Due within 30 days: {summary.get('dueSoon', 0)}   |   "
           f"Date not recorded: {summary.get('notRecorded', 0)}"),
        border=1, new_x="LMARGIN", new_y="NEXT",
    )

    def head() -> None:
        pdf.set_font("Helvetica", "B", 6.2)
        pdf.set_fill_color(*ICE)
        pdf.set_text_color(*NAVY)
        for (_key, label, _w), w in zip(_REG_COLS, widths):
            pdf.cell(w, 7, _s(label), border=1, align="C", fill=True)
        pdf.ln(7)

    head()
    pdf.set_font("Helvetica", "", 6.0)
    for r in rows:
        if pdf.get_y() > pdf.h - 30:
            pdf.add_page()
            head()
            pdf.set_font("Helvetica", "", 6.0)
        badges = r.get("badges") or {}
        for (key, _label, _w), w in zip(_REG_COLS, widths):
            val = r.get(key)
            if key in ("expiryDate", "hpTestedOn", "hpTestDueDate", "dateOfDischarge",
                       "refilledOn", "dueForRefilling"):
                text = _date(val)
            elif val is None:
                text = ""
            else:
                text = str(val)

            badge_key = _COL_BADGE.get(key)
            if badge_key:
                statusname = (badges.get(badge_key) or {}).get("status", "NOT_RECORDED")
                pdf.set_text_color(*_BADGE_INK.get(statusname, INK))
                pdf.set_font("Helvetica", "B" if statusname in ("OVERDUE", "DUE_SOON") else "", 6.0)
                if statusname == "NOT_RECORDED":
                    text = text or "not recorded"
            else:
                pdf.set_text_color(*INK)
                pdf.set_font("Helvetica", "", 6.0)

            # Truncate rather than wrap: a register row that wraps stops being a
            # row, and the full text is on the screen this was printed from.
            while text and pdf.get_string_width(_s(text)) > (w - 2):
                text = text[:-1]
            pdf.cell(w, 5, _s(text), border=1, align="C" if key != "location" else "L")
        pdf.ln(5)

    pdf.set_text_color(*INK)
    _footnotes(pdf, [
        "Badge key: OVERDUE (past due) / bold amber (due within 30 days) / green (in date) / "
        "'not recorded' (no date on file — a register gap, not compliance).",
        "HP test and refill dates are held as asset certificates; this sheet projects the current "
        "certificate of each type.",
    ])
    _sign_off_block(pdf, None, None)
    return _out(pdf)


__all__ = ["render_grid", "render_form", "render_register"]
