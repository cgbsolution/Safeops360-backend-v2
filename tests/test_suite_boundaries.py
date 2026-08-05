"""P3-7 — Suite-boundary regression guard.

Verifies the licensing MODEL: each edition's resolved module set excludes the
modules that edition must not license. Because the FastAPI router_map gates every
module on its code (require_module), an edition that doesn't resolve a module
cannot reach its endpoints — so asserting the resolved set is the boundary check.

Run: pytest tests/test_suite_boundaries.py
"""

import pytest

from app.licensing.editions import EDITIONS
from app.licensing.registry import resolve_dependencies


def _modules(edition_code: str) -> set[str]:
    ed = EDITIONS[edition_code]
    return set(resolve_dependencies(ed.included_modules))


# (edition, modules that MUST NOT be licensed in it)
BOUNDARY_CASES = [
    ("CAMS_ONLY", {"ERM", "INCIDENT", "PTW", "HIRA", "FACILITIES", "FIRE"}),
    ("IMS_CORE", {"ERM", "KRI", "APPETITE", "BCM", "CONTROL", "VENDOR", "INSURANCE", "FACILITIES"}),
]


@pytest.mark.parametrize("edition,blocked", BOUNDARY_CASES)
def test_edition_excludes_unlicensed_modules(edition, blocked):
    enabled = _modules(edition)
    leaked = blocked & enabled
    assert not leaked, f"{edition} must NOT license {leaked}"


def test_full_platform_includes_everything():
    full = _modules("FULL_PLATFORM")
    # FULL includes the new modules added in P1/P2 too
    for code in ("ERM", "CAMS", "FACILITIES", "FIRE"):
        assert code in full, f"FULL_PLATFORM should license {code}"


def test_ims_core_includes_fire_and_cams():
    ims = _modules("IMS_CORE")
    assert "CAMS" in ims and "FIRE" in ims and "INCIDENT" in ims


def test_dependency_expansion_pulls_erm_base():
    # KRI depends on ERM — any edition with KRI must also resolve ERM
    for ed in EDITIONS.values():
        mods = set(resolve_dependencies(ed.included_modules))
        if "KRI" in mods:
            assert "ERM" in mods, f"{ed.code}: KRI without ERM base"
