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

DECISION 1b — THE VALUE IS AN OPAQUE TOKEN, NOT THE ROW ID
----------------------------------------------------------
The sticker used to carry `safeops:fire-asset:<asset.id>`. Two problems, and
neither is fixable while the token IS the id:

  * Not opaque. Ids come from one generator in one format, so a single sticker
    photographed in a corridor tells you the shape of every other one — and a
    scan URL is a bearer credential, resolving for anyone who holds it and
    passes the asset's location scope check.
  * Not revocable. "That label was damaged, issue a new one" has no meaning for
    a derived value: the only way to invalidate it would be to change the row's
    primary key.

So `FireEquipment.qrToken` is now a stored random value — `secrets.token_urlsafe(32)`,
256 bits, unguessable and unordered — and the row id never appears on a label.
Rotation overwrites it, which is exactly what makes the old label stop working.

THE TRANSITION IS THE HARD PART
-------------------------------
Every sticker already stuck on a cylinder encodes the old derived value. Flip
resolution to token-only on deploy day and every one of them dies at once, on a
fire register, with no warning. So `resolve()` accepts BOTH while
`FIRE_QR_LEGACY_SCAN` is on (the default), and the cutover is a separate,
deliberate act once the reprint pass is done — `scripts/fire_qr_reprint_status.py`
is what says whether it is safe yet. `qrLabelPrintedAt` on each row is how that
question gets an answer without walking the site.

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
import secrets
from datetime import datetime, timezone
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


# 32 bytes → 43 url-safe characters. Long enough that guessing is not a threat
# model, short enough that the QR stays a low module count and scans off a 25 mm
# label at arm's length — the practical constraint, since a denser symbol is a
# symbol that fails to scan in a dim corridor.
TOKEN_BYTES = 32


def new_token() -> str:
    """A fresh opaque token. `secrets`, not `random` — this is a credential."""
    return secrets.token_urlsafe(TOKEN_BYTES)


def token_for(token: str) -> str:
    """The sticker's short form. Takes the asset's `qrToken`, never its id."""
    return f"{TOKEN_PREFIX}{token}"


def parse_token(raw: str) -> str | None:
    """The token out of a short form OR a scan URL. Mirrors the frontend parser.

    Returns the raw value only — deciding whether it is a current opaque token
    or a legacy asset id is `resolve()`'s job, because only the database can
    tell. Distinguishing them by shape would be a guess that silently breaks the
    day an id format changes.
    """
    value = (raw or "").strip()
    if value.startswith(TOKEN_PREFIX):
        return value[len(TOKEN_PREFIX):].strip() or None
    m = re.search(rf"{re.escape(SCAN_PATH)}/([A-Za-z0-9_-]+)", value)
    return m.group(1) if m else None


def payload_for(token: str, *, base: str | None = None) -> str:
    """What actually gets encoded into the symbol — the opaque token, not the id."""
    root = base if base is not None else base_url()
    return f"{root}{SCAN_PATH}/{token}" if root else f"{SCAN_PATH}/{token}"


# ═══════════════════════════════════════════════════════════════════════════
# Resolution
# ═══════════════════════════════════════════════════════════════════════════
def legacy_scan_enabled() -> bool:
    """Whether stickers carrying the old derived asset id still resolve.

    ON by default, and that default is deliberate: every label currently in the
    field encodes the old value, so a token-only deploy would kill the entire
    estate's scanning in one step. Turn it off — `FIRE_QR_LEGACY_SCAN=0` — only
    once `scripts/fire_qr_reprint_status.py` reports every asset reprinted.
    """
    raw = (os.environ.get("FIRE_QR_LEGACY_SCAN") or "").strip().lower()
    if raw in ("0", "false", "no", "off"):
        return False
    return True


