"""cc-cursor-bridge hook dispatcher.

Logs every Claude Code hook event, denies dangerous tool calls, and routes
each event to wait, push, or log. No third-party packages and no network calls.
"""

from __future__ import annotations

import fcntl
import hashlib
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


MODES = ("wait", "push", "log", "off")
# SessionStart stays synchronous so CC_BRIDGE_WATCH can inject watchPaths.
# WorktreeCreate stays synchronous because it must print the worktree path.
SYNC_WITHOUT_WAIT = ("SessionStart", "WorktreeCreate")
DEFAULT_EVENT_MODES = {
    "SessionStart": "push",
    "Setup": "log",
    "InstructionsLoaded": "log",
    "UserPromptSubmit": "push",
    "UserPromptExpansion": "push",
    "MessageDisplay": "log",
    "PreToolUse": "wait",
    "PermissionRequest": "wait",
    "PermissionDenied": "log",
    "PostToolUse": "push",
    "PostToolUseFailure": "push",
    "PostToolBatch": "log",
    "Notification": "log",
    "SubagentStart": "log",
    "SubagentStop": "push",
    "TaskCreated": "push",
    "TaskCompleted": "push",
    "Stop": "wait",
    "StopFailure": "push",
    "TeammateIdle": "push",
    "ConfigChange": "log",
    "CwdChanged": "log",
    "DirectoryAdded": "log",
    "FileChanged": "log",
    "WorktreeCreate": "push",
    "WorktreeRemove": "log",
    "PreCompact": "push",
    "PostCompact": "log",
    "PreModelSwitch": "push",
    "PostModelSwitch": "log",
    "Elicitation": "wait",
    "ElicitationResult": "log",
    "SessionEnd": "log",
}


def hook_runs_async(event_name: str) -> bool:
    if event_name in SYNC_WITHOUT_WAIT:
        return False
    return DEFAULT_EVENT_MODES.get(event_name) in ("push", "log")


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
        "assistant_text": {"enabled": False},
        "wait_all": {
            "enabled": False,
            "timeout_seconds": 120,
            "poll_interval_seconds": 0.25,
        },
        "pending": {
            "command_chars": 4000,
            "content_chars": 2000,
        },
        "stop": {"max_blocks": 3},
        "events": dict(DEFAULT_EVENT_MODES),
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
    for key in ("notifier", "wait", "wait_all", "log", "assistant_text", "pending", "stop", "events"):
        if isinstance(data.get(key), dict):
            cfg[key].update(data[key])
    return cfg, ""


