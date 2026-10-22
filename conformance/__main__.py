"""python3 -m conformance [options]

Default: run every scenario against the reference replay producer, the legacy
(9f478f8) bridge and the AHP v1 cc-adapter, check the static legacy fixture, and
print a report. Exit 0 when every failure is listed in that producer's expected
file: expected/legacy-9f478f8.json for legacy-bridge and fixture,
expected/<producer>.json for the others (replay has none; cc-adapter's is empty).

Options:
  --producer NAME|CMD   replay | legacy-bridge | cc-adapter | any adapter command (quoted string). Repeatable.
  --scenario ID         run only these scenarios (repeatable)
  --no-fixtures         skip static fixture checks
  --json FILE           write the full report as JSON
  --keep                keep temp task dirs (paths are printed)
  --update-expected     rewrite expected/legacy-9f478f8.json from this run
  --list                list scenarios and checks
  -v                    print passing checks too
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import json
import shlex
import sys
from pathlib import Path

from .checks import GENERIC
from .common import Validators
from .harness import PRODUCERS, run_scenario, run_static

PKG = Path(__file__).resolve().parent
EXPECTED = PKG / "expected" / "legacy-9f478f8.json"
LEGACY_NAMES = ("legacy-bridge", "fixture")


def expected_file(producer_key: str) -> Path:
    if producer_key in LEGACY_NAMES:
        return EXPECTED
    safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in producer_key)
    return PKG / "expected" / f"{safe}.json"


def load_expected(producer_key: str) -> set:
    path = expected_file(producer_key)
    if not path.is_file():
        return set()
    return set(json.loads(path.read_text(encoding="utf-8")).get("expected_failures") or [])


def key(item: dict, r: dict) -> str:
    return f"{item['scenario']}::{r['id']}::{r['title']}"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python3 -m conformance", add_help=True)
    ap.add_argument("--producer", action="append")
    ap.add_argument("--scenario", action="append")
    ap.add_argument("--no-fixtures", action="store_true")
    ap.add_argument("--json")
    ap.add_argument("--keep", action="store_true")
    ap.add_argument("--update-expected", action="store_true")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("-j", type=int, default=6)
    ap.add_argument("-v", action="store_true")
    a = ap.parse_args(argv)

    scenarios = sorted((PKG / "scenarios").glob("*.json"))
    if a.scenario:
        scenarios = [p for p in scenarios if p.stem in a.scenario]
    if a.list:
        for p in scenarios:
            d = json.loads(p.read_text(encoding="utf-8"))
            print(f"{p.stem:28} {d.get('title', '')}")
        print()
        for cid, title, _ in GENERIC:
            print(f"{cid}  {title}")
        return 0

    if a.update_expected and (a.scenario or a.no_fixtures or (a.producer and "legacy-bridge" not in a.producer)):
        print("--update-expected needs a full run (no --scenario/--no-fixtures, legacy-bridge included)")
        return 2
    val = Validators()
    if not val.available:
        print(f"note: {val.note}")
    producers = a.producer or ["replay", "legacy-bridge", "cc-adapter"]
    report = {"runs": []}
    for name in producers:
        cmd = PRODUCERS.get(name) or shlex.split(name)
        with cf.ThreadPoolExecutor(max_workers=max(1, a.j)) as pool:
            runs = list(pool.map(lambda p: run_scenario(cmd, p, val, keep=a.keep), scenarios))
        for r in runs:
            r["producer_key"] = name
        report["runs"].extend(runs)
    if not a.no_fixtures:
        for fx in sorted((PKG / "fixtures").iterdir()):
            if fx.is_dir():
                r = run_static(fx, val, f"fixture:{fx.name}")
                r["producer_key"] = "fixture"
                report["runs"].append(r)

    exit_code = 0
    summary = {}
    legacy_fail = []
    for item in report["runs"]:
        pk = item["producer_key"]
        s = summary.setdefault(pk, {"pass": 0, "fail": 0, "skip": 0, "xfail": 0, "xpass": 0})
        expected = load_expected(pk)
        for r in item["results"]:
            st = r["status"]
            k = key(item, r)
            if st == "fail":
                if pk in LEGACY_NAMES:
                    legacy_fail.append(k)
                st = "xfail" if k in expected else "fail"
            elif st == "pass" and k in expected:
                st = "xpass"
            r["reported"] = st
            s[st] += 1
            if st == "fail" or (a.v and st in ("pass", "xpass")) or st == "xpass":
                flag = {"fail": "FAIL ", "pass": "ok   ", "xpass": "XPASS"}[st]
                root = f"  [{item['root']}]" if item.get("root") else ""
                print(f"{flag} [{pk}] {item['scenario']} {r['id']} {r['title']}: {r['detail']}{root}")
        if any(r["reported"] == "fail" for r in item["results"]):
            exit_code = 1

    print()
    print(f"{'producer':16} {'pass':>5} {'fail':>5} {'xfail':>6} {'xpass':>6} {'skip':>5}  total")
    for pk, s in summary.items():
        total = sum(s.values())
        print(f"{pk:16} {s['pass']:5} {s['fail']:5} {s['xfail']:6} {s['xpass']:6} {s['skip']:5}  {total}")
    if any(pk in LEGACY_NAMES for pk in summary):
        by_check = {}
        for k in legacy_fail:
            cid = k.split("::")[1]
            by_check[cid] = by_check.get(cid, 0) + 1
        print("legacy failures by check: " + ", ".join(f"{c}×{n}" for c, n in sorted(by_check.items())))
    if a.update_expected:
        EXPECTED.parent.mkdir(exist_ok=True)
        EXPECTED.write_text(json.dumps({"producer": "cc-cursor-bridge 9f478f8 via legacy_to_ahp",
                                        "note": "Phase 2 should turn these into passes; see spec Appendix A.6.",
                                        "expected_failures": sorted(legacy_fail)},
                                       ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"wrote {EXPECTED} ({len(legacy_fail)} expected failures)")
    if a.json:
        Path(a.json).write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
