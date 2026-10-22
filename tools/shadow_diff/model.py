"""Load one producer's AHP task tree and reduce it to comparable, noise-free records.

Noise that is dropped or rewritten (docs/shadow-diff.md, "Normalisation"):
- ts, deadline, created_at, reply_sha256, tool_use_id, task_id, producer, context, native, x-*;
- seq: records are compared by position in the filtered sequence (`index`), and
  policy.deny `target_seq` becomes the target's index;
- paths: the task dir, the AHP root, every context.cwd and $HOME become placeholders;
- truncation: payloads come from the pending/push notice (or its .full.json), not from
  the audit log; a remaining truncation object compares by sha256;
- shell commands: whitespace collapsed, `bash -c 'X'` unwrapped (configurable).
"""
from __future__ import annotations

import fnmatch
import hashlib
import os
import re
import shlex
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from conformance.checks import TaskView
from conformance.common import DEFAULT_DECISION, get_path, is_truncation, read_json

SHELLS = {"sh", "bash", "zsh", "dash", "ksh", "/bin/sh", "/bin/bash", "/bin/zsh", "/usr/bin/bash", "/usr/bin/env"}
GATE_NON_CONSUMED_VIA = {"timeout", "error", "invalid_reply"}


@dataclass
class Rec:
    index: int
    seq: int
    event: str
    cls: str
    key: Tuple
    label: str
    fields: Dict[str, Any] = field(default_factory=dict)
    target_index: Optional[int] = None   # policy.deny: index of the denied record
    raw: dict = field(default_factory=dict)


