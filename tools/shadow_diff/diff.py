"""Align two normalised record sequences and report the divergences that matter.

Codes (docs/shadow-diff.md):
  D00 conformance   a tree fails a conformance generic check (only with conformance: true)
  D01 missing       event in the reference, absent in the candidate
  D02 extra         event in the candidate, absent in the reference (e.g. an extra tool call)
  D03 class         same event, different effective class
  D04 decision      gate outcome.decision / ack.applied_decision differ
  D05 via           how the gate was decided differs (reply|timeout|rule|error|invalid_reply, ack status)
  D06 gate shape    pending present, decisions, default_decision differ
  D07 ack           ack missing on one side, or turn.stop accepted differs
  D08 payload       compared payload fields differ (tool.pre tool_kind / command, plan items, ...)
  D09 policy        static-rule outcome differs (rule_id, policy.deny rule/target)
  D10 timeout       gate timeout_ms differs
  D11 fail-open     a gate (or a declared default) that is not fail-closed. critical, never allowlisted
  D12 capabilities  capabilities.json differs (classes, decisions, defaults, timeouts, features, limits, rules)
  D13 host effect   what the host actually did differs (run mode, harness effects.jsonl)
  D16 consumer      the consumer replied differently, or consumer.json differs (warning: not a fair comparison)
"""
from __future__ import annotations

import hashlib
import json
from difflib import SequenceMatcher
from typing import Any, Dict, List, Optional, Tuple

from conformance.common import CLOSED, DEFAULT_DECISION, version_core

from .config import find_allow
from .model import GATE_NON_CONSUMED_VIA, Rec, Side

CODE_NAMES = {
    "D00": "conformance", "D01": "missing", "D02": "extra", "D03": "class", "D04": "decision",
    "D05": "via", "D06": "gate-shape", "D07": "ack", "D08": "payload", "D09": "policy",
    "D10": "timeout", "D11": "fail-open", "D12": "capabilities", "D13": "host-effect", "D16": "consumer",
}
FIELD_CODES = [
    ("class", "D03"), ("outcome.decision", "D04"), ("ack.applied_decision", "D04"),
    ("outcome.via", "D05"), ("ack.via", "D05"), ("ack.status", "D05"),
    ("gate.pending", "D06"), ("gate.decisions", "D06"), ("gate.default_decision", "D06"),
    ("ack.present", "D07"), ("ack.accepted", "D07"), ("outcome.rule_id", "D09"),
    ("gate.timeout_ms", "D10"), ("reply.decision", "D16"),
]
WARN_CODES = {"D16"}


DP_LIMIT = 4_000_000  # cells; above this fall back to difflib (two-pass)


def align(ref: List[Rec], cand: List[Rec]) -> List[Tuple[Optional[int], Optional[int]]]:
    """Order-preserving alignment that maximises matches: an exact key (event + command / rule)
    scores 3, the same event name 2, anything else cannot pair. Common prefix/suffix are taken
    as-is; the middle is a weighted LCS (difflib two-pass for very long middles)."""
    n, m = len(ref), len(cand)
    lo = 0
    while lo < n and lo < m and ref[lo].key == cand[lo].key:
        lo += 1
    hi_r, hi_c = n, m
    while hi_r > lo and hi_c > lo and ref[hi_r - 1].key == cand[hi_c - 1].key:
        hi_r, hi_c = hi_r - 1, hi_c - 1
    mid = (_align_dp if (hi_r - lo) * (hi_c - lo) <= DP_LIMIT else _align_difflib)(ref[lo:hi_r], cand[lo:hi_c])
    pairs: List[Tuple[Optional[int], Optional[int]]] = [(i, i) for i in range(lo)]
    pairs += [(None if a is None else a + lo, None if b is None else b + lo) for a, b in mid]
    pairs += [(hi_r + k, hi_c + k) for k in range(n - hi_r)]
    return pairs


def _w(r: Rec, c: Rec) -> int:
    return 3 if r.key == c.key else (2 if r.event == c.event else 0)


def _align_dp(ref: List[Rec], cand: List[Rec]) -> List[Tuple[Optional[int], Optional[int]]]:
    n, m = len(ref), len(cand)
    score = [[0] * (m + 1) for _ in range(n + 1)]
    for i in range(1, n + 1):
        row, prev, r = score[i], score[i - 1], ref[i - 1]
        for j in range(1, m + 1):
            best = prev[j] if prev[j] >= row[j - 1] else row[j - 1]
            w = _w(r, cand[j - 1])
            if w and prev[j - 1] + w > best:
                best = prev[j - 1] + w
            row[j] = best
    out: List[Tuple[Optional[int], Optional[int]]] = []
    i, j = n, m
    while i > 0 or j > 0:
        if i > 0 and j > 0:
            w = _w(ref[i - 1], cand[j - 1])
            if w and score[i][j] == score[i - 1][j - 1] + w:
                out.append((i - 1, j - 1))
                i, j = i - 1, j - 1
                continue
        if i > 0 and score[i][j] == score[i - 1][j]:
            out.append((i - 1, None))
            i -= 1
        else:
            out.append((None, j - 1))
            j -= 1
    out.reverse()
    return out


