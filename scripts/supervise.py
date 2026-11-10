#!/usr/bin/env python3
"""Reattachable local supervisor for one cc-cursor-bridge task (read-only).

Attach, detach (Ctrl-C / SIGTERM), then attach to the same CC_TASK_ID again.
This script never starts or stops Claude, never writes replies, approvals,
pending files or status, and never auto-approves anything. It prints pending
items for the operator, who answers with `bridge.py reply <task> <seq> <json>`.

One observer per task: `$CC_BRIDGE_DIR/locks/<task>.observer.lock` is held with
an exclusive flock for as long as this process runs. flock is released by the OS
when the process exits, including crashes, so the lock file content may be stale.
"""

from __future__ import annotations

import fcntl
import json
import os
import signal
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import bridge  # noqa: E402

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_BUSY = 3

USAGE = (
    "usage: supervise.py <task> [--once | --for <seconds>] [--interval <seconds>] [--ahp]\n"
    "  Read-only observer for an existing CC_TASK_ID. Ctrl-C or SIGTERM detaches;\n"
    "  run the same command again to reattach. Nothing in the task tree is deleted.\n"
    "  Answer pending items with: bridge.py reply <task> <seq> <json>\n"
)


def plain(value: object) -> str:
    """Print status values the way status.json encodes them: true/false/null."""
    if value is True:
        return "true"
    if value is False:
        return "false"
    if value is None:
        return "null"
    return str(value)


def effective_ahp(task: str, ahp: bool) -> bool:
    """Same tree choice as `bridge.py`: --ahp, or CC_BRIDGE_LEGACY=0."""
    wanted, _ = bridge.use_ahp_tree(["status", task] + (["--ahp"] if ahp else []))
    return wanted


def status_file_path(task: str, ahp: bool) -> Path | None:
    try:
        root = bridge.ahp_task_root(task) if ahp else bridge.task_root(task)
    except Exception:  # noqa: BLE001 - display only
        return None
    return root / bridge.STATUS_FILE


def status_view(task: str, ahp: bool) -> dict:
    """Run the documented `bridge.py status` and classify its output.

    `missing` means there is no terminal evidence yet, never "running" and never
    "done". A present but unreadable file and a state other than `ended` are also
    not terminal.
    """
    args = ["status", task] + (["--ahp"] if ahp else [])
    try:
        result = bridge.cli(args)
    except Exception as exc:  # noqa: BLE001 - keep observing
        return {"kind": "unreadable", "detail": f"{type(exc).__name__}: {exc}"}
    if result.code != 0:
        detail = (result.stderr or "").strip() or f"exit {result.code}"
        return {"kind": "unreadable", "detail": detail}
    text = result.stdout
    if text.startswith("missing: "):
        return {"kind": "missing", "path": text[len("missing: "):].strip()}
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        return {"kind": "unreadable", "detail": str(exc)}
    if not isinstance(data, dict):
        return {"kind": "unreadable", "detail": "status is not a JSON object"}
    if data.get("state") == "ended":
        return {"kind": "ended", "data": data}
    return {"kind": "unrecognized", "state": data.get("state")}


def pending_lines(task: str, ahp: bool) -> tuple[list[str], str]:
    """Open pending items from the documented `bridge.py pending` view."""
    args = ["pending", task] + (["--ahp"] if ahp else [])
    try:
        result = bridge.cli(args)
    except Exception as exc:  # noqa: BLE001 - keep observing
        return [], f"{type(exc).__name__}: {exc}"
    if result.code != 0:
        return [], (result.stderr or "").strip() or f"exit {result.code}"
    return [line for line in result.stdout.splitlines() if line.strip()], ""


def status_line(view: dict) -> str:
    kind = view["kind"]
    if kind == "missing":
        return f"status: missing {view['path']}"
    if kind == "unrecognized":
        return f"status: unrecognized (state: {plain(view.get('state'))})"
    return f"status: unreadable ({view.get('detail') or 'no details'})"


def terminal_summary(task: str, data: dict, ahp: bool) -> None:
    path = status_file_path(task, ahp)
    print(f"task {task} ended")
    print(f"  state: {plain(data.get('state'))}")
    print(f"  stop_accepted: {plain(data.get('stop_accepted'))}")
    print(f"  reason: {plain(data.get('session_end_reason'))}")
    print(f"  ended_at: {plain(data.get('ended_at'))}")
    print(f"  stop_seq: {plain(data.get('stop_seq'))}")
    if path is not None:
        print(f"  status_file: {path}")


