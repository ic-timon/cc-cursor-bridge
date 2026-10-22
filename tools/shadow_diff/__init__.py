"""AHP v1 shadow diff: compare a reference producer's task tree with a candidate's.

    python3 -m tools.shadow_diff diff REF_TASK_DIR CAND_TASK_DIR [--config FILE] [--json OUT]
    python3 -m tools.shadow_diff run --reference cc-adapter --candidate CMD --scenario ID

See docs/shadow-diff.md. Stdlib only; reuses conformance.checks.TaskView for loading.
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

TOOL = "ahp-shadow-diff"
VERSION = "0.1.0"