def _align_difflib(ref: List[Rec], cand: List[Rec]) -> List[Tuple[Optional[int], Optional[int]]]:
    pairs: List[Tuple[Optional[int], Optional[int]]] = []
    sm = SequenceMatcher(None, [r.key for r in ref], [c.key for c in cand], autojunk=False)
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            pairs += list(zip(range(i1, i2), range(j1, j2)))
            continue
        sub = SequenceMatcher(None, [r.event for r in ref[i1:i2]], [c.event for c in cand[j1:j2]], autojunk=False)
        for t2, a1, a2, b1, b2 in sub.get_opcodes():
            if t2 == "equal":
                pairs += [(i1 + a, j1 + b) for a, b in zip(range(a1, a2), range(b1, b2))]
            else:
                pairs += [(i1 + a, None) for a in range(a1, a2)]
                pairs += [(None, j1 + b) for b in range(b1, b2)]
    return pairs


def _same(a: Any, b: Any) -> bool:
    if a == b:
        return True
    for x, y in ((a, b), (b, a)):  # truncated on one side only: compare the hash of the full text
        if isinstance(x, dict) and x.get("__truncated__") and isinstance(y, str):
            return x.get("sha256") == hashlib.sha256(y.encode("utf-8")).hexdigest()
    return False


class Differ:
    def __init__(self, ref: Side, cand: Side, cfg: dict) -> None:
        self.ref, self.cand, self.cfg = ref, cand, cfg
        self.divs: List[dict] = []
        self.pairs: List[Tuple[Optional[int], Optional[int]]] = []

    def add(self, code: str, path: str, message: str, *, event: Optional[str] = None,
            ref_index: Optional[int] = None, cand_index: Optional[int] = None, side: Optional[str] = None,
            **values: Any) -> None:
        sev = "critical" if code == "D11" else ("warn" if code in WARN_CODES else "error")
        div = {"code": code, "kind": CODE_NAMES[code], "severity": sev, "path": path, "event": event,
               "ref_index": ref_index, "cand_index": cand_index, "message": message}
        if side:
            div["side"] = side
        for k in ("ref", "cand"):
            if k in values:
                div[k] = values[k]
        for k in ("ref_seq", "cand_seq"):
            if values.get(k) is not None:
                div[k] = values[k]
        entry = None if sev == "critical" else find_allow(self.cfg, div)
        div["allowed_by"] = entry["id"] if entry else None
        if entry:
            div["allow_reason"] = entry.get("reason")
        self.divs.append(div)

    # ---------- sequence ----------
    def run(self) -> None:
        self.pairs = align(self.ref.records, self.cand.records)
        mapping = {i: j for i, j in self.pairs if i is not None and j is not None}
        for i, j in self.pairs:
            r = self.ref.records[i] if i is not None else None
            c = self.cand.records[j] if j is not None else None
            if r and not c:
                self.add("D01", f"sequence.missing.{r.event}", f"ref#{i} {r.label}: missing in candidate",
                         event=r.event, ref_index=i, ref=r.label, ref_seq=r.seq)
            elif c and not r:
                self.add("D02", f"sequence.extra.{c.event}", f"cand#{j} {c.label}: not in reference",
                         event=c.event, cand_index=j, cand=c.label, cand_seq=c.seq)
            else:
                self.compare_pair(r, c, mapping)
        for side in (self.ref, self.cand):
            self.fail_open(side)
        self.capabilities()

    def compare_pair(self, r: Rec, c: Rec, mapping: Dict[int, int]) -> None:
        keys = sorted(set(r.fields) | set(c.fields))
        code_of = dict(FIELD_CODES)
        if r.fields.get("class") != c.fields.get("class"):
            # a class change (D03) implies every gate/ack/reply difference; report it once
            keys = [k for k in keys if k == "class" or k.startswith("payload.")]
        elif r.fields.get("ack.present") != c.fields.get("ack.present"):
            keys = [k for k in keys if not k.startswith("ack.") or k == "ack.present"]
        for k in keys:
            a, b = r.fields.get(k, "<absent>"), c.fields.get(k, "<absent>")
            if _same(a, b):
                continue
            if k.startswith("payload."):
                code = "D09" if r.event == "policy.deny" else "D08"
            else:
                code = code_of.get(k)
                if code is None:
                    continue
            self.add(code, f"{r.event}.{k}", f"ref#{r.index}/cand#{c.index} {r.event} {k}: {a!r} != {b!r}",
                     event=r.event, ref_index=r.index, cand_index=c.index, ref=a, cand=b,
                     ref_seq=r.seq, cand_seq=c.seq)
        if r.target_index is not None or c.target_index is not None:
            want = mapping.get(r.target_index) if r.target_index not in (None, -1) else r.target_index
            if want != c.target_index:
                code = "D09" if r.event == "policy.deny" else "D08"
                self.add(code, f"{r.event}.target", f"ref#{r.index}/cand#{c.index} {r.event} points at different "
                         f"events (ref target #{r.target_index}, cand target #{c.target_index})",
                         event=r.event, ref_index=r.index, cand_index=c.index,
                         ref=r.target_index, cand=c.target_index)

    # ---------- fail-closed (per side, independent of alignment) ----------
    def fail_open(self, side: Side) -> None:
        for rec in side.records:
            if rec.cls != "gate":
                continue
            f = rec.fields
            default = side.default_decision(rec.raw)
            idx = {"ref_index": rec.index} if side.name == "ref" else {"cand_index": rec.index}
            non_consumed = f.get("ack.status") in ("expired", "invalid") or f.get("outcome.via") in GATE_NON_CONSUMED_VIA
            applied = f.get("ack.applied_decision", f.get("outcome.decision"))
            spec_default = DEFAULT_DECISION.get(rec.event)
            if default and spec_default in CLOSED and default not in CLOSED:
                self.add("D11", f"fail_open.{rec.event}.default_decision",
                         f"{side.name}#{rec.index} {rec.label}: default_decision {default!r} is not fail-closed "
                         f"(spec {spec_default!r})", event=rec.event, side=side.name, **idx)
            if non_consumed and default and applied != default:
                self.add("D11", f"fail_open.{rec.event}.applied",
                         f"{side.name}#{rec.index} {rec.label}: {f.get('ack.status') or f.get('outcome.via')} but applied "
                         f"{applied!r}, default is {default!r}", event=rec.event, side=side.name, **idx)
            if rec.event == "turn.stop" and non_consumed and f.get("ack.accepted") is not False:
                self.add("D11", "fail_open.turn.stop.accepted",
                         f"{side.name}#{rec.index} turn.stop ended without a valid ack but accepted is "
                         f"{f.get('ack.accepted')!r} (spec 9.3: must be false)", event=rec.event, side=side.name, **idx)
            if rec.event == "turn.stop" and f.get("outcome.via") == "rule" and f.get("ack.accepted") is True:
                self.add("D11", "fail_open.turn.stop.accepted",
                         f"{side.name}#{rec.index} turn.stop forced to ack by max_blocks but accepted is true "
                         f"(spec 9.3 #5)", event=rec.event, side=side.name, **idx)
            if (f.get("outcome.via") == "rule" and spec_default in CLOSED
                    and f.get("outcome.decision") not in CLOSED):
                self.add("D11", f"fail_open.{rec.event}.rule",
                         f"{side.name}#{rec.index} {rec.label}: static rule matched but decision is "
                         f"{f.get('outcome.decision')!r}", event=rec.event, side=side.name, **idx)

    # ---------- capabilities ----------
    def capabilities(self) -> None:
        cj = [self._consumer_json(s) for s in (self.ref, self.cand)]
        if cj[0] != cj[1]:
            self.add("D16", "consumer_json", "consumer.json differs between the two runs (or exists on one side "
                     "only): the producers were not asked the same thing", ref=cj[0] is not None, cand=cj[1] is not None)
        rc, cc = self.ref.caps, self.cand.caps
        for side, caps in (("ref", rc), ("cand", cc)):
            if caps is None:
                self.add("D12", f"capabilities.{side}.missing", f"{side}: capabilities.json missing or unreadable",
                         side=side)
        if rc is None or cc is None:
            return
        a, b = flatten_caps(rc), flatten_caps(cc)
        prefix = "capabilities.events."
        one_sided = {k[len(prefix):-len(".present")] for k in set(a) ^ set(b) if k.startswith(prefix)
                     and k.endswith(".present")}
        for k in sorted(set(a) | set(b)):
            x, y = a.get(k, "<absent>"), b.get(k, "<absent>")
            if x == y:
                continue
            ev = k[len(prefix):].rsplit(".", 1)[0] if k.startswith(prefix) else None
            if ev in one_sided and not k.endswith(".present"):
                continue  # the event itself is declared on one side only: one divergence, not one per field
            if ev and k.rsplit(".", 1)[1] in GATE_ONLY_CAP_FIELDS and not (_gate_capable(rc, ev) and _gate_capable(cc, ev)):
                continue  # gate settings matter only where both sides can run the event as a gate
            self.add("D12", k, f"{k}: {x!r} != {y!r}", event=ev, ref=x, cand=y)
        for ev, cap in (cc.get("events") or {}).items():
            spec_default = DEFAULT_DECISION.get(ev)
            dd = (cap or {}).get("default_decision")
            if dd and spec_default in CLOSED and dd not in CLOSED and (cap.get("class") == "gate"
                                                                     or "gate" in (cap.get("allowed_classes") or [])):
                self.add("D11", f"fail_open.capabilities.{ev}.default_decision",
                         f"candidate declares default_decision {dd!r} for {ev} (spec {spec_default!r})",
                         event=ev, side="cand")

    @staticmethod
    def _consumer_json(side: Side) -> Any:
        path = side.dir / "consumer.json"
        if not path.is_file():
            return None
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return "<unreadable>"
        return {k: v for k, v in doc.items() if k != "created_at"} if isinstance(doc, dict) else doc

    # ---------- extras ----------
    def conformance(self) -> None:
        from conformance.checks import run_generic
        from conformance.common import Validators
        val = Validators()
        for side in (self.ref, self.cand):
            for cid, title, (status, detail) in run_generic(side.view, val, {}):
                if status == "fail":
                    self.add("D00", f"conformance.{side.name}.{cid}", f"{side.name}: {cid} {title}: {detail}",
                             side=side.name)

    def host_effects(self, ref_fx: Dict[int, dict], cand_fx: Dict[int, dict]) -> None:
        canon: Dict[str, str] = {}
        for group in self.cfg.get("effect_equivalents") or []:
            for name in group:
                canon[name] = group[0]
        for step in sorted(set(ref_fx) | set(cand_fx)):
            r, c = ref_fx.get(step) or {}, cand_fx.get(step) or {}
            for k in ("effect", "injected"):
                a, b = r.get(k), c.get(k)
                if k == "effect":
                    a, b = canon.get(a, a), canon.get(b, b)
                if a != b:
                    ev = r.get("event") or c.get("event")
                    self.add("D13", f"host.{ev}.{k}", f"step {step} {ev}: host {k} {r.get(k)!r} != {c.get(k)!r}",
                             event=ev, ref=r.get(k), cand=c.get(k))


