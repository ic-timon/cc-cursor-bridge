#!/usr/bin/env python3
"""Fill the hook events that the first harness did not see. Sandbox is /tmp."""

from __future__ import annotations

import json
import os
import queue
import subprocess
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CLAUDE = str(Path.home() / ".local" / "bin" / "claude")
OUT = ROOT / "tests" / "hooks" / "out"
REPORT_MD = OUT / "report.md"
REPORT_JSON = OUT / "report.json"
FORBIDDEN = ("dangerously", "bypasspermissions", "bypass")


def assert_safe(argv: list) -> None:
    blob = " ".join(argv).lower()
    for token in FORBIDDEN:
        if token in blob:
            raise SystemExit(f"refusing forbidden flag: {token}")


def user_message(text: str) -> str:
    return json.dumps({"type": "user", "message": {"role": "user", "content": text}}, ensure_ascii=False) + "\n"


def base_cmd(extra: list) -> list:
    cmd = [
        CLAUDE,
        "--plugin-dir",
        str(ROOT),
        "--output-format",
        "stream-json",
        "--verbose",
        "--include-hook-events",
        "--strict-mcp-config",
        "--effort",
        "low",
        "--model",
        "haiku",
        "--no-session-persistence",
        *extra,
    ]
    assert_safe(cmd)
    return cmd


def run_once(sandbox: Path, task: str, extra: list, prompt: str | None, env_extra: dict | None = None, timeout: int = 70) -> dict:
    env = os.environ.copy()
    env["CC_TASK_ID"] = task
    env["CC_BRIDGE_DIR"] = str(sandbox / "bridge")
    env.pop("CC_BRIDGE_WAIT_ALL", None)
    env.pop("CC_BRIDGE_PROBE", None)
    env.pop("CC_BRIDGE_WAIT", None)
    if env_extra:
        env.update(env_extra)
    cmd = base_cmd(extra + ["--max-turns", "3"])
    if prompt is not None:
        cmd.extend(["-p", prompt])
    print(f"== {task} ==", flush=True)
    try:
        completed = subprocess.run(cmd, cwd=str(sandbox), env=env, text=True, capture_output=True, timeout=timeout, check=False)
        out, err, code, timed = completed.stdout, completed.stderr, completed.returncode, False
    except subprocess.TimeoutExpired as exc:
        out = exc.stdout or ""
        err = exc.stderr or ""
        if isinstance(out, bytes):
            out = out.decode()
        if isinstance(err, bytes):
            err = err.decode()
        code, timed = 124, True
    (sandbox / "streams").mkdir(exist_ok=True)
    (sandbox / "streams" / f"{task}.jsonl").write_text(out or "", encoding="utf-8")
    events = load_events(sandbox, task)
    print(f"   exit={code} events={[row.get('event') for row in events]}", flush=True)
    return {"task": task, "exit": code, "timed_out": timed, "stdout": out or "", "stderr": err or "", "events": events}


