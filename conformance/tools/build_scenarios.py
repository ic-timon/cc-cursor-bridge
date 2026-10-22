#!/usr/bin/env python3
"""Regenerate conformance/scenarios/*.json (hand-written protocol scenarios).

  python3 -m conformance.tools.build_scenarios

The snapshot scenario comes from tools/snapshot_to_scenario.py instead.
"""
from __future__ import annotations

import json
from pathlib import Path

OUT = Path(__file__).resolve().parents[1] / "scenarios"
T = 1500  # gate timeout used by scenarios, ms
SECRET = "sk-ant-PLANTEDsecret0123456789abcdef"
GH = "ghp_PLANTEDpattern0123456789ABCDEFabcd"


def pre(command=None, tool="Bash", **tool_input):
    if command is not None:
        tool_input["command"] = command
    return {"event": "tool.pre", "payload": {"tool_name": tool, "tool_use_id": "toolu_conf", "tool_input": tool_input}}


def ev(event, **payload):
    return {"event": event, "payload": payload}


def stop(msg="done", active=False):
    return ev("turn.stop", last_assistant_message=msg, stop_active=active)


def sc(id, title, source, steps, expect, consumer=(), **extra):
    data = {"id": id, "title": title, "source": source, "producer": {"timeout_ms": T},
            "steps": steps, "consumer": list(consumer), "expect": expect}
    for k, v in extra.items():
        if k == "producer":
            data["producer"].update(v)
        else:
            data[k] = v
    return data


