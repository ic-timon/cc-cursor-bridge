"""Pure decision validation and Codex JSON-RPC result mapping.

Allowed consumer decisions for command/fileChange gates are AHP allow|deny
(and the input.request aliases accept|decline). Session grants, execpolicy
amendments, network policy amendments, and acceptForSession are rejected.

Unsupported permission classes (item/permissions/requestApproval and any
unknown server request) must not be answered with a grant result. The
verified client responses are taken from the generated 0.167.0 schema:

- CommandExecutionRequestApprovalResponse / FileChangeRequestApprovalResponse
  include decision=decline and decision=cancel.
- PermissionsRequestApprovalResponse requires `permissions` and has no
  decline/cancel field. Do not invent a grant. Complete the JSON-RPC request
  with a JSONRPCError (schema title JSONRPCError) and call turn/interrupt
  (TurnInterruptParams). This was not empirically exercised in the
  2026-11-11 canary proof, which used commandExecution + decline.
"""

from __future__ import annotations

from typing import Any, Dict, Mapping, Optional, Tuple

# AHP permission.request / tool.pre
ALLOWED_GATE_DECISIONS = frozenset({"allow", "deny", "accept", "decline"})
DENY_DECISIONS = frozenset({"deny", "decline", "cancel", "block"})
ALLOW_DECISIONS = frozenset({"allow", "accept"})

FORBIDDEN_REPLY_KEYS = frozenset({
    "acceptForSession",
    "acceptWithExecpolicyAmendment",
    "applyNetworkPolicyAmendment",
    "approved_for_session",
    "approved_execpolicy_amendment",
    "network_policy_amendment",
    "permissions",  # must not be smuggled through an AHP reply into a grant
    "scope",
    "strictAutoReview",
    "grantRoot",
})

COMMAND_METHODS = frozenset({
    "item/commandExecution/requestApproval",
    "execCommandApproval",
})
FILECHANGE_METHODS = frozenset({
    "item/fileChange/requestApproval",
    "applyPatchApproval",
})
PERMISSIONS_METHODS = frozenset({
    "item/permissions/requestApproval",
})

# Other server-initiated requests we never silently fulfill.
UNHANDLED_SERVER_REQUESTS = frozenset({
    "item/tool/requestUserInput",
    "mcpServer/elicitation/request",
    "item/tool/call",
    "account/chatgptAuthTokens/refresh",
    "attestation/generate",
})

JSONRPC_APPLICATION_ERROR = -32001

SupportedKind = str  # "command" | "fileChange"


class DecisionError(ValueError):
    """Reply or request cannot be applied safely."""


def classify_server_request(method: str) -> str:
    """Return command|fileChange|permissions|unhandled|unknown."""
    if method in COMMAND_METHODS:
        return "command"
    if method in FILECHANGE_METHODS:
        return "fileChange"
    if method in PERMISSIONS_METHODS:
        return "permissions"
    if method in UNHANDLED_SERVER_REQUESTS:
        return "unhandled"
    return "unknown"


def normalize_gate_decision(reply: Mapping[str, Any]) -> str:
    """Map a consumer reply object to allow or deny. Raise DecisionError otherwise."""
    if not isinstance(reply, Mapping):
        raise DecisionError("reply must be an object")
    for key in FORBIDDEN_REPLY_KEYS:
        if key in reply:
            raise DecisionError(f"forbidden reply field: {key}")
    # Nested grant-like payloads
    decision = reply.get("decision")
    if isinstance(decision, Mapping):
        raise DecisionError("object-valued decision is not allowed (no amendments or grants)")
    if decision is None:
        raise DecisionError("missing decision")
    if not isinstance(decision, str):
        raise DecisionError("decision must be a string")
    if decision in {"acceptForSession", "approved_for_session", "approved", "approved_mcp_policy_amendment"}:
        raise DecisionError(f"session or persistent grant is not allowed: {decision}")
    if decision in ALLOW_DECISIONS:
        return "allow"
    if decision in DENY_DECISIONS:
        return "deny"
    raise DecisionError(f"unsupported decision: {decision}")


