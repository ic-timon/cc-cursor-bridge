#!/usr/bin/env python3
"""Unit tests for codex_appserver decisions and redaction. No Codex process."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))

from codex_appserver.decisions import (
    DecisionError,
    approval_result,
    classify_server_request,
    decisions_for_approval,
    jsonrpc_error_payload,
    normalize_gate_decision,
    permissions_rejection,
    timeout_decision,
    unsupported_approval_scope,
)
from codex_appserver.redact import REDACTED, redact, redact_text
from codex_appserver.store import Store
from ahp_cc import write_atomic


class DecisionTest(unittest.TestCase):
    def test_allow_and_deny_map_to_accept_and_decline(self):
        self.assertEqual(normalize_gate_decision({"decision": "allow"}), "allow")
        self.assertEqual(normalize_gate_decision({"decision": "deny"}), "deny")
        self.assertEqual(normalize_gate_decision({"decision": "accept"}), "allow")
        self.assertEqual(normalize_gate_decision({"decision": "decline"}), "deny")
        self.assertEqual(
            approval_result("item/commandExecution/requestApproval", "allow"),
            {"decision": "accept"},
        )
        self.assertEqual(
            approval_result("item/fileChange/requestApproval", "deny"),
            {"decision": "decline"},
        )

    def test_rejects_session_grants_and_amendments(self):
        for payload in (
            {"decision": "acceptForSession"},
            {"decision": {"acceptWithExecpolicyAmendment": {"execpolicy_amendment": ["ls"]}}},
            {"decision": "deny", "applyNetworkPolicyAmendment": {"host": "x", "action": "allow"}},
            {"decision": "allow", "permissions": {"network": {"enabled": True}}},
            {"decision": "approved_for_session"},
        ):
            with self.subTest(payload=payload):
                with self.assertRaises(DecisionError):
                    normalize_gate_decision(payload)

    def test_timeout_is_deny(self):
        self.assertEqual(timeout_decision(), "deny")

    def test_permissions_class_has_no_grant_result(self):
        self.assertEqual(classify_server_request("item/permissions/requestApproval"), "permissions")
        kind, err = permissions_rejection()
        self.assertEqual(kind, "jsonrpc_error")
        self.assertIn("code", err)
        self.assertIn("message", err)
        with self.assertRaises(DecisionError):
            approval_result("item/permissions/requestApproval", "deny")

    def test_unsupported_scope(self):
        self.assertIsNone(unsupported_approval_scope({"command": "echo"}))
        self.assertIsNone(unsupported_approval_scope({"networkApprovalContext": None}))
        for params, reason in (
            ({"networkApprovalContext": {}}, "networkApprovalContext"),
            ({"grantRoot": "/tmp"}, "grantRoot"),
            ({"additionalPermissions": {"network": {"enabled": True}}}, "network permission"),
            ({"additionalPermissions": {"fileSystem": {"write": ["/tmp"]}}}, "filesystem permission"),
            ({"permissions": {"filesystem": {"write": ["/tmp"]}}}, "filesystem permission"),
            ({"additionalPermissions": {"futureGrant": True}}, "additionalPermissions"),
            *[({key: ["echo"]}, key) for key in (
                "proposedNetworkPolicyAmendments",
                "proposed_network_policy_amendments")],
        ):
            with self.subTest(params=params):
                self.assertIn(reason, unsupported_approval_scope(params) or "")

    def test_proposed_execpolicy_amendment_is_not_itself_unsupported(self):
        # Real 0.167 command approvals often include this proposal alongside
        # a one-shot accept. It must not fail the one-shot path by itself.
        self.assertIsNone(unsupported_approval_scope({
            "command": "echo",
            "proposedExecpolicyAmendment": ["/bin/bash", "-lc", "echo"],
            "availableDecisions": ["accept", {"acceptWithExecpolicyAmendment": {
                "execpolicy_amendment": ["echo"]}}, "cancel"],
        }))
        self.assertIsNone(unsupported_approval_scope({
            "proposed_execpolicy_amendment": ["echo"]}))

    def test_available_decisions_only_allow_one_shot(self):
        self.assertEqual(decisions_for_approval({}), ["allow", "deny"])
        for menu in ([], None, ["acceptForSession", "cancel"],
                     [{"acceptWithExecpolicyAmendment": {}}], {"accept": True}):
            with self.subTest(menu=menu):
                self.assertEqual(decisions_for_approval({"availableDecisions": menu}), ["deny"])
        for menu in (["accept", "cancel"], ["allow"], ["acceptForSession", "accept"]):
            with self.subTest(menu=menu):
                self.assertEqual(decisions_for_approval({"availableDecisions": menu}), ["allow", "deny"])

    def test_unknown_and_unhandled_classes(self):
        self.assertEqual(classify_server_request("attestation/generate"), "unhandled")
        self.assertEqual(classify_server_request("item/tool/call"), "unhandled")
        self.assertEqual(classify_server_request("totally/unknown"), "unknown")


class RedactTest(unittest.TestCase):
    def test_keeps_token_usage_metrics(self):
        payload = {
            "tokenUsage": {
                "total": {"totalTokens": 100, "inputTokens": 40, "outputTokens": 20,
                          "cachedInputTokens": 10, "reasoningOutputTokens": 5},
                "last": {"total_tokens": 50, "input_tokens": 30},
                "modelContextWindow": 475000,
            },
            "authorization": "Bearer secret-should-go",
            "text": "key sk-abcdefghijklmnopqrstuvwxyz",
        }
        out = redact(payload)
        usage = out["tokenUsage"]
        self.assertEqual(usage["total"]["totalTokens"], 100)
        self.assertEqual(usage["total"]["inputTokens"], 40)
        self.assertEqual(usage["last"]["total_tokens"], 50)
        self.assertEqual(usage["modelContextWindow"], 475000)
        self.assertEqual(out["authorization"], REDACTED)
        self.assertIn(REDACTED, out["text"])
        self.assertNotIn("sk-abcdefghijklmnopqrstuvwxyz", out["text"])

    def test_redacts_extra_secrets_in_strings(self):
        self.assertEqual(redact_text("x=abc123y", extra_secrets=["abc123"]), f"x={REDACTED}y")


class StoreTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="codex-store-test-")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.store = Store("fixture", self.root, 1, {"CC_BRIDGE_AHP_DIR": str(self.root / "ahp")})
        self.store.open()
        self.addCleanup(self.store.close)

    def open_gate(self, **context):
        return self.store.open_gate("item/commandExecution/requestApproval", "server-1", {
            "threadId": "thread-1", "turnId": "turn-1", "command": "echo fixture", **context})

    def test_stashes_publication_window_replies_and_rejects_restored_allows(self):
        reply_path = self.store.dir / "replies" / "1.json"
        write_atomic(reply_path, {"decision": "allow"})
        old_time = time.time_ns() - 1_000_000_000
        os.utime(reply_path, ns=(old_time, old_time))

        def publish(path, data):
            if path.parent.name == "pending":
                # This second allow appears after the initial stash, while the
                # pending envelope is being published.
                write_atomic(reply_path, {"decision": "allow", "reason": "publication race"})
            write_atomic(path, data)
            if path.parent.name == "pending":
                # Model a coarse filesystem where both writes share an mtime.
                stamp = path.stat().st_mtime_ns
                os.utime(reply_path, ns=(stamp, stamp))

        with patch("codex_appserver.store.write_atomic", side_effect=publish):
            env = self.open_gate()
        self.assertFalse(reply_path.exists())
        stashed = list(reply_path.parent.glob("1.preexisting-*.json"))
        self.assertEqual(len(stashed), 2)
        self.assertTrue(any(path.stat().st_mtime_ns == env["published_mtime_ns"] for path in stashed))
        audit = json.loads((self.store.dir / "events.jsonl").read_text())
        self.assertEqual(audit["published_mtime_ns"], env["published_mtime_ns"])
        for path in stashed:
            path.replace(reply_path)
            self.assertIsNone(self.store.poll_reply(env))
        write_atomic(reply_path, {"decision": "allow"})
        self.assertEqual(self.store.poll_reply(env), ("allow", "reply", ""))

    def test_unrelated_reply_with_old_mtime_is_ignored(self):
        env = self.open_gate()
        reply_path = self.store.dir / env["reply_file"]
        write_atomic(reply_path, {"decision": "allow"})
        old_time = env["published_mtime_ns"] - 1_000_000_000
        os.utime(reply_path, ns=(old_time, old_time))
        self.assertIsNone(self.store.poll_reply(env))
        write_atomic(reply_path, {"decision": "allow"})
        self.assertEqual(self.store.poll_reply(env), ("allow", "reply", ""))

    def test_persisted_menu_cannot_be_overridden_by_allow_reply(self):
        env = self.open_gate(availableDecisions=["acceptForSession", "cancel"])
        self.assertEqual(env["decisions"], ["deny"])
        reply_path = self.store.dir / env["reply_file"]
        write_atomic(reply_path, {"decision": "allow"})
        self.assertEqual(self.store.poll_reply(env)[:2], ("deny", "invalid_reply"))
        write_atomic(reply_path, {"decision": "cancel"})
        self.assertEqual(self.store.poll_reply(env), ("deny", "reply", ""))


if __name__ == "__main__":
    unittest.main()
