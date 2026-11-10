"""One supervised Codex turn; no reconnect or persistent permission grants."""

from __future__ import annotations

import math
import signal
import threading
import time
from pathlib import Path
from typing import Any, Optional

from ahp_cc import now_iso, write_atomic

from . import __version__
from .decisions import (approval_result, classify_server_request, jsonrpc_error_payload,
                        permissions_rejection, unsupported_approval_scope)
from .rpc import RPC, RPCError
from .store import Store


class Cancelled(RuntimeError):
    pass


class Session:
    def __init__(self, *, task_id: str, cwd: Path, model: str, effort: str,
                 timeout_seconds: float = 120, turn_timeout_seconds: float = 3600,
                 codex_bin: str = "codex", environ: Optional[dict] = None,
                 jsonl: Optional[Path] = None) -> None:
        if not model.strip() or not effort.strip():
            raise ValueError("model and effort are required")
        if any(not math.isfinite(value) or value <= 0
               for value in (timeout_seconds, turn_timeout_seconds)):
            raise ValueError("timeouts must be finite positive seconds")
        self.cwd = Path(cwd).resolve(strict=True)
        if not self.cwd.is_dir():
            raise ValueError("cwd must be a directory")
        self.store = Store(task_id, self.cwd, timeout_seconds, environ)
        self.model, self.effort = model, effort
        self.timeout_seconds, self.turn_timeout_seconds = timeout_seconds, turn_timeout_seconds
        self.codex_bin, self.jsonl = codex_bin, jsonl
        self.rpc: Optional[RPC] = None
        self.thread_id: Optional[str] = None
        self.turn_id: Optional[str] = None
        self.turn_status: Optional[str] = None
        self.turn_error: Any = ""
        self.usage: dict = {}
        self.pending: dict = {}
        self.items: dict = {}
        self.last_gate: Optional[dict] = None
        self.cancelled = False
        self.fail_closed = False
        self.deadline: Optional[float] = None
        self._ran = False

    def cancel(self) -> None:
        self.cancelled = True

    def _status(self, state: str, **fields: Any) -> dict:
        return self.store.status(state, thread_id=self.thread_id, turn_id=self.turn_id,
                                 turn_status=self.turn_status, usage=self.usage,
                                 fail_closed=self.fail_closed, last_gate=self.last_gate, **fields)

    def _interrupt(self) -> None:
        if self.rpc and self.thread_id and self.turn_id:
            self.rpc.request("turn/interrupt", {"threadId": self.thread_id, "turnId": self.turn_id})

    def _resolve(self, key: Any, decision: str, via: str, reason: str) -> None:
        if self.cancelled:
            raise Cancelled("controller cancelled")
        pending = self.pending[key]
        env = pending["env"]
        if decision == "allow" and (
                not self.turn_id or env["native"]["turn_id"] != self.turn_id
                or env["native"]["thread_id"] != self.thread_id):
            decision, via, reason = "deny", "error", "approval does not match active turn"
        self.store.finish_gate(env, decision, via, reason)
        if self.cancelled:
            raise Cancelled("controller cancelled")
        self.rpc.reply_result(pending["id"], approval_result(pending["method"], decision, reason))
        del self.pending[key]
        if via != "reply":
            self.fail_closed = True
        self.last_gate = {"seq": env["seq"], "decision": decision, "via": via, "reason": reason}
        self._status("waiting" if self.pending else "running")

    def _tick(self) -> None:
        if self.cancelled:
            raise Cancelled("controller cancelled")
        if self.deadline is not None and time.monotonic() >= self.deadline:
            raise TimeoutError("whole turn timed out")
        for key, pending in list(self.pending.items()):
            # Expired replies cannot revive a timed-out approval.
            if time.monotonic() >= pending["deadline"]:
                self._resolve(key, "deny", "timeout", "approval reply timed out")
            elif self.turn_id is None:
                # An early request is not an authoritative turn identity. Wait
                # for turn/start or turn/started before considering its reply.
                continue
            elif pending["env"]["native"]["turn_id"] != self.turn_id:
                self._resolve(key, "deny", "error", "approval turn id mismatch")
            else:
                reply = self.store.poll_reply(pending["env"])
                if reply is not None:
                    self._resolve(key, *reply)

    def _server_request(self, message: dict) -> None:
        method, request_id = message["method"], message["id"]
        kind = classify_server_request(method)
        if kind not in ("command", "fileChange"):
            error = (permissions_rejection()[1] if kind == "permissions" else
                     jsonrpc_error_payload(f"unsupported server request: {method}"))
            self.rpc.reply_error(request_id, error)
            raise RPCError(error["message"])
        params = message.get("params")
        if not isinstance(params, dict):
            self.rpc.reply_error(request_id, jsonrpc_error_payload("approval params must be an object"))
            raise RPCError("invalid approval params")
        thread_id = params.get("threadId", params.get("conversationId"))
        turn_id = params.get("turnId", self.turn_id)
        if not self.thread_id or thread_id != self.thread_id or not turn_id:
            self.rpc.reply_error(request_id, jsonrpc_error_payload("approval does not match active turn"))
            raise RPCError("approval does not match active turn")
        if self.turn_id is not None and turn_id != self.turn_id:
            self.rpc.reply_error(request_id, jsonrpc_error_payload("approval turn id mismatch"))
            raise RPCError("approval turn id mismatch")
        key = (type(request_id), request_id)
        if key in self.pending:
            raise RPCError("duplicate approval request id")
        params = dict(params, turnId=turn_id)
        context_error = None
        if kind == "fileChange" and not any(
                params.get(name) is not None for name in ("changes", "fileChanges", "file_changes")):
            item_id = params.get("itemId", params.get("callId"))
            item = self.items.get((thread_id, turn_id, item_id), {})
            if item.get("type") == "fileChange" and item.get("changes") is not None:
                params["changes"] = item["changes"]
            else:
                context_error = "fileChange operation context cannot be correlated to active thread/turn/item"
        env = self.store.open_gate(method, request_id, params)
        scope = unsupported_approval_scope(env["payload"]["approval_context"]) or context_error
        if not scope and "allow" not in env["decisions"]:
            scope = "availableDecisions does not offer a one-shot accept/allow"
        if scope:
            self.store.finish_gate(env, "deny", "error", scope)
            self.rpc.reply_result(request_id, approval_result(method, "deny", scope))
            self.fail_closed = True
            self.last_gate = {"seq": env["seq"], "decision": "deny", "via": "error", "reason": scope}
            self._status("running", pending_seq=env["seq"])
            return
        self.pending[key] = {"id": request_id, "method": method, "env": env,
                             "deadline": time.monotonic() + self.timeout_seconds}
        self._status("waiting", pending_seq=env["seq"])

    def _handle(self, message: dict) -> None:
        method = message.get("method")
        if "id" in message and method is not None:
            if not isinstance(method, str) or type(message["id"]) not in (int, str):
                self.rpc.reply_error(message.get("id"), jsonrpc_error_payload("invalid server request"))
                raise RPCError("invalid server request")
            self._server_request(message)
            return
        if method is None:
            return
        params = message.get("params") or {}
        if not isinstance(params, dict):
            raise RPCError("notification params must be an object")
        if params.get("threadId", self.thread_id) != self.thread_id:
            return
        if method == "turn/started":
            turn = params.get("turn") or {}
            if self.turn_id and turn["id"] != self.turn_id:
                raise RPCError("turn/started id mismatch")
            self.turn_id = turn["id"]
        elif method == "turn/completed":
            turn = params["turn"]
            if self.turn_id and turn.get("id") != self.turn_id:
                raise RPCError("completion turn id mismatch")
            status = turn.get("status")
            if status not in ("completed", "interrupted", "failed"):
                raise RPCError("unknown turn completion status")
            if self.pending:
                raise RPCError("turn completed with outstanding approvals")
            if self.turn_id is None:
                raise RPCError("turn completed before active turn was established")
            self.turn_status = status
            if isinstance(turn.get("usage"), dict):
                self.usage = turn["usage"]
            if turn.get("error") is not None:
                self.turn_error = turn["error"]
                self.store.notice("producer.error", {"stage": "turn", "message": str(turn["error"])})
        elif method == "thread/tokenUsage/updated":
            if params.get("turnId", self.turn_id) == self.turn_id:
                self.usage = params.get("tokenUsage") or {}
        elif method in ("item/started", "item/completed"):
            thread_id, turn_id = params.get("threadId"), params.get("turnId")
            if not thread_id or not turn_id or (self.turn_id and turn_id != self.turn_id):
                return
            item = params.get("item") or {}
            self.items[(thread_id, turn_id, item["id"])] = item
            if method == "item/completed":
                if item.get("type") == "agentMessage":
                    self.store.notice("message.delta", {"message_id": item["id"], "kind": "text",
                                                        "text": item.get("text", ""), "final": True})
                elif item.get("type") in ("commandExecution", "fileChange"):
                    self.store.notice("tool.post", {"tool_name": item["type"], "tool_input": item,
                                                    "tool_use_id": item["id"]})

    def _receive(self) -> Optional[dict]:
        self._tick()
        try:
            message = self.rpc.read(0.05)
        except TimeoutError:
            return None
        self._handle(message)
        return message

    def _request(self, method: str, params: dict) -> dict:
        request_id = self.rpc.request(method, params)
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            message = self._receive()
            if message is not None and "method" not in message and message.get("id") == request_id:
                if "error" in message:
                    raise RPCError(f"{method} failed: {message['error']}")
                result = message.get("result")
                if not isinstance(result, dict):
                    raise RPCError(f"{method} returned an invalid result")
                return result
        raise TimeoutError(f"{method} timed out")

    def _abort(self, reason: str) -> None:
        for key, pending in list(self.pending.items()):
            self.store.finish_gate(pending["env"], "deny", "error", reason)
            try:
                self.rpc.reply_result(pending["id"], approval_result(pending["method"], "deny", reason))
            except (OSError, ValueError, RPCError, TimeoutError):
                pass
            del self.pending[key]
        try:
            self._interrupt()
        except (OSError, ValueError, RPCError, TimeoutError):
            pass

    def run(self, prompt: str) -> dict:
        if self._ran:
            raise RuntimeError("a Session runs only one turn")
        if not prompt.strip():
            raise ValueError("prompt is empty")
        self._ran = True
        self.store.open()
        handlers = {}
        final_state, error = "failed", ""
        try:
            if threading.current_thread() is threading.main_thread():
                for sig in (signal.SIGINT, signal.SIGTERM):
                    handlers[sig] = signal.getsignal(sig)
                    signal.signal(sig, lambda signum, frame: self.cancel())
            self._status("starting")
            self.rpc = RPC([self.codex_bin, "app-server", "--listen", "stdio://"], self.cwd,
                           transcript=self.jsonl, secrets=self.store.secrets, environ=self.store.environ)
            self._request("initialize", {"clientInfo": {"name": "cc-cursor-bridge-codex",
                                                        "version": __version__}})
            self.rpc.notify("initialized")
            thread = self._request("thread/start", {
                "model": self.model, "cwd": str(self.cwd), "approvalPolicy": "on-request",
                "approvalsReviewer": "user", "sandbox": "workspace-write", "ephemeral": True,
            })
            self.thread_id = thread["thread"]["id"]
            write_atomic(self.store.dir / "state" / "codex-session.json", {"thread_id": self.thread_id})
            self.store.notice("session.start", {"source": "startup"}, {"thread_id": self.thread_id})
            self.deadline = time.monotonic() + self.turn_timeout_seconds
            turn = self._request("turn/start", {
                "threadId": self.thread_id, "input": [{"type": "text", "text": prompt}],
                "model": self.model, "effort": self.effort, "cwd": str(self.cwd),
                "approvalPolicy": "on-request", "approvalsReviewer": "user",
                "sandboxPolicy": {"type": "workspaceWrite", "networkAccess": False,
                                  "writableRoots": [str(self.cwd)],
                                  "excludeSlashTmp": True, "excludeTmpdirEnvVar": True},
            })
            turn_id = turn["turn"]["id"]
            if self.turn_id and self.turn_id != turn_id:
                raise RPCError("turn/start id mismatch")
            self.turn_id = turn_id
            self._status("waiting" if self.pending else "running")
            while self.turn_status is None:
                self._receive()
            if self.cancelled:
                raise Cancelled("controller cancelled")
            final_state = "ended"
            end_reason = self.turn_status
            error = self.turn_error
        except (Exception, KeyboardInterrupt) as exc:
            self.fail_closed = True
            outcome = "cancelled" if isinstance(exc, (Cancelled, KeyboardInterrupt)) else "failed"
            error = str(exc) or type(exc).__name__
            self._abort(error)
            self.store.notice("producer.error", {"stage": "session", "message": error})
            final_state = "ended"
            end_reason = outcome
        finally:
            try:
                if self.rpc:
                    self.rpc.close()
            finally:
                try:
                    end_reason = locals().get("end_reason") or (
                        "completed" if self.turn_status == "completed" and not self.fail_closed else
                        (self.turn_status or "failed")
                    )
                    success = (end_reason == "completed" and self.turn_status == "completed"
                               and not self.fail_closed)
                    self.store.notice("session.end", {"reason": str(error) if error else end_reason,
                                                      "turn_status": self.turn_status,
                                                      "session_end_reason": end_reason,
                                                      "tokenUsage": self.usage})
                    result = self._status("ended", success=success, error=error, ended_at=now_iso(),
                                          session_end_reason=end_reason)
                finally:
                    self.store.close()
                    for sig, handler in handlers.items():
                        signal.signal(sig, handler)
        return result