def command_result(decision: str) -> Dict[str, Any]:
    """JSON-RPC result for command/fileChange requestApproval (0.167 response schema)."""
    if decision == "allow":
        return {"decision": "accept"}  # one-shot only
    if decision == "deny":
        return {"decision": "decline"}
    raise DecisionError(f"cannot map {decision!r} to a command/fileChange result")


def legacy_review_result(decision: str, reason: str = "denied-by-controller") -> Dict[str, Any]:
    """Result for legacy execCommandApproval / applyPatchApproval ReviewDecision."""
    if decision == "allow":
        return {"decision": "approved"}
    if decision == "deny":
        return {"decision": {"denied": {"rejection": reason or "denied-by-controller"}}}
    raise DecisionError(f"cannot map {decision!r} to a legacy review result")


def approval_result(method: str, decision: str, reason: str = "") -> Dict[str, Any]:
    if method in {"execCommandApproval", "applyPatchApproval"}:
        return legacy_review_result(decision, reason)
    if classify_server_request(method) in {"command", "fileChange"}:
        return command_result(decision)
    raise DecisionError(f"method {method} has no grant/decline result mapping")


def jsonrpc_error_payload(message: str, code: int = JSONRPC_APPLICATION_ERROR) -> Dict[str, Any]:
    """Generated JSONRPCErrorError shape. Used when no schema decline exists."""
    return {"code": int(code), "message": str(message)}


def permissions_rejection() -> Tuple[str, Dict[str, Any]]:
    """How to finish item/permissions/requestApproval without granting.

    Returns ("jsonrpc_error", error_object). Caller must also turn/interrupt.
    """
    return (
        "jsonrpc_error",
        jsonrpc_error_payload(
            "declined: permissions grant is not authorized; "
            "PermissionsRequestApprovalResponse has no decline field"
        ),
    )


def unsupported_approval_scope(params: dict) -> str | None:
    """Return a reason if the request asks for a persistent or extra grant.

    The adapter only supports one-shot command/fileChange allow|deny. Network
    enables, extra filesystem grants, grantRoot, and network policy amendments
    are rejected without offering a durable grant path. A proposed execpolicy
    amendment on the request is not itself a grant.
    """
    if not isinstance(params, dict):
        return None
    if params.get("networkApprovalContext") is not None:
        return "networkApprovalContext (managed-network approval) is not supported"
    if params.get("grantRoot"):
        return "grantRoot is not supported"
    # A proposed execpolicy amendment is an optional alternative the real 0.167
    # server often attaches to an otherwise one-shot command approval (menu still
    # includes accept). Record it for audit; do not fail the one-shot path.
    # Replies that apply the amendment remain forbidden. Network policy
    # amendments are grants and stay rejected.
    for key in ("proposedNetworkPolicyAmendments", "proposed_network_policy_amendments"):
        value = params.get(key)
        if value:
            return f"{key} is not supported"
    extra = params.get("additionalPermissions") or params.get("permissions")
    if isinstance(extra, dict) and extra:
        network = extra.get("network") or {}
        if isinstance(network, dict) and network.get("enabled"):
            return "additional network permission is not supported"
        fs = extra.get("fileSystem") or extra.get("filesystem") or {}
        if fs:
            return "additional filesystem permission is not supported"
        return "additionalPermissions is not supported"
    return None


def timeout_decision() -> str:
    return "deny"


def default_decisions_for_event() -> list:
    return ["allow", "deny"]


def decisions_for_approval(context: Mapping[str, Any]) -> list:
    """Advertise only one-shot decisions; denial always remains available."""
    if "availableDecisions" in context:
        menu = context["availableDecisions"]
        if not isinstance(menu, list) or not any(
                isinstance(value, str) and value in ALLOW_DECISIONS for value in menu):
            return ["deny"]
    return default_decisions_for_event()
