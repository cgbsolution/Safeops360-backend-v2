"""Waves 3-5 completion - i18n, evidence pack, notification preferences.

Design: docs/cams/09 §2.6, §3.3, §3.6.

Pure cores only (house style). The properties pinned are the ones where a wrong
default is silently harmful rather than obviously broken.
"""

from __future__ import annotations

import json
import zipfile

import pytest

from app.services.cams_i18n import (
    DEFAULT_LANGUAGE,
    LANGUAGES,
    ResolvedText,
    is_supported,
    list_languages,
    normalise,
)
from app.services.cams_notifications import (
    CATALOGUE,
    CLASS_LABEL,
    EVENT_CLASS,
    EVENT_CLASSES,
    IMMEDIATE_EMAIL,
    event_class,
)
from app.services.evidence_pack import PackBuilder, _canonical_json, _sha256


# ── WP-46 i18n (Q18: en + hi only) ───────────────────────────────────


def test_only_english_and_hindi_ship():
    """Q18 answered NO to Tamil and Kannada. Shipping them anyway would be
    inventing a decision the user already made."""
    assert set(LANGUAGES) == {"en", "hi"}
    assert DEFAULT_LANGUAGE == "en"


def test_unsupported_language_degrades_to_english_rather_than_erroring():
    """A field auditor whose device reports ta-IN must get a working screen, not
    a 400 halfway up a staircase."""
    assert normalise("ta") == "en"
    assert normalise("kn-IN") == "en"
    assert normalise(None) == "en"
    assert normalise("") == "en"


def test_regional_variants_resolve_to_the_base_language():
    assert normalise("hi-IN") == "hi"
    assert normalise("en-GB") == "en"


def test_is_supported_is_exact():
    assert is_supported("hi") is True
    assert is_supported("ta") is False


def test_every_language_declares_a_native_label():
    """A language picker showing "Hindi" to a Hindi speaker is a picker they
    have to translate in their head."""
    for meta in list_languages():
        assert meta["nativeLabel"] and meta["dir"] in ("ltr", "rtl")


def test_fallback_text_announces_itself():
    """The load-bearing i18n property: a field auditor reading a safety question
    must know whether it is a reviewed translation or the English original."""
    fb = ResolvedText("Are exits clear?", "en", False, "SOURCE").as_dict()
    assert fb["isTranslated"] is False
    assert "no translation" in fb["fallbackNotice"].lower()


def test_a_real_translation_carries_no_fallback_notice():
    t = ResolvedText("क्या निकास साफ़ है?", "hi", True, "HUMAN", reviewed=True).as_dict()
    assert t["isTranslated"] is True and t["fallbackNotice"] == ""
    assert t["reviewed"] is True


def test_machine_and_human_translations_are_distinguishable():
    """A machine translation of a safety question is not the same artefact as a
    reviewed one."""
    m = ResolvedText("x", "hi", True, "MACHINE").as_dict()
    h = ResolvedText("x", "hi", True, "HUMAN", reviewed=True).as_dict()
    assert m["source"] != h["source"]
    assert m["reviewed"] is False


# ── WP-43 notification preferences ───────────────────────────────────


def test_every_catalogued_event_belongs_to_a_class():
    """An event with no class would land in a bucket nobody has a preference row
    for, and be silently muted."""
    for code in CATALOGUE:
        assert code in EVENT_CLASS, code
        assert EVENT_CLASS[code] in EVENT_CLASSES


def test_unknown_event_gets_a_real_class_not_a_void():
    assert event_class("SOMETHING_NEW") in EVENT_CLASSES


def test_every_class_has_a_human_label():
    for cls in EVENT_CLASSES:
        assert CLASS_LABEL.get(cls)


def test_urgent_events_are_spread_across_classes():
    """If every immediate-email event sat in one class, muting that class would
    silence all of them at once."""
    classes = {event_class(e) for e in IMMEDIATE_EMAIL}
    assert len(classes) > 1


# ── WP-40 evidence pack ──────────────────────────────────────────────


def test_pack_is_byte_identical_across_builds():
    """Determinism is what makes the manifest hash mean anything: two exports of
    an unchanged audit must not differ."""
    def build():
        b = PackBuilder()
        b.add_json("engagement/audit.json", {"b": 2, "a": 1})
        b.add_json("findings/index.json", [{"code": "F1"}])
        return b.seal()[0]

    assert build() == build()


def test_key_order_does_not_change_the_bytes():
    b1, b2 = PackBuilder(), PackBuilder()
    b1.add_json("x.json", {"a": 1, "b": 2})
    b2.add_json("x.json", {"b": 2, "a": 1})
    assert b1.seal()[0] == b2.seal()[0]


def test_manifest_carries_a_hash_per_entry():
    b = PackBuilder()
    b.add_json("a.json", {"x": 1})
    b.add_bytes("b.txt", b"hello")
    _, man = b.seal()
    entries = {m["path"]: m for m in man}
    assert entries["b.txt"]["sha256"] == _sha256(b"hello")
    assert entries["a.json"]["sha256"] == _sha256(_canonical_json({"x": 1}))


def test_missing_artefacts_are_recorded_not_skipped():
    """A pack that quietly omits 40 unreachable photos LOOKS complete and is not.
    The recipient must be able to see the gap."""
    b = PackBuilder()
    b.add_json("a.json", {"x": 1})
    b.record_failure("evidence/CP-1/0", "object unreachable")
    _, man = b.seal()
    missing = [m for m in man if m["kind"] == "MISSING"]
    assert len(missing) == 1
    assert missing[0]["sha256"] is None
    assert "unreachable" in missing[0]["reason"]


def test_manifest_is_inside_the_archive():
    """Self-verification requires the manifest to travel WITH the pack —
    a recipient has no access to this system."""
    b = PackBuilder()
    b.add_json("a.json", {"x": 1})
    data, _ = b.seal()
    with zipfile.ZipFile(__import__("io").BytesIO(data)) as z:
        assert "manifest.json" in z.namelist()
        man = json.loads(z.read("manifest.json"))
        assert man["manifestVersion"] == 1
        assert man["entryCount"] >= 1


def test_archive_entries_have_a_pinned_timestamp():
    """Python stamps "now" into ZIP headers by default, which would make two
    identical exports hash differently."""
    b = PackBuilder()
    b.add_json("a.json", {"x": 1})
    data, _ = b.seal()
    with zipfile.ZipFile(__import__("io").BytesIO(data)) as z:
        for info in z.infolist():
            assert info.date_time == (1980, 1, 1, 0, 0, 0)


def test_missing_count_is_reported_in_the_manifest_header():
    b = PackBuilder()
    b.record_failure("evidence/x", "gone")
    data, _ = b.seal()
    with zipfile.ZipFile(__import__("io").BytesIO(data)) as z:
        assert json.loads(z.read("manifest.json"))["missingCount"] == 1
