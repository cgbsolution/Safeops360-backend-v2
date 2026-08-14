"""P2-9 — Audit report PDF generation (fpdf2; pure-Python, no system deps).

Renders an AuditReport's immutable snapshot to a branded A4 PDF: cover page,
INTERIM 'PROVISIONAL' watermark on every page, category compliance, findings
register, CAPA summary, sign-off block (FINAL), page numbers + confidential footer.
"""

from __future__ import annotations

import math
import os
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

from fpdf import FPDF

# No tenant-level timezone setting exists anywhere in the platform (checked:
# app/core/config.py has none), and every deployment so far is India-based —
# the checkpoint library cites the Factories Act. So: IST by default, overridable
# per deployment, and never a bare UTC timestamp sitting next to a local one.
_TZ_NAME = os.environ.get("REPORT_TIMEZONE", "Asia/Kolkata")
REPORT_TZ_LABEL = os.environ.get("REPORT_TIMEZONE_LABEL", "IST")
try:
    REPORT_TZ = ZoneInfo(_TZ_NAME)
except Exception:  # noqa: BLE001 — a bad env var must not break report generation
    REPORT_TZ, REPORT_TZ_LABEL = timezone.utc, "UTC"

_REPL = {"—": "-", "–": "-", "‘": "'", "’": "'", "“": '"', "”": '"',
         # U+2026 is not latin-1, so without an entry here it fell through to
         # `encode(..., "replace")` and printed as "?" - a truncated quotation
         # ended `briefed.?"` on the real Page Industries report.
         "…": "...",
         "•": "*", "₹": "Rs ", "→": "->", "≥": ">=", "≤": "<=", " ": " "}


def _s(text: Any) -> str:
    """Sanitise to latin-1 (fpdf2 core fonts) — map common Unicode then drop the rest."""
    t = str(text)
    for k, v in _REPL.items():
        t = t.replace(k, v)
    return t.encode("latin-1", "replace").decode("latin-1")


NAVY = (30, 41, 90)
PURPLE = (88, 28, 135)
GREY = (100, 100, 100)
LIGHT = (235, 235, 240)
RED = (192, 57, 43)
AMBER = (230, 126, 34)
GREEN = (39, 139, 87)

# CAMS violet/indigo — the SAME ramp as the app (`tailwind.config.ts` primary,
# i.e. Tailwind violet). The insight layer is a new visual language on an
# existing product, not a new palette: a report that looked like a different
# product from the screen that produced it would read as a different system.
VIOLET = (109, 40, 217)        # violet-700 — headings, chart accents
VIOLET_DARK = (76, 29, 149)    # violet-900 — banner ground
VIOLET_TINT = (237, 233, 254)  # violet-100 — card fills
SLATE = (71, 85, 105)          # slate-600 — secondary text
HAIRLINE = (226, 232, 240)     # slate-200 — rules and empty track
INK = (15, 23, 42)             # slate-900 — primary text
RED_TINT = (254, 226, 226)
AMBER_TINT = (254, 243, 199)
GREEN_TINT = (209, 250, 229)

# Band key (from `insights.rules_audit_report`) -> ink + fill. The BANDING
# DECISION is not made here: the insight layer already assigned every gauge and
# bar its band, so the PDF, the screen and the stored snapshot cannot disagree
# about what colour a number is. This map only says what each band looks like.
_BAND_INK: dict[str, tuple[int, int, int]] = {
    "green": GREEN, "amber": AMBER, "red": RED, "neutral": GREY,
}
_BAND_TINT: dict[str, tuple[int, int, int]] = {
    "green": GREEN_TINT, "amber": AMBER_TINT, "red": RED_TINT, "neutral": LIGHT,
}

# Severity -> (ink, tint) for finding-card stripes and badges.
_SEV_STYLE: dict[str, tuple[tuple[int, int, int], tuple[int, int, int]]] = {
    "critical": (RED, RED_TINT),
    "major": (AMBER, AMBER_TINT),
    "minor": (VIOLET, VIOLET_TINT),
}
_SEV_ORDER = ("critical", "major", "minor")


def _rag(pct: float | None) -> tuple[int, int, int]:
    if pct is None:
        return GREY
    return GREEN if pct >= 85 else (AMBER if pct >= 70 else RED)


# ── Chart primitives ─────────────────────────────────────────────────────
#
# Drawn with fpdf2 vector calls rather than an embedded chart image. The PDF is
# produced by fpdf2 (pure Python, no headless browser and no system deps —
# that is the whole reason this renderer exists), so an SVG or canvas chart
# library is not on the table: there is no browser to run it. Vector primitives
# also stay sharp at print resolution and add no bytes worth mentioning, which
# a rasterised chart would.
#
# NOTE on page breaks: fpdf2's auto-break fires on TEXT flow, not on rect/arc
# calls. Every helper here is therefore given its height up front and each
# caller checks `_room()` before drawing — a chart half-drawn across a page
# boundary is the one failure mode this section can produce.

def _room(pdf: "_Report", needed: float) -> None:
    """Start a new page unless `needed` mm remain above the bottom margin."""
    if pdf.get_y() + needed > pdf.h - pdf.b_margin:
        pdf.add_page()


def _donut(
    pdf: "_Report", cx: float, cy: float, radius: float, pct: float | None,
    colour: tuple[int, int, int], *, thickness: float = 7.0,
) -> None:
    """A radial gauge: grey track ring, then the score swept over it.

    Built from quadrilateral segments rather than fpdf2's `solid_arc`. That is
    not stubbornness — `solid_arc` interprets its x/y as the arc origin in a way
    that does not agree with `ellipse`'s bounding-box origin, so a sweep drawn
    at the same coordinates as the track lands off-centre and renders the
    COMPLEMENT of the angle asked for (88% drew as a 43° wedge). Trigonometry
    here is a dozen lines, is exact, and cannot be changed underneath us by a
    library upgrade — worth it for the one number on page 1 a reader trusts
    most.

    Angles run clockwise from 12 o'clock, which is what a gauge means by "full".
    """
    inner = max(0.5, radius - thickness)
    segments = 120  # 3° apiece — smooth at print resolution

    def _ring(from_frac: float, to_frac: float, colour_: tuple[int, int, int]) -> None:
        if to_frac <= from_frac:
            return
        pdf.set_fill_color(*colour_)
        # Stroke each segment in its own fill colour ("DF"). Fill-only leaves a
        # hairline of white between adjacent polygons where the rasteriser
        # anti-aliases their shared edge, which printed the ring as a barber
        # pole. The stroke covers its own seam and changes nothing else.
        pdf.set_draw_color(*colour_)
        n = max(1, round((to_frac - from_frac) * segments))
        step = (to_frac - from_frac) * 2 * math.pi / n
        base = from_frac * 2 * math.pi
        for i in range(n):
            a0, a1 = base + i * step, base + (i + 1) * step
            pdf.polygon(
                [
                    (cx + radius * math.sin(a0), cy - radius * math.cos(a0)),
                    (cx + radius * math.sin(a1), cy - radius * math.cos(a1)),
                    (cx + inner * math.sin(a1), cy - inner * math.cos(a1)),
                    (cx + inner * math.sin(a0), cy - inner * math.cos(a0)),
                ],
                style="DF",
            )

    prev_lw = pdf.line_width
    pdf.set_line_width(0.08)
    _ring(0.0, 1.0, HAIRLINE)
    if pct is not None and pct > 0:
        _ring(0.0, max(0.0, min(100.0, float(pct))) / 100.0, colour)
    pdf.set_line_width(prev_lw)
    pdf.set_draw_color(*HAIRLINE)


def _hbar(
    pdf: "_Report", x: float, y: float, width: float, height: float,
    pct: float | None, colour: tuple[int, int, int],
) -> None:
    """Horizontal bar on a full-width track. `pct is None` (nothing assessed)
    draws the track only — a neutral empty bar, never a red 0%, which would
    report 'not assessed' as 'failed everything'."""
    pdf.set_fill_color(*HAIRLINE)
    pdf.rect(x, y, width, height, style="F")
    if pct is None:
        return
    filled = max(0.0, min(100.0, float(pct))) / 100.0 * width
    if filled > 0:
        pdf.set_fill_color(*colour)
        pdf.rect(x, y, filled, height, style="F")


def _chip(
    pdf: "_Report", x: float, y: float, text: str,
    ink: tuple[int, int, int], tint: tuple[int, int, int], *, size: float = 6.5,
) -> float:
    """A pill. Returns its width so callers can flow chips along a row."""
    pdf.set_font("Helvetica", "B", size)
    w = pdf.get_string_width(_s(text)) + 3.6
    pdf.set_fill_color(*tint)
    pdf.rect(x, y, w, size * 0.62, style="F")
    pdf.set_text_color(*ink)
    pdf.set_xy(x, y)
    pdf.cell(w, size * 0.62, text, border=0, ln=0, align="C")
    pdf.set_text_color(0, 0, 0)
    return w