class ObserverLock:
    """Exclusive per-task observer lock. Missing task trees are not created here."""

    def __init__(self, root: Path, task: str):
        self.task = task
        self.path = root / "locks" / f"{task}.observer.lock"
        self.handle = None

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+", encoding="utf-8")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            return False
        self.handle = handle
        handle.seek(0)
        handle.truncate()
        handle.write(json.dumps({
            "protocol": "cc-bridge-observer/1",
            "task": self.task,
            "pid": os.getpid(),
            "attached_at": bridge.local_iso(),
        }, ensure_ascii=False) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
        return True

    def holder(self) -> str:
        """Best-effort description of the process that holds the lock."""
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return "holder unknown"
        if not isinstance(data, dict):
            return "holder unknown"
        pid = data.get("pid")
        since = data.get("attached_at") or data.get("released_at")
        if pid is None and since is None:
            return "holder unknown"
        return f"pid {plain(pid)}, since {plain(since)}"

    def release(self) -> None:
        if self.handle is None:
            return
        handle = self.handle
        self.handle = None
        try:
            handle.seek(0)
            handle.truncate()
            handle.write(json.dumps({
                "protocol": "cc-bridge-observer/1",
                "task": self.task,
                "released_at": bridge.local_iso(),
            }, ensure_ascii=False) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            handle.close()


def parse_args(argv: list[str]) -> tuple[dict | None, str]:
    opts = {"task": "", "once": False, "for": None, "interval": 1.0, "ahp": False}
    index = 0
    while index < len(argv):
        item = argv[index]
        if item == "--once":
            opts["once"] = True
            index += 1
            continue
        if item == "--ahp":
            opts["ahp"] = True
            index += 1
            continue
        if item in ("--for", "--interval"):
            if index + 1 >= len(argv):
                return None, USAGE
            try:
                value = float(argv[index + 1])
            except ValueError:
                return None, USAGE
            if value < 0 or (item == "--interval" and value <= 0):
                return None, USAGE
            opts["for" if item == "--for" else "interval"] = value
            index += 2
            continue
        if item.startswith("-") or opts["task"]:
            return None, USAGE
        opts["task"] = item
        index += 1
    if opts["once"] and opts["for"] is not None:
        return None, USAGE
    task = bridge.safe_component(opts["task"]) or ""
    if not task:
        return None, USAGE
    opts["task"] = task
    return opts, ""


def monitor(task: str, opts: dict) -> int:
    stop = {"flag": False}

    def handle_signal(signum, frame):  # noqa: ARG001 - signal handler signature
        stop["flag"] = True

    previous = {}
    for signum in (signal.SIGINT, signal.SIGTERM):
        try:
            previous[signum] = signal.getsignal(signum)
            signal.signal(signum, handle_signal)
        except (ValueError, OSError):
            pass
    deadline = None if opts["for"] is None else time.monotonic() + max(0.0, opts["for"])
    ahp = effective_ahp(task, bool(opts["ahp"]))
    seen: set[str] = set()
    last_status = ""
    ended = False
    print(f"supervisor attached: task {task} (pid {os.getpid()})", flush=True)
    print(f"supervisor: read-only; answer pending items with: bridge.py reply {task} <seq> <json>",
          flush=True)
    try:
        while True:
            view = status_view(task, ahp)
            if view["kind"] == "ended":
                terminal_summary(task, view["data"], ahp)
                ended = True
                return EXIT_OK
            line = status_line(view)
            if line != last_status:
                print(line, flush=True)
                last_status = line
            lines, error = pending_lines(task, ahp)
            if error:
                line = f"pending: unreadable ({error})"
                if line != last_status:
                    print(line, flush=True)
                    last_status = line
            else:
                for item in lines:
                    entry = f"pending {item}"
                    if entry not in seen:
                        seen.add(entry)
                        print(entry, flush=True)
            if opts["once"]:
                return EXIT_OK
            if stop["flag"]:
                break
            delay = float(opts["interval"])
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                delay = min(delay, remaining)
            time.sleep(max(0.0, delay))
    finally:
        for signum, handler in previous.items():
            try:
                signal.signal(signum, handler)
            except (ValueError, OSError):
                pass
        if not ended and not opts["once"]:
            print(f"supervisor detached: task {task}", flush=True)
            print("supervisor: no terminal status was observed; the task tree was not modified",
                  flush=True)
    return EXIT_OK


def main(argv: list[str]) -> int:
    if not argv or argv[0] in ("-h", "--help"):
        sys.stderr.write(USAGE)
        return EXIT_USAGE if not argv else EXIT_OK
    opts, error = parse_args(argv)
    if opts is None:
        sys.stderr.write(error or USAGE)
        return EXIT_USAGE
    task = opts["task"]
    lock = ObserverLock(bridge.bridge_root_from_env(), task)
    if not lock.acquire():
        holder = lock.holder()
        sys.stderr.write(
            f"supervisor: another observer is already attached to task {task} "
            f"({holder}); lock file: {lock.path}\n"
        )
        return EXIT_BUSY
    try:
        return monitor(task, opts)
    finally:
        lock.release()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
