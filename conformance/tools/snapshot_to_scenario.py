#!/usr/bin/env python3
"""Build conformance fixtures from a 9f478f8 bridge task dir (read-only source).

  python3 -m conformance.tools.snapshot_to_scenario [SNAPSHOT_DIR]

Writes:
  scenarios/demo-snapshot.json            replayable scenario (host events + the consumer's real replies)
  fixtures/legacy-9f478f8-demo/           the snapshot converted by legacy_to_ahp.convert_dir (static fixture)
Default SNAPSHOT_DIR: ~/Sync/Test/ccb-capture-run/demo-bridge-snapshot
"""
from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

from conformance.common import CC_TO_AHP, REPO

sys.path.insert(0, str(REPO / "spec" / "tools"))
import legacy_to_ahp  # noqa: E402

PKG = Path(__file__).resolve().parents[1]
DEFAULT = Path.home() / "Sync" / "Test" / "ccb-capture-run" / "demo-bridge-snapshot"


def main(argv) -> int:
    src = Path(argv[0]) if argv else DEFAULT
    if not src.is_dir():
        print(f"snapshot not found: {src}", file=sys.stderr)
        return 2
    notices = {}
    for kind in ("pending", "push"):
        for path in (src / kind).glob("*.json"):
            if path.name.endswith(".full.json"):
                continue
            body = json.loads(path.read_text(encoding="utf-8"))
            full = src / body["full_file"] if body.get("full_file") else None
            if full and full.is_file():
                body = json.loads(full.read_text(encoding="utf-8"))
            notices[body["seq"]] = (kind, legacy_to_ahp.convert_notice(dict(body, full_file=None, truncated=False),
                                                                        kind, "demo", "9f478f8"))
    steps, expect, consumer, counts = [], [], [], {}
    for line in (src / "events.jsonl").read_text(encoding="utf-8").splitlines():
        rec = json.loads(line)
        seq = rec["seq"]
        if seq in notices:
            env = notices[seq][1]
            payload = env["payload"]
        else:
            env = legacy_to_ahp.convert_record(rec, "demo", "9f478f8")
            payload = env["payload"]
        event = env["event"]
        if event == "prompt.submit" and "prompt" not in payload:
            payload["prompt"] = "[prompt not captured by 9f478f8 (spec A.6 #14)]"
        steps.append({"event": event, "payload": payload, "context": {"session_id": rec.get("session_id"),
                                                                      "cwd": rec.get("cwd")},
                      "x-source-seq": seq})
        reply = src / "replies" / f"{seq}.json"
        if reply.is_file():
            counts[event] = counts.get(event, 0) + 1
            r = legacy_to_ahp.convert_reply(json.loads(reply.read_text(encoding="utf-8")), seq)
            item = {"match": {"event": event, "nth": counts[event]}, "action": r["decision"]}
            if r.get("reason"):
                item["reason"] = r["reason"]
            consumer.append(item)
            exp = {"step": len(steps) - 1, "effect": r["decision"], "ack": "consumed"}
            if event == "turn.stop":
                exp["accepted"] = r["decision"] == "ack"
            expect.append(exp)
    scenario = {"id": "demo-snapshot", "title": "replay of demo-bridge-snapshot (calc add, haiku, 19 events)",
                "source": [f"{src.name} (9f478f8, read-only)"], "producer": {"timeout_ms": 3000},
                "consumer_json": {"protocol": "ahp/1", "consumer": {"name": "demo"},
                                  "event_classes": {"message.delta": "notify"}},
                "steps": steps, "consumer": consumer, "expect": expect}
    (PKG / "scenarios" / "demo-snapshot.json").write_text(json.dumps(scenario, ensure_ascii=False, indent=2) + "\n",
                                                         encoding="utf-8")
    dst = PKG / "fixtures" / "legacy-9f478f8-demo"
    shutil.rmtree(dst, ignore_errors=True)
    legacy_to_ahp.convert_dir(src, dst, "demo-bridge-snapshot", "9f478f8")
    print(f"scenario: {len(steps)} steps, {len(consumer)} replies; fixture: {dst}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
