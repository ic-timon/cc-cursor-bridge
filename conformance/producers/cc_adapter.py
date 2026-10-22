#!/usr/bin/env python3
"""Adapter that drives the AHP v1 cc-cursor-bridge (this checkout) with scenario steps.

Each step is one run of the exact hooks.json command for that hook
(`python3 ${CLAUDE_PLUGIN_ROOT}/scripts/dispatch.py <Hook>`) with a CC hook JSON
on stdin, as Claude Code would call it. The bridge writes the AHP tree itself
(CC_BRIDGE_AHP_DIR=<root>); the legacy tree goes to <root>/legacy and is not checked.
stdout/exit code become effects.jsonl. Runs in a temp root; never touches
~/.cc-bridge, ~/.claude or a running Claude Code.

Faults: "bad_input" sends non-JSON stdin; "crash" sets CC_BRIDGE_FAULT=crash
(a test-only switch that raises inside handle_event).
"""
from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
from conformance.common import AHP_TO_CC, append_line, read_json  # noqa: E402
from conformance.producers.legacy_bridge import effect_of  # noqa: E402

RENAME_IN = {"stop_active": "stop_hook_active", "text": "delta", "server": "mcp_server_name"}
AHP_ONLY = {"tool_kind", "command", "items"}
CC_KIND_TOOLS = {"Bash", "PowerShell", "Write", "Edit", "MultiEdit", "NotebookEdit", "Read", "Grep", "Glob",
                 "LS", "WebFetch", "WebSearch", "AskUserQuestion"}


def version() -> str:
    try:
        return json.loads((REPO / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))["version"]
    except (OSError, ValueError, KeyError):
        return "unknown"


def hook_commands() -> dict:
    data = read_json(REPO / "claude" / "hooks" / "hooks.json")
    out = {}
    for hook, groups in data["hooks"].items():
        for group in groups:
            for h in group.get("hooks") or []:
                out[hook] = h["command"]
    return out


def argv_for(command: str) -> list:
    parts = shlex.split(command.replace("${CLAUDE_PLUGIN_ROOT}", str(REPO)))
    if parts and parts[0] in ("python3", "python"):
        parts[0] = sys.executable
    return parts


def cc_input(step: dict, cwd: str) -> dict:
    hook = AHP_TO_CC.get(step["event"], step["event"])
    data = {"hook_event_name": hook, "session_id": "conformance", "cwd": cwd,
            "transcript_path": str(Path(cwd) / ("transcript-" + "0" * 64 + ".jsonl"))}
    payload = step.get("payload") or {}
    if step["event"] == "plan.update":
        # Appendix A.1: plan.update is synthesized from PostToolUse(TodoWrite).
        data.update(hook_event_name="PostToolUse", tool_name="TodoWrite", tool_response="",
                    tool_input={"todos": [{"content": i.get("content", ""), "status": i.get("status", "pending"),
                                           **({"id": i["id"]} if "id" in i else {})}
                                          for i in payload.get("items") or []]})
        return data
    for k, v in payload.items():
        if k in AHP_ONLY:
            continue
        data[RENAME_IN.get(k, k)] = v
    ctx = step.get("context") or {}
    for k in ("session_id", "cwd", "prompt_id", "permission_mode", "agent_id", "turn_id"):
        if k in ctx:
            data[k] = ctx[k]
    return data


def tool_kinds(sc: dict) -> dict:
    """Non-CC tool names in the scenario become config ahp.tool_kinds entries (Appendix A.1)."""
    out = {}
    for step in sc.get("steps") or []:
        p = step.get("payload") or {}
        name, kind = p.get("tool_name"), p.get("tool_kind")
        if isinstance(name, str) and kind and name not in CC_KIND_TOOLS:
            spec = {"kind": kind}
            if isinstance((p.get("tool_input") or {}).get("argv"), list):
                spec["argv"] = "argv"
            out[name] = spec
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--describe", action="store_true")
    ap.add_argument("--scenario")
    ap.add_argument("--root")
    ap.add_argument("--task")
    a = ap.parse_args(argv)
    if a.describe:
        print(json.dumps({"name": "cc-cursor-bridge", "version": version(), "watch": "tasks/{task}",
                          "effects": True}))
        return 0
    sc = read_json(Path(a.scenario))
    root = Path(a.root)
    prod = sc.get("producer") or {}
    work = root / "work"
    work.mkdir(exist_ok=True)
    env = {k: v for k, v in os.environ.items() if not k.startswith(("CC_BRIDGE_", "CLAUDE_"))}
    env.update({"CC_BRIDGE_DIR": str(root / "legacy"), "CC_BRIDGE_AHP_DIR": str(root), "CC_TASK_ID": a.task,
                "CLAUDE_PLUGIN_ROOT": str(REPO)})
    if "max_blocks" in prod:
        env["CC_BRIDGE_STOP_MAX_BLOCKS"] = str(prod["max_blocks"])
    limits = prod.get("limits") or {}
    cfg = {"pending": {k: limits[k] for k in ("content_chars", "command_chars") if k in limits},
           "log": {k: limits[k] for k in ("max_field_chars",) if k in limits},
           "wait_all": {"timeout_seconds": prod.get("timeout_ms", 120000) / 1000.0, "poll_interval_seconds": 0.05},
           "ahp": {"tool_kinds": tool_kinds(sc)}}
    conf = root / "adapter-config"
    conf.mkdir(exist_ok=True)
    (conf / "bridge.json").write_text(json.dumps(cfg), encoding="utf-8")
    env["CC_BRIDGE_CONFIG"] = str(conf / "bridge.json")
    if prod.get("rules") is not None:
        (conf / "rules.json").write_text(json.dumps({"rules": prod["rules"]}), encoding="utf-8")
        env["CC_BRIDGE_RULES"] = str(conf / "rules.json")
    env.update(prod.get("env") or {})
    commands = hook_commands()
    effects = root / "harness" / "effects.jsonl"
    for i, step in enumerate(sc.get("steps") or []):
        data = cc_input(step, str(work))
        hook = data["hook_event_name"]
        step_env = dict(env)
        stdin = json.dumps(data, ensure_ascii=False)
        if step.get("fault") == "bad_input":
            stdin = "{not json"
        if step.get("fault") == "crash":
            step_env["CC_BRIDGE_FAULT"] = "crash"
        cmd = argv_for(commands.get(hook) or f'python3 "{REPO}/scripts/dispatch.py" {hook}')
        proc = subprocess.run(cmd, input=stdin, capture_output=True, text=True, env=step_env, cwd=str(work),
                              timeout=300)
        append_line(effects, {"step": i, "event": step["event"],
                              "effect": effect_of(step["event"], proc.stdout, proc.returncode),
                              "exit": proc.returncode, "stderr": proc.stderr[-200:]})
        if step.get("pause_ms"):
            time.sleep(step["pause_ms"] / 1000.0)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