GATE_ONLY_CAP_FIELDS = {"decisions", "default_decision", "timeout_ms"}


def _gate_capable(caps: dict, ev: str) -> bool:
    cap = (caps.get("events") or {}).get(ev) or {}
    return cap.get("class") == "gate" or "gate" in (cap.get("allowed_classes") or [])


def _set(v: Any) -> Any:
    return sorted(v) if isinstance(v, list) else v


def flatten_caps(caps: dict) -> Dict[str, Any]:
    """capabilities.json -> {dotted path: comparable value}, without noise fields."""
    out: Dict[str, Any] = {}
    for k in ("protocol", "transport", "consumer_json"):
        if k in caps:
            out[f"capabilities.{k}"] = caps[k]
    core = version_core(caps.get("spec_version"))
    out["capabilities.spec_version"] = f"1.{core[0]}.{core[1]}" if core else caps.get("spec_version")
    for group in ("features", "limits"):
        for k, v in (caps.get(group) or {}).items():
            if not str(k).startswith("x-"):
                out[f"capabilities.{group}.{k}"] = v
    rules = caps.get("rules") or {}
    out["capabilities.rules.ids"] = _set(rules.get("ids") or [])
    out["capabilities.rules.required"] = _set(rules.get("required") or [])
    if "sha256" in rules:
        out["capabilities.rules.sha256"] = rules["sha256"]
    for rid, st in (rules.get("status") or {}).items():
        out[f"capabilities.rules.status.{rid}"] = st
    red = caps.get("redaction") or {}
    if "methods" in red:
        out["capabilities.redaction.methods"] = _set(red["methods"])
    for ev, cap in (caps.get("events") or {}).items():
        if str(ev).startswith("x-") or not isinstance(cap, dict):
            continue
        base = f"capabilities.events.{ev}"
        out[f"{base}.present"] = True
        for k in ("class", "default_decision", "timeout_ms"):
            if k in cap:
                out[f"{base}.{k}"] = cap[k]
        for k in ("allowed_classes", "decisions"):
            if k in cap:
                out[f"{base}.{k}"] = _set(cap[k])
    return out