class Side:
    """One producer's task tree. `name` is 'ref' or 'cand'."""

    def __init__(self, name: str, task_dir: Path, cfg: dict) -> None:
        self.name = name
        self.dir = Path(task_dir)
        self.cfg = cfg
        self.errors: List[str] = []
        if not self.dir.is_dir():
            raise FileNotFoundError(f"{name}: task dir not found: {self.dir}")
        self.view = TaskView(self.dir)
        if not self.view.events:
            self.errors.append("events.jsonl missing or empty")
        self.caps = self.view.caps if isinstance(self.view.caps, dict) else None
        first = next((r for r in self.view.events if isinstance(r, dict) and "producer" in r), {})
        self.producer = (self.caps or {}).get("producer") or first.get("producer") or {}
        self.placeholders = self._placeholders() if cfg.get("path_placeholders", True) else []
        self.all_records = [r for r in self.view.records(include_internal=True) if "__bad_line__" not in r]
        self.records: List[Rec] = []

    # ---------- normalisation helpers ----------
    def _placeholders(self) -> List[Tuple[str, str]]:
        pairs: List[Tuple[str, str]] = []

        def add(path: Optional[str], token: str) -> None:
            if not path or not isinstance(path, str) or len(path) < 2:
                return
            variants = {path.rstrip("/")}
            try:
                variants.add(os.path.realpath(path).rstrip("/"))
            except OSError:
                pass
            for v in list(variants):  # macOS: /tmp == /private/tmp
                if v.startswith("/private/"):
                    variants.add(v[len("/private"):])
            for v in variants:
                if v and v != "/":
                    pairs.append((v, token))

        add(str(self.dir), "<task>")
        if self.dir.parent.name == "tasks":
            add(str(self.dir.parent.parent), "<root>")
        for rec in self.view.events:
            if isinstance(rec, dict):
                add(get_path(rec, ("context", "cwd")), "<cwd>")
        add(os.path.expanduser("~"), "<home>")
        # longest first so <task> wins over <root>, <cwd> over <home>
        return sorted(set(pairs), key=lambda p: -len(p[0]))

    def norm_str(self, text: str) -> str:
        for prefix, token in self.placeholders:
            text = text.replace(prefix, token)
        return text

    def norm_value(self, value: Any) -> Any:
        if is_truncation(value):
            return {"__truncated__": True, "sha256": value.get("sha256"), "original_length": value.get("original_length")}
        if isinstance(value, str):
            return self.norm_str(value)
        if isinstance(value, list):
            return [self.norm_value(v) for v in value]
        if isinstance(value, dict):
            return {k: self.norm_value(v) for k, v in value.items() if not str(k).startswith("x-")}
        return value

    def norm_command(self, value: Any) -> Any:
        if not isinstance(value, str):
            return self.norm_value(value)
        opts = self.cfg.get("command") or {}
        text = value
        if opts.get("unwrap_shell_c", True):
            text = unwrap_shell(text)
        if opts.get("canonical_quoting", True):
            text = canonical_command(text)
        elif opts.get("collapse_whitespace", True):
            text = " ".join(text.split())
        return self.norm_str(text)

    # ---------- payload source ----------
    def payload_for(self, rec: dict) -> dict:
        """Best full payload: .full.json > pending/push notice > audit record."""
        seq = rec.get("seq")
        payload = rec.get("payload") if isinstance(rec.get("payload"), dict) else {}
        for kind in ("pending", "push"):
            hit = self.view.notices[kind].get(seq)
            if hit and isinstance(hit[1], dict) and hit[1].get("event") == rec.get("event"):
                doc = hit[1]
                payload = doc.get("payload") if isinstance(doc.get("payload"), dict) else payload
                full = doc.get("full_file")
                if doc.get("truncated") and isinstance(full, str):
                    fdoc = self.view.full.get(full)
                    if fdoc and isinstance(fdoc[1], dict) and isinstance(fdoc[1].get("payload"), dict):
                        payload = fdoc[1]["payload"]
                break
        return payload

    def notice(self, rec: dict) -> Optional[dict]:
        hit = self.view.notices["pending"].get(rec.get("seq"))
        if hit and isinstance(hit[1], dict) and hit[1].get("event") == rec.get("event"):
            return hit[1]
        return None

    def reply(self, seq: int) -> Any:
        path = self.dir / "replies" / f"{seq}.json"
        if not path.is_file() or path.is_symlink():
            return None
        try:
            return read_json(path)
        except Exception:  # noqa: BLE001
            return {"__unreadable__": True}

    def default_decision(self, rec: dict) -> Optional[str]:
        note = self.notice(rec) or {}
        if note.get("default_decision"):
            return note["default_decision"]
        cap = ((self.caps or {}).get("events") or {}).get(rec.get("event")) or {}
        return cap.get("default_decision") or DEFAULT_DECISION.get(rec.get("event"))

    # ---------- build ----------
    def build(self, keep_events: set) -> None:
        pf = self.cfg.get("payload_fields") or {}
        seq_to_index: Dict[int, int] = {}
        out: List[Rec] = []
        for rec in self.all_records:
            ev = rec.get("event")
            if ev not in keep_events:
                continue
            payload = self.payload_for(rec)
            fields: Dict[str, Any] = {"class": rec.get("class")}
            names = list(pf.get(ev) or [])
            if self.cfg.get("compare_tool_name") and ev in ("tool.pre", "permission.request", "tool.post", "tool.error"):
                names.append("tool_name")
            for name in names:
                value = get_path(payload, tuple(name.split(".")))
                if value is None and name not in payload:
                    continue
                fields[f"payload.{name}"] = self.norm_command(value) if name == "command" else self.norm_value(value)
            if ev == "plan.update" and isinstance(fields.get("payload.items"), list):
                fields["payload.items"] = [{"content": i.get("content"), "status": i.get("status")}
                                           if isinstance(i, dict) else i for i in fields["payload.items"]]
            if rec.get("class") == "gate":
                self._gate_fields(rec, fields)
            cmd = fields.get("payload.command")
            if ev == "tool.pre":
                key: Tuple = (ev, cmd if isinstance(cmd, str) else repr(cmd))
            elif ev == "policy.deny":
                key = (ev, fields.get("payload.rule_id"))
            else:
                key = (ev,)
            seq_to_index[rec["seq"]] = len(out)
            out.append(Rec(index=len(out), seq=rec["seq"], event=ev, cls=rec.get("class"), key=key,
                           label=label(ev, rec.get("class"), fields), fields=fields, raw=rec))
        for r in out:  # policy.deny / producer.error point at another event by seq
            tseq = get_path(r.raw, ("payload", "target_seq"))
            if isinstance(tseq, int):
                r.target_index = seq_to_index.get(tseq, -1)
        self.records = out

    def _gate_fields(self, rec: dict, fields: Dict[str, Any]) -> None:
        outcome = rec.get("outcome") if isinstance(rec.get("outcome"), dict) else {}
        for k in ("decision", "via", "rule_id"):
            if outcome.get(k) is not None:
                fields[f"outcome.{k}"] = outcome[k]
        note = self.notice(rec)
        rule_denied = outcome.get("via") == "rule" and note is None
        fields["gate.pending"] = note is not None
        if note is not None:
            for k in ("default_decision", "timeout_ms"):
                if note.get(k) is not None:
                    fields[f"gate.{k}"] = note[k]
            if isinstance(note.get("decisions"), list):
                fields["gate.decisions"] = sorted(note["decisions"])
        ack = self.view.acks.get(rec["seq"])
        if not rule_denied:
            fields["ack.present"] = isinstance(ack, dict)
        if isinstance(ack, dict):
            for k in ("status", "via", "applied_decision", "accepted"):
                if k in ack:
                    fields[f"ack.{k}"] = ack[k]
        reply = self.reply(rec["seq"])
        if isinstance(reply, dict):
            fields["reply.decision"] = reply.get("decision", reply.get("behavior"))

    def summary(self) -> dict:
        return {"dir": str(self.dir), "producer": self.producer,
                "spec_version": (self.caps or {}).get("spec_version"),
                "events_total": len(self.all_records), "events_compared": len(self.records),
                "errors": self.errors}


