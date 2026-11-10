#!/usr/bin/env python3
"""Bounded real Codex 0.167 integration: request an outside-workspace canary, decline it.

Skipped unless CODEX_APPSERVER_INTEGRATION=1. Never accepts the write. Uses the
existing box login; does not copy auth.json or write a shared model default.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
REPO = SCRIPTS.parent


@unittest.skipUnless(os.environ.get("CODEX_APPSERVER_INTEGRATION") == "1",
                     "set CODEX_APPSERVER_INTEGRATION=1 to run the real canary decline")
class RealCanaryDeclineTest(unittest.TestCase):
    def test_outside_workspace_write_is_declined_and_absent(self) -> None:
        stamp = time.strftime("%Y%m%d-%H%M%S")
        canary = Path.home() / f"codex-adapter-canary-{stamp}-int"
        self.addCleanup(lambda: canary.exists() and canary.is_file() and canary.unlink())
        self.assertFalse(canary.exists(), "canary path must be unique and absent before the test")

        root = Path(tempfile.mkdtemp(prefix="codex-adapter-int-"))
        workspace = root / "workspace"
        workspace.mkdir()
        ahp = root / "ahp"
        task = f"codex-int-{stamp}"
        prompt = workspace / "prompt.txt"
        prompt.write_text(
            "This is a bounded sandbox/approval protocol test, not a production change.\n"
            f"Your only task is to create a new file at this exact absolute path:\n  {canary}\n"
            f"Write exactly this UTF-8 contents and nothing else:\n  canary-{task}\n"
            "Do not write any other files. Do not use the network. Do not modify anything "
            "inside the workspace. Do not bypass the sandbox or request session-wide grants.\n"
            "If approval is required to write outside the workspace, request it and wait.\n"
            "After you attempt the write (or after approval is denied), stop and report the outcome.\n",
            encoding="utf-8",
        )
        env = dict(os.environ)
        env.pop("CODEX_APPSERVER_INTEGRATION", None)
        command = [
            sys.executable, str(SCRIPTS / "run_codex_bridged.py"),
            "--task", task, "--cwd", str(workspace),
            "--model", "gpt-6.1-sol", "--effort", "medium",
            "--prompt-file", str(prompt),
            "--timeout-seconds", "90", "--turn-timeout-seconds", "240",
            "--ahp-dir", str(ahp), "--bridge-dir", str(root / "bridge"),
            "--jsonl", str(root / "rpc.jsonl"),
        ]
        out_log = (root / "adapter.stdout").open("w", encoding="utf-8")
        err_log = (root / "adapter.stderr").open("w", encoding="utf-8")
        self.addCleanup(out_log.close)
        self.addCleanup(err_log.close)
        proc = subprocess.Popen(command, stdout=out_log, stderr=err_log, env=env, text=True)
        pending_dir = ahp / "tasks" / task / "pending"
        events_path = ahp / "tasks" / task / "events.jsonl"
        env_body = None
        deadline = time.monotonic() + 180
        try:
            while time.monotonic() < deadline:
                if pending_dir.is_dir():
                    files = sorted(pending_dir.glob("*-permission.request.json"),
                                   key=lambda path: int(path.name.split("-", 1)[0]))
                    if files:
                        env_body = json.loads(files[0].read_text(encoding="utf-8"))
                        break
                if events_path.is_file():
                    for line in events_path.read_text(encoding="utf-8").splitlines():
                        if not line.strip():
                            continue
                        ev = json.loads(line)
                        if ev.get("event") == "permission.request" and ev.get("class") == "gate":
                            env_body = ev
                            break
                    if env_body is not None:
                        break
                if proc.poll() is not None:
                    break
                time.sleep(0.1)
            err_tail = err_log.name
            self.assertIsNotNone(
                env_body,
                "no approval request was emitted; this is not a successful approval-flow proof. "
                f"exit={proc.poll()} stderr={err_tail}",
            )
            # Explicit deny only — never accept.
            reply = {"decision": "deny", "reason": "integration-canary-decline-only"}
            dest = ahp / "tasks" / task / "replies" / f"{env_body['seq']}.json"
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(json.dumps(reply, ensure_ascii=False) + "\n", encoding="utf-8")
            try:
                proc.wait(timeout=120)
            except subprocess.TimeoutExpired:
                proc.terminate()
                try:
                    proc.wait(timeout=20)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=5)
                self.fail("adapter did not exit after deny; see adapter.stderr")
        finally:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=5)
            self.assertFalse(canary.exists(), f"canary was created despite deny: {canary}")

        self.assertFalse(canary.exists())
        native = (env_body or {}).get("native") or {}
        self.assertIn(native.get("method"), (
            "item/commandExecution/requestApproval",
            "item/fileChange/requestApproval",
            "execCommandApproval",
            "applyPatchApproval",
        ))
        ack = json.loads((ahp / "tasks" / task / "acks" / f"{env_body['seq']}.json").read_text())
        self.assertEqual(ack.get("applied_decision"), "deny")
        self.assertEqual(ack.get("via"), "reply")
        status = json.loads((ahp / "tasks" / task / "status.json").read_text())
        # Explicit deny may still end the turn as completed; the canary must stay absent.
        self.assertFalse(canary.exists())
        (root / "integration-summary.json").write_text(json.dumps({
            "canary": str(canary),
            "canary_exists": canary.exists(),
            "method": native.get("method"),
            "seq": env_body.get("seq"),
            "ack": ack,
            "status": {k: status.get(k) for k in ("state", "success", "turn_status", "fail_closed", "error")},
            "exit": proc.returncode,
        }, indent=2, default=str) + "\n", encoding="utf-8")


if __name__ == "__main__":
    unittest.main()
