"""Run one scenario against one producer adapter and collect check results.

Adapter contract: a command (argv list) that supports
  <cmd> --describe            -> prints {"name","version","watch":"tasks/{task}"|..., "effects": bool}
  <cmd> --scenario F --root R --task T
and leaves AHP artifacts in R/tasks/T when it exits. It SHOULD write
R/harness/effects.jsonl lines {"step", "event", "effect"} with what the host
actually did (allow/deny/ask/accept/decline/block/ack/host-default/none).
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Dict, List

from .checks import TaskView, expectation_checks, run_generic
from .common import REPO, Validators, read_jsonl, write_atomic
from .consumer import ScriptedConsumer

PRODUCERS = {
    "replay": [sys.executable, str(REPO / "conformance" / "producers" / "replay.py")],
    "legacy-bridge": [sys.executable, str(REPO / "conformance" / "producers" / "legacy_bridge.py")],
    "cc-adapter": [sys.executable, str(REPO / "conformance" / "producers" / "cc_adapter.py")],
}


def describe(cmd: List[str]) -> dict:
    out = subprocess.run(cmd + ["--describe"], capture_output=True, text=True, timeout=30, check=True)
    return json.loads(out.stdout.strip().splitlines()[-1])


def run_scenario(cmd: List[str], scenario_path: Path, val: Validators, keep: bool = False) -> dict:
    scenario = json.loads(scenario_path.read_text(encoding="utf-8"))
    info = describe(cmd)
    task = scenario["id"]
    root = Path(tempfile.mkdtemp(prefix="ahp-conf-"))
    (root / "home").mkdir()
    (root / "work").mkdir()
    task_dir = root / "tasks" / task
    task_dir.mkdir(parents=True)
    os.chmod(task_dir, 0o700)
    if scenario.get("consumer_json"):
        write_atomic(task_dir / "consumer.json", scenario["consumer_json"])
    watch = root / info.get("watch", "tasks/{task}").format(task=task)
    watch.mkdir(parents=True, exist_ok=True)
    timeout_ms = int((scenario.get("producer") or {}).get("timeout_ms", 120000))
    consumer = ScriptedConsumer(watch, scenario.get("consumer") or [], timeout_ms)
    consumer.start()
    env = {"PATH": os.environ.get("PATH", ""), "HOME": str(root / "home"), "LANG": "C.UTF-8"}
    if os.environ.get("AHP_REPLAY_MUTATE"):
        env["AHP_REPLAY_MUTATE"] = os.environ["AHP_REPLAY_MUTATE"]
    env.update((scenario.get("producer") or {}).get("env") or {})
    started = time.monotonic()
    proc = subprocess.run(cmd + ["--scenario", str(scenario_path), "--root", str(root), "--task", task],
                          capture_output=True, text=True, env=env, cwd=str(root / "work"),
                          timeout=max(60, len(scenario.get("steps") or []) * timeout_ms / 1000 * 2 + 30))
    consumer.stop(grace_s=timeout_ms / 1000 + 2)
    if scenario.get("after_ms"):
        time.sleep(scenario["after_ms"] / 1000)
    elapsed = time.monotonic() - started
    effects: Dict[int, dict] = {e["step"]: e for e in read_jsonl(root / "harness" / "effects.jsonl")
                                if isinstance(e, dict) and "step" in e}
    view = TaskView(task_dir)
    ctx = {"secrets": scenario.get("secrets") or [], "required_rules": scenario.get("required_rules") or []}
    results = run_generic(view, val, ctx, skip=set(scenario.get("skip_checks") or []))
    results += expectation_checks(view, scenario, effects)
    if proc.returncode != 0:
        results.append(("P00", "producer exited 0", ("fail", f"exit {proc.returncode}: {proc.stderr.strip()[-300:]}")))
    if not keep:
        shutil.rmtree(root, ignore_errors=True)
    return {"scenario": task, "title": scenario.get("title", ""), "producer": info.get("name"),
            "elapsed_s": round(elapsed, 2), "root": str(root) if keep else None,
            "results": [{"id": cid, "title": title, "status": st, "detail": detail}
                        for cid, title, (st, detail) in results]}


def run_static(task_dir: Path, val: Validators, label: str) -> dict:
    view = TaskView(task_dir)
    results = run_generic(view, val, {})
    return {"scenario": label, "title": f"static fixture {task_dir.name}", "producer": "fixture",
            "results": [{"id": cid, "title": title, "status": st, "detail": detail}
                        for cid, title, (st, detail) in results]}
