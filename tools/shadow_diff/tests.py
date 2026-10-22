"""Tests on synthetic AHP trees (+ one run-mode pass over real producers).

    python3 -m tools.shadow_diff.tests          # all
    python3 -m tools.shadow_diff.tests -k Plant  # unittest filters work
"""
from __future__ import annotations

import copy
import io
import json
import shlex
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import REPO
from .__main__ import compare, main
from .config import load_config

from conformance.common import CC_TO_AHP, DECISIONS, DEFAULT_DECISION, EXTRA_EVENTS, allowed_classes, default_class

ALLOW = Path(__file__).resolve().parent / "allow"


# ---------------------------------------------------------------- tree builder
def capabilities(producer: dict, task: str, **over) -> dict:
    events = {}
    for name in [a for (a, _, _) in CC_TO_AHP.values()] + list(EXTRA_EVENTS):
        e = {"class": default_class(name), "allowed_classes": list(allowed_classes(name))}
        if "gate" in e["allowed_classes"]:
            e.update(decisions=DECISIONS.get(name, ["ack"]), default_decision=DEFAULT_DECISION.get(name, "ack"),
                     timeout_ms=120000)
        events[name] = e
    caps = {"protocol": "ahp/1", "spec_version": "1.0.0-draft.3", "task_id": task, "producer": producer,
            "created_at": "2026-10-23T01:00:00.000+08:00", "transport": "local-fs", "consumer_json": "absent",
            "events": events,
            "features": {"acks": True, "full_files": True, "updated_input": True, "additional_context": True,
                         "message_delta": True, "policy_deny_event": True, "consumer_rules": True},
            "limits": {"max_field_chars": 240, "content_chars": 2000, "command_chars": 4000, "max_blocks": 3,
                       "max_gate_timeout_ms": 170000, "max_additional_context_chars": 10000},
            "rules": {"ids": ["bash-rm-rf"], "status": {"bash-rm-rf": "applied"}, "required": [], "sha256": "0" * 64},
            "redaction": {"methods": ["env-values"], "token": "[redacted]"}}
    for path, value in over.items():  # "events.tool.post.class" style keys; first segment is a top-level key
        head, _, rest = path.partition(".")
        if head == "events" and rest:
            ev, _, field = rest.rpartition(".")
            if value is None:
                caps["events"][ev].pop(field, None)
            else:
                caps["events"].setdefault(ev, {})[field] = value
        elif rest:
            caps[head][rest] = value
        else:
            caps[head] = value
    return caps


