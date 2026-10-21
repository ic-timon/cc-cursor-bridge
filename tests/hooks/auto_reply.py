#!/usr/bin/env python3
"""Stand-in Cursor agent: ack every pending, allow Write."""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
import bridge  # noqa: E402


def parse_args(argv: list) -> tuple:
    task = argv[1]
    seconds = 60.0
    stop_reason = ""
    index = 2
    while index < len(argv):
        if argv[index] == "--stop-block-once":
            stop_reason = argv[index + 1]
            index += 2
            continue
        seconds = float(argv[index])
        index += 1
    return task, seconds, stop_reason


def decide(payload: dict, stop_reason: str, state: dict, allow: set) -> dict:
    event = payload.get("event")
    tool = payload.get("tool_name")
    if event == "Stop":
        if stop_reason and state["stops"] < 1:
            state["stops"] += 1
            return {"decision": "block", "reason": stop_reason}
        return {"ack": True}
    if event == "Elicitation":
        return {"decision": "accept"}
    if event == "PermissionRequest" and tool in allow:
        return {"decision": "allow", "message": "stand-in allow"}
    if event == "PreToolUse" and tool in allow:
        return {"decision": "allow", "reason": "stand-in allow"}
    if event in ("PreToolUse", "PermissionRequest"):
        return {"decision": "deny", "reason": "stand-in default deny"}
    return {"ack": True}


def main() -> int:
    task, seconds, stop_reason = parse_args(sys.argv)
    state = {"stops": 0}
    allow = {"Write"}
    if stop_reason:
        allow = {"Write", "Edit", "Read", "Bash", "Glob", "Grep", "MultiEdit"}
    deadline = time.monotonic() + seconds
    seen = set()
    while time.monotonic() < deadline:
        listed = bridge.cli(["pending", task])
        for line in listed.stdout.splitlines():
            path = Path(line.strip())
            if not path.is_file() or path in seen:
                continue
            payload = json.loads(path.read_text(encoding="utf-8"))
            seq = str(payload.get("seq") or path.name.split("-", 1)[0])
            reply = decide(payload, stop_reason, state, allow)
            result = bridge.cli(["reply", task, seq, json.dumps(reply)])
            print(f"replied {seq} {payload.get('event')} {result.stdout.strip()}", flush=True)
            seen.add(path)
        time.sleep(0.2)
    print(f"answered {len(seen)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
