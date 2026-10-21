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


def decide(payload: dict) -> dict:
    event = payload.get("event")
    tool = payload.get("tool_name")
    if event == "PermissionRequest" and tool == "Write":
        return {"decision": "allow", "message": "stand-in allow write"}
    if event == "PreToolUse" and tool == "Write":
        return {"decision": "allow", "reason": "stand-in allow write"}
    if event in ("PreToolUse", "PermissionRequest"):
        return {"decision": "deny", "reason": "stand-in default deny"}
    return {"ack": True}


def main() -> int:
    task = sys.argv[1]
    seconds = float(sys.argv[2]) if len(sys.argv) > 2 else 60
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
            reply = decide(payload)
            result = bridge.cli(["reply", task, seq, json.dumps(reply)])
            print(f"replied {seq} {payload.get('event')} {result.stdout.strip()}", flush=True)
            seen.add(path)
        time.sleep(0.2)
    print(f"answered {len(seen)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
