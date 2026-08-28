"""Chemical / Hazmat permission gate — the migration ramp and the code map.

The risk in this module is not the happy path, it is the ramp: `require()` has
to tell "the CHEMICAL permission does not exist yet" apart from "it exists and
this user does not hold it", because `can()` reports both as simply not-allowed.
Get that wrong in one direction and every user 403s the moment the code ships;
get it wrong in the other and the contractor keeps the hazmat inventory forever.

Offline — no DB, no HTTP. `can` and the seeded-probe are stubbed.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.services import chemical_permissions as chemperm


class _Res:
    def __init__(self, allowed: bool, reason: str | None = None):
        self.allowed = allowed
        self.reason = reason


@pytest.fixture(autouse=True)
def _clear_cache():
    chemperm.reset_cache()
    yield
    chemperm.reset_cache()


def _stub_can(monkeypatch, granted: set[str]):
    """Stub `can` so only `granted` codes pass. Records what was asked."""
    asked: list[str] = []

    async def fake_can(db, user_id, code, ctx):
        asked.append(code)
        return _Res(code in granted, None if code in granted else f"missing {code}")

    monkeypatch.setattr(chemperm, "can", fake_can)
    return asked


def _stub_seeded(monkeypatch, seeded: bool):
    async def fake_seeded(db):
        return seeded

    monkeypatch.setattr(chemperm, "chemical_rbac_seeded", fake_seeded)


_USER = SimpleNamespace(id="u1")


# ── the code map ─────────────────────────────────────────────────────────────
def test_every_code_has_a_legacy_equivalent():
    # A code with no legacy mapping raises rather than falling through to
    # whoever held INCIDENT.UPDATE — but there should not be one yet, because
    # every current chemical endpoint existed under the borrowed grants.
    for code in (chemperm.READ, chemperm.CREATE, chemperm.UPDATE, chemperm.CONFIGURE):
        assert code in chemperm._LEGACY


def test_configure_does_not_degrade_to_the_write_grant():
    # The ramp must never briefly hand the regulatory-threshold masters to
    # whoever could book stock in.
    assert chemperm._LEGACY[chemperm.CONFIGURE] == "CONFIGURATION.MASTERS"
    assert chemperm._LEGACY[chemperm.UPDATE] == "INCIDENT.UPDATE"
    assert chemperm._LEGACY[chemperm.READ] == "INCIDENT.READ"


def test_codes_are_namespaced_to_the_module():
    for code in (chemperm.READ, chemperm.CREATE, chemperm.UPDATE, chemperm.CONFIGURE):
        assert code.startswith("CHEMICAL.")


# ── the ramp: RBAC not yet reseeded ──────────────────────────────────────────
@pytest.mark.asyncio
async def test_unseeded_rbac_falls_back_to_the_legacy_code(monkeypatch):
    _stub_seeded(monkeypatch, False)
    asked = _stub_can(monkeypatch, {"INCIDENT.READ"})
    await chemperm.require(None, _USER, chemperm.READ)  # must not raise
    assert asked == ["INCIDENT.READ"], "the ramp must ask the legacy code, not CHEMICAL.*"


@pytest.mark.asyncio
async def test_unseeded_rbac_still_denies_someone_without_the_legacy_grant(monkeypatch):
    _stub_seeded(monkeypatch, False)
    _stub_can(monkeypatch, set())
    with pytest.raises(HTTPException) as e:
        await chemperm.require(None, _USER, chemperm.READ)
    assert e.value.status_code == 403


@pytest.mark.asyncio
async def test_unseeded_ramp_never_widens_configure_to_the_write_grant(monkeypatch):
    # Someone holding only the old write grant must NOT get the threshold
    # masters while the ramp is active.
    _stub_seeded(monkeypatch, False)
    _stub_can(monkeypatch, {"INCIDENT.UPDATE", "INCIDENT.READ"})
    with pytest.raises(HTTPException):
        await chemperm.require(None, _USER, chemperm.CONFIGURE)


# ── after reseed: the real grants apply, and the defect closes ───────────────
@pytest.mark.asyncio
async def test_seeded_rbac_uses_the_chemical_code_only(monkeypatch):
    _stub_seeded(monkeypatch, True)
    asked = _stub_can(monkeypatch, {chemperm.READ})
    await chemperm.require(None, _USER, chemperm.READ)
    assert asked == [chemperm.READ]
    assert "INCIDENT.READ" not in asked


@pytest.mark.asyncio
async def test_seeded_rbac_denies_the_holder_of_only_the_legacy_grant(monkeypatch):
    # THE FIX. Once seeded, a contractor holding INCIDENT.READ — and nothing
    # else — can no longer read the hazmat inventory.
    _stub_seeded(monkeypatch, True)
    _stub_can(monkeypatch, {"INCIDENT.READ", "INCIDENT.UPDATE"})
    with pytest.raises(HTTPException) as e:
        await chemperm.require(None, _USER, chemperm.READ)
    assert e.value.status_code == 403


@pytest.mark.asyncio
async def test_allowed_is_non_raising_and_agrees_with_require(monkeypatch):
    _stub_seeded(monkeypatch, True)
    _stub_can(monkeypatch, {chemperm.READ})
    assert await chemperm.allowed(None, _USER, chemperm.READ) is True
    assert await chemperm.allowed(None, _USER, chemperm.CONFIGURE) is False


@pytest.mark.asyncio
async def test_capabilities_reports_every_code_and_the_seed_state(monkeypatch):
    _stub_seeded(monkeypatch, True)
    _stub_can(monkeypatch, {chemperm.READ, chemperm.CREATE})
    caps = await chemperm.capabilities(None, _USER)
    assert caps == {
        "read": True, "create": True, "update": False, "configure": False,
        "rbacSeeded": True,
    }
