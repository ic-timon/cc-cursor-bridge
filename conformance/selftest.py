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
    "bad_spec_version": ["gate-replies"],
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
    # The example-drift check must notice a changed example (only when the adapter is in the tree).
    from .cc_example import available, diff, generate
    if available():
        gen = generate()
        drifted = dict(gen, events=dict(gen["events"]))
        drifted["events"]["prompt.submit"] = dict(drifted["events"]["prompt.submit"],
                                                  allowed_classes=["notify", "log", "gate"])
        ok = bool(diff(gen, drifted)) and not diff(gen, dict(gen, created_at="2000-01-01T00:00:00Z"))
        bad += not ok
        print(f"{'caught' if ok else 'MISSED'}  example_drift -> cc_example.diff")
    else:
        print("skip    example_drift (adapter not in this tree)")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
