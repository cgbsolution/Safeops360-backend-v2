"""P2-9 — Audit report PDF generation (fpdf2; pure-Python, no system deps).

Renders an AuditReport's immutable snapshot to a branded A4 PDF: cover page,
INTERIM 'PROVISIONAL' watermark on every page, category compliance, findings
register, CAPA summary, sign-off block (FINAL), page numbers + confidential footer.
"""

from __future__ import annotations

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


def _rag(pct: float | None) -> tuple[int, int, int]:
    if pct is None:
        return GREY
    return GREEN if pct >= 85 else (AMBER if pct >= 70 else RED)


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
        self.cell(0, 6, "CONFIDENTIAL", border=0, ln=0, align="L")
        self.cell(0, 6, f"Page {self.page_no()} of {{nb}}", border=0, ln=0, align="C")
        self.cell(0, 6, f"hash {self.snapshot_hash[:12]}", border=0, ln=1, align="R")


def _h(pdf: _Report, text: str):
    """Section heading, numbered by RENDER ORDER.

    The numbers used to be hardcoded into the strings ("1. Executive Summary" …
    "12. Record Integrity") while six of the twelve sections are conditional, so
    a report that suppressed Independence and Clause Index printed 8 → 10 → 12
    and read as though pages were missing. Counting here means the number can
    only ever describe what actually rendered — on interim and final alike.
    """
    pdf._section_no = getattr(pdf, "_section_no", 0) + 1
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

    # ── Executive summary ──
    pdf.add_page()
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
        pdf.set_font("Helvetica", "B", 9)
        pdf.set_fill_color(*LIGHT)
        pdf.cell(120, 7, "Category", border=1, ln=0, fill=True)
        pdf.cell(30, 7, "Assessed", border=1, ln=0, fill=True, align="C")
        pdf.cell(30, 7, "Score %", border=1, ln=1, fill=True, align="C")
        pdf.set_font("Helvetica", "", 9)
        for c in scored[:30]:
            name = str(c.get("category_name") or c.get("category") or c.get("name") or "-")[:52]
            sc = _cat_pct(c)
            done = (c.get("passed", 0) or 0) + (c.get("partial", 0) or 0) + (c.get("failed", 0) or 0)
            pdf.cell(120, 6, name, border=1, ln=0)
            pdf.cell(30, 6, f"{done} of {c.get('total', done)}", border=1, ln=0, align="C")
            pdf.set_text_color(*_rag(sc))
            pdf.cell(30, 6, f"{sc}", border=1, ln=1, align="C")
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
        # One block per finding.
        #
        # This section used to print "[major]  -" and a bare "-" for every row.
        # It read `standardClauseRef`/`clause` and `title`/`description`, none of
        # which the snapshot has ever produced — `_build_report_snapshot` writes
        # `standard`, `requirementReference`, `question` and `observation`. Four
        # key names, zero matches, so every finding rendered as two dashes and
        # the register was worthless. The keys below are the ones the snapshot
        # actually emits; the legacy names are kept as fallbacks so an old
        # stored snapshot still renders.
        for f in findings:
            sev = str(f.get("severity") or "-")
            code_ = str(f.get("checkpointCode") or "-")
            disc = str(f.get("discipline") or "")
            result = str(f.get("assessmentStatus") or "")
            adverse = "CRIT" in sev.upper() or "MAJOR" in sev.upper() or result == "FAIL"

            pdf.set_x(10)
            pdf.set_font("Helvetica", "B", 8.5)
            pdf.set_text_color(*(RED if adverse else AMBER))
            head = f"{code_}   [{sev[:16]}]"
            if result:
                head += f"   {result}"
            if disc:
                head += f"   -   {disc}"
            pdf.multi_cell(190, 5, _s(head), border=0)
            pdf.set_text_color(0, 0, 0)

            # The requirement being assessed.
            pdf.set_font("Helvetica", "", 8)
            pdf.set_x(10)
            pdf.multi_cell(190, 4.5, _s(f.get("question") or f.get("title") or f.get("description") or "-"), border=0)

            # What the auditor actually saw. Without this the "finding" is only
            # a question with a verdict attached, which is not a finding.
            obs = (f.get("observation") or "").strip()
            pdf.set_font("Helvetica", "I", 8)
            pdf.set_text_color(*GREY)
            pdf.set_x(12)
            pdf.multi_cell(188, 4.5, _s(f"Observation: {obs or 'None recorded.'}"), border=0)

            meta = []
            std = f.get("standard") or f.get("standardClauseRef") or f.get("clause")
            ref = f.get("requirementReference")
            if std:
                meta.append(f"Standard: {std}")
            if ref:
                meta.append(f"Clause: {ref}")
            if f.get("workflowState"):
                meta.append(f"State: {_human(f.get('workflowState'))} (round {f.get('round', 0)})")
            owner = _who(f.get("ownerId"), user_names)
            if owner:
                meta.append(f"Owner: {owner}")
            if f.get("capaNumber"):
                meta.append(f"CAPA: {f['capaNumber']} ({f.get('capaStatus') or 'open'})")
            if f.get("isAdHoc"):
                meta.append("Ad-hoc checkpoint")
            if meta:
                pdf.set_font("Helvetica", "", 7.5)
                pdf.set_x(12)
                pdf.multi_cell(188, 4, _s("   |   ".join(meta)), border=0)
            pdf.set_text_color(0, 0, 0)
            pdf.ln(2)
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
                meta.append(f"Evidence: {ev} photo(s)")
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
        if not signs:
            pdf.cell(0, 6, "Awaiting sign-off.", border=0, ln=1)
        for s in signs:
            pdf.cell(0, 6, f"{s.get('role', '-')}: {s.get('name', '-')}  -  {s.get('signedAt', '')}", border=0, ln=1)
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
