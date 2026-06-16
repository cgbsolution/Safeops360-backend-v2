"""User-initiated agent platform.

See app/services/ai/agents/ for the OTHER agent pattern — workflow-rule-
triggered, fire-and-forget, JSON-only. Don't mix the two:

  app/services/ai/agents/      — Pattern A (workflow-rule agents)
  app/services/agents/          — Pattern B (user-initiated, with tools)

Both write to the same database but use different tables and runtime
plumbing. The decision tree in prisma/schema.prisma (under "User-
initiated AI Agent infrastructure") explains the split.
"""
