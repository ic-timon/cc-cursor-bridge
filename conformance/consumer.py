"""Scripted consumer: watches pending/ and writes replies on cue.

Script items (first match wins; each item is used once unless "repeat": true):
  {"match": {"event": "tool.pre", "nth": 1, "tool_name": "Bash"},
   "action": "allow|deny|ask|block|ack|accept|decline|invalid|late|none|raw",
   "reason": "...", "updated_input": {...}, "content": {...}, "raw": <any JSON or string>,
   "delay_ms": 0}

- invalid: writes {"decision": "maybe"} (or `raw` if given).
- raw: writes `raw` verbatim (string = raw bytes, e.g. broken JSON).
- late: waits until the envelope deadline + 300 ms, then writes a valid reply
  with `decision` (default the first non-default decision).
- none: never replies.
- "source": "push" on an item makes it answer a notify event (a file in push/)
  instead of a pending gate. Spec 7.3 #1: the producer must not apply it and must
  write an invalid ack.
Replies are written atomically. Pending files in the 9f478f8 legacy format are
understood too (flat body, CC hook name in "event"), so the same scenarios can
drive the legacy bridge.
"""
from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional

from .common import CC_TO_AHP, parse_ts, read_json, write_atomic

VALID = {"allow", "deny", "ask", "block", "ack", "accept", "decline"}


class ScriptedConsumer:
    def __init__(self, task_dir: Path, script: List[dict], default_timeout_ms: int) -> None:
        self.dir = task_dir
        self.script = [dict(item, _used=False) for item in script]
        self.timeout_ms = default_timeout_ms
        self.seen: Dict[int, dict] = {}
        self.counts: Dict[str, int] = {}
        self.log: List[dict] = []
        self._stop = threading.Event()
        self._threads: List[threading.Thread] = []
        self._main = threading.Thread(target=self._loop, daemon=True)

    def start(self) -> None:
        self._main.start()

    def stop(self, grace_s: float = 5.0) -> None:
        self._stop.set()
        self._main.join(timeout=grace_s)
        for t in self._threads:
            t.join(timeout=grace_s)

    # ---------- matching ----------
    def _choose(self, event: str, nth: int, doc: dict, source: str = "pending") -> Optional[dict]:
        payload = doc.get("payload") if "protocol" in doc else doc
        tool = (payload or {}).get("tool_name") or doc.get("tool_name")
        for item in self.script:
            if item["_used"] and not item.get("repeat"):
                continue
            if item.get("source", "pending") != source:
                continue
            m = item.get("match") or {}
            if m.get("event") and m["event"] != event:
                continue
            if m.get("nth") and m["nth"] != nth:
                continue
            if m.get("tool_name") and m["tool_name"] != tool:
                continue
            item["_used"] = True
            return item
        return None

    @staticmethod
    def _identify(doc: dict):
        seq = int(doc.get("seq") or 0)
        if doc.get("protocol") == "ahp/1":
            return seq, doc.get("event", "")
        native = str(doc.get("event") or "")
        return seq, CC_TO_AHP.get(native, ("x-" + native.lower(),))[0]

    def _deadline(self, doc: dict) -> datetime:
        if doc.get("deadline"):
            return parse_ts(doc["deadline"])
        created = doc.get("created_at") or doc.get("ts")
        base = parse_ts(created) if created else datetime.now().astimezone()
        return base + timedelta(milliseconds=self.timeout_ms)

    # ---------- loop ----------
    def _loop(self) -> None:
        pending = self.dir / "pending"
        push = self.dir / "push"
        wants_push = any(item.get("source") == "push" for item in self.script)
        while not self._stop.is_set():
            if wants_push and push.is_dir():
                self._scan(push, "push")
            if pending.is_dir():
                self._scan(pending, "pending")
            time.sleep(0.03)

    def _scan(self, folder: Path, source: str) -> None:
        for path in sorted(folder.glob("*.json")):
            if path.name.endswith(".full.json") or path.name.startswith("."):
                continue
            try:
                doc = read_json(path)
            except (OSError, json.JSONDecodeError):
                continue
            seq, event = self._identify(doc)
            if not seq or seq in self.seen:
                continue
            if source == "push":
                item = self._choose(event, 0, doc, "push")
                if item is None:
                    continue
            else:
                self.counts[event] = self.counts.get(event, 0) + 1
                item = self._choose(event, self.counts[event], doc) or {"action": "none"}
            self.seen[seq] = item
            t = threading.Thread(target=self._act, args=(seq, event, doc, item), daemon=True)
            self._threads.append(t)
            t.start()

    def _act(self, seq: int, event: str, doc: dict, item: dict) -> None:
        action = item.get("action", "none")
        if item.get("delay_ms"):
            time.sleep(item["delay_ms"] / 1000.0)
        dest = self.dir / "replies" / f"{seq}.json"
        body = None
        if action == "none":
            self.log.append({"seq": seq, "event": event, "action": action})
            return
        if action == "late":
            wait = (self._deadline(doc) - datetime.now().astimezone()).total_seconds() + 0.3
            if wait > 0:
                time.sleep(wait)
            body = {"protocol": "ahp/1", "seq": seq, "decision": item.get("decision", "allow"),
                    "reason": item.get("reason", "late reply")}
        elif action == "invalid":
            body = item.get("raw", {"decision": "maybe"})
        elif action == "raw":
            body = item.get("raw")
        elif action in VALID:
            body = {"protocol": "ahp/1", "seq": seq, "decision": action}
            for key in ("reason", "updated_input", "content", "additional_context"):
                if key in item:
                    body[key] = item[key]
        if body is None:
            return
        text = body if isinstance(body, str) else json.dumps(body, ensure_ascii=False) + "\n"
        write_atomic(dest, text)
        self.log.append({"seq": seq, "event": event, "action": action, "at": time.time()})
