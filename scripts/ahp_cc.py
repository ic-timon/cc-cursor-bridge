"""AHP v1 producer side of the Claude Code adapter (spec/ahp-v1.md, Appendix A).

bridge.py calls this module from every hook process. It owns the AHP task
directory <ahp-root>/tasks/<task>/: capabilities.json, the seq counter,
pending/ and push/ envelopes (+ .full.json), acks/, events.jsonl, state/.
It also validates replies (with the Appendix A.5 legacy aliases) and shapes CC
hook input into AHP payloads. Stdlib only, no network.

Nothing here decides CC stdout; bridge.py keeps that (Appendix A.2/A.3).
"""

from __future__ import annotations

import fcntl
import hashlib
import importlib.util
import json
import os
import re
import shlex
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

PLUGIN_ROOT = Path(__file__).resolve().parent.parent


def _load_map():
    path = PLUGIN_ROOT / "spec" / "tools" / "ahp_map.py"
    spec = importlib.util.spec_from_file_location("ahp_map_for_cc_adapter", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_MAP = _load_map()
CC_TO_AHP: Dict[str, Tuple[str, str, Tuple[str, ...]]] = _MAP.CC_TO_AHP
DECISIONS: Dict[str, List[str]] = _MAP.DECISIONS
DEFAULT_DECISION: Dict[str, str] = _MAP.DEFAULT_DECISION
SYNTHESIZED = _MAP.SYNTHESIZED
AHP_TO_CC = {ahp: cc for cc, (ahp, _, _) in CC_TO_AHP.items()}

PROTOCOL = "ahp/1"
SPEC_VERSION = "1.0.0-draft.3"
PRODUCER_NAME = "cc-cursor-bridge"
HOST_TIMEOUT_FALLBACK_MS = 180_000
HOST_MARGIN_MS = 10_000
EXTRA_EVENTS = {
    "policy.deny": ("notify", ("notify",)),
    "producer.error": ("notify", ("notify",)),
    **{name: (cls, allowed) for name, (cls, allowed, _) in SYNTHESIZED.items()},
}
CLASS_TO_MODE = {"gate": "wait", "notify": "push", "log": "log"}
MODE_TO_CLASS = {"wait": "gate", "push": "notify", "log": "log"}
CLOSED = {"deny", "decline", "block"}

# Appendix A.1: CC tool name -> tool_kind. Anything else (MCP, Skill, Task) is "other".
TOOL_KINDS = {
    "Bash": "shell", "PowerShell": "shell",
    "Edit": "edit", "MultiEdit": "edit", "Write": "edit", "NotebookEdit": "edit",
    "Read": "read", "Grep": "search", "Glob": "search",
    "WebFetch": "fetch", "WebSearch": "fetch", "AskUserQuestion": "ask",
}
TOOL_KIND_VALUES = ("shell", "edit", "read", "search", "fetch", "ask", "other")
CONTEXT_KEYS = ("session_id", "cwd", "transcript_path", "prompt_id", "permission_mode", "turn_id", "agent_id")
KEEP_IN_PAYLOAD = {"message.delta": {"turn_id"}, "subagent.stop": {"agent_id"}, "subagent.start": {"agent_id"}}
PAYLOAD_RENAMES = {"stop_hook_active": "stop_active", "delta": "text", "mcp_server_name": "server"}
COMMAND_KEYS = {"command"}
SHORT_KEYS = {"tool_response", "error", "text"}
SECRET_ENV = ("CC_BRIDGE_CURSOR_API_KEY", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")
# Spec 12.1 SHOULD: common token formats.
SECRET_PATTERNS = [re.compile(p) for p in (
    r"sk-[A-Za-z0-9_-]{16,}",
    r"ghp_[A-Za-z0-9]{20,}",
    r"gh[ousr]_[A-Za-z0-9]{20,}",
    r"github_pat_[A-Za-z0-9_]{20,}",
    r"xox[abposr]-[A-Za-z0-9-]{10,}",
    r"AKIA[0-9A-Z]{16}",
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----",
)]
REDACTED = "[redacted]"

REPLY_KEYS = {"protocol", "seq", "decision", "reason", "updated_input", "additional_context", "content",
              "watch_paths", "worktree_path", "display_content", "consumer", "ts"}
# Appendix A.5: accepted from existing bots, never written by the adapter.
LEGACY_ALIASES = {
    "behavior": "decision", "message": "reason", "updatedInput": "updated_input",
    "additionalContext": "additional_context", "watchPaths": "watch_paths",
    "worktreePath": "worktree_path", "displayContent": "display_content",
}
# spec 7.2: events whose gate reply may carry additional_context / other event-specific fields
CONTEXT_EVENTS = {"tool.pre", "session.start", "prompt.submit", "prompt.expand", "task.create", "task.complete",
                  "teammate.idle", "compact.pre", "tool.post"}
FIELD_EVENTS = {"watch_paths": {"session.start"}, "worktree_path": {"worktree.create"},
                "display_content": {"message.delta"}}
# Appendix A.2: Claude Code caps each additionalContext string at 10,000 characters (JS length,
# i.e. UTF-16 code units) and silently swaps longer text for a file path + 2,000-char preview.
CC_CONTEXT_CAP = 10_000


def context_length(text: str) -> int:
    """Length as Claude Code counts it (UTF-16 code units); never below the code point count."""
    return len(text.encode("utf-16-le")) // 2


RULE_MATCHERS = {"bash_rm_rf", "powershell_remove_recurse_force", "git_push_force", "env_files", "home_ssh",
                 "command_regex", "path_regex"}
RULE_META = {"id", "enabled", "tools", "tool_kinds", "reason", "required", "include_force_with_lease"}


# --------------------------------------------------------------------------- small helpers

def now_iso(offset_ms: int = 0) -> str:
    return (datetime.now().astimezone() + timedelta(milliseconds=offset_ms)).isoformat(timespec="milliseconds")


def env_flag(environ: dict, name: str, default: bool) -> bool:
    raw = environ.get(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def is_path_key(key: Any) -> bool:
    """Spec A.6 #15: path fields are never truncated."""
    return isinstance(key, str) and (key in ("cwd", "path") or key.endswith("_path") or key.endswith("_dir"))


def ensure_dir(path: Path) -> None:
    if not path.is_dir():
        path.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(path, 0o700)
        except OSError:
            pass


def write_atomic(path: Path, data: Any) -> None:
    """Spec 6.2: dot-prefixed temp file in the same dir, fsync, rename."""
    ensure_dir(path.parent)
    text = data if isinstance(data, str) else json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    tmp = path.parent / f".{path.name}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp"
    try:
        fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()


def create_once(path: Path, data: Any) -> bool:
    """Write path atomically only if it does not exist yet. True if this call created it."""
    ensure_dir(path.parent)
    text = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    tmp = path.parent / f".{path.name}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp"
    try:
        fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(tmp, path)
            return True
        except FileExistsError:
            return False
    finally:
        if tmp.exists():
            tmp.unlink()


def append_line(path: Path, obj: Any) -> None:
    ensure_dir(path.parent)
    fd = os.open(str(path), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    with os.fdopen(fd, "a", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            handle.write(json.dumps(obj, ensure_ascii=False) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def locked_counter(path: Path, update: Callable[[int], int]) -> int:
    """Spec 6.3: read, update, write back and fsync under an exclusive lock."""
    ensure_dir(path.parent)
    fd = os.open(str(path), os.O_RDWR | os.O_CREAT, 0o600)
    with os.fdopen(fd, "r+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            raw = handle.read().strip()
            current = int(raw) if raw.isdigit() else 0
            nxt = update(current)
            if nxt != current:
                handle.seek(0)
                handle.truncate()
                handle.write(str(nxt))
                handle.flush()
                os.fsync(handle.fileno())
            return nxt
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


# --------------------------------------------------------------------------- redaction / truncation

class Shaper:
    """Redaction (spec 12.1) and truncation objects (spec 8)."""

    def __init__(self, environ: dict, limits: Dict[str, int]) -> None:
        self.secrets = sorted({environ[k] for k in SECRET_ENV if len(environ.get(k) or "") >= 8}, key=len, reverse=True)
        self.limits = limits

    def redact_text(self, text: str) -> str:
        for secret in self.secrets:
            if secret in text:
                text = text.replace(secret, REDACTED)
        for pattern in SECRET_PATTERNS:
            text = pattern.sub(REDACTED, text)
        return text

    def redact(self, obj: Any) -> Any:
        if isinstance(obj, str):
            return self.redact_text(obj)
        if isinstance(obj, dict):
            return {k: self.redact(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [self.redact(v) for v in obj]
        return obj

    def limit_for(self, key: Any) -> int:
        if key in COMMAND_KEYS:
            return self.limits["command_chars"]
        if key in SHORT_KEYS:
            return self.limits["max_field_chars"]
        return self.limits["content_chars"]

    @staticmethod
    def _cut(text: str, limit: int, full_file: Optional[str]) -> dict:
        obj = {"text": text[:limit] + "…[truncated]", "truncated": True, "original_length": len(text),
               "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest()}
        if full_file:
            obj["full_file"] = full_file
        return obj

    def truncate(self, payload: dict, full_file: Optional[str], cap: Optional[int] = None) -> Tuple[dict, bool]:
        """Return (payload with truncation objects, whether anything was cut). Input must be redacted."""
        cut = False

        def walk(key: Any, value: Any, depth: int) -> Any:
            nonlocal cut
            if value is None or isinstance(value, (bool, int, float)):
                return value
            if is_path_key(key) and isinstance(value, str):
                return value
            limit = cap or self.limit_for(key)
            if isinstance(value, str):
                if len(value) <= limit:
                    return value
                cut = True
                return self._cut(value, limit, full_file)
            if key in SHORT_KEYS or depth > 6:
                text = json.dumps(value, ensure_ascii=False)
                if len(text) <= limit:
                    return value
                cut = True
                return self._cut(text, limit, full_file)
            if isinstance(value, dict):
                return {k: walk(k, v, depth + 1) for k, v in value.items()}
            if isinstance(value, list):
                return [walk(key, v, depth + 1) for v in value]
            return value

        return {k: walk(k, v, 0) for k, v in payload.items()}, cut


# --------------------------------------------------------------------------- hooks.json / settings

def hook_table(plugin_root: Path) -> Dict[str, dict]:
    """hook name -> {"async": bool, "timeout_ms": int} from claude/hooks/hooks.json."""
    out: Dict[str, dict] = {}
    try:
        data = read_json(plugin_root / "claude" / "hooks" / "hooks.json")
        for name, groups in (data.get("hooks") or {}).items():
            handler = groups[0]["hooks"][0]
            out[name] = {"async": bool(handler.get("async")),
                         "timeout_ms": int(float(handler.get("timeout", 60)) * 1000)}
    except (OSError, ValueError, KeyError, IndexError, TypeError):
        pass
    return out


def ahp_root(environ: dict) -> Path:
    raw = environ.get("CC_BRIDGE_AHP_DIR")
    if raw:
        return Path(raw).expanduser()
    base = environ.get("CC_BRIDGE_DIR") or str(Path(environ.get("HOME") or Path.home()) / ".cc-bridge")
    return Path(base).expanduser() / "ahp"


def enabled(environ: dict) -> bool:
    return env_flag(environ, "CC_BRIDGE_AHP", True)


def legacy_enabled(environ: dict) -> bool:
    return env_flag(environ, "CC_BRIDGE_LEGACY", True)


def plugin_version(plugin_root: Path) -> str:
    try:
        return str(read_json(plugin_root / ".claude-plugin" / "plugin.json").get("version") or "unknown")
    except (OSError, ValueError, AttributeError):
        return "unknown"


# --------------------------------------------------------------------------- rules

def rule_status(rule: dict) -> str:
    """Spec 11: applied only if every key is understood and the rule has a matcher."""
    keys = set(rule)
    unknown = keys - RULE_MATCHERS - RULE_META
    kinds = rule.get("tool_kinds")
    if isinstance(kinds, list) and any(k not in TOOL_KIND_VALUES for k in kinds):
        unknown.add("tool_kinds")
    if not (keys & RULE_MATCHERS):
        return "unapplied"
    return "partially_applied" if unknown else "applied"


def tool_kind_of(tool_name: str, cfg: dict) -> Tuple[str, dict]:
    """Returns (kind, mapping spec). config ahp.tool_kinds maps extra (e.g. MCP) tools."""
    extra = ((cfg.get("ahp") or {}).get("tool_kinds") or {})
    spec = extra.get(tool_name)
    if isinstance(spec, str):
        spec = {"kind": spec}
    if isinstance(spec, dict) and spec.get("kind") in TOOL_KIND_VALUES:
        return str(spec["kind"]), spec
    return TOOL_KINDS.get(tool_name, "other"), {}


def shell_command(tool_input: dict, spec: dict) -> str:
    """Spec 4.3: the host command string, or shlex.join(argv) for argv-style tools."""
    key = spec.get("command") or "command"
    if isinstance(tool_input.get(key), str):
        return tool_input[key]
    argv_key = spec.get("argv") or "argv"
    argv = tool_input.get(argv_key)
    if isinstance(argv, list) and all(isinstance(a, str) for a in argv):
        return shlex.join(argv)
    return ""


# --------------------------------------------------------------------------- replies

def normalize_reply(data: Any, seq: int, event: str, decisions: List[str],
                    max_context: int = CC_CONTEXT_CAP) -> Tuple[Optional[dict], List[str]]:
    """Spec 7 + 9.4 with the Appendix A.5 aliases. Returns (canonical reply, errors)."""
    if not isinstance(data, dict):
        return None, ["reply is not a JSON object"]
    errors: List[str] = []
    out: Dict[str, Any] = {}
    for key, value in data.items():
        if key in REPLY_KEYS or key.startswith("x-"):
            target = key
        elif key in LEGACY_ALIASES:
            target = LEGACY_ALIASES[key]
        elif key == "ack":
            if value is not True:
                errors.append("ack must be true")
                continue
            target, value = "decision", "ack"
        else:
            errors.append(f"unknown field {key!r}")
            continue
        if target in out and out[target] != value:
            errors.append(f"conflicting {target!r} (alias and canonical differ)")
            continue
        out[target] = value
    decision = out.get("decision")
    if isinstance(decision, str):
        decision = decision.strip().lower()
    defer = False
    if event in ("input.request", "input.result") and decision in ("allow", "deny"):
        decision = "accept" if decision == "allow" else "decline"  # A.5
    if event == "tool.pre" and (decision == "defer" or (decision == "deny" and out.get("x-defer") is True)):
        decision, defer = "deny", True  # A.5: CC "defer" (tool not run now), recorded as deny
    out["decision"] = decision
    if not isinstance(decision, str) or not decision:
        errors.append("decision missing")
    elif decision not in decisions:
        errors.append(f"decision {decision!r} not in {decisions}")
    reason = out.get("reason")
    if reason is not None and not isinstance(reason, str):
        errors.append("reason must be a string")
    if decision == "block" and not str(reason or "").strip():
        errors.append("block needs a non-empty reason")
    if "seq" in out and out["seq"] != seq:
        errors.append(f"seq {out['seq']!r} != {seq}")
    if "protocol" in out and out["protocol"] != PROTOCOL:
        errors.append(f"protocol {out['protocol']!r} not supported")
    if "updated_input" in out:
        ui = out["updated_input"]
        if not (decision == "allow" and event in ("tool.pre", "permission.request")):
            errors.append("updated_input only with allow on tool.pre/permission.request")
        elif not isinstance(ui, dict):
            errors.append("updated_input must be an object")
        elif "tool_name" in ui:
            errors.append("updated_input must not change tool_name (spec 7.1)")
    if "tool_name" in out:
        errors.append("a reply cannot change tool_name (spec 7.1)")
    if "content" in out and not (decision == "accept" and event == "input.request" and isinstance(out["content"], dict)):
        errors.append("content only as an object with accept on input.request")
    for key, typ in (("additional_context", str), ("worktree_path", str), ("display_content", str)):
        if key in out and not isinstance(out[key], typ):
            errors.append(f"{key} must be a string")
    if "additional_context" in out:  # spec 7.2 / 7.3
        if event not in CONTEXT_EVENTS:
            errors.append(f"additional_context not allowed on {event} (spec 7.2)")
        elif isinstance(out["additional_context"], str) and context_length(out["additional_context"]) > max_context:
            errors.append(f"additional_context is {context_length(out['additional_context'])} chars > "
                          f"limits.max_additional_context_chars {max_context} (never truncated, spec 7.3)")
    for key, events in FIELD_EVENTS.items():
        if key in out and event not in events:
            errors.append(f"{key} not allowed on {event} (spec 7.2)")
    if "watch_paths" in out and not (isinstance(out["watch_paths"], list)
                                     and all(isinstance(p, str) for p in out["watch_paths"])):
        errors.append("watch_paths must be a list of strings")
    if errors:
        return None, errors
    if defer:
        out["x-cc-defer"] = True
    return out, []


def read_reply(path: Path) -> Tuple[Optional[Any], Optional[str], str]:
    """(parsed JSON or None, error or None, sha256 of bytes). Symlinks are invalid (spec 12.2)."""
    try:
        if path.is_symlink():
            return None, "reply is a symlink", ""
        fd = os.open(str(path), os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(fd, "rb") as handle:
            raw = handle.read()
    except OSError as exc:
        return None, f"unreadable reply: {exc}", ""
    sha = hashlib.sha256(raw).hexdigest()
    try:
        return json.loads(raw.decode("utf-8")), None, sha
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        return None, f"reply is not JSON: {exc}", sha


# --------------------------------------------------------------------------- the task

class Task:
    """One AHP task dir as seen from one hook process."""

    def __init__(self, task_id: str, environ: dict, cfg: dict, rules: List[dict], notes: List[str],
                 base_mode: Callable[[str], str], wait_all: bool, base_timeout_ms: int) -> None:
        self.task_id = task_id
        self.environ = environ
        self.cfg = cfg
        self.dir = ahp_root(environ) / "tasks" / task_id
        self.hooks = hook_table(PLUGIN_ROOT)
        sync_timeouts = [h["timeout_ms"] for n, h in self.hooks.items() if not h["async"]]
        host = min(sync_timeouts) if sync_timeouts else HOST_TIMEOUT_FALLBACK_MS
        pending = cfg.get("pending") or {}
        self.limits = {
            "max_field_chars": int((cfg.get("log") or {}).get("max_field_chars") or 240),
            "content_chars": int(pending.get("content_chars") or 2000),
            "command_chars": int(pending.get("command_chars") or 4000),
            "max_blocks": int(base_max_blocks(cfg, environ)),
            "host_hook_timeout_ms": host,
            "max_gate_timeout_ms": host - HOST_MARGIN_MS,
            "max_additional_context_chars": max_context_chars(cfg),
        }
        self.shaper = Shaper(environ, self.limits)
        self.producer = {"name": PRODUCER_NAME, "version": plugin_version(PLUGIN_ROOT), "host": {"name": "claude-code"}}
        self.base_rules = [r for r in rules if isinstance(r, dict)]
        self.load_notes = list(notes)
        self.wait_all = wait_all
        self.base_timeout_ms = base_timeout_ms
        self.base_mode = base_mode
        self._negotiate()

    # ---------- negotiation (spec 3.3); deterministic, recomputed by every hook ----------
    def _negotiate(self) -> None:
        notes: List[str] = []
        state = "absent"
        cj: dict = {}
        path = self.dir / "consumer.json"
        if path.is_file():
            try:
                cj = read_json(path)
                if not isinstance(cj, dict):
                    raise ValueError("not an object")
                protocols = cj.get("accept_protocols") or [cj.get("protocol")]
                if cj.get("protocol") != PROTOCOL and PROTOCOL not in protocols:
                    raise ValueError(f"protocol {cj.get('protocol')!r} not supported")
                state = "applied"
            except (OSError, ValueError) as exc:
                notes.append(f"consumer.json rejected: {exc}")
                state, cj = "rejected", {}
        ceiling = self.limits["host_hook_timeout_ms"] - HOST_MARGIN_MS
        partial = False
        events: Dict[str, dict] = {}
        requested = cj.get("event_classes") if isinstance(cj.get("event_classes"), dict) else {}
        timeouts = cj.get("timeouts_ms") if isinstance(cj.get("timeouts_ms"), dict) else {}
        self.native_of: Dict[str, str] = {}
        for hook, (name, _, allowed) in CC_TO_AHP.items():
            mode = self.base_mode(hook)
            if mode == "off":
                continue
            base_cls = MODE_TO_CLASS.get(mode, "log")
            allow = [c for c in allowed if c != "gate" or not self.hooks.get(hook, {}).get("async")]
            if self.wait_all and "gate" not in allow:
                allow.append("gate")
            if base_cls not in allow:
                allow.append(base_cls)
            cls = base_cls
            want = requested.get(name)
            if want is not None:
                if want in allow:
                    cls = want
                else:
                    partial = True
                    why = "hook is async in hooks.json" if want == "gate" and "gate" in allowed else "not allowed"
                    notes.append(f"event_classes.{name}={want} not applied ({why})")
            entry: Dict[str, Any] = {"class": cls, "allowed_classes": allow, "native": [hook]}
            if cls == "gate" or "gate" in allow:
                ms = min(self.base_timeout_ms, ceiling)
                if name in timeouts:
                    try:
                        asked = int(timeouts[name])
                        ms = max(1000, min(asked, ceiling))
                        if ms != asked:
                            partial = True
                            notes.append(f"timeouts_ms.{name}={asked} clamped to {ms}")
                    except (TypeError, ValueError):
                        partial = True
                        notes.append(f"timeouts_ms.{name} is not an integer")
                entry.update(decisions=list(DECISIONS.get(name, ["ack"])),
                             default_decision=DEFAULT_DECISION.get(name, "ack"), timeout_ms=int(ms))
            events[name] = entry
            self.native_of[name] = hook
        for name, (cls, allowed) in EXTRA_EVENTS.items():
            events[name] = {"class": cls, "allowed_classes": list(allowed)}
        for name in requested:
            if name not in events:
                partial = True
                notes.append(f"event_classes.{name}: unknown event")
        for name in timeouts:
            if name not in events or "timeout_ms" not in events[name]:
                partial = True
                notes.append(f"timeouts_ms.{name}: not a gate-capable event")
        extra_rules = []
        for rule in cj.get("extra_rules") or []:
            if not isinstance(rule, dict) or not rule.get("id"):
                partial = True
                notes.append("extra_rules: skipped an entry without id")
                continue
            rule = dict(rule)
            rule.pop("enabled", None)  # spec 11: extra rules only add denies
            extra_rules.append(rule)
        features = cj.get("features") if isinstance(cj.get("features"), dict) else {}
        for name, on in features.items():
            if on and name not in FEATURES:
                partial = True
                notes.append(f"features.{name} not supported by the CC adapter")
        if state == "applied" and partial:
            state = "partially_applied"
        self.events = events
        self.consumer_state = state
        self.consumer_notes = notes
        self.rules = self.base_rules + extra_rules

    def caps(self) -> dict:
        blob = json.dumps(self.rules, sort_keys=True, ensure_ascii=False).encode("utf-8")
        ids = [str(r.get("id")) for r in self.rules]
        caps = {
            "protocol": PROTOCOL, "spec_version": SPEC_VERSION, "task_id": self.task_id,
            "producer": self.producer, "created_at": now_iso(), "transport": "local-fs",
            "consumer_json": self.consumer_state, "events": self.events, "features": dict(FEATURES),
            "limits": dict(self.limits),
            "rules": {"ids": ids, "status": {str(r.get("id")): rule_status(r) for r in self.rules},
                      "required": [str(r.get("id")) for r in self.rules if r.get("required")],
                      "sha256": hashlib.sha256(blob).hexdigest(), "source": rules_source(self.environ)},
            "redaction": {"methods": ["env-values", "patterns"], "token": REDACTED},
            "x-cc-adapter": {"legacy_tree": legacy_enabled(self.environ)},
        }
        if self.consumer_notes:
            caps["consumer_notes"] = self.consumer_notes
        return caps

    def ensure_capabilities(self) -> bool:
        """Spec 3.3.2: written once, before the first event. True if this hook wrote it."""
        if (self.dir / "capabilities.json").is_file():
            return False
        ensure_dir(self.dir)
        created = create_once(self.dir / "capabilities.json", self.caps())
        if created:
            for note in self.load_notes:
                self.notify("producer.error", {"stage": "config", "message": note}, {})
        return created

    # ---------- per event ----------
    def entry(self, name: str) -> Optional[dict]:
        return self.events.get(name)

    def reserve_seq(self) -> int:
        return locked_counter(self.dir / "seq", lambda cur: cur + 1)

    def context(self, event: dict) -> dict:
        out = {}
        for key in CONTEXT_KEYS:
            value = event.get(key)
            if isinstance(value, str) and value:
                out[key] = value
        return self.shaper.redact(out)

    def envelope(self, seq: int, name: str, cls: str, payload: dict, context: dict,
                 native: Optional[dict] = None) -> dict:
        env = {"protocol": PROTOCOL, "task_id": self.task_id, "seq": seq, "event": name, "class": cls,
               "producer": self.producer, "ts": now_iso(), "payload": payload, "context": context}
        if native:
            env["native"] = native
        return env

    def write_notice(self, kind: str, env: dict) -> dict:
        """pending/ or push/ file (+ .full.json when cut); push also appends stream.jsonl."""
        stem = f"{env['seq']}-{env['event']}"
        full_rel = f"{kind}/{stem}.full.json"
        short, cut = self.shaper.truncate(env["payload"], full_rel)
        out = dict(env, payload=short)
        if cut:
            out["truncated"] = True
            out["full_file"] = full_rel
            write_atomic(self.dir / full_rel, dict(env, truncated=False, full_file=full_rel))
        write_atomic(self.dir / kind / f"{stem}.json", out)
        if kind == "push":
            append_line(self.dir / "push" / "stream.jsonl", out)
        return out

    def record(self, env: dict, outcome: dict) -> None:
        """events.jsonl audit record (spec 5.3): payload capped at max_field_chars, no full_file."""
        line = {k: v for k, v in env.items()
                if k not in ("reply_file", "deadline", "timeout_ms", "decisions", "default_decision",
                             "truncated", "full_file")}
        line["ts"] = now_iso()
        payload, _ = self.shaper.truncate(env["payload"], None, cap=self.limits["max_field_chars"])
        line["payload"] = payload
        line["outcome"] = outcome
        append_line(self.dir / "events.jsonl", line)

    def notify(self, name: str, payload: dict, context: dict) -> int:
        seq = self.reserve_seq()
        env = self.envelope(seq, name, "notify", self.shaper.redact(payload), context)
        self.write_notice("push", env)
        self.record(env, {"via": "none"})
        return seq

    def write_ack(self, seq: int, ack: dict) -> None:
        body = {"protocol": PROTOCOL, "task_id": self.task_id, "seq": seq, "ts": now_iso()}
        body.update(ack)
        if "reason" in body:
            body["reason"] = self.shaper.redact_text(str(body["reason"]))[:500]
        write_atomic(self.dir / "acks" / f"{seq}.json", body)

    def sweep_stray_replies(self) -> List[int]:
        """Spec 7.3 #1: a reply to a non-gate seq is never applied; ack it invalid before the next event."""
        replies = self.dir / "replies"
        out: List[int] = []
        if not replies.is_dir():
            return out
        try:
            current = int((self.dir / "seq").read_text(encoding="utf-8").strip() or 0)
        except (OSError, ValueError):
            current = 0
        for path in replies.glob("*.json"):
            if path.name.startswith(".") or not path.stem.isdigit():
                continue
            seq = int(path.stem)
            if seq > current or self.has_ack(seq) or any((self.dir / "pending").glob(f"{seq}-*.json")):
                continue
            self.write_ack(seq, {"status": "invalid", "via": "invalid_reply", "applied_decision": "ack",
                                 "errors": [f"seq {seq} is not a gate: reply ignored, nothing injected (spec 7.3)"]})
            out.append(seq)
        return out

    def has_ack(self, seq: int) -> bool:
        return (self.dir / "acks" / f"{seq}.json").is_file()

    # ---------- state ----------
    def stop_blocks(self, increment: bool = False) -> int:
        return locked_counter(self.dir / "state" / "stop-blocks", (lambda c: c + 1) if increment else (lambda c: c))

    def remember_turn_stop(self, seq: int, accepted: bool) -> None:
        write_atomic(self.dir / "state" / "last-turn-stop.json", {"seq": seq, "accepted": accepted})

    def last_turn_stop(self) -> Optional[dict]:
        try:
            data = read_json(self.dir / "state" / "last-turn-stop.json")
            return data if isinstance(data, dict) else None
        except (OSError, ValueError):
            return None


FEATURES = {"acks": True, "full_files": True, "updated_input": True, "additional_context": True,
            "message_delta": True, "policy_deny_event": True, "consumer_rules": True, "reasoning_delta": False}


def max_context_chars(cfg: dict) -> int:
    """bridge JSON ahp.max_additional_context_chars may only lower CC's own cap."""
    try:
        asked = int((cfg.get("ahp") or {}).get("max_additional_context_chars") or CC_CONTEXT_CAP)
    except (TypeError, ValueError):
        asked = CC_CONTEXT_CAP
    return max(1, min(asked, CC_CONTEXT_CAP))


def rules_source(environ: dict) -> str:
    return "CC_BRIDGE_RULES" if environ.get("CC_BRIDGE_RULES") else "config/rules.json"


def base_max_blocks(cfg: dict, environ: dict) -> int:
    raw = str(environ.get("CC_BRIDGE_STOP_MAX_BLOCKS") or "").strip()
    if raw.isdigit():
        return int(raw)
    try:
        return max(0, int((cfg.get("stop") or {}).get("max_blocks", 3)))
    except (TypeError, ValueError):
        return 3


# --------------------------------------------------------------------------- payloads

def payload_for(name: str, event: dict, extra: Optional[dict] = None) -> dict:
    """CC hook input -> AHP payload (snake_case host fields, context keys moved out)."""
    keep = KEEP_IN_PAYLOAD.get(name, set())
    payload: Dict[str, Any] = {}
    for key, value in event.items():
        if key == "hook_event_name" or (key in CONTEXT_KEYS and key not in keep):
            continue
        payload[PAYLOAD_RENAMES.get(key, key)] = value
    if name in ("tool.pre", "tool.post", "tool.error", "permission.request", "permission.denied"):
        if not isinstance(payload.get("tool_name"), str) or not payload.get("tool_name"):
            payload["tool_name"] = "unknown"
        if not isinstance(payload.get("tool_input"), dict):
            payload["tool_input"] = {}
    if name == "tool.error":
        payload.setdefault("error", "")
    if name == "turn.stop":
        payload.setdefault("last_assistant_message", "")
    if name == "subagent.stop":
        payload.setdefault("agent_id", str(event.get("agent_id") or ""))
    if name == "session.start" and payload.get("source") not in ("startup", "resume", "clear", "compact"):
        payload["source"] = "other"
    if name == "session.end":
        payload.setdefault("reason", "other")
    if name == "prompt.submit":
        payload.setdefault("prompt", "")
    if name == "input.request":
        payload.setdefault("message", "")
    if name == "worktree.create":
        payload.setdefault("name", str(event.get("name") or "worktree"))
    if name == "message.delta":
        payload.setdefault("text", "")
        payload.setdefault("message_id", "")
        payload["final"] = bool(payload.get("final", False))
        payload.setdefault("kind", "text")
    if extra:
        payload.update(extra)
    return payload


def plan_items(tool_input: dict) -> List[dict]:
    items = []
    for todo in tool_input.get("todos") or []:
        if not isinstance(todo, dict):
            continue
        status = str(todo.get("status") or "pending")
        if status not in ("pending", "in_progress", "completed", "cancelled"):
            status = "pending"
        item = {"content": str(todo.get("content") or todo.get("activeForm") or ""), "status": status}
        if isinstance(todo.get("id"), str):
            item["id"] = todo["id"]
        items.append(item)
    return items


def iter_task_files(task_dir: Path, kind: str) -> Iterable[Path]:
    folder = task_dir / kind
    if not folder.is_dir():
        return []
    return sorted((p for p in folder.glob("*.json") if not p.name.startswith(".") and not p.name.endswith(".full.json")),
                  key=lambda p: (int(p.name.split("-", 1)[0]) if p.name.split("-", 1)[0].isdigit() else 0, p.name))
