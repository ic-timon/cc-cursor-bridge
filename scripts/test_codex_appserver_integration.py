#!/usr/bin/env python3
"""Bounded real Codex 0.167 integration: request an outside-workspace canary, decline it.

Skipped unless CODEX_APPSERVER_INTEGRATION=1. Never accepts the write, a
network grant, or an execpolicy amendment. Uses the existing box login;
does not copy auth.json or write a shared model default.

This is not automatically a "generic one-shot approval" proof. The
summary classifies the observed gate:

- one_shot_decline: availableDecisions offered a one-shot accept/allow
  (or advertised no menu), no managed-network/grant scope, and the
  adapter applied an explicit controller deny via reply.
- policy_reject: the adapter fail-closed because the menu lacked one-shot
  accept, or because networkApprovalContext / grant / network-amendment
  scope was present. That is not a generic approval success.
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
sys.path.insert(0, str(SCRIPTS))
from codex_appserver.decisions import ALLOW_DECISIONS  # noqa: E402


def _one_shot_offered(menu) -> bool:
    if menu is None:
        return True
    if not isinstance(menu, list):
        return False
    return any(isinstance(value, str) and value in ALLOW_DECISIONS for value in menu)


def _outgoing_approval_decisions(transcript: Path) -> list:
    decisions = []
    if not transcript.is_file():
        return decisions
    for line in transcript.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if row.get("direction") != "send":
            continue
        message = row.get("message") or {}
        if not isinstance(message, dict) or "method" in message:
            continue
        result = message.get("result")
        if isinstance(result, dict) and "decision" in result:
            decisions.append(result["decision"])
    return decisions


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
        env.pop("RINO_GIT_TOKEN", None)
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
        acks_dir = ahp / "tasks" / task / "acks"
        env_body = None
        pre_existing_ack = None
        replied = False
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
            self.assertIsNotNone(
                env_body,
                "no approval request was emitted; this is not a successful approval-flow proof. "
                f"exit={proc.poll()} stderr={err_log.name}",
            )
            ack_path = acks_dir / f"{env_body['seq']}.json"
            if ack_path.is_file():
                pre_existing_ack = json.loads(ack_path.read_text(encoding="utf-8"))
            else:
                # Explicit deny only — never accept, never apply an amendment.
                reply = {"decision": "deny", "reason": "integration-canary-decline-only"}
                dest = ahp / "tasks" / task / "replies" / f"{env_body['seq']}.json"
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_text(json.dumps(reply, ensure_ascii=False) + "\n", encoding="utf-8")
                replied = True
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
        payload = (env_body or {}).get("payload") or {}
        context = payload.get("approval_context") or {}
        menu = context.get("availableDecisions")
        if "availableDecisions" not in context and "availableDecisions" in native:
            menu = native.get("availableDecisions")
        self.assertIn(native.get("method"), (
            "item/commandExecution/requestApproval",
            "item/fileChange/requestApproval",
            "execCommandApproval",
            "applyPatchApproval",
        ))
        ack = json.loads((acks_dir / f"{env_body['seq']}.json").read_text())
        self.assertEqual(ack.get("applied_decision"), "deny")
        status = json.loads((ahp / "tasks" / task / "status.json").read_text())
        self.assertEqual(status.get("state"), "ended")
        self.assertFalse(canary.exists())

        outgoing = _outgoing_approval_decisions(root / "rpc.jsonl")
        for decision in outgoing:
            if isinstance(decision, str):
                self.assertNotEqual(decision, "accept")
                self.assertNotIn("Session", decision)
                self.assertNotIn("Amendment", decision)
            else:
                self.assertNotIn("accept", json.dumps(decision))
                self.assertNotIn("Amendment", json.dumps(decision))

        scope_keys = (
            "networkApprovalContext", "grantRoot", "additionalPermissions", "permissions",
            "proposedNetworkPolicyAmendments", "proposed_network_policy_amendments",
        )
        scoped = any(context.get(key) not in (None, {}, []) for key in scope_keys)
        offered = _one_shot_offered(menu)
        if scoped or not offered:
            classification = "policy_reject"
            self.assertNotEqual(ack.get("via"), "reply")
            self.assertTrue(status.get("fail_closed"))
            self.assertFalse(status.get("success"))
        else:
            classification = "one_shot_decline"
            self.assertTrue(replied)
            self.assertEqual(ack.get("via"), "reply")
            self.assertEqual(ack.get("reason"), "integration-canary-decline-only")
            self.assertIsNone(pre_existing_ack)

        # Truthful terminal fields. A one-shot decline may still complete the turn.
        if status.get("success"):
            self.assertEqual(classification, "one_shot_decline")
            self.assertEqual(status.get("turn_status"), "completed")
            self.assertEqual(status.get("session_end_reason"), "completed")
            self.assertFalse(status.get("fail_closed"))
        else:
            self.assertIn(status.get("session_end_reason"),
                          ("failed", "cancelled", "interrupted", "completed"))
            if status.get("session_end_reason") == "completed":
                self.assertTrue(status.get("fail_closed"))

        summary = {
            "classification": classification,
            "canary": str(canary),
            "canary_exists": canary.exists(),
            "method": native.get("method"),
            "seq": env_body.get("seq"),
            "decisions": env_body.get("decisions"),
            "availableDecisions": menu,
            "approval_context": context,
            "one_shot_accept_offered": offered,
            "scope_extensions_present": scoped,
            "controller_reply_written": replied,
            "pre_existing_ack": pre_existing_ack,
            "outgoing_approval_decisions": outgoing,
            "ack": ack,
            "status": {k: status.get(k) for k in (
                "state", "success", "turn_status", "session_end_reason",
                "fail_closed", "error", "last_gate")},
            "exit": proc.returncode,
            "artifacts": str(root),
            "note": (
                "one_shot_decline is a real controller deny of a grantable command; "
                "policy_reject is not a generic approval success. "
                "The 2026-11-11 04:52 canary must not be reused as proof of current policy."
            ),
        }
        (root / "integration-summary.json").write_text(
            json.dumps(summary, indent=2, default=str) + "\n", encoding="utf-8")
        durable = Path("/tmp/codex-final-verify-dcc000c/integration-summary.json")
        durable.parent.mkdir(parents=True, exist_ok=True)
        durable.write_text(json.dumps(summary, indent=2, default=str) + "\n", encoding="utf-8")
        print(json.dumps({"classification": classification, "canary_exists": canary.exists(),
                          "via": ack.get("via"), "exit": proc.returncode,
                          "availableDecisions": menu,
                          "proposedExecpolicyAmendment": context.get("proposedExecpolicyAmendment"),
                          "networkApprovalContext": context.get("networkApprovalContext"),
                          "artifacts": str(root)}, default=str))


if __name__ == "__main__":
    unittest.main()
