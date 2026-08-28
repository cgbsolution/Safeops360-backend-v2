"""Opaque fire-asset QR tokens — minting, revocation, and the legacy ramp.

The sticker value used to be the row's own id. The properties worth testing are
therefore not "does it make a string" but the two the change exists to provide:

  * a token must not be derivable from the asset, or from another token;
  * rotating must REVOKE — the old value has to stop resolving immediately,
    with no grace period, because the reason to rotate is usually that the old
    label is somewhere you no longer control.

Plus the transition property that keeps the estate scannable: pre-reprint
stickers resolve while `FIRE_QR_LEGACY_SCAN` is on, and stop the moment it is off.

Offline — the DB is a small fake, so this runs without Postgres.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.services import fire_qr as qrsvc


# ── minting ──────────────────────────────────────────────────────────────────
def test_tokens_are_unique_across_many_mints():
    tokens = {qrsvc.new_token() for _ in range(2000)}
    assert len(tokens) == 2000


def test_token_is_not_derivable_from_the_asset():
    # The whole point: nothing about the asset appears in its token.
    asset_id = "0a60844d44064ffc9f5c280109da7fed"
    code = "FE-ACS-0013"
    for _ in range(50):
        t = qrsvc.new_token()
        assert asset_id not in t
        assert code not in t
        assert code.lower() not in t.lower()


def test_token_carries_enough_entropy_to_be_unguessable():
    t = qrsvc.new_token()
    # 32 bytes base64url ≈ 43 chars. Short enough to keep the QR sparse enough
    # to scan off a 25 mm label; long enough that guessing is not a threat model.
    assert len(t) >= 40
    assert qrsvc.TOKEN_BYTES >= 32


def test_tokens_are_not_ordered_or_sequential():
    # Two tokens minted back to back must share no meaningful prefix — an
    # ordered value would let one sticker suggest its neighbours.
    a, b = qrsvc.new_token(), qrsvc.new_token()
    shared = 0
    for x, y in zip(a, b):
        if x != y:
            break
        shared += 1
    assert shared <= 2


# ── the sticker payload ──────────────────────────────────────────────────────
def test_payload_encodes_the_token_and_never_the_asset_id():
    token = qrsvc.new_token()
    payload = qrsvc.payload_for(token, base="https://safeops.example")
    assert payload == f"https://safeops.example{qrsvc.SCAN_PATH}/{token}"


def test_round_trip_through_both_sticker_forms():
    token = qrsvc.new_token()
    assert qrsvc.parse_token(qrsvc.token_for(token)) == token
    assert qrsvc.parse_token(qrsvc.payload_for(token, base="https://x.example")) == token
    assert qrsvc.parse_token("") is None
    assert qrsvc.parse_token("something else entirely") is None


# ── resolution + the legacy ramp ─────────────────────────────────────────────
class _FakeDb:
    """Just enough of an AsyncSession: token lookup and get-by-id."""

    def __init__(self, assets):
        self._assets = assets
        self.flushed = False

    async def execute(self, _stmt):
        # The only select() this service issues is the qrToken lookup; the value
        # is pulled off the compiled statement's bind parameters.
        wanted = [
            v for v in _stmt.compile().params.values() if isinstance(v, str)
        ]
        match = next(
            (a for a in self._assets if a.qrToken and a.qrToken in wanted and not a.isDeleted),
            None,
        )
        return SimpleNamespace(scalars=lambda: SimpleNamespace(first=lambda: match))

    async def get(self, _model, key):
        return next((a for a in self._assets if a.id == key), None)

    async def flush(self):
        self.flushed = True


def _asset(**kw):
    base = dict(id="a1", qrToken=None, isDeleted=False, qrTokenRotations=0,
                qrTokenGeneratedAt=None, qrLabelPrintedAt=None, updatedBy=None)
    base.update(kw)
    return SimpleNamespace(**base)


@pytest.mark.asyncio
async def test_resolves_by_opaque_token(monkeypatch):
    a = _asset(qrToken="tok-abc")
    db = _FakeDb([a])
    found, how = await qrsvc.resolve(db, "tok-abc")
    assert found is a and how == "token"


@pytest.mark.asyncio
async def test_legacy_sticker_resolves_while_the_ramp_is_on(monkeypatch):
    monkeypatch.setenv("FIRE_QR_LEGACY_SCAN", "1")
    a = _asset(id="legacy-id", qrToken="tok-abc")
    db = _FakeDb([a])
    found, how = await qrsvc.resolve(db, "legacy-id")
    assert found is a and how == "legacy"


@pytest.mark.asyncio
async def test_legacy_sticker_stops_resolving_after_cutover(monkeypatch):
    # Verification item 3. Same sticker, same asset, ramp off → dead.
    monkeypatch.setenv("FIRE_QR_LEGACY_SCAN", "0")
    a = _asset(id="legacy-id", qrToken="tok-abc")
    db = _FakeDb([a])
    found, how = await qrsvc.resolve(db, "legacy-id")
    assert found is None and how == "unknown"
    # …while the opaque token still works, so cutover is not an outage.
    still, how2 = await qrsvc.resolve(db, "tok-abc")
    assert still is a and how2 == "token"


@pytest.mark.asyncio
async def test_a_deleted_asset_never_resolves(monkeypatch):
    monkeypatch.setenv("FIRE_QR_LEGACY_SCAN", "1")
    a = _asset(qrToken="tok-abc", isDeleted=True)
    db = _FakeDb([a])
    assert (await qrsvc.resolve(db, "tok-abc"))[0] is None


@pytest.mark.asyncio
async def test_unknown_and_empty_values_resolve_to_nothing():
    db = _FakeDb([_asset(qrToken="tok-abc")])
    assert (await qrsvc.resolve(db, "nope"))[0] is None
    assert (await qrsvc.resolve(db, ""))[0] is None


@pytest.mark.parametrize(
    "value,expected", [("0", False), ("false", False), ("no", False), ("off", False),
                       ("1", True), ("true", True), ("", True)],
)
def test_legacy_flag_parsing(monkeypatch, value, expected):
    monkeypatch.setenv("FIRE_QR_LEGACY_SCAN", value)
    assert qrsvc.legacy_scan_enabled() is expected


def test_legacy_scanning_defaults_on(monkeypatch):
    # The default has to keep the field estate working: a token-only default
    # would kill every sticker already on a cylinder the moment this deploys.
    monkeypatch.delenv("FIRE_QR_LEGACY_SCAN", raising=False)
    assert qrsvc.legacy_scan_enabled() is True


# ── rotation = revocation ────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_rotating_revokes_the_old_token_immediately(monkeypatch):
    monkeypatch.setenv("FIRE_QR_LEGACY_SCAN", "0")
    a = _asset(qrToken="old-token", qrLabelPrintedAt="2026-08-01", qrTokenRotations=0)
    db = _FakeDb([a])
    assert (await qrsvc.resolve(db, "old-token"))[0] is a

    new = await qrsvc.rotate_token(db, a, actor_id="u1")

    assert new != "old-token"
    assert a.qrToken == new
    # THE property. No grace period: the reason to rotate is that the old label
    # is somewhere you no longer control.
    assert (await qrsvc.resolve(db, "old-token"))[0] is None
    assert (await qrsvc.resolve(db, new))[0] is a


@pytest.mark.asyncio
async def test_rotating_marks_the_label_unprinted_and_counts_the_rotation():
    a = _asset(qrToken="old", qrLabelPrintedAt="2026-08-01", qrTokenRotations=2)
    await qrsvc.rotate_token(_FakeDb([a]), a)
    # Back in the reprint queue — the cylinder now carries a dead label.
    assert a.qrLabelPrintedAt is None
    assert a.qrTokenRotations == 3


@pytest.mark.asyncio
async def test_rotation_does_not_retain_the_previous_token_anywhere():
    a = _asset(qrToken="old-secret")
    await qrsvc.rotate_token(_FakeDb([a]), a)
    assert "old-secret" not in str(vars(a))


# ── the sticker sheet ────────────────────────────────────────────────────────
def test_sheet_refuses_an_asset_with_no_token():
    # Would otherwise encode the string "None" — a label that scans perfectly
    # and resolves to nothing, discovered months later on a cylinder.
    with pytest.raises(ValueError, match="no qrToken"):
        qrsvc.sticker_sheet_pdf([{"id": "a1", "equipmentCode": "FE-1", "location": "Bay"}])


def test_sheet_renders_from_the_token():
    token = qrsvc.new_token()
    pdf = qrsvc.sticker_sheet_pdf(
        [{"id": "a1", "qrToken": token, "equipmentCode": "FE-ACS-0011",
          "allottedSerialNo": "36773", "location": "Cutting Hall"}],
        base="https://safeops.example",
    )
    assert pdf.startswith(b"%PDF")
    assert len(pdf) > 1000