def write_tree(task_dir: Path, steps: list, *, producer=None, task="t1", seq_start=1, seq_gap=1,
               ts_base=None, cwd="/work/ref", caps_over=None, notes=None) -> Path:
    """steps: dicts with event, class?, payload?, decision?, via?, reply?, ack?(dict|False), rule_id?,
    timeout_ms?, default_decision?, accepted?, pending?(bool), target?(index of an earlier step)."""
    producer = producer or {"name": "cc-cursor-bridge", "version": "0.2.0", "host": {"name": "claude-code"}}
    ts_base = ts_base or datetime(2026, 10, 23, 1, 0, 0, tzinfo=timezone(timedelta(hours=8)))
    task_dir.mkdir(parents=True, exist_ok=True)
    (task_dir / "capabilities.json").write_text(json.dumps(capabilities(producer, task, **(caps_over or {}))))
    seqs, lines = [], []
    seq = seq_start
    for i, st in enumerate(steps):
        seqs.append(seq)
        ev = st["event"]
        cls = st.get("class", default_class(ev))
        ts = (ts_base + timedelta(milliseconds=137 * i)).isoformat(timespec="milliseconds")
        payload = copy.deepcopy(st.get("payload") or {})
        if "target" in st:
            payload["target_seq"] = seqs[st["target"]]
        env = {"protocol": "ahp/1", "task_id": task, "seq": seq, "event": ev, "class": cls, "producer": producer,
               "ts": ts, "payload": payload, "context": {"session_id": f"s-{task}", "cwd": cwd}}
        rec = dict(env)
        if cls == "gate":
            decision = st.get("decision", "allow" if ev == "tool.pre" else DECISIONS.get(ev, ["ack"])[0])
            via = st.get("via", "reply")
            rec["outcome"] = {"decision": decision, "via": via}
            if st.get("rule_id"):
                rec["outcome"]["rule_id"] = st["rule_id"]
            if st.get("pending", via != "rule"):
                tmo = st.get("timeout_ms", 120000)
                pend = dict(env, reply_file=f"replies/{seq}.json",
                            deadline=(ts_base + timedelta(milliseconds=137 * i + tmo)).isoformat(timespec="milliseconds"),
                            timeout_ms=tmo, decisions=DECISIONS.get(ev, ["ack"]),
                            default_decision=st.get("default_decision", DEFAULT_DECISION.get(ev, "ack")))
                (task_dir / "pending").mkdir(exist_ok=True)
                (task_dir / "pending" / f"{seq}-{ev}.json").write_text(json.dumps(pend, indent=2))
                if st.get("reply", decision if via == "reply" else None):
                    (task_dir / "replies").mkdir(exist_ok=True)
                    (task_dir / "replies" / f"{seq}.json").write_text(
                        json.dumps({"decision": st.get("reply", decision)}))
                if st.get("ack", True) is not False:
                    status = {"reply": "consumed", "timeout": "expired", "error": "expired",
                              "invalid_reply": "invalid"}.get(via, "consumed")
                    ack = {"protocol": "ahp/1", "task_id": task, "seq": seq, "ts": ts, "status": status, "via": via,
                           "applied_decision": decision}
                    if ev == "turn.stop":
                        ack["accepted"] = st.get("accepted", via == "reply" and decision == "ack")
                    ack.update(st.get("ack") or {})
                    (task_dir / "acks").mkdir(exist_ok=True)
                    (task_dir / "acks" / f"{seq}.json").write_text(json.dumps(ack))
        else:
            rec["outcome"] = {"via": "none"}
            if cls == "notify":
                (task_dir / "push").mkdir(exist_ok=True)
                (task_dir / "push" / f"{seq}-{ev}.json").write_text(json.dumps(env))
        lines.append(json.dumps(st.get("record_override") and {**rec, **st["record_override"]} or rec))
        seq += seq_gap
    (task_dir / "events.jsonl").write_text("\n".join(lines) + "\n")
    (task_dir / "seq").write_text(str(seq - seq_gap))
    return task_dir


def bash(cmd: str) -> dict:
    return {"tool_name": "Bash", "tool_use_id": "toolu_1", "tool_input": {"command": cmd},
            "tool_kind": "shell", "command": cmd}


def run_cmd(argv: list) -> dict:
    return {"tool_name": "run_command", "tool_use_id": "call_9", "tool_input": {"argv": argv},
            "tool_kind": "shell", "command": shlex.join(argv)}


BASE = [
    {"event": "session.start", "payload": {"source": "startup"}},
    {"event": "prompt.submit", "payload": {"prompt": "fix the build"}},
    {"event": "tool.pre", "payload": bash("ls -la /work/ref/src"), "decision": "allow"},
    {"event": "tool.post", "payload": {"tool_name": "Bash", "tool_response": "a\nb"}},
    {"event": "tool.pre", "payload": bash("rm -rf build"), "decision": "deny", "via": "rule", "rule_id": "bash-rm-rf"},
    {"event": "policy.deny", "payload": {"rule_id": "bash-rm-rf", "target_event": "tool.pre"}, "target": 4},
    {"event": "permission.request", "payload": {"tool_name": "Write", "tool_input": {"file_path": "/work/ref/a.py"}},
     "decision": "allow"},
    {"event": "message.delta", "payload": {"message_id": "m1", "index": 0, "text": "done", "final": True}},
    {"event": "turn.stop", "payload": {"last_assistant_message": "done"}, "decision": "ack"},
    {"event": "session.end", "payload": {"reason": "completed"}},
]


