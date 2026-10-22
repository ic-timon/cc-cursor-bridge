#!/usr/bin/env python3
"""Tiny stdio MCP servers for the hook harness. No sockets.

  python3 stdio_mcp.py elicit
  python3 stdio_mcp.py permit
"""

from __future__ import annotations

import json
import sys


FRAMING = "content-length"


def read_message():
    global FRAMING
    line = sys.stdin.buffer.readline()
    if not line:
        return None
    if line.lower().startswith(b"content-length:"):
        FRAMING = "content-length"
        headers = {"content-length": line.split(b":", 1)[1].strip().decode()}
        while True:
            extra = sys.stdin.buffer.readline()
            if not extra or extra in (b"\n", b"\r\n"):
                break
            key, value = extra.split(b":", 1)
            headers[key.decode().strip().lower()] = value.strip().decode()
        length = int(headers.get("content-length", "0"))
        body = sys.stdin.buffer.read(length) if length else b""
        if not body:
            return None
        return json.loads(body)
    stripped = line.strip()
    if not stripped:
        return read_message()
    if stripped.startswith(b"{"):
        FRAMING = "ndjson"
        return json.loads(stripped)
    return None


def write_message(payload: dict) -> None:
    data = json.dumps(payload, separators=(",", ":")).encode()
    if FRAMING == "ndjson":
        sys.stdout.buffer.write(data + b"\n")
    else:
        sys.stdout.buffer.write(f"Content-Length: {len(data)}\r\n\r\n".encode() + data)
    sys.stdout.buffer.flush()


def tool(name: str, description: str) -> dict:
    return {
        "name": name,
        "description": description,
        "inputSchema": {"type": "object", "properties": {}},
    }


def serve(mode: str) -> None:
    elicit_id = "elicit-1"
    while True:
        message = read_message()
        if message is None:
            return
        if message.get("method") == "notifications/initialized":
            continue
        if "id" not in message:
            continue
        request_id = message["id"]
        method = message.get("method")
        if method == "ping":
            write_message({"jsonrpc": "2.0", "id": request_id, "result": {}})
            continue
        if method == "initialize":
            version = (message.get("params") or {}).get("protocolVersion") or "2025-06-18"
            capabilities = {"tools": {}}
            if mode == "elicit":
                capabilities["elicitation"] = {}
            write_message(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "result": {
                        "protocolVersion": version,
                        "capabilities": capabilities,
                        "serverInfo": {"name": f"probe-{mode}", "version": "0.0.1"},
                    },
                }
            )
            continue
        if method == "tools/list":
            if mode == "elicit":
                tools = [tool("probe_ask", "Request one elicitation, then return.")]
            else:
                tools = [tool("approve", "Approve a Claude Code permission prompt.")]
            write_message({"jsonrpc": "2.0", "id": request_id, "result": {"tools": tools}})
            continue
        if method == "tools/call" and mode == "elicit":
            write_message(
                {
                    "jsonrpc": "2.0",
                    "id": elicit_id,
                    "method": "elicitation/create",
                    "params": {
                        "message": "Reply yes or no for the hook probe.",
                        "requestedSchema": {
                            "type": "object",
                            "properties": {
                                "answer": {"type": "string", "enum": ["yes", "no"]}
                            },
                            "required": ["answer"],
                        },
                    },
                }
            )
            while True:
                reply = read_message()
                if reply is None:
                    return
                if reply.get("id") == elicit_id:
                    break
                if "id" in reply and reply.get("method"):
                    write_message({"jsonrpc": "2.0", "id": reply["id"], "result": {}})
            write_message(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "result": {"content": [{"type": "text", "text": "elicited"}]},
                }
            )
            continue
        if method == "tools/call" and mode == "permit":
            write_message(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "result": {
                        "content": [
                            {"type": "text", "text": json.dumps({"behavior": "allow"})}
                        ]
                    },
                }
            )
            continue
        write_message({"jsonrpc": "2.0", "id": request_id, "result": {}})


if __name__ == "__main__":
    serve(sys.argv[1] if len(sys.argv) > 1 else "elicit")
