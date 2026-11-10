#!/usr/bin/env python3
"""Behavioral tests of the adapter against a local, non-executing protocol fixture."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from typing import Optional

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))

from ahp_cc import write_atomic
from codex_appserver import __version__
from codex_appserver.session import Session

COMMAND = "item/commandExecution/requestApproval"
FILE_CHANGE = "item/fileChange/requestApproval"
PERMISSIONS = "item/permissions/requestApproval"
SECRET = "sk-fixture_secret_0123456789abcdef"


class Run:
    def __init__(self, root: Path, scenario: dict, timeout: float = 2,
                 turn_timeout: float = 5, extra_args: Optional[list] = None,
                 stdin_prompt: bool = False) -> None:
        self.root = root
        self.cwd = root / "workspace"
        self.cwd.mkdir(exist_ok=True)
        self.task_dir = root / "ahp" / "tasks" / "fixture"
        invocation = str(time.monotonic_ns())
        self.record = root / f"fake-{invocation}.jsonl"
        self.transcript = root / f"rpc-{invocation}.jsonl"
        self.scenario_file = root / f"scenario-{invocation}.json"
        write_atomic(self.scenario_file, scenario)
        self.env = dict(os.environ, CC_BRIDGE_AHP_DIR=str(root / "ahp"),
                        CC_BRIDGE_DIR=str(root / "bridge"), FAKE_CODEX_RECORD=str(self.record),
                        FAKE_CODEX_SCENARIO=str(self.scenario_file), OPENAI_API_KEY=SECRET)
        command = [sys.executable, str(SCRIPTS / "run_codex_bridged.py"),
                   "--task", "fixture", "--cwd", str(self.cwd), "--model", "fixture-model",
                   "--effort", "medium", "--codex-bin", str(SCRIPTS / "codex_appserver" / "fake_server.py"),
                   "--timeout-seconds", str(timeout), "--turn-timeout-seconds", str(turn_timeout),
                   "--jsonl", str(self.transcript)]
        if not stdin_prompt:
            prompt_file = root / "prompt.txt"
            prompt_file.write_text("Simulate a supervised turn.", encoding="utf-8")
            command.extend(["--prompt-file", str(prompt_file)])
        command.extend(extra_args or [])
        self.process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        stderr=subprocess.PIPE, env=self.env, text=True)
        if stdin_prompt:
            self.process.stdin.write("Prompt from stdin.")
        self.process.stdin.close()
        self.process.stdin = None

    def cleanup(self) -> None:
        if self.process.poll() is None:
            self.process.terminate()
        try:
            self.process.communicate(timeout=4)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.communicate(timeout=2)
        for row in self.records():
            if row["event"] == "started":
                try:
                    os.killpg(row["pid"], signal.SIGKILL)
                except ProcessLookupError:
                    pass

    def records(self) -> list:
        if not self.record.exists():
            return []
        # A concurrent final JSONL append may still be in progress.
        return [json.loads(line) for line in self.record.read_text().splitlines() if line.endswith("}")]

    def pending(self, count: int = 1) -> list:
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline:
            files = sorted((self.task_dir / "pending").glob("*.json"), key=lambda path: int(path.name.split("-", 1)[0]))
            if len(files) >= count:
                return [json.loads(path.read_text()) for path in files]
            if self.process.poll() is not None:
                break
            time.sleep(0.01)
        raise AssertionError(f"pending not published; records: {self.records()}")

    def reply(self, env: dict, data: dict, filename_seq: Optional[int] = None) -> None:
        seq = env["seq"] if filename_seq is None else filename_seq
        write_atomic(self.task_dir / "replies" / f"{seq}.json", data)

    def finish(self) -> tuple:
        stdout, stderr = self.process.communicate(timeout=6)
        if stderr:
            raise AssertionError(stderr)
        status = json.loads((self.task_dir / "status.json").read_text())
        return self.process.returncode, status, stdout

    def ack(self, env: dict) -> dict:
        return json.loads((self.task_dir / "acks" / f"{env['seq']}.json").read_text())

    def received(self, method: str) -> list:
        return [row["message"] for row in self.records()
                if row["event"] == "receive" and row["message"].get("method") == method]

    def approvals(self) -> list:
        return [row for row in self.records() if row["event"] == "approval"]


class AdapterTests(unittest.TestCase):
    def launch(self, scenario: Optional[dict] = None, **kwargs: object) -> Run:
        temp = tempfile.TemporaryDirectory(prefix="codex-adapter-test-")
        self.addCleanup(temp.cleanup)
        run = Run(Path(temp.name), scenario if scenario is not None else {"requests": [{"method": COMMAND}]}, **kwargs)
        self.addCleanup(run.cleanup)
        return run

    def assert_not_executed(self, run: Run) -> None:
        self.assertTrue(run.records())
        self.assertFalse(any(row.get("executed") for row in run.records()))
        self.assertFalse(any(row.get("accepted") for row in run.approvals()))

    def assert_child_exited(self, run: Run) -> None:
        pid = next(row["pid"] for row in run.records() if row["event"] == "started")
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)

    def test_command_deny_and_ahp_identity(self) -> None:
        run = self.launch()
        env = run.pending()[0]
        self.assertEqual(env["protocol"], "ahp/1")
        self.assertEqual(env["event"], "permission.request")
        self.assertEqual(env["class"], "gate")
        self.assertEqual(env["task_id"], "fixture")
        self.assertEqual(env["producer"]["name"], "cc-cursor-bridge")
        self.assertEqual(env["producer"]["host"], {"name": "codex-app-server", "version": "0.167.0"})
        self.assertEqual(env["decisions"], ["allow", "deny"])
        self.assertEqual(env["default_decision"], "deny")
        self.assertEqual(env["payload"]["tool_name"], "commandExecution")
        self.assertEqual(env["payload"]["tool_input"]["cwd"], str(run.cwd))
        identity = {"method": COMMAND, "request_id": "server-1", "thread_id": "thread-1",
                    "turn_id": "turn-1", "item_id": "item-1", "approval_id": "approval-1"}
        for key, value in identity.items():
            self.assertEqual(env["native"][key], value)
        self.assertEqual(env["native"].get("reason"), "fixture approval")
        self.assertEqual((env["payload"].get("approval_context") or {}).get("reason"), "fixture approval")
        state = json.loads((run.task_dir / "state" / "codex-requests.jsonl").read_text())
        self.assertEqual(state, {"seq": env["seq"], **identity})
        caps = json.loads((run.task_dir / "capabilities.json").read_text())
        self.assertIn("x-codex-adapter", caps)
        run.reply(env, {"protocol": "ahp/1", "seq": env["seq"], "decision": "deny"})
        code, status, stdout = run.finish()
        self.assertEqual(code, 0)
        self.assertEqual(status["state"], "ended")
        self.assertEqual(status["turn_status"], "completed")
        self.assertTrue(status["success"])
        self.assertIn(str(run.task_dir), stdout)
        self.assertIn(str(run.task_dir / "status.json"), stdout)
        self.assertEqual(run.approvals()[0]["response"]["result"], {"decision": "decline"})
        self.assertEqual(run.ack(env)["applied_decision"], "deny")
        self.assert_not_executed(run)
        self.assert_child_exited(run)

    def test_allow_is_one_shot_and_next_gate_needs_reply(self) -> None:
        run = self.launch({"requests": [{"method": COMMAND}, {"method": COMMAND}]})
        first = run.pending()[0]
        run.reply(first, {"decision": "accept"})
        pending = run.pending(2)
        second = next(env for env in pending if env["seq"] != first["seq"])
        self.assertNotEqual(first["native"]["approval_id"], second["native"]["approval_id"])
        run.reply(second, {"decision": "deny"})
        self.assertEqual(run.finish()[0], 0)
        responses = [row["response"]["result"] for row in run.approvals()]
        self.assertEqual(responses, [{"decision": "accept"}, {"decision": "decline"}])
        self.assertTrue(run.approvals()[0]["accepted"])
        self.assertFalse(run.approvals()[1]["accepted"])
        self.assertFalse((run.cwd / "simulated.txt").exists())

    def test_file_change_deny(self) -> None:
        run = self.launch({"requests": [{"method": FILE_CHANGE}]})
        env = run.pending()[0]
        self.assertEqual(env["payload"]["tool_name"], "fileChange")
        self.assertTrue(env["payload"]["tool_input"]["changes"])
        run.reply(env, {"decision": "decline"})
        self.assertEqual(run.finish()[0], 0)
        self.assertEqual(run.approvals()[0]["response"]["result"], {"decision": "decline"})
        self.assert_not_executed(run)

    def test_timeout_defaults_to_deny(self) -> None:
        run = self.launch(timeout=0.2)
        env = run.pending()[0]
        code, status, _ = run.finish()
        self.assertNotEqual(code, 0)
        self.assertEqual(run.ack(env)["via"], "timeout")
        self.assertEqual(run.ack(env)["status"], "expired")
        self.assertEqual(status["last_gate"]["via"], "timeout")
        self.assertTrue(status["fail_closed"])
        self.assertFalse(status["success"])
        self.assertEqual(status["turn_status"], "completed")
        self.assert_not_executed(run)

    def test_controller_sigterm_declines_and_reaps_child(self) -> None:
        run = self.launch(timeout=3)
        env = run.pending()[0]
        run.process.terminate()
        code, status, _ = run.finish()
        self.assertNotEqual(code, 0)
        self.assertEqual(status["state"], "ended")
        self.assertEqual(status.get("session_end_reason"), "cancelled")
        self.assertNotEqual(status["turn_status"], "completed")
        self.assertFalse(status["success"])
        self.assertEqual(run.ack(env)["applied_decision"], "deny")
        self.assertTrue(run.received("turn/interrupt"))
        self.assert_not_executed(run)
        self.assert_child_exited(run)

    def test_server_disconnect_fails_closed(self) -> None:
        run = self.launch({"requests": [{"method": COMMAND}], "disconnect": True})
        code, status, _ = run.finish()
        self.assertNotEqual(code, 0)
        self.assertEqual(status["state"], "ended")
        self.assertEqual(status.get("session_end_reason"), "failed")
        self.assertNotEqual(status["turn_status"], "completed")
        self.assert_not_executed(run)
        self.assert_child_exited(run)

    def test_unknown_and_permissions_requests_get_error_then_interrupt(self) -> None:
        for method in ("future/unsafeRequest", "account/chatgptAuthTokens/refresh", PERMISSIONS):
            with self.subTest(method=method):
                run = self.launch({"requests": [{"method": method}]})
                code, status, _ = run.finish()
                self.assertNotEqual(code, 0)
                self.assertFalse(status["success"])
                responses = [row["message"] for row in run.records()
                             if row["event"] == "receive" and row["message"].get("id") == "server-1"]
                self.assertEqual(len(responses), 1)
                self.assertIn("error", responses[0])
                self.assertNotIn("result", responses[0])
                self.assertTrue(run.received("turn/interrupt"))
                events = [json.loads(line) for line in (run.task_dir / "events.jsonl").read_text().splitlines()]
                self.assertTrue(any(env["event"] == "producer.error" for env in events))
                self.assert_not_executed(run)

    def test_forbidden_and_invalid_replies_are_denied(self) -> None:
        for reply in ({"decision": "acceptForSession"},
                      {"decision": {"acceptWithExecpolicyAmendment": {"execpolicy_amendment": ["echo"]}}},
                      {"decision": "allow", "permissions": {}},
                      {"decision": "allow", "updated_input": {"command": "changed"}},
                      {"decision": "allow", "applyNetworkPolicyAmendment": {}},
                      {"decision": "allow", "acceptForSession": False},
                      {"decision": "allow", "unknown_field": True},
                      {"decision": "allow", "protocol": "ahp/2"}):
            with self.subTest(reply=reply):
                run = self.launch()
                env = run.pending()[0]
                run.reply(env, reply)
                code, status, _ = run.finish()
                self.assertNotEqual(code, 0)
                self.assertTrue(status["fail_closed"])
                self.assertEqual(run.ack(env)["via"], "invalid_reply")
                self.assertEqual(run.ack(env)["applied_decision"], "deny")
                self.assert_not_executed(run)

    def test_wrong_embedded_seq_cannot_approve(self) -> None:
        run = self.launch()
        env = run.pending()[0]
        run.reply(env, {"seq": env["seq"] + 1, "decision": "allow"})
        self.assertNotEqual(run.finish()[0], 0)
        self.assertEqual(run.ack(env)["via"], "invalid_reply")
        self.assert_not_executed(run)

    def test_wrong_reply_file_does_not_approve(self) -> None:
        run = self.launch(timeout=0.3)
        env = run.pending()[0]
        run.reply(env, {"decision": "allow"}, filename_seq=env["seq"] + 100)
        self.assertNotEqual(run.finish()[0], 0)
        self.assertEqual(run.ack(env)["via"], "timeout")
        self.assert_not_executed(run)

    def test_redaction_keeps_numeric_usage(self) -> None:
        run = self.launch({"secret": SECRET, "stderr_chunks": [SECRET[:12], SECRET[12:] + "\n"]})
        code, status, _ = run.finish()
        self.assertEqual(code, 0)
        rpc = run.transcript.read_text()
        events = (run.task_dir / "events.jsonl").read_text()
        self.assertNotIn(SECRET, rpc + events)
        self.assertNotIn("planted-secret", rpc)
        self.assertIn("[redacted]", rpc)
        self.assertIn("[redacted]", events)
        stderr = [json.loads(line)["message"] for line in rpc.splitlines() if json.loads(line)["direction"] == "stderr"]
        self.assertTrue(stderr)
        self.assertEqual("".join(stderr).strip(), "[redacted]")
        self.assertEqual(status["usage"]["total"]["inputTokens"], 12)
        notifications = [json.loads(line)["message"] for line in rpc.splitlines()
                         if json.loads(line)["direction"] == "receive"]
        usage = next(m["params"]["tokenUsage"] for m in notifications if m.get("method") == "thread/tokenUsage/updated")
        self.assertEqual(usage["total"]["totalTokens"], 16)
        end = next(json.loads(line) for line in events.splitlines() if json.loads(line)["event"] == "session.end")
        self.assertEqual(end["payload"]["tokenUsage"]["total"]["inputTokens"], 12)

    def test_existing_bridge_cli_can_inspect_and_reply(self) -> None:
        run = self.launch()
        env = run.pending()[0]
        pending = subprocess.run([sys.executable, str(SCRIPTS / "bridge.py"), "pending", "fixture", "--ahp"],
                                 capture_output=True, text=True, env=run.env, timeout=3)
        self.assertEqual(pending.returncode, 0, pending.stderr)
        self.assertIn("permission.request", pending.stdout)
        reply = subprocess.run([sys.executable, str(SCRIPTS / "bridge.py"), "reply", "fixture", str(env["seq"]),
                               '{"decision":"deny"}', "--ahp"],
                                capture_output=True, text=True, env=run.env, timeout=3)
        self.assertEqual(reply.returncode, 0, reply.stderr)
        self.assertEqual(run.finish()[0], 0)
        self.assertEqual(run.ack(env)["via"], "reply")
        self.assert_not_executed(run)

    def test_malformed_or_symlink_reply_is_not_applied(self) -> None:
        for kind in ("json", "symlink"):
            with self.subTest(kind=kind):
                run = self.launch()
                env = run.pending()[0]
                path = run.task_dir / env["reply_file"]
                if kind == "json":
                    path.write_text('{"decision":')
                else:
                    target = run.root / "untrusted-reply.json"
                    write_atomic(target, {"decision": "allow"})
                    path.symlink_to(target)
                self.assertNotEqual(run.finish()[0], 0)
                self.assertEqual(run.ack(env)["via"], "invalid_reply")
                self.assert_not_executed(run)

    def test_later_invocation_starts_new_turn_without_reusing_reply(self) -> None:
        first = self.launch()
        old_env = first.pending()[0]
        first.reply(old_env, {"decision": "deny"})
        self.assertEqual(first.finish()[0], 0)
        second = Run(first.root, {"requests": [{"method": COMMAND}]}, timeout=0.3)
        self.addCleanup(second.cleanup)
        env = second.pending(2)[-1]
        self.assertGreater(env["seq"], old_env["seq"])
        second.reply(old_env, {"decision": "allow"})
        self.assertNotEqual(second.finish()[0], 0)
        self.assertEqual(len(second.received("thread/start")), 1)
        self.assertEqual(len(second.received("turn/start")), 1)
        self.assertEqual(second.ack(env)["via"], "timeout")
        self.assert_not_executed(second)

    def test_second_controller_cannot_overwrite_active_task(self) -> None:
        first = self.launch(timeout=3)
        env = first.pending()[0]
        second = Run(first.root, {})
        self.addCleanup(second.cleanup)
        _, stderr = second.process.communicate(timeout=3)
        self.assertNotEqual(second.process.returncode, 0)
        self.assertIn("another controller owns this task", stderr)
        self.assertFalse(second.record.exists())
        first.reply(env, {"decision": "deny"})
        self.assertEqual(first.finish()[0], 0)

    def test_requested_policy_is_on_wire_and_stdin_prompt_works(self) -> None:
        run = self.launch({}, stdin_prompt=True)
        self.assertEqual(run.finish()[0], 0)
        init = run.received("initialize")[0]["params"]
        self.assertEqual(init["clientInfo"], {"name": "cc-cursor-bridge-codex", "version": __version__})
        self.assertEqual(len(run.received("initialized")), 1)
        thread = run.received("thread/start")[0]["params"]
        turn = run.received("turn/start")[0]["params"]
        for params in (thread, turn):
            self.assertEqual(params["model"], "fixture-model")
            self.assertEqual(params["cwd"], str(run.cwd))
            self.assertEqual(params["approvalPolicy"], "on-request")
            self.assertEqual(params["approvalsReviewer"], "user")
        self.assertEqual(thread["sandbox"], "workspace-write")
        self.assertTrue(thread["ephemeral"])
        self.assertEqual(turn["effort"], "medium")
        self.assertEqual(turn["input"], [{"type": "text", "text": "Prompt from stdin."}])
        self.assertEqual(turn["sandboxPolicy"], {"type": "workspaceWrite", "networkAccess": False,
                                                "writableRoots": [str(run.cwd)], "excludeSlashTmp": True,
                                                "excludeTmpdirEnvVar": True})

    def test_extra_fields_and_framing_and_early_approval(self) -> None:
        for allow in (True, False):
            with self.subTest(allow=allow):
                run = self.launch({"requests": [{"method": COMMAND}], "extra_fields": True,
                                   "content_length": True, "fragment_bytes": 3,
                                   "approval_before_start_response": True})
                env = run.pending()[0]
                run.reply(env, {"decision": "allow" if allow else "deny"})
                self.assertEqual(run.finish()[0], 0)
                self.assertEqual(run.approvals()[0]["accepted"], allow)

    def test_legacy_review_decisions(self) -> None:
        for method in ("execCommandApproval", "applyPatchApproval"):
            with self.subTest(method=method):
                run = self.launch({"requests": [{"method": method}]})
                env = run.pending()[0]
                self.assertEqual(env["native"]["item_id"], "item-1")
                run.reply(env, {"decision": "deny", "reason": "fixture denied"})
                self.assertEqual(run.finish()[0], 0)
                self.assertEqual(run.approvals()[0]["response"]["result"],
                                 {"decision": {"denied": {"rejection": "fixture denied"}}})
                self.assert_not_executed(run)

    def test_whole_turn_timeout_interrupts_pending_gate(self) -> None:
        run = self.launch(timeout=3, turn_timeout=0.3)
        env = run.pending()[0]
        code, status, _ = run.finish()
        self.assertNotEqual(code, 0)
        self.assertEqual(status["state"], "ended")
        self.assertFalse(status["success"])
        self.assertIn("whole turn timed out", status["error"])
        self.assertEqual(run.ack(env)["applied_decision"], "deny")
        self.assertTrue(run.received("turn/interrupt"))
        self.assert_not_executed(run)
        self.assert_child_exited(run)

    def test_failed_and_interrupted_turn_status_is_preserved(self) -> None:
        for turn_status in ("failed", "interrupted"):
            with self.subTest(turn_status=turn_status):
                run = self.launch({"turn_status": turn_status})
                code, status, _ = run.finish()
                self.assertNotEqual(code, 0)
                self.assertEqual(status["state"], "ended")
                self.assertEqual(status["turn_status"], turn_status)
                self.assertFalse(status["success"])

    def test_shutdown_kills_uncooperative_server(self) -> None:
        run = self.launch({"ignore_termination": True})
        self.assertEqual(run.finish()[0], 0)
        self.assert_child_exited(run)

    def test_cli_help_required_args_and_safe_task_id(self) -> None:
        help_result = subprocess.run([sys.executable, str(SCRIPTS / "run_codex_bridged.py"), "--help"],
                                     capture_output=True, text=True, timeout=3)
        self.assertEqual(help_result.returncode, 0)
        self.assertIn("--effort", help_result.stdout)
        missing = subprocess.run([sys.executable, str(SCRIPTS / "run_codex_bridged.py")],
                                 capture_output=True, text=True, timeout=3)
        self.assertNotEqual(missing.returncode, 0)
        run = self.launch({}, extra_args=["--task", "../escape"])
        stdout, stderr = run.process.communicate(timeout=3)
        self.assertNotEqual(run.process.returncode, 0)
        self.assertIn("unsafe task id", stderr)
        self.assertFalse(run.task_dir.exists())
        self.assertFalse(run.record.exists())

    def test_session_requires_model_effort_and_positive_timeouts(self) -> None:
        for model, effort, timeout in (("", "medium", 1), ("fixture", "", 1),
                                        ("fixture", "medium", 0), ("fixture", "medium", float("nan"))):
            with self.subTest(model=model, effort=effort, timeout=timeout):
                with self.assertRaises(ValueError):
                    Session(task_id="fixture", cwd=SCRIPTS, model=model, effort=effort,
                            timeout_seconds=timeout)

    def test_preexisting_reply_does_not_authorize_later_gate(self) -> None:
        temp = tempfile.TemporaryDirectory(prefix="codex-adapter-stale-")
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        replies = root / "ahp" / "tasks" / "fixture" / "replies"
        replies.mkdir(parents=True)
        # Plant stale allows against not-yet-published seqs. The first real gate
        # must stash the matching file and wait for a post-publish deny.
        for seq in range(1, 11):
            write_atomic(replies / f"{seq}.json", {"decision": "allow"})
        run = Run(root, {"requests": [{"method": COMMAND}]})
        self.addCleanup(run.cleanup)
        env = run.pending()[0]
        seq = env["seq"]
        self.assertFalse((run.task_dir / "replies" / f"{seq}.json").exists())
        self.assertTrue(list(run.task_dir.glob(f"replies/{seq}.preexisting-*.json")))
        run.reply(env, {"decision": "deny"})
        code, status, _ = run.finish()
        self.assertEqual(code, 0)
        self.assertTrue(status.get("success", True))
        self.assertEqual(run.ack(env)["applied_decision"], "deny")
        self.assertEqual(run.ack(env)["via"], "reply")
        self.assert_not_executed(run)

    def test_approval_context_is_recorded_and_scope_extensions_are_denied(self) -> None:
        params = {
            "availableDecisions": ["accept", "acceptForSession", "cancel"],
            "networkApprovalContext": {"host": "example.test", "reason": "fixture"},
            "additionalPermissions": {"network": {"enabled": True}},
            "grantRoot": "/tmp",
            "proposedExecpolicyAmendment": ["echo"],
        }
        run = self.launch({"requests": [{"method": COMMAND, "params": params}]})
        # Scope extensions are denied immediately; no allow path is offered.
        code, status, _ = run.finish()
        self.assertNotEqual(code, 0)
        self.assertFalse(status["success"])
        events = [json.loads(line) for line in (run.task_dir / "events.jsonl").read_text().splitlines()]
        gates = [env for env in events if env.get("event") == "permission.request"]
        self.assertTrue(gates)
        ctx = gates[0]["payload"].get("approval_context") or {}
        self.assertEqual(ctx.get("grantRoot"), "/tmp")
        self.assertEqual(ctx.get("availableDecisions"), ["accept", "acceptForSession", "cancel"])
        self.assertEqual((ctx.get("additionalPermissions") or {}).get("network"), {"enabled": True})
        ack = json.loads(next((run.task_dir / "acks").glob("*.json")).read_text())
        self.assertEqual(ack["applied_decision"], "deny")
        self.assertIn("not supported", ack.get("reason", ""))
        self.assert_not_executed(run)

    def test_supervisor_treats_unsuccessful_end_as_terminal(self) -> None:
        run = self.launch({"requests": [{"method": COMMAND}], "disconnect": True})
        code, status, _ = run.finish()
        self.assertEqual(status["state"], "ended")
        self.assertFalse(status["success"])
        env = dict(os.environ, CC_BRIDGE_AHP_DIR=str(run.root / "ahp"),
                   CC_BRIDGE_DIR=str(run.root / "bridge"))
        result = subprocess.run([sys.executable, str(SCRIPTS / "supervise.py"), "--ahp", "--once", "fixture"],
                                capture_output=True, text=True, env=env, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("task fixture ended", result.stdout)
        self.assertIn("state: ended", result.stdout)
        self.assertIn("success: false", result.stdout)
        self.assertIn("turn_status:", result.stdout)


if __name__ == "__main__":
    unittest.main()
