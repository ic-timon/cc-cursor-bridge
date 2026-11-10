"""Bounded stdio JSON-RPC transport for the Codex app-server."""

from __future__ import annotations

import json
import os
import select
import selectors
import signal
import subprocess
import time
from pathlib import Path
from typing import Any, Optional, Sequence

from ahp_cc import append_line, now_iso

from .redact import redact


class RPCError(RuntimeError):
    pass


class RPC:
    MAX_FRAME = 16 * 1024 * 1024

    def __init__(self, command: Sequence[str], cwd: Path, *,
                 transcript: Optional[Path] = None, secrets: Sequence[str] = (),
                 environ: Optional[dict] = None) -> None:
        self.transcript = transcript
        self.secrets = secrets
        self.process = subprocess.Popen(
            list(command), cwd=str(cwd), env=environ, stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True,
            bufsize=0,
        )
        self.selector = selectors.DefaultSelector()
        for stream, name in ((self.process.stdout, "stdout"), (self.process.stderr, "stderr")):
            os.set_blocking(stream.fileno(), False)
            self.selector.register(stream, selectors.EVENT_READ, name)
        os.set_blocking(self.process.stdin.fileno(), False)
        self.buffer = bytearray()
        self.stderr_buffer = bytearray()
        self.eof = False
        self.next_id = 0

    def log(self, direction: str, message: Any) -> None:
        if self.transcript:
            append_line(self.transcript, redact(
                {"ts": now_iso(), "direction": direction, "message": message}, self.secrets))

    def _send(self, message: dict) -> None:
        raw = (json.dumps(message, ensure_ascii=False) + "\n").encode("utf-8")
        if len(raw) > self.MAX_FRAME:
            raise RPCError("outgoing RPC frame too large")
        deadline = time.monotonic() + 5
        stream = self.process.stdin
        view = memoryview(raw)
        while view:
            if time.monotonic() >= deadline:
                raise TimeoutError("RPC write timed out")
            if not select.select([], [stream], [], min(0.1, deadline - time.monotonic()))[1]:
                continue
            try:
                written = os.write(stream.fileno(), view)
            except BlockingIOError:
                continue
            view = view[written:]
        self.log("send", message)

    def request(self, method: str, params: dict) -> int:
        self.next_id += 1
        self._send({"jsonrpc": "2.0", "id": self.next_id, "method": method, "params": params})
        return self.next_id

    def notify(self, method: str, params: Optional[dict] = None) -> None:
        message = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            message["params"] = params
        self._send(message)

    def reply_result(self, request_id: Any, result: dict) -> None:
        self._send({"jsonrpc": "2.0", "id": request_id, "result": result})

    def reply_error(self, request_id: Any, error: dict) -> None:
        self._send({"jsonrpc": "2.0", "id": request_id, "error": error})

    def _frame(self) -> Optional[dict]:
        while self.buffer[:1] in (b"\r", b"\n"):
            del self.buffer[:1]
        if not self.buffer:
            return None
        if self.buffer[:15].lower().startswith(b"content-length:"):
            marker = b"\r\n\r\n" if b"\r\n\r\n" in self.buffer else b"\n\n"
            end = self.buffer.find(marker)
            if end < 0:
                return None
            headers = bytes(self.buffer[:end]).decode("ascii")
            try:
                length = int(headers.splitlines()[0].split(":", 1)[1].strip())
            except (ValueError, IndexError) as exc:
                raise RPCError("invalid Content-Length") from exc
            if not 0 < length <= self.MAX_FRAME:
                raise RPCError("invalid RPC frame size")
            start = end + len(marker)
            if len(self.buffer) < start + length:
                return None
            raw = bytes(self.buffer[start:start + length])
            del self.buffer[:start + length]
        else:
            end = self.buffer.find(b"\n")
            if end < 0:
                return None
            raw = bytes(self.buffer[:end])
            del self.buffer[:end + 1]
        try:
            message = json.loads(raw)
        except (ValueError, UnicodeError) as exc:
            raise RPCError("invalid JSON RPC frame") from exc
        if not isinstance(message, dict) or message.get("jsonrpc", "2.0") != "2.0":
            raise RPCError("invalid JSON-RPC message")
        self.log("receive", message)
        return message

    def read(self, timeout: float) -> dict:
        deadline = time.monotonic() + max(0, timeout)
        while True:
            message = self._frame()
            if message is not None:
                return message
            if self.eof:
                raise EOFError("app-server stdout closed")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("RPC read timed out")
            for key, _ in self.selector.select(remaining):
                chunk = os.read(key.fd, 65536)
                if not chunk:
                    self.selector.unregister(key.fileobj)
                    if key.data == "stdout":
                        self.eof = True
                    elif self.stderr_buffer:
                        self.log("stderr", self.stderr_buffer.decode("utf-8", errors="replace"))
                        self.stderr_buffer.clear()
                elif key.data == "stdout":
                    self.buffer.extend(chunk)
                    if len(self.buffer) > self.MAX_FRAME:
                        raise RPCError("incoming RPC frame too large")
                else:
                    self.stderr_buffer.extend(chunk)
                    if len(self.stderr_buffer) > self.MAX_FRAME:
                        raise RPCError("stderr line too large")
                    while b"\n" in self.stderr_buffer:
                        end = self.stderr_buffer.index(b"\n") + 1
                        self.log("stderr", self.stderr_buffer[:end].decode("utf-8", errors="replace"))
                        del self.stderr_buffer[:end]

    def close(self) -> None:
        if not self.process.stdin.closed:
            self.process.stdin.close()
        for sig in (None, signal.SIGTERM, signal.SIGKILL):
            if sig is not None:
                try:
                    os.killpg(self.process.pid, sig)
                except ProcessLookupError:
                    pass
            try:
                self.process.wait(timeout=0.3)
            except subprocess.TimeoutExpired:
                continue
            # The group can outlive its leader.
            if sig == signal.SIGKILL:
                break
        self.process.wait(timeout=1)
        self.selector.close()
        self.process.stdout.close()
        self.process.stderr.close()
