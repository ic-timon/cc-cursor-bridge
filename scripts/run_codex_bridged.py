#!/usr/bin/env python3
"""Run a supervised Codex app-server turn and publish its AHP task tree."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from codex_appserver.session import Session


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--task", required=True)
    parser.add_argument("--cwd", required=True, type=Path)
    parser.add_argument("--model", required=True)
    parser.add_argument("--effort", required=True)
    parser.add_argument("--prompt-file", type=Path, help="read prompt from this file (otherwise stdin)")
    parser.add_argument("--timeout-seconds", type=float, default=120, help="approval timeout (default: 120)")
    parser.add_argument("--turn-timeout-seconds", type=float, default=3600, help="whole turn timeout (default: 3600)")
    parser.add_argument("--codex-bin", default="codex")
    parser.add_argument("--ahp-dir", type=Path)
    parser.add_argument("--bridge-dir", type=Path)
    parser.add_argument("--jsonl", type=Path, help="redacted RPC transcript")
    args = parser.parse_args()
    environ = dict(os.environ)
    if args.bridge_dir is not None:
        environ["CC_BRIDGE_DIR"] = str(args.bridge_dir)
    if args.ahp_dir is not None:
        environ["CC_BRIDGE_AHP_DIR"] = str(args.ahp_dir)
    try:
        prompt = args.prompt_file.read_text(encoding="utf-8") if args.prompt_file else sys.stdin.read()
        session = Session(task_id=args.task, cwd=args.cwd, model=args.model, effort=args.effort,
                          timeout_seconds=args.timeout_seconds, turn_timeout_seconds=args.turn_timeout_seconds,
                          codex_bin=args.codex_bin, environ=environ, jsonl=args.jsonl)
        print(f"Task dir: {session.store.dir}", flush=True)
        print(f"Status path: {session.store.dir / 'status.json'}", flush=True)
        status = session.run(prompt)
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"codex adapter: {exc}", file=sys.stderr)
        return 1
    return 0 if status["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
