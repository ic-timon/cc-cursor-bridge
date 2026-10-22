"""CLI. Exit 0 = no unexpected divergence, 1 = unexpected divergence (or bad tree), 2 = usage/config error.

  python3 -m tools.shadow_diff diff REF CAND [--task ID] [--config F]... [--json OUT] [-v] [--quiet]
      REF / CAND are task dirs (<root>/tasks/<task>), or AHP roots when --task is given.
  python3 -m tools.shadow_diff run --reference NAME|CMD --candidate NAME|CMD
        (--scenario ID|FILE ... | --all) [--config F]... [--json OUT] [--keep] [-v]
      Runs each conformance scenario against both producers (same scripted consumer),
      then diffs the two trees and the host effects. NAME is a conformance producer
      (replay | cc-adapter | legacy-bridge); CMD is any adapter command per conformance/README.md.
"""
from __future__ import annotations

import argparse
import json
import shlex
import shutil
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import REPO
from .config import load_config
from .diff import Differ
from .model import Side, keep_events
from .report import build_result, summary_text


def compare(ref_dir: Path, cand_dir: Path, cfg: dict, effects: Optional[tuple] = None,
            extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    ref, cand = Side("ref", ref_dir, cfg), Side("cand", cand_dir, cfg)
    keep = keep_events(ref, cand, cfg)
    ref.build(keep)
    cand.build(keep)
    d = Differ(ref, cand, cfg)
    d.run()
    if cfg.get("conformance"):
        d.conformance()
    if effects is not None:
        d.host_effects(*effects)
    return build_result(d, extra)


def _emit(results: List[Dict[str, Any]], a: argparse.Namespace) -> int:
    if not a.quiet:
        for i, res in enumerate(results):
            if i:
                print()
            if res["result"] == "skip":
                print(f"scenario: {res['scenario']}\nresult: SKIP  {res['skip_reason']}")
            else:
                print(summary_text(res, verbose=a.v))
        if len(results) > 1:
            tally = {k: sum(r["result"] == k for r in results) for k in ("pass", "fail", "skip")}
            print(f"\n{len(results)} scenarios: {tally['pass']} pass, {tally['fail']} fail, {tally['skip']} skip")
    if a.json:
        doc = results[0] if len(results) == 1 and a.cmd == "diff" else {
            "tool": results[0]["tool"] if results else "ahp-shadow-diff",
            "result": "fail" if any(r["result"] == "fail" for r in results) else "pass",
            "runs": results}
        Path(a.json).write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 1 if any(r["result"] == "fail" for r in results) else 0


def _producer(spec: str) -> List[str]:
    from conformance.harness import PRODUCERS
    return PRODUCERS.get(spec) or shlex.split(spec)


def cmd_run(a: argparse.Namespace, cfg: dict) -> int:
    from conformance.common import Validators, read_jsonl
    from conformance.harness import run_scenario
    scen_dir = REPO / "conformance" / "scenarios"
    paths = sorted(scen_dir.glob("*.json")) if a.all else [
        Path(s) if s.endswith(".json") else scen_dir / f"{s}.json" for s in a.scenario or []]
    if not paths:
        print("run: give --scenario ID (repeatable) or --all", file=sys.stderr)
        return 2
    val = Validators()
    ref_cmd, cand_cmd = _producer(a.reference), _producer(a.candidate)
    results = []
    for path in paths:
        if not path.is_file():
            print(f"run: scenario not found: {path}", file=sys.stderr)
            return 2
        sc = json.loads(path.read_text(encoding="utf-8"))
        runs = [run_scenario(cmd, path, val, keep=True) for cmd in (ref_cmd, cand_cmd)]
        roots = [Path(r["root"]) for r in runs]
        skipped = [f"{side}: {r['detail']}" for side, run in zip(("ref", "cand"), runs)
                   for r in run["results"] if r["id"] == "E00" and r["status"] == "skip"]
        if skipped:  # scenario needs a gate one producer does not offer: nothing comparable
            if not a.keep:
                for root in roots:
                    shutil.rmtree(root, ignore_errors=True)
            results.append({"tool": "ahp-shadow-diff", "scenario": sc["id"], "result": "skip",
                            "skip_reason": "; ".join(sorted(set(skipped)))})
            continue
        fx = tuple({e["step"]: e for e in read_jsonl(root / "harness" / "effects.jsonl")
                    if isinstance(e, dict) and "step" in e} for root in roots)
        try:
            res = compare(roots[0] / "tasks" / sc["id"], roots[1] / "tasks" / sc["id"], cfg, effects=fx,
                          extra={"scenario": sc["id"],
                                 "producer_exit": {"ref": _exit_status(runs[0]), "cand": _exit_status(runs[1])}})
        finally:
            if not a.keep:
                for root in roots:
                    shutil.rmtree(root, ignore_errors=True)
        if a.keep:
            res["kept_roots"] = [str(r) for r in roots]
        results.append(res)
    return _emit(results, a)


def _exit_status(run: dict) -> str:
    bad = [r for r in run["results"] if r["id"] == "P00" and r["status"] == "fail"]
    return bad[0]["detail"] if bad else "0"


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="python3 -m tools.shadow_diff", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--config", action="append", default=[], help="config/allowlist JSON (repeatable)")
    common.add_argument("--json", help="write the structured result here")
    common.add_argument("--conformance", action="store_true", help="also run conformance generic checks (D00)")
    common.add_argument("--fail-on-warn", action="store_true")
    common.add_argument("-v", action="store_true", help="show allowed divergences and the alignment")
    common.add_argument("--quiet", action="store_true", help="no summary on stdout")
    d = sub.add_parser("diff", parents=[common], help="diff two existing task trees")
    d.add_argument("ref")
    d.add_argument("cand")
    d.add_argument("--task", help="treat REF and CAND as AHP roots and compare <root>/tasks/<task>")
    r = sub.add_parser("run", parents=[common], help="run conformance scenarios on two producers and diff")
    r.add_argument("--reference", default="cc-adapter")
    r.add_argument("--candidate", required=True)
    r.add_argument("--scenario", action="append")
    r.add_argument("--all", action="store_true")
    r.add_argument("--keep", action="store_true")
    a = ap.parse_args(argv)
    try:
        cfg = load_config([Path(p) for p in a.config])
    except (OSError, ValueError) as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2
    if a.conformance:
        cfg["conformance"] = True
    if a.fail_on_warn:
        cfg["fail_on_warn"] = True
    if a.cmd == "run":
        return cmd_run(a, cfg)
    ref, cand = Path(a.ref), Path(a.cand)
    if a.task:
        ref, cand = ref / "tasks" / a.task, cand / "tasks" / a.task
    try:
        res = compare(ref, cand, cfg)
    except FileNotFoundError as exc:
        print(f"input error: {exc}", file=sys.stderr)
        return 2
    return _emit([res], a)


if __name__ == "__main__":
    sys.exit(main())
