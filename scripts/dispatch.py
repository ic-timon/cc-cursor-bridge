#!/usr/bin/env python3
"""Hook entrypoint. Claude Code runs this with the event JSON on stdin."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import bridge  # noqa: E402


def main() -> int:
    try:
        result = bridge.main()
    except Exception as exc:  # noqa: BLE001 — a hook bug must not block Claude
        sys.stderr.write(f"cc-cursor-bridge: {exc}\n")
        return 0
    if result.stderr:
        sys.stderr.write(result.stderr)
    if result.stdout:
        sys.stdout.write(result.stdout)
    return result.code


if __name__ == "__main__":
    raise SystemExit(main())
