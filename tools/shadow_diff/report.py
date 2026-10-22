"""Result object (JSON) and the human-readable summary."""
from __future__ import annotations

from typing import Any, Dict, List

from . import TOOL, VERSION
from .diff import Differ


def build_result(d: Differ, extra: Dict[str, Any] = None) -> Dict[str, Any]:
    cfg = d.cfg
    unexpected = [x for x in d.divs if not x["allowed_by"] and x["severity"] in ("critical", "error")]
    warns = [x for x in d.divs if not x["allowed_by"] and x["severity"] == "warn"]
    allowed = [x for x in d.divs if x["allowed_by"]]
    used = {x["allowed_by"] for x in allowed}
    fail = bool(unexpected) or (cfg.get("fail_on_warn") and bool(warns)) or bool(d.ref.errors or d.cand.errors)
    align = []
    for i, j in d.pairs:
        r = d.ref.records[i] if i is not None else None
        c = d.cand.records[j] if j is not None else None
        align.append({"ref": i, "cand": j, "ref_seq": r.seq if r else None, "cand_seq": c.seq if c else None,
                      "event": (r or c).event, "ref_label": r.label if r else None,
                      "cand_label": c.label if c else None})
    out = {
        "tool": TOOL, "version": VERSION, "protocol": "ahp/1",
        "result": "fail" if fail else "pass",
        "reference": d.ref.summary(), "candidate": d.cand.summary(),
        "config": {"sources": cfg.get("_sources") or [], "allow_entries": len(cfg.get("allow") or [])},
        "counts": {"critical": sum(x["severity"] == "critical" for x in unexpected),
                   "unexpected": len(unexpected), "warnings": len(warns), "allowed": len(allowed),
                   "aligned": sum(1 for i, j in d.pairs if i is not None and j is not None)},
        "divergences": d.divs,
        "unused_allow": [e["id"] for e in cfg.get("allow") or [] if e["id"] not in used],
        "alignment": align,
    }
    if extra:
        out.update(extra)
    return out


def _prod(p: Dict[str, Any]) -> str:
    if not p:
        return "?"
    return f"{p.get('name', '?')} {p.get('version', '')}".strip()


def summary_text(res: Dict[str, Any], verbose: bool = False) -> str:
    ref, cand, n = res["reference"], res["candidate"], res["counts"]
    lines: List[str] = [
        f"AHP shadow diff  reference: {_prod(ref['producer'])} ({ref['events_compared']}/{ref['events_total']} events)"
        f"  candidate: {_prod(cand['producer'])} ({cand['events_compared']}/{cand['events_total']} events)",
    ]
    if res.get("scenario"):
        lines.append(f"scenario: {res['scenario']}")
    for side, s in (("reference", ref), ("candidate", cand)):
        for e in s["errors"]:
            lines.append(f"  INPUT {side}: {e}")
    lines.append(f"result: {res['result'].upper()}  {n['unexpected']} unexpected ({n['critical']} critical), "
                 f"{n['warnings']} warnings, {n['allowed']} allowed, {n['aligned']} events aligned")
    tag = {"critical": "CRIT", "error": "DIFF", "warn": "WARN"}
    order = {"critical": 0, "error": 1, "warn": 2}
    for d in sorted((x for x in res["divergences"] if not x["allowed_by"]), key=lambda x: (order[x["severity"]], x["code"])):
        lines.append(f"  {tag[d['severity']]} {d['code']} {d['kind']:12} {d['message']}")
    allowed = [x for x in res["divergences"] if x["allowed_by"]]
    if allowed:
        if verbose:
            for d in allowed:
                lines.append(f"  ok   {d['code']} {d['kind']:12} {d['message']}  [allowed: {d['allowed_by']}]")
        else:
            by: Dict[str, int] = {}
            for d in allowed:
                by[d["allowed_by"]] = by.get(d["allowed_by"], 0) + 1
            lines.append("  allowed: " + ", ".join(f"{k}×{v}" for k, v in sorted(by.items())))
    if res["unused_allow"] and verbose:
        lines.append("  unused allow entries: " + ", ".join(res["unused_allow"]))
    if verbose:
        lines.append("  alignment:")
        for a in res["alignment"]:
            lines.append(f"    {str(a['ref']) if a['ref'] is not None else '-':>3} {str(a['cand']) if a['cand'] is not None else '-':>3}  "
                         f"{a['ref_label'] or ''}  |  {a['cand_label'] or ''}")
    return "\n".join(lines)
