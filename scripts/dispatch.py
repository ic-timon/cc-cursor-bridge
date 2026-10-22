#!/usr/bin/env python3
"""Hook entrypoint. Claude Code runs this with the event JSON on stdin.

hooks.json passes the hook name as argv[1]. A gate hook (PreToolUse, PermissionRequest,
Elicitation, Stop) fails closed (AHP v1 spec 9.1, Appendix A.3): any bridge error gives the
explicit default decision, never Claude Code's own permission flow.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

REASON = "cc-cursor-bridge internal error (fail-closed)"
EMERGENCY = {
    "PreToolUse": {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                          "permissionDecisionReason": REASON}},
    "PermissionRequest": {"hookSpecificOutput": {"hookEventName": "PermissionRequest",
                                                 "decision": {"behavior": "deny", "message": REASON}}},
    "Elicitation": {"hookSpecificOutput": {"hookEventName": "Elicitation", "action": "decline"}},
}


def emergency(hook: str, message: str) -> int:
    """Used only when bridge.py itself cannot be imported or its error path failed."""
    sys.stderr.write(f"cc-cursor-bridge: {message}\n")
    if hook == "Stop":
        return 0  # end the turn; exit 2 would make Claude Code keep going
    body = EMERGENCY.get(hook)
    if body is None:
        return 2 if not hook else 0
    try:
        sys.stdout.write(json.dumps(body) + "\n")
        sys.stdout.flush()
        return 0
    except Exception:  # noqa: BLE001
        return 2  # cannot write JSON: exit 2 still blocks the tool


def main() -> int:
    hint = sys.argv[1] if len(sys.argv) > 1 and re.fullmatch(r"[A-Za-z]+", sys.argv[1]) else ""
    try:
        import bridge  # noqa: E402
    except Exception as exc:  # noqa: BLE001
        raw = ""
        try:
            raw = sys.stdin.read()
        except Exception:  # noqa: BLE001
            pass
        match = re.search(r'"hook_event_name"\s*:\s*"([A-Za-z]+)"', raw)
        return emergency(hint or (match.group(1) if match else ""), f"bridge import failed: {exc}")
    try:
        result = bridge.main()
    except Exception as exc:  # noqa: BLE001 - bridge.main already fails closed; this is the last resort
        event = bridge.CURRENT_EVENT if isinstance(bridge.CURRENT_EVENT, dict) else {}
        hook = str(event.get("hook_event_name") or hint)
        try:
            result = bridge.fail_closed(dict(bridge.os.environ), event, f"{type(exc).__name__}: {exc}", hook)
        except Exception as inner:  # noqa: BLE001
            return emergency(hook, f"{exc}; error path failed: {inner}")
    if result.stderr:
        sys.stderr.write(result.stderr)
    if result.stdout:
        sys.stdout.write(result.stdout)
    return result.code


if __name__ == "__main__":
    raise SystemExit(main())
