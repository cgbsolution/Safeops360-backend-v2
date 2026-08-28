"""Env-supplied licence signing keys — the non-production trust escape hatch.

This is trust-root code: whatever `get_public_key` returns is what a licence
signature is verified against. The properties that matter are not "does it load
a key" but the ones that keep it from becoming a way to forge entitlements:

  * an embedded key can never be shadowed by the environment;
  * a malformed or missing value degrades to "no extra keys", never to a crash
    at boot and never to trusting something unparsed;
  * only `*.public.pem` is read, so a private key sitting beside it in the same
    developer directory is not loaded and cannot be mistaken for one.

Offline — no DB, no HTTP, no signing.
"""

from __future__ import annotations

import json

import pytest

from app.licensing import keys

REAL_KID = "vf-2026-06"
FAKE_PEM = (
    "-----BEGIN PUBLIC KEY-----\n"
    "MCowBQYDK2VwAyEA8O4+nFpGGG3hy4IHciCYv3o8Gfws0UTcIJ9tvqbg0v4=\n"
    "-----END PUBLIC KEY-----\n"
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv(keys._ENV_VAR, raising=False)
    keys._env_cache = None
    yield
    keys._env_cache = None


def test_no_env_var_means_only_the_embedded_keys(monkeypatch):
    assert keys.trusted_kids() == [REAL_KID]
    assert keys.get_public_key(REAL_KID) is not None
    assert keys.get_public_key("anything-else") is None


def test_json_form_adds_a_key(monkeypatch):
    monkeypatch.setenv(keys._ENV_VAR, json.dumps({"dev-1": FAKE_PEM}))
    assert keys.get_public_key("dev-1") == FAKE_PEM
    assert set(keys.trusted_kids()) == {REAL_KID, "dev-1"}


def test_directory_form_reads_public_pems_and_takes_the_kid_from_the_filename(tmp_path, monkeypatch):
    (tmp_path / "dev-2.public.pem").write_text(FAKE_PEM, encoding="utf-8")
    monkeypatch.setenv(keys._ENV_VAR, str(tmp_path))
    assert keys.get_public_key("dev-2").strip() == FAKE_PEM.strip()


def test_directory_form_ignores_private_keys(tmp_path, monkeypatch):
    # A genkey run writes both files side by side. Loading the private one would
    # be useless here and alarming in a log; it must simply not be read.
    (tmp_path / "dev-3.public.pem").write_text(FAKE_PEM, encoding="utf-8")
    (tmp_path / "dev-3.private.pem").write_text("-----BEGIN PRIVATE KEY-----\nx\n", encoding="utf-8")
    monkeypatch.setenv(keys._ENV_VAR, str(tmp_path))
    kids = keys.trusted_kids()
    assert "dev-3" in kids
    assert not any("private" in k for k in kids)


def test_directory_form_skips_files_that_are_not_pem_public_keys(tmp_path, monkeypatch):
    (tmp_path / "junk.public.pem").write_text("not a key at all", encoding="utf-8")
    monkeypatch.setenv(keys._ENV_VAR, str(tmp_path))
    assert keys.get_public_key("junk") is None


# ── the properties that stop this becoming a forgery route ───────────────────
def test_env_can_never_shadow_an_embedded_key(monkeypatch):
    """The whole security argument. If an attacker-supplied env var could
    redefine vf-2026-06, every real client licence could be forged."""
    embedded = keys.get_public_key(REAL_KID)
    monkeypatch.setenv(keys._ENV_VAR, json.dumps({REAL_KID: FAKE_PEM}))
    assert keys.get_public_key(REAL_KID) == embedded
    assert keys.get_public_key(REAL_KID) != FAKE_PEM


@pytest.mark.parametrize("value", ["{bad json", "[]", '"a string"', "{}", "   "])
def test_malformed_values_degrade_to_no_extra_keys(monkeypatch, value):
    monkeypatch.setenv(keys._ENV_VAR, value)
    assert keys.trusted_kids() == [REAL_KID]  # never raises, never widens


def test_missing_directory_degrades_to_no_extra_keys(monkeypatch, tmp_path):
    monkeypatch.setenv(keys._ENV_VAR, str(tmp_path / "does-not-exist"))
    assert keys.trusted_kids() == [REAL_KID]


def test_cache_rekeys_when_the_variable_changes(tmp_path, monkeypatch):
    (tmp_path / "dev-4.public.pem").write_text(FAKE_PEM, encoding="utf-8")
    monkeypatch.setenv(keys._ENV_VAR, str(tmp_path))
    assert keys.get_public_key("dev-4") is not None
    monkeypatch.delenv(keys._ENV_VAR)
    assert keys.get_public_key("dev-4") is None, "unsetting the var must drop the trust"
