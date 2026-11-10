#!/usr/bin/env python3
"""Deterministic protocol fixture. It records simulated execution, never runs tools.

FAKE_CODEX_SCENARIO is a JSON file; FAKE_CODEX_RECORD is its JSONL audit path.
The executable accepts the same trailing arguments as `codex app-server`.
"""

from __future__ import annotations

import json
import os
import signal
import sys
import time
from pathlib import Path


class FakeServer:
    def __init__(self) -> None:
        scenario = os.environ.get("FAKE_CODEX_SCENARIO")
        self.scenario = json.loads(Path(scenario).read_text()) if scenario else {}
        self.record_path = Path(os.environ["FAKE_CODEX_RECORD"])
        self.requests = list(self.scenario.get("requests", []))
        self.waiting = None
        self.index = 0
        self.executed = False
        self.finished = False

    def record(self, event: str, **fields: object) -> None:
        with self.record_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"event": event, **fields}) + "\n")
            handle.flush()

    def send(self, message: dict) -> None:
        if self.scenario.get("extra_fields"):
            message["futureField"] = {"nested": True}
        self.record("send", message=message)
        raw = json.dumps(message).encode("utf-8")
        if self.scenario.get("content_length"):
            raw = f"Content-Length: {len(raw)}\r\nContent-Type: application/json\r\n\r\n".encode() + raw
        else:
            raw += b"\n"
        fragment = self.scenario.get("fragment_bytes", len(raw))
        for start in range(0, len(raw), fragment):
            sys.stdout.buffer.write(raw[start:start + fragment])
            sys.stdout.buffer.flush()

    def notify(self, method: str, params: dict) -> None:
        self.send({"method": method, "params": params})

    def complete(self, status: str = "completed") -> None:
        if self.finished:
            return
        self.finished = True
        usage = {"total": {"inputTokens": 12, "outputTokens": 4, "totalTokens": 16,
                           "cachedInputTokens": 3, "reasoningOutputTokens": 2},
                 "last": {"inputTokens": 12, "totalTokens": 16}}
        self.notify("thread/tokenUsage/updated", {"threadId": "thread-1", "turnId": "turn-1",
                                                   "tokenUsage": usage, "accessToken": "planted-secret"})
        self.notify("item/completed", {"threadId": "thread-1", "turnId": "turn-1",
                                       "item": {"id": "answer-1", "type": "agentMessage",
                                                "text": "done " + self.scenario.get("secret", "")}})
        turn = {"id": "turn-1", "status": status}
        if "turn_error" in self.scenario:
            turn["error"] = self.scenario["turn_error"]
        self.notify("turn/completed", {"threadId": "thread-1", "turn": turn})

    def next_request(self) -> None:
        if self.index >= len(self.requests):
            if not self.scenario.get("hang_turn"):
                self.complete(self.scenario.get("turn_status", "completed"))
            return
        spec = self.requests[self.index]
        self.index += 1
        method = spec.get("method", "item/commandExecution/requestApproval")
        item_id = f"item-{self.index}"
        params = {"threadId": "thread-1", "turnId": "turn-1", "itemId": item_id,
                  "approvalId": f"approval-{self.index}", "startedAtMs": 1,
                  "command": "echo simulated", "cwd": str(Path.cwd()), "reason": "fixture approval"}
        if "fileChange" in method or method == "applyPatchApproval":
            changes = [{"path": str(Path.cwd() / "simulated.txt"), "kind": {"type": "add"}, "diff": "+simulated"}]
            self.notify("item/started", {"threadId": "thread-1", "turnId": "turn-1",
                                         "item": {"id": item_id, "type": "fileChange", "changes": changes},
                                         **spec.get("item_context", {})})
            params.pop("command")
        if method in ("execCommandApproval", "applyPatchApproval"):
            params.update(conversationId=params.pop("threadId"), callId=params.pop("itemId"))
            params.pop("turnId")
            if method == "execCommandApproval":
                params.update(command=["echo", "simulated"], parsedCmd=[])
            else:
                params["fileChanges"] = {"simulated.txt": {"type": "add", "content": "simulated"}}
        params.update(spec.get("params", {}))
        if self.scenario.get("extra_fields"):
            params["futureApprovalField"] = [1, 2, 3]
        self.waiting = f"server-{self.index}"
        self.send({"id": self.waiting, "method": method, "params": params})
        if self.scenario.get("disconnect"):
            raise EOFError("scripted disconnect")

    def handle(self, message: dict) -> None:
        self.record("receive", message=message)
        method = message.get("method")
        request_id = message.get("id")
        if method == "initialize":
            for chunk in self.scenario.get("stderr_chunks", []):
                sys.stderr.write(chunk)
                sys.stderr.flush()
            self.send({"id": request_id, "result": {"userAgent": "fake-codex/0.167.0"}})
        elif method == "initialized":
            pass
        elif method == "thread/start":
            self.send({"id": request_id, "result": {"thread": {"id": "thread-1"}}})
        elif method == "turn/start":
            if not self.scenario.get("omit_turn_started"):
                self.notify("turn/started", {"threadId": "thread-1", "turn": {"id": "turn-1", "status": "inProgress"}})
            if not self.scenario.get("approval_before_start_response"):
                self.send({"id": request_id, "result": {"turn": {"id": "turn-1", "status": "inProgress"}}})
            for notification in self.scenario.get("notifications", []):
                self.send(notification)
            self.next_request()
            if self.scenario.get("approval_before_start_response"):
                if self.scenario.get("wait_for_reply_before_start_response"):
                    # Hold the authoritative identity until the consumer has
                    # written its reply. This fixture never executes commands.
                    deadline = time.monotonic() + 2
                    root = Path(os.environ["CC_BRIDGE_AHP_DIR"])
                    while not list(root.glob("tasks/*/replies/*.json")):
                        if time.monotonic() >= deadline:
                            raise TimeoutError("consumer did not write early reply")
                        time.sleep(0.01)
                    self.record("early_reply_before_start_response")
                self.send({"id": request_id, "result": {"turn": {"id": "turn-1", "status": "inProgress"}}})
        elif method == "turn/interrupt":
            self.waiting = None
            self.send({"id": request_id, "result": {}})
            self.complete("interrupted")
        elif request_id == self.waiting and method is None:
            decision = (message.get("result") or {}).get("decision")
            accepted = decision == "accept" or decision == "approved"
            self.executed = self.executed or accepted
            self.record("approval", accepted=accepted, executed=self.executed, response=message)
            self.waiting = None
            if "error" not in message:
                self.next_request()

    def run(self) -> None:
        self.record("started", pid=os.getpid())
        if self.scenario.get("ignore_termination"):
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
        try:
            for line in sys.stdin.buffer:
                self.handle(json.loads(line))
            if self.scenario.get("ignore_termination"):
                while True:
                    time.sleep(1)
        except (EOFError, BrokenPipeError):
            pass
        finally:
            self.record("exit", executed=self.executed)


if __name__ == "__main__":
    FakeServer().run()
