"""QR stickers for fire assets — mint, render, and print.

Register an extinguisher, download its QR, stick it on the cylinder. An inspector
scans it and lands on that cylinder's checklist for the current period. That is
the whole feature, and two decisions carry it.

DECISION 1 — THE PAYLOAD IS A URL, NOT A BARE TOKEN
---------------------------------------------------
The platform's existing stickers carry bare `safeops:` tokens
(`safeops:equipment:<id>`, `safeops:area:<id>`) which only the in-app scanner in
`components/capture/qr-scanner.tsx` understands. That is right for a Field
Capture flow where the technician is already in the PWA.

It is wrong for a sticker on an extinguisher. The person holding the phone is
usually pointing the stock camera app at a cylinder in a corridor, and a bare
token shows them an unhelpful string. A URL opens the browser, hits the app's
own auth, and lands on the checklist — no app install, no scanner screen, no
training.

So the encoded value is:

    {base}/fire-safety/scan/{assetId}

and the canonical short token `safeops:fire-asset:{assetId}` is ALSO recognised,
so the in-app scanner keeps working and a sticker printed either way resolves.
`qrCode` on the row stores the token; the URL is derived at render time, because
the deployment's hostname is configuration and baking it into 37 database rows
would mean re-printing every sticker the day the domain changes.

DECISION 2 — A NEW NOUN, NOT A REUSED ONE
------------------------------------------
`safeops:fire-asset:` rather than the existing `safeops:equipment:`.

`qr-jump.tsx` states the constraint plainly — "one QR standard platform-wide",
because a second scheme means a second sticker beside the first. This honours
that: same `safeops:` namespace, same parser, same scanner. What it does not do
is pretend a `FireEquipment` row is a Field Capture `Equipment` row. They are
different tables with different ids, and encoding a fire asset as
`safeops:equipment:<id>` would hand the capture wizard an id that resolves to
nothing there — a sticker that scans successfully and then fails silently.

WHAT WAS ALREADY WRONG
----------------------
`FireEquipment.qrCode` was being seeded as `SAFEOPS-FIRE-FE-ACS-0013` — a third
scheme, in neither the platform's format nor any parser's vocabulary. A sticker
printed from it would scan and do nothing at all. `backfill_tokens()` below
rewrites those, and the 11 of 37 assets that had no token at all.

Deterministic and offline: segno is a pure-Python encoder with no dependencies
and makes no network calls. Nothing here reaches outside the process.
"""

from __future__ import annotations

import os
import re
from io import BytesIO
from typing import Any, Iterable

import segno
from sqlalchemy import select

from app.models.fire_safety import FireEquipment

# The canonical sticker token. One noun for a fire asset, in the platform's own
# `safeops:` namespace — see DECISION 2.
TOKEN_PREFIX = "safeops:fire-asset:"

# Where a scanned sticker lands. Kept here so the route, the sticker and the
# parser cannot drift apart.
SCAN_PATH = "/fire-safety/scan"

# Error correction level. 'M' (~15% recoverable) rather than the default 'L':
# these stickers live on cylinders in corridors and get scuffed, painted around
# and partly peeled. 'H' would be tougher still but makes the symbol denser at
# the same physical size, which costs more scan reliability at arm's length on a
# 25 mm label than the extra redundancy buys.
ERROR_LEVEL = "m"


def base_url() -> str:
    """The public origin a scanned sticker should resolve against.

    Falls back to a relative path when unset: a relative URL still works if the
    sticker is scanned from inside the app, and a sticker carrying `localhost`
    is worse than one carrying no host at all — it looks fine on the print sheet
    and is dead on every phone that is not the developer's.
    """
    raw = (os.environ.get("APP_PUBLIC_URL") or os.environ.get("NEXTAUTH_URL") or "").strip()
    return raw.rstrip("/")


def token_for(asset_id: str) -> str:
    return f"{TOKEN_PREFIX}{asset_id}"


def parse_token(raw: str) -> str | None:
    """Asset id out of a token OR a scan URL. Mirrors the frontend parser."""
    value = (raw or "").strip()
    if value.startswith(TOKEN_PREFIX):
        return value[len(TOKEN_PREFIX):].strip() or None
    m = re.search(rf"{re.escape(SCAN_PATH)}/([A-Za-z0-9_-]+)", value)
    return m.group(1) if m else None


def payload_for(asset_id: str, *, base: str | None = None) -> str:
    """What actually gets encoded into the symbol."""
    root = base if base is not None else base_url()
    return f"{root}{SCAN_PATH}/{asset_id}" if root else f"{SCAN_PATH}/{asset_id}"


# ═══════════════════════════════════════════════════════════════════════════
# Rendering
# ═══════════════════════════════════════════════════════════════════════════
def render_png(payload: str, *, scale: int = 8, border: int = 2) -> bytes:
    """A QR as PNG bytes.

    `border` is the quiet zone in modules. The spec minimum is 4; 2 is used here
    because the sticker layouts below draw their own white margin around the
    symbol, and doubling the quiet zone would shrink the symbol itself on a
    fixed-size label — which hurts scanning more than a tight quiet zone does.
    A caller embedding the raw PNG with no margin should pass border=4.
    """
    buf = BytesIO()
    segno.make(payload, error=ERROR_LEVEL).save(buf, kind="png", scale=scale, border=border)
    return buf.getvalue()


