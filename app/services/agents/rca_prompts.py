"""RCA Assistant agent prompt loader.

The active prompt is stored as a Markdown file under prompts/ so it's
human-readable and reviewable in a PR diff. Both the Python runtime
(tests, validation) and the TypeScript seed script (which seeds the
AgentPrompt row in Prisma) load from this single file — no copy-paste,
no version drift.

Adding a v2 prompt:
  1. Drop prompts/rca_assistant_v2.md alongside v1.
  2. Bump RCA_ASSISTANT_ACTIVE_VERSION and update the loader.
  3. Re-run the seed; it inserts the new row with version=2 and
     repoints Agent.activePromptId.
"""

from __future__ import annotations

from pathlib import Path

# Pinned constants. Match these to seed-agents.ts.
RCA_ASSISTANT_AGENT_CODE = "RCA_ASSISTANT"
RCA_ASSISTANT_ACTIVE_VERSION = 1


def load_rca_assistant_prompt(version: int = RCA_ASSISTANT_ACTIVE_VERSION) -> str:
    """Read the RCA assistant system prompt from disk.

    Returns the file content verbatim. The seed script and the runtime
    must read the SAME file so the rolled-out prompt matches what
    tests validated.
    """
    path = (
        Path(__file__).parent
        / "prompts"
        / f"rca_assistant_v{version}.md"
    )
    if not path.is_file():
        raise FileNotFoundError(
            f"RCA assistant prompt v{version} not found at {path}"
        )
    return path.read_text(encoding="utf-8")
