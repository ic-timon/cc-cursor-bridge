#!/usr/bin/env python3
"""Unit tests for codex_appserver decisions and redaction. No Codex process."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from codex_appserver.decisions import (
    DecisionError,
    approval_result,
    classify_server_request,
    jsonrpc_error_payload,
    normalize_gate_decision,
    permissions_rejection,
    timeout_decision,
    unsupported_approval_scope,
)
from codex_appserver.redact import REDACTED, redact, redact_text


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
        self.assertIn("grantRoot", unsupported_approval_scope({"grantRoot": "/tmp"}) or "")
        self.assertIn("network", unsupported_approval_scope(
            {"additionalPermissions": {"network": {"enabled": True}}}) or "")

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


if __name__ == "__main__":
    unittest.main()