def render_svg(payload: str, *, scale: int = 8, border: int = 2) -> bytes:
    """A QR as SVG — the right format for a printer.

    Vector, so it stays sharp at any label size. A 25 mm sticker printed from a
    72-dpi PNG is a sticker that does not scan.
    """
    buf = BytesIO()
    segno.make(payload, error=ERROR_LEVEL).save(
        buf, kind="svg", scale=scale, border=border, xmldecl=True, svgclass=None, lineclass=None,
    )
    return buf.getvalue()


# ═══════════════════════════════════════════════════════════════════════════
# Print sheet
# ═══════════════════════════════════════════════════════════════════════════
# A4 grid of labels. 3 across x 8 down = 24 per page at 63.5 x 33.9 mm, which is
# the common Avery L7160/5160 address-label pitch — so these can be run on stock
# label sheets rather than cut by hand.
_COLS, _ROWS = 3, 8
_LABEL_W, _LABEL_H = 63.5, 33.9
_MARGIN_X, _MARGIN_Y = 7.0, 13.0
_GUTTER_X = 2.5


def sticker_sheet_pdf(assets: Iterable[dict[str, Any]], *, base: str | None = None,
                      show_cut_marks: bool = True) -> bytes:
    """A print-ready sheet of QR labels, one per asset.

    Each label carries the symbol plus the asset code, its allotted tag and its
    location — because a QR nobody can read is a QR nobody can file a fault
    against when the sticker is damaged, and the person applying twenty of these
    needs to know which cylinder each belongs on without scanning every one.
    """
    from fpdf import FPDF

    from app.services.report_pdf import _s

    NAVY = (11, 31, 77)
    GREY = (110, 118, 135)
    RULE = (205, 213, 227)

    pdf = FPDF(orientation="portrait", unit="mm", format="A4")
    pdf.set_auto_page_break(auto=False)
    pdf.set_margins(_MARGIN_X, _MARGIN_Y, _MARGIN_X)

    items = list(assets)
    if not items:
        pdf.add_page()
        pdf.set_font("Helvetica", "", 10)
        pdf.cell(0, 10, _s("No assets selected."))
        return bytes(pdf.output())

    per_page = _COLS * _ROWS
    for index, asset in enumerate(items):
        slot = index % per_page
        if slot == 0:
            pdf.add_page()
        col, row = slot % _COLS, slot // _COLS
        x = _MARGIN_X + col * (_LABEL_W + _GUTTER_X)
        y = _MARGIN_Y + row * _LABEL_H

        if show_cut_marks:
            pdf.set_draw_color(*RULE)
            pdf.set_line_width(0.1)
            pdf.rect(x, y, _LABEL_W, _LABEL_H)

        qr = BytesIO(render_png(payload_for(asset["id"], base=base), scale=6, border=1))
        qr_size = _LABEL_H - 7
        pdf.image(qr, x=x + 2.5, y=y + 3.5, w=qr_size, h=qr_size)

        text_x = x + qr_size + 5
        text_w = _LABEL_W - qr_size - 7

        pdf.set_xy(text_x, y + 4.5)
        pdf.set_font("Helvetica", "B", 8.5)
        pdf.set_text_color(*NAVY)
        pdf.cell(text_w, 4, _s(asset.get("equipmentCode") or ""), align="L")

        tag = asset.get("allottedSerialNo")
        if tag:
            pdf.set_xy(text_x, y + 9)
            pdf.set_font("Helvetica", "", 7)
            pdf.set_text_color(*GREY)
            pdf.cell(text_w, 3.5, _s(f"Tag {tag}"), align="L")

        pdf.set_xy(text_x, y + (13 if tag else 9))
        pdf.set_font("Helvetica", "", 6.4)
        pdf.set_text_color(*GREY)
        pdf.multi_cell(text_w, 2.8, _s((asset.get("location") or "")[:70]), align="L")

        pdf.set_xy(text_x, y + _LABEL_H - 7.5)
        pdf.set_font("Helvetica", "", 5.4)
        pdf.set_text_color(*GREY)
        pdf.cell(text_w, 2.6, _s("Scan to open this unit's checklist"), align="L")

    return bytes(pdf.output())


# ═══════════════════════════════════════════════════════════════════════════
# Backfill
# ═══════════════════════════════════════════════════════════════════════════
async def backfill_tokens(db, *, dry_run: bool = False) -> dict[str, Any]:
    """Bring every fire asset onto the canonical token.

    Rewrites the legacy `SAFEOPS-FIRE-<code>` values, which matched no parser on
    this platform, and fills the assets that carried no token at all. Idempotent.
    """
    rows = (
        await db.execute(select(FireEquipment).where(FireEquipment.isDeleted.is_(False)))
    ).scalars().all()
    fixed, filled, already = [], [], 0
    for e in rows:
        want = token_for(e.id)
        if e.qrCode == want:
            already += 1
            continue
        (filled if not e.qrCode else fixed).append(e.equipmentCode)
        if not dry_run:
            e.qrCode = want
    if not dry_run:
        await db.flush()
    return {
        "total": len(rows), "alreadyCorrect": already,
        "rewritten": fixed, "filled": filled, "dryRun": dry_run,
    }


__all__ = [
    "TOKEN_PREFIX", "SCAN_PATH", "base_url", "token_for", "parse_token", "payload_for",
    "render_png", "render_svg", "sticker_sheet_pdf", "backfill_tokens",
]