S = [
    sc("rules-rm-rf", "rm -rf is denied by a static rule without a pending; plain rm reaches the gate",
       ["test_bridge.py::test_rm_rf_denied_and_plain_rm_allowed", "test_bridge.py::test_wait_all_rule_deny_has_no_pending"],
       [pre("rm -rf build"), pre("rm build.log"), pre("cd /tmp && rm -fr x")],
       [{"step": 0, "effect": "deny", "via": "rule", "pending": False},
        {"step": 1, "effect": "allow", "via": "reply", "ack": "consumed", "pending": True},
        {"step": 2, "effect": "deny", "via": "rule", "pending": False}],
       consumer=[{"match": {"event": "tool.pre"}, "action": "allow", "reason": "ok", "repeat": True}],
       expect_task={"policy_deny_count": 2}),
    sc("rules-force-push", "force pushes are rule-denied; a normal push is asked",
       ["test_bridge.py::test_force_push"],
       [pre("git push --force origin main"), pre("git push -f"), pre("git push --force-with-lease"),
        pre("git -C repo push origin main")],
       [{"step": 0, "effect": "deny", "via": "rule"}, {"step": 1, "effect": "deny", "via": "rule"},
        {"step": 2, "effect": "deny", "via": "rule"},
        {"step": 3, "effect": "allow", "via": "reply", "ack": "consumed"}],
       consumer=[{"match": {"event": "tool.pre"}, "action": "allow", "reason": "ok", "repeat": True}]),
    sc("rules-env-ssh", ".env and ~/.ssh are rule-denied; .env.example is allowed by the consumer",
       ["test_bridge.py::test_env_and_ssh_paths"],
       [pre(tool="Read", file_path="/proj/.env"), pre(tool="Read", file_path="/proj/.env.example"),
        pre("cat ~/.ssh/id_rsa"), pre(tool="Edit", file_path="/proj/.env.local", old_string="a", new_string="b")],
       [{"step": 0, "effect": "deny", "via": "rule"}, {"step": 1, "effect": "allow", "via": "reply"},
        {"step": 2, "effect": "deny", "via": "rule"}, {"step": 3, "effect": "deny", "via": "rule"}],
       consumer=[{"match": {"event": "tool.pre"}, "action": "allow", "reason": "ok", "repeat": True}]),
    sc("rules-powershell", "PowerShell Remove-Item -Recurse -Force is rule-denied",
       ["test_bridge.py::test_powershell_remove"],
       [pre("Remove-Item -Recurse -Force C:\\tmp\\x", tool="PowerShell"), pre("Remove-Item C:\\tmp\\x.txt", tool="PowerShell")],
       [{"step": 0, "effect": "deny", "via": "rule"}, {"step": 1, "effect": "allow", "via": "reply"}],
       consumer=[{"match": {"event": "tool.pre"}, "action": "allow", "reason": "ok", "repeat": True}]),
    sc("rules-disabled", "a disabled rule does not deny; the gate decides",
       ["test_bridge.py::test_disabled_rule_allows"],
       [pre("rm -rf build")],
       [{"step": 0, "effect": "allow", "via": "reply", "pending": True}],
       consumer=[{"match": {"event": "tool.pre"}, "action": "allow", "reason": "ok"}],
       producer={"rules": [{"id": "bash-rm-rf", "enabled": False, "tools": ["Bash"], "reason": "x", "bash_rm_rf": True}]}),
    sc("rule-not-overridable", "a consumer that would allow everything cannot override a rule deny",
       ["test_bridge.py::test_probe_allow_overrides_only_when_set (protocol part)"],
       [pre("rm -rf /"), pre("echo ok")],
       [{"step": 0, "effect": "deny", "via": "rule", "pending": False}, {"step": 1, "effect": "allow"}],
       consumer=[{"match": {}, "action": "allow", "reason": "yes to all", "repeat": True}]),
    sc("redaction-env", "known secret env values never reach any producer file",
       ["test_bridge.py::test_log_shape_redaction_and_seq"],
       [pre(f"curl -H 'x-api-key: {SECRET}' https://example.invalid"),
        ev("tool.post", tool_name="Bash", tool_input={"command": "env"}, tool_response={"stdout": f"KEY={SECRET}\n"}),
        ev("prompt.submit", prompt=f"use {SECRET}"), stop(f"I used {SECRET}")],
       [{"step": 0, "effect": "allow"}, {"step": 3, "effect": "ack", "accepted": True}],
       consumer=[{"match": {"event": "tool.pre"}, "action": "allow", "reason": "ok"},
                 {"match": {"event": "turn.stop"}, "action": "ack", "reason": "ok"}],
       producer={"env": {"ANTHROPIC_API_KEY": SECRET}}, secrets=[SECRET]),
    sc("redaction-patterns", "SHOULD: token-shaped strings are redacted even when not in env",
       ["spec 12.1 (SHOULD)"],
       [ev("tool.post", tool_name="Bash", tool_input={"command": "cat cfg"}, tool_response=f"token={GH}")],
       [], secrets=[GH], level="SHOULD"),
    sc("gate-timeouts", "no reply: every gate times out to its fail-closed default",
       ["test_bridge.py::test_wait_reply_and_timeout", "test_bridge.py::test_wait_all_reply_and_timeout",
        "test_bridge.py::test_elicitation_timeout_declines"],
       [pre("ls"), ev("permission.request", tool_name="Write", tool_input={"file_path": "/p/a.py", "content": "x"}),
        ev("input.request", server="demo", message="pick one", mode="form", requested_schema={"type": "object"}),
        stop("finished")],
       [{"step": 0, "effect": "deny", "via": "timeout", "ack": "expired"},
        {"step": 1, "effect": "deny", "via": "timeout", "ack": "expired"},
        {"step": 2, "effect": "decline", "via": "timeout", "ack": "expired"},
        {"step": 3, "effect": "ack", "via": "timeout", "ack": "expired", "accepted": False}]),
    sc("gate-replies", "valid replies are applied and acked as consumed",
       ["test_bridge.py::test_wait_reply_and_timeout", "test_bridge.py::test_wait_all_reply_and_timeout"],
       [pre("ls"), pre("make"), pre("python3 x.py"),
        ev("permission.request", tool_name="Write", tool_input={"file_path": "/p/a.py", "content": "x"}),
        ev("input.request", server="demo", message="env?", mode="form", requested_schema={"type": "object"}),
        stop("all tests pass")],
       [{"step": 0, "effect": "allow", "ack": "consumed"}, {"step": 1, "effect": "deny", "ack": "consumed"},
        {"step": 2, "effect": "ask", "ack": "consumed"}, {"step": 3, "effect": "allow", "ack": "consumed"},
        {"step": 4, "effect": "accept", "ack": "consumed"},
        {"step": 5, "effect": "ack", "ack": "consumed", "accepted": True}],
       consumer=[{"match": {"event": "tool.pre", "nth": 1}, "action": "allow", "reason": "read only"},
                 {"match": {"event": "tool.pre", "nth": 2}, "action": "deny", "reason": "no builds"},
                 {"match": {"event": "tool.pre", "nth": 3}, "action": "ask", "reason": "human"},
                 {"match": {"event": "permission.request"}, "action": "allow", "reason": "repo file"},
                 {"match": {"event": "input.request"}, "action": "accept", "content": {"env": "staging"}},
                 {"match": {"event": "turn.stop"}, "action": "ack", "reason": "verified"}]),
    sc("invalid-replies", "invalid replies are rejected: invalid ack and the fail-closed default",
       ["spec 9.4", "spec A.6 #3/#4"],
       [pre("ls"), pre("ls -a"), pre("ls -l"), pre("ls -R"), stop("done")],
       [{"step": 0, "effect": "deny", "ack": "invalid", "via": "invalid_reply"},
        {"step": 1, "effect": "deny", "ack": "invalid"},
        {"step": 2, "effect": "deny", "ack": "invalid"},
        {"step": 3, "effect": "deny", "ack": "invalid"},
        {"step": 4, "effect": "ack", "ack": "invalid", "accepted": False}],
       consumer=[{"match": {"event": "tool.pre", "nth": 1}, "action": "invalid", "raw": {"decision": "maybe"}},
                 {"match": {"event": "tool.pre", "nth": 2}, "action": "raw", "raw": "{\"decision\": \"allow\""},
                 {"match": {"event": "tool.pre", "nth": 3}, "action": "raw",
                  "raw": {"decision": "allow", "updatedinput": {"command": "rm -rf /"}}},
                 {"match": {"event": "tool.pre", "nth": 4}, "action": "raw", "raw": {"decision": "accept"}},
                 {"match": {"event": "turn.stop"}, "action": "raw", "raw": {"decision": "block"}}]),
    sc("late-reply", "a reply after the deadline is not applied; ack says expired",
       ["spec 9.4"],
       [pre("ls"), stop("done")],
       [{"step": 0, "effect": "deny", "ack": "expired"},
        {"step": 1, "effect": "ack", "ack": "expired", "accepted": False}],
       consumer=[{"match": {"event": "tool.pre"}, "action": "late", "decision": "allow"},
                 {"match": {"event": "turn.stop"}, "action": "late", "decision": "ack"}],
       after_ms=500),
    sc("producer-error", "producer failures on a gate fail closed and are reported",
       ["test_bridge.py::test_dispatch_cli", "spec 9.1", "spec A.6 #1/#2"],
       [dict(pre("ls"), fault="crash"), dict(pre("ls"), fault="bad_input"),
        dict(ev("permission.request", tool_name="Write", tool_input={"file_path": "/p/a", "content": "x"}), fault="crash"),
        dict(stop("done"), fault="crash")],
       [{"step": 0, "effect": "deny"}, {"step": 1, "effect": "deny"}, {"step": 2, "effect": "deny"},
        {"step": 3, "effect": "ack"}],
       consumer=[{"match": {}, "action": "allow", "reason": "would allow", "repeat": True}],
       expect_task={"producer_error": True}),
    sc("truncation", "long fields become truncation objects backed by full files",
       ["test_bridge.py::test_truncation", "test_bridge.py::test_pending_truncation_writes_full_payload",
        "test_bridge.py::test_stop_notice_carries_last_message_and_full_file",
        "test_bridge.py::test_tool_response_and_error_write_full_files"],
       [pre("echo " + "x" * 120),
        ev("tool.post", tool_name="Bash", tool_input={"command": "seq 100"},
           tool_response={"stdout": "\n".join(str(i) for i in range(100)), "stderr": ""}),
        ev("tool.error", tool_name="Bash", tool_input={"command": "false"}, error="E" * 90, is_interrupt=False),
        stop("答" * 80)],
       [{"step": 0, "effect": "allow", "truncated": ["tool_input.command"]},
        {"step": 1, "truncated": ["tool_response"]}, {"step": 2, "truncated": ["error"]},
        {"step": 3, "effect": "ack", "truncated": ["last_assistant_message"], "payload_has": ["last_assistant_message"]}],
       consumer=[{"match": {"event": "tool.pre"}, "action": "allow", "reason": "ok"},
                 {"match": {"event": "turn.stop"}, "action": "ack", "reason": "ok"}],
       producer={"limits": {"command_chars": 40, "content_chars": 50, "max_field_chars": 30}}),
    sc("stop-loop-guard", "turn.stop blocks are capped by max_blocks; the cap ends the turn unaccepted",
       ["test_bridge.py::test_stop_loop_guard"],
       [stop("v1"), stop("v2", active=True), stop("v3", active=True)],
       [{"step": 0, "effect": "block", "ack": "consumed"}, {"step": 1, "effect": "block", "ack": "consumed"},
        {"step": 2, "effect": "ack", "accepted": False}],
       consumer=[{"match": {"event": "turn.stop"}, "action": "block", "reason": "add a test", "repeat": True}],
       producer={"max_blocks": 2}),
    sc("class-routing", "consumer.json can move events between classes within allowed_classes",
       ["test_bridge.py::test_mode_routing_push_log_off_and_env", "test_bridge.py::test_assistant_text_toggle_off_and_on",
        "test_bridge.py::test_assistant_text_config_and_env_override", "test_bridge.py::test_session_start_watch_does_not_wait"],
       [ev("session.start", source="startup"),
        ev("message.delta", message_id="m1", turn_id="t1", index=0, final=True, text="hello"),
        ev("tool.post", tool_name="Bash", tool_input={"command": "true"}, tool_response=""),
        ev("tool.batch", tool_calls=[])],
       [{"step": 0, "class": "notify", "pending": False}, {"step": 1, "class": "notify"},
        {"step": 2, "class": "log"}, {"step": 3, "class": "log"}],
       consumer_json={"protocol": "ahp/1", "consumer": {"name": "conformance"},
                      "event_classes": {"message.delta": "notify", "tool.post": "log"}},
       expect_task={"capabilities_class": {"message.delta": "notify", "tool.post": "log", "tool.pre": "gate"}}),
    sc("updated-input-rerule", "an allow with updated_input is re-checked against static rules",
       ["spec 7.1 / 13.5 (draft.2 #7)"],
       [pre("ls build")],
       [{"step": 0, "effect": "deny", "via": "rule"}],
       consumer=[{"match": {"event": "tool.pre"}, "action": "allow", "reason": "use rm instead",
                  "updated_input": {"command": "rm -rf build"}}]),
    sc("updated-input-tool-name", "a reply that tries to change the tool name is invalid",
       ["draft.2 #7"],
       [pre("ls")],
       [{"step": 0, "effect": "deny", "ack": "invalid"}],
       consumer=[{"match": {"event": "tool.pre"}, "action": "allow", "reason": "read instead",
                  "updated_input": {"tool_name": "Read", "file_path": "/etc/hosts"}}]),
    sc("subagent-stop-context", "subagent.stop carries the subagent's answer and context.agent_id",
       ["test_bridge.py::test_subagent_stop_notice_carries_agent_context"],
       [dict(ev("subagent.stop", agent_id="a-1", agent_type="Explore", last_assistant_message="found 3 files",
                agent_transcript_path="/tmp/a-1.jsonl"), context={"agent_id": "a-1"})],
       [{"step": 0, "class": "notify",
         "payload_has": ["agent_id", "agent_type", "last_assistant_message", "agent_transcript_path"]}]),
    sc("timeout-clamp", "consumer timeouts above the producer limit are clamped and reported",
       ["draft.2 #3"],
       [ev("session.start", source="startup")], [],
       consumer_json={"protocol": "ahp/1", "consumer": {"name": "RINO"},
                      "timeouts_ms": {"turn.stop": 300000, "tool.pre": 60000}}),
    sc("plan-update", "plan.update is a standard notify event",
       ["draft.2 #1"],
       [ev("plan.update", source="TodoWrite", items=[{"content": "write add", "status": "completed"},
                                                     {"content": "run tests", "status": "in_progress"}])],
       [{"step": 0, "class": "notify", "payload_has": ["items"]}]),
    sc("required-rule-tool-kind", "a required consumer rule matched by tool_kind + POSIX command denies a non-CC shell tool",
       ["draft.2 #2"],
       [{"event": "tool.pre", "payload": {"tool_name": "run_command", "tool_kind": "shell", "command": "sudo ls '/var/log'",
                                          "tool_input": {"argv": ["sudo", "ls", "/var/log"]}}}],
       [{"step": 0, "effect": "deny", "via": "rule", "pending": False}],
       consumer_json={"protocol": "ahp/1", "consumer": {"name": "RINO"},
                      "extra_rules": [{"id": "no-sudo", "required": True, "tool_kinds": ["shell"],
                                       "reason": "no sudo", "command_regex": "(^|[;&|\\n])\\s*sudo\\b"}]},
       required_rules=["no-sudo"]),
]

if __name__ == "__main__":
    OUT.mkdir(exist_ok=True)
    for s in S:
        (OUT / f"{s['id']}.json").write_text(json.dumps(s, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {len(S)} scenarios")
