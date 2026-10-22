"""Consumer-side checks over one task directory.

Generic checks (G*) look only at the files a producer wrote. Scenario checks
(E*) compare what happened with what the scenario expects. Every check returns
(status, detail) with status in pass | fail | skip.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from .common import (CLOSED, DEFAULT_DECISION, Validators, get_path, is_truncation, parse_ts, read_json,
                     read_jsonl, walk_truncations)

Result = Tuple[str, str]
NAME_RE = re.compile(r"^(\d+)-(.+?)(\.full)?\.json$")
PRODUCER_FILES = ("capabilities.json", "events.jsonl", "seq")
PRODUCER_DIRS = ("pending", "push", "acks", "state")


class TaskView:
    """Parsed view of a task directory, loaded once per run."""

    def __init__(self, task_dir: Path) -> None:
        self.dir = task_dir
        self.caps = self._load(task_dir / "capabilities.json")
        self.events = read_jsonl(task_dir / "events.jsonl")
        self.notices: Dict[str, Dict[int, Tuple[Path, Any]]] = {"pending": {}, "push": {}}
        self.full: Dict[str, Tuple[Path, Any]] = {}
        for kind in ("pending", "push"):
            folder = task_dir / kind
            if not folder.is_dir():
                continue
            for path in folder.glob("*.json"):
                if path.name.startswith("."):
                    continue
                doc = self._load(path)
                if path.name.endswith(".full.json"):
                    self.full[f"{kind}/{path.name}"] = (path, doc)
                else:
                    m = NAME_RE.match(path.name)
                    seq = int(m.group(1)) if m else -1
                    self.notices[kind][seq] = (path, doc)
        self.acks: Dict[int, Any] = {}
        self.ack_files: List[Path] = []
        if (task_dir / "acks").is_dir():
            for path in (task_dir / "acks").glob("*.json"):
                if path.name.startswith("."):
                    continue
                self.ack_files.append(path)
                if path.stem.isdigit():
                    self.acks[int(path.stem)] = self._load(path)
        self.by_seq: Dict[int, dict] = {}
        for rec in self.events:
            if isinstance(rec, dict) and isinstance(rec.get("seq"), int):
                self.by_seq.setdefault(rec["seq"], rec)

    @staticmethod
    def _load(path: Path) -> Any:
        try:
            return read_json(path)
        except FileNotFoundError:
            return None
        except (OSError, json.JSONDecodeError) as exc:
            return {"__unreadable__": str(exc)}

    def records(self, include_internal: bool = False) -> List[dict]:
        out = [r for r in self.events if isinstance(r, dict) and "seq" in r]
        if not include_internal:
            out = [r for r in out if r.get("event") not in ("policy.deny", "producer.error")]
        return sorted(out, key=lambda r: r["seq"])


def _fail(items: List[str]) -> Result:
    return ("fail", "; ".join(items[:6]) + (f" (+{len(items) - 6} more)" if len(items) > 6 else "")) if items else ("pass", "")


# ---------------- generic checks ----------------

def g_capabilities(v: TaskView, val: Validators, ctx: dict) -> Result:
    if v.caps is None:
        return "fail", "capabilities.json missing (spec 3.3)"
    errs = val.errors("capabilities", v.caps)
    if errs is None:
        return "skip", val.note
    return _fail(errs)


def g_envelopes(v: TaskView, val: Validators, ctx: dict) -> Result:
    if not val.available:
        return "skip", val.note
    bad = []
    for kind, items in v.notices.items():
        for seq, (path, doc) in items.items():
            for e in val.errors("envelope", doc) or []:
                bad.append(f"{kind}/{path.name}: {e}")
    for rel, (path, doc) in v.full.items():
        for e in val.errors("envelope", doc) or []:
            bad.append(f"{rel}: {e}")
    return _fail(bad)


def g_events_log(v: TaskView, val: Validators, ctx: dict) -> Result:
    if not v.events:
        return "fail", "events.jsonl missing or empty (spec 4.1)"
    bad = []
    for i, rec in enumerate(v.events, 1):
        if "__bad_line__" in rec:
            bad.append(f"line {i}: not JSON")
            continue
        if "outcome" not in rec:
            bad.append(f"line {i}: no outcome (spec 5.3)")
        for e in (val.errors("envelope", rec) or []):
            bad.append(f"line {i} seq {rec.get('seq')}: {e}")
    return _fail(bad)


def g_acks_schema(v: TaskView, val: Validators, ctx: dict) -> Result:
    if not v.ack_files:
        return "skip", "no acks/"
    if not val.available:
        return "skip", val.note
    bad = [f"acks/{p.name}: {e}" for p in v.ack_files for e in (val.errors("ack", TaskView._load(p)) or [])]
    return _fail(bad)


def g_seq(v: TaskView, val: Validators, ctx: dict) -> Result:
    bad = []
    seqs = [r.get("seq") for r in v.events if isinstance(r, dict) and "__bad_line__" not in r]
    if any(not isinstance(s, int) or s < 1 for s in seqs):
        bad.append("non-positive or missing seq")
    dup = sorted({s for s in seqs if seqs.count(s) > 1 and isinstance(s, int)})
    if dup:
        bad.append(f"duplicate seq in events.jsonl: {dup}")
    for kind, items in v.notices.items():
        for seq, (path, doc) in items.items():
            if seq not in v.by_seq:
                bad.append(f"{kind}/{path.name} has no events.jsonl record")
    counter = v.dir / "seq"
    if counter.is_file() and seqs:
        raw = counter.read_text().strip()
        if raw.isdigit() and max(s for s in seqs if isinstance(s, int)) > int(raw):
            bad.append(f"seq counter {raw} < max seq used")
    return _fail(bad)


def g_no_tmp(v: TaskView, val: Validators, ctx: dict) -> Result:
    stray = [str(p.relative_to(v.dir)) for p in v.dir.rglob("*")
             if p.is_file() and (p.name.endswith(".tmp") or ".tmp." in p.name)]
    return _fail([f"stray temp file {s}" for s in stray])


def g_one_ack_per_gate(v: TaskView, val: Validators, ctx: dict) -> Result:
    gates = set(v.notices["pending"])
    acks = set(v.acks)
    errs = [r.get("seq") for r in v.records() if r.get("class") == "gate"
            and (r.get("outcome") or {}).get("via") == "error"]
    want = gates | {s for s in errs if isinstance(s, int)}
    # spec 7.3 #1: a reply to a non-gate event gets an invalid ack
    stray = {s for s in acks - want if isinstance(v.acks.get(s), dict) and v.acks[s].get("status") == "invalid"
             and v.acks[s].get("via") == "invalid_reply" and (v.dir / "replies" / f"{s}.json").exists()}
    bad = []
    if want - acks:
        bad.append(f"gates without ack: {sorted(want - acks)}")
    if acks - want - stray:
        bad.append(f"acks without gate: {sorted(acks - want - stray)}")
    return _fail(bad)


def g_ack_matches_outcome(v: TaskView, val: Validators, ctx: dict) -> Result:
    if not v.acks:
        return "skip", "no acks"
    bad = []
    for seq, ack in v.acks.items():
        rec = v.by_seq.get(seq)
        if not isinstance(ack, dict) or rec is None or rec.get("class") != "gate":
            continue  # non-gate acks (spec 7.3 #1) are checked by G07
        out = rec.get("outcome") or {}
        if out.get("decision") != ack.get("applied_decision"):
            bad.append(f"seq {seq}: ack {ack.get('applied_decision')} vs events {out.get('decision')}")
        if ack.get("status") != "consumed" and ack.get("applied_decision") != rec.get("default_decision",
                                                                                      _default_for(v, rec)):
            bad.append(f"seq {seq}: status {ack.get('status')} but applied {ack.get('applied_decision')} is not the default")
    return _fail(bad)


def _default_for(v: TaskView, rec: dict) -> Optional[str]:
    ev = rec.get("event")
    cap = ((v.caps or {}).get("events") or {}).get(ev) or {}
    notice = v.notices["pending"].get(rec.get("seq"))
    if notice and isinstance(notice[1], dict) and notice[1].get("default_decision"):
        return notice[1]["default_decision"]
    return cap.get("default_decision") or DEFAULT_DECISION.get(ev)


def g_fail_closed_defaults(v: TaskView, val: Validators, ctx: dict) -> Result:
    bad = []
    for seq, (path, doc) in v.notices["pending"].items():
        if not isinstance(doc, dict):
            continue
        ev, d = doc.get("event"), doc.get("default_decision")
        if d is None:
            bad.append(f"{path.name}: no default_decision")
            continue
        expected_closed = DEFAULT_DECISION.get(ev)
        if ev in ("tool.pre", "permission.request", "input.request", "model.switch.pre") and d not in CLOSED:
            bad.append(f"{path.name}: default {d} is not closed")
        elif expected_closed and d != expected_closed:
            bad.append(f"{path.name}: default {d} != spec {expected_closed}")
        if doc.get("decisions") and d not in doc["decisions"] and d != "ack":
            bad.append(f"{path.name}: default not in decisions")
        try:
            if parse_ts(doc["deadline"]) <= parse_ts(doc["ts"]):
                bad.append(f"{path.name}: deadline not after ts")
        except (KeyError, ValueError):
            bad.append(f"{path.name}: deadline/ts missing or unparsable")
    return _fail(bad)


def g_truncation(v: TaskView, val: Validators, ctx: dict) -> Result:
    bad = []
    checked = 0
    for kind, items in v.notices.items():
        for seq, (path, doc) in items.items():
            if not isinstance(doc, dict):
                continue
            hits = list(walk_truncations(doc.get("payload")))
            if hits and not (doc.get("truncated") is True and doc.get("full_file")):
                bad.append(f"{path.name}: truncated fields but no envelope truncated/full_file")
            for fpath, obj in hits:
                checked += 1
                rel = obj.get("full_file") or doc.get("full_file")
                if not rel:
                    bad.append(f"{path.name}:{'.'.join(map(str, fpath))}: no full_file")
                    continue
                full = v.full.get(rel)
                if full is None:
                    bad.append(f"{path.name}: full_file {rel} missing")
                    continue
                whole = get_path(full[1].get("payload") if isinstance(full[1], dict) else None, fpath)
                if whole is None:
                    bad.append(f"{rel}: field {'.'.join(map(str, fpath))} absent")
                    continue
                text = whole if isinstance(whole, str) else json.dumps(whole, ensure_ascii=False)
                if hashlib.sha256(text.encode("utf-8")).hexdigest() != obj.get("sha256"):
                    bad.append(f"{path.name}:{'.'.join(map(str, fpath))}: sha256 mismatch with full_file")
                if len(text) != obj.get("original_length"):
                    bad.append(f"{path.name}:{'.'.join(map(str, fpath))}: original_length {obj.get('original_length')} != {len(text)}")
                if not text.startswith(obj.get("text", "").replace("…[truncated]", "")):
                    bad.append(f"{path.name}: text is not a prefix of the full value")
    if not checked and not bad:
        return "skip", "no truncated fields"
    return _fail(bad)


def g_paths(v: TaskView, val: Validators, ctx: dict) -> Result:
    bad = []
    root = v.dir.resolve()
    for kind, items in v.notices.items():
        for seq, (path, doc) in items.items():
            if not isinstance(doc, dict):
                continue
            m = NAME_RE.match(path.name)
            if not m or int(m.group(1)) != doc.get("seq") or m.group(2) != doc.get("event"):
                bad.append(f"{kind}/{path.name}: name != <seq>-<event>.json ({doc.get('seq')}, {doc.get('event')})")
            if kind == "pending" and doc.get("reply_file") != f"replies/{doc.get('seq')}.json":
                bad.append(f"{path.name}: reply_file {doc.get('reply_file')}")
            if kind == "pending" and doc.get("class") != "gate":
                bad.append(f"{path.name}: class {doc.get('class')} in pending/")
            if kind == "push" and doc.get("class") != "notify":
                bad.append(f"{path.name}: class {doc.get('class')} in push/")
            for rel in [doc.get("full_file")] + [o.get("full_file") for _, o in walk_truncations(doc.get("payload"))]:
                if rel and not str((v.dir / rel).resolve()).startswith(str(root) + "/"):
                    bad.append(f"{path.name}: full_file escapes task dir: {rel}")
    if v.dir.is_symlink():
        bad.append("task dir is a symlink")
    return _fail(bad)


def g_class_consistency(v: TaskView, val: Validators, ctx: dict) -> Result:
    if v.caps is None:
        return "skip", "no capabilities.json"
    caps = v.caps.get("events") or {}
    bad = []
    for rec in v.records(include_internal=True):
        ev = rec.get("event")
        if ev not in caps:
            if not str(ev).startswith("x-"):
                bad.append(f"seq {rec['seq']}: {ev} not declared in capabilities")
            continue
        declared = caps[ev].get("class")
        via = (rec.get("outcome") or {}).get("via")
        if rec.get("class") != declared and via != "rule":
            bad.append(f"seq {rec['seq']}: {ev} class {rec.get('class')} != declared {declared}")
    return _fail(bad)


def g_rule_deny(v: TaskView, val: Validators, ctx: dict) -> Result:
    ruled = [r for r in v.records() if (r.get("outcome") or {}).get("via") == "rule" and r.get("event") == "tool.pre"]
    if not ruled:
        return "skip", "no rule denials"
    feature = ((v.caps or {}).get("features") or {}).get("policy_deny_event", True)
    notices = {}
    for seq, (path, doc) in v.notices["push"].items():
        if isinstance(doc, dict) and doc.get("event") == "policy.deny":
            notices[(doc.get("payload") or {}).get("target_seq")] = doc
    bad = []
    for r in ruled:
        seq = r["seq"]
        if (r.get("outcome") or {}).get("decision") != "deny":
            bad.append(f"seq {seq}: rule outcome {r['outcome'].get('decision')} != deny")
        if seq in v.notices["pending"] and seq not in v.acks:
            bad.append(f"seq {seq}: rule-denied tool.pre has a pending file")
        if seq not in v.notices["pending"] and seq in v.acks:
            bad.append(f"seq {seq}: ack written for a rule deny without pending")
        if feature and seq not in notices:
            bad.append(f"seq {seq}: no policy.deny notify (spec 11)")
    return _fail(bad)


def g_redaction(v: TaskView, val: Validators, ctx: dict) -> Result:
    secrets = [s for s in (ctx.get("secrets") or []) if s]
    if not secrets:
        return "skip", "scenario plants no secret"
    hits = []
    for p in v.dir.rglob("*"):
        if not p.is_file() or p.name == "consumer.json" or "replies" in p.relative_to(v.dir).parts:
            continue
        data = p.read_bytes()
        for s in secrets:
            if s.encode() in data:
                hits.append(f"{p.relative_to(v.dir)} contains planted secret {s[:6]}…")
    return _fail(hits)


def g_late_reply_ignored(v: TaskView, val: Validators, ctx: dict) -> Result:
    expired = {s: a for s, a in v.acks.items() if isinstance(a, dict) and a.get("status") == "expired"}
    if not expired:
        return "skip", "no expired acks"
    bad = []
    for seq, ack in expired.items():
        rec = v.by_seq.get(seq) or {}
        default = _default_for(v, rec)
        if default and ack.get("applied_decision") != default:
            bad.append(f"seq {seq}: expired but applied {ack.get('applied_decision')} != default {default}")
        if rec.get("event") == "turn.stop" and ack.get("accepted") is not False:
            bad.append(f"seq {seq}: expired turn.stop without accepted:false")
    return _fail(bad)


def g_timeout_clamp(v: TaskView, val: Validators, ctx: dict) -> Result:
    cj_path = v.dir / "consumer.json"
    if v.caps is None or not cj_path.is_file():
        return "skip", "no consumer.json/capabilities"
    try:
        cj = read_json(cj_path)
    except (OSError, json.JSONDecodeError):
        return "skip", "consumer.json unreadable"
    req = cj.get("timeouts_ms") or {}
    if not req:
        return "skip", "no timeouts requested"
    limits = v.caps.get("limits") or {}
    host = limits.get("host_hook_timeout_ms")
    ceiling = host - 10000 if host else limits.get("max_gate_timeout_ms")
    bad = []
    if not ceiling:
        return "fail", "neither host_hook_timeout_ms nor max_gate_timeout_ms declared (spec 9.2)"
    clamped = False
    for ev, ms in req.items():
        got = ((v.caps.get("events") or {}).get(ev) or {}).get("timeout_ms")
        want = max(1000, min(int(ms), ceiling))
        clamped = clamped or want != int(ms)
        if got != want:
            bad.append(f"{ev}: timeout_ms {got} != clamp({ms}) = {want}")
    if clamped and v.caps.get("consumer_json") != "partially_applied":
        bad.append(f"timeouts were clamped but consumer_json={v.caps.get('consumer_json')} (want partially_applied)")
    return _fail(bad)


def g_rules_declared(v: TaskView, val: Validators, ctx: dict) -> Result:
    if v.caps is None:
        return "skip", "no capabilities.json"
    rules = v.caps.get("rules")
    if not rules:
        return "fail", "capabilities.rules missing (spec 11)"
    status = rules.get("status") or {}
    bad = [f"rule {rid} not in status" for rid in rules.get("ids", []) if rid not in status]
    for rid in ctx.get("required_rules") or []:
        if status.get(rid) != "applied":
            bad.append(f"required rule {rid} is {status.get(rid, 'missing')} (consumer must refuse to start)")
    return _fail(bad)


GENERIC: List[Tuple[str, str, Callable[[TaskView, Validators, dict], Result]]] = [
    ("G01", "capabilities.json present and valid", g_capabilities),
    ("G02", "pending/push envelopes valid", g_envelopes),
    ("G03", "events.jsonl: one valid record with outcome per event", g_events_log),
    ("G04", "acks valid", g_acks_schema),
    ("G05", "seq unique, positive, every notice logged", g_seq),
    ("G06", "no stray temp files", g_no_tmp),
    ("G07", "exactly one ack per gate", g_one_ack_per_gate),
    ("G08", "ack agrees with outcome; non-consumed => default", g_ack_matches_outcome),
    ("G09", "gate defaults are fail-closed; deadline after ts", g_fail_closed_defaults),
    ("G10", "truncation objects: sha256/original_length/full_file", g_truncation),
    ("G11", "file names, reply_file, class/dir, path trust", g_paths),
    ("G12", "event classes match capabilities", g_class_consistency),
    ("G13", "static rule deny: no pending, policy.deny emitted", g_rule_deny),
    ("G14", "no planted secret in producer files", g_redaction),
    ("G15", "expired acks applied the default", g_late_reply_ignored),
    ("G16", "consumer timeouts clamped and reported", g_timeout_clamp),
    ("G17", "rules declared with status; required rules applied", g_rules_declared),
]


# ---------------- scenario expectations ----------------

def match_steps(v: TaskView, steps: List[dict]) -> Dict[int, dict]:
    """Step i -> the i-th unmatched record with the same event, in seq order."""
    pool = v.records()
    used = set()
    out = {}
    for i, step in enumerate(steps):
        for rec in pool:
            if rec["seq"] in used or rec.get("event") != step.get("event"):
                continue
            used.add(rec["seq"])
            out[i] = rec
            break
    return out


def expectation_checks(v: TaskView, scenario: dict, effects: Dict[int, dict]) -> List[Tuple[str, str, Result]]:
    steps = scenario.get("steps") or []
    for ev in scenario.get("requires_gate") or []:
        allowed = ((((v.caps or {}).get("events") or {}).get(ev) or {}).get("allowed_classes")) or []
        if "gate" not in allowed:
            return [("E00", f"requires {ev} as gate", ("skip", f"producer does not offer {ev} as a gate"))]
    matched = match_steps(v, steps)
    results = []
    for exp in scenario.get("expect") or []:
        i = exp["step"]
        rec = matched.get(i)
        label = f"step {i} ({steps[i]['event']})"
        if "effect" in exp:
            eff = effects.get(i, {}).get("effect")
            source = "effects.jsonl"
            if eff is None and rec is not None:
                eff, source = (rec.get("outcome") or {}).get("decision", "none"), "events.jsonl"
            results.append(("E01", f"{label} effect", ("pass", "") if eff == exp["effect"]
                            else ("fail", f"effect {eff!r} (from {source}), want {exp['effect']!r}")))
        if "injected" in exp:
            got = effects.get(i, {}).get("injected")
            results.append(("E12", f"{label} context injected", ("pass", "") if got is exp["injected"]
                            else ("fail", f"injected {got!r} (effects.jsonl), want {exp['injected']!r}")))
        if rec is None:
            if any(k in exp for k in ("via", "ack", "pending", "class", "accepted", "truncated", "payload_has")):
                results.append(("E00", f"{label} recorded", ("fail", "no events.jsonl record for this step")))
            continue
        seq = rec["seq"]
        out = rec.get("outcome") or {}
        if "via" in exp:
            results.append(("E02", f"{label} via", ("pass", "") if out.get("via") == exp["via"]
                            else ("fail", f"via {out.get('via')!r}, want {exp['via']!r}")))
        if "ack" in exp:
            ack = v.acks.get(seq)
            got = ack.get("status") if isinstance(ack, dict) else None
            results.append(("E03", f"{label} ack status", ("pass", "") if got == exp["ack"]
                            else ("fail", f"ack {got!r}, want {exp['ack']!r}")))
        if "accepted" in exp:
            ack = v.acks.get(seq) or {}
            results.append(("E04", f"{label} accepted", ("pass", "") if ack.get("accepted") == exp["accepted"]
                            else ("fail", f"accepted {ack.get('accepted')!r}, want {exp['accepted']!r}")))
        if "pending" in exp:
            has = seq in v.notices["pending"]
            results.append(("E05", f"{label} pending file", ("pass", "") if has == exp["pending"]
                            else ("fail", f"pending present={has}, want {exp['pending']}")))
        if "class" in exp:
            results.append(("E06", f"{label} class", ("pass", "") if rec.get("class") == exp["class"]
                            else ("fail", f"class {rec.get('class')!r}, want {exp['class']!r}")))
        if "truncated" in exp:
            notice = v.notices["pending"].get(seq) or v.notices["push"].get(seq)
            doc = notice[1] if notice else {}
            missing = [f for f in exp["truncated"] if not is_truncation(get_path((doc or {}).get("payload"), f.split(".")))]
            results.append(("E07", f"{label} truncated fields", _fail([f"{f} not a truncation object" for f in missing])
                            if notice else ("fail", "no pending/push notice")))
        if "payload_has" in exp:
            notice = v.notices["pending"].get(seq) or v.notices["push"].get(seq)
            payload = (notice[1] or {}).get("payload") if notice else rec.get("payload")
            missing = [k for k in exp["payload_has"] if get_path(payload, k.split(".")) in (None, "")]
            results.append(("E08", f"{label} payload fields", _fail([f"missing {k}" for k in missing])))
    for name, want in (scenario.get("expect_task") or {}).items():
        if name == "policy_deny_count":
            got = sum(1 for _, (_, d) in v.notices["push"].items() if isinstance(d, dict) and d.get("event") == "policy.deny")
            results.append(("E09", "policy.deny count", ("pass", "") if got == want else ("fail", f"{got}, want {want}")))
        if name == "producer_error":
            got = any(isinstance(d, dict) and d.get("event") == "producer.error" for _, (_, d) in v.notices["push"].items())
            results.append(("E10", "producer.error emitted", ("pass", "") if got == want else ("fail", f"{got}, want {want}")))
        if name == "limits":
            for key, val in want.items():
                got = ((v.caps or {}).get("limits") or {}).get(key)
                results.append(("E13", f"capabilities limits.{key}", ("pass", "") if got == val
                                else ("fail", f"{got!r}, want {val!r}")))
        if name == "capabilities_class":
            for ev, cls in want.items():
                got = (((v.caps or {}).get("events") or {}).get(ev) or {}).get("class")
                results.append(("E11", f"capabilities {ev} class", ("pass", "") if got == cls
                                else ("fail", f"{got!r}, want {cls!r}")))
    return results


def run_generic(v: TaskView, val: Validators, ctx: dict, skip=()) -> List[Tuple[str, str, Result]]:
    out = []
    for cid, title, fn in GENERIC:
        if cid in skip:
            out.append((cid, title, ("skip", "skipped by scenario")))
            continue
        try:
            out.append((cid, title, fn(v, val, ctx)))
        except Exception as exc:  # noqa: BLE001
            out.append((cid, title, ("fail", f"check crashed: {exc!r}")))
    return out