async def resolve(db, value: str) -> tuple[Any | None, str]:
    """A scanned value → (asset, how). `how` is one of:

        "token"   resolved against the opaque qrToken — the intended path
        "legacy"  resolved as a bare asset id from a pre-cutover sticker
        "unknown" nothing matched, or a legacy sticker after cutover

    Returned rather than logged so the caller can tell the operator WHY a label
    failed. "This sticker is from before the reprint" and "this sticker is not
    in the register" send someone to two different places.
    """
    token = (value or "").strip()
    if not token:
        return None, "unknown"

    asset = (
        await db.execute(
            select(FireEquipment)
            .where(FireEquipment.qrToken == token)
            .where(FireEquipment.isDeleted.is_(False))
        )
    ).scalars().first()
    if asset is not None:
        return asset, "token"

    if legacy_scan_enabled():
        # A pre-cutover sticker. Looked up by primary key, which is what those
        # labels encode. Never falls through to this once legacy scan is off.
        legacy = await db.get(FireEquipment, token)
        if legacy is not None and not legacy.isDeleted:
            return legacy, "legacy"

    return None, "unknown"


async def rotate_token(db, asset, *, actor_id: str | None = None) -> str:
    """Issue a new token for a lost or damaged label, revoking the old one.

    Revocation is the overwrite — there is no list of retired tokens to check,
    because the old value is simply gone from the row and the unique lookup that
    used to find it now finds nothing. That is the property the whole change
    exists to provide, so it must not be softened into a grace period.

    `qrLabelPrintedAt` is cleared: the new token has not been printed yet, so
    this asset is once again one whose field sticker does not match its token,
    and the reprint report must say so.
    """
    asset.qrToken = new_token()
    asset.qrTokenGeneratedAt = datetime.now(timezone.utc)
    asset.qrTokenRotations = (asset.qrTokenRotations or 0) + 1
    asset.qrLabelPrintedAt = None
    if actor_id:
        asset.updatedBy = actor_id
    await db.flush()
    return asset.qrToken


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

        # The opaque token. A row that reaches here without one would silently
        # print a label encoding the string "None", which scans successfully and
        # resolves to nothing — the exact failure a printed sticker must never
        # have, since it is discovered in a corridor months later.
        token = asset.get("qrToken")
        if not token:
            raise ValueError(
                f"{asset.get('equipmentCode') or asset.get('id')} has no qrToken; "
                "run backfill_tokens before printing labels."
            )
        qr = BytesIO(render_png(payload_for(token, base=base), scale=6, border=1))
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
    """Mint an opaque `qrToken` for every asset that has none. Idempotent.

    Deliberately NEVER re-mints an asset that already has a token. A second run
    that rotated everything would invalidate labels printed after the first run,
    silently, which is the one thing a backfill must not do — so "already has
    one" is a skip, and reissuing is `rotate_token`'s job, one asset at a time
    and on purpose.

    `qrCode` (the legacy derived value) is left exactly as it is. Those strings
    are what the stickers currently in the field encode, and `resolve()` still
    needs to honour them until the reprint pass is finished.
    """
    rows = (
        await db.execute(select(FireEquipment).where(FireEquipment.isDeleted.is_(False)))
    ).scalars().all()
    minted: list[str] = []
    already = 0
    now = datetime.now(timezone.utc)
    for e in rows:
        if e.qrToken:
            already += 1
            continue
        minted.append(e.equipmentCode)
        if not dry_run:
            e.qrToken = new_token()
            e.qrTokenGeneratedAt = now
            # Left NULL on purpose: a freshly minted token has never been
            # printed, so this asset's field sticker is still the old derived
            # one. That is precisely what the reprint report needs to count.
            e.qrLabelPrintedAt = None
    if not dry_run:
        await db.flush()
    return {
        "total": len(rows), "alreadyHadToken": already,
        "minted": minted, "dryRun": dry_run,
    }


async def mark_printed(db, assets: Iterable[Any]) -> int:
    """Record that a label carrying each asset's CURRENT token was produced.

    Called when a sheet is generated, because that is the only moment the system
    can observe. It means "a label was printed", not "a label was applied to the
    cylinder" — the gap between those two is a physical walk, which is why the
    reprint report presents this as evidence for a human decision rather than as
    an automatic go-ahead for cutover.
    """
    now = datetime.now(timezone.utc)
    count = 0
    for a in assets:
        if a.qrToken:
            a.qrLabelPrintedAt = now
            count += 1
    await db.flush()
    return count


__all__ = [
    "TOKEN_PREFIX", "SCAN_PATH", "TOKEN_BYTES", "base_url", "new_token", "token_for",
    "parse_token", "payload_for", "legacy_scan_enabled", "resolve", "rotate_token",
    "render_png", "render_svg", "sticker_sheet_pdf", "backfill_tokens", "mark_printed",
]
