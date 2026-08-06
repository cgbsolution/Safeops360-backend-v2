"""Shared cross-module trigger/rule engine.

WHY THIS EXISTS
───────────────
Before this file the platform had three independent post-closure rule runners
with three different reliability contracts:

  * ``incident_post_closure.run_incident_post_closure_rules`` — hardened: a
    SAVEPOINT per rule, ``logger.exception`` on failure, and an audit entry
    carrying ``error: True`` so a crash is visible rather than invisible.
  * ``post_closure_rules_nm.run_near_miss_post_closure_rules`` — a crash was
    ``print(..., file=sys.stderr)`` and an audit entry with no operator ever
    told. On Azure App Service stderr is not the place anyone looks.
  * ``post_closure_rules.run_post_closure_rules`` (observation) — same, PLUS
    the *persistence* of the audit itself was wrapped in a bare try/except that
    printed and moved on, so a failed write meant no trace of the run at all.

That divergence is the actual defect class behind the incident→HIRA trigger
report ("fired 0 of 22, no notification, exceptions swallowed"). The matching
and notification bugs in that specific rule have since been fixed in place (see
``incident_post_closure._rule_hira_review_trigger``), but the *pattern* that
allowed them to go unnoticed for 22 closures was never fixed — it was fixed
once, in one rule, in one of three runners.

This module is that fix, made once, at the shared level. Every trigger written
from here on — including the Chemical module's threshold→MOC trigger, which is
that module's core value proposition — runs through ``run_trigger_rules`` and
therefore inherits, without the rule author having to remember any of it:

  1. **Isolation** — each rule runs in its own SAVEPOINT. One rule raising
     cannot poison the outer transaction or stop later rules.
  2. **No silent failure** — an exception is caught, logged with a stack trace
     via ``logger.exception``, AND converted into a ``FAILED`` result whose
     ``failure_reason`` is *guaranteed non-empty* (a bare ``raise ValueError()``
     still yields "ValueError (no message)", never "").
  3. **Someone is told** — every FAILED result notifies a resolved human
     audience. A trigger that fails into a log file nobody reads is
     operationally identical to a trigger that never ran.
  4. **Durable audit** — results go to a sink (module-specific table, JSON
     column, whatever), and a sink that itself explodes is reported rather than
     printed. Persisting the audit is not best-effort.
  5. **Observability** — FIRED and FAILED both emit a DomainEvent, so the
     "0 of 22" question is answerable with a SQL query instead of a prod
     investigation.

DESIGN NOTE — why a sink callback rather than one shared log table
──────────────────────────────────────────────────────────────────
The three existing runners persist their audit into module-owned JSON columns
(``Incident.closureTriggers`` etc.) and the Chemical module's spec calls for a
first-class ``MocTriggerLog`` table. Forcing all of them onto one central table
would be a data migration this change does not need and cannot safely do.
Instead the *contract* is shared and the *storage* is pluggable: pass a sink,
get the guarantees. ``json_column_sink`` reproduces the legacy shape exactly,
so adopting the engine in an existing runner is behaviour-preserving apart from
the reliability improvements.
"""

from __future__ import annotations

import logging
import traceback
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Awaitable, Callable, Protocol, Sequence

from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)


# ── outcome vocabulary ────────────────────────────────────────────────────────
class TriggerOutcome(str, Enum):
    """Deliberately three-valued.

    Two values (fired / didn't fire) is what the old runners had, and it is why
    a crash was indistinguishable from a rule correctly deciding "not
    applicable". SKIPPED means "evaluated cleanly, conditions not met"; FAILED
    means "we do not know what should have happened". They must never share a
    bucket — the whole point of the audit is that FAILED is actionable and
    SKIPPED is not.
    """

    FIRED = "FIRED"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"


