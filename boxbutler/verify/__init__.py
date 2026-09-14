"""Verification layer (Task 15; spec §2 step 4, §3.4).

`verify_rendition` is the last gate before the orchestrator (Task 21)
clears a real tonie and uploads its replacement. Spec §2: "stage, verify,
then swap" — a tonie is never cleared until its replacement is downloaded,
trimmed *and verified*. Everything before this point can fail safely (the
tonie still holds last night's story); this module cannot fail safely, so
it is biased to refuse: **refuse unless demonstrably correct**, never
"accept unless clearly broken". See `boxbutler/verify/verify.py` for the
full rationale and the exact list of refusal conditions.
"""
