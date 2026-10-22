#!/usr/bin/env python3
"""Reference AHP v1 producer that replays a scenario's host events.

Adapter contract (see conformance/README.md):
  replay.py --describe
  replay.py --scenario FILE --root DIR --task ID
It writes <root>/tasks/<task>/ per spec and <root>/harness/effects.jsonl.

This is the reference implementation of the producer side of the spec: gates
block on replies with a deadline, every failure is fail-closed, acks are
written once, static rules deny before any pending, fields are redacted and
truncated. Rule matching reuses scripts/bridge.py's matcher as the reference
rule language when it is importable (pure functions only).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from conformance.common import (  # noqa: E402
    DECISIONS, DEFAULT_DECISION, append_line, allowed_classes, default_class, now_iso,
    read_json, reserve_seq, walk_truncations, write_atomic, CC_TO_AHP, EXTRA_EVENTS, SPEC_VERSION,
)

NAME, VERSION = "ahp-replay", "0.1.0"
# Self-test only: deliberately break the reference producer to prove checks bite.
MUTATE = set(filter(None, os.environ.get("AHP_REPLAY_MUTATE", "").split(",")))
SECRET_ENV = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CC_BRIDGE_CURSOR_API_KEY")
SECRET_PATTERNS = [re.compile(p) for p in (
    r"sk-[A-Za-z0-9_-]{16,}", r"ghp_[A-Za-z0-9]{20,}", r"github_pat_[A-Za-z0-9_]{20,}",
    r"xox[abposr]-[A-Za-z0-9-]{10,}", r"AKIA[0-9A-Z]{16}",
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----")]
REPLY_KEYS = {"protocol", "seq", "decision", "reason", "updated_input", "additional_context", "content",
              "watch_paths", "worktree_path", "display_content", "consumer", "ts"}
COMMAND_KEYS = {"command"}
CONTENT_KEYS = {"content", "old_string", "new_string", "last_assistant_message", "prompt", "message"}
SHORT_KEYS = {"tool_response", "error", "text"}
# spec 7.2: events whose gate reply may carry additional_context
CONTEXT_EVENTS = {"tool.pre", "session.start", "prompt.submit", "prompt.expand", "task.create", "task.complete",
                  "teammate.idle", "compact.pre", "tool.post"}

try:  # reference rule language
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
    import bridge as _bridge  # type: ignore
except Exception:  # noqa: BLE001
    _bridge = None


class Producer:
    def __init__(self, scenario: dict, root: Path, task: str) -> None:
        self.sc = scenario
        self.cfg = scenario.get("producer") or {}
        self.root = root
        self.task = task
        self.dir = root / "tasks" / task
        self.effects = root / "harness" / "effects.jsonl"
        self.limits = {"max_field_chars": 240, "content_chars": 2000, "command_chars": 4000,
                       "max_blocks": 3, "host_hook_timeout_ms": 180000, "max_gate_timeout_ms": 170000,
                       "max_additional_context_chars": 10000}
        self.limits.update(self.cfg.get("limits") or {})
        if self.limits.get("host_hook_timeout_ms") in (None, 0):
            self.limits.pop("host_hook_timeout_ms", None)
        if "max_blocks" in self.cfg:
            self.limits["max_blocks"] = self.cfg["max_blocks"]
        self.timeout_ms = int(self.cfg.get("timeout_ms", 120000))
        self.secrets = [os.environ[k] for k in SECRET_ENV if len(os.environ.get(k, "")) >= 8]
        self.rules = self.cfg["rules"] if self.cfg.get("rules") is not None else (
            _bridge.default_rules() if _bridge else [])
        self.notes: List[str] = []
        self.events: Dict[str, dict] = {}
        self.consumer_state = "absent"
        self.features = {"acks": True, "full_files": True, "updated_input": True, "additional_context": True,
                         "message_delta": True, "policy_deny_event": True, "consumer_rules": True}

    # ---------- setup ----------
    def negotiate(self) -> None:
        names = [a for (a, _, _) in CC_TO_AHP.values()] + list(EXTRA_EVENTS)
        timeouts = {}
        classes = {n: default_class(n) for n in names}
        cj_path = self.dir / "consumer.json"
        if cj_path.is_file():
            try:
                cj = read_json(cj_path)
                if not isinstance(cj, dict) or cj.get("protocol") != "ahp/1":
                    raise ValueError("protocol mismatch")
                partial = False
                for ev, cls in (cj.get("event_classes") or {}).items():
                    if ev in classes and cls in allowed_classes(ev):
                        classes[ev] = cls
                    else:
                        partial = True
                        self.notes.append(f"event_classes.{ev}={cls} not allowed")
                host = self.limits.get("host_hook_timeout_ms")
                hi = host - 10000 if host else self.limits["max_gate_timeout_ms"]
                for ev, ms in (cj.get("timeouts_ms") or {}).items():
                    timeouts[ev] = max(1000, min(int(ms), hi))
                    if timeouts[ev] != int(ms):
                        partial = True
                        self.notes.append(f"timeouts_ms.{ev}={ms} clamped to {timeouts[ev]}")
                for rule in cj.get("extra_rules") or []:
                    if isinstance(rule, dict):
                        self.rules = list(self.rules) + [dict(rule)]
                self.consumer_state = "partially_applied" if partial else "applied"
            except Exception as exc:  # noqa: BLE001
                self.consumer_state = "rejected"
                self.notes.append(f"consumer.json rejected: {exc}")
        for n in names:
            e: Dict[str, Any] = {"class": classes[n], "allowed_classes": list(allowed_classes(n))}
            if classes[n] == "gate" or "gate" in allowed_classes(n):
                e["decisions"] = DECISIONS.get(n, ["ack"])
                e["default_decision"] = DEFAULT_DECISION.get(n, "ack")
                e["timeout_ms"] = timeouts.get(n, self.timeout_ms)
            self.events[n] = e

    def write_capabilities(self) -> None:
        rules_blob = json.dumps(self.rules, sort_keys=True, ensure_ascii=False).encode()
        caps = {
            "protocol": "ahp/1", "spec_version": "1.0-beta" if "bad_spec_version" in MUTATE else SPEC_VERSION, "task_id": self.task,
            "producer": {"name": NAME, "version": VERSION, "host": {"name": "scenario-replay"}},
            "created_at": now_iso(), "transport": "local-fs", "consumer_json": self.consumer_state,
            "events": self.events, "features": self.features,
            "limits": {k: int(v) for k, v in self.limits.items()},
            "rules": {"ids": [str(r.get("id")) for r in self.rules if isinstance(r, dict)],
                      "status": self.rule_status(),
                      "required": [str(r.get("id")) for r in self.rules if isinstance(r, dict) and r.get("required")],
                      "sha256": hashlib.sha256(rules_blob).hexdigest(), "source": "scenario"},
            "redaction": {"methods": ["env-values", "patterns"], "token": "[redacted]"},
        }
        if self.notes:
            caps["consumer_notes"] = self.notes
        self.dir.mkdir(parents=True, exist_ok=True)
        os.chmod(self.dir, 0o700)
        write_atomic(self.dir / "capabilities.json", caps)

    # ---------- data shaping ----------
    def redact(self, obj: Any) -> Any:
        if "no_redact" in MUTATE:
            return obj
        if isinstance(obj, str):
            for s in self.secrets:
                obj = obj.replace(s, "[redacted]")
            for pat in SECRET_PATTERNS:
                obj = pat.sub("[redacted]", obj)
            return obj
        if isinstance(obj, dict):
            return {k: self.redact(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [self.redact(v) for v in obj]
        return obj

    def limit_for(self, key: str) -> int:
        if key in COMMAND_KEYS:
            return self.limits["command_chars"]
        if key in SHORT_KEYS:
            return self.limits["max_field_chars"]
        return self.limits["content_chars"]

    def truncate_payload(self, payload: dict, full_file: str, cap: Optional[int] = None) -> Tuple[dict, bool]:
        cut = False

        def one(key: str, value: Any) -> Any:
            nonlocal cut
            if value is None or isinstance(value, (bool, int, float)):
                return value
            if not isinstance(value, str) and key not in SHORT_KEYS:
                return value
            text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
            limit = cap or self.limit_for(key)
            if len(text) <= limit:
                return value
            cut = True
            return {"text": text[:limit] + "…[truncated]", "truncated": True, "original_length": len(text),
                    "sha256": hashlib.sha256((text + ("x" if "bad_sha" in MUTATE else "")).encode("utf-8")).hexdigest(),
                    "full_file": full_file}

        out = {}
        for key, value in payload.items():
            if key == "tool_input" and isinstance(value, dict):
                out[key] = {k: one(k, v) for k, v in value.items()}
            else:
                out[key] = one(key, value)
        return out, cut

    # ---------- rules ----------
    RULE_KEYS = {"id", "enabled", "tools", "tool_kinds", "reason", "required", "bash_rm_rf",
                 "powershell_remove_recurse_force", "git_push_force", "include_force_with_lease", "env_files",
                 "home_ssh", "command_regex", "path_regex"}

    def rule_status(self) -> Dict[str, str]:
        out = {}
        for rule in self.rules:
            if not isinstance(rule, dict):
                continue
            unknown = set(rule) - self.RULE_KEYS
            if _bridge is None:
                usable = {"command_regex", "path_regex"} & set(rule)
                out[str(rule.get("id"))] = "partially_applied" if usable else "unapplied"
            else:
                out[str(rule.get("id"))] = "partially_applied" if unknown else "applied"
        return out

    def rule_hit(self, payload: dict, cwd: str) -> Optional[dict]:
        if not _bridge:
            return None
        tool_input = payload.get("tool_input") if isinstance(payload.get("tool_input"), dict) else {}
        kind = payload.get("tool_kind")
        command = tool_input.get("command") if isinstance(tool_input.get("command"), str) else ""
        if kind == "shell" and isinstance(payload.get("command"), str):
            command = payload["command"]  # spec 4.3: POSIX-quoted argv join
        tool = str(payload.get("tool_name") or "")
        ctx = {"tool_name": tool, "tool_input": tool_input, "command": command,
               "texts": _bridge.texts_for_rules(tool, tool_input, command), "cwd": Path(cwd),
               "home": Path(os.environ.get("HOME") or str(Path.home())), "rule_errors": []}
        for rule in self.rules:
            if not isinstance(rule, dict):
                continue
            eff = dict(rule)
            kinds = rule.get("tool_kinds")
            if kinds:
                if kind in kinds:
                    eff["tools"] = []
                elif not rule.get("tools"):
                    continue
            try:
                if _bridge.rule_matches(eff, ctx):
                    return rule
            except re.error:
                ctx["rule_errors"].append(f"{rule.get('id')}: bad regex")
            if ctx["rule_errors"]:  # spec 11: a broken rule denies its tools
                return {"id": str(rule.get("id")), "reason": "; ".join(ctx["rule_errors"])}
        return None

    # ---------- emitting ----------
    def envelope(self, seq: int, event: str, cls: str, payload: dict, context: dict) -> dict:
        return {"protocol": "ahp/1", "task_id": self.task, "seq": seq, "event": event, "class": cls,
                "producer": {"name": NAME, "version": VERSION}, "ts": now_iso(), "payload": payload,
                "context": context}

    def record(self, env: dict, outcome: dict) -> None:
        line = {k: v for k, v in env.items() if k not in ("reply_file", "deadline", "timeout_ms", "decisions",
                                                         "default_decision", "truncated", "full_file")}
        line["ts"] = now_iso()
        line["payload"], _ = self.truncate_payload(self.redact(env["payload"]), "events.jsonl",
                                                   cap=self.limits["max_field_chars"])
        for _, obj in walk_truncations(line["payload"]):
            obj.pop("full_file", None)
        line["outcome"] = outcome
        append_line(self.dir / "events.jsonl", line)

    def write_notice(self, kind: str, env: dict, raw_payload: dict) -> dict:
        name = f"{env['seq']}-{env['event']}"
        full_rel = f"{kind}/{name}.full.json"
        short, cut = self.truncate_payload(raw_payload, full_rel)
        env = dict(env, payload=short)
        if cut:
            env["truncated"] = True
            env["full_file"] = full_rel
            write_atomic(self.dir / full_rel, dict(env, payload=raw_payload, truncated=False))
        write_atomic(self.dir / kind / f"{name}.json", env)
        if kind == "push":
            append_line(self.dir / "push" / "stream.jsonl", env)
        return env

    def notify(self, event: str, payload: dict, context: dict) -> int:
        seq = reserve_seq(self.dir)
        env = self.envelope(seq, event, "notify", self.redact(payload), context)
        self.write_notice("push", env, env["payload"])
        self.record(env, {"via": "none"})
        return seq

    def effect(self, step: int, event: str, effect: str, injected: bool = False) -> None:
        append_line(self.effects, {"step": step, "event": event, "effect": effect, "injected": injected})

    def sweep_stray_replies(self) -> None:
        """Spec 7.3 #1: a reply to a non-gate seq is never applied; it gets an invalid ack."""
        replies = self.dir / "replies"
        if not replies.is_dir() or "no_stray_ack" in MUTATE:
            return
        try:
            current = int((self.dir / "seq").read_text().strip() or 0)
        except (OSError, ValueError):
            current = 0
        for path in replies.glob("*.json"):
            if path.name.startswith(".") or not path.stem.isdigit():
                continue
            seq = int(path.stem)
            if seq > current or (self.dir / "acks" / f"{seq}.json").exists():
                continue
            if list((self.dir / "pending").glob(f"{seq}-*.json")):
                continue  # a gate: handled by gate()
            write_atomic(self.dir / "acks" / f"{seq}.json", {
                "protocol": "ahp/1", "task_id": self.task, "seq": seq, "ts": now_iso(), "status": "invalid",
                "via": "invalid_reply", "applied_decision": "ack",
                "errors": [f"seq {seq} is not a gate: reply ignored, nothing injected (spec 7.3)"]})

    # ---------- gates ----------
    def validate_reply(self, path: Path, seq: int, event: str, decisions: List[str]) -> Tuple[Optional[dict], List[str]]:
        if path.is_symlink():
            return None, ["reply is a symlink"]
        try:
            raw = path.read_bytes()
            data = json.loads(raw.decode("utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            return None, [f"unreadable reply: {exc}"]
        errs = []
        if not isinstance(data, dict):
            return None, ["reply is not an object"]
        unknown = [k for k in data if k not in REPLY_KEYS and not k.startswith("x-")]
        if unknown:
            errs.append(f"unknown fields {unknown}")
        d = data.get("decision")
        if d not in decisions:
            errs.append(f"decision {d!r} not in {decisions}")
        if d == "block" and not str(data.get("reason") or "").strip():
            errs.append("block without reason")
        if "seq" in data and data["seq"] != seq:
            errs.append(f"seq {data['seq']} != {seq}")
        if "updated_input" in data and not (d == "allow" and event in ("tool.pre", "permission.request")):
            errs.append("updated_input only with allow on tool.pre/permission.request")
        if isinstance(data.get("updated_input"), dict) and "tool_name" in data["updated_input"]:
            errs.append("updated_input must not change tool_name")
        if "content" in data and not (d == "accept" and event == "input.request"):
            errs.append("content only with accept on input.request")
        if "additional_context" in data:  # spec 7.2 / 7.3
            ctx = data["additional_context"]
            if event not in CONTEXT_EVENTS:
                errs.append(f"additional_context not allowed on {event}")
            elif not isinstance(ctx, str):
                errs.append("additional_context must be a string")
            elif len(ctx) > int(self.limits["max_additional_context_chars"]) and "truncate_context" in MUTATE:
                data["additional_context"] = ctx[: int(self.limits["max_additional_context_chars"])]
            elif len(ctx) > int(self.limits["max_additional_context_chars"]):
                errs.append(f"additional_context has {len(ctx)} chars > limit "
                            f"{self.limits['max_additional_context_chars']} (not truncated)")
        if "watch_paths" in data and event != "session.start":
            errs.append(f"watch_paths not allowed on {event}")
        if "worktree_path" in data and event != "worktree.create":
            errs.append(f"worktree_path not allowed on {event}")
        data["__sha__"] = hashlib.sha256(raw).hexdigest()
        return (None if errs else data), errs

    def gate(self, step: int, seq: int, event: str, payload: dict, context: dict, cap: dict) -> Tuple[str, bool]:
        timeout_ms = int(cap["timeout_ms"])
        default = cap["default_decision"]
        decisions = cap["decisions"]
        env = self.envelope(seq, event, "gate", payload, context)
        env.update({"reply_file": f"replies/{seq}.json", "deadline": now_iso(timeout_ms), "timeout_ms": timeout_ms,
                    "decisions": decisions, "default_decision": default})
        self.write_notice("pending", env, payload)
        reply_path = self.dir / "replies" / f"{seq}.json"
        end = time.monotonic() + timeout_ms / 1000.0
        reply, errors = None, []
        while True:
            if reply_path.exists():
                reply, errors = self.validate_reply(reply_path, seq, event, decisions)
                break
            if time.monotonic() >= end:
                break
            time.sleep(0.05)
        ack = {"protocol": "ahp/1", "task_id": self.task, "seq": seq, "ts": now_iso()}
        outcome: Dict[str, Any] = {}
        applied = default
        accepted = None
        if reply is None and errors:
            ack.update(status="invalid", via="invalid_reply", errors=errors)
            outcome = {"via": "invalid_reply", "reason": "; ".join(errors)[:200]}
        elif reply is None:
            ack.update(status="expired", via="timeout")
            outcome = {"via": "timeout"}
        else:
            applied = reply["decision"]
            ack.update(status="consumed", via="reply", reply_sha256=reply["__sha__"])
            outcome = {"via": "reply", "reason": str(reply.get("reason") or "")[:200]}
            if event == "tool.pre" and applied == "allow" and isinstance(reply.get("updated_input"), dict):
                hit = self.rule_hit(dict(payload, tool_input=reply["updated_input"]), context.get("cwd") or ".")
                if hit:  # spec 13.5: rewritten input is checked again
                    applied = "deny"
                    ack["via"] = "rule"
                    ack["reason"] = f"updated_input hit rule {hit.get('id')}"
                    outcome = {"via": "rule", "rule_id": str(hit.get("id")), "reason": ack["reason"]}
                    self.notify("policy.deny", {"rule_id": str(hit.get("id")), "target_event": event,
                                                "target_seq": seq, "reason": str(hit.get("reason") or ""),
                                                "tool_name": payload.get("tool_name", "")}, context)
        if event in ("turn.stop", "subagent.stop"):
            if applied == "block":
                used_path = self.dir / "state" / "stop-blocks"
                used = int(used_path.read_text().strip() or 0) if used_path.is_file() else 0
                if used >= int(self.limits["max_blocks"]):
                    applied = "ack"
                    ack["via"] = "rule"
                    ack["reason"] = f"max_blocks {self.limits['max_blocks']} reached"
                    outcome = {"via": "rule", "reason": ack["reason"]}
                    accepted = False
                else:
                    write_atomic(used_path, str(used + 1))
            else:
                accepted = ack["status"] == "consumed" and applied == "ack"
            if accepted is not None and event == "turn.stop":
                ack["accepted"] = accepted
        if "fail_open" in MUTATE and ack["status"] != "consumed" and event == "tool.pre":
            applied = "allow"
        ack["applied_decision"] = applied
        outcome["decision"] = applied
        if "no_ack" not in MUTATE:
            write_atomic(self.dir / "acks" / f"{seq}.json", ack)  # spec 9.4 / 7.3: ack before executing/injecting
        if "stray_tmp" in MUTATE:
            (self.dir / "pending" / f"{seq}-{event}.json.tmp").write_text("{}")
        self.record(env, outcome)
        injected = bool(reply and ack.get("via") == "reply" and reply.get("additional_context"))
        return applied, injected

    # ---------- main loop ----------
    def handle(self, step: int, item: dict) -> None:
        self.sweep_stray_replies()
        event = item["event"]
        cap = self.events.get(event) or {"class": "log"}
        cls = cap["class"]
        context = dict(item.get("context") or {})
        context.setdefault("session_id", "conformance")
        context.setdefault("cwd", str(self.root / "work"))
        payload = self.redact(item.get("payload") or {})
        seq = None
        try:
            if item.get("fault") == "bad_input":
                raise ValueError("host input is not a JSON object")
            if item.get("fault") == "crash":
                if "crash_open" in MUTATE:
                    self.effect(step, event, "host-default")
                    return
                raise RuntimeError("simulated producer crash")
            if event == "tool.pre" and "no_rules" not in MUTATE:
                hit = self.rule_hit(payload, context["cwd"])
                if hit:
                    seq = reserve_seq(self.dir)
                    env = self.envelope(seq, event, cls, payload, context)
                    self.record(env, {"decision": "deny", "via": "rule", "rule_id": str(hit.get("id")),
                                      "reason": str(hit.get("reason") or "")})
                    self.notify("policy.deny", {"rule_id": str(hit.get("id")), "target_event": event,
                                                "target_seq": seq, "reason": str(hit.get("reason") or ""),
                                                "tool_name": payload.get("tool_name", ""),
                                                "tool_input": payload.get("tool_input") or {}}, context)
                    self.effect(step, event, "deny")
                    return
            seq = reserve_seq(self.dir)
            if cls == "gate":
                applied, injected = self.gate(step, seq, event, payload, context, cap)
                self.effect(step, event, applied, injected)
            elif cls == "notify":
                env = self.envelope(seq, event, cls, payload, context)
                self.write_notice("push", env, payload)
                self.record(env, {"via": "none"})
                self.effect(step, event, "none")
            else:
                self.record(self.envelope(seq, event, cls, payload, context), {"via": "none"})
                self.effect(step, event, "none")
        except Exception as exc:  # noqa: BLE001 — spec 9.1 fail-closed
            if cls != "gate":
                self.effect(step, event, "none")
                return
            default = cap.get("default_decision", "deny")
            if seq is None:
                seq = reserve_seq(self.dir)
            write_atomic(self.dir / "acks" / f"{seq}.json", {
                "protocol": "ahp/1", "task_id": self.task, "seq": seq, "status": "expired", "via": "error",
                "applied_decision": default, "reason": str(exc)[:200], "ts": now_iso(),
                **({"accepted": False} if event == "turn.stop" else {})})
            env = self.envelope(seq, event, "gate", payload, context)
            self.record(env, {"decision": default, "via": "error", "reason": str(exc)[:200]})
            self.notify("producer.error", {"stage": "gate", "message": str(exc)[:200], "target_seq": seq,
                                           "applied_decision": default}, context)
            self.effect(step, event, default)

    def run(self) -> int:
        self.negotiate()
        self.write_capabilities()
        for i, item in enumerate(self.sc.get("steps") or []):
            self.handle(i, item)
            if item.get("pause_ms"):
                time.sleep(item["pause_ms"] / 1000.0)
        self.sweep_stray_replies()  # spec 7.3 #1: at the latest when the task ends
        return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--describe", action="store_true")
    ap.add_argument("--scenario")
    ap.add_argument("--root")
    ap.add_argument("--task")
    a = ap.parse_args(argv)
    if a.describe:
        print(json.dumps({"name": NAME, "version": VERSION, "watch": "tasks/{task}", "effects": True}))
        return 0
    sc = read_json(Path(a.scenario))
    return Producer(sc, Path(a.root), a.task).run()


if __name__ == "__main__":
    raise SystemExit(main())