@dataclass
class TriggerResult:
    """What one rule returns (or what the engine synthesises when it raises)."""

    rule_name: str
    outcome: TriggerOutcome
    #: Stable machine key. The observation AI panel and the near-miss detail
    #: page look entries up by ``ruleId`` (e.g. "rule_lessons_distribution"),
    #: so it is part of the persisted contract, not decoration.
    rule_id: str | None = None
    reason: str = ""
    #: Failure detail. The engine enforces that this is non-empty whenever
    #: outcome is FAILED — see ``_coerce_failure_reason``.
    failure_reason: str | None = None
    #: Type + id of anything the rule created (an MOC, a review cycle, a CAPA).
    spawned_record_type: str | None = None
    spawned_record_id: str | None = None
    #: Free-form rule payload for the UI / downstream consumers.
    data: dict[str, Any] = field(default_factory=dict)
    #: Full traceback, kept out of ``failure_reason`` so the audit stays short
    #: and readable while the detail is still recoverable.
    stack: str | None = None

    @property
    def fired(self) -> bool:
        return self.outcome is TriggerOutcome.FIRED

    def to_audit_entry(self) -> dict[str, Any]:
        """Legacy-compatible dict shape used by the JSON-column sinks and the
        existing UI, extended with the fields the old shape could not express.

        ``fired`` stays a boolean because three UIs and several tests read it;
        ``status`` carries the information ``fired`` throws away."""
        entry: dict[str, Any] = {
            "ruleId": self.rule_id,
            "ruleName": self.rule_name,
            "fired": self.outcome is TriggerOutcome.FIRED,
            "status": self.outcome.value,
            "reason": self.reason or (self.failure_reason or ""),
        }
        if self.outcome is TriggerOutcome.FAILED:
            entry["error"] = True
            entry["failureReason"] = self.failure_reason
        if self.spawned_record_type:
            entry["spawnedRecordType"] = self.spawned_record_type
        if self.spawned_record_id:
            entry["spawnedRecordId"] = self.spawned_record_id
        if self.data:
            entry["data"] = self.data
        return entry


# ── rule + sink protocols ─────────────────────────────────────────────────────
#: A rule is any async callable taking (session, subject) and returning a
#: TriggerResult. Returning None is treated as SKIPPED so trivial rules stay
#: terse; raising is caught and converted to FAILED.
TriggerRule = Callable[[AsyncSession, Any], Awaitable["TriggerResult | None"]]


class TriggerSink(Protocol):
    """Where results are durably recorded.

    Called ONCE with the full result list, inside its own SAVEPOINT. A sink is
    allowed to raise — the engine reports the failure loudly instead of
    swallowing it, which is precisely what the observation runner got wrong."""

    async def __call__(
        self, db: AsyncSession, subject: Any, results: list[TriggerResult]
    ) -> None: ...


@dataclass
class TriggerRun:
    """Outcome of a whole run — what the caller and the UI get back."""

    source_kind: str
    source_id: str
    results: list[TriggerResult]
    #: True when the audit could not be persisted. The results are still
    #: returned so an operator can act on them; the flag says the trail is
    #: incomplete and must not be read as "nothing happened".
    sink_failed: bool = False
    sink_error: str | None = None

    @property
    def fired(self) -> list[TriggerResult]:
        return [r for r in self.results if r.outcome is TriggerOutcome.FIRED]

    @property
    def failed(self) -> list[TriggerResult]:
        return [r for r in self.results if r.outcome is TriggerOutcome.FAILED]

    def audit_entries(self) -> list[dict[str, Any]]:
        return [r.to_audit_entry() for r in self.results]


# ── failure-reason coercion ───────────────────────────────────────────────────
def _coerce_failure_reason(exc: BaseException) -> str:
    """Never return an empty string.

    ``str(KeyError('plantId'))`` is ``"'plantId'"`` and ``str(ValueError())`` is
    ``""``. A FAILED row whose failureReason is blank is exactly as useless as
    no row at all, and MocTriggerLog's spec calls this out explicitly, so the
    class name is always part of the message."""
    detail = str(exc).strip()
    name = type(exc).__name__
    if not detail:
        return f"{name} (no message)"
    return f"{name}: {detail}"[:500]