def candidate_steps() -> list:
    """BASE as a different producer would write it: same behaviour, different noise."""
    steps = copy.deepcopy(BASE)
    steps[2]["payload"] = run_cmd(["ls", "-la", "/work/cand/src"])
    steps[4]["payload"] = run_cmd(["bash", "-c", "rm -rf build"])
    steps[6]["payload"]["tool_input"]["file_path"] = "/work/cand/a.py"
    return steps


RINO = {"name": "rino", "version": "0.4.0"}


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="ahp-sd-test-"))
        self.cfg = load_config([])

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def ref(self, steps=None, **kw) -> Path:
        kw.setdefault("cwd", "/work/ref")
        return write_tree(self.tmp / "ref" / "tasks" / kw.pop("task", "t1"), steps or BASE, **kw)

    def cand(self, steps=None, **kw) -> Path:
        kw.setdefault("producer", RINO)
        kw.setdefault("cwd", "/work/cand")
        kw.setdefault("task", "rino-abcd1234-session-0")
        kw.setdefault("seq_start", 100)
        kw.setdefault("seq_gap", 3)
        kw.setdefault("ts_base", datetime(2026, 10, 24, 9, 30, 0, tzinfo=timezone.utc))
        return write_tree(self.tmp / "cand" / "tasks" / kw["task"], steps or candidate_steps(), **kw)

    def diff(self, ref: Path, cand: Path, cfg=None) -> dict:
        return compare(ref, cand, cfg or self.cfg)

    def codes(self, res: dict, unexpected_only=True) -> list:
        return sorted({d["code"] for d in res["divergences"] if not (unexpected_only and d["allowed_by"])})

    def assertPass(self, res: dict) -> None:
        bad = [d["message"] for d in res["divergences"] if not d["allowed_by"]]
        self.assertEqual(res["result"], "pass", bad)


class Identical(Base):
    def test_same_tree_twice(self):
        r = self.ref()
        res = self.diff(r, r)
        self.assertPass(res)
        self.assertEqual(res["divergences"], [])
        self.assertEqual(res["counts"]["aligned"], 8)  # 10 records minus log-class message.delta, session.end

    def test_noise_is_normalised(self):
        # different producer, task_id, seq base and gaps, timestamps/zone, cwd paths, tool names,
        # argv-built commands, bash -c wrapper
        res = self.diff(self.ref(), self.cand())
        self.assertPass(res)
        self.assertEqual(res["counts"]["aligned"], 8)

    def test_truncated_audit_record_uses_pending_payload(self):
        long_cmd = "echo " + "x" * 400
        steps = copy.deepcopy(BASE)
        steps[2]["payload"] = bash(long_cmd)
        r = self.ref(steps)
        c_steps = copy.deepcopy(steps)
        trunc = {"text": long_cmd[:240], "truncated": True, "original_length": len(long_cmd), "sha256": "f" * 64}
        c_steps[2]["record_override"] = {"payload": dict(bash(long_cmd), command=trunc)}
        c = self.cand(c_steps)
        self.assertPass(self.diff(r, c))

    def test_quoting_is_canonical(self):
        steps = copy.deepcopy(BASE)
        steps[2]["payload"] = bash("sudo ls '/var/log'")
        c_steps = candidate_steps()
        c_steps[2]["payload"] = run_cmd(["sudo", "ls", "/var/log"])
        self.assertPass(self.diff(self.ref(steps), self.cand(c_steps)))