def _banner(pdf: "_Report", text: str, ink: tuple[int, int, int],
            tint: tuple[int, int, int], *, height: float = 11.0) -> None:
    """Full-width callout with a solid leading rule. Used for the critical-fail
    gate, which the report has always stated in prose — this only stops it being
    a sentence a reader can skim past."""
    _room(pdf, height + 3)
    y = pdf.get_y()
    pdf.set_fill_color(*tint)
    pdf.rect(10, y, 190, height, style="F")
    pdf.set_fill_color(*ink)
    pdf.rect(10, y, 1.8, height, style="F")
    pdf.set_font("Helvetica", "B", 10)
    pdf.set_text_color(*ink)
    pdf.set_xy(14, y + (height - 5) / 2)
    pdf.cell(184, 5, text, border=0, ln=1)
    pdf.set_text_color(0, 0, 0)
    pdf.set_y(y + height + 3)


def _stat(pdf: "_Report", x: float, y: float, w: float, label: str, value: str,
          ink: tuple[int, int, int]) -> None:
    pdf.set_fill_color(248, 250, 252)
    pdf.rect(x, y, w, 14, style="F")
    pdf.set_font("Helvetica", "B", 13)
    pdf.set_text_color(*ink)
    pdf.set_xy(x + 3, y + 1.5)
    pdf.cell(w - 6, 7, value, border=0, ln=1)
    pdf.set_font("Helvetica", "", 6.5)
    pdf.set_text_color(*SLATE)
    pdf.set_xy(x + 3, y + 8.2)
    pdf.cell(w - 6, 4, label.upper(), border=0, ln=1)
    pdf.set_text_color(0, 0, 0)


class _Report(FPDF):
    def __init__(self, report_type: str, audit_code: str, snapshot_hash: str):
        super().__init__(orientation="P", unit="mm", format="A4")
        self.report_type = (report_type or "").upper()
        self.audit_code = audit_code
        self.snapshot_hash = snapshot_hash
        self.set_auto_page_break(auto=True, margin=20)
        self.set_title(_s(f"Audit Report {audit_code}"))

    # Centralised sanitisation — fpdf2 core fonts are latin-1 only.
    def cell(self, *a, **k):  # type: ignore[override]
        if len(a) >= 3 and isinstance(a[2], str):
            a = (a[0], a[1], _s(a[2])) + a[3:]
        for key in ("txt", "text"):
            if key in k and isinstance(k[key], str):
                k[key] = _s(k[key])
        return super().cell(*a, **k)

    def multi_cell(self, *a, **k):  # type: ignore[override]
        if len(a) >= 3 and isinstance(a[2], str):
            a = (a[0], a[1], _s(a[2])) + a[3:]
        for key in ("txt", "text"):
            if key in k and isinstance(k[key], str):
                k[key] = _s(k[key])
        return super().multi_cell(*a, **k)

    def text(self, x, y, txt=""):  # type: ignore[override]
        return super().text(x, y, _s(txt))

    def header(self):
        if self.page_no() == 1:
            return
        self.set_font("Helvetica", "B", 9)
        self.set_text_color(*NAVY)
        self.cell(0, 8, f"SafeOps360 — Audit Report {self.audit_code}", border=0, ln=0, align="L")
        self.cell(0, 8, self.report_type, border=0, ln=1, align="R")
        self.set_draw_color(*LIGHT)
        self.line(10, 18, 200, 18)
        self.ln(4)
        if self.report_type == "INTERIM":
            self._watermark()

    def _watermark(self):
        self.set_text_color(230, 210, 210)
        self.set_font("Helvetica", "B", 50)
        with self.rotation(45, x=105, y=150):
            self.text(55, 150, "PROVISIONAL")
        self.set_text_color(0, 0, 0)

    def footer(self):
        self.set_y(-15)
        self.set_font("Helvetica", "I", 7)
        self.set_text_color(*GREY)
        # Explicit thirds, not `w=0`. A zero width means "run to the right
        # margin", so all three cells started at the right margin and printed
        # on top of each other — the shipped samples read "hashRage2 of 4".
        # Pre-existing, and every page of this report carries it.
        third = (self.w - self.l_margin - self.r_margin) / 3
        self.cell(third, 6, "CONFIDENTIAL", border=0, ln=0, align="L")
        self.cell(third, 6, f"Page {self.page_no()} of {{nb}}", border=0, ln=0, align="C")
        self.cell(third, 6, f"hash {self.snapshot_hash[:12]}", border=0, ln=1, align="R")


def _h(pdf: _Report, text: str):
    """Section heading, numbered by RENDER ORDER.

    The numbers used to be hardcoded into the strings ("1. Executive Summary" …
    "12. Record Integrity") while six of the twelve sections are conditional, so
    a report that suppressed Independence and Clause Index printed 8 → 10 → 12
    and read as though pages were missing. Counting here means the number can
    only ever describe what actually rendered — on interim and final alike.
    """
    pdf._section_no = getattr(pdf, "_section_no", 0) + 1
    # A section heading is never the last thing on a page. fpdf2's auto-break
    # fires on the text that FOLLOWS, so a heading could sit alone at the foot
    # while its first row began overleaf — which reads as a section that failed
    # to render.
    _room(pdf, 26)
    pdf.set_font("Helvetica", "B", 12)
    pdf.set_text_color(*PURPLE)
    pdf.cell(0, 8, f"{pdf._section_no}. {text}", border=0, ln=1)
    pdf.set_text_color(0, 0, 0)
    pdf.set_font("Helvetica", "", 10)


def _human(value: str | None) -> str:
    """`integrated_compliance_audit` -> `Integrated Compliance Audit`."""
    if not value:
        return "—"
    return " ".join(w.capitalize() for w in str(value).replace("_", " ").split())


def _who(user_id: str | None, names: dict[str, str] | None) -> str:
    """A person's name, never a raw id.

    Falls back to nothing rather than printing a cuid: an unresolved id in a
    report is noise a reader cannot act on, and a blank is honest.
    """
    if not user_id:
        return ""
    return (names or {}).get(user_id) or ""