def env_flag(environ: dict, name: str, default: bool) -> bool:
    raw = environ.get(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in TRUTHY


def assistant_text_enabled(cfg: dict, environ: dict) -> bool:
    block = cfg.get("assistant_text") or {}
    return env_flag(environ, "CC_BRIDGE_ASSISTANT_TEXT", bool(block.get("enabled")))


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
    if event.get("hook_event_name") in ("Stop", "SubagentStop"):
        for key in (
            "last_assistant_message",
            "transcript_path",
            "agent_transcript_path",
            "agent_type",
            "agent_id",
        ):
            if key in event and event[key] not in (None, "") and key not in summary:
                summary[key] = truncate(event[key], limit, secrets)
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
            if not record.get("seq"):
                record["seq"] = len(existing) + 1
            if before_write is not None:
                before_write(record)
            handle.seek(0, os.SEEK_END)
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
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


def wait_all_settings(cfg: dict, environ: dict) -> dict:
    block = dict((cfg.get("wait_all") or {}))
    block["enabled"] = env_flag(environ, "CC_BRIDGE_WAIT_ALL", bool(block.get("enabled")))
    if environ.get("CC_BRIDGE_WAIT_ALL_TIMEOUT"):
        block["timeout_seconds"] = float(environ["CC_BRIDGE_WAIT_ALL_TIMEOUT"])
    return block


def resolve_event_mode(event_name: str, cfg: dict, environ: dict) -> str:
    """wait_all forces every event to wait. Otherwise env, then config, then the built-in plan."""
    if wait_all_settings(cfg, environ).get("enabled"):
        return "wait"
    raw = str(environ.get(f"CC_BRIDGE_EVENT_MODE_{event_name}") or "").strip().lower()
    if raw in MODES:
        return raw
    configured = (cfg.get("events") or {}).get(event_name)
    if isinstance(configured, str) and configured.strip().lower() in MODES:
        return configured.strip().lower()
    return DEFAULT_EVENT_MODES.get(event_name, "log")


def stop_block_limit(cfg: dict, environ: dict) -> int:
    raw = str(environ.get("CC_BRIDGE_STOP_MAX_BLOCKS") or "").strip()
    if raw.isdigit():
        return int(raw)
    try:
        return max(0, int((cfg.get("stop") or {}).get("max_blocks", 3)))
    except (TypeError, ValueError):
        return 3


def read_stop_blocks(task_dir: Path) -> int:
    path = task_dir / "stop-blocks"
    try:
        raw = path.read_text(encoding="utf-8").strip()
    except OSError:
        return 0
    return int(raw) if raw.isdigit() else 0


def increment_stop_blocks(task_dir: Path) -> int:
    task_dir.mkdir(parents=True, exist_ok=True)
    path = task_dir / "stop-blocks"
    with path.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            handle.seek(0)
            raw = handle.read().strip()
            current = int(raw) if raw.isdigit() else 0
            nxt = current + 1
            handle.seek(0)
            handle.truncate()
            handle.write(str(nxt))
            handle.flush()
            os.fsync(handle.fileno())
            return nxt
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.parent / (path.name + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(path)


def reserve_seq(task_dir: Path) -> int:
    task_dir.mkdir(parents=True, exist_ok=True)
    path = task_dir / "seq"
    with path.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            handle.seek(0)
            raw = handle.read().strip()
            current = int(raw) if raw.isdigit() else 0
            nxt = current + 1
            handle.seek(0)
            handle.truncate()
            handle.write(str(nxt))
            handle.flush()
            os.fsync(handle.fileno())
            return nxt
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


REPLY_HINTS = {
    "PreToolUse": "decision allow|deny|ask|defer, optional reason, updatedInput, additionalContext",
    "PermissionRequest": "decision allow|deny, optional message/reason, updatedInput",
    "UserPromptSubmit": "decision block and/or additionalContext",
    "UserPromptExpansion": "decision block and/or additionalContext",
    "Stop": "decision block plus reason, or ack",
    "SubagentStop": "decision block plus reason, or ack",
    "PreCompact": "decision block plus reason, or ack",
    "PreModelSwitch": "decision allow|deny|ask plus reason, or ack",
    "PostToolUse": "additionalContext and/or decision block, or ack",
    "Elicitation": "decision accept|decline, optional content",
    "ElicitationResult": "decision accept|decline",
    "WorktreeCreate": "worktreePath absolute path, or ack to keep the git worktree this hook created",
    "MessageDisplay": "displayContent, or ack",
    "SessionStart": "additionalContext and/or watchPaths, or ack",
    "TeammateIdle": "decision block plus reason, or ack",
}


def reply_hint(event_name: str) -> str:
    return REPLY_HINTS.get(event_name, "ack true; this event has no decision control")


def watch_paths_from_env(environ: dict) -> List[str]:
    raw = environ.get("CC_BRIDGE_WATCH") or ""
    return [part for part in raw.split(os.pathsep) if part]


def poll_reply_file(path: Path, timeout: float, interval: float) -> Tuple[Optional[dict], str]:
    deadline = time.monotonic() + max(0.0, timeout)
    while True:
        if path.is_file():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                data = None
            if isinstance(data, dict):
                return data, "reply"
        if time.monotonic() >= deadline:
            return None, "timeout"
        time.sleep(min(interval, max(0.0, deadline - time.monotonic())))


def timeout_result(event_name: str) -> Tuple[str, int, str, str]:
    if event_name == "PreToolUse":
        return decision_stdout(event_name, "deny", "等待回复超时"), 0, "", "timeout-deny"
    if event_name == "PermissionRequest":
        body = {
            "hookSpecificOutput": {
                "hookEventName": "PermissionRequest",
                "decision": {"behavior": "deny", "message": "等待回复超时"},
            }
        }
        return json.dumps(body, ensure_ascii=False) + "\n", 0, "", "timeout-deny"
    if event_name == "Elicitation":
        body = {"hookSpecificOutput": {"hookEventName": "Elicitation", "action": "decline"}}
        return json.dumps(body, ensure_ascii=False) + "\n", 0, "", "timeout-decline"
    if event_name == "Stop":
        return "", 0, "", "timeout-allow-stop"
    return "", 0, "", "timeout-noop"


def apply_stop_reply(
    event: dict, reply: Optional[dict], task_dir: Path, cfg: dict, environ: dict
) -> Tuple[str, int, str, str, str]:
    """Block only on an explicit decision. stop_hook_active without one lets the turn end.

    A task can be blocked at most stop.max_blocks times (default 3). After that, let it stop.
    """
    active = bool(event.get("stop_hook_active"))
    explicit = isinstance(reply, dict) and str(reply.get("decision") or "").lower() == "block"
    reason = str((reply or {}).get("reason") or (reply or {}).get("message") or "")
    if not explicit:
        label = "stop-hook-active" if active else "ack"
        return "", 0, "", label, ""
    limit = stop_block_limit(cfg, environ)
    used = read_stop_blocks(task_dir)
    if used >= limit:
        return "", 0, "", "stop-block-cap", f"已阻断 {used} 次，达到上限 {limit}，放行结束"
    increment_stop_blocks(task_dir)
    answered, code, extra, decision = apply_reply("Stop", reply or {})
    return answered, code, extra, decision, reason or "blocked"


def apply_reply(event_name: str, reply: dict) -> Tuple[str, int, str, str]:
    decision = str(reply.get("decision") or reply.get("behavior") or "").lower()
    reason = str(reply.get("reason") or reply.get("message") or "")
    if event_name == "PreToolUse" and decision in ("allow", "deny", "ask", "defer"):
        hook: Dict[str, Any] = {
            "hookEventName": "PreToolUse",
            "permissionDecision": decision,
            "permissionDecisionReason": reason,
        }
        if "updatedInput" in reply:
            hook["updatedInput"] = reply["updatedInput"]
        if reply.get("additionalContext"):
            hook["additionalContext"] = reply["additionalContext"]
        return json.dumps({"hookSpecificOutput": hook}, ensure_ascii=False) + "\n", 0, "", decision
    if event_name == "PermissionRequest" and decision in ("allow", "deny"):
        decided: Dict[str, Any] = {"behavior": decision}
        if decision == "deny" and reason:
            decided["message"] = reason
        if "updatedInput" in reply:
            decided["updatedInput"] = reply["updatedInput"]
        body = {"hookSpecificOutput": {"hookEventName": "PermissionRequest", "decision": decided}}
        return json.dumps(body, ensure_ascii=False) + "\n", 0, "", decision
    if event_name == "PreModelSwitch" and decision in ("allow", "deny", "ask"):
        hook = {
            "hookEventName": "PreModelSwitch",
            "permissionDecision": decision,
            "permissionDecisionReason": reason,
        }
        return json.dumps({"hookSpecificOutput": hook}, ensure_ascii=False) + "\n", 0, "", decision
    if event_name in ("Elicitation", "ElicitationResult") and decision in ("accept", "decline", "allow", "deny"):
        action = "accept" if decision in ("accept", "allow") else "decline"
        hook = {"hookEventName": event_name, "action": action}
        if "content" in reply:
            hook["content"] = reply["content"]
        return json.dumps({"hookSpecificOutput": hook}, ensure_ascii=False) + "\n", 0, "", action
    if event_name == "WorktreeCreate" and reply.get("worktreePath"):
        return str(reply["worktreePath"]).rstrip() + "\n", 0, "", "path"
    if event_name == "MessageDisplay" and reply.get("displayContent") is not None:
        hook = {"hookEventName": "MessageDisplay", "displayContent": reply["displayContent"]}
        return json.dumps({"hookSpecificOutput": hook}, ensure_ascii=False) + "\n", 0, "", "display"
    block_events = (
        "Stop",
        "SubagentStop",
        "PreCompact",
        "UserPromptSubmit",
        "UserPromptExpansion",
        "PostToolUse",
        "PostToolUseFailure",
        "TeammateIdle",
        "TaskCreated",
        "TaskCompleted",
    )
    body: Dict[str, Any] = {}
    label = "ack"
    if event_name in block_events and decision == "block":
        body["decision"] = "block"
        body["reason"] = reason or "blocked"
        label = "block"
    context = reply.get("additionalContext")
    watch = reply.get("watchPaths")
    if event_name == "SessionStart" and (context or watch):
        hook = {"hookEventName": "SessionStart"}
        if context:
            hook["additionalContext"] = context
        if watch:
            hook["watchPaths"] = watch
        body["hookSpecificOutput"] = hook
        label = "session"
    elif context and event_name in block_events + ("SessionStart", "PostToolUse", "PostToolUseFailure"):
        body["hookSpecificOutput"] = {"hookEventName": event_name, "additionalContext": context}
        if label == "ack":
            label = "context"
    if body:
        return json.dumps(body, ensure_ascii=False) + "\n", 0, "", label
    return "", 0, "", "ack"


def inject_watch_paths(stdout: str, paths: List[str]) -> str:
    if not paths:
        return stdout
    if not stdout.strip():
        body = {"hookSpecificOutput": {"hookEventName": "SessionStart", "watchPaths": paths}}
        return json.dumps(body) + "\n"
    try:
        parsed = json.loads(stdout)
    except json.JSONDecodeError:
        return stdout
    hook = parsed.get("hookSpecificOutput")
    if not isinstance(hook, dict):
        hook = {"hookEventName": "SessionStart"}
        parsed["hookSpecificOutput"] = hook
    existing = hook.get("watchPaths") if isinstance(hook.get("watchPaths"), list) else []
    merged = list(existing)
    for path in paths:
        if path not in merged:
            merged.append(path)
    hook["watchPaths"] = merged
    return json.dumps(parsed) + "\n"


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


def pending_limits(cfg: dict) -> Tuple[int, int]:
    pending = cfg.get("pending") or {}
    return int(pending.get("command_chars") or 4000), int(pending.get("content_chars") or 2000)


def bounded_field(value: Any, limit: int, secrets: List[str], full: bool) -> Any:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    text = redact(text, secrets)
    if full or len(text) <= limit:
        return text
    return {
        "text": text[:limit] + "…[truncated]",
        "truncated": True,
        "original_length": len(text),
        "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
    }


def pending_tool_view(tool_input: dict, cfg: dict, secrets: List[str], full: bool = False) -> Tuple[dict, bool]:
    command_limit, content_limit = pending_limits(cfg)
    view: Dict[str, Any] = {}
    truncated = False

    def put_string(key: str, value: str) -> Any:
        nonlocal truncated
        limit = command_limit if key == "command" else content_limit
        shown = bounded_field(value, limit, secrets, full)
        if isinstance(shown, dict) and shown.get("truncated"):
            truncated = True
        return shown

    for key, value in tool_input.items():
        if key == "edits" and isinstance(value, list):
            shown_edits = []
            for item in value:
                if isinstance(item, dict):
                    shown_edits.append(
                        {
                            edit_key: put_string(edit_key, edit_value) if isinstance(edit_value, str) else edit_value
                            for edit_key, edit_value in item.items()
                        }
                    )
                else:
                    shown_edits.append(item)
            view["edits"] = shown_edits
            continue
        if isinstance(value, str):
            view[key] = put_string(key, value)
        else:
            view[key] = value
    return view, truncated


def bounded_pair(value: Any, limit: int, secrets: List[str]) -> Tuple[Any, Any, bool]:
    short = bounded_field(value, limit, secrets, full=False)
    full = bounded_field(value, limit, secrets, full=True)
    return short, full, isinstance(short, dict) and bool(short.get("truncated"))


def notice_extra_fields(
    event_name: str,
    event: dict,
    cfg: dict,
    secrets: List[str],
    assistant_text: bool = False,
) -> Tuple[dict, dict, bool]:
    """Reply and tool-result fields that the compact summary would lose."""
    _, content_limit = pending_limits(cfg)
    log_limit = int((cfg.get("log") or {}).get("max_field_chars") or 240)
    short: Dict[str, Any] = {}
    full: Dict[str, Any] = {}
    truncated = False

    def add(key: str, value: Any, limit: int) -> None:
        nonlocal truncated
        shown, whole, was_truncated = bounded_pair(value, limit, secrets)
        short[key] = shown
        full[key] = whole
        truncated = truncated or was_truncated

    if event_name == "MessageDisplay" and assistant_text:
        delta = event.get("delta")
        if isinstance(delta, str) and delta:
            add("assistant_text", delta, log_limit)
        transcript = event.get("transcript_path")
        if isinstance(transcript, str) and transcript:
            add("transcript_path", transcript, log_limit)
        for key in ("message_id", "turn_id"):
            value = event.get(key)
            if isinstance(value, str) and value:
                short[key] = truncate(value, log_limit, secrets)
        if "final" in event:
            short["final"] = bool(event.get("final"))
        index = event.get("index")
        if isinstance(index, int) and not isinstance(index, bool):
            short["index"] = index
    if event_name in ("Stop", "SubagentStop"):
        message = event.get("last_assistant_message")
        if isinstance(message, str) and message:
            add("last_assistant_message", message, content_limit)
        transcript = event.get("transcript_path")
        if isinstance(transcript, str) and transcript:
            add("transcript_path", transcript, content_limit)
        if event_name == "SubagentStop":
            for key in ("agent_transcript_path", "agent_type", "agent_id"):
                value = event.get(key)
                if isinstance(value, str) and value:
                    add(key, value, content_limit)
    if event_name in ("PostToolUse", "PostToolUseFailure"):
        response = event.get("tool_response")
        if response not in (None, ""):
            add("tool_response", response, log_limit)
        error = event.get("error")
        if error not in (None, ""):
            add("error", error, log_limit)
        if "is_interrupt" in event:
            short["is_interrupt"] = bool(event.get("is_interrupt"))
    return short, full, truncated


def notice_body(
    seq: int,
    event_name: str,
    event: dict,
    tool_name: str,
    tool_input: dict,
    summary: dict,
    cfg: dict,
    secrets: List[str],
    stdout: str,
    assistant_text: bool = False,
) -> Tuple[dict, dict, bool]:
    short_input, input_truncated = pending_tool_view(tool_input, cfg, secrets, full=False)
    full_input, _ = pending_tool_view(tool_input, cfg, secrets, full=True)
    extra_short, extra_full, extra_truncated = notice_extra_fields(
        event_name, event, cfg, secrets, assistant_text
    )
    body = {
        "seq": seq,
        "event": event_name,
        "session_id": event.get("session_id") or "",
        "cwd": event.get("cwd") or "",
        "tool_name": tool_name,
        "tool_input": short_input,
        "summary": summary,
        "input_keys": sorted(str(key) for key in event.keys()),
        "reply_hint": reply_hint(event_name),
        "created_at": local_iso(),
    }
    body.update(extra_short)
    if "stop_hook_active" in event:
        body["stop_hook_active"] = bool(event.get("stop_hook_active"))
    was_truncated = input_truncated or extra_truncated
    if was_truncated:
        body["truncated"] = True
    if event_name == "WorktreeCreate" and stdout.strip():
        body["worktreePath"] = stdout.strip()
    full_body = dict(body)
    full_body["tool_input"] = full_input
    full_body.update(extra_full)
    full_body["truncated"] = False
    return body, full_body, was_truncated


def write_notice(
    directory: Path,
    kind: str,
    seq: int,
    event_name: str,
    body: dict,
    full_body: dict,
    was_truncated: bool,
) -> None:
    payload = dict(body)
    if was_truncated:
        payload["full_file"] = f"{kind}/{seq}-{event_name}.full.json"
    path = directory / kind / f"{seq}-{event_name}.json"
    write_atomic(path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    if was_truncated:
        full_payload = dict(full_body)
        full_payload["full_file"] = payload["full_file"]
        full_path = directory / kind / f"{seq}-{event_name}.full.json"
        write_atomic(full_path, json.dumps(full_payload, ensure_ascii=False, indent=2) + "\n")
    if kind == "push":
        stream = directory / "push" / "stream.jsonl"
        stream.parent.mkdir(parents=True, exist_ok=True)
        with stream.open("a", encoding="utf-8") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


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

    mode = resolve_event_mode(event_name, cfg, environ)
    assistant_text = assistant_text_enabled(cfg, environ)
    if event_name == "MessageDisplay" and assistant_text:
        summary["assistant_text"] = "enabled"
        if mode == "log":
            mode = "push"
    watch = watch_paths_from_env(environ)
    wait_all = wait_all_settings(cfg, environ)
    if event_name == "SessionStart" and watch and mode == "wait" and not wait_all.get("enabled"):
        mode = "push"
        summary["session_start"] = "sync-without-wait"
    if mode == "off" and event_name not in ("PreToolUse", "WorktreeCreate", "WorktreeRemove", "SessionStart"):
        return HookResult()

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
            # A matching safety rule always denies. wait_all and probe replies cannot override it.
            rule_id = str(matched.get("id") or "")
            decision = "deny"
            reason = str(matched.get("reason") or rule_id or "denied")
            stdout = decision_stdout(event_name, decision, reason)
    if ctx["rule_errors"]:
        summary["rule_errors"] = truncate("; ".join(ctx["rule_errors"]), limit, secrets)

    probe_stdout = None
    try:
        probe_stdout = take_probe_stdout(environ, event)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        summary["probe_error"] = truncate(str(exc), limit, secrets)
    if rule_id and probe_stdout is not None:
        summary["probe"] = "ignored_for_rule_deny"
    elif probe_stdout is not None and event_name not in ("WorktreeCreate", "WorktreeRemove"):
        stdout = probe_stdout
        decision = "probe"
        reason = "CC_BRIDGE_PROBE"
        summary["probe"] = "applied"
    elif probe_stdout is not None:
        summary["probe"] = "ignored_for_worktree_protocol"

    if mode == "off" and event_name == "PreToolUse" and not rule_id and decision != "probe":
        return HookResult()
    if mode == "off" and event_name == "SessionStart" and not watch and decision != "probe":
        return HookResult()

    directory = task_dir_for(event, environ)
    seq = reserve_seq(directory)
    pending_id = ""
    push_id = ""
    summary["channel"] = mode
    blocked_by_probe = probe_stdout is not None and event_name not in ("WorktreeCreate", "WorktreeRemove")
    if not rule_id and not blocked_by_probe and mode in ("wait", "push"):
        body, full_body, was_truncated = notice_body(
            seq,
            event_name,
            event,
            tool_name,
            tool_input,
            summary,
            cfg,
            secrets,
            stdout,
            assistant_text=assistant_text,
        )
        if mode == "wait":
            body["reply_file"] = f"replies/{seq}.json"
            pending_id = str(seq)
            write_notice(directory, "pending", seq, event_name, body, full_body, was_truncated)
            reply, via = poll_reply_file(
                directory / "replies" / f"{seq}.json",
                float(wait_all.get("timeout_seconds", 120)),
                float(wait_all.get("poll_interval_seconds", 0.25)),
            )
            if wait_all.get("enabled"):
                summary["wait_all"] = via
            summary["delivery"] = via
            if reply is None:
                timed_out, code, extra, decision = timeout_result(event_name)
                keep_worktree = event_name == "WorktreeCreate" and stdout.strip() and decision in (
                    "timeout-noop",
                    "timeout-allow-stop",
                )
                if not keep_worktree:
                    stdout = timed_out
                if extra:
                    stderr += extra
                if decision in ("timeout-deny", "timeout-decline"):
                    reason = "等待回复超时"
            elif event_name == "Stop":
                answered, code, extra, decision, reason = apply_stop_reply(event, reply, directory, cfg, environ)
                if extra:
                    stderr += extra
                stdout = answered
            else:
                answered, code, extra, decision = apply_reply(event_name, reply)
                if extra:
                    stderr += extra
                if not (event_name == "WorktreeCreate" and decision != "path"):
                    stdout = answered
                reason = str(reply.get("reason") or reply.get("message") or "")
        else:
            push_id = str(seq)
            write_notice(directory, "push", seq, event_name, body, full_body, was_truncated)
            summary["delivery"] = "push"
    if rule_id:
        summary["delivery"] = "rule"

    wait = wait_settings(cfg, environ)
    if (
        probe_stdout is None
        and mode == "log"
        and not rule_id
        and should_wait(event_name, wait, decision == "deny")
    ):
        pending_id = safe_file_id(str(event.get("tool_use_id") or ""))
        legacy = {
            "id": pending_id,
            "event": event_name,
            "session_id": event.get("session_id") or "",
            "tool_name": tool_name,
            "tool_input": pending_input(tool_input, max(limit, 400), secrets),
            "created_at": local_iso(),
        }
        decision, reason, via = wait_for_reply(directory, pending_id, legacy, wait)
        summary["wait"] = via
        summary["delivery"] = via
        stdout = decision_stdout(event_name, decision, reason)

    if "delivery" not in summary:
        summary["delivery"] = mode
    record: Dict[str, Any] = {
        "ts": local_iso(),
        "event": event_name,
        "channel": mode,
        "session_id": event.get("session_id") or "",
        "cwd": str(event.get("cwd") or ""),
        "input_keys": sorted(str(key) for key in event.keys()),
        "summary": summary,
        "seq": seq,
    }
    if event_name == "SessionStart":
        stdout = inject_watch_paths(stdout, watch)
    if event.get("prompt_id"):
        record["prompt_id"] = str(event["prompt_id"])[:80]
    if decision:
        record["decision"] = decision
        record["decision_reason"] = truncate(reason, limit, secrets)
    if rule_id:
        record["rule_id"] = rule_id
    if pending_id:
        record["pending_id"] = pending_id
    if push_id:
        record["push_id"] = push_id

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


def bridge_root_from_env() -> Path:
    return Path(os.environ.get("CC_BRIDGE_DIR") or (Path.home() / ".cc-bridge")).expanduser()


USAGE = (
    "usage: bridge.py pending <task> | reply <task> <seq> <json> "
    "| push <task> [--since <seq>] | watch <task> [--once | --for <seconds>]\n"
)


def task_root(task: str) -> Path:
    return bridge_root_from_env() / "tasks" / task


def short_notices(directory: Path) -> List[Path]:
    if not directory.is_dir():
        return []
    return sorted(path for path in directory.glob("*.json") if not path.name.endswith(".full.json"))


def notice_seq(path: Path) -> int:
    head = path.name.split("-", 1)[0]
    return int(head) if head.isdigit() else 0


def field_text(value: Any) -> str:
    if isinstance(value, dict):
        text = value.get("text")
        return text if isinstance(text, str) else ""
    return value if isinstance(value, str) else ""


def first_line(text: str, limit: int = 160) -> str:
    for raw in text.splitlines():
        line = raw.strip()
        if line:
            return line[:limit] + ("…" if len(line) > limit else "")
    return ""


def notice_annotation(path: Path) -> str:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ""
    if not isinstance(data, dict):
        return ""
    event_name = str(data.get("event") or "")
    if event_name in ("Stop", "SubagentStop"):
        line = first_line(field_text(data.get("last_assistant_message")))
        return f"reply: {line}" if line else ""
    if event_name == "MessageDisplay":
        line = first_line(field_text(data.get("assistant_text")))
        return f"assistant: {line}" if line else ""
    if event_name == "PostToolUse":
        line = first_line(field_text(data.get("tool_response")))
        return f"response: {line}" if line else ""
    if event_name == "PostToolUseFailure":
        line = first_line(field_text(data.get("error")))
        return f"error: {line}" if line else ""
    return ""


def notice_line(path: Path, kind: str = "") -> str:
    annotation = notice_annotation(path)
    line = f"{kind} {path}" if kind else str(path)
    return line + (f"\t# {annotation}" if annotation else "")


def read_push_cursor(task_dir: Path) -> int:
    path = task_dir / "push" / "cursor"
    try:
        raw = path.read_text(encoding="utf-8").strip()
    except OSError:
        return 0
    return int(raw) if raw.isdigit() else 0


def write_push_cursor(task_dir: Path, seq: int) -> None:
    write_atomic(task_dir / "push" / "cursor", str(seq) + "\n")


def cli(argv: List[str]) -> HookResult:
    if not argv or argv[0] not in ("pending", "reply", "push", "watch"):
        return HookResult(stderr=USAGE, code=2)
    if argv[0] == "pending":
        if len(argv) != 2:
            return HookResult(stderr="usage: bridge.py pending <task>\n", code=2)
        task = safe_component(argv[1]) or ""
        if not task:
            return HookResult(stderr="bad task id\n", code=2)
        root = task_root(task)
        lines = []
        for path in short_notices(root / "pending"):
            seq = path.name.split("-", 1)[0]
            if not (root / "replies" / f"{seq}.json").is_file():
                lines.append(notice_line(path))
        return HookResult(stdout=("\n".join(lines) + ("\n" if lines else "")))
    if argv[0] == "push":
        return push_command(argv[1:])
    if argv[0] == "watch":
        return watch_command(argv[1:])
    if len(argv) != 4:
        return HookResult(stderr="usage: bridge.py reply <task> <seq> <json>\n", code=2)
    task = safe_component(argv[1]) or ""
    if not task or not argv[2].isdigit():
        return HookResult(stderr="bad task id or seq\n", code=2)
    try:
        payload = json.loads(argv[3])
    except json.JSONDecodeError as exc:
        return HookResult(stderr=f"reply is not JSON: {exc}\n", code=2)
    if not isinstance(payload, dict):
        return HookResult(stderr="reply JSON must be an object\n", code=2)
    dest = task_root(task) / "replies" / f"{argv[2]}.json"
    dest.parent.mkdir(parents=True, exist_ok=True)
    temporary = dest.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False) + "\n", encoding="utf-8")
    temporary.replace(dest)
    return HookResult(stdout=str(dest) + "\n")


def push_command(argv: List[str]) -> HookResult:
    since: Optional[int] = None
    task = ""
    index = 0
    while index < len(argv):
        item = argv[index]
        if item == "--since":
            if index + 1 >= len(argv) or not argv[index + 1].isdigit():
                return HookResult(stderr="usage: bridge.py push <task> [--since <seq>]\n", code=2)
            since = int(argv[index + 1])
            index += 2
            continue
        if item.startswith("-") or task:
            return HookResult(stderr="usage: bridge.py push <task> [--since <seq>]\n", code=2)
        task = item
        index += 1
    task = safe_component(task) or ""
    if not task:
        return HookResult(stderr="usage: bridge.py push <task> [--since <seq>]\n", code=2)
    root = task_root(task)
    cursor = read_push_cursor(root)
    floor = since if since is not None else cursor
    chosen = [path for path in short_notices(root / "push") if notice_seq(path) > floor]
    if chosen:
        write_push_cursor(root, max(cursor, max(notice_seq(path) for path in chosen)))
    text = "\n".join(notice_line(path) for path in chosen)
    return HookResult(stdout=text + ("\n" if text else ""))


def watch_command(argv: List[str]) -> HookResult:
    once = False
    seconds: Optional[float] = None
    task = ""
    index = 0
    while index < len(argv):
        item = argv[index]
        if item == "--once":
            once = True
            index += 1
            continue
        if item == "--for":
            if index + 1 >= len(argv):
                return HookResult(stderr="usage: bridge.py watch <task> [--once | --for <seconds>]\n", code=2)
            try:
                seconds = float(argv[index + 1])
            except ValueError:
                return HookResult(stderr="usage: bridge.py watch <task> [--once | --for <seconds>]\n", code=2)
            index += 2
            continue
        if item.startswith("-") or task:
            return HookResult(stderr="usage: bridge.py watch <task> [--once | --for <seconds>]\n", code=2)
        task = item
        index += 1
    task = safe_component(task) or ""
    if not task:
        return HookResult(stderr="usage: bridge.py watch <task> [--once | --for <seconds>]\n", code=2)
    root = task_root(task)
    seen = set()
    deadline = None if seconds is None else time.monotonic() + max(0.0, seconds)
    lines: List[str] = []

    def take() -> None:
        for kind, folder in (("pending", root / "pending"), ("push", root / "push")):
            replies = root / "replies"
            for path in short_notices(folder):
                if kind == "pending" and (replies / f"{notice_seq(path)}.json").is_file():
                    continue
                key = notice_line(path, kind)
                if key in seen:
                    continue
                seen.add(key)
                lines.append(key)
                sys.stdout.write(key + "\n")
                sys.stdout.flush()

    take()
    if once:
        return HookResult(stdout="")
    while deadline is None or time.monotonic() < deadline:
        time.sleep(0.25)
        take()
    return HookResult(stdout="")


def main() -> HookResult:
    if len(sys.argv) > 1 and sys.argv[1] in ("pending", "reply", "push", "watch"):
        return cli(sys.argv[1:])
    raw = sys.stdin.read()
    if not raw.strip():
        return HookResult()
    try:
        event = json.loads(raw)
    except json.JSONDecodeError as exc:
        return HookResult(stderr=f"cc-cursor-bridge: 无法解析 stdin JSON: {exc}\n")
    return handle_event(event)


if __name__ == "__main__":
    result = main()
    if result.stderr:
        sys.stderr.write(result.stderr)
    if result.stdout:
        sys.stdout.write(result.stdout)
    raise SystemExit(result.code)
