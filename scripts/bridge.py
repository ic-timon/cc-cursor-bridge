"""cc-cursor-bridge hook dispatcher.

Logs every Claude Code hook event, denies dangerous tool calls, and leaves a
local pending/replies channel plus a notifier call site. No third-party
packages and no network calls.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

TRUTHY = {"1", "true", "yes", "on"}
ENV_TOKEN = re.compile(r"(?<![\w.\\-])\.env(?:\.[\w-]+)*(?![\w.-])")
RM_AT = re.compile(r"(?:^|[ \t\n;&|`(\"'])rm\b")
GIT_PUSH_AT = re.compile(r"(?:^|[ \t\n;&|`(\"'])git(?:\s+-C\s+\S+)?\s+push\b")
SHELL_SPLIT = re.compile(r"&&|\|\||[;\n`]")
HOME_SSH = re.compile(r"(?:~|\$\{HOME\}|\$HOME)/\.ssh\b[^\s'\"|&;<>]*")
ABS_SSH = re.compile(
    r"(?:^|[\s'\"=:])(/(?:[^\s'\"|&;<>]+/)*\.ssh\b[^\s'\"|&;<>]*)"
)
REL_SSH = re.compile(r"(?<![\w./-])(\.ssh\b(?:/[^\s'\"|&;<>]+)?)")


@dataclass
class HookResult:
    stdout: str = ""
    stderr: str = ""
    code: int = 0


def default_rules() -> List[dict]:
    return [
        {
            "id": "bash-rm-rf",
            "enabled": True,
            "tools": ["Bash", "PowerShell"],
            "reason": "拦截递归强制删除（rm -rf，或 PowerShell 的 Remove-Item -Recurse -Force）",
            "bash_rm_rf": True,
            "powershell_remove_recurse_force": True,
        },
        {
            "id": "bash-git-push-force",
            "enabled": True,
            "tools": ["Bash", "PowerShell"],
            "reason": "拦截 git push --force / -f / --force-with-lease",
            "git_push_force": True,
            "include_force_with_lease": True,
        },
        {
            "id": "secret-env-file",
            "enabled": True,
            "tools": [
                "Bash",
                "PowerShell",
                "Read",
                "Edit",
                "Write",
                "MultiEdit",
                "NotebookEdit",
                "Glob",
                "Grep",
            ],
            "reason": "拦截读写 .env（放行 .env.example、.env.sample、.env.template）",
            "env_files": {
                "allow_basenames": [".env.example", ".env.sample", ".env.template"]
            },
        },
        {
            "id": "ssh-dir",
            "enabled": True,
            "tools": [
                "Bash",
                "PowerShell",
                "Read",
                "Edit",
                "Write",
                "MultiEdit",
                "NotebookEdit",
                "Glob",
                "Grep",
            ],
            "reason": "拦截访问 ~/.ssh",
            "home_ssh": True,
        },
    ]


def default_config() -> dict:
    return {
        "notifier": {
            "enabled": False,
            "events": [
                "PermissionRequest",
                "PreToolUse",
                "PostToolUseFailure",
                "StopFailure",
                "Stop",
                "Notification",
            ],
            "pre_tool_use": "denied_only",
            "endpoint": "https://api.cursor.com/v0/agents/{agent_id}/followup",
            "agent_id_env": "CC_BRIDGE_CURSOR_AGENT_ID",
            "api_key_env": "CC_BRIDGE_CURSOR_API_KEY",
        },
        "wait": {
            "enabled": False,
            "on_permission_request": True,
            "on_pre_tool_use": False,
            "timeout_seconds": 120,
            "poll_interval_seconds": 0.25,
            "timeout_decision": "deny",
        },
        "log": {"max_field_chars": 240},
    }


def plugin_root_from(environ: dict) -> Path:
    raw = environ.get("CLAUDE_PLUGIN_ROOT")
    if raw:
        return Path(raw)
    return Path(__file__).resolve().parent.parent


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def load_rules(root: Path, environ: dict) -> Tuple[List[dict], str]:
    override = environ.get("CC_BRIDGE_RULES")
    path = Path(override).expanduser() if override else root / "config" / "rules.json"
    if not path.is_file():
        return default_rules(), ""
    try:
        data = read_json(path)
    except (OSError, json.JSONDecodeError) as exc:
        return default_rules(), f"规则文件无法解析，已回退到内置规则: {exc}"
    rules = data.get("rules") if isinstance(data, dict) else None
    if not isinstance(rules, list):
        return default_rules(), "规则文件缺少 rules 数组，已回退到内置规则"
    return rules, ""


def load_config(root: Path, environ: dict) -> Tuple[dict, str]:
    override = environ.get("CC_BRIDGE_CONFIG")
    path = Path(override).expanduser() if override else root / "config" / "bridge.json"
    cfg = default_config()
    if not path.is_file():
        return cfg, ""
    try:
        data = read_json(path)
    except (OSError, json.JSONDecodeError) as exc:
        return cfg, f"配置文件无法解析，已回退到内置配置: {exc}"
    if not isinstance(data, dict):
        return cfg, "配置文件不是对象，已回退到内置配置"
    for key in ("notifier", "wait", "log"):
        if isinstance(data.get(key), dict):
            cfg[key].update(data[key])
    return cfg, ""


def env_flag(environ: dict, name: str, default: bool) -> bool:
    raw = environ.get(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in TRUTHY


def safe_component(value: str) -> str:
    if not value or not re.fullmatch(r"[A-Za-z0-9._-]{1,200}", value):
        return ""
    if value in (".", ".."):
        return ""
    return value


def safe_file_id(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "", value or "")
    if not cleaned or cleaned in (".", ".."):
        return uuid.uuid4().hex
    return cleaned[:120]


def secret_values(environ: dict) -> List[str]:
    found = []
    for key in (
        "CC_BRIDGE_CURSOR_API_KEY",
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
    ):
        value = environ.get(key) or ""
        if len(value) >= 8:
            found.append(value)
    return found


def redact(text: str, secrets: List[str]) -> str:
    for secret in secrets:
        if secret and secret in text:
            text = text.replace(secret, "[redacted]")
    return text


def truncate(value: Any, limit: int, secrets: List[str]) -> str:
    if isinstance(value, str):
        text = value
    else:
        text = json.dumps(value, ensure_ascii=False, default=str)
    text = redact(text, secrets)
    if len(text) <= limit:
        return text
    return text[:limit] + "…[truncated]"


def home_dir(environ: dict) -> Path:
    raw = environ.get("HOME")
    if raw:
        return Path(raw)
    return Path.home()


def expand_home(raw: str, home: Path) -> str:
    text = raw.strip().strip("'\"")
    text = text.replace("${HOME}", "~").replace("$HOME", "~")
    if text == "~":
        return str(home)
    if text.startswith("~/"):
        return str(home / text[2:])
    return text


def resolve_path(raw: str, cwd: Path, home: Path) -> Path:
    expanded = expand_home(raw, home)
    path = Path(expanded)
    if not path.is_absolute():
        path = cwd / path
    return path.resolve()


def rm_args_are_rf(rest: str) -> bool:
    recursive = False
    force = False
    for raw in rest.split():
        tok = raw.strip("'\"")
        if tok == "--":
            break
        if tok in ("--recursive", "-r", "-R"):
            recursive = True
            continue
        if tok == "--force":
            force = True
            continue
        if tok.startswith("--"):
            continue
        if tok.startswith("-") and len(tok) > 1:
            flags = tok[1:]
            if "r" in flags or "R" in flags:
                recursive = True
            if "f" in flags:
                force = True
    return recursive and force


def command_has_rm_rf(command: str) -> bool:
    for match in RM_AT.finditer(command):
        rest = SHELL_SPLIT.split(command[match.end() :], maxsplit=1)[0]
        if rm_args_are_rf(rest):
            return True
    return False


def push_is_force(rest: str, include_lease: bool) -> bool:
    if include_lease and "--force-with-lease" in rest:
        return True
    if re.search(r"(?:^|\s)--force(?:\s|=|$)", rest):
        return True
    for raw in rest.split():
        tok = raw.strip("'\"")
        if tok.startswith("-") and not tok.startswith("--") and "f" in tok[1:]:
            return True
    return False


def command_has_force_push(command: str, include_lease: bool) -> bool:
    for match in GIT_PUSH_AT.finditer(command):
        rest = SHELL_SPLIT.split(command[match.end() :], maxsplit=1)[0]
        if push_is_force(rest, include_lease):
            return True
    return False


def powershell_recursive_force(command: str) -> bool:
    if not re.search(r"\bRemove-Item\b", command, re.IGNORECASE):
        return False
    return bool(
        re.search(r"-Recurse\b", command, re.IGNORECASE)
        and re.search(r"-Force\b", command, re.IGNORECASE)
    )


def text_has_denied_env(text: str, allow: set) -> bool:
    hits = ENV_TOKEN.findall(text)
    base = Path(text.rstrip("/\\")).name
    if (base == ".env" or base.startswith(".env.")) and base not in hits:
        hits.append(base)
    for hit in hits:
        name = Path(hit).name
        if name in allow:
            continue
        if name == ".env" or name.startswith(".env."):
            return True
    return False


def path_is_under_home_ssh(raw: str, cwd: Path, home: Path) -> bool:
    if HOME_SSH.search(raw):
        return True
    try:
        resolved = resolve_path(raw, cwd, home)
    except OSError:
        return False
    ssh = (home / ".ssh").resolve()
    return resolved == ssh or ssh in resolved.parents


def text_reaches_home_ssh(text: str, cwd: Path, home: Path) -> bool:
    if HOME_SSH.search(text):
        return True
    for match in ABS_SSH.finditer(text):
        if path_is_under_home_ssh(match.group(1), cwd, home):
            return True
    for match in REL_SSH.finditer(text):
        if path_is_under_home_ssh(match.group(1), cwd, home):
            return True
    if not re.search(r"\s", text) and path_is_under_home_ssh(text, cwd, home):
        return True
    return False


def texts_for_rules(tool_name: str, tool_input: dict, command: str) -> List[str]:
    texts: List[str] = []
    if command:
        texts.append(command)
    if tool_name == "Grep":
        keys = ("path", "glob")
    elif tool_name == "Glob":
        keys = ("pattern", "path")
    else:
        keys = ("file_path", "path", "notebook_path", "target_notebook")
    for key in keys:
        value = tool_input.get(key)
        if isinstance(value, str) and value:
            texts.append(value)
    return texts


def rule_matches(rule: dict, ctx: dict) -> bool:
    if not rule.get("enabled", True):
        return False
    tools = rule.get("tools") or []
    if tools and ctx["tool_name"] not in tools:
        return False
    command = ctx["command"]
    texts = ctx["texts"]
    if rule.get("bash_rm_rf") and command and command_has_rm_rf(command):
        return True
    if rule.get("powershell_remove_recurse_force") and command and powershell_recursive_force(command):
        return True
    if rule.get("git_push_force") and command and command_has_force_push(
        command, bool(rule.get("include_force_with_lease", True))
    ):
        return True
    if isinstance(rule.get("env_files"), dict):
        allow = set(rule["env_files"].get("allow_basenames") or [])
        for text in texts:
            if text_has_denied_env(text, allow):
                return True
    if rule.get("home_ssh"):
        for text in texts:
            if text_reaches_home_ssh(text, ctx["cwd"], ctx["home"]) or _glob_reaches_ssh(
                ctx, text
            ):
                return True
    if rule.get("command_regex") and command:
        try:
            if re.search(rule["command_regex"], command):
                return True
        except re.error:
            ctx["rule_errors"].append(f"{rule.get('id')}: command_regex 无效")
    if rule.get("path_regex"):
        try:
            compiled = re.compile(rule["path_regex"])
        except re.error:
            ctx["rule_errors"].append(f"{rule.get('id')}: path_regex 无效")
        else:
            for text in texts:
                if compiled.search(text):
                    return True
    return False


def _glob_reaches_ssh(ctx: dict, text: str) -> bool:
    """A Glob/Grep rooted at $HOME whose pattern names .ssh reaches ~/.ssh."""
    if ctx["tool_name"] not in ("Glob", "Grep"):
        return False
    if not re.search(r"(?:^|[/\\])\.ssh(?:$|[/\\])", text):
        return False
    tool_input = ctx["tool_input"]
    search = tool_input.get("path") if isinstance(tool_input.get("path"), str) else ""
    if not search:
        root = ctx["cwd"]
    else:
        try:
            root = resolve_path(search, ctx["cwd"], ctx["home"])
        except OSError:
            return False
    home = ctx["home"].resolve()
    ssh = (home / ".ssh").resolve()
    if root == ssh or ssh in root.parents:
        return True
    # Only a search rooted at $HOME itself. A project under $HOME is not enough.
    return root == home


def first_deny(rules: List[dict], ctx: dict) -> Optional[dict]:
    for rule in rules:
        if not isinstance(rule, dict):
            continue
        try:
            matched = rule_matches(rule, ctx)
        except re.error:
            continue
        if matched:
            return rule
    return None


def summarize(event: dict, limit: int, secrets: List[str]) -> dict:
    summary: Dict[str, Any] = {}
    tool_name = event.get("tool_name")
    if isinstance(tool_name, str):
        summary["tool_name"] = tool_name
    tool_input = event.get("tool_input") if isinstance(event.get("tool_input"), dict) else {}
    for key in (
        "command",
        "file_path",
        "notebook_path",
        "path",
        "pattern",
        "glob",
        "old_string",
        "new_string",
        "content",
        "description",
    ):
        if key in tool_input and tool_input[key] not in (None, ""):
            summary[key] = truncate(tool_input[key], limit, secrets)
    edits = tool_input.get("edits")
    if isinstance(edits, list):
        summary["edit_count"] = len(edits)
        if edits and isinstance(edits[0], dict):
            for key in ("old_string", "new_string"):
                if key in edits[0]:
                    summary[key] = truncate(edits[0][key], limit, secrets)
    if "tool_response" in event and event.get("tool_response") not in (None, ""):
        summary["tool_response"] = truncate(event["tool_response"], limit, secrets)
    for key in (
        "source",
        "trigger",
        "notification_type",
        "error",
        "reason",
        "agent_type",
        "load_reason",
        "name",
        "worktree_path",
        "delta",
        "message",
    ):
        if key in event and event[key] not in (None, "") and key not in summary:
            summary[key] = truncate(event[key], limit, secrets)
    if isinstance(event.get("file_path"), str) and "file_path" not in summary:
        summary["file_path"] = truncate(event["file_path"], limit, secrets)
    if isinstance(event.get("tool_use_id"), str):
        summary["tool_use_id"] = event["tool_use_id"][:120]
    return summary


def task_dir_for(event: dict, environ: dict) -> Path:
    base = environ.get("CC_BRIDGE_DIR") or str(Path.home() / ".cc-bridge")
    task = safe_component(environ.get("CC_TASK_ID") or "")
    if not task:
        task = safe_component(str(event.get("session_id") or ""))
    if not task:
        task = "unknown"
    return Path(base).expanduser() / "tasks" / task


def local_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="milliseconds")


def append_record(path: Path, record: dict, before_write: Optional[Callable[[dict], None]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            handle.seek(0)
            existing = [line for line in handle.read().splitlines() if line.strip()]
            record["seq"] = len(existing) + 1
            if before_write is not None:
                before_write(record)
            handle.seek(0, os.SEEK_END)
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
            handle.flush()
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def should_push(cfg: dict, record: dict) -> bool:
    notifier = cfg.get("notifier") or {}
    events = notifier.get("events") or []
    name = record.get("event")
    if name == "PreToolUse":
        mode = notifier.get("pre_tool_use", "denied_only")
        if mode == "all":
            return True
        if mode == "denied_only":
            return record.get("decision") == "deny"
        return False
    return name in events


def maybe_notify(cfg: dict, record: dict, task_dir: Path, environ: dict) -> str:
    notifier = cfg.get("notifier") or {}
    if not should_push(cfg, record):
        return "not_selected"
    enabled = env_flag(environ, "CC_BRIDGE_NOTIFIER", bool(notifier.get("enabled")))
    if not enabled:
        return "disabled"
    agent_env = notifier.get("agent_id_env") or "CC_BRIDGE_CURSOR_AGENT_ID"
    key_env = notifier.get("api_key_env") or "CC_BRIDGE_CURSOR_API_KEY"
    agent_id = environ.get(agent_env) or ""
    endpoint = notifier.get("endpoint") or "https://api.cursor.com/v0/agents/{agent_id}/followup"
    url = endpoint.replace("{agent_id}", agent_id or "{agent_id}")
    secrets = secret_values(environ)
    prompt = redact(
        f"[cc-cursor-bridge] {record.get('event')} session={record.get('session_id')} "
        f"summary={json.dumps(record.get('summary'), ensure_ascii=False)}",
        secrets,
    )
    payload = {
        "kind": "cursor_followup",
        "http": "not_sent",
        "intended_method": "POST",
        "intended_url": url,
        "agent_id": agent_id or None,
        "api_key_configured": bool(environ.get(key_env)),
        "prompt_text": prompt,
        "event": record.get("event"),
        "session_id": record.get("session_id"),
        "summary": record.get("summary"),
        "decision": record.get("decision"),
        "rule_id": record.get("rule_id"),
    }
    outbox = task_dir / "outbox"
    outbox.mkdir(parents=True, exist_ok=True)
    name = f"{int(record.get('seq') or 0):06d}-{record.get('event')}.json"
    (outbox / name).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return "queued_local"


def wait_settings(cfg: dict, environ: dict) -> dict:
    wait = dict(cfg.get("wait") or {})
    wait["enabled"] = env_flag(environ, "CC_BRIDGE_WAIT", bool(wait.get("enabled")))
    if environ.get("CC_BRIDGE_WAIT_PRE_TOOL"):
        wait["on_pre_tool_use"] = env_flag(
            environ, "CC_BRIDGE_WAIT_PRE_TOOL", bool(wait.get("on_pre_tool_use"))
        )
    if environ.get("CC_BRIDGE_WAIT_TIMEOUT"):
        wait["timeout_seconds"] = float(environ["CC_BRIDGE_WAIT_TIMEOUT"])
    return wait


def should_wait(event_name: str, wait: dict, already_denied: bool) -> bool:
    if not wait.get("enabled") or already_denied:
        return False
    if event_name == "PermissionRequest":
        return bool(wait.get("on_permission_request", True))
    if event_name == "PreToolUse":
        return bool(wait.get("on_pre_tool_use"))
    return False


def wait_for_reply(task_dir: Path, request_id: str, pending_body: dict, wait: dict) -> Tuple[str, str, str]:
    pending_path = task_dir / "pending" / f"{request_id}.json"
    reply_path = task_dir / "replies" / f"{request_id}.json"
    pending_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = pending_path.with_suffix(".tmp")
    tmp.write_text(json.dumps(pending_body, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(pending_path)
    timeout = max(0.0, float(wait.get("timeout_seconds", 120)))
    interval = max(0.05, float(wait.get("poll_interval_seconds", 0.25)))
    default = wait.get("timeout_decision", "deny")
    if default not in ("allow", "deny"):
        default = "deny"
    deadline = time.monotonic() + timeout
    while True:
        if reply_path.is_file():
            try:
                data = json.loads(reply_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                data = None
            if isinstance(data, dict) and data.get("decision") in ("allow", "deny"):
                return str(data["decision"]), str(data.get("reason") or ""), "reply"
        if time.monotonic() >= deadline:
            return str(default), "等待回复超时", "timeout"
        time.sleep(min(interval, max(0.0, deadline - time.monotonic())))


def spec_matches(spec: dict, event: dict) -> bool:
    if spec.get("event") != event.get("hook_event_name"):
        return False
    match = spec.get("match") or {}
    if not isinstance(match, dict):
        return False
    if "tool_name" in match and event.get("tool_name") != match["tool_name"]:
        return False
    return True


def render_probe_output(event_name: str, output: dict) -> str:
    """Turn a probe output object into the hook stdout Claude Code reads."""
    if not isinstance(output, dict):
        raise ValueError("probe output must be an object")
    if "hookSpecificOutput" in output:
        body: Dict[str, Any] = output
    elif event_name == "PermissionRequest":
        decision = output.get("decision")
        if isinstance(decision, str):
            decision = {"behavior": decision}
        elif not isinstance(decision, dict):
            decision = {"behavior": output.get("behavior", "allow")}
            if output.get("message"):
                decision["message"] = output["message"]
            if "updatedInput" in output:
                decision["updatedInput"] = output["updatedInput"]
        body = {
            "hookSpecificOutput": {
                "hookEventName": "PermissionRequest",
                "decision": decision,
            }
        }
    elif event_name == "PreToolUse":
        hook: Dict[str, Any] = {"hookEventName": "PreToolUse"}
        for key in (
            "permissionDecision",
            "permissionDecisionReason",
            "updatedInput",
            "additionalContext",
        ):
            if key in output:
                hook[key] = output[key]
        body = {"hookSpecificOutput": hook}
    else:
        hook_keys = (
            "additionalContext",
            "watchPaths",
            "sessionTitle",
            "initialUserMessage",
            "permissionDecision",
            "permissionDecisionReason",
        )
        hook = {"hookEventName": event_name}
        top: Dict[str, Any] = {}
        for key, value in output.items():
            if key in hook_keys:
                hook[key] = value
            else:
                top[key] = value
        if len(hook) > 1:
            top["hookSpecificOutput"] = hook
        body = top
    return json.dumps(body, ensure_ascii=False) + "\n"


def take_probe_stdout(environ: dict, event: dict) -> Optional[str]:
    """Return canned hook stdout for this event, or None when probe is unset.

    CC_BRIDGE_PROBE points at {"responses": [{"event", "match"?, "once"?, "output"}]}.
    A matching response is consumed once unless "once" is false. The default
    hook behaviour is unchanged when the variable is unset.
    """
    raw = environ.get("CC_BRIDGE_PROBE") or ""
    if not raw:
        return None
    path = Path(raw).expanduser()
    if not path.is_file():
        raise FileNotFoundError(str(path))
    data = json.loads(path.read_text(encoding="utf-8"))
    specs = data.get("responses") if isinstance(data, dict) else None
    if not isinstance(specs, list):
        raise ValueError("probe file needs a responses array")
    used_path = Path(str(path) + ".used")
    used_path.parent.mkdir(parents=True, exist_ok=True)
    with used_path.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            handle.seek(0)
            used = {int(part) for part in handle.read().split() if part.strip().isdigit()}
            chosen = None
            chosen_index = None
            for index, spec in enumerate(specs):
                if not isinstance(spec, dict) or index in used:
                    continue
                if spec_matches(spec, event):
                    chosen = spec
                    chosen_index = index
                    break
            if chosen is None:
                return None
            if chosen.get("once", True) and chosen_index is not None:
                handle.write(f"{chosen_index}\n")
                handle.flush()
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    return render_probe_output(str(event.get("hook_event_name") or ""), chosen.get("output") or {})


def decision_stdout(event_name: str, decision: str, reason: str) -> str:
    if event_name == "PreToolUse":
        body = {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": decision,
                "permissionDecisionReason": reason,
            }
        }
        return json.dumps(body, ensure_ascii=False) + "\n"
    if event_name == "PermissionRequest":
        decision_obj: Dict[str, str] = {"behavior": decision}
        if decision == "deny":
            decision_obj["message"] = reason
        body = {
            "hookSpecificOutput": {
                "hookEventName": "PermissionRequest",
                "decision": decision_obj,
            }
        }
        return json.dumps(body, ensure_ascii=False) + "\n"
    return ""


def pending_input(tool_input: dict, limit: int, secrets: List[str]) -> dict:
    view: Dict[str, Any] = {}
    for key, value in tool_input.items():
        if key in ("content", "old_string", "new_string", "command"):
            view[key] = truncate(value, limit, secrets)
        elif key == "edits" and isinstance(value, list):
            view["edit_count"] = len(value)
        elif isinstance(value, str):
            view[key] = truncate(value, limit, secrets)
        else:
            view[key] = value
    return view


def create_worktree(cwd: Path, name: str) -> Tuple[str, str]:
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", str(name or "")).strip("-")[:80] or "worktree"
    dest = (cwd / ".claude" / "worktrees" / slug).resolve()
    if dest.is_dir() and any(dest.iterdir()):
        return str(dest), ""
    dest.parent.mkdir(parents=True, exist_ok=True)
    branch = f"cc-bridge/{slug}"
    attempts = [
        ["git", "-C", str(cwd), "worktree", "add", "-b", branch, str(dest), "HEAD"],
        ["git", "-C", str(cwd), "worktree", "add", "--detach", str(dest), "HEAD"],
    ]
    errors = []
    for cmd in attempts:
        try:
            completed = subprocess.run(
                cmd, capture_output=True, text=True, timeout=30, check=False
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            errors.append(str(exc))
            continue
        if completed.returncode == 0 and dest.is_dir():
            return str(dest), ""
        errors.append((completed.stderr or completed.stdout or "git worktree add failed").strip())
    return "", "; ".join(errors) or "git worktree add failed"


def remove_worktree(cwd: Path, raw_path: str) -> Tuple[int, str]:
    if not raw_path:
        return 1, "WorktreeRemove 缺少 worktree_path\n"
    path = Path(raw_path)
    if not path.is_absolute():
        path = (cwd / path).resolve()
    else:
        path = path.resolve()
    parts = path.parts
    if ".claude" not in parts or "worktrees" not in parts:
        return 1, "拒绝删除 .claude/worktrees 以外的路径\n"
    if not path.exists():
        return 0, ""
    try:
        completed = subprocess.run(
            ["git", "-C", str(cwd), "worktree", "remove", "--force", str(path)],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 1, f"{exc}\n"
    if completed.returncode != 0 and path.exists():
        message = (completed.stderr or completed.stdout or "git worktree remove failed").strip()
        return 1, message + "\n"
    return 0, ""


def handle_event(
    event: dict,
    environ: Optional[dict] = None,
    rules: Optional[List[dict]] = None,
    cfg: Optional[dict] = None,
) -> HookResult:
    environ = dict(os.environ if environ is None else environ)
    if not isinstance(event, dict):
        return HookResult(stderr="cc-cursor-bridge: stdin 不是 JSON 对象\n")
    root = plugin_root_from(environ)
    notes = []
    if rules is None:
        rules, note = load_rules(root, environ)
        if note:
            notes.append(note)
    if cfg is None:
        cfg, note = load_config(root, environ)
        if note:
            notes.append(note)
    limit = int((cfg.get("log") or {}).get("max_field_chars") or 240)
    secrets = secret_values(environ)
    event_name = str(event.get("hook_event_name") or "unknown")
    cwd = Path(event.get("cwd") or environ.get("PWD") or ".")
    home = home_dir(environ)
    tool_input = event.get("tool_input") if isinstance(event.get("tool_input"), dict) else {}
    command = tool_input.get("command") if isinstance(tool_input.get("command"), str) else ""
    tool_name = event.get("tool_name") if isinstance(event.get("tool_name"), str) else ""

    stdout = ""
    code = 0
    stderr = ""
    decision = ""
    reason = ""
    rule_id = ""
    pending_id = ""
    summary = summarize(event, limit, secrets)
    if notes:
        summary["config_note"] = truncate(" ".join(notes), limit, secrets)

    if event_name == "WorktreeCreate":
        path, error = create_worktree(cwd, str(event.get("name") or "worktree"))
        if path:
            summary["worktree_path"] = path
            stdout = path + "\n"
        else:
            summary["worktree_error"] = truncate(error, limit, secrets)
            stderr = (error or "git worktree add failed") + "\n"
            code = 1
    elif event_name == "WorktreeRemove":
        code, stderr = remove_worktree(cwd, str(event.get("worktree_path") or ""))
        if stderr:
            summary["worktree_error"] = truncate(stderr.strip(), limit, secrets)

    ctx = {
        "tool_name": tool_name,
        "tool_input": tool_input,
        "command": command,
        "texts": texts_for_rules(tool_name, tool_input, command),
        "cwd": cwd,
        "home": home,
        "rule_errors": [],
    }
    if event_name == "PreToolUse":
        matched = first_deny(rules, ctx)
        if matched:
            decision = "deny"
            reason = str(matched.get("reason") or matched.get("id") or "denied")
            rule_id = str(matched.get("id") or "")
            stdout = decision_stdout(event_name, decision, reason)
    if ctx["rule_errors"]:
        summary["rule_errors"] = truncate("; ".join(ctx["rule_errors"]), limit, secrets)

    probe_stdout = None
    try:
        probe_stdout = take_probe_stdout(environ, event)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        summary["probe_error"] = truncate(str(exc), limit, secrets)
    if probe_stdout is not None and event_name not in ("WorktreeCreate", "WorktreeRemove"):
        stdout = probe_stdout
        decision = "probe"
        reason = "CC_BRIDGE_PROBE"
        summary["probe"] = "applied"
    elif probe_stdout is not None:
        summary["probe"] = "ignored_for_worktree_protocol"

    wait = wait_settings(cfg, environ)
    if probe_stdout is None and should_wait(event_name, wait, decision == "deny"):
        pending_id = safe_file_id(str(event.get("tool_use_id") or ""))
        directory = task_dir_for(event, environ)
        pending_body = {
            "id": pending_id,
            "event": event_name,
            "session_id": event.get("session_id") or "",
            "tool_name": tool_name,
            "tool_input": pending_input(tool_input, max(limit, 400), secrets),
            "created_at": local_iso(),
        }
        decision, reason, via = wait_for_reply(directory, pending_id, pending_body, wait)
        summary["wait"] = via
        stdout = decision_stdout(event_name, decision, reason)

    record: Dict[str, Any] = {
        "ts": local_iso(),
        "event": event_name,
        "session_id": event.get("session_id") or "",
        "cwd": str(event.get("cwd") or ""),
        "input_keys": sorted(str(key) for key in event.keys()),
        "summary": summary,
    }
    if event.get("prompt_id"):
        record["prompt_id"] = str(event["prompt_id"])[:80]
    if decision:
        record["decision"] = decision
        record["decision_reason"] = truncate(reason, limit, secrets)
    if rule_id:
        record["rule_id"] = rule_id
    if pending_id:
        record["pending_id"] = pending_id

    directory = task_dir_for(event, environ)
    log_path = directory / "events.jsonl"

    def before_write(item: dict) -> None:
        try:
            item["notify"] = maybe_notify(cfg, item, directory, environ)
        except OSError as exc:
            item["notify"] = f"error:{exc}"

    try:
        append_record(log_path, record, before_write)
    except OSError as exc:
        stderr += f"cc-cursor-bridge: 写事件日志失败: {exc}\n"
    return HookResult(stdout=stdout, stderr=stderr, code=code)


def main() -> HookResult:
    raw = sys.stdin.read()
    if not raw.strip():
        return HookResult()
    try:
        event = json.loads(raw)
    except json.JSONDecodeError as exc:
        return HookResult(stderr=f"cc-cursor-bridge: 无法解析 stdin JSON: {exc}\n")
    return handle_event(event)