class Planted(Base):
    """Each planted divergence must be caught with the right code."""

    def test_missing_gate(self):
        steps = candidate_steps()
        del steps[6]  # no permission.request
        res = self.diff(self.ref(), self.cand(steps))
        self.assertEqual(res["result"], "fail")
        self.assertEqual(self.codes(res), ["D01"])
        d = res["divergences"][0]
        self.assertEqual((d["event"], d["path"]), ("permission.request", "sequence.missing.permission.request"))

    def test_different_decision(self):
        steps = candidate_steps()
        steps[6]["decision"] = "deny"
        res = self.diff(self.ref(), self.cand(steps))
        self.assertIn("D04", self.codes(res))
        self.assertIn("D16", self.codes(res))  # the consumer replied differently: flagged as a warning
        self.assertEqual(res["result"], "fail")

    def test_fail_open_timeout(self):
        steps = candidate_steps()
        steps[2].update(via="timeout", decision="allow")  # expired, yet allowed
        res = self.diff(self.ref(), self.cand(steps))
        crit = [d for d in res["divergences"] if d["severity"] == "critical"]
        self.assertTrue(crit and crit[0]["code"] == "D11" and crit[0]["side"] == "cand", res["divergences"])
        self.assertEqual(res["counts"]["critical"], len(crit))

    def test_fail_open_cannot_be_allowlisted(self):
        steps = candidate_steps()
        steps[2].update(via="timeout", decision="allow")
        cfg = load_config([])
        cfg["allow"] = [{"id": "everything", "codes": [f"D{n:02d}" for n in range(20)], "path": "*", "reason": "test"}]
        res = self.diff(self.ref(), self.cand(steps), cfg)
        self.assertEqual(res["result"], "fail")
        self.assertEqual(self.codes(res), ["D11"])

    def test_extra_tool_call(self):
        steps = candidate_steps()
        steps[4:4] = [{"event": "tool.pre", "payload": run_cmd(["cat", "secrets.txt"]), "decision": "allow"},
                      {"event": "tool.post", "payload": {"tool_name": "run_command", "tool_response": "..."}}]
        steps[7]["target"] = 6  # policy.deny still targets the rm -rf call
        res = self.diff(self.ref(), self.cand(steps))
        self.assertEqual(self.codes(res), ["D02"])
        extra = [d for d in res["divergences"] if d["code"] == "D02"]
        self.assertEqual(sorted(d["event"] for d in extra), ["tool.post", "tool.pre"])
        self.assertIn("cat secrets.txt", next(d["cand"] for d in extra if d["event"] == "tool.pre"))

    def test_different_command(self):
        steps = candidate_steps()
        steps[2]["payload"] = run_cmd(["ls", "-la", "/etc"])
        res = self.diff(self.ref(), self.cand(steps))
        self.assertEqual(self.codes(res), ["D08"])

    def test_missing_tool_kind(self):
        steps = candidate_steps()
        del steps[2]["payload"]["tool_kind"]
        res = self.diff(self.ref(), self.cand(steps))
        self.assertEqual([d["path"] for d in res["divergences"]], ["tool.pre.payload.tool_kind"])

    def test_policy_deny_missing_and_rule_not_applied(self):
        steps = candidate_steps()
        steps[4].update(via="reply", decision="deny")  # asked the consumer instead of the static rule
        del steps[4]["rule_id"]
        del steps[5]
        res = self.diff(self.ref(), self.cand(steps))
        codes = self.codes(res)
        for c in ("D01", "D05", "D06", "D09"):
            self.assertIn(c, codes)

    def test_policy_deny_other_rule(self):
        steps = candidate_steps()
        steps[4]["rule_id"] = "no-sudo"
        steps[5]["payload"]["rule_id"] = "no-sudo"
        res = self.diff(self.ref(), self.cand(steps))
        self.assertEqual(self.codes(res), ["D09"])

    def test_policy_deny_points_elsewhere(self):
        steps = candidate_steps()
        steps[5]["target"] = 2
        res = self.diff(self.ref(), self.cand(steps))
        self.assertEqual([d["path"] for d in res["divergences"]], ["policy.deny.target"])

    def test_turn_stop_outcome(self):
        steps = candidate_steps()
        steps[8].update(via="timeout", decision="ack", accepted=False)
        res = self.diff(self.ref(), self.cand(steps))
        self.assertEqual(res["counts"]["critical"], 0)  # timeout -> ack, accepted:false is fail-closed
        self.assertIn("D05", self.codes(res))
        self.assertIn("D07", self.codes(res))  # accepted true vs false

    def test_turn_stop_timeout_accepted_is_fail_open(self):
        steps = candidate_steps()
        steps[8].update(via="timeout", decision="ack", accepted=True)
        res = self.diff(self.ref(), self.cand(steps))
        self.assertIn("fail_open.turn.stop.accepted", [d["path"] for d in res["divergences"]])

    def test_missing_ack(self):
        steps = candidate_steps()
        steps[2]["ack"] = False
        res = self.diff(self.ref(), self.cand(steps))
        self.assertEqual([d["path"] for d in res["divergences"]], ["tool.pre.ack.present"])

    def test_timeout_differs(self):
        steps = candidate_steps()
        steps[8]["timeout_ms"] = 300000
        res = self.diff(self.ref(), self.cand(steps))
        self.assertEqual(self.codes(res), ["D10"])

    def test_class_change(self):
        steps = candidate_steps()
        steps[3]["class"] = "log"  # tool.post demoted: still compared because the ref has it as notify
        res = self.diff(self.ref(), self.cand(steps))
        self.assertEqual(self.codes(res), ["D03"])

    def test_weakened_default_decision(self):
        steps = candidate_steps()
        steps[2]["default_decision"] = "allow"
        res = self.diff(self.ref(), self.cand(steps))
        self.assertIn("fail_open.tool.pre.default_decision", [d["path"] for d in res["divergences"]])

    def test_reorder(self):
        steps = candidate_steps()
        steps[6], steps[8] = steps[8], steps[6]  # turn.stop before permission.request
        res = self.diff(self.ref(), self.cand(steps))
        self.assertEqual(self.codes(res), ["D01", "D02"])


