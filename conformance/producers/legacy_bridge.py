#!/usr/bin/env python3
"""Adapter that drives the pre-AHP cc-cursor-bridge hook (scripts/dispatch.py as of
commit 9f478f8, extracted with `git archive` into a cache dir) with scenario steps,
then converts its legacy task dir to AHP shape with spec/tools/legacy_to_ahp.convert_dir.
This keeps expected/legacy-9f478f8.json meaningful after Phase 2 changed scripts/.
If git or the commit is unavailable it falls back to this checkout (version "...-unpinned").

Each step becomes one `python3 scripts/dispatch.py` run with a CC hook JSON on
stdin, exactly as Claude Code would call it. The hook's stdout/exit code is
turned into the effect the host would apply (effects.jsonl). Runs in a temp
root; it never touches ~/.cc-bridge, ~/.claude or a running Claude Code.

Faults: "bad_input" sends non-JSON stdin; "crash" sets
CC_BRIDGE_WAIT_ALL_TIMEOUT=boom so handle_event raises inside the hook.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "spec" / "tools"))
from conformance.common import AHP_TO_CC, append_line, read_json  # noqa: E402
import legacy_to_ahp  # noqa: E402

CLASS_TO_MODE = {"gate": "wait", "notify": "push", "log": "log"}
RENAME_IN = {"stop_active": "stop_hook_active", "text": "delta", "server": "mcp_server_name"}


PIN = "9f478f8"


def pinned_root() -> Path:
    """The bridge tree at PIN, cached per user; REPO when it cannot be extracted."""
    cache = Path(tempfile.gettempdir()) / f"ahp-conformance-{os.getuid()}" / f"bridge-{PIN}"
    if (cache / "scripts" / "dispatch.py").is_file():
        return cache
    try:
        cache.parent.mkdir(parents=True, exist_ok=True)
        tmp = Path(tempfile.mkdtemp(dir=cache.parent, prefix=".extract-"))
        arch = subprocess.run(["git", "-C", str(REPO), "archive", "--format=tar", PIN,
                               "scripts", "config", "claude", ".claude-plugin"],
                              capture_output=True, timeout=30, check=True)
        subprocess.run(["tar", "-x", "-C", str(tmp)], input=arch.stdout, check=True, timeout=30)
        try:
            tmp.rename(cache)
        except OSError:  # another scenario process won the race
            shutil.rmtree(tmp, ignore_errors=True)
        return cache if (cache / "scripts" / "dispatch.py").is_file() else REPO
    except (OSError, subprocess.SubprocessError):
        return REPO


BRIDGE = pinned_root()


def version() -> str:
    return PIN if BRIDGE != REPO else f"{PIN}-unpinned"


def cc_input(step: dict, cwd: str) -> dict:
    hook = AHP_TO_CC.get(step["event"], step["event"])
    data = {"hook_event_name": hook, "session_id": "conformance", "cwd": cwd,
            # fixed long name so path truncation (spec A.6 #15) shows up on every machine
            "transcript_path": str(Path(cwd) / ("transcript-" + "0" * 64 + ".jsonl"))}
    for k, v in (step.get("payload") or {}).items():
        if k in ("tool_kind",):
            continue
        data[RENAME_IN.get(k, k)] = v
    ctx = step.get("context") or {}
    for k in ("session_id", "cwd", "prompt_id", "permission_mode", "agent_id"):
        if k in ctx:
            data[k] = ctx[k]
    return data


def effect_of(event: str, stdout: str, code: int) -> str:
    if code == 2:
        return "block" if event in ("turn.stop", "subagent.stop") else "deny"
    try:
        body = json.loads(stdout) if stdout.strip() else {}
    except json.JSONDecodeError:
        return "host-default" if event != "worktree.create" else "ack"
    hso = body.get("hookSpecificOutput") or {}
    if event == "tool.pre":
        return hso.get("permissionDecision") or "host-default"
    if event == "permission.request":
        return (hso.get("decision") or {}).get("behavior") or "host-default"
    if event == "input.request":
        return hso.get("action") or "host-default"
    if body.get("decision") == "block":
        return "block"
    return "ack" if event in ("turn.stop", "subagent.stop") else "none"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--describe", action="store_true")
    ap.add_argument("--scenario")
    ap.add_argument("--root")
    ap.add_argument("--task")
    a = ap.parse_args(argv)
    if a.describe:
        print(json.dumps({"name": "cc-cursor-bridge-legacy", "version": version(),
                          "watch": "legacy/tasks/{task}", "effects": True}))
        return 0
    sc = read_json(Path(a.scenario))
    root = Path(a.root)
    prod = sc.get("producer") or {}
    legacy_root = root / "legacy"
    work = root / "work"
    work.mkdir(exist_ok=True)
    env = dict(os.environ)
    env.update({"CC_BRIDGE_DIR": str(legacy_root), "CC_TASK_ID": a.task, "CLAUDE_PLUGIN_ROOT": str(BRIDGE),
                "CC_BRIDGE_WAIT_ALL_TIMEOUT": str(prod.get("timeout_ms", 120000) / 1000.0)})
    for k in list(env):  # never inherit a real bridge setup
        if k.startswith("CC_BRIDGE_") and k not in ("CC_BRIDGE_DIR", "CC_BRIDGE_WAIT_ALL_TIMEOUT"):
            env.pop(k)
    if "max_blocks" in prod:
        env["CC_BRIDGE_STOP_MAX_BLOCKS"] = str(prod["max_blocks"])
    cfg = {"pending": {}, "log": {}}
    limits = prod.get("limits") or {}
    if "content_chars" in limits:
        cfg["pending"]["content_chars"] = limits["content_chars"]
    if "command_chars" in limits:
        cfg["pending"]["command_chars"] = limits["command_chars"]
    if "max_field_chars" in limits:
        cfg["log"]["max_field_chars"] = limits["max_field_chars"]
    (root / "legacy-config").mkdir(exist_ok=True)
    (root / "legacy-config" / "bridge.json").write_text(json.dumps(cfg), encoding="utf-8")
    env["CC_BRIDGE_CONFIG"] = str(root / "legacy-config" / "bridge.json")
    rules = list(prod["rules"]) if prod.get("rules") is not None else None
    cj_path = root / "tasks" / a.task / "consumer.json"
    if cj_path.is_file():
        cj = read_json(cj_path)
        for ev, cls in (cj.get("event_classes") or {}).items():
            if ev in AHP_TO_CC:
                env[f"CC_BRIDGE_EVENT_MODE_{AHP_TO_CC[ev]}"] = CLASS_TO_MODE[cls]
                if ev == "message.delta" and cls == "notify":
                    env["CC_BRIDGE_ASSISTANT_TEXT"] = "1"
        if cj.get("extra_rules"):
            base = rules if rules is not None else read_json(BRIDGE / "config" / "rules.json")["rules"]
            rules = list(base) + list(cj["extra_rules"])
    if rules is not None:
        (root / "legacy-config" / "rules.json").write_text(json.dumps({"rules": rules}), encoding="utf-8")
        env["CC_BRIDGE_RULES"] = str(root / "legacy-config" / "rules.json")
    effects = root / "harness" / "effects.jsonl"
    for i, step in enumerate(sc.get("steps") or []):
        step_env = dict(env)
        stdin = json.dumps(cc_input(step, str(work)), ensure_ascii=False)
        if step.get("fault") == "bad_input":
            stdin = "{not json"
        if step.get("fault") == "crash":
            step_env["CC_BRIDGE_WAIT_ALL_TIMEOUT"] = "boom"
        proc = subprocess.run([sys.executable, str(BRIDGE / "scripts" / "dispatch.py")], input=stdin,
                              capture_output=True, text=True, env=step_env, cwd=str(work), timeout=300)
        append_line(effects, {"step": i, "event": step["event"],
                              "effect": effect_of(step["event"], proc.stdout, proc.returncode),
                              "exit": proc.returncode, "stderr": proc.stderr[-200:]})
    src = legacy_root / "tasks" / a.task
    if src.is_dir():
        legacy_to_ahp.convert_dir(src, root / "tasks" / a.task, a.task, version())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