# ── the engine ────────────────────────────────────────────────────────────────
async def run_trigger_rules(
    db: AsyncSession,
    rules: Sequence[TriggerRule],
    subject: Any,
    *,
    source_kind: str,
    source_id: str,
    sink: TriggerSink | None = None,
    site_id: str | None = None,
    failure_audience_roles: Sequence[str] = ("HSE_MANAGER",),
    failure_audience_user_ids: Sequence[str] = (),
    emit_events: bool = True,
) -> TriggerRun:
    """Run ``rules`` against ``subject`` with the reliability contract above.

    Args:
        source_kind: entity type driving the run ("ChemicalInventoryItem",
            "Incident", ...). Used for the audit trail and DomainEvents.
        source_id: id of that entity.
        sink: durable recorder for the results. Omitting it is legitimate only
            for read-only/dry-run evaluation; a warning is logged so an
            accidentally sink-less production trigger is visible.
        failure_audience_roles: who gets told when a rule FAILS. Defaults to
            HSE_MANAGER per the Chemical module's §4.3 requirement.
        emit_events: also write a DomainEvent per FIRED/FAILED result.

    Never raises for rule-level problems. It can only raise if the caller's
    session is already unusable, which is not a condition this engine can or
    should paper over.
    """
    results: list[TriggerResult] = []

    for rule in rules:
        rule_name = getattr(rule, "trigger_name", None) or _rule_display_name(rule)
        try:
            # SAVEPOINT per rule: a rule that leaves the session dirty (a failed
            # INSERT, a constraint violation) is rolled back to this point and
            # the remaining rules still run against a clean session.
            async with db.begin_nested():
                res = await rule(db, subject)
            if res is None:
                res = TriggerResult(
                    rule_name=rule_name,
                    outcome=TriggerOutcome.SKIPPED,
                    reason="Rule returned no result (treated as not applicable).",
                )
            if not res.rule_name:
                res.rule_name = rule_name
            # A rule may hand back FAILED without raising (e.g. a downstream
            # HTTP call returned 500). Enforce the non-empty invariant there too
            # — the guarantee is about the *outcome*, not about how it arose.
            if res.outcome is TriggerOutcome.FAILED and not (res.failure_reason or "").strip():
                res.failure_reason = res.reason.strip() or "Rule reported FAILED without a reason."
            results.append(res)
        except Exception as exc:  # noqa: BLE001 — converting to a FAILED record IS the handling
            reason = _coerce_failure_reason(exc)
            logger.exception(
                "[trigger_engine] rule %s FAILED for %s %s", rule_name, source_kind, source_id
            )
            results.append(
                TriggerResult(
                    rule_name=rule_name,
                    outcome=TriggerOutcome.FAILED,
                    reason=f"Rule errored: {reason}",
                    failure_reason=reason,
                    stack=traceback.format_exc(limit=12),
                )
            )

    # ── persist ───────────────────────────────────────────────────────────────
    sink_failed = False
    sink_error: str | None = None
    if sink is None:
        logger.warning(
            "[trigger_engine] %s %s ran %d rule(s) with NO sink — results are not "
            "durably recorded. This is only correct for dry-run evaluation.",
            source_kind, source_id, len(results),
        )
    else:
        try:
            async with db.begin_nested():
                await sink(db, subject, results)
        except Exception as exc:  # noqa: BLE001
            sink_failed = True
            sink_error = _coerce_failure_reason(exc)
            logger.exception(
                "[trigger_engine] sink FAILED for %s %s — %d result(s) not persisted",
                source_kind, source_id, len(results),
            )

    run = TriggerRun(
        source_kind=source_kind,
        source_id=source_id,
        results=results,
        sink_failed=sink_failed,
        sink_error=sink_error,
    )

    # ── tell a human ─────────────────────────────────────────────────────────
    # Notification is intentionally AFTER persistence and outside the result
    # loop: the audit row is the compliance artefact and must exist even if the
    # message cannot be delivered. It is also deliberately not wrapped around
    # each rule — one digest per run, not one message per failed rule.
    if run.failed or sink_failed:
        await _notify_failures(
            db,
            run,
            site_id=site_id,
            roles=failure_audience_roles,
            user_ids=failure_audience_user_ids,
        )

    if emit_events:
        await _emit_trigger_events(db, run, site_id=site_id)

    return run


