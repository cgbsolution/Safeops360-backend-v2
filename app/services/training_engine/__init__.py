"""Training & Competency Engine (spec §B/§C/§D).

Deterministic, airgap-safe (no LLM/external calls) trigger + assignment engine
layered additively over the existing Skill-Matrix competency models. Public
surface:

  rules.*        — the four pure rule types (unit-tested independently of DB/UI)
  classify.*     — classification extraction + SIF inference + trigger emission
  service.*      — orchestration (drain outbox, scans, completion, manual assign)
  correlation.*  — the before/after re-incident correlation data asset
  config.*       — tenant/plant-configurable thresholds & windows
"""

from app.services.training_engine.classify import emit_training_trigger
from app.services.training_engine.service import (
    assign_manual,
    complete_assignment,
    drain_trigger_events,
    run_overdue_scan,
    run_recert_scan,
)

__all__ = [
    "emit_training_trigger",
    "drain_trigger_events",
    "run_recert_scan",
    "run_overdue_scan",
    "complete_assignment",
    "assign_manual",
]
