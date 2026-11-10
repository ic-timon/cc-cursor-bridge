"""Durable AHP task tree and fail-closed approval replies."""

from __future__ import annotations

import fcntl
import json
import os
import stat
import time
from pathlib import Path
from typing import Any, Optional, Tuple

from ahp_cc import (DECISIONS, DEFAULT_DECISION, PRODUCER_NAME, PROTOCOL, SPEC_VERSION,
                    ahp_root, append_line, locked_counter, normalize_reply, now_iso,
                    write_atomic)
from bridge import safe_component

from . import __version__
from .decisions import (DecisionError, classify_server_request, decisions_for_approval,
                        normalize_gate_decision)
from .redact import redact, secrets_from_environ


class Store:
    def __init__(self, task_id: str, cwd: Path, timeout_seconds: float,
                 environ: Optional[dict] = None) -> None:
        if not safe_component(task_id):
            raise ValueError("unsafe task id")
        self.environ = dict(os.environ if environ is None else environ)
        self.task_id = task_id
        self.cwd = cwd
        self.timeout_ms = max(1, int(timeout_seconds * 1000))
        self.dir = ahp_root(self.environ).resolve() / "tasks" / task_id
        self.secrets = secrets_from_environ(self.environ)
        self.producer = {"name": PRODUCER_NAME, "version": __version__,
                         "host": {"name": "codex-app-server", "version": "0.167.0"}}
        self._lock = None

    def open(self) -> None:
        for name in ("pending", "replies", "acks", "push", "state"):
            (self.dir / name).mkdir(parents=True, exist_ok=True, mode=0o700)
        self._lock = (self.dir / "state" / "controller.lock").open("a")
        try:
            fcntl.flock(self._lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.close()
            raise RuntimeError("another controller owns this task")
        try:
            self._initialize()
        except BaseException:
            self.close()
            raise

    def _initialize(self) -> None:
        locked_counter(self.dir / "seq", lambda seq: seq)
        events = {name: {"class": "notify", "allowed_classes": ["notify"]}
                  for name in ("session.start", "session.end", "tool.post", "message.delta", "producer.error")}
        events["permission.request"] = {
            "class": "gate", "allowed_classes": ["gate"],
            "decisions": list(DECISIONS["permission.request"]),
            "default_decision": DEFAULT_DECISION["permission.request"],
            "timeout_ms": self.timeout_ms,
            "native": ["item/commandExecution/requestApproval", "item/fileChange/requestApproval",
                       "execCommandApproval", "applyPatchApproval"],
        }
        write_atomic(self.dir / "capabilities.json", {
            "protocol": PROTOCOL, "spec_version": SPEC_VERSION, "task_id": self.task_id,
            "producer": self.producer, "created_at": now_iso(), "transport": "local-fs",
            "events": events, "features": {"acks": True, "updated_input": False,
                                          "additional_context": False, "consumer_rules": False},
            "x-codex-adapter": {"reconnect": False, "one_shot_approvals": True},
        })
        # Previous invocations never resume an outstanding grant.
        for path in (self.dir / "pending").glob("*.json"):
            env = json.loads(path.read_text(encoding="utf-8"))
            if not (self.dir / "acks" / f"{env['seq']}.json").exists():
                self.finish_gate(env, "deny", "error", "previous controller disconnected")

    def close(self) -> None:
        if self._lock:
            self._lock.close()
            self._lock = None

    def clean(self, obj: Any) -> Any:
        return redact(obj, self.secrets)

    def envelope(self, event: str, cls: str, payload: dict, native: Optional[dict] = None) -> dict:
        return self.clean({
            "protocol": PROTOCOL, "task_id": self.task_id,
            "seq": locked_counter(self.dir / "seq", lambda seq: seq + 1),
            "event": event, "class": cls, "producer": self.producer, "ts": now_iso(),
            "payload": payload, "context": {"cwd": str(self.cwd)}, "native": native or {},
        })

    def notice(self, event: str, payload: dict, native: Optional[dict] = None) -> None:
        env = self.envelope(event, "notify", payload, native)
        write_atomic(self.dir / "push" / f"{env['seq']}-{event}.json", env)
        append_line(self.dir / "push" / "stream.jsonl", env)
        append_line(self.dir / "events.jsonl", env)

    def status(self, state: str, **fields: Any) -> dict:
        body = self.clean({"protocol": PROTOCOL, "task_id": self.task_id,
                           "producer": self.producer, "state": state, "ts": now_iso(), **fields})
        write_atomic(self.dir / "status.json", body)
        return body

    def _stash_preexisting_reply(self, seq: int) -> Optional[Tuple[int, int, int]]:
        """A reply written before this gate was published must not approve it."""
        path = self.dir / "replies" / f"{seq}.json"
        if not path.is_file():
            return
        stamp = time.time_ns()
        dest = self.dir / "replies" / f"{seq}.preexisting-{stamp}.json"
        path.replace(dest)
        info = dest.lstat()
        return info.st_dev, info.st_ino, info.st_mtime_ns

    def open_gate(self, method: str, request_id: Any, params: dict) -> dict:
        kind = classify_server_request(method)
        context_keys = (
            "additionalPermissions", "permissions", "networkApprovalContext", "availableDecisions",
            "grantRoot", "proposedExecpolicyAmendment", "proposedNetworkPolicyAmendments",
            "proposed_execpolicy_amendment", "proposed_network_policy_amendments",
            "reason",
        )
        native = {"method": method, "request_id": request_id,
                  "thread_id": params.get("threadId", params.get("conversationId")),
                  "turn_id": params.get("turnId"),
                  "item_id": params.get("itemId", params.get("callId")),
                  "approval_id": params.get("approvalId")}
        for key in context_keys:
            if key in params:
                native[key] = params[key]
        if kind == "command":
            tool_input = {"command": params.get("command"), "cwd": params.get("cwd", str(self.cwd))}
        else:
            tool_input = {"changes": params.get("changes", params.get("fileChanges", params.get("file_changes", {})))}
            if params.get("grantRoot") is not None:
                tool_input["grantRoot"] = params.get("grantRoot")
        payload = {"tool_name": "commandExecution" if kind == "command" else "fileChange",
                   "tool_input": tool_input, "approval_context": {k: params[k] for k in context_keys if k in params}}
        if params.get("reason") is not None:
            payload["reason"] = params["reason"]
        env = self.envelope("permission.request", "gate", payload, native)
        stale_identity = self._stash_preexisting_reply(env["seq"])
        env["stale_reply_identities"] = [stale_identity] if stale_identity else []
        env.update(reply_file=f"replies/{env['seq']}.json", deadline=now_iso(self.timeout_ms),
                   timeout_ms=self.timeout_ms,
                   decisions=decisions_for_approval(env["payload"]["approval_context"]),
                   default_decision=DEFAULT_DECISION["permission.request"])
        append_line(self.dir / "state" / "codex-requests.jsonl", {"seq": env["seq"], **{k: env["native"].get(k)
                    for k in ("method", "request_id", "thread_id", "turn_id", "item_id", "approval_id")}})
        pending_path = self.dir / "pending" / f"{env['seq']}-permission.request.json"
        write_atomic(pending_path, env)
        # Use the filesystem clock for both sides of the freshness comparison.
        # Wall time can lead coarse filesystem mtimes and reject fresh replies.
        env["published_mtime_ns"] = pending_path.stat().st_mtime_ns
        stale_identity = self._stash_preexisting_reply(env["seq"])
        if stale_identity:
            env["stale_reply_identities"].append(stale_identity)
        append_line(self.dir / "events.jsonl", env)
        return env

    def poll_reply(self, env: dict) -> Optional[Tuple[str, str, str]]:
        path = self.dir / env["reply_file"]
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0))
        except FileNotFoundError:
            return None
        except OSError as exc:
            return "deny", "invalid_reply", str(exc)
        try:
            with os.fdopen(fd, "rb") as handle:
                info = os.fstat(handle.fileno())
                if not stat.S_ISREG(info.st_mode):
                    raise ValueError("reply must be a regular file")
                if (info.st_mtime_ns < env["published_mtime_ns"]
                        or (info.st_dev, info.st_ino, info.st_mtime_ns) in env["stale_reply_identities"]):
                    # Keep restored stale files stashed, including on filesystems
                    # whose timestamp resolution cannot distinguish two writes.
                    identity = self._stash_preexisting_reply(env["seq"])
                    if identity and identity not in env["stale_reply_identities"]:
                        env["stale_reply_identities"].append(identity)
                    return None
                raw = handle.read(1024 * 1024 + 1)
            if len(raw) > 1024 * 1024:
                raise ValueError("reply too large")
            data = json.loads(raw)
            # Check grants before normalize_reply can discard unsupported fields.
            decision = normalize_gate_decision(data)
            canonical = dict(data, decision=decision)
            normalized, errors = normalize_reply(canonical, env["seq"], env["event"], env["decisions"])
            if errors:
                raise ValueError("; ".join(errors))
            if any(key in normalized for key in ("updated_input", "additional_context")):
                raise DecisionError("input amendments and injected context are not supported")
            return normalize_gate_decision(normalized), "reply", normalized.get("reason", "")
        except (OSError, ValueError, TypeError) as exc:
            return "deny", "invalid_reply", str(exc)

    def finish_gate(self, env: dict, decision: str, via: str, reason: str = "") -> None:
        status = "consumed" if via == "reply" else "invalid" if via == "invalid_reply" else "expired"
        ack = self.clean({"protocol": PROTOCOL, "task_id": self.task_id, "seq": env["seq"],
                          "ts": now_iso(), "status": status, "via": via,
                          "applied_decision": decision, "reason": reason})
        write_atomic(self.dir / "acks" / f"{env['seq']}.json", ack)
        append_line(self.dir / "events.jsonl", dict(env, outcome=self.clean({
            "decision": decision, "via": via, "reason": reason})))
