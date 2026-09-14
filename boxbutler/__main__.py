"""`python -m boxbutler` — serve the web UI + scheduler (Task 29; spec §11 P5).

`boxbutler` (the CLI, `boxbutler/cli/main.py`) is the operator's one-shot
tool: `run`, `status`, `prefetch`, `cache`, `library`, `snapshot`. This is
the long-running process a container's entrypoint starts instead: the web
UI on `:{listen_port}` plus the daily scheduler thread, both driven by the
same real store/sink/orchestrator `boxbutler.main.build()` constructs.
"""
from __future__ import annotations

from boxbutler.main import main

if __name__ == "__main__":
    main()
