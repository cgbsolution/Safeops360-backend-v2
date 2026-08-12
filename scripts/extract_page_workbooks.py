"""Regenerate the two management-system / social-compliance checkpoint libraries
from the customer's source workbooks.

  • "QMS, EMS OHS.xls"                                  -> page_ims_checkpoints.json
  • "Annexure-2, PIL Social Compliance Audit Checklist" -> page_social_compliance_checkpoints.json

Output matches the shape of app/seed/data/page_industries_checkpoints.json, so
the three libraries are seeded and materialised by exactly the same code path.
Committed alongside the JSON it produces, because the mapping from workbook
geometry to checkpoints (which column is which standard, what "--" means, where
a section banner stops and data starts) is the part worth being able to re-read
when Page issue a new revision of either sheet.

Needs `xlrd` (the QMS workbook is a real BIFF .xls) and `openpyxl`.

Run from the backend root, pointing at the directory holding both workbooks:
    python scripts/extract_page_workbooks.py /path/to/workbooks app/seed/data
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import openpyxl
import xlrd

SRC = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(".")
OUT = Path(sys.argv[2]) if len(sys.argv) > 2 else Path(__file__).resolve().parents[1] / "app" / "seed" / "data"

IMS_WORKBOOK = "QMS, EMS OHS.xls"
SOCIAL_WORKBOOK = "Annexure-2, PIL Social Compliance Audit Checklist.xlsx"

# ── Statutory classifier ─────────────────────────────────────────────────
# Column I of the Page grading vocabulary is a property of the REQUIREMENT,
# not of the audit, so it is set once here (same reasoning as the existing
# seed_page_industries_library.py). A line naming a licence, consent, NOC,
# statutory register or an evaluation-of-compliance clause is statutory.
STATUTORY = re.compile(
    r"licen[cs]e|consent|\bNOC\b|statutory|legal|compliance obligation|"
    r"evaluation of compliance|certificate|manifest|form \d|register|FSSAI|"
    r"minimum wage|EPFO|ESIC|bonus|child labour|forced labour|discrimination|"
    r"working hours|overtime|standing order|factory licence|PCB|boiler|"
    r"pressure vessel|environmental statement|medical test|health surveillance",
    re.I,
)
# Lines whose failure is a safety / legal exposure rather than a paperwork gap.
CRITICAL = re.compile(
    r"child labour|forced labour|discrimination|fire|emergency|hazard|"
    r"toxic|drowning|electrical safety|evacuation|exit|PPE|personal protective|"
    r"boiler|pressure vessel|HIRA|risk control|safety data sheet|chemical",
    re.I,
)


def classify(text: str) -> tuple[str, str]:
    """-> (criticality, requirement_type)."""
    req = "STATUTORY_REGULATORY" if STATUTORY.search(text) else "INTERNAL_REQUIREMENT"
    if CRITICAL.search(text):
        crit = "critical"
    elif req == "STATUTORY_REGULATORY":
        crit = "major"
    else:
        crit = "minor"
    return crit, req


def clean(v) -> str:
    return re.sub(r"\s+", " ", str(v or "")).strip()


def clean_multiline(v) -> str:
    """Like `clean`, but keeps the line breaks.

    The Audit Reference cells hold an enumerated procedure ("i. … ii. … iii. …")
    one step per line. Collapsing that to a single line turns an auditor's
    method into a paragraph, so the newlines are load-bearing content here.
    """
    lines = [re.sub(r"[ \t]+", " ", ln).strip() for ln in str(v or "").splitlines()]
    return "\n".join(ln for ln in lines if ln)


# ── PAGE_IMS ─────────────────────────────────────────────────────────────
# The workbook is ONE sheet with three clause columns (F=QMS, G=EMS,
# H=OHSMS, header row 8). A checkpoint belongs to a discipline when that
# discipline's clause cell carries a clause — "--" is the workbook's own
# marker for "this line does not apply to this standard". EnMS is a separate
# tab, all of whose rows are EnMS.
IMS_DISCIPLINES = [
    ("QMS", "Quality Management System (ISO 9001:2015)", "#0EA5E9", "badge-check", 5),
    ("EMS", "Environmental Management System (ISO 14001:2015)", "#16A34A", "leaf", 6),
    ("OHS", "Occupational H&S Management System (ISO 45001:2018)", "#DC2626", "shield", 7),
]
IMS_STANDARD = {
    "QMS": "ISO 9001:2015",
    "EMS": "ISO 14001:2015",
    "OHS": "ISO 45001:2018",
    "ENMS": "ISO 50001:2018",
}


def build_ims() -> list[dict]:
    wb = xlrd.open_workbook(str(SRC / IMS_WORKBOOK))
    sh = wb.sheet_by_name("QMS, EMS & OHS")

    buckets: dict[str, list[dict]] = {code: [] for code, *_ in IMS_DISCIPLINES}
    section = ""
    for r in range(8, sh.nrows):  # row 9 (0-indexed 8) onward
        a, b = clean(sh.cell_value(r, 0)), clean(sh.cell_value(r, 1))
        # Section banners: one of the two cells carries a heading and the
        # other is blank. They scope the lines beneath them (STP / ETP), so
        # they are carried onto the question rather than dropped.
        if not b or not re.match(r"^\d+(\.\d+)?$", a):
            head = b or a
            if head:
                section = head.rstrip(":")
            continue
        guidance = clean_multiline(sh.cell_value(r, 2))
        for code, _name, _colour, _icon, col in IMS_DISCIPLINES:
            clause = clean(sh.cell_value(r, col))
            if not clause or clause in {"--", "-", "NA", "N/A"}:
                continue
            prefix = ""
            m = re.match(r"^(Sewage Treatment Plant \(STP\)|Effluent Treatment Plant \(ETP\))", section)
            if m:
                prefix = f"{'STP' if 'STP' in m.group(1) else 'ETP'} — "
            question = prefix + b
            crit, req = classify(f"{question} {guidance}")
            buckets[code].append({
                "code": f"PI-{code}-{len(buckets[code]) + 1:03d}",
                "question": question,
                "criticality": crit,
                "requirement_type": req,
                "guidance": guidance,
                "requirement_reference": f"Clause {clause}",
                "standard": IMS_STANDARD[code],
            })

    # EnMS tab — every row is an EnMS checkpoint; the single clause column is F.
    en = wb.sheet_by_name("EnMS")
    enms: list[dict] = []
    for r in range(8, en.nrows):
        a, b = clean(en.cell_value(r, 0)), clean(en.cell_value(r, 1))
        if not b or not re.match(r"^\d+(\.\d+)?$", a):
            continue
        guidance = clean_multiline(en.cell_value(r, 2))
        clause = clean(en.cell_value(r, 5))
        crit, req = classify(f"{b} {guidance}")
        enms.append({
            "code": f"PI-ENMS-{len(enms) + 1:03d}",
            "question": b,
            "criticality": crit,
            "requirement_type": req,
            "guidance": guidance,
            "requirement_reference": f"Clause {clause}" if clause else "",
            "standard": IMS_STANDARD["ENMS"],
        })

    cats = [
        {"category_code": code, "category_name": name, "category_color": colour,
         "category_icon": icon, "checkpoints": buckets[code]}
        for code, name, colour, icon, _col in IMS_DISCIPLINES
    ]
    cats.append({
        "category_code": "ENMS",
        "category_name": "Energy Management System (ISO 50001:2018)",
        "category_color": "#CA8A04", "category_icon": "zap", "checkpoints": enms,
    })
    return cats


# ── PAGE_SOCIAL ──────────────────────────────────────────────────────────
# Column C is the section (= discipline), column E the checkpoint. Both are
# vertically merged, so the section is carried down until it changes.
#
# Every section of this checklist is backed by statute — the Factories Act,
# Payment of Wages / Minimum Wages / Payment of Bonus Acts, the EPF & ESI
# Acts, the Child & Adolescent Labour (P&R) Act, the Trade Unions Act and the
# Environment (Protection) Act respectively. `requirement_type` is therefore
# STATUTORY_REGULATORY throughout, which is a statement about the checklist,
# not a shortcut: there is no internal-only line in it.
#
# The per-section base criticality carries the part that DOES differ — the
# zero-tolerance sections (child / forced labour, discrimination) and life
# safety are critical whatever the wording of the individual line, and the
# CRITICAL keyword pass can lift a line but never lower one.
SOCIAL_STYLE = {
    "LAWS": ("#7C3AED", "scale", "major"),
    "HOURS": ("#0EA5E9", "clock", "major"),
    "WAGES": ("#16A34A", "wallet", "major"),
    "HS": ("#DC2626", "shield", "critical"),
    "FOA": ("#EA580C", "users", "major"),
    "CHILD": ("#BE123C", "user-x", "critical"),
    "DISCRIM": ("#DB2777", "user-minus", "critical"),
    "FORCED": ("#9333EA", "lock", "critical"),
    "ENV": ("#059669", "leaf", "major"),
}
SOCIAL_CODES = ["LAWS", "HOURS", "WAGES", "HS", "FOA", "CHILD", "DISCRIM", "FORCED", "ENV"]


def build_social() -> list[dict]:
    wb = openpyxl.load_workbook(SRC / SOCIAL_WORKBOOK, data_only=True)
    sh = wb["Sheet1"]
    cats: list[dict] = []
    section = None
    for r in range(5, sh.max_row + 1):
        sec = clean(sh.cell(r, 3).value)
        q = clean(sh.cell(r, 5).value)
        if sec and sec != section:
            section = sec
            code = SOCIAL_CODES[len(cats)]
            colour, icon, _base = SOCIAL_STYLE[code]
            cats.append({
                "category_code": code, "category_name": sec,
                "category_color": colour, "category_icon": icon,
                # Declared on every category so `library_subject_scope` and
                # `library_audit_category` both classify this library without a
                # code change. This checklist audits a SUPPLIER's factory —
                # licences, wages, child labour — so it belongs to the vendor
                # subject, not to an audit of our own site.
                "subject_scope": "VENDOR",
                "audit_category": "SOCIAL_COMPLIANCE",
                "checkpoints": [],
            })
        if not q or not cats:
            continue
        cat = cats[-1]
        base = SOCIAL_STYLE[cat["category_code"]][2]
        crit = "critical" if base == "critical" or CRITICAL.search(q) else base
        cat["checkpoints"].append({
            "code": f"PI-SC-{cat['category_code']}-{len(cat['checkpoints']) + 1:03d}",
            "question": q,
            "criticality": crit,
            "requirement_type": "STATUTORY_REGULATORY",
            "guidance": "",
            "requirement_reference": cat["category_name"],
            "standard": "PIL Social Compliance Audit Checklist (Annexure-2, v4)",
        })
    return cats


def report(label: str, cats: list[dict]) -> None:
    total = sum(len(c["checkpoints"]) for c in cats)
    print(f"\n{label} — {total} checkpoints / {len(cats)} disciplines")
    for c in cats:
        stat = sum(1 for cp in c["checkpoints"] if cp["requirement_type"] == "STATUTORY_REGULATORY")
        print(f"  {c['category_code']:<8} {c['category_name'][:52]:<54} "
              f"{len(c['checkpoints']):>3}  ({stat} statutory)")


if __name__ == "__main__":
    ims, social = build_ims(), build_social()
    report("PAGE_IMS", ims)
    report("PAGE_SOCIAL", social)
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "page_ims_checkpoints.json").write_text(
        json.dumps(ims, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    (OUT / "page_social_compliance_checkpoints.json").write_text(
        json.dumps(social, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"\nWrote to {OUT.resolve()}")
