#!/usr/bin/env python3
"""Read a cc-cursor-bridge (9f478f8) task directory READ-ONLY and print the
pending/ and push/ notices, and replies, converted to AHP v1 shapes as JSON lines.

  python3 spec/tools/legacy_to_ahp.py <legacy-task-dir> [--producer-version 9f478f8]

Each output line: {"kind": "envelope"|"reply", "source": <relative path>, "doc": {...}}.
Nothing is written to disk. Used to seed spec/examples and the golden set.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from ahp_map import CC_TO_AHP, DECISIONS, DEFAULT_DECISION, DEFAULT_TIMEOUT_MS  # noqa: E402

CONTEXT_KEYS = ("session_id", "cwd", "transcript_path", "prompt_id", "permission_mode", "turn_id")
LEGACY_META = {"seq", "event", "session_id", "cwd", "tool_name", "tool_input", "summary", "input_keys",
               "reply_hint", "created_at", "reply_file", "truncated", "full_file"}
RENAMES = {"assistant_text": "text", "stop_hook_active": "stop_active"}


def convert_notice(body: dict, kind: str, task_id: str, version: str) -> dict:
    cc_name = body.get("event", "")
    ahp_name, default_class, _ = CC_TO_AHP.get(cc_name, ("x-cc." + cc_name.lower(), "log", ("log",)))
    cls = "gate" if kind == "pending" else "notify"
    payload = {}
    if body.get("tool_name"):
        payload["tool_name"] = body["tool_name"]
        payload["tool_input"] = body.get("tool_input") or {}
        tuid = (body.get("summary") or {}).get("tool_use_id")
        if tuid:
            payload["tool_use_id"] = tuid
    for key, value in body.items():
        if key in LEGACY_META or key in ("transcript_path",) and cc_name not in ("Stop", "SubagentStop"):
            continue
        payload[RENAMES.get(key, key)] = value
    if ahp_name == "session.start":
        payload.setdefault("source", (body.get("summary") or {}).get("source", "other"))
    full_file = body.get("full_file")
    if full_file:  # spec: full_file also lives inside each truncation object
        for value in payload.values():
            if isinstance(value, dict) and value.get("truncated") is True:
                value["full_file"] = full_file
    ts = body.get("created_at")
    env = {
        "protocol": "ahp/1",
        "task_id": task_id,
        "seq": body["seq"],
        "event": ahp_name,
        "class": cls,
        "producer": {"name": "cc-cursor-bridge", "version": version,
                      "host": {"name": "claude-code"}},
        "ts": ts,
        "payload": payload,
        "context": {k: body[k] for k in CONTEXT_KEYS if body.get(k)},
        "native": {"event": cc_name, "input_keys": body.get("input_keys", [])},
    }
    if cls == "gate":
        env["reply_file"] = body.get("reply_file") or f"replies/{body['seq']}.json"
        dt = datetime.fromisoformat(ts)
        env["deadline"] = (dt + timedelta(milliseconds=DEFAULT_TIMEOUT_MS)).isoformat(timespec="milliseconds")
        env["timeout_ms"] = DEFAULT_TIMEOUT_MS
        env["decisions"] = DECISIONS.get(ahp_name, ["ack"])
        env["default_decision"] = DEFAULT_DECISION.get(ahp_name, "ack")
    if body.get("truncated"):
        env["truncated"] = True
        env["full_file"] = full_file
    return env


def convert_reply(data: dict, seq: int) -> dict:
    """Legacy aliases (Appendix A.5) -> canonical reply."""
    out = {"protocol": "ahp/1", "seq": seq}
    decision = str(data.get("decision") or data.get("behavior") or "").lower()
    if not decision and data.get("ack") is True:
        decision = "ack"
    out["decision"] = decision
    reason = data.get("reason") or data.get("message")
    if reason:
        out["reason"] = reason
    for old, new in (("updatedInput", "updated_input"), ("additionalContext", "additional_context"),
                     ("watchPaths", "watch_paths"), ("worktreePath", "worktree_path"),
                     ("displayContent", "display_content"), ("content", "content")):
        if old in data:
            out[new] = data[old]
    return out


def main(argv):
    if not argv:
        print(__doc__, file=sys.stderr)
        return 2
    root = Path(argv[0])
    version = argv[argv.index("--producer-version") + 1] if "--producer-version" in argv else "legacy"
    task_id = root.name if root.name != "demo-bridge-snapshot" else "demo-bridge-snapshot"
    for kind in ("pending", "push"):
        folder = root / kind
        if not folder.is_dir():
            continue
        for path in sorted(folder.glob("*.json"), key=lambda p: int(p.name.split("-", 1)[0])):
            if path.name.endswith(".full.json"):
                continue
            body = json.loads(path.read_text(encoding="utf-8"))
            doc = convert_notice(body, kind, task_id, version)
            print(json.dumps({"kind": "envelope", "source": f"{kind}/{path.name}", "doc": doc}, ensure_ascii=False))
    replies = root / "replies"
    if replies.is_dir():
        for path in sorted(replies.glob("*.json"), key=lambda p: int(p.stem)):
            data = json.loads(path.read_text(encoding="utf-8"))
            doc = convert_reply(data, int(path.stem))
            print(json.dumps({"kind": "reply", "source": f"replies/{path.name}", "doc": doc}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