class Capabilities(Base):
    def test_capability_difference_and_allowlist(self):
        over = {"events.prompt.submit.allowed_classes": ["notify", "log"], "events.prompt.submit.decisions": None,
                "events.prompt.submit.default_decision": None, "events.prompt.submit.timeout_ms": None,
                "limits.host_hook_timeout_ms": 180000}
        r = self.ref(caps_over=over)
        c = self.cand()
        res = self.diff(r, c)
        self.assertEqual(sorted(d["path"] for d in res["divergences"]),
                         ["capabilities.events.prompt.submit.allowed_classes",
                          "capabilities.limits.host_hook_timeout_ms"])
        cfg = load_config([ALLOW / "cc-known.json"])
        res = self.diff(r, c, cfg)
        self.assertPass(res)
        self.assertEqual(sorted({d["allowed_by"] for d in res["divergences"]}),
                         ["cc-async-not-gate", "cc-host-hook-timeout"])
        self.assertIn("cc-no-reasoning-delta", res["unused_allow"])

    def test_allowlist_is_value_specific(self):
        # cc-async-not-gate only covers "CC cannot gate it"; a candidate that cannot gate tool.pre is not covered
        r = self.ref()
        c = self.cand(caps_over={"events.tool.pre.allowed_classes": ["notify", "log"]})
        res = self.diff(r, c, load_config([ALLOW / "cc-known.json"]))
        self.assertEqual(res["result"], "fail")

    def test_undeclared_event_reported_once(self):
        r = self.ref()
        c = self.cand(caps_over={"events": {k: v for k, v in capabilities(RINO, "x")["events"].items()
                                            if k != "worktree.create"}})
        res = self.diff(r, c)
        self.assertEqual([d["path"] for d in res["divergences"]], ["capabilities.events.worktree.create.present"])

    def test_spec_version_draft_suffix_ignored(self):
        res = self.diff(self.ref(), self.cand(caps_over={"spec_version": "1.0.0-draft.4"}))
        self.assertPass(res)

    def test_rino_allowlist_loads_and_covers_planned_gaps(self):
        cfg = load_config([ALLOW / "cc-vs-rino.json"])
        events = capabilities(RINO, "x")["events"]
        for ev in ("permission.request", "input.request", "subagent.stop", "worktree.create"):
            events.pop(ev)
        steps = candidate_steps()
        del steps[6]
        c = self.cand(steps, caps_over={"events": events, "features.updated_input": False})
        res = self.diff(self.ref(), c, cfg)
        self.assertPass(res)


