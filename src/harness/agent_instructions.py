"""Shared host-neutral wording for the agent's Harness Task boundary."""

TASK_CONTINUITY_INSTRUCTIONS = (
    "Same outcome: one Task across diagnosis, implementation, checks and follow-ups. "
    "New Task only for a distinct outcome. "
)

TASK_CREATION_INSTRUCTIONS = (
    "After status, start/resume a Task for substantial changes, multi-step work, needed continuity, "
    "or an explicit request. Quick questions, reads, and small local edits may proceed without a Task. "
)

TASK_REVIEW_INSTRUCTIONS = (
    "Checkpoint with task_id+expected_revision: working or waiting; "
    "ready => waiting(operator_review). Only the operator completes Tasks. "
)
