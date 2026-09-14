"""The run orchestrator (spec §2, §3): plan -> stage -> verify -> swap -> settle -> commit.

Phase 4 builds this incrementally. Task 19 adds only the settle-polling helper
(`settle.poll_until_settled`) and the `FakeSink` transcoding simulation it is
tested against. Nothing here may import `tonie_api` or touch the network —
that waits for the orchestrator itself, reviewed, in a later task.
"""
