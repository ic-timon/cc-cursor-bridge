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
    # Fields the legacy notice only kept in its (240-char) summary.
    summary = body.get("summary") or {}
    for key in ("message", "mcp_server_name", "mode", "reason", "source", "error", "prompt"):
        if key in summary and key not in payload:
            payload["server" if key == "mcp_server_name" else key] = summary[key]
    if ahp_name == "message.delta" and "text" not in payload and "delta" in summary:
        payload["text"] = summary["delta"]
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
    if body.get("truncated") and full_file:
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


# ---------------------------------------------------------------------------
# Whole-directory conversion (used by conformance/producers/legacy_bridge.py)
# ---------------------------------------------------------------------------

LEGACY_LABELS = {
    "allow": ("allow", "reply"), "deny": ("deny", "reply"), "ask": ("ask", "reply"),
    "block": ("block", "reply"), "accept": ("accept", "reply"), "decline": ("decline", "reply"),
    "ack": ("ack", "reply"), "context": ("ack", "reply"), "session": ("ack", "reply"),
    "display": ("ack", "reply"), "path": ("ack", "reply"), "stop-hook-active": ("ack", "reply"),
    "timeout-deny": ("deny", "timeout"), "timeout-decline": ("decline", "timeout"),
    "timeout-allow-stop": ("ack", "timeout"), "timeout-noop": ("ack", "timeout"),
    "stop-block-cap": ("ack", "rule"),
}
SUMMARY_META = {"channel", "delivery", "wait_all", "config_note", "probe", "session_start", "assistant_text",
                "tool_use_id", "rule_errors", "wait"}


def _rename_full(obj, old, new):
    if isinstance(obj, dict):
        if obj.get("full_file") == old:
            obj["full_file"] = new
        for v in obj.values():
            _rename_full(v, old, new)
    elif isinstance(obj, list):
        for v in obj:
            _rename_full(v, old, new)


def _mark_nested(obj, full_file):
    if isinstance(obj, dict):
        if obj.get("truncated") is True and "sha256" in obj:
            obj["full_file"] = full_file
            return
        for v in obj.values():
            _mark_nested(v, full_file)


def convert_record(rec: dict, task_id: str, version: str) -> dict:
    cc_name = rec.get("event", "")
    ahp_name, _, _ = CC_TO_AHP.get(cc_name, ("x-cc." + cc_name.lower(), "log", ("log",)))
    cls = {"wait": "gate", "push": "notify"}.get(rec.get("channel"), "log")
    summary = dict(rec.get("summary") or {})
    payload = {RENAMES.get(k, k): v for k, v in summary.items() if k not in SUMMARY_META}
    if ahp_name == "message.delta" and "delta" in payload:
        payload["text"] = payload.pop("delta")
    if summary.get("tool_name"):
        tool_input = {k: payload.pop(k) for k in list(payload) if k in (
            "command", "file_path", "path", "pattern", "glob", "old_string", "new_string", "content",
            "description", "notebook_path")}
        payload["tool_input"] = tool_input
        if summary.get("tool_use_id"):
            payload["tool_use_id"] = summary["tool_use_id"]
    label = str(rec.get("decision") or "")
    if rec.get("rule_id"):
        outcome = {"decision": "deny", "via": "rule", "rule_id": rec["rule_id"]}
    elif label in LEGACY_LABELS:
        d, via = LEGACY_LABELS[label]
        outcome = {"decision": d, "via": via}
    else:
        outcome = {"via": "none"}
        if label:
            outcome["reason"] = f"legacy decision label {label!r}"
    if rec.get("decision_reason"):
        outcome["reason"] = rec["decision_reason"]
    env = {
        "protocol": "ahp/1", "task_id": task_id, "seq": rec.get("seq"), "event": ahp_name, "class": cls,
        "producer": {"name": "cc-cursor-bridge", "version": version, "host": {"name": "claude-code"}},
        "ts": rec.get("ts"), "payload": payload,
        "context": {k: rec[k] for k in ("session_id", "cwd", "prompt_id") if rec.get(k)},
        "native": {"event": cc_name, "input_keys": rec.get("input_keys", [])},
        "outcome": outcome,
    }
    return env


def convert_dir(src: Path, dst: Path, task_id: str, version: str = "legacy") -> None:
    """Write an AHP-shaped task dir from a legacy one. No capabilities.json and
    no acks/ are invented: the legacy bridge does not produce them."""
    import shutil
    dst.mkdir(parents=True, exist_ok=True)
    for kind in ("pending", "push"):
        folder = src / kind
        if not folder.is_dir():
            continue
        for path in folder.iterdir():
            if path.name.endswith(".tmp") or path.name.startswith("."):
                (dst / kind).mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, dst / kind / path.name)  # keep evidence of stray temp files
                continue
            if not path.name.endswith(".json") or path.name.endswith(".full.json"):
                continue
            body = json.loads(path.read_text(encoding="utf-8"))
            env = convert_notice(body, kind, task_id, version)
            new_name = f"{env['seq']}-{env['event']}"
            old_full = body.get("full_file")
            if old_full:
                _mark_nested(env["payload"], old_full)
                new_full = f"{kind}/{new_name}.full.json"
                _rename_full(env, old_full, new_full)
                env["full_file"] = new_full
                full_src = src / old_full
                (dst / kind).mkdir(parents=True, exist_ok=True)
                if full_src.is_file():
                    fbody = json.loads(full_src.read_text(encoding="utf-8"))
                    fenv = convert_notice(dict(fbody, full_file=None, truncated=False), kind, task_id, version)
                    fenv["ts"] = env["ts"]
                    if kind == "pending":
                        for key in ("deadline", "timeout_ms", "decisions", "default_decision", "reply_file"):
                            fenv[key] = env.get(key, fenv.get(key))
                    (dst / new_full).write_text(json.dumps(fenv, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            (dst / kind).mkdir(parents=True, exist_ok=True)
            (dst / kind / f"{new_name}.json").write_text(json.dumps(env, ensure_ascii=False, indent=2) + "\n",
                                                         encoding="utf-8")
    if (src / "replies").is_dir():
        (dst / "replies").mkdir(parents=True, exist_ok=True)
        for path in (src / "replies").iterdir():
            shutil.copy2(path, dst / "replies" / path.name)
    if (src / "events.jsonl").is_file():
        with open(dst / "events.jsonl", "w", encoding="utf-8") as out:
            for line in (src / "events.jsonl").read_text(encoding="utf-8").splitlines():
                if line.strip():
                    out.write(json.dumps(convert_record(json.loads(line), task_id, version), ensure_ascii=False) + "\n")
    if (src / "seq").is_file():
        shutil.copy2(src / "seq", dst / "seq")
