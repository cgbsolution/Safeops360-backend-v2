"""Assurance integrity — canonical hashing and competence verdicts.

Design: [docs/cams/09-module-completion.md](../../docs/cams/09-module-completion.md) §2.2, §2.5.

The hashing tests pin the invariant that would otherwise be got wrong later:
`snapshotHash` is computed over the snapshot and then *inserted into it*, so any
verification must remove the hash keys before rehashing. Getting that wrong makes
every report read as tampered.
"""

from __future__ import annotations

from app.services.assurance import canonical_hash
from app.services.audit_compliance import _canonical_hash as legacy_hash


# ── canonical_hash ───────────────────────────────────────────────────


def test_full_hash_is_64_hex_chars():
    h = canonical_hash({"a": 1})
    assert len(h) == 64 and all(c in "0123456789abcdef" for c in h)


def test_short_form_is_the_prefix_of_the_full_form():
    obj = {"b": 2, "a": [1, 2, {"z": None}]}
    assert canonical_hash(obj, full=False) == canonical_hash(obj, full=True)[:16]


def test_hash_is_key_order_independent():
    """`sort_keys=True` is what makes the digest reproducible across processes;
    without it a dict rebuilt in another order would read as tampered."""
    assert canonical_hash({"a": 1, "b": 2}) == canonical_hash({"b": 2, "a": 1})


def test_hash_is_sensitive_to_values():
    assert canonical_hash({"a": 1}) != canonical_hash({"a": 2})


def test_hash_matches_the_legacy_16_char_implementation():
    """The generator's existing `_canonical_hash` and the new verifier MUST agree,
    or every report generated before this change verifies as a mismatch."""
    obj = {"reportType": "FINAL", "checkpointsTotal": 82, "nested": {"x": [1, None, "s"]}}
    assert legacy_hash(obj) == canonical_hash(obj, full=False)


def test_hash_tolerates_non_json_types_via_default_str():
    """Snapshots carry datetimes; `default=str` is why hashing them does not raise."""
    from datetime import datetime

    h = canonical_hash({"generatedAt": datetime(2026, 7, 26)})
    assert len(h) == 64


def test_verification_round_trip_after_stripping_the_hash_keys():
    """The load-bearing invariant, asserted end to end: build a snapshot, embed
    the prefix the way the generator does, then strip both keys and rehash."""
    snapshot = {"reportType": "FINAL", "checkpointsAssessed": 82, "overallScorePct": 78.1}
    full = canonical_hash(snapshot, full=True)
    snapshot["snapshotHash"] = full[:16]  # exactly what generate_report does

    verify_target = dict(snapshot)
    stored_short = verify_target.pop("snapshotHash")
    verify_target.pop("snapshotHashFull", None)
    recomputed = canonical_hash(verify_target, full=True)

    assert recomputed == full
    assert recomputed[:16] == stored_short


def test_a_mutated_snapshot_fails_verification():
    snapshot = {"overallScorePct": 78.1}
    full = canonical_hash(snapshot, full=True)
    snapshot["snapshotHash"] = full[:16]

    snapshot["overallScorePct"] = 99.9  # someone edits the score
    target = dict(snapshot)
    target.pop("snapshotHash")
    assert canonical_hash(target, full=True) != full