def _dt(value: str | None, *, with_time: bool = True) -> str:
    """ISO -> `22 Jul 2026, 09:00 IST`.

    The cover already printed `Generated: … UTC` beside raw ISO strings like
    `2026-07-22T03:30:00`, i.e. two conventions on one page. Everything the
    report renders now goes through here, in the tenant's timezone.
    """
    if not value:
        return "—"
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return str(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    local = dt.astimezone(REPORT_TZ)
    return local.strftime("%d %b %Y, %H:%M " + REPORT_TZ_LABEL) if with_time else local.strftime("%d %b %Y")


def _sub(pdf: _Report, text: str) -> None:
    """Sub-heading inside a numbered section — deliberately NOT `_h`, which
    would consume a section number and renumber the whole document."""
    _room(pdf, 12)
    pdf.set_font("Helvetica", "B", 8.5)
    pdf.set_text_color(*SLATE)
    pdf.set_x(10)
    pdf.cell(0, 5, text.upper(), border=0, ln=1)
    pdf.set_text_color(0, 0, 0)
    pdf.ln(1)


def _insight_summary(pdf: _Report, snap: dict[str, Any]) -> None:
    """Section 1 — the insight layer, read off the frozen snapshot.

    Renders `snapshot["insights"]` and computes NOTHING: the block was built by
    `services/insights/rules_audit_report` at issue and hashed into the
    snapshot, so this page says the same thing every time it is regenerated,
    for as long as the report exists. A report frozen before the insight layer
    shipped has no block and simply skips the section — an immutable snapshot
    cannot be backfilled, and a reconstructed insight would be a claim about a
    reading nobody actually took.
    """
    ins = snap.get("insights") or {}
    if not ins:
        return

    pdf.add_page()
    _h(pdf, "Insight Summary")

    # ── The gate, stated as a banner rather than buried in prose ──────────
    banner = ins.get("criticalBanner") or {}
    if banner.get("headline"):
        _banner(pdf, banner["headline"], RED, RED_TINT)
        codes = ", ".join(banner.get("codes") or [])
        if codes:
            pdf.set_font("Helvetica", "", 7.5)
            pdf.set_text_color(*SLATE)
            pdf.set_x(14)
            pdf.multi_cell(186, 4, _s(f"Critical non-conformances: {codes}"))
            pdf.set_text_color(0, 0, 0)
            pdf.ln(2)

    # ── Gauge + headline counts ───────────────────────────────────────────
    g = ins.get("gauge") or {}
    _room(pdf, 50)
    top = pdf.get_y()
    band = str(g.get("displayBand") or "neutral")
    ink = _BAND_INK.get(band, GREY)

    cx, cy, r = 32.0, top + 19.0, 17.0
    _donut(pdf, cx, cy, r, g.get("pct"), ink)
    pdf.set_font("Helvetica", "B", 15)
    pdf.set_text_color(*ink)
    pdf.set_xy(cx - r, cy - 6.5)
    pdf.cell(r * 2, 7, f"{g.get('pct')}%" if g.get("pct") is not None else "n/a",
             border=0, ln=1, align="C")
    pdf.set_font("Helvetica", "B", 6)
    pdf.set_text_color(*SLATE)
    pdf.set_xy(cx - r, cy + 0.8)
    pdf.cell(r * 2, 4, "OVERALL", border=0, ln=1, align="C")
    # The verdict word, under the dial — the dial is the picture, this is the
    # result, and the result is the one a reader must not have to infer.
    pdf.set_font("Helvetica", "B", 8)
    pdf.set_text_color(*ink)
    pdf.set_xy(10, top + 38)
    pdf.cell(44, 4, str(g.get("result") or "-").replace("_", " "), border=0, ln=1, align="C")
    # What the dial is made of. Printed under it so the headline number can be
    # reconciled by hand against the category table further down.
    if g.get("scoreAllotted"):
        pdf.set_font("Helvetica", "", 6.5)
        pdf.set_text_color(*SLATE)
        pdf.set_xy(10, top + 42)
        pdf.cell(44, 3.5, f"{g['scoreObtained']} of {g['scoreAllotted']} points",
                 border=0, ln=1, align="C")
    pdf.set_text_color(0, 0, 0)

    capa = ins.get("capaStrip") or {}
    repeats = ins.get("repeats") or {}
    stats = [
        ("Assessed", f"{snap.get('checkpointsAssessed', 0)}/{snap.get('checkpointsTotal', 0)}", INK),
        ("Critical NC", str(snap.get("criticalFailures", 0)),
         RED if snap.get("criticalFailures") else INK),
        ("Major NC", str(snap.get("majorFailures", 0)),
         AMBER if snap.get("majorFailures") else INK),
        ("Repeat NC", str(repeats.get("count", 0)),
         RED if repeats.get("count") else INK),
        ("CAPAs open", f"{capa.get('open', 0)}/{capa.get('total', 0)}",
         AMBER if capa.get("open") else INK),
        ("CAPAs overdue", str(capa.get("overdue", 0)),
         RED if capa.get("overdue") else INK),
    ]
    sx, sw, gap = 58.0, 45.3, 1.5
    for i, (label, value, tone) in enumerate(stats):
        col, row = i % 3, i // 3
        _stat(pdf, sx + col * (sw + gap), top + row * 16, sw, label, value, tone)

    # Clear of the donut (bottom top+36), its verdict word (top+38) and the
    # points line under it (top+42).
    pdf.set_y(top + 46.5)

    # The rule behind the verdict. A number without its rule is not a result —
    # and this is the same `gate.explanation` the cover prints, not a restatement.
    if g.get("explanation"):
        pdf.set_font("Helvetica", "", 7.5)
        pdf.set_text_color(*SLATE)
        pdf.set_x(10)
        pdf.multi_cell(190, 4, _s(g["explanation"]))
        pdf.set_text_color(0, 0, 0)
    if g.get("coverageLabel"):
        pdf.set_font("Helvetica", "I", 7.5)
        pdf.set_text_color(*GREY)
        pdf.set_x(10)
        pdf.multi_cell(190, 4, _s(
            f"{g['coverageLabel']} — no overall grade is issued for this report."))
        pdf.set_text_color(0, 0, 0)
    pdf.ln(3)

    # ── Compliance by discipline (or by department) ───────────────────────
    #
    # The axis is frozen into the snapshot at issue, so this document keeps
    # naming what its own rows are even after the library is restructured.
    # Absent reads as "discipline" — every report issued before departments.
    _axis = "department" if snap.get("scopeAxis") == "DEPARTMENT" else "discipline"
    chart = [c for c in (ins.get("categoryChart") or []) if c.get("total")]
    if chart:
        _room(pdf, 12 + min(len(chart), 3) * 5.6)
        _sub(pdf, f"Compliance by {_axis}")
        for c in chart[:14]:
            _room(pdf, 9)
            y = pdf.get_y()
            pct = c.get("pct")
            c_ink = _BAND_INK.get(str(c.get("band") or "neutral"), GREY)
            pdf.set_font("Helvetica", "", 8)
            pdf.set_text_color(*INK)
            pdf.set_xy(10, y)
            pdf.cell(44, 4.5, _s(str(c.get("name"))[:30]), border=0, ln=0)
            _hbar(pdf, 56, y + 1.1, 78, 2.6, pct, c_ink)
            pdf.set_font("Helvetica", "B", 8)
            pdf.set_text_color(*c_ink)
            pdf.set_xy(136, y)
            pdf.cell(14, 4.5, f"{pct}%" if pct is not None else "n/a", border=0, ln=0, align="R")
            # The arithmetic behind the percentage, so a reader can check it
            # rather than take it on trust — and the FULL outcome split.
            # This line used to read "32P 4F / 40", silently omitting Partial
            # even though a partial contributes points to the percentage beside
            # it, so the counts never added up to the total.
            #
            # Columns are sized for the LONGEST string each can hold —
            # "32P 3Pa 4F 1NA" is ~20mm at 7pt, and a right-aligned cell
            # narrower than its text overflows LEFT into its neighbour, which is
            # how "101/117 pts" and the counts first collided.
            pdf.set_font("Helvetica", "", 7)
            pdf.set_text_color(*SLATE)
            pdf.set_xy(151, y)
            pdf.cell(20, 4.5, f"{c.get('scoreObtained', 0)}/{c.get('scoreAllotted', 0)} pts",
                     border=0, ln=0, align="R")
            pdf.set_xy(172, y)
            pdf.cell(28, 4.5, _s(
                f"{c.get('passed', 0)}P {c.get('partial', 0)}Ptl {c.get('failed', 0)}F"
                + (f" {c['na']}NA" if c.get("na") else "")
                + f" / {c.get('total', 0)}"), border=0, ln=1, align="R")
            pdf.set_text_color(0, 0, 0)
            pdf.set_y(y + 5.6)
        # One sentence that removes the question the chart otherwise raises:
        # what IS this percentage? Without it a reader has to guess whether 85%
        # means "85% of checkpoints passed" — which it does not.
        pdf.ln(1)
        pdf.set_font("Helvetica", "I", 6.8)
        pdf.set_text_color(*GREY)
        pdf.set_x(10)
        pdf.multi_cell(190, 3.4, _s(
            "Score = points earned / points available. Each assessed checkpoint is worth 3 "
            "points: Effective 3, Some Improvement Needed 2, Major Improvement Needed 1, "
            "Unsatisfactory 0, and a repeat finding -1. N/A checkpoints are excluded. This is "
            "the same calculation as the overall score above."))
        pdf.set_text_color(0, 0, 0)
        pdf.ln(2)

    # ── Systemic patterns ─────────────────────────────────────────────────
    patterns = ins.get("patterns") or []
    if patterns:
        _room(pdf, 26)
        _sub(pdf, "Systemic patterns")
        for p in patterns:
            sev = str(p.get("severity") or "info")
            p_ink = {"critical": RED, "high": AMBER, "watch": VIOLET}.get(sev, SLATE)
            # Measure BOTH the headline and the body before drawing the card
            # ground: the rectangle is painted first, so an under-measured
            # height shows as text spilling past its own background. Headlines
            # are capped at 90 chars by `templates.fill` and wrap to one line in
            # practice, but measuring costs nothing and does not assume that.
            pdf.set_font("Helvetica", "B", 8.5)
            head_h = len(pdf.multi_cell(184, 4.4, _s(p.get("headline") or ""),
                                        dry_run=True, output="LINES"))
            pdf.set_font("Helvetica", "", 7.5)
            body_h = len(pdf.multi_cell(178, 4, _s(p.get("evidence") or ""), dry_run=True,
                                        output="LINES"))
            action_h = 0
            if p.get("suggestedAction"):
                pdf.set_font("Helvetica", "I", 7.5)
                action_h = len(pdf.multi_cell(178, 4, _s(f"-> {p['suggestedAction']}"),
                                              dry_run=True, output="LINES")) * 4
            need = 3.2 + head_h * 4.4 + body_h * 4 + action_h
            _room(pdf, need + 3)
            y = pdf.get_y()
            pdf.set_fill_color(250, 250, 252)
            pdf.rect(10, y, 190, need, style="F")
            pdf.set_fill_color(*p_ink)
            pdf.rect(10, y, 1.6, need, style="F")

            pdf.set_font("Helvetica", "B", 8.5)
            pdf.set_text_color(*p_ink)
            pdf.set_xy(14, y + 1.6)
            pdf.multi_cell(184, 4.4, _s(p.get("headline") or ""))
            pdf.set_font("Helvetica", "", 7.5)
            pdf.set_text_color(*SLATE)
            pdf.set_x(14)
            pdf.multi_cell(178, 4, _s(p.get("evidence") or ""))
            if p.get("suggestedAction"):
                pdf.set_font("Helvetica", "I", 7.5)
                pdf.set_text_color(*VIOLET)
                pdf.set_x(14)
                pdf.multi_cell(178, 4, _s(f"-> {p['suggestedAction']}"))
            pdf.set_text_color(0, 0, 0)
            pdf.set_y(y + need + 2)
        pdf.ln(1)
    elif ins.get("patternNote"):
        # Say why there is nothing here. A silent gap where patterns should be
        # reads as a rendering failure; "not enough findings to infer from" is
        # a finding of its own.
        pdf.set_font("Helvetica", "I", 7.5)
        pdf.set_text_color(*GREY)
        pdf.set_x(10)
        pdf.multi_cell(190, 4, _s(ins["patternNote"]))
        pdf.set_text_color(0, 0, 0)
        pdf.ln(2)

    # ── Repeat non-conformances ───────────────────────────────────────────
    if repeats.get("count"):
        _banner(pdf, repeats["headline"], RED, RED_TINT, height=9)
        for it in repeats.get("items") or []:
            _room(pdf, 10)
            pdf.set_font("Helvetica", "B", 7.5)
            pdf.set_text_color(*RED)
            pdf.set_x(14)
            pdf.multi_cell(186, 4, _s(
                f"{it.get('checkpointCode')}   {it.get('statusLabel')}"
                f"   -   {it.get('discipline')}"
                + (f"   -   CAPA {it['capaNumber']}" if it.get("capaNumber") else "")))
            pdf.set_font("Helvetica", "", 7.5)
            pdf.set_text_color(*INK)
            pdf.set_x(16)
            pdf.multi_cell(184, 4, _s(str(it.get("question") or "-")))
            if it.get("observation"):
                pdf.set_font("Helvetica", "I", 7)
                pdf.set_text_color(*SLATE)
                pdf.set_x(16)
                pdf.multi_cell(184, 3.8, _s(str(it["observation"])))
            pdf.set_text_color(0, 0, 0)
            pdf.ln(1.5)
        if repeats.get("truncated"):
            pdf.set_font("Helvetica", "I", 7)
            pdf.set_text_color(*GREY)
            pdf.set_x(14)
            pdf.multi_cell(186, 4, _s(
                f"{repeats['truncated']} further repeat finding(s) appear in the findings "
                "register below."))
            pdf.set_text_color(0, 0, 0)
        pdf.ln(1)

    # ── CAPA status strip ─────────────────────────────────────────────────
    chips = capa.get("chips") or []
    if chips:
        # Reserve the heading AND the chips together — `_sub` would otherwise
        # fit at the foot of a page and leave "CAPA STATUS" stranded over
        # nothing, which reads as a chart that failed to render.
        _room(pdf, 12 + (len(chips) // 4 + 1) * 6)
        _sub(pdf, "CAPA status")
        x, y = 10.0, pdf.get_y()
        for c in chips:
            status = str(c.get("status") or "OPEN").upper()
            closed = status in ("CLOSED", "VERIFIED", "CLOSED_RECURRED")
            c_ink, c_tint = (GREEN, GREEN_TINT) if closed else (AMBER, AMBER_TINT)
            label = f"{c.get('capaNumber')}  {c.get('checkpointCode')}  {status.title()}"
            pdf.set_font("Helvetica", "B", 6.5)
            w = pdf.get_string_width(_s(label)) + 3.6
            if x + w > 200:
                x, y = 10.0, y + 6.0
                _room(pdf, 8)
            _chip(pdf, x, y, label, c_ink, c_tint)
            x += w + 2
        pdf.set_y(y + 8)
        if capa.get("truncated"):
            pdf.set_font("Helvetica", "I", 7)
            pdf.set_text_color(*GREY)
            pdf.set_x(10)
            pdf.multi_cell(190, 4, _s(
                f"{capa['truncated']} further linked CAPA(s) — the full list is in the CAPA "
                "summary and findings register below."))
            pdf.set_text_color(0, 0, 0)

    # The provenance of this page, stated where it is read. Without it a reader
    # cannot tell a computed summary from an auditor's written opinion, and the
    # difference matters to whoever has to defend the report.
    pdf.ln(2)
    pdf.set_font("Helvetica", "I", 6.5)
    pdf.set_text_color(*GREY)
    pdf.set_x(10)
    pdf.multi_cell(190, 3.6, _s(
        "This summary is computed by fixed rules from the findings recorded below and was "
        "frozen into this report at issue. It re-presents the register; it does not add to it. "
        "No judgement here overrides the auditor's."))
    pdf.set_text_color(0, 0, 0)


def render_audit_report_pdf(
    report: dict[str, Any],
    generated_by_name: str = "—",
    register: list[dict[str, Any]] | None = None,
    user_names: dict[str, str] | None = None,
    register_truncated: int = 0,
) -> bytes:
    """Render the report.

    `register` is the full checkpoint register, passed in by the caller because
    it is deliberately NOT stored in the snapshot (a 1,500-checkpoint audit
    would bloat every read of the report row). `user_names` resolves the owner
    and actor ids the register carries, so the PDF never prints a raw cuid.
    """
    snap: dict[str, Any] = report.get("snapshot") or {}
    rtype = report.get("reportType") or snap.get("reportType") or "INTERIM"
    code = snap.get("auditCode") or report.get("reportCode") or "—"
    pdf = _Report(rtype, code, report.get("id", ""))
    pdf.alias_nb_pages()

    # ── Cover page ──
    pdf.add_page()
    pdf.set_fill_color(*NAVY)
    pdf.rect(0, 0, 210, 45, style="F")
    pdf.set_y(14)
    pdf.set_text_color(255, 255, 255)
    pdf.set_font("Helvetica", "B", 22)
    pdf.cell(0, 12, "SafeOps360", border=0, ln=1, align="C")
    pdf.set_font("Helvetica", "", 12)
    pdf.cell(0, 7, "Audit & Compliance Report", border=0, ln=1, align="C")
    pdf.set_text_color(0, 0, 0)
    pdf.ln(20)
    pdf.set_font("Helvetica", "B", 16)
    pdf.set_x(10)
    pdf.multi_cell(0, 9, snap.get("title") or "Audit Report", align="C")

    # ── Which management system this document reports on ─────────────────
    #
    # A department audit is conducted once and reported TWICE — an IMS document
    # (ISO 9001/14001/45001) and an EnMS one (ISO 50001). Every number in this
    # snapshot is scoped to its own stream, so a cover that did not name the
    # stream would let one half of the pair be read as the whole audit. Absent
    # on every single-report audit, which is what `reportStream: null` means.
    if snap.get("reportStreamTitle"):
        pdf.ln(1)
        pdf.set_font("Helvetica", "B", 12)
        pdf.set_text_color(*NAVY)
        pdf.cell(0, 7, _s(snap["reportStreamTitle"]), border=0, ln=1, align="C")
        if snap.get("reportStreamStandards"):
            pdf.set_font("Helvetica", "", 10)
            pdf.cell(0, 6, _s(snap["reportStreamStandards"]), border=0, ln=1, align="C")
        pdf.set_text_color(0, 0, 0)

    pdf.ln(2)
    pdf.set_font("Helvetica", "B", 13)
    badge = RED if rtype.upper() == "INTERIM" else GREEN
    pdf.set_text_color(*badge)
    pdf.cell(0, 8, f"{rtype.upper()} REPORT" + (" — PROVISIONAL, SUBJECT TO CHANGE" if rtype.upper() == "INTERIM" else ""), border=0, ln=1, align="C")
    pdf.set_text_color(0, 0, 0)
    pdf.ln(6)
    pdf.set_font("Helvetica", "", 11)
    now = datetime.now(REPORT_TZ).strftime("%d %b %Y, %H:%M " + REPORT_TZ_LABEL)
    for label, val in [
        ("Audit Code", code),
        # `integrated_compliance_audit` is a storage key, not a label.
        ("Audit Type", _human(snap.get("auditType"))),
        # The snapshot has carried the resolved `plantName` since WP-12; this
        # renderer was still printing the raw CUID from `siteId`.
        ("Site", snap.get("plantName") or snap.get("siteId") or "—"),
        ("Planned", _dt(snap.get("plannedDate"), with_time=False)),
        ("Closed", _dt(snap.get("closedAt"))),
        ("Generated", now), ("Generated by", generated_by_name),
    ]:
        pdf.cell(50, 7, f"{label}:", border=0, ln=0)
        pdf.cell(0, 7, _s(str(val))[:80], border=0, ln=1)
    pdf.ln(4)

    # ── Headline verdict, or an honest refusal to give one ──────────────
    # Below the coverage floor no grade renders at all: "100.0% (CONFORMING)"
    # over 1 of 82 checkpoints is the 78.9%-over-0-of-82 defect with a
    # disclaimer nobody reading the cover will see. The replacement occupies
    # the same position and weight — stated, not demoted.
    pct = snap.get("overallScorePct")
    grade = snap.get("grade") or {}
    pdf.set_font("Helvetica", "B", 14)
    if grade and not grade.get("showGrade", True):
        pdf.set_text_color(120, 120, 120)
        pdf.cell(0, 10, _s(f"{grade.get('label', 'Insufficient coverage')} - no grade issued"),
                 border=0, ln=1)
        pdf.set_text_color(0, 0, 0)
        pdf.set_font("Helvetica", "", 9)
        pdf.multi_cell(0, 5, _s(
            f"Coverage is below the {grade.get('threshold', 20):g}% minimum required for a "
            "compliance grade. No overall percentage or conformance verdict is issued for this "
            "report."
        ))
    else:
        pdf.set_text_color(*_rag(pct))
        assessed_frac = (
            f"   [{grade.get('assessed')} of {grade.get('applicable')} assessed]"
            if grade else ""
        )
        pdf.cell(0, 10, _s(
            f"Overall compliance: {pct if pct is not None else '-'}%   "
            f"({snap.get('overallResult') or '-'}){assessed_frac}"
        ), border=0, ln=1)
        pdf.set_text_color(0, 0, 0)
        # The rule behind the verdict (F-22) — a number without its rule is not
        # a result.
        gate = snap.get("gate") or {}
        if gate.get("explanation"):
            pdf.set_font("Helvetica", "", 9)
            pdf.multi_cell(0, 5, _s(gate["explanation"]))
    pdf.set_text_color(0, 0, 0)

    # ── Insight summary (Section 1) ──
    # Placed immediately after the cover, ahead of the Executive Summary: the
    # register below is the record, but a reader who only gets one page should
    # get the one that says what the audit found.
    _insight_summary(pdf, snap)

    # ── Executive summary ──
    # Flows on from the insight summary rather than forcing a page break. It
    # used to break because it followed the cover; now that Section 1 precedes
    # it, a forced break stranded the CAPA strip alone on a near-empty page.
    _room(pdf, 40)
    pdf.ln(2)
    _h(pdf, "Executive Summary")
    pdf.set_x(10)
    # "Open iterations" counts findings awaiting a response. It used to be
    # derived from `not _is_terminal(...)`, which made every UNASSESSED
    # checkpoint an open iteration — hence "Open iterations 81" on an audit
    # whose Findings Register correctly read 0. Not-yet-started is now reported
    # as its own number, because that is what a reader actually wants to know.
    _not_assessed = snap.get("notAssessedCount")
    pdf.multi_cell(0, 6, (
        f"Checkpoints assessed: {snap.get('checkpointsAssessed', 0)} of {snap.get('checkpointsTotal', 0)}. "
        f"Pass {snap.get('passCount', 0)}, Fail {snap.get('failCount', 0)}, Partial {snap.get('partialCount', 0)}, N/A {snap.get('naCount', 0)}. "
        f"Failures by severity — Critical {snap.get('criticalFailures', 0)}, Major {snap.get('majorFailures', 0)}, Minor {snap.get('minorFailures', 0)}. "
        f"Findings awaiting response: {snap.get('openIterationsCount', 0)} ({snap.get('criticalOpenCount', 0)} critical)."
        + (f" Not yet assessed: {_not_assessed}." if _not_assessed else "")
    ))
    pdf.ln(3)

    # ── Scope, methodology & limitations (WP-12) ──
    # A certification body reads this BEFORE the numbers. The limitations list is
    # what earns trust: a report that states what it could not establish is more
    # credible than one implying total coverage.
    meth = snap.get("methodology") or {}
    if meth:
        _h(pdf, "Scope, Methodology & Limitations")
        pdf.set_font("Helvetica", "B", 9)
        pdf.cell(0, 5, "Audit criteria", border=0, ln=1)
        pdf.set_font("Helvetica", "", 9)
        pdf.set_x(10)
        pdf.multi_cell(0, 5, ", ".join(meth.get("criteria") or ["Not specified"]))
        if meth.get("scopeDescription"):
            pdf.ln(1)
            pdf.set_font("Helvetica", "B", 9)
            pdf.cell(0, 5, "Scope", border=0, ln=1)
            pdf.set_font("Helvetica", "", 9)
            pdf.set_x(10)
            pdf.multi_cell(0, 5, _s(meth["scopeDescription"]))
        pdf.ln(1)
        pdf.set_font("Helvetica", "B", 9)
        pdf.cell(0, 5, "Method", border=0, ln=1)
        pdf.set_font("Helvetica", "", 9)
        pdf.set_x(10)
        pdf.multi_cell(0, 5, _s(meth.get("method") or "—"))
        pdf.ln(1)
        pdf.set_font("Helvetica", "B", 9)
        pdf.cell(0, 5, "Limitations", border=0, ln=1)
        pdf.set_font("Helvetica", "", 9)
        for lim in meth.get("limitations") or []:
            pdf.set_x(10)
            pdf.multi_cell(0, 5, f"-  {_s(lim)}")
        pdf.ln(2)

    # ── Auditor independence (docs/cams/09 §2.1.6) ──
    # Asserts absence explicitly. A reader must be able to tell "none issued"
    # from "not tracked", and only a sentence does that.
    ind = snap.get("independence") or {}
    if ind:
        _h(pdf, "Auditor Independence")
        pdf.set_font("Helvetica", "", 9)
        pdf.set_x(10)
        pdf.multi_cell(0, 5, _s(ind.get("statement") or "—"))
        for w in ind.get("waivers") or []:
            pdf.ln(1)
            pdf.set_font("Helvetica", "B", 8)
            pdf.set_text_color(*RED)
            pdf.set_x(10)
            pdf.multi_cell(0, 5, f"Waiver — {_s(w.get('subject'))}")
            pdf.set_text_color(0, 0, 0)
            pdf.set_font("Helvetica", "", 8)
            if w.get("conflict"):
                pdf.set_x(10)
                pdf.multi_cell(0, 4.5, f"Conflict: {_s(w['conflict'])}")
            pdf.set_x(10)
            pdf.multi_cell(0, 4.5, f"Justification: {_s(w.get('justification'))}")
            pdf.set_x(10)
            pdf.multi_cell(0, 4.5, f"Approved by {_s(w.get('approvedBy'))} on {_s(w.get('approvedAt'))}")
        pdf.ln(2)

    # ── Opening & closing meetings (ISO 19011 §6.4) ──
    mtg = snap.get("meetings") or {}
    if mtg:
        _h(pdf, "Opening & Closing Meetings")
        pdf.set_font("Helvetica", "", 9)
        for key, label in (("opening", "Opening meeting"), ("closing", "Closing meeting")):
            m = mtg.get(key) or {}
            pdf.set_font("Helvetica", "B", 9)
            pdf.cell(0, 5, label, border=0, ln=1)
            pdf.set_font("Helvetica", "", 8)
            if not m.get("recorded"):
                # Never assert a meeting the product has no record of.
                pdf.set_x(10)
                pdf.multi_cell(0, 4.5, f"No {label.lower()} was recorded.")
            else:
                pdf.set_x(10)
                pdf.multi_cell(0, 4.5, f"Held: {_s(m.get('heldAt'))}")
                names = ", ".join(a.get("name", "") for a in (m.get("attendees") or []))
                pdf.set_x(10)
                pdf.multi_cell(0, 4.5, f"Attendees: {_s(names) or '-'}")
                if key == "opening" and m.get("scopeConfirmed"):
                    pdf.set_x(10)
                    pdf.multi_cell(0, 4.5, "Scope and criteria confirmed with the auditee.")
                if key == "closing":
                    pdf.set_x(10)
                    pdf.multi_cell(
                        0, 4.5,
                        f"Auditee acknowledgement: {_s(m.get('auditeeAcknowledgedBy')) or 'not recorded'}",
                    )
            pdf.ln(1)
        pdf.ln(1)

    # ── Category compliance (RAG) ──
    # Two defects here. (a) The keys were wrong: the snapshot writes
    # `category_name` / `score_pct` (from `_compute_score`) and this read
    # `category` / `scorePct`, so EVERY row rendered "- / -" regardless of data.
    # (b) Rendering ten empty rows at all is a zero-state chart, which Appendix D
    # bans — one honest sentence replaces it until there is something to show.
    cats = snap.get("categoryScores") or []
    if isinstance(cats, dict):
        cats = [{"category_name": k, **(v if isinstance(v, dict) else {"score_pct": v})}
                for k, v in cats.items()]

    def _cat_pct(c: dict[str, Any]) -> float | None:
        assessed = (c.get("passed", 0) or 0) + (c.get("partial", 0) or 0) + (c.get("failed", 0) or 0)
        if not assessed:
            return None
        v = c.get("score_pct", c.get("scorePct", c.get("score")))
        return v if isinstance(v, (int, float)) else None

    scored = [c for c in cats if _cat_pct(c) is not None]
    if scored:
        _h(pdf, "Category-wise Compliance")
        # Charted, then tabulated. The chart is what a reader scans; the table
        # is what an assessor cites. Neither is a summary of the other — they
        # are the same numbers twice, so removing the table to make room for the
        # chart would have cost data for presentation.
        chart = [c for c in ((snap.get("insights") or {}).get("categoryChart") or [])
                 if c.get("total")]
        if chart:
            for c in chart[:20]:
                _room(pdf, 9)
                y = pdf.get_y()
                pct = c.get("pct")
                c_ink = _BAND_INK.get(str(c.get("band") or "neutral"), GREY)
                pdf.set_font("Helvetica", "", 8.5)
                pdf.set_text_color(*INK)
                pdf.set_xy(10, y)
                pdf.cell(52, 5, _s(str(c.get("name"))[:34]), border=0, ln=0)
                _hbar(pdf, 64, y + 1.3, 96, 3.0, pct, c_ink)
                pdf.set_font("Helvetica", "B", 8.5)
                pdf.set_text_color(*c_ink)
                pdf.set_xy(162, y)
                pdf.cell(16, 5, f"{pct}%" if pct is not None else "n/a", border=0, ln=0, align="R")
                pdf.set_font("Helvetica", "", 7)
                pdf.set_text_color(*SLATE)
                pdf.set_xy(180, y)
                pdf.cell(20, 5, f"{c.get('scoreObtained', 0)}/{c.get('scoreAllotted', 0)} pts",
                         border=0, ln=1, align="R")
                pdf.set_text_color(0, 0, 0)
                pdf.set_y(y + 6.2)
            pdf.ln(2)
            _sub(pdf, "Underlying figures")

        _room(pdf, 14)
        pdf.set_font("Helvetica", "B", 8)
        pdf.set_fill_color(*LIGHT)
        # "Points" is the column that makes "Score %" checkable: 106 / 120 is
        # visibly 88.3%. Without it the percentage is a number the reader has to
        # take on trust, and cannot reconcile against their own workbook.
        for w, t, al in ((70, "Category", "L"), (16, "Pass", "C"), (16, "Partial", "C"),
                         (16, "Fail", "C"), (14, "N/A", "C"), (16, "Assessed", "C"),
                         (24, "Points", "C"), (18, "Score %", "C")):
            pdf.cell(w, 6, t, border=1, ln=0, fill=True, align=al)
        pdf.ln()
        pdf.set_font("Helvetica", "", 8)
        for c in scored[:30]:
            _room(pdf, 8)
            name = str(c.get("category_name") or c.get("category") or c.get("name") or "-")[:46]
            sc = _cat_pct(c)
            done = (c.get("passed", 0) or 0) + (c.get("partial", 0) or 0) + (c.get("failed", 0) or 0)
            pdf.cell(70, 5.5, name, border=1, ln=0)
            pdf.cell(16, 5.5, str(c.get("passed", 0) or 0), border=1, ln=0, align="C")
            pdf.cell(16, 5.5, str(c.get("partial", 0) or 0), border=1, ln=0, align="C")
            fail = c.get("failed", 0) or 0
            pdf.set_text_color(*(RED if fail else GREY))
            pdf.cell(16, 5.5, str(fail), border=1, ln=0, align="C")
            pdf.set_text_color(0, 0, 0)
            pdf.cell(14, 5.5, str(c.get("na", 0) or 0), border=1, ln=0, align="C")
            pdf.cell(16, 5.5, f"{done}/{c.get('total', done)}", border=1, ln=0, align="C")
            pdf.cell(24, 5.5,
                     f"{c.get('score_obtained', 0) or 0}/{c.get('score_allotted', 0) or 0}",
                     border=1, ln=0, align="C")
            # Coloured by the SAME band the bar above used, read off the frozen
            # insight block — two colour scales for one number is a defect.
            _b = next((x.get("band") for x in chart
                       if x.get("name") == (c.get("category_name") or c.get("category"))), None)
            pdf.set_text_color(*_BAND_INK.get(str(_b), _rag(sc)))
            pdf.cell(18, 5.5, f"{sc}", border=1, ln=1, align="C")
            pdf.set_text_color(0, 0, 0)
        pdf.ln(3)
    elif cats:
        _h(pdf, "Category-wise Compliance")
        pdf.set_font("Helvetica", "I", 9)
        pdf.set_text_color(*GREY)
        pdf.set_x(10)
        pdf.multi_cell(0, 5, "Category-level compliance will appear once assessment begins.")
        pdf.set_text_color(0, 0, 0)
        pdf.ln(3)

    # ── Findings register ──
    _h(pdf, "Findings Register")
    findings = snap.get("findings") or []
    if not findings:
        pdf.cell(0, 6, "No findings recorded.", border=0, ln=1)
    else:
        # Grouped by severity, worst tier first, then in the order the snapshot
        # froze them (category × sequence). The old flat list made a reader scan
        # 39 rows to find the two that fail the audit.
        #
        # An unrecognised severity does NOT vanish into a default bucket — it
        # gets its own group under its own name, so a value the grading
        # vocabulary grows later still prints rather than silently disappearing
        # from the register.
        _by_sev: dict[str, list[dict[str, Any]]] = {}
        for f in findings:
            _by_sev.setdefault(str(f.get("severity") or "unspecified").lower(), []).append(f)
        _groups = [(s, _by_sev.pop(s)) for s in _SEV_ORDER if s in _by_sev]
        _groups += sorted(_by_sev.items())

        # State icons. The register is FAIL/PARTIAL in every audit seen so far
        # (PASS/NA rows raise no finding), but the map is keyed off whatever the
        # row carries rather than assuming that — a data model that starts
        # emitting a PASS finding must print it, not drop it.
        _STATE_MARK = {"FAIL": "X", "PARTIAL": "!", "PASS": "OK", "NA": "-",
                       "NOT_ASSESSED": "?"}
        _STATE_INK = {"FAIL": RED, "PARTIAL": AMBER, "PASS": GREEN, "NA": GREY,
                      "NOT_ASSESSED": GREY}

        for _sev, _items in _groups:
            _sev_ink, _sev_tint = _SEV_STYLE.get(_sev, (SLATE, LIGHT))
            # Room for the band AND the first card under it — a severity band
            # alone at the foot of a page reads as an empty group.
            _room(pdf, 32)
            _y = pdf.get_y()
            pdf.set_fill_color(*_sev_tint)
            pdf.rect(10, _y, 190, 6.5, style="F")
            pdf.set_fill_color(*_sev_ink)
            pdf.rect(10, _y, 1.8, 6.5, style="F")
            pdf.set_font("Helvetica", "B", 8.5)
            pdf.set_text_color(*_sev_ink)
            pdf.set_xy(14, _y + 1)
            pdf.cell(184, 4.5,
                     _s(f"{_sev.upper()}  -  {len(_items)} finding(s)"), border=0, ln=1)
            pdf.set_text_color(0, 0, 0)
            pdf.set_y(_y + 8)

            for f in _items:
                _room(pdf, 22)
                result = str(f.get("assessmentStatus") or "")
                _r_ink = _STATE_INK.get(result, GREY)
                # Header line: state mark, code, category, owner, CAPA.
                pdf.set_font("Helvetica", "B", 8)
                pdf.set_text_color(*_r_ink)
                pdf.set_x(12)
                _head = f"[{_STATE_MARK.get(result, '?')}] {f.get('checkpointCode') or '-'}"
                pdf.cell(38, 4.6, _s(_head), border=0, ln=0)
                pdf.set_font("Helvetica", "", 7.5)
                pdf.set_text_color(*SLATE)
                _bits = [str(f.get("discipline") or "")]
                _owner = _who(f.get("ownerId"), user_names)
                if _owner:
                    _bits.append(f"Owner: {_owner}")
                if f.get("capaNumber"):
                    _bits.append(f"CAPA {f['capaNumber']} ({f.get('capaStatus') or 'open'})")
                if f.get("isRepeat"):
                    _bits.append("REPEAT")
                if f.get("isAdHoc"):
                    _bits.append("ad-hoc")
                pdf.cell(150, 4.6, _s("   |   ".join(b for b in _bits if b)), border=0, ln=1)
                pdf.set_text_color(0, 0, 0)

                # The requirement being assessed.
                pdf.set_font("Helvetica", "", 8)
                pdf.set_x(12)
                pdf.multi_cell(188, 4.4, _s(
                    f.get("question") or f.get("title") or f.get("description") or "-"))

                # What the auditor actually saw. Without this the "finding" is
                # only a question with a verdict attached, which is not a
                # finding. Kept in full — the redesign re-lays it out, it does
                # not abridge it.
                obs = (f.get("observation") or "").strip()
                pdf.set_font("Helvetica", "I", 7.5)
                pdf.set_text_color(*GREY)
                pdf.set_x(14)
                pdf.multi_cell(186, 4.2, _s(f"Observation: {obs or 'None recorded.'}"))

                meta = []
                std = f.get("standard") or f.get("standardClauseRef") or f.get("clause")
                if std:
                    meta.append(f"Standard: {std}")
                if f.get("requirementReference"):
                    meta.append(f"Clause: {f['requirementReference']}")
                if f.get("requirementType"):
                    meta.append(f"Type: {_human(f.get('requirementType'))}")
                if f.get("gradeAwarded"):
                    _pts = ""
                    if f.get("scoreObtained") is not None and f.get("scoreAllotted") is not None:
                        _pts = f" {f['scoreObtained']}/{f['scoreAllotted']}"
                    meta.append(f"Grade: {_human(f.get('gradeAwarded'))}{_pts}")
                if f.get("complianceStatus"):
                    meta.append(f"Status: {_human(f.get('complianceStatus'))}")
                if f.get("riskGrade"):
                    meta.append(f"Risk: {_human(f.get('riskGrade'))}")
                if f.get("workflowState"):
                    meta.append(
                        f"State: {_human(f.get('workflowState'))} (round {f.get('round', 0)})")
                if meta:
                    pdf.set_font("Helvetica", "", 7)
                    pdf.set_text_color(*SLATE)
                    pdf.set_x(14)
                    pdf.multi_cell(186, 3.8, _s("   |   ".join(meta)))
                pdf.set_text_color(0, 0, 0)
                pdf.set_draw_color(*HAIRLINE)
                pdf.line(12, pdf.get_y() + 1, 200, pdf.get_y() + 1)
                pdf.ln(3)
            pdf.ln(1)

    pdf.ln(3)

    # ── CAPA summary ──
    _h(pdf, "CAPA Summary")
    cs = snap.get("capaSummary") or {}
    pdf.cell(0, 6, f"Total CAPAs: {cs.get('total', 0)}   Open: {cs.get('open', 0)}   Overdue: {cs.get('overdue', 0)}", border=0, ln=1)
    pdf.ln(3)

    # ── Clause index (WP-12) ──
    # The index an assessor navigates by. Worst clauses first: they open this to
    # find problems, not to read A-Z.
    clauses = snap.get("clauseIndex") or []
    if clauses:
        pdf.add_page()
        _h(pdf, "Clause Index")

        # Provenance caveat, printed BEFORE the table rather than as a trailing
        # note. Most of this library's citations are AI drafts, and the index
        # cannot distinguish them from sourced ones — a reader who takes the
        # clause column as verified fact has been misled by the time they reach
        # a footnote underneath it.
        _foot = ((snap.get("citationProvenance") or {}).get("footnote")) or None
        if _foot:
            pdf.set_font("Helvetica", "I", 8)
            pdf.set_text_color(*RED)
            pdf.set_x(10)
            pdf.multi_cell(0, 4.4, _s(_foot.get("statement")))
            pdf.set_text_color(0, 0, 0)
            pdf.ln(1.5)

        pdf.set_font("Helvetica", "B", 8)
        pdf.set_fill_color(*LIGHT)
        for w, t in ((45, "Standard"), (60, "Clause"), (15, "CPs"), (15, "Pass"),
                     (15, "Fail"), (15, "Part."), (15, "N/A")):
            pdf.cell(w, 6, t, border=1, ln=0, fill=True, align="C" if w < 40 else "L")
        pdf.ln()
        pdf.set_font("Helvetica", "", 8)
        for e in clauses[:70]:
            pdf.cell(45, 5.5, _s(e.get("standard"))[:30], border=1, ln=0)
            pdf.cell(60, 5.5, _s(e.get("clause"))[:42], border=1, ln=0)
            pdf.cell(15, 5.5, str(e.get("total", 0)), border=1, ln=0, align="C")
            pdf.cell(15, 5.5, str(e.get("pass", 0)), border=1, ln=0, align="C")
            fail = e.get("fail", 0)
            pdf.set_text_color(*(RED if fail else GREY))
            pdf.cell(15, 5.5, str(fail), border=1, ln=0, align="C")
            pdf.set_text_color(0, 0, 0)
            pdf.cell(15, 5.5, str(e.get("partial", 0)), border=1, ln=0, align="C")
            pdf.cell(15, 5.5, str(e.get("na", 0)), border=1, ln=1, align="C")
        if len(clauses) > 70:
            pdf.set_font("Helvetica", "I", 8)
            pdf.cell(0, 5, f"... {len(clauses) - 70} further clause row(s) in the register.", ln=1)
        pdf.ln(3)

    # ── Full checkpoint register ──
    #
    # Every checkpoint, not only the ones that failed. A reader who wants to
    # know what was assessed and found compliant had no way to see it: the PDF
    # printed the findings and stopped, so a 19-checkpoint audit showed 3 rows
    # and the other 16 existed nowhere in the download. The register is passed
    # in rather than read from the snapshot because it is deliberately not
    # stored there.
    if register:
        pdf.add_page()
        _h(pdf, "Checkpoint Register")
        pdf.set_font("Helvetica", "", 8)
        pdf.set_text_color(*GREY)
        pdf.multi_cell(190, 4.5, _s(
            f"All {len(register)} checkpoint(s) assessed on this audit, in sequence, with the "
            "auditor's observation, the assignment, any corrective action and the full iteration "
            "history for each."), border=0)
        pdf.set_text_color(0, 0, 0)
        pdf.ln(2)

        _RESULT_COLOUR = {"PASS": GREEN, "PARTIAL": AMBER, "FAIL": RED, "NA": GREY, "NOT_ASSESSED": GREY}
        current_disc = None
        for cp in register:
            # Discipline banner, printed once per group.
            disc = cp.get("discipline") or "Uncategorised"
            if disc != current_disc:
                current_disc = disc
                pdf.ln(1)
                pdf.set_font("Helvetica", "B", 9)
                pdf.set_fill_color(*LIGHT)
                pdf.set_x(10)
                pdf.cell(190, 6, _s(f"  {disc}"), border=0, ln=1, fill=True)
                pdf.ln(1)

            result = str(cp.get("assessmentStatus") or "NOT_ASSESSED")
            sev = str(cp.get("severity") or "-")
            pdf.set_font("Helvetica", "B", 8.5)
            pdf.set_text_color(*_RESULT_COLOUR.get(result, GREY))
            pdf.set_x(10)
            head = f"{cp.get('checkpointCode') or '-'}   {result}   [{sev}]"
            if cp.get("isAdHoc"):
                head += "   (ad-hoc)"
            pdf.multi_cell(190, 5, _s(head), border=0)

            pdf.set_text_color(0, 0, 0)
            pdf.set_font("Helvetica", "", 8)
            pdf.set_x(10)
            pdf.multi_cell(190, 4.5, _s(cp.get("question") or "-"), border=0)

            obs = (cp.get("observation") or "").strip()
            if obs:
                pdf.set_font("Helvetica", "I", 8)
                pdf.set_text_color(*GREY)
                pdf.set_x(12)
                pdf.multi_cell(188, 4.5, _s(f"Observation: {obs}"), border=0)
                pdf.set_text_color(0, 0, 0)

            meta = []
            if cp.get("standard"):
                meta.append(f"Standard: {cp['standard']}")
            if cp.get("requirementReference"):
                meta.append(f"Clause: {cp['requirementReference']}")
            if cp.get("workflowState"):
                meta.append(f"State: {_human(cp.get('workflowState'))}")
            owner = _who(cp.get("ownerId"), user_names)
            if owner:
                meta.append(f"Owner: {owner}")
            if cp.get("capaNumber"):
                meta.append(f"CAPA: {cp['capaNumber']}")
            ev = len(cp.get("auditorEvidenceIds") or []) + len(cp.get("auditeeEvidenceIds") or [])
            if ev:
                # "file(s)", not "photo(s)": evidence is photographs AND
                # documents (licences, certificates, test reports), and the PDF
                # carries only the count — it must not assert a kind it cannot
                # see from a list of storage paths.
                meta.append(f"Evidence: {ev} file(s)")
            if meta:
                pdf.set_font("Helvetica", "", 7.5)
                pdf.set_text_color(*GREY)
                pdf.set_x(12)
                pdf.multi_cell(188, 4, _s("   |   ".join(meta)), border=0)
                pdf.set_text_color(0, 0, 0)

            # The iteration thread — who said what, when. This is the part an
            # assessor asks for and the part a screenshot cannot provide.
            for it in cp.get("interactions") or []:
                pdf.set_font("Helvetica", "", 7.5)
                pdf.set_text_color(*GREY)
                pdf.set_x(14)
                actor = _who(it.get("actorId"), user_names) or _human(it.get("actorRole"))
                line = (f"R{it.get('round', 0)}  {_dt(it.get('timestamp'))}  -  "
                        f"{_human(it.get('action'))}  by {actor}  ->  {_human(it.get('resultingState'))}")
                pdf.multi_cell(186, 4, _s(line), border=0)
                if it.get("comment"):
                    pdf.set_x(16)
                    pdf.multi_cell(184, 4, _s(f'"{it["comment"]}"'), border=0)
                pdf.set_text_color(0, 0, 0)
            pdf.ln(2)

        # Never truncate silently — a register that quietly stops is worse than
        # one that says where it stopped.
        if register_truncated:
            pdf.set_font("Helvetica", "I", 8)
            pdf.set_text_color(*RED)
            pdf.multi_cell(190, 5, _s(
                f"{register_truncated} further checkpoint(s) are on this audit but are not printed "
                "here — this PDF caps the register to keep the file openable. Use the on-screen "
                "register for the remainder."), border=0)
            pdf.set_text_color(0, 0, 0)
        pdf.ln(3)

    # ── Sign-off (FINAL) ──
    if rtype.upper() == "FINAL":
        _h(pdf, "Sign-Off")
        signs = report.get("signOffs") or []
        summary = snap.get("signOffSummary") or {}
        if not signs:
            pdf.set_font("Helvetica", "", 9)
            pdf.cell(0, 6, "No sign-off has been recorded for this audit.", border=0, ln=1)
        for s in signs:
            _room(pdf, 14)
            role = _human(s.get("role"))
            if s.get("disciplineCode"):
                role += f" - {s['disciplineCode']}"
            pdf.set_font("Helvetica", "B", 9)
            pdf.set_text_color(*INK)
            pdf.set_x(10)
            pdf.cell(0, 5, _s(f"{s.get('name') or s.get('typedName') or 'Unnamed signer'}"),
                     border=0, ln=1)
            pdf.set_font("Helvetica", "", 8)
            pdf.set_text_color(*SLATE)
            bits = [role]
            if s.get("designation"):
                bits.append(str(s["designation"]))
            # Signed WHEN and HOW. A name with neither is not a sign-off, and
            # the old renderer printed exactly that.
            bits.append(f"Signed {_dt(s.get('signedAt'))}")
            if s.get("signatureKind"):
                bits.append(
                    "Drawn signature on file" if s.get("signatureKind") == "DRAWN"
                    else f"Typed signature: {s.get('typedName') or s.get('name') or '-'}"
                )
            pdf.set_x(12)
            pdf.multi_cell(188, 4.2, _s("   |   ".join(bits)))
            if s.get("statement"):
                pdf.set_font("Helvetica", "I", 8)
                pdf.set_x(12)
                pdf.multi_cell(188, 4.2, _s(f'"{s["statement"]}"'))
            pdf.set_text_color(0, 0, 0)
            pdf.ln(1.5)

        # Absence, stated. A reader must be able to tell "nobody else was
        # required" from "somebody has not signed yet", and only a sentence does
        # that — a section that simply stops says neither.
        missing = summary.get("missingRequiredRoles") or []
        unsigned_disc = summary.get("unsignedDisciplines") or []
        pdf.set_font("Helvetica", "", 8)
        if missing:
            pdf.set_text_color(*RED)
            pdf.set_x(10)
            pdf.multi_cell(190, 4.4, _s(
                "Outstanding required sign-off: "
                + ", ".join(_human(r) for r in missing) + "."))
        elif signs:
            pdf.set_text_color(*GREY)
            pdf.set_x(10)
            pdf.multi_cell(190, 4.4, "All sign-offs required for closure were recorded.")
        # Per-discipline sign-off is expected from each auditor who actually
        # held checkpoints, so an unsigned discipline is a real gap and not an
        # absence the reader should have to infer from a shorter list.
        if unsigned_disc:
            pdf.set_text_color(*AMBER)
            pdf.set_x(10)
            pdf.multi_cell(190, 4.4, _s(
                f"Discipline sign-off outstanding ({summary.get('disciplinesSigned', 0)} of "
                f"{summary.get('disciplinesTotal', 0)} signed): "
                + ", ".join(str(d) for d in unsigned_disc) + "."))
        pdf.set_text_color(0, 0, 0)
        pdf.ln(3)

    # ── Distribution list (WP-12) ──
    dist = snap.get("distributionList") or []
    if dist:
        _h(pdf, "Distribution")
        pdf.set_font("Helvetica", "", 9)
        for d in dist:
            pdf.cell(0, 5.5, f"{_s(d.get('role'))}: {_s(d.get('name'))}", border=0, ln=1)
        # A heading called "Distribution" over a single name reads as a
        # rendering failure. It is not — the builder correctly walks lead
        # auditor, co-auditors, plant manager and auditees; this engagement
        # simply has none of the latter. Say which, so the reader can tell an
        # empty team from a broken report.
        if len(dist) == 1:
            pdf.ln(1)
            pdf.set_font("Helvetica", "I", 8)
            pdf.set_text_color(*GREY)
            pdf.set_x(10)
            pdf.multi_cell(0, 4.5, (
                "No co-auditors, plant manager or auditee owners are assigned to this "
                "engagement, so the distribution is the lead auditor only."
            ))
            pdf.set_text_color(0, 0, 0)
        pdf.ln(3)

    # ── Revision history + errata (WP-12, §2.5) ──
    revs = snap.get("revisionHistory") or []
    errata = report.get("errata") or []
    if revs or errata:
        _h(pdf, "Revision History")
        pdf.set_font("Helvetica", "", 9)
        pdf.cell(0, 5.5, f"This is issue {snap.get('revision', 1)} of this audit's reports.", ln=1)
        for r in revs:
            pdf.cell(
                0, 5,
                f"  {_s(r.get('reportCode'))} ({_s(r.get('reportType'))}) - "
                f"{_s(r.get('generatedAt'))} - superseded",
                border=0, ln=1,
            )
        for e in errata:
            pdf.ln(1)
            pdf.set_font("Helvetica", "B", 8)
            pdf.cell(0, 5, f"Erratum {e.get('sequence')} - {_s(e.get('createdAt'))}", ln=1)
            pdf.set_font("Helvetica", "", 8)
            pdf.set_x(10)
            pdf.multi_cell(0, 4.5, _s(e.get("text")))
            pdf.set_x(10)
            pdf.multi_cell(
                0, 4.5,
                f"Raised by {_s(e.get('raisedBy'))}, approved by {_s(e.get('approvedBy'))}",
            )
        pdf.ln(2)

    # ── Integrity footer ──
    _h(pdf, "Record Integrity")
    pdf.set_font("Helvetica", "", 8)
    pdf.set_x(10)
    pdf.multi_cell(
        0, 4.5,
        "This report is generated from an immutable snapshot taken at issue. Its SHA-256 digest "
        f"is {_s(report.get('snapshotHashFull') or snap.get('snapshotHash') or '-')}. "
        "Any change to the underlying record after issue appears as an erratum above, never as a "
        "silent edit.",
    )

    out = pdf.output()
    return bytes(out)
