#!/usr/bin/env python3
"""Run real claude -p sessions and record which hook events fire.

Sandbox output stays under examples/sandbox/hook-harness/. This script writes
tests/hooks/report.md and tests/hooks/report.json. It never passes a bypass
or --dangerously-* flag.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CLAUDE = Path.home() / ".local/bin/claude"
REPORT_MD = ROOT / "tests" / "hooks" / "report.md"
REPORT_JSON = ROOT / "tests" / "hooks" / "report.json"
MCP = ROOT / "tests" / "hooks" / "stdio_mcp.py"
SANDBOX_ROOT = ROOT / "examples" / "sandbox" / "hook-harness"

EVENTS = [
    "SessionStart",
    "Setup",
    "InstructionsLoaded",
    "UserPromptSubmit",
    "UserPromptExpansion",
    "MessageDisplay",
    "PreToolUse",
    "PermissionRequest",
    "PermissionDenied",
    "PostToolUse",
    "PostToolUseFailure",
    "PostToolBatch",
    "Notification",
    "SubagentStart",
    "SubagentStop",
    "TaskCreated",
    "TaskCompleted",
    "Stop",
    "StopFailure",
    "TeammateIdle",
    "ConfigChange",
    "CwdChanged",
    "DirectoryAdded",
    "FileChanged",
    "WorktreeCreate",
    "WorktreeRemove",
    "PreCompact",
    "PostCompact",
    "PreModelSwitch",
    "PostModelSwitch",
    "Elicitation",
    "ElicitationResult",
    "SessionEnd",
]

FORBIDDEN = ("dangerously", "bypasspermissions", "bypass")
SECRET = re.compile(r"(?i)(sk-ant-|api[_-]?key\s*[:=]\s*\S+|bearer\s+\S+|super-secret)")


def redact(text: str, limit: int = 240) -> str:
    text = SECRET.sub("[redacted]", text or "")
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) <= limit:
        return text
    return text[:limit] + "…[truncated]"


def assert_safe(argv: list) -> None:
    blob = " ".join(argv).lower()
    for token in FORBIDDEN:
        if token in blob:
            raise SystemExit(f"refusing forbidden flag in {argv}")


def git(sandbox: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(sandbox), *args],
        check=True,
        capture_output=True,
        text=True,
        env={**os.environ, "GIT_AUTHOR_NAME": "probe", "GIT_AUTHOR_EMAIL": "probe@example.com",
             "GIT_COMMITTER_NAME": "probe", "GIT_COMMITTER_EMAIL": "probe@example.com"},
    )


def prepare_sandbox() -> Path:
    sandbox = SANDBOX_ROOT / time.strftime("run-%Y%m%d-%H%M%S")
    sandbox.mkdir(parents=True)
    (sandbox / "CLAUDE.md").write_text("# harness\nOne line so InstructionsLoaded can fire.\n", encoding="utf-8")
    (sandbox / "extra").mkdir()
    (sandbox / "sub").mkdir()
    commands = sandbox / ".claude" / "commands"
    commands.mkdir(parents=True)
    (commands / "ping.md").write_text("---\ndescription: reply pong\n---\nReply with the single word pong.\n", encoding="utf-8")
    (sandbox / "README").write_text("hook harness\n", encoding="utf-8")
    git(sandbox, "init")
    git(sandbox, "add", "README", "CLAUDE.md")
    git(sandbox, "commit", "-m", "harness")
    return sandbox


def load_events(sandbox: Path, task: str) -> list:
    path = sandbox / "bridge" / "tasks" / task / "events.jsonl"
    if not path.is_file():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


def stream_errors(text: str) -> list:
    found = []
    for line in text.splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        subtype = row.get("subtype")
        if subtype in ("permission_denied", "error") or row.get("is_error") is True:
            snippet = {
                key: row.get(key)
                for key in (
                    "type",
                    "subtype",
                    "tool_name",
                    "decision_reason",
                    "decision_reason_type",
                    "message",
                    "error",
                    "result",
                )
                if key in row
            }
            found.append(redact(json.dumps(snippet, ensure_ascii=False), 400))
        content = ((row.get("message") or {}).get("content") if isinstance(row.get("message"), dict) else None)
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_result" and block.get("is_error"):
                    found.append(redact(str(block.get("content") or ""), 400))
        if row.get("type") == "result" and isinstance(row.get("result"), str) and row.get("is_error"):
            found.append(redact(row["result"], 400))
    # unique, preserve order
    seen = []
    for item in found:
        if item and item not in seen:
            seen.append(item)
    return seen[:8]


class Harness:
    def __init__(self, sandbox: Path):
        self.sandbox = sandbox
        self.model = "haiku"
        self.model_note = ""
        self.runs = []

    def run(
        self,
        task: str,
        extra: list,
        prompt: str | None = None,
        stdin: str | None = None,
        probe: dict | None = None,
        timeout: int = 90,
        max_turns: int | None = 2,
        persist: bool = False,
    ) -> dict:
        bridge = self.sandbox / "bridge"
        env = os.environ.copy()
        env["CC_TASK_ID"] = task
        env["CC_BRIDGE_DIR"] = str(bridge)
        env.pop("CC_BRIDGE_WAIT", None)
        env.pop("CC_BRIDGE_NOTIFIER", None)
        env.pop("CC_BRIDGE_PROBE", None)
        if probe is not None:
            probe_path = self.sandbox / "probes" / f"{task}.json"
            probe_path.parent.mkdir(parents=True, exist_ok=True)
            probe_path.write_text(json.dumps({"responses": probe}, ensure_ascii=False), encoding="utf-8")
            env["CC_BRIDGE_PROBE"] = str(probe_path)
        cmd = [
            str(CLAUDE),
            "--plugin-dir",
            str(ROOT),
            "--output-format",
            "stream-json",
            "--verbose",
            "--include-hook-events",
            "--strict-mcp-config",
            "--effort",
            "low",
            "--debug-file",
            str(self.sandbox / "debug" / f"{task}.log"),
        ]
        if max_turns is not None:
            cmd.extend(["--max-turns", str(max_turns)])
        if not persist:
            cmd.append("--no-session-persistence")
        if self.model:
            cmd.extend(["--model", self.model])
        cmd.extend(extra)
        if prompt is not None:
            cmd.extend(["-p", prompt])
        elif "-p" not in extra and "--print" not in extra and "--init-only" not in extra:
            cmd.append("-p")
        assert_safe(cmd)
        (self.sandbox / "debug").mkdir(exist_ok=True)
        print(f"== {task} ==", flush=True)
        try:
            completed = subprocess.run(
                cmd,
                cwd=str(self.sandbox),
                env=env,
                input=stdin,
                text=True,
                capture_output=True,
                timeout=timeout,
                check=False,
            )
            timed_out = False
            stdout = completed.stdout or ""
            stderr = completed.stderr or ""
            code = completed.returncode
        except subprocess.TimeoutExpired as exc:
            timed_out = True
            stdout = exc.stdout or ""
            if isinstance(stdout, bytes):
                stdout = stdout.decode("utf-8", "replace")
            stderr = exc.stderr or ""
            if isinstance(stderr, bytes):
                stderr = stderr.decode("utf-8", "replace")
            code = 124
        (self.sandbox / "streams").mkdir(exist_ok=True)
        (self.sandbox / "streams" / f"{task}.jsonl").write_text(stdout, encoding="utf-8")
        errors = stream_errors(stdout)
        if stderr.strip():
            errors.append(redact(stderr, 400))
        events = load_events(self.sandbox, task)
        names = [row.get("event") for row in events]
        rejected = self.model and any(
            "model" in item.lower() and any(word in item.lower() for word in ("not found", "unknown", "invalid", "unavailable"))
            for item in errors
        )
        record = {
            "task": task,
            "argv": cmd,
            "exit_code": code,
            "timed_out": timed_out,
            "events": names,
            "errors": errors[:8],
            "rows": events,
        }
        if rejected and self.model:
            self.model_note = errors[0] if errors else "model rejected"
            self.model = ""
            print(f"model haiku rejected, retrying {task} on the proxy default", flush=True)
            return self.run(task, extra, prompt, stdin, probe, timeout, max_turns, persist)
        self.runs.append(record)
        print(f"   exit={code} timeout={timed_out} events={names}", flush=True)
        return record


def file_has(path: Path, content: str) -> bool:
    if not path.is_file():
        return False
    return content in path.read_text(encoding="utf-8", errors="replace")


def write_probe(decision_event: str, kind: str) -> list:
    if decision_event == "PreToolUse":
        output = {"permissionDecision": kind, "permissionDecisionReason": f"probe {kind}"}
    else:
        output = {"behavior": kind, "message": f"probe {kind}"}
    return [{"event": decision_event, "match": {"tool_name": "Write"}, "once": True, "output": output}]


def main() -> int:
    only = set()
    if "--only" in sys.argv:
        only = set(sys.argv[sys.argv.index("--only") + 1].split(","))
    if "--sandbox" in sys.argv:
        sandbox = Path(sys.argv[sys.argv.index("--sandbox") + 1]).resolve()
        if not sandbox.is_dir():
            raise SystemExit(f"sandbox missing: {sandbox}")
    else:
        sandbox = prepare_sandbox()
    harness = Harness(sandbox)
    extra_dir = str(sandbox / "extra")

    def wanted(task: str) -> bool:
        return not only or task in only

    if wanted("setup-init"):
        harness.run(
            "setup-init",
            ["--init-only", "--add-dir", extra_dir],
            prompt=None,
            max_turns=None,
            timeout=40,
        )
    if wanted("baseline"):
        settings = sandbox / ".claude" / "settings.json"

        def touch_settings() -> None:
            time.sleep(2)
            settings.write_text('{"permissions":{"allow":[]}}\n', encoding="utf-8")
            watched = sandbox / "cc-bridge-probe-watch"
            watched.write_text("x\n", encoding="utf-8")

        import threading

        threading.Thread(target=touch_settings, daemon=True).start()
        harness.run(
            "baseline",
            ["--maintenance", "--add-dir", extra_dir, "--allowedTools", "Bash"],
            prompt="Run exactly this Bash command and nothing else: echo hi. Then reply pong.",
            max_turns=3,
            timeout=120,
        )
    if wanted("cwd"):
        harness.run(
            "cwd",
            ["--allowedTools", "Bash"],
            prompt="Run exactly this Bash command and nothing else: cd sub && pwd",
            max_turns=2,
            timeout=90,
        )
    if wanted("tool-fail"):
        harness.run(
            "tool-fail",
            ["--allowedTools", "Bash"],
            prompt="Run exactly this Bash command once and do not retry: false",
            max_turns=2,
            timeout=90,
        )
    if wanted("probe-deny"):
        harness.run(
            "probe-deny",
            ["--allowedTools", "Bash"],
            prompt="Run exactly this Bash command: echo hi",
            probe=[{
                "event": "PreToolUse",
                "match": {"tool_name": "Bash"},
                "once": True,
                "output": {"permissionDecision": "deny", "permissionDecisionReason": "probe deny"},
            }],
            max_turns=2,
            timeout=90,
        )
    if wanted("slash-ping"):
        harness.run("slash-ping", [], prompt="/ping", max_turns=2, timeout=90)
    if wanted("subagent"):
        harness.run(
            "subagent",
            [],
            prompt="Use the Task tool once with subagent_type Explore. The task prompt is: reply ok. Description: probe. Do not use other tools.",
            max_turns=3,
            timeout=150,
        )
    if wanted("elicit"):
        config = sandbox / "mcp-elicit.json"
        config.write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "elicit": {
                            "command": sys.executable,
                            "args": [str(MCP), "elicit"],
                        }
                    }
                }
            ),
            encoding="utf-8",
        )
        harness.run(
            "elicit",
            ["--mcp-config", str(config), "--allowedTools", "mcp__elicit__probe_ask"],
            prompt="Call the probe_ask tool once, then stop. Do not use any other tool.",
            max_turns=3,
            timeout=90,
        )
    if wanted("worktree"):
        harness.run(
            "worktree",
            ["--worktree", "probe"],
            prompt="Reply ok. Do not use tools.",
            max_turns=1,
            timeout=90,
        )
    if wanted("slash-compact"):
        harness.run("slash-compact", [], prompt="/compact", max_turns=1, timeout=90)
    if wanted("slash-model"):
        harness.run("slash-model", [], prompt="/model haiku", max_turns=1, timeout=90)
    if wanted("slash-model-sonnet"):
        harness.run("slash-model-sonnet", [], prompt="/model sonnet", max_turns=1, timeout=90)
    if wanted("tasks"):
        harness.run(
            "tasks",
            [],
            prompt="Call TaskCreate once with subject probe and description probe. Then call TaskUpdate to mark that task completed. Do not use any other tool.",
            max_turns=5,
            timeout=120,
        )
    if wanted("slash-add-dir"):
        harness.run(
            "slash-add-dir",
            [],
            prompt="/add-dir extra",
            max_turns=1,
            timeout=60,
        )
    if wanted("bad-model"):
        saved = harness.model
        harness.model = "not-a-real-model-probe"
        harness.run("bad-model", [], prompt="Reply ok.", max_turns=1, timeout=40)
        harness.model = saved

    write_prompt = (
        "Use the Write tool once to create {name} with the exact content hi. "
        "Do not use Bash or any other tool. Then reply done."
    )
    experiments = [
        ("write-a", [], None, "no hook decision, default permission mode"),
        ("write-b", [], write_probe("PreToolUse", "allow"), "PreToolUse permissionDecision allow"),
        ("write-c", [], write_probe("PermissionRequest", "allow"), "PermissionRequest decision allow"),
        ("write-d", ["--permission-mode", "acceptEdits"], None, "--permission-mode acceptEdits"),
        ("write-e", ["--allowedTools", "Write"], None, "--allowedTools Write"),
    ]
    permit_config = sandbox / "mcp-permit.json"
    permit_config.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "permit": {
                        "command": sys.executable,
                        "args": [str(MCP), "permit"],
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    experiments.append(
        (
            "write-f",
            [
                "--mcp-config",
                str(permit_config),
                "--permission-prompt-tool",
                "mcp__permit__approve",
                "--permission-prompts",
                "host",
            ],
            None,
            "--permission-prompt-tool mcp__permit__approve",
        )
    )
    write_results = []
    for task, extra, probe, label in experiments:
        if not wanted(task):
            continue
        name = f"{task}.txt"
        target = sandbox / name
        record = harness.run(
            task,
            extra,
            prompt=write_prompt.format(name=name),
            probe=probe,
            max_turns=3,
            timeout=70,
        )
        rows = record["rows"]
        pre = [row for row in rows if row.get("event") == "PreToolUse" and (row.get("summary") or {}).get("tool_name") == "Write"]
        perm = [row for row in rows if row.get("event") == "PermissionRequest" and (row.get("summary") or {}).get("tool_name") == "Write"]
        write_results.append(
            {
                "id": task,
                "label": label,
                "permission_request_fired": bool(perm),
                "pretool_fired": bool(pre),
                "pretool_probe": (pre[-1].get("summary") or {}).get("probe") if pre else None,
                "permission_probe": (perm[-1].get("summary") or {}).get("probe") if perm else None,
                "file_written": file_has(target, "hi"),
                "errors": record["errors"],
                "exit_code": record["exit_code"],
                "timed_out": record["timed_out"],
            }
        )

    # Events we did not try to force because there is no headless trigger.
    static_reasons = {
        "TeammateIdle": "NOT_TRIGGERABLE: claude -p has no agent-team teammate lifecycle. TeammateIdle fires when a teammate is about to go idle, and this CLI session never starts a team.",
        "TaskCreated": "NOT_TRIGGERABLE unless TaskCreate is in the session tool list. The previous local tool list has Task but not TaskCreate; confirmed again from this run's init message.",
        "TaskCompleted": "NOT_TRIGGERABLE for the same reason as TaskCreated: no TaskCreate/TaskUpdate tool is exposed, so a task cannot be completed headlessly.",
    }

    seen_tasks = {run["task"] for run in harness.runs}
    tasks_dir = sandbox / "bridge" / "tasks"
    if tasks_dir.is_dir():
        for task_dir in sorted(tasks_dir.iterdir()):
            if not task_dir.is_dir() or task_dir.name in seen_tasks:
                continue
            rows = load_events(sandbox, task_dir.name)
            stream = sandbox / "streams" / f"{task_dir.name}.jsonl"
            text = stream.read_text(encoding="utf-8", errors="replace") if stream.is_file() else ""
            harness.runs.append(
                {
                    "task": task_dir.name,
                    "argv": [],
                    "exit_code": None,
                    "timed_out": False,
                    "events": [row.get("event") for row in rows],
                    "errors": stream_errors(text),
                    "rows": rows,
                }
            )

    by_event = {name: [] for name in EVENTS}
    keys = {name: set() for name in EVENTS}
    probes = {name: [] for name in EVENTS}
    for run in harness.runs:
        for row in run["rows"]:
            event = row.get("event")
            if event not in by_event:
                by_event.setdefault(event, [])
                keys.setdefault(event, set())
                probes.setdefault(event, [])
            by_event[event].append(run["task"])
            for key in row.get("input_keys") or []:
                keys[event].add(key)
            if (row.get("summary") or {}).get("probe"):
                probes[event].append(run["task"] + ":" + str(row["summary"].get("probe")))

    init_tools = []
    for run in harness.runs:
        stream = sandbox / "streams" / f"{run['task']}.jsonl"
        if not stream.is_file():
            continue
        for line in stream.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if row.get("subtype") == "init" and row.get("tools"):
                init_tools = list(row["tools"])
                break
        if init_tools:
            break
    if "TaskCreate" in init_tools:
        static_reasons.pop("TaskCreated", None)
        static_reasons.pop("TaskCompleted", None)

    deny_run = next((run for run in harness.runs if run["task"] == "probe-deny"), None)
    deny_honoured = None
    if deny_run:
        bash_posts = [
            row for row in deny_run["rows"]
            if row.get("event") == "PostToolUse" and (row.get("summary") or {}).get("tool_name") == "Bash"
        ]
        deny_honoured = not bash_posts and any(
            (row.get("summary") or {}).get("probe") == "applied" and row.get("event") == "PreToolUse"
            for row in deny_run["rows"]
        )

    rows_out = []
    for event in EVENTS:
        fired = bool(by_event.get(event))
        note = ""
        if event in static_reasons and not fired:
            note = static_reasons[event]
        elif not fired:
            note = "Did not fire in any scenario that was expected to reach it. See scenario errors."
        decision = "none"
        honoured = "n/a"
        if probes.get(event):
            decision = ", ".join(probes[event])
            if event == "PreToolUse" and deny_honoured is not None:
                honoured = "yes" if deny_honoured else "no"
            else:
                honoured = "see write experiments" if event in ("PreToolUse", "PermissionRequest") else "emitted; async hooks ignore decisions"
        if event == "TaskCompleted" and not fired:
            note = "TaskCreate did emit TaskCreated. TaskUpdate was called, and the session then hit error_max_turns before TaskCompleted. A longer retry is included when that scenario is rerun."
        if event == "PermissionDenied" and not fired:
            note = "NOT_TRIGGERABLE here: Write denials were safetyCheck/config rejects and did not emit PermissionDenied. Auto mode, which is what this hook documents, could not be entered on haiku (debug: model does not support auto mode)."
        if event == "Notification" and not fired:
            note = "Did not fire. Denials were immediate, so the permission_prompt notification (it waits about six seconds) never started, and no other headless notification was emitted."
        if event == "DirectoryAdded" and not fired:
            note = "NOT_TRIGGERABLE: --add-dir at launch did not emit DirectoryAdded, and claude -p /add-dir extra returned \"/add-dir isn't available in this environment.\""
        if event == "WorktreeRemove" and not fired:
            note = "--worktree fired WorktreeCreate, but exiting that -p process did not fire WorktreeRemove."
        if event == "PreCompact" and not fired:
            note = "NOT_TRIGGERABLE: claude -p /compact returned 'Not enough messages to compact' and did not emit PreCompact or PostCompact."
        if event == "PostCompact" and not fired:
            note = "NOT_TRIGGERABLE: same /compact result as PreCompact."
        if event == "StopFailure" and not fired:
            note = "NOT_TRIGGERABLE without a real API failure. --model not-a-real-model-probe still completed and replied ok through the proxy."
        if event == "PreModelSwitch" and not fired:
            note = "/model haiku on an already-haiku session did not switch. /model sonnet did switch: the stream shows PreModelSwitch:claude-sonnet-5 with empty stdout, but this plugin's jsonl did not get that row (the async hook exited before the log flush). PostModelSwitch was logged."
        if event == "PostModelSwitch" and not fired:
            note = "Not logged. See PreModelSwitch."
        if event == "Elicitation" and not fired:
            note = "The elicit MCP server was not a callable tool. First run timed out connecting; see the elicit scenario errors."
        if event == "ElicitationResult" and not fired:
            note = "Did not fire because Elicitation did not fire."
        if event == "FileChanged" and not fired:
            note = "NOT_TRIGGERABLE with the current plugin: FileChanged has no filename matcher, and SessionStart is async so a watchPaths reply is ignored. Touching cc-bridge-probe-watch during baseline did not fire it."
        if event == "CwdChanged" and not fired:
            note = "NOT_TRIGGERABLE in this headless run: scenario cwd asked Bash to `cd sub && pwd`. That did not emit CwdChanged. There is no separate -p cd control."
        rows_out.append(
            {
                "event": event,
                "fired": fired,
                "tasks": by_event.get(event, []),
                "payload_keys": sorted(keys.get(event) or []),
                "decision_tested": decision,
                "decision_honoured": honoured,
                "notes": note,
            }
        )

    # Cwd note is only fair if we didn't try cd. We didn't. Good.

    if REPORT_JSON.is_file() and "--sandbox" in sys.argv:
        previous = json.loads(REPORT_JSON.read_text(encoding="utf-8")).get("write_experiments") or []
        merged = {item["id"]: item for item in previous}
        for item in write_results:
            merged[item["id"]] = item
        order = ["write-a", "write-b", "write-c", "write-d", "write-e", "write-f"]
        write_results = [merged[key] for key in order if key in merged]

    payload = {
        "sandbox": str(sandbox),
        "model": harness.model or "proxy default",
        "model_note": harness.model_note,
        "init_tools_sample": [tool for tool in init_tools if not str(tool).startswith("mcp__")],
        "events": rows_out,
        "write_experiments": write_results,
        "runs": [
            {
                "task": run["task"],
                "exit_code": run["exit_code"],
                "timed_out": run["timed_out"],
                "events": run["events"],
                "errors": run["errors"],
            }
            for run in harness.runs
        ],
    }
    REPORT_JSON.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    lines = [
        "# Hook harness report",
        "",
        f"Sandbox: `{sandbox}`",
        f"Model: `{payload['model']}`. {payload['model_note']}",
        "",
        "| event | fired | payload keys | decision tested | decision honoured | notes |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for row in rows_out:
        lines.append(
            "| {event} | {fired} | {keys} | {decision} | {honoured} | {notes} |".format(
                event=row["event"],
                fired="yes" if row["fired"] else "no",
                keys=", ".join(row["payload_keys"]) or "-",
                decision=row["decision_tested"].replace("|", "/"),
                honoured=row["decision_honoured"],
                notes=(row["notes"] or "-").replace("|", "/"),
            )
        )
    lines.extend(["", "## Write in claude -p", ""])
    lines.append("| id | setup | PermissionRequest | file written | exit | errors |")
    lines.append("| --- | --- | --- | --- | --- | --- |")
    for item in write_results:
        lines.append(
            "| {id} | {label} | {perm} | {written} | {code} | {errors} |".format(
                id=item["id"],
                label=item["label"].replace("|", "/"),
                perm="yes" if item["permission_request_fired"] else "no",
                written="yes" if item["file_written"] else "no",
                code=item["exit_code"],
                errors="; ".join(item["errors"])[:500].replace("|", "/") or "-",
            )
        )
    lines.append("")
    lines.append("PreToolUse deny probe (echo hi): " + (
        "honoured, Bash did not reach PostToolUse" if deny_honoured else "not honoured or not run"
    ))
    REPORT_MD.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"wrote {REPORT_MD}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