def _rule_display_name(rule: TriggerRule) -> str:
    raw = getattr(rule, "__name__", None) or rule.__class__.__name__
    return raw.replace("_rule_", "").replace("rule_", "").replace("_", " ").strip().title()


# ── failure notification ──────────────────────────────────────────────────────
async def _notify_failures(
    db: AsyncSession,
    run: TriggerRun,
    *,
    site_id: str | None,
    roles: Sequence[str],
    user_ids: Sequence[str],
) -> int:
    """Notify the responsible audience that a trigger failed.

    Best-effort by design — ``create_notification`` documents that it never
    raises, and even if it did, the outer try here guarantees a delivery
    problem cannot undo the rules that DID fire. But unlike the old runners,
    "best effort" here means "attempted and logged", not "not attempted"."""
    try:
        from app.models.user import User  # local import: avoids a model cycle at boot
        from app.services.erm_notifications import _users_with_role, create_notification

        recipients: dict[str, User] = {}
        for role in roles:
            for u in await _users_with_role(db, role, site_id):
                recipients[u.id] = u
        if user_ids:
            for uid in user_ids:
                if uid not in recipients:
                    u = await db.get(User, uid)
                    if u is not None:
                        recipients[uid] = u

        if not recipients:
            # Not being able to find anyone to tell is itself a finding. Log it
            # at WARNING so a mis-seeded role shows up in monitoring rather than
            # producing a run that looks quietly successful.
            logger.warning(
                "[trigger_engine] %s %s had %d failed rule(s) but no recipient "
                "resolved for roles=%s site=%s",
                run.source_kind, run.source_id, len(run.failed), list(roles), site_id,
            )
            return 0

        failed_names = ", ".join(r.rule_name for r in run.failed) or "audit persistence"
        detail_lines = [f"• {r.rule_name}: {r.failure_reason}" for r in run.failed]
        if run.sink_failed:
            detail_lines.append(f"• Audit trail could not be written: {run.sink_error}")
        n = len(run.failed) + (1 if run.sink_failed else 0)

        sent = 0
        for user_id in recipients:
            await create_notification(
                db,
                user_id=user_id,
                type="TRIGGER_RULE_FAILED",
                severity="CRITICAL",
                title=f"{n} automatic trigger{'s' if n != 1 else ''} failed on {run.source_kind}",
                body=(
                    f"Automatic follow-up did not complete for {run.source_kind} "
                    f"{run.source_id} ({failed_names}). The record was saved, but the "
                    f"downstream action was NOT created and needs manual action.\n\n"
                    + "\n".join(detail_lines)
                ),
                entity_type=run.source_kind,
                entity_id=run.source_id,
                link_url="/moc/trigger-log",
            )
            sent += 1
        return sent
    except Exception:  # noqa: BLE001
        logger.exception(
            "[trigger_engine] failure notification could not be sent for %s %s",
            run.source_kind, run.source_id,
        )
        return 0


