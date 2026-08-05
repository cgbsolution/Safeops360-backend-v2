"""Impact-rule registry package — one file per rule (build spec Part 2.1).

Each module exposes ``RULE: ImpactRule`` whose ``resolve(event, ctx)`` is a
pure async function over the RuleContext protocol: no ORM imports, no session
handling — which is exactly what makes tests/test_alert_rules.py possible
with a hand-rolled fake context. Adding a rule = new file here + one line in
app/services/alerts/__init__.py rule_registry().
"""