class Cli(Base):
    def test_exit_codes_and_json(self):
        r, c = self.ref(), self.cand()
        out = self.tmp / "res.json"
        with redirect_stdout(io.StringIO()) as buf:
            self.assertEqual(main(["diff", str(r), str(c), "--json", str(out)]), 0)
        self.assertIn("result: PASS", buf.getvalue())
        self.assertEqual(json.loads(out.read_text())["result"], "pass")
        steps = candidate_steps()
        del steps[6]
        c2 = self.cand(steps, task="other")
        with redirect_stdout(io.StringIO()) as buf:
            self.assertEqual(main(["diff", str(r), str(c2), "-v"]), 1)
        self.assertIn("D01", buf.getvalue())
        with redirect_stdout(io.StringIO()):
            self.assertEqual(main(["diff", str(self.tmp / "nope"), str(c)]), 2)

    def test_roots_with_task(self):
        self.ref(task="same")
        self.cand(task="same")
        with redirect_stdout(io.StringIO()):
            self.assertEqual(main(["diff", str(self.tmp / "ref"), str(self.tmp / "cand"), "--task", "same"]), 0)

    def test_bad_config(self):
        bad = self.tmp / "bad.json"
        bad.write_text(json.dumps({"allow": [{"id": "x", "code": "D12"}]}))  # no reason
        r = self.ref()
        with redirect_stdout(io.StringIO()):
            self.assertEqual(main(["diff", str(r), str(r), "--config", str(bad)]), 2)

    def test_fail_on_warn(self):
        r = self.ref()
        steps = copy.deepcopy(BASE)
        steps[6]["reply"] = "deny"  # consumer said deny, producer (wrongly or by rule) recorded allow
        c = self.ref(steps, task="t2")
        res = self.diff(r, c)
        self.assertEqual(self.codes(res), ["D16"])
        self.assertEqual(res["result"], "pass")
        with redirect_stdout(io.StringIO()):
            self.assertEqual(main(["diff", str(r), str(c), "--fail-on-warn"]), 1)


class ConsumerJson(Base):
    def test_consumer_json_differs_is_warning(self):
        r, c = self.ref(), self.cand()
        (r / "consumer.json").write_text(json.dumps({"protocol": "ahp/1", "timeouts_ms": {"turn.stop": 300000}}))
        res = self.diff(r, c)
        self.assertEqual(self.codes(res), ["D16"])
        (c / "consumer.json").write_text(json.dumps({"protocol": "ahp/1", "timeouts_ms": {"turn.stop": 300000},
                                                     "created_at": "2026-10-24T00:00:00Z"}))
        self.assertEqual(self.diff(r, c)["divergences"], [])


class RunMode(unittest.TestCase):
    """Real producers through the conformance harness: cc-adapter vs the replay reference producer."""

    def test_gate_replies_and_rules(self):
        if not (REPO / "scripts" / "ahp_cc.py").is_file():
            self.skipTest("CC adapter not in this tree")
        out = Path(tempfile.mkdtemp(prefix="ahp-sd-run-")) / "run.json"
        try:
            with redirect_stdout(io.StringIO()) as buf:
                code = main(["run", "--reference", "cc-adapter", "--candidate", "replay",
                             "--scenario", "gate-replies", "--scenario", "rules-rm-rf",
                             "--config", str(ALLOW / "cc-vs-replay.json"), "--json", str(out)])
            self.assertEqual(code, 0, buf.getvalue())
            doc = json.loads(out.read_text())
            self.assertEqual([r["result"] for r in doc["runs"]], ["pass", "pass"])
            # without the replay allowlist, replay's missing tool_kind is reported
            with redirect_stdout(io.StringIO()):
                code = main(["run", "--reference", "cc-adapter", "--candidate", "replay", "--scenario", "gate-replies",
                             "--config", str(ALLOW / "cc-known.json"), "--quiet"])
            self.assertEqual(code, 1)
        finally:
            shutil.rmtree(out.parent, ignore_errors=True)


if __name__ == "__main__":
    unittest.main(module=__name__, argv=[sys.argv[0]] + sys.argv[1:], verbosity=1)