async def _emit_trigger_events(db: AsyncSession, run: TriggerRun, *, site_id: str | None) -> None:
    """One DomainEvent per FIRED/FAILED result.

    This is what makes "has this trigger ever fired in production?" a query
    rather than an investigation. SKIPPED is not emitted — it is the common
    case and would drown the outbox."""
    try:
        from app.services.events import emit

        for r in run.results:
            if r.outcome is TriggerOutcome.SKIPPED:
                continue
            emit(
                db,
                event_type=(
                    TRIGGER_FIRED if r.outcome is TriggerOutcome.FIRED else TRIGGER_FAILED
                ),
                entity_type=run.source_kind,
                entity_id=run.source_id,
                site_id=site_id,
                payload={
                    "ruleName": r.rule_name,
                    "status": r.outcome.value,
                    "reason": r.reason,
                    "failureReason": r.failure_reason,
                    "spawnedRecordType": r.spawned_record_type,
                    "spawnedRecordId": r.spawned_record_id,
                },
            )
    except Exception:  # noqa: BLE001
        logger.exception("[trigger_engine] event emission failed for %s", run.source_id)


TRIGGER_FIRED = "trigger.fired"
TRIGGER_FAILED = "trigger.failed"


# ── ready-made sinks ──────────────────────────────────────────────────────────
def json_column_sink(column: str) -> TriggerSink:
    """Append audit entries to a JSON list column on the subject row.

    Reproduces the shape the incident / near-miss / observation runners already
    write (``closureTriggers``), so an existing runner can adopt the engine
    without changing what the UI reads. Re-assigns the list rather than
    mutating it in place — SQLAlchemy does not track in-place mutation of a
    plain JSON column, which is a separate silent-data-loss bug the legacy
    runners happened to avoid only because they did the same."""

    async def _sink(db: AsyncSession, subject: Any, results: list[TriggerResult]) -> None:
        existing = getattr(subject, column, None)
        if not isinstance(existing, list):
            existing = []
        setattr(subject, column, [*existing, *(r.to_audit_entry() for r in results)])
        await db.flush()

    return _sink


# ── legacy adapter ────────────────────────────────────────────────────────────
def adapt_dict_rule(fn: Callable[..., Awaitable[dict[str, Any] | None]]) -> TriggerRule:
    """Wrap a legacy ``async (db, subject) -> {ruleName, fired, reason}`` rule.

    Lets the three existing runners move onto the engine without rewriting ~30
    rule bodies. The mapping is deliberately conservative:

      * ``fired: True``                → FIRED
      * ``fired: False`` + ``error``   → FAILED (a rule that caught its own
                                         exception and reported it — still a
                                         failure, and previously indistinguishable
                                         from a clean no-op)
      * ``fired: False``               → SKIPPED
    """

    async def _wrapped(db: AsyncSession, subject: Any) -> TriggerResult:
        raw = await fn(db, subject)
        if raw is None:
            return TriggerResult(
                rule_name=_rule_display_name(fn),
                outcome=TriggerOutcome.SKIPPED,
                reason="Rule returned nothing.",
            )
        name = raw.get("ruleName") or _rule_display_name(fn)
        err = raw.get("error")
        if raw.get("fired"):
            outcome = TriggerOutcome.FIRED
        elif err:
            outcome = TriggerOutcome.FAILED
        else:
            outcome = TriggerOutcome.SKIPPED
        return TriggerResult(
            rule_name=name,
            rule_id=raw.get("ruleId"),
            outcome=outcome,
            reason=str(raw.get("reason") or ""),
            failure_reason=(str(err) if err else None),
            spawned_record_type=raw.get("spawnedRecordType"),
            spawned_record_id=raw.get("spawnedRecordId"),
            data=raw.get("data") or {},
        )

    _wrapped.trigger_name = _rule_display_name(fn)  # type: ignore[attr-defined]
    return _wrapped


__all__ = [
    "TriggerOutcome",
    "TriggerResult",
    "TriggerRun",
    "TriggerRule",
    "TriggerSink",
    "run_trigger_rules",
    "json_column_sink",
    "adapt_dict_rule",
    "TRIGGER_FIRED",
    "TRIGGER_FAILED",
]