def unwrap_shell(cmd: str) -> str:
    """`bash -c 'X'`, `sh -lc "X"`, `/usr/bin/env bash -c X` -> `X`; anything else unchanged."""
    try:
        argv = shlex.split(cmd)
    except ValueError:
        return cmd
    if argv[:1] == ["/usr/bin/env"] or argv[:1] == ["env"]:
        argv = argv[1:]
    if len(argv) == 3 and (argv[0] in SHELLS or os.path.basename(argv[0]) in SHELLS) and re.fullmatch(r"-l?c", argv[1]):
        return argv[2]
    return cmd


SHELL_OPERATORS = re.compile(r"^[;&|<>()]+$")


def canonical_command(cmd: str) -> str:
    """Re-quote a shell command canonically so `ls '/var/log'` == `ls /var/log` (shlex.join of
    shlex.split, operators left bare). Unparseable strings only get whitespace collapsed."""
    try:
        argv = shlex.split(cmd, posix=True)
    except ValueError:
        return " ".join(cmd.split())
    return " ".join(a if SHELL_OPERATORS.match(a) else shlex.quote(a) for a in argv)


def label(event: str, cls: Optional[str], fields: Dict[str, Any]) -> str:
    bits = [f"{event}({cls})"]
    if fields.get("payload.tool_kind"):
        bits.append(f"[{fields['payload.tool_kind']}]")
    cmd = fields.get("payload.command")
    if isinstance(cmd, str):
        bits.append(repr(cmd if len(cmd) <= 60 else cmd[:57] + "..."))
    if fields.get("payload.rule_id"):
        bits.append(f"rule={fields['payload.rule_id']}")
    if fields.get("outcome.decision"):
        bits.append(f"-> {fields['outcome.decision']}/{fields.get('outcome.via')}")
    return " ".join(bits)


def keep_events(ref: Side, cand: Side, cfg: dict) -> set:
    """Events compared: not ignored by name, and not log-only on every side where they occur."""
    classes: Dict[str, set] = {}
    for side in (ref, cand):
        for rec in side.all_records:
            classes.setdefault(rec.get("event"), set()).add(rec.get("class"))
    ignore_cls = set(cfg.get("ignore_classes") or [])
    out = set()
    for ev, cls in classes.items():
        if any(fnmatch.fnmatchcase(ev or "", pat) for pat in cfg.get("ignore_events") or []):
            continue
        if cls and cls <= ignore_cls:
            continue
        out.add(ev)
    return out


def value_hash(value: Any) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest() if isinstance(value, str) else ""
