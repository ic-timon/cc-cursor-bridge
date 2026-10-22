"""Mutation self-test: each deliberately broken reference producer must be caught.

python3 -m conformance.selftest
"""
from __future__ import annotations

import os
import sys

from .common import Validators
from .harness import PRODUCERS, run_scenario
from .__main__ import PKG

MUTATIONS = {
    "fail_open": ["gate-timeouts", "invalid-replies", "late-reply"],
    "no_ack": ["gate-replies"],
    "no_redact": ["redaction-env"],
    "stray_tmp": ["gate-replies"],
    "bad_sha": ["truncation"],
    "no_rules": ["rules-rm-rf", "required-rule-tool-kind"],
    "crash_open": ["producer-error"],
    "truncate_context": ["context-limit"],
    "no_stray_ack": ["context-notify-reply"],
}


def main() -> int:
    val = Validators()
    bad = 0
    for mutation, scenarios in MUTATIONS.items():
        os.environ["AHP_REPLAY_MUTATE"] = mutation
        caught = []
        for sid in scenarios:
            run = run_scenario(PRODUCERS["replay"], PKG / "scenarios" / f"{sid}.json", val)
            caught += [f"{sid}:{r['id']}" for r in run["results"] if r["status"] == "fail"]
        ok = bool(caught)
        bad += not ok
        print(f"{'caught' if ok else 'MISSED'}  {mutation:11} -> {', '.join(sorted(set(caught))[:6])}")
    os.environ.pop("AHP_REPLAY_MUTATE", None)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