def load_events(sandbox: Path, task: str) -> list:
    path = sandbox / "bridge" / "tasks" / task / "events.jsonl"
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def stream_turns(sandbox: Path, task: str, prompts: list, env_extra: dict | None = None, between=None, timeout: int = 180) -> dict:
    env = os.environ.copy()
    env["CC_TASK_ID"] = task
    env["CC_BRIDGE_DIR"] = str(sandbox / "bridge")
    env.pop("CC_BRIDGE_WAIT_ALL", None)
    env.pop("CC_BRIDGE_PROBE", None)
    if env_extra:
        env.update(env_extra)
    cmd = base_cmd(["--input-format", "stream-json", "--max-turns", str(len(prompts) + 1), "-p"])
    print(f"== {task} stream ==", flush=True)
    proc = subprocess.Popen(cmd, cwd=str(sandbox), env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    lines: queue.Queue = queue.Queue()

    def reader() -> None:
        assert proc.stdout is not None
        for line in proc.stdout:
            lines.put(line)
        lines.put(None)

    threading.Thread(target=reader, daemon=True).start()
    collected = []

    def read_result(limit: int) -> dict | None:
        deadline = time.time() + limit
        while time.time() < deadline:
            try:
                line = lines.get(timeout=1)
            except queue.Empty:
                if proc.poll() is not None:
                    break
                continue
            if line is None:
                break
            collected.append(line)
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if row.get("type") == "result":
                return row
        return None

    assert proc.stdin is not None
    last = None
    for index, prompt in enumerate(prompts):
        if between and index:
            between()
        proc.stdin.write(user_message(prompt))
        proc.stdin.flush()
        last = read_result(70)
        if last is None:
            break
    proc.stdin.close()
    try:
        err = proc.stderr.read() if proc.stderr else ""
    except Exception:
        err = ""
    try:
        proc.wait(timeout=20)
    except subprocess.TimeoutExpired:
        proc.kill()
    text = "".join(collected)
    (sandbox / "streams").mkdir(exist_ok=True)
    (sandbox / "streams" / f"{task}.jsonl").write_text(text, encoding="utf-8")
    events = load_events(sandbox, task)
    print(f"   exit={proc.returncode} events={[row.get('event') for row in events]}", flush=True)
    return {"task": task, "exit": proc.returncode, "stdout": text, "stderr": err or "", "events": events, "last": last}


def snippet(text: str, limit: int = 400) -> str:
    text = " ".join((text or "").split())
    return text[:limit]


def main() -> int:
    sandbox = Path(tempfile.mkdtemp(prefix="ccb-gap-"))
    (sandbox / "sub").mkdir()
    (sandbox / "extra").mkdir()
    (sandbox / "watched.txt").write_text("a\n", encoding="utf-8")
    subprocess.run(["git", "init"], cwd=sandbox, check=True, capture_output=True)
    subprocess.run(["git", "-c", "user.email=probe@example.com", "-c", "user.name=probe", "commit", "--allow-empty", "-m", "init"], cwd=sandbox, check=True, capture_output=True)
    print("sandbox", sandbox, flush=True)

    findings = {}

    model = run_once(sandbox, "gap-model", [], "/model sonnet", timeout=60)
    findings["PreModelSwitch"] = {
        "fired": any(row.get("event") == "PreModelSwitch" for row in model["events"]),
        "evidence": "command: claude -p /model sonnet (hooks now synchronous). events="
        + ",".join(row.get("event") for row in model["events"]),
    }
    findings["PostModelSwitch"] = {
        "fired": any(row.get("event") == "PostModelSwitch" for row in model["events"]),
        "evidence": findings["PreModelSwitch"]["evidence"],
    }

    watched = sandbox / "watched.txt"

    def touch() -> None:
        time.sleep(1)
        watched.write_text("b\n", encoding="utf-8")

    changed = stream_turns(
        sandbox,
        "gap-watch",
        ["Reply with the single word ok.", "Reply with the single word ok."],
        env_extra={"CC_BRIDGE_WATCH": str(watched.resolve())},
        between=lambda: touch(),
        timeout=150,
    )
    findings["FileChanged"] = {
        "fired": any(row.get("event") == "FileChanged" for row in changed["events"]),
        "evidence": "SessionStart is sync and CC_BRIDGE_WATCH points at watched.txt; the file was rewritten between two stream-json turns. events="
        + ",".join(row.get("event") for row in changed["events"]),
    }

    cwd = stream_turns(
        sandbox,
        "gap-cwd",
        ["Run exactly this Bash command and nothing else: cd sub && pwd"],
        timeout=100,
    )
    findings["CwdChanged"] = {
        "fired": any(row.get("event") == "CwdChanged" for row in cwd["events"]),
        "evidence": "stream-json user message asked Bash to run `cd sub && pwd`. events="
        + ",".join(row.get("event") for row in cwd["events"])
        + " result="
        + snippet(str((cwd.get("last") or {}).get("result", ""))),
    }

    added = stream_turns(sandbox, "gap-add-dir", [f"/add-dir {sandbox / 'extra'}"], timeout=70)
    findings["DirectoryAdded"] = {
        "fired": any(row.get("event") == "DirectoryAdded" for row in added["events"]),
        "evidence": "stream-json message `/add-dir <sandbox>/extra`. result="
        + snippet(str((added.get("last") or {}).get("result", ""))),
    }

    prompts = ["Reply with the single word ok."] * 6 + ["/compact"]
    compact = stream_turns(sandbox, "gap-compact", prompts, timeout=240)
    compact_text = snippet(str((compact.get("last") or {}).get("result", "")))
    findings["PreCompact"] = {
        "fired": any(row.get("event") == "PreCompact" for row in compact["events"]),
        "evidence": "stream-json: six 'reply ok' turns then /compact. result=" + compact_text,
    }
    findings["PostCompact"] = {
        "fired": any(row.get("event") == "PostCompact" for row in compact["events"]),
        "evidence": findings["PreCompact"]["evidence"],
    }

    failure = run_once(
        sandbox,
        "gap-stop-failure",
        ["--max-turns", "1"],
        "Reply ok.",
        env_extra={"ANTHROPIC_BASE_URL": "http://127.0.0.1:9"},
        timeout=40,
    )
    findings["StopFailure"] = {
        "fired": any(row.get("event") == "StopFailure" for row in failure["events"]),
        "evidence": "one run with ANTHROPIC_BASE_URL=http://127.0.0.1:9 (cc-switch config untouched). stderr="
        + snippet(failure["stderr"])
        + " result="
        + snippet(failure["stdout"][-500:]),
    }

    work = run_once(sandbox, "gap-worktree", ["--worktree"], "Reply ok. Do not use tools or edit files.", timeout=70)
    findings["WorktreeRemove"] = {
        "fired": any(row.get("event") == "WorktreeRemove" for row in work["events"]),
        "evidence": "claude -p --worktree (unnamed) 'Reply ok. Do not use tools or edit files.' events="
        + ",".join(row.get("event") for row in work["events"]),
    }
    findings["WorktreeCreate"] = {
        "fired": any(row.get("event") == "WorktreeCreate" for row in work["events"]),
        "evidence": findings["WorktreeRemove"]["evidence"],
    }

    teams = run_once(
        sandbox,
        "gap-teams",
        ["--agent-teams"],
        "Create one teammate whose only job is to reply ok and then go idle. Do not edit files.",
        env_extra={"CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS": "1"},
        timeout=70,
    )
    findings["TeammateIdle"] = {
        "fired": any(row.get("event") == "TeammateIdle" for row in teams["events"]),
        "evidence": "CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1 and --agent-teams. stderr="
        + snippet(teams["stderr"])
        + " result="
        + snippet(teams["stdout"][-400:]),
    }

    denied = run_once(
        sandbox,
        "gap-auto",
        ["--permission-mode", "auto", "--model", "sonnet", "--allowedTools", "Bash", "--max-turns", "2"],
        "Run exactly this Bash command once: curl -I https://example.com",
        timeout=90,
    )
    findings["PermissionDenied"] = {
        "fired": any(row.get("event") == "PermissionDenied" for row in denied["events"]),
        "evidence": "--permission-mode auto --model sonnet, Bash curl -I https://example.com. stderr="
        + snippet(denied["stderr"])
        + " tail="
        + snippet(denied["stdout"][-400:]),
    }

    out = sandbox / "findings.json"
    out.write_text(json.dumps(findings, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print("findings", out, flush=True)
    merge(findings, sandbox)
    return 0


def merge(findings: dict, sandbox: Path) -> None:
    report = json.loads(REPORT_JSON.read_text(encoding="utf-8"))
    by_name = {row["event"]: row for row in report["events"]}
    for event, info in findings.items():
        row = by_name.get(event)
        if row is None:
            continue
        if info["fired"]:
            row["fired"] = True
            row["notes"] = "Fired in /tmp gap run. " + info["evidence"]
            row["tasks"] = list(dict.fromkeys(list(row.get("tasks") or []) + [f"gap:{event}"]))
        elif not row.get("fired"):
            row["notes"] = "NOT_TRIGGERABLE after a further attempt. " + info["evidence"]
    report["sensitive_file"] = {
        "inside_repo": "claude -p Write of <repo>/note.txt or examples/sandbox/.../write-a.txt was denied: decision_reason_type safetyCheck, message 'Claude requested permissions to edit <path> which is a sensitive file.'",
        "outside_repo": "The same Write with no permission flags, cwd /tmp/ccb-probe-xcoc, created note.txt containing hi. Exit 0, no permission_denied line.",
    }
    report["gap_sandbox"] = str(sandbox)
    OUT.mkdir(parents=True, exist_ok=True)
    REPORT_JSON.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    lines = [
        "# Hook harness report",
        "",
        "Outside this repo, a plain Write of note.txt succeeds with no permission flags. Inside the repo, Claude Code's safetyCheck calls the same write a sensitive file. Gap scenarios below ran under /tmp.",
        "",
        f"Gap sandbox: `{sandbox}`",
        "",
        "| event | fired | payload keys | decision tested | decision honoured | notes |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    fired_count = 0
    for row in report["events"]:
        if row["fired"]:
            fired_count += 1
        lines.append(
            "| {event} | {fired} | {keys} | {decision} | {honoured} | {notes} |".format(
                event=row["event"],
                fired="yes" if row["fired"] else "no",
                keys=", ".join(row.get("payload_keys") or []) or "-",
                decision=str(row.get("decision_tested") or "-").replace("|", "/"),
                honoured=str(row.get("decision_honoured") or "-"),
                notes=(row.get("notes") or "-").replace("|", "/").replace("\n", " "),
            )
        )
    lines.append("")
    lines.append(f"Fired {fired_count}/33.")
    lines.extend(["", "## Write in claude -p", ""])
    lines.append("Inside the repo, experiments (a)(b)(d)(e) failed the safetyCheck. (c) PermissionRequest allow and (f) permission-prompt-tool wrote the file. Outside the repo, experiment (a) wrote note.txt with no hook decision and no extra permission flags.")
    lines.append("")
    lines.append("| id | setup | PermissionRequest | file written | exit | errors |")
    lines.append("| --- | --- | --- | --- | --- | --- |")
    for item in report.get("write_experiments") or []:
        lines.append(
            "| {id} | {label} | {perm} | {written} | {code} | {errors} |".format(
                id=item["id"],
                label=str(item["label"]).replace("|", "/"),
                perm="yes" if item["permission_request_fired"] else "no",
                written="yes" if item["file_written"] else "no",
                code=item["exit_code"],
                errors="; ".join(item.get("errors") or [])[:400].replace("|", "/") or "-",
            )
        )
    REPORT_MD.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"fired {fired_count}/33")


if __name__ == "__main__":
    raise SystemExit(main())
